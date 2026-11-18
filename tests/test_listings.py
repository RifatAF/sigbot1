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
    bars = [bar(d0, 1.05, 1.2, 0.9, 1.0), bar(d0 + 1, 1.0, 1.1, 0.95, 1.0)]
    bn = FakeBN(onboard, bars)
    ls = ListingShorts(cfg, conn, Publisher(cfg, conn, tg, -100, 7), bn)
    assert await ls.run_daily((d0 + 2) * DAY + 60_000) == ["sent"]          # вход на закрытии первых полных суток
    t = ls.open_trades()[0]
    assert t["entry"] == 1.0 and t["stop"] == pytest.approx(1.5) and "NEW · ШОРТ" in tg.sent[-1][1]
    bars.append(bar(d0 + 2, 1.0, 1.2, 0.9, 1.1))
    bars.append(bar(d0 + 3, 1.1, 1.6, 1.0, 1.5))                               # вынос выше стопа
    assert await ls.run_daily((d0 + 4) * DAY + 60_000) == ["sent"]
    r = conn.execute("SELECT status, exit_reason, pnl_pct FROM listing_trades").fetchone()
    assert r[0] == "closed" and r[1] == "stop" and r[2] == pytest.approx((-50 - 0.1) * cfg["listings"]["notional_pct"] / 100)
    assert "стоп ❌" in tg.sent[-1][1]


@pytest.mark.asyncio
async def test_listing_time_exit_and_late_skip(cfg, conn, tg):
    d0 = 20_000
    bars = [bar(d0 + i, 1.0 - (i - 1) * 0.005, 1.05, 0.5, 1.0 - i * 0.005) for i in range(70)]
    ls = ListingShorts(cfg, conn, Publisher(cfg, conn, tg, -100, 7), FakeBN(d0 * DAY, bars))
    await ls.run_daily((d0 + 2) * DAY)
    await ls.run_daily((d0 + 65) * DAY)
    r = conn.execute("SELECT exit_reason, pnl_pct FROM listing_trades").fetchone()
    assert r[0] == "time" and r[1] > 0
    late = ListingShorts(cfg, conn, Publisher(cfg, conn, tg, -100, 7), FakeBN((d0 + 100) * DAY, []))
    assert await late.run_daily((d0 + 106) * DAY) == []                         # узнали поздно: пропуск


@pytest.mark.asyncio
async def test_hot_listing_is_filtered(cfg, conn, tg):
    d0 = 20_000
    bars = [bar(d0, 1.0, 1.5, 0.9, 1.3), bar(d0 + 1, 1.3, 1.4, 1.2, 1.25)]      # первые сутки выше открытия
    ls = ListingShorts(cfg, conn, Publisher(cfg, conn, tg, -100, 7), FakeBN(d0 * DAY, bars))
    assert await ls.run_daily((d0 + 2) * DAY) == []
    assert conn.execute("SELECT status FROM listing_trades").fetchone()[0] == "filtered"


class FakeHttp:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    async def get_json(self, url, params=None, headers=None):
        self.calls.append((url, params))
        return self.pages[url.split("/v5")[1]](params)


@pytest.mark.asyncio
async def test_bybit_source_parses_v5():
    from sigbot.sources import Bybit
    lst = {"retCode": 0, "result": {"list": [
        {"symbol": "NEWUSDT", "contractType": "LinearPerpetual", "quoteCoin": "USDT", "status": "Trading", "launchTime": "1700000000000"},
        {"symbol": "OLDUSDC", "contractType": "LinearPerpetual", "quoteCoin": "USDC", "status": "Trading", "launchTime": "1"},
        {"symbol": "FUTUSDT", "contractType": "LinearFutures", "quoteCoin": "USDT", "status": "Trading", "launchTime": "1"}],
        "nextPageCursor": ""}}
    kl = {"retCode": 0, "result": {"list": [["1700086400000", "2", "3", "1", "2.5", "10", "25"],
                                            ["1700000000000", "1", "2", "0.5", "2", "10", "20"]]}}
    bb = Bybit(FakeHttp({"/market/instruments-info": lambda p: lst, "/market/kline": lambda p: kl}))
    assert await bb.perp_listings() == [("NEWUSDT", 1700000000000)]
    k = await bb.klines("NEWUSDT", "futures", "1d", 1700000000000, 10)
    assert k[0][0] < k[1][0] and k[1][4] == 2.5


class FakeBB(FakeBN):
    async def perp_listings(self):
        return [("BYBUSDT", self.onboard), ("BOTHUSDT", self.onboard)]


@pytest.mark.asyncio
async def test_bybit_only_listing_is_traded(cfg, conn, tg):
    from sigbot import settings
    d0 = 20_000
    onboard = d0 * DAY + 8 * 3_600_000
    bars = [bar(d0, 1.05, 1.2, 0.9, 1.0), bar(d0 + 1, 1.0, 1.1, 0.95, 1.0)]
    bn = FakeBN(onboard, bars)

    async def bn_list():
        return [("BOTHUSDT", onboard - 10 * DAY)]                    # на Binance раньше: с Bybit не берём
    bn.perp_listings = bn_list
    settings.put(conn, "listings.bybit", True)
    ls = ListingShorts(cfg, conn, Publisher(cfg, conn, tg, -100, 7), bn, FakeBB(onboard, bars))
    await ls.run_daily((d0 + 2) * DAY + 60_000)
    rows = conn.execute("SELECT symbol, exchange FROM listing_trades WHERE status='open'").fetchall()
    assert [tuple(r) for r in rows] == [("BYBUSDT", "bybit")] and "Bybit" in tg.sent[-1][1]


def test_perp_kind_detects_tradfi():
    from sigbot.sources import _symbols_in, perp_kind
    assert perp_kind({"underlyingType": "COIN", "contractType": "PERPETUAL", "underlyingSubType": ["Meme"]}) == "COIN"
    assert perp_kind({"underlyingType": "COIN", "contractType": "TRADIFI_PERPETUAL"}) == "TRADFI"
    assert perp_kind({"underlyingType": "COIN", "contractType": "PERPETUAL", "underlyingSubType": ["TradFi"]}) == "TRADFI"
    assert perp_kind({"underlyingType": "EQUITY"}) == "EQUITY"
    assert _symbols_in({"markets": {"US": {"symbols": ["DUOLUSDT", "x"]}}, "t": 1}) == {"DUOLUSDT"}


@pytest.mark.asyncio
async def test_stock_listing_cancelled(cfg, conn, tg):
    d0 = 20_000
    conn.execute("INSERT INTO listing_trades (symbol, onboard_ts, entry_ts, entry, stop, exit_due_ts, notional_pct, status) "
                 "VALUES ('DUOLUSDT', 1, ?, 150, 225, ?, 4.5, 'open')", (d0 * DAY, (d0 + 60) * DAY))
    cfg["listings"]["exclude"] = ["DUOL"]
    ls = ListingShorts(cfg, conn, Publisher(cfg, conn, tg, -100, 7), FakeBN(1, []))
    await ls.run_daily((d0 + 2) * DAY)
    assert conn.execute("SELECT status FROM listing_trades WHERE symbol='DUOLUSDT'").fetchone()[0] == "cancelled"
