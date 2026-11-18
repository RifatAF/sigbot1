"""Подготовка при запуске, без ручных команд: докачать историю, если её мало, и пересчитать бэктест,
если его нет или он от старых настроек. Всё в фоне: бот и панель работают сразу, панель показывает статус."""
from __future__ import annotations

import asyncio
import calendar
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
        from .__main__ import FALLBACK_UNIVERSE, _universe
        from .archive import Archive
        from .history import coverage, fill
        from .sources import Binance
        bn = Binance(http)
        t0 = date.fromisoformat(cfg["backtest"]["start"]) - timedelta(days=200)   # запас под дневной портфель
        start = calendar.timegm(t0.timetuple()) * 1000
        now = int(time.time() * 1000) // 3_600_000 * 3_600_000
        syms = list(dict.fromkeys(["BTCUSDT"] + (await _universe(cfg, conn, http) or FALLBACK_UNIVERSE)))
        try:
            listed = dict(await bn.perp_listings())          # дата листинга: молодые монеты не считаются дырявыми
        except Exception:
            listed = {}
        ok = lambda s: coverage(conn, s, max(start, listed.get(s, start)), now) >= 0.97
        todo = [s for s in syms if not ok(s)]
        changed = bool(todo)
        for i, s in enumerate(todo, 1):
            name = s.replace("USDT", "")
            if not conn.execute("SELECT 1 FROM open_interest WHERE symbol=? AND ts<? LIMIT 1", (s, now - 60 * 86_400_000)).fetchone():
                _set("work", f"История {name} ({i}/{len(todo)}): архив Binance (OI, фандинг, свечи)")
                await Archive(http, conn).symbol_all(s, t0, date.today() - timedelta(days=1))
            _set("work", f"История {name} ({i}/{len(todo)}): докачиваю пропуски через API")
            await fill(conn, bn, s, start, now)
        bad = [s.replace("USDT", "") for s in syms if not ok(s)]
        if bad:
            _set("fail", f"Неполная история: {', '.join(bad[:10])}. Проверь интернет и перезапусти бота")
        no_oi = [s.replace("USDT", "") for s in syms if not conn.execute(
            "SELECT 1 FROM open_interest WHERE symbol=? AND ts<? LIMIT 1", (s, now - 60 * 86_400_000)).fetchone()]
        if len(no_oi) > len(syms) // 3:
            _set("fail", f"Архив data.binance.vision не открывается: нет истории OI по {len(no_oi)} монетам, "
                         "сигналов в бэктесте будет меньше. Включи VPN и перезапусти бота")
        if changed or need_backtest(cfg):
            _set("work", "Считаю бэктест часовых сигналов (около минуты)")
            path, n = await asyncio.to_thread(_backtest_sync, cfg)
            log.info("backtest saved %s", path)
        ov = Path("reports/overview.json")
        if changed or not ov.exists() or time.time() - ov.stat().st_mtime > 7 * 86400:
            _set("work", "Считаю сводный бэктест портфеля и листингов для /overview (до 10 минут)")
            try:
                await asyncio.to_thread(_overview_sync, cfg)
            except Exception as e:
                log.exception("overview")
                _set("fail", f"Сводный отчёт /overview не посчитан ({str(e)[:80]}), бэктест сигналов на главной есть")
                return
        if not bad:
            _set("ok", "История полная, бэктест готов: на панели кнопка «Бэктест»")
    except Exception as e:
        log.exception("autoprep")
        _set("fail", f"Бэктест не посчитан: {str(e)[:120]}")

