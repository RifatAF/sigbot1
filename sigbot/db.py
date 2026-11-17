from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT, market TEXT, tf TEXT, ts INTEGER,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, taker_buy REAL,
    PRIMARY KEY (symbol, market, tf, ts)
);
CREATE TABLE IF NOT EXISTS open_interest (
    symbol TEXT, ts INTEGER, oi REAL, oi_usd REAL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS funding (
    symbol TEXT, ts INTEGER, rate REAL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS liquidations (
    symbol TEXT, ts INTEGER, side TEXT, price REAL, qty REAL, usd REAL,
    PRIMARY KEY (symbol, ts, side, price, qty)
);
CREATE TABLE IF NOT EXISTS premium (
    ts INTEGER PRIMARY KEY, coinbase REAL, binance REAL, premium_pct REAL
);
CREATE TABLE IF NOT EXISTS fear_greed (
    ts INTEGER PRIMARY KEY, value INTEGER
);
CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, symbol TEXT, start_ts INTEGER, end_ts INTEGER,
    backfilled INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS health (
    ts INTEGER, component TEXT, status TEXT, detail TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    params_version TEXT NOT NULL,
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('long','short')),
    entry REAL NOT NULL, stop REAL NOT NULL, tp1 REAL NOT NULL, tp2 REAL NOT NULL,
    risk_pct REAL NOT NULL, leverage REAL NOT NULL, strength REAL NOT NULL,
    mode TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'live' CHECK (source IN ('live','backtest')),
    features_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    ts INTEGER NOT NULL,
    event TEXT NOT NULL,
    price REAL NOT NULL,
    closed_fraction REAL NOT NULL DEFAULT 0,
    pnl_r REAL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS params_versions (
    version TEXT PRIMARY KEY, ts INTEGER NOT NULL, params_json TEXT NOT NULL,
    backtest_report TEXT
);

CREATE TRIGGER IF NOT EXISTS signals_no_update BEFORE UPDATE ON signals
BEGIN SELECT RAISE(ABORT, 'signals is append-only'); END;
CREATE TRIGGER IF NOT EXISTS signals_no_delete BEFORE DELETE ON signals
BEGIN SELECT RAISE(ABORT, 'signals is append-only'); END;
CREATE TRIGGER IF NOT EXISTS outcomes_no_update BEFORE UPDATE ON outcomes
BEGIN SELECT RAISE(ABORT, 'outcomes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS outcomes_no_delete BEFORE DELETE ON outcomes
BEGIN SELECT RAISE(ABORT, 'outcomes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS params_no_update BEFORE UPDATE ON params_versions
BEGIN SELECT RAISE(ABORT, 'params_versions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS params_no_delete BEFORE DELETE ON params_versions
BEGIN SELECT RAISE(ABORT, 'params_versions is append-only'); END;
"""


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
