"""BTC Layers: дневная модель позиции по BTC (макро-режим + тренд + PCI + размер под волатильность).

Один файл, зависимости: numpy, pandas. Сеть: только публичные источники без ключей.
Решение принимается раз в сутки по закрытию дня UTC и действует до следующего закрытия.

    python btc_layers_engine.py            # позиция на сегодня + разбор слоёв
    python btc_layers_engine.py --bt       # плюс бэктест $100 с 21.05.2018

Как использовать из бота:
    from btc_layers_engine import fetch_all, compute, backtest
    data = fetch_all(cache_dir="data/btc_layers")      # раз в сутки после 00:05 UTC
    df = compute(data, pci_long_start=None)             # DataFrame по дням
    row = df.iloc[-1]                                   # row.pos: −1.5..2.25 (доля депозита, знак = сторона)

Позиция в df на дату D = позиция, которую держать в течение дня D (решение на закрытии D−1).
"""
from __future__ import annotations

import io
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# ───────── Параметры (зафиксированы исследованием, не подгонять)
P = dict(
    chg=30,            # окно изменения, дней
    zwin=365,          # окно нормы z-score
    zmin=180,          # минимум наблюдений для z
    fed_start="2016-06-01",
    band=0.10,         # гистерезис режима
    vol_target=0.40,   # целевая волатильность (0.30 осторожно, 0.50 агрессивно)
    vol_cap=1.5,       # максимум множителя волатильности
    pci_boost=0.5, pci_days=7,
    cost=0.001, lev_rate=0.10, short_rate=0.05,   # для бэктеста
)
UA = {"User-Agent": "Mozilla/5.0"}


# ───────── Загрузка
def _get(url: str, tries: int = 3) -> bytes:
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40).read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def _yahoo(sym: str) -> pd.Series:
    u = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?period1=1451606400&period2={int(time.time()) + 86400}&interval=1d"
    j = json.loads(_get(u))["chart"]["result"][0]
    return _yahoo_parse(j)


def _yahoo_parse(j: dict) -> pd.Series:
    off = j["meta"].get("gmtoffset", 0)
    c = j["indicators"].get("adjclose", [{}])[0].get("adjclose") or j["indicators"]["quote"][0]["close"]
    s = pd.Series(c, index=pd.to_datetime(np.array(j["timestamp"]) + off, unit="s").normalize(), dtype=float).dropna()
    return s[~s.index.duplicated(keep="last")]


def _treasury() -> pd.DataFrame:
    out = []
    for y in range(2016, datetime.now(timezone.utc).year + 1):
        u = f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv"
        out.append(pd.read_csv(io.BytesIO(_get(u))))
    return _treasury_parse(out)


def _treasury_parse(frames) -> pd.DataFrame:
    U = pd.concat(frames)
    U.index = pd.to_datetime(U.Date)
    U = U.sort_index()
    return U[~U.index.duplicated()][["3 Mo", "2 Yr", "10 Yr"]].astype(float)


def _nyfed():
    effr = json.loads(_get("https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json?startDate=2016-01-01"))
    rrp = json.loads(_get("https://markets.newyorkfed.org/api/rp/reverserepo/propositions/search.json?startDate=2016-01-01"))
    soma = json.loads(_get("https://markets.newyorkfed.org/api/soma/summary.json"))
    return effr, rrp, soma


def _tga():
    rows, page = [], 1
    while True:
        u = ("https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance"
             f"?filter=record_date:gte:2016-01-01&page[size]=10000&page[number]={page}")
        j = json.loads(_get(u))
        rows += j["data"]
        if page >= j["meta"]["total-pages"]:
            return rows
        page += 1


def _binance_daily(sym="BTCUSDT") -> pd.Series:
    for host in ("https://api.binance.com", "https://data-api.binance.vision"):
        try:
            return _binance_from(host, sym)
        except Exception:
            if host.endswith("vision"):
                raise


def _binance_from(host: str, sym: str) -> pd.Series:
    out, start = [], 1502928000000
    while True:
        j = json.loads(_get(f"{host}/api/v3/klines?symbol={sym}&interval=1d&startTime={start}&limit=1000"))
        out += j
        if len(j) < 1000:
            break
        start = j[-1][0] + 86400000
    return pd.Series([float(x[4]) for x in out], index=pd.to_datetime([x[0] for x in out], unit="ms"))


