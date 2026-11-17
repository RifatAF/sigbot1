import numpy as np
import pytest

from sigbot import config, db

HOUR = 3_600_000


@pytest.fixture
def cfg():
    c = config.load()
    c["storage"]["db_path"] = ":memory:"
    return c


@pytest.fixture
def conn():
    return db.connect(":memory:")


class FakeTG:
    def __init__(self):
        self.sent = []          # (chat, text, has_photo, markup)
        self.pinned = []

    async def send_message(self, chat, text, reply_markup=None, parse_mode="HTML", **kw):
        self.sent.append((chat, text, False, reply_markup))
        return {"message_id": len(self.sent)}

    async def send_photo(self, chat, png, caption, reply_markup=None, parse_mode="HTML"):
        self.sent.append((chat, caption, True, reply_markup))
        return {"message_id": len(self.sent)}

    async def pin(self, chat, mid):
        self.pinned.append((chat, mid))


@pytest.fixture
def tg():
    return FakeTG()


def put_hourly(conn, symbol, closes, start_ts=1_700_000_000_000 // HOUR * HOUR, oi=None, market_spot=True):
    """Синтетические часовые свечи (и спот) в базу."""
    rows, srows = [], []
    for i, c in enumerate(closes):
        ts = start_ts + i * HOUR
        o = closes[i - 1] if i else c
        h, l = max(o, c) * 1.002, min(o, c) * 0.998
        vol = 1000.0
        tb = 600.0 if c >= o else 400.0
        rows.append((symbol, "futures", "1h", ts, o, h, l, c, vol, tb))
        srows.append((symbol, "spot", "1h", ts, o, h, l, c, vol / 2, tb / 2))
    conn.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    if market_spot:
        conn.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)", srows)
    if oi is not None:
        conn.executemany("INSERT OR REPLACE INTO open_interest VALUES (?,?,?,?)",
                         [(symbol, start_ts + i * HOUR + 30 * 60_000, v, v * closes[i]) for i, v in enumerate(oi)])
    return start_ts


def random_walk(n, seed=0, start=100.0):
    rng = np.random.default_rng(seed)
    return list(start * np.exp(np.cumsum(rng.normal(0, 0.01, n))))
