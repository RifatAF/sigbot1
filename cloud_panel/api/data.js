// Телефон читает данные панели по ключу из ссылки, которую бот присылает в Telegram.
import { head } from '@vercel/blob';
import crypto from 'node:crypto';

export default async function handler(req, res) {
  const t = String(req.query.t || '');
  const k = String(req.query.k || '');
  res.setHeader('cache-control', 'no-store');
  if (t.length < 16 || !/^[a-z_]{1,24}$/.test(k)) return res.status(403).json({ error: 'Нужна ссылка с ключом из Telegram: /menu → 📱 Панель на телефоне' });
  const h = crypto.createHash('sha256').update(t).digest('hex');
  try {
    const b = await head(`d/${h}/${k}.json`);
    const r = await fetch(b.url);
    if (!r.ok) throw new Error(String(r.status));
    res.setHeader('content-type', 'application/json; charset=utf-8');
    return res.status(200).send(Buffer.from(await r.arrayBuffer()));
  } catch (e) {
    return res.status(404).json({ error: 'Данных нет: бот ещё не прислал их или ключ неверный' });
  }
}
