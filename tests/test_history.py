import asyncio

from sigbot import db
from sigbot.history import HOUR, coverage, fill, missing_ranges


def test_missing_ranges():
    ts = [0, HOUR, 5 * HOUR, 6 * HOUR]
    assert missing_ranges(ts, 0, 10 * HOUR) == [(2 * HOUR, 4 * HOUR), (7 * HOUR, 9 * HOUR)]
    assert missing_ranges([], 0, 3 * HOUR) == [(0, 2 * HOUR)]


class FakeBN:
    def __init__(self, end):
        self.end, self.calls = end, 0

    async def klines(self, symbol, market, tf, start_ms, limit):
        self.calls += 1
        return [(t, 1.0, 2.0, 0.5, 1.5, 10.0, 5.0) for t in range(start_ms, min(self.end, start_ms + limit * HOUR), HOUR)]

    async def funding_hist(self, symbol, start_ms):
        return [(t, 0.0001) for t in range(start_ms, min(self.end, start_ms + 1000 * 8 * HOUR), 8 * HOUR)]


def test_fill_closes_holes():
    conn = db.connect(":memory:")
    end = 5000 * HOUR
    conn.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)",
                     [("BTCUSDT", "futures", "1h", t, 1, 2, .5, 1.5, 10, 5) for t in range(4000 * HOUR, end, HOUR)])
    assert coverage(conn, "BTCUSDT", 0, end) < 0.3
    fut, spot, fr = asyncio.run(fill(conn, FakeBN(end), "BTCUSDT", 0, end))
    assert coverage(conn, "BTCUSDT", 0, end) == 1.0 and coverage(conn, "BTCUSDT", 0, end, "spot") == 1.0
    assert fut == 4000 and fr > 600
