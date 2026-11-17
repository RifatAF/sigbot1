"""Веб панель: график с маркерами сделок из журнала, статистика, PNG сигналов.
Живые сделки и бэктест показываются раздельно. Наружу через Cloudflare Tunnel."""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

from aiohttp import web

from . import chart, features, settings

STATIC = Path(__file__).parent / "static"

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>sigbot</title><script src="static/lightweight-charts.js"></script>
<style>body{margin:0;background:#0f1115;color:#e6e6e6;font:14px -apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;flex-wrap:wrap}
select,button{background:#1b1f27;color:#e6e6e6;border:1px solid #2a2f3a;border-radius:6px;padding:6px 10px}
button.on{background:#4c8bf5;border-color:#4c8bf5}#c{height:60vh;margin:0 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;padding:16px}
.card{background:#151922;border:1px solid #232836;border-radius:8px;padding:10px}
.card b{display:block;font-size:20px;margin-top:4px}.muted{color:#8b93a1}
table{width:calc(100% - 32px);margin:0 16px 24px;border-collapse:collapse}td,th{padding:6px;border-bottom:1px solid #232836;text-align:left}
.bt{color:#b39ddb}.live{color:#4c8bf5}</style></head><body>
<header><b>sigbot</b><select id="sym"></select>
<button id="bLive" class="on">Live</button><button id="bBt">Backtest</button>
<span class="muted" id="note"></span></header>
<div id="c"></div><div class="grid" id="stats"></div>
<table id="tbl"><thead><tr><th>Source</th><th>Coin</th><th>Side</th><th>Entry</th><th>Exit</th><th>R</th></tr></thead><tbody></tbody></table>
<script>
const q = new URLSearchParams(location.search); const tok = q.get('token') ? '&token='+q.get('token') : '';
let source = 'live';
const chart = LightweightCharts.createChart(document.getElementById('c'), {autoSize:true,
  layout:{background:{color:'#0f1115'}, textColor:'#c9ced6'}, grid:{vertLines:{color:'#1b1f27'}, horzLines:{color:'#1b1f27'}},
  timeScale:{timeVisible:true}});
const s = chart.addCandlestickSeries({upColor:'#26a69a', downColor:'#ef5350', borderVisible:false, wickUpColor:'#26a69a', wickDownColor:'#ef5350'});
async function j(u){ const r = await fetch(u + (u.includes('?')?'':'?') + tok); return r.json(); }
async function load(){
  const sym = document.getElementById('sym').value;
  s.setData(await j('api/candles?symbol='+sym));
  const tr = await j('api/trades?source='+source+'&symbol='+sym);
  const m = [];
  for (const t of tr) {
    const long = t.side === 'long', col = source==='live' ? '#4c8bf5' : '#b39ddb';
    m.push({time:t.entry_time, position: long?'belowBar':'aboveBar', color:col, shape: long?'arrowUp':'arrowDown', text:(source==='live'?'':'BT ')+t.side});
    if (t.exit_time) m.push({time:t.exit_time, position: long?'aboveBar':'belowBar', color: t.r>0?'#26a69a':'#ef5350',
      shape:'circle', text:(t.r>0?'+':'')+t.r.toFixed(2)+'R'});
  }
  m.sort((a,b)=>a.time-b.time); s.setMarkers(m);
  const all = await j('api/trades?source='+source);
  document.querySelector('#tbl tbody').innerHTML = all.slice(-50).reverse().map(t =>
    `<tr><td class="${source==='live'?'live':'bt'}">${source}</td><td>${t.symbol}</td><td>${t.side}</td><td>${new Date(t.entry_time*1000).toISOString().slice(0,16)}</td><td>${t.exit_time?new Date(t.exit_time*1000).toISOString().slice(0,16):'open'}</td><td>${t.r==null?'':t.r.toFixed(2)}</td></tr>`).join('');
  const st = await j('api/stats?source='+source);
  document.getElementById('stats').innerHTML = Object.entries(st).map(([k,v]) => `<div class="card"><span class="muted">${k}</span><b>${v}</b></div>`).join('');
  document.getElementById('note').textContent = source==='live' ? 'Live journal: only these go to the channel' : 'Backtest: never published';
}
for (const [id,src] of [['bLive','live'],['bBt','backtest']]) document.getElementById(id).onclick = () => {
  source = src; document.getElementById('bLive').classList.toggle('on', src==='live'); document.getElementById('bBt').classList.toggle('on', src==='backtest'); load(); };
(async () => { const syms = await j('api/symbols'); const sel = document.getElementById('sym');
  sel.innerHTML = syms.map(x=>`<option>${x}</option>`).join(''); sel.onchange = load; load(); })();
</script></body></html>"""


def latest_backtest() -> dict | None:
    files = sorted(glob.glob("reports/backtest_*.json"))
    return json.loads(Path(files[-1]).read_text()) if files else None


def make_app(cfg, conn) -> web.Application:
    token = os.environ.get("DASHBOARD_TOKEN")

    @web.middleware
    async def auth(request, handler):
        if token and request.path != "/static/lightweight-charts.js" and request.query.get("token") != token:
            raise web.HTTPUnauthorized(text="token required")
        return await handler(request)

    async def index(_):
        return web.Response(text=PAGE, content_type="text/html")

    async def symbols(_):
        syms = settings.get(conn, "symbols_manual") or settings.get(conn, "universe") or [
            r[0] for r in conn.execute("SELECT DISTINCT symbol FROM candles WHERE tf='1h'")]
        return web.json_response(syms)

    async def candles(request):
        sym = request.query.get("symbol", "BTCUSDT")
        rows = conn.execute("SELECT ts, open, high, low, close FROM candles WHERE symbol=? AND market='futures' "
                            "AND tf='1h' ORDER BY ts DESC LIMIT 2000", (sym,)).fetchall()
        return web.json_response([{"time": r[0] // 1000, "open": r[1], "high": r[2], "low": r[3], "close": r[4]}
                                  for r in reversed(rows)])

    def live_trades(sym=None):
        q = ("SELECT s.id, s.symbol, s.side, s.ts, s.risk_pct, t.status, "
             "(SELECT MAX(ts) FROM outcomes WHERE signal_id=s.id AND closed_fraction>0) AS exit_ts, "
             "(SELECT SUM(pnl_r) FROM outcomes WHERE signal_id=s.id) AS r "
             "FROM signals s LEFT JOIN trade_state t ON t.signal_id=s.id WHERE s.source='live'")
        args = []
        if sym:
            q += " AND s.symbol=?"
            args.append(sym)
        out = []
        for r in conn.execute(q + " ORDER BY s.ts", args):
            closed = r["status"] == "closed"
            out.append({"id": r["id"], "symbol": r["symbol"], "side": r["side"], "entry_time": (r["ts"] - 3_600_000) // 1000,
                        "exit_time": r["exit_ts"] // 1000 // 3600 * 3600 if closed and r["exit_ts"] else None,
                        "r": round(r["r"], 2) if closed and r["r"] is not None else None, "risk_pct": r["risk_pct"]})
        return out

    def bt_trades(sym=None):
        rep = latest_backtest()
        if not rep:
            return []
        return [{"symbol": t["symbol"], "side": t["side"], "entry_time": t["entry_ts"] // 1000,
                 "exit_time": t["exit_ts"] // 1000, "r": t["r"], "pnl_pct": t["pnl_pct"]}
                for t in rep["trades"] if not sym or t["symbol"] == sym]

    async def trades(request):
        sym = request.query.get("symbol")
        src = request.query.get("source", "live")
        return web.json_response(bt_trades(sym) if src == "backtest" else live_trades(sym))

    async def stats(request):
        if request.query.get("source") == "backtest":
            rep = latest_backtest()
            if not rep:
                return web.json_response({"backtest": "none yet"})
            t, b = rep["total"], rep["benchmarks"]
            return web.json_response({"version": rep["params_version"], "trades": t["trades"],
                                      "return %": t["return_pct"], "buy&hold %": b["equal_weight_pct"],
                                      "max DD %": t["max_drawdown_pct"], "win %": t["win_rate_pct"],
                                      "profit factor": t["profit_factor"],
                                      "beats B&H and cash": "yes" if rep["beats_buy_and_hold_and_cash"] else "no"})
        tr = live_trades()
        closed = [x for x in tr if x["r"] is not None]
        wins = [x for x in closed if x["r"] > 0]
        return web.json_response({"signals": len(tr), "open": len(tr) - len(closed), "closed": len(closed),
                                  "win %": round(len(wins) / len(closed) * 100, 1) if closed else 0,
                                  "sum R": round(sum(x["r"] for x in closed), 2),
                                  "mode": settings.effective(cfg, conn)["publishing"]["mode"]})

    async def signal_png(request):
        sid = int(request.match_info["sid"])
        r = conn.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
        if not r:
            raise web.HTTPNotFound()
        df = features.build(conn, r["symbol"], r["ts"] - 200 * 3_600_000, r["ts"] + 72 * 3_600_000)
        f = json.loads(r["features_json"])
        html = chart.build_html(r["symbol"], r["side"], chart.candles_from_df(df.reset_index(drop=True), 200),
                                {k: r[k] for k in ("entry", "stop", "tp1", "tp2")}, f.get("attention_zones"),
                                f.get("liq_clusters"), subtitle=f"Signal #{sid}",
                                risk_pct=r["risk_pct"], leverage=r["leverage"])
        return web.Response(body=await chart.render_png(html), content_type="image/png")

    async def health(_):
        rows = conn.execute("SELECT component, status, detail, MAX(ts) AS ts FROM health GROUP BY component").fetchall()
        return web.json_response({r["component"]: {"status": r["status"], **json.loads(r["detail"])} for r in rows})

    app = web.Application(middlewares=[auth])
    app.router.add_get("/", index)
    app.router.add_get("/api/symbols", symbols)
    app.router.add_get("/api/candles", candles)
    app.router.add_get("/api/trades", trades)
    app.router.add_get("/api/stats", stats)
    app.router.add_get("/api/health", health)
    app.router.add_get(r"/chart/{sid:\d+}.png", signal_png)
    app.router.add_static("/static", STATIC)
    return app


async def serve(cfg, conn):
    app = make_app(cfg, conn)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg["dashboard"]["host"], cfg["dashboard"]["port"])
    await site.start()
    return runner
