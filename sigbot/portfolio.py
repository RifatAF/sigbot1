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
    g = fr.groupby(["day", "symbol"]).rate
    # архив: одна строка на расчёт (сумма за сутки); живой сборщик пишет прогноз каждые 5 минут: среднее × 3 расчёта
    daily = g.sum().where(g.count() <= 6, g.mean() * 3)
    FR = daily.unstack().reindex(index=C.index, columns=C.columns).fillna(0)
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


def weights(C: pd.DataFrame, V: pd.DataFrame, pc: dict, universe: list[str] | None = None,
            FR: pd.DataFrame | None = None) -> pd.DataFrame:
    """Целевые веса на закрытии каждого дня (доля депозита, знак = сторона): тренд плюс лонг отрицательного фандинга."""
    W = trend_weights(C, V, pc, universe)
    fc = pc.get("neg_funding")
    if fc and FR is not None:
        W = W.add(neg_funding_weights(C, FR, fc, W.columns), fill_value=0)
    return W


def neg_funding_weights(C: pd.DataFrame, FR: pd.DataFrame, fc: dict, cols) -> pd.DataFrame:
    """Шорты переполнены и платят лонгам: фандинг за сутки ниже порога -> лонг на hold_days, size на монету."""
    neg = (FR.reindex(index=C.index, columns=cols).fillna(0) < -fc["threshold"]).astype(float)
    w = sum(neg.shift(k).fillna(0) for k in range(fc["hold_days"])) * fc["size"] / fc["hold_days"]
    return w.where(C.reindex(columns=cols).notna(), 0)


