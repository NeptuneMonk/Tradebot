"""Verify cashback flag detection + sell IX shape for both cashback & non-cashback."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from solders.pubkey import Pubkey
import pumpfun


async def main():
    mints = [
        ("M.I.G.A",   "4L4hou7WevgyukfR6QMRb3TGxQve3Uvzpqf11pMWpump", "EWLVbzvyEhh5m9WEsUtPoLCLxn5QonaZYqN5rL2L6Qef"),
        ("3Dk",       "3Dk65Vo2ifLtuVMFtW8M32vZXFsYxjJUNxhjEqdKpump", "3TLpUnrLomnwNkNN2m1qMZqv1hXCpXPBMr2LRCwCBmKk"),
        ("5cv",       "5cvboqr6ouEHmo7DpEyVA3HyHC3XktyfgehaCrSGpump", "Hauh1ykTy8hYEGhqQNbGoEzNobpqrTkhi6HSV2cY5BBK"),
        ("MOONBANK",  "64vtJ1QAHVZftiy3f997tfMCjRotv36HRvR1ikS9pump", "Dw5pc6uMVpChQQ7VvCeR9F34XFzPoZFe5QBmf4rvMhmm"),
    ]
    user = Pubkey.from_string("Gbp9yFREc9dPvnfSjBmi9udg3UCrMmjZh2rjaPebRPrR")
    for label, m, c in mints:
        state = await pumpfun.fetch_bonding_curve_state(m)
        if not state:
            print(f"{label}: no state")
            continue
        tp = await pumpfun.get_mint_token_program(m)
        is_cb = state.get("is_cashback", False)
        sell = await pumpfun.build_sell_ix(user, Pubkey.from_string(m), 1000000, 100000,
                                     Pubkey.from_string(c), tp, cashback=is_cb)
        print(f"{label:10s}  cashback={is_cb}  tp={'2022' if str(tp).startswith('Tokenz') else 'classic'}  sell_accounts={len(sell.accounts)}")

asyncio.run(main())
