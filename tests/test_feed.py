import asyncio

import numpy as np
import pandas as pd

from sigbot import feed, portfolio
from sigbot.publisher import Publisher

DAY = 86_400_000


def test_exit_level_breaks_trend(cfg):
    pc = cfg["portfolio"]
    c = 100 * np.exp(np.cumsum(np.full(300, 0.01)))                 # ровный рост: сильный лонг
    lv = feed.exit_level(c, 1, pc)
    assert lv and lv < c[-1]
    assert feed.consensus_at(c, lv * 1.001, pc) >= pc["min_consensus"]
    assert feed.consensus_at(c, lv * 0.999, pc) < pc["min_consensus"]
    lv_s = feed.exit_level(c[::-1].copy(), -1, pc)                   # падение: шорт, выход выше цены
    assert lv_s and lv_s > c[0]


def test_positions_and_fixed_weights(cfg):
    idx = np.arange(6) * DAY
    W = pd.DataFrame({"A": [0, .2, .3, 0, -.05, 0]}, index=idx)
    C = pd.DataFrame({"A": [10, 10, 11, 12, 12, 12.0]}, index=idx)
    pos = feed.trend_positions(W, C, cfg["portfolio"], 0, 10 * DAY, 0.0)
    assert len(pos) == 1 and pos[0]["entry"] == 10 and pos[0]["exit"] == 12 and pos[0]["size_pct"] == 20
    assert feed.fixed_weights(W, 0.1)["A"].tolist() == [0, .2, .2, 0, 0, 0]


class TG:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text))
        return {"message_id": len(self.sent)}

    async def send_photo(self, chat, png, caption, **kw):
        return await self.send_message(chat, caption)


def _hourly(conn, sym, day, closes_hi_lo):
    rows = [(sym, "futures", "1h", day + h * 3_600_000, c, hi, lo, c, 1e6, 5e5) for h, (c, hi, lo) in enumerate(closes_hi_lo)]
    conn.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def test_live_trend_signal_targets_then_trend_exit(cfg, conn):
    cfg["publishing"]["mode"] = "live"
    tg = TG()
    lp = portfolio.LivePortfolio(cfg, conn, Publisher(cfg, conn, tg, -100, 7))
    idx = np.arange(200) * DAY
    C = pd.DataFrame({"AUSDT": 100 * np.exp(np.cumsum(np.full(200, 0.01)))}, index=idx)
    d1, d2, d3 = idx[-3], idx[-2], idx[-1]
    for d in idx[-30:-1]:                                              # дневной размах около 2: ATR ~ 2
        px = float(C["AUSDT"].loc[d])
        _hourly(conn, "AUSDT", int(d), [(px, px + 1, px - 1)] * 24)
    conn.execute("INSERT INTO portfolio_weights VALUES (?,?,?)", (d1 - DAY, "BUSDT", 0.05))
    asyncio.run(lp.signals(d2, C, {"AUSDT": 0.3}))
    post = tg.sent[-1][1]
    assert "A · ЛОНГ" in post and "TP1" in post and "TP2" in post and "Стоп" in post
    row = conn.execute("SELECT * FROM trend_signals").fetchone()
    e = row["entry"]
    assert row["status"] == "open" and row["stop"] < e < row["tp1"] < row["tp2"]
    _hourly(conn, "AUSDT", int(d3), [(e, e, e)] * 3 + [(row["tp1"], row["tp1"] + .01, e)] + [(row["tp2"], row["tp2"] + .01, e)])
    asyncio.run(lp.manage(int(d3) + 6 * 3_600_000))
    assert any("TP1" in t and "Закрыто 25%" in t for _, t in tg.sent) and any("TP2" in t for _, t in tg.sent)
    asyncio.run(lp.signals(d3, C, {}))                                 # портфель убрал монету: остаток закрыт
    row = conn.execute("SELECT * FROM trend_signals").fetchone()
    assert row["status"] == "closed" and row["result_pct"] > 0
    assert any("TP1 ✅" in t and "TP2 ✅" in t and "тренд сломался" in t and "Итог" in t for _, t in tg.sent)


def test_live_trend_stop(cfg, conn):
    cfg["publishing"]["mode"] = "live"
    tg = TG()
    lp = portfolio.LivePortfolio(cfg, conn, Publisher(cfg, conn, tg, -100, 7))
    idx = np.arange(200) * DAY
    C = pd.DataFrame({"AUSDT": 100 * np.exp(np.cumsum(np.full(200, 0.01)))}, index=idx)
    asyncio.run(lp.signals(idx[-2], C, {"AUSDT": 0.3}, first=False))
    row = conn.execute("SELECT * FROM trend_signals").fetchone()
    _hourly(conn, "AUSDT", int(idx[-1]), [(row["stop"] - 1, row["entry"], row["stop"] - 1)])
    asyncio.run(lp.manage(int(idx[-1]) + 2 * 3_600_000))
    row = conn.execute("SELECT * FROM trend_signals").fetchone()
    assert row["status"] == "closed" and row["result_pct"] < 0 and "стоп ❌" in tg.sent[-1][1]


def test_plan_step_and_backtest_exits():
    st = feed.new_state("short", 100.0, feed.levels(100.0, 2.0, -1, feed.PLAN))
    assert st["stop"] == 104 and st["tp1"] == 96 and st["tp2"] == 92
    assert [e["what"] for e in feed.step(st, 1, 100.5, 91)] == ["tp1", "tp2"] and st["rem"] == 0.5
    feed.finish(st, 2, 90.0, "trend")
    mv, res = feed.realized(st, 20, 0.0)
    assert mv == 0.25 * 4 + 0.25 * 8 + 0.5 * 10 and res == 20 * mv / 100
    st = feed.new_state("long", 100.0, feed.levels(100.0, 2.0, 1, feed.PLAN))
    assert [e["what"] for e in feed.step(st, 1, 110, 95)] == ["sl"]       # стоп и цель в одной свече: считаем стоп


def test_legacy_open_signal_gets_levels(cfg, conn):
    lp = portfolio.LivePortfolio(cfg, conn, Publisher(cfg, conn, TG(), -100, 7))
    d = 300 * DAY
    for k in range(20):
        _hourly(conn, "AUSDT", d - k * DAY, [(100.0, 101.0, 99.0)] * 24)
    conn.execute("INSERT INTO trend_signals (symbol, side, open_day, entry, size_pct) VALUES ('AUSDT','long',?,100,15)", (d,))
    asyncio.run(lp.manage(d + 5 * DAY))
    r = conn.execute("SELECT * FROM trend_signals").fetchone()
    assert r["stop"] == 96 and r["tp1"] == 104 and r["status"] == "open"
