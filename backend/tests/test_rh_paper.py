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
    base = dict(enabled=True, rh_paper_enabled=True, paper_entry_latency_ms=0, paper_exit_latency_ms=0, max_trade_usd=10.0, rh_max_trade_usd=10.0)
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
             usd_market_cap=mc if mc is not None else price * 1e9 * 2500.0, last_trade_ms=int(now * 1000),
             net_quote=b.get("net_quote") or 1.5)   # ~$3.7k pool depth so a $5 stake clears the cost gate
    for i in range(buyers):
        w = f"0x{i:040x}"
        b["buyers"].add(w)
        b["buy_events"].append((now - 10, 0.05, w))
    disc.tracking[TOKEN] = b
    return b


async def _enter_and_fill(st, token=TOKEN, **kw):
    """Paper buys are queued and filled from the poll at their fill block — drive that here."""
    await st.rh_paper._enter(token, **kw)
    pb = st.rh_paper.pending_buys.get(token)
    if pb:
        st.rh_paper.resolve_pending_buys(pb["fill_block"])
        for _ in range(3):
            await asyncio.sleep(0)


def enter(st, **kw):
    asyncio.run(_enter_and_fill(st, **kw))


def test_paper_buy_fills_at_fill_block_price_with_own_impact_and_rejects_stale_decisions():
    """Regression 2026-09-07 (MANTA +213% in 1.6s): the paper buy filled at a price that was
    3 s stale while the curve had already graduated. Live would have reverted."""
    st = make_state(paper_exit_latency_ms=600)       # 6 blocks at 100 ms
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=6.289e-9)
    st.rh_discovery.stats["head"] = 100
    V, K0 = 1.68, 1.68e9
    b.update(curve_a=(6.289e-9 * K0) ** 0.5, curve_k0=K0)
    # 1) graduated before the fill block → rejected, slot released
    asyncio.run(st.rh_paper._enter(TOKEN))
    assert TOKEN in st.rh_paper.pending_buys and TOKEN in st.rh_paper._pending_entries
    assert st.rh_paper.pending_buys[TOKEN]["fill_block"] == 106
    b["graduated"] = True
    st.rh_paper.resolve_pending_buys(105)
    assert TOKEN in st.rh_paper.pending_buys            # not landed yet
    st.rh_paper.resolve_pending_buys(106)
    assert TOKEN not in st.rh_paper.pending_buys and TOKEN not in st.rh_paper.positions
    assert TOKEN not in st.rh_paper._pending_entries and st.rh_paper.stats["entries_rejected"] == 1
    # 2) price ran +220% before the fill block → rejected like a live buy past its slippage tolerance
    b["graduated"] = False
    b["block_prices"].append((106, 2.015e-8))
    asyncio.run(st.rh_paper._enter(TOKEN))
    st.rh_paper.resolve_pending_buys(106)
    assert TOKEN not in st.rh_paper.positions and st.rh_paper.stats["entries_rejected"] == 2
    # 3) normal: fills at the fill block's price (+3%) and our own $10 buy nudges the price a little more
    b["block_prices"].clear()
    b["block_prices"].append((106, 6.289e-9 * 1.03))
    enter(st)
    t = st.rh_paper.positions[TOKEN]["trade"]
    assert t["entry_block"] == 106 and abs(t["entry_decision_price_quote"] - 6.289e-9) < 1e-15
    assert t["entry_price_quote"] > 6.289e-9 * 1.03          # own impact on the exact curve
    assert t["entry_price_quote"] < 6.289e-9 * 1.03 * 1.01   # …but tiny for a $10 stake on a $8k curve


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
    assert st.rh_paper._gates(TOKEN, b, now) == "rh-grad-no-pool"   # post-pool entries need a live v4 pool print
    b["graduated"] = False
    b["quote_symbol"] = "NVDA"
    assert st.rh_paper._gates(TOKEN, b, now) == "unpriced-quote"


def test_enter_then_take_profit_exit_math():
    st = make_state(take_profit_pct=20.0)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=2e-9)
    enter(st)
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
    enter(st)
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
    pos["peak_price"] = 1e-9
    assert check(1.0e-9, True, graduated=True) is None    # graduation no longer forces an exit (rides on the pool)
    assert pos["trade"]["venue"] == "pool"
    b["graduated"] = False
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
    enter(st)
    other = "0x" + "e" * 40
    b2 = dict(st.rh_discovery.tracking[TOKEN])
    b2["buyers"] = set(st.rh_discovery.tracking[TOKEN]["buyers"])
    b2["buy_events"] = deque(st.rh_discovery.tracking[TOKEN]["buy_events"])
    assert st.rh_paper._gates(other, b2, now) == "max-positions"


