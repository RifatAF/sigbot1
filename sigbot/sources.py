"""Публичные источники: Binance (фьючерсы, спот), Coinbase, Alternative.me. Только чтение."""
from __future__ import annotations

from .http import Http, RateLimiter

FAPI = "https://fapi.binance.com"
SAPI = "https://api.binance.com"
FWS = "wss://fstream.binance.com/ws/!forceOrder@arr"

# лимиты Binance: 2400 веса/мин фьючерсы, 6000 спот; берём с запасом
FUT_LIMIT = RateLimiter(1200)
SPOT_LIMIT = RateLimiter(3000)

KLINE_COLS = ("ts", "open", "high", "low", "close", "volume", "taker_buy")


def parse_klines(raw: list) -> list[tuple]:
    """Ответ Binance klines -> (ts, o, h, l, c, volume, taker_buy_base)."""
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[9]))
            for k in raw]


BYBIT = "https://api.bybit.com"


class Bybit:
    """Открытый API Bybit v5 без ключей: список линейных перпетуалов с датой запуска и свечи."""

    def __init__(self, http: Http):
        self.http = http

    async def _get(self, path: str, params: dict) -> dict:
        d = await self.http.get_json(BYBIT + path, params=params)
        if not isinstance(d, dict) or d.get("retCode", 0) != 0:
            raise RuntimeError(f"bybit {path}: {str(d)[:120]}")
        return d.get("result") or {}

    async def perp_listings(self, quote: str = "USDT") -> list[tuple[str, int]]:
        out, cursor = [], ""
        for _ in range(20):
            p = {"category": "linear", "limit": 1000, **({"cursor": cursor} if cursor else {})}
            r = await self._get("/v5/market/instruments-info", p)
            out += [(x["symbol"], int(x.get("launchTime") or 0)) for x in r.get("list", [])
                    if x.get("contractType") == "LinearPerpetual" and x.get("quoteCoin") == quote
                    and x.get("status") == "Trading"]
            cursor = r.get("nextPageCursor")
            if not cursor:
                break
        return out

    async def klines(self, symbol: str, market: str, interval: str, start_ms: int | None = None,
                     limit: int = 200) -> list[tuple]:
        """Тот же формат, что у Binance: (ts, o, h, l, c, volume, 0), по возрастанию времени."""
        step = {"1d": 86_400_000, "1h": 3_600_000}[interval]
        p = {"category": "linear", "symbol": symbol, "interval": {"1d": "D", "1h": "60"}[interval], "limit": min(limit, 1000)}
        if start_ms is not None:
            p.update(start=int(start_ms), end=int(start_ms) + min(limit, 1000) * step)
        r = await self._get("/v5/market/kline", p)
        return sorted((int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), 0.0)
                      for k in r.get("list", []))


TRADFI_WORDS = ("tradfi", "stock", "equity", "etf", "commodit", "metal", "forex", "index", "bond", "pre-market")


def perp_kind(x: dict) -> str:
    """COIN для криптовалюты; иначе акция, ETF, металл, валюта. Binance помечает их по-разному:
    отдельный contractType (TRADIFI_PERPETUAL), underlyingType или метки в underlyingSubType."""
    ut = str(x.get("underlyingType") or "COIN")
    if ut.upper() != "COIN":
        return ut
    ct = str(x.get("contractType") or "")
    if ct and ct != "PERPETUAL" and "TRADIFI" in ct.upper():
        return "TRADFI"
    tags = " ".join(map(str, x.get("underlyingSubType") or [])).lower()
    return "TRADFI" if any(w in tags for w in TRADFI_WORDS) else "COIN"


def _symbols_in(x) -> set[str]:
    """Все строки вида XXXUSDT в ответе любой формы."""
    if isinstance(x, dict):
        return set().union(*(_symbols_in(v) for v in x.values())) if x else set()
    if isinstance(x, list):
        return set().union(*(_symbols_in(v) for v in x)) if x else set()
    return {x} if isinstance(x, str) and x.endswith("USDT") and x.isupper() and x.isalnum() else set()


