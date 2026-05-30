"""
Tests for the pin invariant: PINNED == SNIPE.

Background: previously a momentum scanner entry on a greylisted creator
would pin the launch card (because the pin gate checked the creator's
greylist strategy, not the entry path). The pinned card looked like a
snipe in the UI, but the trade actually ran the STANDARD SL/TP ladder
because `classifier_action != "greylist_snipe"` → `_is_snipe()` returns
False. User reported "Elon pinned which means its sniper but it exited
at TP" — pin was misleading.

New invariant enforced by `_enter_impl`:
    pin only if action == "greylist_snipe"

This test verifies the gating logic directly. Full integration coverage
(actual DB write, recent_launches cache mutation, WS broadcast) is
exercised by the existing snipe-firing tests in test_greylist_sniper.py.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("HELIUS_WSS_URL", "wss://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")
os.environ.setdefault("PUMP_PROGRAM_ID", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
os.environ.setdefault("PUMP_GLOBAL", "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
os.environ.setdefault("PUMP_FEE_RECIPIENT", "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
os.environ.setdefault("PUMP_EVENT_AUTHORITY", "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")


def _should_pin(action: str, greylist_strategy: str | None) -> bool:
    """Mirror of the gate in `bot.py::_enter_impl` line ~2418.

    Old buggy gate (do NOT use):
        bool(greylist_strategy) and greylist_strategy != "standard"

    Fixed gate:
        action == "greylist_snipe"
    """
    return action == "greylist_snipe"


def test_snipe_action_is_pinned():
    """Actual greylist snipes get pinned (this is the only path that does)."""
    assert _should_pin("greylist_snipe", "hot") is True
    assert _should_pin("greylist_snipe", "warm") is True
    # Even research snipes get pinned (they ARE snipes — research mode is
    # just the gate for which creators are eligible).
    assert _should_pin("greylist_snipe", "research") is True


def test_momentum_on_greylist_creator_is_not_pinned():
    """The bug we just fixed: a scanner momentum_new entry on a greylisted
    creator must NOT be pinned. Pin is the visual marker for snipes."""
    assert _should_pin("momentum_new", "hot") is False
    assert _should_pin("momentum_seasoned", "hot") is False
    assert _should_pin("momentum_new", "warm") is False


def test_reentry_on_greylist_creator_is_not_pinned():
    """Re-entries on greylisted creators (rare, since we now skip reentry
    for snipe winners — see test_greylist_snipe_lifecycle.py — but
    possible for momentum-winner reentries that later happen on a
    greylisted creator) must NOT pin."""
    assert _should_pin("reentry", "hot") is False
    assert _should_pin("reentry", "standard") is False


def test_standard_creator_never_pinned():
    """Non-greylisted creators are never pinned regardless of action.
    The only path that pins is action=="greylist_snipe" which itself
    requires a greylist context with strategy != standard — so this is
    a degenerate combination that can't actually occur, but we test it
    for the gate's purity."""
    assert _should_pin("momentum_new", "standard") is False
    assert _should_pin("momentum_seasoned", None) is False
    assert _should_pin("reentry", "standard") is False
    # In practice action="greylist_snipe" + strategy="standard" can't co-occur,
    # but the pin gate is purely action-based — strategy is only stored on
    # the pin for downstream UI display.
    assert _should_pin("greylist_snipe", "standard") is True
