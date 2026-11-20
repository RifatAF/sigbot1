"""Ежедневный пост «Позиции»: все открытые сделки всех стратегий, что с каждой сейчас, и что закрылось за сутки."""
from __future__ import annotations

import json
import time

from . import settings
from .templates import Fmt, _coin, _side, _tag

DAY = 86_400_000
ICON = {"trend": "🧭", "capitulation": "⚡", "listing": "🆕", "layers": "🧱"}


def _price(conn, sym: str) -> float | None:
    r = conn.execute("SELECT close FROM candles WHERE symbol=? AND market='futures' ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
    return float(r[0]) if r else None


def collect(conn, now_ms: int | None = None) -> dict:
    """Открытые позиции и закрытые за последние сутки, в одном виде для поста."""
    now = now_ms or int(time.time() * 1000)
    op, cl = [], []
    for r in conn.execute("SELECT * FROM trend_signals WHERE status='open' ORDER BY open_day"):
        st = json.loads(r["state"]) if r["state"] else {}
        hits = [e["what"][-1] for e in st.get("exits", []) if e["what"] in ("tp1", "tp2")]
        op.append(dict(kind="trend", symbol=r["symbol"], side=r["side"], entry=r["entry"], stop=st.get("stop", r["stop"]),
                       hits=hits, next_tp=None if "2" in hits else (r["tp2"] if hits else r["tp1"])))
    for r in conn.execute("SELECT s.symbol, s.side, s.entry, s.tp1, s.tp2, t.stop, t.remaining FROM signals s "
                          "JOIN trade_state t ON t.signal_id=s.id WHERE t.status='open' AND s.source='live'"):
        hits = ["1"] if (r["remaining"] or 1) < 0.99 else []
        op.append(dict(kind="capitulation", symbol=r["symbol"], side=r["side"], entry=r["entry"], stop=r["stop"],
                       hits=hits, next_tp=r["tp2"] if hits else r["tp1"]))
    for r in conn.execute("SELECT * FROM listing_trades WHERE status='open'"):
        op.append(dict(kind="listing", symbol=r["symbol"], side="short", entry=r["entry"], stop=r["stop"], hits=[],
                       days_left=max(0, round(((r["exit_due_ts"] or now) - now) / DAY))))
    try:
        from .btc_layers import live_items
        lay = live_items(conn)
    except Exception:
        lay = []
    for x in lay:
        if x.get("open"):
            op.append(dict(kind="layers", symbol="BTCUSDT", side=x["side"], entry=x["entry"], size=x.get("size_now")))
        elif x.get("exit_time") and x["exit_time"] * 1000 >= now - DAY:
            cl.append(dict(kind="layers", symbol="BTCUSDT", side=x["side"], result=x.get("result_pct")))
    for x in op:
        x["price"] = _price(conn, x["symbol"])
        if x["price"]:
            x["move"] = (x["price"] / x["entry"] - 1) * 100 * (1 if x["side"] == "long" else -1)
    for r in conn.execute("SELECT symbol, side, result_pct FROM trend_signals WHERE status='closed' AND close_day>=?",
                          (now - DAY,)):
        cl.append(dict(kind="trend", symbol=r["symbol"], side=r["side"], result=r["result_pct"]))
    for r in conn.execute("SELECT symbol, pnl_pct FROM listing_trades WHERE status='closed' AND exit_ts>=?", (now - DAY,)):
        cl.append(dict(kind="listing", symbol=r["symbol"], side="short", result=r["pnl_pct"]))
    for r in conn.execute("SELECT s.symbol, s.side, s.risk_pct, SUM(o.pnl_r) AS r, MAX(o.ts) AS closed_at FROM signals s "
                          "JOIN outcomes o ON o.signal_id=s.id JOIN trade_state t ON t.signal_id=s.id "
                          "WHERE t.status='closed' AND s.source='live' GROUP BY s.id HAVING closed_at>=?", (now - DAY,)):
        cl.append(dict(kind="capitulation", symbol=r["symbol"], side=r["side"],
                       result=None if r["r"] is None else r["r"] * r["risk_pct"]))
    return {"open": op, "closed": cl, "now": now}


def post(data: dict, mode: str, lang: str = "en") -> tuple[str, list[float]]:
    f, ru = Fmt(), lang == "ru"
    day = time.strftime("%d.%m", time.gmtime(data["now"] / 1000))
    f.numbers += [float(day), 1.0, 2.0]                 # дата 20.11 читается как одно число
    op = sorted(data["open"], key=lambda x: -(x.get("move") or -1e9))
    lines = [f"📋 <b>{'Позиции' if ru else 'Positions'} · {day}</b>", ""]
    if not op:
        lines.append("Открытых позиций нет, ждём сигнал" if ru else "No open positions, waiting for a signal")
    for x in op:
        lines.append(f"{'🟢' if x['side'] == 'long' else '🔴'} <b>#{_coin(x['symbol'])} {_side(x['side'], lang)}</b> {ICON[x['kind']]}")
        now = f"{f.p(x['entry'])} → {f.p(x['price'])} ({f.n(x['move'], 1, sign=True)}%)" if x.get("price") else f"{'вход' if ru else 'entry'} {f.p(x['entry'])}"
        tail = []
        if x["kind"] == "layers":
            tail.append((f"размер {f.n(x['size'], 0)}%" if ru else f"size {f.n(x['size'], 0)}%") if x.get("size") else "")
        else:
            for h in x.get("hits", []):
                tail.append(f"{'цель' if ru else 'target'} {h} ✅")
            if x.get("next_tp") and x.get("price"):
                left = abs(x["next_tp"] / x["price"] - 1) * 100
                n = "2" if x.get("hits") else "1"
                tail.append((f"до цели {n}: {f.n(left, 1)}%" if ru else f"target {n} is {f.n(left, 1)}% away"))
            if x["kind"] == "listing":
                tail.append(f"выход через {f.i(x['days_left'])} дн." if ru else f"exit in {f.i(x['days_left'])} d")
            if x.get("stop"):
                tail.append(f"{'стоп' if ru else 'stop'} {f.p(x['stop'])}")
        lines.append("   " + " · ".join([now] + [t for t in tail if t]))
    if data["closed"]:
        lines += ["", "🏁 " + ("Закрыто за сутки" if ru else "Closed in 24h")]
        for x in data["closed"]:
            res = x.get("result")
            lines.append(f"   #{_coin(x['symbol'])} {_side(x['side'], lang)} {ICON[x['kind']]}"
                         + (f": <b>{f.n(res, 2, sign=True)}%</b> {'депозита' if ru else 'of capital'}" if res is not None else ""))
    moved = [x for x in op if x.get("move") is not None]
    if moved:
        win = sum(1 for x in moved if x["move"] > 0)
        lines += ["", (f"В плюсе {f.i(win)} из {f.i(len(moved))}" if ru else f"In profit: {f.i(win)} of {f.i(len(moved))}")]
    return "\n".join(lines) + _tag(mode), f.numbers


async def publish(cfg, conn, pub) -> str:
    eff = settings.effective(cfg, conn)["publishing"]
    text, nums = post(collect(conn), eff.get("mode", "paper"), eff.get("lang", "en"))
    return await pub.publish("positions", text, nums)
