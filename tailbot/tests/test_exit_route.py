"""Sell-route sanity check: a buy needs a quote back out, and the round trip stays under 25%."""
import asyncio

from tailbot.market import MarketInfo
from tailbot.quotes import Quote, curve_round_trip_impact_pct, exit_allowed, round_trip_sol_out
from tests.test_engine import MINT, Quotes, buy, make


DEEP = (30_000_000_000, 1_000_000_000_000_000)  # 30 virtual SOL, typical curve
THIN = (500_000_000, 1_000_000_000_000)  # 0.5 SOL behind the price


def test_deep_curve_round_trip_is_under_the_default_cap():
    impact = curve_round_trip_impact_pct(*DEEP, sol_in_lamports=100_000_000, fee_bps=100, extra_slip_bps=100)
    assert impact is not None and impact < 25


def test_thin_pool_and_missing_route_are_rejected():
    impact = curve_round_trip_impact_pct(*THIN, sol_in_lamports=400_000_000, fee_bps=100, extra_slip_bps=100)
    assert impact is not None and impact > 25
    assert curve_round_trip_impact_pct(0, DEEP[1], 100_000_000, 100, 100) is None
    assert round_trip_sol_out(0, DEEP[1], 100_000_000, 100, 100) is None
    assert exit_allowed(0.1, None, 25) is False
    assert exit_allowed(0.1, 0.0, 25) is False
    # Cap of 0 still requires a route, and then allows any loss.
    assert exit_allowed(0.1, 0.01, 0) is True
    assert exit_allowed(0.1, 0.09, 25) is True
    assert exit_allowed(0.1, 0.05, 25) is False


def test_engine_skips_when_no_sell_quote(tmp_path):
    engine, box, _clock = make(tmp_path)

    async def unknown(mint, pool=None):
        return MarketInfo("unknown", None, None, None)

    class NoSell(Quotes):
        async def sell(self, mint, tokens_raw, market=None, decimals=6):
            return None

    engine.market_loader = unknown
    engine.quotes = NoSell(0.01, 0.01)

    async def go():
        await engine.on_leader_fill(buy(signature="noroute"))

    asyncio.run(go())
    assert engine.db.open_positions() == []
    assert any("no exit route" in text for text, _m in box.msgs)


def test_engine_skips_a_jupiter_round_trip_over_25_percent(tmp_path):
    engine, box, _clock = make(tmp_path / "wide")

    async def paired(mint, pool=None):
        return MarketInfo("pumpswap", None, None, None)

    class Wide(Quotes):
        async def sell(self, mint, tokens_raw, market=None, decimals=6):
            ui = tokens_raw / (10 ** decimals)
            # Buy was priced at 0.01; selling at 0.004 is a 60% round trip.
            return Quote(price_sol_per_ui=0.004, sol_out=ui * 0.004, tokens_raw=tokens_raw, source="jupiter", decimals=decimals)

    engine.market_loader = paired
    engine.quotes = Wide(0.01, 0.004)

    async def go():
        await engine.on_leader_fill(buy(signature="wide", mint=MINT))

    asyncio.run(go())
    assert engine.db.open_positions() == []
    assert any(text.startswith("SKIP") and "no exit route" in text for text, _m in box.msgs)


def test_engine_copies_a_paired_coin_with_a_tight_jupiter_exit(tmp_path):
    engine, box, _clock = make(tmp_path / "paired")

    async def paired(mint, pool=None):
        return MarketInfo("unknown", None, None, None)

    class Tight(Quotes):
        async def sell(self, mint, tokens_raw, market=None, decimals=6):
            ui = tokens_raw / (10 ** decimals)
            return Quote(price_sol_per_ui=0.009, sol_out=ui * 0.009, tokens_raw=tokens_raw, source="jupiter", decimals=decimals)

    engine.market_loader = paired
    engine.quotes = Tight(0.01, 0.009)

    async def go():
        await engine.on_leader_fill(buy(signature="paired"))

    asyncio.run(go())
    assert engine.db.open_positions()
    assert any(text.startswith("COPY") for text, _m in box.msgs)
