"""multihop route resolution + bot router fallback (no RPC: pumpfun / pumpswap reads are monkeypatched)."""
import asyncio
import struct
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import multihop  # noqa: E402
import pumpfun  # noqa: E402
import pumpswap  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

WSOL = str(pumpswap.WSOL)
COIN = "41NRdXTgvtdUBnqR7AABprmrRLRqSFGFpb7fEGRupump"
Q_PUMP_COIN = "Bs5oWCyQBMTszdEYenKApgEwHDzx9EHinTTADZConDom"
Q_LISTED = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
USER = Pubkey.from_string("FXCMFSvRPkWkxnMLUQTDe2iFHRivUaaYWVJowLXHrcWe")
TOKEN_PROGRAM = pumpswap.TOKEN_PROGRAM


def _pool_state(pool: Pubkey, base: str, quote: str) -> dict:
    return {"base_mint": base, "quote_mint": quote, "pool_base_token_account": str(Pubkey.new_unique()),
            "pool_quote_token_account": str(Pubkey.new_unique()), "base_reserves": 10**12, "quote_reserves": 10**11}


@pytest.fixture
def chain(monkeypatch):
    """curves: mint → curve dict (None = not a pump coin); pools: canonical pool pda str → state."""
    curves: dict[str, dict | None] = {}
    pools: dict[str, dict] = {}

    async def fetch_curve(mint):
        return curves.get(mint)

    async def fetch_pool(pool):
        return pools.get(str(pool))

    async def tp(mint):
        return TOKEN_PROGRAM

    async def no_buyback(user):
        return []

    monkeypatch.setattr(pumpfun, "fetch_bonding_curve_state", fetch_curve)
    monkeypatch.setattr(pumpswap, "fetch_pool_state", fetch_pool)
    monkeypatch.setattr(pumpfun, "get_mint_token_program", tp)
    monkeypatch.setattr(multihop, "_buyback_ata_ixs", no_buyback)
    return curves, pools


def test_curve_then_pool_route(chain):
    curves, pools = chain
    curves[COIN] = {"complete": False, "quote_mint": Q_PUMP_COIN, "is_mayhem": False}
    curves[Q_PUMP_COIN] = {"complete": True, "quote_mint": None}
    pool = multihop.canonical_pool_with_quote(Pubkey.from_string(Q_PUMP_COIN), pumpswap.WSOL)
    pools[str(pool)] = _pool_state(pool, Q_PUMP_COIN, WSOL)
    route = asyncio.run(multihop.resolve_route(COIN, Q_PUMP_COIN))
    assert [h["venue"] for h in route] == ["curve", "pool"]
    assert route[0]["base"] == COIN and route[0]["quote"] == Q_PUMP_COIN
    assert route[1]["base"] == Q_PUMP_COIN and route[1]["quote"] == WSOL
    assert route[1]["addr"] == pool


def test_two_live_curves_route_no_wrap(chain):
    curves, _ = chain
    curves[COIN] = {"complete": False, "quote_mint": Q_PUMP_COIN, "is_mayhem": False}
    curves[Q_PUMP_COIN] = {"complete": False, "quote_mint": None, "is_mayhem": False}
    ixs, cu = asyncio.run(multihop.buy_ixs(USER, COIN, Q_PUMP_COIN, 5_000_000, 1))
    # SOL-side hop is a curve → native lamports, no transfer/sync_native wrap: create WSOL ATA, create coin ATA, swap, close
    assert len(ixs) == 4 and cu == 80_000 + 2 * multihop.CU_PER_HOP
    swap = ixs[2]
    assert swap.program_id == multihop.PUMPSWAP_PROGRAM_ID and bytes(swap.data)[:8] == multihop.DISC
    assert len(swap.accounts) == 16 + 5 * 2
    # hop order on a buy is SOL side first: hop 1 = Q/WSOL, hop 2 = COIN/Q
    assert swap.accounts[16].pubkey == Pubkey.from_string(Q_PUMP_COIN) and swap.accounts[17].pubkey == pumpswap.WSOL
    assert swap.accounts[21].pubkey == Pubkey.from_string(COIN) and swap.accounts[22].pubkey == Pubkey.from_string(Q_PUMP_COIN)


