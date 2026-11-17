"""HTTP с повторами, бэкоффом и учётом лимитов."""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

log = logging.getLogger(__name__)


class RateLimiter:
    """Простое окно: не больше `capacity` единиц веса за `period` секунд."""

    def __init__(self, capacity: int, period: float = 60.0):
        self.capacity, self.period = capacity, period
        self.events: list[tuple[float, int]] = []
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int = 1):
        async with self._lock:
            while True:
                now = time.monotonic()
                self.events = [(t, w) for t, w in self.events if now - t < self.period]
                used = sum(w for _, w in self.events)
                if used + weight <= self.capacity:
                    self.events.append((now, weight))
                    return
                await asyncio.sleep(self.period - (now - self.events[0][0]) + 0.05)


class Http:
    def __init__(self, session: aiohttp.ClientSession | None = None, retries: int = 5):
        self._session = session
        self.retries = retries

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_json(self, url: str, params: dict | None = None, headers: dict | None = None):
        return await self._request("GET", url, params=params, headers=headers)

    async def post_json(self, url: str, json: dict | None = None, data=None, headers: dict | None = None,
                        timeout: float | None = None):
        return await self._request("POST", url, json=json, data=data, headers=headers, timeout=timeout)

    async def get_bytes(self, url: str) -> bytes | None:
        s = await self.session()
        for attempt in range(self.retries):
            try:
                async with s.get(url) as r:
                    if r.status == 404:
                        return None
                    r.raise_for_status()
                    return await r.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                await asyncio.sleep(min(2 ** attempt, 30))
                last = e
        raise RuntimeError(f"GET {url} failed: {last}")

    async def _request(self, method, url, timeout=None, **kw):
        s = await self.session()
        delay = 1.0
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                to = aiohttp.ClientTimeout(total=timeout) if timeout else None
                async with s.request(method, url, timeout=to, **kw) as r:
                    if r.status in (418, 429):
                        wait = float(r.headers.get("Retry-After", 60))
                        log.warning("rate limited %s, sleep %ss", url, wait)
                        await asyncio.sleep(wait)
                        continue
                    if r.status >= 500:
                        raise aiohttp.ClientResponseError(r.request_info, (), status=r.status)
                    data = await r.json(content_type=None)
                    if r.status >= 400:
                        raise HttpError(r.status, data)
                    return data
            except HttpError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last = e
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
        raise RuntimeError(f"{method} {url} failed after {self.retries} tries: {last!r}")


class HttpError(Exception):
    def __init__(self, status: int, data):
        super().__init__(f"HTTP {status}: {str(data)[:300]}")
        self.status, self.data = status, data
