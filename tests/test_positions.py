import json
import time

from sigbot import positions, settings
from sigbot.textguard import verify

DAY = 86_400_000


def _candle(conn, sym, px, ts):
    conn.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)", (sym, "futures", "1h", ts, px, px, px, px, 1, 1))


def test_daily_positions_post_all_strategies(conn):
    now = int(time.time() * 1000)
    st = {"exits": [{"what": "tp1", "price": 59800}], "stop": 68200}
    conn.execute("INSERT INTO trend_signals (symbol, side, open_day, entry, size_pct, stop, tp1, tp2, state) "
                 "VALUES ('BTCUSDT','short',?,64000,14,68200,59800,55600,?)", (now - 5 * DAY, json.dumps(st)))
    conn.execute("INSERT INTO trend_signals (symbol, side, open_day, entry, size_pct, status, close_day, result_pct) "
                 "VALUES ('ETHUSDT','long',?,3000,10,'closed',?,1.25)", (now - 9 * DAY, now - 3600_000))
    conn.execute("INSERT INTO listing_trades (symbol, entry_ts, entry, stop, exit_due_ts, notional_pct, status) "
                 "VALUES ('NEWUSDT',?,0.5,0.75,?,4.5,'open')", (now - 10 * DAY, now + 50 * DAY))
    settings.put(conn, "btc_layers_posted", {"date": "2026-11-16", "signal": -0.9, "btc": 64856.0})
    _candle(conn, "BTCUSDT", 62000.0, now - 3600_000)
    _candle(conn, "NEWUSDT", 0.45, now - 3600_000)
    for lang in ("en", "ru"):
        text, nums = positions.post(positions.collect(conn, now), "paper", lang)
        verify(text, nums)
    text, _ = positions.post(positions.collect(conn, now), "paper", "en")
    assert "#BTC SHORT</b> 🧭" in text and "64,000 → 62,000 (+3.1%)" in text and "target 1 ✅" in text
    assert "stop 68,200" in text and "#NEW SHORT" in text and "exit in 50 d" in text and "🧱" in text
    assert "Closed in 24h" in text and "#ETH LONG 🧭: <b>+1.25%</b>" in text and "In profit: 3 of 3" in text


def test_empty_positions(conn):
    text, nums = positions.post(positions.collect(conn), "paper", "ru")
    verify(text, nums)
    assert "Открытых позиций нет" in text
