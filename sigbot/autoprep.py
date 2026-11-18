"""Подготовка при запуске, без ручных команд: докачать историю, если её мало, и пересчитать бэктест,
если его нет или он от старых настроек. Всё в фоне: бот и панель работают сразу, панель показывает статус."""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)
STATUS = {"state": "idle", "text": ""}


def _set(state, text):
    STATUS.update(state=state, text=text, ts=int(time.time()))
    print(("⏳ " if state == "work" else "✅ " if state == "ok" else "⚠️ ") + text, flush=True)


def history_days(conn) -> int:
    r = conn.execute("SELECT MIN(ts), MAX(ts) FROM candles WHERE symbol='BTCUSDT' AND market='futures' AND tf='1h'").fetchone()
    return int((r[1] - r[0]) / 86_400_000) if r and r[0] else 0


def need_backtest(cfg) -> bool:
    files = sorted(glob.glob(f"reports/backtest_{cfg['params_version']}_*.json"))
    return not files or time.time() - Path(files[-1]).stat().st_mtime > 7 * 86400


def _backtest_sync(cfg):
    from . import backtest, db, settings
    from .__main__ import FALLBACK_UNIVERSE
    conn = db.connect(cfg["storage"]["db_path"])
    syms = settings.get(conn, "universe") or FALLBACK_UNIVERSE
    rep = backtest.walk_forward(conn, cfg, syms)
    return backtest.save_report(conn, cfg, rep), rep["total"]["trades"]


def _overview_sync(cfg):
    from . import db, overview
    conn = db.connect(cfg["storage"]["db_path"])
    overview.save(overview.build(conn, cfg))


async def run(cfg, conn, http):
    try:
        need = (date.today() - date.fromisoformat(cfg["backtest"]["start"])).days
        if history_days(conn) < need - 5:
            from .__main__ import _universe
            from .archive import Archive
            syms = await _universe(cfg, conn, http)
            start = date.fromisoformat(cfg["backtest"]["start"]) - timedelta(days=40)
            end = date.today() - timedelta(days=1)
            arc = Archive(http, conn)
            for i, s in enumerate(syms, 1):
                _set("work", f"Докачиваю историю для бэктеста: {s.replace('USDT', '')} ({i}/{len(syms)}), это один раз")
                await arc.symbol_all(s, start, end)
        if need_backtest(cfg):
            _set("work", "Считаю бэктест часовых сигналов (несколько минут)")
            path, n = await asyncio.to_thread(_backtest_sync, cfg)
            log.info("backtest saved %s", path)
        if not Path("reports/overview.json").exists() or time.time() - Path("reports/overview.json").stat().st_mtime > 7 * 86400:
            _set("work", "Считаю сводный бэктест портфеля и листингов для /overview (до 10 минут)")
            await asyncio.to_thread(_overview_sync, cfg)
        _set("ok", "Бэктест готов: на панели кнопка «Бэктест»")
    except Exception as e:
        log.exception("autoprep")
        _set("fail", f"Бэктест не посчитан: {str(e)[:120]}")
