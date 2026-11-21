import json

import numpy as np
import pandas as pd
import pytest

from sigbot import backtest, bloggers, engine, ledger, live, settings
from sigbot.dashboard import make_app
from sigbot.engine import Candidate
from sigbot.publisher import Publisher

from .conftest import HOUR, put_hourly, random_walk


def prepared_row(n=300, **last):
    p = pd.DataFrame({
        "ts": np.arange(n) * HOUR, "close": 100.0, "high": 100.5, "low": 99.5, "atr": 1.0, "rsi": 40.0,
        "index": 0.0, "trend": 0, "bull": 0.0, "bear": 0.0, "bull_anchor": np.nan, "bear_anchor": np.nan,
        "labels": [[] for _ in range(n)],
    })
    for k, v in last.items():
        p.at[n - 1, k] = v
    return p


def test_decide_long_levels(cfg):
    p = prepared_row(bull=1.5, index=0.3, trend=1, bull_anchor=98.0, labels=["regular bullish cvd_spot"])
    c = engine.decide(p, len(p) - 1, "SOLUSDT", cfg)
    assert c and c.side == "long"
    assert c.invalidation == 98.0 and c.stop == pytest.approx(98.0 - 0.3)
    r = c.entry - c.stop
    assert c.tp1 == pytest.approx(c.entry + 1.2 * r) and c.tp2 == pytest.approx(c.entry + 2.5 * r)
    assert 0 <= c.strength <= 1


def test_decide_filters(cfg):
    base = dict(bull=1.5, index=0.3, bull_anchor=98.0)
    assert engine.decide(prepared_row(trend=-1, **base), 299, "X", cfg) is None          # против тренда 4ч
    p = prepared_row(trend=1, **base)
    p["fear_greed"] = 90.0
    assert engine.decide(p, 299, "X", cfg) is None                                       # экстремальная жадность
    assert engine.decide(prepared_row(trend=1, bull=1.5, index=-0.3, bull_anchor=98.0), 299, "X", cfg) is None
    assert engine.decide(prepared_row(trend=1, bull=0.5, index=0.3, bull_anchor=98.0), 299, "X", cfg) is None


def test_tight_anchor_uses_min_stop_and_liq_snap(cfg):
    p = prepared_row(bear=1.0, index=-0.4, trend=-1, bear_anchor=100.1)
    c = engine.decide(p, 299, "X", cfg)
    assert c.side == "short" and c.stop == pytest.approx(100.0 + cfg["engine"]["min_stop_atr"])
    r = c.stop - c.entry
    clusters = {"long_liq_below": [[100.0 - 3 * r, 1e6], [100.0 - 10 * r, 5e6]], "short_liq_above": []}
    c2 = engine.decide(p, 299, "X", cfg, liq_clusters=clusters)
    assert c2.tp2 == pytest.approx(100.0 - 3 * r) and c2.features["tp2_from_liq_cluster"]


def test_divergence_table_matches_simple_case(cfg):
    price = [10, 9, 8, 7, 8, 9, 10, 9, 8, 6, 7, 8, 9, 10]
    osc = [50, 45, 40, 30, 40, 45, 50, 45, 40, 35, 40, 45, 50, 55]
    df = pd.DataFrame({"high": price, "low": price}, dtype=float)
    t = engine.divergence_table(df, {"rsi": pd.Series(osc, dtype=float)}, 2, 2, 60, 10)
    # минимум на баре 9 подтверждается на баре 11
    assert t.bull.iloc[10] == 0 and t.bull.iloc[11] == 1.0
    assert t.labels.iloc[11] == ["regular bullish rsi"] and t.bull_anchor.iloc[11] == 6


class NoLLM:
    async def comment(self, facts):
        return "Spot buyers absorbed the drop."


