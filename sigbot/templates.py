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
        d = 0 if a >= 10000 else 2 if a >= 100 else 3 if a >= 10 else 4 if a >= 1 else 5 if a >= 0.1 else 6 if a >= 0.01 else 8
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


def signal_ru(sig: dict) -> tuple[str, list[float]]:
    """Часовой сигнал коротко: вход, цели, стоп, риск. Без лишних индикаторов."""
    f, side, e = Fmt(), sig["side"], sig["entry"]
    feats = sig.get("features", {})
    lines = [f"{'🟢' if side == 'long' else '🔴'} <b>{_coin(sig['symbol'])} · {_side(side, 'ru')}</b>  #{_coin(sig['symbol'])}",
             "Отскок после слива" if feats.get("setup") == "capitulation" else "Часовой сигнал", "",
             f"Вход: <b>{f.p(e)}</b>",
             f"🎯 TP1: {f.p(sig['tp1'])} ({_pct(f, side, e, sig['tp1'])}) · закрыть {f.i(50)}%",
             f"🎯 TP2: {f.p(sig['tp2'])} ({_pct(f, side, e, sig['tp2'])}) · закрыть {f.i(50)}%",
             f"🛑 Стоп: {f.p(sig['stop'])} ({_pct(f, side, e, sig['stop'])})",
             "После TP1 стоп переносим в безубыток", "",
             f"Риск: {f.n(sig['risk_pct'], 1)}% депозита · плечо x{f.n(sig['leverage'], 1)}",
             _foot(sig.get("mode", ""), "ru")]
    return "\n".join(lines), f.numbers


def signal(sig: dict, comment: str = "", zones: list[dict] | None = None, lang: str = "en") -> tuple[str, list[float]]:
    if lang == "ru":
        return signal_ru(sig)
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


EVENT_RU = {"breakeven": "стоп в безубыток", "tp1": "TP1 взят, закрыта половина", "tp2": "TP2 взят, сделка закрыта",
            "stop": "стоп", "breakeven_stop": "закрыта в безубыток", "trail_stop": "трейлинг-стоп, закрыта",
            "invalidation": "идея сломалась, закрыта", "timeout": "закрыта по времени", "manual": "закрыта"}


def update(sig: dict, event: str, price: float, r_total: float | None = None, lang: str = "en",
           pnl_pct: float | None = None) -> tuple[str, list[float]]:
    if lang == "ru":
        f = Fmt()
        icon = {"tp1": "✅", "tp2": "🎯", "stop": "❌", "breakeven": "🛡", "breakeven_stop": "➖"}.get(event, "•")
        lines = [f"{icon} <b>{_coin(sig['symbol'])} {_side(sig['side'], 'ru')}: {EVENT_RU.get(event, event)}</b>",
                 f"Цена {f.p(price)} ({_pct(f, sig['side'], sig['entry'], price)} от входа {f.p(sig['entry'])})"]
        if r_total is not None:
            lines.append(f"Итог сделки: {f.n(r_total, 2, sign=True)}R"
                         + (f" ({f.n(pnl_pct, 2, sign=True)}% депозита)" if pnl_pct is not None else ""))
        return "\n".join(lines) + _tag(sig.get("mode", "")), f.numbers
    f = Fmt()
    icon = {"tp1": "✅", "tp2": "🎯", "stop": "❌", "breakeven": "🛡", "breakeven_stop": "➖", "trail_stop": "🎯",
            "invalidation": "⚠️", "timeout": "⏱"}.get(event, "•")
    lines = [f"{icon} <b>{_coin(sig['symbol'])} {sig['side'].upper()}</b>: {EVENT_TEXT.get(event, event)}",
             f"Price: {f.p(price)}"]
    if r_total is not None:
        lines.append(f"Result: {f.n(r_total, 2, sign=True)}R")
    return "\n".join(lines) + _tag(sig.get("mode", "")), f.numbers


