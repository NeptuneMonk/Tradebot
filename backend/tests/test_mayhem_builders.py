"""EMERGENT_CHANGES #6 — mayhem builders ready, entry skipped by default."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pumpfun  # noqa: E402
from models import BotConfig  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402


def test_skip_mayhem_default_on():
    assert BotConfig().skip_mayhem_coins is True


def test_buy_sell_builders_swap_fee_recipient_for_mayhem():
    user = Pubkey.new_unique(); mint = Pubkey.new_unique(); creator = Pubkey.new_unique()
    buy_n = asyncio.run(pumpfun.build_buy_ix(user, mint, 1, 1, creator, pumpfun.TOKEN_2022_PROGRAM))
    buy_m = asyncio.run(pumpfun.build_buy_ix(user, mint, 1, 1, creator, pumpfun.TOKEN_2022_PROGRAM, mayhem=True))
    assert buy_m.accounts[1].pubkey == pumpfun.MAYHEM_FEE_RECIPIENT and buy_n.accounts[1].pubkey != pumpfun.MAYHEM_FEE_RECIPIENT
    assert buy_m.accounts[8].pubkey == pumpfun.TOKEN_2022_PROGRAM and len(buy_m.accounts) == 18
    sell_m = asyncio.run(pumpfun.build_sell_ix(user, mint, 1, 1, creator, pumpfun.TOKEN_2022_PROGRAM, cashback=True, mayhem=True))
    assert sell_m.accounts[1].pubkey == pumpfun.MAYHEM_FEE_RECIPIENT and sell_m.accounts[9].pubkey == pumpfun.TOKEN_2022_PROGRAM
    assert sell_m.data == asyncio.run(pumpfun.build_sell_ix(user, mint, 1, 1, creator, pumpfun.TOKEN_2022_PROGRAM, cashback=True)).data
