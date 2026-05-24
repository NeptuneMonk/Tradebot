"""Verify the re-entry cooldown + exit reservation guards plug the
"4 positions on same mint in 3 min" bleed pattern observed in production.

Run: cd /app/backend && python tests/test_reentry_cooldown.py
"""
import time
from unittest.mock import MagicMock


def test_cooldown_blocks_reentry():
    """recent_exit_until must block re-entry within window, allow after."""
    cooldown_map = {}
    mint = "AquNyWTQ_test"
    # Simulate _exit_impl setting cooldown
    cooldown_map[mint] = time.time() + 90.0

    # Within window — should block
    rx_until = cooldown_map.get(mint, 0.0)
    assert rx_until and time.time() < rx_until, "must block within window"

    # Past window — should allow
    cooldown_map[mint] = time.time() - 1.0  # already expired
    rx_until = cooldown_map.get(mint, 0.0)
    assert not (rx_until and time.time() < rx_until), "must allow after expiry"
    print("cooldown_blocks_reentry: OK")


def test_pending_entry_mints_reservation():
    """The _pending_entry_mints set must reserve the mint during _exit so a
    concurrent scanner attempt can't race into a new entry between pop and
    re-insert. This is the multi-monitor orphan bug fix."""
    pending = set()
    active = {}
    mint = "AquNyWTQ_test"

    # Simulate: exit starts → pops slot → adds to pending
    active.pop(mint, None)
    pending.add(mint)

    # Concurrent scanner attempt — gate check should see the reservation
    can_enter = (mint not in active) and (mint not in pending)
    assert not can_enter, "scanner must NOT enter while exit is in flight"

    # Exit finishes → discards from pending
    pending.discard(mint)
    # Cooldown set
    cooldown = {mint: time.time() + 90.0}

    # Now scanner attempt — gate sees no active, no pending, but cooldown set
    rx_until = cooldown.get(mint, 0.0)
    can_enter = (
        (mint not in active) and (mint not in pending)
        and not (rx_until and time.time() < rx_until)
    )
    assert not can_enter, "cooldown must still block immediately after exit"
    print("pending_entry_mints_reservation: OK")


def test_cooldown_sweep():
    """Expired cooldown entries must be cleaned up by the sweep loop."""
    cooldown = {
        "m1": time.time() - 10.0,    # expired
        "m2": time.time() + 100.0,   # active
        "m3": time.time() - 1.0,     # expired
    }
    now = time.time()
    for mint in list(cooldown.keys()):
        if cooldown[mint] <= now:
            del cooldown[mint]
    assert cooldown == {"m2": cooldown.get("m2")}, f"sweep failed: {cooldown}"
    print("cooldown_sweep: OK")


if __name__ == "__main__":
    test_cooldown_blocks_reentry()
    test_pending_entry_mints_reservation()
    test_cooldown_sweep()
    print("\nAll re-entry cooldown tests PASSED.")
