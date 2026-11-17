"""PNG сигнала: свечи, вход, стоп, цели, зоны. Рендер сервером через headless Chromium."""
from __future__ import annotations

import json
import os
from pathlib import Path

LWC = (Path(__file__).parent / "static" / "lightweight-charts.js").read_text()

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>html,body{margin:0;background:#0f1115;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
#t{position:absolute;left:12px;top:8px;background:rgba(15,17,21,.85);padding:2px 6px;border-radius:4px;color:#e6e6e6;font-size:20px;font-weight:600;z-index:5}
#s{position:absolute;left:12px;top:38px;background:rgba(15,17,21,.85);padding:2px 6px;border-radius:4px;color:#8b93a1;font-size:13px;z-index:5}
#c{width:%(w)dpx;height:%(h)dpx}</style></head><body>
<div id="t">%(title)s</div><div id="s">%(sub)s</div><div id="c"></div>
<script>%(lwc)s</script>
<script>
const d = %(data)s;
const chart = LightweightCharts.createChart(document.getElementById('c'), {
  width:%(w)d, height:%(h)d,
  layout:{background:{color:'#0f1115'}, textColor:'#c9ced6'},
  grid:{vertLines:{color:'#1b1f27'}, horzLines:{color:'#1b1f27'}},
  rightPriceScale:{borderColor:'#2a2f3a', scaleMargins:{top:0.15,bottom:0.08}},
  timeScale:{borderColor:'#2a2f3a', timeVisible:true}
});
const s = chart.addCandlestickSeries({upColor:'#26a69a', downColor:'#ef5350', borderVisible:false,
  wickUpColor:'#26a69a', wickDownColor:'#ef5350', lastValueVisible:false, priceLineVisible:false,
  autoscaleInfoProvider: orig => { const r = orig(); if (!r) return r;
    const ps = d.lines.filter(l => l.fit).map(l => l.price);
    r.priceRange.minValue = Math.min(r.priceRange.minValue, ...ps);
    r.priceRange.maxValue = Math.max(r.priceRange.maxValue, ...ps); return r; }});
s.setData(d.candles);
for (const l of d.lines) s.createPriceLine({price:l.price, color:l.color, lineWidth:l.width||2,
  lineStyle:l.style||0, axisLabelVisible:true, title:l.title});
if (d.markers) s.setMarkers(d.markers);
chart.timeScale().fitContent();
window.__done = true;
</script></body></html>"""


def build_html(symbol: str, side: str, candles: list[dict], levels: dict, zones: list[dict] | None = None,
               liq: dict | None = None, markers: list | None = None, subtitle: str = "",
               w: int = 1000, h: int = 560) -> str:
    lines = [
        {"price": levels["entry"], "color": "#4c8bf5", "title": "Entry", "fit": True},
        {"price": levels["stop"], "color": "#ef5350", "title": "Stop", "fit": True},
        {"price": levels["tp1"], "color": "#26a69a", "title": "TP1", "style": 2, "fit": True},
        {"price": levels["tp2"], "color": "#26a69a", "title": "TP2", "fit": True},
    ]
    for z in zones or []:
        for edge in ("low", "high"):
            lines.append({"price": z[edge], "color": "#b39ddb", "title": "zone", "style": 1, "width": 1})
    for key, title in (("long_liq_below", "liq L"), ("short_liq_above", "liq S")):
        for price, _usd in (liq or {}).get(key, [])[:2]:
            lines.append({"price": price, "color": "#ffb74d", "title": title, "style": 3, "width": 1})
    data = {"candles": candles, "lines": lines, "markers": markers or []}
    title = f"{symbol.replace('USDT', '')} {side.upper()}"
    return PAGE % {"w": w, "h": h, "title": title, "sub": subtitle, "lwc": LWC, "data": json.dumps(data)}


def candles_from_df(df, n: int = 120) -> list[dict]:
    t = df.tail(n)
    return [{"time": int(r.ts // 1000), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
            for r in t.itertuples()]


async def render_png(html: str, w: int = 1000, h: int = 560) -> bytes:
    from playwright.async_api import async_playwright
    exe = os.environ.get("CHROMIUM_PATH") or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
    async with async_playwright() as pw:
        kw = {"args": ["--no-sandbox"]}
        if exe and Path(exe).is_file():
            kw["executable_path"] = exe
        browser = await pw.chromium.launch(**kw)
        try:
            page = await browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=2)
            await page.set_content(html)
            await page.wait_for_function("window.__done === true", timeout=15000)
            await page.wait_for_timeout(150)
            return await page.screenshot(type="png")
        finally:
            await browser.close()
