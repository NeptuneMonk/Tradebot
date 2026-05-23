"""
Pump.fun on-chain integration.
- Program constants
- Bonding curve PDA + ATA derivation
- Buy / sell instruction builders
- Helpers to fetch bonding curve state and quote prices
"""
import os
import struct
import base64
import httpx
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

from solana_client import rpc_call, LAMPORTS_PER_SOL

PUMP_PROGRAM_ID = Pubkey.from_string(os.environ["PUMP_PROGRAM_ID"])
GLOBAL_PDA = Pubkey.from_string(os.environ["PUMP_GLOBAL"])
FEE_RECIPIENT = Pubkey.from_string(os.environ["PUMP_FEE_RECIPIENT"])
EVENT_AUTHORITY = Pubkey.from_string(os.environ["PUMP_EVENT_AUTHORITY"])

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
RENT_SYSVAR = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

BUY_DISCRIMINATOR = bytes([102, 6, 61, 18, 1, 218, 235, 234])
SELL_DISCRIMINATOR = bytes([51, 230, 133, 164, 1, 127, 131, 173])
CREATE_DISCRIMINATOR = bytes([24, 30, 200, 40, 5, 28, 7, 119])

# Pump.fun initial bonding curve constants — every newly created Pump token
# starts at exactly these virtual reserves, so the launch-baseline price
# (SOL per raw token unit) is universal and time-invariant.
INITIAL_VIRTUAL_SOL_RESERVES = 30_000_000_000  # 30 SOL in lamports
INITIAL_VIRTUAL_TOKEN_RESERVES = 1_073_000_000_000_000  # 1.073B tokens × 1e6
LAUNCH_BASELINE_PRICE_SOL = (
    INITIAL_VIRTUAL_SOL_RESERVES / INITIAL_VIRTUAL_TOKEN_RESERVES / LAMPORTS_PER_SOL
)  # ≈ 2.796e-14 SOL per raw token unit


def derive_bonding_curve(mint: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint)], PUMP_PROGRAM_ID
    )
    return pda


def derive_associated_token(owner: Pubkey, mint: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )
    return pda


async def fetch_bonding_curve_state(mint_str: str) -> dict | None:
    """
    Fetch the bonding curve account data.
    Layout (Anchor):
      8  discriminator
      8  virtual_token_reserves (u64)
      8  virtual_sol_reserves (u64)
      8  real_token_reserves (u64)
      8  real_sol_reserves (u64)
      8  token_total_supply (u64)
      1  complete (bool)
    """
    mint = Pubkey.from_string(mint_str)
    bc = derive_bonding_curve(mint)
    res = await rpc_call(
        "getAccountInfo",
        [str(bc), {"encoding": "base64", "commitment": "confirmed"}],
    )
    val = res.get("result", {}).get("value")
    if not val:
        return None
    data_b64 = val["data"][0]
    raw = base64.b64decode(data_b64)
    if len(raw) < 49:
        return None
    vtr, vsr, rtr, rsr, tts = struct.unpack("<QQQQQ", raw[8:48])
    complete = raw[48] != 0
    return {
        "bonding_curve": str(bc),
        "virtual_token_reserves": vtr,
        "virtual_sol_reserves": vsr,
        "real_token_reserves": rtr,
        "real_sol_reserves": rsr,
        "token_total_supply": tts,
        "complete": complete,
    }


def quote_buy_tokens(state: dict, sol_in_lamports: int, slippage_bps: int = 500) -> tuple[int, int]:
    """
    Constant product AMM math (virtual reserves model used by Pump.fun).
    Returns: (expected_tokens_out, max_sol_cost_with_slippage)
    """
    vsr = state["virtual_sol_reserves"]
    vtr = state["virtual_token_reserves"]
    # tokens_out = vtr - (vsr * vtr) / (vsr + sol_in)
    new_vsr = vsr + sol_in_lamports
    new_vtr = (vsr * vtr) // new_vsr
    tokens_out = vtr - new_vtr
    max_sol = sol_in_lamports + (sol_in_lamports * slippage_bps) // 10_000
    return tokens_out, max_sol


