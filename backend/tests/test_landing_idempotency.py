"""EMERGENT_CHANGES #7 — one blockhash per land_tx, buy ladder rungs configurable, every rung sig tracked."""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import landing  # noqa: E402
from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402


def test_buy_ladder_rungs_from_config():
    st = BotState.__new__(BotState)
    st.config = BotConfig(buy_fee_ladder_rungs=1)
    assert st._buy_fee_ladder(100_000, None, 135_000) == [100_000]
    st.config.buy_fee_ladder_rungs = 3
    assert st._buy_fee_ladder(100_000, None, 135_000) == [100_000, 200_000, 400_000]
    assert BotConfig().buy_fee_ladder_rungs == 1 and BotConfig().land_late_watch_s == 45


def test_land_tx_fetches_one_blockhash_for_all_rungs(monkeypatch):
    calls = {"bh": 0, "signed_bh": []}

    async def fake_rpc(method, params):
        if method == "getLatestBlockhash":
            calls["bh"] += 1
            return {"result": {"value": {"blockhash": "11111111111111111111111111111111"}}}
        if method == "getSignatureStatuses":
            sigs = params[0]
            # both rungs confirm once two have been sent
            return {"result": {"value": [{"confirmationStatus": "confirmed", "err": None} if len(sigs) >= 2 else None for _ in sigs]}}
        if method == "getTransaction":
            return {"result": {"meta": {"err": None}}}
        return {"result": None}

    async def fake_sign(keypair, ixs, priority, cu_limit, sender_mode, alts=None, blockhash=None):
        calls["signed_bh"].append(blockhash)
        return f"SIG{priority}", "raw"

    async def fake_broadcast(raw, mode):
        return None
    monkeypatch.setattr(landing, "rpc_call", fake_rpc)
    monkeypatch.setattr(landing, "_BH_CACHE", None)
    monkeypatch.setattr(landing, "_sign", fake_sign)
    monkeypatch.setattr(landing, "_broadcast", fake_broadcast)

    async def fake_verify(sig):
        return None
    monkeypatch.setattr(landing, "_verify_meta", fake_verify)
    sig, prio = asyncio.run(landing.land_tx(SimpleNamespace(pubkey=lambda: "P"), [], priority_ladder=[1000, 2000], compute_unit_limit=80_000,
                                            rung_s=0.0, budget_s=5.0, use_sender=False, label="t"))
    assert calls["bh"] == 1 and len(calls["signed_bh"]) == 2 and len(set(calls["signed_bh"])) == 1
    assert sorted(landing.stats_for(sig)["sigs"]) == ["SIG1000", "SIG2000"]      # every rung sig is tracked


def test_stamp_sigs_into_trade_and_sink():
    st = BotState.__new__(BotState)
    st.config = BotConfig()
    st.active_trades = {"m": {"trade": {"id": "t"}}}
    st._stamp_sigs("m", ["A", "B", "A"])
    assert st.active_trades["m"]["trade"]["all_sigs"] == ["A", "B"]
    st._stamp_sigs("new", ["C"])
    assert st._take_sigs("new") == ["C"] and st._take_sigs("new") == []
