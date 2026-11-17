"""Сборщик данных. Свечи, OI и фандинг опрашиваются по REST: каждый цикл догружает всё
с последней сохранённой метки, поэтому перезапуск и обрывы сами закрывают пропуски, а
первичные ключи исключают дубли. Ликвидации идут через WebSocket с переподключением."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp

from . import settings
from .sources import FWS, Binance, Coinbase, FearGreed, parse_force_order

log = logging.getLogger(__name__)
MIN = 60_000
HOUR = 3_600_000


def now_ms() -> int:
    return int(time.time() * 1000)


class Health:
    def __init__(self, conn):
        self.conn = conn
        self.last_ok: dict[str, int] = {}
        self.errors: dict[str, str] = {}

    def ok(self, component: str):
        self.last_ok[component] = now_ms()
        self.errors.pop(component, None)

    def fail(self, component: str, err: Exception | str):
        self.errors[component] = str(err)[:300]
        log.warning("%s: %s", component, err)

    def snapshot(self) -> dict:
        t = now_ms()
        return {c: {"age_s": (t - ts) // 1000, "error": self.errors.get(c)}
                for c, ts in sorted(self.last_ok.items())} | \
               {c: {"age_s": None, "error": e} for c, e in self.errors.items() if c not in self.last_ok}

    def write(self):
        t = now_ms()
        rows = [(t, c, "error" if v["error"] else "ok", json.dumps(v)) for c, v in self.snapshot().items()]
        self.conn.executemany("INSERT INTO health VALUES (?,?,?,?)", rows)
        self.conn.execute("DELETE FROM health WHERE ts < ?", (t - 7 * 86_400_000,))


def last_ts(conn, symbol, market, tf) -> int | None:
    r = conn.execute("SELECT MAX(ts) FROM candles WHERE symbol=? AND market=? AND tf=?",
                     (symbol, market, tf)).fetchone()
    return r[0]


def find_gaps(ts_list: list[int], step: int) -> list[tuple[int, int]]:
    """Пропуски в отсортированном ряду меток: [(начало, конец)] отсутствующих баров."""
    gaps = []
    for a, b in zip(ts_list, ts_list[1:]):
        if b - a > step:
            gaps.append((a + step, b - step))
    return gaps


def coverage(conn, symbols: list[str], market: str, start: int, end: int) -> float:
    """Доля присутствующих минутных свечей в окне по всем символам."""
    expected = (end - start) // MIN * len(symbols)
    if not expected:
        return 1.0
    q = ",".join("?" * len(symbols))
    got = conn.execute(
        f"SELECT COUNT(*) FROM candles WHERE market=? AND tf='1m' AND ts>=? AND ts<? AND symbol IN ({q})",
        (market, start, end, *symbols)).fetchone()[0]
    return got / expected


class Collector:
    def __init__(self, cfg, conn, http):
        self.cfg, self.conn = cfg, conn
        self.bn, self.cb, self.fg = Binance(http), Coinbase(http), FearGreed(http)
        self.health = Health(conn)
        self.symbols: list[str] = settings.get(conn, "universe", [])
        self.ws_last_msg = 0

    # ---------- вселенная ----------
    async def refresh_universe(self):
        u = self.cfg["universe"]
        manual = settings.get(self.conn, "symbols_manual", None)
        if manual:
            self.symbols = manual
        else:
            self.symbols = await self.bn.universe(u["top_n"], u["quote"], u.get("exclude", []))
        settings.put(self.conn, "universe", self.symbols)
        self.health.ok("universe")

    # ---------- свечи ----------
    async def sync_klines(self, symbol: str, market: str, tf: str, first_back_ms: int):
        step = MIN if tf == "1m" else HOUR
        start = last_ts(self.conn, symbol, market, tf)
        start = start + step if start else now_ms() - first_back_ms
        while True:
            rows = await self.bn.klines(symbol, market, tf, start_ms=start, limit=1000)
            if not rows:
                break
            self.conn.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  [(symbol, market, tf, *r) for r in rows])
            if len(rows) < 1000:
                break
            start = rows[-1][0] + step

    async def loop_klines(self):
        while True:
            t0 = time.monotonic()
            for sym in list(self.symbols):
                for market in ("futures", "spot"):
                    try:
                        await self.sync_klines(sym, market, "1m", 2 * 86_400_000)
                        await self.sync_klines(sym, market, "1h", 40 * 86_400_000)
                        self.health.ok(f"klines_{market}")
                    except Exception as e:
                        self.health.fail(f"klines_{market}", f"{sym}: {e}")
            await asyncio.sleep(max(5, 60 - (time.monotonic() - t0)))

    # ---------- OI и фандинг ----------
    async def loop_oi_funding(self):
        while True:
            try:
                prem = await self.bn.premium_index()
                t = now_ms()
                syms = set(self.symbols)
                self.conn.executemany("INSERT OR REPLACE INTO funding VALUES (?,?,?)",
                                      [(p["symbol"], t // MIN * MIN, float(p["lastFundingRate"]))
                                       for p in prem if p["symbol"] in syms])
                marks = {p["symbol"]: float(p["markPrice"]) for p in prem}
                self.health.ok("funding")
            except Exception as e:
                self.health.fail("funding", e)
                marks = {}
            for sym in list(self.symbols):
                try:
                    ts, oi = await self.bn.open_interest(sym)
                    self.conn.execute("INSERT OR REPLACE INTO open_interest VALUES (?,?,?,?)",
                                      (sym, ts // MIN * MIN, oi, oi * marks.get(sym, 0)))
                    self.health.ok("open_interest")
                except Exception as e:
                    self.health.fail("open_interest", f"{sym}: {e}")
            await asyncio.sleep(300)

    # ---------- ликвидации ----------
    async def loop_liquidations(self):
        backoff = 1
        while True:
            try:
                session = await self.bn.http.session()
                async with session.ws_connect(FWS, heartbeat=30) as ws:
                    backoff = 1
                    self.health.ok("liquidations_ws")
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        for item in (data if isinstance(data, list) else [data]):
                            row = parse_force_order(item)
                            if row:
                                self.conn.execute("INSERT OR IGNORE INTO liquidations VALUES (?,?,?,?,?,?)", row)
                        self.ws_last_msg = now_ms()
                        self.health.ok("liquidations_ws")
                self.health.fail("liquidations_ws", "closed, reconnecting")
            except Exception as e:
                self.health.fail("liquidations_ws", e)
            self.conn.execute("INSERT INTO gaps (source, symbol, start_ts, end_ts) VALUES (?,?,?,?)",
                              ("liquidations_ws", "*", now_ms(), now_ms()))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    # ---------- премиум Coinbase и страх/жадность ----------
    async def loop_premium(self):
        while True:
            try:
                cb, bn = await asyncio.gather(self.cb.price("BTC-USD"), self.bn.spot_price("BTCUSDT"))
                t = now_ms() // MIN * MIN
                self.conn.execute("INSERT OR REPLACE INTO premium VALUES (?,?,?,?)",
                                  (t, cb, bn, (cb - bn) / bn * 100))
                self.health.ok("premium")
            except Exception as e:
                self.health.fail("premium", e)
            await asyncio.sleep(60)

    async def loop_fear_greed(self):
        while True:
            try:
                rows = await self.fg.history(limit=3)
                self.conn.executemany("INSERT OR REPLACE INTO fear_greed VALUES (?,?)", rows)
                self.health.ok("fear_greed")
            except Exception as e:
                self.health.fail("fear_greed", e)
            await asyncio.sleep(3 * 3600)

    # ---------- пропуски и health ----------
    def detect_gaps(self, window_ms: int = HOUR):
        end = now_ms() // MIN * MIN - 2 * MIN
        start = end - window_ms
        for sym in self.symbols:
            for market in ("futures", "spot"):
                ts = [r[0] for r in self.conn.execute(
                    "SELECT ts FROM candles WHERE symbol=? AND market=? AND tf='1m' AND ts>=? AND ts<=? ORDER BY ts",
                    (sym, market, start - MIN, end))]
                for a, b in find_gaps(ts, MIN):
                    exists = self.conn.execute(
                        "SELECT 1 FROM gaps WHERE source=? AND symbol=? AND start_ts=?",
                        (f"klines_{market}", sym, a)).fetchone()
                    if not exists:
                        self.conn.execute("INSERT INTO gaps (source, symbol, start_ts, end_ts) VALUES (?,?,?,?)",
                                          (f"klines_{market}", sym, a, b))
                        log.warning("gap %s %s %s..%s", market, sym, a, b)

    async def loop_health(self):
        while True:
            await asyncio.sleep(60)
            try:
                self.detect_gaps()
                prune = now_ms() - self.cfg["storage"].get("keep_1m_days", 14) * 86_400_000
                self.conn.execute("DELETE FROM candles WHERE tf='1m' AND ts<?", (prune,))
                self.health.write()
            except Exception as e:
                log.exception("health: %s", e)

    async def loop_universe(self):
        while True:
            try:
                await self.refresh_universe()
            except Exception as e:
                self.health.fail("universe", e)
            await asyncio.sleep(self.cfg["universe"]["refresh_hours"] * 3600)

    def tasks(self) -> list:
        return [self.loop_universe, self.loop_klines, self.loop_oi_funding,
                self.loop_liquidations, self.loop_premium, self.loop_fear_greed, self.loop_health]
