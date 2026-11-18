"""Бумажная торговля: виртуальные счета «Binance» и «Bybit» внутри бота, по 100$ на старте.

Бот повторяет свои сигналы как живой трейдер: вход рыночным ордером по текущей цене биржи с проскальзыванием,
комиссия тейкера, плечо и изолированная маржа, фандинг каждые 8 часов, ликвидация, частичные выходы на TP1 и TP2,
закрытие остатка на стопе или выходе. На биржу ничего не отправляется: ключи не нужны, ордеров бот не ставит.

Цены: последние свечи из базы бота, а если монеты там нет (свежий листинг), открытый API биржи."""
from __future__ import annotations

import logging
import time

from . import settings

log = logging.getLogger(__name__)
HOUR = 3_600_000
FEES = {"binance": 0.05, "bybit": 0.055}          # тейкер, % от объёма
MMR = 0.005                                       # поддерживающая маржа: ликвидация, когда убыток съел маржу почти целиком


class DemoTrader:
    """Счета в таблице paper_accounts, позиции в demo_trades (одна строка на сигнал и биржу)."""

    def __init__(self, cfg, conn, notify=None, prices=None):
        self.cfg, self.conn, self.notify = cfg, conn, notify
        self.prices = prices                       # для тестов: функция (exchange, symbol) -> цена

    # ---------- настройки и счёт
    def pc(self) -> dict:
        return settings.effective(self.cfg, self.conn).get("paper", {})

    def on(self) -> bool:
        e = settings.effective(self.cfg, self.conn)
        return bool(e.get("paper", {}).get("enabled", True)) and not e["publishing"].get("kill_switch")

    def exchanges(self) -> list[str]:
        return list(self.pc().get("exchanges", ["binance", "bybit"]))

    def account(self, ex: str) -> dict:
        r = self.conn.execute("SELECT * FROM paper_accounts WHERE exchange=?", (ex,)).fetchone()
        if r:
            return dict(r)
        start = float(self.pc().get("start_usd", 100))
        self.conn.execute("INSERT INTO paper_accounts (exchange, balance, start_usd, fees, funding, created_ts) "
                          "VALUES (?,?,?,0,0,?)", (ex, start, start, int(time.time() * 1000)))
        return self.account(ex)

    def reset(self):
        self.conn.execute("DELETE FROM demo_trades")
        self.conn.execute("DELETE FROM paper_accounts")

    def _book(self, ex: str, delta: float, fee: float = 0.0, funding: float = 0.0):
        self.account(ex)
        self.conn.execute("UPDATE paper_accounts SET balance=balance+?, fees=fees+?, funding=funding+? WHERE exchange=?",
                          (delta, fee, funding, ex))

    async def _say(self, text: str):
        if self.notify:
            try:
                await self.notify(text)
            except Exception:
                pass

    # ---------- цены
    async def price(self, ex: str, sym: str, fast: bool = False) -> float | None:
        """Цена для ордера. fast (проверка ликвидаций раз в минуту): свежая свеча из базы для обеих бирж."""
        if self.prices:
            return self.prices(ex, sym)
        r = self.conn.execute("SELECT close, ts FROM candles WHERE symbol=? AND market='futures' AND tf IN ('1m','1h') "
                              "ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
        if r and time.time() * 1000 - r[1] < 2 * HOUR and (ex == "binance" or fast):
            return float(r[0])
        from .http import Http
        from .sources import Binance, Bybit
        http = Http(retries=2)
        try:
            for cls in ([Bybit, Binance] if ex == "bybit" else [Binance, Bybit]):
                try:
                    k = await cls(http).klines(sym, "futures", "1h", None, 2) if cls is Binance else \
                        await cls(http).klines(sym, "futures", "1h", int(time.time() * 1000) - 3 * HOUR, 3)
                    if k:
                        return float(k[-1][4])
                except Exception:
                    continue
        finally:
            await http.close()
        return float(r[0]) if r else None

    def _fill(self, px: float, buy: bool) -> float:
        slip = float(self.pc().get("slippage_pct", 0.02)) / 100
        return px * (1 + slip if buy else 1 - slip)

    # ---------- сделки
    async def open(self, kind: str, ref: str, symbol: str, side: str, notional_pct: float, exchange: str | None = None):
        """Вход: notional_pct % от капитала счёта (как размер в сигнале). exchange: только этот счёт, иначе оба."""
        if not self.on() or notional_pct <= 0:
            return
        lev = max(1, int(self.pc().get("leverage", 5)))
        for ex in self.exchanges():
            if exchange and ex != exchange:
                continue
            if self.conn.execute("SELECT 1 FROM demo_trades WHERE exchange=? AND kind=? AND ref=?", (ex, kind, ref)).fetchone():
                continue
            name = f"Бумажный {ex.capitalize()}"
            px = await self.price(ex, symbol)
            if not px:
                await self._say(f"🧪 {name}: {symbol.replace('USDT', '')} не открыт, нет цены")
                continue
            eq = self.equity(ex)
            notional = eq * notional_pct / 100
            margin = notional / lev
            if notional < 5:
                await self._say(f"🧪 {name}: {symbol.replace('USDT', '')} не открыт: позиция {notional:.2f}$ меньше минимума биржи 5$")
                continue
            if margin > self.free(ex):
                await self._say(f"🧪 {name}: {symbol.replace('USDT', '')} не открыт: не хватает свободной маржи "
                                f"({margin:.2f}$ нужно, {self.free(ex):.2f}$ есть)")
                continue
            fill = self._fill(px, side == "long")
            qty = notional / fill
            fee = notional * FEES.get(ex, 0.05) / 100
            sd = 1 if side == "long" else -1
            liq = fill * (1 - sd / lev + sd * MMR)
            self._book(ex, -fee, fee=fee)
            self.conn.execute("INSERT INTO demo_trades (exchange, kind, ref, symbol, side, qty, open_qty, entry, opened_ts, status, "
                              "margin, leverage, liq, fees, funding, realized, last_funding) VALUES (?,?,?,?,?,?,?,?,?, 'open', ?,?,?,?,0,?,?)",
                              (ex, kind, ref, symbol, side, qty, qty, fill, int(time.time() * 1000), margin, lev, liq, fee,
                               -fee, int(time.time() * 1000) // (8 * HOUR)))       # итог сделки с комиссией входа
            await self._say(f"🧪 {name}: {'лонг' if side == 'long' else 'шорт'} {symbol.replace('USDT', '')} по {fill:g}, "
                            f"позиция {notional:.2f}$ (маржа {margin:.2f}$, плечо x{lev}), комиссия {fee:.3f}$")

    async def reduce(self, kind: str, ref: str, part: float | None, why: str):
        """Закрыть долю part исходной позиции (None: всё, что осталось)."""
        for r in self.conn.execute("SELECT * FROM demo_trades WHERE kind=? AND ref=? AND status='open'", (kind, ref)).fetchall():
            px = await self.price(r["exchange"], r["symbol"])
            if not px:
                continue
            await self._close(r, px, part, why)

    async def _close(self, r, px: float, part: float | None, why: str, liquidated: bool = False):
        ex, sd = r["exchange"], 1 if r["side"] == "long" else -1
        qty = r["open_qty"] if part is None else min(r["open_qty"], r["qty"] * part)
        if r["open_qty"] - qty < r["qty"] * 1e-6:
            qty = r["open_qty"]
        fill = px if liquidated else self._fill(px, sd < 0)
        gross = (fill - r["entry"]) * qty * sd
        fee = fill * qty * FEES.get(ex, 0.05) / 100
        if liquidated:
            gross = -r["margin"] * qty / r["qty"]                   # ликвидация забирает маржу этой части
        left = r["open_qty"] - qty
        closed = left <= r["qty"] * 1e-6
        self._book(ex, gross - fee, fee=fee)
        self.conn.execute("UPDATE demo_trades SET open_qty=?, realized=realized+?, fees=fees+?, status=?, closed_ts=?, "
                          "exit=? WHERE id=?", (0 if closed else left, gross - fee, fee, "closed" if closed else "open",
                                                int(time.time() * 1000) if closed else None, fill, r["id"]))
        tot = r["realized"] + gross - fee                           # с комиссиями и фандингом
        await self._say(f"🧪 Бумажный {ex.capitalize()}: {why}, {symbol_name(r)} закрыто {qty / r['qty'] * 100:.0f}% по {fill:g}, "
                        f"{gross - fee:+.2f}$" + (f". Сделка закрыта, итог {tot:+.2f}$" if closed else "")
                        + f". Баланс {self.account(ex)['balance']:.2f}$")

    # ---------- раз в минуту: фандинг и ликвидации
    async def tick(self, now_ms: int | None = None):
        now = now_ms or int(time.time() * 1000)
        period = now // (8 * HOUR)
        for r in self.conn.execute("SELECT * FROM demo_trades WHERE status='open'").fetchall():
            px = await self.price(r["exchange"], r["symbol"], fast=True)
            if not px:
                continue
            sd = 1 if r["side"] == "long" else -1
            if (px - r["liq"]) * sd <= 0:
                await self._close(r, r["liq"], None, "⚠️ ликвидация", liquidated=True)
                continue
            if r["last_funding"] is not None and period > r["last_funding"]:
                rate = self.conn.execute("SELECT rate FROM funding WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                                         (r["symbol"],)).fetchone()
                pay = (rate[0] if rate else 0.0001) * px * r["open_qty"] * sd * (period - r["last_funding"])
                self._book(r["exchange"], -pay, funding=pay)
                self.conn.execute("UPDATE demo_trades SET funding=funding+?, realized=realized-?, last_funding=? WHERE id=?",
                                  (pay, pay, period, r["id"]))

    # ---------- учёт
    def unrealized(self, ex: str, marks: dict | None = None) -> float:
        out = 0.0
        for r in self.conn.execute("SELECT * FROM demo_trades WHERE exchange=? AND status='open'", (ex,)):
            px = (marks or {}).get(r["symbol"])
            if px is None:
                c = self.conn.execute("SELECT close FROM candles WHERE symbol=? AND market='futures' ORDER BY ts DESC LIMIT 1",
                                      (r["symbol"],)).fetchone()
                px = c[0] if c else r["entry"]
            out += (px - r["entry"]) * r["open_qty"] * (1 if r["side"] == "long" else -1)
        return out

    def used_margin(self, ex: str) -> float:
        return self.conn.execute("SELECT COALESCE(SUM(margin*open_qty/qty),0) FROM demo_trades WHERE exchange=? AND status='open'",
                                 (ex,)).fetchone()[0]

    def equity(self, ex: str) -> float:
        return self.account(ex)["balance"] + self.unrealized(ex)

    def free(self, ex: str) -> float:
        return self.account(ex)["balance"] + min(0.0, self.unrealized(ex)) - self.used_margin(ex)

    async def report(self) -> str:
        lines = []
        for ex in self.exchanges():
            a = self.account(ex)
            eq = self.equity(ex)
            ch = (eq / a["start_usd"] - 1) * 100
            done = self.conn.execute("SELECT COUNT(*), SUM(realized>0) FROM demo_trades WHERE exchange=? AND status='closed'",
                                     (ex,)).fetchone()
            op = self.conn.execute("SELECT * FROM demo_trades WHERE exchange=? AND status='open' ORDER BY opened_ts DESC",
                                   (ex,)).fetchall()
            lines.append(f"<b>{ex.capitalize()}</b>: {eq:.2f}$ ({ch:+.1f}% от {a['start_usd']:.0f}$), свободно {self.free(ex):.2f}$")
            lines.append(f"  сделок закрыто {done[0]}, в плюс {done[1] or 0}; комиссии {a['fees']:.2f}$, фандинг {-a['funding']:+.2f}$")
            for r in op[:10]:
                c = self.conn.execute("SELECT close FROM candles WHERE symbol=? AND market='futures' ORDER BY ts DESC LIMIT 1",
                                      (r["symbol"],)).fetchone()
                px = c[0] if c else r["entry"]
                u = (px - r["entry"]) * r["open_qty"] * (1 if r["side"] == "long" else -1)
                lines.append(f"  {'🟢' if r['side'] == 'long' else '🔴'} {symbol_name(r)} от {r['entry']:g}, "
                             f"{r['open_qty'] / r['qty'] * 100:.0f}% позиции, сейчас {u:+.2f}$")
            if len(op) > 10:
                lines.append(f"  … и ещё {len(op) - 10}")
            lines.append("")
        return "\n".join(lines).strip()


def symbol_name(r) -> str:
    return r["symbol"].replace("USDT", "")
