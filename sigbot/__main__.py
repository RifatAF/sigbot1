"""Точка входа: python -m sigbot <команда>."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, timedelta

from . import config, db, ledger, settings

FALLBACK_UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT",
    "SUIUSDT", "LTCUSDT", "DOTUSDT", "BCHUSDT", "TRXUSDT", "NEARUSDT", "APTUSDT", "AAVEUSDT", "UNIUSDT",
    "ARBUSDT", "OPUSDT", "FILUSDT", "ETCUSDT", "ATOMUSDT", "INJUSDT", "TIAUSDT", "WLDUSDT", "SEIUSDT",
    "HBARUSDT", "XLMUSDT", "ENAUSDT",
]


def _load_env(path=".env"):
    import os
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


async def _universe(cfg, conn, http) -> list[str]:
    from .sources import Binance
    syms = settings.get(conn, "symbols_manual") or settings.get(conn, "universe")
    if syms:
        return syms
    try:
        syms = await Binance(http).universe(cfg["universe"]["top_n"], cfg["universe"]["quote"],
                                            cfg["universe"].get("exclude", []))
    except Exception as e:
        logging.warning("universe from API failed (%s), using fallback list", e)
        syms = FALLBACK_UNIVERSE[: cfg["universe"]["top_n"]]
    settings.put(conn, "universe", syms)
    return syms


async def cmd_backfill(cfg, conn, args):
    from .archive import Archive
    from .http import Http
    from .sources import FearGreed
    http = Http()
    try:
        syms = args.symbols.split(",") if args.symbols else await _universe(cfg, conn, http)
        start = date.fromisoformat(args.start or cfg["backtest"]["start"]) - timedelta(days=40)
        end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
        arc = Archive(http, conn)
        for s in syms:
            await arc.symbol_all(s, start, end)
        rows = await FearGreed(http).history(limit=0)
        conn.executemany("INSERT OR REPLACE INTO fear_greed VALUES (?,?)", rows)
        print(f"backfill done: {len(syms)} symbols, {start}..{end}")
    finally:
        await http.close()


def cmd_backtest(cfg, conn, args):
    from . import backtest
    syms = args.symbols.split(",") if args.symbols else settings.get(conn, "universe") or FALLBACK_UNIVERSE
    if args.start:
        cfg["backtest"]["start"] = args.start
    if args.end:
        cfg["backtest"]["end"] = args.end
    rep = backtest.walk_forward(conn, cfg, syms)
    path = backtest.save_report(conn, cfg, rep)
    print(path.read_text())
    print(f"report: {path}")


def cmd_export(cfg, conn, args):
    for p in ledger.export_csv(conn, args.out):
        print(p)


def cmd_health(cfg, conn, args):
    import time
    t = int(time.time() * 1000)
    rows = conn.execute("SELECT component, status, detail, MAX(ts) FROM health GROUP BY component").fetchall()
    for r in rows:
        print(f"{r[0]:18} {r[1]:6} {r[2]}")
    day = conn.execute("SELECT COUNT(*), COALESCE(SUM(end_ts - start_ts + 60000), 0) FROM gaps "
                       "WHERE source LIKE 'klines%' AND start_ts > ?", (t - 3 * 86_400_000,)).fetchone()
    syms = settings.get(conn, "universe", [])
    from .collector import coverage
    for m in ("futures", "spot"):
        if syms:
            print(f"coverage 1m {m} 72h: {coverage(conn, syms, m, t - 3 * 86_400_000, t - 120_000) * 100:.2f}%")
    print(f"gaps 72h: {day[0]}")


def cmd_run(cfg, conn, args):
    from .app import run
    asyncio.run(run(cfg, conn))


def main(argv=None):
    _load_env()
    ap = argparse.ArgumentParser(prog="sigbot")
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="загрузить историю из data.binance.vision")
    b.add_argument("--symbols")
    b.add_argument("--start")
    b.add_argument("--end")
    t = sub.add_parser("backtest", help="walk forward бэктест и отчёт в reports/")
    t.add_argument("--symbols")
    t.add_argument("--start")
    t.add_argument("--end")
    e = sub.add_parser("export", help="журнал в CSV")
    e.add_argument("--out", default="export")
    sub.add_parser("health", help="состояние сборщика")
    sub.add_parser("run", help="запустить бота")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load(args.config)
    conn = db.connect(cfg["storage"]["db_path"])
    if args.cmd == "backfill":
        asyncio.run(cmd_backfill(cfg, conn, args))
    elif args.cmd == "backtest":
        cmd_backtest(cfg, conn, args)
    elif args.cmd == "export":
        cmd_export(cfg, conn, args)
    elif args.cmd == "health":
        cmd_health(cfg, conn, args)
    elif args.cmd == "run":
        cmd_run(cfg, conn, args)


if __name__ == "__main__":
    main()
