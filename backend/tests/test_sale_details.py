"""Reconciler: only a tx that reduced our balance of the mint counts as the sale (rent reclaim / unrelated txs never do)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import solana_client  # noqa: E402

W = "8h7RHbmqfAnAN5NZeAX9B9z8TKubkAqPBqdiHKTnn2ue"
RELAYER = "FHpcNSe6xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
MINT = "DzMwArmdMintxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _tx(keys, pre_bal, post_bal, pre_tok, post_tok, err=None):
    return {"result": {"meta": {"err": err, "preBalances": pre_bal, "postBalances": post_bal,
                                "preTokenBalances": pre_tok, "postTokenBalances": post_tok},
                       "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys]}}}}


def _tb(idx, mint, owner, amount):
    return {"accountIndex": idx, "mint": mint, "owner": owner, "uiTokenAmount": {"amount": str(amount)}}


def _run(tx):
    async def fake_rpc(method, params):
        return tx
    orig = solana_client.rpc_call
    solana_client.rpc_call = fake_rpc
    try:
        return asyncio.run(solana_client.get_tx_sale_details("sig", W, MINT))
    finally:
        solana_client.rpc_call = orig


def test_rent_reclaim_is_not_a_sale():
    # wallet signs, gains 0.0046 SOL from closing empty accounts, mint balance 0 → 0 (account gone)
    tx = _tx([W], [100_000_000], [104_561_404], [_tb(1, MINT, W, 0)], [])
    assert _run(tx) is None


def test_relayer_sale_to_usdc_is_a_sale():
    tx = _tx([RELAYER, W], [5_000_000, 100_000_000], [4_985_000, 99_975_000],
             [_tb(2, MINT, W, 1_532_916_172), _tb(3, USDC, W, 0)],
             [_tb(2, MINT, W, 0), _tb(3, USDC, W, 808_410)])
    d = _run(tx)
    assert d["tokens_sold"] == 1_532_916_172
    assert abs(d["stable_usd"] - 0.80841) < 1e-9
    assert d["lamports"] == -25_000 and d["signer_is_wallet"] is False


def test_normal_sol_sale():
    tx = _tx([W], [100_000_000], [124_876_057], [_tb(1, MINT, W, 26_000_000_000)], [_tb(1, MINT, W, 130_000_000)])
    d = _run(tx)
    assert d["tokens_sold"] == 25_870_000_000 and d["lamports"] == 24_876_057 and d["stable_usd"] == 0.0


def test_failed_tx_ignored():
    tx = _tx([W], [1], [1], [_tb(1, MINT, W, 5)], [_tb(1, MINT, W, 0)], err={"InstructionError": [3, {"Custom": 1}]})
    assert _run(tx) is None
