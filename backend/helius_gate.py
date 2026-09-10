"""
helius_gate — single source-of-truth for the Helius kill switch.

Every module that consumes Helius credits MUST check `is_helius_paused()`
before issuing traffic. The gate is intentionally kept in a tiny, dep-free
module so that `listener.py`, `discovery.py`, `scanner.py`, `bot.py`,
`account_event_bus.py`, and any future caller can all read the same state
without circular-import gymnastics.

State is mutated by:
  - `BotState.load()` on every config reload (reads `BotConfig.helius_tracker_enabled`)
  - `PUT /api/bot/config` when the user flips the toggle
  - `set_paused(bool)` for tests / programmatic control

Default: NOT paused (preserves existing behaviour for users who never
touch the toggle).
"""
from __future__ import annotations

_paused: bool = False            # operator switch (helius_tracker_enabled = False)
_auto_paused: bool = False       # doctor pause: both Solana books paused / inventory halt, no open Solana position
_auto_reason: str = ""


def set_paused(paused: bool) -> None:
    global _paused
    _paused = bool(paused)


def set_auto_paused(paused: bool, reason: str = "") -> bool:
    """Returns True when the auto state flipped."""
    global _auto_paused, _auto_reason
    changed = bool(paused) != _auto_paused
    _auto_paused, _auto_reason = bool(paused), (reason if paused else "")
    return changed


def is_helius_paused() -> bool:
    return _paused or _auto_paused


def snapshot() -> dict:
    return {"paused": is_helius_paused(), "manual": _paused, "auto": _auto_paused, "auto_reason": _auto_reason}
