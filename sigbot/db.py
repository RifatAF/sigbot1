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
CREATE TABLE IF NOT EXISTS positioning (
    symbol TEXT, ts INTEGER, top_ls_acc REAL, top_ls_pos REAL, ls_acc REAL, taker_ls_vol REAL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS basis (
    symbol TEXT, ts INTEGER, basis REAL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS portfolio_weights (
    day INTEGER, symbol TEXT, weight REAL, PRIMARY KEY (day, symbol)
);
CREATE TABLE IF NOT EXISTS portfolio_equity (
    day INTEGER PRIMARY KEY, equity REAL, pnl_pct REAL, gross REAL
);
CREATE TABLE IF NOT EXISTS listing_trades (
    symbol TEXT PRIMARY KEY, onboard_ts INTEGER, entry_ts INTEGER, entry REAL, stop REAL, exit_due_ts INTEGER,
    notional_pct REAL, status TEXT, exit_ts INTEGER, exit_price REAL, exit_reason TEXT, pnl_pct REAL
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

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated INTEGER
);
CREATE TABLE IF NOT EXISTS trade_state (
    signal_id INTEGER PRIMARY KEY REFERENCES signals(id),
    status TEXT NOT NULL, stop REAL NOT NULL, remaining REAL NOT NULL,
    be_moved INTEGER NOT NULL DEFAULT 0, last_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, kind TEXT NOT NULL,
    signal_id INTEGER, status TEXT NOT NULL, text TEXT NOT NULL, photo TEXT,
    message_id INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS blogger_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, author TEXT NOT NULL,
    source TEXT, symbol TEXT NOT NULL, bias TEXT, levels_json TEXT NOT NULL,
    conditions TEXT, call_ts INTEGER NOT NULL, expires_ts INTEGER NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0, raw_json TEXT
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
