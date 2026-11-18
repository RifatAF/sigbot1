"""Кто основной: два компьютера с одним ботом.

Основной экземпляр закрепляет в личке владельца сообщение «sigbot main: <имя>» и раз в 5 минут
закрепляет свежее (старое удаляет), без звука. Остальные читают закреп через getChat (это не мешает опросу getUpdates
основного) и молчат. Переключение руками: команда /main <имя>. Автоматически: если закреп не
обновлялся 15 минут, запасной забирает роль себе и пишет об этом владельцу."""
from __future__ import annotations

import asyncio
import logging
import re
import time

log = logging.getLogger(__name__)
TAG = "sigbot main:"
RE = re.compile(r"sigbot main:\s*(\S+)(?:.*?alive (\d{10}))?", re.S)


class Leader:
    def __init__(self, tg, owner_id, name: str, stale_s: int = 900, beat_s: int = 300, conn=None):
        self.conn = conn
        self.tg, self.owner, self.name = tg, owner_id, name
        self.stale_s, self.beat_s = stale_s, beat_s
        self.is_main = False
        self.msg_id = None
        self.last_beat = 0.0

    async def read(self) -> tuple[str | None, int, int | None]:
        chat = await self.tg.call("getChat", chat_id=self.owner)
        pm = chat.get("pinned_message") or {}
        m = RE.search(pm.get("text") or "")
        if not m:
            return None, 0, None
        ts = int(m.group(2) or pm.get("edit_date") or pm.get("date") or 0)
        return m.group(1), ts, pm.get("message_id")

    def _text(self) -> str:
        now = int(time.time())
        return (f"🖥 {TAG} {self.name}\nalive {now}\n"
                f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))} UTC. Switch: /main &lt;name&gt;")

    async def claim(self, name: str | None = None):
        """Закрепить основным name (по умолчанию себя)."""
        name = name or self.name
        now = int(time.time())
        text = f"🖥 {TAG} {name}\nalive {now}\n{time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))} UTC. Switch: /main &lt;name&gt;"
        msg = await self.tg.send_message(self.owner, text, disable_notification="true")
        await self.tg.pin(self.owner, msg["message_id"])
        if name == self.name:
            self.msg_id, self.last_beat = msg["message_id"], time.time()
        return msg["message_id"]

    async def tick(self) -> bool:
        """Один шаг: обновить роль. Возвращает True, если этот экземпляр основной."""
        main, ts, mid = await self.read()
        now = time.time()
        if main is None:
            await self.claim()
            main, ts, mid = await self.read()
        if main == self.name:
            if not self.is_main:
                log.info("leader: this instance (%s) is main", self.name)
            self.is_main, self.msg_id = True, mid
            if now - self.last_beat >= self.beat_s:
                # новое закреплённое сообщение вместо правки: getChat надёжно отдаёт самый свежий закреп
                await self.claim()
                if mid and mid != self.msg_id:
                    try:
                        await self.tg.call("deleteMessage", chat_id=self.owner, message_id=mid)
                    except Exception:
                        pass
        else:
            if self.is_main:
                log.info("leader: %s is main now, %s goes standby", main, self.name)
            self.is_main = False
            if ts and now - ts > self.stale_s:
                log.warning("leader: %s silent for %d min, %s takes over", main, (now - ts) // 60, self.name)
                await self.claim()
                self.is_main = True
                await self.tg.send_message(self.owner, f"⚠️ {main} молчит {int((now - ts) // 60)} мин, "
                                                       f"основным стал {self.name}.", parse_mode=None)
        if self.conn is not None:
            from . import settings
            settings.put(self.conn, "leader_name", f"{main or self.name}{' (this)' if self.is_main else ''}")
        return self.is_main

    async def loop(self):
        while True:
            try:
                await self.tick()
            except Exception as e:
                log.warning("leader tick: %s", e)
            await asyncio.sleep(60)
