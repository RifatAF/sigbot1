"""Изменяемые владельцем настройки поверх config.yaml (админка в Telegram)."""
from __future__ import annotations

import json
import time


def get(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def put(conn, key: str, value):
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)", (key, json.dumps(value), int(time.time())))


def effective(cfg: dict, conn) -> dict:
    """config.yaml + переопределения из админки."""
    import copy
    out = copy.deepcopy(dict(cfg))
    for path in OVERRIDABLE:
        v = get(conn, path)
        if v is None:
            continue
        node = out
        *parents, leaf = path.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = v
    return out


OVERRIDABLE = [
    "publishing.mode", "publishing.lang", "publishing.paused", "publishing.kill_switch",
    "risk.risk_min_pct", "risk.risk_max_pct", "risk.public_leverage_cap", "risk.personal_leverage_cap",
    "scanner.max_signals_per_day", "schedule.daily_note_hour_utc", "schedule.weekly_day",
    "portfolio.enabled", "listings.enabled", "listings.bybit", "scanner.enabled", "portfolio.signal_min_weight",
]
