from sigbot import cloud, phone


def test_link_has_token_and_can_be_off(cfg, conn):
    cfg["cloud"] = {"enabled": True, "url": "https://sigbot-panel.vercel.app/"}
    assert cloud.link(cfg, conn) == "https://sigbot-panel.vercel.app/?t=" + phone.token(conn)
    cfg["cloud"]["enabled"] = False
    assert cloud.link(cfg, conn) is None


def test_signature_ignores_prices_but_sees_events():
    a = {"feed": {"items": [{"kind": "trend", "ref": "1", "open": True, "exits": [], "result_pct": 1.0}]},
         "paper": {"trades": {"trend:1": [{"status": "open", "open_qty": 2.0}]}}}
    b = {"feed": {"items": [{"kind": "trend", "ref": "1", "open": True, "exits": [], "result_pct": 3.5}]},
         "paper": {"trades": {"trend:1": [{"status": "open", "open_qty": 2.0}]}}}
    c = {"feed": {"items": [{"kind": "trend", "ref": "1", "open": True, "exits": [{"what": "tp1"}]}]},
         "paper": {"trades": {"trend:1": [{"status": "open", "open_qty": 1.5}]}}}
    assert cloud.signature(a) == cloud.signature(b) != cloud.signature(c)
