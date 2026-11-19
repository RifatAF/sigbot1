"""Админка в личном чате с ботом. Отвечает только владельцу (TELEGRAM_OWNER_ID)."""
from __future__ import annotations

import asyncio
import logging
import time
from html import escape

from . import ledger, postmortem, settings
from .bloggers import Bloggers
from .telegram import Telegram, kb

log = logging.getLogger(__name__)
DAY = 86_400_000


def _round(x: float) -> float:
    return round(x + 1e-9, 2)


class Admin:
    def __init__(self, cfg, conn, tg: Telegram, owner_id: int, publisher, live, llm, bloggers: Bloggers,
                 collector=None, leader=None):
        self.leader = leader
        self.cfg, self.conn, self.tg, self.owner = cfg, conn, tg, int(owner_id)
        self.pub, self.live, self.llm, self.bl, self.collector = publisher, live, llm, bloggers, collector

    def eff(self):
        return settings.effective(self.cfg, self.conn)

    # ---------- экраны ----------
    BACK = [("⬅️ В меню", "menu")]
    MODES = {"paper": ("🧪 Тестовый", "посты сразу уходят в канал с пометкой #forwardtest, деньги не участвуют"),
             "confirm": ("✋ С подтверждением", "каждый пост сначала приходит тебе, в канал уходит после кнопки «Опубликовать»"),
             "auto": ("🚀 Боевой", "посты уходят в канал сразу, без пометки и без подтверждения")}
    NAMES = {"universe": "список монет", "klines_futures": "свечи фьючерсов", "klines_spot": "свечи спота",
             "oi_funding": "открытый интерес и фандинг", "liquidations": "ликвидации", "premium": "премия Coinbase",
             "fear_greed": "индекс страха и жадности"}

    def _state(self, p) -> str:
        return ("⛔ ОСТАНОВЛЕН (аварийная кнопка)" if p.get("kill_switch") else
                "⏸ на паузе, посты не уходят" if p.get("paused") else "▶️ работает")

    def menu(self):
        e = self.eff()
        p = e["publishing"]
        on = lambda x: "вкл" if x else "выкл"
        role = ""
        if self.leader:
            role = f"\nКомпьютер: <b>{self.leader.name}</b> ({'основной' if self.leader.is_main else 'запасной'})"
        n_tr = self.conn.execute("SELECT COUNT(*) FROM trend_signals WHERE status='open'").fetchone()[0]
        n_ls = self.conn.execute("SELECT COUNT(*) FROM listing_trades WHERE status='open'").fetchone()[0]
        n_h = len(self.live.open_trades()) if self.live else 0
        text = (f"🤖 <b>sigbot</b> · {self._state(p)}{role}\n"
                f"Режим публикации: <b>{self.MODES.get(p['mode'], (p['mode'],))[0]}</b>\n"
                f"Открыто сейчас: тренд {n_tr}, листинги {n_ls}, капитуляции {n_h}\n"
                f"Стратегии: 🧭 тренд {on(e['portfolio'].get('enabled'))} · 🆕 листинги {on(e['listings'].get('enabled'))}"
                f" · ⚡ капитуляции {on(e['scanner'].get('enabled', True))}\n\n"
                "Выбери раздел. Непонятно, что делает кнопка: «❓ Помощь».")
        rows = [
            [("📊 Что открыто", "status"), ("🩺 Состояние", "health")],
            [("✅ Проверить всё", "selfcheck")],
            [("🧪 Тест в канал", "testposts:ch"), ("🧪 Тест мне", "testposts:me")],
            [("📣 Режим публикации", "mode"), ("▶️ Продолжить" if p.get("paused") else "⏸ Пауза", "pause")],
            [("🧭 Стратегии", "strat"), ("⚖️ Риск и плечо", "risk")],
            [("🪙 Монеты", "coins"), ("🕒 Расписание", "sched")],
            [("🧾 Итоги недели", "pm"), ("📤 Сделки в Excel", "export")],
            [("🗣 Блогеры", "blog"), ("📌 Пост о методе", "method")],
            [("📈 Сделки на графике", "snaps"), ("📱 Панель на телефоне", "phone")],
            [("🧪 Бумажная торговля (100$)", "demo"), ("🧭 BTC Layers", "layers")],
            [("💻 Компьютеры", "pcs"), ("❓ Помощь", "help")],
            [("✅ Снять аварийную остановку" if p.get("kill_switch") else "⛔ Аварийная остановка", "kill")],
        ]
        return text, kb(rows)

    def help_screen(self):
        text = ("<b>Что где</b>\n\n"
                "📊 <b>Что открыто</b>: все открытые сигналы с уровнями выхода.\n"
                "🩺 <b>Состояние</b>: идут ли данные с биржи, когда был последний поиск.\n"
                "✅ <b>Проверить всё</b>: Telegram, канал, биржа, история, картинки. Ответ ✅ / ⚠️ / ❌ по каждому пункту.\n"
                "🧪 <b>Тест</b>: все виды постов с пометкой TEST, в канал или тебе. Ничего не ломает.\n"
                "📣 <b>Режим публикации</b>: тестовый, с подтверждением или боевой.\n"
                "⏸ <b>Пауза</b>: бот считает, но посты не отправляет. Снимается той же кнопкой.\n"
                "🧭 <b>Стратегии</b>: включить или выключить тренд, листинги, капитуляции; порог сигнала тренда.\n"
                "⚖️ <b>Риск и плечо</b>: риск на часовую сделку и потолок плеча.\n"
                "🪙 <b>Монеты</b>: какие монеты смотрит бот (авто: топ по обороту).\n"
                "🕒 <b>Расписание</b>: время ежедневной заметки.\n"
                "🧾 <b>Итоги недели</b>: что закрылось, сколько заработано, разбор минусов.\n"
                "📤 <b>Сделки в Excel</b>: все сигналы и результаты файлами CSV.\n"
                "🗣 <b>Блогеры</b>: перешли сюда пост или скриншот, бот вытащит уровни и покажет их на картинках сигналов.\n"
                "📌 <b>Пост о методе</b>: закреплённый пост в канале с объяснением стратегии.\n"
                "💻 <b>Компьютеры</b>: какой компьютер сейчас главный и как переключить.\n"
                "⛔ <b>Аварийная остановка</b>: бот перестаёт искать сигналы и писать в канал, пока не снимешь.\n\n"
                f"Панель с графиками: http://127.0.0.1:{self.cfg['dashboard']['port']} на компьютере с ботом. "
                "📱 <b>Панель на телефоне</b>: присылает ссылку с ключом, работает и с мобильного интернета.")
        return text, kb([self.BACK])

    async def demo_screen(self):
        from .demo import DemoTrader
        dt = DemoTrader(self.cfg, self.conn)
        pc = self.eff().get("paper", {})
        on = pc.get("enabled", True)
        text = ("🧪 <b>Бумажная торговля</b> " + ("✅ вкл" if on else "❌ выкл") + "\n"
                f"Виртуальные счета по {pc.get('start_usd', 100)}$: бот торгует свои сигналы как живой трейдер. "
                f"Цены и комиссии бирж, плечо x{pc.get('leverage', 5)} (меняется кнопками ниже, действует на новые сделки), "
                "фандинг, ликвидация. Ордера на биржу не уходят.\n\n"
                + await dt.report())
        lev = int(pc.get("leverage", 5))
        text += (f"\n\nМинимальная позиция по правилам биржи (обычно 5$, у BTC и ETH больше). При плече x{lev} "
                 f"на неё уходит маржа от {5 / lev:.2f}$. Если сигнал меньше минимума, бот поднимает позицию до минимума, "
                 "а если маржи не хватает, пропускает сделку и пишет почему.")
        levs = [(("• " if lev == x else "") + f"x{x}", f"plev:{x}") for x in (1, 3, 5, 10, 20)]
        total, miss = dt.live_counts()
        rows = [[(f"▶️ Открыть живые сигналы ({miss} из {total})", "demo_live")]] if miss else []
        if total:
            text += (f"\n\nЖивых открытых сигналов: {total}, на бумажном счёте нет: {miss}. "
                     "Кнопка «Открыть живые сигналы» откроет их по текущей цене (не по цене сигнала).")
        return text, kb(rows + [[("Выключить" if on else "Включить", "demo_tg"), ("🔄 Обновить", "demo")], levs,
                         [(f"♻️ Начать заново с {pc.get('start_usd', 100)}$", "demo_reset")], self.BACK])

    async def snaps_screen(self):
        """Открытые и последние закрытые сигналы кнопками: нажал, бот прислал картинку графика."""
        from . import snap
        try:
            items = await snap.feed(self.cfg["dashboard"]["port"])
        except Exception as e:
            return f"Панель не отвечает: {escape(str(e)[:100])}", kb([self.BACK])
        op = sorted([x for x in items if x.get("open")], key=lambda x: -x["entry_time"])[:12]
        cl = sorted([x for x in items if not x.get("open")], key=lambda x: -(x.get("exit_time") or 0))[:6]
        rows = [[(snap.label(x), f"snap:{x['symbol']}:{x['entry_time']}:{x['kind']}")] for x in op + cl]
        text = ("📈 <b>Сделки на графике</b>\n\nНажми на сделку: пришлю картинку графика со входом, целями, стопом и итогом. "
                f"Сверху {len(op)} открытых, ниже последние закрытые.") if rows else "Сделок пока нет."
        return text, kb(rows + [self.BACK])

    async def send_snap(self, chat, data: str):
        from . import snap
        _, sym, t, *k = data.split(":")
        kind = k[0] if k else ""
        port = self.cfg["dashboard"]["port"]
        items = await snap.feed(port)
        it = next((x for x in items if x["symbol"] == sym and str(x["entry_time"]) == t and (not kind or x["kind"] == kind)), None)
        if not it:
            return await self.tg.send_message(chat, "Сделка не найдена, открой список заново.")
        try:
            png = await snap.render(port, "live", sym, int(t), kind)
        except Exception as e:
            return await self.tg.send_message(chat, snap.caption(it) + f"\n\n(картинка не получилась: {escape(str(e)[:120])})")
        await self.tg.send_photo(chat, png, snap.caption(it))

    async def phone_screen(self):
        """Панель на телефоне: перебирает способы доступа и даёт первую ссылку, которая реально открылась снаружи."""
        from . import cloud, phone
        from .http import Http
        port, tok = self.cfg["dashboard"]["port"], phone.token(self.conn)
        lines = ["📱 <b>Панель на телефоне</b>\n"]
        local_ok, local_why = await phone.check(f"http://127.0.0.1:{port}", tok, tries=1)
        if not local_ok:
            lines.append(f"❌ Панель не отвечает даже на этом компьютере ({escape(local_why)}). Перезапусти бота.\n")
        cl = cloud.link(self.cfg, self.conn)
        if cl and cloud.STATE.get("ok_ts") and not cloud.STATE.get("err"):
            lines.append(f"☁️ <b>Через облако</b>:\n{cl}\n")
        phone.STATE["conn"] = self.conn
        http = Http()
        try:
            url, how, rep = await phone.best(http, port, tok)
        finally:
            await http.close()
        if url:
            lines.append(f"🌍 <b>Откуда угодно</b> (мобильный интернет тоже), через {how}:\n{url}/?token={tok}\n"
                         "✅ Проверил с компьютера: открывается.")
            if how == "pinggy":
                lines.append("Бесплатная ссылка Pinggy живёт 60 минут, потом нажми «🔁 Новый адрес». Постоянная: ngrok, ниже.")
            elif how == "ngrok":
                lines.append("Адрес постоянный, добавь в закладки. В первый раз ngrok покажет предупреждение: нажми Visit Site.")
            else:
                lines.append("Адрес меняется при перезапуске бота, тогда нажми кнопку снова.")
        else:
            lines.append("⚠️ Ни один способ не открылся снаружи.")
        if rep:
            lines.append("<i>" + "\n".join(escape(x) for x in rep) + "</i>")
        lines.append("")
        if not phone.ngrok_cfg(self.conn)[0]:
            lines.append("🔒 <b>Постоянная ссылка (ngrok, бесплатно)</b>: зарегистрируйся на ngrok.com, открой Your Authtoken, "
                         "скопируй и пришли мне сообщением: <code>/ngrok ТОКЕН</code>. Бот сам скачает ngrok, ссылка больше не "
                         "будет меняться.\n")
        ips = phone.lan_ips()
        if self.cfg["dashboard"].get("host") == "0.0.0.0" and ips:
            lines.append("🏠 <b>Дома по Wi-Fi</b>: " + " или ".join(f"http://{ip}:{port}/?token={tok}" for ip in ips)
                         + "\nНе открывается: один раз запусти в папке бота <b>allow_phone.bat</b>.\n")
        lines.append("Ссылки с секретным ключом: не пересылай их. Телефон запоминает ключ после первого открытия.")
        return "\n".join(lines), kb([[("🔄 Проверить", "phone"), ("🔁 Новый адрес", "phone_new")], self.BACK])

    def mode_screen(self):
        m = self.eff()["publishing"]["mode"]
        lines = ["<b>Режим публикации</b>\n"] + [f"{'👉 ' if m == k else ''}<b>{v[0]}</b>: {v[1]}" for k, v in self.MODES.items()]
        lang = self.eff()["publishing"].get("lang", "en")
        lines.append(f"\nЯзык постов в канале: <b>{'русский' if lang == 'ru' else 'английский'}</b>")
        rows = [[(("• " if m == k else "") + v[0], f"setmode:{k}")] for k, v in self.MODES.items()]
        rows.append([("🌐 Посты на английском" if lang == "ru" else "🌐 Посты на русском", "lang")])
        return "\n".join(lines), kb(rows + [self.BACK])

    def strat_screen(self):
        e = self.eff()
        st = lambda x: "✅ вкл" if x else "❌ выкл"
        thr = e["portfolio"].get("signal_min_weight", 0.1)
        text = ("<b>Стратегии</b>\n\n"
                f"🧭 <b>Тренд</b> {st(e['portfolio'].get('enabled'))}: основная прибыль, около сигнала в день. "
                f"Сигналом становится позиция от {thr * 100:.0f}% депозита: ниже порог, больше сигналов, но они мельче.\n\n"
                f"🆕 <b>Шорт листингов</b> {st(e['listings'].get('enabled'))}: шорт новых монет со слабым первым днём, держим до 60 дней.\n"
                f"   Bybit {st(e['listings'].get('bybit'))}: те же правила для монет, которых нет на Binance. {self.bybit_stats()}\n\n"
                f"⚡ <b>Капитуляции</b> {st(e['scanner'].get('enabled', True))}: редкие часовые сигналы после сильного слива, со стопом и целями.\n\n"
                f"🧭 <b>BTC Layers</b> {st(e.get('btc_layers', {}).get('enabled'))}: дневная позиция по BTC (макро + тренд), "
                f"пост только при смене, свой бумажный счёт 100$. {self.layers_now()}")
        rows = [[("🧭 Тренд: " + ("выключить" if e["portfolio"].get("enabled") else "включить"), "tg:portfolio.enabled")],
                [("Порог −", "thr:-0.02"), (f"порог {thr * 100:.0f}% депо", "noop"), ("Порог +", "thr:0.02")],
                [("🆕 Листинги: " + ("выключить" if e["listings"].get("enabled") else "включить"), "tg:listings.enabled")],
                [("🆕 Листинги Bybit: " + ("выключить" if e["listings"].get("bybit") else "включить"), "tg:listings.bybit")],
                [("⚡ Капитуляции: " + ("выключить" if e["scanner"].get("enabled", True) else "включить"), "tg:scanner.enabled")],
                [("🧭 BTC Layers: " + ("выключить" if e.get("btc_layers", {}).get("enabled") else "включить"), "tg:btc_layers.enabled")],
                self.BACK]
        return text, kb(rows)

    def layers_screen(self):
        """Как работает BTC Layers: позиция и откуда она, слои, бэктест, бумажный счёт, история смен."""
        from .btc_layers import pos_text
        from .demo import DemoTrader
        e = self.eff().get("btc_layers", {})
        s = settings.get(self.conn, "btc_layers_state")
        on = bool(e.get("enabled"))
        L = [f"🧭 <b>BTC Layers</b> {'✅ работает' if on else '❌ выключен'}",
             "Дневная модель по биткоину. Раз в сутки после закрытия дня UTC считает, сколько держать BTC: "
             "макро решает сторону, тренд долю, волатильность размер. Пост в канал только при смене позиции.", ""]
        if not s:
            L.append("Расчёта ещё не было: нажми «🔄 Пересчитать сейчас».")
        else:
            x = s["signal"]
            lev = int(self.eff().get("paper", {}).get("leverage", 5))
            L.append(f"<b>Сейчас: {pos_text(x)}</b> (по закрытию {s['date']}, BTC {s['btc']:,.0f}$)")
            if abs(x) > 1e-9:
                L.append(f"На 100$: {'лонг' if x > 0 else 'шорт'} на {abs(x) * 100:.0f}$, маржа при x{lev}: {abs(x) * 100 / lev:.0f}$")
            if s.get("side") is not None:
                L.append(f"Расчёт: доля {s['side']:+.1f} × размер {s['vt']:.2f} = {x:+.2f}")
            L += ["", f"1️⃣ Макро {s['macro']:+.2f} → режим <b>{'бычий' if s['regime'] > 0 else 'медвежий'}</b> "
                      "(смена только за ±0.10)"]
            names = {"gas": "газ США", "fed": "ожидания ФРС", "liq": "ликвидность ФРС", "curve": "кривая доходности",
                     "ttf": "газ Европа к США", "prem": "премия Coinbase"}
            for k, v in s["parts"].items():
                if v is not None:
                    L.append(f"   {'🟢' if v > 0 else '🔴'} {names.get(k, k)} {v:+.2f}")
            L.append(f"2️⃣ Тренд {s['trend']:+.2f} ({'вверх' if s['trend'] > 0 else 'вниз'}): "
                     + ("полная доля" if (s['trend'] > 0) == (s['regime'] > 0) else "против макро, доля меньше или кэш"))
            L.append(f"3️⃣ Волатильность {s['vol'] * 100:.0f}% в год: размер {s.get('vt', 0):.2f} (цель {e.get('vol_target', 0.4) * 100:.0f}%, не больше 1.5)")
            bt = s.get("bt")
            if bt:
                L += ["", f"📊 На истории с {bt['start']}: 100$ → {bt['final']:,.0f}$, {bt['cagr'] * 100:+.0f}% в год, "
                          f"просадка до {bt['max_dd'] * 100:.0f}%"]
        dt = DemoTrader(self.cfg, self.conn)
        if dt.has("layers"):
            a = dt.account("layers")
            eq = dt.equity("layers")
            L += ["", f"🧪 <b>Бумажный счёт</b>: {eq:.2f}$ ({(eq / a['start_usd'] - 1) * 100:+.1f}% от {a['start_usd']:.0f}$), "
                      f"комиссии {a['fees']:.2f}$, фандинг {-a['funding']:+.2f}$"]
            for r in self.conn.execute("SELECT * FROM demo_trades WHERE exchange='layers' AND status='open'"):
                L.append(f"   {'🟢 лонг' if r['side'] == 'long' else '🔴 шорт'} BTC {r['open_qty'] * r['entry']:.2f}$ "
                         f"от {r['entry']:,.0f}, маржа {r['margin'] * r['open_qty'] / r['qty']:.2f}$")
            done = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(realized),0) FROM demo_trades "
                                     "WHERE exchange='layers' AND status='closed'").fetchone()
            if done[0]:
                L.append(f"   закрыто сделок {done[0]}, итог {done[1]:+.2f}$")
        hist = settings.get(self.conn, "btc_layers_log") or []
        if hist:
            L += ["", "🕓 <b>Смены позиции</b>"] + [f"   {h['date']}: {pos_text(h['signal'])} при BTC {h['btc']:,.0f}$"
                                                   for h in hist[-6:][::-1]]
        rows = []
        try:
            from .btc_layers import live_items
            op = [x for x in live_items(self.conn) if x.get("open")]
            if op:
                rows.append([("📍 Текущая сделка на графике", f"snap:BTCUSDT:{op[-1]['entry_time']}:layers")])
        except Exception:
            pass
        rows += [[("📈 График и бэктест", "layers_png"), ("🔄 Пересчитать сейчас", "layers_run")],
                [("⏸ Выключить" if on else "▶️ Включить", "layers_tg")], self.BACK]
        return "\n".join(L), kb(rows)

    def layers_now(self) -> str:
        s = settings.get(self.conn, "btc_layers_state")
        if not s:
            return "Расчёта ещё не было."
        x = s["signal"]
        pos = "кэш" if abs(x) < 1e-9 else f"{'лонг' if x > 0 else 'шорт'} {abs(x) * 100:.0f}% депозита"
        return f"Сейчас: {pos} (по закрытию {s['date']})."

    @staticmethod
    def bybit_stats() -> str:
        import json
        from .listings_bt import BYBIT_REPORT
        if not BYBIT_REPORT.exists():
            return "Бэктест Bybit ещё не посчитан (считается при запуске бота, нужен доступ к Bybit)."
        m = json.loads(BYBIT_REPORT.read_text())["summary"]
        if not m["closed"]:
            return "На истории Bybit подходящих сделок нет."
        return (f"История Bybit: {m['closed']} сделок, {m['win_pct']}% в плюс, в среднем {m['avg_pct']:+.2f}% депозита "
                f"на сделку, сумма {m['sum_pct']:+.1f}%. Включай, только если сумма заметно в плюсе.")

    def risk_screen(self):
        e = self.eff()
        r = e["risk"]
        text = ("<b>Риск и плечо</b> (только для ⚡ капитуляций: у тренда и листингов размер в посте)\n\n"
                f"Риск на сделку: от {r['risk_min_pct']}% до {r['risk_max_pct']}% депозита, столько теряешь, если сработает стоп. "
                "Слабый сигнал берёт нижнюю границу, сильный верхнюю.\n"
                f"Не больше {e['scanner']['max_signals_per_day']} сигналов в день.\n"
                f"Потолок плеча: в канале x{r['public_leverage_cap']}, для тебя x{r['personal_leverage_cap']}. "
                "Далёкий стоп снижает плечо, но сигнал не отменяет.")
        rows = [[("мин −", "rk:risk_min_pct:-0.1"), (f"мин {r['risk_min_pct']}%", "noop"), ("мин +", "rk:risk_min_pct:0.1")],
                [("макс −", "rk:risk_max_pct:-0.1"), (f"макс {r['risk_max_pct']}%", "noop"), ("макс +", "rk:risk_max_pct:0.1")],
                [("сигналов в день −", "sp:-1"), (str(e["scanner"]["max_signals_per_day"]), "noop"), ("+", "sp:1")],
                [("плечо в канале −5", "rk:public_leverage_cap:-5"), (f"x{r['public_leverage_cap']}", "noop"), ("+5", "rk:public_leverage_cap:5")],
                [("моё плечо −1", "rk:personal_leverage_cap:-1"), (f"x{r['personal_leverage_cap']}", "noop"), ("+1", "rk:personal_leverage_cap:1")],
                self.BACK]
        return text, kb(rows)

    def lev_screen(self):
        return self.risk_screen()

    def sched_screen(self):
        s = self.eff()["schedule"]
        msk = (s["daily_note_hour_utc"] + 3) % 24
        text = ("<b>Расписание</b>\n\n"
                f"Ежедневная заметка о рынке: {s['daily_note_hour_utc']}:00 UTC ({msk}:00 по Москве).\n"
                "Тренд считается после закрытия суток (около 03:00 по Москве), листинги каждые 6 часов, капитуляции каждый час.")
        rows = [[("−1 час", "sch:-1"), (f"заметка {msk}:00 МСК", "noop"), ("+1 час", "sch:1")], self.BACK]
        return text, kb(rows)

    def coins_screen(self):
        manual = settings.get(self.conn, "symbols_manual")
        cur = manual or settings.get(self.conn, "universe", [])
        src = "свой список" if manual else f"автоматически: топ {self.cfg['universe']['top_n']} по обороту"
        text = (f"<b>Монеты</b> ({src}, {len(cur)} шт.)\n{', '.join(s.replace('USDT', '') for s in cur)}\n\n"
                "Задать свой список: напиши <code>/coins BTC,ETH,SOL</code>\n"
                "Вернуть автоматический: <code>/coins auto</code>")
        return text, kb([self.BACK])

    def pcs_screen(self):
        if not self.leader:
            return "Бот работает на одном компьютере, переключать нечего.", kb([self.BACK])
        text = ("<b>Компьютеры</b>\n\n"
                f"Этот компьютер: <b>{self.leader.name}</b> ({'основной, пишет в канал' if self.leader.is_main else 'запасной, молчит'}).\n\n"
                "Бот можно держать на двух компьютерах. В канал пишет только основной, запасной ждёт. "
                "Если основной молчит 15 минут, запасной сам становится главным.\n\n"
                "Сделать главным другой компьютер: напиши <code>/main имя</code> (имя: SIGBOT_NAME из .env того компьютера).")
        return text, kb([self.BACK])

    def status_text(self) -> str:
        lines = ["<b>Открыто сейчас</b>"]
        tr = self.conn.execute("SELECT * FROM trend_signals WHERE status='open' ORDER BY size_pct DESC").fetchall()
        lines.append(f"\n🧭 Тренд: {len(tr)}")
        for r in tr:
            lv = (f", выход при закрытии дня {'ниже' if r['side'] == 'long' else 'выше'} {r['exit_level']:.6g}"
                  if r["exit_level"] else "")
            lines.append(f"  {r['symbol'].replace('USDT', '')} {'лонг' if r['side'] == 'long' else 'шорт'} "
                         f"от {r['entry']:.6g}, {r['size_pct']}% депо{lv}")
        ls = self.conn.execute("SELECT * FROM listing_trades WHERE status='open'").fetchall()
        lines.append(f"\n🆕 Шорты листингов: {len(ls)}")
        for r in ls:
            lines.append(f"  {r['symbol'].replace('USDT', '')} шорт от {r['entry']:.6g}, стоп {r['stop']:.6g}, "
                         f"выход {time.strftime('%d.%m', time.gmtime(r['exit_due_ts'] / 1000))}")
        opens = self.live.open_trades() if self.live else []
        lines.append(f"\n⚡ Капитуляции: {len(opens)}")
        for o in opens:
            lines.append(f"  #{o['id']} {o['symbol'].replace('USDT', '')} {'лонг' if o['side'] == 'long' else 'шорт'} "
                         f"вход {o['entry']:.6g}, стоп {o['cur_stop']:.6g}, осталось {o['remaining']:.0%}")
        pend = self.conn.execute("SELECT COUNT(*) FROM posts WHERE status='pending'").fetchone()[0]
        if pend:
            lines.append(f"\n✋ Ждут твоего подтверждения: {pend}")
        return "\n".join(lines)

    def health_text(self) -> str:
        lines = ["<b>Состояние</b>\n"]
        if self.collector:
            for c, v in self.collector.health.snapshot().items():
                ago = f", {v['age_s'] // 60} мин назад" if v["age_s"] is not None else ""
                lines.append(f"{'❌' if v['error'] else '✅'} {self.NAMES.get(c, c)}"
                             + (f": ошибка {v['error'][:80]}" if v["error"] else ago))
        last = settings.get(self.conn, "last_scan")
        if last:
            lines.append(f"\nПоследний поиск капитуляций: {time.strftime('%d.%m %H:%M', time.gmtime(last['ts'] / 1000))} UTC, "
                         f"монет {last['symbols']}, кандидатов {last['candidates']}")
        t = int(time.time() * 1000)
        g = self.conn.execute("SELECT COUNT(*) FROM gaps WHERE start_ts>?", (t - DAY,)).fetchone()[0]
        lines.append(f"Пропусков в данных за сутки: {g}")
        return "\n".join(lines)

    # ---------- обработка ----------
    async def show(self, chat, screen, message_id=None):
        text, markup = screen
        if message_id:
            try:
                await self.tg.edit_text(chat, message_id, text, markup)
                return
            except Exception:
                pass
        await self.tg.send_message(chat, text, reply_markup=markup)

    async def on_callback(self, cq):
        data, chat, mid = cq["data"], cq["message"]["chat"]["id"], cq["message"]["message_id"]
        await self.tg.answer_callback(cq["id"])
        e = self.eff()
        if data == "menu":
            return await self.show(chat, self.menu(), mid)
        if data == "status":
            return await self.show(chat, (self.status_text(), kb([self.BACK])), mid)
        if data == "health":
            return await self.show(chat, (self.health_text(), kb([self.BACK])), mid)
        if data == "selfcheck":
            from . import diag
            await self.tg.send_message(chat, "Проверяю… до минуты.", parse_mode=None)
            rows = await diag.selfcheck(self.cfg, self.conn, self.tg.http)
            return await self.tg.send_message(chat, diag.format_check(rows), parse_mode=None)
        if data.startswith("testposts"):
            from . import diag
            to = self.pub.channel_id if data.endswith(":ch") else chat
            n = await diag.test_posts(self.cfg, self.conn, self.tg, to)
            return await self.tg.send_message(chat, f"🧪 Отправлено тестовых постов: {n} ({'в канал' if to != chat else 'сюда'}).",
                                              parse_mode=None)
        if data == "noop":
            return
        if data == "help":
            return await self.show(chat, self.help_screen(), mid)
        if data == "demo_live":
            from .demo import DemoTrader
            await self.show(chat, ("🧪 Открываю живые сигналы на бумажных счетах…", kb([self.BACK])), mid)
            dt = DemoTrader(self.cfg, self.conn, self.pub.notify_owner if self.pub else None)
            n, had = await dt.open_live()
            await self.tg.send_message(chat, f"🧪 Открыто сигналов: {n}" + (f", уже были на счёте: {had}" if had else "")
                                       + ". Подробности по каждой сделке выше, итог ниже.")
            return await self.tg.send_message(chat, *await self.demo_screen())
        if data.startswith("plev:"):
            settings.put(self.conn, "paper.leverage", int(data[5:]))
            return await self.show(chat, await self.demo_screen(), mid)
        if data == "demo_reset":
            return await self.show(chat, ("Закрыть все бумажные позиции и начать заново со стартового депозита?",
                                          kb([[("♻️ Да, заново", "demo_reset_yes"), ("Отмена", "demo")]])), mid)
        if data in ("demo", "demo_tg", "demo_reset_yes"):
            if data == "demo_tg":
                settings.put(self.conn, "paper.enabled", not e.get("paper", {}).get("enabled", True))
            if data == "demo_reset_yes":
                from .demo import DemoTrader
                DemoTrader(self.cfg, self.conn).reset()
            return await self.show(chat, await self.demo_screen(), mid)
        if data == "layers":
            return await self.show(chat, self.layers_screen(), mid)
        if data == "layers_tg":
            settings.put(self.conn, "btc_layers.enabled", not e.get("btc_layers", {}).get("enabled"))
            return await self.show(chat, self.layers_screen(), mid)
        if data == "layers_run":
            from .btc_layers import LayersService
            from .demo import DemoTrader
            await self.show(chat, ("🧭 Считаю BTC Layers, до минуты…", kb([self.BACK])), mid)
            try:
                await LayersService(self.cfg, self.conn, self.pub, DemoTrader(self.cfg, self.conn, self.pub.notify_owner)
                                    if self.pub else None).run_daily(int(time.time() * 1000), force=True)
            except Exception as ex:
                await self.tg.send_message(chat, f"⚠️ Расчёт не удался: {escape(str(ex)[:150])}")
            return await self.show(chat, self.layers_screen(), mid)
        if data == "layers_png":
            from . import snap
            try:
                png = await snap.render_layers(self.cfg["dashboard"]["port"])
                return await self.tg.send_photo(chat, png, "🧭 BTC Layers: позиция, бэктест с 2018 года и позиция по дням")
            except Exception as ex:
                return await self.tg.send_message(chat, f"Картинка не получилась: {escape(str(ex)[:150])}")
        if data == "snaps":
            return await self.show(chat, await self.snaps_screen(), mid)
        if data.startswith("snap:"):
            return await self.send_snap(chat, data)
        if data == "phone_new":
            from . import phone
            phone.stop()
            data = "phone"
        if data == "phone":
            await self.show(chat, ("📱 Запускаю доступ к панели, до 40 секунд…", kb([self.BACK])), mid)
            return await self.show(chat, await self.phone_screen(), mid)
        if data == "pcs":
            return await self.show(chat, self.pcs_screen(), mid)
        if data == "strat":
            return await self.show(chat, self.strat_screen(), mid)
        if data.startswith("tg:"):
            key = data[3:]
            sec, leaf = key.split(".")
            settings.put(self.conn, key, not e[sec].get(leaf, True))
            return await self.show(chat, self.strat_screen(), mid)
        if data.startswith("thr:"):
            v = round(min(max(e["portfolio"].get("signal_min_weight", 0.1) + float(data[4:]), 0.04), 0.3), 2)
            settings.put(self.conn, "portfolio.signal_min_weight", v)
            return await self.show(chat, self.strat_screen(), mid)
        if data == "mode":
            return await self.show(chat, self.mode_screen(), mid)
        if data.startswith("setmode:"):
            settings.put(self.conn, "publishing.mode", data.split(":")[1])
            return await self.show(chat, self.mode_screen(), mid)
        if data == "lang":
            settings.put(self.conn, "publishing.lang", "en" if e["publishing"].get("lang", "en") == "ru" else "ru")
            return await self.show(chat, self.mode_screen(), mid)
        if data == "pause":
            settings.put(self.conn, "publishing.paused", not e["publishing"].get("paused"))
            return await self.show(chat, self.menu(), mid)
        if data == "kill":
            if not e["publishing"].get("kill_switch"):
                return await self.show(chat, ("⛔ Аварийная остановка: бот перестанет искать сигналы и писать в канал, пока не снимешь её. Остановить?",
                                              kb([[("⛔ Да, остановить всё", "kill_yes"), ("Отмена", "menu")]])), mid)
            settings.put(self.conn, "publishing.kill_switch", False)
            return await self.show(chat, self.menu(), mid)
        if data == "kill_yes":
            settings.put(self.conn, "publishing.kill_switch", True)
            return await self.show(chat, self.menu(), mid)
        if data == "risk":
            return await self.show(chat, self.risk_screen(), mid)
        if data == "lev":
            return await self.show(chat, self.lev_screen(), mid)
        if data.startswith("rk:"):
            _, key, delta = data.split(":")
            val = _round(e["risk"][key] + float(delta))
            bounds = {"risk_min_pct": (0.1, e["risk"]["risk_max_pct"]), "risk_max_pct": (e["risk"]["risk_min_pct"], 3.0),
                      "public_leverage_cap": (1, 50), "personal_leverage_cap": (1, 50)}[key]
            settings.put(self.conn, f"risk.{key}", min(max(val, bounds[0]), bounds[1]))
            return await self.show(chat, self.lev_screen() if "leverage" in key else self.risk_screen(), mid)
        if data.startswith("sp:"):
            v = min(max(e["scanner"]["max_signals_per_day"] + int(data[3:]), 0), 5)
            settings.put(self.conn, "scanner.max_signals_per_day", v)
            return await self.show(chat, self.risk_screen(), mid)
        if data == "sched":
            return await self.show(chat, self.sched_screen(), mid)
        if data.startswith("sch:"):
            settings.put(self.conn, "schedule.daily_note_hour_utc",
                         (e["schedule"]["daily_note_hour_utc"] + int(data[4:])) % 24)
            return await self.show(chat, self.sched_screen(), mid)
        if data == "coins":
            return await self.show(chat, self.coins_screen(), mid)
        if data == "export":
            import tempfile
            with tempfile.TemporaryDirectory() as d:
                for p in ledger.export_csv(self.conn, d):
                    await self.tg.send_document(chat, p.read_bytes(), p.name)
            return
        if data == "pm":
            t = int(time.time() * 1000)
            return await self.tg.send_message(chat, postmortem.build(self.conn, t - 7 * DAY, t), parse_mode=None)
        if data == "blog":
            acc = self.bl.accuracy()
            text = "<b>Блогеры</b>\n\nПерешли сюда пост блогера или пришли скриншот: бот вытащит монету и уровни, спросит подтверждение и покажет их зонами на картинках сигналов. На входы бота это не влияет.\n\nТочность авторов (уровень сработал / всего):\n" + (
                "\n".join(f"{a['author']}: {a['hits']}/{a['calls']} ({a['hit_rate_pct']}%)" for a in acc)
                or "пока нет завершённых прогнозов")
            return await self.show(chat, (text, kb([self.BACK])), mid)
        if data == "method":
            return await self.show(chat, ("Опубликовать в канале пост с объяснением метода и закрепить его?",
                                          kb([[("📌 Опубликовать и закрепить", "method_yes"), ("Отмена", "menu")]])), mid)
        if data == "method_yes":
            return await self.post_method(chat)
        if data.startswith(("pub:", "drop:")):
            kind, pid = data.split(":")
            res = await self.pub.resolve_pending(int(pid), kind == "pub")
            return await self.tg.send_message(chat, f"Пост #{pid}: " + {"sent": "опубликован ✅", "dropped": "удалён", "paused": "пауза, не опубликован"}.get(res, str(res)), parse_mode=None)
        if data.startswith(("bl_ok:", "bl_no:")):
            kind, cid = data.split(":")
            self.bl.confirm(int(cid), kind == "bl_ok")
            return await self.tg.send_message(chat, "Сохранено ✅" if kind == "bl_ok" else "Удалено", parse_mode=None)

    async def post_method(self, chat):
        import glob
        import json
        from . import templates
        reports = sorted(glob.glob("reports/backtest_*.json"))
        bt = json.loads(open(reports[-1]).read()) if reports else None
        text, nums = templates.method(self.eff(), bt)
        status = await self.pub.publish("method", text, nums)
        if status == "sent":
            mid = self.conn.execute("SELECT message_id FROM posts WHERE kind='method' AND status='sent' "
                                    "ORDER BY id DESC LIMIT 1").fetchone()[0]
            try:
                await self.tg.pin(self.pub.channel_id, mid)
            except Exception as e:
                status += f" (закрепить не вышло: {e})"
        await self.tg.send_message(chat, "Пост о методе: " + ("опубликован" if status.startswith("sent") else status), parse_mode=None)

    async def on_message(self, msg):
        chat = msg["chat"]["id"]
        text = msg.get("text") or msg.get("caption") or ""
        if text.startswith("/start") or text.startswith("/menu"):
            return await self.show(chat, self.menu())
        if text.startswith("/main") and self.leader:
            parts = text.split()
            if len(parts) < 2:
                return await self.tg.send_message(chat, f"Сейчас главный: {self.leader.name}. Переключить: /main имя (SIGBOT_NAME другого компьютера).", parse_mode=None)
            target = parts[1].lower()
            await self.leader.claim(target)
            if target != self.leader.name:
                self.leader.is_main = False
            return await self.tg.send_message(chat, f"Главный теперь {target}. Он подхватит работу в течение минуты.",
                                              parse_mode=None)
        if text.startswith("/ngrok"):
            parts = text.split()
            if len(parts) < 2:
                return await self.tg.send_message(chat, "Пришли так: /ngrok ТОКЕН (с ngrok.com). Можно добавить свой адрес: /ngrok ТОКЕН имя.ngrok-free.app",
                                                  parse_mode=None)
            settings.put(self.conn, "ngrok_token", parts[1])
            settings.put(self.conn, "ngrok_domain", parts[2] if len(parts) > 2 else None)
            from . import phone
            phone.stop()
            await self.tg.send_message(chat, "🔒 Токен ngrok сохранён только на этом компьютере. Запускаю постоянную ссылку…", parse_mode=None)
            await self.show(chat, ("📱 Запускаю доступ к панели, до минуты…", kb([self.BACK])))
            return await self.show(chat, await self.phone_screen())
        if text.startswith("/coins"):
            arg = text[6:].strip()
            if arg.lower() == "auto" or not arg:
                settings.put(self.conn, "symbols_manual", None)
            else:
                syms = [s.strip().upper() for s in arg.replace(" ", ",").split(",") if s.strip()]
                settings.put(self.conn, "symbols_manual", [s if s.endswith("USDT") else s + "USDT" for s in syms])
            if self.collector:
                try:
                    await self.collector.refresh_universe()
                except Exception as e:
                    log.warning("universe refresh: %s", e)
            return await self.show(chat, self.coins_screen())
        if text.startswith("/"):
            return await self.show(chat, self.menu())

        # блогер: текст или скриншот
        image = None
        if msg.get("photo"):
            image = await self.tg.download_file(msg["photo"][-1]["file_id"])
        if not image and not text:
            return
        author = None
        fo = msg.get("forward_origin") or {}
        if fo.get("type") == "channel":
            author = fo["chat"].get("title")
        elif fo.get("type") == "user":
            author = fo["sender_user"].get("username") or fo["sender_user"].get("first_name")
        elif fo.get("type") == "hidden_user":
            author = fo.get("sender_user_name")
        await self.tg.send_message(chat, "Читаю… до минуты. Меню при этом работает.", parse_mode=None)
        # разбор в фоне: пока нейросеть думает, бот отвечает на команды
        asyncio.create_task(self._extract(chat, text, image, author))

    async def _extract(self, chat, text, image, author):
        try:
            data = await self.llm.extract_call(text or None, image) if self.llm else None
            why = None if data else (getattr(self.llm, "last_error", None) or "нейросеть не нашла уровней")
            if not data and text:
                from .bloggers import parse_text
                data = parse_text(text)              # простой разбор без нейросети
            cid = self.bl.add_draft(data or {}, author)
            if not cid:
                return await self.tg.send_message(
                    chat, "Не нашёл монету с ценами (вход, стоп, цели или уровни)." +
                    (f"\nПричина: {why}." if why else "") +
                    ("\nСкриншоты без нейросети не читаются." if image and why else ""), parse_mode=None)
            call = self.bl.get(cid)
            await self._ask_confirm(chat, cid, call, why)
        except Exception as e:
            log.exception("extract")
            await self.tg.send_message(chat, f"Не получилось разобрать: {str(e)[:150]}", parse_mode=None)

    async def _ask_confirm(self, chat, cid, call, why=None):
        await self.tg.send_message(chat, ("Разобрал без нейросети (" + why + "):\n\n" if why else "Извлёк:\n\n") +
                                   Bloggers.describe(call) + "\n\nСохранить?",
                                   reply_markup=kb([[("✅ Confirm", f"bl_ok:{cid}"), ("✖️ Drop", f"bl_no:{cid}")]]),
                                   parse_mode=None)

    async def loop(self):
        import json as _json
        try:   # подсказки команд в Telegram по кнопке «/» и в меню бота
            await self.tg.call("setMyCommands", commands=_json.dumps([
                {"command": "menu", "description": "Главное меню и настройки"},
                {"command": "main", "description": "Какой компьютер главный, переключить"},
                {"command": "coins", "description": "Список монет: /coins BTC,ETH или /coins auto"}], ensure_ascii=False),
                scope=_json.dumps({"type": "chat", "chat_id": self.owner}))
        except Exception as e:
            log.info("setMyCommands: %s", e)
        offset = settings.get(self.conn, "tg_offset")
        while True:
            if self.leader and not self.leader.is_main:
                await asyncio.sleep(20)          # команды слушает только основной экземпляр
                continue
            try:
                ups = await self.tg.get_updates(offset, timeout=20)
                for u in ups:
                    offset = u["update_id"] + 1
                    settings.put(self.conn, "tg_offset", offset)
                    src = (u.get("message") or u.get("callback_query") or {}).get("from", {})
                    if src.get("id") != self.owner:
                        continue   # чужим не отвечаем
                    try:
                        if "callback_query" in u:
                            await self.on_callback(u["callback_query"])
                        elif "message" in u and u["message"]["chat"]["type"] == "private":
                            await self.on_message(u["message"])
                    except Exception as e:
                        log.exception("admin update failed: %s", e)
            except Exception as e:
                log.warning("getUpdates: %s", e)
                await asyncio.sleep(5)
