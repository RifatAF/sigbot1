"""Английские шаблоны постов. Каждое число проходит через Fmt и попадает в список чисел движка,
по которому потом сверяется итоговый текст."""
from __future__ import annotations

from html import escape


class Fmt:
    def __init__(self):
        self.numbers: list[float] = []

    def _keep(self, s: str) -> str:
        self.numbers.append(float(s.replace(",", "").replace("+", "")))
        return s

    def p(self, x: float) -> str:
        """Цена с разумной точностью."""
        a = abs(x)
        d = 2 if a >= 100 else 3 if a >= 10 else 4 if a >= 1 else 5 if a >= 0.1 else 6 if a >= 0.01 else 8
        return self._keep(f"{x:,.{d}f}")

    def n(self, x: float, d: int = 2, sign: bool = False) -> str:
        return self._keep(f"{x:+.{d}f}" if sign else f"{x:.{d}f}")

    def i(self, x: int) -> str:
        return self._keep(str(int(x)))


def _coin(symbol: str) -> str:
    return symbol.replace("USDT", "")


def _tag(mode: str) -> str:
    return "\n<i>#forwardtest</i>" if mode == "paper" else ""


def _div_human(labels: list[str]) -> str:
    names = {"rsi": "RSI", "cvd_fut": "futures CVD", "cvd_spot": "spot CVD", "oi": "open interest"}
    out = []
    for l in labels:
        kind, direction, osc = l.split(" ", 2)
        out.append(f"{kind} {direction} vs {names.get(osc, osc)}")
    return ", ".join(out)


def signal(sig: dict, comment: str = "", zones: list[dict] | None = None) -> tuple[str, list[float]]:
    f = Fmt()
    side = sig["side"].upper()
    arrow = "🟢" if sig["side"] == "long" else "🔴"
    feats = sig.get("features", {})
    lines = [
        f"{arrow} <b>{_coin(sig['symbol'])} {side}</b>  #{_coin(sig['symbol'])}",
        "",
        f"Entry: <b>{f.p(sig['entry'])}</b>",
        f"Stop: {f.p(sig['stop'])}",
        *([f"Reference target: {f.p(sig['tp2'])} (exit by trailing stop)"] if feats.get("setup") == "trend" else
          [f"TP1: {f.p(sig['tp1'])} (close {f.i(50)}%)", f"TP2: {f.p(sig['tp2'])}"]),
        "",
        f"Risk: {f.n(sig['risk_pct'])}% of deposit · effective leverage x{f.n(sig['leverage'], 1)}",
        f"Strength: {f.n(sig['strength'])}",
    ]
    if feats.get("setup") == "capitulation" and feats.get("drop_sigma") is not None:
        lines.append(f"Setup: rebound after a liquidation flush, 24h drop {f.n(feats['drop_sigma'], 1)} sigma, "
                     "open interest flushed")
    if feats.get("setup") == "trend":
        ma = feats.get("ma", [20, 50])
        lines.append(f"Setup: daily trend, MA{f.i(ma[0])} crossed {'above' if sig['side'] == 'long' else 'below'} "
                     f"MA{f.i(ma[1])}, holding days to weeks")
    if feats.get("divergences"):
        lines.append(f"Divergences: {escape(_div_human(feats['divergences']), quote=False)}")
    trend = {1: "up", -1: "down", 0: "flat"}.get(feats.get("trend_4h"), "n/a")
    lines.append(f"4h trend: {trend}")
    if feats.get("fear_greed") is not None:
        lines.append(f"Fear &amp; Greed: {f.i(feats['fear_greed'])}")
    if feats.get("tp2_from_liq_cluster"):
        lines.append("TP2 sits at an estimated liquidation cluster.")
    if zones:
        z = [f"{f.p(x['low'])}-{f.p(x['high'])} ({f.i(x['sources'])} src)" for x in zones]
        lines.append("Watch zones from tracked analysts (not used for entry): " + "; ".join(z))
    if comment:
        lines += ["", escape(comment, quote=False)]
    if feats.get("setup") == "trend":
        lines.append(f"\nExit: trailing stop {f.n(feats['trail_atr'], 1)} daily ATR from the daily close, "
                     "TP is a reference only. Not financial advice." + _tag(sig.get("mode", "")))
    else:
        lines.append("\nStop moves to breakeven halfway to TP2. Not financial advice." + _tag(sig.get("mode", "")))
    return "\n".join(lines), f.numbers


