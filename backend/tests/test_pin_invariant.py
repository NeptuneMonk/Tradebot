"""
Tests for the pin invariant (option C, 2026-05-30):

    PINNED ⇔ this trade follows the SNIPE LADDER

The snipe ladder fires when EITHER:
  (a) action == "greylist_snipe", OR
  (b) the entry is on a greylisted creator with a tradeable pattern
      (slow_rug_tradeable, predictable_dump_tradeable, fake_hype_tradeable,
       bimodal_tradeable) — even if the entry path was momentum_new /
      momentum_seasoned / reentry.

Tokens with `unknown` / `unpredictable_rug` patterns DO NOT inherit the
snipe ladder (we have no stable pattern anchors).
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

from bot import _make_snipe_ctx, SNIPE_LADDER_PATTERNS  # noqa: E402


def _ctx(pattern=None, peak_mc=200_000, rug_curve=70):
    return {
        "pattern": pattern,
        "expected_peak_mc_usd": peak_mc,
        "expected_peak_mc_stddev": 30_000,
        "expected_rug_curve_pct": rug_curve,
    }


# -----------------------------------------------------------------------
# Direct snipe action: always gets the ladder
# -----------------------------------------------------------------------

def test_snipe_action_with_known_pattern_gets_ctx():
    """An explicit greylist_snipe entry with a known pattern populates ctx."""
    ctx = _make_snipe_ctx(_ctx(pattern="slow_rug_tradeable"), "greylist_snipe")
    assert ctx is not None
    assert ctx["pattern"] == "slow_rug_tradeable"
    assert ctx["expected_peak_mc_usd"] == 200_000
    assert ctx["via_action"] == "greylist_snipe"


def test_snipe_action_with_unknown_pattern_still_gets_ctx():
    """An explicit greylist_snipe entry ALWAYS gets ctx (the action itself
    is the highest-priority signal). Pattern can be unknown."""
    ctx = _make_snipe_ctx(_ctx(pattern="unknown"), "greylist_snipe")
    assert ctx is not None
    assert ctx["pattern"] == "unknown"
    assert ctx["via_action"] == "greylist_snipe"


def test_research_snipe_gets_ctx_with_unpredictable_pattern():
    """Research snipes fire on `unpredictable_rug` creators. They MUST
    still get ctx — the snipe ladder's profit ripcord / stale exit /
    velocity decay all work without expected_peak_mc_usd."""
    ctx = _make_snipe_ctx(_ctx(pattern="unpredictable_rug"), "greylist_snipe")
    assert ctx is not None
    assert ctx["pattern"] == "unpredictable_rug"


# -----------------------------------------------------------------------
# Momentum/reentry on greylisted creators: inherits ladder for tradeable patterns
# -----------------------------------------------------------------------

def test_momentum_new_on_greylisted_creator_inherits_snipe_ladder():
    """The Elon-bug scenario: scanner momentum_new entry on a greylisted
    creator with a known tradeable pattern now inherits the snipe ladder
    (option C, 2026-05-30). Previously this fell through to standard
    SL/TP exits and the user saw it exit at 10% TP while the card was
    pinned, which they reasonably reported as "the snipe ladder is being
    overridden by std rules"."""
    for pattern in SNIPE_LADDER_PATTERNS:
        ctx = _make_snipe_ctx(_ctx(pattern=pattern), "momentum_new")
        assert ctx is not None, f"pattern {pattern!r} should inherit ladder"
        assert ctx["pattern"] == pattern
        assert ctx["via_action"] == "momentum_new"


def test_momentum_seasoned_on_greylisted_creator_inherits_ladder():
    """Same rule applies to the Seasoned band."""
    ctx = _make_snipe_ctx(_ctx(pattern="predictable_dump_tradeable"), "momentum_seasoned")
    assert ctx is not None
    assert ctx["via_action"] == "momentum_seasoned"


def test_reentry_on_greylisted_creator_inherits_ladder():
    """Re-entries on greylisted creators (rare path since snipe winners
    no longer trigger reentry — see test_greylist_snipe_lifecycle.py —
    but a momentum-winner reentry on a greylisted creator still flows
    through) get the pattern ladder."""
    ctx = _make_snipe_ctx(_ctx(pattern="bimodal_tradeable"), "reentry")
    assert ctx is not None


# -----------------------------------------------------------------------
# Non-tradeable patterns: standard exits apply
# -----------------------------------------------------------------------

def test_momentum_on_unknown_pattern_creator_uses_standard_exits():
    """A momentum entry on a creator the greylist has classified as
    `unknown` (insufficient data) does NOT inherit the snipe ladder —
    we have no pattern anchors to drive peak-MC / curve-fill exits."""
    ctx = _make_snipe_ctx(_ctx(pattern="unknown"), "momentum_new")
    assert ctx is None


def test_momentum_on_unpredictable_rug_creator_uses_standard_exits():
    """Same for `unpredictable_rug` — no stable anchor → standard exits.
    Research-mode snipes get the ladder via the action gate; momentum
    entries on the same creator do NOT (action != greylist_snipe AND
    pattern is not tradeable)."""
    ctx = _make_snipe_ctx(_ctx(pattern="unpredictable_rug"), "momentum_new")
    assert ctx is None


def test_momentum_on_non_greylisted_creator_uses_standard_exits():
    """Standard creators (no greylist record) → pattern is None →
    standard exits."""
    ctx = _make_snipe_ctx(_ctx(pattern=None), "momentum_new")
    assert ctx is None
    ctx = _make_snipe_ctx({}, "momentum_new")
    assert ctx is None


# -----------------------------------------------------------------------
# Pin invariant test (mirrors the gate in bot.py::_enter_impl)
# -----------------------------------------------------------------------

def _should_pin(action: str, greylist_ctx: dict | None) -> bool:
    """Snipe-ladder gate (feed pinning itself was removed on operator request — nothing is pinned any more):
        pattern exits apply ⇔ snipe_pattern_ctx is not None
    """
    return _make_snipe_ctx(greylist_ctx or {}, action) is not None


def test_snipe_ladder_gate():
    """Which fills inherit the snipe ladder (formerly also the pin gate)."""
    # Snipes always pinned
    assert _should_pin("greylist_snipe", _ctx(pattern="slow_rug_tradeable"))
    assert _should_pin("greylist_snipe", _ctx(pattern="unknown"))
    # Momentum on greylisted-tradeable: PINNED (option C)
    assert _should_pin("momentum_new", _ctx(pattern="slow_rug_tradeable"))
    assert _should_pin("momentum_seasoned", _ctx(pattern="fake_hype_tradeable"))
    # Momentum on non-tradeable greylist: NOT pinned
    assert not _should_pin("momentum_new", _ctx(pattern="unknown"))
    assert not _should_pin("momentum_new", _ctx(pattern="unpredictable_rug"))
    # Momentum on non-greylisted creator: NOT pinned
    assert not _should_pin("momentum_new", {})
    assert not _should_pin("momentum_new", _ctx(pattern=None))
    # Reentry: same rules
    assert _should_pin("reentry", _ctx(pattern="predictable_dump_tradeable"))
    assert not _should_pin("reentry", _ctx(pattern="unknown"))
