"""Трендовый портфель: ансамбль каналов Дончиана и MA кросса по дневкам, лонг и шорт,
размер позиций по волатильности (риск паритет), весь портфель под целевую годовую волатильность.

Идея из литературы по time series momentum (Moskowitz, Ooi, Pedersen; для крипты Zarattini, Pagani, Barbon).
Параметры не подбирались под историю: окна стандартные, проверка на 2020..2026 с умершими монетами.
Веса считаются на закрытии суток и держатся следующие сутки. Бот ордеров не ставит: это бумажный портфель
и посты в канал."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
DAY = 86_400_000


def daily_frames(conn, symbols: list[str] | None, start: int, end: int):
    """Дневные закрытия, оборот в $ и сумма фандинга за сутки из часовых свечей."""
    q = "SELECT symbol, ts, close, volume FROM candles WHERE market='futures' AND tf='1h' AND ts>=? AND ts<?"
    args: list = [start, end]
    if symbols:
        q += f" AND symbol IN ({','.join('?' * len(symbols))})"
        args += symbols
    h = pd.read_sql_query(q, conn, params=args)
    if h.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    h["day"] = h.ts // DAY * DAY
    g = h.groupby(["symbol", "day"])
    full = g.ts.count() >= 24                      # только полные сутки
    d = pd.DataFrame({"close": g.close.last(), "qv": (h.volume * h.close).groupby([h.symbol, h.day]).sum()})[full]
    C = d.close.unstack(0).sort_index()
    V = d.qv.unstack(0).reindex(C.index)
    fr = pd.read_sql_query("SELECT symbol, ts, rate FROM funding WHERE ts>=? AND ts<?", conn, params=(start, end))
    fr["day"] = fr.ts // DAY * DAY
    FR = fr.groupby(["day", "symbol"]).rate.sum().unstack().reindex(index=C.index, columns=C.columns).fillna(0)
    return C, V, FR


def signal(C: pd.DataFrame, pc: dict) -> pd.DataFrame:
    """Среднее голосов: каждый канал Дончиана и MA кросс дают +1 (тренд вверх) или −1 (вниз, 0 без шортов)."""
    neg = -1.0 if pc.get("allow_short", True) else 0.0
    votes = []
    for lb in pc["donchian"]:
        mid = (C.rolling(lb).max() + C.rolling(lb).min()) / 2
        votes.append(np.where(C >= mid, 1.0, neg))
    don = sum(votes) / len(votes)
    f, s = C.rolling(pc["ma_fast"]).mean(), C.rolling(pc["ma_slow"]).mean()
    ma = np.where(f > s, 1.0, neg)
    sig = pd.DataFrame((don + ma) / 2, index=C.index, columns=C.columns)
    return sig.where(C.rolling(pc["donchian"][-1]).count() >= pc["donchian"][-1])   # нужна история самого длинного окна


def weights(C: pd.DataFrame, V: pd.DataFrame, pc: dict, universe: list[str] | None = None) -> pd.DataFrame:
    """Целевые веса на закрытии каждого дня (доля депозита, знак = сторона)."""
    R = np.log(C).diff()
    if universe:
        uni = pd.DataFrame(False, index=C.index, columns=C.columns)
        uni[[c for c in universe if c in C.columns]] = True
    else:   # точечный во времени: топ N по среднему обороту за 30 дней
        liq = V.rolling(30, min_periods=20).mean()
        uni = liq.rank(axis=1, ascending=False) <= pc["top_n"]
    uni &= C.notna()
    vol = R.rolling(pc["vol_window"], min_periods=20).std()
    w = (signal(C, pc).where(uni) / vol).replace([np.inf, -np.inf], np.nan).fillna(0)
    w = w.div(w.abs().sum(1).replace(0, np.nan), axis=0).fillna(0)
    pv = (w.shift(1) * R.fillna(0)).sum(1).rolling(60, min_periods=20).std() * np.sqrt(365)
    k = (pc["target_vol"] / pv).clip(upper=pc["max_gross"]).fillna(1.0)
    return w.mul(k, axis=0)


def simulate(C: pd.DataFrame, FR: pd.DataFrame, W: pd.DataFrame, cost_pct: float) -> pd.DataFrame:
    """Дневной результат: веса дня t держатся в день t+1. Комиссия с оборота, фандинг платит лонг при плюсе."""
    w = W.shift(1).fillna(0)
    ret = C.pct_change().fillna(0)
    turn = w.diff().abs().sum(1)
    fees = turn * cost_pct / 100
    fund = (w * FR).sum(1)
    pnl = (w * ret).sum(1) - fees - fund
    return pd.DataFrame({"pnl": pnl, "fees": fees, "funding": fund, "gross": w.abs().sum(1)})


def stats(daily: pd.DataFrame) -> dict:
    p = daily.pnl
    eq = (1 + p).cumprod()
    years = {}
    for y, g in p.groupby(pd.to_datetime(p.index, unit="ms").year):
        years[int(y)] = round(((1 + g).prod() - 1) * 100, 1)
    n = max(len(p), 1)
    months = (1 + p).groupby(pd.to_datetime(p.index, unit="ms").to_period("M")).prod() - 1
    return {
        "days": int(n),
        "return_pct": round((eq.iloc[-1] - 1) * 100, 1) if len(eq) else 0.0,
        "cagr_pct": round((eq.iloc[-1] ** (365 / n) - 1) * 100, 1) if len(eq) else 0.0,
        "sharpe": round(float(p.mean() / p.std() * np.sqrt(365)), 2) if p.std() > 0 else 0.0,
        "max_drawdown_pct": round(float((eq / eq.cummax() - 1).min() * 100), 1) if len(eq) else 0.0,
        "avg_month_pct": round(float(months.mean() * 100), 2) if len(months) else 0.0,
        "positive_months_pct": round(float((months > 0).mean() * 100), 0) if len(months) else 0.0,
        "avg_gross": round(float(daily.gross.mean()), 2),
        "fees_pct": round(float(daily.fees.sum() * 100), 1),
        "years": years,
    }


def backtest(conn, cfg, start: str, end: str) -> dict:
    pc = cfg["portfolio"]
    s = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    e = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp() * 1000)
    C, V, FR = daily_frames(conn, None, s - 400 * DAY, e)
    W = weights(C, V, pc)
    d = simulate(C, FR, W, cfg["trade"]["fee_taker_pct"] * pc.get("cost_mult", 1.0))
    d = d[(d.index >= s) & (d.index < e)]
    liq = V.rolling(30, min_periods=20).mean()
    uni = (liq.rank(axis=1, ascending=False) <= pc["top_n"]).shift(1).fillna(False)
    bh = C.pct_change().where(uni).mean(1).fillna(0)
    bh = bh[(bh.index >= s) & (bh.index < e)]
    btc = C["BTCUSDT"].pct_change().fillna(0) if "BTCUSDT" in C else bh * 0
    btc = btc[(btc.index >= s) & (btc.index < e)]
    z = pd.DataFrame({"pnl": bh, "fees": 0.0, "funding": 0.0, "gross": 1.0})
    zb = pd.DataFrame({"pnl": btc, "fees": 0.0, "funding": 0.0, "gross": 1.0})
    return {"period": [start, end], "coins_seen": int((W.abs() > 0).any().sum()), "strategy": stats(d),
            "buy_and_hold": stats(z), "btc": stats(zb), "params": pc}


def to_markdown(r: dict) -> str:
    s, b, t = r["strategy"], r["buy_and_hold"], r["btc"]
    yrs = sorted(s["years"])
    lines = [f"# Трендовый портфель, {r['period'][0]} .. {r['period'][1]}", "",
             f"Монет за период: {r['coins_seen']} (универсум по обороту на каждый день, включая умершие монеты).", "",
             "| | Портфель | Buy and hold топа | BTC |", "|---|---|---|---|",
             f"| Годовая доходность, % | {s['cagr_pct']} | {b['cagr_pct']} | {t['cagr_pct']} |",
             f"| Средний месяц, % | {s['avg_month_pct']} | {b['avg_month_pct']} | {t['avg_month_pct']} |",
             f"| Макс. просадка, % | {s['max_drawdown_pct']} | {b['max_drawdown_pct']} | {t['max_drawdown_pct']} |",
             f"| Sharpe | {s['sharpe']} | {b['sharpe']} | {t['sharpe']} |",
             *[f"| {y}, % | {s['years'].get(y)} | {b['years'].get(y)} | {t['years'].get(y)} |" for y in yrs],
             "", f"Месяцев в плюсе: {s['positive_months_pct']}%. Среднее плечо портфеля: {s['avg_gross']}. "
                 f"Комиссии за период: {s['fees_pct']}% капитала."]
    return "\n".join(lines) + "\n"


class LivePortfolio:
    """Каждые сутки: новые целевые веса, бумажный результат прошлых суток, пост при изменении состава."""

    def __init__(self, cfg, conn, publisher):
        self.cfg, self.conn, self.pub = cfg, conn, publisher

    def last_weights(self) -> tuple[int | None, dict]:
        r = self.conn.execute("SELECT MAX(day) FROM portfolio_weights").fetchone()[0]
        if r is None:
            return None, {}
        return r, {s: w for s, w in self.conn.execute("SELECT symbol, weight FROM portfolio_weights WHERE day=?", (r,))}

    def equity(self) -> float:
        r = self.conn.execute("SELECT equity FROM portfolio_equity ORDER BY day DESC LIMIT 1").fetchone()
        return r[0] if r else 1.0

    async def run_daily(self, now_ms: int, symbols: list[str]) -> str | None:
        from . import templates
        pc = self.cfg["portfolio"]
        day = now_ms // DAY * DAY - DAY                     # последние полные сутки
        prev_day, prev = self.last_weights()
        if prev_day is not None and prev_day >= day:
            return None
        C, V, FR = daily_frames(self.conn, symbols, day - 400 * DAY, day + DAY)
        if C.empty or day not in C.index:
            log.warning("portfolio: no full daily bar for %s", day)
            return None
        W = weights(C, V, pc, universe=symbols)
        new = {s: round(float(w), 4) for s, w in W.loc[day].items() if abs(w) >= 0.001}
        # бумажный результат суток day по весам, выставленным на закрытии prev_day
        pnl = 0.0
        if prev:
            ret = C.pct_change().loc[day]
            pnl = sum(w * float(ret.get(s, 0) or 0) - w * float(FR.loc[day].get(s, 0)) for s, w in prev.items())
            turn = sum(abs(new.get(s, 0) - prev.get(s, 0)) for s in set(new) | set(prev))
            pnl -= turn * self.cfg["trade"]["fee_taker_pct"] / 100
        eq = self.equity() * (1 + pnl)
        self.conn.executemany("INSERT OR REPLACE INTO portfolio_weights VALUES (?,?,?)",
                              [(day, s, w) for s, w in new.items()])
        self.conn.execute("INSERT OR REPLACE INTO portfolio_equity VALUES (?,?,?,?)",
                          (day, eq, pnl * 100, sum(abs(w) for w in new.values())))
        opened = [s for s in new if s not in prev or np.sign(new[s]) != np.sign(prev[s])]
        closed = [s for s in prev if s not in new]
        weekly = datetime.fromtimestamp(day / 1000, timezone.utc).weekday() == 6
        if not prev or opened or closed or weekly:
            text, nums = templates.portfolio(new, opened, closed, (eq - 1) * 100, pnl * 100, pc,
                                             self.cfg["publishing"]["mode"], full=weekly or not prev)
            return await self.pub.publish("portfolio", text, nums)
        return None
