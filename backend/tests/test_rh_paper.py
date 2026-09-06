"""RH PONS paper trader — fee model, gates, entry/exit sim, isolation."""
import asyncio
import os
import sys
import time
from collections import deque
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
os.environ.setdefault("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")

import rh_discovery as rh  # noqa: E402
import rh_paper as rp  # noqa: E402
from models import BotConfig  # noqa: E402
from pl_sources import classify_source, SOURCE_LABELS  # noqa: E402

TOKEN = "0xa027116861ce778bbf5ba8b14d06ebfdbfd4f8db"


class FakeCol:
    def __init__(self):
        self.docs = {}

    async def update_one(self, flt, upd, upsert=False):
        _id = flt.get("_id")
        if _id is None:
            return
        cur = self.docs.setdefault(_id, {})
        cur.update(upd.get("$set", {}))
        cur.update(upd.get("$setOnInsert", {}) if _id not in self.docs or not cur else {})

    def find(self, *a, **k):
        class _C:
            async def to_list(self_inner, n):
                return []
        return _C()


def make_state(**cfg):
    base = dict(enabled=True, rh_paper_enabled=True, paper_entry_latency_ms=0, paper_exit_latency_ms=0, max_trade_usd=10.0)
    base.update(cfg)
    st = SimpleNamespace(
        config=BotConfig(**base),
        db=SimpleNamespace(launches=FakeCol(), trades=FakeCol()),
        tracking={}, active_trades={}, entered_mints=set(),
    )
    st.rh_discovery = rh.RHDiscovery(st)
    st.rh_paper = rp.RHPaperTrader(st)
    rh._eth_usd_cache.update(price=2500.0, ts=time.time())
    return st


def hot_bucket(disc, now, *, age_s=60, price=2e-9, first=1e-9, buyers=12, curve=30.0, mc=None, quote="ETH"):
    b = disc._new_bucket({"token": TOKEN, "curve": "0x" + "c" * 40, "deployer": "0x" + "d" * 40,
                          "pair_token": "0x" + "0" * 40, "quote_symbol": quote, "quote_decimals": 18,
                          "graduation_threshold": 4.2, "block": 1}, start=now - age_s)
    b.update(symbol="TST", name="Test", first_price_quote=first, last_price_quote=price, curve_fill_pct=curve,
             usd_market_cap=mc if mc is not None else price * 1e9 * 2500.0, last_trade_ms=int(now * 1000))
    for i in range(buyers):
        w = f"0x{i:040x}"
        b["buyers"].add(w)
        b["buy_events"].append((now - 10, 0.05, w))
    disc.tracking[TOKEN] = b
    return b


def test_snipe_tax_schedule():
    assert rp.snipe_tax_bps(0.0) == 9900
    assert rp.snipe_tax_bps(1.0) == 618
    assert rp.snipe_tax_bps(2.0) == 19
    assert rp.snipe_tax_bps(3.0) == 0
    assert abs(rp.fee_fraction(10.0) - 0.01) < 1e-9


def test_gates_pass_and_reasons():
    st = make_state()
    now = time.time()
    b = hot_bucket(st.rh_discovery, now)
    assert st.rh_paper._gates(TOKEN, b, now) is None
    b["curve_fill_pct"] = 95.0
    assert st.rh_paper._gates(TOKEN, b, now) == "curve"
    b["curve_fill_pct"] = 30.0
    b["start"] = now - 1
    assert st.rh_paper._gates(TOKEN, b, now) == "age"
    b["start"] = now - 60
    b["graduated"] = True
    assert st.rh_paper._gates(TOKEN, b, now) == "graduated"
    b["graduated"] = False
    b["quote_symbol"] = "NVDA"
    assert st.rh_paper._gates(TOKEN, b, now) == "unpriced-quote"