def _coinbase_daily() -> pd.Series:
    out, end = [], int(time.time())
    while end > 1514764800:
        st = end - 300 * 86400
        u = f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400&start={datetime.fromtimestamp(st, timezone.utc).isoformat()}&end={datetime.fromtimestamp(end, timezone.utc).isoformat()}"
        j = json.loads(_get(u))
        if not j:
            break
        out += j
        end = st
    df = pd.DataFrame(out).drop_duplicates(0)
    return pd.Series(df[4].values, index=pd.to_datetime(df[0], unit="s")).sort_index()


def fetch_all(cache_dir: str | None = None) -> dict:
    """Скачивает всё нужное. Возвращает dict сырых рядов. С cache_dir кладёт копию в pickle."""
    effr, rrp, soma = _nyfed()
    d = dict(
        btc=_yahoo("BTC-USD"), ung=_yahoo("UNG"), ng=_yahoo("NG=F"), ttf=_yahoo("TTF=F"),
        ust=_treasury(), effr=effr, rrp=rrp, soma=soma, tga=_tga(),
        cb=_coinbase_daily(), bn=_binance_daily(),
    )
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        pd.to_pickle(d, os.path.join(cache_dir, "raw.pkl"))
    return d


# ───────── Расчёт
def _daily(s: pd.Series) -> pd.Series:
    return s.asfreq("D").ffill()


def _z(x: pd.Series, expanding: bool = False) -> pd.Series:
    if expanding:
        m, s = x.expanding(P["zmin"]).mean(), x.expanding(P["zmin"]).std()
    else:
        m, s = x.rolling(P["zwin"], min_periods=P["zmin"]).mean(), x.rolling(P["zwin"], min_periods=P["zmin"]).std()
    return ((x - m) / s).clip(-3, 3)


def _liquidity(d: dict) -> pd.Series:
    r = [x for x in d["rrp"]["repo"]["operations"] if x["operationType"] == "Reverse Repo"]
    rrp = pd.Series([float(x["totalAmtAccepted"]) for x in r], index=pd.to_datetime([x["operationDate"] for x in r]))
    rrp = rrp.groupby(level=0).sum().sort_index().asfreq("D").ffill().fillna(0) / 1e9
    t = pd.DataFrame(d["tga"])
    t = t[t.account_type.str.contains("Closing Balance|Federal Reserve Account", regex=True) & ~t.account_type.str.contains("Opening")]
    t["v"] = pd.to_numeric(t.close_today_bal, errors="coerce")
    tga = t.groupby("record_date").v.max()
    tga.index = pd.to_datetime(tga.index)
    tga = tga.sort_index().asfreq("D").ffill() / 1e3
    so = d["soma"]["soma"]["summary"]
    soma = pd.Series([float(x["total"]) for x in so], index=pd.to_datetime([x["asOfDate"] for x in so])).sort_index()
    soma = soma.set_axis(soma.index + pd.Timedelta(days=1)).asfreq("D").ffill() / 1e9
    return (soma - tga - rrp).dropna()


def _regime(m: pd.Series) -> np.ndarray:
    out, st = np.zeros(len(m)), 0
    for i, v in enumerate(m.values):
        if not np.isnan(v):
            if st >= 0 and v < -P["band"]:
                st = -1
            elif st <= 0 and v > P["band"]:
                st = 1
            elif st == 0:
                st = 1 if v > 0 else -1
        out[i] = st
    return out


