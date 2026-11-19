"""Панель на телефоне через облако: бот сам отправляет страницу и данные панели на сайт (Vercel).

Туннель и проброс портов не нужны: с домашнего компьютера идут только исходящие HTTPS запросы, как в браузере.
На телефоне панель открывается по ссылке с ключом (/menu → 📱 Панель на телефоне). Свечи телефон берёт прямо с биржи.
Данные лежат в облаке под хэшем ключа: без ключа их не прочитать и не перезаписать.

Сколько отправок: живые данные при новой сделке или событии (не чаще раза в 10 минут) и раз в час,
история и страница раз в сутки или после обновления бота. Это укладывается в бесплатный тариф хранилища."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

import aiohttp

from . import phone

log = logging.getLogger(__name__)
STATE: dict = {"last": {}, "sig": None, "err": None, "ok_ts": None}


def cc(cfg) -> dict:
    return cfg.get("cloud", {}) or {}


def link(cfg, conn) -> str | None:
    c = cc(cfg)
    if not c.get("enabled") or not c.get("url"):
        return None
    return c["url"].rstrip("/") + "/?t=" + phone.token(conn)


def signature(live: dict) -> str:
    """Что поменялось по сути: сделки, события по ним, бумажные сделки, позиция BTC Layers. Цены не считаются."""
    items = [(x.get("kind"), x.get("ref"), x.get("open"), len(x.get("exits") or [])) for x in (live.get("feed") or {}).get("items", [])]
    paper = sorted((k, tuple((t.get("status"), round(t.get("open_qty") or 0, 8)) for t in v))
                   for k, v in ((live.get("paper") or {}).get("trades") or {}).items())
    return hashlib.sha1(json.dumps([items, paper], default=str).encode()).hexdigest()


class Cloud:
    def __init__(self, cfg, conn):
        self.cfg, self.conn = cfg, conn
        self.local = f"http://127.0.0.1:{cfg['dashboard']['port']}"

    async def _get(self, s: aiohttp.ClientSession, path: str, as_text: bool = False):
        async with s.get(self.local + path, timeout=aiohttp.ClientTimeout(total=120)) as r:
            r.raise_for_status()
            return await (r.text() if as_text else r.json())

    async def push(self, s: aiohttp.ClientSession, key: str, obj) -> int:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        url = cc(self.cfg)["url"].rstrip("/") + "/api/push?k=" + key
        async with s.post(url, data=body, headers={"x-token": phone.token(self.conn), "content-type": "application/json"},
                          timeout=aiohttp.ClientTimeout(total=120)) as r:
            if r.status != 200:
                raise RuntimeError(f"{key}: {r.status} {(await r.text())[:120]}")
        STATE["last"][key] = time.time()
        STATE["ok_ts"], STATE["err"] = time.time(), None
        return len(body)

    async def live(self, s) -> dict:
        lay = await self._get(s, "/api/layers")
        return {"ts": int(time.time()), "feed": await self._get(s, "/api/feed?source=live"),
                "paper": await self._get(s, "/api/paper"), "status": await self._get(s, "/api/status"),
                "layers_paper": lay.get("paper")}

    async def daily(self, s) -> dict:
        lay = await self._get(s, "/api/layers")
        lay.pop("paper", None)
        bt = await self._get(s, "/api/feed?source=backtest")
        items = bt.get("items") or []
        while items and len(json.dumps(bt, separators=(",", ":"))) > 3_800_000:   # предел запроса Vercel 4.5 МБ
            items = items[len(items) // 5:]                                          # самые старые сделки истории убираются
            bt["items"] = items
        return {"ts": int(time.time()), "feed_bt": bt, "layers": lay}

    async def pages(self, s):
        for key, path in (("page_index", "/"), ("page_layers", "/layers")):
            await self.push(s, key, {"html": await self._get(s, path, as_text=True)})

    def _stamp(self) -> list:
        """Когда пора отправить историю заново: новый день, новый бэктест ленты или модели."""
        from pathlib import Path
        m = [int(time.time() // 86400)]
        for f in ("reports/feed.json", "reports/btc_layers.json"):
            p = Path(f)
            m.append(int(p.stat().st_mtime) if p.exists() else 0)
        return m

    async def loop(self, say=print, every_s: int = 60):
        if not cc(self.cfg).get("enabled") or not cc(self.cfg).get("url"):
            return
        await asyncio.sleep(20)                          # панель и данные успевают подняться
        first, stamp = True, None
        ev_gap, hour = float(cc(self.cfg).get("event_min_s", 600)), float(cc(self.cfg).get("refresh_s", 3600))
        async with aiohttp.ClientSession() as s:
            while True:
                try:
                    if first:
                        await self.pages(s)
                    st = self._stamp()
                    if st != stamp:
                        await self.push(s, "daily", await self.daily(s))
                        if not first and st[0] != (stamp or [0])[0]:
                            await self.pages(s)
                        stamp = st
                    lv = await self.live(s)
                    sig, since = signature(lv), time.time() - STATE["last"].get("live", 0)
                    if first or since >= hour or (sig != STATE["sig"] and since >= ev_gap):
                        await self.push(s, "live", lv)
                        STATE["sig"] = sig
                    if first:
                        say("✅ Панель в облаке: открой на телефоне ссылку из Telegram (/menu → 📱 Панель на телефоне)")
                except Exception as e:
                    STATE["err"] = str(e)[:200]
                    log.warning("cloud: %s", e)
                    if first:
                        say(f"⚠️ Панель в облаке: не отправилось ({str(e)[:120]}), повтор через минуту")
                        await asyncio.sleep(every_s)
                        continue
                first = False
                await asyncio.sleep(every_s)
