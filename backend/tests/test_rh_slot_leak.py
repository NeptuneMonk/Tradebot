"""max-positions must never be held by a phantom: early returns release the slot, queued buys expire."""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_rh_paper import make_state, hot_bucket, TOKEN


def test_fenced_or_gated_entry_releases_the_reserved_slot():
    st = make_state()
    hot_bucket(st.rh_discovery, time.time())
    st.leader_fence = AsyncMock(return_value=False)
    st.rh_paper._pending_entries.add(TOKEN)
    asyncio.run(st.rh_paper._enter(TOKEN))
    assert TOKEN not in st.rh_paper._pending_entries and TOKEN not in st.rh_paper.positions
    # untracked token: early return before any gate
    st.rh_paper._pending_entries.add("0x" + "9" * 40)
    asyncio.run(st.rh_paper._enter("0x" + "9" * 40))
    assert "0x" + "9" * 40 not in st.rh_paper._pending_entries
    assert st.rh_paper._gates(TOKEN, st.rh_discovery.tracking[TOKEN], time.time()) != "max-positions"


def test_queued_paper_buy_keeps_slot_then_expires():
    st = make_state(paper_entry_latency_ms=600)
    hot_bucket(st.rh_discovery, time.time())
    asyncio.run(st.rh_paper._enter(TOKEN))
    assert TOKEN in st.rh_paper.pending_buys and TOKEN in st.rh_paper._pending_entries      # reserved while queued
    st.rh_paper.pending_buys[TOKEN]["ts"] = time.time() - 500
    assert st.rh_paper.expire_pending_buys(time.time()) == 1
    assert TOKEN not in st.rh_paper.pending_buys and TOKEN not in st.rh_paper._pending_entries
    assert st.rh_paper.stats["entries_expired"] == 1


def test_orphan_reservation_is_released_after_grace():
    st = make_state()
    st.rh_paper._pending_entries.add(TOKEN)
    now = time.time()
    st.rh_paper.expire_pending_buys(now)                 # first sighting: start the clock
    assert TOKEN in st.rh_paper._pending_entries
    st.rh_paper.expire_pending_buys(now + 31)
    assert TOKEN not in st.rh_paper._pending_entries and st.rh_paper.stats["slots_released"] == 1
    status = st.rh_paper.status()
    assert status["slots_used"] == 0 and status["pending_entries"] == []
