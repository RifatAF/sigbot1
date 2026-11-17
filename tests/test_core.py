import sqlite3

import numpy as np
import pandas as pd
import pytest

from sigbot import db, indicators as ind, ledger, risk, textguard


@pytest.fixture
def conn():
    return db.connect(":memory:")


def _sig(**kw):
    base = dict(params_version="v1", ts=1, symbol="BTCUSDT", side="long", entry=100.0,
                stop=98.0, tp1=103.0, tp2=106.0, risk_pct=1.0, leverage=0.5,
                strength=0.7, mode="paper", features={"index": 0.4})
    base.update(kw)
    return ledger.Signal(**base)


# ---------- журнал ----------

def test_signal_update_and_delete_fail(conn):
    sid = ledger.add_signal(conn, _sig())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE signals SET entry=1 WHERE id=?", (sid,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM signals WHERE id=?", (sid,))


def test_outcome_append_only(conn):
    sid = ledger.add_signal(conn, _sig())
    oid = ledger.add_outcome(conn, sid, 2, "tp1", 103.0, 0.5, 1.5)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE outcomes SET price=0 WHERE id=?", (oid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM outcomes")


def test_params_version_frozen(conn):
    ledger.register_params(conn, "v1", 1, {"a": 1})
    ledger.register_params(conn, "v1", 2, {"a": 1})  # тот же набор, ок
    with pytest.raises(ValueError):
        ledger.register_params(conn, "v1", 3, {"a": 2})


def test_export_csv(conn, tmp_path):
    sid = ledger.add_signal(conn, _sig())
    ledger.add_outcome(conn, sid, 2, "stop", 98.0, 1.0, -1.0)
    paths = ledger.export_csv(conn, tmp_path)
    text = paths[0].read_text()
    assert "BTCUSDT" in text and "params_version" in text.splitlines()[0]


# ---------- риск ----------

def test_sizing_basic():
    s = risk.size(100, 98, "long", strength=0.5, deposit=1000, rmin=0.5, rmax=1.5, lev_cap=25)
    assert s.risk_pct == 1.0
    assert s.position_usd == pytest.approx(500)  # 10$ риска / 2% стопа
    assert s.leverage == pytest.approx(0.5)
    assert not s.capped


def test_sizing_tight_stop_caps_leverage_not_signal():
    s = risk.size(100, 99.98, "long", strength=1.0, deposit=1000, rmin=0.5, rmax=1.5, lev_cap=25)
    assert s.capped and s.leverage == 25
    assert s.risk_pct < 1.5


def test_short_and_liq_beyond_stop():
    s = risk.size(100, 103, "short", strength=0.0, deposit=1000, rmin=0.5, rmax=1.5, lev_cap=25)
    assert s.risk_pct == 0.5
    assert risk.stop_before_liquidation(s, 103, "short")


def test_wrong_side_stop():
    with pytest.raises(ValueError):
        risk.size(100, 101, "long", 0.5, 1000, 0.5, 1.5, 25)


# ---------- индикаторы ----------

def _wave(prices):
    s = pd.Series(prices, dtype=float)
    return s, s


def test_regular_bullish_divergence():
    # цена: два минимума, второй ниже; осциллятор: второй минимум выше
    price = [10, 9, 8, 7, 8, 9, 10, 9, 8, 6, 7, 8, 9, 10]
    osc = [50, 45, 40, 30, 40, 45, 50, 45, 40, 35, 40, 45, 50, 55]
    hi, lo = _wave(price)
    d = ind.divergences(hi, lo, pd.Series(osc, dtype=float), "rsi", left=2, right=2, max_age=10)
    assert any(x.kind == "regular" and x.direction == "bullish" for x in d)


def test_hidden_bearish_divergence():
    price = [5, 6, 7, 9, 7, 6, 5, 6, 7, 8, 7, 6, 5, 4]
    osc = [40, 50, 60, 65, 60, 50, 40, 50, 60, 70, 60, 50, 40, 30]
    hi, lo = _wave(price)
    d = ind.divergences(hi, lo, pd.Series(osc, dtype=float), "cvd_spot", left=2, right=2, max_age=10)
    assert any(x.kind == "hidden" and x.direction == "bearish" for x in d)