def daily_note_ru(m: dict) -> tuple[str, list[float]]:
    f = Fmt()
    lines = ["📊 <b>Рынок за сутки</b>", ""]
    if m.get("btc_price") is not None:
        lines.append(f"BTC: {f.p(m['btc_price'])} ({f.n(m['btc_change_24h'], 2, sign=True)}% за сутки)")
    if m.get("avg_funding_pct") is not None:
        lines.append(f"Средний фандинг: {f.n(m['avg_funding_pct'], 4, sign=True)}%")
    if m.get("fear_greed") is not None:
        lines.append(f"Индекс страха и жадности: {f.i(m['fear_greed'])}")
    lines.append(f"Открытых часовых сделок: {f.i(m['open_trades'])}")
    if m.get("signals_today", 0) == 0:
        lines += ["", "Нового часового сигнала нет, ждём."]
    return "\n".join(lines), f.numbers


def daily_note(m: dict, comment: str = "", lang: str = "en") -> tuple[str, list[float]]:
    if lang == "ru":
        return daily_note_ru(m)
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


def weekly(trades: list[dict], totals: dict, lang: str = "en") -> tuple[str, list[float]]:
    f = Fmt()
    if lang == "ru":
        lines = ["🗓 <b>Часовые сделки за неделю</b>", ""]
        if not trades:
            lines.append("Закрытых сделок за неделю нет.")
        for t in trades:
            mark = "✅" if t["r"] > 0 else "❌" if t["r"] < 0 else "➖"
            lines.append(f"{mark} {_coin(t['symbol'])} {_side(t['side'], 'ru')}: {f.n(t['pnl_pct'], 2, sign=True)}% депозита")
        if trades:
            lines += ["", f"Сделок: {f.i(totals['trades'])}, в плюс: {f.i(totals['wins'])}, "
                          f"итог: <b>{f.n(totals['pnl_pct'], 2, sign=True)}%</b> депозита", "Показаны все сделки, включая убыточные."]
        return "\n".join(lines), f.numbers
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


def _side(side: str, lang: str) -> str:
    return ({"long": "ЛОНГ", "short": "ШОРТ"} if lang == "ru" else {"long": "LONG", "short": "SHORT"})[side]


def _pct(f: Fmt, side: str, entry: float, p: float) -> str:
    return f.n((p / entry - 1) * 100 * (1 if side == "long" else -1), 1, sign=True) + "%"


def _foot(mode: str, lang: str) -> str:
    return ("<i>Не финансовый совет</i>" if lang == "ru" else "<i>Not financial advice</i>") + _tag(mode)


def trend_signal(symbol: str, side: str, entry: float, size_pct: float, exit_level: float | None, mode: str,
                 stats: dict | None = None, plan: dict | None = None, lang: str = "en") -> tuple[str, list[float]]:
    """Сигнал тренда: вход, две цели с долей закрытия, стоп, остаток по тренду. Одна строка на уровень."""
    f, ru, long = Fmt(), lang == "ru", side == "long"
    lines = [f"{'🟢' if long else '🔴'} <b>{_coin(symbol)} · {_side(side, lang)}</b>  #{_coin(symbol)}",
             "Тренд по дневкам" if ru else "Daily trend", "",
             f"{'Вход' if ru else 'Entry'}: <b>{f.p(entry)}</b>"]
    rest = 1.0
    if plan:
        for k in ("tp1", "tp2"):
            part = plan[f"{k}_part"]
            rest -= part
            lines.append(f"🎯 {k.upper()}: {f.p(plan[k])} ({_pct(f, side, entry, plan[k])}) · "
                         + (f"закрыть {f.i(part * 100)}%" if ru else f"close {f.i(part * 100)}%"))
        lines.append(f"🛑 {'Стоп' if ru else 'Stop'}: {f.p(plan['stop'])} ({_pct(f, side, entry, plan['stop'])})")
    lvl = ""
    if exit_level:
        lvl = (f" (выход, если день закроется {'ниже' if long else 'выше'} {f.p(exit_level)})" if ru else
               f" (exit on a daily close {'below' if long else 'above'} {f.p(exit_level)})")
    lines.append(f"⏳ {'Остальные' if ru else 'Remaining'} {f.i(round(rest * 100))}%: "
                 + ("держим, пока жив тренд" if ru else "ride the trend") + lvl)
    lines += ["", (f"Размер: {f.n(size_pct, 1)}% депозита" if ru else f"Size: {f.n(size_pct, 1)}% of capital")]
    if stats and stats.get("win_pct"):
        lines.append(f"На истории: {f.i(stats['win_pct'])}% сделок в плюс, средняя прибыль {f.n(stats['avg_win'], 2, sign=True)}%, "
                     f"средний убыток {f.n(stats['avg_loss'], 2, sign=True)}% депозита" if ru else
                     f"Backtest: {f.i(stats['win_pct'])}% win, avg {f.n(stats['avg_win'], 2, sign=True)}% / "
                     f"{f.n(stats['avg_loss'], 2, sign=True)}% of capital")
    lines.append(_foot(mode, lang))
    return "\n".join(lines), f.numbers


