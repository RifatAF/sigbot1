// Бот присылает сюда данные панели (только исходящий запрос с домашнего компьютера, туннель не нужен).
// Данные лежат под путём из хэша ключа: без ключа их не найти и не перезаписать.
import { put } from '@vercel/blob';
import crypto from 'node:crypto';

export default async function handler(req, res) {
  const t = String(req.headers['x-token'] || '');
  const k = String(req.query.k || '');
  if (req.method !== 'POST' || t.length < 16) return res.status(403).json({ error: 'no token' });
  if (!/^[a-z_]{1,24}$/.test(k)) return res.status(400).json({ error: 'bad key' });
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const body = Buffer.concat(chunks);
  const h = crypto.createHash('sha256').update(t).digest('hex');
  await put(`d/${h}/${k}.json`, body, {
    access: 'public', addRandomSuffix: false, allowOverwrite: true,
    contentType: 'application/json', cacheControlMaxAge: 60,
  });
  return res.status(200).json({ ok: true, bytes: body.length });
}
