"""Сигнальный движок: признаки -> кандидат в сделку. Детерминированно, одинаково для бэктеста и живой работы.

Правила v1:
* направление задают дивергенции (RSI, CVD фьючерсов, CVD спота, OI), бычьи за лонг, медвежьи за шорт;
* композитный индекс должен быть на той же стороне;
* сделки только по тренду 4ч (тренд 0 разрешает обе стороны);
* индекс страха и жадности как фильтр: экстремальная жадность запрещает лонг, экстремальный страх шорт;
* стоп за экстремумом дивергенции плюс буфер ATR, tp1 = 1.2R, tp2 = 2.5R или ближайший кластер ликвидаций.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import indicators as ind

OSC_NAMES = ("rsi", "cvd_fut", "cvd_spot", "oi")


@dataclass
class Candidate:
    symbol: str
    ts: int
    side: str
    entry: float
    stop: float
    invalidation: float
    tp1: float
    tp2: float
    strength: float
    features: dict = field(default_factory=dict)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _pivot_flags(s: pd.Series, left: int, right: int, kind: str) -> np.ndarray:
    w = left + right + 1
    roll = s.rolling(w, center=False)
    ext = (roll.max() if kind == "high" else roll.min()).shift(-right)
    flag = (s == ext).to_numpy().copy()
    flag[:left] = False
    flag[len(s) - right:] = False
    return np.flatnonzero(flag)


def divergence_table(df: pd.DataFrame, oscs: dict[str, pd.Series], left: int, right: int,
                     lookback: int, max_age: int) -> pd.DataFrame:
    """Для каждого бара t: бычий/медвежий балл и метки дивергенций, известные на закрытии t.

    Экстремум i подтверждён на баре i + right. Используются два последних подтверждённых
    экстремума в окне lookback, последний не старше max_age баров.
    """
    n = len(df)
    hi, lo = df.high.to_numpy(), df.low.to_numpy()
    ph = _pivot_flags(df.high, left, right, "high")
    pl = _pivot_flags(df.low, left, right, "low")
    bull = np.zeros(n)
    bear = np.zeros(n)
    labels: list[list[str]] = [[] for _ in range(n)]
    bull_anchor = np.full(n, np.nan)
    bear_anchor = np.full(n, np.nan)
    ov = {k: v.to_numpy() for k, v in oscs.items()}

    for t in range(n):
        lim = t - right
        k = np.searchsorted(pl, lim, side="right")
        if k >= 2:
            a, b = pl[k - 2], pl[k - 1]
            if t - b <= max_age and t - a <= lookback:
                for name, o in ov.items():
                    if np.isnan(o[a]) or np.isnan(o[b]):
                        continue
                    if lo[b] < lo[a] and o[b] > o[a]:
                        bull[t] += 1.0
                        labels[t].append(f"regular bullish {name}")
                    elif lo[b] > lo[a] and o[b] < o[a]:
                        bull[t] += 0.5
                        labels[t].append(f"hidden bullish {name}")
                if bull[t]:
                    bull_anchor[t] = lo[b]
        k = np.searchsorted(ph, lim, side="right")
        if k >= 2:
            a, b = ph[k - 2], ph[k - 1]
            if t - b <= max_age and t - a <= lookback:
                for name, o in ov.items():
                    if np.isnan(o[a]) or np.isnan(o[b]):
                        continue
                    if hi[b] > hi[a] and o[b] < o[a]:
                        bear[t] += 1.0
                        labels[t].append(f"regular bearish {name}")
                    elif hi[b] < hi[a] and o[b] > o[a]:
                        bear[t] += 0.5
                        labels[t].append(f"hidden bearish {name}")
                if bear[t]:
                    bear_anchor[t] = hi[b]
    return pd.DataFrame({"bull": bull, "bear": bear, "labels": labels,
                         "bull_anchor": bull_anchor, "bear_anchor": bear_anchor}, index=df.index)


def prepare(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Все ряды, нужные для решения на каждом баре. Считается один раз на символ."""
    ic = cfg["indicators"]
    out = df.copy()
    out["rsi"] = ind.rsi(df.close, ic["rsi_period"])
    out["atr"] = atr(df)
    out["index"] = ind.composite_index(df, ic["weights"], ic["zscore_window"])
    dt_close = pd.Series(df.close.to_numpy(), index=pd.to_datetime(df.ts, unit="ms", utc=True))
    out["trend"] = ind.trend_4h(dt_close, ic["trend_ema_fast"], ic["trend_ema_slow"]).to_numpy()
    oscs = {"rsi": out.rsi}
    for c in ("cvd_fut", "cvd_spot", "oi"):
        if c in df and df[c].notna().any():
            oscs[c] = df[c]
    dv = divergence_table(df, oscs, ic["pivot_left"], ic["pivot_right"], ic["divergence_lookback"],
                          ic.get("divergence_max_age", ic["pivot_right"] + 2))
    lr = np.log(df.close).diff()
    vol = lr.rolling(168, min_periods=48).std()
    out["sigma24"] = vol * np.sqrt(24)                                    # суточная сигма в долях цены
    out["mom24"] = np.log(df.close / df.close.shift(24)) / out.sigma24    # падение за сутки в сигмах
    if "oi" in df:
        oi = df.oi.where(df.oi > 0)
        out["doi24"] = np.log(oi / oi.shift(24)) / out.sigma24
    return out.join(dv)