LAYER_NAMES = {"ru": {"gas": "газ США", "fed": "ожидания ФРС", "liq": "ликвидность ФРС", "curve": "кривая доходности",
                       "ttf": "газ Европа к США", "prem": "премия Coinbase"},
               "en": {"gas": "US gas", "fed": "Fed expectations", "liq": "Fed liquidity", "curve": "yield curve",
                      "ttf": "EU vs US gas", "prem": "Coinbase premium"}}


def _layers_pos(f: Fmt, x: float, ru: bool, bold: bool = True) -> str:
    if abs(x) < 1e-9:
        return "КЭШ (вне рынка)" if ru else "CASH (flat)"
    side = ("ЛОНГ" if x > 0 else "ШОРТ") if ru else ("LONG" if x > 0 else "SHORT")
    v = f.n(abs(x) * 100, 0)
    return (f"<b>{side} {v}%</b>" if bold else f"{side} {v}%") + (" депозита" if ru else " of capital")


def btc_layers(st: dict, prev: dict | None, mode: str, lang: str = "ru", lev: int = 5) -> tuple[str, list[float]]:
    """BTC Layers: позиция по BTC на сутки. Пост только при смене направления или размера."""
    f, ru = Fmt(), lang == "ru"
    x = st["signal"]
    icon = "🟢" if x > 0 else "🔴" if x < 0 else "⚪️"
    lines = ["🧱 <b>BTC Layers</b>  #BTC", "Дневная модель по биткоину: макро решает сторону, тренд долю" if ru else
             "Daily BTC model: macro picks the side, trend the size", ""]
    lines.append(f"{icon} {'Позиция' if ru else 'Position'}: {_layers_pos(f, x, ru)}")
    if prev is not None:
        lines.append(f"{'Было' if ru else 'Was'}: {_layers_pos(f, prev['signal'], ru, bold=False)}")
    if abs(x) > 1e-9:
        lines.append((f"Пример: на 100$ депозита {'лонг' if x > 0 else 'шорт'} на {f.n(abs(x) * 100, 0)}$ по цене около {f.p(st['btc'])}"
                      if ru else f"Example: on a 100$ account, {'long' if x > 0 else 'short'} {f.n(abs(x) * 100, 0)}$ near {f.p(st['btc'])}"))
    if abs(x) > 1e-9:
        m = abs(x) * 100 / lev
        lines.append((f"Это объём позиции, не маржа: при плече x{f.i(lev)} из 100$ занято {f.n(m, 0)}$ маржи" if ru else
                      f"This is position size, not margin: at x{f.i(lev)} leverage it uses {f.n(m, 0)}$ of margin per 100$"))
    if st.get("side") is not None and st.get("vt"):
        sd = st["side"]
        why = (("макро и тренд вверх" if sd >= 1 else "макро вверх, тренд вниз: половина" if sd > 0 else
                "макро и тренд вниз" if sd < 0 else "тренд против макро: вне рынка") if ru else
               ("macro and trend up" if sd >= 1 else "macro up, trend down: half" if sd > 0 else
                "macro and trend down" if sd < 0 else "trend against macro: flat"))
        tv = st["vt"] * st["vol"] * 100
        lines.append((f"Как посчитано: доля {f.n(sd, 1, sign=True)} ({why}) × размер {f.n(st['vt'], 2)} "
                      f"(цель волатильности {f.n(tv, 0)}% / сейчас {f.n(st['vol'] * 100, 0)}%) = {f.n(x, 2, sign=True)}" if ru else
                      f"How: side {f.n(sd, 1, sign=True)} ({why}) × size {f.n(st['vt'], 2)} "
                      f"(target vol {f.n(tv, 0)}% / now {f.n(st['vol'] * 100, 0)}%) = {f.n(x, 2, sign=True)}"))
    lines.append("Без стопа и целей: позиция меняется только по следующему посту модели" if ru else
                 "No stop or targets: the position changes only with the next model post")
    reg = st["regime"] > 0
    lines += ["", (f"Макро {f.n(st['macro'], 2, sign=True)} → режим {'бычий' if reg else 'медвежий'}" if ru else
                   f"Macro {f.n(st['macro'], 2, sign=True)} → {'bull' if reg else 'bear'} regime")]
    parts = [f"{LAYER_NAMES[lang][k]} {f.n(v, 2, sign=True)}" for k, v in st["parts"].items() if v is not None]
    for i in range(0, len(parts), 3):
        lines.append("  " + " · ".join(parts[i:i + 3]))
    tr = st["trend"]
    lines.append((f"Тренд {f.n(tr, 2, sign=True)} ({'вверх' if tr > 0 else 'вниз' if tr < 0 else 'ровно'}) · "
                  f"волатильность {f.n(st['vol'] * 100, 0)}%" if ru else
                  f"Trend {f.n(tr, 2, sign=True)} · volatility {f.n(st['vol'] * 100, 0)}%"))
    bt = st.get("bt")
    if bt:
        lines.append((f"На истории с {bt['start'][:4]}: 100$ → {f.n(bt['final'], 0)}$, {f.n(bt['cagr'] * 100, 0, sign=True)}% в год, "
                      f"просадка до {f.n(bt['max_dd'] * 100, 0)}%" if ru else
                      f"Backtest since {bt['start'][:4]}: 100$ → {f.n(bt['final'], 0)}$, {f.n(bt['cagr'] * 100, 0, sign=True)}%/yr, "
                      f"max drawdown {f.n(bt['max_dd'] * 100, 0)}%"))
        f.numbers += [float(bt["start"][:4]), 100.0]
    f.numbers.append(100.0)
    lines.append(_foot(mode, lang))
    return "\n".join(lines), f.numbers


