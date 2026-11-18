"""Единая лента сигналов: позиции трендового портфеля (крупные), часовые капитуляции и шорты листингов
в одном формате. Позиция портфеля становится сигналом, если при открытии она весит от signal_min_weight депозита.
Сигнал торгуется фиксированным размером до выхода: так его может повторить человек, бэктест считается так же."""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import portfolio

DAY = 86_400_000
FEED = Path("reports/feed.json")


def consensus_at(c: np.ndarray, p: float, pc: dict) -> float:
    """Сила тренда (−1..1), если следующее дневное закрытие будет p. c: прошлые закрытия, последнее в конце."""
    x = np.append(c, p)
    neg = -1.0 if pc.get("allow_short", True) else 0.0
    votes = []
    for lb in pc["donchian"]:
        w = x[-lb:]
        votes.append(1.0 if p >= (w.max() + w.min()) / 2 else neg)
    ma = 1.0 if x[-pc["ma_fast"]:].mean() > x[-pc["ma_slow"]:].mean() else neg
    return (sum(votes) / len(votes) + ma) / 2


def exit_level(c: np.ndarray, side: int, pc: dict) -> float | None:
    """Цена дневного закрытия, на которой тренд ломается (согласие голосов падает ниже порога) и позиция закрывается."""
    c = np.asarray(c, float)
    c = c[~np.isnan(c)]
    if len(c) < max(pc["donchian"]):
        return None
    th, last = pc["min_consensus"], c[-1]
    ok = lambda p: consensus_at(c, p, pc) * side >= th
    if not ok(last):
        return last
    lo, hi = (last * 0.2, last) if side > 0 else (last, last * 5)       # long: ищем вниз, short: вверх
    bad, good = (lo, hi) if side > 0 else (hi, lo)
    if ok(bad):
        return None
    for _ in range(40):
        mid = (bad + good) / 2
        if ok(mid):
            good = mid
        else:
            bad = mid
    return float(bad)


def trend_positions(W: pd.DataFrame, C: pd.DataFrame, pc: dict, start: int, end: int, fee_pct: float) -> list[dict]:
    """Позиции портфеля от signal_min_weight: вход по закрытию дня решения, выход по закрытию дня, когда вес обнулился или сменил знак."""
    thr = pc.get("signal_min_weight", 0.1)
    out = []
    idx = W.index.values
    for sym in W.columns:
        x = W[sym].values
        cl = C[sym].values
        sg = np.sign(np.where(np.abs(x) >= 0.001, x, 0))
        i, n = 0, len(x)
        while i < n:
            if sg[i] == 0:
                i += 1
                continue
            j = i
            while j + 1 < n and sg[j + 1] == sg[i]:
                j += 1
            if abs(x[i]) >= thr and start <= idx[i] < end and not np.isnan(cl[i]):
                closed = j + 1 < n
                k = j + 1 if closed else n - 1
                kk = k
                while kk > i and np.isnan(cl[kk]):      # монету сняли с торгов: последняя известная цена
                    kk -= 1
                ex = float(cl[kk])
                move = (ex / cl[i] - 1) * 100 * sg[i]
                size = abs(x[i]) * 100
                item = {"kind": "trend", "symbol": sym, "side": "long" if sg[i] > 0 else "short",
                        "entry_time": int(idx[i]) // 1000 + 86400, "entry": float(cl[i]), "size_pct": round(size, 1),
                        "exit_time": int(idx[k]) // 1000 + 86400 if closed else None, "exit": ex,
                        "move_pct": round(move, 2), "result_pct": round(size * (move - 2 * fee_pct) / 100, 3),
                        "exit_reason": "тренд сломался" if closed else None, "open": not closed}
                if not closed:
                    item["exit_level"] = exit_level(C[sym].values[:n], int(sg[i]), pc)
                out.append(item)
            i = j + 1
    return out


def fixed_weights(W: pd.DataFrame, thr: float) -> pd.DataFrame:
    """Веса, если торговать только сигналы фиксированным размером входа (для честной доходности ленты)."""
    out = np.zeros(W.shape)
    X = W.values
    for c in range(X.shape[1]):
        x = X[:, c]
        sg = np.sign(np.where(np.abs(x) >= 0.001, x, 0))
        i = 0
        while i < len(x):
            if sg[i] == 0:
                i += 1
                continue
            j = i
            while j + 1 < len(x) and sg[j + 1] == sg[i]:
                j += 1
            if abs(x[i]) >= thr:
                out[i:j + 1, c] = x[i]
            i = j + 1
    return pd.DataFrame(out, index=W.index, columns=W.columns)