EVENT_TEXT = {
    "breakeven": "Stop moved to breakeven",
    "tp1": "TP1 hit, half closed",
    "tp2": "TP2 hit, position closed",
    "stop": "Stopped out",
    "breakeven_stop": "Closed at breakeven",
    "trail_stop": "Trailing stop hit, position closed",
    "invalidation": "Idea invalidated, closed",
    "timeout": "Time exit, closed",
    "manual": "Closed",
}


def update(sig: dict, event: str, price: float, r_total: float | None = None) -> tuple[str, list[float]]:
    f = Fmt()
    icon = {"tp1": "✅", "tp2": "🎯", "stop": "❌", "breakeven": "🛡", "breakeven_stop": "➖", "trail_stop": "🎯",
            "invalidation": "⚠️", "timeout": "⏱"}.get(event, "•")
    lines = [f"{icon} <b>{_coin(sig['symbol'])} {sig['side'].upper()}</b>: {EVENT_TEXT.get(event, event)}",
             f"Price: {f.p(price)}"]
    if r_total is not None:
        lines.append(f"Result: {f.n(r_total, 2, sign=True)}R")
    return "\n".join(lines) + _tag(sig.get("mode", "")), f.numbers


def daily_note(m: dict, comment: str = "") -> tuple[str, list[float]]:
    f = Fmt()
    lines = ["📊 <b>Market note</b>", ""]
    if m.get("btc_price") is not None:
        lines.append(f"BTC: {f.p(m['btc_price'])} ({f.n(m['btc_change_24h'], 2, sign=True)}% 24h)")
    if m.get("avg_funding_pct") is not None:
        lines.append(f"Avg funding: {f.n(m['avg_funding_pct'], 4, sign=True)}%")
    if m.get("fear_greed") is not None:
        lines.append(f"Fear &amp; Greed: {f.i(m['fear_greed'])}")
    if m.get("coinbase_premium_pct") is not None:
        lines.append(f"Coinbase premium: {f.n(m['coinbase_premium_pct'], 3, sign=True)}%")
    lines.append(f"Open trades: {f.i(m['open_trades'])}")
    if m.get("signals_today", 0) == 0:
        lines += ["", "No signal, we wait."]
    if comment:
        lines += ["", escape(comment, quote=False)]
    return "\n".join(lines), f.numbers


def weekly(trades: list[dict], totals: dict) -> tuple[str, list[float]]:
    f = Fmt()
    lines = ["🗓 <b>Weekly results</b>", ""]
    if not trades:
        lines.append("No closed trades this week.")
    for t in trades:
        mark = "✅" if t["r"] > 0 else "❌" if t["r"] < 0 else "➖"
        lines.append(f"{mark} {_coin(t['symbol'])} {t['side']}: {f.n(t['r'], 2, sign=True)}R "
                     f"({f.n(t['pnl_pct'], 2, sign=True)}%)")
    if trades:
        lines += ["", f"Trades: {f.i(totals['trades'])} · wins: {f.i(totals['wins'])} · "
                      f"total: {f.n(totals['sum_r'], 2, sign=True)}R ({f.n(totals['pnl_pct'], 2, sign=True)}% of deposit)",
                  "All trades are listed, including losses."]
    return "\n".join(lines), f.numbers


