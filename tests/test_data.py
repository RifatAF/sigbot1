import pandas as pd
import pytest

from sigbot import archive, features, settings
from sigbot.collector import Collector, coverage, find_gaps
from sigbot.sources import parse_force_order, parse_klines

from .conftest import HOUR, put_hourly

MIN = 60_000


def test_find_gaps():
    ts = [0, MIN, 2 * MIN, 5 * MIN, 6 * MIN]
    assert find_gaps(ts, MIN) == [(3 * MIN, 4 * MIN)]
    assert find_gaps([0, MIN], MIN) == []


def test_force_order_side_mapping():
    msg = {"o": {"s": "BTCUSDT", "S": "SELL", "ap": "60000", "z": "0.5", "T": 123, "p": "0", "q": "0"}}
    assert parse_force_order(msg) == ("BTCUSDT", 123, "long", 60000.0, 0.5, 30000.0)
    assert parse_force_order({"o": {"s": "X", "S": "BUY", "ap": "2", "z": "3", "T": 1}})[2] == "short"


def test_parse_klines_and_archive_rows():
    raw = [[1000, "1", "2", "0.5", "1.5", "10", 1999, "15", 5, "6", "9", "0"]]
    assert parse_klines(raw) == [(1000, 1.0, 2.0, 0.5, 1.5, 10.0, 6.0)]
    rows = [["1735689600000000", "1", "2", "0.5", "1.5", "10", "x", "15", "5", "6", "9", "0"]]
    assert archive.parse_kline_rows(rows)[0][0] == 1735689600000   # микросекунды спота -> мс
    m = archive.parse_metrics_rows([["2026-09-01 00:05:00", "BTCUSDT", "100.5", "8000000", "1.4", "1.2", "1.3", "0.9"]])
    assert m[0][:3] == (1788220800000 + 5 * MIN, 100.5, 8000000.0) and m[0][3:7] == (1.4, 1.2, 1.3, 0.9)


class FakeBinance:
    """Отдаёт минутные свечи из «биржи», как REST с startTime."""

    def __init__(self, upto):
        self.upto = upto

    async def klines(self, symbol, market, interval, start_ms=None, limit=1000):
        step = MIN if interval == "1m" else HOUR
        t = start_ms
        out = []
        while t <= self.upto and len(out) < limit:
            out.append((t, 1.0, 1.0, 1.0, 1.0, 1.0, 0.5))
            t += step
        return out


@pytest.mark.asyncio
async def test_sync_restart_has_no_duplicates_and_fills_gaps(cfg, conn):
    col = Collector(cfg, conn, http=None)
    base = 1_800_000_000_000 // HOUR * HOUR
    col.bn = FakeBinance(base + 30 * MIN)
    import sigbot.collector as cm
    orig = cm.now_ms
    cm.now_ms = lambda: base + 31 * MIN
    try:
        await col.sync_klines("BTCUSDT", "futures", "1m", 30 * MIN)
        n1 = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        await col.sync_klines("BTCUSDT", "futures", "1m", 30 * MIN)   # «перезапуск»
        assert conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == n1
        # обрыв на 10 минут: следующая синхронизация догружает всё с последней метки
        col.bn.upto = base + 45 * MIN
        await col.sync_klines("BTCUSDT", "futures", "1m", 30 * MIN)
        ts = [r[0] for r in conn.execute("SELECT ts FROM candles ORDER BY ts")]
        assert find_gaps(ts, MIN) == [] and ts[-1] == base + 45 * MIN
        assert coverage(conn, ["BTCUSDT"], "futures", base + MIN, base + 45 * MIN) == 1.0
    finally:
        cm.now_ms = orig


def test_detect_gaps_logged_once(cfg, conn):
    col = Collector(cfg, conn, http=None)
    col.symbols = ["BTCUSDT"]
    import sigbot.collector as cm
    end = 1_800_000_000_000 // MIN * MIN
    rows = [("BTCUSDT", "futures", "1m", end - i * MIN, 1, 1, 1, 1, 1, 0.5) for i in range(70) if i not in (20, 21)]
    conn.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    orig = cm.now_ms
    cm.now_ms = lambda: end + 3 * MIN
    try:
        col.detect_gaps()
        col.detect_gaps()
    finally:
        cm.now_ms = orig
    g = conn.execute("SELECT source, start_ts, end_ts FROM gaps").fetchall()
    assert [(r[0], r[1], r[2]) for r in g] == [("klines_futures", end - 21 * MIN, end - 20 * MIN)]


def test_features_no_lookahead(conn):
    closes = [100.0 + i for i in range(10)]
    start = put_hourly(conn, "BTCUSDT", closes, oi=[1000.0 + i for i in range(10)])
    # OI, записанный после закрытия бара, не должен попасть в этот бар
    conn.execute("DELETE FROM open_interest WHERE ts > ?", (start + 4 * HOUR,))
    conn.execute("INSERT INTO open_interest VALUES ('BTCUSDT', ?, 99999, 0)", (start + 4 * HOUR,))
    df = features.build(conn, "BTCUSDT", start, start + 10 * HOUR)
    assert df.oi.iloc[3] == 1003.0       # значение после закрытия бара 3 в него не попало
    assert df.oi.iloc[4] == 99999.0
    assert {"cvd_fut", "cvd_spot"} <= set(df.columns)
    assert "liq_long_usd" not in df       # своей записи ликвидаций за окно нет


def test_features_liquidations_used_when_recorded(conn):
    start = put_hourly(conn, "BTCUSDT", [100.0] * 5)
    conn.execute("INSERT INTO liquidations VALUES ('BTCUSDT', ?, 'long', 100, 2, 200)", (start - 1,))
    conn.execute("INSERT INTO liquidations VALUES ('BTCUSDT', ?, 'long', 100, 3, 300)", (start + HOUR + 5,))
    df = features.build(conn, "BTCUSDT", start, start + 5 * HOUR)
    assert df.liq_long_usd.tolist() == [0, 300, 0, 0, 0]


def test_settings_override(cfg, conn):
    settings.put(conn, "risk.risk_max_pct", 1.2)
    eff = settings.effective(cfg, conn)
    assert eff["risk"]["risk_max_pct"] == 1.2 and cfg["risk"]["risk_max_pct"] == 4.5
