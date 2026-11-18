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

.seg{display:inline-flex;background:#151a24;border:1px solid #2b3345;border-radius:9px;padding:2px}
.seg button{border:0;background:none;padding:6px 12px;border-radius:7px}.seg button.on{background:#5b8cff}
.hint{padding:6px 16px 0;font-size:13px;color:#8a93a6}
.sum{display:flex;gap:18px;flex-wrap:wrap;padding:10px 16px 0;font-size:14px}.sum b{font-size:16px}
</style></head><body>
<header><b>sigbot</b>
<select id="sym" title="Монета"></select>
<span class="seg"><button data-g="source" data-v="live" class="on">Живые</button><button data-g="source" data-v="backtest">Бэктест</button></span>
<span class="seg"><button data-g="view" data-v="signals" class="on">Часовые сигналы</button><button data-g="view" data-v="portfolio">Трендовый портфель</button></span>
<span class="seg"><button data-g="tf" data-v="1h" class="on">Час</button><button data-g="tf" data-v="1d">День</button></span>
<a href="overview" id="ovl">сводный отчёт →</a></header>
<div id="status"></div>
<div class="sum" id="sum"></div>
<div class="hint" id="hint"></div>
<div class="wrap"><div id="c"></div><canvas id="ov"></canvas><div id="empty"></div></div>
<div class="legend" id="legend"></div>
<div class="nav"><button id="prev" title="Предыдущая (←)">◀</button><span id="cnt" class="muted"></span><button id="next" title="Следующая (→)">▶</button>
<span class="title" id="ttl"></span><span id="bdg"></span><span class="muted" id="when"></span></div>
<div class="why" id="why"></div><div class="cards" id="det"></div>
<h3 id="th"></h3>
<table id="tbl"><thead id="thead"></thead><tbody></tbody></table>
<h3>Бэктест часовых сигналов, все монеты</h3><div class="cards" id="stats"></div>
<script>
const q = new URLSearchParams(location.search), tok = q.get('token') ? 'token='+q.get('token') : '';
const S = {source: q.get('source') || 'live', view: q.get('view') || 'signals', tf: '1h'};
let cs = [], times = [], items = [], all = [], cur = -1, center = 0, ST = {}, BT = {}, daily = [];
const $ = id => document.getElementById(id);
const G = '#3ddc97', R = '#ff6b6b', B = '#5b8cff';
const chart = LightweightCharts.createChart($('c'), {autoSize:true,
  layout:{background:{color:'#0d1017'}, textColor:'#9aa3b5', fontSize:13},
  grid:{vertLines:{visible:false}, horzLines:{color:'#161b26'}},
  rightPriceScale:{borderVisible:false, scaleMargins:{top:0.1, bottom:0.08}}, timeScale:{borderVisible:false, timeVisible:true, rightOffset:8},
  crosshair:{mode:0}, localization:{priceFormatter: p => p <= 0 ? '' : px(p)}});
const s = chart.addCandlestickSeries({upColor:G, downColor:R, borderVisible:false, wickUpColor:G, wickDownColor:R});
s.applyOptions({autoscaleInfoProvider: orig => { const r = orig(), t = items[cur]; if (!r || !t || S.view!=='signals' || t.stop==null) return r;
  return {priceRange:{minValue: Math.min(r.priceRange.minValue, t.stop, t.tp2), maxValue: Math.max(r.priceRange.maxValue, t.stop, t.tp2)}}; }});
function px(p){ return p >= 1000 ? p.toLocaleString('en-US',{maximumFractionDigits:0}) : p >= 1 ? p.toFixed(p>=100?2:3) : p.toPrecision(4); }
const pct = v => (v>0?'+':'') + v.toFixed(1) + '%';
async function j(u){ const r = await fetch(u + (u.includes('?')?'&':'?') + tok); return r.json(); }
const fmt = (t, d) => new Date(t*1000).toISOString().slice(0, d ? 10 : 16).replace('T',' ');
const step = () => S.tf==='1d' ? 86400 : 3600, snap = x => Math.floor(x/step())*step();
const EXIT = {tp2:'цель 2', tp1:'цель 1', stop:'стоп', breakeven_stop:'безубыток после цели 1', timeout:'по времени', trail_stop:'трейлинг стоп'};
function idx(t){ let lo=0, hi=times.length; while(lo<hi){const m=(lo+hi)>>1; if(times[m]<t) lo=m+1; else hi=m;} return lo; }
function xOf(t){ if (!times.length) return null; const tt = snap(t); let i = idx(tt);
  if (i>=times.length) i = times.length-1 + (tt-times[times.length-1])/step(); return chart.timeScale().logicalToCoordinate(i); }
// ---------- рисование поверх графика
const cv = $('ov'), g = cv.getContext('2d');
function chip(x, y, text, bg, fg){ g.font='600 12px -apple-system,Segoe UI,Roboto,sans-serif'; const w=g.measureText(text).width+12;
  g.fillStyle=bg; g.beginPath(); g.roundRect ? g.roundRect(x, y-10, w, 20, 4) : g.rect(x, y-10, w, 20); g.fill(); g.fillStyle=fg||'#0d1017'; g.fillText(text, x+6, y+4); return w; }
function draw(){
  const W = chart.timeScale().width(), H = $('c').clientHeight - 28, dpr = devicePixelRatio || 1;
  cv.width = W*dpr; cv.height = H*dpr; cv.style.width = W+'px'; cv.style.height = H+'px'; g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,W,H);
  const last = times[times.length-1];
  items.forEach((t, i) => {
    const sel = i === cur, x1 = xOf(t.entry_time); let x2 = xOf(t.exit_time || last);
    if (x1==null || x2==null) return; x2 = Math.max(x2, x1 + 4); if (x2 < 0 || x1 > W) return;
    if (S.view === 'portfolio') {                       // позиция портфеля: фон на всю высоту
      const c = t.side==='long' ? '61,220,151' : '255,107,107';
      g.fillStyle = `rgba(${c},${sel ? .1 : .05})`; g.fillRect(x1, 0, x2-x1, H);
      g.fillStyle = `rgba(${c},${sel ? 1 : .6})`; g.fillRect(x1, 0, x2-x1, 4);
      if (sel) { g.strokeStyle = `rgba(${c},.9)`; g.setLineDash([4,4]); g.beginPath(); g.moveTo(x1,0); g.lineTo(x1,H); g.moveTo(x2,0); g.lineTo(x2,H); g.stroke(); g.setLineDash([]); }
      if (x2 - x1 > 70 || sel) chip(Math.max(x1+4, 2), 18, (t.side==='long'?'ЛОНГ ':'ШОРТ ') + (t.pnl!=null ? pct(t.pnl) : ''), t.side==='long'?G:R);
      return;
    }
    const y = p => s.priceToCoordinate(p), ye = y(t.entry), ys = y(t.stop), y1 = y(t.tp1), y2 = y(t.tp2);
    if ([ye,ys,y1,y2].some(v => v==null)) return;
    const a = sel ? 1 : .45;
    g.fillStyle = `rgba(61,220,151,${.16*a})`; g.fillRect(x1, Math.min(ye,y2), x2-x1, Math.abs(y2-ye));
    g.fillStyle = `rgba(255,107,107,${.2*a})`; g.fillRect(x1, Math.min(ye,ys), x2-x1, Math.abs(ys-ye));
    const line = (yy, c, dash) => { g.strokeStyle=c; g.globalAlpha=a; g.setLineDash(dash||[]); g.lineWidth=1.3;
      g.beginPath(); g.moveTo(x1, yy); g.lineTo(x2, yy); g.stroke(); g.setLineDash([]); g.globalAlpha=1; };
    line(y2,G); line(y1,G,[3,3]); line(ye,B); line(ys,R);
    if (sel) { const lx = Math.min(x2 + 6, W - 120);
      chip(lx, y2, 'TP2 '+px(t.tp2), G); chip(lx, y1, 'TP1 '+px(t.tp1), G); chip(lx, ye, 'Вход '+px(t.entry), '#7aa7ff'); chip(lx, ys, 'Стоп '+px(t.stop), R); }
  });
}
const redraw = () => requestAnimationFrame(draw);
chart.timeScale().subscribeVisibleLogicalRangeChange(redraw); chart.timeScale().subscribeSizeChange(redraw); new ResizeObserver(redraw).observe($('c'));
chart.subscribeClick(p => { if (!p.time || !items.length) return;      // клик по графику выбирает ближайшую сделку
  let best = -1, d = 1e18; items.forEach((t,i) => { const a = t.entry_time, b = t.exit_time || 4e9;
    const dist = p.time >= a && p.time <= b ? 0 : Math.min(Math.abs(p.time-a), Math.abs(p.time-b)); if (dist < d) { d = dist; best = i; } });
  if (best >= 0 && d < step()*40) go(best); });
// ---------- данные
function segments(marks){                                 // смены стороны портфеля -> отрезки позиций с результатом
  const out = []; let open = null;
  const price = t => { const i = Math.min(idx(t), daily.length-1); return i >= 0 && daily[i] ? daily[i].open : null; };
  for (const m of marks) {
    if (open && m.side !== (open.side==='long'?1:-1)) { open.exit_time = m.time; open.exit = price(m.time); out.push(open); open = null; }
    if (!open && m.side !== 0) open = {side: m.side>0?'long':'short', entry_time: m.time, entry: price(m.time)};
  }
  if (open) { open.exit_time = null; open.exit = daily.length ? daily[daily.length-1].close : null; out.push(open); }
  out.forEach(t => { t.pnl = t.entry && t.exit ? (t.exit/t.entry - 1) * 100 * (t.side==='long'?1:-1) : null; t.r = t.pnl; });
  return out;
}
async function loadCandles(sym){
  cs = await j('api/candles?symbol='+sym+'&tf='+S.tf+(S.tf==='1h' && center ? '&center='+center : ''));
  cs = cs.filter(c => c.open!=null && c.close!=null); times = cs.map(c => c.time); s.setData(cs);
  const m = [];
  items.forEach((t, i) => { const long = t.side==='long';
    if (S.view==='signals') {
      m.push({time:snap(t.entry_time), position: long?'belowBar':'aboveBar', color:B, shape: long?'arrowUp':'arrowDown', size: i===cur?2:1, text: '#'+(i+1)});
      if (t.exit_time && t.r != null) m.push({time:snap(t.exit_time), position: long?'aboveBar':'belowBar', color: t.r>0?G:R,
        shape:'circle', size:1, text: S.tf==='1h' ? (t.r>0?'+':'')+t.r.toFixed(1)+'R' : ''});
    } else {
      m.push({time:snap(t.entry_time), position: long?'belowBar':'aboveBar', color: long?G:R, shape: long?'arrowUp':'arrowDown', size: i===cur?2:1});
    } });
  s.setMarkers(m.filter(x => times.length && x.time >= times[0] && x.time <= times[times.length-1]).sort((a,b)=>a.time-b.time));
  $('empty').style.display = cs.length ? 'none' : 'flex';
  $('empty').textContent = 'По этой монете ещё нет свечей: бот докачивает историю, обнови страницу через пару минут.';
  redraw();
}
async function go(i){
  if (!items.length) { cur=-1; render(); return; }
  cur = Math.max(0, Math.min(items.length-1, i)); const t = items[cur];
  if (S.tf==='1h' && (!times.length || t.entry_time < times[0] || (t.exit_time||t.entry_time) > times[times.length-1] + 3600)) {
    center = t.entry_time; await loadCandles($('sym').value); } else await loadCandles($('sym').value);
  const end = t.exit_time || times[times.length-1], span = Math.max(end - t.entry_time, step()*12);
  chart.timeScale().setVisibleLogicalRange({from: idx(t.entry_time - span*0.8), to: idx(end + span*0.5) + 3});
  render(); redraw();
}
function render(){
  const t = items[cur], name = $('sym').value.replace('USDT',''), P = S.view==='portfolio';
  const noun = P ? 'позиция' : 'сигнал';
  $('cnt').textContent = items.length ? `${noun} ${cur+1} из ${items.length}` : (P ? 'позиций портфеля нет' : 'сигналов нет');
  $('prev').disabled = cur <= 0; $('next').disabled = cur >= items.length-1;
  $('why').style.display = t ? 'none' : 'block';
  if (!t) { $('ttl').textContent=''; $('bdg').innerHTML=''; $('when').textContent=''; $('det').innerHTML='';
    $('why').innerHTML = P ? `Трендовый портфель по ${name} позиций не открывал${S.source==='live' ? ' с момента запуска бота' : ' за период бэктеста'}.` :
      S.source==='live' ? `По ${name} бот ещё не публиковал часовых сигналов. Историю смотри в режиме «Бэктест».` :
      ST.state==='work' ? `⏳ Бэктест пересчитывается (${ST.pct||0}%). Сигналы появятся, когда полоса дойдёт до 100%.` :
      !all.length ? 'Бэктест ещё не посчитан или нет истории открытого интереса (смотри предупреждение выше).' :
      `За период бэктеста ${BT.period||''} по ${name} часовых сигналов не было: стратегия входит только после сильного слива с падением открытого интереса. `+
      `Всего ${all.length} сигналов по всем монетам, монеты с сигналами стоят вверху списка. Основная доходность у трендового портфеля: переключатель «Трендовый портфель».`;
  } else {
    $('ttl').textContent = (P ? '' : '#'+(cur+1)+' ') + name;
    $('bdg').innerHTML = `<span class="badge ${t.side}">${t.side==='long'?'LONG':'SHORT'}</span>`;
    $('when').textContent = fmt(t.entry_time, P) + ' → ' + (t.exit_time ? fmt(t.exit_time, P) : 'сейчас, позиция открыта') +
      (P ? ` · ${Math.round(((t.exit_time || Date.now()/1000) - t.entry_time)/86400)} дн.` : ' UTC');
    const res = t.r==null ? 'открыта' : P ? pct(t.pnl) : (t.r>0?'+':'')+t.r.toFixed(2)+'R' + (t.pnl_pct!=null ? ` (${t.pnl_pct>0?'+':''}${t.pnl_pct.toFixed(2)}%)` : '');
    const c = P ? [['Цена входа',t.entry?px(t.entry):'','c-e'],['Цена выхода',t.exit?px(t.exit):'', ''],
        ['Движение в нашу сторону',`<span class="${t.pnl>0?'pos':'neg'}">${res}</span>`,''],['Статус',t.exit_time?'закрыта':'открыта','']]
      : [['Вход',px(t.entry),'c-e'],['Стоп',px(t.stop),'c-s'],['TP1',px(t.tp1),'c-t'],['TP2',px(t.tp2),'c-t'],
        ['Риск', t.risk_pct!=null ? t.risk_pct.toFixed(2)+'%' : '', ''],['Плечо', t.leverage!=null ? 'x'+t.leverage.toFixed(1) : '', ''],
        ['Итог', `<span class="${t.r>0?'pos':t.r<0?'neg':''}">${res}</span>`, ''],['Закрыта', EXIT[t.exit_event] || (t.exit_time?'':'—'), '']];
    $('det').innerHTML = c.filter(x=>x[1]!=='').map(([k,v,cl]) => `<div class="card ${cl}"><span>${k}</span><b>${v}</b></div>`).join('');
  }
  // сводка по монете
  const done = items.filter(x => x.r != null), win = done.filter(x => x.r > 0).length;
  const tot = done.reduce((a,x) => a + x.r, 0);
  $('sum').innerHTML = items.length ? `<span><b>${name}</b></span><span>${P?'позиций':'сигналов'}: <b>${items.length}</b></span>
    <span>в плюс: <b class="pos">${win}</b>, в минус: <b class="neg">${done.length-win}</b></span>
    <span>${P?'сумма движений':'итог'}: <b class="${tot>0?'pos':'neg'}">${P ? pct(tot) : (tot>0?'+':'')+tot.toFixed(2)+'R'}</b></span>` : '';
  $('th').textContent = (P ? 'Позиции трендового портфеля' : 'Часовые сигналы') + ' по ' + name + (S.source==='live' ? ' (живые)' : ' (бэктест)');
  $('thead').innerHTML = P ? '<tr><th>Сторона</th><th>Открыта</th><th>Закрыта</th><th>Дней</th><th>Результат</th></tr>'
    : '<tr><th>#</th><th>Сторона</th><th>Вход</th><th>Выход</th><th>Как закрылась</th><th>Результат</th></tr>';
  $('tbl').querySelector('tbody').innerHTML = items.map((t,i)=>[t,i]).reverse().map(([t,i]) => {
    const side = `<td class="${t.side==='long'?'pos':'neg'}">${t.side==='long'?'лонг':'шорт'}</td>`, cl = t.r>0?'pos':t.r<0?'neg':'';
    return `<tr data-i="${i}" class="${i===cur?'sel':''}">` + (P
      ? `${side}<td>${fmt(t.entry_time,1)}</td><td>${t.exit_time?fmt(t.exit_time,1):'открыта'}</td><td>${Math.round(((t.exit_time||Date.now()/1000)-t.entry_time)/86400)}</td><td class="${cl}">${t.pnl==null?'':pct(t.pnl)}</td>`
      : `<td>${i+1}</td>${side}<td>${fmt(t.entry_time)}</td><td>${t.exit_time?fmt(t.exit_time):'открыта'}</td><td>${EXIT[t.exit_event]||''}</td><td class="${cl}">${t.r==null?'':(t.r>0?'+':'')+t.r.toFixed(2)+'R'}</td>`) + '</tr>'; }).join('')
    || `<tr><td colspan=6 class="muted">пусто</td></tr>`;
  document.querySelectorAll('#tbl tbody tr[data-i]').forEach(r => r.onclick = () => { go(+r.dataset.i); window.scrollTo({top:0, behavior:'smooth'}); });
  $('legend').innerHTML = P
    ? '<span><i style="background:rgba(61,220,151,.5)"></i>портфель держит лонг</span><span><i style="background:rgba(255,107,107,.5)"></i>портфель держит шорт</span><span>▲▼ открытие позиции</span><span>процент: движение цены в нашу сторону за время позиции (вес в портфеле меняется каждый день)</span>'
    : '<span><i style="background:'+B+'"></i>▲▼ вход, #номер сигнала</span><span><i style="background:rgba(61,220,151,.5)"></i>зона цели</span><span><i style="background:rgba(255,107,107,.5)"></i>зона риска</span><span><i style="background:'+G+';border-radius:50%"></i><i style="background:'+R+';border-radius:50%"></i>выход в плюс / минус</span>';
  $('hint').textContent = 'Листай ◀ ▶ или стрелками на клавиатуре, кликни по сделке на графике или в таблице. ' +
    (S.source==='live' ? 'Живые: то, что ушло в канал.' : 'Бэктест: история, в канал не публикуется.');
}
async function fillSymbols(){
  const sel = $('sym'), keep = sel.value || q.get('symbol');
  const syms = await j('api/symbols'); all = await j('api/trades?source='+S.source);
  const n = {}; all.forEach(t => n[t.symbol] = (n[t.symbol]||0)+1);
  const list = [...new Set([...syms, ...Object.keys(n)])].sort((a,b) => (n[b]||0)-(n[a]||0) || syms.indexOf(a)-syms.indexOf(b));
  sel.innerHTML = list.map(x => `<option value="${x}">${x.replace('USDT','')}${S.view==='signals' && n[x] ? ' · '+n[x]+' сигн.' : ''}</option>`).join('');
  if (keep && list.includes(keep)) sel.value = keep;
}
async function load(){
  const sym = $('sym').value; center = 0; s.applyOptions({priceLineVisible: S.source==='live'});
  chart.applyOptions({timeScale:{timeVisible: S.tf==='1h'}});
  if (S.view === 'signals') items = all.filter(t => t.symbol === sym).sort((a,b) => a.entry_time-b.entry_time);
  else { daily = await j('api/candles?symbol='+sym+'&tf=1d'); const dt = daily.map(c => c.time);
    const save = times; times = dt; items = segments((await j('api/marks?symbol='+sym+'&source='+S.source))
      .filter(m => S.source==='backtest' ? m.kind==='portfolio' : m.kind==='portfolio_live').sort((a,b)=>a.time-b.time)); times = save; }
  if (items.length) await go(items.length-1);
  else { cur=-1; await loadCandles(sym); if (S.tf==='1h') chart.timeScale().setVisibleLogicalRange({from: cs.length-240, to: cs.length+8}); else chart.timeScale().fitContent(); render(); }
  const st = await j('api/stats?source=backtest'); BT = {period: st['период'] || ''};
  const names = {version:'версия', 'посчитан':'посчитан', trades:'сделок', 'return %':'доходность, %', 'buy&hold %':'buy and hold, %', 'max DD %':'просадка, %',
    'win %':'прибыльных, %', 'profit factor':'profit factor', 'beats B&H and cash':'лучше рынка и кэша', backtest:'бэктест'};
  $('stats').innerHTML = Object.entries(st).filter(([k]) => k!=='период').map(([k,v]) =>
    `<div class="card"><span>${names[k]||k}</span><b>${v==='yes'?'да':v==='no'?'нет':v}</b></div>`).join('');
  render();
}
async function poll(){ try { const st = await j('api/status'); ST = st; const b = $('status');
  const show = st.state==='work' || st.state==='fail' || (st.warn||[]).length || (st.state==='ok' && Date.now()/1000 - st.ts < 60);
  b.style.display = show ? 'block' : 'none'; b.className = 'st-'+st.state;
  const icon = st.state==='work' ? '⏳' : st.state==='ok' ? '✅' : '❌';
  b.innerHTML = `<b>${icon} ${st.state==='work' ? 'Подготовка данных: ' + st.pct + '%' : st.state==='ok' ? 'Подготовка завершена' : 'Ошибка подготовки'}</b>
    <span class="muted">&nbsp; ${st.text||''}</span><div class="bar"><i style="width:${st.state==='ok'?100:st.pct||0}%"></i></div>` +
    (st.warn||[]).map(w => `<div class="warn">⚠️ ${w}</div>`).join('');
  if (st.state==='ok' && poll.was==='work') { await fillSymbols(); load(); } poll.was = st.state; } catch(e){} setTimeout(poll, 3000); }
function setUI(){ document.querySelectorAll('.seg button').forEach(b => b.classList.toggle('on', S[b.dataset.g] === b.dataset.v)); }
document.querySelectorAll('.seg button').forEach(b => b.onclick = async () => {
  S[b.dataset.g] = b.dataset.v;
  if (b.dataset.g === 'view') S.tf = S.view === 'portfolio' ? '1d' : '1h';
  setUI(); if (b.dataset.g !== 'tf') await fillSymbols(); load(); });
$('prev').onclick = () => go(cur-1); $('next').onclick = () => go(cur+1);
document.addEventListener('keydown', e => { if (e.target.tagName==='SELECT') return; if (e.key==='ArrowLeft') go(cur-1); if (e.key==='ArrowRight') go(cur+1); });
$('sym').onchange = load;
(async () => { $('ovl').href = 'overview' + (tok ? '?'+tok : ''); if (S.view==='portfolio') S.tf='1d'; setUI();
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
