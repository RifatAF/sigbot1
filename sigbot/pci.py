"""PCI v9 «прогноз ЛОНГ» на дневках BTC: перенос из Pine (индикатор PCI+SmartTrap v9), как в исследовании BTC Layers.

Нужен только старт «прогноза ЛОНГ» (forecastDir переходит в 1): модель BTC Layers добавляет +0.5 к доле на 7 дней,
если сторона > 0. Доминации USDT.D и BTC.D выключены, как в бэктесте.

Данные бесплатные и без ключей:
  * BTC дневки (OHLCV): Yahoo;
  * открытый интерес: дневные архивы Binance (data.binance.vision, metrics, с 2022 года), свежие дни из базы бота;
  * фандинг: месячные архивы Binance (fundingRate, с 2020 года), текущий месяц из базы бота или API;
  * DVOL: публичный API Deribit;
  * Coinbase и Binance спот: те же ряды, что у модели (d["cb"], d["bn"]).
Скачанное хранится в data/btc_layers/pci_raw.pkl и докачивается только новое.
"""
from __future__ import annotations

import io
import json
import os
import time
import zipfile

import numpy as np
import pandas as pd

VISION = "https://data.binance.vision/data/futures/um"
DAY = 86_400_000


def _get(url: str, tries: int = 3) -> bytes:
    from .btc_layers import _get as g
    return g(url, tries)


# ───────── загрузка
def _yahoo_ohlcv() -> pd.DataFrame:
    u = f"https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?period1=1577836800&period2={int(time.time()) + 86400}&interval=1d"
    r = json.loads(_get(u))["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    o = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close", "volume")},
                     index=pd.to_datetime(r["timestamp"], unit="s").normalize()).dropna()
    return o[~o.index.duplicated(keep="last")]


def _zip_csv(b: bytes, **kw) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        return pd.read_csv(z.open(z.namelist()[0]), **kw)


def _funding_month(ym: str) -> pd.Series:
    x = _zip_csv(_get(f"{VISION}/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-{ym}.zip"), header=None)
    if not str(x.iloc[0, 0]).isdigit():
        x = x.iloc[1:]
    return pd.Series(x.iloc[:, -1].astype(float).values, index=pd.to_datetime(x.iloc[:, 0].astype("int64"), unit="ms"))


def _oi_day(day: str) -> float | None:
    x = _zip_csv(_get(f"{VISION}/daily/metrics/BTCUSDT/BTCUSDT-metrics-{day}.zip", tries=1),
                 usecols=["create_time", "sum_open_interest_value"])
    x = x.sort_values("create_time")
    v = float(x.sum_open_interest_value.iloc[-1])
    return v if v > 0 else None


def _dvol() -> pd.Series:
    out, end = [], int(time.time() * 1000)
    start = int(pd.Timestamp("2021-03-01").timestamp() * 1000)
    while end > start:
        j = json.loads(_get("https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC"
                            f"&start_timestamp={start}&end_timestamp={end}&resolution=1D"))["result"]
        rows = j.get("data") or []
        if not rows:
            break
        out += rows
        cont = j.get("continuation")
        if not cont or cont >= end:
            break
        end = int(cont)
    s = pd.Series([x[4] for x in out], index=pd.to_datetime([x[0] for x in out], unit="ms").normalize())
    return s.groupby(level=0).last().sort_index()


