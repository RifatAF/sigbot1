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

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>sigbot</title><script src="static/lightweight-charts.js"></script>
<style>
body{margin:0;background:#0d1017;color:#e8ebf1;font:14px -apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:10px;align-items:center;padding:12px 16px;flex-wrap:wrap}
select,button{background:#151a24;color:#e8ebf1;border:1px solid #2b3345;border-radius:8px;padding:7px 12px;font-size:14px;cursor:pointer}
button.on{background:#5b8cff;border-color:#5b8cff}button:disabled{opacity:.35;cursor:default}.sep{width:1px;height:24px;background:#2b3345}
a{color:#7aa7ff}.muted{color:#8a93a6}
#status{margin:0 16px 10px;padding:10px 14px;border-radius:8px;background:#151a24;display:none}
.bar{height:8px;border-radius:4px;background:#232b3a;overflow:hidden;margin:8px 0 2px}.bar i{display:block;height:100%;background:#5b8cff;transition:width .6s}
.st-ok .bar i{background:#3ddc97}.st-fail .bar i{background:#ff6b6b}.warn{color:#ffd27a;font-size:13px;margin-top:6px}
.why{margin:4px 16px 0;padding:10px 14px;border-radius:8px;background:#151a24;color:#a9b1c2;font-size:13px;line-height:1.5;display:none}
.wrap{position:relative;height:62vh;margin:0 16px;border:1px solid #1b2130;border-radius:10px;overflow:hidden}
#c{position:absolute;inset:0}#ov{position:absolute;left:0;top:0;pointer-events:none;z-index:3}
#empty{position:absolute;inset:0;z-index:4;display:none;align-items:center;justify-content:center;text-align:center;color:#8a93a6;font-size:15px;padding:20px}
.nav{display:flex;gap:10px;align-items:center;padding:12px 16px 0;flex-wrap:wrap}
.nav .title{font-size:20px;font-weight:700}.badge{padding:3px 10px;border-radius:6px;font-weight:700;font-size:13px}
.long{background:#123b2c;color:#3ddc97}.short{background:#3d1820;color:#ff6b6b}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;padding:12px 16px}
.card{background:#151a24;border-radius:10px;padding:10px 14px;border-left:3px solid #2b3345}.card>span{color:#8a93a6;font-size:12px;text-transform:uppercase}
.card b{display:block;font-size:19px;margin-top:4px;font-variant-numeric:tabular-nums}
.c-e{border-color:#5b8cff}.c-s{border-color:#ff6b6b}.c-t{border-color:#3ddc97}
h3{margin:18px 16px 4px;font-size:15px;color:#a9b1c2}
table{width:calc(100% - 32px);margin:0 16px 24px;border-collapse:collapse;font-variant-numeric:tabular-nums}
td,th{padding:7px;border-bottom:1px solid #1b2130;text-align:left}tbody tr{cursor:pointer}tbody tr:hover,tr.sel{background:#182030}
.pos{color:#3ddc97}.neg{color:#ff6b6b}
.legend{display:flex;gap:16px;flex-wrap:wrap;padding:8px 16px 0;font-size:12px;color:#8a93a6}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
</style></head><body>
<header><b>sigbot</b><a href="overview" id="ovl">обзор и бэктест портфеля →</a><span class="sep"></span>
<select id="sym"></select><span class="sep"></span>
<button id="bLive" class="on">Живые</button><button id="bBt">Бэктест</button><span class="sep"></span>
<button id="t1h" class="on">Час</button><button id="t1d">День</button>
<span class="muted" id="note"></span></header>
<div id="status"></div>
<div class="wrap"><div id="c"></div><canvas id="ov"></canvas><div id="empty"></div></div>
<div class="legend"><span><i style="background:rgba(61,220,151,.5)"></i>зона цели</span><span><i style="background:rgba(255,107,107,.5)"></i>зона риска</span>
<span><i style="background:#5b8cff"></i>вход</span><span><i style="background:#3ddc97;border-radius:50%"></i><i style="background:#ff6b6b;border-radius:50%"></i>выход в плюс / минус</span>
<span><i style="background:rgba(61,220,151,.55)"></i><i style="background:rgba(255,107,107,.55)"></i>полоса внизу: трендовый портфель в лонге / шорте</span></div>
<div class="nav"><button id="prev">◀</button><span id="cnt" class="muted"></span><button id="next">▶</button>
<span class="title" id="ttl"></span><span id="bdg"></span><span class="muted" id="when"></span></div>
<div class="why" id="why"></div><div class="cards" id="det"></div>
<div class="cards" id="stats"></div>
<h3 id="th">Сигналы</h3>
<table id="tbl"><thead><tr><th>Монета</th><th>Сторона</th><th>Вход</th><th>Выход</th><th>Как закрылась</th><th>Результат</th></tr></thead><tbody></tbody></table>
<script>
const q = new URLSearchParams(location.search), tok = q.get('token') ? 'token='+q.get('token') : '';
let BT = {}, source = 'live', tf = '1h', cs = [], times = [], trades = [], all = [], cur = -1, center = 0;
const $ = id => document.getElementById(id);
const chart = LightweightCharts.createChart($('c'), {autoSize:true,
  layout:{background:{color:'#0d1017'}, textColor:'#9aa3b5', fontSize:13},
  grid:{vertLines:{visible:false}, horzLines:{color:'#161b26'}},
  rightPriceScale:{borderVisible:false, scaleMargins:{top:0.08, bottom:0.16}}, timeScale:{borderVisible:false, timeVisible:true, rightOffset:12},
  localization:{priceFormatter: p => p < 0 ? '' : px(p)}});
const s = chart.addCandlestickSeries({upColor:'#3ddc97', downColor:'#ff6b6b', borderVisible:false, wickUpColor:'#3ddc97', wickDownColor:'#ff6b6b'});
s.applyOptions({autoscaleInfoProvider: orig => { const r = orig(), t = trades[cur]; if (!r || !t || t.entry==null) return r;
  return {priceRange:{minValue: Math.min(r.priceRange.minValue, t.stop, t.tp2), maxValue: Math.max(r.priceRange.maxValue, t.stop, t.tp2)}}; }});
const band = chart.addHistogramSeries({priceScaleId:'pos', priceLineVisible:false, lastValueVisible:false, base:0});
chart.priceScale('pos').applyOptions({scaleMargins:{top:0.9, bottom:0}, visible:false});
function px(p){ return p >= 1000 ? p.toLocaleString('en-US',{maximumFractionDigits:0}) : p >= 1 ? p.toFixed(p>=100?2:3) : p.toPrecision(4); }
async function j(u){ const r = await fetch(u + (u.includes('?')?'&':'?') + tok); return r.json(); }
const fmt = t => new Date(t*1000).toISOString().slice(0,16).replace('T',' ');
const step = () => tf==='1d' ? 86400 : 3600;
const EXIT = {tp2:'цель 2', tp1:'цель 1', stop:'стоп', breakeven_stop:'безубыток после цели 1', timeout:'по времени', trail_stop:'трейлинг стоп'};
// --- рисование зон сигналов поверх графика
const cv = $('ov'), g = cv.getContext('2d');
function xOf(t){ if (!times.length) return null; let lo=0, hi=times.length; const tt = Math.floor(t/step())*step();
  while (lo<hi){ const m=(lo+hi)>>1; if (times[m]<tt) lo=m+1; else hi=m; }
  if (lo>=times.length) lo = times.length-1 + (tt-times[times.length-1])/step();
  return chart.timeScale().logicalToCoordinate(lo); }
function chip(x, y, text, bg){ g.font='600 12px -apple-system,Segoe UI,Roboto,sans-serif'; const w=g.measureText(text).width+12;
  g.fillStyle=bg; g.beginPath(); g.roundRect ? g.roundRect(x, y-10, w, 20, 4) : g.rect(x, y-10, w, 20); g.fill();
  g.fillStyle='#0d1017'; g.fillText(text, x+6, y+4); }
function draw(){
  const W = chart.timeScale().width(), H = $('c').clientHeight - 28, dpr = devicePixelRatio || 1;
  cv.width = W*dpr; cv.height = H*dpr; cv.style.width = W+'px'; cv.style.height = H+'px'; g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,W,H);
  trades.forEach((t, i) => {
    if (t.entry == null) return;
    const sel = i === cur, x1 = xOf(t.entry_time), last = times[times.length-1] || t.entry_time;
    let x2 = xOf(t.exit_time || last); if (x1==null || x2==null) return; x2 = Math.max(x2, x1 + 6);
    if (x2 < 0 || x1 > W) return;
    const y = p => s.priceToCoordinate(p), ye = y(t.entry), ys = y(t.stop), y1 = y(t.tp1), y2 = y(t.tp2);
    if ([ye,ys,y1,y2].some(v => v==null)) return;
    const a = sel ? 1 : .45;
    g.fillStyle = `rgba(61,220,151,${.16*a})`; g.fillRect(x1, Math.min(ye,y2), x2-x1, Math.abs(y2-ye));
    g.fillStyle = `rgba(255,107,107,${.2*a})`; g.fillRect(x1, Math.min(ye,ys), x2-x1, Math.abs(ys-ye));
    const line = (yy, c, dash) => { g.strokeStyle=c; g.globalAlpha=a; g.setLineDash(dash||[]); g.lineWidth=1.3;
      g.beginPath(); g.moveTo(x1, yy); g.lineTo(x2, yy); g.stroke(); g.setLineDash([]); g.globalAlpha=1; };
    line(y2,'#3ddc97'); line(y1,'#3ddc97',[3,3]); line(ye,'#5b8cff'); line(ys,'#ff6b6b');
    if (sel) { const lx = Math.min(x2 + 6, W - 120);
      chip(lx, y2, 'TP2 '+px(t.tp2), '#3ddc97'); chip(lx, y1, 'TP1 '+px(t.tp1), '#3ddc97');
      chip(lx, ye, 'Вход '+px(t.entry), '#7aa7ff'); chip(lx, ys, 'Стоп '+px(t.stop), '#ff6b6b'); }
  });
}
const redraw = () => requestAnimationFrame(draw);
chart.timeScale().subscribeVisibleLogicalRangeChange(redraw); chart.timeScale().subscribeSizeChange(redraw);
chart.subscribeCrosshairMove(redraw); new ResizeObserver(redraw).observe($('c'));
// --- данные
async function loadCandles(sym){
  cs = await j('api/candles?symbol='+sym+'&tf='+tf+(tf==='1h' && center ? '&center='+center : ''));
  cs = cs.filter(c => c.open!=null && c.close!=null); times = cs.map(c => c.time); s.setData(cs);
  const marks = (await j('api/marks?symbol='+sym+'&source='+source)).sort((a,b)=>a.time-b.time);
  let k = 0, side = 0; band.setData(cs.map(c => { while (k < marks.length && marks[k].time <= c.time) side = marks[k++].side;
    return {time:c.time, value: side===0 ? 0 : 1, color: side>0 ? 'rgba(61,220,151,.55)' : side<0 ? 'rgba(255,107,107,.55)' : 'rgba(0,0,0,0)'}; }));
  const m = [];
  for (const t of trades) { const long = t.side==='long', sn = x => Math.floor(x/step())*step();
    m.push({time:sn(t.entry_time), position: long?'belowBar':'aboveBar', color:'#5b8cff', shape: long?'arrowUp':'arrowDown', size:1});
    if (t.exit_time && t.r != null) m.push({time:sn(t.exit_time), position: long?'aboveBar':'belowBar', color: t.r>0?'#3ddc97':'#ff6b6b',
      shape:'circle', size:1, text: tf==='1h' ? (t.r>0?'+':'')+t.r.toFixed(1)+'R' : ''}); }
  s.setMarkers(m.filter(x => x.time >= (times[0]||0) && x.time <= (times[times.length-1]||0)).sort((a,b)=>a.time-b.time));
  const e = $('empty');
  if (!cs.length) { e.style.display='flex'; e.textContent='По этой монете ещё нет часовых свечей: бот докачивает историю, обнови страницу через пару минут.'; }
  else e.style.display='none';
}
function focus(i){
  if (!trades.length) { cur=-1; renderDet(); redraw(); return; }
  cur = Math.max(0, Math.min(trades.length-1, i)); const t = trades[cur];
  const span = Math.max((t.exit_time || t.entry_time) - t.entry_time, step()*10);
  const lo = xIdx(t.entry_time - span*1.5), hi = xIdx((t.exit_time||t.entry_time) + span*1.2);
  if (lo!=null && hi!=null) chart.timeScale().setVisibleLogicalRange({from: lo, to: Math.max(hi, lo+30)});
  renderDet(); renderTable(); redraw();
}
function xIdx(t){ if (!times.length) return null; let lo=0, hi=times.length; while(lo<hi){const m=(lo+hi)>>1; if(times[m]<t) lo=m+1; else hi=m;} return lo; }
async function go(i){
  const t = trades[i]; if (!t) return focus(i);
  if (tf==='1h' && (!times.length || t.entry_time < times[0] || (t.exit_time||t.entry_time) > times[times.length-1])) {
    center = t.entry_time; await loadCandles($('sym').value); }
  focus(i);
}
function renderDet(){
  const t = trades[cur];
  $('cnt').textContent = trades.length ? `сигнал ${cur+1} из ${trades.length}` : 'по этой монете сигналов нет';
  $('prev').disabled = cur <= 0; $('next').disabled = cur >= trades.length-1;
  $('why').style.display = t ? 'none' : 'block';
  if (!t) { $('ttl').textContent=''; $('bdg').innerHTML=''; $('when').textContent=''; $('det').innerHTML='';
    const name = $('sym').value.replace('USDT',''), total = all.length;
    $('why').innerHTML = source==='live' ? `По ${name} бот ещё не публиковал сигналов. Бэктест истории: кнопка «Бэктест».` :
      ST.state==='work' ? `⏳ Бэктест пересчитывается на полной истории (${ST.pct}%). Сигналы появятся, когда полоса дойдёт до 100%.` :
      !total ? 'Бэктест ещё не посчитан или в истории нет данных открытого интереса (смотри предупреждение выше).' :
      `За период бэктеста${BT.period ? ' ' + BT.period : ''} по ${name} сигналов не было. Часовая стратегия входит только после сильного слива `+
      `с падением открытого интереса (капитуляция), это редкое событие: всего ${total} сигналов по всем монетам. `+
      `Монеты с сигналами стоят вверху списка, рядом число сигналов. Основная прибыль идёт от трендового портфеля: `+
      `полоса внизу графика и страница «обзор и бэктест портфеля».`;
    return; }
  $('ttl').textContent = t.symbol.replace('USDT','');
  $('bdg').innerHTML = `<span class="badge ${t.side}">${t.side==='long'?'LONG':'SHORT'}</span>`;
  $('when').textContent = 'вход ' + fmt(t.entry_time) + ' UTC' + (t.exit_time ? ', выход ' + fmt(t.exit_time) : ', сделка открыта');
  const res = t.r==null ? 'открыта' : (t.r>0?'+':'')+t.r.toFixed(2)+'R' + (t.pnl_pct!=null ? ` (${t.pnl_pct>0?'+':''}${t.pnl_pct.toFixed(2)}%)` : '');
  const c = [['Вход',px(t.entry),'c-e'],['Стоп',px(t.stop),'c-s'],['TP1',px(t.tp1),'c-t'],['TP2',px(t.tp2),'c-t'],
    ['Риск', t.risk_pct!=null ? t.risk_pct.toFixed(2)+'%' : '', ''],['Плечо', t.leverage!=null ? 'x'+t.leverage.toFixed(1) : '', ''],
    ['Итог', `<span class="${t.r>0?'pos':t.r<0?'neg':''}">${res}</span>`, ''],['Закрыта', EXIT[t.exit_event] || (t.exit_time?'':'—'), '']];
  $('det').innerHTML = c.filter(x=>x[1]!=='').map(([k,v,cl]) => `<div class="card ${cl}"><span>${k}</span><b>${v}</b></div>`).join('');
}
function renderTable(){
  $('th').textContent = (source==='live' ? 'Живые сигналы' : 'Сигналы бэктеста') + ' по монете';
  $('tbl').querySelector('tbody').innerHTML = trades.map((t,i) => [t,i]).reverse().map(([t,i]) =>
    `<tr data-i="${i}" class="${i===cur?'sel':''}"><td>${t.symbol.replace('USDT','')}</td><td class="${t.side==='long'?'pos':'neg'}">${t.side==='long'?'лонг':'шорт'}</td><td>${fmt(t.entry_time)}</td><td>${t.exit_time?fmt(t.exit_time):'открыта'}</td><td>${EXIT[t.exit_event]||''}</td><td class="${t.r>0?'pos':t.r<0?'neg':''}">${t.r==null?'':(t.r>0?'+':'')+t.r.toFixed(2)+'R'}</td></tr>`).join('') ||
    '<tr><td colspan=6 class="muted">по этой монете сигналов нет, выбери монету с числом в списке</td></tr>';
  document.querySelectorAll('#tbl tbody tr[data-i]').forEach(r => r.onclick = () => { go(+r.dataset.i); window.scrollTo({top:0, behavior:'smooth'}); });
}
async function fillSymbols(){
  const sel = $('sym'), keep = sel.value || q.get('symbol');
  const syms = await j('api/symbols'); all = await j('api/trades?source='+source);
  const n = {}; all.forEach(t => n[t.symbol] = (n[t.symbol]||0)+1);
  const list = [...new Set([...syms, ...Object.keys(n)])].sort((a,b) => (n[b]||0)-(n[a]||0));
  sel.innerHTML = list.map(x => `<option value="${x}">${x.replace('USDT','')}${n[x] ? ' · '+n[x] : ''}</option>`).join('');
  if (keep && list.includes(keep)) sel.value = keep;
}
async function load(){
  const sym = $('sym').value; center = 0; s.applyOptions({priceLineVisible: source==='live'});
  trades = all.filter(t => t.symbol === sym).sort((a,b) => a.entry_time-b.entry_time);
  await loadCandles(sym);
  if (trades.length) await go(trades.length-1);
  else { cur=-1; if (tf==='1h') chart.timeScale().setVisibleLogicalRange({from: cs.length-240, to: cs.length+12}); else chart.timeScale().fitContent(); renderDet(); renderTable(); redraw(); }
  const st = await j('api/stats?source='+source); if (source==='backtest') BT = {period: st['период'] || ''}; renderDet();
  const names = {signals:'сигналов', open:'открыто', closed:'закрыто', 'win %':'прибыльных, %', 'sum R':'сумма R', mode:'режим',
    version:'версия', trades:'сделок (все монеты)', 'return %':'доходность, %', 'buy&hold %':'buy and hold, %', 'max DD %':'просадка, %',
    'profit factor':'profit factor', 'beats B&H and cash':'лучше рынка и кэша', backtest:'бэктест'};
  $('stats').innerHTML = Object.entries(st).filter(([k]) => k!=='период').map(([k,v]) =>
    `<div class="card"><span>${names[k]||k}</span><b>${v==='yes'?'да':v==='no'?'нет':v}</b></div>`).join('');
  $('note').textContent = source==='live' ? 'Живая работа: то, что ушло в канал' : 'Бэктест часовых сигналов, в канал не публикуется';
}
let ST = {};
async function poll(){ try { const st = await j('api/status'); ST = st; const b = $('status');
  const show = st.state==='work' || st.state==='fail' || (st.warn||[]).length || (st.state==='ok' && Date.now()/1000 - st.ts < 60);
  b.style.display = show ? 'block' : 'none'; b.className = 'st-'+st.state;
  const icon = st.state==='work' ? '⏳' : st.state==='ok' ? '✅' : '❌';
  b.innerHTML = `<b>${icon} ${st.state==='work' ? 'Подготовка данных: ' + st.pct + '%' : st.state==='ok' ? 'Подготовка завершена' : 'Ошибка подготовки'}</b>
    <span class="muted">&nbsp; ${st.text||''}</span><div class="bar"><i style="width:${st.state==='ok'?100:st.pct||0}%"></i></div>` +
    (st.warn||[]).map(w => `<div class="warn">⚠️ ${w}</div>`).join('');
  if (st.state==='ok' && poll.was==='work') { await fillSymbols(); load(); } poll.was = st.state; } catch(e){} setTimeout(poll, 3000); }
const toggle = (a, b, on) => { $(a).classList.toggle('on', on); $(b).classList.toggle('on', !on); };
$('t1h').onclick = () => { tf='1h'; toggle('t1h','t1d',true); chart.applyOptions({timeScale:{timeVisible:true}}); load(); };
$('t1d').onclick = () => { tf='1d'; toggle('t1h','t1d',false); chart.applyOptions({timeScale:{timeVisible:false}}); load(); };
$('bLive').onclick = async () => { source='live'; toggle('bLive','bBt',true); await fillSymbols(); load(); };
$('bBt').onclick = async () => { source='backtest'; toggle('bLive','bBt',false); await fillSymbols(); load(); };
$('prev').onclick = () => go(cur-1); $('next').onclick = () => go(cur+1);
document.addEventListener('keydown', e => { if (e.key==='ArrowLeft') go(cur-1); if (e.key==='ArrowRight') go(cur+1); });
$('sym').onchange = load;
(async () => { $('ovl').href = 'overview' + (tok ? '?'+tok : '');
  if (q.get('source')==='backtest') { source='backtest'; toggle('bLive','bBt',false); }
  await fillSymbols(); load(); poll(); })();
</script></body></html>
"""


def latest_backtest() -> dict | None:
    files = sorted(glob.glob("reports/backtest_*.json"), key=os.path.getmtime)
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
        c = int(request.query.get("center") or 0) * 1000     # окно вокруг выбранного сигнала, иначе последние бары
        if c:
            rows = conn.execute("SELECT ts, open, high, low, close FROM candles WHERE symbol=? AND market='futures' "
                                "AND tf='1h' AND ts BETWEEN ? AND ? ORDER BY ts",
                                (sym, c - 1200 * 3_600_000, c + 800 * 3_600_000)).fetchall()
        else:
            rows = conn.execute("SELECT ts, open, high, low, close FROM candles WHERE symbol=? AND market='futures' "
                                "AND tf='1h' ORDER BY ts DESC LIMIT 2000", (sym,)).fetchall()[::-1]
        return web.json_response([{"time": r[0] // 1000, "open": r[1], "high": r[2], "low": r[3], "close": r[4]}
                                  for r in rows])

    def live_trades(sym=None):
        q = ("SELECT s.id, s.symbol, s.side, s.ts, s.risk_pct, s.leverage, s.entry, s.stop, s.tp1, s.tp2, t.status, "
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
                        "r": round(r["r"], 2) if closed and r["r"] is not None else None, "risk_pct": r["risk_pct"],
                        "leverage": r["leverage"], **{k: r[k] for k in ("entry", "stop", "tp1", "tp2")},
                        "exits": [[e[0] // 1000 // 3600 * 3600, e[1]] for e in conn.execute(
                            "SELECT ts, event FROM outcomes WHERE signal_id=? AND closed_fraction>0 ORDER BY ts", (r["id"],))],
                        "exit_event": None})
        return out

    def bt_trades(sym=None):
        rep = latest_backtest()
        if not rep:
            return []
        return [{"symbol": t["symbol"], "side": t["side"], "entry_time": t["entry_ts"] // 1000 - 3600,
                 "exit_time": t["exit_ts"] // 1000, "r": t["r"], "pnl_pct": t["pnl_pct"],
                 **{k: t[k] for k in ("entry", "stop", "tp1", "tp2", "risk_pct", "leverage", "strength", "exit_event")},
                 "exits": [[e[0] // 1000, e[1]] for e in t.get("exits", [])]}
                for t in rep["trades"] if not sym or t["symbol"] == sym]

    async def trades(request):
        sym = request.query.get("symbol")
        src = request.query.get("source", "live")
        return web.json_response(bt_trades(sym) if src == "backtest" else live_trades(sym))

    async def stats(request):
        if request.query.get("source") == "backtest":
            rep = latest_backtest()
            if not rep:
                from . import autoprep
                return web.json_response({"backtest": autoprep.STATUS.get("text") or "ещё не посчитан"})
            t, b = rep["total"], rep["benchmarks"]
            import time as _t
            pr = rep.get("period", {})
            return web.json_response({"version": rep["params_version"],
                                      "период": (_t.strftime("%Y-%m-%d", _t.gmtime(pr["out_of_sample_start"] / 1000)) if isinstance(pr.get("out_of_sample_start"), int) else pr.get("start", "")) + " … " + pr.get("end", ""),
                                      "посчитан": _t.strftime("%d.%m %H:%M", _t.localtime(rep["generated"] / 1000)),
                                      "trades": t["trades"],
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

    async def status(_):
        from . import autoprep
        return web.json_response(autoprep.STATUS)

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
    app.router.add_get("/api/status", status)
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
