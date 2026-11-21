import asyncio

from aiohttp.test_utils import TestClient, TestServer

from sigbot import config, dashboard, db


def test_endpoints_answer():
    cfg = config.load()
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)",
                     [("BTCUSDT", "futures", "1h", t * 3_600_000, 1, 2, .5, 1.5, 10, 5) for t in range(500_000, 500_100)])

    async def run():
        async with TestClient(TestServer(dashboard.make_app(cfg, conn))) as c:
            for u in ("/", "/api/symbols", "/api/candles?symbol=BTCUSDT", "/api/candles?symbol=BTCUSDT&tf=1d",
                      "/api/candles?symbol=BTCUSDT&center=1800000000", "/api/trades?source=backtest",
                      "/api/trades?source=live&symbol=BTCUSDT", "/api/marks?symbol=BTCUSDT&source=backtest",
                      "/api/stats?source=backtest", "/api/stats", "/api/status", "/api/live", "/overview",
                      "/coins", "/api/feed?source=live", "/api/feed?source=backtest", "/api/paper"):
                r = await c.get(u)
                assert r.status == 200, u
            d = await (await c.get("/api/candles?symbol=BTCUSDT&tf=1d")).json()
            assert d and all(x["low"] <= x["open"] <= x["high"] for x in d)
    asyncio.run(run())


def test_remote_needs_token_then_cookie():
    from sigbot import phone
    cfg = config.load()
    conn = db.connect(":memory:")
    tok = phone.token(conn)
    assert tok == phone.token(conn)                                     # ключ один и тот же

    async def run():
        async with TestClient(TestServer(dashboard.make_app(cfg, conn))) as c:
            far = {"X-Forwarded-For": "8.8.8.8"}                       # запрос через туннель
            assert (await c.get("/api/status")).status == 200          # с этого компьютера без ключа
            assert (await c.get("/api/status", headers=far)).status == 401
            r = await c.get(f"/?token={tok}", headers=far)
            assert r.status == 200 and "sigbot" in r.cookies
            assert (await c.get("/api/status", headers=far)).status == 200   # дальше по cookie
    asyncio.run(run())