def method(cfg: dict, bt: dict | None) -> tuple[str, list[float]]:
    f = Fmt()
    rc = cfg["risk"]
    lines = [
        "📌 <b>How this channel works</b>", "",
        "Signals come from a deterministic engine, not from a person or an AI model. "
        "The bot never places orders; it only publishes ideas.", "",
        f"• Universe: top {f.i(cfg['universe']['top_n'])} USDT perpetuals by volume, scanned every hour.",
        "• Inputs: open interest, funding, futures and spot CVD, liquidations, Coinbase premium, Fear &amp; Greed.",
        ("• Entries: long after a liquidation flush (a sharp 24h drop with open interest falling), "
         "wide stop, short target, exit by time. Divergences are shown as context."
         if "capitulation" in str(cfg["engine"].get("setup")) else
         "• Entries need bullish or bearish divergences (RSI, CVD, open interest) in line with the 4h trend."),
        *(["• Trend trades: daily MA20/MA50 cross, long and short, trailing stop from the daily close, "
           "held from days to weeks."] if "trend" in str(cfg["engine"].get("setup")) else []),
        f"• Risk per trade: {f.n(rc['risk_min_pct'], 1)}% to {f.n(rc['risk_max_pct'], 1)}% of deposit, "
        f"leverage capped at x{f.i(rc['public_leverage_cap'])}.",
        "• Management: half closed at TP1, stop to breakeven halfway to TP2, exit if the idea breaks. No averaging down.",
        "• Every trade is logged in an append-only journal. Losses are published too.",
    ]
    if bt:
        t, b = bt["total"], bt["benchmarks"]
        lines += ["", f"Walk forward backtest ({f.i(len(bt['symbols']))} coins, after fees and funding): "
                      f"{f.n(t['return_pct'], 1, sign=True)}% vs buy and hold {f.n(b['equal_weight_pct'], 1, sign=True)}%, "
                      f"max drawdown {f.n(t['max_drawdown_pct'], 1)}%."]
    lines += ["", "Not financial advice. Past results do not guarantee future results."]
    return "\n".join(lines), f.numbers


def portfolio(w: dict, opened: list[str], closed: list[str], total_pct: float, day_pct: float, pc: dict,
              mode: str, full: bool = False) -> tuple[str, list[float]]:
    """Пост трендового портфеля: что открылось, что закрылось, при full весь состав."""
    f = Fmt()
    lines = ["🧭 <b>Trend portfolio</b>", ""]
    def row(s):
        side = "LONG" if w[s] > 0 else "SHORT"
        return f"{_coin(s)} {side} {f.n(abs(w[s]) * 100, 1)}%"
    if opened:
        lines.append("Opened: " + ", ".join(row(s) for s in sorted(opened, key=lambda x: -abs(w[x]))))
    if closed:
        lines.append("Closed: " + ", ".join(_coin(s) for s in sorted(closed)))
    if full:
        longs = sorted([s for s in w if w[s] > 0], key=lambda x: -w[x])
        shorts = sorted([s for s in w if w[s] < 0], key=lambda x: w[x])
        if opened or closed:
            lines.append("")
        lines.append("🟢 " + (", ".join(row(s) for s in longs) if longs else "no longs"))
        lines.append("🔴 " + (", ".join(row(s) for s in shorts) if shorts else "no shorts"))
    gross = sum(abs(x) for x in w.values())
    lines += ["", f"Exposure: {f.n(gross * 100, 0)}% of capital · target volatility {f.i(pc['target_vol'] * 100)}% a year",
              f"Portfolio since start: {f.n(total_pct, 2, sign=True)}% · last day {f.n(day_pct, 2, sign=True)}%",
              "",
              "Weights are % of capital per coin, rebalanced daily at the daily close. "
              "Trend following across daily channels and moving averages, sized by volatility. Not financial advice."
              + _tag(mode)]
    return "\n".join(lines), f.numbers


