"""Gemini: только текст комментариев и чтение скриншотов. Торговых решений модель не принимает."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re

from .http import Http
from .textguard import llm_text_has_no_digits

log = logging.getLogger(__name__)
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

COMMENT_SYSTEM = (
    "You write one or two short sentences of plain English commentary for a crypto signals channel. "
    "Use ONLY the facts given. Do not write any digits or numbers at all, not even spelled out prices. "
    "No hype, no emojis, no predictions, no advice, no emotions. Neutral, factual tone."
)

EXTRACT_SYSTEM = (
    "Extract a crypto trading call from the user's text or screenshot. Return JSON only with keys: "
    "author (string or null), symbol (ticker like BTC, or null), bias (long|short|neutral|null), "
    "levels (list of {price: number, type: support|resistance|entry|stop|target|liquidity|other}), "
    "conditions (short string or null), date (ISO date or null), source (url or null). "
    "Do not invent anything that is not visible."
)


class Gemini:
    def __init__(self, api_key: str | None, http: Http, model: str, fallback_model: str, timeout_s: int = 60):
        self.key, self.http = api_key, http
        self.models = [model, fallback_model]
        self.timeout = timeout_s
        self.last_error: str | None = None if api_key else "нет ключа GEMINI_API_KEY в .env"

    async def _gen(self, parts: list, system: str, json_mode: bool = False) -> str | None:
        if not self.key:
            self.last_error = "нет ключа GEMINI_API_KEY в .env"
            return None
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"temperature": 0.3}}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        self.last_error = None
        for m in self.models:
            try:   # жёсткий срок на модель: без него повторы http тянулись до 10 минут и бот «зависал»
                d = await asyncio.wait_for(self.http.post_json(URL.format(model=m), json=body, timeout=min(self.timeout, 40),
                                                               headers={"x-goog-api-key": self.key}), timeout=45)
                return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"]).strip()
            except asyncio.TimeoutError:
                self.last_error = "Gemini не ответил за 45 секунд (из России Google API часто закрыт: нужен VPN)"
                log.warning("gemini %s timeout", m)
            except Exception as e:
                msg = str(e)
                self.last_error = ("Gemini недоступен из твоей страны: включи VPN" if "location" in msg.lower()
                                   else "ключ Gemini не принят: проверь GEMINI_API_KEY" if "API key" in msg or "403" in msg
                                   else f"Gemini ошибка: {msg[:120]}")
                log.warning("gemini %s failed: %s", m, e)
        return None

    async def comment(self, facts: dict) -> str:
        """Комментарий без цифр. Если модель вставила цифры или недоступна, комментария нет."""
        txt = await self._gen([{"text": json.dumps(facts)}], COMMENT_SYSTEM)
        if not txt:
            return ""
        txt = re.sub(r"\s+", " ", txt).strip()
        if not llm_text_has_no_digits(txt):
            log.warning("llm comment dropped: contains digits: %r", txt)
            return ""
        return txt[:400]

    async def extract_call(self, text: str | None = None, image: bytes | None = None,
                           mime: str = "image/jpeg") -> dict | None:
        parts = []
        if text:
            parts.append({"text": text})
        if image:
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}})
        raw = await self._gen(parts, EXTRACT_SYSTEM, json_mode=True)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data[0] if isinstance(data, list) else data
        except json.JSONDecodeError:
            log.warning("bad extract json: %r", raw[:200])
            return None
