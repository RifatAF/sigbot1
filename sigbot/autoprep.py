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
STATUS = {"state": "idle", "text": "", "pct": 0, "warn": []}


def _set(state, text, pct=None, quiet=False):
    """state work/ok/fail; pct общий прогресс 0..100; предупреждения копятся отдельно и не затираются этапами."""
    if state == "warn":
        STATUS["warn"].append(text)
        print("⚠️ " + text, flush=True)
        return
    if pct is not None:
        STATUS["pct"] = int(pct)
    STATUS.update(state=state, text=text, ts=int(time.time()))
    p = STATUS["pct"]
    if quiet and time.time() - STATUS.get("printed", 0) < 30:   # в консоль не чаще раза в 30 секунд
        return
    STATUS["printed"] = time.time()
    bar = "█" * (p // 5) + "░" * (20 - p // 5)
    print(("⏳ " if state == "work" else "✅ " if state == "ok" else "❌ ") + f"[{bar}] {p}%  {text}", flush=True)


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


def _feed_sync(cfg):
    from . import db, feed
    conn = db.connect(cfg["storage"]["db_path"])
    feed.save(feed.build(conn, cfg))


async def run(cfg, conn, http):
    STATUS.update(warn=[], pct=0)
    _set("work", "Проверяю полноту истории по монетам", 1)
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
                arc, end_d = Archive(http, conn), date.today() - timedelta(days=1)
                exp = Archive.expected_files(t0, end_d)
                job = asyncio.create_task(arc.symbol_all(s, t0, end_d))
                while not job.done():               # полоса двигается по мере скачивания файлов
                    k = min(arc.done / exp, 1)
                    _set("work", f"История {name} ({i}/{len(todo)}): архив Binance, файлов {arc.done} из ~{exp}",
                         3 + 67 * (i - 1 + 0.8 * k) / len(todo), quiet=True)
                    await asyncio.wait({job}, timeout=2)
                await job
            _set("work", f"История {name} ({i}/{len(todo)}): докачиваю пропуски через API", 3 + 67 * (i - .4) / len(todo))
            await fill(conn, bn, s, start, now)
        bad = [s.replace("USDT", "") for s in syms if not ok(s)]
        if bad:
            _set("warn", f"Неполная история: {', '.join(bad[:10])}. Проверь интернет и перезапусти бота")
        no_oi = [s.replace("USDT", "") for s in syms if not conn.execute(
            "SELECT 1 FROM open_interest WHERE symbol=? AND ts<? LIMIT 1", (s, now - 60 * 86_400_000)).fetchone()]
        if len(no_oi) > len(syms) // 3:
            _set("warn", f"Архив data.binance.vision не открывается: нет истории OI по {len(no_oi)} монетам, "
                         "сигналов в бэктесте будет меньше. Включи VPN и перезапусти бота")
        try:
            from . import settings as _st
            _st.put(conn, "perp_kinds", await bn.perp_kinds())
        except Exception:
            pass
        if changed or need_backtest(cfg):
            _set("work", "Считаю бэктест часовых сигналов (около минуты)", 72)
            path, n = await asyncio.to_thread(_backtest_sync, cfg)
            log.info("backtest saved %s", path)
        ov = Path("reports/overview.json")
        if changed or not ov.exists() or time.time() - ov.stat().st_mtime > 7 * 86400:
            _set("work", "Считаю сводный бэктест портфеля и листингов для /overview (до 10 минут)", 85)
            try:
                await asyncio.to_thread(_overview_sync, cfg)
            except Exception as e:
                log.exception("overview")
                _set("warn", f"Сводный отчёт /overview не посчитан ({str(e)[:80]}), бэктест сигналов на главной есть")
        _set("work", "Собираю ленту сигналов", 97)
        try:
            await asyncio.to_thread(_feed_sync, cfg)
        except Exception as e:
            log.exception("feed")
            _set("warn", f"Лента сигналов не собрана: {str(e)[:100]}")
        _set("ok", "Готово: история загружена, бэктест посчитан" + (" (есть предупреждения)" if STATUS["warn"] else ""), 100)
    except Exception as e:
        log.exception("autoprep")
        _set("fail", f"Бэктест не посчитан: {str(e)[:120]}")

