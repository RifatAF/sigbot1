import asyncio

import numpy as np
import pandas as pd
import pytest

from sigbot import btc_layers as bl
from sigbot import settings
from sigbot.demo import DemoTrader
from sigbot.textguard import verify


def sample(seed=1, end="2024-06-30"):
    """Синтетическая выборка в формате fetch_all: те же структуры, что приходят из источников."""
    rng = np.random.default_rng(seed)
    days = pd.date_range("2016-01-01", end)
    n = len(days)
    walk = lambda v, s: pd.Series(v * np.exp(np.cumsum(rng.normal(0, s, n))), index=days)
    btc = walk(1000, 0.035)
    bd = days[days.dayofweek < 5]
    ust = pd.DataFrame({"3 Mo": 2 + rng.normal(0, .02, len(bd)).cumsum() * .1, "2 Yr": 2.5 + rng.normal(0, .03, len(bd)).cumsum(),
                        "10 Yr": 3 + rng.normal(0, .03, len(bd)).cumsum()}, index=bd)
    effr = {"refRates": [{"effectiveDate": str(d.date()), "percentRate": 2 + 0.001 * i} for i, d in enumerate(bd)]}
    rrp = {"repo": {"operations": [{"operationType": "Reverse Repo", "operationDate": str(d.date()),
                                    "totalAmtAccepted": float(1e11 * (1 + 0.5 * np.sin(i / 50)))} for i, d in enumerate(bd)]}}
    wk = days[days.dayofweek == 2]
    soma = {"soma": {"summary": [{"asOfDate": str(d.date()), "total": float(5e12 + 1e10 * i + 3e11 * np.sin(i / 9))}
                                 for i, d in enumerate(wk)]}}
    tga = [{"record_date": str(d.date()), "account_type": "Treasury General Account (TGA) Closing Balance",
            "close_today_bal": str(400_000 + 50_000 * np.sin(i / 30))} for i, d in enumerate(bd)]
    cb = btc[days >= "2018-01-01"] * (1 + rng.normal(0, 0.001, (days >= "2018-01-01").sum()))
    return dict(btc=btc, ung=walk(20, .02)[bd], ng=walk(3, .03)[bd], ttf=walk(20, .03)[bd], ust=ust, effr=effr, rrp=rrp,
                soma=soma, tga=tga, cb=cb, bn=btc[days >= "2018-01-01"])


@pytest.fixture(scope="module")
def data():
    return sample()


def test_model_matches_reference_and_bounds(cfg, data):
    df = bl.model(cfg, d=data)
    ref = bl.compute(bl.closed_only(data))
    pd.testing.assert_series_equal(df.signal, ref.signal)
    assert len(df) > 1500 and df.signal.notna().sum() > 1000
    assert df.signal.abs().max() <= bl.LIMIT + 1e-9
    assert set(np.unique(df.regime)) <= {-1.0, 1.0}
    b = bl.backtest(df)
    assert np.isfinite(b["final"]) and b["max_dd"] < 0


def test_hysteresis_does_not_flicker():
    m = pd.Series([0.2, 0.05, -0.05, 0.08, -0.09, 0.03, -0.11, 0.0, 0.09, 0.11])
    assert list(bl._regime(m)) == [1, 1, 1, 1, 1, 1, -1, -1, -1, 1]


def test_no_lookahead(cfg, data):
    """Решение на дату D считается только по данным до закрытия D: обрезка будущего его не меняет."""
    full = bl.model(cfg, d=data)
    D = full.index[-200]
    cut = {k: (v[v.index <= D] if isinstance(v, (pd.Series, pd.DataFrame)) else v) for k, v in data.items()}
    part = bl.model(cfg, d=cut)
    assert part.index[-1] <= D
    common = part.index[-50:]
    np.testing.assert_allclose(part.signal.loc[common].values, full.signal.loc[common].values)
    # позиция на D — это решение на закрытии D−1
    assert full.pos.iloc[-1] == full.signal.iloc[-2]


def test_open_candle_of_today_is_dropped(data):
    today = data["btc"].index[-1]
    d = bl.closed_only(data, today)
    assert d["btc"].index[-1] < today and d["cb"].index[-1] < today


