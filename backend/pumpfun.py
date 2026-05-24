"""
Pump.fun on-chain integration — aligned with the 2026-04-28 program upgrade.

Key changes from the legacy layout (which was silently failing every tx with
`IncorrectProgramId`):
  - Buy IX:  18 accounts (was 12). Adds creator_vault, global/user volume
             accumulators, fee_config, fee_program, bonding_curve_v2, and a
             writable breaking_fee_recipient (picked from 8 fixed pubkeys).
  - Sell IX: 15 accounts (was 12). Same new accounts (no volume accumulators
             unless the coin is a cashback coin — we skip cashback for now).
  - Instruction args: appended OptionBool track_volume = Some(true) → 2 bytes
                      `[1, 1]` at the tail of buy/sell data.

Reference: github.com/chainstacklabs/pumpfun-bonkfun-bot (2026-04-27 commit
22a0c23, the maintained reference that aligns with the live program).
"""
import os
import random
import struct
import base64
import httpx
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

from solana_client import rpc_call, LAMPORTS_PER_SOL

# ---------- Program constants ----------
PUMP_PROGRAM_ID = Pubkey.from_string(os.environ["PUMP_PROGRAM_ID"])
GLOBAL_PDA = Pubkey.from_string(os.environ["PUMP_GLOBAL"])
FEE_RECIPIENT = Pubkey.from_string(os.environ["PUMP_FEE_RECIPIENT"])
EVENT_AUTHORITY = Pubkey.from_string(os.environ["PUMP_EVENT_AUTHORITY"])

# Mayhem-mode fee recipient: used for Token-2022 ("mayhem") coins introduced
# with the 2026-04-28 upgrade. Hardcoded per chainstack reference; cross-check
# at Global account offset 483 if Pump.fun rotates it.
MAYHEM_FEE_RECIPIENT = Pubkey.from_string(
    "GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS"
)

# 2026-04-28 upgrade: fee handling now routes through a separate program +
# config PDA. These are fixed across all coins.
FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

# 2026-04-28 upgrade: each buy/sell must append ONE of these 8 writable
# "breaking-upgrade fee recipients". Pump.fun's docs recommend picking at
# random per tx to spread load. (BREAKING_FEE_RECIPIENT.md)
BREAKING_FEE_RECIPIENTS = [
    Pubkey.from_string("5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"),
    Pubkey.from_string("9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7"),
    Pubkey.from_string("GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL"),
    Pubkey.from_string("3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR"),
    Pubkey.from_string("5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6"),
    Pubkey.from_string("EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL"),
    Pubkey.from_string("5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"),
    Pubkey.from_string("A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW"),
]


def _pick_breaking_fee_recipient() -> Pubkey:
    return random.choice(BREAKING_FEE_RECIPIENTS)


# Authorized fee_recipients for account[1] in buy/sell ixs (post-2026-04-28).
# These were extracted by scanning recent SUCCESSFUL on-chain Pump.fun trades.
# The Global account also contains them but at non-uniform offsets mixed with
# unrelated fields, so an offset-based scan can pick up an invalid pubkey and
# trip NotAuthorized (6000). Using the observed-good list is the safe path.
AUTHORIZED_FEE_RECIPIENTS = [
    Pubkey.from_string("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV"),  # legacy
    Pubkey.from_string("7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ"),
    Pubkey.from_string("9rPYyANsfQZw3DnDmKE3YCQF5E8oD89UXoHn9JFEhJUz"),
    Pubkey.from_string("AVmoTthdrX6tKt4nDjco2D775W2YK3sDhxPcMmzUAmTY"),
    Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"),
    Pubkey.from_string("FWsW1xNtWscwNmKv6wVsU1iTzRN6wmmk3MjxRP5tT7hz"),
    Pubkey.from_string("G5UZAVbAf46s7cKWoyKu8kYTip9DGTpbLZ2qa9Aq69dP"),
    Pubkey.from_string("TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"),
]


async def pick_fee_recipient() -> Pubkey:
    return random.choice(AUTHORIZED_FEE_RECIPIENTS)


async def get_authorized_fee_recipients() -> list[Pubkey]:
    return list(AUTHORIZED_FEE_RECIPIENTS)


TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
RENT_SYSVAR = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

# Anchor 8-byte discriminators (unchanged across the 2026-04-28 upgrade —
# only the instruction args and account list changed).
BUY_DISCRIMINATOR = bytes([102, 6, 61, 18, 1, 218, 235, 234])
SELL_DISCRIMINATOR = bytes([51, 230, 133, 164, 1, 127, 131, 173])
CREATE_DISCRIMINATOR = bytes([24, 30, 200, 40, 5, 28, 7, 119])

# Tail bytes for buy/sell args: OptionBool track_volume = Some(true)
TRACK_VOLUME_TAIL = bytes([1, 1])

