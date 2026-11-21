"""Минимальный клиент Telegram Bot API."""
from __future__ import annotations

import json

import aiohttp

from .http import Http, HttpError


class Telegram:
    def __init__(self, token: str, http: Http):
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.http = http

    async def call(self, method: str, **params):
        payload = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                   for k, v in params.items() if v is not None}
        d = await self.http.post_json(f"{self.base}/{method}", data=payload, timeout=70)
        if not d.get("ok"):
            raise HttpError(400, d)
        return d["result"]

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode="HTML", **kw):
        return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode,
                               reply_markup=reply_markup, disable_web_page_preview="true", **kw)

    async def send_photo(self, chat_id, png: bytes, caption: str, reply_markup=None, parse_mode="HTML"):
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption)
        form.add_field("parse_mode", parse_mode)
        if reply_markup:
            form.add_field("reply_markup", json.dumps(reply_markup))
        form.add_field("photo", png, filename="chart.png", content_type="image/png")
        d = await self.http.post_json(f"{self.base}/sendPhoto", data=form, timeout=60)
        if not d.get("ok"):
            raise HttpError(400, d)
        return d["result"]

    async def edit_text(self, chat_id, message_id, text, reply_markup=None):
        return await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                               parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview="true")

    async def answer_callback(self, cq_id, text=None):
        return await self.call("answerCallbackQuery", callback_query_id=cq_id, text=text)

    async def pin(self, chat_id, message_id):
        return await self.call("pinChatMessage", chat_id=chat_id, message_id=message_id,
                               disable_notification="true")

    async def get_updates(self, offset: int | None, timeout: int = 50):
        return await self.call("getUpdates", offset=offset, timeout=timeout,
                               allowed_updates=["message", "callback_query"])

    async def download_file(self, file_id: str) -> bytes:
        f = await self.call("getFile", file_id=file_id)
        return await self.http.get_bytes(f"{self.file_base}/{f['file_path']}")

    async def send_document(self, chat_id, data: bytes, filename: str, caption: str = ""):
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption)
        form.add_field("document", data, filename=filename)
        return await self.http.post_json(f"{self.base}/sendDocument", data=form, timeout=60)


def kb(rows: list[list[tuple[str, str]]]) -> dict:
    """[[(текст, callback_data)]] -> inline клавиатура."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}
