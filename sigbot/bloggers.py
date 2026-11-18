"""Уровни блогеров: приём, подтверждение, зоны внимания, точность авторов.
На вход сделки не влияют: только пометки в посте и на картинке."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

DAY = 86_400_000


def _now() -> int:
    return int(time.time() * 1000)


def normalize_symbol(s: str | None) -> str | None:
    if not s:
        return None
    s = s.upper().replace("/", "").replace("-", "").replace("PERP", "").strip()
    if s.endswith("USD") and not s.endswith("USDT"):
        s = s[:-3]
    return s if s.endswith("USDT") else s + "USDT"


def parse_text(text: str) -> dict | None:
    """Разбор без нейросети: тикер и цены по словам вход/стоп/цель (рус и англ). Для простых постов хватает."""
    import re
    if not text:
        return None
    t = re.sub(r"(\d),(\d)", r"\1.\2", text)
    m = re.search(r"#?\$?\b([A-Z]{2,10})(?:/?USDT|USD|\.P|PERP)?\b", text.upper())
    skip = {"LONG", "SHORT", "TP", "SL", "USDT", "USD", "ENTRY", "STOP", "TARGET", "BUY", "SELL", "THE", "AND"}
    syms = [x for x in re.findall(r"#?\$?\b([A-Z]{2,10})(?:/?USDT|\.P|PERP)?\b", text.upper()) if x not in skip]
    if not syms:
        return None
    kinds = [(r"(вход|entry|buy|зона входа|open)", "entry"), (r"(стоп|stop|sl\b|stop[- ]?loss)", "stop"),
             (r"(цел|тейк|tp\d?|take|target)", "target"), (r"(поддерж|support)", "support"), (r"(сопрот|resist)", "resistance")]
    levels = []
    for line in t.splitlines():
        low = line.lower()
        typ = next((k for rx, k in kinds if re.search(rx, low)), None)
        if not typ:
            continue
        for num in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w%]|\.\d)", line):
            v = float(num)
            if v > 0 and not (typ == "target" and v < 10 and re.search(r"tp\s*" + num, low)):
                levels.append({"price": v, "type": typ})
    if not levels:
        return None
    up = text.lower()
    bias = "long" if re.search(r"\b(long|лонг|buy|покуп)", up) else "short" if re.search(r"\b(short|шорт|sell|продаж)", up) else None
    return {"symbol": syms[0], "bias": bias, "levels": levels}


def _parse_date(v) -> int:
    if not v:
        return _now()
    try:
        return int(datetime.fromisoformat(str(v)).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return _now()


class Bloggers:
    def __init__(self, cfg, conn):
        self.cfg, self.conn = cfg, conn

    def add_draft(self, extracted: dict, fallback_author: str | None = None) -> int | None:
        sym = normalize_symbol(extracted.get("symbol"))
        levels = [lv for lv in (extracted.get("levels") or [])
                  if isinstance(lv, dict) and isinstance(lv.get("price"), (int, float)) and lv["price"] > 0]
        if not sym or not levels:
            return None
        call_ts = _parse_date(extracted.get("date"))
        exp = call_ts + self.cfg["bloggers"]["zone_expiry_days"] * DAY
        cur = self.conn.execute(
            "INSERT INTO blogger_calls (ts, author, source, symbol, bias, levels_json, conditions, call_ts, "
            "expires_ts, confirmed, raw_json) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
            (_now(), extracted.get("author") or fallback_author or "unknown", extracted.get("source"), sym,
             extracted.get("bias"), json.dumps(levels), extracted.get("conditions"), call_ts, exp,
             json.dumps(extracted)))
        return cur.lastrowid

    def confirm(self, call_id: int, ok: bool):
        if ok:
            self.conn.execute("UPDATE blogger_calls SET confirmed=1 WHERE id=?", (call_id,))
        else:
            self.conn.execute("DELETE FROM blogger_calls WHERE id=? AND confirmed=0", (call_id,))

    def get(self, call_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM blogger_calls WHERE id=?", (call_id,)).fetchone()
        return dict(r) if r else None

    def zones(self, symbol: str, t: int | None = None) -> list[dict]:
        """Подтверждённые и не истёкшие уровни, слитые в зоны с числом источников."""
        t = t or _now()
        rows = self.conn.execute(
            "SELECT author, levels_json FROM blogger_calls WHERE symbol=? AND confirmed=1 AND expires_ts>? "
            "AND call_ts<=?", (symbol, t, t)).fetchall()
        pts = sorted((lv["price"], r["author"]) for r in rows for lv in json.loads(r["levels_json"]))
        merge = self.cfg["bloggers"]["zone_merge_pct"] / 100
        zones: list[dict] = []
        for price, author in pts:
            if zones and price <= zones[-1]["high"] * (1 + merge):
                z = zones[-1]
                z["high"] = max(z["high"], price)
                z["authors"].add(author)
            else:
                zones.append({"low": price, "high": price, "authors": {author}})
        return [{"low": z["low"], "high": z["high"], "sources": len(z["authors"]),
                 "authors": sorted(z["authors"])} for z in zones]

    def zones_for(self, symbol: str, price: float | None = None, top: int = 3) -> list[dict]:
        z = self.zones(symbol)
        if price:
            z.sort(key=lambda x: abs((x["low"] + x["high"]) / 2 - price))
        else:
            z.sort(key=lambda x: -x["sources"])
        return [{k: v for k, v in x.items() if k != "authors"} for x in z[:top]]

    def accuracy(self) -> list[dict]:
        """Направленная точность авторов по истёкшим прогнозам с явным bias."""
        t = _now()
        rows = self.conn.execute("SELECT * FROM blogger_calls WHERE confirmed=1 AND bias IN ('long','short') "
                                 "AND expires_ts<=?", (t,)).fetchall()
        stats: dict[str, dict] = {}
        for r in rows:
            p0 = self._price_at(r["symbol"], r["call_ts"])
            p1 = self._price_at(r["symbol"], r["expires_ts"])
            if not p0 or not p1:
                continue
            move = (p1 / p0 - 1) * (1 if r["bias"] == "long" else -1)
            s = stats.setdefault(r["author"], {"author": r["author"], "calls": 0, "hits": 0})
            s["calls"] += 1
            s["hits"] += int(move > 0)
        for s in stats.values():
            s["hit_rate_pct"] = round(s["hits"] / s["calls"] * 100, 1)
        return sorted(stats.values(), key=lambda s: -s["calls"])

    def _price_at(self, symbol: str, ts: int) -> float | None:
        r = self.conn.execute("SELECT close FROM candles WHERE symbol=? AND market='futures' AND tf='1h' "
                              "AND ts<=? ORDER BY ts DESC LIMIT 1", (symbol, ts)).fetchone()
        return r[0] if r else None

    @staticmethod
    def describe(call: dict) -> str:
        lv = json.loads(call["levels_json"])
        lines = [f"Author: {call['author']}", f"Coin: {call['symbol']}", f"Bias: {call.get('bias') or '—'}",
                 "Levels:"] + [f"  {x.get('type', 'level')}: {x['price']}" for x in lv]
        if call.get("conditions"):
            lines.append(f"Conditions: {call['conditions']}")
        if call.get("source"):
            lines.append(f"Source: {call['source']}")
        return "\n".join(lines)