# Pump.fun initial bonding curve constants
INITIAL_VIRTUAL_SOL_RESERVES = 30_000_000_000  # 30 SOL in lamports
INITIAL_VIRTUAL_TOKEN_RESERVES = 1_073_000_000_000_000  # 1.073B tokens × 1e6
LAUNCH_BASELINE_PRICE_SOL = (
    INITIAL_VIRTUAL_SOL_RESERVES / INITIAL_VIRTUAL_TOKEN_RESERVES / LAMPORTS_PER_SOL
)


# ---------- Token program detection ----------
async def get_mint_token_program(mint_str: str) -> Pubkey:
    """Return the token program (classic SPL or Token-2022) that owns the mint.

    Post-2026-04-28 upgrade, Pump.fun supports both. The buy/sell ix MUST pass
    the right one as the `token_program` account or the inner ATA CPI fails
    with `IncorrectProgramId`.
    """
    res = await rpc_call(
        "getAccountInfo",
        [mint_str, {"encoding": "base64", "commitment": "confirmed"}],
    )
    val = (res.get("result") or {}).get("value")
    if not val:
        return TOKEN_PROGRAM
    owner = val.get("owner") or ""
    try:
        return Pubkey.from_string(owner)
    except Exception:
        return TOKEN_PROGRAM


def derive_associated_token_for_program(owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )
    return pda


# ---------- PDA derivations ----------
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


def derive_creator_vault(creator: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [b"creator-vault", bytes(creator)], PUMP_PROGRAM_ID
    )
    return pda


def derive_global_volume_accumulator() -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [b"global_volume_accumulator"], PUMP_PROGRAM_ID
    )
    return pda


def derive_user_volume_accumulator(user: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)], PUMP_PROGRAM_ID
    )
    return pda


def derive_bonding_curve_v2(mint: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [b"bonding-curve-v2", bytes(mint)], PUMP_PROGRAM_ID
    )
    return pda


def derive_fee_config() -> Pubkey:
    # Note: seeded under FEE_PROGRAM, NOT PUMP_PROGRAM_ID
    pda, _ = Pubkey.find_program_address(
        [b"fee_config", bytes(PUMP_PROGRAM_ID)], FEE_PROGRAM
    )
    return pda