def test_enter_then_take_profit_exit_math():
    st = make_state(take_profit_pct=20.0)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=2e-9)
    asyncio.run(st.rh_paper._enter(TOKEN))
    assert TOKEN in st.rh_paper.positions
    t = st.rh_paper.positions[TOKEN]["trade"]
    assert t["chain"] == "rh" and t["mode"] == "paper" and t["classifier_action"] == "rh_pons_paper"
    assert abs(t["entry_usd"] - 10.0) < 1e-9
    stake_quote = 10.0 / 2500.0
    assert abs(t["entry_tokens"] - stake_quote * 0.99 / 2e-9) < 1e-6
    assert TOKEN not in st.active_trades, "RH paper positions must never enter BotState.active_trades"
    # +30% move → TP
    b["last_price_quote"] = 2.6e-9
    assert st.rh_paper._decide_exit(st.rh_paper.positions[TOKEN], b, time.time()) == "take_profit"
    asyncio.run(st.rh_paper.exit(TOKEN, "take_profit"))
    doc = st.db.trades.docs[t["id"]]
    assert doc["status"] == "closed" and doc["exit_reason"] == "take_profit"
    expected_exit = t["entry_tokens"] * 2.6e-9 * 0.99 * 2500.0 - rp.RH_GAS_USD
    assert abs(doc["exit_usd"] - expected_exit) < 1e-6
    assert doc["pnl_usd"] > 0 and abs(doc["pnl_pct"] - (doc["pnl_usd"] / 10.0 * 100)) < 1e-6
    assert TOKEN not in st.rh_paper.positions


def test_stop_loss_trailing_graduation_and_hold():
    st = make_state(stop_loss_pct=12.0, trailing_arm_pct=12.0, trailing_stop_pct=6.0, hold_max_seconds=35,
                    no_momentum_exit_enabled=False)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9, first=5e-10)
    asyncio.run(st.rh_paper._enter(TOKEN))
    pos = st.rh_paper.positions[TOKEN]

    def check(price, expect, graduated=False, opened_ago=0):
        pos["opened"] = time.time() - opened_ago
        b["graduated"] = graduated
        b["last_price_quote"] = price
        return st.rh_paper._decide_exit(pos, b, time.time())

    assert check(0.95e-9, False) is None                  # -5%: hold
    assert check(0.87e-9, True) == "stop_loss"            # -13%
    pos["peak_price"] = 1e-9
    assert check(1.15e-9, False) is None                  # +15% arms trail
    assert check(1.07e-9, True) == "trailing_stop"        # 7% off peak
    assert check(1.0e-9, True, graduated=True) == "graduated"
    pos["peak_price"] = 1e-9
    assert check(1.0e-9, True, opened_ago=40) == "max_hold"


def test_inactive_when_bot_stopped_or_toggle_off():
    st = make_state()
    assert st.rh_paper._active() is True
    st.config = BotConfig(enabled=False, rh_paper_enabled=True)
    assert st.rh_paper._active() is False
    st.config = BotConfig(enabled=True, rh_paper_enabled=False)
    assert st.rh_paper._active() is False
    assert BotConfig().rh_paper_enabled is False


def test_pl_source_classification():
    assert classify_source("rh_pons_paper") == "rh_pons"
    assert SOURCE_LABELS["rh_pons"] == "RH · PONS (paper)"
    assert classify_source("momentum_new") == "new"


def test_max_positions_gate():
    st = make_state(rh_max_positions=1)
    now = time.time()
    hot_bucket(st.rh_discovery, now)
    asyncio.run(st.rh_paper._enter(TOKEN))
    other = "0x" + "e" * 40
    b2 = dict(st.rh_discovery.tracking[TOKEN])
    b2["buyers"] = set(st.rh_discovery.tracking[TOKEN]["buyers"])
    b2["buy_events"] = deque(st.rh_discovery.tracking[TOKEN]["buy_events"])
    assert st.rh_paper._gates(other, b2, now) == "max-positions"