def trend_signal(symbol: str, side: str, entry: float, size_pct: float, exit_level: float | None, mode: str,
                 stats: dict | None = None) -> tuple[str, list[float]]:
    """Сигнал трендового портфеля: вход, размер, уровень выхода. Цели нет: держим, пока тренд жив."""
    f = Fmt()
    long = side == "long"
    lines = [f"{'🟢' if long else '🔴'} <b>{_coin(symbol)} {'LONG' if long else 'SHORT'}</b>  #{_coin(symbol)} · trend", "",
             f"Entry: <b>{f.p(entry)}</b> (daily close)",
             f"Size: {f.n(size_pct, 1)}% of capital"]
    if exit_level:
        lines.append(f"Exit: daily close {'below' if long else 'above'} <b>{f.p(exit_level)}</b> (the trend breaks). "
                     "The level moves with the trend, updates come in the weekly list")
    else:
        lines.append("Exit: when the trend breaks, the bot posts it")
    lines.append("No take profit: the position rides the trend as long as it lasts")
    if stats and stats.get("win_pct"):
        lines += ["", f"Backtest of these signals: {f.i(stats['win_pct'])}% win, winners average "
                      f"+{f.n(stats['avg_win'], 2)}% of capital, losers {f.n(stats['avg_loss'], 2)}%. "
                      "Few big wins pay for many small losses."]
    lines += ["", "Trend following on daily channels and moving averages. Not financial advice." + _tag(mode)]
    return "\n".join(lines), f.numbers


def trend_exit(symbol: str, side: str, entry: float, price: float, move_pct: float, result_pct: float,
               days: int, mode: str) -> tuple[str, list[float]]:
    f = Fmt()
    head = "✅ Closed in profit" if move_pct > 0 else "❌ Closed"
    lines = [f"{head}: <b>{_coin(symbol)} {'LONG' if side == 'long' else 'SHORT'}</b> (trend ended)",
             f"Entry {f.p(entry)} → exit {f.p(price)} after {f.i(days)} days",
             f"Price move in our favour: {f.n(move_pct, 2, sign=True)}%",
             f"Result: {f.n(result_pct, 2, sign=True)}% of capital" + _tag(mode)]
    return "\n".join(lines), f.numbers


def trend_open_list(rows: list[dict], mode: str) -> tuple[str, list[float]]:
    """Раз в неделю: открытые сигналы тренда с текущими уровнями выхода."""
    f = Fmt()
    lines = ["🧭 <b>Open trend signals</b>", ""]
    for r in rows:
        lv = f"exit {'below' if r['side'] == 'long' else 'above'} {f.p(r['exit_level'])}" if r.get("exit_level") else "exit on trend break"
        lines.append(f"{'🟢' if r['side'] == 'long' else '🔴'} {_coin(r['symbol'])} {r['side'].upper()} "
                     f"from {f.p(r['entry'])}, {f.n(r['size_pct'], 1)}% · {lv}")
    if not rows:
        lines.append("No open trend signals")
    lines += ["", "Exit levels are daily closes. Not financial advice." + _tag(mode)]
    return "\n".join(lines), f.numbers


def listing_signal(symbol: str, entry: float, stop: float, lc: dict, mode: str) -> tuple[str, list[float]]:
    f = Fmt()
    lines = [f"🔴 <b>{_coin(symbol)} SHORT</b>  #{_coin(symbol)} · new listing", "",
             f"Entry: <b>{f.p(entry)}</b>",
             f"Stop: {f.p(stop)} (+{f.i(lc['stop_pct'])}%)",
             f"Exit: after {f.i(lc['hold_days'])} days or at the stop",
             f"Size: {f.n(lc['notional_pct'], 1)}% of capital, no leverage needed", "",
             "Most new perpetual listings drift lower in their first two months as supply unlocks. "
             "A few explode upward, so the stop is wide and the size is small. Not financial advice." + _tag(mode)]
    return "\n".join(lines), f.numbers


def listing_exit(symbol: str, entry: float, price: float, reason: str, pnl_pct: float, mode: str) -> tuple[str, list[float]]:
    f = Fmt()
    head = "❌ Stopped out" if reason == "stop" else "⏱ Time exit"
    lines = [f"{head}: <b>{_coin(symbol)} SHORT</b> (new listing)",
             f"Entry {f.p(entry)} → exit {f.p(price)}",
             f"Result: {f.n(pnl_pct, 2, sign=True)}% of capital" + _tag(mode)]
    return "\n".join(lines), f.numbers