def quote_sell_sol(state: dict, tokens_in: int, slippage_bps: int = 500) -> tuple[int, int]:
    vsr = state["virtual_sol_reserves"]
    vtr = state["virtual_token_reserves"]
    new_vtr = vtr + tokens_in
    new_vsr = (vsr * vtr) // new_vtr
    sol_out = vsr - new_vsr
    min_sol = sol_out - (sol_out * slippage_bps) // 10_000
    return sol_out, max(0, min_sol)


def build_create_ata_ix(payer: Pubkey, owner: Pubkey, mint: Pubkey) -> Instruction:
    ata = derive_associated_token(owner, mint)
    return Instruction(
        program_id=ASSOCIATED_TOKEN_PROGRAM,
        data=bytes([0]),  # Create
        accounts=[
            AccountMeta(payer, True, True),
            AccountMeta(ata, False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(TOKEN_PROGRAM, False, False),
        ],
    )


def build_buy_ix(
    user: Pubkey,
    mint: Pubkey,
    amount_tokens: int,
    max_sol_cost_lamports: int,
) -> Instruction:
    bonding_curve = derive_bonding_curve(mint)
    associated_bonding_curve = derive_associated_token(bonding_curve, mint)
    associated_user = derive_associated_token(user, mint)
    data = BUY_DISCRIMINATOR + struct.pack("<QQ", amount_tokens, max_sol_cost_lamports)
    return Instruction(
        program_id=PUMP_PROGRAM_ID,
        data=data,
        accounts=[
            AccountMeta(GLOBAL_PDA, False, False),
            AccountMeta(FEE_RECIPIENT, False, True),
            AccountMeta(mint, False, False),
            AccountMeta(bonding_curve, False, True),
            AccountMeta(associated_bonding_curve, False, True),
            AccountMeta(associated_user, False, True),
            AccountMeta(user, True, True),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(TOKEN_PROGRAM, False, False),
            AccountMeta(RENT_SYSVAR, False, False),
            AccountMeta(EVENT_AUTHORITY, False, False),
            AccountMeta(PUMP_PROGRAM_ID, False, False),
        ],
    )


def build_sell_ix(
    user: Pubkey,
    mint: Pubkey,
    amount_tokens: int,
    min_sol_out_lamports: int,
) -> Instruction:
    bonding_curve = derive_bonding_curve(mint)
    associated_bonding_curve = derive_associated_token(bonding_curve, mint)
    associated_user = derive_associated_token(user, mint)
    data = SELL_DISCRIMINATOR + struct.pack("<QQ", amount_tokens, min_sol_out_lamports)
    return Instruction(
        program_id=PUMP_PROGRAM_ID,
        data=data,
        accounts=[
            AccountMeta(GLOBAL_PDA, False, False),
            AccountMeta(FEE_RECIPIENT, False, True),
            AccountMeta(mint, False, False),
            AccountMeta(bonding_curve, False, True),
            AccountMeta(associated_bonding_curve, False, True),
            AccountMeta(associated_user, False, True),
            AccountMeta(user, True, True),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(ASSOCIATED_TOKEN_PROGRAM, False, False),
            AccountMeta(TOKEN_PROGRAM, False, False),
            AccountMeta(EVENT_AUTHORITY, False, False),
            AccountMeta(PUMP_PROGRAM_ID, False, False),
        ],
    )


async def send_versioned_tx(
    keypair, instructions: list, priority_fee_microlamports: int = 500_000
) -> str:
    """Build, sign, and send a versioned transaction. Returns signature string."""
    # Prepend compute budget instructions
    ixs = [
        set_compute_unit_limit(200_000),
        set_compute_unit_price(priority_fee_microlamports),
        *instructions,
    ]
    bh_res = await rpc_call("getLatestBlockhash", [{"commitment": "finalized"}])
    blockhash_str = bh_res["result"]["value"]["blockhash"]
    from solders.hash import Hash
    blockhash = Hash.from_string(blockhash_str)
    msg = MessageV0.try_compile(
        payer=keypair.pubkey(),
        instructions=ixs,
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    tx = VersionedTransaction(msg, [keypair])
    raw = bytes(tx)
    sig_res = await rpc_call(
        "sendTransaction",
        [
            base64.b64encode(raw).decode("utf-8"),
            {"encoding": "base64", "skipPreflight": True, "maxRetries": 3},
        ],
    )
    if "error" in sig_res:
        raise RuntimeError(f"sendTransaction error: {sig_res['error']}")
    return sig_res["result"]
