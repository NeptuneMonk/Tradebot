"""Live PumpSwap SELL simulation — proves the 6053 fix without spending SOL.

Builds the exact recovery-sale instruction the bot would submit, then runs
`simulateTransaction` against Helius. A `err: None` result means the program
accepts our breaking-fee accounts, WSOL ATA shape, and account ordering —
i.e. the BuybackFeeRecipientNotAuthorized (0x17a5 / 6053) regression that
broke recovery sales is fixed.

Run:
    cd /app/backend && python3 tests/sim_pumpswap_sell.py [MINT]

If no MINT is given, pulls the top-value graduated mint from /wallet/token-scan.
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys

from dotenv import load_dotenv

# Path setup (so `import pumpfun` works when run from anywhere)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
load_dotenv(os.path.join(os.path.dirname(HERE), ".env"))

from solders.pubkey import Pubkey  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402
from solders.hash import Hash  # noqa: E402

import pumpfun  # noqa: E402
import pumpswap as _ps  # noqa: E402
from wallet import get_keypair, get_pubkey  # noqa: E402
from solana_client import rpc_call  # noqa: E402


async def pick_top_graduated() -> tuple[str, int]:
    """Pick the highest-value graduated mint by scanning wallet balances.
    Returns (mint, amount_raw)."""
    wallet = str(get_pubkey())
    candidates: list[tuple[str, int]] = []
    for prog in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                 "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        r = await rpc_call(
            "getTokenAccountsByOwner",
            [wallet, {"programId": prog},
             {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        for acc in (r.get("result") or {}).get("value") or []:
            info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            ta = info.get("tokenAmount") or {}
            amt = int(ta.get("amount") or 0)
            ui = float(ta.get("uiAmount") or 0)
            mint = info.get("mint")
            if mint and amt > 0 and ui > 0:
                candidates.append((mint, amt))
    # Probe each for graduation + pool
    best: tuple[float, str, int] | None = None
    for mint, amt in candidates[:80]:  # cap probe count
        try:
            state = await pumpfun.fetch_bonding_curve_state(mint)
            graduated = (state is None) or bool(state.get("complete"))
            if not graduated:
                continue
            pool = await _ps.find_pool_for_mint(mint)
            if not pool:
                continue
            pool_state = await _ps.fetch_pool_state(pool)
            if not pool_state:
                continue
            sol_out, _ = _ps.quote_sell_sol(pool_state, amt, 0)
            if best is None or sol_out > best[0]:
                best = (sol_out, mint, amt)
        except Exception:
            continue
    if not best:
        raise RuntimeError("no graduated mints found in wallet")
    return best[1], best[2]


async def main():
    mint = sys.argv[1] if len(sys.argv) > 1 else None
    if not mint:
        mint, amt_raw = await pick_top_graduated()
        print(f"[auto] using top graduated mint: {mint}")
    else:
        # Fetch balance for the user-supplied mint
        user = get_pubkey()
        mint_pk = Pubkey.from_string(mint)
        tp = await pumpfun.get_mint_token_program(mint)
        ata = pumpfun.derive_associated_token_for_program(user, mint_pk, tp)
        amt_raw = await _ps.get_token_balance(ata)
        if amt_raw == 0:
            ata = _ps.get_associated_token_address(user, mint_pk, _ps.TOKEN_PROGRAM)
            amt_raw = await _ps.get_token_balance(ata)
        if amt_raw == 0:
            print(f"FAIL: wallet holds 0 of {mint}")
            sys.exit(2)

    kp = get_keypair()
    user = get_pubkey()
    mint_pk = Pubkey.from_string(mint)

    # Build exact recovery sale instructions (matches server.py:recover_stuck_trade
    # / wallet_recover_mints — PumpSwap branch).
    tp = await pumpfun.get_mint_token_program(mint)
    pool = await _ps.find_pool_for_mint(mint)
    assert pool, f"no PumpSwap pool for {mint}"
    pool_state = await _ps.fetch_pool_state(pool)
    assert pool_state, f"pool state unavailable for {pool}"

    # Discover which ATA the wallet actually holds the token in
    ata = pumpfun.derive_associated_token_for_program(user, mint_pk, tp)
    bal = await _ps.get_token_balance(ata)
    if bal == 0:
        alt = _ps.get_associated_token_address(user, mint_pk, _ps.TOKEN_PROGRAM)
        if str(alt) != str(ata):
            bal2 = await _ps.get_token_balance(alt)
            if bal2 > 0:
                ata = alt
                tp = _ps.TOKEN_PROGRAM
                bal = bal2

    sell_amount = max(int(bal * 0.995), 1)
    sol_out_q, min_sol = _ps.quote_sell_sol(pool_state, sell_amount, 3000)
    wsol_ata, wsol_ixs = _ps.build_wsol_ata_idempotent_ixs(user)
    ixs = [
        _ps.build_create_ata_ix(user, user, mint_pk, tp),
        *wsol_ixs,
        _ps.build_sell_ix(
            user, pool_state, ata, wsol_ata,
            base_amount_in=sell_amount,
            min_quote_amount_out=min_sol,
            base_token_program=tp,
        ),
        # Close WSOL ATA → unwrap to native SOL in the same atomic tx
        _ps.build_close_wsol_ix(user, wsol_ata),
    ]

    print(f"mint:         {mint}")
    print(f"pool:         {pool}")
    print(f"balance:      {bal}  (selling {sell_amount} = 99.5%)")
    print(f"min_sol_out:  {min_sol / 1e9:.6f} SOL (30% slip floor)")
    print(f"quote_sol:    {sol_out_q / 1e9:.6f} SOL")
    print(f"token_prog:   {tp}")
    print(f"wsol_ata:     {wsol_ata}")
    print(f"sell_ix_acct: {len(ixs[-1].accounts)} accounts")
    print(f"cashback:     {pool_state.get('is_cashback')}")

    # Build a v0 transaction with a dummy blockhash for simulation
    bh_res = await rpc_call("getLatestBlockhash", [{"commitment": "confirmed"}])
    bh = bh_res["result"]["value"]["blockhash"]
    msg = MessageV0.try_compile(
        payer=user,
        instructions=ixs,
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.from_string(bh),
    )
    tx = VersionedTransaction(msg, [kp])
    tx_b64 = base64.b64encode(bytes(tx)).decode()

    sim = await rpc_call(
        "simulateTransaction",
        [tx_b64, {"sigVerify": False, "commitment": "confirmed",
                  "encoding": "base64", "replaceRecentBlockhash": True,
                  "accounts": {"encoding": "base64",
                               "addresses": [str(user), str(wsol_ata)]}}],
    )
    result = sim.get("result", {}).get("value", {})
    err = result.get("err")
    logs = result.get("logs") or []
    units = result.get("unitsConsumed")
    accts = result.get("accounts") or []

    print(f"\n=== SIMULATE TRANSACTION RESULT ===")
    print(f"err:           {err}")
    print(f"unitsConsumed: {units}")
    # Decode post-sim lamports to prove wallet receives native SOL
    if len(accts) >= 1 and accts[0]:
        post_user_lamports = int(accts[0].get("lamports") or 0)
        print(f"wallet (user) post-sim lamports: {post_user_lamports}  ({post_user_lamports/1e9:.6f} SOL)")
    if len(accts) >= 2:
        wsol_post = accts[1]
        if wsol_post is None:
            print(f"wsol ATA post-sim: CLOSED ✅ (proceeds unwrapped to wallet)")
        else:
            print(f"wsol ATA post-sim: still open with {wsol_post.get('lamports')} lamports ❌")
    print(f"logs ({len(logs)} lines):")
    for ln in logs[-20:]:
        print(f"  {ln}")

    if err is None:
        print("\n✅ PASS: PumpSwap sell IX simulates clean — 6053 fix verified.")
        sys.exit(0)
    else:
        # Map common pump-swap custom errors
        err_map = {
            6002: "slippage too tight",
            6022: "SellZeroAmount",
            6023: "NotEnoughTokensToSell / Overflow",
            6053: "BuybackFeeRecipientNotAuthorized (THE BUG WE'RE FIXING)",
        }
        if isinstance(err, dict) and "InstructionError" in err:
            ie = err["InstructionError"]
            if len(ie) == 2 and isinstance(ie[1], dict) and "Custom" in ie[1]:
                code = ie[1]["Custom"]
                print(f"\n❌ FAIL: Custom error {code} — {err_map.get(code, 'unknown')}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