def compute(d: dict, pci_long_start: pd.Series | None = None, start: str = "2018-01-01",
            vol_target: float | None = None) -> pd.DataFrame:
    """pci_long_start: bool по дням UTC, True в день старта «прогноз ЛОНГ» PCI (forecastDir 0/−1 → 1).
    Если None, слой PCI выключен (это шаг 5 исследования, чуть слабее)."""
    U = d["ust"].asfreq("D").ffill()
    e = d["effr"]["refRates"]
    effr = pd.Series({pd.Timestamp(x["effectiveDate"]): x["percentRate"] for x in e}).sort_index().asfreq("D").ffill()
    ung, ttf, ng = (np.log(_daily(d[k])) for k in ("ung", "ttf", "ng"))
    liq = _liquidity(d)
    n = P["chg"]
    raw = pd.DataFrame({
        "gas": -(ung.diff(n).shift(1)),                     # газ США дорожает → BTC хуже
        "fed": -((U["2 Yr"] - effr).shift(2)),              # рынок ждёт повышений → хуже
        "liq": np.log(liq).diff(n).shift(2),                # ликвидность растёт → лучше
        "curve": (U["10 Yr"] - U["2 Yr"]).diff(n).shift(2), # кривая круче → лучше
        "ttf": (ttf - ng).diff(n).shift(2),                 # Европа дорожает относительно США → лучше
    }).loc[P["fed_start"]:].ffill(limit=5)
    Z = pd.DataFrame({c: _z(raw[c], expanding=(c == "fed")) for c in raw})
    Z = Z.dropna()   # как в исследовании: дни, где есть все 5
    prem = (np.log(d["cb"] / d["bn"].reindex(d["cb"].index)) * 100).dropna()
    Z["prem"] = _z(prem.rolling(7).mean()).shift(1).reindex(Z.index)

    btc = _daily(d["btc"])
    idx = Z.index[Z.index >= start]
    idx = idx[idx <= btc.index[-1]]
    C = btc.reindex(pd.date_range("2016-01-01", idx[-1])).ffill()
    votes = [np.where(C >= (C.rolling(lb).max() + C.rolling(lb).min()) / 2, 1.0, -1.0) for lb in (10, 20, 40, 80, 160)]
    ma = np.where(C.rolling(20).mean() > C.rolling(50).mean(), 1.0, -1.0)
    trend = pd.Series((sum(votes) / 5 + ma) / 2, index=C.index).reindex(idx)
    vol = (np.log(C).diff().rolling(30, min_periods=20).std() * np.sqrt(365)).reindex(idx)

    out = Z.reindex(idx).copy()
    out["macro"] = out[["gas", "fed", "liq", "curve", "ttf", "prem"]].mean(axis=1).clip(-1, 1)
    out["regime"] = _regime(out["macro"])
    out["trend"] = trend
    out["side"] = np.where(out.regime > 0, np.where(trend > 0, 1.0, 0.5), np.where(out.regime < 0, np.where(trend < 0, -1.0, 0.0), 0.0))
    if pci_long_start is not None:
        s = pci_long_start.reindex(idx).fillna(False).astype(float)
        on = sum(s.shift(k).fillna(0) for k in range(P["pci_days"])) > 0
    else:
        on = pd.Series(False, index=idx)
    out["pci_on"] = on
    out["boost"] = np.where(on & (out.side > 0), P["pci_boost"], 0.0)
    out["side_x"] = (out.side + out.boost).clip(-1.5, 1.5)
    out["vol"] = vol
    out["vt"] = ((vol_target or P["vol_target"]) / vol).clip(upper=P["vol_cap"])
    out["signal"] = out.side_x * out.vt       # решение на закрытии дня D
    out["pos"] = out.signal.shift(1)          # позиция, которую держим в день D
    out["btc"] = C.reindex(idx)
    out["rf"] = (U["3 Mo"].reindex(idx).ffill() / 100 / 365).fillna(0)
    return out


def backtest(df: pd.DataFrame, start: str = "2018-05-21", deposit: float = 100.0) -> dict:
    """Дневной бэктест: доход = позиция × доходность BTC − комиссия − плата за плечо/шорт + доход на свободный кэш."""
    ret = df.btc.pct_change().shift(-1)        # доходность дня D+1 для сигнала на закрытии D
    p = df.signal.fillna(0)
    r = p * ret - P["cost"] * p.diff().abs().fillna(0) - np.clip(p - 1, 0, None) * P["lev_rate"] / 365 \
        - np.clip(-p, 0, None) * P["short_rate"] / 365 + (1 - p.abs()).clip(lower=0) * df.rf.shift(-1).fillna(0)
    r = r.loc[start:].dropna()
    eq = deposit * (1 + r).cumprod()
    yrs = len(r) / 365
    return dict(final=eq.iloc[-1], cagr=(eq.iloc[-1] / deposit) ** (1 / yrs) - 1,
                max_dd=(eq / eq.cummax() - 1).min(), sharpe=r.mean() / r.std() * np.sqrt(365), equity=eq)