def trend_weights(C: pd.DataFrame, V: pd.DataFrame, pc: dict, universe: list[str] | None = None) -> pd.DataFrame:
    R = np.log(C).diff()
    if universe:
        uni = pd.DataFrame(False, index=C.index, columns=C.columns)
        uni[[c for c in universe if c in C.columns]] = True
    else:   # точечный во времени: топ N по среднему обороту за 30 дней
        liq = V.rolling(30, min_periods=20).mean()
        uni = liq.rank(axis=1, ascending=False) <= pc["top_n"]
    uni &= C.notna()
    vol = R.rolling(pc["vol_window"], min_periods=20).std()
    sig = signal(C, pc).where(uni)
    if pc.get("min_consensus"):   # только монеты, где почти все голоса согласны, и не больше max_positions самых уверенных
        sc = sig.abs()
        keep = (sc >= pc["min_consensus"]) & (sc.rank(axis=1, ascending=False, method="first") <= pc.get("max_positions", 99))
        sig = sig.where(keep)
    w = (sig / vol).replace([np.inf, -np.inf], np.nan).fillna(0)
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
    W = weights(C, V, pc, FR=FR)
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
        W = weights(C, V, pc, universe=symbols, FR=FR)
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
        return await self.signals(day, C, new, first=not prev)

    def _lang(self) -> str:
        from . import settings
        return settings.effective(self.cfg, self.conn)["publishing"].get("lang", "en")

    @staticmethod
    def _st(r) -> dict:
        import json
        if r["state"]:
            return json.loads(r["state"])
        return {"side": r["side"], "entry": r["entry"], "rem": 1.0, "exits": []}   # старый сигнал без плана

    def _save(self, rid: int, st: dict):
        import json
        self.conn.execute("UPDATE trend_signals SET state=? WHERE id=?", (json.dumps(st), rid))

    async def _closed(self, r, st: dict, day_ms: int, mode: str, label: str = "") -> str | None:
        """Сделка закрыта целиком: запись итога и один пост цепочкой вход → цели → выход."""
        from . import feed, templates
        mv, res = feed.realized(st, r["size_pct"], self.cfg["trade"]["fee_taker_pct"])
        last = st["exits"][-1]["price"] if st["exits"] else r["entry"]
        self.conn.execute("UPDATE trend_signals SET status='closed', close_day=?, exit_price=?, move_pct=?, result_pct=? "
                          "WHERE id=?", (int(day_ms), last, mv, res, r["id"]))
        self._save(r["id"], st)
        text, nums = templates.trade_closed(r["symbol"], r["side"], r["entry"], st["exits"], res,
                                            int((day_ms - r["open_day"]) / DAY), mode, self._lang(), label)
        return await self.pub.publish("trend_exit", text, nums)

    def _levels(self, sym: str, day: int, px: float, sd: int, C: pd.DataFrame | None = None) -> dict | None:
        """Стоп и цели от дневного ATR на день входа."""
        from . import feed
        plan = feed.plan_cfg(self.cfg["portfolio"])
        H, L, CL = feed.daily_ohlc(self.conn, day - 40 * DAY, day + DAY, [sym])
        av = np.nan
        if not CL.empty and sym in CL:
            a = feed.atr(H, L, CL, plan["atr_days"])[sym]
            av = float(a.get(day, a.dropna().iloc[-1] if a.notna().any() else np.nan))
        if np.isnan(av) and C is not None and sym in C:              # нет часовых свечей: размах по закрытиям
            av = float(C[sym].loc[:day].diff().abs().tail(plan["atr_days"]).mean()) * 1.3
        return None if np.isnan(av) or av <= 0 else feed.levels(px, av, sd, plan)

    async def manage(self, now_ms: int) -> None:
        """Каждый час: дошла ли цена открытых сигналов тренда до TP1, TP2 или стопа (по закрытым часовым свечам)."""
        from . import feed, templates
        mode = self.cfg["publishing"]["mode"]
        for r in self.conn.execute("SELECT * FROM trend_signals WHERE status='open'").fetchall():
            st = self._st(r)
            if st.get("stop") is None:                      # сигнал открыт до появления целей: ставим их с этого часа
                lv = self._levels(r["symbol"], int(r["open_day"]), r["entry"], 1 if r["side"] == "long" else -1)
                if lv is None:
                    continue
                st.update(lv, last_ts=now_ms // 3_600_000 * 3_600_000)
                self.conn.execute("UPDATE trend_signals SET stop=?, tp1=?, tp2=? WHERE id=?", (lv["stop"], lv["tp1"], lv["tp2"], r["id"]))
                self._save(r["id"], st)
                continue
            rows = self.conn.execute("SELECT ts, high, low FROM candles WHERE symbol=? AND market='futures' AND tf='1h' "
                                     "AND ts>=? AND ts<=? ORDER BY ts", (r["symbol"], st.get("last_ts", r["open_day"] + DAY),
                                                                         now_ms - 3_600_000)).fetchall()
            for ts, hi, lo in rows:
                st["last_ts"] = ts + 3_600_000
                for ev in feed.step(st, ts // 1000 + 3600, hi, lo):
                    if st["rem"] > 1e-9:
                        text, nums = templates.trade_event(r["symbol"], r["side"], ev["what"], ev["price"], r["entry"],
                                                           ev["part"], st["rem"], st["stop"], mode, self._lang())
                        await self.pub.publish("trend_update", text, nums)
                if st["rem"] <= 1e-9:
                    await self._closed(r, st, ts + 3_600_000, mode)
                    break
            else:
                self._save(r["id"], st)

    async def signals(self, day: int, C: pd.DataFrame, new: dict, first: bool = False) -> str | None:
        """Сигналы ленты: позиция от signal_min_weight открывается постом со стопом и двумя целями;
        цели и стоп ведёт manage() по часовым свечам; остаток закрывается, когда портфель убрал монету
        или развернул её. Раз в неделю список открытых."""
        from . import feed, templates
        day = int(day)
        pc, mode, lang = self.cfg["portfolio"], self.cfg["publishing"]["mode"], self._lang()
        thr = pc.get("signal_min_weight", 0.1)
        last = C.loc[day]
        sent = None
        stats = None
        if feed.FEED.exists():
            import json
            stats = json.loads(feed.FEED.read_text()).get("summary", {}).get("trend")
        for r in self.conn.execute("SELECT * FROM trend_signals WHERE status='open'").fetchall():
            w = new.get(r["symbol"], 0)
            px = float(last.get(r["symbol"], np.nan))
            if np.isnan(px):
                continue
            sd = 1 if r["side"] == "long" else -1
            if np.sign(w) == sd:
                lv = feed.exit_level(C[r["symbol"]].loc[:day].values, sd, pc)
                self.conn.execute("UPDATE trend_signals SET exit_level=? WHERE id=?", (lv, r["id"]))
                continue
            st = self._st(r)
            feed.finish(st, (day + DAY) // 1000, px, "trend")
            sent = await self._closed(r, st, day + DAY, mode) or sent
        open_syms = {r[0] for r in self.conn.execute("SELECT symbol FROM trend_signals WHERE status='open'")}
        for sym, w in sorted(new.items(), key=lambda x: -abs(x[1])):
            if abs(w) < thr or sym in open_syms:
                continue
            prev_w = self.conn.execute("SELECT weight FROM portfolio_weights WHERE symbol=? AND day=?",
                                       (sym, day - DAY)).fetchone()
            if prev_w and np.sign(prev_w[0]) == np.sign(w):        # позиция уже была до этого дня: не новый сигнал
                continue
            sd = 1 if w > 0 else -1
            side = "long" if sd > 0 else "short"
            px = float(last[sym])
            lv = feed.exit_level(C[sym].loc[:day].values, sd, pc)
            levels = self._levels(sym, day, px, sd, C)
            if levels is None:
                continue
            st = {**feed.new_state(side, px, levels), "last_ts": day + DAY}
            import json
            self.conn.execute("INSERT INTO trend_signals (symbol, side, open_day, entry, size_pct, exit_level, stop, tp1, tp2, state) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?)", (sym, side, day, px, round(abs(w) * 100, 1), lv,
                                                              levels["stop"], levels["tp1"], levels["tp2"], json.dumps(st)))
            if not first:                       # первый запуск: не заваливаем канал, ниже одним списком
                text, nums = templates.trend_signal(sym, side, px, abs(w) * 100, lv, mode, stats, levels, lang)
                sent = await self.pub.publish("trend_signal", text, nums) or sent
        if first or datetime.fromtimestamp(day / 1000, timezone.utc).weekday() == 6:
            rows = []
            for r in self.conn.execute("SELECT * FROM trend_signals WHERE status='open' ORDER BY size_pct DESC"):
                rows.append({**dict(r), "exits": self._st(r).get("exits", [])})
            text, nums = templates.trend_open_list(rows, mode, lang)
            sent = await self.pub.publish("trend_list", text, nums) or sent
        return sent
