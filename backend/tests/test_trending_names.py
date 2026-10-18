"""Trending-name gate: a cloned ticker → buy only the leader smart wallets pile into."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import trending_names as tn  # noqa: E402
from models import BotConfig  # noqa: E402

NOW = 1_000_000.0


def _b(symbol, start_ago=60, smart=(), mc=0.0, buyers=0, name=None):
    hits = {f"W{i}": {"ts": NOW - 10, "kind": k, "sol": 1.0} for i, k in enumerate(smart)}
    return {"symbol": symbol, "name": name or symbol, "start": NOW - start_ago, "smart_hits": hits, "usd_market_cap": mc,
            "buyers": set(f"b{i}" for i in range(buyers)), "buy_events": []}


def test_norm_key_merges_ticker_variants():
    assert tn.norm_key("$FLY", None) == tn.norm_key("fly", None) == tn.norm_key("Fly ", "whatever") == "fly"
    assert tn.norm_key("", "Fly Token") == "flytoken"
    assert tn.norm_key("$", "") is None


def test_cluster_needs_min_copies_inside_window():
    cfg = BotConfig(trend_name_min_copies=3, band_new_max_age_min=15, band_seasoned_max_age_min=60)
    tr = {"A": _b("FLY"), "B": _b("fly"), "C": _b("$FLY", start_ago=61 * 60)}     # third copy is older than the window
    assert tn.build_clusters(tr, cfg, NOW) == {}
    tr["D"] = _b("FLY", start_ago=30 * 60)                                        # 30 min < 60 min seasoned window
    cl = tn.build_clusters(tr, cfg, NOW)
    assert set(cl) == {"fly"} and cl["fly"]["copies"] == 3
    assert tn.build_clusters(tr, BotConfig(trend_name_gate_enabled=False), NOW) == {}


def test_leader_is_smart_wallets_then_mc_then_buyers():
    cfg = BotConfig()
    tr = {"A": _b("FLY", mc=90_000, buyers=200), "B": _b("FLY", smart=("learned",), mc=20_000), "C": _b("FLY", smart=("manual",), mc=5_000)}
    cl = tn.build_clusters(tr, cfg, NOW)["fly"]
    assert cl["leader"] == "C"                       # one manual (weight 2) beats one learned (weight 1) beats big MC with nobody smart
    tr["B"]["smart_hits"]["W9"] = {"ts": NOW, "kind": "learned", "sol": 1.0}
    assert tn.build_clusters(tr, cfg, NOW)["fly"]["leader"] == "B"                   # tie on weight 2 → MC decides → B
    nobody = {"A": _b("FLY", mc=90_000, buyers=200), "B": _b("FLY", mc=20_000, buyers=400), "C": _b("FLY", mc=90_000, buyers=250)}
    assert tn.build_clusters(nobody, cfg, NOW)["fly"]["leader"] == "C"               # MC tie → buyers


def test_gate_refuses_non_leader_and_smartless_leader_and_caps_positions():
    cfg = BotConfig(trend_name_min_copies=3, trend_name_leader_min_smart=1, trend_name_max_positions=2)
    tr = {"A": _b("FLY", mc=90_000), "B": _b("FLY", mc=20_000), "C": _b("FLY", mc=5_000)}
    cl = tn.build_clusters(tr, cfg, NOW)
    ok, why, c = tn.gate(cl, "A", tr["A"], cfg)
    assert not ok and "no smart wallet" in why and c["copies"] == 3            # leader by MC but nobody smart → buy none
    tr["B"]["smart_hits"]["W"] = {"ts": NOW, "kind": "learned", "sol": 1.0}
    cl = tn.build_clusters(tr, cfg, NOW)
    assert tn.gate(cl, "B", tr["B"], cfg)[0]                                    # leader with a smart buy → allowed
    ok, why, _ = tn.gate(cl, "A", tr["A"], cfg)
    assert not ok and "not the leader" in why and "B" in why                    # the others are gated
    # we hold A (not the leader): B may still be bought (cap 2) …
    cl = tn.build_clusters(tr, cfg, NOW, active_mints={"A"})
    assert tn.gate(cl, "B", tr["B"], cfg)[0]
    # … but with A and C held the cap is used up
    cl = tn.build_clusters(tr, cfg, NOW, active_mints={"A", "C"})
    ok, why, _ = tn.gate(cl, "B", tr["B"], cfg)
    assert not ok and "cap 2" in why
    # an unrelated name is never gated
    assert tn.gate(cl, "Z", _b("DOGE"), cfg) == (True, None, None)


def test_annotate_and_strip_shapes():
    cfg = BotConfig()
    tr = {"A": _b("FLY", mc=90_000), "B": _b("FLY", smart=("manual",), mc=20_000), "C": _b("FLY")}
    cl = tn.build_clusters(tr, cfg, NOW, active_mints={"A"})
    m = {"mint": "A"}
    tn.annotate(cl, m, tr["A"])
    assert m["trend_copies"] == 3 and m["trend_is_leader"] is False and m["trend_leader"] == "B" and "1 smart" in m["trend_leader_label"]
    s = tn.strip(cl)
    assert s[0]["key"] == "fly" and s[0]["leader"]["mint"] == "B" and s[0]["held"] == ["A"] and len(s[0]["members"]) == 3
