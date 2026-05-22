"""
Withdraw SOL from the bot wallet to an external address.
"""
import logging
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer

import pumpfun  # send_versioned_tx
from wallet import get_keypair, get_pubkey
from solana_client import LAMPORTS_PER_SOL, get_sol_balance

logger = logging.getLogger("wallet_send")

FEE_BUFFER_LAMPORTS = 5_000_000  # ~0.005 SOL kept back for fees & rent


async def send_sol(to_address: str, amount_sol: float, priority_fee_microlamports: int = 200_000) -> dict:
    """Send SOL from the bot wallet to `to_address`.
    Returns { signature, lamports, to } on success; raises on validation/RPC error."""
    if amount_sol <= 0:
        raise ValueError("amount_sol must be > 0")
    try:
        to_pk = Pubkey.from_string(to_address)
    except Exception:
        raise ValueError(f"invalid recipient address: {to_address}")

    kp = get_keypair()
    from_pk = get_pubkey()
    if to_pk == from_pk:
        raise ValueError("cannot send to self")

    bal_sol = await get_sol_balance(str(from_pk))
    bal_lamports = int(bal_sol * LAMPORTS_PER_SOL)
    amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)
    if amount_lamports + FEE_BUFFER_LAMPORTS > bal_lamports:
        raise ValueError(
            f"insufficient balance: have {bal_sol:.6f} SOL, "
            f"need ~{(amount_lamports + FEE_BUFFER_LAMPORTS)/LAMPORTS_PER_SOL:.6f} SOL (incl. fee buffer)"
        )

    ix = transfer(
        TransferParams(from_pubkey=from_pk, to_pubkey=to_pk, lamports=amount_lamports)
    )
    sig = await pumpfun.send_versioned_tx(kp, [ix], priority_fee_microlamports)
    logger.info(f"Sent {amount_sol} SOL to {to_address}, sig={sig}")
    return {"signature": sig, "lamports": amount_lamports, "to": to_address}
