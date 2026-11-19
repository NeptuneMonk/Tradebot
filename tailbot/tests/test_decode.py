"""Leader fill decoding. The Wirelys fixture is a real mainnet tx: USDC in, SOL balance up."""
import json
from pathlib import Path

import pytest

from tailbot.decode import decode_activity, fills_from_logs, needs_fetch, payment_sol
from tailbot.pump_logs import TRADE_FIELDS, USDC, WSOL, encode_trade_event, parse_trade_event

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "wirelys_usdc_buy.json").read_text())
LEADER = FIXTURE["leader"]
MINT = FIXTURE["mint"]
SIG = FIXTURE["signature"]


def _tx():
    return {"meta": FIXTURE["meta"], "transaction": FIXTURE["transaction"]}


def test_usdc_buy_ignores_positive_sol_delta():
    logs = FIXTURE["meta"]["logMessages"]
    assert needs_fetch(logs, LEADER, None) is True
    assert fills_from_logs(logs, LEADER, SIG) == []
    fills = decode_activity(LEADER, SIG, logs, _tx())
    buys = [f for f in fills if f.side == "buy" and f.mint == MINT]
    assert len(buys) == 1
    fill = buys[0]
    assert fill.payment_mint == USDC
    assert fill.payment_kind == "stable"
    assert fill.payment_ui == pytest.approx(650.231719, abs=1e-3)
    assert fill.token_ui == pytest.approx(13_125_763.601089, abs=1e-3)
    assert fill.token_raw == 13_125_763_601_089
    assert fill.sol_delta_lamports > 0
    assert fill.sol_delta_lamports / 1e9 == pytest.approx(0.00148844, abs=5e-6)
    assert fill.relayer is True
    assert fill.fee_payer != LEADER
    assert fill.venue == "pumpswap"
    assert fill.pool == "FBzrWLqWuzVzWBTYL1jZYmUP5CKHe5gda1V443pLLaMi"
    # SOL-equivalent of the USDC paid, not the +0.0015 rebate.
    assert payment_sol(fill, 76) == pytest.approx(650.231719 / 76, rel=1e-6)
    assert payment_sol(fill, 76) > 1.0


def test_pumpfun_event_decodes_without_a_fetch():
    raw = encode_trade_event({
        "mint": MINT,
        "sol_amount": 1_000_000_000,
        "token_amount": 100_000_000,
        "is_buy": True,
        "user": LEADER,
        "timestamp": 10,
        "virtual_sol_reserves": 30_000_000_000,
        "virtual_token_reserves": 10**15,
    })
    logs = [f"Program data: {__import__('base64').b64encode(raw).decode()}"]
    assert needs_fetch(logs, LEADER, None) is False
    fills = fills_from_logs(logs, LEADER, "localsig")
    assert len(fills) == 1
    assert fills[0].side == "buy" and fills[0].payment_kind == "sol"
    assert fills[0].payment_ui == pytest.approx(1.0)
    assert fills[0].token_ui == pytest.approx(100.0)
    assert fills[0].venue == "pumpfun"
    assert fills[0].mint == MINT


def test_usdc_quoted_curve_event():
    values = {}
    for name, typ in TRADE_FIELDS:
        if typ == "pubkey":
            values[name] = "11111111111111111111111111111111"
        elif typ == "bool":
            values[name] = False
        elif typ == "string":
            values[name] = "buy"
        elif typ == "shareholders":
            values[name] = []
        else:
            values[name] = 0
    values.update({
        "mint": MINT, "user": LEADER, "is_buy": True, "sol_amount": 0,
        "token_amount": 5_000_000, "quote_mint": USDC, "quote_amount": 650_230_000,
    })
    ev = parse_trade_event(encode_trade_event(values))
    assert ev["quote_mint"] == USDC and ev["quote_amount"] == 650_230_000
    fill = fills_from_logs([f"Program data: {__import__('base64').b64encode(encode_trade_event(values)).decode()}"], LEADER, "q")[0]
    assert fill.payment_mint == USDC
    assert fill.payment_ui == pytest.approx(650.23)
    assert fill.payment_kind == "stable"
    assert needs_fetch([f"Program data: {__import__('base64').b64encode(encode_trade_event(values)).decode()}"], LEADER, None) is False


def _tx_rows(keys, pre_sol, post_sol, pre_tok, post_tok):
    return {
        "meta": {
            "err": None, "fee": 5000,
            "preBalances": pre_sol, "postBalances": post_sol,
            "preTokenBalances": pre_tok, "postTokenBalances": post_tok,
            "logMessages": [],
        },
        "transaction": {"message": {"accountKeys": [{"pubkey": k, "signer": i == 0 or k == LEADER} for i, k in enumerate(keys)]}},
    }


def _tok(idx, mint, owner, amount, decimals=6):
    return {"accountIndex": idx, "mint": mint, "owner": owner,
            "uiTokenAmount": {"amount": str(amount), "decimals": decimals}}


def test_paired_coin_payment_is_the_token_not_the_sol_rebate():
    fee_payer = "11111111111111111111111111111111"
    quote = "So11111111111111111111111111111112"  # placeholder replaced below
    quote = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"  # some other mint
    base = MINT
    keys = [fee_payer, LEADER, "ataQ", "ataB"]
    pre = _tok(2, quote, LEADER, 1_000_000_000, 6)
    post_q = _tok(2, quote, LEADER, 400_000_000, 6)
    post_b = _tok(3, base, LEADER, 5_000_000, 6)
    # Leader SOL goes UP by 0.0015 while they spent the paired token.
    tx = _tx_rows(keys, [10**9, 2 * 10**9, 0, 0], [10**9 - 5000, 2 * 10**9 + 1_500_000, 0, 0], [pre], [post_q, post_b])
    fills = decode_activity(LEADER, "pair", [], tx)
    buy = next(f for f in fills if f.mint == base)
    assert buy.side == "buy"
    assert buy.payment_mint == quote
    assert buy.payment_kind == "token"
    assert buy.payment_raw == 600_000_000
    assert buy.sol_delta_lamports > 0
    assert payment_sol(buy, 100) is None  # not priced off the SOL rebate
    sell = next(f for f in fills if f.mint == quote)
    assert sell.side == "sell"
    assert sell.pre_token_raw == 1_000_000_000
    assert sell.token_raw == 600_000_000


def test_plain_sol_transfer_does_not_need_a_fetch():
    logs = ["Program 11111111111111111111111111111111 invoke [1]", "Program 11111111111111111111111111111111 success"]
    assert needs_fetch(logs, LEADER, None) is False


def test_failed_tx_is_ignored():
    assert needs_fetch(["Program " + WSOL], LEADER, {"InstructionError": [0, "Custom"]}) is False


def test_unsigned_airdrop_or_drip_is_not_a_leader_buy():
    # A third party created the leader's token account and dripped tokens. The leader never signed.
    sender = "3qscUPxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    keys = [sender, LEADER, "ataX"]
    post = _tok(2, MINT, LEADER, 1_000_000_000_000)
    tx = _tx_rows(keys, [10**9, 10**9, 0], [10**9 - 5000, 10**9, 0], [], [post])
    tx["transaction"]["message"]["accountKeys"][1]["signer"] = False
    assert decode_activity(LEADER, "drop", [], tx) == []


def test_signers_from_plain_json_header():
    from tailbot.decode import tx_signers
    tx = {"transaction": {"message": {"accountKeys": ["A", "B", "C"], "header": {"numRequiredSignatures": 2}}}}
    assert tx_signers(tx) == {"A", "B"}
