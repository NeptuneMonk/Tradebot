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
