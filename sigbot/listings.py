"""Шорт новых листингов фьючерсов Binance.

Исследование 2020-2026 по 887 листингам: 71% новых монет за 60 дней дешевеют, медиана −19%.
Среднее портит редкий вынос на сотни процентов, поэтому стоп +50%. Исполнение стопа проверено
по минутным свечам: проскальзывание около 0.5%. Вход на закрытии первых полных суток после листинга,
выход по стопу или через 60 дней. Размер маленький и одинаковый: прибыль даёт число сделок, а не одна."""
from __future__ import annotations

import logging

from . import templates

log = logging.getLogger(__name__)
DAY = 86_400_000


class ListingShorts:
    def __init__(self, cfg, conn, publisher, binance):
        self.cfg, self.conn, self.pub, self.bn = cfg, conn, publisher, binance

    def open_trades(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM listing_trades WHERE status='open'")]

    async def run_daily(self, now: int) -> list[str]:
        lc = self.cfg["listings"]
        posts = []
        # 1. новые листинги
        for sym, onboard in await self.bn.perp_listings():
            if onboard <= 0 or now - onboard > (lc["max_entry_delay_days"] + 2) * DAY or now - onboard < DAY:
                continue
            if self.conn.execute("SELECT 1 FROM listing_trades WHERE symbol=?", (sym,)).fetchone():
                continue
            k = await self.bn.klines(sym, "futures", "1d", onboard // DAY * DAY, 10)
            if len(k) < 2:
                continue
            entry_close = k[1][0] + DAY                      # закрытие первых полных суток
            if entry_close > now:
                continue
            if now - entry_close > lc["max_entry_delay_days"] * DAY:
                self.conn.execute("INSERT INTO listing_trades (symbol, onboard_ts, status) VALUES (?,?,'skipped')",
                                  (sym, onboard))
                continue
            entry = k[1][4]
            stop = entry * (1 + lc["stop_pct"] / 100)
            due = entry_close + lc["hold_days"] * DAY
            self.conn.execute("INSERT INTO listing_trades VALUES (?,?,?,?,?,?,?, 'open', NULL, NULL, NULL, NULL)",
                              (sym, onboard, entry_close, entry, stop, due, lc["notional_pct"]))
            text, nums = templates.listing_signal(sym, entry, stop, lc, self.cfg["publishing"]["mode"])
            posts.append(await self.pub.publish("listing", text, nums))
        # 2. ведение: стоп по максимуму суток, иначе выход по времени
        for t in self.open_trades():
            k = await self.bn.klines(t["symbol"], "futures", "1d", t["entry_ts"], 100)
            exit_price = reason = None
            exit_ts = None
            for ts, o, h, lo, c, *_ in k:
                if ts + DAY > now:
                    break                                    # только закрытые сутки
                if h >= t["stop"]:
                    exit_price, reason, exit_ts = t["stop"], "stop", ts + DAY
                    break
                if ts + DAY >= t["exit_due_ts"]:
                    exit_price, reason, exit_ts = c, "time", ts + DAY
                    break
            if exit_price is None:
                continue
            fee = 2 * self.cfg["trade"]["fee_taker_pct"]
            pnl = (-(exit_price / t["entry"] - 1) * 100 - fee) * t["notional_pct"] / 100
            self.conn.execute("UPDATE listing_trades SET status='closed', exit_ts=?, exit_price=?, exit_reason=?, "
                              "pnl_pct=? WHERE symbol=?", (exit_ts, exit_price, reason, pnl, t["symbol"]))
            text, nums = templates.listing_exit(t["symbol"], t["entry"], exit_price, reason, pnl,
                                                self.cfg["publishing"]["mode"])
            posts.append(await self.pub.publish("listing", text, nums))
        return posts
