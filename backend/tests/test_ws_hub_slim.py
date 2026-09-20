"""WS hub live path: slim `candidate` frames, `{id, seq, p}` patches, seq monotonic, caps pruned, no full-doc warehouse."""
from ws_hub import WSHub, LIVE_FIELDS, SEEN_CAP, IDENT_CAP, slim_launch


def full_doc(i: str, **over) -> dict:
    d = {"id": i, "mint": f"mint{i}", "chain": "solana", "symbol": f"S{i}", "name": f"Name {i}", "creator": "c" * 44,
         "classifier_action": "scalp", "classifier_reasons": ["strong inflow 3.2 SOL"] * 5, "signature": "x" * 88,
         "unique_buyers": 7, "sol_inflow": 3.2, "detected_at": "2026-09-20T00:00:00", "pin_creator_pattern": {"big": "blob" * 50}}
    d.update(over)
    return d


def test_candidate_frame_is_slim_and_carries_seq():
    h = WSHub()
    ev, d = h._gate_launch("launch", full_doc("a"))
    assert ev == "candidate" and d["seq"] == 1
    assert set(d) - {"seq"} <= LIVE_FIELDS
    assert "classifier_reasons" not in d and "signature" not in d and "pin_creator_pattern" not in d
    assert d["symbol"] == "Sa" and d["unique_buyers"] == 7


def test_update_becomes_patch_with_changed_fields_only():
    h = WSHub()
    h._gate_launch("launch", full_doc("a"))
    ev, d = h._gate_launch("launch_update", {"id": "a", "mint": "minta", "unique_buyers": 9, "sol_inflow": 3.2, "classifier_reasons": ["x"]})
    assert ev == "candidate_update"
    assert d == {"id": "a", "seq": 2, "p": {"unique_buyers": 9}}   # mint / sol_inflow unchanged, reasons not a live field
    assert h._gate_launch("launch_update", {"id": "a", "unique_buyers": 9}) == (None, None)
    ev, d = h._gate_launch("launch_update", {"id": "a", "unique_buyers": 12})
    assert d["seq"] == 3


def test_drop_patch_then_requalify_restarts_as_candidate():
    h = WSHub()
    h._gate_launch("launch", full_doc("a"))
    ev, d = h._gate_launch("launch_update", {"id": "a", "classifier_action": "skip"})
    assert ev == "candidate_update" and d["p"] == {"dropped": True} and d["seq"] == 2
    assert "a" not in h._seen
    ev, d = h._gate_launch("launch_update", {"id": "a", "classifier_action": "scalp", "unique_buyers": 3})
    assert ev == "candidate" and d["seq"] == 1 and "symbol" not in d   # identity was not kept for a dropped candidate


def test_non_candidate_keeps_identity_stub_only_and_late_qualifier_arrives_named():
    h = WSHub()
    assert h._gate_launch("launch", full_doc("b", classifier_action="pending", unique_buyers=1)) == (None, None)
    stub = h._ident["b"]
    assert stub["symbol"] == "Sb" and "classifier_reasons" not in stub and "unique_buyers" not in stub
    ev, d = h._gate_launch("launch_update", {"id": "b", "classifier_action": "pending", "unique_buyers": 8})
    assert ev == "candidate" and d["symbol"] == "Sb" and d["unique_buyers"] == 8 and "b" not in h._ident


def test_caps_are_enforced():
    h = WSHub()
    for i in range(IDENT_CAP + 300):
        h._gate_launch("launch", full_doc(f"p{i}", classifier_action="pending", unique_buyers=0))
    assert len(h._ident) == IDENT_CAP and "p0" not in h._ident and f"p{IDENT_CAP + 299}" in h._ident
    for i in range(SEEN_CAP + 50):
        h._gate_launch("launch", full_doc(f"c{i}"))
    assert len(h._seen) == SEEN_CAP and "c0" not in h._seen
    assert not hasattr(h, "_raw")


def test_slim_launch_helper():
    assert slim_launch({"id": "x", "junk": 1, "symbol": "S"}) == {"id": "x", "symbol": "S"}
