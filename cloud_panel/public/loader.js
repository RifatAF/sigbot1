// Панель sigbot в облаке: страницу и данные присылает домашний бот, свечи телефон берёт прямо с биржи.
(async function () {
  const q = new URLSearchParams(location.search);
  let T = q.get('t') || q.get('token');
  try { if (T) localStorage.setItem('sigbot_t', T); else T = localStorage.getItem('sigbot_t'); } catch (e) {}
  const say = m => { document.getElementById('msg').textContent = m; };
  if (!T) return say('Открой ссылку из Telegram: /menu → 📱 Панель на телефоне. В ней ключ доступа.');
  const page = location.pathname.replace(/\/+$/, '') === '/layers' ? 'page_layers' : 'page_index';
  let r;
  try { r = await fetch('/api/data?k=' + page + '&t=' + encodeURIComponent(T)); } catch (e) { return say('Нет интернета'); }
  if (!r.ok) return say((await r.json().catch(() => ({}))).error || 'Ошибка ' + r.status);
  const html = (await r.json()).html;
  const shim = '<script>window.__SIGBOT_T=' + JSON.stringify(T) + ';(' + SHIM.toString() + ')();<\/script>';
  document.open();
  document.write(html.replace(/<head>/i, '<head><base href="/">' + shim));
  document.close();
})();

// Подменяет запросы панели к домашнему серверу: данные из облака, свечи с биржи.
function SHIM() {
  const T = window.__SIGBOT_T, of = window.fetch.bind(window), cache = {};
  const bundle = k => cache[k] || (cache[k] = of('/api/data?k=' + k + '&t=' + encodeURIComponent(T)).then(r => r.ok ? r.json() : {}));
  const J = o => new Response(JSON.stringify(o), { headers: { 'content-type': 'application/json' } });
  async function rows(urls) {
    for (const u of urls) {
      try {
        const r = await of(u); if (!r.ok) continue;
        const a = await r.json();
        const x = Array.isArray(a) ? a : (a.result && a.result.list ? a.result.list.slice().reverse() : null);
        if (x && x.length) return x;
      } catch (e) {}
    }
    return [];
  }
  async function candles(sym, tf, center, ex) {
    const H = 3600000, d = tf === '1d';
    const st = !d && center ? center * 1000 - 1200 * H : 0;
    const bi = d ? '1d' : '1h', by = d ? 'D' : '60';
    const fa = `https://fapi.binance.com/fapi/v1/klines?symbol=${sym}&interval=${bi}&limit=1500` + (st ? '&startTime=' + st : '');
    const sp = `https://data-api.binance.vision/api/v3/klines?symbol=${sym}&interval=${bi}&limit=1000` + (st ? '&startTime=' + st : '');
    const bb = `https://api.bybit.com/v5/market/kline?category=linear&symbol=${sym}&interval=${by}&limit=1000` + (st ? '&start=' + st : '');
    const x = await rows(ex === 'bybit' ? [bb, fa, sp] : [fa, bb, sp]);
    const seen = new Set();
    return x.map(k => ({ time: Math.floor(+k[0] / 1000), open: +k[1], high: +k[2], low: +k[3], close: +k[4] }))
      .filter(c => !seen.has(c.time) && seen.add(c.time)).sort((a, b) => a.time - b.time);
  }
  window.fetch = async function (u, o) {
    const s = typeof u === 'string' ? u : u.url;
    const m = s.replace(location.origin, '').replace(/^\/+/, '');
    if (!m.startsWith('api/')) return of(u, o);
    const url = new URL(m, 'http://x/'), p = url.pathname.slice(1), g = k => url.searchParams.get(k);
    if (p === 'api/feed') {
      if (g('source') === 'backtest') return J((await bundle('daily')).feed_bt || { items: [], note: 'история ещё не пришла' });
      return J((await bundle('live')).feed || { items: [] });
    }
    if (p === 'api/paper') return J((await bundle('live')).paper || {});
    if (p === 'api/status') return J((await bundle('live')).status || {});
    if (p === 'api/layers') {
      const [dl, lv] = await Promise.all([bundle('daily'), bundle('live')]);
      return J({ ...(dl.layers || {}), ...(lv.layers_paper ? { paper: lv.layers_paper } : {}) });
    }
    if (p === 'api/candles') return J(await candles(g('symbol') || 'BTCUSDT', g('tf'), +g('center') || 0, g('ex')));
    return J({});
  };
  addEventListener('DOMContentLoaded', () => document.querySelectorAll('a[href="overview"],a[href="coins"]').forEach(a => a.remove()));
  bundle('live').then(b => {
    if (!b.ts) return;
    const min = Math.round((Date.now() / 1000 - b.ts) / 60);
    const el = document.createElement('div');
    el.style.cssText = 'position:fixed;right:8px;bottom:8px;background:#151a24;color:#8a93a6;border:1px solid #232b3a;border-radius:8px;padding:4px 8px;font:12px sans-serif;z-index:99';
    el.textContent = '☁️ данные бота: ' + (min < 1 ? 'только что' : min + ' мин назад');
    (document.body ? Promise.resolve() : new Promise(r => addEventListener('DOMContentLoaded', r))).then(() => document.body.appendChild(el));
  });
}
