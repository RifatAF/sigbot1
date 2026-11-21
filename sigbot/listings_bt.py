"""Бэктест шорта листингов на всех фьючерсах Binance (включая снятые с торгов).

Дневные свечи всех USDT перпетуалов качаются из data.binance.vision один раз в data/listings_daily.json
(около 5 минут), дальше считается локально. Правила те же, что у живого модуля listings.py."""
from __future__ import annotations

import asyncio
import calendar
import csv
import io
import json
import logging
import re
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
DAY = 86_400_000
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CACHE = Path("data/listings_daily.json")


async def _download(path: Path = CACHE) -> dict:
    import aiohttp
    sem = asyncio.Semaphore(24)

    async def get(sess, url, binary=False):
        async with sem:
            for _ in range(4):
                try:
                    async with sess.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                        if r.status == 404:
                            return None
                        return await (r.read() if binary else r.text())
                except Exception:
                    await asyncio.sleep(2)

    async with aiohttp.ClientSession() as sess:
        syms, marker = [], ""
        while True:
            x = await get(sess, f"{S3}?delimiter=/&prefix=data/futures/um/monthly/klines/" + (f"&marker={marker}" if marker else ""))
            p = re.findall(r"<Prefix>data/futures/um/monthly/klines/([^/]+)/</Prefix>", x or "")
            syms += p
            if not x or "<IsTruncated>true" not in x or not p:
                break
            marker = "data/futures/um/monthly/klines/" + p[-1] + "/"
        syms = sorted({s for s in syms if s.endswith("USDT")})
        out = {}

        async def one(s):
            x = await get(sess, f"{S3}?prefix=data/futures/um/monthly/klines/{s}/1d/")
            rows = []
            for f in re.findall(r"<Key>(data/futures/um/monthly/klines/[^<]+\.zip)</Key>", x or ""):
                b = await get(sess, "https://data.binance.vision/" + f, True)
                if not b:
                    continue
                with zipfile.ZipFile(io.BytesIO(b)) as z:
                    for r in csv.reader(io.StringIO(z.read(z.namelist()[0]).decode())):
                        if r and r[0][:1].isdigit():
                            rows.append([int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7])])
            out[s] = sorted(rows)
        await asyncio.gather(*(one(s) for s in syms))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out))
    log.info("listings archive: %d symbols", len(out))
    return out


def load(refresh: bool = False) -> dict:
    if refresh or not CACHE.exists():
        return asyncio.run(_download())
    return json.loads(CACHE.read_text())


def backtest(D: dict, lc: dict, fee_pct: float, start_ms: int, end_ms: int, exclude=()) -> tuple[pd.Series, list[dict]]:
    """Дневной результат портфеля шортов (доля капитала) и список сделок."""
    days = np.arange(start_ms // DAY * DAY, end_ms, DAY)
    pnl = pd.Series(0.0, index=days)
    trades = []
    n = lc["notional_pct"] / 100
    stop_k = 1 + lc["stop_pct"] / 100
    for s, v in D.items():
        if len(v) < 3 or s in ("BTCUSDT", "ETHUSDT") or s in exclude:
            continue
        a = np.array(v)
        t, o, h, c = a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 4]
        if t[0] < start_ms or t[1] + DAY > end_ms:
            continue
        if lc.get("require_weak_first_day", True) and not c[1] < o[0]:
            continue
        e = c[1]
        trig = e * stop_k
        prev = e
        reason, exit_px, exit_t = "time", None, None
        last = min(1 + lc["hold_days"], len(t) - 1)
        for j in range(2, last + 1):
            stopped = h[j] >= trig
            px = trig * 1.005 if stopped else c[j]           # стоп-маркет, проскальзывание 0.5% (по минуткам)
            r = -(px - prev) / e
            if j == 2:
                r -= fee_pct / 100
            if stopped or j == last:
                r -= fee_pct / 100
            d = int(t[j]) // DAY * DAY
            if d in pnl.index:
                pnl[d] += r * n
            prev = px
            if stopped:
                reason, exit_px, exit_t = "stop", px, int(t[j]) + DAY
                break
        if exit_px is None:
            exit_px, exit_t = c[last], int(t[last]) + DAY
            if last == 1 + lc["hold_days"]:
                reason = "time"
            else:
                reason = "open" if int(t[last]) + 3 * DAY >= end_ms else "delisted"   # открыта на конец периода
        trades.append({"symbol": s, "entry_t": int(t[1]) + DAY, "entry": float(e), "exit_t": exit_t,
                       "exit": float(exit_px), "reason": reason,
                       "pnl_pct": round((-(exit_px / e - 1) * 100 - 2 * fee_pct) * n, 3)})
    return pnl, trades


