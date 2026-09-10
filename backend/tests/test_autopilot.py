"""Autopilot: bankroll sizing math, governor, Doctor risk dial."""
import asyncio, os, sys, time
from types import SimpleNamespace
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from bankroll import BankrollEngine
from models import BotConfig
import doctor_learning as dl


class _Cur:
    def __init__(self, rows): self.rows = rows
    def __aiter__(self):
        async def gen():
            for r in self.rows: yield r
        return gen()


class _Trades:
    def __init__(self, rows): self.rows = rows
    def find(self, q, proj=None):
        out = [r for r in self.rows if r.get("mode") == q.get("mode")]
        ch = q.get("chain")
        if isinstance(ch, dict):
            out = [r for r in out if r.get("chain") in ch["$in"]]
        elif ch:
            out = [r for r in out if r.get("chain") == ch]
        if "exit_time" in q:
            out = [r for r in out if r["exit_time"] >= q["exit_time"]["$gte"]]
        if "entry_gas_usd" in q:
            out = [r for r in out if (r.get("entry_gas_usd") or 0) > 0]
        return _Sorted(out)


class _Sorted(_Cur):
    def sort(self, *a, **k): return self
    def limit(self, n): return self


class _State:
    def __init__(self, rows):
        self.db = SimpleNamespace(trades=_Trades(rows), autopilot_state=SimpleNamespace(
            update_one=self._noop, find_one=self._none))
        self.config = BotConfig(bankroll_sizing_enabled=True, paper_bankroll_usd=1000.0)
        self.saved = 0
    async def _noop(self, *a, **k): pass
    async def _none(self, *a, **k): return None
    async def save_config(self): self.saved += 1


def test_derive_from_bankroll():
    d = BankrollEngine.derive(1000.0, BotConfig())
    assert d == {"max_trade_usd": 20.0, "min_trade_usd": 5.0, "daily_kill_switch_usd": 100.0}   # slots are never derived
    d = BankrollEngine.derive(20000.0, BotConfig(risk_per_trade_pct=5.0, max_exposure_pct=25.0, daily_loss_limit_pct=10.0))
    assert d["max_trade_usd"] == 100.0 and "max_concurrent_positions" not in d and d["daily_kill_switch_usd"] == 1000.0
    assert BankrollEngine.derive(30.0, BotConfig())["max_trade_usd"] == 1.0


def test_refresh_applies_sizing_and_compounds_paper_pnl():
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    st = _State([{"mode": "paper", "pnl_usd": 250.0, "exit_time": now_iso}])
    eng = BankrollEngine(st, st.db)
    snap = asyncio.run(eng.refresh())
    sol, rh = snap["chains"]["sol"], snap["chains"]["rh"]
    assert sol["bankroll_usd"] == 1250.0 and sol["bankroll_source"] == "paper"
    assert rh["bankroll_usd"] == 1000.0  # RH paper pool is its own — SOL paper wins don't leak in
    assert st.config.max_trade_usd == 25.0 and st.config.max_concurrent_positions == 3 and st.saved == 1
    assert st.config.rh_max_trade_usd == 20.0 and st.config.rh_daily_kill_switch_usd == 100.0
    assert not snap["governor_active"] and eng.size_mult("sol") == 1.0 and eng.size_mult("rh") == 1.0


def test_governor_engages_on_drawdown_and_releases():
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    st = _State([{"mode": "paper", "pnl_usd": -80.0, "exit_time": now_iso}])   # -8% of $1000 → past the 5% line
    eng = BankrollEngine(st, st.db)
    snap = asyncio.run(eng.refresh())
    assert snap["governor_active"] and eng.size_mult("sol") == 0.5 and "-8." in snap["governor_reason"]
    assert eng.size_mult("rh") == 1.0, "a Solana drawdown must not throttle the Robinhood book"
    asyncio.run(eng.release_governor("sol"))
    assert eng.size_mult("sol") == 1.0


def test_rh_live_bankroll_never_mixes_with_solana_paper_pnl():
    """Regression 2026-09-07: RH live on with a $33 ETH wallet + SOL paper -$38/24h read as '-114% of bankroll'."""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    st = _State([{"mode": "paper", "pnl_usd": -38.0, "exit_time": now_iso}])  # Solana paper loss (no chain = sol)
    st.config.rh_live_trading = True
    eng = BankrollEngine(st, st.db)

    async def fake_bankroll(chain):
        return (33.0, "eth wallet") if chain == "rh" else (1000.0 - 38.0, "paper")
    eng.bankroll_usd = fake_bankroll
    snap = asyncio.run(eng.refresh())
    rh, sol = snap["chains"]["rh"], snap["chains"]["sol"]
    assert rh["pnl_24h_usd"] == 0.0 and rh["drawdown_24h_pct"] == 0.0 and not rh["governor_active"]
    assert sol["drawdown_24h_pct"] < -3.9 and not sol["governor_active"]  # -3.95% < 5% line
    assert not snap["governor_active"]


def test_rh_fee_floor_lifts_small_stakes_and_sits_out_tiny_bankrolls():
    cfg = BotConfig()
    floor = {"min_stake_usd": 3.8}
    assert BankrollEngine.derive_rh(1000.0, cfg, floor)["rh_max_trade_usd"] == 20.0     # 2% of $1000 clears the floor
    assert BankrollEngine.derive_rh(100.0, cfg, floor)["rh_max_trade_usd"] == 3.8       # 2% = $2 → lifted to floor
    assert BankrollEngine.derive_rh(10.0, cfg, floor)["rh_max_trade_usd"] == 0.0        # floor > 25% of bankroll → sit out
    assert BankrollEngine.derive_rh(1000.0, cfg, {})["rh_daily_kill_switch_usd"] == 100.0


def test_fee_floor_measured_from_live_fills():
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [{"mode": "live", "chain": "rh", "status": "closed", "exit_time": now_iso, "pnl_usd": 0.0,
             "entry_gas_usd": 0.08, "exit_gas_usd": 0.10}] * 5
    st = _State(rows)
    eng = BankrollEngine(st, st.db)
    f = asyncio.run(eng.measure_rh_fee_floor())
    assert f["gas_round_trip_usd"] == 0.18 and f["samples"] == 5
    assert f["min_stake_usd"] == 3.6          # $0.18 / 5% drag
    assert f["break_even_pct_at_min_stake"] == 7.0  # 2% curve fee + 5% gas drag


def test_sizing_not_applied_when_disabled():
    st = _State([])
    st.config.bankroll_sizing_enabled = False
    eng = BankrollEngine(st, st.db)
    snap = asyncio.run(eng.refresh())
    assert snap["applied"] is False and st.saved == 0 and st.config.max_trade_usd == BotConfig().max_trade_usd
    assert st.config.rh_max_trade_usd == BotConfig().rh_max_trade_usd


def _g(n, exp, exp7=None, payoff=1.5):
    return {"n": n, "expectancy_usd": exp, "expectancy_7d": exp7, "n_7d": n, "payoff_ratio": payoff,
            "winrate": 50.0, "total_usd": exp * n, "sl_share": 0, "tp_share": 0}