def decide_capitulation(p: pd.DataFrame, i: int, symbol: str, cfg: dict) -> Candidate | None:
    """v2: отскок после каскада ликвидаций. Лонг, когда монета за сутки упала сильнее порога в сигмах.
    Стоп широкий (k сигм), цель короткая, выход по времени. Дивергенции идут в пост как контекст."""
    cc = cfg["capitulation"]
    row = p.iloc[i]
    m, sg = row.get("mom24", np.nan), row.get("sigma24", np.nan)
    if i < cfg["indicators"]["zscore_window"] or not (np.isfinite(m) and np.isfinite(sg)) or sg <= 0:
        return None
    if m > -cc["min_drop_sigma"]:
        return None
    if cc.get("require_oi_drop") and not (row.get("doi24", 0) < 0):
        return None
    entry = float(row.close)
    u = entry * float(sg)
    stop = entry - cc["stop_sigma"] * u
    tp2 = entry + cc["target_sigma"] * u
    tp1 = entry + cc["tp1_sigma"] * u
    strength = float(np.clip(0.5 + 0.5 * (-m - cc["min_drop_sigma"]) / 2.0, 0, 1))
    feats = {"setup": "capitulation", "drop_sigma": round(float(-m), 2), "divergences": list(row.labels),
             "trend_4h": int(row.trend), "rsi": round(float(row.rsi), 2), "atr": float(row.atr),
             "funding": None if "funding" not in p else float(row.funding),
             "fear_greed": None if not np.isfinite(row.get("fear_greed", np.nan)) else int(row.fear_greed),
             "tp2_from_liq_cluster": False}
    return Candidate(symbol, int(row.ts), "long", entry, float(stop), float(stop), float(tp1), float(tp2),
                     round(strength, 3), feats)


def decide(p: pd.DataFrame, i: int, symbol: str, cfg: dict, min_strength: float | None = None,
           liq_clusters: dict | None = None) -> Candidate | None:
    if cfg.get("engine", {}).get("setup") == "capitulation":
        return decide_capitulation(p, i, symbol, cfg)
    ec = cfg["engine"]
    row = p.iloc[i]
    if i < cfg["indicators"]["zscore_window"] or not np.isfinite(row.atr) or row.atr <= 0:
        return None
    fgc = cfg["indicators"]["fear_greed"]
    fg = row.get("fear_greed", np.nan)
    side = None
    if row.bull >= ec["min_divergence_score"] and row.bull > row.bear and row["index"] >= ec["min_index"]:
        if row.trend >= 0 and not (np.isfinite(fg) and fg > fgc["long_block_above"]):
            side = "long"
    elif row.bear >= ec["min_divergence_score"] and row.bear > row.bull and row["index"] <= -ec["min_index"]:
        if row.trend <= 0 and not (np.isfinite(fg) and fg < fgc["short_block_below"]):
            side = "short"
    if side is None:
        return None

    div = row.bull if side == "long" else row.bear
    idx_dir = row["index"] if side == "long" else -row["index"]
    strength = float(np.clip(0.5 * min(div / 2.0, 1.0) + 0.5 * min(max(idx_dir, 0) / 0.5, 1.0), 0, 1))
    if strength < (min_strength if min_strength is not None else cfg["scanner"]["min_strength"]):
        return None

    entry, a = float(row.close), float(row.atr)
    anchor = float(row.bull_anchor if side == "long" else row.bear_anchor)
    sgn = 1 if side == "long" else -1
    invalidation = anchor
    stop = anchor - sgn * ec["stop_atr_buffer"] * a
    if sgn * (entry - stop) < ec["min_stop_atr"] * a:
        stop = entry - sgn * ec["min_stop_atr"] * a
        invalidation = stop + sgn * ec["stop_atr_buffer"] * a
    r = abs(entry - stop)
    tp1 = entry + sgn * ec["tp1_r"] * r
    tp2 = entry + sgn * ec["tp2_r"] * r
    snapped = None
    if liq_clusters:
        key = "short_liq_above" if side == "long" else "long_liq_below"
        for price, _usd in liq_clusters.get(key, []):
            dist_r = sgn * (price - entry) / r
            if ec["tp2_snap_min_r"] <= dist_r <= ec["tp2_snap_max_r"]:
                if snapped is None or abs(price - entry) < abs(snapped - entry):
                    snapped = float(price)
        if snapped:
            tp2 = snapped

    feats = {
        "index": round(float(row["index"]), 4),
        "divergences": list(row.labels),
        "trend_4h": int(row.trend),
        "rsi": round(float(row.rsi), 2),
        "atr": a,
        "funding": None if "funding" not in p else float(row.funding),
        "fear_greed": None if not np.isfinite(fg) else int(fg),
        "tp2_from_liq_cluster": bool(snapped),
    }
    return Candidate(symbol, int(row.ts), side, entry, float(stop), float(invalidation),
                     float(tp1), float(tp2), round(strength, 3), feats)
