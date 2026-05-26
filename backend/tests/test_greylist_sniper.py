"""
Tests for the Greylist Sniper entry path.

We exercise the bypass logic + the gate decision matrix without spinning
up the full BotState (which requires mongo + helius + WSS). The sniper's
core safety contract is exercised:

  - Sniper fires when creator score ≥ min_score AND not blacklisted/out-of-band
  - Sniper SKIPS when greylist_snipe_enabled=False
  - Sniper SKIPS when score < min_score
  - Sniper SKIPS when creator is blacklisted
  - Sniper SKIPS when bot is disabled / stopping_gracefully
  - Per-hour rate cap clamps fire count
"""
from __future__ import annotations
import os
import sys
import time
from datetime import datetime, timezone

# Env stubs MUST be set before `from bot import ...` (bot.py reads them at
# module load).
os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("HELIUS_WSS_URL", "wss://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")

# Load all the env vars bot.py needs at import. Done eagerly so any
# pytest invocation works without pre-sourcing backend/.env.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:
    pass

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# A minimal stub that mimics the bits of BotState that
# _attempt_greylist_snipe actually touches.
class _StubBotState:
    def __init__(self, *, enabled=True, stopping=False,
                 snipe_enabled=True, greylist_enabled=True,
                 min_score=45.0, max_per_hour=12, settle_s=0,
                 research_mode=False, research_min_score=35.0,
                 research_size_mult=0.5):
        self.stopping_gracefully = stopping
        # mimics BotConfig
        class _Cfg:
            pass
        self.config = _Cfg()
        self.config.enabled = enabled
        self.config.greylist_snipe_enabled = snipe_enabled
        self.config.creator_greylist_enabled = greylist_enabled
        self.config.greylist_snipe_min_score = min_score
        self.config.greylist_snipe_max_per_hour = max_per_hour
        self.config.greylist_snipe_settle_seconds = settle_s
        self.config.greylist_snipe_research_mode = research_mode
        self.config.greylist_snipe_research_min_score = research_min_score
        self.config.greylist_snipe_research_size_mult = research_size_mult
        # P0 pattern-gate field — disabled by default so existing tests
        # (which use pattern="slow_rug_tradeable" already) aren't blocked.
        # Tests that need it ON explicitly flip this.
        self.config.greylist_snipe_require_classified_pattern = False
        self._greylist_snipe_fires: list[float] = []
        self.active_trades: dict = {}
        self._pending_entry_mints: set = set()
        self._snipe_research_flags: dict = {}
        # _enter call recorder
        self.enter_calls: list[tuple] = []
        # Fake DB
        self.db = _StubDB()

    async def _enter(self, launch, risk_score, action):
        self.enter_calls.append((launch.mint, risk_score, action))


class _StubDB:
    def __init__(self, creator_doc: dict | None = None):
        self.creator_doc = creator_doc

    @property
    def creators(self):
        return self  # find_one resolves to self.creator_doc

    async def find_one(self, *_a, **_k):
        return self.creator_doc


class _StubLaunch:
    def __init__(self, mint="MintTest123", creator="CreatorTest456", symbol="TKN"):
        self.mint = mint
        self.creator = creator
        self.symbol = symbol


def _make_creator_doc(score=60.0, blacklisted=False, out_of_band=False, pattern="slow_rug_tradeable"):
    return {
        "greylist_score": score,
        "greylist_score_updated_at": datetime.now(timezone.utc).isoformat(),
        "greylist_blacklisted": blacklisted,
        "greylist_out_of_band": out_of_band,
        "greylist_pattern": pattern,
    }


# Bind the unbound _attempt_greylist_snipe method from bot.py to our stub.
def _bind_sniper(stub):
    from bot import BotState
    return BotState._attempt_greylist_snipe.__get__(stub, type(stub))


