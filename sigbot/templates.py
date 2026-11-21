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


def signal_ru(sig: dict, lang: str = "ru") -> tuple[str, list[float]]:
    """Капитуляция: вход, две цели, стоп. Без индикаторов."""
    f, side, e, L, ru = Fmt(), sig["side"], sig["entry"], LBL[lang], lang == "ru"
    lines = [_hdr(sig["symbol"], side, lang), "⚡ " + ("Отскок после слива" if ru else "Rebound after a flush"), "",
             f"▶️ {L['entry']}: <b>{f.p(e)}</b>",
             f"🎯 {L['tp']} 1: {f.p(sig['tp1'])} ({_pct(f, side, e, sig['tp1'])}) · {L['close']} {f.i(50)}%",
             f"🎯 {L['tp']} 2: {f.p(sig['tp2'])} ({_pct(f, side, e, sig['tp2'])})",
             f"🛑 {L['stop']}: {f.p(sig['stop'])} ({_pct(f, side, e, sig['stop'])})",
             "После цели 1 стоп в безубыток" if ru else "After target 1 the stop moves to breakeven"]
    return _lines_end(lines, sig.get("mode", ""), f), f.numbers


def signal(sig: dict, comment: str = "", zones: list[dict] | None = None, lang: str = "en") -> tuple[str, list[float]]:
    return signal_ru(sig, "ru" if lang == "ru" else "en")


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


EVENT_RU = {"breakeven": "стоп в безубыток", "tp1": "цель 1 взята, закрыли 50%", "tp2": "цель 2 взята",
            "stop": "стоп", "breakeven_stop": "стоп в безубытке", "trail_stop": "трейлинг-стоп",
            "invalidation": "идея сломалась", "timeout": "по времени", "manual": "вручную"}
EVENT_FINAL = {"tp2", "stop", "breakeven_stop", "trail_stop", "invalidation", "timeout", "manual"}


def update(sig: dict, event: str, price: float, r_total: float | None = None, lang: str = "en",
           pnl_pct: float | None = None) -> tuple[str, list[float]]:
    if lang == "ru":
        f, sd, e = Fmt(), sig["side"], sig["entry"]
        icon = {"tp1": "✅", "tp2": "🎯", "stop": "❌", "breakeven": "🛡", "breakeven_stop": "➖"}.get(event, "🏁")
        if event in EVENT_FINAL:
            lines = [f"{icon} <b>Выход #{_coin(sig['symbol'])} · {_side(sd, 'ru')}: {f.p(price)}</b>",
                     f"{EVENT_RU.get(event, event).capitalize()} · вход {f.p(e)} → {f.p(price)} ({_pct(f, sd, e, price)})"]
            if pnl_pct is not None:
                lines.append(f"Итог: <b>{f.n(pnl_pct, 2, sign=True)}%</b> депозита")
        elif event == "breakeven":
            lines = [f"🛡 <b>#{_coin(sig['symbol'])} · {_side(sd, 'ru')}: стоп переносим в безубыток {f.p(e)}</b>"]
        else:
            lines = [f"{icon} <b>#{_coin(sig['symbol'])} · {_side(sd, 'ru')}: {EVENT_RU.get(event, event)}</b>",
                     f"Цена {f.p(price)} ({_pct(f, sd, e, price)}). Остаток держим, стоп в безубыток {f.p(e)}"]
        f.numbers += [1.0, 2.0, 50.0]
        return "\n".join(lines) + _tag(sig.get("mode", "")), f.numbers
    f, sd, e = Fmt(), sig["side"], sig["entry"]
    name = {"breakeven": "stop to breakeven", "tp1": "target 1 hit, closed 50%", "tp2": "target 2 hit", "stop": "stop",
            "breakeven_stop": "stopped at breakeven", "trail_stop": "trailing stop", "invalidation": "setup invalidated",
            "timeout": "time exit", "manual": "manual"}.get(event, event)
    icon = {"tp1": "✅", "tp2": "🎯", "stop": "❌", "breakeven": "🛡", "breakeven_stop": "➖"}.get(event, "🏁")
    if event in EVENT_FINAL:
        lines = [f"{icon} <b>Exit #{_coin(sig['symbol'])} · {_side(sd, 'en')}: {f.p(price)}</b>",
                 f"{name.capitalize()} · entry {f.p(e)} → {f.p(price)} ({_pct(f, sd, e, price)})"]
        if pnl_pct is not None:
            lines.append(f"Result: <b>{f.n(pnl_pct, 2, sign=True)}%</b> of capital")
    elif event == "breakeven":
        lines = [f"🛡 <b>#{_coin(sig['symbol'])} · {_side(sd, 'en')}: stop moved to breakeven {f.p(e)}</b>"]
    else:
        lines = [f"{icon} <b>#{_coin(sig['symbol'])} · {_side(sd, 'en')}: {name}</b>",
                 f"Price {f.p(price)} ({_pct(f, sd, e, price)}). Holding the rest, stop at breakeven {f.p(e)}"]
    f.numbers += [1.0, 2.0, 50.0]
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


