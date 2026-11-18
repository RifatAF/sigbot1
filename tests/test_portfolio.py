import numpy as np
import pandas as pd
import pytest

from sigbot import portfolio, templates, textguard
from sigbot.publisher import Publisher

DAY = 86_400_000


def frames(n=400, seed=0):
    rng = np.random.default_rng(seed)
    idx = np.arange(n) * DAY
    up = 100 * np.exp(np.cumsum(rng.normal(0.004, 0.02, n)))
    dn = 100 * np.exp(np.cumsum(rng.normal(-0.004, 0.02, n)))
    C = pd.DataFrame({"AUSDT": up, "BUSDT": dn}, index=idx)
    V = pd.DataFrame(1e9, index=idx, columns=C.columns)
    return C, V, pd.DataFrame(0.0, index=idx, columns=C.columns)


def test_weights_follow_trend_and_target_vol(cfg):
    C, V, FR = frames()
    W = portfolio.weights(C, V, cfg["portfolio"])
    assert W["AUSDT"].iloc[-1] > 0 and W["BUSDT"].iloc[-1] < 0          # лонг растущей, шорт падающей
    assert W.abs().sum(1).max() <= cfg["portfolio"]["max_gross"] + 1e-9
    d = portfolio.simulate(C, FR, W, 0.05)
    assert d.pnl.iloc[200:].sum() > 0


def test_weights_have_no_lookahead(cfg):
    C, V, _ = frames()
    W1 = portfolio.weights(C, V, cfg["portfolio"])
    C2 = C.copy()
    C2.iloc[-1] *= 3                                                      # меняем только последний день
    W2 = portfolio.weights(C2, V, cfg["portfolio"])
    pd.testing.assert_frame_equal(W1.iloc[:-1], W2.iloc[:-1])


def test_portfolio_post_passes_textguard(cfg):
    w = {"BTCUSDT": 0.123, "ETHUSDT": -0.0456}
    text, nums = templates.portfolio(w, ["ETHUSDT"], ["SOLUSDT"], 12.345, -0.4, cfg["portfolio"], "paper", full=True)
    assert textguard.verify(text, nums) is None
    assert "ETH SHORT 4.6%" in text and "BTC LONG 12.3%" in text and "#forwardtest" in text


@pytest.mark.asyncio
async def test_live_portfolio_daily(cfg, conn, tg):
    C, V, FR = frames(260)
    rows = []
    for s in C:
        for d, px in C[s].items():
            for h in range(24):
                rows.append((s, "futures", "1h", int(d + h * 3_600_000), px, px, px, px, 1e6, 5e5))
    conn.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    lp = portfolio.LivePortfolio(cfg, conn, Publisher(cfg, conn, tg, -100, 7))
    last = int(C.index[-1])
    assert await lp.run_daily(last, list(C)) == "sent"                  # первый пост: список открытых сигналов
    assert "Открытые сигналы тренда" in tg.sent[-1][1]
    assert await lp.run_daily(last, list(C)) is None                    # тот же день второй раз не считается
    assert await lp.run_daily(last + DAY, list(C)) in (None, "sent")
    assert conn.execute("SELECT COUNT(*) FROM portfolio_equity").fetchone()[0] == 2


def test_negative_funding_long_and_consensus(cfg):
    C, V, FR = frames()
    FR.iloc[300, 1] = -0.002                                     # шорты переполнены по BUSDT
    fc = cfg["portfolio"]["neg_funding"]
    nf = portfolio.neg_funding_weights(C, FR, fc, C.columns)
    assert nf["BUSDT"].iloc[300:303].tolist() == [pytest.approx(fc["size"] / 3)] * 3 and nf["BUSDT"].iloc[303] == 0
    W = portfolio.weights(C, V, cfg["portfolio"], FR=FR)
    assert W["BUSDT"].iloc[301] > portfolio.trend_weights(C, V, cfg["portfolio"])["BUSDT"].iloc[301]


def test_live_funding_daily_from_predicted(cfg, conn):
    rows = [("AUSDT", 1000 * 300_000, -0.0004) for _ in range(1)]
    conn.executemany("INSERT INTO funding VALUES (?,?,?)", [("AUSDT", i * 300_000, -0.0004) for i in range(288)])
    conn.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)",
                     [("AUSDT", "futures", "1h", h * 3_600_000, 1, 1, 1, 1, 1, 0.5) for h in range(24)])
    C, V, FR = portfolio.daily_frames(conn, ["AUSDT"], 0, DAY)
    assert FR.iloc[0, 0] == pytest.approx(-0.0012)               # прогноз каждые 5 минут: среднее × 3, а не сумма 288 строк
