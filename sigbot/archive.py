"""История из data.binance.vision для бэктеста и первичной загрузки.

Есть: свечи фьючерсов и спота (с объёмом покупок тейкеров, отсюда CVD), метрики OI (5 мин),
фандинг. Истории ликвидаций нет: она копится только из собственного потока.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import zipfile
from datetime import date, timedelta

from .http import Http

log = logging.getLogger(__name__)
BASE = "https://data.binance.vision/data"


def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _rows(blob: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = z.namelist()[0]
        text = z.read(name).decode()
    rows = list(csv.reader(io.StringIO(text)))
    if rows and not rows[0][0][:1].isdigit():
        rows = rows[1:]  # заголовок
    return rows


def _ts_ms(v: str) -> int:
    t = int(v)
    return t // 1000 if t > 10**14 else t  # спот с 2025 года в микросекундах


def parse_kline_rows(rows) -> list[tuple]:
    return [(_ts_ms(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[9]))
            for r in rows]


def parse_metrics_rows(rows) -> list[tuple]:
    from datetime import datetime, timezone
    out = []
    for r in rows:
        ts = int(datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
        out.append((ts, float(r[2]), float(r[3])))
    return out


def parse_funding_rows(rows) -> list[tuple]:
    return [(int(r[0]) // 1000 * 1000, float(r[2])) for r in rows]


class Archive:
    def __init__(self, http: Http, conn, concurrency: int = 8):
        self.http, self.conn = http, conn
        self.sem = asyncio.Semaphore(concurrency)

    async def _get(self, url: str):
        async with self.sem:
            blob = await self.http.get_bytes(url)
        return _rows(blob) if blob else None

    async def klines(self, symbol: str, market: str, tf: str, start: date, end: date):
        seg = "futures/um" if market == "futures" else "spot"
        n = 0
        for y, m in _months(start, end):
            url = f"{BASE}/{seg}/monthly/klines/{symbol}/{tf}/{symbol}-{tf}-{y}-{m:02d}.zip"
            rows = await self._get(url)
            if rows is None:  # текущий месяц лежит только дневными файлами
                rows = []
                d = date(y, m, 1)
                while d.month == m and d <= end:
                    r = await self._get(f"{BASE}/{seg}/daily/klines/{symbol}/{tf}/{symbol}-{tf}-{d}.zip")
                    rows += r or []
                    d += timedelta(days=1)
            data = parse_kline_rows(rows)
            self.conn.executemany(
                "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(symbol, market, tf, *k) for k in data])
            n += len(data)
        return n

    async def metrics(self, symbol: str, start: date, end: date):
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

        async def one(d):
            return await self._get(f"{BASE}/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{d}.zip")

        n = 0
        for chunk in range(0, len(days), 32):
            res = await asyncio.gather(*(one(d) for d in days[chunk:chunk + 32]))
            data = [x for rows in res if rows for x in parse_metrics_rows(rows)]
            self.conn.executemany("INSERT OR REPLACE INTO open_interest VALUES (?,?,?,?)",
                                  [(symbol, *x) for x in data])
            n += len(data)
        return n

    async def funding(self, symbol: str, start: date, end: date):
        n = 0
        for y, m in _months(start, end):
            rows = await self._get(
                f"{BASE}/futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{y}-{m:02d}.zip")
            if not rows:
                continue
            data = parse_funding_rows(rows)
            self.conn.executemany("INSERT OR REPLACE INTO funding VALUES (?,?,?)", [(symbol, *x) for x in data])
            n += len(data)
        return n

    async def symbol_all(self, symbol: str, start: date, end: date):
        r = await asyncio.gather(
            self.klines(symbol, "futures", "1h", start, end),
            self.klines(symbol, "spot", "1h", start, end),
            self.metrics(symbol, start, end),
            self.funding(symbol, start, end),
        )
        log.info("%s archive: fut=%d spot=%d oi=%d funding=%d", symbol, *r)
        return r