def fetch(cache_dir: str, conn=None, today: pd.Timestamp | None = None) -> dict:
    """Ряды для PCI. Архивы докачиваются по одному файлу (уже скачанные не трогаются); дни, которых в архиве
    ещё нет, берутся из базы бота (open_interest, funding)."""
    today = (today or pd.Timestamp.now(tz="UTC").tz_localize(None)).normalize()
    f = os.path.join(cache_dir, "pci_raw.pkl")
    raw = pd.read_pickle(f) if os.path.exists(f) else {"oi": {}, "fr_months": {}}
    # открытый интерес: дневные архивы с 2022-01-01 до позавчера (вчерашний может ещё не выйти)
    oi = raw["oi"]
    need = [str(d.date()) for d in pd.date_range("2022-01-01", today - pd.Timedelta(days=1))
            if not (str(d.date()) in oi and (oi[str(d.date())] is not None or d < today - pd.Timedelta(days=4)))]

    def one(k):
        try:
            return k, _oi_day(k)
        except Exception:
            return k, None
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:                    # первый раз около 1800 маленьких файлов
        for k, v in ex.map(one, need):
            oi[k] = v
    # фандинг: завершённые месяцы из архива
    frm = raw["fr_months"]
    for m in pd.period_range("2020-01", (today - pd.offsets.MonthBegin(1)).to_period("M") - 1, freq="M"):
        k = str(m)
        if k not in frm:
            try:
                frm[k] = _funding_month(k)
            except Exception:
                pass
    raw["dvol"] = _dvol()
    raw["ohlcv"] = _yahoo_ohlcv()
    os.makedirs(cache_dir, exist_ok=True)
    pd.to_pickle(raw, f)
    oiu = pd.Series({pd.Timestamp(k): v for k, v in oi.items() if v is not None}, dtype=float).sort_index()
    fr = pd.concat(list(frm.values())).sort_index() if frm else pd.Series(dtype=float)
    if conn is not None:                                  # свежие дни из базы бота
        try:
            q = pd.read_sql_query("SELECT ts, oi_usd FROM open_interest WHERE symbol='BTCUSDT' AND oi_usd>0", conn)
            if len(q):
                s = pd.Series(q.oi_usd.values, index=pd.to_datetime(q.ts, unit="ms")).sort_index().resample("D").last().dropna()
                oiu = pd.concat([oiu, s[s.index > (oiu.index.max() if len(oiu) else pd.Timestamp(0))]])
            q = pd.read_sql_query("SELECT ts, rate FROM funding WHERE symbol='BTCUSDT'", conn)
            if len(q):
                s = pd.Series(q.rate.values, index=pd.to_datetime(q.ts, unit="ms")).sort_index()
                fr = pd.concat([fr, s[s.index > (fr.index.max() if len(fr) else pd.Timestamp(0))]])
        except Exception:
            pass
    return {"ohlcv": raw["ohlcv"], "oiu": oiu, "fr": fr, "dvol": raw["dvol"]}