def summary(items: list[dict], days: float) -> dict:
    done = [x for x in items if not x.get("open")]
    res = [x["result_pct"] for x in done if x.get("result_pct") is not None]
    win = [r for r in res if r > 0]
    loss = [r for r in res if r <= 0]
    return {"signals": len(items), "per_month": round(len(items) / max(days, 1) * 30.4, 1), "open": len(items) - len(done),
            "win_pct": round(len(win) / len(res) * 100) if res else 0,
            "avg_win": round(float(np.mean(win)), 2) if win else 0, "avg_loss": round(float(np.mean(loss)), 2) if loss else 0,
            "sum_pct": round(float(np.nansum(res)), 1)}


def build(conn, cfg, start="2020-07-01", end=None) -> dict:
    from .backtest import _ms
    from .dashboard import latest_backtest
    pc = cfg["portfolio"]
    s = _ms(start)
    e = int(time.time() * 1000) if end is None else _ms(end)
    C, V, FR = portfolio.daily_frames(conn, None, s - 400 * DAY, e)
    W = portfolio.weights(C, V, pc, FR=FR)
    fee = cfg["trade"]["fee_taker_pct"]
    trend = trend_positions(W, C, pc, s, e, fee)
    d = portfolio.simulate(C, FR, fixed_weights(W, pc.get("signal_min_weight", 0.1)), fee)
    st = portfolio.stats(d[(d.index >= s) & (d.index < e)])
    rep = latest_backtest()
    hourly = []
    for t in (rep or {}).get("trades", []):
        hourly.append({"kind": "capitulation", "symbol": t["symbol"], "side": t["side"], "entry_time": t["entry_ts"] // 1000 - 3600,
                       "exit_time": t["exit_ts"] // 1000, "entry": t["entry"], "stop": t["stop"], "tp1": t["tp1"], "tp2": t["tp2"],
                       "size_pct": None, "risk_pct": t["risk_pct"], "leverage": t["leverage"], "r": t["r"],
                       "result_pct": t["pnl_pct"], "exit_reason": t["exit_event"], "open": False,
                       "exit": (t.get("exits") or [[0, 0, None]])[-1][2]})
    ov = Path("reports/overview.json")
    listing = []
    if ov.exists():
        lc = cfg["listings"]
        for t in json.loads(ov.read_text()).get("listing_trades", []):
            listing.append({"kind": "listing", "symbol": t["symbol"], "side": "short", "entry_time": t["entry_t"] // 1000,
                            "exit_time": None if t["reason"] == "open" else t["exit_t"] // 1000, "entry": t["entry"],
                            "exit": t["exit"], "stop": t["entry"] * (1 + lc["stop_pct"] / 100), "size_pct": lc["notional_pct"],
                            "move_pct": round(-(t["exit"] / t["entry"] - 1) * 100, 2), "result_pct": t["pnl_pct"],
                            "exit_reason": {"stop": "стоп", "time": "по времени", "delisted": "делистинг"}.get(t["reason"]),
                            "open": t["reason"] == "open"})
    items = sorted(trend + hourly + listing, key=lambda x: x["entry_time"])
    days = (e - s) / DAY
    return {"generated": int(time.time()), "period": [start, time.strftime("%Y-%m-%d", time.gmtime(e / 1000))],
            "items": items, "summary": {"all": summary(items, days), "trend": summary(trend, days),
                                        "capitulation": summary(hourly, days), "listing": summary(listing, days)},
            "trend_equity": {"cagr_pct": st["cagr_pct"], "max_dd_pct": st["max_drawdown_pct"], "years": st.get("years")}}


def _clean(x):
    """NaN и numpy в обычные значения: браузер не разбирает NaN в JSON."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    if isinstance(x, (float, np.floating)):
        return None if np.isnan(x) or np.isinf(x) else round(float(x), 8)
    if isinstance(x, np.integer):
        return int(x)
    return x


def save(rep: dict):
    FEED.parent.mkdir(exist_ok=True)
    FEED.write_text(json.dumps(_clean(rep), allow_nan=False))
