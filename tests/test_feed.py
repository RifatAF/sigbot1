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


def test_live_trend_signal_open_and_close(cfg, conn):
    cfg["publishing"]["mode"] = "live"
    tg = TG()
    lp = portfolio.LivePortfolio(cfg, conn, Publisher(cfg, conn, tg, -100, 7))
    idx = np.arange(200) * DAY
    C = pd.DataFrame({"AUSDT": 100 * np.exp(np.cumsum(np.full(200, 0.01)))}, index=idx)
    d1, d2, d3 = idx[-3], idx[-2], idx[-1]
    conn.execute("INSERT INTO portfolio_weights VALUES (?,?,?)", (d1 - DAY, "BUSDT", 0.05))
    asyncio.run(lp.signals(d2, C, {"AUSDT": 0.3}))
    assert any("AUSDT".replace("USDT", "") + " LONG" in t for _, t in tg.sent)
    row = conn.execute("SELECT * FROM trend_signals").fetchone()
    assert row["status"] == "open" and row["exit_level"] < row["entry"]
    asyncio.run(lp.signals(d3, C, {}))                                 # портфель убрал монету: закрытие
    row = conn.execute("SELECT * FROM trend_signals").fetchone()
    assert row["status"] == "closed" and row["move_pct"] > 0 and any("Closed in profit" in t for _, t in tg.sent)
