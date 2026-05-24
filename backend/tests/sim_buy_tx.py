"""
Simulate a Pump.fun BUY tx (no SOL spent) to verify the new IX layout
post-2026-04-28 upgrade. Uses simulateTransaction RPC.
"""
import asyncio
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

import pumpfun
from wallet import get_keypair
from solana_client import rpc_call, LAMPORTS_PER_SOL


async def main(mint_str: str, creator_str: str):
    kp = get_keypair()
    user = kp.pubkey()
    mint = Pubkey.from_string(mint_str)
    creator = Pubkey.from_string(creator_str)

    state = await pumpfun.fetch_bonding_curve_state(mint_str)
    if not state:
        print("ERROR: no bonding curve state — token may have graduated")
        return
    if state["complete"]:
        print("ERROR: curve complete (graduated)")
        return
    print(f"Bonding curve state ok: vsr={state['virtual_sol_reserves']}, vtr={state['virtual_token_reserves']}, complete={state['complete']}")
    curve_creator = state.get("creator")
    print(f"Curve creator: {curve_creator}  (CLI creator was: {creator_str})")
    if curve_creator and curve_creator != creator_str:
        creator = Pubkey.from_string(curve_creator)
        print("  -> using curve creator (overrides CLI value)")

    tp = await pumpfun.get_mint_token_program(mint_str)
    print(f"Token program: {tp}")
    is_cb = state.get("is_cashback", False)
    print(f"Cashback enabled: {is_cb}")

    # Quote a $0.50-ish buy
    sol_in = int(0.005 * LAMPORTS_PER_SOL)  # ~$0.50 at $100/SOL
    tokens_out, max_sol = pumpfun.quote_buy_tokens(state, sol_in, 500)
    print(f"Quote: {tokens_out} tokens for max {max_sol} lamports")

    buy_ix = await pumpfun.build_buy_ix(user, mint, tokens_out, max_sol, creator, tp)
    ixs = [
        set_compute_unit_limit(200_000),
        set_compute_unit_price(500_000),
        pumpfun.build_create_ata_ix(user, user, mint, tp),
        buy_ix,
    ]

    bh = await rpc_call("getLatestBlockhash", [{"commitment": "finalized"}])
    blockhash_str = bh["result"]["value"]["blockhash"]
    from solders.hash import Hash
    msg = MessageV0.try_compile(
        payer=user,
        instructions=ixs,
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.from_string(blockhash_str),
    )
    tx = VersionedTransaction(msg, [kp])
    raw_b64 = base64.b64encode(bytes(tx)).decode()

    sim = await rpc_call(
        "simulateTransaction",
        [raw_b64, {"encoding": "base64", "commitment": "confirmed", "sigVerify": False, "replaceRecentBlockhash": True}],
    )
    res = sim.get("result", {}).get("value", {}) if "result" in sim else {}
    err = res.get("err")
    logs = res.get("logs", []) or []
    units_consumed = res.get("unitsConsumed")
    print(f"\n=== SIMULATION RESULT ===")
    print(f"  err = {err}")
    print(f"  unitsConsumed = {units_consumed}")
    print(f"  logs ({len(logs)}):")
    for line in logs:
        print(f"    {line}")
    if err is None:
        print("\n✅ BUY TX WOULD LAND ON CHAIN — fix verified.")
    else:
        print(f"\n❌ STILL FAILING: {err}")


if __name__ == "__main__":
    mint = sys.argv[1] if len(sys.argv) > 1 else "4L4hou7WevgyukfR6QMRb3TGxQve3Uvzpqf11pMWpump"
    creator = sys.argv[2] if len(sys.argv) > 2 else "EWLVbzvyEhh5m9WEsUtPoLCLxn5QonaZYqN5rL2L6Qef"
    asyncio.run(main(mint, creator))
