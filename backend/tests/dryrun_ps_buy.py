import asyncio, sys, base64
sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
import pumpfun, pumpswap
from wallet import get_pubkey
from solana_client import rpc_call
from solders.compute_budget import set_compute_unit_limit
from solders.hash import Hash
from solders.message import MessageV0
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey

async def main(mint, lamports, slip):
    user = get_pubkey()
    pool = await pumpswap.find_pool_for_mint(mint)
    st = await pumpswap.fetch_pool_state(pool)
    tokens_out, max_sol = pumpswap.quote_buy_tokens(st, lamports, slip)
    print("pool", pool[:8], "reserves", st["base_reserves"], st["quote_reserves"], "→ base_out", tokens_out, "max_in", max_sol)
    base_tp = await pumpfun.get_mint_token_program(mint)
    ata = pumpswap.get_associated_token_address(user, Pubkey.from_string(mint), base_tp)
    wsol_acc, wsol_ixs = pumpswap.build_wsol_ata_fund_ixs(user, max_sol)
    ixs = [pumpswap.build_create_ata_ix(user, user, Pubkey.from_string(mint), base_tp), *wsol_ixs,
           pumpswap.build_buy_ix(user, st, ata, wsol_acc, base_amount_out=tokens_out, max_quote_amount_in=max_sol, base_token_program=base_tp),
           pumpswap.build_close_wsol_ix(user, wsol_acc)]
    msg = MessageV0.try_compile(payer=user, instructions=[set_compute_unit_limit(400_000), *ixs], address_lookup_table_accounts=[], recent_blockhash=Hash.default())
    tx = VersionedTransaction.populate(msg, [Signature.default()])
    res = await rpc_call("simulateTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "processed"}])
    v = res.get("result", {}).get("value", {})
    print("sim err", v.get("err"), "cu", v.get("unitsConsumed"))
    for l in (v.get("logs") or []):
        if True:
            print("  ", l[:200])

asyncio.run(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