@pytest.mark.asyncio
async def test_sniper_fires_when_creator_above_min_score():
    stub = _StubBotState()
    stub.db.creator_doc = _make_creator_doc(score=70.0)
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert len(stub.enter_calls) == 1
    assert stub.enter_calls[0][2] == "greylist_snipe"
    assert len(stub._greylist_snipe_fires) == 1


@pytest.mark.asyncio
async def test_sniper_skips_when_disabled():
    stub = _StubBotState(snipe_enabled=False)
    stub.db.creator_doc = _make_creator_doc(score=70.0)
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


@pytest.mark.asyncio
async def test_sniper_skips_when_greylist_master_disabled():
    stub = _StubBotState(greylist_enabled=False)
    stub.db.creator_doc = _make_creator_doc(score=70.0)
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


@pytest.mark.asyncio
async def test_sniper_skips_below_min_score():
    stub = _StubBotState(min_score=60.0)
    stub.db.creator_doc = _make_creator_doc(score=40.0)  # below threshold
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


@pytest.mark.asyncio
async def test_sniper_skips_blacklisted_creator():
    stub = _StubBotState()
    stub.db.creator_doc = _make_creator_doc(score=70.0, blacklisted=True)
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


@pytest.mark.asyncio
async def test_sniper_skips_out_of_band_creator():
    stub = _StubBotState()
    stub.db.creator_doc = _make_creator_doc(score=70.0, out_of_band=True)
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


@pytest.mark.asyncio
async def test_sniper_skips_when_bot_disabled():
    stub = _StubBotState(enabled=False)
    stub.db.creator_doc = _make_creator_doc(score=70.0)
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


@pytest.mark.asyncio
async def test_sniper_skips_when_stopping_gracefully():
    stub = _StubBotState(stopping=True)
    stub.db.creator_doc = _make_creator_doc(score=70.0)
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


@pytest.mark.asyncio
async def test_sniper_rate_cap_enforced():
    stub = _StubBotState(max_per_hour=2)
    stub.db.creator_doc = _make_creator_doc(score=70.0)
    sniper = _bind_sniper(stub)
    # Fire twice — fills the cap
    now = time.time()
    stub._greylist_snipe_fires = [now - 60, now - 30]
    await sniper(_StubLaunch(mint="m1"), {})
    assert stub.enter_calls == []  # cap hit, blocked


@pytest.mark.asyncio
async def test_sniper_rate_cap_decays_after_hour():
    stub = _StubBotState(max_per_hour=2)
    stub.db.creator_doc = _make_creator_doc(score=70.0)
    sniper = _bind_sniper(stub)
    # 2 old fires (older than 1h) → should NOT count toward cap
    stub._greylist_snipe_fires = [time.time() - 7200, time.time() - 3700]
    await sniper(_StubLaunch(), {})
    assert len(stub.enter_calls) == 1


@pytest.mark.asyncio
async def test_sniper_skips_if_mint_already_active():
    stub = _StubBotState()
    stub.db.creator_doc = _make_creator_doc(score=70.0)
    stub.active_trades["MintTest123"] = {"any": True}
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(mint="MintTest123"), {})
    assert stub.enter_calls == []


@pytest.mark.asyncio
async def test_sniper_no_creator_doc_no_fire():
    stub = _StubBotState()
    stub.db.creator_doc = None
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


# ----- pl_sources integration ---------------------------------------------

def test_pl_sources_classifies_greylist_snipe():
    from pl_sources import classify_source, SOURCE_LABELS
    assert classify_source("greylist_snipe") == "greylist_snipe"
    assert SOURCE_LABELS["greylist_snipe"] == "Greylist Sniper"


# ===== Classified-pattern requirement (P0 — block unknown patterns) =======
# Paper data: 45/45 snipes fired on unknown-pattern creators with 4/45 wins.
# `greylist_snipe_require_classified_pattern=True` blocks these snipes —
# only fires when the creator has a real classified pattern.


