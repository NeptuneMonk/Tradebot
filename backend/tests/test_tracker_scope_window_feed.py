"""Integration tests for tracker scope eviction + launches-in-window live feed.

Covers:
- GET /api/diagnostics/loop shape (tracked_mints, tracker_evictions, tracker_bands, rh_tracked)
- Scope eviction via PUT /api/bot/config book_scalp_enabled toggle (restores value)
- GET /api/launches/recent returns only in_band rows with age within band window
"""
import os
import time
import pytest
import requests
from datetime import datetime, timezone

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")


# ---------- diagnostics shape ----------
def test_diagnostics_loop_shape(auth_headers):
    r = requests.get(f"{BASE_URL}/api/diagnostics/loop", headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "tracked_mints" in d and isinstance(d["tracked_mints"], int)
    assert "tracker_evictions" in d and isinstance(d["tracker_evictions"], dict)
    assert "tracker_bands" in d
    for k in ("new", "seasoned", "pre_band", "discovered"):
        assert k in d["tracker_bands"], f"missing band key {k}"
    assert "rh_tracked" in d and isinstance(d["rh_tracked"], int)


# ---------- scope eviction via book toggle ----------
def test_scope_eviction_book_toggle(auth_headers):
    # snapshot original
    cfg0 = requests.get(f"{BASE_URL}/api/bot/config", headers=auth_headers, timeout=15).json()
    original = bool(cfg0.get("book_scalp_enabled", True))

    try:
        # disable → should prune non-held tokens out of scope
        r = requests.put(
            f"{BASE_URL}/api/bot/config",
            headers=auth_headers,
            json={"book_scalp_enabled": False},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        time.sleep(2)

        d = requests.get(f"{BASE_URL}/api/diagnostics/loop", headers=auth_headers, timeout=15).json()
        evictions = d.get("tracker_evictions", {})
        assert "scope:book-off" in evictions, f"expected scope:book-off, got {evictions}"
        assert evictions["scope:book-off"] >= 1
        # tracked_mints should drop sharply (only held/pinned remain)
        assert d["tracked_mints"] < 20, f"tracked_mints did not drop: {d['tracked_mints']}"
    finally:
        # restore
        requests.put(
            f"{BASE_URL}/api/bot/config",
            headers=auth_headers,
            json={"book_scalp_enabled": original},
            timeout=15,
        )


# ---------- launches/recent in-band filter + age window ----------
def test_launches_recent_in_band_and_age(auth_headers):
    cfg = requests.get(f"{BASE_URL}/api/bot/config", headers=auth_headers, timeout=15).json()
    min_age = float(cfg["band_new_min_age_min"])
    max_age = float(cfg["band_new_max_age_min"])

    r = requests.get(f"{BASE_URL}/api/launches/recent?limit=30", headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert isinstance(rows, list)

    allowed_bands = {"new", "seasoned", "rh_new", "held"}
    now = datetime.now(timezone.utc)
    for row in rows:
        assert row.get("in_band") is True, f"row not in_band: {row.get('mint')}"
        assert row.get("band") in allowed_bands, f"invalid band {row.get('band')}"
        # new-band age check (allow ±1 min tolerance)
        if row.get("band") == "new" and row.get("chain") == "sol":
            det = row.get("detected_at")
            if det:
                try:
                    dt = datetime.fromisoformat(det.replace("Z", "+00:00"))
                except Exception:
                    continue
                age_min = (now - dt).total_seconds() / 60.0
                assert (min_age - 1) <= age_min <= (max_age + 1), (
                    f"new-band row {row['mint']} age {age_min:.1f}m outside "
                    f"[{min_age}, {max_age}]"
                )
