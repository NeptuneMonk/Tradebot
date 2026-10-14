"""Dry-run: build routed buy ixs for non-SOL-quoted coins and simulateTransaction (no funds moved)."""
import asyncio, base64, sys
sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
import multihop, jupiter, pumpfun
from helius_sender import _build_tip_transfer_ix, TIP_LAMPORTS_DUAL
from solana_client import rpc_call
from wallet import get_keypair, get_pubkey
from quote_mints import quote_book


async def sim(ixs, cu, alts, label):
    kp = get_keypair()
    bh = await rpc_call("getLatestBlockhash", [{"commitment": "confirmed"}])
    msg = MessageV0.try_compile(payer=kp.pubkey(), instructions=[set_compute_unit_limit(cu), set_compute_unit_price(100_000), *ixs, _build_tip_transfer_ix(kp.pubkey(), TIP_LAMPORTS_DUAL)],
                                address_lookup_table_accounts=list(alts), recent_blockhash=Hash.from_string(bh["result"]["value"]["blockhash"]))
    tx = VersionedTransaction(msg, [kp])
    raw = base64.b64encode(bytes(tx)).decode()
    res = await rpc_call("simulateTransaction", [raw, {"encoding": "base64", "commitment": "confirmed", "replaceRecentBlockhash": True}])
    v = (res.get("result") or {}).get("value") or {}
    print("RAW", str(res)[:400]) if not v.get("logs") else None
    print(f"[{label}] tx_bytes={len(bytes(tx))} err={v.get('err')} cu={v.get('unitsConsumed')}")
    for line in (v.get("logs") or [])[-8:]:
        print("   ", line[:160])
    return v.get("err") is None


async def main(mint: str, lamports: int):
    user = get_pubkey()
    st = await pumpfun.fetch_bonding_curve_state(mint)
    print("curve:", {k: st.get(k) for k in ("complete", "quote_mint", "quote_symbol", "is_mayhem", "virtual_sol_reserves")} if st else None)
    qm = st["quote_mint"]
    tokens, _ = pumpfun.quote_buy_tokens(st, lamports, 500) if st.get("virtual_sol_reserves") else (1, 0)
    print("expected tokens (SOL-equiv math):", tokens)
    try:
        ixs, cu = await multihop.buy_ixs(user, mint, qm, lamports, 1)
        print("multihop route built, cu", cu)
        await sim(ixs, cu, [], "multihop buy")
    except multihop.NotPumpRoutable as e:
        print("multihop: NotPumpRoutable:", e)
        ixs, cu, alts, q = await jupiter.buy_ixs(user, mint, lamports, 500)
        print("jupiter route:", " → ".join(r["swapInfo"]["label"] for r in q["routePlan"]), "out", q["outAmount"], "cu", cu, "alts", len(alts))
        await sim(ixs, cu, alts, "jupiter buy")


asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 3_000_000))
