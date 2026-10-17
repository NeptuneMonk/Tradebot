"""Code-review ba5782d — one test per item (#1–#10)."""
import asyncio
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
os.environ["RH_WALLET_PATH"] = os.path.join(tempfile.mkdtemp(), "rh_wallet.json")
os.environ["RH_WALLET_PASS_PATH"] = os.path.join(os.path.dirname(os.environ["RH_WALLET_PATH"]), "rh_wallet.pass")
import base58  # noqa: E402
from solders.keypair import Keypair  # noqa: E402

import wallet  # noqa: E402
import landing  # noqa: E402
import fills  # noqa: E402
import pumpfun  # noqa: E402
import pumpswap  # noqa: E402
import rh_wallet  # noqa: E402
import rh_dex  # noqa: E402
import solana_client  # noqa: E402
from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402
from pnl_reconciler import PnLReconciler  # noqa: E402


# ---------------------------------------------------------------- #1 env key wins, Mongo key purged
class _Coll:
    def __init__(self, docs):
        self.docs = docs
        self.deleted = 0

    async def delete_many(self, q):
        self.deleted = len(self.docs)
        self.docs.clear()

        class R:
            deleted_count = self.deleted
        return R()


class _DB:
    def __init__(self, docs):
        self.wallet_keys = _Coll(docs)


def test_1_signer_is_env_key_not_mongo_key(monkeypatch):
    a, b = Keypair(), Keypair()
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", base58.b58encode(bytes(a)).decode())
    monkeypatch.setattr(wallet, "_KEYPAIR", None, raising=False)
    kp = wallet._load_or_create_keypair()
    assert str(kp.pubkey()) == str(a.pubkey())
    st = BotState.__new__(BotState)
    st.db = _DB([{"_id": "current", "public_key": str(b.pubkey()), "secret_key_b58": base58.b58encode(bytes(b)).decode()}])
    asyncio.run(st._sync_wallet_key())
    assert st.db.wallet_keys.deleted == 1 and st.db.wallet_keys.docs == []
    assert str(wallet._load_or_create_keypair().pubkey()) == str(a.pubkey())
    monkeypatch.delenv("WALLET_PATH", raising=False)
    monkeypatch.delenv("WALLET_SECRET_PATH", raising=False)
    default = str(wallet._default_wallet_path())
    assert default.startswith(("/data/", str(os.path.expanduser("~")))) and "/app/backend" not in default


def test_1_export_endpoint_removed_and_import_paper_only():
    src = open(os.path.join(os.path.dirname(__file__), "..", "server.py")).read()
    assert "export-private-key" not in src
    assert "wallet_keys.update_one" not in src and "wallet_keys.update_one" not in open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    assert "HTTPException(410" in src


# ---------------------------------------------------------------- #2 direct PumpSwap 6004 escalates the slip ladder
def test_2_pumpswap_6004_is_slip_revert_direct_and_ladder_ends_at_terminal():
    assert BotState._is_slip_revert("custom program error: 0x1774 Custom:6004 ExceededSlippage", None, "pumpswap") is True
    assert BotState._is_slip_revert("Custom:6004", None, "pumpfun") is False      # plain curve: 6004 = MintDoesNotMatch
    assert BotState._is_slip_revert("Custom:6003", None) is True
    assert BotState._is_slip_revert("Custom:6001", None) is False and BotState._is_slip_revert("Custom:6001", "jupiter") is True
    st = BotState.__new__(BotState)
    st.config = BotConfig(intelligent_exit_v2=True, auto_exit_retry_slip_floors_bps=[1200, 1500], max_exit_slippage_bps=2000,
                          exit_terminal_slippage_bps=3000)
    ladder = st._exit_slip_ladder(800, terminal=True)
    assert ladder[0] == 800 and ladder == sorted(ladder) and ladder[-1] == 3000
    # every 6004 on a direct sell moves to the next rung until the terminal one
    idx = 0
    while BotState._is_slip_revert("Custom:6004", None, "pumpswap") and idx + 1 < len(ladder):
        idx += 1
    assert ladder[idx] == 3000