# ───────── расчёт (логика и веса исходного индикатора, дневной режим, effLen 50)
def forecast_long_start(P: dict, cb: pd.Series, bn: pd.Series) -> pd.Series:
    """Булев ряд по дням: в этот день стартовал «прогноз ЛОНГ» PCI v9 (по закрытию дня)."""
    O = P["ohlcv"].loc["2020-06":]
    c, h, l, o, v = O.close, O.high, O.low, O.open, O.volume
    I = O.index
    norm = lambda s: s.set_axis(pd.DatetimeIndex(s.index).normalize()).groupby(level=0).last()
    fr = P["fr"].resample("D").mean().reindex(I) * 3 if len(P["fr"]) else pd.Series(np.nan, index=I)
    oiu = P["oiu"].reindex(I) if len(P["oiu"]) else pd.Series(np.nan, index=I)
    oic = oiu / c
    bn_ = norm(bn).reindex(I)
    cb_ = norm(cb).reindex(I)
    dvol = P["dvol"].reindex(I) if len(P["dvol"]) else pd.Series(np.nan, index=I)

    sma = lambda s, n: s.rolling(n).mean()
    sd = lambda s, n: s.rolling(n).std(ddof=0)

    def zs(s, n):
        z = (s - sma(s, n)) / sd(s, n)
        return z.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    rma = lambda s, n: s.ewm(alpha=1 / n, adjust=False).mean()
    ema = lambda s, n: s.ewm(span=n, adjust=False).mean()

    def rsi(s, n):
        d = s.diff()
        return 100 - 100 / (1 + rma(d.clip(lower=0), n) / rma(-d.clip(upper=0), n))
    cl = lambda x, a, b: np.clip(x, a, b)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = rma(tr, 14)
    L = 50
    btcZ = zs(100 * (c / c.shift(14) - 1), L)
    usdtZ = 0.0
    btcDomZ = 0.0
    rsiV = rsi(c, 14)
    rsiZ = zs(rsiV, 14)
    MAI = ema((btcZ - usdtZ).abs(), 3)
    manip = sma(MAI, 3) + sd(MAI, 3)
    macd = ema(c, 12) - ema(c, 26)
    macdZ = zs(macd - ema(macd, 9), L)
    volat = (sd(c, L) / sma(c, L)).fillna(0)
    volW = np.where(volat <= 0, 1.0, cl(1 / (volat * 50), 0.5, 1.5))
    trendDir = np.sign(c - ema(c, 100))
    prem = cb_ - bn_
    premZ = zs(prem, L)
    premW = cl(1 + premZ * 0.3, 0.5, 1.5)
    fundZ = zs(sma(fr, 3) * 365, L)
    dOI = oic.diff()
    oiZ = zs(dOI, L)
    liq = zs(c.diff() * v.diff(), L)
    pp = sma(premZ, 24)
    spotDom = cl((pp.abs() - 0.8) / 1.5, 0, 1)
    pDir = np.sign(c.diff())
    oDir = np.sign(dOI).fillna(0)
    oiTP = np.select([(pDir == 1) & (oDir == 1), (pDir == -1) & (oDir == -1), (pDir == 1) & (oDir == -1),
                      (pDir == -1) & (oDir == 1)], [1.25, 1.30, 0.70, 0.65], 1.0)
    oiRP = np.select([(pDir == 1) & (oDir == -1), (pDir == -1) & (oDir == -1)], [1.15, 1.25], 1.0)
    dvolZ = zs(dvol, L)
    rv = sd(np.log(c / c.shift()), 20) * np.sqrt(365) * 100
    rvZ = zs(rv, L)
    coil = -rvZ * 0.9 + oiZ * 0.7
    dow = ((I.dayofweek + 1) % 7) + 1                     # как в Pine: вс=1 ... сб=7
    dtf = (6 - dow)
    dtf = np.where(dtf < 0, dtf + 7, dtf)
    dim = I.days_in_month
    dom = I.day
    mon = I.month
    lastFri = (dow == 6) & (dom + 7 > dim)
    qFri = lastFri & np.isin(mon, [3, 6, 9, 12])
    pre = np.where(dtf <= 1, 1.0, np.where(dtf <= 2, 0.6, np.where(dtf <= 5, 0.25, 0.0))) + lastFri * 0.6 + qFri * 0.8
    post = np.isin(dow, [7, 1, 2]).astype(float)
    dec = ((mon == 12) & (dom >= 15)) * 0.25
    optRaw = pre + dec + np.where(coil > 0, np.minimum(coil, 2) * 0.25, 0) + np.where(dvolZ < -1, 0.25, 0)
    wOpt = cl(optRaw / 2, 0, 1)
    optMR = wOpt * (1 - post)
    optTB = wOpt * (0.35 + 0.45 * post)
    turb = np.where(dvolZ > 1.2, dvolZ - 1.2, 0) + np.where(rvZ > 1.2, rvZ - 1.2, 0)
    turbW = cl(turb / 3, 0, 1)
    wkd = np.isin(dow, [7, 1]).astype(float)
    mEnd = (dom >= dim - 2).astype(float)
    qEnd = (np.isin(mon, [3, 6, 9, 12]) & (dom >= dim - 2)).astype(float)
    hol = (((mon == 12) & (dom >= 20)) | ((mon == 1) & (dom <= 5))).astype(float)
    seasonW = cl(0.35 * wkd + 0.45 * mEnd + 0.55 * qEnd + 0.40 * hol, 0, 1) * 0.6
    dd = ((I - pd.Timestamp("2024-04-20")) / pd.Timedelta(days=1)).astype(int)
    cpos = np.fmod(dd, 1795)
    cBias = np.where((cpos >= 0) & (cpos < 1430), 1.0, -1.0)
    dowC = np.where(dow == 5, 0.30, np.where(dow == 2, 0.10, 0))
    dLB = np.where(dow == 2, 0.06, 0)
    dSB = np.where(dow == 5, 0.04, 0)
    mB = np.select([mon == 10, mon == 11, mon == 9, mon == 4], [0.10, 0.06, -0.10, -0.05], 0.0)
    tCal = cl(0.45 * wkd + 0.35 * dowC, 0, 1)
    conf = 0.40 + 0.35 * oiu.notna() + 0.15 * dvol.notna() + 0.10 * fr.notna()
    rng = h - l
    body = (c - o).abs()
    upW = h - np.maximum(o, c)
    loW = np.minimum(o, c) - l
    rngN = rng / atr
    bf = np.where(rng == 0, 0, body / rng)
    cp = np.where(rng == 0, 0.5, (c - l) / rng)
    prN = (h.shift() - l.shift()) / atr.shift()
    exp_ = ((rngN > 1.35) & (prN < 0.85)).astype(float)
    cs = (np.where((bf > 0.55) & (cp > 0.70), 0.55, 0) - np.where((bf > 0.55) & (cp < 0.30), 0.55, 0)
          + np.where((loW > 2 * body) & (cp > 0.60), 0.35, 0) - np.where((upW > 2 * body) & (cp < 0.40), 0.35, 0)
          + exp_ * 0.15 * np.where(c > o, 1, -1))
    cs = cl(cs, -1, 1)
    ph = (c.shift(1) > c.shift(2)) & (c.shift(1) > c.shift(3)) & (c.shift(1) > c.shift(4)) & (c.shift(1) > c)
    pl = (c.shift(1) < c.shift(2)) & (c.shift(1) < c.shift(3)) & (c.shift(1) < c.shift(4)) & (c.shift(1) < c)
    PH = c.shift(1).where(ph)
    PL = c.shift(1).where(pl)
    lastPH = PH.ffill()
    prevPH = PH.dropna().shift(1).reindex(I).ffill()
    lastPL = PL.ffill()
    prevPL = PL.dropna().shift(1).reindex(I).ffill()
    sBull = (lastPH > prevPH) & (lastPL > prevPL)
    sBear = (lastPH < prevPH) & (lastPL < prevPL)
    compT = (btcZ - usdtZ) * 0.8 + btcDomZ * 0.6 + macdZ * 0.4 + rsiZ * 0.3 + trendDir * 0.3
    compD = fundZ * (-0.5) + oiZ * 0.6 + liq * 0.7
    compR = -MAI * 0.4
    wD = 1 - 0.65 * spotDom
    wT = 1 + 0.35 * spotDom
    optDamp = 1 - optMR * (0.55 + 0.35 * 0.75)
    optBoost = 1 + optTB * 0.35
    caution = cl(0.5 * turbW + 0.3 * seasonW + 0.2 * tCal, 0, 1)
    cLB = np.where(cBias > 0, 0.18, -0.18)
    dConf = cl(0.55 + 0.45 * conf, 0.55, 1)
    oConf = cl(0.40 + 0.60 * conf, 0.40, 1)
    dDamp = 1 - 0.18 * wkd
    base = ((compT * wT * oiTP * optBoost) + (compD * wD * dDamp * dConf) + compR) * volW * premW
    ps = base * optDamp + wOpt * (((coil * 0.18) + np.where(dvolZ < -1, 0.12, 0)) * oConf) * oiRP
    zth = 1.0 * (1 + wOpt * (0.7 + 0.6 * 0.75) + 0.75 * caution)
    zth = zth * (1 + 0.35 * pre) * (1 - 0.12 * post) * (1 + (1 - conf) * 0.35) * (1 + 0.25 * wkd) * (1 - 0.08 * cs)
    thL = zth * cl(1 - cLB, 0.65, 1.35) * cl(1 - mB - dLB, 0.8, 1.2) * np.where(sBear, 1.15, 1.0)
    thS = zth * cl(1 + cLB, 0.65, 1.35) * cl(1 + mB - dSB, 0.8, 1.2) * np.where(sBull, 1.15, 1.0)
    sel = (pp.abs() > 0.3).astype(int) + (oiZ.abs() > 0.5) + (turbW < 0.6) + (conf > 0.45 + 0.25 * 0.65) + (wkd == 0)
    elig = (sel >= 4).values
    rHi = rsiV.rolling(5).max().values
    rLo = rsiV.rolling(5).min().values
    psv, thLv, thSv = np.asarray(ps, float), np.asarray(thL, float), np.asarray(thS, float)
    MAIv, mnv, cv = MAI.values, manip.values, c.values
    n = len(I)
    rawT, rawB = np.zeros(n, bool), np.zeros(n, bool)
    fd = np.zeros(n, int)
    last = None
    for i in range(n):
        g = elig[i] and (last is None or i - last > 30)
        fd[i] = (1 if psv[i] > thLv[i] else -1 if psv[i] < -thSv[i] else 0) if g else 0
        rawT[i] = g and ((psv[i] > thLv[i]) or rHi[i] > 70) and MAIv[i] > mnv[i]
        rawB[i] = g and ((psv[i] < -thSv[i]) or rLo[i] < 30) and MAIv[i] > mnv[i]
        if i >= 3 and ((rawT[i - 3] and cv[i] < cv[i - 3]) or (rawB[i - 3] and cv[i] > cv[i - 3])):
            last = i
    fd = pd.Series(fd, index=I)
    return (fd == 1) & (fd.shift() != 1)