def test_event_driven_stop_fires_on_breaching_trade_and_fills_after_latency():
    """A dump inside one poll batch: SL must trigger on the first breaching
    sell (block-accurate) and fill `latency_blocks` later — NOT at the
    end-of-batch price the 1s tick would have used."""
    st = make_state(stop_loss_pct=12.0, paper_exit_latency_ms=600, exit_momentum_gate_enabled=False)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9, first=5e-10)
    enter(st)
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
    enter(st)
    asyncio.run(st.rh_paper.exit(TOKEN, "manual exit"))
    doc = st.db.trades.docs[next(iter(st.db.trades.docs))]
    assert doc["exit_mode"] == "tick" and doc["exit_reason"] == "manual exit"




def test_rh_momentum_gate_defers_sl_until_buyers_fade_or_budget():
    st = make_state(stop_loss_pct=12.0, no_momentum_exit_enabled=False, hold_max_seconds=999)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9, first=5e-10)
    enter(st)
    pos = st.rh_paper.positions[TOKEN]
    b["last_price_quote"] = 0.85e-9                      # -15%
    b["buy_events"].clear()
    for i in range(4):                                   # 4 fresh buyers → strong
        b["buy_events"].append((time.time() - 1, 0.05, f"0x{i:040x}"))
    assert st.rh_paper._decide_exit(pos, b, time.time()) is None
    assert "_mom_defer_sl" in pos
    pos["_mom_defer_sl"] = time.time() - 25              # budget spent
    assert st.rh_paper._decide_exit(pos, b, time.time()) == "stop_loss"
    pos.pop("_mom_defer_sl", None)
    b["last_price_quote"] = 0.35e-9                      # -65% past hard floor
    assert st.rh_paper._decide_exit(pos, b, time.time()) == "stop_loss"
    b["last_price_quote"] = 0.85e-9
    b["buy_events"].clear()                              # buyers gone
    assert st.rh_paper._decide_exit(pos, b, time.time()) == "stop_loss"


def test_rh_reentry_watch_pullback_and_breakout():
    st = make_state(reentry_enabled=True, reentry_window_seconds=600, reentry_max_attempts=2,
                    reentry_pullback_pct=20.0, reentry_size_multiplier=1.0, take_profit_pct=10.0,
                    exit_momentum_gate_enabled=False, reentry_min_wait_s=0)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9, first=5e-10)
    enter(st)
    b["last_price_quote"] = 1.3e-9
    asyncio.run(st.rh_paper.exit(TOKEN, "take_profit"))
    assert TOKEN in st.rh_paper.watch and TOKEN not in st.rh_paper.positions
    w = st.rh_paper.watch[TOKEN]
    assert abs(w["exit_price_quote"] - 1.3e-9) < 1e-20 and w["chain"] == "rh" and w["attempts"] == 0
    # no move → no re-entry
    st.rh_paper._scan_reentries(time.time())
    assert w["attempts"] == 0
    # a straight dump 25% below exit is NOT a pullback (token never ran on)
    b["buy_events"].append((time.time(), 0.05, "0x" + "b" * 40))
    b["last_price_quote"] = 0.97e-9
    st.rh_paper._scan_reentries(time.time())
    assert w["attempts"] == 0
    # real pullback: run on to +23%, drop 25% from that peak, bounce +4% off the trough with 2 buyers
    b["last_price_quote"] = 1.6e-9
    st.rh_paper._scan_reentries(time.time())
    b["last_price_quote"] = 1.2e-9
    st.rh_paper._scan_reentries(time.time())
    assert w["attempts"] == 0
    b["buy_events"].append((time.time(), 0.05, "0x" + "c" * 40))
    b["last_price_quote"] = 1.25e-9

    async def run():
        st.rh_paper._scan_reentries(time.time())
        await asyncio.sleep(0.05)
        pb = st.rh_paper.pending_buys.get(TOKEN)
        st.rh_paper.resolve_pending_buys(pb["fill_block"])
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert w["attempts"] == 1 and w["last_trigger"] == "pullback"
    assert TOKEN in st.rh_paper.positions
    assert st.rh_paper.positions[TOKEN]["trade"]["classifier_action"] == "rh_pons_reentry"
    # losing exit does NOT create a watch entry
    st.rh_paper.watch.clear()
    b["last_price_quote"] = 0.5e-9
    asyncio.run(st.rh_paper.exit(TOKEN, "stop_loss"))
    assert TOKEN not in st.rh_paper.watch
    # window expiry drops the entry
    st.rh_paper.watch[TOKEN] = dict(w, exit_time=time.time() - 700, attempts=0, window_s=600)  # (hot winners get a 2× window)
    st.rh_paper._scan_reentries(time.time())
    assert TOKEN not in st.rh_paper.watch


