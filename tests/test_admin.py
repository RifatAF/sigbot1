import asyncio

from sigbot.admin import Admin
from sigbot.bloggers import Bloggers, parse_text


class TG:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append(text)
        return {"message_id": len(self.sent)}

    async def edit_message_text(self, *a, **kw):
        return {}


class SlowLLM:
    last_error = "Gemini не ответил за 45 секунд"

    async def extract_call(self, text=None, image=None):
        await asyncio.sleep(3600)


def test_blogger_text_does_not_block_menu(cfg, conn):
    tg = TG()
    adm = Admin(cfg, conn, tg, 7, None, None, SlowLLM(), Bloggers(cfg, conn), None, None)

    async def run():
        msg = {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "#SOL long\nEntry 145\nStop 136\nTarget 160"}
        await asyncio.wait_for(adm.on_message(msg), 2)           # ответ «Читаю» сразу, разбор в фоне
        assert "Читаю" in tg.sent[-1]
        await asyncio.wait_for(adm.on_message({**msg, "text": "/menu"}), 2)
        assert len(tg.sent) == 2                                  # меню ответило, пока нейросеть «думает»
    asyncio.run(run())


def test_parse_text_without_llm():
    d = parse_text("#SOL лонг\nВход: 142,5 - 145\nСтоп: 136\nЦели: 152, 160, 171.")
    assert d["symbol"] == "SOL" and d["bias"] == "long"
    assert [x["price"] for x in d["levels"] if x["type"] == "target"] == [152, 160, 171]
    assert parse_text("Рынок сегодня вялый") is None


class FailLLM:
    last_error = "Gemini недоступен из твоей страны: включи VPN"

    async def extract_call(self, text=None, image=None):
        return None


def test_fallback_parser_when_llm_fails(cfg, conn):
    tg = TG()
    adm = Admin(cfg, conn, tg, 7, None, None, FailLLM(), Bloggers(cfg, conn), None, None)

    async def run():
        await adm.on_message({"chat": {"id": 7, "type": "private"}, "from": {"id": 7},
                              "text": "BTC short\nEntry 71200\nSL 72800\nTP1 69000"})
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert "Разобрал без нейросети" in tg.sent[-1] and "VPN" in tg.sent[-1]


def test_every_menu_button_answers_in_russian(cfg, conn):
    import re

    class TG2(TG):
        async def answer_callback(self, *a, **kw):
            return {}

        async def edit_text(self, chat, mid, text, markup=None):
            self.sent.append(text)

        async def send_document(self, *a, **kw):
            return {}

    class Live:
        def open_trades(self):
            return []

    tg = TG2()
    adm = Admin(cfg, conn, tg, 7, None, Live(), FailLLM(), Bloggers(cfg, conn), None, None)
    text, markup = adm.menu()
    assert "Режим публикации" in text
    datas = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    skip = {"selfcheck", "testposts:ch", "testposts:me", "export", "phone", "phone_new", "snaps", "demo"}       # ходят в сеть
    sub = ["strat", "tg:portfolio.enabled", "thr:0.02", "risk", "rk:risk_max_pct:0.1", "sched", "sch:1", "mode", "setmode:confirm"]

    async def run():
        for d in [x for x in datas if x not in skip] + sub:
            n = len(tg.sent)
            await adm.on_callback({"id": "1", "data": d, "message": {"chat": {"id": 7}, "message_id": 1}})
            if d not in ("pause", "kill", "noop") and len(tg.sent) > n:
                assert re.search("[а-яА-Я]", tg.sent[-1]), d
    asyncio.run(run())
    assert adm.eff()["portfolio"]["enabled"] is False and adm.eff()["publishing"]["mode"] == "confirm"


def test_phone_screen_lists_links(cfg, conn, monkeypatch):
    from sigbot import phone
    from sigbot.admin import Admin

    async def start(http, port, wait=40):
        return "https://abc.trycloudflare.com"

    async def check(url, tok, tries=12):
        return ("127.0.0.1" in url), "ClientConnectorError: нет связи"
    monkeypatch.setattr(phone, "start", start)
    monkeypatch.setattr(phone, "check", check)
    monkeypatch.setattr(phone, "lan_ips", lambda: ["192.168.1.5"])

    async def lhr(port, wait=30):
        return "https://zz.lhr.life"
    monkeypatch.setattr(phone, "start_lhr", lhr)
    cfg["dashboard"]["host"] = "0.0.0.0"
    adm = Admin(cfg, conn, None, 7, None, None, None, None, None, None)
    text, _ = asyncio.run(adm.phone_screen())
    tok = phone.token(conn)
    assert f"https://abc.trycloudflare.com/?token={tok}" in text and f"http://192.168.1.5:8787/?token={tok}" in text
    assert "allow_phone.bat" in text and "не открывается" in text and "нет связи" in text and "VPN" not in text
    assert f"https://zz.lhr.life/?token={tok}" in text
