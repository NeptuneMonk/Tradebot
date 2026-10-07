"""Inventory halt: configurable trigger/window, operator LIFT, and the Controls switch that stops it blocking entries."""
import asyncio
import time

from inventory import InventoryHalt, is_loss_exit
from models import BotConfig


def test_loss_exit_markers():
    assert is_loss_exit("stop-loss -12%") and is_loss_exit("rip-cord") and is_loss_exit("rug")
    assert not is_loss_exit("profit rip-cord") and not is_loss_exit("target 1R") and not is_loss_exit("manual")


def test_configurable_trigger_and_window():
    cfg = BotConfig(inventory_halt_n=3, inventory_halt_window_min=10)
    h = InventoryHalt()
    h.configure(cfg)
    assert h.n == 3 and h.window_s == 600
    assert not h.record_close("stop-loss") and not h.record_close("stop-loss")
    assert h.record_close("stop-loss") is True and h.active()
    assert h.snapshot()["trigger_n"] == 3 and h.snapshot()["window_min"] == 10


def test_lift_clears_halt_and_resets_streak():
    h = InventoryHalt()
    h.configure(BotConfig(inventory_halt_n=2))
    h.record_close("rug"); assert h.record_close("rug") and h.active()
    h.lift()
    assert not h.active() and h.snapshot()["recent_loss_closes"] == 0 and h.snapshot()["lifted"] == 1
    assert h.record_close("stop-loss") is False          # fresh count after a lift


def test_switch_off_means_halt_never_blocks_entry(monkeypatch):
    import bot as bot_mod
    from bot import BotState
    from models import Launch

    class _DB:
        pass
    st = BotState(_DB())
    st.config = BotConfig(inventory_halt_enabled=False, book_scalp_enabled=True)
    st.inventory.halted_until = time.time() + 3600        # a halt is "active" but the switch is off
    monkeypatch.setattr(bot_mod.reputation, "configured", lambda: False)
    reached = []

    async def fence(*a, **k):
        return True
    monkeypatch.setattr(st, "leader_fence", fence)
    monkeypatch.setattr(st, "search_regime_block", lambda: None)

    async def impl(*a, **k):
        reached.append(1)
    monkeypatch.setattr(st, "_enter_impl", impl)
    monkeypatch.setattr(st, "_skip_event", fence)
    import helius_gate
    monkeypatch.setattr(helius_gate, "is_helius_paused", lambda: False)
    launch = Launch(mint="M" * 44, creator="C" * 44, bonding_curve="B" * 44, name="x", symbol="x")
    asyncio.run(st._enter(launch, 30, "momentum_new"))
    assert reached, "entry must proceed when the inventory-halt switch is off"
    reached.clear()
    st.config.inventory_halt_enabled = True
    asyncio.run(st._enter(launch, 30, "momentum_new"))
    assert not reached, "switch on + active halt blocks the entry"


def test_pnl_stop_trips_from_armed_baseline_and_flattens(monkeypatch):
    """PnL stop: realised (since armed) + open PnL ≤ −limit → bot off, kill switch with reason, positions flattened."""
    import asyncio, time
    import bot as bot_mod
    from bot import BotState

    class _Agg:
        def __init__(self, v): self.v = v
        async def to_list(self, n): return [{"_id": None, "pnl": self.v}]

    class _Trades:
        realized = -12.0
        def aggregate(self, pipeline): return _Agg(self.realized)

    class _DB:
        trades = _Trades()
    st = BotState(_DB())
    st.config = BotConfig(pnl_stop_usd=20.0, pnl_stop_armed_ts=time.time() - 600, live_trading=False)
    st.active_trades = {"m1": {"trade": {"id": "t1", "mode": "paper"}, "live_pnl_usd": -5.0}}
    exits = []

    async def fake_exit(mint, reason=None, **k): exits.append((mint, reason))
    monkeypatch.setattr(st, "_exit", fake_exit)

    async def noop(*a, **k): return None
    monkeypatch.setattr(st, "save_enabled", noop)
    monkeypatch.setattr(bot_mod.hub, "broadcast", noop)
    snap = asyncio.run(st.pnl_stop_snapshot())
    assert snap["armed"] and snap["pnl_usd"] == -17.0 and snap["mode"] == "paper"
    assert asyncio.run(st.check_pnl_stop()) is False                       # −17 > −20: still inside the budget
    _DB.trades.realized = -16.0
    assert asyncio.run(st.check_pnl_stop()) is True
    assert st.kill_switch_tripped and st.config.enabled is False and "PnL stop" in st.kill_switch_reason
    assert exits == [("m1", "pnl stop")]
    st2 = BotState(_DB()); st2.config = BotConfig(pnl_stop_usd=0.0)       # 0 = disarmed, never trips
    assert asyncio.run(st2.pnl_stop_snapshot())["armed"] is False and asyncio.run(st2.check_pnl_stop()) is False


def test_breakers_follow_your_target_and_switch():
    import asyncio
    import live_doctor as ld_mod
    from live_doctor import LiveDoctor

    class _BS:
        config = BotConfig()
    ld = LiveDoctor(db=None, bot_state=_BS(), hub=None)
    _BS.config.book_exits = {"scalp": {"stop_loss_pct": 8, "target_r": 0.4}}
    rows = []
    for i in range(ld_mod.BREAKER_MIN_N):
        win = i % 2 == 0
        rows.append({"trade": {"book": "scalp", "exit_time": "2999-01-01T00:00:00+00:00", "pnl_usd": 4.0 if win else -8.0,
                               "entry_usd": 100.0, "r_usd": 10.0, "mfe_pct": 5.0}})
    out = asyncio.run(ld._evaluate_book_breakers(rows))
    s = out["scalp"]
    assert abs(s["payoff"] - 0.5) < 1e-9 and s["paused"] is False          # payoff 0.5 ≥ your 0.4R target: no bench
    _BS.config.book_exits = {"scalp": {"stop_loss_pct": 8, "target_r": 1.5}}
    out = asyncio.run(ld._evaluate_book_breakers(rows))
    assert out["scalp"]["paused"] is True and "1.00" in out["scalp"]["reason"]   # same tape, 1.5R target: judged against 1.0
    _BS.config.live_doctor_breakers_enabled = False
    out = asyncio.run(ld._evaluate_book_breakers(rows))
    assert out == {"disabled": True} and not ld.book_paused_until              # switch off lifts the pause
