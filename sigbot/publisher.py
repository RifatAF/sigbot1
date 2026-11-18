"""Публикация в канал. Режимы: paper и auto публикуют сразу (paper с пометкой #forwardtest),
confirm отправляет пост владельцу с кнопками. Пауза и kill switch останавливают всё.
Каждый пост перед отправкой сверяется с числами движка."""
from __future__ import annotations

import logging
import time

from . import settings
from .telegram import Telegram, kb
from .textguard import NumberMismatch, verify

log = logging.getLogger(__name__)


class Publisher:
    def __init__(self, cfg, conn, tg: Telegram | None, channel_id, owner_id, leader=None):
        self.cfg, self.conn, self.tg = cfg, conn, tg
        self.channel_id, self.owner_id = channel_id, owner_id
        self.leader = leader          # не основной экземпляр пишет только в свой журнал

    def _standby(self) -> bool:
        return self.leader is not None and not self.leader.is_main

    def _eff(self):
        return settings.effective(self.cfg, self.conn)["publishing"]

    def _log(self, kind, text, status, signal_id=None, message_id=None, detail=None, photo=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO posts (ts, kind, signal_id, status, text, photo, message_id, detail) VALUES (?,?,?,?,?,?,?,?)",
            (int(time.time() * 1000), kind, signal_id, status, text, photo, message_id, detail))
        return cur.lastrowid

    async def publish(self, kind: str, text: str, engine_numbers: list[float], signal_id: int | None = None,
                      png: bytes | None = None) -> str:
        """Возвращает статус: sent | pending | blocked | paused | killed | no_telegram."""
        try:
            verify(text, engine_numbers)
        except NumberMismatch as e:
            self._log(kind, text, "blocked", signal_id, detail=str(e))
            log.error("post blocked: %s", e)
            await self.notify_owner(f"⛔ Post blocked by number check: {e}")
            return "blocked"

        if self._standby():
            self._log(kind, text, "standby", signal_id)
            return "standby"
        p = self._eff()
        if p.get("kill_switch"):
            self._log(kind, text, "killed", signal_id)
            return "killed"
        if p.get("paused"):
            self._log(kind, text, "paused", signal_id)
            return "paused"
        if not self.tg or not self.channel_id:
            self._log(kind, text, "no_telegram", signal_id)
            return "no_telegram"

        if p.get("mode") == "confirm":
            pid = self._log(kind, text, "pending", signal_id, photo="1" if png else None)
            if png:
                settings.put(self.conn, f"pending_png_{pid}", png.hex())
            markup = kb([[("✅ Publish", f"pub:{pid}"), ("✖️ Drop", f"drop:{pid}")]])
            if png:
                await self.tg.send_photo(self.owner_id, png, text, reply_markup=markup)
            else:
                await self.tg.send_message(self.owner_id, text, reply_markup=markup)
            return "pending"

        mid = await self._send(text, png)
        self._log(kind, text, "sent", signal_id, message_id=mid)
        return "sent"

    async def _send(self, text: str, png: bytes | None) -> int:
        if png and len(text) <= 1024:
            r = await self.tg.send_photo(self.channel_id, png, text)
        else:
            if png:
                await self.tg.send_photo(self.channel_id, png, "")
            r = await self.tg.send_message(self.channel_id, text)
        return r["message_id"]

    async def resolve_pending(self, post_id: int, approve: bool) -> str:
        row = self.conn.execute("SELECT * FROM posts WHERE id=? AND status='pending'", (post_id,)).fetchone()
        if not row:
            return "not found"
        hexpng = settings.get(self.conn, f"pending_png_{post_id}")
        png = bytes.fromhex(hexpng) if hexpng else None
        if approve:
            if self._eff().get("kill_switch"):
                return "kill switch on"
            self.conn.execute("UPDATE posts SET status='sending' WHERE id=?", (post_id,))
            mid = await self._send(row["text"], png)
            self.conn.execute("UPDATE posts SET status='sent', message_id=?, detail='approved' WHERE id=?",
                              (mid, post_id))
            settings.put(self.conn, f"pending_png_{post_id}", None)
            return "published"
        self.conn.execute("UPDATE posts SET status='dropped' WHERE id=?", (post_id,))
        settings.put(self.conn, f"pending_png_{post_id}", None)
        return "dropped"

    async def notify_owner(self, text: str):
        if self.tg and self.owner_id and not self._standby():
            try:
                await self.tg.send_message(self.owner_id, text, parse_mode=None)
            except Exception as e:
                log.warning("owner notify failed: %s", e)