def describe(row: pd.Series) -> str:
    s = row.signal
    side = "ЛОНГ" if s > 0 else "ШОРТ" if s < 0 else "КЭШ"
    return (f"{row.name.date()} закрытие → {side} {abs(s):.2f}x | макро {row.macro:+.2f} (режим {'бычий' if row.regime > 0 else 'медвежий'}), "
            f"тренд {row.trend:+.2f}, PCI {'+0.5' if row.boost else 'нет'}, вол. {row.vol:.0%} | "
            + " ".join(f"{k} {row[k]:+.2f}" for k in ("gas", "fed", "liq", "curve", "ttf", "prem")))


# ───────── Встраивание в бота: кэш данных, расчёт, сигнал, бумажный счёт
CACHE = "data/btc_layers"
REPORT = "reports/btc_layers.json"
LIMIT = 2.25                                   # |позиция| не больше 1.5 доли × 1.5 размера


def lc(cfg, conn=None) -> dict:
    if conn is None:
        return dict(cfg.get("btc_layers", {}))
    from . import settings
    return dict(settings.effective(cfg, conn).get("btc_layers", {}))


def load(refresh: bool = False, cache_dir: str = CACHE, max_age_h: float = 20) -> dict:
    """Сырые ряды из кэша на диске; старше max_age_h часов или refresh: скачать заново. Сеть упала: старый кэш."""
    f = os.path.join(cache_dir, "raw.pkl")
    fresh = os.path.exists(f) and time.time() - os.path.getmtime(f) < max_age_h * 3600
    if os.path.exists(f) and fresh and not refresh:
        return pd.read_pickle(f)
    try:
        return fetch_all(cache_dir)
    except Exception:
        if os.path.exists(f):
            return pd.read_pickle(f)
        raise


def closed_only(d: dict, today: pd.Timestamp | None = None) -> dict:
    """Убрать незакрытую свечу текущего дня UTC: решение только по закрытым дням."""
    today = (today or pd.Timestamp.now(tz="UTC").tz_localize(None)).normalize()
    out = dict(d)
    for k in ("btc", "cb", "bn"):
        out[k] = d[k][d[k].index < today]
    return out


def model(cfg, conn=None, refresh: bool = False, d: dict | None = None) -> pd.DataFrame:
    c = lc(cfg, conn)
    d = closed_only(d if d is not None else load(refresh))
    return compute(d, pci_long_start=None, vol_target=float(c.get("vol_target", P["vol_target"])))


def pos_text(x: float) -> str:
    return "КЭШ (вне рынка)" if abs(x) < 1e-9 else f"{'ЛОНГ' if x > 0 else 'ШОРТ'} {abs(x) * 100:.0f}% депозита"


def side_of(x: float) -> int:
    return 0 if abs(x) < 1e-9 else (1 if x > 0 else -1)


def changed(prev: float | None, new: float, step: float = 0.25) -> bool:
    """Пост только при смене направления (лонг, шорт, кэш) или изменении размера больше чем на step."""
    return prev is None or side_of(prev) != side_of(new) or abs(new - prev) > step


def state_of(row: pd.Series) -> dict:
    return {"date": str(row.name.date()), "signal": round(float(np.clip(row.signal, -LIMIT, LIMIT)), 4),
            "macro": float(row.macro), "regime": int(row.regime), "trend": float(row.trend), "vol": float(row.vol),
            "pci": bool(row.boost), "btc": float(row.btc), "side": float(row.side_x), "vt": float(row.vt),
            "parts": {k: (None if pd.isna(row[k]) else float(row[k])) for k in ("gas", "fed", "liq", "curve", "ttf", "prem")}}