# ---------------------------------------------------------------- #3 RH graduated sell: no zero floor, abort if re-quote fails
def test_3_rh_dex_sell_fallback_is_capped_and_never_zero(monkeypatch):
    sent = []
    quotes = iter([10 ** 18, 9 * 10 ** 17])

    async def fake_quote(token, raw, quote):
        return next(quotes)

    async def fake_permit(token, raw):
        return 0

    def fake_calldata(token, raw, min_out, quote=None):
        sent.append(min_out)
        return f"0x{len(sent):02x}"

    async def fake_sim(to, data, value=0, sender=None):
        if data == "0x01":
            raise rh_wallet.RhRpcError("revert")
        return "0x"

    async def fake_send(to, data, value=0, gas_limit=None):
        return "0xtx"

    async def fake_receipt(tx):
        return {"ok": True, "gas_cost_wei": 1, "blockNumber": "0x1", "logs": []}

    async def fake_bal(*a, **k):
        return 0

    monkeypatch.setattr(rh_dex, "quote_sell", fake_quote)
    monkeypatch.setattr(rh_dex, "ensure_permit2", fake_permit)
    monkeypatch.setattr(rh_dex, "build_sell_calldata", fake_calldata)
    monkeypatch.setattr(rh_wallet, "simulate", fake_sim)
    monkeypatch.setattr(rh_wallet, "send", fake_send)
    monkeypatch.setattr(rh_wallet, "wait_receipt", fake_receipt)
    monkeypatch.setattr(rh_wallet, "balance_wei", fake_bal)
    monkeypatch.setattr(rh_wallet, "erc20_balance", fake_bal)
    monkeypatch.setattr(rh_wallet, "address", lambda: "0x" + "ab" * 20)
    cfg = BotConfig(rh_sell_fallback_slippage_pct=20.0, rh_protocol_fee_pct=1.0)
    asyncio.run(rh_dex.sell("0x" + "cd" * 20, 10 ** 18, 8.0, cfg=cfg))
    assert sent[0] == int(10 ** 18 * 0.92)
    assert sent[1] == int(9 * 10 ** 17 * 0.99 * 0.80) and sent[1] > 0
    assert all(m > 0 for m in sent)
    assert "minOut=0" not in open(os.path.join(os.path.dirname(__file__), "..", "rh_dex.py")).read()


def test_3_rh_dex_sell_aborts_when_requote_fails(monkeypatch):
    calls = {"q": 0}

    async def fake_quote(token, raw, quote):
        calls["q"] += 1
        if calls["q"] == 1:
            return 10 ** 18
        raise RuntimeError("rpc down")

    async def fake_permit(token, raw):
        return 0

    async def fake_sim(to, data, value=0, sender=None):
        raise rh_wallet.RhRpcError("revert")

    sent = {"n": 0}

    async def fake_send(*a, **k):
        sent["n"] += 1
        return "0xtx"

    monkeypatch.setattr(rh_dex, "quote_sell", fake_quote)
    monkeypatch.setattr(rh_dex, "ensure_permit2", fake_permit)
    monkeypatch.setattr(rh_dex, "build_sell_calldata", lambda *a, **k: "0x01")
    monkeypatch.setattr(rh_wallet, "simulate", fake_sim)
    monkeypatch.setattr(rh_wallet, "send", fake_send)
    try:
        asyncio.run(rh_dex.sell("0x" + "cd" * 20, 10 ** 18, 8.0, cfg=BotConfig()))
        assert False, "should abort"
    except RuntimeError as e:
        assert "keeping the position" in str(e)
    assert sent["n"] == 0


# ---------------------------------------------------------------- #4 realistic paper round trip
def _curve(sol=30.0, tokens=1_000_000_000 * 10 ** 6):
    return {"virtual_sol_reserves": int(sol * 1e9), "virtual_token_reserves": tokens, "complete": False}


def test_4_flat_price_paper_round_trip_nets_two_fees_plus_tx_costs():
    cfg = BotConfig(protocol_fee_pct_pumpfun=1.25, paper_extra_slip_bps=0, cost_gate_failed_tx_allowance_pct=0.0,
                    priority_fee_microlamports=100_000, speed_mode="manual")
    pumpfun.set_fee_bps(125)
    sol_in = int(0.02 * 1e9)
    deep = _curve(sol=30_000.0, tokens=10 ** 18)         # huge pool → negligible impact, isolates the fee model
    pf = fills.paper_fill("pumpfun", deep, sol_in, cfg)
    assert pf is not None and abs(pf["fee_pct"] - 1.25) < 1e-9
    sol_out_net, _ = pumpfun.quote_sell_sol(deep, pf["tokens_out"], 1000)
    exit_sol = fills.paper_exit_sol(sol_out_net, cfg)
    rng = random.Random(1)
    fee_in = fills.paper_tx_fee_sol(cfg, 100_000, 120_000, "swqos", rng=rng)["fee_sol"]
    fee_out = fills.paper_tx_fee_sol(cfg, 100_000, 120_000, "swqos", rng=rng)["fee_sol"]
    pnl = exit_sol - sol_in / 1e9 - fee_in - fee_out
    expected = -(sol_in / 1e9) * (2 * 0.0125) - fee_in - fee_out
    assert pnl < 0 and abs(pnl - expected) < 0.0125 * 0.0125 * sol_in / 1e9 + 1e-7   # within the fee-on-fee second-order term


