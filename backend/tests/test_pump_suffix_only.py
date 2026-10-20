"""Only pump.fun-minted coins (…pump) on automatic Solana paths; manual / force buys unaffected."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import asyncio  # noqa: E402
from bot import BotState, MANUAL_ENTRY_ACTIONS  # noqa: E402
from models import BotConfig  # noqa: E402


def _st(**cfg):
    st = BotState.__new__(BotState)
    st.config = BotConfig(**cfg)
    st.stopping_gracefully = False
    st.refusals = []
    st.tracking = {}
    st._manual_force = set()

    async def fence(_):
        return True

    async def refuse(mint, band, reason, detail):
        st.refusals.append(reason)
    st.leader_fence = fence
    st._refuse = refuse
    st.search_regime_block = lambda: None
    st._band_of = lambda m: "new"
    return st


class _L:
    def __init__(self, mint):
        self.mint = mint
        self.symbol = "X"


def _run(st, mint, action):
    # drive _enter until it either refuses or passes our gate (the next gate — helius pause — ends the call in tests)
    import helius_gate
    helius_gate.set_auto_paused(True, "test")
    try:
        asyncio.run(st._enter(_L(mint), 0, action))
    finally:
        helius_gate.set_auto_paused(False)


def test_non_pump_mint_refused_on_automatic_paths():
    st = _st()
    _run(st, "H7HWTpq5kQnkHHQkCBg5PCG9tj8aKAkwTSBDUUQFmoon", "momentum_new")
    assert st.refusals == ["not-pump-mint"]
    st = _st()
    _run(st, "H8oCUhxdDHZmMo3vUE3vcwVLh7ovtnDxZdGnbPCwXV8N", "scanner_momentum")
    assert st.refusals == ["not-pump-mint"]


def test_pump_mint_passes_and_manual_bypasses():
    st = _st()
    _run(st, "BU9hz1JXJZCjJwMdBPggxdEF1FEsJzyHiRwAkU8qpump", "momentum_new")
    assert "not-pump-mint" not in st.refusals
    st = _st()
    _run(st, "H7HWTpq5kQnkHHQkCBg5PCG9tj8aKAkwTSBDUUQFmoon", next(iter(MANUAL_ENTRY_ACTIONS)))
    assert "not-pump-mint" not in st.refusals


def test_switch_off_allows_any_mint():
    st = _st(pump_suffix_only=False)
    _run(st, "H7HWTpq5kQnkHHQkCBg5PCG9tj8aKAkwTSBDUUQFmoon", "momentum_new")
    assert "not-pump-mint" not in st.refusals