def report(df: pd.DataFrame, path: str = REPORT) -> dict:
    """Бэктест и текущее состояние для панели: капитал $100 с 21.05.2018 по дням, позиция, купить и держать."""
    b = backtest(df)
    eq = b["equity"]
    bh = 100 * df.btc.loc[eq.index] / df.btc.loc[eq.index[0]]
    out = {"final": round(float(b["final"]), 2), "cagr": float(b["cagr"]), "max_dd": float(b["max_dd"]),
           "sharpe": float(b["sharpe"]), "start": str(eq.index[0].date()), "now": state_of(df.iloc[-1]),
           "days": [{"t": int(t.timestamp()), "eq": round(float(e), 2), "bh": round(float(h), 2),
                     "pos": round(float(df.signal.get(t, 0) or 0), 3), "px": round(float(df.btc.get(t)), 2)}
                    for t, e, h in zip(eq.index, eq.values, bh.values)]}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    os.replace(tmp, path)
    return out


def posted_points(days: list[dict]) -> list[dict]:
    """Из ежедневных решений бэктеста: только дни, когда модель выпустила бы пост (то же правило, что вживую)."""
    out, last = [], None
    for d in days:
        if d.get("px") is None:
            continue
        if last is None or changed(last, d["pos"]):
            out.append({"t": d["t"] + 86400, "signal": d["pos"], "px": d["px"]})
            last = d["pos"]
    return out


def segments(points: list[dict], mark: float | None = None, mark_t: int | None = None, fee_pct: float = 0.05) -> list[dict]:
    """Позиции модели в виде сделок для ленты: сделка от входа в сторону до разворота или кэша.
    Изменение размера внутри сделки: событие «размер». Итог в % депозита с комиссией тейкера."""
    out, cur = [], None
    for i, p in enumerate(points):
        s = side_of(p["signal"])
        if cur and s == side_of(cur["_sig"]):                       # та же сторона: новый размер
            cur["_pnl"] += cur["_sig"] * (p["px"] / cur["_px"] - 1) * 100 - abs(p["signal"] - cur["_sig"]) * fee_pct
            cur["exits"].append({"what": "resize", "t": p["t"], "price": p["px"], "part": round(abs(p["signal"]), 3)})
            cur["_sig"], cur["_px"] = p["signal"], p["px"]
            continue
        if cur:                                                     # разворот или кэш: сделка закрыта
            cur["_pnl"] += cur["_sig"] * (p["px"] / cur["_px"] - 1) * 100 - abs(cur["_sig"]) * fee_pct
            cur["exits"].append({"what": "flip" if s else "cash", "t": p["t"], "price": p["px"], "part": 1.0})
            out.append(_close_seg(cur, p["px"], p["t"]))
            cur = None
        if s:
            cur = {"kind": "layers", "ref": str(p["t"]), "symbol": "BTCUSDT", "side": "long" if s > 0 else "short",
                   "entry_time": p["t"], "entry": p["px"], "size_pct": round(abs(p["signal"]) * 100, 1), "exits": [],
                   "_sig": p["signal"], "_px": p["px"], "_pnl": -abs(p["signal"]) * fee_pct}
    if cur:
        m = mark or cur["_px"]
        cur["_pnl"] += cur["_sig"] * (m / cur["_px"] - 1) * 100
        it = _close_seg(cur, m, None)
        it["open"], it["exit"], it["exit_time"], it["size_now"] = True, None, None, round(abs(cur["_sig"]) * 100, 1)
        out.append(it)
    return out


def _close_seg(c: dict, px: float, t: int | None) -> dict:
    sd = 1 if c["side"] == "long" else -1
    it = {k: v for k, v in c.items() if not k.startswith("_")}
    it.update(open=False, exit=px, exit_time=t, result_pct=round(c["_pnl"], 2),
              move_pct=round(sd * (px / c["entry"] - 1) * 100, 2))
    return it


def live_items(conn) -> list[dict]:
    """Живые позиции модели: опубликованные посты из журнала, итог открытой по последней цене BTC."""
    from . import settings
    log_ = settings.get(conn, "btc_layers_log") or []
    pts = [{"t": int(pd.Timestamp(x["date"]).timestamp()) + 86400, "signal": x["signal"], "px": x["btc"]} for x in log_]
    c = conn.execute("SELECT close FROM candles WHERE symbol='BTCUSDT' AND market='futures' ORDER BY ts DESC LIMIT 1").fetchone()
    st = settings.get(conn, "btc_layers_state") or {}
    return segments(pts, mark=c[0] if c else st.get("btc"))


