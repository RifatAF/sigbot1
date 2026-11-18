"""Единая лента сигналов: позиции трендового портфеля (крупные), часовые капитуляции и шорты листингов
в одном формате. Позиция портфеля становится сигналом, если при открытии она весит от signal_min_weight депозита.
Сигнал торгуется фиксированным размером до выхода: так его может повторить человек, бэктест считается так же.

План сделки тренда: стоп и две цели от дневного ATR. На TP1 и TP2 закрывается по части позиции,
остаток держится, пока жив тренд. Проверка на истории: цели снижают итог примерно на треть против чистого
тренда (крупные движения отдаются раньше), зато сделка понятна и часть прибыли фиксируется."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import portfolio

log = logging.getLogger(__name__)
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


PLAN = {"sl_atr": 2.0, "tp1_atr": 2.0, "tp2_atr": 4.0, "tp1_part": 0.25, "tp2_part": 0.25, "atr_days": 14}


def plan_cfg(pc: dict) -> dict:
    return {**PLAN, **(pc.get("plan") or {})}


def daily_ohlc(conn, start: int, end: int, symbols: list[str] | None = None):
    """Дневные максимум, минимум и закрытие из часовых свечей."""
    q = "SELECT symbol, ts, high, low, close FROM candles WHERE market='futures' AND tf='1h' AND ts>=? AND ts<?"
    args: list = [start, end]
    if symbols:
        q += f" AND symbol IN ({','.join('?' * len(symbols))})"
        args += symbols
    h = pd.read_sql_query(q, conn, params=args)
    if h.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    h["day"] = h.ts // DAY * DAY
    g = h.groupby(["symbol", "day"])
    return g.high.max().unstack(0).sort_index(), g.low.min().unstack(0).sort_index(), g.close.last().unstack(0).sort_index()


def atr(H: pd.DataFrame, L: pd.DataFrame, C: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    pc = C.shift()
    tr = np.maximum(H - L, np.maximum((H - pc).abs(), (L - pc).abs()))
    return tr.rolling(n, min_periods=max(3, n // 2)).mean()


def levels(entry: float, a: float, side: int, pl: dict) -> dict:
    """Стоп и цели от ATR: long стоп ниже входа, цели выше; short наоборот."""
    return {"stop": entry - side * pl["sl_atr"] * a, "tp1": entry + side * pl["tp1_atr"] * a,
            "tp2": entry + side * pl["tp2_atr"] * a, "tp1_part": pl["tp1_part"], "tp2_part": pl["tp2_part"]}


def new_state(side: str, entry: float, lv: dict) -> dict:
    return {"side": side, "entry": entry, **lv, "rem": 1.0, "exits": []}


def step(st: dict, t: int, hi: float, lo: float) -> list[dict]:
    """Одна свеча: сначала стоп (осторожно, если в свече и стоп, и цель), потом TP1, потом TP2.
    Возвращает новые события: {what: tp1|tp2|sl, t, price, part}. st меняется на месте."""
    if st["rem"] <= 1e-9 or hi is None or lo is None or np.isnan(hi) or np.isnan(lo):
        return []
    sd = 1 if st["side"] == "long" else -1
    fav, adv = (hi, lo) if sd > 0 else (lo, hi)
    done = {x["what"] for x in st["exits"]}
    out = []
    if (adv - st["stop"]) * sd <= 0:
        out.append({"what": "sl", "t": int(t), "price": st["stop"], "part": st["rem"]})
        st["rem"] = 0.0
    else:
        for k in ("tp1", "tp2"):
            if k not in done and st.get(k) and (fav - st[k]) * sd >= 0:
                part = min(st[f"{k}_part"], st["rem"])
                out.append({"what": k, "t": int(t), "price": st[k], "part": part})
                st["rem"] -= part
    st["exits"] += out
    return out


def finish(st: dict, t: int | None, price: float, what: str) -> list[dict]:
    """Закрыть остаток (тренд сломался, время, текущая цена для открытой)."""
    if st["rem"] <= 1e-9:
        return []
    ev = {"what": what, "t": None if t is None else int(t), "price": float(price), "part": st["rem"]}
    st["exits"].append(ev)
    st["rem"] = 0.0
    return [ev]


def realized(st: dict, size_pct: float, fee_pct: float, mark: float | None = None) -> tuple[float, float]:
    """Средний ход цены в нашу сторону, % (взвешенный по частям) и итог в % депозита после комиссий."""
    sd = 1 if st["side"] == "long" else -1
    mv = sum(x["part"] * (x["price"] / st["entry"] - 1) * sd for x in st["exits"])
    if mark is not None and st["rem"] > 1e-9:
        mv += st["rem"] * (mark / st["entry"] - 1) * sd
    mv *= 100
    return round(mv, 2), round(size_pct * (mv - 2 * fee_pct) / 100, 3)


def with_plan(items: list[dict], H, L, C, pc: dict, fee_pct: float) -> list[dict]:
    """Добавляет сделкам тренда стоп, цели и прохождение по дневным свечам."""
    pl = plan_cfg(pc)
    A = atr(H, L, C, pl["atr_days"])
    idx = C.index.values
    for it in items:
        sym, d0 = it["symbol"], it["entry_time"] * 1000 - DAY
        if sym not in A.columns or d0 not in A.index or np.isnan(A.at[d0, sym]):
            continue
        sd = 1 if it["side"] == "long" else -1
        st = new_state(it["side"], it["entry"], levels(it["entry"], float(A.at[d0, sym]), sd, pl))
        d1 = it["exit_time"] * 1000 - DAY if it["exit_time"] else idx[-1]
        i0, i1 = np.searchsorted(idx, d0) + 1, np.searchsorted(idx, d1)
        for i in range(i0, min(i1 + 1, len(idx))):
            step(st, idx[i] // 1000 + 86400 - 1, H[sym].iat[i], L[sym].iat[i])
            if st["rem"] <= 1e-9:
                break
        if it["open"] and st["rem"] > 1e-9:
            mv, res = realized(st, it["size_pct"], fee_pct, mark=it["exit"])
        else:
            finish(st, it["exit_time"], it["exit"], "trend")
            mv, res = realized(st, it["size_pct"], fee_pct)
        last = st["exits"][-1] if st["exits"] else None
        it.update({k: st[k] for k in ("stop", "tp1", "tp2", "tp1_part", "tp2_part")}, atr=float(A.at[d0, sym]))
        it.update(exits=st["exits"], rem=round(st["rem"], 4), move_pct=mv, result_pct=res)
        if st["rem"] <= 1e-9 and last:                               # стоп или все части закрыты раньше тренда
            it.update(open=False, exit_time=last["t"], exit=last["price"])
    return items


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


QS = (10, 25, 50, 75, 90)


def _quant(M: list[list[float]], min_n: int = 30) -> dict | None:
    """Квантили хода цены по шагам после входа. Ход в нашу сторону, в единицах сигнала (ATR, риск, %)."""
    if len(M) < min_n:
        return None
    A = np.array([r + [np.nan] * (max(map(len, M)) - len(r)) for r in M], float)
    ok = np.sum(~np.isnan(A), 0) >= min_n
    A = A[:, ok]
    return {"n": len(M), "q": {str(q): [round(float(v), 3) for v in np.nanpercentile(A, q, axis=0)] for q in QS}}


def paths(conn, trend: list[dict], hourly: list[dict], CL: pd.DataFrame) -> dict:
    """Обычный путь цены после сигнала: как шла цена у всех прошлых сигналов этого типа (медиана и разброс).
    Это не прогноз конкретной монеты, а статистика: в половине случаев цена была выше медианы, в половине ниже."""
    out = {}
    M = []
    for it in trend:
        a, d0 = it.get("atr"), it["entry_time"] * 1000 - DAY
        if not a or it["symbol"] not in CL or d0 not in CL.index:
            continue
        sd = 1 if it["side"] == "long" else -1
        x = CL[it["symbol"]].loc[d0:d0 + 30 * DAY].values
        M.append([float(v) for v in (x / it["entry"] - 1) * sd * it["entry"] / a])
    r = _quant(M)
    if r:
        out["trend"] = {**r, "step": 86400, "unit": "atr"}
    M = []
    for it in hourly:
        risk = abs(it["entry"] - (it.get("stop") or it["entry"]))
        if not risk:
            continue
        t0 = (it["entry_time"] + 3600) * 1000
        x = [c for (c,) in conn.execute("SELECT close FROM candles WHERE symbol=? AND market='futures' AND tf='1h' "
                                         "AND ts>=? AND ts<? ORDER BY ts", (it["symbol"], t0 - 3_600_000, t0 + 72 * 3_600_000))]
        sd = 1 if it["side"] == "long" else -1
        if x:
            M.append([(c / it["entry"] - 1) * sd * it["entry"] / risk for c in x])
    r = _quant(M, 20)
    if r:
        out["capitulation"] = {**r, "step": 3600, "unit": "risk"}
    try:
        from .listings_bt import CACHE
        if CACHE.exists():
            D, M = json.loads(CACHE.read_text()), []
            for s, v in D.items():
                if len(v) > 3 and not v[1][4] < v[0][1]:              # те же условия входа: слабый первый день
                    continue
                if len(v) > 3:
                    e = v[1][4]
                    M.append([(1 - r_[4] / e) * 100 for r_ in v[1:62]])
            r = _quant(M)
            if r:
                out["listing"] = {**r, "step": 86400, "unit": "pct"}
    except Exception as e:
        log.warning("listing paths: %s", e)
    return out


def equity_stats(items: list[dict]) -> dict:
    """Сумма итогов закрытых сигналов по дате выхода: доход по годам и максимальная просадка (в % депозита, без сложного %)."""
    done = sorted((x["exit_time"], x["result_pct"]) for x in items if not x.get("open") and x.get("exit_time")
                  and x.get("result_pct") is not None)
    if not done:
        return {}
    eq = np.cumsum([r for _, r in done])
    dd = float(np.min(eq - np.maximum.accumulate(np.maximum(eq, 0))))
    years: dict = {}
    for t, r in done:
        y = time.strftime("%Y", time.gmtime(t))
        years[y] = round(years.get(y, 0) + r, 1)
    span = max((done[-1][0] - done[0][0]) / 86400 / 365.25, 0.1)
    return {"per_year_pct": round(float(eq[-1]) / span, 1), "max_dd_pct": round(dd, 1), "years": years}


CAP_EVENTS = {"tp1": "tp1", "tp2": "tp2", "stop": "sl", "breakeven_stop": "be", "timeout": "time",
              "invalidation": "invalid", "trail_stop": "trail", "manual": "manual"}


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
    H, L, CL = daily_ohlc(conn, s - 60 * DAY, e)
    trend = with_plan(trend, H, L, CL, pc, fee)
    rep = latest_backtest()
    hourly = []
    for t in (rep or {}).get("trades", []):
        hourly.append({"kind": "capitulation", "symbol": t["symbol"], "side": t["side"], "entry_time": t["entry_ts"] // 1000 - 3600,
                       "exit_time": t["exit_ts"] // 1000, "entry": t["entry"], "stop": t["stop"], "tp1": t["tp1"], "tp2": t["tp2"],
                       "size_pct": None, "risk_pct": t["risk_pct"], "leverage": t["leverage"], "r": t["r"],
                       "result_pct": t["pnl_pct"], "exit_reason": t["exit_event"], "open": False,
                       "exit": (t.get("exits") or [[0, 0, None]])[-1][2], "tp1_part": 0.5, "tp2_part": 0.5,
                       "exits": [{"what": CAP_EVENTS.get(x[1], x[1]), "t": x[0] // 1000, "price": x[2],
                                  "part": x[3] if len(x) > 3 else None} for x in t.get("exits") or []]})
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
                            "open": t["reason"] == "open",
                            "exits": [] if t["reason"] == "open" else
                            [{"what": {"stop": "sl", "time": "time", "delisted": "delist"}.get(t["reason"], t["reason"]),
                              "t": t["exit_t"] // 1000, "price": t["exit"], "part": 1.0}]})
    from . import settings
    from .listings_bt import BYBIT_REPORT
    if settings.effective(cfg, conn)["listings"].get("bybit") and BYBIT_REPORT.exists():
        lc = cfg["listings"]
        for t in json.loads(BYBIT_REPORT.read_text()).get("trades", []):
            listing.append({"kind": "listing", "exchange": "bybit", "symbol": t["symbol"], "side": "short",
                            "entry_time": t["entry_t"] // 1000, "exit_time": None if t["reason"] == "open" else t["exit_t"] // 1000,
                            "entry": t["entry"], "exit": t["exit"], "stop": t["entry"] * (1 + lc["stop_pct"] / 100),
                            "size_pct": lc["notional_pct"], "move_pct": round(-(t["exit"] / t["entry"] - 1) * 100, 2),
                            "result_pct": t["pnl_pct"], "open": t["reason"] == "open",
                            "exits": [] if t["reason"] == "open" else
                            [{"what": {"stop": "sl"}.get(t["reason"], t["reason"]), "t": t["exit_t"] // 1000, "price": t["exit"], "part": 1.0}]})
    items = sorted(trend + hourly + listing, key=lambda x: x["entry_time"])
    days = (e - s) / DAY
    return {"generated": int(time.time()), "period": [start, time.strftime("%Y-%m-%d", time.gmtime(e / 1000))],
            "items": items, "summary": {"all": summary(items, days), "trend": summary(trend, days),
                                        "capitulation": summary(hourly, days), "listing": summary(listing, days)},
            "plan": plan_cfg(pc), "trend_equity": equity_stats(trend), "paths": paths(conn, trend, hourly, CL)}


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
