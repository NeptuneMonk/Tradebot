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
                 min_score=45.0, max_per_hour=12, settle_s=0):
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
        self._greylist_snipe_fires: list[float] = []
        self.active_trades: dict = {}
        self._pending_entry_mints: set = set()
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