EV = {"ru": {"tp1": "✅ {c} {s}: TP1 {p}", "tp2": "🎯 {c} {s}: TP2 {p}", "sl": "❌ {c} {s}: стоп {p}"},
      "en": {"tp1": "✅ {c} {s}: TP1 {p}", "tp2": "🎯 {c} {s}: TP2 {p}", "sl": "❌ {c} {s}: stopped at {p}"}}


def trade_event(symbol: str, side: str, what: str, price: float, entry: float, part: float, rem: float,
                stop: float | None, mode: str, lang: str = "en") -> tuple[str, list[float]]:
    """Цель или стоп: что случилось, сколько закрыто, что дальше."""
    f, ru = Fmt(), lang == "ru"
    head = EV[lang][what].format(c=_coin(symbol), s=_side(side, lang), p=f.p(price))
    lines = [f"<b>{head}</b> ({_pct(f, side, entry, price)})"]
    if what == "sl" or rem <= 1e-9:
        lines.append("Сделка закрыта полностью" if ru else "Trade fully closed")
    else:
        lines.append((f"Закрыто {f.i(round(part * 100))}%, держим {f.i(round(rem * 100))}%" if ru else
                      f"Closed {f.i(round(part * 100))}%, holding {f.i(round(rem * 100))}%")
                     + (f". Стоп {f.p(stop)}" if ru and stop else f". Stop {f.p(stop)}" if stop else ""))
    return "\n".join(lines) + _tag(mode), f.numbers


