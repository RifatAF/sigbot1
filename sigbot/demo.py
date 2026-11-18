"""Демо-торговля на демо-счетах Binance и Bybit: бот ставит рыночные ордера по своим сигналам, деньги виртуальные.

Безопасность. Адреса бирж зашиты в код и ведут только на демо: Binance demo-fapi.binance.com, Bybit api-demo.bybit.com.
Настоящий ключ там не работает, поэтому реальный счёт этот модуль тронуть не может, даже если ключи перепутать.
Ключи берутся из .env: BINANCE_DEMO_KEY / BINANCE_DEMO_SECRET и BYBIT_DEMO_KEY / BYBIT_DEMO_SECRET.

Как торгует. Вход рыночным ордером в момент сигнала. Цели и стоп бот ведёт сам (как в канале): при TP1/TP2 закрывает
часть позиции, при стопе или выходе закрывает остаток ордером reduceOnly. Ордера стоп-лосс на биржу не ставятся,
поэтому стоп сработает тогда, когда бот его увидит (тренд: по часовым свечам, капитуляция: по минутным)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import time
from urllib.parse import urlencode

import aiohttp

from . import settings

log = logging.getLogger(__name__)

BINANCE_DEMO = "https://demo-fapi.binance.com"          # только демо: в коде нет ни одного адреса настоящего счёта
BYBIT_DEMO = "https://api-demo.bybit.com"


class DemoError(Exception):
    pass


def _round_step(x: float, step: float) -> float:
    n = math.floor(x / step + 1e-9)
    d = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, d)


class BinanceDemo:
    name = "binance"

    def __init__(self, key: str, secret: str, session: aiohttp.ClientSession | None = None):
        self.key, self.secret, self._s = key, secret, session
        self.rules: dict = {}

    async def s(self):
        if self._s is None or self._s.closed:
            self._s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._s

    async def close(self):
        if self._s and not self._s.closed:
            await self._s.close()

    def sign(self, params: dict) -> str:
        q = urlencode(params)
        return q + "&signature=" + hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()

    async def call(self, method: str, path: str, params: dict | None = None, signed: bool = True):
        p = dict(params or {})
        if signed:
            p.update(timestamp=int(time.time() * 1000), recvWindow=10000)
        q = self.sign(p) if signed else urlencode(p)
        s = await self.s()
        async with s.request(method, f"{BINANCE_DEMO}{path}?{q}", headers={"X-MBX-APIKEY": self.key}) as r:
            d = await r.json(content_type=None)
            if r.status >= 400 or (isinstance(d, dict) and d.get("code", 0) not in (0, 200)):
                raise DemoError(f"binance {path}: {str(d)[:160]}")
            return d

    async def load_rules(self):
        if self.rules:
            return
        info = await self.call("GET", "/fapi/v1/exchangeInfo", signed=False)
        for x in info["symbols"]:
            f = {y["filterType"]: y for y in x.get("filters", [])}
            self.rules[x["symbol"]] = {"step": float(f.get("MARKET_LOT_SIZE", f.get("LOT_SIZE", {})).get("stepSize", 0.001)),
                                       "min_qty": float(f.get("LOT_SIZE", {}).get("minQty", 0)),
                                       "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5))}

    async def equity(self) -> float:
        d = await self.call("GET", "/fapi/v2/balance")
        return sum(float(x.get("balance", 0)) + float(x.get("crossUnPnl", 0)) for x in d if x.get("asset") == "USDT")

    async def price(self, sym: str) -> float:
        return float((await self.call("GET", "/fapi/v1/ticker/price", {"symbol": sym}, signed=False))["price"])

    async def leverage(self, sym: str, lev: int):
        try:
            await self.call("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": lev})
        except DemoError as e:
            log.info("leverage %s: %s", sym, e)

    async def market(self, sym: str, side: str, qty: float, reduce: bool = False) -> tuple[float, float]:
        """side long/short направление сделки ордера: BUY или SELL. Возвращает (исполненное количество, средняя цена)."""
        p = {"symbol": sym, "side": "BUY" if side == "buy" else "SELL", "type": "MARKET", "quantity": f"{qty:.10g}",
             "newOrderRespType": "RESULT"}
        if reduce:
            p["reduceOnly"] = "true"
        d = await self.call("POST", "/fapi/v1/order", p)
        return float(d.get("executedQty") or qty), float(d.get("avgPrice") or 0)

    async def positions(self) -> list[dict]:
        d = await self.call("GET", "/fapi/v2/positionRisk")
        return [{"symbol": x["symbol"], "qty": float(x["positionAmt"]), "entry": float(x["entryPrice"]),
                 "pnl": float(x.get("unRealizedProfit", 0))} for x in d if float(x.get("positionAmt", 0)) != 0]


class BybitDemo:
    name = "bybit"

    def __init__(self, key: str, secret: str, session: aiohttp.ClientSession | None = None):
        self.key, self.secret, self._s = key, secret, session
        self.rules: dict = {}

    s, close = BinanceDemo.s, BinanceDemo.close

    def headers(self, payload: str) -> dict:
        ts, recv = str(int(time.time() * 1000)), "10000"
        sig = hmac.new(self.secret.encode(), (ts + self.key + recv + payload).encode(), hashlib.sha256).hexdigest()
        return {"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": recv, "X-BAPI-SIGN": sig,
                "Content-Type": "application/json"}

    async def call(self, method: str, path: str, params: dict | None = None, signed: bool = True):
        s = await self.s()
        if method == "GET":
            q = urlencode(params or {})
            async with s.get(f"{BYBIT_DEMO}{path}?{q}", headers=self.headers(q) if signed else {}) as r:
                d = await r.json(content_type=None)
        else:
            body = json.dumps(params or {}, separators=(",", ":"))
            async with s.post(f"{BYBIT_DEMO}{path}", data=body, headers=self.headers(body)) as r:
                d = await r.json(content_type=None)
        if not isinstance(d, dict) or d.get("retCode", 0) != 0:
            raise DemoError(f"bybit {path}: {str(d)[:160]}")
        return d.get("result") or {}

    async def load_rules(self):
        if self.rules:
            return
        cursor = ""
        for _ in range(10):
            r = await self.call("GET", "/v5/market/instruments-info",
                                {"category": "linear", "limit": 1000, **({"cursor": cursor} if cursor else {})}, signed=False)
            for x in r.get("list", []):
                lf = x.get("lotSizeFilter", {})
                self.rules[x["symbol"]] = {"step": float(lf.get("qtyStep", 0.001)), "min_qty": float(lf.get("minOrderQty", 0)),
                                           "min_notional": float(lf.get("minNotionalValue", 5) or 5)}
            cursor = r.get("nextPageCursor")
            if not cursor:
                break

    async def equity(self) -> float:
        r = await self.call("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        acc = (r.get("list") or [{}])[0]
        return float(acc.get("totalEquity") or 0)

    async def price(self, sym: str) -> float:
        r = await self.call("GET", "/v5/market/tickers", {"category": "linear", "symbol": sym}, signed=False)
        return float(r["list"][0]["lastPrice"])

    async def leverage(self, sym: str, lev: int):
        try:
            await self.call("POST", "/v5/position/set-leverage",
                            {"category": "linear", "symbol": sym, "buyLeverage": str(lev), "sellLeverage": str(lev)})
        except DemoError as e:                       # 110043: плечо уже такое
            log.info("leverage %s: %s", sym, e)

    async def market(self, sym: str, side: str, qty: float, reduce: bool = False) -> tuple[float, float]:
        r = await self.call("POST", "/v5/order/create", {"category": "linear", "symbol": sym, "side": "Buy" if side == "buy" else "Sell",
                                                          "orderType": "Market", "qty": f"{qty:.10g}", "reduceOnly": reduce})
        avg = 0.0
        try:
            h = await self.call("GET", "/v5/order/history", {"category": "linear", "orderId": r.get("orderId", "")})
            o = (h.get("list") or [{}])[0]
            avg, qty = float(o.get("avgPrice") or 0), float(o.get("cumExecQty") or qty)
        except DemoError:
            pass
        return qty, avg

    async def positions(self) -> list[dict]:
        r = await self.call("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        return [{"symbol": x["symbol"], "qty": float(x["size"]) * (1 if x["side"] == "Buy" else -1),
                 "entry": float(x.get("avgPrice") or 0), "pnl": float(x.get("unrealisedPnl") or 0)}
                for x in r.get("list", []) if float(x.get("size") or 0) > 0]


def accounts() -> list:
    out = []
    if os.environ.get("BINANCE_DEMO_KEY") and os.environ.get("BINANCE_DEMO_SECRET"):
        out.append(BinanceDemo(os.environ["BINANCE_DEMO_KEY"], os.environ["BINANCE_DEMO_SECRET"]))
    if os.environ.get("BYBIT_DEMO_KEY") and os.environ.get("BYBIT_DEMO_SECRET"):
        out.append(BybitDemo(os.environ["BYBIT_DEMO_KEY"], os.environ["BYBIT_DEMO_SECRET"]))
    return out


class DemoTrader:
    """Повторяет сигналы бота на демо-счетах. Одна строка demo_trades на сделку и биржу."""

    def __init__(self, cfg, conn, notify=None, accs: list | None = None):
        self.cfg, self.conn, self.notify = cfg, conn, notify
        self.accs = accounts() if accs is None else accs

    def on(self) -> bool:
        e = settings.effective(self.cfg, self.conn)
        return bool(self.accs) and e.get("demo", {}).get("enabled", False) and not e["publishing"].get("kill_switch")

    async def _say(self, text: str):
        if self.notify:
            try:
                await self.notify(text)
            except Exception:
                pass

    async def open(self, kind: str, ref: str, symbol: str, side: str, notional_pct: float, exchange: str | None = None):
        """Вход: notional_pct % от капитала демо-счёта. exchange: только эта биржа (листинг Bybit), иначе все."""
        if not self.on() or notional_pct <= 0:
            return
        lev = int(self.cfg.get("demo", {}).get("leverage", 5))
        for a in self.accs:
            if exchange and a.name != exchange:
                continue
            if self.conn.execute("SELECT 1 FROM demo_trades WHERE exchange=? AND kind=? AND ref=?", (a.name, kind, ref)).fetchone():
                continue
            try:
                await a.load_rules()
                rule = a.rules.get(symbol)
                if not rule:
                    raise DemoError("монеты нет на этой бирже")
                eq, px = await a.equity(), await a.price(symbol)
                qty = _round_step(eq * notional_pct / 100 / px, rule["step"])
                if qty < rule["min_qty"] or qty * px < rule["min_notional"]:
                    raise DemoError(f"слишком маленький размер ({qty * px:.2f}$)")
                await a.leverage(symbol, lev)
                got, avg = await a.market(symbol, "buy" if side == "long" else "sell", qty)
                self.conn.execute("INSERT INTO demo_trades (exchange, kind, ref, symbol, side, qty, open_qty, entry, opened_ts, status) "
                                  "VALUES (?,?,?,?,?,?,?,?,?, 'open')", (a.name, kind, ref, symbol, side, got, got, avg or px,
                                                                         int(time.time() * 1000)))
                await self._say(f"🧪 Демо {a.name}: {'купил' if side == 'long' else 'продал'} {got:g} {symbol.replace('USDT', '')} "
                                f"по {avg or px:g} ({notional_pct:.1f}% счёта, плечо x{lev})")
            except Exception as e:
                log.warning("demo open %s %s: %s", a.name, symbol, e)
                await self._say(f"🧪 Демо {a.name}: {symbol.replace('USDT', '')} не открыт: {str(e)[:150]}")

    async def reduce(self, kind: str, ref: str, part: float | None, why: str):
        """Закрыть долю part исходной позиции (None: всё, что осталось)."""
        if not self.accs:
            return
        for r in self.conn.execute("SELECT * FROM demo_trades WHERE kind=? AND ref=? AND status='open'", (kind, ref)).fetchall():
            a = next((x for x in self.accs if x.name == r["exchange"]), None)
            if not a:
                continue
            try:
                await a.load_rules()
                step = a.rules.get(r["symbol"], {}).get("step", 0.001)
                qty = r["open_qty"] if part is None else min(r["open_qty"], _round_step(r["qty"] * part, step))
                if part is not None and r["open_qty"] - qty < step:
                    qty = r["open_qty"]
                if qty <= 0:
                    continue
                got, avg = await a.market(r["symbol"], "sell" if r["side"] == "long" else "buy", qty, reduce=True)
                left = max(0.0, r["open_qty"] - got)
                sd = 1 if r["side"] == "long" else -1
                pnl = (avg - r["entry"]) * got * sd if avg else 0.0
                self.conn.execute("UPDATE demo_trades SET open_qty=?, realized=COALESCE(realized,0)+?, status=?, closed_ts=? WHERE id=?",
                                  (left, pnl, "open" if left > step / 2 else "closed",
                                   None if left > step / 2 else int(time.time() * 1000), r["id"]))
                await self._say(f"🧪 Демо {a.name}: {why}, закрыл {got:g} {r['symbol'].replace('USDT', '')} по {avg:g}, "
                                f"результат {pnl:+.2f}$")
            except Exception as e:
                log.warning("demo reduce %s %s: %s", a.name, r["symbol"], e)
                await self._say(f"🧪 Демо {a.name}: не смог закрыть {r['symbol'].replace('USDT', '')}: {str(e)[:150]}")

    async def report(self) -> str:
        """Экран «Демо-счета»: капитал, открытые позиции на бирже, итог закрытых сделок."""
        if not self.accs:
            return ("Ключей демо-счетов нет. Как подключить:\n"
                    "• Binance: зайди на demo.binance.com → API Management → создай ключ. В .env впиши BINANCE_DEMO_KEY и BINANCE_DEMO_SECRET.\n"
                    "• Bybit: на bybit.com переключись в Demo Trading → API → создай ключ. В .env впиши BYBIT_DEMO_KEY и BYBIT_DEMO_SECRET.\n"
                    "Потом перезапусти бота. Настоящие ключи сюда не подойдут: бот ходит только на демо-адреса.")
        lines = []
        for a in self.accs:
            done = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(realized),0), SUM(realized>0) FROM demo_trades "
                                     "WHERE exchange=? AND status='closed'", (a.name,)).fetchone()
            try:
                eq, pos = await a.equity(), await a.positions()
                lines.append(f"<b>{a.name.capitalize()} демо</b>: капитал {eq:,.2f}$, открыто позиций {len(pos)}")
                for p in pos[:10]:
                    lines.append(f"  {'🟢' if p['qty'] > 0 else '🔴'} {p['symbol'].replace('USDT', '')} {abs(p['qty']):g} "
                                 f"от {p['entry']:g}, сейчас {p['pnl']:+.2f}$")
            except Exception as e:
                lines.append(f"<b>{a.name.capitalize()} демо</b>: нет ответа ({str(e)[:120]})")
            if done[0]:
                lines.append(f"  закрыто сделок {done[0]}, в плюс {done[2] or 0}, итог {done[1]:+.2f}$")
        return "\n".join(lines)
