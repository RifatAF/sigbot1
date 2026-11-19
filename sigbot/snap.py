"""Картинка сделки для Telegram: бот открывает свою же панель в невидимом браузере и снимает график с карточкой.
Так панель видна на телефоне без доступа к компьютеру по сети."""
from __future__ import annotations

import os
from pathlib import Path

import aiohttp

WHAT = {"tp1": "TP1", "tp2": "TP2", "sl": "стоп", "be": "безубыток", "trend": "тренд сломался", "time": "по времени",
        "delist": "делистинг", "invalid": "идея сломалась", "trail": "трейлинг", "manual": "вручную"}
KIND = {"trend": "🧭 Тренд", "capitulation": "⚡ Капитуляция", "listing": "🆕 Листинг"}


async def feed(port: int, source: str = "live") -> list[dict]:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{port}/api/feed?source={source}", timeout=aiohttp.ClientTimeout(total=60)) as r:
            return (await r.json()).get("items", [])


def _px(p) -> str:
    if p is None:
        return "—"
    a = abs(p)
    return f"{p:,.0f}" if a >= 1000 else f"{p:.2f}" if a >= 100 else f"{p:.3f}" if a >= 1 else f"{p:.4g}"


def _pct(t, p) -> str:
    v = ((p / t["entry"] - 1) * 100) * (1 if t["side"] == "long" else -1)
    return f"{v:+.1f}%"


def label(t: dict) -> str:
    res = t.get("result_pct")
    return (f"{'🟢' if t['side'] == 'long' else '🔴'} {t['symbol'].replace('USDT', '')} "
            f"{'лонг' if t['side'] == 'long' else 'шорт'}" + (f" {res:+.2f}%" if res is not None else ""))


def caption(t: dict) -> str:
    """Текст под картинкой: план и что произошло с каждым уровнем."""
    side = "ЛОНГ" if t["side"] == "long" else "ШОРТ"
    hit = {e["what"]: e for e in t.get("exits") or []}
    lines = [f"<b>{t['symbol'].replace('USDT', '')} · {side}</b> · {KIND.get(t['kind'], t['kind'])}"
             + (" · открыта" if t.get("open") else ""), f"Вход: <b>{_px(t['entry'])}</b>"]
    for k in ("tp1", "tp2"):
        if t.get(k) is not None:
            lines.append(f"🎯 {k.upper()}: {_px(t[k])} ({_pct(t, t[k])}) " + ("✅" if k in hit else "⏳" if t.get("open") else "—"))
    if t.get("stop") is not None:
        lines.append(f"🛑 Стоп: {_px(t['stop'])} ({_pct(t, t['stop'])})" + (" ❌" if "sl" in hit else ""))
    if t.get("open") and t.get("exit_level"):
        lines.append(f"〰️ Слом тренда: {_px(t['exit_level'])}")
    fin = [e for e in t.get("exits") or [] if e["what"] not in ("tp1", "tp2", "sl")]
    if fin:
        lines.append(f"Выход: {WHAT.get(fin[-1]['what'], fin[-1]['what'])} {_px(fin[-1]['price'])}")
    if t.get("result_pct") is not None:
        lines.append(f"{'Итог сейчас' if t.get('open') else 'Итог'}: <b>{t['result_pct']:+.2f}%</b> депозита")
    return "\n".join(lines)


async def render_layers(port: int) -> bytes:
    """Картинка страницы BTC Layers: позиция, бэктест с 2018 года, слои."""
    from playwright.async_api import async_playwright
    exe = os.environ.get("CHROMIUM_PATH") or "/opt/pw-browsers/chromium"
    async with async_playwright() as pw:
        kw = {"args": ["--no-sandbox"]}
        if Path(exe).exists():
            kw["executable_path"] = exe
        browser = await pw.chromium.launch(**kw)
        try:
            page = await browser.new_page(viewport={"width": 520, "height": 900}, device_scale_factor=2)
            await page.goto(f"http://127.0.0.1:{port}/layers")
            await page.wait_for_function("window.__ready === true", timeout=30000)
            await page.wait_for_timeout(300)
            return await page.locator("#shot").screenshot(type="png")
        finally:
            await browser.close()


async def render(port: int, source: str, symbol: str, entry_time: int) -> bytes:
    from playwright.async_api import async_playwright
    exe = os.environ.get("CHROMIUM_PATH") or "/opt/pw-browsers/chromium"
    async with async_playwright() as pw:
        kw = {"args": ["--no-sandbox"]}
        if Path(exe).exists():
            kw["executable_path"] = exe
        browser = await pw.chromium.launch(**kw)
        try:
            page = await browser.new_page(viewport={"width": 520, "height": 900}, device_scale_factor=2)
            await page.goto(f"http://127.0.0.1:{port}/?source={source}&shot=1&pick={symbol}:{entry_time}")
            await page.wait_for_function("window.__ready === true", timeout=30000)
            await page.wait_for_timeout(300)
            return await page.locator("#detail").screenshot(type="png")
        finally:
            await browser.close()
