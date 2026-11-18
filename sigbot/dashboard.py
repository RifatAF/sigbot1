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
<header><b>sigbot</b><a href="overview" id="ov" style="color:#7aa7ff">обзор и бэктест</a><select id="sym"></select>
<button id="bLive" class="on">Live</button><button id="bBt">Backtest</button>
<button id="t1h" class="on">1h</button><button id="t1d">1d</button>
<span class="muted" id="note"></span></header>
<div id="c"></div><div class="grid" id="stats"></div>
<table id="tbl"><thead><tr><th>Source</th><th>Coin</th><th>Side</th><th>Entry</th><th>Exit</th><th>R</th></tr></thead><tbody></tbody></table>
<script>
const q = new URLSearchParams(location.search); const tok = q.get('token') ? '&token='+q.get('token') : '';
let source = 'live', tf = '1h';
const chart = LightweightCharts.createChart(document.getElementById('c'), {autoSize:true,
  layout:{background:{color:'#0f1115'}, textColor:'#c9ced6'}, grid:{vertLines:{color:'#1b1f27'}, horzLines:{color:'#1b1f27'}},
  timeScale:{timeVisible:true}});
const s = chart.addCandlestickSeries({upColor:'#26a69a', downColor:'#ef5350', borderVisible:false, wickUpColor:'#26a69a', wickDownColor:'#ef5350'});
async function j(u){ const r = await fetch(u + (u.includes('?')?'':'?') + tok); return r.json(); }
async function load(){
  const sym = document.getElementById('sym').value;
  s.setData(await j('api/candles?symbol='+sym+'&tf='+tf));
  const snap = t => tf==='1d' ? Math.floor(t/86400)*86400 : t;
  const tr = await j('api/trades?source='+source+'&symbol='+sym);
  const m = [];
  for (const t of tr) {
    const long = t.side === 'long', col = source==='live' ? '#4c8bf5' : '#b39ddb';
    m.push({time:snap(t.entry_time), position: long?'belowBar':'aboveBar', color:col, shape: long?'arrowUp':'arrowDown', text:(source==='live'?'':'BT ')+t.side});
    if (t.exit_time) m.push({time:snap(t.exit_time), position: long?'aboveBar':'belowBar', color: t.r>0?'#26a69a':'#ef5350',
      shape:'circle', text:(t.r>0?'+':'')+t.r.toFixed(2)+'R'});
  }
  for (const k of await j('api/marks?symbol='+sym+'&source='+source)) {
    const live = k.kind==='portfolio_live', pre = live ? 'P ' : 'BT P ';
    if (k.side === 0) m.push({time:snap(k.time), position:'aboveBar', color:'#8b93a1', shape:'circle', text:pre+'exit'});
    else m.push({time:snap(k.time), position: k.side>0?'belowBar':'aboveBar', color: k.side>0?'#3ddc97':'#ff6b6b',
            shape: k.side>0?'arrowUp':'arrowDown', text:pre+(k.side>0?'long':'short')});
  }
  m.sort((a,b)=>a.time-b.time); s.setMarkers(m);
  const all = await j('api/trades?source='+source);
  document.querySelector('#tbl tbody').innerHTML = all.slice(-50).reverse().map(t =>
    `<tr><td class="${source==='live'?'live':'bt'}">${source}</td><td>${t.symbol}</td><td>${t.side}</td><td>${new Date(t.entry_time*1000).toISOString().slice(0,16)}</td><td>${t.exit_time?new Date(t.exit_time*1000).toISOString().slice(0,16):'open'}</td><td>${t.r==null?'':t.r.toFixed(2)}</td></tr>`).join('');
  const st = await j('api/stats?source='+source);
  document.getElementById('stats').innerHTML = Object.entries(st).map(([k,v]) => `<div class="card"><span class="muted">${k}</span><b>${v}</b></div>`).join('');
  document.getElementById('note').textContent = source==='live' ? 'Live journal: only these go to the channel' : 'Backtest: never published';
}
for (const [id,t] of [['t1h','1h'],['t1d','1d']]) document.getElementById(id).onclick = () => {
  tf = t; document.getElementById('t1h').classList.toggle('on', t==='1h'); document.getElementById('t1d').classList.toggle('on', t==='1d');
  chart.applyOptions({timeScale:{timeVisible: t==='1h'}}); load(); };
