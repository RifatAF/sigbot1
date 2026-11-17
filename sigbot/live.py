"""Живая работа: часовой скан, поминутное ведение сделок, публикации по событиям."""
from __future__ import annotations

import json
import logging
import time

import numpy as np
import pandas as pd

from . import chart, engine, features, ledger, risk, scanner, settings, templates
from . import indicators as ind
from .trade import TradeState, step

log = logging.getLogger(__name__)
HOUR = 3_600_000
DAY = 86_400_000


def now_ms() -> int:
    return int(time.time() * 1000)


class Live:
    def __init__(self, cfg, conn, publisher, gemini, bloggers=None):
        self.cfg, self.conn, self.pub, self.llm, self.bloggers = cfg, conn, publisher, gemini, bloggers

    def eff(self):
        return settings.effective(self.cfg, self.conn)

    # ---------- данные ----------
    def frame(self, symbol: str, t: int) -> pd.DataFrame:
        cfg = self.cfg
        hours = cfg["indicators"]["zscore_window"] + 400
        df = features.build(self.conn, symbol, t - hours * HOUR, t)
        df = df[df.ts + HOUR <= t].reset_index(drop=True)   # только закрытые свечи
        return df

    def open_trades(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT s.*, t.stop AS cur_stop, t.remaining, t.be_moved, t.last_ts FROM trade_state t "
            "JOIN signals s ON s.id = t.signal_id WHERE t.status='open'").fetchall()
        return [dict(r) for r in rows]

    # ---------- скан ----------
    async def scan(self, t: int | None = None) -> list[int]:
        t = t or now_ms() // HOUR * HOUR
        cfg = self.eff()
        if cfg["publishing"].get("kill_switch"):
            return []
        symbols = settings.get(self.conn, "symbols_manual") or settings.get(self.conn, "universe", [])
        cands, closes, frames = [], {}, {}
        for sym in symbols:
            try:
                df = self.frame(sym, t)
                if len(df) < cfg["indicators"]["zscore_window"] + 50:
                    continue
                p = engine.prepare(df, cfg)
                frames[sym] = p
                closes[sym] = p.set_index("ts").close
                c = engine.decide(p, len(p) - 1, sym, cfg)
                if c is None:
                    continue
                if "oi" in p:
                    w = p.tail(720)
                    lm = ind.liquidation_map(w.close, w.oi.ffill().fillna(0), cfg["liq_map"]["leverages"],
                                             cfg["liq_map"]["bin_pct"])
                    clusters = ind.nearest_clusters(lm, c.entry)
                    c = engine.decide(p, len(p) - 1, sym, cfg, liq_clusters=clusters) or c
                    c.features["liq_clusters"] = clusters
                cands.append(c)
            except Exception as e:
                log.exception("scan %s: %s", sym, e)
        settings.put(self.conn, "last_scan", {"ts": t, "symbols": len(symbols), "candidates": len(cands)})
        if not cands:
            return []
        day_start = t // DAY * DAY
        taken = self.conn.execute("SELECT COUNT(*) FROM signals WHERE ts>=? AND source='live'",
                                  (day_start,)).fetchone()[0]
        corr = scanner.correlation_matrix(closes, cfg["scanner"]["correlation_window_hours"])
        chosen = scanner.select(cands, [(o["symbol"], o["side"]) for o in self.open_trades()], taken,
                                cfg["scanner"]["max_signals_per_day"], corr, cfg["scanner"]["correlation_threshold"])
        ids = []
        rc = cfg["risk"]
        for c in chosen:
            orisk = [o["risk_pct"] for o in self.open_trades()]
            results = [(x["closed_ts"], x["pnl_pct"]) for x in
                       sorted(self.closed_trades(0, t + DAY), key=lambda x: x["closed_ts"])]
            g = risk.guard(rc, results, orisk, t)
            sz = risk.size_guarded(c.entry, c.stop, c.side, c.strength, rc["deposit_usd"], rc, g, orisk)
            if sz is None:
                log.info("skip %s %s: %s", c.symbol, c.side, g.reason or "stop too tight or no risk room")
                if not g.allow and settings.get(self.conn, "guard_notice") != g.reason:
                    settings.put(self.conn, "guard_notice", g.reason)
                    await self.pub.notify_owner(f"Risk guard: new signals stopped ({g.reason}).")
                continue
            ids.append(await self.open_signal(c, frames[c.symbol], cfg, sz))
        return ids

    async def open_signal(self, c: engine.Candidate, p: pd.DataFrame, cfg, sz: risk.Sizing | None = None) -> int:
        rc = cfg["risk"]
        sz = sz or risk.size(c.entry, c.stop, c.side, c.strength, rc["deposit_usd"], rc["risk_min_pct"],
                             rc["risk_max_pct"], rc["public_leverage_cap"], rc["maintenance_margin_pct"])
        feats = dict(c.features)
        feats.update({"invalidation": c.invalidation, "liq_price": sz.liq_price, "leverage_capped": sz.capped,
                      "personal_leverage": min(sz.leverage, rc["personal_leverage_cap"])})
        zones = self.bloggers.zones_for(c.symbol, c.entry) if self.bloggers else []
        if zones:
            feats["attention_zones"] = zones
        sig = ledger.Signal(cfg["params_version"], c.ts + HOUR, c.symbol, c.side, c.entry, c.stop, c.tp1, c.tp2,
                            sz.risk_pct, sz.leverage, c.strength, cfg["publishing"]["mode"], feats)
        sid = ledger.add_signal(self.conn, sig)
        self.conn.execute("INSERT INTO trade_state VALUES (?,?,?,?,?,?)", (sid, "open", c.stop, 1.0, 0, sig.ts))

        d = {**sig.__dict__, "features": feats}
        facts = {"coin": c.symbol.replace("USDT", ""), "side": c.side, "divergences": feats.get("divergences"),
                 "trend_4h": feats.get("trend_4h"), "funding_sign": None if feats.get("funding") is None
                 else ("negative" if feats["funding"] < 0 else "positive"),
                 "tp2_at_liquidation_cluster": feats.get("tp2_from_liq_cluster")}
        comment = await self.llm.comment(facts) if self.llm else ""
        text, nums = templates.signal(d, comment, zones)
        png = None
        try:
            html = chart.build_html(c.symbol, c.side, chart.candles_from_df(p, 96),
                                    {"entry": c.entry, "stop": c.stop, "tp1": c.tp1, "tp2": c.tp2},
                                    zones, feats.get("liq_clusters"),
                                    subtitle=f"Strength {c.strength:.2f}", risk_pct=sz.risk_pct,
                                    leverage=sz.leverage)
            png = await chart.render_png(html)
        except Exception as e:
            log.warning("chart render failed: %s", e)
        status = await self.pub.publish("signal", text, nums, sid, png)
        log.info("signal %s %s %s -> %s", sid, c.symbol, c.side, status)
        return sid

    # ---------- ведение ----------
    def _state(self, o: dict) -> TradeState:
        f = json.loads(o["features_json"])
        events = [r["event"] for r in self.conn.execute("SELECT event FROM outcomes WHERE signal_id=?", (o["id"],))]
        m = f.get("manage", {})
        st = TradeState(o["side"], o["entry"], o["cur_stop"], f.get("invalidation", o["stop"]), o["tp1"], o["tp2"],
                        o["ts"], remaining=o["remaining"], be_moved=bool(o["be_moved"]), tp1_done="tp1" in events,
                        trail=m.get("trail", 0.0), max_hold_h=m.get("max_hold_h"), tp1_frac=m.get("tp1_frac"),
                        use_breakeven=m.get("breakeven", True))
        st.trailed = "trail" in events
        if st.trail:
            prev = self.conn.execute("SELECT ts, close FROM candles WHERE symbol=? AND market='futures' AND tf='1m' "
                                     "AND ts<? ORDER BY ts DESC LIMIT 1", (o["symbol"], o["last_ts"])).fetchone()
            if prev:
                st.last_day, st.last_close = prev[0] // 86_400_000, prev[1]
        return st

    async def manage(self, t: int | None = None):
        t = t or now_ms()
        for o in self.open_trades():
            st = self._state(o)
            bars = self.conn.execute(
                "SELECT ts, high, low, close FROM candles WHERE symbol=? AND market='futures' AND tf='1m' "
                "AND ts>=? AND ts+60000<=? ORDER BY ts", (o["symbol"], o["last_ts"], t)).fetchall()
            last = o["last_ts"]
            for b in bars:
                bar_end = b["ts"] + 60_000
                hour_close = bar_end % HOUR == 0
                evs = step(st, b["ts"], b["high"], b["low"], b["close"], self.cfg, bar_closed=False)
                if st.status == "open" and hour_close:
                    hc = self.conn.execute("SELECT close FROM candles WHERE symbol=? AND market='futures' "
                                           "AND tf='1h' AND ts=?", (o["symbol"], bar_end - HOUR)).fetchone()
                    c = hc["close"] if hc else b["close"]
                    evs += step(st, bar_end, c, c, c, self.cfg, bar_closed=True)
                last = bar_end
                for ev in evs:
                    await self._record(o, st, ev)
                if st.status != "open":
                    break
            self.conn.execute("UPDATE trade_state SET status=?, stop=?, remaining=?, be_moved=?, last_ts=? "
                              "WHERE signal_id=?", (st.status, st.stop, st.remaining, int(st.be_moved), last, o["id"]))

    async def _record(self, o: dict, st: TradeState, ev):
        r_unit = abs(o["entry"] - o["stop"])
        part_r = ev.fraction * st.sgn * (ev.price - o["entry"]) / r_unit if ev.fraction else None
        ledger.add_outcome(self.conn, o["id"], ev.ts, ev.event, ev.price, ev.fraction,
                           None if part_r is None else round(part_r, 4))
        total = None
        if st.status == "closed":
            total = self.conn.execute("SELECT COALESCE(SUM(pnl_r),0) FROM outcomes WHERE signal_id=?",
                                      (o["id"],)).fetchone()[0]
        if ev.event == "trail":
            return   # трейлинг подтягивается каждые сутки: в журнал, без поста
        text, nums = templates.update(o, ev.event, ev.price, total)
        await self.pub.publish("update", text, nums, o["id"])

    # ---------- регулярные посты ----------
    def market_snapshot(self) -> dict:
        t = now_ms()
        m: dict = {}
        btc = self.conn.execute("SELECT ts, close FROM candles WHERE symbol='BTCUSDT' AND market='futures' "
                                "AND tf='1h' ORDER BY ts DESC LIMIT 25").fetchall()
        if len(btc) >= 25:
            m["btc_price"] = btc[0]["close"]
            m["btc_change_24h"] = (btc[0]["close"] / btc[24]["close"] - 1) * 100
        fr = self.conn.execute("SELECT AVG(rate) FROM (SELECT symbol, rate, MAX(ts) FROM funding "
                               "WHERE ts>? GROUP BY symbol)", (t - DAY,)).fetchone()[0]
        if fr is not None:
            m["avg_funding_pct"] = fr * 100
        fg = self.conn.execute("SELECT value FROM fear_greed ORDER BY ts DESC LIMIT 1").fetchone()
        if fg:
            m["fear_greed"] = fg[0]
        pr = self.conn.execute("SELECT AVG(premium_pct) FROM premium WHERE ts>?", (t - HOUR,)).fetchone()[0]
        if pr is not None:
            m["coinbase_premium_pct"] = pr
        m["open_trades"] = len(self.open_trades())
        m["signals_today"] = self.conn.execute("SELECT COUNT(*) FROM signals WHERE ts>=? AND source='live'",
                                               (t // DAY * DAY,)).fetchone()[0]
        return m

    async def daily_note(self):
        m = self.market_snapshot()
        facts = {"btc_change_direction": None if "btc_change_24h" not in m else
                 ("up" if m["btc_change_24h"] > 0 else "down"),
                 "funding": None if "avg_funding_pct" not in m else ("negative" if m["avg_funding_pct"] < 0 else "positive"),
                 "fear_greed_zone": None if "fear_greed" not in m else
                 ("extreme fear" if m["fear_greed"] < 25 else "fear" if m["fear_greed"] < 45 else
                  "neutral" if m["fear_greed"] < 56 else "greed" if m["fear_greed"] < 76 else "extreme greed"),
                 "new_signals_today": m["signals_today"] > 0}
        comment = await self.llm.comment(facts) if self.llm else ""
        text, nums = templates.daily_note(m, comment)
        return await self.pub.publish("daily", text, nums)

    def closed_trades(self, start: int, end: int) -> list[dict]:
        cfg = self.cfg
        rows = self.conn.execute(
            "SELECT s.id, s.symbol, s.side, s.leverage, s.entry, s.risk_pct, MAX(o.ts) AS closed_ts, "
            "SUM(o.pnl_r) AS r FROM signals s JOIN outcomes o ON o.signal_id=s.id "
            "JOIN trade_state t ON t.signal_id=s.id WHERE t.status='closed' AND s.source='live' "
            "GROUP BY s.id HAVING closed_ts>=? AND closed_ts<?", (start, end)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["r"] = round(d["r"] or 0.0, 2)
            fee = d["leverage"] * cfg["trade"]["fee_taker_pct"] * 2
            d["pnl_pct"] = round(d["r"] * d["risk_pct"] - fee, 2)
            out.append(d)
        return out

    async def weekly(self):
        t = now_ms()
        trades = self.closed_trades(t - 7 * DAY, t)
        totals = {"trades": len(trades), "wins": sum(1 for x in trades if x["r"] > 0),
                  "sum_r": sum(x["r"] for x in trades), "pnl_pct": sum(x["pnl_pct"] for x in trades)}
        text, nums = templates.weekly(trades, totals)
        return await self.pub.publish("weekly", text, nums)