BYBIT_CACHE = Path("data/listings_bybit.json")
BYBIT_REPORT = Path("reports/listings_bybit.json")


async def _download_bybit(binance_first: dict) -> dict:
    """Дневные свечи первых 100 дней всех перпетуалов Bybit, которых не было на Binance к их запуску на Bybit."""
    from .http import Http
    from .sources import Binance, Bybit
    http = Http()
    try:
        bb = Bybit(http)
        try:
            for s, t in await Binance(http).perp_listings():        # свежие листинги Binance, которых ещё нет в архиве
                binance_first.setdefault(s, t)
        except Exception:
            pass
        sem, out = asyncio.Semaphore(8), {}

        async def one(sym, launch):
            if launch <= 0 or (sym in binance_first and binance_first[sym] <= launch + 2 * DAY):
                return
            async with sem:
                try:
                    k = await bb.klines(sym, "futures", "1d", launch // DAY * DAY, 100)
                except Exception as e:
                    log.info("bybit %s: %s", sym, e)
                    return
            if k:
                out[sym] = {"launch": launch, "rows": [[t, o, h, l, c, v] for t, o, h, l, c, v, _ in k]}
        await asyncio.gather(*(one(s, t) for s, t in await bb.perp_listings()))
        return out
    finally:
        await http.close()


def bybit_summary(trades: list[dict]) -> dict:
    done = [t for t in trades if t["reason"] != "open"]
    res = [t["pnl_pct"] for t in done]
    years: dict = {}
    for t in done:
        y = time.strftime("%Y", time.gmtime(t["exit_t"] / 1000))
        years[y] = round(years.get(y, 0) + t["pnl_pct"], 1)
    return {"trades": len(trades), "closed": len(done), "win_pct": round(100 * sum(r > 0 for r in res) / len(res)) if res else 0,
            "avg_pct": round(float(np.mean(res)), 2) if res else 0, "sum_pct": round(float(np.sum(res)), 1),
            "stops": sum(t["reason"] == "stop" for t in done), "years": years}


def bybit_report(cfg) -> dict:
    """Бэктест шорта листингов Bybit теми же правилами. Bybit отдаёт только монеты, что торгуются сейчас:
    делистнутые (обычно упавшие) не попадают, значит результат скорее занижен."""
    D = load()
    first = {s: int(v[0][0]) for s, v in D.items() if v}
    B = asyncio.run(_download_bybit(first))
    BYBIT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    BYBIT_CACHE.write_text(json.dumps(B))
    lc = cfg["listings"]
    _, trades = backtest({s: x["rows"] for s, x in B.items()}, lc, cfg["trade"]["fee_taker_pct"],
                         calendar.timegm(time.strptime(cfg["backtest"]["start"], "%Y-%m-%d")) * 1000, int(time.time() * 1000))
    for t in trades:
        t["exchange"] = "bybit"
    rep = {"generated": int(time.time()), "coins": len(B), "summary": bybit_summary(trades),
           "trades": sorted(trades, key=lambda x: -x["entry_t"])}
    BYBIT_REPORT.parent.mkdir(exist_ok=True)
    BYBIT_REPORT.write_text(json.dumps(rep))
    return rep