@pytest.mark.asyncio
async def test_live_signal_to_close(cfg, conn, tg, monkeypatch):
    closes = random_walk(cfg["indicators"]["zscore_window"] + 100)
    start = put_hourly(conn, "SOLUSDT", closes)
    settings.put(conn, "universe", ["SOLUSDT"])
    t = start + len(closes) * HOUR
    entry = closes[-1]

    def fake_decide(p, i, sym, cfg_, min_strength=None, liq_clusters=None):
        return Candidate(sym, int(p.ts.iloc[i]), "long", entry, entry - 2, entry - 1.7, entry + 2.4, entry + 5, 0.7,
                         {"divergences": ["hidden bullish rsi"], "trend_4h": 1, "fear_greed": None,
                          "tp2_from_liq_cluster": False})

    async def fake_png(html, w=1000, h=560):
        return b"png"
    monkeypatch.setattr(engine, "decide", fake_decide)
    monkeypatch.setattr(live.chart, "render_png", fake_png)

    bl = bloggers.Bloggers(cfg, conn)
    cid = bl.add_draft({"author": "anna", "symbol": "SOL", "bias": "long",
                        "levels": [{"price": entry - 3, "type": "support"}], "date": None})
    bl.confirm(cid, True)

    lv = live.Live(cfg, conn, Publisher(cfg, conn, tg, -100, 7), NoLLM(), bl)
    ids = await lv.scan(t)
    assert len(ids) == 1
    assert "SOL · ЛОНГ" in tg.sent[0][1] and "Цель 1" in tg.sent[0][1] and tg.sent[0][2]   # пост с картинкой
    assert await lv.scan(t) == []                                   # по открытому символу второго входа нет

    sig_ts = conn.execute("SELECT ts FROM signals").fetchone()[0]
    bars = [(sig_ts + 0, entry + 2.5, entry - 0.5, entry + 2.0),     # tp1
            (sig_ts + 60_000, entry + 5.2, entry + 1.0, entry + 5.0)]  # tp2
    conn.executemany("INSERT INTO candles VALUES ('SOLUSDT','futures','1m',?,?,?,?,?,1,0.5)",
                     [(ts, c, h, l, c) for ts, h, l, c in bars])
    await lv.manage(sig_ts + 3 * 60_000)
    ev = [r[0] for r in conn.execute("SELECT event FROM outcomes ORDER BY id")]
    assert ev[0] == "tp1" and ev[-1] == "tp2"
    total_r = conn.execute("SELECT SUM(pnl_r) FROM outcomes").fetchone()[0]
    assert total_r == pytest.approx(0.5 * 1.2 + 0.5 * 2.5)
    assert conn.execute("SELECT status FROM trade_state").fetchone()[0] == "closed"
    assert "Выход #SOL" in tg.sent[-1][1] and "Итог" in tg.sent[-1][1]

    tr = lv.closed_trades(0, sig_ts + 10 * HOUR)
    assert len(tr) == 1 and tr[0]["r"] == pytest.approx(1.85)
    await lv.weekly()  # время сейчас, сделка в прошлом: пост без сделок, но публикуется
    assert "Часовые сделки за неделю" in tg.sent[-1][1]

    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(make_app(cfg, conn))) as client:
        r = await client.get("/api/trades?source=live")
        data = await r.json()
        assert data[0]["symbol"] == "SOLUSDT" and data[0]["r"] == pytest.approx(1.85)
        r = await client.get("/api/stats")
        assert (await r.json())["closed"] == 1
        assert (await client.get("/")).status == 200


def test_blogger_zones_merge_and_expire(cfg, conn):
    bl = bloggers.Bloggers(cfg, conn)
    a = bl.add_draft({"author": "a", "symbol": "btc", "levels": [{"price": 60000, "type": "support"}]})
    b = bl.add_draft({"author": "b", "symbol": "BTCUSDT", "levels": [{"price": 60200, "type": "support"},
                                                                    {"price": 70000, "type": "resistance"}]})
    c = bl.add_draft({"author": "c", "symbol": "BTC", "levels": [{"price": 61000}]})
    bl.confirm(a, True)
    bl.confirm(b, True)
    bl.confirm(c, False)                          # не подтверждён: удалён
    z = bl.zones("BTCUSDT")
    assert z[0]["low"] == 60000 and z[0]["high"] == 60200 and z[0]["sources"] == 2
    assert len(z) == 2
    exp = bl.get(a)["expires_ts"]
    assert bl.zones("BTCUSDT", exp - 10)[0]["sources"] == 2
    assert bl.zones("BTCUSDT", exp + 10 ** 6) == []      # истекли
    assert bl.add_draft({"symbol": None, "levels": []}) is None


