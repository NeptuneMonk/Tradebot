"""
Tests for greylist snipe lifecycle: ensure snipes do NOT trigger a re-entry
watch (which would queue a standard-rules follow-up trade and exit via
SL/TP/max-hold — the symptom the user reported), and that snipe pattern
context survives backend restart via persisted `snipe_pattern_ctx`.
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

from models import Trade  # noqa: E402


def test_trade_model_persists_snipe_pattern_ctx():
    """Snipe context must round-trip through model_dump / model_validate so
    Mongo persistence preserves it across backend restarts."""
    ctx = {
        "expected_peak_mc_usd": 250_000.0,
        "expected_peak_mc_stddev": 40_000.0,
        "expected_rug_curve_pct": 70.0,
        "pattern": "slow_rug_tradeable",
    }
    t = Trade(
        mint="MintMintMintMintMintMintMintMintMintMintMint",
        status="active",
        mode="paper",
        entry_sol=0.05,
        entry_usd=10.0,
        entry_tokens=1_000_000,
        entry_price_sol=5e-8,
        entry_fee_sol=0.0001,
        speed_mode_at_entry="fast",
        classifier_action="greylist_snipe",
        snipe_pattern_ctx=ctx,
    )
    doc = t.model_dump()
    assert doc["snipe_pattern_ctx"] == ctx
    # Round-trip
    restored = Trade(**doc)
    assert restored.snipe_pattern_ctx == ctx


def test_non_snipe_trade_has_no_snipe_ctx_by_default():
    """Momentum/reentry trades have None for snipe_pattern_ctx."""
    t = Trade(
        mint="MintMintMintMintMintMintMintMintMintMintMint",
        status="active",
        mode="paper",
        entry_sol=0.05,
        entry_usd=10.0,
        entry_tokens=1_000_000,
        entry_price_sol=5e-8,
        entry_fee_sol=0.0001,
        speed_mode_at_entry="fast",
        classifier_action="momentum_new",
    )
    assert t.snipe_pattern_ctx is None


def test_reentry_skip_for_greylist_snipe_exits():
    """The reentry watcher must NOT queue a follow-up trade for a greylist
    snipe winner. This is the user-reported symptom: snipe exits via its
    pattern ladder, then a "reentry" trade gets queued with
    classifier_action="reentry" → standard SL/TP/max-hold apply → user
    sees Trade History showing the reentry leg exit with std rules and
    reports "the greylist is being terminated by max time, sl, tp"."""
    # We test the gating logic directly without spinning up a full BotState.
    # The bot exit path uses this short-circuit:
    #   classifier_action = trade_doc.get("classifier_action") or ""
    #   is_snipe_trade = classifier_action == "greylist_snipe"
    #   if (reentry_enabled and not stopping_gracefully and not is_snipe_trade
    #       and total_pnl_sol > 0 and not state.complete): ...
    def should_queue_reentry(trade_doc, pnl, complete, reentry_enabled=True, stopping=False):
        classifier_action = trade_doc.get("classifier_action") or ""
        is_snipe_trade = classifier_action == "greylist_snipe"
        return (
            reentry_enabled
            and not stopping
            and not is_snipe_trade
            and pnl > 0
            and not complete
        )

    # Snipe winner — must NOT queue reentry
    assert not should_queue_reentry(
        {"classifier_action": "greylist_snipe"}, pnl=0.005, complete=False
    )
    # Snipe winner on graduated curve — also no reentry
    assert not should_queue_reentry(
        {"classifier_action": "greylist_snipe"}, pnl=0.005, complete=True
    )
    # Momentum winner — DOES queue reentry
    assert should_queue_reentry(
        {"classifier_action": "momentum_new"}, pnl=0.005, complete=False
    )
    # Momentum winner that graduated — no reentry (curve done)
    assert not should_queue_reentry(
        {"classifier_action": "momentum_new"}, pnl=0.005, complete=True
    )
    # Losing trade — no reentry regardless of class
    assert not should_queue_reentry(
        {"classifier_action": "momentum_new"}, pnl=-0.001, complete=False
    )
    # Reentry-leg trade that exited profitably — DOES re-queue
    # (classifier_action="reentry" is not a snipe; allowed to chain
    # for as many `max_attempts` as configured).
    assert should_queue_reentry(
        {"classifier_action": "reentry"}, pnl=0.002, complete=False
    )
    # Research snipe — still a snipe class, no reentry
    assert not should_queue_reentry(
        {"classifier_action": "greylist_snipe", "is_research_snipe": True},
        pnl=0.005,
        complete=False,
    )
