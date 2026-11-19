"""Шорт новых листингов фьючерсов Binance.

Исследование 2020-2026 по 887 листингам: 71% новых монет за 60 дней дешевеют, медиана −19%.
Среднее портит редкий вынос на сотни процентов, поэтому стоп +50%. Исполнение стопа проверено
по минутным свечам: проскальзывание около 0.5%. Вход на закрытии первых полных суток после листинга,
выход по стопу или через 60 дней. Размер маленький и одинаковый: прибыль даёт число сделок, а не одна.

Фильтр: шортятся только листинги, у которых первые полные сутки закрылись ниже цены открытия листинга.
Таких 60%, результат +7.0% на сделку против +3.8% у всех подряд, плюс в каждом году 2020-2026.

Bybit (listings.bybit): те же правила для монет, которых на момент запуска на Bybit нет на фьючерсах Binance.
Бэктест по Bybit считается на компьютере с ботом (из облака Bybit закрыт), итог в /menu → Стратегии."""
from __future__ import annotations

import logging

from . import settings, templates

log = logging.getLogger(__name__)
DAY = 86_400_000


class ListingShorts:
    def __init__(self, cfg, conn, publisher, binance, bybit=None):
        self.cfg, self.conn, self.pub, self.bn, self.bb = cfg, conn, publisher, binance, bybit

    def src(self, exchange: str | None):
        return self.bb if exchange == "bybit" and self.bb else self.bn

    def open_trades(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM listing_trades WHERE status='open'")]

    def _lang(self) -> str:
        from . import settings
        return settings.effective(self.cfg, self.conn)["publishing"].get("lang", "en")

    async def run_daily(self, now: int) -> list[str]:
        lc = self.cfg["listings"]
        posts = []
        # 1. новые листинги
        try:
            kinds = await self.bn.perp_kinds()
            settings.put(self.conn, "perp_kinds", kinds)
        except Exception:
            kinds = settings.get(self.conn, "perp_kinds") or {}
        bn_list = await self.bn.perp_listings()
        excl = {x.upper().replace("USDT", "") for x in lc.get("exclude", [])}
        bad = lambda sym: kinds.get(sym, "COIN") != "COIN" or sym.replace("USDT", "") in excl
        for t in self.open_trades():                     # акция попала в шорт раньше, до фильтра: снимаем без поста
            if bad(t["symbol"]):
                self.conn.execute("UPDATE listing_trades SET status='cancelled', exit_reason='not_crypto' WHERE symbol=?",
                                  (t["symbol"],))
                if getattr(self, "demo", None):
                    await self.demo.reduce("listing", t["symbol"], None, "не криптовалюта")
                await self.pub.notify_owner(f"🆕 {t['symbol'].replace('USDT', '')}: это не криптовалюта (акция или фонд), "
                                            "сделка шорта листинга снята из учёта.")
        cands = [(sym, onboard, "binance") for sym, onboard in bn_list if not bad(sym)]
        # акции, золото, валюты на Binance: не новые токены, правило к ним не относится
        if self.bb and settings.effective(self.cfg, self.conn)["listings"].get("bybit"):
            first_bn = dict(bn_list)
            try:
                for sym, launch in await self.bb.perp_listings():
                    if (sym in first_bn and first_bn[sym] <= launch + 2 * DAY) or bad(sym):   # уже есть на Binance или акция
                        continue
                    cands.append((sym, launch, "bybit"))
            except Exception as e:
                log.warning("bybit listings: %s", e)
        for sym, onboard, ex in cands:
            if onboard <= 0 or now - onboard > (lc["max_entry_delay_days"] + 2) * DAY or now - onboard < DAY:
                continue
            if self.conn.execute("SELECT 1 FROM listing_trades WHERE symbol=?", (sym,)).fetchone():
                continue
            try:
                k = await self.src(ex).klines(sym, "futures", "1d", onboard // DAY * DAY, 10)
            except Exception as e:
                log.warning("listing klines %s %s: %s", ex, sym, e)
                continue
            if len(k) < 2:
                continue
            entry_close = k[1][0] + DAY                      # закрытие первых полных суток
            if entry_close > now:
                continue
            if now - entry_close > lc["max_entry_delay_days"] * DAY:
                self.conn.execute("INSERT INTO listing_trades (symbol, onboard_ts, status, exchange) VALUES (?,?,'skipped',?)",
                                  (sym, onboard, ex))
                continue
            entry = k[1][4]
            if lc.get("require_weak_first_day", True) and not entry < k[0][1]:
                # первые сутки закрылись выше цены открытия листинга: горячие листинги чаще выносят шорты
                self.conn.execute("INSERT INTO listing_trades (symbol, onboard_ts, status, exchange) VALUES (?,?,'filtered',?)",
                                  (sym, onboard, ex))
                continue
            stop = entry * (1 + lc["stop_pct"] / 100)
            due = entry_close + lc["hold_days"] * DAY
            self.conn.execute("INSERT INTO listing_trades (symbol, onboard_ts, entry_ts, entry, stop, exit_due_ts, notional_pct, "
                              "status, exchange) VALUES (?,?,?,?,?,?,?, 'open', ?)",
                              (sym, onboard, entry_close, entry, stop, due, lc["notional_pct"], ex))
            if getattr(self, "demo", None):
                await self.demo.open("listing", sym, sym, "short", lc["notional_pct"], ex if ex == "bybit" else None,
                                     stop=stop)
            text, nums = templates.listing_signal(sym, entry, stop, lc, self.cfg["publishing"]["mode"], self._lang(), ex)
            posts.append(await self.pub.publish("listing", text, nums))
        # 2. ведение: стоп по максимуму суток, иначе выход по времени
        for t in self.open_trades():
            try:
                k = await self.src(t.get("exchange")).klines(t["symbol"], "futures", "1d", t["entry_ts"], 100)
            except Exception as e:
                log.warning("listing klines %s: %s", t["symbol"], e)
                continue
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
            if getattr(self, "demo", None):
                await self.demo.reduce("listing", t["symbol"], None, "стоп" if reason == "stop" else "выход по времени")
            text, nums = templates.listing_exit(t["symbol"], t["entry"], exit_price, reason, pnl,
                                                self.cfg["publishing"]["mode"], self._lang(),
                                                int((exit_ts - t["entry_ts"]) / DAY))
            posts.append(await self.pub.publish("listing", text, nums))
        return posts
