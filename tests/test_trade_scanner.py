import pandas as pd
import pytest

from sigbot import scanner
from sigbot.engine import Candidate
from sigbot.trade import TradeState, pnl_pct, step


def st_long():
    # entry 100, stop 98 (1R = 2), invalidation 98.5, tp1 102.4, tp2 105
    return TradeState("long", 100.0, 98.0, 98.5, 102.4, 105.0, opened_ts=0)


def test_tp1_then_breakeven_then_be_stop(cfg):
    st = st_long()
    ev = step(st, 1, 102.6, 100.5, 102.0, cfg)
    assert [e.event for e in ev] == ["breakeven", "tp1"] or [e.event for e in ev] == ["tp1", "breakeven"]
    assert st.remaining == 0.5 and st.stop == 100.0
    ev = step(st, 2, 101.0, 99.9, 100.2, cfg)
    assert ev[0].event == "breakeven_stop" and st.status == "closed"


def test_stop_checked_before_targets(cfg):
    st = st_long()
    ev = step(st, 1, 106.0, 97.0, 104.0, cfg)  # бар задел и стоп, и tp2
    assert ev[0].event == "stop" and st.status == "closed"


def test_breakeven_halfway_to_tp2(cfg):
    st = st_long()
    step(st, 1, 102.3, 100.1, 102.0, cfg)  # tp1 не достигнут, но > половины пути к tp2 (102.5)? нет
    assert not st.be_moved
    step(st, 2, 102.39, 100.1, 102.0, cfg)
    assert not st.be_moved
    st2 = TradeState("long", 100.0, 98.0, 98.5, 104.0, 105.0, 0)
    step(st2, 1, 102.6, 100.1, 102.0, cfg)
    assert st2.be_moved and st2.stop == 100.0 and st2.remaining == 1.0


def test_invalidation_on_close_only(cfg):
    st = st_long()
    step(st, 1, 100.2, 98.2, 99.5, cfg)  # фитиль ниже invalidation, закрытие выше
    assert st.status == "open"
    ev = step(st, 2, 99.0, 98.2, 98.4, cfg)
    assert ev[-1].event == "invalidation" and st.status == "closed"
    st2 = st_long()
    step(st2, 1, 99.0, 98.2, 98.4, cfg, bar_closed=False)  # минутный бар внутри часа
    assert st2.status == "open"


def test_timeout(cfg):
    st = st_long()
    ev = step(st, cfg["engine"]["max_hold_hours"] * 3_600_000, 100.5, 99.5, 100.1, cfg)
    assert ev[-1].event == "timeout"


def test_short_side(cfg):
    st = TradeState("short", 100.0, 102.0, 101.5, 97.6, 95.0, 0)
    ev = step(st, 1, 100.5, 94.0, 95.5, cfg)
    assert [e.event for e in ev][0] == "tp1" and st.status == "closed"
    assert ev[-1].event == "tp2"


def test_pnl_includes_fees_and_funding():
    st = st_long()
    st.exits = [(1, "tp1", 102.4, 0.5), (2, "tp2", 105.0, 0.5)]
    p = pnl_pct(st, leverage=2.0, fee_pct=0.05, funding_paid_pct=0.1)
    gross = (0.5 * 0.024 + 0.5 * 0.05) * 2 * 100
    assert p == pytest.approx(gross - 2 * 0.05 * 2 - 0.1)


def _c(sym, side, strength):
    return Candidate(sym, 0, side, 100, 98, 98.5, 102, 105, strength)


def test_select_daily_cap_and_rank():
    out = scanner.select([_c("A", "long", 0.6), _c("B", "short", 0.9), _c("C", "long", 0.7)], [], 0, 2, None, 0.8)
    assert [c.symbol for c in out] == ["B", "C"]
    assert scanner.select([_c("A", "long", 0.6)], [], 2, 2, None, 0.8) == []


def test_correlated_same_side_is_one_bet():
    corr = pd.DataFrame([[1, 0.9, 0.1], [0.9, 1, 0.1], [0.1, 0.1, 1]], index=list("ABC"), columns=list("ABC"))
    out = scanner.select([_c("A", "long", 0.9), _c("B", "long", 0.8), _c("C", "long", 0.7)], [], 0, 5, corr, 0.8)
    assert [c.symbol for c in out] == ["A", "C"]
    # противоположная сторона по коррелированной монете разрешена
    out = scanner.select([_c("B", "short", 0.8)], [("A", "long")], 0, 5, corr, 0.8)
    assert [c.symbol for c in out] == ["B"]
    # уже открыта сделка по символу
    assert scanner.select([_c("A", "short", 0.8)], [("A", "long")], 0, 5, corr, 0.8) == []
