"""Tests for the post-entry graduation migration (`_detect_and_migrate_graduation`).

Verifies:
  * idempotent — re-calling on an already-migrated slot returns True without
    additional pool lookups
  * returns False when no PumpSwap pool exists yet (caller continues waiting)
  * returns False when find_pool_for_mint raises (handled, not propagated)
  * returns False when pool exists but fetch_pool_state returns null
  * returns True + mutates the slot when both pool and state are healthy
  * persists protocol + pumpswap_pool to the trade doc in Mongo so reconciler
    respawns inherit migrated state
  * clears `_graduation_grace_start` on successful migration
"""
from __future__ import annotations
import os
import sys
import time
from unittest.mock import AsyncMock, patch

os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("HELIUS_WSS_URL", "wss://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


def _make_stub(trade_id="t1"):
    """Bind _detect_and_migrate_graduation to a minimal stub state."""
    from bot import BotState

    class _DB:
        def __init__(self):
            self.updates: list = []
            self.trades = type("T", (), {
                "update_one": self._update_one,
            })()
        async def _update_one(self, filt, patch):
            self.updates.append((filt, patch))

    stub = type("S", (), {})()
    stub.db = _DB()
    stub._detect_and_migrate_graduation = BotState._detect_and_migrate_graduation.__get__(stub, type(stub))
    stub.slot = {
        "protocol": "pumpfun",
        "trade": {"id": trade_id, "mint": "MINT", "symbol": "TEST"},
        "_graduation_grace_start": time.time(),
    }
    return stub


@pytest.mark.asyncio
async def test_migration_returns_true_and_mutates_slot_when_pool_healthy():
    stub = _make_stub()
    fake_pool = "PoolAddr_AAAA1111"
    with patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=fake_pool)), \
         patch("bot.pumpswap.fetch_pool_state", new=AsyncMock(return_value={"any": "state"})), \
         patch("bot.hub.broadcast", new=AsyncMock(return_value=None)):
        ok = await stub._detect_and_migrate_graduation("MINT", stub.slot)
    assert ok is True
    assert stub.slot["protocol"] == "pumpswap"
    assert stub.slot["pumpswap_pool"] == fake_pool
    assert "_graduation_detected_ts" in stub.slot
    # Grace timer cleared
    assert "_graduation_grace_start" not in stub.slot
    # Persisted to Mongo
    assert len(stub.db.updates) == 1
    filt, patch_doc = stub.db.updates[0]
    assert filt == {"id": "t1"}
    assert patch_doc["$set"]["protocol"] == "pumpswap"
    assert patch_doc["$set"]["pumpswap_pool"] == fake_pool
    assert "graduation_migrated_at" in patch_doc["$set"]


@pytest.mark.asyncio
async def test_migration_returns_false_when_no_pool_yet():
    """Pool doesn't exist yet — caller (monitor) should keep waiting."""
    stub = _make_stub()
    with patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=None)):
        ok = await stub._detect_and_migrate_graduation("MINT", stub.slot)
    assert ok is False
    assert stub.slot["protocol"] == "pumpfun"  # unchanged
    assert stub.slot.get("pumpswap_pool") is None
    assert stub.db.updates == []  # no persist


@pytest.mark.asyncio
async def test_migration_returns_false_when_pool_state_null():
    """Pool address exists but its state isn't available — treat as not-yet-migrated."""
    stub = _make_stub()
    with patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value="Pool1")), \
         patch("bot.pumpswap.fetch_pool_state", new=AsyncMock(return_value=None)):
        ok = await stub._detect_and_migrate_graduation("MINT", stub.slot)
    assert ok is False
    assert stub.slot["protocol"] == "pumpfun"
    assert stub.db.updates == []


@pytest.mark.asyncio
async def test_migration_swallows_pool_lookup_exception():
    """Helius hiccup during pool lookup — never propagate, return False."""
    stub = _make_stub()
    with patch("bot.pumpswap.find_pool_for_mint",
               new=AsyncMock(side_effect=RuntimeError("RPC timeout"))):
        ok = await stub._detect_and_migrate_graduation("MINT", stub.slot)
    assert ok is False
    assert stub.slot["protocol"] == "pumpfun"


@pytest.mark.asyncio
async def test_migration_is_idempotent_on_already_migrated_slot():
    """If the slot was already migrated, return True without lookups."""
    stub = _make_stub()
    stub.slot["protocol"] = "pumpswap"  # already migrated
    stub.slot["pumpswap_pool"] = "ExistingPool"
    find_mock = AsyncMock()
    fetch_mock = AsyncMock()
    with patch("bot.pumpswap.find_pool_for_mint", new=find_mock), \
         patch("bot.pumpswap.fetch_pool_state", new=fetch_mock):
        ok = await stub._detect_and_migrate_graduation("MINT", stub.slot)
    assert ok is True
    # Critical: no expensive lookups when already migrated
    find_mock.assert_not_called()
    fetch_mock.assert_not_called()
    # Slot unchanged
    assert stub.slot["pumpswap_pool"] == "ExistingPool"


@pytest.mark.asyncio
async def test_migration_handles_persist_failure_gracefully():
    """Mongo update failure should NOT prevent the migration — slot is
    already migrated in-memory; the reconciler will try persisting again
    on the next cycle."""
    stub = _make_stub()
    async def _bad_update(*_a, **_k):
        raise RuntimeError("mongo network blip")
    stub.db.trades.update_one = _bad_update
    with patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value="P")), \
         patch("bot.pumpswap.fetch_pool_state", new=AsyncMock(return_value={"x": 1})), \
         patch("bot.hub.broadcast", new=AsyncMock(return_value=None)):
        ok = await stub._detect_and_migrate_graduation("MINT", stub.slot)
    # Still True — in-memory migration succeeded
    assert ok is True
    assert stub.slot["protocol"] == "pumpswap"
    assert stub.slot["pumpswap_pool"] == "P"


@pytest.mark.asyncio
async def test_migration_skips_persist_when_trade_doc_has_no_id():
    """Some legacy code paths build a slot without persisting the trade
    first. Don't crash — just skip the persist step."""
    stub = _make_stub()
    stub.slot["trade"] = {"mint": "MINT", "symbol": "TEST"}  # no `id`
    with patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value="P")), \
         patch("bot.pumpswap.fetch_pool_state", new=AsyncMock(return_value={"x": 1})), \
         patch("bot.hub.broadcast", new=AsyncMock(return_value=None)):
        ok = await stub._detect_and_migrate_graduation("MINT", stub.slot)
    assert ok is True
    assert stub.db.updates == []  # no persist attempt without id
