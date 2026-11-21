"""Цели и стопы как на бирже: срабатывают внутри часа и внутри суток, недокачанные свечи заменяются."""
import json

import pytest

from sigbot import feed, portfolio, settings
from sigbot.collector import Collector
from sigbot.listings import ListingShorts
from sigbot.publisher import Publisher

MIN, HOUR, DAY = 60_000, 3_600_000, 86_400_000


class GrowingBN:
    """Биржа отдаёт текущую свечу недосформированной: максимум растёт, пока час не закрыт."""

    def __init__(self):
        self.now = 0

    async def klines(self, symbol, market, interval, start_ms=None, limit=1000):
        step = MIN if interval == "1m" else HOUR
        out, t = [], -(-start_ms // step) * step
        while t <= self.now and len(out) < limit:
            closed = t + step <= self.now
            out.append((t, 100.0, 110.0 if closed else 101.0, 99.0, 100.0, 1.0, 0.5))
            t += step
        return out


@pytest.mark.asyncio
async def test_unclosed_candle_is_replaced(cfg, conn):
    import sigbot.collector as cm
    col = Collector(cfg, conn, http=None)
    col.bn = GrowingBN()
    base = 1_800_000_000_000 // HOUR * HOUR
    orig = cm.now_ms
    try:
        col.bn.now = base + 30_000                                   # первые 30 секунд часа
        cm.now_ms = lambda: col.bn.now
        await col.sync_klines("BTCUSDT", "futures", "1h", 2 * HOUR)
        assert conn.execute("SELECT high FROM candles WHERE ts=?", (base,)).fetchone()[0] == 101.0
        col.bn.now = base + HOUR + 30_000                            # час закрылся
        await col.sync_klines("BTCUSDT", "futures", "1h", 2 * HOUR)
        assert conn.execute("SELECT high FROM candles WHERE ts=?", (base,)).fetchone()[0] == 110.0
    finally:
        cm.now_ms = orig


def _signal(conn, cfg, entry=100.0, side="short", open_day=20_000 * DAY):
    lv = feed.levels(entry, 2.0, 1 if side == "long" else -1, feed.plan_cfg(cfg["portfolio"]))
    st = feed.new_state(side, entry, lv)
    st["last_ts"] = open_day + DAY
    conn.execute("INSERT INTO trend_signals (symbol, side, open_day, entry, size_pct, stop, tp1, tp2, state) "
                 "VALUES ('BTCUSDT', ?, ?, ?, 20, ?, ?, ?, ?)",
                 (side, open_day, entry, lv["stop"], lv["tp1"], lv["tp2"], json.dumps(st)))
    return lv, open_day + DAY


@pytest.mark.asyncio
async def test_trend_tp1_inside_unclosed_hour(cfg, conn, tg):
    lv, t0 = _signal(conn, cfg)
    rows = [("BTCUSDT", "futures", "1m", t0 + i * MIN, 100, 100.5, 99.5, 100, 1, 0.5) for i in range(20)]
    rows.append(("BTCUSDT", "futures", "1m", t0 + 20 * MIN, 100, 100, lv["tp1"] - 0.1, lv["tp1"], 1, 0.5))
    conn.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    lp = portfolio.LivePortfolio(cfg, conn, Publisher(cfg, conn, tg, -100, 7))
    await lp.manage(t0 + 22 * MIN)                                  # час ещё не закрыт
    assert any("цель 1" in x[1] for x in tg.sent if x[0] == -100)
    st = json.loads(conn.execute("SELECT state FROM trend_signals").fetchone()[0])
    assert [x["what"] for x in st["exits"]] == ["tp1"]
    await lp.manage(t0 + 23 * MIN)                                  # не повторяется
    assert sum("цель 1" in x[1] for x in tg.sent if x[0] == -100) == 1


@pytest.mark.asyncio
async def test_missed_tp1_found_after_repair(cfg, conn, tg):
    """Старые недокачанные свечи не показали TP1; после перекачки сигнал проходится с входа и пост уходит."""
    lv, t0 = _signal(conn, cfg)
    conn.execute("INSERT INTO candles VALUES ('BTCUSDT','futures','1h',?,100,100.2,99.8,100,1,0.5)", (t0,))
    lp = portfolio.LivePortfolio(cfg, conn, Publisher(cfg, conn, tg, -100, 7))
    await lp.manage(t0 + 5 * HOUR)
    assert not any("цель 1" in x[1] for x in tg.sent)
    conn.execute("UPDATE candles SET low=? WHERE ts=?", (lv["tp1"] - 0.5, t0))   # перекачанная полная свеча
    settings.put(conn, "klines_repaired_v1", 1)
    await lp.manage(t0 + 5 * HOUR)
    sent = [x[1] for x in tg.sent if x[0] == -100 and "цель 1" in x[1]]
    assert len(sent) == 1 and "с опозданием" in sent[0]
    await lp.manage(t0 + 6 * HOUR)
    assert sum("цель 1" in x[1] for x in tg.sent if x[0] == -100) == 1


class BN:
    def __init__(self, bars):
        self.bars = bars

    async def perp_listings(self):
        return []

    async def klines(self, sym, market, interval, start, limit):
        return [b for b in self.bars if b[0] >= start][:limit]


@pytest.mark.asyncio
async def test_listing_stop_inside_unclosed_day(cfg, conn, tg):
    d0 = 20_000
    conn.execute("INSERT INTO listing_trades (symbol, onboard_ts, entry_ts, entry, stop, exit_due_ts, notional_pct, status) "
                 "VALUES ('NEWUSDT', ?, ?, 1.0, 1.5, ?, 10, 'open')", (d0 * DAY, (d0 + 1) * DAY, (d0 + 61) * DAY))
    bn = BN([((d0 + 1) * DAY, 1.0, 1.1, 0.9, 1.0, 1, 0.5), ((d0 + 2) * DAY, 1.0, 1.6, 1.0, 1.4, 1, 0.5)])
    ls = ListingShorts(cfg, conn, Publisher(cfg, conn, tg, -100, 7), bn)
    now = (d0 + 2) * DAY + 5 * HOUR                                 # сутки ещё идут
    assert await ls.manage(now) == ["sent"]
    r = conn.execute("SELECT status, exit_reason, exit_price, exit_ts FROM listing_trades").fetchone()
    assert r[0] == "closed" and r[1] == "stop" and r[2] == 1.5 and r[3] == now