LBL = {"ru": dict(entry="Вход", tp="Цель", stop="Стоп", exit="Выход", rest="Остаток", close="закрыть", trade="Сделка",
                  result="Итог", dep="депозита", days="дн.", closed="закрыта", hold="держим"),
       "en": dict(entry="Entry", tp="Target", stop="Stop", exit="Exit", rest="Rest", close="close", trade="Trade",
                  result="Result", dep="of capital", days="d", closed="closed", hold="holding")}


def _hdr(symbol: str, side: str, lang: str, icon: str | None = None) -> str:
    """Заголовок сигнала: 🔴 #BTC · ШОРТ."""
    return f"{icon or ('🟢' if side == 'long' else '🔴')} <b>#{_coin(symbol)} · {_side(side, lang)}</b>"


def _lines_end(lines: list[str], mode: str, f: Fmt | None = None) -> str:
    if f is not None:
        f.numbers += [1.0, 2.0]                     # номера целей «Цель 1», «Цель 2» в тексте
    return "\n".join(lines) + _tag(mode)


def trend_signal(symbol: str, side: str, entry: float, size_pct: float, exit_level: float | None, mode: str,
                 stats: dict | None = None, plan: dict | None = None, lang: str = "en") -> tuple[str, list[float]]:
    """Тренд: вход, две цели (по четверти), стоп, остаток до слома тренда."""
    f, L, long = Fmt(), LBL[lang], side == "long"
    lines = [_hdr(symbol, side, lang), "🧭 " + ("Тренд" if lang == "ru" else "Trend"), "",
             f"▶️ {L['entry']}: <b>{f.p(entry)}</b>"]
    rest = 1.0
    if plan:
        for i, k in enumerate(("tp1", "tp2"), 1):
            rest -= plan[f"{k}_part"]
            lines.append(f"🎯 {L['tp']} {f.i(i)}: {f.p(plan[k])} ({_pct(f, side, entry, plan[k])}) · {L['close']} "
                         f"{f.i(plan[k + '_part'] * 100)}%")
        lines.append(f"🛑 {L['stop']}: {f.p(plan['stop'])} ({_pct(f, side, entry, plan['stop'])})")
    if exit_level:
        lines.append((f"🏁 {L['rest']} {f.i(round(rest * 100))}%: выход, если день закроется {'ниже' if long else 'выше'} "
                      f"{f.p(exit_level)}") if lang == "ru" else
                     (f"🏁 {L['rest']} {f.i(round(rest * 100))}%: exit on a daily close {'below' if long else 'above'} "
                      f"{f.p(exit_level)}"))
    return _lines_end(lines, mode, f), f.numbers


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
    """BTC Layers: вход, смена размера или выход. Без стопа и целей: позицию меняет только следующий пост."""
    f, ru = Fmt(), lang == "ru"
    x, px = st["signal"], st["btc"]
    side = "long" if x > 0 else "short"
    p = prev["signal"] if prev else 0.0
    same = prev is not None and abs(p) > 1e-9 and abs(x) > 1e-9 and (p > 0) == (x > 0)
    tag = "🧱 BTC Layers"
    if abs(x) < 1e-9:                                             # модель ушла в кэш: выход
        ps = "long" if p > 0 else "short"
        lines = [f"🏁 <b>{'Выход' if ru else 'Exit'} #BTC · {_side(ps, lang)}: {f.p(px)}</b>", tag,
                 "Позиции нет до следующего сигнала" if ru else "Flat until the next signal"]
        return _lines_end(lines, mode, f), f.numbers
    if same:                                                      # та же сторона, другой размер
        lines = [f"↔️ <b>#BTC · {_side(side, lang)}: {'размер' if ru else 'size'} {f.n(abs(p) * 100, 0)}% → "
                 f"{f.n(abs(x) * 100, 0)}% {'депозита' if ru else 'of capital'}</b>", tag,
                 (f"Цена {f.p(px)}" if ru else f"Price {f.p(px)}")]
        return _lines_end(lines, mode, f), f.numbers
    lines = []
    if prev is not None and abs(p) > 1e-9:                        # разворот: сначала выход из прошлой
        lines += [f"🏁 <b>{'Выход' if ru else 'Exit'} #BTC · {_side('long' if p > 0 else 'short', lang)}: {f.p(px)}</b>", ""]
    lines += [_hdr("BTCUSDT", side, lang), tag, "",
              f"▶️ {'Вход' if ru else 'Entry'}: <b>{f.p(px)}</b>",
              (f"📦 Размер: {f.n(abs(x) * 100, 0)}% депозита (при x{f.i(lev)} это {f.n(abs(x) * 100 / lev, 0)}% маржи)" if ru else
               f"📦 Size: {f.n(abs(x) * 100, 0)}% of capital (at x{f.i(lev)}: {f.n(abs(x) * 100 / lev, 0)}% margin)"),
              ("🏁 Выход: следующим постом, когда модель сменит позицию. Стопа нет" if ru else
               "🏁 Exit: in the next post, when the model changes. No stop")]
    return _lines_end(lines, mode, f), f.numbers


