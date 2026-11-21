import asyncio

import pytest

from sigbot import settings, templates
from sigbot.llm import Gemini
from sigbot.publisher import Publisher


def _sig():
    return dict(symbol="ETHUSDT", side="short", entry=3012.5, stop=3080.0, tp1=2931.5, tp2=2843.75,
                risk_pct=0.9, leverage=4.02, strength=0.66, mode="paper",
                features={"divergences": ["regular bearish cvd_fut"], "trend_4h": -1, "fear_greed": 61})


@pytest.mark.asyncio
async def test_paper_mode_publishes_to_channel_with_tag(cfg, conn, tg):
    pub = Publisher(cfg, conn, tg, -100, 7)
    text, nums = templates.signal(_sig())
    assert await pub.publish("signal", text, nums, png=b"x") == "sent"
    chat, body, photo, _ = tg.sent[0]
    assert chat == -100 and photo and "#forwardtest" in body


@pytest.mark.asyncio
async def test_number_mismatch_blocked_and_logged(cfg, conn, tg):
    pub = Publisher(cfg, conn, tg, -100, 7)
    text, nums = templates.signal(_sig())
    tampered = text.replace("3,080.00", "3,090.00")
    assert await pub.publish("signal", tampered, nums) == "blocked"
    assert all(chat != -100 for chat, *_ in tg.sent)       # в канал ничего
    assert conn.execute("SELECT status FROM posts").fetchone()[0] == "blocked"


@pytest.mark.asyncio
async def test_pause_and_kill_switch(cfg, conn, tg):
    pub = Publisher(cfg, conn, tg, -100, 7)
    text, nums = templates.update(_sig(), "tp1", 2931.5)
    settings.put(conn, "publishing.paused", True)
    assert await pub.publish("update", text, nums) == "paused"
    settings.put(conn, "publishing.paused", False)
    settings.put(conn, "publishing.kill_switch", True)
    assert await pub.publish("update", text, nums) == "killed"
    assert tg.sent == []


@pytest.mark.asyncio
async def test_confirm_mode_waits_for_owner(cfg, conn, tg):
    settings.put(conn, "publishing.mode", "confirm")
    pub = Publisher(cfg, conn, tg, -100, 7)
    text, nums = templates.update(_sig(), "stop", 3080.0, -1.0)
    assert await pub.publish("update", text, nums) == "pending"
    assert tg.sent[0][0] == 7 and tg.sent[0][3]  # владельцу, с кнопками
    pid = conn.execute("SELECT id FROM posts WHERE status='pending'").fetchone()[0]
    assert await pub.resolve_pending(pid, True) == "published"
    assert tg.sent[-1][0] == -100
    assert await pub.resolve_pending(pid, True) == "not found"   # второй раз не публикуется


def test_all_templates_pass_their_own_check(cfg):
    from sigbot.textguard import verify
    for text, nums in [
        templates.signal(_sig(), "Sellers lead on futures.", [{"low": 2950.0, "high": 2961.2, "sources": 3}]),
        templates.update(_sig(), "tp2", 2843.75, 1.85),
        templates.daily_note({"btc_price": 64321.5, "btc_change_24h": -2.345, "avg_funding_pct": -0.0042,
                              "fear_greed": 12, "coinbase_premium_pct": -0.051, "open_trades": 1, "signals_today": 0}),
        templates.weekly([{"symbol": "SOLUSDT", "side": "long", "r": -1.0, "pnl_pct": -0.95}],
                         {"trades": 1, "wins": 0, "sum_r": -1.0, "pnl_pct": -0.95}),
        templates.method(cfg, None),
    ]:
        verify(text, nums)
    assert "No signal, we wait." in templates.daily_note({"open_trades": 0, "signals_today": 0})[0]


@pytest.mark.asyncio
async def test_llm_comment_with_digits_is_dropped():
    g = Gemini("k", None, "m", "m2")

    async def fake(parts, system, json_mode=False):
        return "Price could reach seventy k, maybe 70000 soon"
    g._gen = fake
    assert await g.comment({"x": 1}) == ""

    async def ok(parts, system, json_mode=False):
        return "Spot buyers absorbed the drop."
    g._gen = ok
    assert await g.comment({}) == "Spot buyers absorbed the drop."
