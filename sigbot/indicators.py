"""Детерминированные индикаторы. Никакой LLM здесь нет и быть не должно."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


# ---------- базовые ряды ----------

def zscore(s: pd.Series, window: int) -> pd.Series:
    mean = s.rolling(window, min_periods=max(10, window // 4)).mean()
    std = s.rolling(window, min_periods=max(10, window // 4)).std(ddof=0)
    return ((s - mean) / std.replace(0, np.nan)).fillna(0.0)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    out = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    out = out.where(dn > 0, np.where(up > 0, 100.0, 50.0))
    return out.astype(float)


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def cvd(volume: pd.Series, taker_buy: pd.Series) -> pd.Series:
    """CVD из свечей: покупки тейкеров минус продажи тейкеров, накопленно."""
    delta = 2 * taker_buy - volume
    return delta.cumsum()


def trend_4h(close_1h: pd.Series, fast: int = 20, slow: int = 50) -> pd.Series:
    """+1 восходящий, −1 нисходящий, 0 нет тренда. Считается по 4ч, протягивается на 1ч."""
    c4 = close_1h.resample("4h").last().dropna() if isinstance(close_1h.index, pd.DatetimeIndex) \
        else close_1h.iloc[3::4]
    f, s = ema(c4, fast), ema(c4, slow)
    t = pd.Series(np.where(f > s * 1.002, 1, np.where(f < s * 0.998, -1, 0)), index=c4.index)
    # сдвиг на один 4ч бар: используем только закрытую свечу
    t = t.shift(1).fillna(0)
    return t.reindex(close_1h.index, method="ffill").fillna(0).astype(int)


# ---------- экстремумы и дивергенции ----------

def pivots(s: pd.Series, left: int, right: int) -> tuple[np.ndarray, np.ndarray]:
    """Индексы подтверждённых локальных максимумов и минимумов (нужны right баров справа)."""
    v = s.to_numpy()
    n = len(v)
    highs, lows = [], []
    for i in range(left, n - right):
        win = v[i - left:i + right + 1]
        if v[i] == win.max() and (win == v[i]).sum() == 1:
            highs.append(i)
        if v[i] == win.min() and (win == v[i]).sum() == 1:
            lows.append(i)
    return np.array(highs, dtype=int), np.array(lows, dtype=int)


@dataclass
class Divergence:
    kind: str        # regular | hidden
    direction: str   # bullish | bearish
    against: str     # rsi | cvd_futures | cvd_spot | oi
    i1: int
    i2: int

    @property
    def score(self) -> float:
        w = 1.0 if self.kind == "regular" else 0.5
        return w if self.direction == "bullish" else -w


def divergences(price_high: pd.Series, price_low: pd.Series, osc: pd.Series, name: str,
                left: int = 3, right: int = 3, lookback: int = 60,
                max_age: int | None = None) -> list[Divergence]:
    """Классические и скрытые дивергенции по двум последним экстремумам цены.

    Регулярная бычья: цена ниже минимум, осциллятор выше минимум.
    Скрытая бычья: цена выше минимум, осциллятор ниже минимум.
    Регулярная медвежья: цена выше максимум, осциллятор ниже максимум.
    Скрытая медвежья: цена ниже максимум, осциллятор выше максимум.
    """
    n = len(osc)
    max_age = max_age if max_age is not None else right + 2
    start = max(0, n - lookback)
    o = osc.to_numpy()
    hi = price_high.to_numpy()
    lo = price_low.to_numpy()
    ph, _ = pivots(price_high.iloc[start:], left, right)
    _, pl = pivots(price_low.iloc[start:], left, right)
    ph, pl = ph + start, pl + start
    out: list[Divergence] = []

    if len(pl) >= 2 and n - 1 - pl[-1] <= max_age:
        a, b = pl[-2], pl[-1]
        if lo[b] < lo[a] and o[b] > o[a]:
            out.append(Divergence("regular", "bullish", name, a, b))
        elif lo[b] > lo[a] and o[b] < o[a]:
            out.append(Divergence("hidden", "bullish", name, a, b))
    if len(ph) >= 2 and n - 1 - ph[-1] <= max_age:
        a, b = ph[-2], ph[-1]
        if hi[b] > hi[a] and o[b] < o[a]:
            out.append(Divergence("regular", "bearish", name, a, b))
        elif hi[b] < hi[a] and o[b] > o[a]:
            out.append(Divergence("hidden", "bearish", name, a, b))
    return out


# ---------- карта ликвидаций ----------

def liquidation_map(close: pd.Series, oi: pd.Series, leverages=(10, 25, 50, 100),
                    bin_pct: float = 0.25, mm_pct: float = 0.5) -> pd.DataFrame:
    """Оценка уровней ликвидаций по приросту открытого интереса.

    Прирост OI на баре считается новыми позициями по цене закрытия, поровну лонги и шорты
    и поровну по плечам. Уровень снимается, когда цена его прошла (позиции ликвидированы).
    Возвращает бины цены: long_usd (ниже цены), short_usd (выше цены).
    """
    levels: list[list[float]] = []  # [price, usd, side(+1 long/-1 short)]
    c = close.to_numpy()
    d_oi = oi.diff().fillna(0).to_numpy()
    lo_seen, hi_seen = c.copy(), c.copy()
    for i in range(len(c)):
        p = c[i]
        if levels:
            arr = np.array(levels)
            alive = ~(((arr[:, 2] > 0) & (p <= arr[:, 0])) | ((arr[:, 2] < 0) & (p >= arr[:, 0])))
            levels = arr[alive].tolist()
        add = d_oi[i] * p
        if add <= 0:
            continue
        per = add / (2 * len(leverages))
        for lev in leverages:
            mv = 1 / lev - mm_pct / 100
            levels.append([p * (1 - mv), per, 1])
            levels.append([p * (1 + mv), per, -1])
    if not levels:
        return pd.DataFrame(columns=["price", "long_usd", "short_usd"])
    arr = np.array(levels)
    ref = c[-1]
    step = ref * bin_pct / 100
    bins = np.round(arr[:, 0] / step) * step
    df = pd.DataFrame({"price": bins, "usd": arr[:, 1], "side": arr[:, 2]})
    g = df.pivot_table(index="price", columns="side", values="usd", aggfunc="sum", fill_value=0)
    return pd.DataFrame({
        "price": g.index.to_numpy(),
        "long_usd": g.get(1.0, pd.Series(0, index=g.index)).to_numpy(),
        "short_usd": g.get(-1.0, pd.Series(0, index=g.index)).to_numpy(),
    }).reset_index(drop=True)


def nearest_clusters(lmap: pd.DataFrame, price: float, top: int = 3) -> dict:
    """Самые плотные кластеры ниже (лонги) и выше (шорты) текущей цены."""
    below = lmap[lmap.price < price].nlargest(top, "long_usd")
    above = lmap[lmap.price > price].nlargest(top, "short_usd")
    return {
        "long_liq_below": below[["price", "long_usd"]].values.tolist(),
        "short_liq_above": above[["price", "short_usd"]].values.tolist(),
    }


# ---------- композитный индекс ----------

def composite_index(df: pd.DataFrame, weights: dict, window: int) -> pd.Series:
    """Индекс от −1 (медвежий) до +1 (бычий). Все компоненты направлены как «+ = за лонг».

    Ожидаемые колонки df (1ч): close, oi, funding, cvd_fut, cvd_spot, premium,
    liq_long_usd, liq_short_usd. Отсутствующие колонки пропускаются, вес перераспределяется.
    """
    comps: dict[str, pd.Series] = {}
    ret = df["close"].pct_change().fillna(0)
    if "oi" in df:
        # OI растёт против движения цены: набирают позиции против тренда, топливо для сквиза
        comps["oi"] = zscore(df["oi"].pct_change().fillna(0), window) * -np.sign(ret)
    if "funding" in df:
        comps["funding"] = -zscore(df["funding"], window)  # отрицательный фандинг за лонг
    if "cvd_spot" in df:
        comps["cvd_spot"] = zscore(df["cvd_spot"].diff().fillna(0), window)
    if "cvd_fut" in df:
        # агрессивные фьючерсные покупки без роста цены: поглощение, контрсигнал
        flat = 1 - zscore(ret, window).abs().clip(0, 1)
        comps["cvd_futures"] = -zscore(df["cvd_fut"].diff().fillna(0), window) * flat
    if "premium" in df:
        comps["premium"] = zscore(df["premium"], window)
    if "liq_long_usd" in df and "liq_short_usd" in df:
        # вынос лонгов (капитуляция) за лонг, вынос шортов против
        comps["liquidations"] = zscore(df["liq_long_usd"], window) - zscore(df["liq_short_usd"], window)

    num = pd.Series(0.0, index=df.index)
    den = 0.0
    for k, s in comps.items():
        w = float(weights.get(k, 0))
        if w:
            num += w * s.clip(-3, 3).astype(float)
            den += w
    return (num / den / 3).fillna(0) if den else num