for (const [id,src] of [['bLive','live'],['bBt','backtest']]) document.getElementById(id).onclick = () => {
  source = src; document.getElementById('bLive').classList.toggle('on', src==='live'); document.getElementById('bBt').classList.toggle('on', src==='backtest'); load(); };
(async () => { const syms = await j('api/symbols'); const sel = document.getElementById('sym');
  sel.innerHTML = syms.map(x=>`<option>${x}</option>`).join(''); if (q.get('symbol')) sel.value = q.get('symbol');
  document.getElementById('ov').href = 'overview' + (q.get('token') ? '?token='+q.get('token') : '');
  sel.onchange = load; load(); })();
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
        if request.query.get("tf") == "1d":   # дневки из часовых за всю историю: видно позиции портфеля
            rows = conn.execute("SELECT ts / 86400000 * 86400 AS d, ts, open, high, low, close FROM candles "
                                "WHERE symbol=? AND market='futures' AND tf='1h' ORDER BY ts", (sym,)).fetchall()
            out = {}
            for d, _ts, o, h, lo, c in rows:
                x = out.get(d)
                out[d] = [o, h, lo, c] if x is None else [x[0], max(x[1], h), min(x[2], lo), c]
            return web.json_response([{"time": d, "open": v[0], "high": v[1], "low": v[2], "close": v[3]}
                                      for d, v in out.items()])
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

    async def overview_page(_):
        return web.Response(text=(STATIC / "overview.html").read_text(encoding="utf-8"), content_type="text/html")

    async def overview_api(_):
        f = Path("reports/overview.json")
        return web.json_response(json.loads(f.read_text()) if f.exists() else {})

    async def live_api(_):
        import time as _t
        day = conn.execute("SELECT MAX(day) FROM portfolio_weights").fetchone()[0]
        pos = [{"symbol": r[0], "w": r[1]} for r in conn.execute(
            "SELECT symbol, weight FROM portfolio_weights WHERE day=? ORDER BY ABS(weight) DESC", (day,))] if day else []
        eq = conn.execute("SELECT equity FROM portfolio_equity ORDER BY day DESC LIMIT 1").fetchone()
        lo = [dict(r) for r in conn.execute("SELECT symbol, entry, stop, exit_due_ts FROM listing_trades WHERE status='open'")]
        posts = [{"ts": _t.strftime("%m-%d %H:%M", _t.gmtime(r["ts"] / 1000)), "kind": r["kind"], "status": r["status"],
                  "text": __import__("html").escape(r["text"][:90].replace("\n", " "))}
                 for r in conn.execute("SELECT ts, kind, status, text FROM posts ORDER BY id DESC LIMIT 15")]
        leader = conn.execute("SELECT value FROM settings WHERE key='leader_name'").fetchone()
        return web.json_response({"mode": settings.effective(cfg, conn)["publishing"]["mode"], "positions": pos,
                                  "paper_total_pct": round((eq[0] - 1) * 100, 2) if eq else 0.0, "listings_open": lo,
                                  "posts": posts, "last_post": posts[0]["ts"] + " " + posts[0]["status"] if posts else None,
                                  "leader": json.loads(leader[0]) if leader else None})

    async def marks_api(request):
        """Маркеры для графика монеты: позиции портфеля (бэктест и живые) и шорты листингов."""
        sym = request.query.get("symbol", "BTCUSDT")
        out = []
        f = Path("reports/overview.json")
        if f.exists() and request.query.get("source") == "backtest":
            rep = json.loads(f.read_text())
            for t, side in rep.get("portfolio_marks", {}).get(sym, []):
                out.append({"time": t, "kind": "portfolio", "side": side})
        rows = conn.execute("SELECT day, weight FROM portfolio_weights WHERE symbol=? ORDER BY day", (sym,)).fetchall()
        prev, last_day = 0, None
        days = [r[0] for r in conn.execute("SELECT DISTINCT day FROM portfolio_weights ORDER BY day")]
        wmap = dict(rows)
        for day in days:                                   # день без строки по монете = позиции нет
            w = wmap.get(day, 0)
            sd = 1 if w > 0.001 else -1 if w < -0.001 else 0
            if sd != prev:
                out.append({"time": day // 1000 + 86400, "kind": "portfolio_live", "side": sd})
                prev = sd
        return web.json_response(out)

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
    app.router.add_get("/overview", overview_page)
    app.router.add_get("/api/overview", overview_api)
    app.router.add_get("/api/live", live_api)
    app.router.add_get("/api/marks", marks_api)
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
