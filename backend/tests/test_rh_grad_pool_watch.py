"""RH graduated (seasoned) entries: pool Swap logs must be watched for graduated tokens we do NOT hold yet,
otherwise `pool_live` never flips and the gate is stuck on 'rh-grad-no-pool'."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rh_dex
import rh_discovery as rh
from tests.test_rh_paper import make_state, hot_bucket, TOKEN


def _graduate(b, now, age_s=120):
    b["graduated"] = True
    b["graduated_at"] = now - age_s
    b["curve_fill_pct"] = 100.0


def test_pool_watch_includes_unheld_fresh_graduates_and_caps():
    st = make_state()
    now = time.time()
    b = hot_bucket(st.rh_discovery, now)
    _graduate(b, now)
    assert st.rh_discovery._pool_watch_tokens() == [TOKEN]          # held nothing, still watched
    # too old for the seasoned window → dropped from the watch list
    b["graduated_at"] = now - (st.config.rh_seasoned_max_age_min * 60 + 600)
    assert st.rh_discovery._pool_watch_tokens() == []
    # cap: newest graduations first
    for i in range(rh.POOL_WATCH_MAX + 5):
        t = f"0x{i + 1:040x}"
        bb = st.rh_discovery._new_bucket({"token": t, "curve": "0x" + "c" * 40, "deployer": "0x" + "d" * 40,
                                          "pair_token": "0x" + "0" * 40, "quote_symbol": "ETH", "quote_decimals": 18,
                                          "graduation_threshold": 4.2, "block": 1}, start=now - 100)
        bb["graduated"], bb["graduated_at"] = True, now - i
        st.rh_discovery.tracking[t] = bb
    watched = st.rh_discovery._pool_watch_tokens()
    assert len(watched) == rh.POOL_WATCH_MAX and watched[0] == f"0x{1:040x}"


def test_gate_moves_past_no_pool_once_a_swap_is_ingested():
    st = make_state()
    disc, paper = st.rh_discovery, st.rh_paper
    now = time.time()
    b = hot_bucket(disc, now, buyers=25, mc=20_000.0)
    _graduate(b, now)
    assert paper._gates(TOKEN, b, now) == "rh-grad-no-pool"
    pid = "0x" + rh_dex.pool_id(TOKEN, b["pair_token"]).hex()
    swap_log = {"address": rh_dex.POOL_MANAGER, "topics": [rh_dex.T_SWAP, pid, "0x" + "a" * 64],
                "data": "0x" + "00" * 32 * 6, "blockNumber": "0x10", "logIndex": "0x0"}
    fake_tr = {"side": "buy", "wallet": "0x" + "b" * 40, "quote": 0.05, "tokens": 1e6, "price": 2e-9, "block": 16}
    orig = rh_dex.decode_swap
    rh_dex.decode_swap = lambda *a, **k: fake_tr
    try:
        disc._ingest_pool_swaps([swap_log], now)
    finally:
        rh_dex.decode_swap = orig
    assert b["pool_live"] is True and b["last_pool_swap_ts"] == now
    assert paper._gates(TOKEN, b, now) != "rh-grad-no-pool"
