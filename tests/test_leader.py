import time

import pytest

from sigbot.leader import Leader


class FakeTG:
    def __init__(self):
        self.pinned = None
        self.msgs = {}
        self.sent = []

    async def call(self, method, **kw):
        if method == "deleteMessage":
            self.msgs.pop(kw["message_id"], None)
            return True
        return {"pinned_message": self.msgs.get(self.pinned)} if self.pinned else {}

    async def send_message(self, chat, text, **kw):
        mid = len(self.msgs) + 1
        self.msgs[mid] = {"message_id": mid, "text": text, "date": int(time.time())}
        self.sent.append(text)
        return {"message_id": mid}

    async def pin(self, chat, mid):
        self.pinned = mid

    async def edit_text(self, chat, mid, text, reply_markup=None):
        self.msgs[mid]["text"] = text


@pytest.mark.asyncio
async def test_two_instances_one_main_and_switch():
    tg = FakeTG()
    pc, nb = Leader(tg, 1, "pc"), Leader(tg, 1, "laptop")
    assert await pc.tick() is True                     # первый закрепил себя
    assert await nb.tick() is False                    # второй видит закреп и молчит
    await pc.claim("laptop")                           # команда /main laptop на основном
    assert await nb.tick() is True and await pc.tick() is False


@pytest.mark.asyncio
async def test_failover_when_main_silent():
    tg = FakeTG()
    pc, nb = Leader(tg, 1, "pc", stale_s=900), Leader(tg, 1, "laptop", stale_s=900)
    await pc.tick()
    old = int(time.time()) - 2000
    tg.msgs[tg.pinned]["text"] = f"🖥 sigbot main: pc\nalive {old}\n"
    assert await nb.tick() is True                     # pc молчит 33 минуты: laptop забирает роль
    assert "laptop" in tg.msgs[tg.pinned]["text"] and any("молчит" in t for t in tg.sent)
    assert await pc.tick() is False                    # вернувшийся pc видит свежий закреп laptop
