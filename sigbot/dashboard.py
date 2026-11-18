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
<style>
body{margin:0;background:#0d1017;color:#e8ebf1;font:14px -apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:10px;align-items:center;padding:12px 16px;flex-wrap:wrap}
select,button{background:#151a24;color:#e8ebf1;border:1px solid #2b3345;border-radius:8px;padding:7px 12px;font-size:14px}
button.on{background:#5b8cff;border-color:#5b8cff}.sep{width:1px;height:24px;background:#2b3345}
a{color:#7aa7ff}.muted{color:#8a93a6}
.legend{display:flex;gap:18px;flex-wrap:wrap;padding:0 16px 8px;font-size:13px;color:#a9b1c2}
.legend i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:6px;vertical-align:-1px}
#c{height:64vh;margin:0 16px;border:1px solid #1b2130;border-radius:10px;overflow:hidden}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;padding:16px}
.card{background:#151a24;border-radius:10px;padding:10px 14px}.card b{display:block;font-size:22px;margin-top:4px}
table{width:calc(100% - 32px);margin:0 16px 24px;border-collapse:collapse;font-variant-numeric:tabular-nums}
td,th{padding:7px;border-bottom:1px solid #1b2130;text-align:left}.pos{color:#3ddc97}.neg{color:#ff6b6b}
</style></head><body>
<header><b>sigbot</b><a href="overview" id="ov">обзор и бэктест →</a><span class="sep"></span>
<select id="sym"></select><span class="sep"></span>
<button id="bLive" class="on">Живые</button><button id="bBt">Бэктест</button><span class="sep"></span>
<button id="t1h">Час</button><button id="t1d" class="on">День</button>
<span class="muted" id="note"></span></header>
<div class="legend">
<span><i style="background:#5b8cff"></i>вход часового сигнала (▲ лонг, ▼ шорт)</span>
<span><i style="background:#3ddc97;border-radius:50%"></i>выход в плюс (на часовом графике подписан результат в R)</span>
<span><i style="background:#ff6b6b;border-radius:50%"></i>выход в минус</span>
<span><i style="background:rgba(61,220,151,.55)"></i><i style="background:rgba(255,107,107,.55)"></i>полоса внизу: портфель в лонге / в шорте по монете</span>
</div>
<div id="c"></div><div class="grid" id="stats"></div>
<table id="tbl"><thead><tr><th>Монета</th><th>Сторона</th><th>Вход</th><th>Выход</th><th>Результат</th></tr></thead><tbody></tbody></table>
<script>
const q = new URLSearchParams(location.search); const tok = q.get('token') ? '&token='+q.get('token') : '';
let source = 'live', tf = '1d';
const chart = LightweightCharts.createChart(document.getElementById('c'), {autoSize:true,
  layout:{background:{color:'#0d1017'}, textColor:'#9aa3b5', fontSize:13},
  grid:{vertLines:{visible:false}, horzLines:{color:'#161b26'}},
  rightPriceScale:{borderVisible:false, scaleMargins:{top:0.05, bottom:0.18}}, timeScale:{borderVisible:false, timeVisible:false},
  localization:{priceFormatter: p => p < 0 ? '' : p >= 1000 ? p.toFixed(0) : p >= 1 ? p.toFixed(2) : p.toPrecision(4)}});
const s = chart.addCandlestickSeries({upColor:'#3ddc97', downColor:'#ff6b6b', borderVisible:false, wickUpColor:'#3ddc97', wickDownColor:'#ff6b6b'});
const pos = chart.addHistogramSeries({priceScaleId:'pos', priceLineVisible:false, lastValueVisible:false, base:0});
chart.priceScale('pos').applyOptions({scaleMargins:{top:0.86, bottom:0}, visible:false});
async function j(u){ const r = await fetch(u + (u.includes('?')?'':'?') + tok); return r.json(); }
const fmt = t => new Date(t*1000).toISOString().slice(0, tf==='1d' ? 10 : 16).replace('T',' ');
async function load(){
  const sym = document.getElementById('sym').value;
  const cs = await j('api/candles?symbol='+sym+'&tf='+tf);
  s.setData(cs);
  const step = tf==='1d' ? 86400 : 3600, snap = t => Math.floor(t/step)*step;
  // полоса позиции портфеля: из точек смены стороны в столбик на каждой свече
  const marks = (await j('api/marks?symbol='+sym+'&source='+source)).sort((a,b)=>a.time-b.time);
  let k = 0, side = 0; const bars = [];
  for (const c of cs) { while (k < marks.length && marks[k].time <= c.time) side = marks[k++].side;
    bars.push({time:c.time, value: side===0 ? 0 : 1,
    color: side>0 ? 'rgba(61,220,151,.55)' : side<0 ? 'rgba(255,107,107,.55)' : 'rgba(0,0,0,0)'}); }
  pos.setData(bars);
  const tr = await j('api/trades?source='+source+'&symbol='+sym), m = [];
  for (const t of tr) {
    const long = t.side === 'long';
    m.push({time:snap(t.entry_time), position: long?'belowBar':'aboveBar', color:'#5b8cff', shape: long?'arrowUp':'arrowDown', size:1});
    if (t.exit_time && t.r != null) m.push({time:snap(t.exit_time), position: long?'aboveBar':'belowBar',
      color: t.r>0?'#3ddc97':'#ff6b6b', shape:'circle', size:1, text: tf==='1h' ? (t.r>0?'+':'')+t.r.toFixed(1)+'R' : ''});
  }
  m.sort((a,b)=>a.time-b.time); s.setMarkers(m);
  if (tf==='1h') chart.timeScale().setVisibleLogicalRange({from: cs.length-240, to: cs.length+5}); else chart.timeScale().fitContent();
  document.querySelector('#tbl tbody').innerHTML = tr.slice().reverse().slice(0,40).map(t =>
    `<tr><td>${t.symbol.replace('USDT','')}</td><td class="${t.side==='long'?'pos':'neg'}">${t.side==='long'?'лонг':'шорт'}</td><td>${fmt(t.entry_time)}</td><td>${t.exit_time?fmt(t.exit_time):'открыта'}</td><td class="${t.r>0?'pos':t.r<0?'neg':''}">${t.r==null?'':(t.r>0?'+':'')+t.r.toFixed(2)+'R'}</td></tr>`).join('') ||
    '<tr><td colspan=5 class="muted">по этой монете часовых сигналов нет</td></tr>';
  const st = await j('api/stats?source='+source);
  const names = {signals:'сигналов', open:'открыто', closed:'закрыто', 'win %':'прибыльных, %', 'sum R':'сумма R', mode:'режим',
    version:'версия', trades:'сделок', 'return %':'доходность, %', 'buy&hold %':'buy and hold, %', 'max DD %':'просадка, %',
    'profit factor':'profit factor', 'beats B&H and cash':'лучше рынка и кэша', backtest:'бэктест'};
  document.getElementById('stats').innerHTML = Object.entries(st).map(([k,v]) =>
    `<div class="card"><span class="muted">${names[k]||k}</span><b>${v==='yes'?'да':v==='no'?'нет':v}</b></div>`).join('');
  document.getElementById('note').textContent = source==='live' ? 'Живая работа: то, что ушло в канал' :
    'Бэктест часовых сигналов и трендового портфеля, в канал не публикуется';
}
const toggle = (a, b, on) => { document.getElementById(a).classList.toggle('on', on); document.getElementById(b).classList.toggle('on', !on); };
document.getElementById('t1h').onclick = () => { tf='1h'; toggle('t1h','t1d',true); chart.applyOptions({timeScale:{timeVisible:true}}); load(); };
document.getElementById('t1d').onclick = () => { tf='1d'; toggle('t1h','t1d',false); chart.applyOptions({timeScale:{timeVisible:false}}); load(); };
document.getElementById('bLive').onclick = () => { source='live'; toggle('bLive','bBt',true); load(); };
document.getElementById('bBt').onclick = () => { source='backtest'; toggle('bLive','bBt',false); load(); };
(async () => { const syms = await j('api/symbols'); const sel = document.getElementById('sym');
  sel.innerHTML = syms.map(x=>`<option value="${x}">${x.replace('USDT','')}</option>`).join(''); if (q.get('symbol')) sel.value = q.get('symbol');
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
