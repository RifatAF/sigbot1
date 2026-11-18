"""Самопроверка и тестовые посты: понять, что каждая часть бота работает, не дожидаясь настоящего сигнала.

`python -m sigbot check`     все проверки с понятным ответом ✅ / ⚠️ / ❌
`python -m sigbot testpost`  все виды постов с настоящими данными и картинкой в канал (`--me`: только в личку)
Те же действия есть кнопками в /menu."""
from __future__ import annotations

import os
import time

import numpy as np

from . import chart, features, portfolio, settings, templates

DAY = 86_400_000
HOUR = 3_600_000
OK, WARN, FAIL = "✅", "⚠️", "❌"


async def selfcheck(cfg, conn, http) -> list[tuple[str, str, str]]:
    from .sources import Binance
    from .telegram import Telegram
    out: list[tuple[str, str, str]] = []
    add = lambda st, name, detail="": out.append((st, name, detail))
    now = int(time.time() * 1000)

    # 1. ключи и Telegram
    token, channel, owner = (os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID", "TELEGRAM_OWNER_ID"))
    if not token:
        add(FAIL, "Токен Telegram", "нет в .env: python -m sigbot setup")
    else:
        tg = Telegram(token, http)
        try:
            me = await tg.call("getMe")
            add(OK, "Бот Telegram", "@" + me.get("username", "?"))
            if channel:
                try:
                    m = await tg.call("getChatMember", chat_id=channel, user_id=me["id"])
                    can = m.get("status") == "administrator" and m.get("can_post_messages", True)
                    add(OK if can else FAIL, "Права в канале", m.get("status", "?") + ("" if can else ": нужен админ с правом публикации"))
                except Exception as e:
                    add(FAIL, "Канал", f"бот не видит канал {channel}: {str(e)[:80]}")
            else:
                add(FAIL, "ID канала", "нет в .env")
            if owner:
                try:
                    await tg.call("getChat", chat_id=owner)
                    add(OK, "Личка владельца", "бот может писать")
                except Exception:
                    add(FAIL, "Личка владельца", "напиши боту /start в Telegram")
            else:
                add(FAIL, "ID владельца", "нет в .env")
        except Exception as e:
            add(FAIL, "Бот Telegram", f"токен не принят: {str(e)[:80]}")

    # 2. Gemini
    if os.environ.get("GEMINI_API_KEY"):
        from .llm import Gemini
        lc = cfg["llm"]
        g = Gemini(os.environ["GEMINI_API_KEY"], http, lc["model"], lc["fallback_model"], 30)
        txt = await g.comment({"coin": "BTC", "side": "long", "note": "self check"})
        add(OK if txt else WARN, "Gemini", "отвечает" if txt else "не ответил: посты уйдут без комментария")
    else:
        add(WARN, "Gemini", "ключа нет: посты без комментария")

    # 3. биржа
    bn = Binance(http)
    try:
        k = await bn.klines("BTCUSDT", "futures", "1h", None, 2)
        add(OK, "Binance API", f"BTC {k[-1][4]:,.0f}")
    except Exception as e:
        add(FAIL, "Binance API", f"недоступен: {str(e)[:80]} (из облака и некоторых стран закрыт)")
    try:
        n = len(await bn.perp_listings())
        add(OK, "Список фьючерсов", f"{n} USDT перпетуалов (для шорта листингов)")
    except Exception as e:
        add(FAIL, "Список фьючерсов", str(e)[:80])

    # 4. данные в базе
    syms = settings.get(conn, "symbols_manual") or settings.get(conn, "universe", [])
    add(OK if syms else FAIL, "Список монет", f"{len(syms)} монет" if syms else "пусто: python -m sigbot backfill")
    stale = []
    for s in syms:
        r = conn.execute("SELECT MAX(ts) FROM candles WHERE symbol=? AND market='futures' AND tf='1h'", (s,)).fetchone()[0]
        if not r or now - r > 3 * HOUR:
            stale.append(s.replace("USDT", ""))
    add(OK if not stale else WARN if len(stale) < 5 else FAIL, "Свежесть часовых свечей",
        "все не старше 3 часов" if not stale else f"старые или нет: {', '.join(stale[:8])}" + (" (бот запущен?)" if len(stale) >= 5 else ""))
    days = conn.execute("SELECT (MAX(ts)-MIN(ts))/86400000 FROM candles WHERE symbol='BTCUSDT' AND tf='1h'").fetchone()[0] or 0
    need = max(cfg["portfolio"]["donchian"]) + 30
    add(OK if days >= need else FAIL, "История для портфеля", f"{int(days)} дней, нужно {need}")
    fr = conn.execute("SELECT MAX(ts) FROM funding").fetchone()[0]
    add(OK if fr and now - fr < 2 * HOUR else WARN, "Фандинг", "свежий" if fr and now - fr < 2 * HOUR else "старый: сборщик не работал")

    # 5. расчёты
    try:
        C, V, FR = portfolio.daily_frames(conn, syms, now - 400 * DAY, now)
        W = portfolio.weights(C, V, cfg["portfolio"], universe=syms, FR=FR)
        last = W.iloc[-1]
        n = int((last.abs() > 0.001).sum())
        add(OK, "Трендовый портфель", f"{n} позиций, плечо {last.abs().sum():.2f}")
    except Exception as e:
        add(FAIL, "Трендовый портфель", str(e)[:100])
    try:
        from . import engine
        df = features.build(conn, "BTCUSDT", now - 400 * HOUR, now).reset_index(drop=True)
        engine.prepare(df, cfg)
        add(OK, "Часовой движок", f"BTC {len(df)} баров посчитано")
    except Exception as e:
        add(FAIL, "Часовой движок", str(e)[:100])
    try:
        png = await chart.render_png(chart.build_html("BTCUSDT", "long", [{"time": 1, "open": 1, "high": 2, "low": 0.5, "close": 1.5},
                                                                          {"time": 3601, "open": 1.5, "high": 2, "low": 1, "close": 1.8}],
                                                      {"entry": 1.8, "stop": 1.5, "tp1": 2.0, "tp2": 2.4}))
        add(OK, "Картинки (Chromium)", f"{len(png) // 1024} КБ")
    except Exception as e:
        add(FAIL, "Картинки (Chromium)", f"{str(e)[:80]}: .venv\\Scripts\\python -m playwright install chromium")

    # 6. режим
    p = settings.effective(cfg, conn)["publishing"]
    st = ("аварийная остановка" if p.get("kill_switch") else "пауза" if p.get("paused") else
          {"paper": "тестовый режим", "confirm": "с подтверждением", "auto": "боевой режим"}.get(p["mode"], p["mode"]))
    add(WARN if p.get("kill_switch") or p.get("paused") else OK, "Публикация", st)
    lp = conn.execute("SELECT ts, kind, status FROM posts ORDER BY id DESC LIMIT 1").fetchone()
    if lp:
        add(OK, "Последний пост", f"{time.strftime('%d.%m %H:%M', time.gmtime(lp[0] / 1000))} UTC, {lp[1]}, {lp[2]}")
    return out


def format_check(rows) -> str:
    bad = sum(1 for r in rows if r[0] == FAIL)
    head = "🩺 Самопроверка: всё в порядке" if not bad else f"🩺 Самопроверка: проблем {bad}"
    return head + "\n\n" + "\n".join(f"{st} {name}: {d}" if d else f"{st} {name}" for st, name, d in rows)


class _LoggedTG:
    """Обёртка: каждый тестовый пост пишется в таблицу posts со статусом test, чтобы его было видно на панели."""

    def __init__(self, tg, conn):
        self.tg, self.conn = tg, conn

    def _log(self, text, mid):
        self.conn.execute("INSERT INTO posts (ts, kind, signal_id, status, text, photo, message_id, detail) "
                          "VALUES (?,?,?,?,?,?,?,?)", (int(time.time() * 1000), "test", None, "test", text, None, mid, None))

    async def send_message(self, chat, text, **kw):
        r = await self.tg.send_message(chat, text, **kw)
        self._log(text, (r or {}).get("message_id"))
        return r

    async def send_photo(self, chat, png, caption, **kw):
        r = await self.tg.send_photo(chat, png, caption, **kw)
        self._log(caption, (r or {}).get("message_id"))
        return r


async def test_posts(cfg, conn, tg, chat_id) -> int:
    """Все виды постов на настоящих данных: сигнал с картинкой, обновление, портфель, листинг, заметка."""
    tg = _LoggedTG(tg, conn)
    from .bloggers import Bloggers
    mode = "paper"
    sent = 0
    T = "🧪 <b>TEST post</b>, not a real signal\n\n"
    now = int(time.time() * 1000)
    sym = "BTCUSDT"
    df = features.build(conn, sym, now - 200 * HOUR, now).reset_index(drop=True)
    if len(df) < 30:
        await tg.send_message(chat_id, "🧪 Мало данных для теста: сначала python -m sigbot backfill", parse_mode=None)
        return 0
    entry = float(df.close.iloc[-1])
    atr = float((df.high - df.low).tail(24).mean())
    lv = {"entry": entry, "stop": entry - 2 * atr, "tp1": entry + 2.4 * atr, "tp2": entry + 5 * atr}
    sig = {"symbol": sym, "side": "long", **lv, "risk_pct": 1.5, "leverage": 3.2, "strength": 0.72, "ts": now,
           "mode": mode, "features": {"setup": "capitulation", "drop_sigma": 3.1, "divergences": ["regular bullish cvd_spot"],
                                      "trend_4h": 1, "fear_greed": 24}}
    zones = Bloggers(cfg, conn).zones_for(sym, entry)
    text, _ = templates.signal(sig, "Spot buyers absorbed the drop while futures sold.", zones)
    html = chart.build_html(sym, "long", chart.candles_from_df(df, 96), lv, zones, None, subtitle="Strength 0.72 · TEST",
                            risk_pct=1.5, leverage=3.2)
    await tg.send_photo(chat_id, await chart.render_png(html), T + text)
    sent += 1
    for ev, px, tot in (("tp1", lv["tp1"], None), ("breakeven", entry, None), ("tp2", lv["tp2"], 1.85)):
        text, _ = templates.update(sig, ev, px, tot)
        await tg.send_message(chat_id, T + text)
        sent += 1
    syms = settings.get(conn, "universe", [])
    C, V, FR = portfolio.daily_frames(conn, syms, now - 400 * DAY, now)
    if not C.empty:
        W = portfolio.weights(C, V, cfg["portfolio"], universe=syms, FR=FR).iloc[-1]
        w = {s: float(x) for s, x in W.items() if abs(x) >= 0.001}
        from . import feed
        big = sorted(w.items(), key=lambda x: -abs(x[1]))[:1] or [("BTCUSDT", 0.12)]
        sym2, w2 = big[0]
        side = "long" if w2 > 0 else "short"
        lv = feed.exit_level(C[sym2].dropna().values, 1 if w2 > 0 else -1, cfg["portfolio"]) if sym2 in C else None
        px = float(C[sym2].dropna().iloc[-1]) if sym2 in C else entry
        text, _ = templates.trend_signal(sym2, side, px, abs(w2) * 100, lv, mode,
                                         {"win_pct": 38, "avg_win": 2.01, "avg_loss": -0.68})
        await tg.send_message(chat_id, T + text)
        text, _ = templates.trend_exit(sym2, side, px, px * 1.08, 8.0, abs(w2) * 8, 9, mode)
        await tg.send_message(chat_id, T + text)
        sent += 2
    lc = cfg["listings"]
    text, _ = templates.listing_signal("NEWCOINUSDT", 0.4321, 0.4321 * (1 + lc["stop_pct"] / 100), lc, mode)
    await tg.send_message(chat_id, T + text)
    text, _ = templates.listing_exit("NEWCOINUSDT", 0.4321, 0.3105, "time", 1.27, mode)
    await tg.send_message(chat_id, T + text)
    sent += 2
    m = {"btc_price": entry, "btc_change_24h": float((df.close.iloc[-1] / df.close.iloc[-25] - 1) * 100),
         "open_trades": 0, "signals_today": 0, "fear_greed": None, "avg_funding_pct": None, "coinbase_premium_pct": None}
    text, _ = templates.daily_note(m, "")
    await tg.send_message(chat_id, T + text)
    return sent + 1
