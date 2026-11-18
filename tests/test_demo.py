import asyncio

import pytest

from sigbot import settings
from sigbot.demo import DemoTrader


def make(cfg, conn, px):
    said = []

    async def note(t):
        said.append(t)
    settings.put(conn, "paper.enabled", True)
    return DemoTrader(cfg, conn, note, prices=lambda ex, s: px[0]), said


def test_paper_account_trade_cycle(cfg, conn):
    px = [100.0]
    dt, said = make(cfg, conn, px)

    async def run():
        await dt.open("trend", "7", "AUSDT", "long", 20)                 # 20% от 100$ = 20$ позиция, маржа 4$ при x5
        await dt.open("trend", "7", "AUSDT", "long", 20)                 # повтор не открывает второй раз
        px[0] = 110.0
        await dt.reduce("trend", "7", 0.25, "TP1")
        px[0] = 120.0
        await dt.reduce("trend", "7", None, "сигнал закрыт")
    asyncio.run(run())
    rows = conn.execute("SELECT * FROM demo_trades ORDER BY exchange").fetchall()
    assert [r["exchange"] for r in rows] == ["binance", "bybit"] and all(r["status"] == "closed" for r in rows)
    b = rows[0]
    assert b["margin"] == pytest.approx(4.0) and b["leverage"] == 5
    gross = 0.25 * (20 / 100.02) * (110 * 0.9998 - 100.02) + 0.75 * (20 / 100.02) * (120 * 0.9998 - 100.02)
    assert b["realized"] == pytest.approx(gross - b["fees"], rel=1e-6)          # все комиссии: вход и два выхода
    acc = dt.account("binance")
    assert acc["balance"] == pytest.approx(100 + b["realized"]) and acc["fees"] > 0
    assert any("Бумажный Binance" in s and "итог" in s for s in said)


def test_paper_min_size_margin_and_liquidation(cfg, conn):
    px = [100.0]
    dt, said = make(cfg, conn, px)

    async def run():
        await dt.open("trend", "1", "AUSDT", "long", 3)                  # 3$ меньше минимума 5$
        await dt.open("listing", "X", "BUSDT", "short", 50, "bybit")     # только Bybit
        px[0] = 125.0                                                    # шорт x5: +25% это ликвидация
        await dt.tick()
    asyncio.run(run())
    assert conn.execute("SELECT COUNT(*) FROM demo_trades WHERE symbol='AUSDT'").fetchone()[0] == 0
    r = conn.execute("SELECT * FROM demo_trades WHERE symbol='BUSDT'").fetchone()
    assert r["exchange"] == "bybit" and r["status"] == "closed" and r["realized"] < -9.9
    assert any("ликвидация" in s for s in said) and any("меньше минимума" in s for s in said)


def test_paper_reset_and_report(cfg, conn):
    dt, _ = make(cfg, conn, [100.0])
    asyncio.run(dt.open("trend", "1", "AUSDT", "long", 20))
    text = asyncio.run(dt.report())
    assert "Binance" in text and "от 100$" in text
    dt.reset()
    assert dt.account("binance")["balance"] == 100
