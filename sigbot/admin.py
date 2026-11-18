"""Админка в личном чате с ботом. Отвечает только владельцу (TELEGRAM_OWNER_ID)."""
from __future__ import annotations

import asyncio
import logging
import time

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
    def menu(self):
        e = self.eff()
        p = e["publishing"]
        status = "⛔ KILL" if p.get("kill_switch") else "⏸ paused" if p.get("paused") else "▶️ running"
        text = (f"<b>sigbot</b> · {status}\nMode: <b>{p['mode']}</b>\n"
                f"Risk: {e['risk']['risk_min_pct']}–{e['risk']['risk_max_pct']}%\n"
                f"Leverage cap: public x{e['risk']['public_leverage_cap']}, personal x{e['risk']['personal_leverage_cap']}\n"
                f"Signals/day: {e['scanner']['max_signals_per_day']}")
        rows = [
            [("📊 Status", "status"), ("🩺 Health", "health")],
            [("✅ Self-check", "selfcheck")],
            [("🧪 Test → channel", "testposts:ch"), ("🧪 Test → me", "testposts:me")],
            [("🔁 Mode", "mode"), ("⏸ Pause" if not p.get("paused") else "▶️ Resume", "pause")],
            [("⚖️ Risk", "risk"), ("📐 Leverage", "lev")],
            [("🪙 Coins", "coins"), ("🕒 Schedule", "sched")],
            [("📤 Export CSV", "export"), ("🧾 Post mortem", "pm")],
            [("🗣 Bloggers", "blog"), ("📌 Post method", "method")],
            [("⛔ Kill switch OFF" if p.get("kill_switch") else "⛔ Kill switch", "kill")],
        ]
        return text, kb(rows)

    def mode_screen(self):
        m = self.eff()["publishing"]["mode"]
        rows = [[(("• " if m == x else "") + x, f"setmode:{x}") for x in ("paper", "confirm", "auto")],
                [("⬅️ Back", "menu")]]
        return ("paper: publish right away with #forwardtest\nconfirm: every post waits for your button\n"
                "auto: publish without confirmation"), kb(rows)

    def risk_screen(self):
        r = self.eff()["risk"]
        rows = [[("min −", "rk:risk_min_pct:-0.1"), (f"min {r['risk_min_pct']}%", "noop"), ("min +", "rk:risk_min_pct:0.1")],
                [("max −", "rk:risk_max_pct:-0.1"), (f"max {r['risk_max_pct']}%", "noop"), ("max +", "rk:risk_max_pct:0.1")],
                [("signals/day −", "sp:-1"), (str(self.eff()["scanner"]["max_signals_per_day"]), "noop"), ("+", "sp:1")],
                [("⬅️ Back", "menu")]]
        return "Risk per trade by signal strength", kb(rows)

    def lev_screen(self):
        r = self.eff()["risk"]
        rows = [[("public −5", "rk:public_leverage_cap:-5"), (f"x{r['public_leverage_cap']}", "noop"),
                 ("public +5", "rk:public_leverage_cap:5")],
                [("personal −1", "rk:personal_leverage_cap:-1"), (f"x{r['personal_leverage_cap']}", "noop"),
                 ("personal +1", "rk:personal_leverage_cap:1")],
                [("⬅️ Back", "menu")]]
        return "Leverage caps. A far stop lowers leverage, it never cancels a signal.", kb(rows)

    def sched_screen(self):
        s = self.eff()["schedule"]
        rows = [[("note −1h", "sch:-1"), (f"daily note {s['daily_note_hour_utc']}:00 UTC", "noop"), ("+1h", "sch:1")],
                [("⬅️ Back", "menu")]]
        return "Schedule", kb(rows)

    def coins_screen(self):
        manual = settings.get(self.conn, "symbols_manual")
        cur = manual or settings.get(self.conn, "universe", [])
        src = "manual list" if manual else f"auto top {self.cfg['universe']['top_n']} by volume"
        text = (f"Coins ({src}, {len(cur)}):\n{', '.join(s.replace('USDT', '') for s in cur)}\n\n"
                "Set your own list: /coins BTC,ETH,SOL\nBack to auto: /coins auto")
        return text, kb([[("⬅️ Back", "menu")]])

    def status_text(self) -> str:
        opens = self.live.open_trades()
        lines = [f"Open trades: {len(opens)}"]
        for o in opens:
            lines.append(f"  #{o['id']} {o['symbol']} {o['side']} entry {o['entry']} stop {o['cur_stop']} "
                         f"left {o['remaining']:.0%}")
        t = int(time.time() * 1000)
        n7 = self.conn.execute("SELECT COUNT(*) FROM signals WHERE ts>? AND source='live'", (t - 7 * DAY,)).fetchone()[0]
        last = settings.get(self.conn, "last_scan")
        lines.append(f"Signals 7d: {n7}")
        if last:
            lines.append(f"Last scan: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(last['ts'] / 1000))} UTC, "
                         f"{last['symbols']} coins, {last['candidates']} candidates")
        pend = self.conn.execute("SELECT COUNT(*) FROM posts WHERE status='pending'").fetchone()[0]
        if pend:
            lines.append(f"Posts waiting for you: {pend}")
        return "\n".join(lines)

    def health_text(self) -> str:
        if self.collector:
            snap = self.collector.health.snapshot()
            lines = [f"{c}: {'ERR ' + v['error'][:80] if v['error'] else 'ok'}"
                     + (f" ({v['age_s']}s ago)" if v["age_s"] is not None else "") for c, v in snap.items()]
        else:
            lines = []
        t = int(time.time() * 1000)
        g = self.conn.execute("SELECT COUNT(*) FROM gaps WHERE start_ts>?", (t - DAY,)).fetchone()[0]
        lines.append(f"Gaps 24h: {g}")
        return "\n".join(lines) or "no data yet"

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
            return await self.show(chat, (self.status_text(), kb([[("⬅️ Back", "menu")]])), mid)
        if data == "health":
            return await self.show(chat, (self.health_text(), kb([[("⬅️ Back", "menu")]])), mid)
        if data == "selfcheck":
            from . import diag
            await self.tg.send_message(chat, "Checking…", parse_mode=None)
            rows = await diag.selfcheck(self.cfg, self.conn, self.tg.http)
            return await self.tg.send_message(chat, diag.format_check(rows), parse_mode=None)
        if data.startswith("testposts"):
            from . import diag
            to = self.pub.channel_id if data.endswith(":ch") else chat
            n = await diag.test_posts(self.cfg, self.conn, self.tg, to)
            return await self.tg.send_message(chat, f"🧪 Test posts sent: {n} ({'channel' if to != chat else 'here'}).",
                                              parse_mode=None)
        if data == "mode":
            return await self.show(chat, self.mode_screen(), mid)
        if data.startswith("setmode:"):
            settings.put(self.conn, "publishing.mode", data.split(":")[1])
            return await self.show(chat, self.mode_screen(), mid)
        if data == "pause":
            settings.put(self.conn, "publishing.paused", not e["publishing"].get("paused"))
            return await self.show(chat, self.menu(), mid)
        if data == "kill":
            if not e["publishing"].get("kill_switch"):
                return await self.show(chat, ("Kill switch stops scanning and all posts. Sure?",
                                              kb([[("⛔ Yes, stop everything", "kill_yes"), ("Cancel", "menu")]])), mid)
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
            text = "Send me a blogger's text or screenshot and I'll extract the levels.\n\nAccuracy:\n" + (
                "\n".join(f"{a['author']}: {a['hits']}/{a['calls']} ({a['hit_rate_pct']}%)" for a in acc)
                or "no finished calls yet")
            return await self.show(chat, (text, kb([[("⬅️ Back", "menu")]])), mid)
        if data == "method":
            return await self.show(chat, ("Publish and pin the method post in the channel?",
                                          kb([[("📌 Publish & pin", "method_yes"), ("Cancel", "menu")]])), mid)
        if data == "method_yes":
            return await self.post_method(chat)
        if data.startswith(("pub:", "drop:")):
            kind, pid = data.split(":")
            res = await self.pub.resolve_pending(int(pid), kind == "pub")
            return await self.tg.send_message(chat, f"Post #{pid}: {res}", parse_mode=None)
        if data.startswith(("bl_ok:", "bl_no:")):
            kind, cid = data.split(":")
            self.bl.confirm(int(cid), kind == "bl_ok")
            return await self.tg.send_message(chat, "Saved ✅" if kind == "bl_ok" else "Dropped", parse_mode=None)

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
                status += f" (pin failed: {e})"
        await self.tg.send_message(chat, f"Method post: {status}", parse_mode=None)

    async def on_message(self, msg):
        chat = msg["chat"]["id"]
        text = msg.get("text") or msg.get("caption") or ""
        if text.startswith("/start") or text.startswith("/menu"):
            return await self.show(chat, self.menu())
        if text.startswith("/main") and self.leader:
            parts = text.split()
            if len(parts) < 2:
                return await self.tg.send_message(chat, f"Main now: {self.leader.name}. Switch: /main <name>, "
                                                        "name is SIGBOT_NAME of the other computer.", parse_mode=None)
            target = parts[1].lower()
            await self.leader.claim(target)
            if target != self.leader.name:
                self.leader.is_main = False
            return await self.tg.send_message(chat, f"Main is now {target}. It takes over within a minute.",
                                              parse_mode=None)
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