def test_post_rule():
    assert bl.changed(None, 0.5)
    assert not bl.changed(0.9, 1.1) and bl.changed(0.9, 1.2)
    assert bl.changed(0.3, -0.3) and bl.changed(0.4, 0.0)


def test_post_numbers_pass_guard():
    st = {"signal": -0.9, "macro": -0.61, "regime": -1, "trend": -1.0, "vol": 0.44, "btc": 91234.5,
          "parts": {"gas": 0.19, "fed": -1.35, "liq": -0.86, "curve": -0.44, "ttf": -0.42, "prem": None},
          "bt": {"start": "2018-05-21", "final": 15159.4, "cagr": 0.806, "max_dd": -0.419}}
    for lang in ("ru", "en"):
        text, nums = bl_text(st, {"signal": 0.5}, lang)
        verify(text, nums)
    text, _ = bl_text({**st, "signal": 0.0}, None, "ru")
    assert "КЭШ" in text


def bl_text(st, prev, lang):
    from sigbot import templates
    return templates.btc_layers(st, prev, "paper", lang)


class Pub:
    def __init__(self):
        self.posts = []

    async def publish(self, kind, text, nums, *a, **k):
        verify(text, nums)
        self.posts.append(text)
        return "sent"


def test_service_posts_on_change_and_rebalances_paper(cfg, conn, data, tmp_path):
    px = [60000.0]
    settings.put(conn, "paper.enabled", True)
    dt = DemoTrader(cfg, conn, prices=lambda ex, s: px[0])
    pub = Pub()
    sv = bl.LayersService(cfg, conn, pub, dt)
    sv.report_path = str(tmp_path / "bl.json")
    sv.cfg = {**cfg, "btc_layers": {**cfg["btc_layers"], "enabled": True}}
    st = asyncio.run(sv.run_daily(86_400_000 * 20000, d=data))
    assert len(pub.posts) == 1 and "BTC Layers" in pub.posts[0]
    assert (tmp_path / "bl.json").exists()
    eq = dt.equity("layers")
    net = dt.net_notional("layers", px[0])
    if abs(st["signal"]) > 0.1:
        assert net == pytest.approx(st["signal"] * 100, rel=0.02)
    # тот же день: второй раз не считает; новый день с той же позицией: без поста и без сделок
    assert asyncio.run(sv.run_daily(86_400_000 * 20000, d=data)) is None
    n = conn.execute("SELECT COUNT(*) FROM demo_trades").fetchone()[0]
    asyncio.run(sv.run_daily(86_400_000 * 20001, d=data))
    assert len(pub.posts) == 1 and conn.execute("SELECT COUNT(*) FROM demo_trades").fetchone()[0] == n
    assert eq > 0
    # обычные счета Binance и Bybit модель не трогает
    assert not conn.execute("SELECT 1 FROM demo_trades WHERE exchange!='layers'").fetchone()


def test_paper_target_flip_and_reduce(cfg, conn):
    px = [50000.0]
    settings.put(conn, "paper.enabled", True)
    dt = DemoTrader(cfg, conn, prices=lambda ex, s: px[0])

    async def run():
        await dt.layers_target(1.0, "d1")
        assert dt.net_notional("layers", px[0]) == pytest.approx(100, rel=0.01)
        await dt.layers_target(1.05, "d2")                  # разница меньше 10% капитала: без сделки
        assert conn.execute("SELECT COUNT(*) FROM demo_trades").fetchone()[0] == 1
        await dt.layers_target(0.5, "d3")                   # уменьшить вдвое
        assert dt.net_notional("layers", px[0]) == pytest.approx(50, rel=0.02)
        await dt.layers_target(1.5, "d4")                   # добрать новой сделкой
        assert dt.net_notional("layers", px[0]) == pytest.approx(150, rel=0.02)
        await dt.layers_target(-0.9, "d5")                  # разворот: всё закрыть, открыть шорт
        assert dt.net_notional("layers", px[0]) == pytest.approx(-0.9 * dt.equity("layers"), rel=0.02)
        await dt.layers_target(0.0, "d6")                   # кэш
        assert dt.net_notional("layers", px[0]) == 0
    asyncio.run(run())
    assert not conn.execute("SELECT 1 FROM demo_trades WHERE status='open'").fetchone()


