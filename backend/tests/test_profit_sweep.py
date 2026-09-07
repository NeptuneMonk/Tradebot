"""Profit sweep: baseline anchoring, projection, schedule, paper ledger, safety refusals."""
import asyncio, os, sys, time
from types import SimpleNamespace
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from profit_sweep import ProfitSweeper, valid_pubkey
from models import BotConfig

COLD = "11111111111111111111111111111112"   # valid base58 pubkey (system program-ish)


class _Coll:
    def __init__(self): self.rows = []
    async def insert_one(self, d): self.rows.append(dict(d))
    def find(self, q, proj=None):
        rows = [r for r in self.rows if all(r.get(k) == v for k, v in q.items())]
        class C:
            def __init__(s, r): s.r = r
            def sort(s, *a): s.r = sorted(s.r, key=lambda x: -x["ts"]); return s
            def limit(s, n): s.r = s.r[:n]; return s
            def __aiter__(s):
                async def g():
                    for x in s.r: yield x
                return g()
        return C(rows)


class _State:
    def __init__(self, bankroll):
        self.config = BotConfig(sweep_cold_wallet=COLD, sweep_enabled=True, sweep_pct_of_profit=50.0, sweep_min_usd=10.0)
        self._bankroll = bankroll
        self.saved = 0
    async def save_config(self): self.saved += 1


class _Bank:
    def __init__(self, st): self.st = st
    async def bankroll_usd(self, chain="sol"): return self.st._bankroll, "paper"
    async def refresh(self): return {}


def _mk(bankroll):
    st = _State(bankroll)
    db = SimpleNamespace(profit_sweeps=_Coll(), autopilot_state=SimpleNamespace(update_one=_noop, find_one=_none))
    return st, ProfitSweeper(st, db, _Bank(st))


async def _noop(*a, **k): pass
async def _none(*a, **k): return None


def test_valid_pubkey():
    assert valid_pubkey(COLD) and not valid_pubkey("not-an-address") and not valid_pubkey("")


def test_baseline_anchors_to_current_bankroll_once():
    st, sw = _mk(1000.0)
    asyncio.run(sw.ensure_baseline(1000.0))
    assert st.config.sweep_baseline_usd == 1000.0 and st.config.sweep_started_ts > 0 and st.saved == 1
    asyncio.run(sw.ensure_baseline(1500.0))
    assert st.config.sweep_baseline_usd == 1000.0


def test_preview_projects_slice_of_growth_and_schedule():
    st, sw = _mk(1300.0)
    asyncio.run(sw.ensure_baseline(1000.0))
    pv = asyncio.run(sw.preview())
    assert pv["profit_above_baseline_usd"] == 300.0 and pv["projected_sweep_usd"] == 150.0
    assert pv["due_now"] is False and abs(pv["next_due_ts"] - (st.config.sweep_started_ts + 7 * 86400)) < 1


def test_paper_sweep_records_ledger_and_moves_baseline():
    st, sw = _mk(1300.0)
    asyncio.run(sw.ensure_baseline(1000.0))
    assert asyncio.run(sw.sweep_now(force=False))["ok"] is False          # not due
    r = asyncio.run(sw.sweep_now(force=True))
    assert r["ok"] and r["sweep"]["amount_usd"] == 150.0 and r["sweep"]["mode"] == "paper"
    assert st.config.sweep_baseline_usd == 1300.0                          # only new growth sliced next time
    assert asyncio.run(sw.total_swept_usd("paper")) == 150.0 and sw.last_sweep_ts > 0
    r2 = asyncio.run(sw.sweep_now(force=True))
    assert r2["ok"] is False and "below minimum" in r2["reason"]           # nothing new to skim


def test_refuses_without_valid_cold_wallet_or_below_min():
    st, sw = _mk(1300.0)
    st.config.sweep_cold_wallet = "bogus"
    asyncio.run(sw.ensure_baseline(1000.0))
    assert "invalid" in asyncio.run(sw.sweep_now(force=True))["reason"]
    st.config.sweep_cold_wallet = COLD
    st._bankroll = 1010.0
    assert "below minimum" in asyncio.run(sw.sweep_now(force=True))["reason"]


def test_config_defaults():
    c = BotConfig()
    assert (c.sweep_enabled, c.sweep_pct_of_profit, c.sweep_interval_days, c.sweep_min_usd, c.sweep_reserve_sol) == (False, 50.0, 7, 10.0, 0.05)
