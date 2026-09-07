"""Sequencer feed: tx decoding, curve→token mapping, rug alert → immediate exit trigger."""
import base64, json, os, sys, time
from types import SimpleNamespace
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from eth_account import Account
import rh_feed
import rh_wallet
import rh_live
from models import BotConfig

CURVE = "0x" + "c" * 40
TOKEN = "0x" + "a" * 40
ME = Account.create()


def _signed_sell(tokens_raw: int) -> bytes:
    data = rh_wallet.calldata(rh_live.SELL_SIG, ["uint256", "uint256", "address"], [tokens_raw, 0, ME.address])
    tx = {"type": 2, "chainId": 4663, "nonce": 1, "to": rh_wallet.checksum(CURVE), "value": 0, "data": data,
          "gas": 160000, "maxPriorityFeePerGas": 1, "maxFeePerGas": 2, "accessList": []}
    return bytes(ME.sign_transaction(tx).raw_transaction)


def _feed_message(seq: int, raw_txs: list[bytes]) -> str:
    batch = b"\x03" + b"".join(len(b"\x04" + r).to_bytes(8, "big") + b"\x04" + r for r in raw_txs)
    return json.dumps({"version": 1, "messages": [{"sequenceNumber": seq, "message": {"message": {
        "header": {"kind": 3}, "l2Msg": base64.b64encode(batch).decode()}}}]})


def test_decode_and_classify_typed_sell():
    raw = _signed_sell(27_018_636 * 10**18)
    tx = rh_feed.decode_tx(raw)
    assert tx["to"] == CURVE and tx["value"] == 0
    assert rh_feed.classify(tx) == ("sell", 27_018_636 * 10**18)
    assert list(rh_feed._walk(b"\x04" + raw)) == [raw]


def _state(price=1e-9, net_quote=2.0, with_pos=True):
    b = {"curve": CURVE, "symbol": "TST", "quote_symbol": "ETH", "last_price_quote": price, "net_quote": net_quote}
    disc = SimpleNamespace(tracking={TOKEN: b}, _curve_to_token={CURVE: TOKEN}, _quote_usd=lambda s: 2500.0)
    paper = SimpleNamespace(positions={}, stats={}, latency_blocks=lambda: 6)
    st = SimpleNamespace(config=BotConfig(), rh_discovery=disc, rh_paper=paper)
    import rh_paper as rp
    paper.on_rug_alert = lambda token, bb, seq, now, **kw: rp.RHPaperTrader.on_rug_alert(paper, token, bb, seq, now, **kw)
    if with_pos:
        paper.positions[TOKEN] = {"trade": {"entry_price_quote": price}, "_last_price": price}
    return st, b


def test_big_sell_on_held_curve_triggers_immediate_exit():
    st, b = _state()
    feed = rh_feed.RHSequencerFeed(st)
    # 1,000,000 tokens × 1e-9 ETH = 0.001 ETH ≈ $2.50 → not a rug
    feed._on_message(_feed_message(100, [_signed_sell(1_000_000 * 10**18)]))
    assert feed.stats["curve_sells"] == 1 and feed.stats["rug_alerts"] == 0
    assert "_exiting" not in st.rh_paper.positions[TOKEN]
    # 400,000,000 tokens × 1e-9 = 0.4 ETH ≈ $1000 and 20% of 2 ETH reserves → rug
    feed._on_message(_feed_message(101, [_signed_sell(400_000_000 * 10**18)]))
    pos = st.rh_paper.positions[TOKEN]
    assert feed.stats["rug_alerts"] == 1 and pos["_exiting"] is True
    assert pos["exit_trigger"]["reason"] == "rug_detected" and pos["exit_trigger"]["block"] == 101
    assert pos["exit_trigger"]["fill_block"] == 107 and pos["_rug_alert"]["est_usd"] == 1000.0
    # a second alert while exiting is ignored
    feed._on_message(_feed_message(102, [_signed_sell(400_000_000 * 10**18)]))
    assert feed.stats["rug_alerts"] == 1