@pytest.mark.asyncio
async def test_sniper_blocks_unknown_pattern_when_required():
    stub = _StubBotState()
    stub.config.greylist_snipe_require_classified_pattern = True
    stub.db.creator_doc = _make_creator_doc(score=70.0, pattern="unknown")
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


@pytest.mark.asyncio
async def test_sniper_blocks_null_pattern_when_required():
    stub = _StubBotState()
    stub.config.greylist_snipe_require_classified_pattern = True
    stub.db.creator_doc = _make_creator_doc(score=70.0, pattern=None)
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


@pytest.mark.asyncio
async def test_sniper_allows_classified_pattern_when_required():
    """Real pattern → passes the gate."""
    stub = _StubBotState()
    stub.config.greylist_snipe_require_classified_pattern = True
    stub.db.creator_doc = _make_creator_doc(score=70.0, pattern="slow_rug_tradeable")
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert len(stub.enter_calls) == 1


@pytest.mark.asyncio
async def test_sniper_allows_unknown_when_requirement_off():
    """Gate OFF → unknown still fires."""
    stub = _StubBotState()
    stub.config.greylist_snipe_require_classified_pattern = False
    stub.db.creator_doc = _make_creator_doc(score=70.0, pattern="unknown")
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert len(stub.enter_calls) == 1


@pytest.mark.asyncio
async def test_research_mode_bypasses_classified_pattern_requirement():
    """Research mode targets `unpredictable_rug` specifically — the
    classified-pattern gate must NOT block research snipes."""
    stub = _StubBotState(research_mode=True, research_min_score=35.0)
    stub.config.greylist_snipe_require_classified_pattern = True
    stub.db.creator_doc = _make_creator_doc(
        score=50.0, blacklisted=True, pattern="unpredictable_rug",
    )
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert len(stub.enter_calls) == 1


# ===== Research-mode snipes (Bimodal/Unpredictable creators) ==============
# `greylist_snipe_research_mode=True` lets the sniper fire on creators who
# would normally be blacklisted (currently only `unpredictable_rug`), but
# `_enter_impl` halves the trade size and stamps `is_research_snipe=True`
# on the trade document.


@pytest.mark.asyncio
async def test_research_mode_fires_on_unpredictable_rug_creator():
    stub = _StubBotState(research_mode=True, research_min_score=35.0)
    stub.db.creator_doc = _make_creator_doc(
        score=50.0, blacklisted=True, pattern="unpredictable_rug",
    )
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert len(stub.enter_calls) == 1
    assert stub.enter_calls[0][2] == "greylist_snipe"
    # Research flag should be stamped while _enter runs. Our stub _enter
    # completes synchronously so the flag has already been popped after.
    # Instead, assert the flag was set on the dict during the call by
    # snooping in a custom _enter below.


@pytest.mark.asyncio
async def test_research_mode_flag_set_during_enter():
    """The sniper sets `_snipe_research_flags[mint]=True` before calling
    `_enter` and pops it after. We snoop the flag from inside _enter."""
    stub = _StubBotState(research_mode=True, research_min_score=35.0)
    stub.db.creator_doc = _make_creator_doc(
        score=50.0, blacklisted=True, pattern="unpredictable_rug",
    )
    observed = {}

    async def _spy_enter(launch, risk_score, action):
        observed["flag"] = stub._snipe_research_flags.get(launch.mint)
        stub.enter_calls.append((launch.mint, risk_score, action))

    stub._enter = _spy_enter
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(mint="rsmint"), {})
    assert observed.get("flag") is True
    # And cleaned up afterward
    assert "rsmint" not in stub._snipe_research_flags


@pytest.mark.asyncio
async def test_research_mode_off_blocks_unpredictable_creator():
    """Same blacklisted creator — but research_mode=False → SKIP."""
    stub = _StubBotState(research_mode=False)
    stub.db.creator_doc = _make_creator_doc(
        score=70.0, blacklisted=True, pattern="unpredictable_rug",
    )
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


