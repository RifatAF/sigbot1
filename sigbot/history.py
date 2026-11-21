"""Дозаполнение истории через обычный API Binance (тот же, по которому бот собирает живые свечи).

Архив data.binance.vision у части провайдеров не открывается или рвётся: тогда в базе остаются дыры
и короткая история. Здесь ищутся пропуски часовых свечей и фандинга за период и докачиваются по API."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)
HOUR = 3_600_000


def missing_ranges(ts: list[int], start: int, end: int, step: int = HOUR) -> list[tuple[int, int]]:
    """Отсутствующие отрезки [a, b] в ряду ts на окне [start, end)."""
    out, prev = [], start - step
    for t in ts:
        if t - prev > step:
            out.append((prev + step, t - step))
        prev = max(prev, t)
    if end - prev > step:
        out.append((prev + step, end - step))
    return out


def coverage(conn, symbol: str, start: int, end: int, market: str = "futures") -> float:
    n = conn.execute("SELECT COUNT(*) FROM candles WHERE symbol=? AND market=? AND tf='1h' AND ts>=? AND ts<?",
                     (symbol, market, start, end)).fetchone()[0]
    return n / max(1, (end - start) // HOUR)


async def fill_candles(conn, bn, symbol: str, market: str, start: int, end: int) -> int:
    ts = [r[0] for r in conn.execute("SELECT ts FROM candles WHERE symbol=? AND market=? AND tf='1h' "
                                     "AND ts>=? AND ts<? ORDER BY ts", (symbol, market, start, end))]
    lim = 1500 if market == "futures" else 1000
    n = 0
    for a, b in missing_ranges(ts, start, end):
        cur = a
        while cur <= b:
            try:
                rows = await bn.klines(symbol, market, "1h", start_ms=cur, limit=lim)
            except Exception as e:          # спотовой пары может не быть
                log.info("history %s %s: %s", symbol, market, str(e)[:100])
                return n
            rows = [r for r in rows if r[0] <= b]
            if not rows:
                break
            conn.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)",
                             [(symbol, market, "1h", *r) for r in rows])
            n += len(rows)
            cur = rows[-1][0] + HOUR
    return n


async def fill_funding(conn, bn, symbol: str, start: int, end: int) -> int:
    have = conn.execute("SELECT COUNT(*) FROM funding WHERE symbol=? AND ts>=? AND ts<?", (symbol, start, end)).fetchone()[0]
    if have >= (end - start) / (8 * HOUR) * 0.95:
        return 0
    n, cur = 0, start
    while cur < end:
        try:
            rows = await bn.funding_hist(symbol, cur)
        except Exception as e:                      # сеть или биржа недоступна: не роняем подготовку
            log.info("funding %s: %s", symbol, str(e)[:100])
            break
        if not rows:
            break
        conn.executemany("INSERT OR REPLACE INTO funding VALUES (?,?,?)", [(symbol, t, r) for t, r in rows])
        n += len(rows)
        if len(rows) < 1000:
            break
        cur = rows[-1][0] + 1
    return n


async def fill(conn, bn, symbol: str, start: int, end: int) -> tuple[int, int, int]:
    return (await fill_candles(conn, bn, symbol, "futures", start, end),
            await fill_candles(conn, bn, symbol, "spot", start, end),
            await fill_funding(conn, bn, symbol, start, end))
