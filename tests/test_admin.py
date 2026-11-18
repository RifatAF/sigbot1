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
