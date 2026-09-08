import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from classifier import classify  # noqa: E402
from project_score import project_score  # noqa: E402


def test_project_score_counts_five_signals_only():
    b = {"image_uri": "https://x/logo.png", "website": "https://site", "twitter": "https://x.com/a", "telegram": "t.me/a",
         "creator_tokens_graduated": 2, "reply_count": 7, "meta_seen": True}
    s, f = project_score(b)
    assert s == 5 and f["telegram"] and f["meta_seen"]
    b.update(telegram="", reply_count=2, creator_tokens_graduated=0)
    s, f = project_score(b)
    assert s == 3 and not f["posts"] and not f["creator_graduated"]
    assert project_score({})[0] == 0


def test_classifier_project_score_gate():
    from models import ClassifierRules
    rules = {**ClassifierRules().model_dump(), "project_score_min": 3}
    base = {"curve_fill_pct": 10, "elapsed_s": 5, "unique_buyers": 5, "sol_inflow": 2, "creator_rugs": 0}
    v = classify({**base, "project_score": 2}, rules)
    assert v["action"] == "abort_trade" and "project score 2/5" in v["reasons"][0]
    v2 = classify({**base, "project_score": 4}, rules)
    assert v2["action"] != "abort_trade"
    v3 = classify({**base, "project_score": 0}, {**rules, "project_score_min": 0})
    assert v3["action"] != "abort_trade"
    v4 = classify({**base, "project_score": 0, "project_meta_seen": False}, rules)   # metadata not fetched yet → no verdict on it
    assert v4["action"] != "abort_trade"


def test_serial_creator_gate():
    from models import ClassifierRules
    rules = {**ClassifierRules().model_dump(), "serial_creator_gate_enabled": True, "serial_creator_min_launches": 3,
             "serial_creator_requires_graduation": True}
    base = {"curve_fill_pct": 10, "elapsed_s": 5, "unique_buyers": 5, "sol_inflow": 2, "creator_rugs": 0, "project_score": 3}
    v = classify({**base, "creator_prior_launches": 7, "creator_graduated_before": False}, rules)
    assert v["action"] == "abort_trade" and "serial creator" in v["reasons"][0]
    assert classify({**base, "creator_prior_launches": 7, "creator_graduated_before": True}, rules)["action"] != "abort_trade"
    assert classify({**base, "creator_prior_launches": 2, "creator_graduated_before": False}, rules)["action"] != "abort_trade"
    assert classify({**base, "creator_prior_launches": 30}, {**rules, "serial_creator_min_launches": 0})["action"] != "abort_trade"
    assert classify({**base, "creator_prior_launches": 30}, {**rules, "serial_creator_gate_enabled": False})["action"] != "abort_trade"


def test_ceiling_feature_split_proposes_lowering():
    from book_params import entry_feature_splits
    cfg = {"serial_creator_min_launches": 0, "min_curve_liquidity_sol": 0, "min_buyers_for_entry": 0, "project_score_min": 0}
    rows = [{"pnl_usd": -1.0, "entry_ctx": {"creator_prior_launches": 10 + i}} for i in range(8)]
    rows += [{"pnl_usd": 0.8, "entry_ctx": {"creator_prior_launches": i % 3}} for i in range(8)]
    sp = [x for x in entry_feature_splits(rows, "momentum", cfg) if x["feature"] == "creator_prior_launches"][0]
    assert sp["direction"] == "ceiling" and sp["actionable"] and sp["gain_usd_per_fill"] > 0 and sp["split"] >= 1