def _open_pos(st, b, entry=1e-9):
    st.rh_paper.positions[TOKEN] = {"trade": {"entry_price_quote": entry, "entry_usd": 5.0, "entry_tokens": 1.0},
                                    "peak_price": entry, "_last_price": entry, "opened": time.time() - 5}
    return st.rh_paper.positions[TOKEN]






def test_hot_winner_gets_uncapped_watch_and_focus():
    st = make_state(reentry_enabled=True, reentry_max_attempts=2, reentry_window_seconds=300, reentry_size_multiplier=0.5,
                    hot_token_pnl_pct=25.0, hot_reentry_size_mult=1.5, hot_focus_mode="slow",
                    hot_focus_fresh_cooldown_s=90, hot_focus_reserve_slots=1, rh_max_positions=3)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9)
    enter(st)
    assert st.rh_paper.focus_state(now)["active"] is False
    b["last_price_quote"] = 1.6e-9                 # +60% → hot
    asyncio.run(st.rh_paper.exit(TOKEN, "take_profit"))
    w = st.rh_paper.watch[TOKEN]
    assert w["hot"] and w["max_attempts"] is None and w["window_s"] is None and abs(w["size_multiplier"] - 0.75) < 1e-9
    f = st.rh_paper.focus_state(now)
    assert f["active"] and f["mode"] == "slow" and f["hot"] == [b["symbol"]] and f["reserved_slots"] == 1
    # focus gating for FRESH entries: cooldown after the last fresh entry, then reserved slot
    st.rh_paper._last_fresh_entry_ts = now - 10
    assert st.rh_paper._focus_blocks_fresh(now) == "focus_cooldown"
    st.rh_paper._last_fresh_entry_ts = now - 100
    assert st.rh_paper._focus_blocks_fresh(now) is None
    st.rh_paper._pending_entries.update({"0x1", "0x2"})    # 2 of 3 slots used → the last one is reserved
    assert st.rh_paper._focus_blocks_fresh(now) == "focus_reserved_slot"
    st.rh_paper._pending_entries.clear()
    st.config.hot_focus_mode = "pause"
    assert st.rh_paper._focus_blocks_fresh(now) == "focus_pause"
    st.config.hot_focus_mode = "off"
    assert st.rh_paper._focus_blocks_fresh(now) is None
    board = st.rh_paper.status()
    row = board["hot_board"][0]
    assert row["hot"] and row["attempts_left"] is None and row["seconds_left"] is None and "focus" in board


def test_hot_watch_walks_away_on_lower_lows_and_losing_legs():
    st = make_state(reentry_enabled=True, hot_token_pnl_pct=25.0, hot_walk_lower_lows_n=2, reentry_bounce_confirm_pct=3.0,
                    hot_stagnant_s=100000, hot_weak_bounce_s=100000, hot_breakdown_pct=90.0)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9)
    enter(st)
    b["last_price_quote"] = 1.6e-9
    asyncio.run(st.rh_paper.exit(TOKEN, "take_profit"))
    w = st.rh_paper.watch[TOKEN]
    # zig-zag: 1.6 → 1.4 (low) → 1.55 (lower high) → 1.3 (lower low) → 1.4 (lower high) → 1.2 (lower low) → 1.3
    for px in (1.6e-9, 1.4e-9, 1.55e-9, 1.3e-9, 1.4e-9, 1.2e-9, 1.3e-9):
        b["last_price_quote"] = px
        st.rh_paper._scan_reentries(now + 30)
        if TOKEN not in st.rh_paper.watch:
            break
    assert TOKEN not in st.rh_paper.watch
    d = st.rh_paper.hot_history[0]
    assert d["reason"] == "lower_lows" and d["mint"] == TOKEN and st.rh_paper.stats["hot_walk_aways"] == 1
    # losing legs: a losing re-entry on a hot token is a strike, the second one drops the watch
    b["last_price_quote"] = 1e-9
    st.rh_paper.entered.discard(TOKEN)
    enter(st)
    b["last_price_quote"] = 1.6e-9
    asyncio.run(st.rh_paper.exit(TOKEN, "take_profit"))
    assert st.rh_paper.watch[TOKEN]["hot"]
    for _ in range(2):
        b["last_price_quote"] = 1.5e-9
        st.rh_paper.entered.discard(TOKEN)
        enter(st)
        b["last_price_quote"] = 1.3e-9
        asyncio.run(st.rh_paper.exit(TOKEN, "stop_loss"))
        w = st.rh_paper.watch.get(TOKEN)
        if w:
            assert w["hot"] and w["strikes"] == 1
    assert TOKEN not in st.rh_paper.watch and st.rh_paper.hot_history[0]["reason"] == "losing_legs"


