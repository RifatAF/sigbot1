import pytest

from sigbot.listings import ListingShorts
from sigbot.publisher import Publisher

DAY = 86_400_000


class FakeBN:
    def __init__(self, onboard, bars):
        self.onboard, self.bars = onboard, bars

    async def perp_listings(self):
        return [("NEWUSDT", self.onboard), ("BTCUSDT", 1)]

    async def klines(self, sym, market, interval, start, limit):
        return [b for b in self.bars if b[0] >= start][:limit]


def bar(day, o, h, l, c):
    return (day * DAY, o, h, l, c, 1.0, 0.5)


@pytest.mark.asyncio
async def test_listing_short_entry_and_stop(cfg, conn, tg):
    d0 = 20_000
    onboard = d0 * DAY + 8 * 3_600_000
    bars = [bar(d0, 1.0, 1.2, 0.9, 1.0), bar(d0 + 1, 1.0, 1.1, 0.95, 1.0)]
    bn = FakeBN(onboard, bars)
    ls = ListingShorts(cfg, conn, Publisher(cfg, conn, tg, -100, 7), bn)
    assert await ls.run_daily((d0 + 2) * DAY + 60_000) == ["sent"]          # вход на закрытии первых полных суток
    t = ls.open_trades()[0]
    assert t["entry"] == 1.0 and t["stop"] == pytest.approx(1.5) and "NEW SHORT" in tg.sent[-1][1]
    bars.append(bar(d0 + 2, 1.0, 1.2, 0.9, 1.1))
    bars.append(bar(d0 + 3, 1.1, 1.6, 1.0, 1.5))                               # вынос выше стопа
    assert await ls.run_daily((d0 + 4) * DAY + 60_000) == ["sent"]
    r = conn.execute("SELECT status, exit_reason, pnl_pct FROM listing_trades").fetchone()
    assert r[0] == "closed" and r[1] == "stop" and r[2] == pytest.approx((-50 - 0.1) * 1.5 / 100)
    assert "Stopped out" in tg.sent[-1][1]


@pytest.mark.asyncio
async def test_listing_time_exit_and_late_skip(cfg, conn, tg):
    d0 = 20_000
    bars = [bar(d0 + i, 1.0, 1.05, 0.5, 1.0 - i * 0.005) for i in range(70)]
    ls = ListingShorts(cfg, conn, Publisher(cfg, conn, tg, -100, 7), FakeBN(d0 * DAY, bars))
    await ls.run_daily((d0 + 2) * DAY)
    await ls.run_daily((d0 + 65) * DAY)
    r = conn.execute("SELECT exit_reason, pnl_pct FROM listing_trades").fetchone()
    assert r[0] == "time" and r[1] > 0
    late = ListingShorts(cfg, conn, Publisher(cfg, conn, tg, -100, 7), FakeBN((d0 + 100) * DAY, []))
    assert await late.run_daily((d0 + 106) * DAY) == []                         # узнали поздно: пропуск
