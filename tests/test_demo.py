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
        await dt.open("trend", "1", "AUSDT", "long", 3)                  # 3$ меньше минимума: поднимается до 5$
        await dt.open("listing", "X", "BUSDT", "short", 50, "bybit")     # только Bybit
        px[0] = 125.0                                                    # шорт x5: +25% это ликвидация
        await dt.tick()
    asyncio.run(run())
    a = conn.execute("SELECT * FROM demo_trades WHERE symbol='AUSDT' AND exchange='binance'").fetchone()
    assert a["qty"] * a["entry"] >= 5 and a["margin"] == pytest.approx(a["qty"] * a["entry"] / 5)
    r = conn.execute("SELECT * FROM demo_trades WHERE symbol='BUSDT'").fetchone()
    assert r["exchange"] == "bybit" and r["status"] == "closed" and r["realized"] < -9.9
    assert any("ликвидация" in s for s in said) and any("поднял до минимума биржи" in s for s in said)


def test_paper_reset_and_report(cfg, conn):
    dt, _ = make(cfg, conn, [100.0])
    asyncio.run(dt.open("trend", "1", "AUSDT", "long", 20))
    text = asyncio.run(dt.report())
    assert "Binance" in text and "от 100$" in text
    dt.reset()
    assert dt.account("binance")["balance"] == 100


def test_paper_exchange_rules_min_qty_and_step(cfg, conn):
    settings.put(conn, "paper_rules_binance", {"_ts": 9e12, "BTCUSDT": {"min_notional": 100, "min_qty": 0.001, "step": 0.001}})
    settings.put(conn, "paper.exchanges", ["binance"])
    px = [60_000.0]
    dt, said = make(cfg, conn, px)
    asyncio.run(dt.open("trend", "1", "BTCUSDT", "long", 10))           # 10$ < минимум 100$: маржа 20$ при x5, есть
    r = conn.execute("SELECT * FROM demo_trades").fetchone()
    assert r["qty"] == pytest.approx(0.002) and r["margin"] == pytest.approx(0.002 * r["entry"] / 5)
    settings.put(conn, "paper.leverage", 1)                              # без плеча минимум 100$ не влезает в свободные деньги
    asyncio.run(dt.open("trend", "2", "BTCUSDT", "long", 10))
    assert conn.execute("SELECT COUNT(*) FROM demo_trades").fetchone()[0] == 1 and "требует маржу" in said[-1]
    asyncio.run(dt.reduce("trend", "1", 0.25, "TP1"))                   # 0.0005 BTC меньше шага 0.001: часть не закрыть
    assert conn.execute("SELECT open_qty FROM demo_trades").fetchone()[0] == pytest.approx(0.002) and "держу всю позицию" in said[-1]


def test_open_live_signals_button(cfg, conn):
    import json
    dt, said = make(cfg, conn, [10.0])
    settings.put(conn, "paper.exchanges", ["binance"])
    conn.execute("INSERT INTO trend_signals (symbol, side, open_day, entry, size_pct, state) VALUES ('AUSDT','long',0,9,20,?)",
                 (json.dumps({"rem": 0.75}),))
    conn.execute("INSERT INTO listing_trades (symbol, onboard_ts, entry_ts, entry, stop, exit_due_ts, notional_pct, status, exchange) "
                 "VALUES ('NEWUSDT',0,0,10,15,0,6,'open','binance')")
    assert dt.live_counts() == (2, 2)
    assert asyncio.run(dt.open_live()) == (2, 0)
    t = conn.execute("SELECT * FROM demo_trades WHERE kind='trend'").fetchone()
    assert t["open_qty"] == pytest.approx(0.75 * t["qty"])                  # TP1 уже взят: открыто 75% позиции
    assert t["open_qty"] * t["entry"] == pytest.approx(15, rel=1e-3)        # 20% от 100$ × 0.75
    assert dt.live_counts() == (2, 0) and asyncio.run(dt.open_live()) == (0, 2)
    asyncio.run(dt.reduce("trend", "1", 0.25, "TP2"))                       # следующая четверть исходной позиции
    assert conn.execute("SELECT open_qty/qty FROM demo_trades WHERE kind='trend'").fetchone()[0] == pytest.approx(0.5)


def test_risk_sizing_leverage_and_open_risk_limit(cfg, conn):
    """От риска: на стопе теряем 2.5% капитала, плечо такое, что ликвидация дальше стопа, общий риск не больше 20%."""
    px = [100.0]
    dt, said = make(cfg, conn, px)
    settings.put(conn, "paper.exchanges", ["binance"])
    asyncio.run(dt.open("trend", "1", "AUSDT", "long", 13.9, stop=90.0))       # обычный сигнал, стоп −10%
    r = conn.execute("SELECT * FROM demo_trades").fetchone()
    assert r["qty"] * r["entry"] == pytest.approx(25.0, rel=0.01)             # 2.5$ / 10% = 25$ позиция
    assert r["leverage"] == 8 and r["liq"] < 90.0                              # x8: ликвидация ниже стопа
    assert r["risk_usd"] == pytest.approx(2.5, rel=0.02) and r["margin"] == pytest.approx(25 / 8, rel=0.01)
    assert "ликвидация" in said[-1] and "дальше стопа" in said[-1]
    asyncio.run(dt.open("trend", "2", "BUSDT", "short", 27.8, stop=101.0))     # сильный сигнал x2, узкий стоп 1%
    r2 = conn.execute("SELECT * FROM demo_trades WHERE ref='2'").fetchone()
    assert r2["risk_usd"] == pytest.approx(5.0, rel=0.05) and r2["leverage"] == 39   # 1 / (1% стоп + 0.5% + 1% запас)
    for i in range(3, 10):                                                      # до лимита 20%: 2.5+5+2.5*5 = 20
        asyncio.run(dt.open("trend", str(i), f"C{i}USDT", "long", 13.9, stop=90.0))
    n = conn.execute("SELECT COUNT(*) FROM demo_trades").fetchone()[0]
    assert dt.open_risk("binance") <= 20.0 + 1e-6 and n == 6 and "на пределе" in said[-1]


def test_exchange_minimum_does_not_break_risk(cfg, conn):
    px = [50000.0]
    dt, said = make(cfg, conn, px)
    settings.put(conn, "paper.exchanges", ["binance"])
    settings.put(conn, "paper_rules_binance", {"_ts": 9e12, "BTCUSDT": {"min_notional": 100.0, "min_qty": 0.001, "step": 0.001}})
    asyncio.run(dt.open("trend", "1", "BTCUSDT", "long", 13.9, stop=45000.0))   # план 25$, минимум 100$: риск 10$ > 2×2.5$
    assert not conn.execute("SELECT 1 FROM demo_trades").fetchone() and "Риск-менеджмент важнее" in said[-1]
