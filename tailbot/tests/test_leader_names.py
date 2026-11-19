import json
from tailbot.board import format_active_trades
from tailbot.config import Config, leader_label, load_config
from tailbot.helius_ws import HeliusWS

W = [f"{c}" * 44 for c in "ABCDEF"]

class Meter:
    def add_ws(self, n): pass

def test_label_with_and_without_name():
    cfg = Config(); cfg.leader_names = {W[0]: "losther"}
    assert leader_label(W[0], cfg) == "losther (AAAA…)"
    assert leader_label(W[1], cfg) == "BBBB…"
    text = format_active_trades(
        [{
            "mint": "2tKjPFvzeWqBmmxjtGBb3zfb8zfFVePjpBhHbE2epump",
            "leader": W[0], "opened_ts": 1_700_000_000.0, "entry_price": 0.01,
            "last_price": 0.01, "peak_price": 0.01, "tokens_raw": 1_000_000,
            "decimals": 6, "cost_sol": 0.1,
        }],
        pot=1, deployed=0.1, realized=0, now=1_700_000_000.0, cfg=cfg,
    )
    assert "losther AAAA…AAAA" in text

def test_six_leaders_with_max_leaders_6_all_subscribed(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"leaders": W, "max_leaders": 6, "leader_names": {W[5]: "Ansem"}}))
    cfg = load_config(str(p))
    assert len(cfg.leaders) == 6 and cfg.leader_names[W[5]] == "Ansem"
    ws = HeliusWS("k", None, None, Meter()); ws.set_leaders(cfg.leaders)
    assert ws.leaders == W  # no silent [:3] truncation

def test_more_leaders_than_max_is_refused(tmp_path):
    p = tmp_path / "c.json"; p.write_text(json.dumps({"leaders": W}))  # default max_leaders 3
    try:
        load_config(str(p)); assert False
    except SystemExit as e:
        assert "maximum is 3" in str(e)