def test_sell_on_untracked_curve_ignored_and_no_position_no_alert():
    st, b = _state(with_pos=False)
    feed = rh_feed.RHSequencerFeed(st)
    feed._on_message(_feed_message(1, [_signed_sell(400_000_000 * 10**18)]))
    assert feed.stats["curve_sells"] == 1 and feed.stats["rug_alerts"] == 0
    st.rh_discovery._curve_to_token.clear()
    feed._on_message(_feed_message(2, [_signed_sell(400_000_000 * 10**18)]))
    assert feed.stats["curve_sells"] == 1


def _signed_buy(quote_wei: int) -> bytes:
    data = rh_wallet.calldata(rh_live.BUY_SIG, ["uint256", "uint256", "address"], [quote_wei, 0, ME.address])
    tx = {"type": 2, "chainId": 4663, "nonce": 2, "to": rh_wallet.checksum(CURVE), "value": quote_wei, "data": data,
          "gas": 160000, "maxPriorityFeePerGas": 1, "maxFeePerGas": 2, "accessList": []}
    return bytes(ME.sign_transaction(tx).raw_transaction)


def test_feed_ticks_estimate_price_and_trigger_sl_early():
    import rh_paper as rp
    st, b = _state(price=1e-9, net_quote=2.0)
    b["impact_k"] = 2.0
    paper = st.rh_paper
    paper.state = st
    paper.on_feed_tick = lambda *a, **k: rp.RHPaperTrader.on_feed_tick(paper, *a, **k)
    paper._decide_exit = lambda *a, **k: rp.RHPaperTrader._decide_exit(paper, *a, **k)
    st.config.stop_loss_pct = 20.0
    st.config.rh_rug_sell_usd = 1e9
    st.config.rh_rug_sell_curve_pct = 100.0
    st.config.exit_momentum_gate_enabled = False
    st.config.no_momentum_exit_enabled = False
    st.config.hold_max_seconds = 600
    pos = paper.positions[TOKEN]
    pos.update({"trade": {"entry_price_quote": 1e-9, "entry_usd": 5.0, "entry_tokens": 1.0}, "peak_price": 1e-9, "opened": time.time() - 5})
    feed = rh_feed.RHSequencerFeed(st)
    # small sell: 50,000,000 tokens ≈ 0.05 ETH of a 2 ETH curve → ~-4.9%, no exit
    feed._on_message(_feed_message(10, [_signed_sell(50_000_000 * 10**18)]))
    assert "_exiting" not in pos and b["feed_est"]["seq"] == 10 and 0.94e-9 < b["feed_est"]["price"] < 0.96e-9
    # compounding sells on the same base (poll hasn't caught up): 250,000,000 tokens ≈ 0.24 ETH → past -20% → SL now
    feed._on_message(_feed_message(11, [_signed_sell(250_000_000 * 10**18)]))
    assert pos["_exiting"] is True and pos["exit_trigger"]["reason"] == "stop_loss"
    assert pos["exit_trigger"]["source"] == "feed" and pos["exit_trigger"]["block"] == 11 and pos["exit_trigger"]["fill_block"] == 17
    assert paper.stats["feed_exits"] == 1


def test_feed_buy_tick_raises_estimate_and_poll_resets_it():
    st, b = _state(price=1e-9, net_quote=1.0, with_pos=True)
    st.rh_paper.on_feed_tick = lambda *a, **k: None
    feed = rh_feed.RHSequencerFeed(st)
    feed._on_message(_feed_message(5, [_signed_buy(int(0.1 * 10**18))]))
    assert b["feed_est"]["kind"] == "buy" and b["feed_est"]["price"] > 1.15e-9
    b["last_block"] = 6
    b.pop("feed_est")           # what apply_trade does when the poll lands a real trade
    feed._on_message(_feed_message(7, [_signed_buy(int(0.1 * 10**18))]))
    assert abs(b["feed_est"]["price"] - 1e-9 * (1 + 2.0 * 0.1 / 1.1)) < 1e-15


