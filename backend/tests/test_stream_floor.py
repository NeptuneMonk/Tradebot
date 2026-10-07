"""Stream-driven inflow floor: rolling buy-inflow ring per launch, seeding the instant an in-gate token clears the floor."""
import time

import pytest

import stream_floor as sf_mod
from bot import BotState
from models import BotConfig


class _DB:
    def __getattr__(self, name):
        return self

    async def find_one(self, *a, **k):
        return None

    async def update_one(self, *a, **k):
        return None


def _state(**cfg) -> BotState:
    st = BotState(_DB())
    st.config = BotConfig(band_new_min_age_min=20, band_new_max_age_min=40, scanner_min_recent_inflow_sol=3.0, scanner_recent_inflow_window_s=300, **cfg)
    return st


def _buy(mint, sol, vsr=40_000_000_000, vtr=900_000_000_000_000):
    return {"mint": mint, "is_buy": True, "sol_amount": int(sol * 1e9), "virtual_sol_reserves": vsr, "virtual_token_reserves": vtr, "user": "u"}


def test_rolling_inflow_window_forgets_old_buys():
    st = _state()
    sf = st.stream_floor
    t0 = 1_000_000.0
    sf.on_launch({"mint": "A", "creator": "c", "name": "a", "symbol": "A", "bonding_curve": "bc"}, now=t0)
    sf.on_trade(_buy("A", 2.0), now=t0 + 5)
    sf.on_trade(_buy("A", 1.5), now=t0 + 100)
    sf.on_trade({"mint": "A", "is_buy": False, "sol_amount": int(9e9)}, now=t0 + 101)      # sells never count
    assert sf.inflow_sol("A", t0 + 120) == pytest.approx(3.5)
    assert sf.inflow_sol("A", t0 + 320) == pytest.approx(1.5)        # first buy aged out of the 5-min window
    assert sf.inflow_sol("A", t0 + 1000) == 0.0
    assert sf.on_trade(_buy("ZZZ", 5.0)) is None and "ZZZ" not in sf.pulses     # unknown mint (pre-start launch): ignored


@pytest.mark.asyncio
async def test_tick_seeds_in_gate_tokens_that_clear_the_floor_and_drops_aged_pulses(monkeypatch):
    st = _state()
    sf = st.stream_floor
    seeded: list[tuple] = []

    async def fake_seed(coin, created_s, is_pumpswap=False, pool_state=None):
        seeded.append((coin["mint"], round(created_s), is_pumpswap, coin["usd_market_cap"] > 0))
        st.tracking[coin["mint"]] = {"start": created_s, "protocol": "pumpfun"}
    monkeypatch.setattr(st.discovery, "_seed_token", fake_seed)

    async def fake_price():
        return 150.0
    import solana_client
    monkeypatch.setattr(solana_client, "get_sol_usd_price", fake_price)

    now = time.time()
    for mint, age_min in (("young", 5), ("hot", 25), ("cold", 25), ("old", 45)):
        sf.on_launch({"mint": mint, "creator": "c", "name": mint, "symbol": mint, "bonding_curve": "bc"}, now=now - age_min * 60)
    for m in ("young", "hot", "old"):
        sf.on_trade(_buy(m, 4.0), now=now - 30)
    sf.on_trade(_buy("cold", 1.0), now=now - 30)
    assert await sf.tick(now) == 1
    assert seeded == [("hot", round(now - 25 * 60), False, True)]
    assert st.tracking["hot"]["alive_inflow_sol"] == 4.0 and st.tracking["hot"]["stream_seeded"] is True
    assert "old" not in sf.pulses and "young" in sf.pulses and "cold" in sf.pulses   # past the gate → pulse dropped; others keep waiting
    # cold token heats up a minute later → seeded on the next tick, without any API pull
    sf.on_trade(_buy("cold", 2.5), now=now + 25)
    assert await sf.tick(now + 30) == 1 and seeded[-1][0] == "cold"
    assert await sf.tick(now + 33) == 0                                 # already tracked: no re-seed
    assert sf.snapshot()["seeded_1m"] == 2


def test_pulse_cap_drops_the_oldest(monkeypatch):
    monkeypatch.setattr(sf_mod, "MAX_PULSES", 3)
    st = _state()
    sf = st.stream_floor
    for i in range(4):
        sf.on_launch({"mint": f"m{i}", "creator": "c"}, now=1000.0 + i)
    assert set(sf.pulses) == {"m1", "m2", "m3"} and sf.stats["dropped"] == 1
