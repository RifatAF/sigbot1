from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Signal:
    params_version: str
    ts: int
    symbol: str
    side: str
    entry: float
    stop: float
    tp1: float
    tp2: float
    risk_pct: float
    leverage: float
    strength: float
    mode: str
    features: dict = field(default_factory=dict)
    source: str = "live"


def add_signal(conn: sqlite3.Connection, s: Signal) -> int:
    d = asdict(s)
    d["features_json"] = json.dumps(d.pop("features"), sort_keys=True, default=float)
    cols = ",".join(d)
    cur = conn.execute(
        f"INSERT INTO signals ({cols}) VALUES ({','.join('?' * len(d))})", tuple(d.values())
    )
    return cur.lastrowid


def add_outcome(conn, signal_id: int, ts: int, event: str, price: float,
                closed_fraction: float = 0.0, pnl_r: float | None = None, note: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO outcomes (signal_id, ts, event, price, closed_fraction, pnl_r, note)"
        " VALUES (?,?,?,?,?,?,?)",
        (signal_id, ts, event, price, closed_fraction, pnl_r, note),
    )
    return cur.lastrowid


def register_params(conn, version: str, ts: int, params: dict, report_path: str | None = None):
    """Новая версия параметров = новая запись. Повтор версии с другими параметрами запрещён."""
    row = conn.execute("SELECT params_json FROM params_versions WHERE version=?", (version,)).fetchone()
    blob = json.dumps(params, sort_keys=True)
    if row:
        if row["params_json"] != blob:
            raise ValueError(f"params for {version} changed; bump params_version")
        return
    conn.execute("INSERT INTO params_versions VALUES (?,?,?,?)", (version, ts, blob, report_path))


def export_csv(conn, out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for table in ("signals", "outcomes"):
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        p = out / f"{table}.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            cur = conn.execute(f"SELECT * FROM {table} LIMIT 0")
            w.writerow([c[0] for c in cur.description])
            w.writerows([tuple(r) for r in rows])
        paths.append(p)
    return paths
