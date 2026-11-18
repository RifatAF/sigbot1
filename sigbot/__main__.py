"""Точка входа: python -m sigbot <команда>."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, timedelta

from . import config, db, ledger, settings

# бывшие крупные монеты, часть уже снята с торгов: нужны для честного бэктеста портфеля с 2020 года
HISTORY_EXTRA = [
    "LUNAUSDT", "FTTUSDT", "EOSUSDT", "XTZUSDT", "MATICUSDT", "THETAUSDT", "ALGOUSDT", "AXSUSDT", "SANDUSDT",
    "MANAUSDT", "ICPUSDT", "FTMUSDT", "GALAUSDT", "CHZUSDT", "XMRUSDT", "ZECUSDT", "DASHUSDT", "SUSHIUSDT",
    "COMPUSDT", "MKRUSDT", "CRVUSDT", "EGLDUSDT", "KSMUSDT", "1INCHUSDT", "SNXUSDT", "YFIUSDT", "WAVESUSDT",
    "ZILUSDT", "GMTUSDT", "APEUSDT",
]

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
        if args.long:          # длинная история для сводного бэктеста: с 2020 года и с умершими монетами
            syms = list(dict.fromkeys(syms + FALLBACK_UNIVERSE + HISTORY_EXTRA))
            args.start = args.start or "2020-01-01"
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


async def cmd_check(cfg, conn, args):
    from . import diag
    from .http import Http
    http = Http()
    try:
        print(diag.format_check(await diag.selfcheck(cfg, conn, http)))
    finally:
        await http.close()


async def cmd_testpost(cfg, conn, args):
    import os
    from . import diag
    from .http import Http
    from .telegram import Telegram
    http = Http()
    try:
        tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"], http)
        to = os.environ["TELEGRAM_OWNER_ID"] if args.me else os.environ["TELEGRAM_CHANNEL_ID"]
        n = await diag.test_posts(cfg, conn, tg, to)
        print(f"отправлено тестовых постов {'в личку' if args.me else 'в канал'}: {n}")
    finally:
        await http.close()


def cmd_setup(cfg, conn, args):
    """Интерактивно создать или дополнить .env. Уже заполненные значения остаются, пустой ввод их не меняет."""
    import getpass
    import socket
    from pathlib import Path
    p = Path(".env")
    cur = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                cur[k.strip()] = v.strip()
    fields = [
        ("TELEGRAM_BOT_TOKEN", "Токен Telegram бота", "", True),
        ("GEMINI_API_KEY", "Ключ Gemini", "", True),
        ("TELEGRAM_CHANNEL_ID", "ID канала", "-1003595508596", False),
        ("TELEGRAM_OWNER_ID", "Твой ID в Telegram", "569798955", False),
        ("SIGBOT_NAME", "Имя этого компьютера (pc, laptop)", socket.gethostname().lower(), False),
        ("BINANCE_DEMO_KEY", "Демо-ключ Binance (demo.binance.com, можно пропустить Enter)", "", True),
        ("BINANCE_DEMO_SECRET", "Демо-секрет Binance", "", True),
        ("BYBIT_DEMO_KEY", "Демо-ключ Bybit (режим Demo Trading, можно пропустить)", "", True),
        ("BYBIT_DEMO_SECRET", "Демо-секрет Bybit", "", True),
    ]
    for key, label, default, secret in fields:
        have = cur.get(key) or default
        shown = ("задан" if cur.get(key) else "пусто") if secret else (have or "пусто")
        ask = getpass.getpass if secret else input
        v = ask(f"{label} [{shown}]: ").strip()
        cur[key] = v or have
    for k in [k for k, v in cur.items() if not v and "DEMO" not in k]:
        print(f"внимание: {k} пустой")
    p.write_text("".join(f"{k}={v}\n" for k, v in cur.items()), encoding="utf-8")
    print(f".env записан: {p.resolve()}")


def cmd_report(cfg, conn, args):
    from . import overview
    rep = overview.build(conn, cfg, refresh=args.refresh)
    path = overview.save(rep)
    print(overview.to_text(rep))
    print(f"\nотчёт: {path}; графики на панели: http://127.0.0.1:{cfg['dashboard']['port']}/overview")


def cmd_portfolio(cfg, conn, args):
    from pathlib import Path
    from . import portfolio
    r = portfolio.backtest(conn, cfg, args.start or "2020-07-01", args.end or "2026-11-01")
    md = portfolio.to_markdown(r)
    Path("reports").mkdir(exist_ok=True)
    out = Path("reports") / f"portfolio_{args.start or '2020-07-01'}_{args.end or '2026-11-01'}.md"
    out.write_text(md)
    print(md)
    print(f"report: {out}")


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
    import os
    if not os.environ.get("SIGBOT_CHILD") and os.environ.get("AUTO_UPDATE", "1") != "0":
        from . import updater           # сторож: бот в дочернем процессе, обновления ставятся сами
        conn.close()
        raise SystemExit(updater.supervise())
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
    b.add_argument("--long", action="store_true", help="история с 2020 года, 60 монет с умершими (для report)")
    t = sub.add_parser("backtest", help="walk forward бэктест и отчёт в reports/")
    t.add_argument("--symbols")
    t.add_argument("--start")
    t.add_argument("--end")
    sub.add_parser("setup", help="создать или дополнить .env")
    sub.add_parser("check", help="самопроверка всех частей бота")
    tp = sub.add_parser("testpost", help="все виды постов с пометкой TEST в канал")
    tp.add_argument("--me", action="store_true", help="только владельцу в личку")
    rp = sub.add_parser("report", help="сводный бэктест всех блоков с графиками на панели")
    rp.add_argument("--refresh", action="store_true", help="заново скачать дневные свечи всех фьючерсов")
    pf = sub.add_parser("portfolio", help="бэктест трендового портфеля")
    pf.add_argument("--start")
    pf.add_argument("--end")
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
    elif args.cmd == "check":
        asyncio.run(cmd_check(cfg, conn, args))
    elif args.cmd == "testpost":
        asyncio.run(cmd_testpost(cfg, conn, args))
    elif args.cmd == "setup":
        cmd_setup(cfg, conn, args)
    elif args.cmd == "report":
        cmd_report(cfg, conn, args)
    elif args.cmd == "portfolio":
        cmd_portfolio(cfg, conn, args)
    elif args.cmd == "export":
        cmd_export(cfg, conn, args)
    elif args.cmd == "health":
        cmd_health(cfg, conn, args)
    elif args.cmd == "run":
        cmd_run(cfg, conn, args)


if __name__ == "__main__":
    main()
