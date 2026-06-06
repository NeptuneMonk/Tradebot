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

_paused: bool = False


def set_paused(paused: bool) -> None:
    global _paused
    _paused = bool(paused)


def is_helius_paused() -> bool:
    return _paused
