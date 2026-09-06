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
        if "exit_time" in q:
            out = [r for r in out if r["exit_time"] >= q["exit_time"]["$gte"]]
        return _Cur(out)


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
    assert d == {"max_trade_usd": 20.0, "min_trade_usd": 5.0, "max_concurrent_positions": 12, "daily_kill_switch_usd": 100.0}
    d = BankrollEngine.derive(20000.0, BotConfig(risk_per_trade_pct=5.0, max_exposure_pct=25.0, daily_loss_limit_pct=10.0))
    assert d["max_trade_usd"] == 100.0 and d["max_concurrent_positions"] == 5 and d["daily_kill_switch_usd"] == 1000.0
    assert BankrollEngine.derive(30.0, BotConfig())["max_trade_usd"] == 1.0


def test_refresh_applies_sizing_and_compounds_paper_pnl():
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    st = _State([{"mode": "paper", "pnl_usd": 250.0, "exit_time": now_iso}])
    eng = BankrollEngine(st, st.db)
    snap = asyncio.run(eng.refresh())
    assert snap["bankroll_usd"] == 1250.0 and snap["bankroll_source"] == "paper"
    assert st.config.max_trade_usd == 25.0 and st.config.max_concurrent_positions == 12 and st.saved == 1
    assert not snap["governor_active"] and eng.size_mult() == 1.0


def test_governor_engages_on_drawdown_and_releases():
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    st = _State([{"mode": "paper", "pnl_usd": -80.0, "exit_time": now_iso}])   # -8% of $1000 → past the 5% line
    eng = BankrollEngine(st, st.db)
    snap = asyncio.run(eng.refresh())
    assert snap["governor_active"] and eng.size_mult() == 0.5 and "-8." in snap["governor_reason"]
    asyncio.run(eng.release_governor())
    assert eng.size_mult() == 1.0


def test_sizing_not_applied_when_disabled():
    st = _State([])
    st.config.bankroll_sizing_enabled = False
    eng = BankrollEngine(st, st.db)
    snap = asyncio.run(eng.refresh())
    assert snap["applied"] is False and st.saved == 0 and st.config.max_trade_usd == BotConfig().max_trade_usd


def _g(n, exp, exp7=None, payoff=1.5):
    return {"n": n, "expectancy_usd": exp, "expectancy_7d": exp7, "n_7d": n, "payoff_ratio": payoff,
            "winrate": 50.0, "total_usd": exp * n, "sl_share": 0, "tp_share": 0}


def test_doctor_risk_dial_down_and_up():
    stats = {"global": _g(20, -0.05, -0.02)}
    p = dl.propose({"bankroll_sizing_enabled": True, "risk_per_trade_pct": 2.0}, stats, 15)
    assert p["key"] == "risk_per_trade_pct" and p["value"] == 1.5 and p["book"] == "global"
    assert dl.propose({"bankroll_sizing_enabled": True, "risk_per_trade_pct": 0.5}, stats, 15) is None
    assert dl.propose({"bankroll_sizing_enabled": False, "risk_per_trade_pct": 2.0}, stats, 15) is None
    stats = {"global": _g(40, 0.08, 0.05)}
    p = dl.propose({"bankroll_sizing_enabled": True, "risk_per_trade_pct": 2.0}, stats, 15)
    assert p["key"] == "risk_per_trade_pct" and p["value"] == 2.5
    assert dl.propose({"bankroll_sizing_enabled": True, "risk_per_trade_pct": 5.0}, stats, 15) is None
    assert "risk_per_trade_pct" in dl.ALLOWED_KEYS and "max_trade_usd" in dl.FORBIDDEN_KEYS