def test_4_paper_tx_fee_charges_tip_and_failed_attempts():
    cfg = BotConfig(cost_gate_failed_tx_allowance_pct=100.0)       # every roll fails → 2 extra attempts (cap)
    r = fills.paper_tx_fee_sol(cfg, 200_000, 100_000, "dual", rng=random.Random(0))
    per_tx = (5_000 + 200_000 * 100_000 / 1e6 + 200_000) / 1e9
    assert abs(r["per_tx_sol"] - per_tx) < 1e-12 and r["failed_attempts"] == 2 and abs(r["fee_sol"] - 3 * per_tx) < 1e-12
    assert fills.fee_bps("pumpswap", BotConfig(protocol_fee_pct_pumpswap=0.3)) == 30
    assert fills.fee_bps("pumpfun", BotConfig(protocol_fee_pct_pumpfun=1.25)) == 125


# ---------------------------------------------------------------- #5 secrets never in tracked docs/code
def test_5_no_api_key_urls_in_repo_docs_and_gitleaks_wired():
    import re
    import subprocess
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    files = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout.split()
    pat = re.compile(r"api[-_]?key=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
    hits = []
    for f in files:
        if f.endswith((".lock", ".png", ".jpg", ".ico", ".woff", ".woff2")) or "node_modules" in f:
            continue
        try:
            txt = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        if pat.search(txt):
            hits.append(f)
    assert hits == [], hits
    assert os.path.exists(os.path.join(root, ".pre-commit-config.yaml")) and os.path.exists(os.path.join(root, ".github/workflows/gitleaks.yml"))
    assert "gitleaks" in open(os.path.join(root, ".pre-commit-config.yaml")).read()
    assert "api-key" not in open(os.path.join(root, "memory/PRD.md")).read().split("\n")[9]


# ---------------------------------------------------------------- #6 emergency sell uses venue CU + capped fee ladder via land_tx
def test_6_emergency_pumpswap_sell_uses_cu_limit_and_position_capped_ladder(monkeypatch):
    st = BotState.__new__(BotState)
    st.config = BotConfig(speed_mode="manual", priority_fee_microlamports=300_000, exit_terminal_slippage_bps=3000)
    st.active_trades = {}
    captured = {}

    async def fake_find_pool(mint):
        return "Pool111"

    async def fake_pool_state(pool):
        return {"base_reserves": 10 ** 15, "quote_reserves": 50 * 10 ** 9, "pool": pool}

    async def fake_tp(mint):
        return pumpfun.TOKEN_PROGRAM

    async def fake_land_sell(kp, ixs, eff_priority, panic, cu, label, alts=None, position_sol=None, mint=None):
        captured.update(prio=eff_priority, panic=panic, cu=cu, position_sol=position_sol, label=label)
        return "SIG", eff_priority

    monkeypatch.setattr(pumpswap, "find_pool_for_mint", fake_find_pool)
    monkeypatch.setattr(pumpswap, "fetch_pool_state", fake_pool_state)
    monkeypatch.setattr(pumpswap, "get_associated_token_address", lambda *a, **k: a[0])
    monkeypatch.setattr(pumpswap, "build_wsol_ata_idempotent_ixs", lambda user: (user, []))
    monkeypatch.setattr(pumpswap, "build_create_ata_ix", lambda *a, **k: "ix")
    monkeypatch.setattr(pumpswap, "build_sell_ix", lambda *a, **k: "ix")
    monkeypatch.setattr(pumpswap, "build_close_wsol_ix", lambda *a, **k: "ix")
    monkeypatch.setattr(pumpfun, "get_mint_token_program", fake_tp)
    monkeypatch.setattr(st, "_land_sell", fake_land_sell)
    kp = Keypair()
    from solders.pubkey import Pubkey
    res = asyncio.run(st._attempt_emergency_pumpswap_sell(mint=str(Pubkey.default()), kp=kp, user=kp.pubkey(), mint_pk=Pubkey.default(),
                                                           tokens_in=10 ** 12, trade_doc={}))
    assert res is not None and res[0] == "SIG"
    assert captured["cu"] == st.cu_limit("pumpswap", "sell") and captured["cu"] != 600_000
    assert captured["prio"] == 300_000 and captured["panic"] is True
    assert captured["position_sol"] is not None and captured["position_sol"] > 0
    # the capped ladder itself: one attempt's fee never exceeds exit_fee_max_pct_of_position of the position
    ladder = st._exit_fee_ladder(300_000, True, position_sol=captured["position_sol"], cu=captured["cu"])
    tip = 200_000
    for prio in ladder:
        assert (5_000 + prio * captured["cu"] / 1e6 + tip) <= captured["position_sol"] * 1e9 * st.config.exit_fee_max_pct_of_position / 100.0 + 5_000 + 1
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    assert "send_versioned_tx(kp, ixs, eff_priority" not in src          # stray raw sends (reentry / flush / runner add-on) are gone


# ---------------------------------------------------------------- #7 reconciler: real rent, re-arm on new exit sig, bounded retries, real entry price
def _tx(wallet, pre, post, pre_tb=(), post_tb=(), err=None, keys=None):
    keys = keys or [wallet, "ATA1", "Prog"]
    return {"transaction": {"message": {"accountKeys": keys}},
            "meta": {"preBalances": pre, "postBalances": post, "preTokenBalances": list(pre_tb), "postTokenBalances": list(post_tb), "err": err}}


def test_7_summary_reads_actual_rent_for_token2022_and_tokens_received():
    w = "W" * 32
    t22_rent = 2_074_080
    tx = _tx(w, [10_000_000, 0, 1], [10_000_000 - t22_rent - 5_000 - 1_000_000, t22_rent, 1],
             post_tb=[{"accountIndex": 1, "owner": w, "mint": "M", "uiTokenAmount": {"amount": "5000"}}])
    sm = solana_client.summarize_tx_meta(tx, w, "M")
    assert sm["rent_created"] == t22_rent and sm["rent_reclaimed"] == 0 and sm["tokens_delta"] == 5000
    close = _tx(w, [1_000_000, t22_rent, 1], [1_000_000 + t22_rent - 5_000, 0, 1],
                pre_tb=[{"accountIndex": 1, "owner": w, "mint": "M", "uiTokenAmount": {"amount": "5000"}}])
    sm2 = solana_client.summarize_tx_meta(close, w, "M")
    assert sm2["rent_reclaimed"] == t22_rent and sm2["rent_created"] == 0 and sm2["tokens_delta"] == -5000


class _Trades:
    def __init__(self):
        self.updates = []

    async def update_one(self, q, u):
        self.updates.append((q, u))


def _reconciler(summaries):
    st = type("S", (), {})()
    st.db = type("D", (), {})()
    st.db.trades = _Trades()
    r = PnLReconciler(st)

    async def fake_summary(sig, wallet, mint=None):
        return summaries.get(sig)
    import pnl_reconciler as pr
    pr.get_tx_wallet_summary = fake_summary
    return r, st


def test_7_real_entry_price_from_tokens_received_and_bounded_retries():
    sm_entry = {"delta": -(20_000_000 + 2_074_080), "failed": False, "rent_created": 2_074_080, "rent_reclaimed": 0, "tokens_delta": 4_000_000}
    sm_exit = {"delta": 25_000_000 + 2_074_080, "failed": False, "rent_created": 0, "rent_reclaimed": 2_074_080, "tokens_delta": -4_000_000}
    r, st = _reconciler({"E": sm_entry, "X": sm_exit})
    t = {"id": "t1", "mint": "M", "entry_sig": "E", "exit_sig": "X", "entry_tokens": 4_100_000}
    asyncio.run(r._reconcile_one(t, "W", 100.0))
    upd = st.db.trades.updates[-1][1]["$set"]
    assert upd["pnl_reconciled"] is True and upd["reconcile_incomplete"] is False
    assert upd["rent_locked_lamports"] == 0 and upd["rent_reclaimed_lamports"] == 2_074_080
    assert abs(upd["real_pnl_sol"] - 0.005) < 1e-9                       # +25 −20 SOL-cents, rent round-tripped
    assert abs(upd["real_entry_price_sol"] - (0.02 / 4_000_000)) < 1e-15   # from tokens RECEIVED, not quoted entry_tokens
    assert upd["real_entry_tokens"] == 4_000_000
    # exit leg never indexes: bounded retries then reconcile_incomplete
    r2, st2 = _reconciler({"E": sm_entry})
    t2 = {"id": "t2", "mint": "M", "entry_sig": "E", "exit_sig": "GONE", "entry_tokens": 1, "reconcile_attempts": 2}
    asyncio.run(r2._reconcile_one(t2, "W", 100.0))
    assert st2.db.trades.updates[-1][1]["$set"] == {"reconcile_attempts": 3}
    t2["reconcile_attempts"] = 7
    asyncio.run(r2._reconcile_one(t2, "W", 100.0))
    upd2 = st2.db.trades.updates[-1][1]["$set"]
    assert upd2["reconcile_incomplete"] is True and upd2["pnl_reconciled"] is True and upd2["sigs_unindexed"] == ["GONE"]


def test_7_close_paths_rearm_reconciler():
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    assert src.count('"pnl_reconciled": False') + src.count('trade_doc["pnl_reconciled"] = False') >= 4


# ---------------------------------------------------------------- #8 pump quotes include the fee
def test_8_quotes_apply_fee_before_slippage():
    st = _curve(sol=30.0)
    sol_in = 10 ** 8
    tokens, max_sol = pumpfun.quote_buy_tokens(st, sol_in, 300, fee_bps=125)
    assert max_sol == (sol_in + sol_in * 125 // 10_000) + ((sol_in + sol_in * 125 // 10_000) * 300) // 10_000
    net, min_sol = pumpfun.quote_sell_sol(st, tokens, 1000, fee_bps=125)
    gross = st["virtual_sol_reserves"] - (st["virtual_sol_reserves"] * st["virtual_token_reserves"]) // (st["virtual_token_reserves"] + tokens)
    assert net == gross - gross * 125 // 10_000 and min_sol == net - net * 1000 // 10_000
    # pumpswap: fee added to max_quote_in
    ps = {"base_reserves": 10 ** 15, "quote_reserves": 50 * 10 ** 9}
    _, ps_max = pumpswap.quote_buy_tokens(ps, sol_in, 300, fee_bps=30)
    assert ps_max == (sol_in + sol_in * 30 // 10_000) + ((sol_in + sol_in * 30 // 10_000) * 300) // 10_000
    # config → module fee sync
    s = BotState.__new__(BotState)
    s.config = BotConfig(protocol_fee_pct_pumpfun=1.0, protocol_fee_pct_pumpswap=0.3)
    s._sync_venue_fees()
    assert pumpfun.FEE_BPS == 100 and pumpswap.TOTAL_FEE_BPS == 30
    s.config = BotConfig()
    s._sync_venue_fees()


# ---------------------------------------------------------------- #9 max-hold clock from persisted entry_time
def test_9_entry_epoch_from_persisted_entry_time():
    from datetime import datetime, timezone, timedelta
    t0 = datetime.now(timezone.utc) - timedelta(seconds=1200)
    assert abs(BotState._entry_epoch({"entry_time": t0.isoformat()}) - t0.timestamp()) < 1e-6
    assert abs(BotState._entry_epoch({"entry_time": t0}) - t0.timestamp()) < 1e-6
    before = time.time()
    assert BotState._entry_epoch({}) >= before - 1
    # a restart (reattach) must not reset the 900 s clock
    age = time.time() - BotState._entry_epoch({"entry_time": t0.isoformat()})
    assert age >= 900


# ---------------------------------------------------------------- #10 blockhash cadence + RPC/day counter
def test_10_refresh_cadence_and_daily_rpc_counter():
    assert landing.refresh_interval_s(True) == 2.0 and landing.refresh_interval_s(False) == 10.0
    st = BotState.__new__(BotState)
    st.active_trades = {}
    st._pending_entry_mints = set()
    assert st._rpc_busy() is False
    st.active_trades = {"m": {}}
    assert st._rpc_busy() is True
    before = solana_client.stats["day_calls"]
    solana_client._count_daily("getBalance")
    solana_client._count_daily("getBalance")
    assert solana_client.stats["day_calls"] == before + 2 and solana_client.stats["day_by_method"]["getBalance"] >= 2
    assert solana_client.stats["day"] == time.strftime("%Y-%m-%d", time.gmtime())
