"""Unverified PumpSwap pools (not the pump.fun graduation PDA) are refused; a pool whose SOL depth collapses below
pool_drain_exit_pct of the entry depth is a rug, not a take-profit (Sibbear / Leowin / PIERRE / ZCAT, 2026-10-19)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from solders.pubkey import Pubkey  # noqa: E402
import pumpswap  # noqa: E402
from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402

MINT = "D3fSfRDsGCRWykjB1qfpR1EtbZBP48JHx2ZZVJWab8iU"
PUMP_MINT = "6HLEB1rDqkEP37RdWen83GVfH2jhUsCVwmuwaNgbWHKV"


def _state(**cfg):
    st = BotState.__new__(BotState)
    st.config = BotConfig(**cfg)
    st.stats = {}
    st.tracking = {}
    return st


def test_canonical_pool_is_deterministic_pda():
    a = str(pumpswap.derive_canonical_pool(Pubkey.from_string(PUMP_MINT)))
    b = str(pumpswap.derive_canonical_pool(Pubkey.from_string(PUMP_MINT)))
    assert a == b and a != str(pumpswap.derive_canonical_pool(Pubkey.from_string(MINT)))


def test_default_config_requires_canonical_pool_and_has_drain_floor():
    c = BotConfig()
    assert c.pumpswap_canonical_pool_only is True
    assert c.pool_drain_exit_pct == 20.0


def test_pool_drained_detects_lp_pull():
    st = _state()
    slot = {"protocol": "pumpswap", "entry_depth_sol": 40.0, "_depth_sol": 2.5, "trade": {"symbol": "ZCAT"}}
    r = st._pool_drained(slot)
    assert r and r.startswith("pool drained: 2.50 SOL left of 40.00") and "rug" in r


def test_pool_drained_ignores_healthy_pool_and_curve_positions():
    st = _state()
    assert st._pool_drained({"protocol": "pumpswap", "entry_depth_sol": 40.0, "_depth_sol": 30.0, "trade": {}}) is None
    assert st._pool_drained({"protocol": "pumpswap", "entry_depth_sol": 40.0, "_depth_sol": 8.0, "trade": {}}) is None   # exactly 20 %
    assert st._pool_drained({"protocol": "pumpfun", "entry_depth_sol": 40.0, "_depth_sol": 1.0, "trade": {}}) is None


def test_pool_drained_falls_back_to_persisted_entry_ctx_and_can_be_off():
    st = _state()
    slot = {"protocol": "pumpswap", "_depth_sol": 1.0, "trade": {"entry_ctx": {"curve_liquidity_sol": 35.0}}}
    assert st._pool_drained(slot) is not None
    assert _state(pool_drain_exit_pct=0)._pool_drained(slot) is None
    assert st._pool_drained({"protocol": "pumpswap", "_depth_sol": 1.0, "trade": {}}) is None      # no reference depth → no call


def test_autopsy_classifies_drain_as_rug():
    from autopsy import exit_class
    assert exit_class({"exit_reason": "pool drained: 0.01 SOL left of 40.00 at entry (−100 %, LP pulled — unverified pool rug)"}) == "rug"