def trade_closed(symbol: str, side: str, entry: float, exits: list[dict], result_pct: float, days: int, mode: str,
                 lang: str = "en", label: str = "") -> tuple[str, list[float]]:
    """Итог сделки одной цепочкой: вход → TP1 → TP2 → выход, и результат в % депозита."""
    f, ru = Fmt(), lang == "ru"
    names = {"tp1": "TP1 ✅", "tp2": "TP2 ✅", "sl": ("стоп ❌" if ru else "stop ❌"),
             "trend": ("тренд сломался" if ru else "trend ended"), "time": ("по времени" if ru else "time exit"),
             "delist": ("делистинг" if ru else "delisted")}
    chain = [f"{'Вход' if ru else 'Entry'} {f.p(entry)}"] + [f"{names.get(e['what'], e['what'])} {f.p(e['price'])}" for e in exits]
    win = result_pct > 0
    lines = [f"{'🏁' if win else '⚪️'} <b>{_coin(symbol)} {_side(side, lang)} {'закрыт' if ru else 'closed'}</b>" + (f" · {label}" if label else ""),
             " → ".join(chain),
             (f"Итог: <b>{f.n(result_pct, 2, sign=True)}%</b> депозита за {f.i(max(days, 1))} дн." if ru else
              f"Result: <b>{f.n(result_pct, 2, sign=True)}%</b> of capital in {f.i(max(days, 1))} days")]
    return "\n".join(lines) + _tag(mode), f.numbers


def trend_exit(symbol: str, side: str, entry: float, price: float, move_pct: float, result_pct: float,
               days: int, mode: str) -> tuple[str, list[float]]:
    return trade_closed(symbol, side, entry, [{"what": "trend", "price": price}], result_pct, days, mode)


def trend_open_list(rows: list[dict], mode: str, lang: str = "en") -> tuple[str, list[float]]:
    """Раз в неделю: открытые сигналы тренда, что уже взято и где стоп."""
    f, ru = Fmt(), lang == "ru"
    lines = ["🧭 <b>" + ("Открытые сигналы тренда" if ru else "Open trend signals") + "</b>", ""]
    for r in rows:
        hits = [x["what"].upper() for x in r.get("exits", []) if x["what"] in ("tp1", "tp2")]
        stop = r.get("stop")
        lines.append(f"{'🟢' if r['side'] == 'long' else '🔴'} {_coin(r['symbol'])} {_side(r['side'], lang)} "
                     f"{'от' if ru else 'from'} {f.p(r['entry'])}"
                     + (f" · {' '.join(hits)} ✅" if hits else "")
                     + (f" · {'стоп' if ru else 'stop'} {f.p(stop)}" if stop else "")
                     + (f" · {'тренд до' if ru else 'trend exit'} {f.p(r['exit_level'])}" if r.get("exit_level") else ""))
    if not rows:
        lines.append("Открытых нет" if ru else "No open trend signals")
    lines += ["", _foot(mode, lang)]
    return "\n".join(lines), f.numbers


def listing_signal(symbol: str, entry: float, stop: float, lc: dict, mode: str, lang: str = "en",
                   exchange: str = "binance") -> tuple[str, list[float]]:
    f, ru = Fmt(), lang == "ru"
    exn = "Bybit" if exchange == "bybit" else "Binance"
    lines = [f"🔴 <b>{_coin(symbol)} · {_side('short', lang)}</b>  #{_coin(symbol)}",
             (f"Новый листинг на {exn}" if ru else f"New listing on {exn}"), "",
             f"{'Вход' if ru else 'Entry'}: <b>{f.p(entry)}</b>",
             f"🛑 {'Стоп' if ru else 'Stop'}: {f.p(stop)} ({_pct(f, 'short', entry, stop)})",
             f"⏳ {'Выход' if ru else 'Exit'}: " + (f"через {f.i(lc['hold_days'])} дней, целей нет" if ru else
                                                    f"after {f.i(lc['hold_days'])} days, no targets"), "",
             (f"Размер: {f.n(lc['notional_pct'], 1)}% депозита, без плеча" if ru else
              f"Size: {f.n(lc['notional_pct'], 1)}% of capital, no leverage"),
             ("Новые монеты обычно дешевеют первые недели из-за разлоков. Редкие улетают вверх, поэтому стоп широкий, а размер маленький"
              if ru else "New listings usually drift lower in their first weeks as supply unlocks. A few explode, so the stop is wide and the size small"),
             _foot(mode, lang)]
    return "\n".join(lines), f.numbers


def listing_exit(symbol: str, entry: float, price: float, reason: str, pnl_pct: float, mode: str,
                 lang: str = "en", days: int = 0) -> tuple[str, list[float]]:
    return trade_closed(symbol, "short", entry, [{"what": "sl" if reason == "stop" else reason, "price": price}],
                        pnl_pct, days, mode, lang)
