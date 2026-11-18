import asyncio

from sigbot import demo, settings
from sigbot.demo import BinanceDemo, DemoTrader, _round_step


def test_binance_signature_matches_docs_example():
    b = BinanceDemo("k", "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j")
    q = b.sign({"symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": 1, "price": 0.1,
                "recvWindow": 5000, "timestamp": 1499827319559})
    assert q.endswith("signature=c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")


def test_only_demo_hosts():
    import inspect
    src = inspect.getsource(demo)
    assert "fapi.binance.com" not in src.replace("demo-fapi.binance.com", "")
    assert "api.bybit.com" not in src.replace("api-demo.bybit.com", "")


def test_round_step():
    assert _round_step(0.123456, 0.001) == 0.123 and _round_step(17.9, 1) == 17


class FakeEx:
    def __init__(self, name):
        self.name, self.rules, self.orders = name, {"BTCUSDT": {"step": 0.001, "min_qty": 0.001, "min_notional": 5}}, []

    async def load_rules(self):
        pass

    async def equity(self):
        return 10_000.0

    async def price(self, sym):
        return 50_000.0

    async def leverage(self, sym, lev):
        pass

    async def market(self, sym, side, qty, reduce=False):
        self.orders.append((side, qty, reduce))
        return qty, 50_000.0 if not reduce else 55_000.0


def test_trader_opens_reduces_closes(cfg, conn):
    ex = [FakeEx("binance"), FakeEx("bybit")]
    said = []

    async def note(t):
        said.append(t)
    settings.put(conn, "demo.enabled", True)
    dt = DemoTrader(cfg, conn, note, ex)

    async def run():
        await dt.open("trend", "7", "BTCUSDT", "long", 10)               # 10% от 10 000$ = 0.02 BTC
        await dt.open("trend", "7", "BTCUSDT", "long", 10)               # повтор не открывает второй раз
        await dt.reduce("trend", "7", 0.25, "TP1")
        await dt.reduce("trend", "7", None, "сигнал закрыт")
        await dt.open("listing", "NEWUSDT", "BTCUSDT", "short", 4.5, "bybit")   # только Bybit
    asyncio.run(run())
    assert ex[0].orders == [("buy", 0.02, False), ("sell", 0.005, True), ("sell", 0.015, True)]
    rows = conn.execute("SELECT exchange, status, realized FROM demo_trades WHERE kind='trend'").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("binance", "closed"), ("bybit", "closed")]
    assert abs(rows[0][2] - 0.02 * 5000) < 1e-6
    assert ex[1].orders[-1] == ("sell", 0.009, False) and len(ex[0].orders) == 3
    assert any("Демо binance" in s for s in said)


def test_trader_off_without_switch(cfg, conn):
    ex = [FakeEx("binance")]
    settings.put(conn, "demo.enabled", False)
    asyncio.run(DemoTrader(cfg, conn, None, ex).open("trend", "1", "BTCUSDT", "long", 10))
    assert ex[0].orders == []