def test_stale_divergence_ignored():
    price = [10, 9, 8, 7, 8, 9, 10, 9, 8, 6, 7, 8] + [9 + i * 0.1 for i in range(30)]
    osc = [50, 45, 40, 30, 40, 45, 50, 45, 40, 35, 40, 45] + [50] * 30
    hi, lo = _wave(price)
    d = ind.divergences(hi, lo, pd.Series(osc, dtype=float), "rsi", left=2, right=2, max_age=5)
    assert not any(x.direction == "bullish" for x in d)


def test_cvd_from_candles():
    c = ind.cvd(pd.Series([10.0, 10.0]), pd.Series([7.0, 2.0]))
    assert c.tolist() == [4.0, -2.0]


def test_rsi_bounds():
    r = ind.rsi(pd.Series(np.linspace(1, 100, 50)))
    assert 0 <= r.min() and r.max() <= 100 and r.iloc[-1] > 90


def test_liquidation_map_levels_and_consumption():
    close = pd.Series([100.0, 100.0, 100.0])
    oi = pd.Series([0.0, 10.0, 10.0])  # +10 монет по 100$ = 1000$
    m = ind.liquidation_map(close, oi, leverages=(10,), bin_pct=0.5, mm_pct=0.5)
    assert m.long_usd.sum() == pytest.approx(500)
    assert m.short_usd.sum() == pytest.approx(500)
    assert (m[m.long_usd > 0].price < 100).all() and (m[m.short_usd > 0].price > 100).all()
    # цена падает на 15%: лонги с x10 ликвидированы
    m2 = ind.liquidation_map(pd.Series([100.0, 100.0, 85.0]), oi, leverages=(10,), bin_pct=0.5)
    assert m2.long_usd.sum() == 0


def test_composite_index_range_and_missing_columns():
    rng = np.random.default_rng(0)
    n = 300
    df = pd.DataFrame({
        "close": 100 + rng.normal(0, 1, n).cumsum(),
        "funding": rng.normal(0, 1e-4, n),
        "oi": 1e6 + rng.normal(0, 1e3, n).cumsum(),
    })
    idx = ind.composite_index(df, {"oi": 1, "funding": 1, "premium": 1}, 100)
    assert idx.between(-1, 1).all()


def test_negative_funding_is_bullish():
    n = 200
    f = np.full(n, 1e-4)
    f[-1] = -5e-4
    df = pd.DataFrame({"close": np.full(n, 100.0), "funding": f})
    idx = ind.composite_index(df, {"funding": 1}, 100)
    assert idx.iloc[-1] > 0.5


# ---------- сверка текста ----------

def test_textguard_blocks_unknown_number():
    textguard.verify("BTC long entry 100 stop 98 TP1 103 risk 1.0%", [100, 98, 103, 1.0])
    with pytest.raises(textguard.NumberMismatch):
        textguard.verify("BTC long entry 100 stop 97", [100, 98])


def test_llm_comment_no_digits():
    assert textguard.llm_text_has_no_digits("Funding flipped negative while spot keeps buying.")
    assert not textguard.llm_text_has_no_digits("Target around 70k")


def test_risk_guard_throttles_and_caps(cfg):
    from sigbot import risk
    rc = dict(cfg["risk"], risk_min_pct=0.5, risk_max_pct=1.5, max_open_trades=3, max_open_risk_pct=3.0, dd_half_pct=10, dd_pause_pct=20,
              loss_streak=3, min_stop_pct=1.5)
    assert risk.guard(rc, [1, 1], []).multiplier == 1.0
    assert risk.guard(rc, [5, -12], []).multiplier == 0.5                    # просадка больше 10%
    assert not risk.guard(rc, [5, -25], []).allow                            # просадка больше 20%
    day = 86_400_000
    assert not risk.guard(dict(rc, dd_pause_days=7), [(0, 5), (day, -25)], [], 3 * day).allow
    assert risk.guard(dict(rc, dd_pause_days=7), [(0, 5), (day, -25)], [], 9 * day).allow   # пауза отбыта
    assert risk.guard(rc, [2, -0.5, -0.5, -0.5], []).multiplier == 0.5       # три убытка подряд
    assert not risk.guard(rc, [], [1, 1, 1]).allow                           # три открытые сделки
    g = risk.guard(rc, [], [1.5, 1.0])
    sz = risk.size_guarded(100, 97, "long", 1.0, 1000, rc, g, [1.5, 1.0])
    assert sz.risk_pct == 0.5                                                # остаток лимита 0.5%
    assert risk.size_guarded(100, 99, "long", 1.0, 1000, rc, risk.guard(rc, [], []), []) is None  # стоп 1%
