"""Недельный разбор убыточных сделок для владельца. Правила не меняет."""
from __future__ import annotations

import json
from collections import Counter

DAY = 86_400_000


def reasons(features: dict, exit_event: str) -> list[str]:
    out = [f"exit: {exit_event}"]
    trend = features.get("trend_4h")
    out.append({1: "trend 4h up", -1: "trend 4h down", 0: "trend 4h flat"}.get(trend, "trend n/a"))
    fg = features.get("fear_greed")
    if fg is not None:
        out.append("F&G extreme fear" if fg < 25 else "F&G extreme greed" if fg > 75 else "F&G middle")
    divs = features.get("divergences") or []
    out.append("only hidden divergences" if divs and all(d.startswith("hidden") for d in divs) else
               "has regular divergence" if divs else "no divergence label")
    if len(divs) >= 2:
        out.append("multi oscillator confluence")
    if features.get("leverage_capped"):
        out.append("leverage capped")
    if features.get("tp2_from_liq_cluster"):
        out.append("tp2 at liq cluster")
    return out


def build(conn, start: int, end: int) -> str:
    rows = conn.execute(
        "SELECT s.id, s.symbol, s.side, s.features_json, SUM(o.pnl_r) AS r, MAX(o.ts) AS closed, "
        "(SELECT event FROM outcomes WHERE signal_id=s.id AND closed_fraction>0 ORDER BY ts DESC, id DESC LIMIT 1) AS last_event "
        "FROM signals s JOIN outcomes o ON o.signal_id=s.id JOIN trade_state t ON t.signal_id=s.id "
        "WHERE t.status='closed' GROUP BY s.id HAVING closed>=? AND closed<?", (start, end)).fetchall()
    losers = [r for r in rows if (r["r"] or 0) < 0]
    lines = ["Weekly post mortem (rules are not changed automatically)", "",
             f"Closed trades: {len(rows)}, losing: {len(losers)}"]
    if not losers:
        return "\n".join(lines + ["No losing trades this week."])
    cnt: Counter = Counter()
    by_reason: dict[str, list] = {}
    for r in losers:
        for reason in reasons(json.loads(r["features_json"]), r["last_event"] or "?"):
            cnt[reason] += 1
            by_reason.setdefault(reason, []).append(f"{r['symbol']} {r['side']} {r['r']:+.2f}R")
    lines.append("")
    for reason, n in cnt.most_common():
        lines.append(f"{reason}: {n}  ({', '.join(by_reason[reason][:5])})")
    side = Counter(r["side"] for r in losers)
    lines += ["", f"By side: {dict(side)}", f"Sum R of losers: {sum(r['r'] for r in losers):+.2f}"]
    return "\n".join(lines)