def test_hot_watch_walks_away_when_stagnant_or_weak_bounce():
    from reentry_logic import hot_walk_away_reason
    cfg = dict(hot_walk_lower_lows_n=2, hot_weak_bounce_s=120, hot_weak_bounce_pct=3.0, hot_stagnant_s=180,
               hot_stagnant_range_pct=4.0, hot_breakdown_pct=40.0, reentry_pullback_pct=25.0)
    now = 1000.0
    w = {"peak_price_after_exit": 2e-9, "trough_after_peak": 1.5e-9, "trough_ts": now - 130, "exit_time": now - 60, "pullback_pct": 25.0}
    b = {"buy_events": [(now - 5, 0.01, "0xa")], "price_samples": []}
    assert hot_walk_away_reason(w, b, 1.52e-9, now, cfg) == "weak_bounce"       # 25% dip, 130s near the trough, +1.3% only
    assert hot_walk_away_reason(w, b, 1.6e-9, now, cfg) is None                # bounced +6.7% → still alive
    assert hot_walk_away_reason(w, b, 1.1e-9, now, cfg) == "broke_down"        # 45% under the peak
    w2 = {"peak_price_after_exit": 2e-9, "trough_after_peak": 1.98e-9, "trough_ts": now, "exit_time": now - 200, "pullback_pct": 25.0}
    b2 = {"buy_events": [], "price_samples": [(now - 150, 1.99e-9), (now - 100, 2.0e-9), (now - 50, 1.98e-9)]}
    assert hot_walk_away_reason(w2, b2, 1.99e-9, now, cfg) == "no_buyers"
    b2["buy_events"] = [(now - 5, 0.01, "0xa")]
    assert hot_walk_away_reason(w2, b2, 1.99e-9, now, cfg) == "stagnant"       # 1% range over 180s
    b2["price_samples"].append((now - 10, 2.2e-9))
    assert hot_walk_away_reason(w2, b2, 2.2e-9, now, cfg) is None             # 10% range → trending


def test_pyramid_adds_on_higher_high_while_riding():
    st = make_state(pyramid_step_pct=10.0, pyramid_add_frac=0.5, pyramid_max_adds=2, hold_max_seconds=60)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9)
    enter(st)
    pos = st.rh_paper.positions[TOKEN]
    t = pos["trade"]
    q0, tok0 = t["entry_quote"], t["entry_tokens"]
    pos["_riding"] = True
    pos["peak_price"] = 1.3e-9
    b["last_price_quote"] = 1.35e-9                       # not +10% above the 1.3 peak yet
    st.rh_paper._maybe_pyramid(TOKEN, pos, b, now)
    assert "_pyramiding" not in pos and not t.get("pyramids")
    b["last_price_quote"] = 1.45e-9                       # higher-high confirmed
    asyncio.run(_run_pyramid(st, pos, b))
    assert t["pyramids"] == 1 and abs(t["entry_quote"] - q0 * 1.5) < 1e-12 and t["entry_tokens"] > tok0
    assert 1e-9 < t["entry_price_quote"] < 1.45e-9      # averaged up, below the add price
    assert abs(pos["_pyramid_next"] - 1.45e-9 * 1.1) < 1e-20
    b["last_price_quote"] = 1.7e-9
    asyncio.run(_run_pyramid(st, pos, b))
    assert t["pyramids"] == 2
    b["last_price_quote"] = 2.5e-9                        # cap reached → no third add
    st.rh_paper._maybe_pyramid(TOKEN, pos, b, now)
    assert "_pyramiding" not in pos and t["pyramids"] == 2
    board = st.rh_paper.status()
    assert board["positions"][0]["pyramids"] == 2 and board["positions"][0]["riding"] and "hot_board" in board


async def _run_pyramid(st, pos, b):
    st.rh_paper._maybe_pyramid(TOKEN, pos, b, time.time())
    for _ in range(4):
        await asyncio.sleep(0)