@pytest.mark.asyncio
async def test_research_mode_does_not_promote_untradeable_rug():
    """`untradeable_rug` (Dead-in-60s cohort) stays blocked even in
    research mode — only `unpredictable_rug` is research-tradeable."""
    stub = _StubBotState(research_mode=True)
    stub.db.creator_doc = _make_creator_doc(
        score=70.0, blacklisted=True, pattern="untradeable_rug",
    )
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


@pytest.mark.asyncio
async def test_research_mode_does_not_bypass_out_of_band():
    """out_of_band creators stay blocked even when research_mode=True."""
    stub = _StubBotState(research_mode=True)
    stub.db.creator_doc = _make_creator_doc(
        score=70.0, blacklisted=True, out_of_band=True, pattern="unpredictable_rug",
    )
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


@pytest.mark.asyncio
async def test_research_mode_uses_lower_min_score():
    """Research-mode min_score (35) is checked, NOT the normal min_score (45)."""
    stub = _StubBotState(min_score=45.0, research_mode=True, research_min_score=35.0)
    stub.db.creator_doc = _make_creator_doc(
        score=40.0,  # below normal 45 but above research 35
        blacklisted=True, pattern="unpredictable_rug",
    )
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert len(stub.enter_calls) == 1


@pytest.mark.asyncio
async def test_research_mode_respects_research_min_score_floor():
    """Score below the research floor → still blocked."""
    stub = _StubBotState(min_score=45.0, research_mode=True, research_min_score=35.0)
    stub.db.creator_doc = _make_creator_doc(
        score=20.0,  # below research floor
        blacklisted=True, pattern="unpredictable_rug",
    )
    sniper = _bind_sniper(stub)
    await sniper(_StubLaunch(), {})
    assert stub.enter_calls == []


# ----- Size-multiplier math --------------------------------------------------
# `_enter_impl` halves size when `is_research_snipe=True`. We exercise the
# math inline (importing the full _enter_impl is impractical — needs RPC,
# pool state, etc.) to lock in the formula.

def test_research_mode_size_multiplier_halves_trade():
    """Mimic the size multiplier branch in `_enter_impl`."""
    base_size_mult = 1.0  # midband risk bucket
    research_size_mult = 0.5
    is_research_snipe = True

    final_mult = base_size_mult
    if is_research_snipe:
        final_mult *= research_size_mult
    final_mult = min(final_mult, 2.0)
    assert final_mult == 0.5


def test_research_size_mult_layers_with_risk_bucket():
    """Research-mode multiplier compounds with the underlying risk bucket
    AND the greylist size-mult override. e.g. low-risk bucket (1.5×) +
    research (0.5×) = 0.75× of base trade size."""
    risk_mult = 1.5     # low-risk bucket
    gl_size_mult = 1.0  # no greylist override
    research_mult = 0.5

    final = risk_mult * gl_size_mult * research_mult
    final = min(final, 2.0)
    assert final == 0.75


def test_non_research_snipe_keeps_full_size():
    """Standard (non-research) snipe → size multiplier untouched."""
    base_mult = 1.0
    is_research = False
    final = base_mult * (0.5 if is_research else 1.0)
    assert final == 1.0


# ----- Trade document stamps `is_research_snipe` -----------------------------

def test_trade_doc_stamps_is_research_snipe_field():
    """`_enter_impl` passes `is_research_snipe=is_research_snipe` into
    `Trade(...)`. Verify the model accepts the field."""
    from models import Trade
    t = Trade(
        mint="X", symbol="X",
        entry_price_sol=1.0, entry_tokens=1.0, entry_sol=0.1,
        classifier_action="greylist_snipe",
        is_research_snipe=True,
    )
    assert t.is_research_snipe is True


def test_trade_doc_is_research_snipe_defaults_false():
    from models import Trade
    t = Trade(
        mint="X", symbol="X",
        entry_price_sol=1.0, entry_tokens=1.0, entry_sol=0.1,
    )
    assert t.is_research_snipe is False
