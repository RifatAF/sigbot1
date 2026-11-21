"""PNG сигнала: свечи, зоны риска и прибыли, уровни, плитки с числами. Рендер через headless Chromium.

Картинку смотрят в Telegram на телефоне, поэтому: крупный шрифт, мало подписей на графике,
все числа продублированы плитками внизу, цвет зон однозначен (красный риск, зелёный цель)."""
from __future__ import annotations

import json
import os
from html import escape
from pathlib import Path

LWC = (Path(__file__).parent / "static" / "lightweight-charts.js").read_text()
W, H = 1200, 675

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}
html,body{margin:0;width:%(w)dpx;height:%(h)dpx;background:#0d1017;color:#e8ebf1;
  font-family:Inter,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{display:flex;flex-direction:column;height:100%%;padding:22px 26px 20px}
.head{display:flex;align-items:center;gap:14px;height:52px}
.coin{font-size:34px;font-weight:700;letter-spacing:.3px}
.pill{font-size:20px;font-weight:700;padding:5px 14px;border-radius:8px;letter-spacing:1px}
.long{background:#123d2e;color:#3ddc97}.short{background:#45161b;color:#ff6b6b}
.meta{margin-left:auto;font-size:18px;color:#8a93a6;text-align:right;line-height:1.35}
.meta b{color:#e8ebf1;font-weight:600}
#c{flex:1;margin:10px 0 14px;border-radius:10px;overflow:hidden}
.tiles{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;height:86px}
.t{background:#151a24;border-radius:10px;padding:10px 14px;border-left:4px solid #2b3345}
.t span{display:block;font-size:15px;color:#8a93a6;text-transform:uppercase;letter-spacing:.8px}
.t b{display:block;font-size:26px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}
.e{border-color:#5b8cff}.s{border-color:#ff6b6b}.g{border-color:#3ddc97}.n{border-color:#2b3345}
.foot{display:flex;justify-content:space-between;margin-top:10px;font-size:14px;color:#5d667a}
</style></head><body><div class="wrap">
<div class="head"><div class="coin">%(coin)s</div><div class="pill %(side)s">%(SIDE)s</div>
<div class="meta">%(meta)s</div></div>
<div id="c"></div>
<div class="tiles">%(tiles)s</div>
<div class="foot"><div>%(legend)s</div><div>1h · USDT perpetual</div></div>
</div>
<script>%(lwc)s</script>
<script>
const d = %(data)s;
const el = document.getElementById('c');
const chart = LightweightCharts.createChart(el, {width: el.clientWidth, height: el.clientHeight,
  localization:{priceFormatter: p => p.toLocaleString('en-US', {minimumFractionDigits:d.axisPrec, maximumFractionDigits:d.axisPrec})},
  layout:{background:{color:'#0d1017'}, textColor:'#9aa3b5', fontSize:15,
          fontFamily:'Inter, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif'},
  grid:{vertLines:{visible:false}, horzLines:{color:'#171c27'}},
  rightPriceScale:{borderVisible:false, scaleMargins:{top:0.08,bottom:0.08}},
  timeScale:{borderVisible:false, timeVisible:true, rightOffset:0},
  crosshair:{mode:2}, handleScroll:false, handleScale:false});

// зоны: цель (вход..TP2) и риск (вход..стоп) как заливка между двумя уровнями
const band = (top, bottom, color) => {
  const b = chart.addBaselineSeries({baseValue:{type:'price', price:bottom}, lineVisible:false,
    topFillColor1:color, topFillColor2:color, bottomFillColor1:color, bottomFillColor2:color,
    lastValueVisible:false, priceLineVisible:false, crosshairMarkerVisible:false});
  b.setData(d.span.map(t => ({time:t, value:top})));
};
band(d.lv.tp2, d.lv.entry, 'rgba(61,220,151,0.20)');
band(d.lv.stop, d.lv.entry, 'rgba(255,107,107,0.24)');
for (const z of d.zones) band(z.high, z.low, 'rgba(168,140,255,0.30)');

const s = chart.addCandlestickSeries({upColor:'#3ddc97', downColor:'#ff6b6b', borderVisible:false,
  wickUpColor:'#3ddc97', wickDownColor:'#ff6b6b', lastValueVisible:false, priceLineVisible:false,
  priceFormat:{type:'price', precision:d.axisPrec, minMove:Math.pow(10, -d.axisPrec)},
  autoscaleInfoProvider: orig => { const r = orig(); if (!r) return r;
    r.priceRange.minValue = Math.min(r.priceRange.minValue, ...d.fit);
    r.priceRange.maxValue = Math.max(r.priceRange.maxValue, ...d.fit); return r; }});
s.setData(d.candles);
for (const l of d.lines) s.createPriceLine({price:l.price, color:l.color, lineWidth:l.width,
  lineStyle:l.style, axisLabelVisible:l.label, title:l.title||''});
s.setMarkers([{time:d.entryTime, position: d.side==='long'?'belowBar':'aboveBar',
  color:'#5b8cff', shape: d.side==='long'?'arrowUp':'arrowDown', size:2}]);
chart.timeScale().fitContent();
window.__done = true;
</script></body></html>"""


def _prec(price: float) -> int:
    a = abs(price)
    return 2 if a >= 100 else 3 if a >= 10 else 4 if a >= 1 else 5 if a >= 0.1 else 6 if a >= 0.01 else 8


def _fmt(x: float, prec: int) -> str:
    return f"{x:,.{prec}f}"


def build_html(symbol: str, side: str, candles: list[dict], levels: dict, zones: list[dict] | None = None,
               liq: dict | None = None, markers: list | None = None, subtitle: str = "",
               w: int = W, h: int = H, risk_pct: float | None = None, leverage: float | None = None) -> str:
    prec = _prec(levels["entry"])
    entry, stop, tp1, tp2 = (levels[k] for k in ("entry", "stop", "tp1", "tp2"))
    r = abs(entry - stop) or 1
    lines = [
        {"price": entry, "color": "#5b8cff", "width": 2, "style": 0, "label": True, "title": "Entry"},
        {"price": stop, "color": "#ff6b6b", "width": 2, "style": 0, "label": True, "title": "Stop"},
        {"price": tp1, "color": "#3ddc97", "width": 2, "style": 2, "label": True, "title": "TP1"},
        {"price": tp2, "color": "#3ddc97", "width": 2, "style": 0, "label": True, "title": "TP2"},
    ]
    # кластеры ликвидаций: тонкие линии без подписей на оси, не больше двух, только рядом с графиком
    lo, hi = min(stop, tp2), max(stop, tp2)
    span_r = (hi - lo) * 0.5
    liq_lines = 0
    for key in ("short_liq_above", "long_liq_below"):
        for price, _usd in (liq or {}).get(key, [])[:1]:
            if lo - span_r <= price <= hi + span_r:
                lines.append({"price": price, "color": "rgba(255,183,77,0.75)", "width": 1, "style": 1,
                              "label": False, "title": "liq"})
                liq_lines += 1

    step = (candles[1]["time"] - candles[0]["time"]) if len(candles) > 1 else 3600
    last = candles[-1]["time"] if candles else 0
    future = [last + step * i for i in range(1, 25)]
    span = [last] + future                               # зоны только от входа вправо
    candles = candles + [{"time": t} for t in future]   # место справа для зон

    tiles = [
        ("Entry", _fmt(entry, prec), "e"),
        ("Stop", _fmt(stop, prec), "s"),
        ("TP1", _fmt(tp1, prec), "g"),
        ("TP2", _fmt(tp2, prec), "g"),
        ("Risk", f"{risk_pct:.2f}%" if risk_pct is not None else "—", "n"),
        # «плечо» здесь было отношением позиции к депозиту (x0.3), такого плеча на бирже нет: показываем размер позиции
        ("Size", f"{leverage * 100:.0f}% of capital" if leverage is not None else "—", "n"),
    ]
    tiles_html = "".join(f'<div class="t {c}"><span>{escape(k)}</span><b>{escape(v)}</b></div>' for k, v, c in tiles)
    rr = abs(tp2 - entry) / r
    meta = f"Reward/risk <b>{rr:.1f}</b>"
    if subtitle:
        meta += f"<br>{escape(subtitle)}"
    legend = ("<span style='color:#3ddc97'>■</span> target zone &nbsp; "
              "<span style='color:#ff6b6b'>■</span> risk zone")
    if zones:
        legend += " &nbsp; <span style='color:#a88cff'>■</span> analyst zones"
    if liq_lines:
        legend += " &nbsp; <span style='color:#ffb74d'>┄</span> est. liquidations"

    axis_prec = 0 if abs(entry) >= 1000 else 1 if abs(entry) >= 100 else prec
    data = {"candles": candles, "lines": lines, "span": span, "zones": zones or [], "axisPrec": axis_prec,
            "lv": {"entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2}, "side": side,
            "fit": [entry, stop, tp1, tp2], "entryTime": last}
    return PAGE % {"w": w, "h": h, "coin": escape(symbol.replace("USDT", "")), "side": side,
                   "SIDE": side.upper(), "meta": meta, "tiles": tiles_html, "legend": legend,
                   "lwc": LWC, "data": json.dumps(data)}


def candles_from_df(df, n: int = 120) -> list[dict]:
    t = df.tail(n)
    return [{"time": int(r.ts // 1000), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
            for r in t.itertuples()]


async def render_png(html: str, w: int = W, h: int = H) -> bytes:
    from playwright.async_api import async_playwright
    exe = os.environ.get("CHROMIUM_PATH") or "/opt/pw-browsers/chromium"
    async with async_playwright() as pw:
        kw = {"args": ["--no-sandbox"]}
        if Path(exe).exists():
            kw["executable_path"] = exe
        browser = await pw.chromium.launch(**kw)
        try:
            page = await browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=2)
            await page.set_content(html)
            await page.wait_for_function("window.__done === true", timeout=15000)
            await page.wait_for_timeout(200)
            return await page.screenshot(type="png")
        finally:
            await browser.close()