def test_pool_first_buy_wraps_sol(chain):
    curves, pools = chain
    curves[COIN] = {"complete": False, "quote_mint": Q_PUMP_COIN, "is_mayhem": False}
    curves[Q_PUMP_COIN] = {"complete": True, "quote_mint": None}
    pool = multihop.canonical_pool_with_quote(Pubkey.from_string(Q_PUMP_COIN), pumpswap.WSOL)
    pools[str(pool)] = _pool_state(pool, Q_PUMP_COIN, WSOL)
    ixs, _ = asyncio.run(multihop.buy_ixs(USER, COIN, Q_PUMP_COIN, 5_000_000, 1))
    assert len(ixs) == 6                       # + transfer + sync_native


def test_sell_route_coin_first(chain):
    curves, _ = chain
    curves[COIN] = {"complete": False, "quote_mint": Q_PUMP_COIN, "is_mayhem": False}
    curves[Q_PUMP_COIN] = {"complete": False, "quote_mint": None, "is_mayhem": False}
    ixs, _ = asyncio.run(multihop.sell_ixs(USER, COIN, Q_PUMP_COIN, 1_000, 777))
    swap = ixs[1]
    assert swap.accounts[16].pubkey == Pubkey.from_string(COIN)
    assert struct.unpack_from("<QQ", bytes(swap.data), 8) == (1_000, 777)


def test_listed_quote_not_pump_routable(chain):
    curves, _ = chain
    curves[COIN] = {"complete": False, "quote_mint": Q_LISTED, "is_mayhem": False}
    curves[Q_LISTED] = None                    # PUMP has no bonding curve
    with pytest.raises(multihop.NotPumpRoutable):
        asyncio.run(multihop.resolve_route(COIN, Q_LISTED))


def test_mayhem_curve_refused(chain):
    curves, _ = chain
    curves[COIN] = {"complete": False, "quote_mint": Q_PUMP_COIN, "is_mayhem": True}
    with pytest.raises(multihop.NotPumpRoutable):
        asyncio.run(multihop.resolve_route(COIN, Q_PUMP_COIN))


def test_depth_over_max_hops_falls_to_jupiter(chain):
    curves, _ = chain
    mid = str(Pubkey.new_unique())
    curves[COIN] = {"complete": False, "quote_mint": Q_PUMP_COIN, "is_mayhem": False}
    curves[Q_PUMP_COIN] = {"complete": False, "quote_mint": mid, "is_mayhem": False}
    curves[mid] = {"complete": False, "quote_mint": None, "is_mayhem": False}
    with pytest.raises(multihop.NotPumpRoutable):
        asyncio.run(multihop.resolve_route(COIN, Q_PUMP_COIN))


def test_with_min_out_rewrites_only_swap(chain):
    curves, _ = chain
    curves[COIN] = {"complete": False, "quote_mint": Q_PUMP_COIN, "is_mayhem": False}
    curves[Q_PUMP_COIN] = {"complete": False, "quote_mint": None, "is_mayhem": False}
    ixs, _ = asyncio.run(multihop.sell_ixs(USER, COIN, Q_PUMP_COIN, 1_000, 1))
    out = multihop.with_min_out(ixs, 4242)
    assert struct.unpack_from("<QQ", bytes(out[1].data), 8) == (1_000, 4242)
    assert [bytes(a.data) for a in out if a is not out[1]] == [bytes(a.data) for a in ixs if a is not ixs[1]]


def test_slip_revert_classifier():
    from bot import BotState
    assert BotState._is_slip_revert("{'Custom': 6003}", None)
    assert not BotState._is_slip_revert("{'Custom': 6004}", None)          # plain curve sell: 6004 is MintDoesNotMatch
    assert BotState._is_slip_revert("{'Custom': 6004}", "multihop")        # pump-amm ExceededSlippage on a route
    assert BotState._is_slip_revert("{'Custom': 6001}", "jupiter")
    assert not BotState._is_slip_revert("{'Custom': 6023}", "jupiter")
