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
FEES = {"binance": 0.05, "bybit": 0.055, "layers": 0.05}   # layers: отдельный счёт модели BTC Layers, цены и правила Binance          # тейкер, % от объёма
PROFILES = {"careful": (1.0, "Осторожный", "+82% в год, просадка до −28%"),
            "balance": (1.6, "Баланс", "+133% в год, просадка до −41%"),
            "aggressive": (2.0, "Агрессивный", "+165% в год, просадка до −49%")}   # история 2020–2026, все стратегии вместе
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
        if getattr(self, "prices_fallback", None) and ex == "layers":
            return self.prices_fallback
        r = self.conn.execute("SELECT close, ts FROM candles WHERE symbol=? AND market='futures' AND tf IN ('1m','1h') "
                              "ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
        if r and time.time() * 1000 - r[1] < 2 * HOUR and (ex in ("binance", "layers") or fast):
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

    async def rules(self, ex: str, sym: str) -> dict:
        """Правила биржи по монете: минимальная сумма позиции, минимальное количество и шаг количества.
        Берутся с открытого API биржи раз в сутки. Нет ответа: минимум 5$ и шаг без ограничений."""
        ex = "binance" if ex == "layers" else ex
        cache = settings.get(self.conn, f"paper_rules_{ex}") or {}
        if not self.prices and time.time() - cache.get("_ts", 0) > 86400:
            try:
                cache = await _fetch_rules(ex)
                cache["_ts"] = time.time()
                settings.put(self.conn, f"paper_rules_{ex}", cache)
            except Exception as e:
                log.info("paper rules %s: %s", ex, e)
        return cache.get(sym) or {"min_notional": 5.0, "min_qty": 0.0, "step": 0.0}

    def _fill(self, px: float, buy: bool) -> float:
        slip = float(self.pc().get("slippage_pct", 0.02)) / 100
        return px * (1 + slip if buy else 1 - slip)

    # ---------- сделки
    def open_risk(self, ex: str) -> float:
        """Сколько $ потеряют все открытые сделки счёта, если сработают их стопы."""
        return self.conn.execute("SELECT COALESCE(SUM(risk_usd*open_qty/qty),0) FROM demo_trades "
                                 "WHERE exchange=? AND status='open' AND risk_usd IS NOT NULL", (ex,)).fetchone()[0]

    def plan(self, kind: str, ex: str, px: float, stop: float | None, notional_pct: float, rem: float = 1.0) -> dict:
        """Риск-менеджмент: размер от риска до стопа, сильный сигнал до x2 риска, предел общего риска,
        плечо так, чтобы ликвидация была дальше стопа (в сделке лежит минимум денег, а потеря на стопе та же)."""
        pc, eq = self.pc(), self.equity(ex)
        lev_fixed = max(1, int(pc.get("leverage", 5)))
        if pc.get("sizing", "risk") != "risk" or not stop or kind not in (pc.get("risk_pct") or {}):
            return {"notional": eq * notional_pct / 100 * rem, "lev": lev_fixed, "risk_usd": None, "note": ""}
        dist = abs(stop / px - 1)
        if dist <= 0:
            return {"notional": 0, "lev": lev_fixed, "risk_usd": None, "note": ""}
        med = (pc.get("strength_median") or {}).get(kind)
        strength = min(float(pc.get("strength_cap", 2.0)), notional_pct / med) if med else 1.0
        mult = PROFILES.get(pc.get("risk_profile", "balance"), PROFILES["balance"])[0]
        risk_pct = float(pc["risk_pct"][kind]) * max(strength, 0.25) * mult
        left = float(pc.get("max_open_risk_pct", 20)) * mult / 100 * eq - self.open_risk(ex)
        risk_usd = eq * risk_pct / 100 * rem
        if risk_usd > left + 1e-9:                       # лимит общего риска: сделка пропускается (урезать хуже на истории)
            risk_usd = 0.0
        note = f"риск {risk_usd:.2f}$ ({risk_usd / eq * 100:.1f}% капитала) до стопа {stop:g}"
        lev = lev_fixed
        if pc.get("auto_leverage", True):
            # ликвидация на расстоянии 1/плечо минус поддерживающая маржа: держим её на 1% цены дальше стопа
            lev = max(1, min(int(pc.get("max_leverage", 50)), int(1 / (dist + MMR + 0.01))))
        return {"notional": risk_usd / dist, "lev": lev, "risk_usd": risk_usd, "note": note, "min_risk": 1e-9}

    async def open(self, kind: str, ref: str, symbol: str, side: str, notional_pct: float, exchange: str | None = None,
                   rem: float = 1.0, stop: float | None = None):
        """Вход. С риск-менеджментом (paper.sizing: risk) размер считается от стопа: на стопе теряем risk_pct капитала.
        Без стопа (BTC Layers) или sizing: signal: notional_pct % капитала, как в сигнале.
        exchange: только этот счёт, иначе оба. rem: какая доля позиции ещё открыта у сигнала (TP1 уже взят: 0.75)."""
        if not self.on() or notional_pct <= 0:
            return
        for ex in self.exchanges() + (["layers"] if exchange == "layers" else []):
            if exchange and ex != exchange:
                continue
            if self.conn.execute("SELECT 1 FROM demo_trades WHERE exchange=? AND kind=? AND ref=?", (ex, kind, ref)).fetchone():
                continue
            name = f"Бумажный {acc_name(ex)}"
            px = await self.price(ex, symbol)
            if not px:
                await self._say(f"🧪 {name}: {symbol.replace('USDT', '')} не открыт, нет цены")
                continue
            rule, coin = await self.rules(ex, symbol), symbol.replace("USDT", "")
            pl = self.plan(kind, ex, px, stop, notional_pct, rem)
            notional, lev = pl["notional"], pl["lev"]
            if pl["risk_usd"] is not None and pl["risk_usd"] < pl.get("min_risk", 0):
                await self._say(f"🧪 {name}: {coin} не открыт: общий риск открытых сделок уже на пределе "
                                f"({self.pc().get('max_open_risk_pct', 20)}% капитала)")
                continue
            # минимум биржи: сумма позиции и минимальное количество монет (у BTC и ETH он заметно больше 5$)
            need = max(rule["min_notional"], rule["min_qty"] * px)
            note = ""
            if notional < need:
                if pl["risk_usd"] and stop and need * abs(stop / px - 1) > 2 * pl["risk_usd"]:
                    await self._say(f"🧪 {name}: {coin} не открыт: минимум биржи {need:.2f}$ при стопе {abs(stop / px - 1) * 100:.1f}% "
                                    f"даёт риск {need * abs(stop / px - 1):.2f}$, а по плану {pl['risk_usd']:.2f}$. "
                                    "Риск-менеджмент важнее сделки")
                    continue
                note = f" (по сигналу {notional:.2f}$, поднял до минимума биржи {need:.2f}$)"
                notional = need * 1.001
            fill = self._fill(px, side == "long")
            qty = _step(notional / fill, rule["step"], up=bool(note))
            notional = qty * fill
            margin = notional / lev
            if margin > self.free(ex):
                await self._say(f"🧪 {name}: {coin} не открыт. Минимум биржи {need:.2f}$ при плече x{lev} требует маржу "
                                f"{margin:.2f}$, а свободно {self.free(ex):.2f}$")
                continue
            fee = notional * FEES.get(ex, 0.05) / 100
            sd = 1 if side == "long" else -1
            liq = fill * (1 - sd / lev + sd * MMR)
            risk_usd = qty * abs(fill - stop) if stop and pl["risk_usd"] is not None else None
            self._book(ex, -fee, fee=fee)
            self.conn.execute("INSERT INTO demo_trades (exchange, kind, ref, symbol, side, qty, open_qty, entry, opened_ts, status, "
                              "margin, leverage, liq, fees, funding, realized, last_funding, stop, risk_usd) "
                              "VALUES (?,?,?,?,?,?,?,?,?, 'open', ?,?,?,?,0,?,?,?,?)",
                              (ex, kind, ref, symbol, side, qty / rem, qty, fill, int(time.time() * 1000), margin / rem, lev, liq, fee,
                               -fee, int(time.time() * 1000) // (8 * HOUR), stop,
                               risk_usd / rem if risk_usd is not None else None))       # итог сделки с комиссией входа
            why = f"\n   {pl['note']}; плечо x{lev} подобрано так, что ликвидация ({liq:g}) дальше стопа" if pl["note"] else ""
            await self._say(f"🧪 {name}: {'лонг' if side == 'long' else 'шорт'} {symbol.replace('USDT', '')} по {fill:g}, "
                            f"позиция {notional:.2f}$ (маржа {margin:.2f}$, плечо x{lev}){note}, комиссия {fee:.3f}${why}")

    async def open_live(self) -> tuple[int, int]:
        """Открыть на бумажных счетах все живые сигналы, которые уже открыты, но на счёт ещё не попали.
        Вход по текущей цене. Возвращает (сколько сигналов открыто, сколько уже было на счёте)."""
        import json
        if not self.on():
            return 0, 0
        todo = []
        for r in self.conn.execute("SELECT * FROM trend_signals WHERE status='open'"):
            st = json.loads(r["state"]) if r["state"] else {}
            todo.append(("trend", str(r["id"]), r["symbol"], r["side"], r["size_pct"], None, st.get("rem", 1.0), r["stop"]))
        for r in self.conn.execute("SELECT * FROM listing_trades WHERE status='open'"):
            ex = r["exchange"] if "exchange" in r.keys() else None
            todo.append(("listing", r["symbol"], r["symbol"], "short", r["notional_pct"], "bybit" if ex == "bybit" else None, 1.0,
                         r["stop"]))
        for r in self.conn.execute("SELECT s.id, s.symbol, s.side, s.entry, s.stop, s.risk_pct, t.remaining FROM signals s "
                                   "JOIN trade_state t ON t.signal_id=s.id WHERE t.status='open' AND s.source='live'"):
            dist = abs(r["entry"] - r["stop"]) / r["entry"] * 100
            if dist:
                todo.append(("capitulation", str(r["id"]), r["symbol"], r["side"], r["risk_pct"] / dist * 100, None,
                             r["remaining"] or 1.0, r["stop"]))
        opened = had = 0
        for kind, ref, sym, side, pct, ex, rem, stop in todo:
            if self.conn.execute("SELECT 1 FROM demo_trades WHERE kind=? AND ref=?", (kind, ref)).fetchone():
                had += 1
                continue
            n = self.conn.execute("SELECT COUNT(*) FROM demo_trades").fetchone()[0]
            await self.open(kind, ref, sym, side, pct, ex, rem=max(min(rem, 1.0), 0.01), stop=stop)
            opened += self.conn.execute("SELECT COUNT(*) FROM demo_trades").fetchone()[0] > n
        return opened, had

    def live_counts(self) -> tuple[int, int]:
        """(открытых живых сигналов, из них ещё не на бумажном счёте)."""
        refs = [("trend", str(r[0])) for r in self.conn.execute("SELECT id FROM trend_signals WHERE status='open'")]
        refs += [("listing", r[0]) for r in self.conn.execute("SELECT symbol FROM listing_trades WHERE status='open'")]
        refs += [("capitulation", str(r[0])) for r in self.conn.execute(
            "SELECT s.id FROM signals s JOIN trade_state t ON t.signal_id=s.id WHERE t.status='open' AND s.source='live'")]
        miss = sum(1 for k, r in refs if not self.conn.execute("SELECT 1 FROM demo_trades WHERE kind=? AND ref=?", (k, r)).fetchone())
        return len(refs), miss

    async def reduce(self, kind: str, ref: str, part: float | None, why: str):
        """Закрыть долю part исходной позиции (None: всё, что осталось)."""
        for r in self.conn.execute("SELECT * FROM demo_trades WHERE kind=? AND ref=? AND status='open'", (kind, ref)).fetchall():
            px = await self.price(r["exchange"], r["symbol"])
            if not px:
                continue
            await self._close(r, px, part, why)

    async def _close(self, r, px: float, part: float | None, why: str, liquidated: bool = False):
        ex, sd = r["exchange"], 1 if r["side"] == "long" else -1
        rule = await self.rules(ex, r["symbol"])
        qty = r["open_qty"] if part is None else min(r["open_qty"], _step(r["qty"] * part, rule["step"]))
        if part is not None and qty < max(rule["min_qty"], rule["step"], 1e-12):
            await self._say(f"🧪 Бумажный {acc_name(ex)}: {why}, но {symbol_name(r)}: часть меньше минимального количества "
                            "биржи, держу всю позицию до следующего выхода")
            return
        if r["open_qty"] - qty < max(rule["step"], r["qty"] * 1e-6):
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
        await self._say(f"🧪 Бумажный {acc_name(ex)}: {why}, {symbol_name(r)} закрыто {qty / r['qty'] * 100:.0f}% по {fill:g}, "
                        f"{gross - fee:+.2f}$" + (f". Сделка закрыта, итог {tot:+.2f}$" if closed else "")
                        + f". Баланс {self.account(ex)['balance']:.2f}$")

    # ---------- BTC Layers: целевая позиция раз в сутки
    def has(self, ex: str) -> bool:
        return self.conn.execute("SELECT 1 FROM paper_accounts WHERE exchange=?", (ex,)).fetchone() is not None

    def net_notional(self, ex: str, px: float) -> float:
        """Текущая позиция счёта в $, со знаком (лонг +, шорт −)."""
        return sum(r["open_qty"] * px * (1 if r["side"] == "long" else -1) for r in
                   self.conn.execute("SELECT * FROM demo_trades WHERE exchange=? AND status='open'", (ex,)))

    async def layers_target(self, pos: float, day: str, band: float = 0.10, symbol: str = "BTCUSDT",
                            px_hint: float | None = None):
        """Привести счёт «BTC Layers» к позиции pos × капитал (pos со знаком: −2.25..2.25).
        Ребаланс, только если разница больше band капитала. Без стопов и целей: выход только по модели."""
        if not self.on():
            return
        ex = "layers"
        self.account(ex)
        px = await self.price(ex, symbol)
        if not px and px_hint:                         # биржи не ответили: последнее дневное закрытие модели
            px = px_hint
            self.prices_fallback = px_hint
        if not px:
            await self._say("🧪 BTC Layers: нет цены BTC, ребаланс перенесён на следующий расчёт")
            return
        try:
            await self._rebalance(ex, px, pos, day, band, symbol)
        finally:
            self.prices_fallback = None

    async def _rebalance(self, ex: str, px: float, pos: float, day: str, band: float, symbol: str):
        eq = self.equity(ex)
        cur, tgt = self.net_notional(ex, px), pos * eq
        if abs(tgt - cur) <= band * eq:
            return
        rows = self.conn.execute("SELECT * FROM demo_trades WHERE exchange=? AND status='open' ORDER BY id DESC", (ex,)).fetchall()
        flip = cur and (tgt == 0 or (tgt > 0) != (cur > 0))
        if flip:                                       # смена стороны или кэш: закрыть всё
            for r in rows:
                await self._close(r, px, None, "BTC Layers: " + ("в кэш" if not tgt else "смена стороны"))
            cur = 0.0
        rule = await self.rules(ex, symbol)
        need = max(rule["min_notional"], rule["min_qty"] * px)
        if cur and abs(abs(tgt) - abs(cur)) < need:    # у BTC минимум Binance около 100$: мелкую разницу не трогаем
            await self._say(f"🧪 BTC Layers: позиция {pos:+.2f}, разница {abs(abs(tgt) - abs(cur)):.2f}$ меньше минимума "
                            f"биржи {need:.2f}$, держу как есть")
            return
        if abs(tgt) > abs(cur):                        # добрать разницу новой сделкой
            await self.open("layers", f"{day}:{len(rows)}", symbol, "long" if tgt > 0 else "short",
                            (abs(tgt) - abs(cur)) / max(self.equity(ex), 1e-9) * 100, exchange=ex)
        elif abs(tgt) < abs(cur):                      # уменьшить: срезать долю с каждой сделки
            k = 1 - abs(tgt) / abs(cur)
            for r in rows:
                await self._close(r, px, k * r["open_qty"] / r["qty"], f"BTC Layers: позиция {pos:+.2f}")

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
        for ex in self.exchanges() + (["layers"] if self.has("layers") else []):
            a = self.account(ex)
            eq = self.equity(ex)
            ch = (eq / a["start_usd"] - 1) * 100
            done = self.conn.execute("SELECT COUNT(*), SUM(realized>0) FROM demo_trades WHERE exchange=? AND status='closed'",
                                     (ex,)).fetchone()
            op = self.conn.execute("SELECT * FROM demo_trades WHERE exchange=? AND status='open' ORDER BY opened_ts DESC",
                                   (ex,)).fetchall()
            lines.append(f"<b>{acc_name(ex)}</b>: {eq:.2f}$ ({ch:+.1f}% от {a['start_usd']:.0f}$), свободно {self.free(ex):.2f}$")
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


def _step(q: float, step: float, up: bool = False) -> float:
    if not step:
        return q
    import math
    n = math.ceil(q / step - 1e-9) if up else math.floor(q / step + 1e-9)
    return round(n * step, 12)


async def _fetch_rules(ex: str) -> dict:
    from .http import Http
    http, out = Http(retries=2), {}
    try:
        if ex == "binance":
            info = await http.get_json("https://fapi.binance.com/fapi/v1/exchangeInfo")
            for x in info.get("symbols", []):
                f = {y["filterType"]: y for y in x.get("filters", [])}
                lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {}
                out[x["symbol"]] = {"min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5)),
                                    "min_qty": float(lot.get("minQty", 0)), "step": float(lot.get("stepSize", 0))}
        else:
            cursor = ""
            for _ in range(10):
                d = await http.get_json("https://api.bybit.com/v5/market/instruments-info",
                                        params={"category": "linear", "limit": 1000, **({"cursor": cursor} if cursor else {})})
                r = d.get("result") or {}
                for x in r.get("list", []):
                    lf = x.get("lotSizeFilter", {})
                    out[x["symbol"]] = {"min_notional": float(lf.get("minNotionalValue") or 5),
                                        "min_qty": float(lf.get("minOrderQty") or 0), "step": float(lf.get("qtyStep") or 0)}
                cursor = r.get("nextPageCursor")
                if not cursor:
                    break
    finally:
        await http.close()
    if not out:
        raise RuntimeError("пустой ответ")
    return out


def acc_name(ex: str) -> str:
    return "BTC Layers" if ex == "layers" else ex.capitalize()


def symbol_name(r) -> str:
    return r["symbol"].replace("USDT", "")