class Binance:
    def __init__(self, http: Http):
        self.http = http

    async def universe(self, top_n: int, quote: str = "USDT", exclude=()) -> list[str]:
        """Топ N USDT перпетуалов по обороту, у которых есть спот пара."""
        await FUT_LIMIT.acquire(40)
        tickers = await self.http.get_json(f"{FAPI}/fapi/v1/ticker/24hr")
        await FUT_LIMIT.acquire(1)
        info = await self.http.get_json(f"{FAPI}/fapi/v1/exchangeInfo")
        perps = {s["symbol"] for s in info["symbols"]
                 if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == quote
                 and s.get("status") == "TRADING"}
        await SPOT_LIMIT.acquire(20)
        spot_info = await self.http.get_json(f"{SAPI}/api/v3/exchangeInfo", params={"permissions": "SPOT"})
        spot = {s["symbol"] for s in spot_info["symbols"] if s.get("status") == "TRADING"}
        rows = [t for t in tickers if t["symbol"] in perps and t["symbol"] in spot
                and t["symbol"] not in set(exclude)]
        rows.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
        return [t["symbol"] for t in rows[:top_n]]

    async def perp_listings(self, quote: str = "USDT") -> list[tuple[str, int]]:
        """Все торгуемые USDT перпетуалы с датой листинга (onboardDate, мс)."""
        await FUT_LIMIT.acquire(1)
        info = await self.http.get_json(f"{FAPI}/fapi/v1/exchangeInfo")
        return [(x["symbol"], int(x.get("onboardDate", 0))) for x in info["symbols"]
                if x.get("contractType") == "PERPETUAL" and x.get("quoteAsset") == quote and x.get("status") == "TRADING"]

    async def perp_kinds(self, quote: str = "USDT") -> dict[str, str]:
        """Тип базового актива перпетуала: COIN для криптовалют, иначе акции, сырьё, валюты (их не шортим как листинги)."""
        await FUT_LIMIT.acquire(1)
        info = await self.http.get_json(f"{FAPI}/fapi/v1/exchangeInfo")
        out = {x["symbol"]: perp_kind(x) for x in info["symbols"] if x.get("quoteAsset") == quote}
        try:                                    # расписание торгов есть только у акций и фондов: всё, что в нём, не крипта
            for sym in _symbols_in(await self.http.get_json(f"{FAPI}/fapi/v1/tradingSchedule")):
                if out.get(sym) == "COIN":
                    out[sym] = "TRADFI"
        except Exception:
            pass
        return out

    async def klines(self, symbol: str, market: str, interval: str, start_ms: int | None = None,
                     limit: int = 1000) -> list[tuple]:
        if market == "futures":
            url, lim, w = f"{FAPI}/fapi/v1/klines", FUT_LIMIT, 5 if limit > 500 else 2
        else:
            url, lim, w = f"{SAPI}/api/v3/klines", SPOT_LIMIT, 2
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        await lim.acquire(w)
        return parse_klines(await self.http.get_json(url, params=params))

    async def open_interest(self, symbol: str) -> tuple[int, float]:
        await FUT_LIMIT.acquire(1)
        d = await self.http.get_json(f"{FAPI}/fapi/v1/openInterest", params={"symbol": symbol})
        return int(d["time"]), float(d["openInterest"])

    async def open_interest_hist(self, symbol: str, period: str = "5m", start_ms: int | None = None):
        """История OI за последние 30 дней (ограничение Binance)."""
        await FUT_LIMIT.acquire(1)
        p = {"symbol": symbol, "period": period, "limit": 500}
        if start_ms:
            p["startTime"] = start_ms
        d = await self.http.get_json(f"{FAPI}/futures/data/openInterestHist", params=p)
        return [(int(x["timestamp"]), float(x["sumOpenInterest"]), float(x["sumOpenInterestValue"])) for x in d]

    async def premium_index(self) -> list[dict]:
        """Текущий фандинг и марк цена по всем символам одним запросом."""
        await FUT_LIMIT.acquire(10)
        return await self.http.get_json(f"{FAPI}/fapi/v1/premiumIndex")

    async def funding_hist(self, symbol: str, start_ms: int | None = None):
        await FUT_LIMIT.acquire(1)
        p = {"symbol": symbol, "limit": 1000}
        if start_ms:
            p["startTime"] = start_ms
        d = await self.http.get_json(f"{FAPI}/fapi/v1/fundingRate", params=p)
        return [(int(x["fundingTime"]), float(x["fundingRate"])) for x in d]

    async def spot_price(self, symbol: str) -> float:
        await SPOT_LIMIT.acquire(2)
        d = await self.http.get_json(f"{SAPI}/api/v3/ticker/price", params={"symbol": symbol})
        return float(d["price"])


def parse_force_order(msg: dict) -> tuple | None:
    """Сообщение !forceOrder@arr -> (symbol, ts, side, price, qty, usd).

    side ликвидационного ордера SELL = ликвидирован лонг, BUY = шорт.
    """
    o = msg.get("o") if isinstance(msg, dict) else None
    if not o:
        return None
    price = float(o.get("ap") or o.get("p"))
    qty = float(o.get("z") or o.get("q"))
    side = "long" if o["S"] == "SELL" else "short"
    return o["s"], int(o["T"]), side, price, qty, price * qty


class Coinbase:
    URL = "https://api.exchange.coinbase.com"

    def __init__(self, http: Http):
        self.http = http

    async def price(self, product: str = "BTC-USD") -> float:
        d = await self.http.get_json(f"{self.URL}/products/{product}/ticker")
        return float(d["price"])

    async def candles_1h(self, product: str, start_iso: str, end_iso: str) -> list[tuple]:
        d = await self.http.get_json(f"{self.URL}/products/{product}/candles",
                                     params={"granularity": 3600, "start": start_iso, "end": end_iso})
        return [(int(r[0]) * 1000, float(r[4])) for r in d]  # (ts_ms, close)


class FearGreed:
    URL = "https://api.alternative.me/fng/"

    def __init__(self, http: Http):
        self.http = http

    async def history(self, limit: int = 0) -> list[tuple[int, int]]:
        d = await self.http.get_json(self.URL, params={"limit": limit})
        return [(int(x["timestamp"]) * 1000, int(x["value"])) for x in d["data"]]