def test_btc_trend_parallel_by_default_and_skip_option(cfg, conn):
    from sigbot.portfolio import LivePortfolio
    assert cfg["btc_layers"]["replace_btc_trend"] is False      # по умолчанию обе стратегии работают параллельно
    cfg["btc_layers"]["replace_btc_trend"] = True
    idx = pd.date_range("2024-01-01", periods=60, freq="D")
    day = int(idx[-1].timestamp() * 1000)
    C = pd.DataFrame({"BTCUSDT": np.linspace(40000, 60000, 60), "ETHUSDT": np.linspace(2000, 3000, 60)},
                     index=[int(t.timestamp() * 1000) for t in idx])
    p = LivePortfolio(cfg, conn, Pub())
    asyncio.run(p.signals(day, C, {"BTCUSDT": 0.3, "ETHUSDT": 0.3}))
    syms = {r[0] for r in conn.execute("SELECT symbol FROM trend_signals")}
    assert "ETHUSDT" in syms and "BTCUSDT" not in syms


def test_paper_target_respects_exchange_minimum(cfg, conn):
    px = [50000.0]
    settings.put(conn, "paper.enabled", True)
    settings.put(conn, "paper_rules_binance", {"_ts": 9e12, "BTCUSDT": {"min_notional": 100.0, "min_qty": 0.001, "step": 0.001}})
    dt = DemoTrader(cfg, conn, prices=lambda ex, s: px[0])
    asyncio.run(dt.layers_target(0.9, "d1"))                 # 90$ меньше минимума 100$: поднято до минимума
    assert 100 <= dt.net_notional("layers", px[0]) <= 150            # шаг 0.001 BTC = 50$, округление вверх
    asyncio.run(dt.layers_target(1.15, "d2"))                # добрать 15$ нельзя: меньше минимума, без сделки
    assert conn.execute("SELECT COUNT(*) FROM demo_trades").fetchone()[0] == 1


def test_loop_calculates_at_start(cfg, conn, data, tmp_path, monkeypatch):
    """При запуске бота модель считается сразу, не дожидаясь ночи, и печатает позицию."""
    settings.put(conn, "paper.enabled", True)
    dt = DemoTrader(cfg, conn, prices=lambda ex, s: 60000.0)
    sv = bl.LayersService(cfg, conn, Pub(), dt)
    sv.report_path = str(tmp_path / "bl.json")
    monkeypatch.setattr(bl, "load", lambda refresh=False: data)
    said = []

    async def run():
        t = asyncio.create_task(sv.loop(said.append, every_s=3600))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if said:
                break
        t.cancel()
    asyncio.run(run())
    assert said and said[0].startswith("✅ BTC Layers:") and settings.get(conn, "btc_layers_state")
    assert dt.has("layers")


def test_positions_become_trades_like_other_signals():
    """Посты модели в ленте как сделки: вход, смены размера, разворот или кэш, итог в % депозита."""
    pts = [{"t": 1, "signal": -1.0, "px": 100.0}, {"t": 2, "signal": -0.5, "px": 90.0},
           {"t": 3, "signal": 0.8, "px": 80.0}, {"t": 4, "signal": 0.0, "px": 88.0}, {"t": 5, "signal": 1.0, "px": 90.0}]
    L = bl.segments(pts, mark=99.0, fee_pct=0)
    assert [x["side"] for x in L] == ["short", "long", "long"]
    a, b, c = L
    assert [e["what"] for e in a["exits"]] == ["resize", "flip"] and a["result_pct"] == pytest.approx(10 + 0.5 * 100 * (1 - 80 / 90), abs=0.01)
    assert b["exits"][-1]["what"] == "cash" and b["result_pct"] == pytest.approx(8.0)
    assert c["open"] and c["result_pct"] == pytest.approx(10.0) and c["size_now"] == 100.0
    days = [{"t": i, "pos": p, "px": 100.0} for i, p in enumerate([0.5, 0.6, 0.8, 0.8, -0.2])]
    assert [p["signal"] for p in bl.posted_points(days)] == [0.5, 0.8, -0.2]      # мелкие изменения размера без поста
