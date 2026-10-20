"""Gate Replay endpoint: every gate row has a reading/threshold/verdict; bad mints are rejected."""
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from conftest import BASE_URL  # noqa: E402


def test_replay_rows_and_shape(auth_headers):
    r = httpx.get(f"{BASE_URL}/api/replay/BU9hz1JXJZCjJwMdBPggxdEF1FEsJzyHiRwAkU8qpump", headers=auth_headers, timeout=60)
    assert r.status_code == 200, r.text
    d = r.json()
    for k in ("rows", "would_enter", "blockers", "trades", "decisions", "protocol", "age_min"):
        assert k in d
    gates = [row["gate"] for row in d["rows"]]
    assert "pump.fun mint (…pump)" in gates and "cost gate (round trip)" in gates and "creator verdict" in gates
    for row in d["rows"]:
        assert set(("gate", "reading", "threshold", "pass", "note")) <= set(row)
    assert d["would_enter"] == (len(d["blockers"]) == 0)


def test_replay_rejects_garbage(auth_headers):
    r = httpx.get(f"{BASE_URL}/api/replay/nope", headers=auth_headers, timeout=30)
    assert r.status_code == 400
