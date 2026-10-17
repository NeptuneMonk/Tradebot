"""EMERGENT_CHANGES #11 — RH sell: fee comes off the quote side in min-out, and NO min-out-0 fallback."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
os.environ["RH_WALLET_PATH"] = os.path.join(tempfile.mkdtemp(), "rh_wallet.json")
os.environ["RH_WALLET_PASS_PATH"] = os.path.join(os.path.dirname(os.environ["RH_WALLET_PATH"]), "rh_wallet.pass")
import rh_wallet  # noqa: E402
import rh_live  # noqa: E402
from models import BotConfig  # noqa: E402

WEI = 10 ** 18


def test_min_out_subtracts_fee_then_slippage():
    # tokens × price = 1.0 ETH, slip 8 %, fee 1 % → 0.9108 ETH
    assert rh_live.sell_min_out(WEI, 8.0, 1.0) == int(WEI * 0.99 * 0.92)
    assert abs(rh_live.sell_min_out(WEI, 8.0, 1.0) / WEI - 0.9108) < 1e-9


def test_defaults_and_clamps():
    cfg = BotConfig()
    assert cfg.rh_sell_fallback_slippage_pct == 20.0 and cfg.rh_protocol_fee_pct == 1.0


def test_sim_revert_retries_once_at_capped_slippage_never_zero(monkeypatch):
    sent_min_outs: list[int] = []
    sims = {"n": 0}

    def fake_calldata(sig, types, args):
        sent_min_outs.append(int(args[1]))
        return f"0x{len(sent_min_outs):02x}"

    async def fake_simulate(to, data, value=0, sender=None):
        sims["n"] += 1
        if data == "0x01":
            raise rh_wallet.RhRpcError("execution reverted: slippage")
        if data == "0x02":          # read-only re-quote on the curve → 0x (no output) so the feed-price path is used
            return "0x"
        return "0x" + "00" * 32

    async def fake_send(to, data, value=0, gas_limit=None):
        return "0xtx"

    async def fake_receipt(tx):
        return {"ok": True, "gas_cost_wei": 1, "blockNumber": "0x10", "logs": []}

    monkeypatch.setattr(rh_wallet, "calldata", fake_calldata)
    monkeypatch.setattr(rh_wallet, "simulate", fake_simulate)
    monkeypatch.setattr(rh_wallet, "send", fake_send)
    monkeypatch.setattr(rh_wallet, "wait_receipt", fake_receipt)
    cfg = BotConfig(rh_sell_fallback_slippage_pct=20.0, rh_protocol_fee_pct=1.0)
    fill = asyncio.run(rh_live.sell("0x" + "ab" * 20, WEI, 1.0, 8.0, cfg=cfg))
    assert fill["tx"] == "0xtx"
    # calldata #1: first attempt (8 %) · #2: read-only quote (floor 0, never sent) · #3: the ONE retry at 20 %
    assert sent_min_outs[0] == int(WEI * 0.99 * 0.92)
    assert sent_min_outs[2] == int(WEI * 0.99 * 0.80) and sent_min_outs[2] > 0
    assert abs(sent_min_outs[2] / WEI - 0.792) < 1e-9
    assert all(m > 0 for i, m in enumerate(sent_min_outs) if i != 1)


def test_sim_revert_uses_curve_requote_when_available(monkeypatch):
    sent: list[int] = []

    def fake_calldata(sig, types, args):
        sent.append(int(args[1]))
        return f"0x{len(sent):02x}"

    async def fake_simulate(to, data, value=0, sender=None):
        if data == "0x01":
            raise rh_wallet.RhRpcError("revert")
        if data == "0x02":
            return "0x" + hex(WEI // 2)[2:].rjust(64, "0")     # curve says 0.5 ETH out now
        return "0x"

    async def fake_send(to, data, value=0, gas_limit=None):
        return "0xtx"

    async def fake_receipt(tx):
        return {"ok": True, "gas_cost_wei": 1, "blockNumber": "0x10", "logs": []}

    monkeypatch.setattr(rh_wallet, "calldata", fake_calldata)
    monkeypatch.setattr(rh_wallet, "simulate", fake_simulate)
    monkeypatch.setattr(rh_wallet, "send", fake_send)
    monkeypatch.setattr(rh_wallet, "wait_receipt", fake_receipt)
    asyncio.run(rh_live.sell("0x" + "ab" * 20, WEI, 1.0, 8.0, cfg=BotConfig()))
    assert sent[2] == int((WEI // 2) * 0.80)       # exact post-fee requote × (1 − 20 %)


def test_no_zero_min_out_in_source():
    src = open(os.path.join(os.path.dirname(__file__), "..", "rh_live.py")).read()
    assert "minOut=0" not in src and ", 0, me]" not in src
