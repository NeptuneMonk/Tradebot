import asyncio, sys
sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
import pumpfun, pumpswap
from wallet import get_pubkey
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.message import MessageV0
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey
from helius_sender import _build_tip_transfer_ix, TIP_LAMPORTS_DUAL


async def main(mint):
    user = get_pubkey()
    pool = await pumpswap.find_pool_for_mint(mint)
    st = await pumpswap.fetch_pool_state(pool)
    print("pool", pool, "quote_is_sol", st.get("quote_is_sol"), "quote", st.get("quote_mint"))
    base_tp = await pumpfun.get_mint_token_program(mint)
    ata = pumpswap.get_associated_token_address(user, Pubkey.from_string(mint), base_tp)
    wsol_acc, wsol_ixs = pumpswap.build_wsol_ata_fund_ixs(user, 9_770_000)
    ixs = [pumpswap.build_create_ata_ix(user, user, Pubkey.from_string(mint), base_tp), *wsol_ixs,
           pumpswap.build_buy_ix(user, st, ata, wsol_acc, base_amount_out=1, max_quote_amount_in=9_770_000, base_token_program=base_tp),
           pumpswap.build_close_wsol_ix(user, wsol_acc)]
    for i, ix in enumerate(ixs):
        print(i, str(ix.program_id)[:10], "accounts", len(ix.accounts), "data", len(bytes(ix.data)))
    full = [set_compute_unit_limit(400_000), set_compute_unit_price(100_000), *ixs, _build_tip_transfer_ix(user, TIP_LAMPORTS_DUAL)]
    msg = MessageV0.try_compile(payer=user, instructions=full, address_lookup_table_accounts=[], recent_blockhash=Hash.default())
    tx = VersionedTransaction.populate(msg, [Signature.default()])
    print("tx bytes", len(bytes(tx)), "unique accounts", len(msg.account_keys))

asyncio.run(main(sys.argv[1]))