def test_event_driven_stop_fires_on_breaching_trade_and_fills_after_latency():
    """A dump inside one poll batch: SL must trigger on the first breaching
    sell (block-accurate) and fill `latency_blocks` later — NOT at the
    end-of-batch price the 1s tick would have used."""
    st = make_state(stop_loss_pct=12.0, paper_exit_latency_ms=600)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9, first=5e-10)
    asyncio.run(st.rh_paper._enter(TOKEN))
    pos = st.rh_paper.positions[TOKEN]
    assert st.rh_paper.latency_blocks() == 6
    disc = st.rh_discovery

    def sell(block, price):
        tr = {"side": "sell", "wallet": "0x" + "9" * 40, "quote": price * 1e6, "tokens": 1e6, "price": price, "block": block}
        disc.apply_trade(b, tr, now)
        st.rh_paper.on_trade(TOKEN, b, tr, now)

    sell(1000, 0.95e-9)            # -5%  hold
    assert "exit_trigger" not in pos
    sell(1001, 0.85e-9)            # -15% breach → trigger here
    trig = pos["exit_trigger"]
    assert trig["reason"] == "stop_loss" and trig["block"] == 1001 and trig["fill_block"] == 1007
    sell(1004, 0.70e-9)            # dump continues (within latency window)
    sell(1010, 0.30e-9)            # after fill block — must NOT affect fill
    assert pos["_exiting"] is True and "exit_trigger" in pos   # no second trigger
    # head hasn't reached fill block yet → nothing resolves
    async def run():
        st.rh_paper.resolve_pending(1005)
        assert "_fill_task" not in pos
        # head past fill block → fills at last price at/before block 1007 (=0.70e-9)
        st.rh_paper.resolve_pending(1012)
        assert pos.get("_fill_task") is True
        await asyncio.sleep(0.05)
    asyncio.run(run())
    doc = st.db.trades.docs[pos["trade"]["id"]]
    assert doc["exit_mode"] == "event" and doc["exit_trigger_block"] == 1001 and doc["exit_fill_block"] == 1007
    assert abs(doc["exit_price_quote"] - 0.70e-9) < 1e-20
    assert abs(doc["exit_trigger_price_quote"] - 0.85e-9) < 1e-20
    # realised loss reflects 0.70 fill (-30% gross) rather than 0.30 (-70%)
    assert -35 < doc["pnl_pct"] < -28


def test_tick_exit_marked_and_event_path_ignored_when_no_position():
    st = make_state()
    now = time.time()
    b = hot_bucket(st.rh_discovery, now)
    # no position → on_trade is a no-op
    st.rh_paper.on_trade(TOKEN, b, {"side": "sell", "price": 1e-12, "block": 5}, now)
    assert TOKEN not in st.rh_paper.positions
    asyncio.run(st.rh_paper._enter(TOKEN))
    asyncio.run(st.rh_paper.exit(TOKEN, "manual exit"))
    doc = st.db.trades.docs[next(iter(st.db.trades.docs))]
    assert doc["exit_mode"] == "tick" and doc["exit_reason"] == "manual exit"


def test_no_momentum_exit_one_shot():
    st = make_state(no_momentum_after_s=30, no_momentum_min_mfe_pct=5.0, stop_loss_pct=50, hold_max_seconds=999)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9, first=5e-10)
    asyncio.run(st.rh_paper._enter(TOKEN))
    pos = st.rh_paper.positions[TOKEN]
    # 20s in, flat → not yet
    pos["opened"] = time.time() - 20
    b["last_price_quote"] = 1.01e-9
    assert st.rh_paper._decide_exit(pos, b, time.time()) is None
    # 31s in, peak only +1% → no_momentum
    pos["opened"] = time.time() - 31
    assert st.rh_paper._decide_exit(pos, b, time.time()) == "no_momentum"
    # a position that DID move (+8% peak) passes and is never re-checked
    pos2 = {"trade": dict(pos["trade"]), "peak_price": 1.08e-9, "_last_price": 1.0e-9, "opened": time.time() - 31}
    assert st.rh_paper._decide_exit(pos2, b, time.time()) is None
    assert pos2["_nm_checked"] is True
    b["last_price_quote"] = 0.99e-9
    pos2["opened"] = time.time() - 60
    assert st.rh_paper._decide_exit(pos2, b, time.time()) is None
    # disabled
    st.config = BotConfig(enabled=True, rh_paper_enabled=True, no_momentum_exit_enabled=False, stop_loss_pct=50, hold_max_seconds=999)
    pos3 = {"trade": dict(pos["trade"]), "peak_price": 1e-9, "_last_price": 1e-9, "opened": time.time() - 90}
    assert st.rh_paper._decide_exit(pos3, b, time.time()) is None
