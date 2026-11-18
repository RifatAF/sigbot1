"""Итоги недели для владельца: что закрылось по всем стратегиям и разбор убыточных часовых сделок. Правила не меняет."""
from __future__ import annotations

import json
from collections import Counter

DAY = 86_400_000


def reasons(features: dict, exit_event: str) -> list[str]:
    out = ["выход: " + {"stop": "стоп", "tp2": "цель 2", "breakeven_stop": "безубыток", "timeout": "по времени"}.get(exit_event, exit_event)]
    trend = features.get("trend_4h")
    out.append({1: "тренд 4ч вверх", -1: "тренд 4ч вниз", 0: "тренд 4ч боковик"}.get(trend, "тренд неизвестен"))
    fg = features.get("fear_greed")
    if fg is not None:
        out.append("индекс страха: паника" if fg < 25 else "индекс страха: жадность" if fg > 75 else "индекс страха: середина")
    divs = features.get("divergences") or []
    out.append("только скрытые дивергенции" if divs and all(d.startswith("hidden") for d in divs) else
               "есть обычная дивергенция" if divs else "без дивергенции")
    if len(divs) >= 2:
        out.append("совпали несколько осцилляторов")
    if features.get("leverage_capped"):
        out.append("плечо урезано")
    if features.get("tp2_from_liq_cluster"):
        out.append("цель 2 у кластера ликвидаций")
    return out


def build(conn, start: int, end: int) -> str:
    rows = conn.execute(
        "SELECT s.id, s.symbol, s.side, s.features_json, SUM(o.pnl_r) AS r, MAX(o.ts) AS closed, "
        "(SELECT event FROM outcomes WHERE signal_id=s.id AND closed_fraction>0 ORDER BY ts DESC, id DESC LIMIT 1) AS last_event "
        "FROM signals s JOIN outcomes o ON o.signal_id=s.id JOIN trade_state t ON t.signal_id=s.id "
        "WHERE t.status='closed' GROUP BY s.id HAVING closed>=? AND closed<?", (start, end)).fetchall()
    losers = [r for r in rows if (r["r"] or 0) < 0]
    lines = ["🧾 Итоги недели", ""]
    tr = conn.execute("SELECT * FROM trend_signals WHERE close_day>=? AND close_day<?", (start, end)).fetchall()
    if tr:
        w = sum(1 for r in tr if (r["result_pct"] or 0) > 0)
        lines.append(f"🧭 Тренд: закрыто {len(tr)}, в плюс {w}, итог {sum(r['result_pct'] or 0 for r in tr):+.2f}% депозита")
        for r in sorted(tr, key=lambda r: -(r["result_pct"] or 0))[:6]:
            lines.append(f"   {r['symbol'].replace('USDT', '')} {'лонг' if r['side'] == 'long' else 'шорт'}: "
                         f"цена {r['move_pct']:+.1f}%, депозит {r['result_pct']:+.2f}%")
    else:
        lines.append("🧭 Тренд: за неделю сделки не закрывались")
    ls = conn.execute("SELECT * FROM listing_trades WHERE status='closed' AND exit_ts>=? AND exit_ts<?", (start, end)).fetchall()
    lines.append(f"🆕 Листинги: закрыто {len(ls)}, итог {sum(r['pnl_pct'] or 0 for r in ls):+.2f}% депозита" if ls
                 else "🆕 Листинги: за неделю сделки не закрывались")
    lines.append(f"⚡ Капитуляции: закрыто {len(rows)}, в минус {len(losers)}")
    if not losers:
        return "\n".join(lines)
    cnt: Counter = Counter()
    by_reason: dict[str, list] = {}
    for r in losers:
        for reason in reasons(json.loads(r["features_json"]), r["last_event"] or "?"):
            cnt[reason] += 1
            by_reason.setdefault(reason, []).append(f"{r['symbol'].replace('USDT', '')} {r['r']:+.2f}R")
    lines += ["", "Почему часовые сделки ушли в минус (что у них было общего):"]
    for reason, n in cnt.most_common():
        lines.append(f"  {reason}: {n}  ({', '.join(by_reason[reason][:5])})")
    lines.append(f"Сумма убытков: {sum(r['r'] for r in losers):+.2f}R. Правила бот сам не меняет.")
    return "\n".join(lines)
