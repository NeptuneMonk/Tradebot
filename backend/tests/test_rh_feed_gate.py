"""RH launches must reach the candidate-only WS feed: gate verdict rides on the row, hub passes rh rows."""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ws_hub import WSHub
from tests.test_rh_paper import make_state, hot_bucket, TOKEN


def test_hub_passes_rh_rows_that_cleared_the_gate_or_are_hot():
    assert WSHub.is_candidate({"chain": "rh", "classifier_action": "tracking", "rh_gate": "pass", "unique_buyers": 1})
    assert WSHub.is_candidate({"chain": "rh", "classifier_action": "rh_pons", "unique_buyers": 0})
    assert WSHub.is_candidate({"chain": "rh", "classifier_action": "tracking", "rh_gate": "min-buyers", "unique_buyers": 7})
    assert not WSHub.is_candidate({"chain": "rh", "classifier_action": "tracking", "rh_gate": "min-buyers", "unique_buyers": 2})
    assert not WSHub.is_candidate({"chain": "solana", "classifier_action": "tracking", "unique_buyers": 9})


def test_launch_and_update_events_become_candidates_for_rh():
    hub = WSHub()
    ev, data = hub._gate_launch("launch", {"id": "L1", "chain": "rh", "symbol": "PONS1", "name": "Pons One", "creator": "0xdead",
                                           "classifier_action": "tracking", "rh_gate": None, "unique_buyers": 1})
    assert ev is None                                                        # fresh, cold, ungated: not yet
    ev, data = hub._gate_launch("launch_update", {"id": "L1", "unique_buyers": 6})
    # merged with the remembered raw launch → first appearance carries the whole row (symbol, creator, chain…)
    assert ev == "candidate" and data["chain"] == "rh" and data["unique_buyers"] == 6 and data["symbol"] == "PONS1" and data["creator"] == "0xdead"
    ev, data = hub._gate_launch("launch_update", {"id": "L1", "rh_gate": "pass", "classifier_action": "rh_pons"})
    assert ev == "candidate_update" and data["rh_gate"] == "pass"


def test_scan_stamps_gate_verdict_on_bucket_and_marks_it_dirty():
    st = make_state()
    b = hot_bucket(st.rh_discovery, time.time())
    st.rh_discovery._dirty.clear()

    async def scan():
        st.rh_paper._scan_entries(time.time())
        await asyncio.sleep(0)
    asyncio.run(scan())
    assert b.get("gate_reason") is not None and TOKEN in st.rh_discovery._dirty
    fields = st.rh_discovery._launch_fields(b)
    assert fields["rh_gate"] == b["gate_reason"]
    assert fields["classifier_action"] == ("rh_pons" if b["gate_reason"] == "pass" else "tracking")
    assert WSHub.is_candidate({"chain": "rh", **fields}) is True            # hot bucket (12 buyers) always visible