def btc_layers_coins(st: dict, prev: dict | None, mode: str, lang: str = "ru") -> tuple[str, list[float]]:
    """BTC Layers, портфель монет (v6): строка на каждую монету, у которой сменилась сторона или размер."""
    f, ru = Fmt(), lang == "ru"
    cur, old, px = st.get("coins") or {}, (prev or {}).get("coins") or {}, st.get("coin_px") or {}
    total = sum(abs(v) for v in cur.values())
    lines = [f"🧱 <b>BTC Layers · {'портфель монет' if ru else 'coin portfolio'}</b>",
             (f"Всего в позициях {f.n(total * 100, 0)}% депозита" if ru else f"Total exposure {f.n(total * 100, 0)}% of capital"), ""]
    dep = "депозита" if ru else "of capital"
    for c in sorted(set(cur) | set(old), key=lambda c: -abs(cur.get(c, 0))):
        x, p = cur.get(c, 0.0), old.get(c, 0.0)
        price = f" · {f.p(px[c])}" if c in px else ""
        if abs(x) < 1e-9:
            lines.append(f"🏁 #{c} · {_side('long' if p > 0 else 'short', lang)}: {'выход' if ru else 'exit'}{price}")
        elif abs(p) < 1e-9 or (p > 0) != (x > 0):
            lines.append(f"{'🟢' if x > 0 else '🔴'} #{c} · {_side('long' if x > 0 else 'short', lang)} "
                         f"{f.n(abs(x) * 100, 0)}% {dep}{price}")
        elif abs(x - p) >= 0.005:
            lines.append(f"↔️ #{c} · {_side('long' if x > 0 else 'short', lang)} {f.n(abs(p) * 100, 0)}% → "
                         f"{f.n(abs(x) * 100, 0)}% {dep}{price}")
        else:
            lines.append(f"▪️ #{c} · {_side('long' if x > 0 else 'short', lang)} {f.n(abs(x) * 100, 0)}% {dep}")
    lines += ["", ("Стопов нет: позиции меняет только следующий пост" if ru else "No stops: only the next post changes positions")]
    return _lines_end(lines, mode, f), f.numbers


EV = {"ru": {"tp1": "✅ {c} {s}: TP1 {p}", "tp2": "🎯 {c} {s}: TP2 {p}", "sl": "❌ {c} {s}: стоп {p}"},
      "en": {"tp1": "✅ {c} {s}: TP1 {p}", "tp2": "🎯 {c} {s}: TP2 {p}", "sl": "❌ {c} {s}: stopped at {p}"}}


def trade_event(symbol: str, side: str, what: str, price: float, entry: float, part: float, rem: float,
                stop: float | None, mode: str, lang: str = "en", late: bool = False) -> tuple[str, list[float]]:
    """Цель или стоп одной-двумя строками. late: уровень был пройден раньше, пост с опозданием."""
    f, ru = Fmt(), lang == "ru"
    if late:
        text, nums = trade_event(symbol, side, what, price, entry, part, rem, stop, mode, lang)
        tag, note = _tag(mode), ("⏱ Уровень был пройден раньше, пост с опозданием" if ru else "⏱ Level was hit earlier, late post")
        body = text[:len(text) - len(tag)] if tag and text.endswith(tag) else text
        return body + "\n" + note + (tag if tag and text.endswith(tag) else ""), nums
    if what == "sl" or rem <= 1e-9:
        icon = "❌" if what == "sl" else "🎯"
        name = ("стоп" if ru else "stop") if what == "sl" else (f"цель {what[-1]}" if ru else f"target {what[-1]}")
        lines = [f"{icon} <b>{'Выход' if ru else 'Exit'} #{_coin(symbol)} · {_side(side, lang)}: {f.p(price)}</b>",
                 f"{name.capitalize()} · {'вход' if ru else 'entry'} {f.p(entry)} → {f.p(price)} ({_pct(f, side, entry, price)})"]
        return _lines_end(lines, mode, f), f.numbers
    lines = [f"✅ <b>#{_coin(symbol)} · {_side(side, lang)}: {'цель' if ru else 'target'} {what[-1]} {f.p(price)}</b> "
             f"({_pct(f, side, entry, price)})",
             (f"Закрыли {f.i(round(part * 100))}%, держим {f.i(round(rem * 100))}%" if ru else
              f"Closed {f.i(round(part * 100))}%, holding {f.i(round(rem * 100))}%")
             + ((f". Стоп {f.p(stop)}" if ru else f". Stop {f.p(stop)}") if stop else "")]
    return _lines_end(lines, mode, f), f.numbers