class LayersService:
    """Раз в сутки после закрытия дня UTC: расчёт, пост при смене позиции, ребаланс бумажного счёта «BTC Layers»."""

    def __init__(self, cfg, conn, pub=None, demo=None):
        self.cfg, self.conn, self.pub, self.demo = cfg, conn, pub, demo
        self.report_path = REPORT

    def on(self) -> bool:
        from . import settings
        e = settings.effective(self.cfg, self.conn)
        return bool(e.get("btc_layers", {}).get("enabled")) and not e["publishing"].get("kill_switch")

    async def loop(self, say=print, every_s: int = 600):
        """Отдельная работа модели, параллельно с остальными сигналами: расчёт сразу при запуске бота,
        дальше раз в сутки после закрытия дня UTC (проверка каждые 10 минут)."""
        import asyncio
        import logging
        from . import settings
        first = True
        while True:
            now = int(time.time() * 1000)
            try:
                st = await self.run_daily(now)
                if first:
                    st = st or settings.get(self.conn, "btc_layers_state")
                    if not self.on():
                        say("⏸ BTC Layers выключен: /menu → Стратегии → 🧭 BTC Layers")
                    elif st:
                        say(f"✅ BTC Layers: {pos_text(st['signal'])} (решение по закрытию {st['date']}), "
                            f"панель: http://127.0.0.1:{self.cfg['dashboard']['port']}/layers")
            except Exception as e:
                logging.getLogger(__name__).exception("btc layers: %s", e)
                if first:
                    say(f"⚠️ BTC Layers: расчёт не удался ({e}), повтор через 10 минут")
                await asyncio.sleep(every_s)
                continue
            first = False
            await asyncio.sleep(every_s)

    async def run_daily(self, now_ms: int, force: bool = False, d: dict | None = None) -> dict | None:
        import asyncio
        from . import settings, templates
        day = now_ms // 86_400_000
        if not self.on() or (not force and settings.get(self.conn, "btc_layers_day") == day):
            return None
        df = await asyncio.to_thread(model, self.cfg, self.conn, True, d)
        st = state_of(df.iloc[-1])
        rep = await asyncio.to_thread(report, df, self.report_path)
        st["bt"] = {k: rep[k] for k in ("final", "cagr", "max_dd", "start")}
        prev = settings.get(self.conn, "btc_layers_posted")          # с чем сравнивать: последний опубликованный пост
        if self.pub and (prev is None or changed(prev["signal"], st["signal"])):
            eff = settings.effective(self.cfg, self.conn)["publishing"]
            lev = int(settings.effective(self.cfg, self.conn).get("paper", {}).get("leverage", 5))
            text, nums = templates.btc_layers(st, prev, eff.get("mode", "paper"), eff.get("lang", "ru"), lev)
            if await self.pub.publish("btc_layers", text, nums) in ("sent", "pending", "no_telegram"):
                settings.put(self.conn, "btc_layers_posted", st)
                log_ = (settings.get(self.conn, "btc_layers_log") or [])[-19:]
                settings.put(self.conn, "btc_layers_log", log_ + [{"date": st["date"], "signal": st["signal"], "btc": st["btc"]}])
        settings.put(self.conn, "btc_layers_state", st)
        settings.put(self.conn, "btc_layers_day", day)
        if self.demo and lc(self.cfg, self.conn).get("paper", True):
            await self.demo.layers_target(st["signal"], st["date"], px_hint=st["btc"])
        return st


if __name__ == "__main__":
    import sys
    df = compute(fetch_all())
    print(describe(df.iloc[-1]))
    if "--bt" in sys.argv:
        b = backtest(df)
        print(f"$100 с 21.05.2018 → ${b['final']:,.0f}, {b['cagr']:+.0%} годовых, просадка {b['max_dd']:.0%}, Шарп {b['sharpe']:.2f}")