# ---------- Bonding curve state ----------
async def fetch_bonding_curve_state(mint_str: str) -> dict | None:
    """
    Fetch the bonding curve account data.
    Layout (Anchor, post-2026-04-28):
      0..8    discriminator
      8..16   virtual_token_reserves (u64)
      16..24  virtual_sol_reserves (u64)
      24..32  real_token_reserves (u64)
      32..40  real_sol_reserves (u64)
      40..48  token_total_supply (u64)
      48      complete (bool)
      49..81  creator (Pubkey)
      81      reserved (u8)
      82      cashback_enabled (bool)   ← determines sell IX shape
      83..    extended fields
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
    data = base64.b64decode(data_b64)
    if len(data) < 49:
        return None
    vtr, vsr, rtr, rsr, tts = struct.unpack_from("<QQQQQ", data, 8)
    complete = bool(data[48])
    # Creator stored ON the bonding curve (offset 49-81). MUST use this for the
    # creator_vault PDA — the launch metadata's "creator" can differ for
    # tokens minted via Pump.fun's deployer-as-a-service, causing the program
    # to reject the IX with ConstraintSeeds (2006).
    try:
        creator = str(Pubkey(data[49:81])) if len(data) >= 81 else None
    except Exception:
        creator = None
    is_cashback = bool(data[82]) if len(data) > 82 else False
    return {
        "virtual_token_reserves": vtr,
        "virtual_sol_reserves": vsr,
        "real_token_reserves": rtr,
        "real_sol_reserves": rsr,
        "token_total_supply": tts,
        "complete": complete,
        "creator": creator,
        "is_cashback": is_cashback,
    }


# ---------- Quote math (unchanged constant-product) ----------
def quote_buy_tokens(state: dict, sol_in_lamports: int, slippage_bps: int = 500) -> tuple[int, int]:
    vsr = state["virtual_sol_reserves"]
    vtr = state["virtual_token_reserves"]
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


# ---------- Instruction builders ----------
def build_create_ata_ix(payer: Pubkey, owner: Pubkey, mint: Pubkey, token_program: Pubkey = TOKEN_PROGRAM) -> Instruction:
    ata = derive_associated_token_for_program(owner, mint, token_program)
    return Instruction(
        program_id=ASSOCIATED_TOKEN_PROGRAM,
        data=bytes([1]),  # CreateIdempotent
        accounts=[
            AccountMeta(payer, True, True),
            AccountMeta(ata, False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(token_program, False, False),
        ],
    )


async def build_buy_ix(
    user: Pubkey,
    mint: Pubkey,
    amount_tokens: int,
    max_sol_cost_lamports: int,
    creator: Pubkey,
    token_program: Pubkey = TOKEN_PROGRAM,
) -> Instruction:
    """Build a Pump.fun buy instruction with the post-2026-04-28 18-account layout."""
    bonding_curve = derive_bonding_curve(mint)
    associated_bonding_curve = derive_associated_token_for_program(bonding_curve, mint, token_program)
    associated_user = derive_associated_token_for_program(user, mint, token_program)
    creator_vault = derive_creator_vault(creator)
    global_vol_acc = derive_global_volume_accumulator()
    user_vol_acc = derive_user_volume_accumulator(user)
    fee_config = derive_fee_config()
    bonding_curve_v2 = derive_bonding_curve_v2(mint)
    breaking_fee = _pick_breaking_fee_recipient()
    fee_recipient = await pick_fee_recipient()

    data = (
        BUY_DISCRIMINATOR
        + struct.pack("<QQ", amount_tokens, max_sol_cost_lamports)
        + TRACK_VOLUME_TAIL
    )
    return Instruction(
        program_id=PUMP_PROGRAM_ID,
        data=data,
        accounts=[
            AccountMeta(GLOBAL_PDA, False, False),                  # 0
            AccountMeta(fee_recipient, False, True),                # 1
            AccountMeta(mint, False, False),                        # 2
            AccountMeta(bonding_curve, False, True),                # 3
            AccountMeta(associated_bonding_curve, False, True),     # 4
            AccountMeta(associated_user, False, True),              # 5
            AccountMeta(user, True, True),                          # 6
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),           # 7
            AccountMeta(token_program, False, False),               # 8
            AccountMeta(creator_vault, False, True),                # 9
            AccountMeta(EVENT_AUTHORITY, False, False),             # 10
            AccountMeta(PUMP_PROGRAM_ID, False, False),             # 11
            AccountMeta(global_vol_acc, False, False),              # 12
            AccountMeta(user_vol_acc, False, True),                 # 13
            AccountMeta(fee_config, False, False),                  # 14
            AccountMeta(FEE_PROGRAM, False, False),                 # 15
            AccountMeta(bonding_curve_v2, False, False),            # 16
            AccountMeta(breaking_fee, False, True),                 # 17
        ],
    )


async def build_sell_ix(
    user: Pubkey,
    mint: Pubkey,
    amount_tokens: int,
    min_sol_out_lamports: int,
    creator: Pubkey,
    token_program: Pubkey = TOKEN_PROGRAM,
    cashback: bool = False,
) -> Instruction:
    """Build a Pump.fun sell instruction with the post-2026-04-28 layout.

    Cashback coins require `user_volume_accumulator` inserted before
    `bonding_curve_v2`. Detect this via byte 82 of the bonding curve account.
    """
    bonding_curve = derive_bonding_curve(mint)
    associated_bonding_curve = derive_associated_token_for_program(bonding_curve, mint, token_program)
    associated_user = derive_associated_token_for_program(user, mint, token_program)
    creator_vault = derive_creator_vault(creator)
    fee_config = derive_fee_config()
    bonding_curve_v2 = derive_bonding_curve_v2(mint)
    breaking_fee = _pick_breaking_fee_recipient()
    fee_recipient = await pick_fee_recipient()

    data = (
        SELL_DISCRIMINATOR
        + struct.pack("<QQ", amount_tokens, min_sol_out_lamports)
        + TRACK_VOLUME_TAIL
    )
    accounts = [
        AccountMeta(GLOBAL_PDA, False, False),                  # 0
        AccountMeta(fee_recipient, False, True),                # 1
        AccountMeta(mint, False, False),                        # 2
        AccountMeta(bonding_curve, False, True),                # 3
        AccountMeta(associated_bonding_curve, False, True),     # 4
        AccountMeta(associated_user, False, True),              # 5
        AccountMeta(user, True, True),                          # 6
        AccountMeta(SYSTEM_PROGRAM_ID, False, False),           # 7
        AccountMeta(creator_vault, False, True),                # 8
        AccountMeta(token_program, False, False),               # 9
        AccountMeta(EVENT_AUTHORITY, False, False),             # 10
        AccountMeta(PUMP_PROGRAM_ID, False, False),             # 11
        AccountMeta(fee_config, False, False),                  # 12
        AccountMeta(FEE_PROGRAM, False, False),                 # 13
    ]
    if cashback:
        user_vol_acc = derive_user_volume_accumulator(user)
        accounts.append(AccountMeta(user_vol_acc, False, True))  # +1 cashback
    accounts.append(AccountMeta(bonding_curve_v2, False, False))
    accounts.append(AccountMeta(breaking_fee, False, True))
    return Instruction(
        program_id=PUMP_PROGRAM_ID,
        data=data,
        accounts=accounts,
    )


async def send_versioned_tx(
    keypair, instructions: list, priority_fee_microlamports: int = 500_000,
    compute_unit_limit: int = 200_000,
) -> str:
    """Build, sign, and send a versioned transaction. Returns signature string."""
    ixs = [
        set_compute_unit_limit(compute_unit_limit),
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
