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
    skip = {"selfcheck", "testposts:ch", "testposts:me", "export", "phone", "phone_new", "snaps"}       # ходят в сеть
    sub = ["strat", "tg:portfolio.enabled", "thr:0.02", "risk", "rk:risk_max_pct:0.1", "sched", "sch:1", "mode", "setmode:confirm"]

    async def run():
        for d in [x for x in datas if x not in skip] + sub:
            n = len(tg.sent)
            await adm.on_callback({"id": "1", "data": d, "message": {"chat": {"id": 7}, "message_id": 1}})
            if d not in ("pause", "kill", "noop") and len(tg.sent) > n:
                assert re.search("[а-яА-Я]", tg.sent[-1]), d
    asyncio.run(run())
    assert adm.eff()["portfolio"]["enabled"] is False and adm.eff()["publishing"]["mode"] == "confirm"


def test_phone_screen_tries_ways_until_one_opens(cfg, conn, monkeypatch):
    """Порт Cloudflare закрыт: бот его пропускает, Pinggy через 443 работает, ссылку даёт он; localhost.run не трогает."""
    from sigbot import phone
    from sigbot.admin import Admin
    phone.stop()
    started = []

    async def diagnose():
        return {"cloudflare": False, "pinggy": True, "localhost.run": True, "ngrok": True}

    async def start_ssh(name, port, wait=25):
        started.append(name)
        return "https://ab-12.a.free.pinggy.link" if name == "pinggy" else None

    async def check(url, tok, tries=12):
        return True, ""
    monkeypatch.setattr(phone, "diagnose", diagnose)
    monkeypatch.setattr(phone, "start_ssh", start_ssh)
    monkeypatch.setattr(phone, "check", check)
    monkeypatch.setattr(phone, "lan_ips", lambda: ["192.168.1.5"])
    cfg["dashboard"]["host"] = "0.0.0.0"
    adm = Admin(cfg, conn, None, 7, None, None, None, None, None, None)
    text, _ = asyncio.run(adm.phone_screen())
    tok = phone.token(conn)
    assert f"https://ab-12.a.free.pinggy.link/?token={tok}" in text and "через pinggy" in text
    assert started == ["pinggy"] and "cloudflare ⛔" in text and "/ngrok" in text
    assert f"http://192.168.1.5:8787/?token={tok}" in text and "VPN" not in text
    phone.stop()


def test_ngrok_command_saves_token_and_goes_first(cfg, conn, monkeypatch):
    from sigbot import phone, settings
    phone.stop()
    settings.put(conn, "ngrok_token", "tok_abc")
    phone.STATE["conn"] = conn

    async def diagnose():
        return {"cloudflare": True, "pinggy": True, "localhost.run": True, "ngrok": True}

    async def start_ngrok(http, port, token, domain=None, wait=25):
        assert token == "tok_abc"
        return "https://calm-fox.ngrok-free.app"

    async def check(url, tok, tries=12):
        return True, ""
    monkeypatch.setattr(phone, "diagnose", diagnose)
    monkeypatch.setattr(phone, "start_ngrok", start_ngrok)
    monkeypatch.setattr(phone, "check", check)
    url, how, _ = asyncio.run(phone.best(None, 8787, "k" * 20))
    assert (url, how) == ("https://calm-fox.ngrok-free.app", "ngrok")
    phone.stop()


def test_strategies_screen_shows_btc_layers(cfg, conn):
    from sigbot import settings
    from sigbot.admin import Admin
    from sigbot.bloggers import Bloggers
    adm = Admin(cfg, conn, None, 7, None, None, None, Bloggers(cfg, conn), None, None)
    text, _ = adm.strat_screen()
    assert "BTC Layers" in text and "ещё не было" in text
    settings.put(conn, "btc_layers_state", {"signal": -0.9, "date": "2026-11-16"})
    assert "шорт 90% депозита" in adm.strat_screen()[0]


def test_layers_screen_explains_position(cfg, conn):
    from sigbot import settings
    from sigbot.admin import Admin
    from sigbot.bloggers import Bloggers
    adm = Admin(cfg, conn, None, 7, None, None, None, Bloggers(cfg, conn), None, None)
    assert "Пересчитать" in str(adm.layers_screen()[1])
    settings.put(conn, "btc_layers_state", {"signal": -0.9, "date": "2026-11-16", "btc": 64856.0, "macro": -0.61, "regime": -1,
                                            "trend": -1.0, "vol": 0.44, "side": -1.0, "vt": 0.9, "parts": {"gas": 0.19}})
    t = adm.layers_screen()[0]
    assert "ШОРТ 90% депозита" in t and "маржа при x5: 18$" in t and "доля -1.0 × размер 0.90" in t
