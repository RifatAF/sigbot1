"""Сборка часового набора признаков по символу из базы."""
from __future__ import annotations

import pandas as pd

from . import indicators as ind

HOUR = 3_600_000


def _candles(conn, symbol, market, start, end) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT ts, open, high, low, close, volume, taker_buy FROM candles "
        "WHERE symbol=? AND market=? AND tf='1h' AND ts>=? AND ts<? ORDER BY ts",
        conn, params=(symbol, market, start, end))


def _asof(base: pd.DataFrame, other: pd.DataFrame, col: str, out: str) -> pd.DataFrame:
    """Значение `col` на момент закрытия бара (ts + 1ч − 1мс), без заглядывания вперёд."""
    if other.empty:
        return base
    b = base.assign(_close_ts=base.ts + HOUR - 1).sort_values("_close_ts")
    o = other[["ts", col]].rename(columns={"ts": "_close_ts", col: out}).sort_values("_close_ts")
    m = pd.merge_asof(b, o, on="_close_ts", direction="backward")
    return m.drop(columns="_close_ts")


def build(conn, symbol: str, start: int, end: int) -> pd.DataFrame:
    fut = _candles(conn, symbol, "futures", start, end)
    if fut.empty:
        return fut
    df = fut.copy()
    df["cvd_fut"] = ind.cvd(df.volume, df.taker_buy)

    spot = _candles(conn, symbol, "spot", start, end)
    if not spot.empty:
        s = spot[["ts", "volume", "taker_buy"]].rename(columns={"volume": "sv", "taker_buy": "stb"})
        df = df.merge(s, on="ts", how="left")
        df["cvd_spot"] = ind.cvd(df.sv.fillna(0), df.stb.fillna(0))
        df = df.drop(columns=["sv", "stb"])

    oi = pd.read_sql_query("SELECT ts, oi FROM open_interest WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
                           conn, params=(symbol, start - 6 * HOUR, end + HOUR))
    df = _asof(df, oi, "oi", "oi")

    fr = pd.read_sql_query("SELECT ts, rate FROM funding WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
                           conn, params=(symbol, start - 9 * HOUR, end + HOUR))
    df = _asof(df, fr, "rate", "funding")

    pr = pd.read_sql_query("SELECT ts, premium_pct FROM premium WHERE ts>=? AND ts<? ORDER BY ts",
                           conn, params=(start - HOUR, end + HOUR))
    if len(pr) > 0:
        df = _asof(df, pr, "premium_pct", "premium")

    fg = pd.read_sql_query("SELECT ts, value FROM fear_greed WHERE ts>=? AND ts<? ORDER BY ts",
                           conn, params=(start - 3 * 86_400_000, end + HOUR))
    df = _asof(df, fg, "value", "fear_greed")

    # ликвидации включаются, только если собственная запись покрывает окно
    first_liq = conn.execute("SELECT MIN(ts) FROM liquidations").fetchone()[0]
    if first_liq is not None and first_liq <= start:
        lq = pd.read_sql_query(
            "SELECT ts / 3600000 * 3600000 AS ts, side, SUM(usd) AS usd FROM liquidations "
            "WHERE symbol=? AND ts>=? AND ts<? GROUP BY 1, 2", conn, params=(symbol, start, end + HOUR))
        piv = lq.pivot_table(index="ts", columns="side", values="usd", fill_value=0)
        df["liq_long_usd"] = df.ts.map(piv.get("long", pd.Series(dtype=float))).fillna(0)
        df["liq_short_usd"] = df.ts.map(piv.get("short", pd.Series(dtype=float))).fillna(0)

    for c in ("oi", "funding", "premium", "fear_greed"):
        if c in df:
            df[c] = df[c].ffill()
            if df[c].isna().all():
                df = df.drop(columns=c)
    df.index = pd.to_datetime(df.ts, unit="ms", utc=True)
    return df
