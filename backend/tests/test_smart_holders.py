"""Holder ↔ smart-wallet cross-check: smart wallets found among a token's holders count exactly like tape hits."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import smart_holders as sh  # noqa: E402
from models import BotConfig  # noqa: E402

NOW = time.time()


class _SW:
    def __init__(self):
        self.manual = {"MANUAL1": {"label": "Aurelius"}}
        self.learned = {"LEARNED1": {"wins": 5, "misses": 1}}
        self.stats = {"hits": 0}
        self.fired = []

    def enabled(self):
        return True

    def mode(self):
        return "both"

    def kind_of(self, w):
        return "manual" if w in self.manual else "learned" if w in self.learned else None

    def _maybe_fire(self, mint, b, wallet, kind, lamports, now):
        self.fired.append((mint, wallet, kind))


class _State:
    def __init__(self, tracking):
        self.config = BotConfig(smart_holder_check_min_buyers=10, smart_holder_check_max_per_scan=2, band_new_max_age_min=15)
        self.tracking = tracking
        self.active_trades = {}
        self.smart_wallets = _SW()
        self.db = None


def _bucket(buyers=20, refusal=None, age_s=60, creator="CREATOR"):
    b = {"symbol": "LUNA", "protocol": "pumpfun", "start": NOW - age_s, "creator": creator, "buyers": set(f"b{i}" for i in range(buyers))}
    if refusal:
        b["entry_refusal"] = {"reason": refusal, "ts": NOW}
    return b


def test_pick_candidates_prefers_blocked_and_respects_cadence_and_cap():
    from scanner import MomentumScanner
    cfg = BotConfig(smart_holder_check_min_buyers=10, smart_holder_check_max_per_scan=2)
    tr = {"A": _bucket(buyers=50), "B": _bucket(buyers=12, refusal="creator-block"), "C": _bucket(buyers=5), "D": _bucket(buyers=40, age_s=20 * 60),
          "E": _bucket(buyers=30)}
    tr["E"]["_holder_check_ts"] = NOW - 5          # checked 5 s ago → skipped
    picked = [m for m, _ in sh.pick_candidates(tr, cfg, NOW, MomentumScanner.classify_band, set())]
    assert picked == ["B", "A"]                    # blocked copy first, then busiest; C too small, D out of band, E too recent


def test_sweep_registers_holder_hits_and_fires(monkeypatch):
    tr = {"LUNA1": _bucket(buyers=20, refusal="creator-block")}
    st = _State(tr)

    async def fake_owners(mint):
        return {"MANUAL1", "LEARNED1", "random", "CREATOR"}
    monkeypatch.setattr(sh, "holder_owners", fake_owners)
    tally = asyncio.run(sh.sweep(st, NOW))
    b = tr["LUNA1"]
    assert tally["checked"] == 1 and tally["matched"] == 1 and tally["new_hits"] == 2
    assert set(b["smart_hits"]) == {"MANUAL1", "LEARNED1"} and b["smart_hits"]["MANUAL1"]["source"] == "holder"
    assert b["smart_hit_ts"] == NOW and b["holder_count"] == 4
    assert st.smart_wallets.fired == [("LUNA1", "MANUAL1", "manual")]          # manual wallet preferred for the trigger
    # second sweep inside the cadence: nothing re-read, no duplicate hits
    assert asyncio.run(sh.sweep(st, NOW + 5))["checked"] == 0
    tally2 = asyncio.run(sh.sweep(st, NOW + 40))
    assert tally2["checked"] == 1 and tally2["new_hits"] == 0 and len(b["smart_hits"]) == 2


def test_sweep_off_switch_and_rpc_error(monkeypatch):
    st = _State({"X": _bucket(buyers=20)})
    st.config.smart_holder_check_enabled = False
    assert asyncio.run(sh.sweep(st, NOW))["checked"] == 0
    st.config.smart_holder_check_enabled = True

    async def boom(mint):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(sh, "holder_owners", boom)
    t = asyncio.run(sh.sweep(st, NOW))
    assert t["checked"] == 1 and t["errors"] == 1 and "smart_hits" not in st.tracking["X"]