# ---------------- exact PONS curve model (V = 1.68 ETH virtual reserve, fitted on-chain) ----------------
V, SUPPLY = 1.68, 1_000_000_000
K0 = V * SUPPLY


def _sim_buy(a, q_in, fee=0.01):
    """Return (tokens_out, a_after) for a gross quote-in on the exact curve."""
    q_eff = q_in * (1 - fee)
    x = K0 / a
    a2 = a + q_eff
    return x - K0 / a2, a2


def _sim_sell(a, tokens):
    x = K0 / a
    a2 = K0 / (x + tokens)
    return a - a2, a2  # gross quote out, a_after


def test_curve_after_trade_recovers_exact_state_from_a_single_trade():
    from rh_discovery import curve_after_trade
    a = V + 0.37                      # 0.37 ETH of real reserves
    tokens_out, a2 = _sim_buy(a, 0.25)
    pre, post = curve_after_trade("buy", 0.25 * 0.99, tokens_out, K0)
    assert abs(pre - a) < 1e-9 and abs(post - a2) < 1e-9
    q_out, a3 = _sim_sell(a2, 40_000_000)
    pre, post = curve_after_trade("sell", q_out, 40_000_000, K0)
    assert abs(pre - a2) < 1e-9 and abs(post - a3) < 1e-9


def test_feed_projection_matches_exact_curve_and_compounds_until_poll():
    st, b = _state(price=1e-9, net_quote=0.1, with_pos=True)
    st.rh_paper.on_feed_tick = lambda *a, **k: None
    a = V + 0.1
    b.update({"curve_a": a, "curve_k0": K0, "quote_decimals": 18, "start": time.time() - 60, "last_block": 4})
    feed = rh_feed.RHSequencerFeed(st)
    feed._on_message(_feed_message(5, [_signed_sell(50_000_000 * 10**18)]))
    _, a2 = _sim_sell(a, 50_000_000)
    assert b["feed_est"]["exact"] and abs(b["feed_est"]["price"] - a2 * a2 / K0) < 1e-18
    # legacy model would have called this -56% (0.05 ETH sell vs 0.1 real reserves, k=2); truth is -9.8%
    assert 0.89 < b["feed_est"]["price"] / (a * a / K0) < 0.91
    feed._on_message(_feed_message(6, [_signed_buy(int(0.2 * 10**18))]))
    _, a3 = _sim_buy(a2, 0.2)
    assert abs(b["feed_est"]["a"] - a3) < 1e-9 and b["feed_est"]["kind"] == "buy"


def test_rug_alert_uses_predicted_price_drop_not_real_reserve_share():
    st, b = _state(price=1e-9, net_quote=0.1, with_pos=True)
    st.config.rh_rug_sell_usd = 1e9          # isolate the %-drop rule
    st.config.rh_rug_sell_curve_pct = 15.0
    b.update({"curve_a": V + 0.1, "curve_k0": K0, "quote_decimals": 18, "start": time.time() - 60})
    feed = rh_feed.RHSequencerFeed(st)
    # 20M tokens ≈ 0.02 ETH: 20% of the 0.1 ETH REAL reserves (legacy: rug) but only ~4% price drop (exact: not a rug)
    feed._on_message(_feed_message(1, [_signed_sell(20_000_000 * 10**18)]))
    assert feed.stats["rug_alerts"] == 0 and 3.5 < b["last_seq_sell"]["curve_pct"] < 4.5
    # 90M tokens → ~-17% price → rug
    feed._on_message(_feed_message(2, [_signed_sell(90_000_000 * 10**18)]))
    pos = st.rh_paper.positions[TOKEN]
    assert feed.stats["rug_alerts"] == 1 and pos["exit_trigger"]["reason"] == "rug_detected"
    assert pos["exit_trigger"]["source"] == "feed" and pos["_rug_alert"]["exact"] is True
    assert pos["exit_trigger"]["price"] < 0.85 * (V + 0.1) ** 2 / K0