def trade_closed(symbol: str, side: str, entry: float, exits: list[dict], result_pct: float, days: int, mode: str,
                 lang: str = "en", label: str = "") -> tuple[str, list[float]]:
    """Выход: цена выхода в заголовке, путь сделки одной строкой, итог в % депозита."""
    f, ru = Fmt(), lang == "ru"
    names = {"tp1": ("цель 1 ✅" if ru else "target 1 ✅"), "tp2": ("цель 2 ✅" if ru else "target 2 ✅"),
             "sl": ("стоп ❌" if ru else "stop ❌"), "trend": ("слом тренда" if ru else "trend ended"),
             "time": ("по времени" if ru else "time exit"), "delist": ("делистинг" if ru else "delisted")}
    last = exits[-1]["price"] if exits else entry
    chain = [f"{'вход' if ru else 'entry'} {f.p(entry)}"] + [f"{names.get(e['what'], e['what'])} {f.p(e['price'])}" for e in exits]
    lines = [f"{'🏁' if result_pct > 0 else '⚪️'} <b>{'Выход' if ru else 'Exit'} #{_coin(symbol)} · {_side(side, lang)}: {f.p(last)}</b>"
             + (f" · {label}" if label else ""),
             " → ".join(chain).capitalize(),
             (f"Итог: <b>{f.n(result_pct, 2, sign=True)}%</b> депозита за {f.i(max(days, 1))} дн." if ru else
              f"Result: <b>{f.n(result_pct, 2, sign=True)}%</b> of capital in {f.i(max(days, 1))} days")]
    return _lines_end(lines, mode, f), f.numbers


def trend_exit(symbol: str, side: str, entry: float, price: float, move_pct: float, result_pct: float,
               days: int, mode: str) -> tuple[str, list[float]]:
    return trade_closed(symbol, side, entry, [{"what": "trend", "price": price}], result_pct, days, mode)


def trend_open_list(rows: list[dict], mode: str, lang: str = "en") -> tuple[str, list[float]]:
    """Раз в неделю: открытые сигналы тренда одной строкой каждый."""
    f, ru = Fmt(), lang == "ru"
    lines = ["🧭 <b>" + ("Открытые сделки" if ru else "Open trades") + "</b>", ""]
    for r in rows:
        hits = [x["what"][-1] for x in r.get("exits", []) if x["what"] in ("tp1", "tp2")]
        stop = r.get("stop")
        lines.append(f"{'🟢' if r['side'] == 'long' else '🔴'} #{_coin(r['symbol'])} {_side(r['side'], lang)} "
                     f"{'от' if ru else 'from'} {f.p(r['entry'])}"
                     + (f" · {'цель' if ru else 'target'} {', '.join(hits)} ✅" if hits else "")
                     + (f" · {'стоп' if ru else 'stop'} {f.p(stop)}" if stop else ""))
    if not rows:
        lines.append("Открытых нет" if ru else "No open trades")
    return _lines_end(lines, mode, f), f.numbers


def listing_signal(symbol: str, entry: float, stop: float, lc: dict, mode: str, lang: str = "en",
                   exchange: str = "binance") -> tuple[str, list[float]]:
    f, ru = Fmt(), lang == "ru"
    exn = "Bybit" if exchange == "bybit" else "Binance"
    lines = [_hdr(symbol, "short", lang), (f"🆕 Новый листинг {exn}" if ru else f"🆕 New listing {exn}"), "",
             f"▶️ {'Вход' if ru else 'Entry'}: <b>{f.p(entry)}</b>",
             f"🛑 {'Стоп' if ru else 'Stop'}: {f.p(stop)} ({_pct(f, 'short', entry, stop)})",
             f"🏁 {'Выход' if ru else 'Exit'}: " + (f"через {f.i(lc['hold_days'])} дней" if ru else f"after {f.i(lc['hold_days'])} days")]
    return _lines_end(lines, mode, f), f.numbers


def listing_exit(symbol: str, entry: float, price: float, reason: str, pnl_pct: float, mode: str,
                 lang: str = "en", days: int = 0) -> tuple[str, list[float]]:
    return trade_closed(symbol, "short", entry, [{"what": "sl" if reason == "stop" else reason, "price": price}],
                        pnl_pct, days, mode, lang)
