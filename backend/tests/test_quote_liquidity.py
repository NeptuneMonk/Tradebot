"""Non-SOL-paired liquidity: a PUMP-/USDC-paired PumpSwap pool or curve must read in SOL-equivalents, not as 0."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_quote_liq")
os.environ.setdefault("HELIUS_RPC_URL", "https://rpc.invalid/?api-key=x")
os.environ.setdefault("HELIUS_WSS_URL", "wss://rpc.invalid/?api-key=x")
os.environ.setdefault("PUMP_PROGRAM_ID", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

import pumpswap
from quote_mints import quote_book, WSOL, PUMP, LAMPORTS_PER_SOL

PUMP_RATE = 0.0000045 * LAMPORTS_PER_SOL / 1e6        # ~4500 lamports per raw PUMP unit (6 decimals)


def _acct(amount: int, decimals: int = 6) -> dict:
    return {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(amount), "decimals": decimals}}}}}


def test_sol_pool_unchanged():
    st = pumpswap._with_reserves({"pool": "P", "quote_mint": WSOL}, _acct(10**15), _acct(50 * LAMPORTS_PER_SOL, 9))
    assert st["quote_is_sol"] is True and st["quote_reserves"] == 50 * LAMPORTS_PER_SOL == st["real_sol_reserves"]
    assert st["quote_priced"] is True and st["quote_symbol"] is None


def test_pump_paired_pool_converts_to_sol_equivalents():
    quote_book.rates[PUMP] = {"lamports_per_raw": PUMP_RATE, "symbol": "PUMP", "decimals": 6, "price_usd": 0.001, "ts": time.time()}
    raw_quote = 20_000_000 * 10**6                     # 20M PUMP in the vault
    st = pumpswap._with_reserves({"pool": "P", "quote_mint": PUMP}, _acct(10**15), _acct(raw_quote))
    assert st["quote_is_sol"] is False and st["quote_symbol"] == "PUMP" and st["quote_priced"] is True
    assert st["quote_reserves_raw"] == raw_quote
    assert abs(st["quote_reserves"] - raw_quote * PUMP_RATE) < 2                  # ≈ 90 SOL-equivalent, not "0 liquidity"
    assert st["real_sol_reserves"] == st["quote_reserves"]
    assert pumpswap.price_sol_per_raw_token(st) > 0
    tokens, max_sol = pumpswap.quote_buy_tokens(st, int(0.5 * LAMPORTS_PER_SOL), 500)
    assert tokens > 0 and max_sol > int(0.5 * LAMPORTS_PER_SOL)


def test_unpriced_quote_reads_zero_until_priced_but_is_flagged():
    quote_book.rates.pop("Unknown111111111111111111111111111111111111", None)
    st = pumpswap._with_reserves({"pool": "P", "quote_mint": "Unknown111111111111111111111111111111111111"}, _acct(10**15), _acct(10**12))
    assert st["quote_is_sol"] is False and st["quote_priced"] is False and st["quote_reserves"] == 0
    assert st["quote_reserves_raw"] == 10**12