def test_blogger_accuracy(cfg, conn):
    start = put_hourly(conn, "ETHUSDT", [100.0] * 5 + [110.0] * 400)
    bl = bloggers.Bloggers(cfg, conn)
    for author, bias in (("x", "long"), ("y", "short")):
        cid = bl.add_draft({"author": author, "symbol": "ETH", "bias": bias, "levels": [{"price": 100}],
                            "date": pd.Timestamp(start + HOUR, unit="ms").isoformat()})
        bl.confirm(cid, True)
        conn.execute("UPDATE blogger_calls SET call_ts=?, expires_ts=? WHERE id=?",
                     (start + HOUR, start + 300 * HOUR, cid))
    acc = {a["author"]: a["hit_rate_pct"] for a in bl.accuracy()}
    assert acc == {"x": 100.0, "y": 0.0}


def test_backtest_runs_on_synthetic_data(cfg, conn):
    n = 24 * 200
    for i, s in enumerate(("BTCUSDT", "ETHUSDT")):
        closes = random_walk(n, seed=i + 1)
        put_hourly(conn, s, closes, start_ts=1_700_000_000_000 // HOUR * HOUR,
                   oi=list(1e6 + np.cumsum(np.random.default_rng(i).normal(0, 1e3, n))))
    cfg["backtest"].update({"start": "2023-11-25", "end": "2024-05-01", "train_days": 60, "test_days": 30,
                            "min_strength_grid": [0.5], "min_trades_train": 1})
    rep = backtest.walk_forward(conn, cfg, ["BTCUSDT", "ETHUSDT"])
    assert rep["folds"] and set(rep["benchmarks"]) == {"equal_weight_pct", "btc_pct", "cash_pct"}
    for t in rep["trades"]:
        assert t["exit_ts"] > t["entry_ts"] and t["fees_pct"] > 0
    md = backtest.to_markdown(rep)
    assert "Buy and hold" in md and "Вердикт" in md


def test_capitulation_setup(cfg):
    cfg["engine"]["setup"] = "capitulation"
    p = prepared_row(mom24=-3.5, sigma24=0.04, doi24=-1.0)
    c = engine.decide(p, len(p) - 1, "SOLUSDT", cfg)
    cc = cfg["capitulation"]
    assert c.side == "long" and c.features["setup"] == "capitulation"
    assert c.stop == pytest.approx(100 * (1 - cc["stop_sigma"] * 0.04))
    assert c.tp2 == pytest.approx(100 * (1 + cc["target_sigma"] * 0.04)) and c.tp1 < c.tp2
    assert c.strength == pytest.approx(0.75)
    assert engine.decide(prepared_row(mom24=-2.0, sigma24=0.04), 299, "X", cfg) is None   # падение мало


def test_trend_setup_daily_cross(cfg):
    cfg["engine"]["setup"] = ["capitulation", "trend"]
    n = 24 * 80
    closes = np.r_[np.full(24 * 60, 100.0), np.linspace(100, 130, 24 * 20)]
    p = pd.DataFrame({"ts": np.arange(n) * HOUR, "close": closes, "high": closes + 0.5, "low": closes - 0.5})
    t = engine.daily_trend(p, cfg["trend"])
    hits = np.flatnonzero(t.trend_cross.to_numpy() != 0)
    assert len(hits) >= 1 and all((p.ts.iloc[i] + HOUR) % 86_400_000 == 0 for i in hits)   # только закрытие суток
    q = prepared_row(n=len(p))
    q["trend_cross"] = t.trend_cross.to_numpy(); q["atr_d"] = 2.0; q["mom24"] = 0.0; q["sigma24"] = 0.02
    c = engine.decide(q, int(hits[0]), "BTCUSDT", cfg)
    assert c.side == "long" and c.features["setup"] == "trend" and c.stop == pytest.approx(100 - 4.0)
    assert c.features["manage"]["trail"] == pytest.approx(6.0) and c.features["manage"]["tp1_frac"] == 0
