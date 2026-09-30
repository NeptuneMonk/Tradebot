"""_persist_metrics coalesces into one launches.bulk_write per flush instead of one update_one per mint."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from bot import BotState


def _bucket(lid):
    return {"launch_id": lid, "buyers": {"a", "b"}, "sol_inflow_lamports": 10 ** 9, "buy_count": 2, "curve_fill_pct": 3.0,
            "social_score": 1, "usd_market_cap": 1000.0, "peak_mc_usd": 0.0}


def test_persist_metrics_batches_into_single_bulk_write():
    async def run():
        b = BotState.__new__(BotState)
        b.db = MagicMock()
        b.db.launches.update_one = AsyncMock()
        b.db.launches.bulk_write = AsyncMock()
        b.tracking = {"m1": _bucket("L1"), "m2": _bucket("L2")}
        b.recent_launches = [{"id": "L1"}]
        b._metrics_pending = {}
        await b._persist_metrics("m1")
        await b._persist_metrics("m2")
        b.tracking["m1"]["buy_count"] = 9
        await b._persist_metrics("m1")           # second write for the same launch coalesces (last wins)
        b.db.launches.update_one.assert_not_called()
        assert set(b._metrics_pending) == {"L1", "L2"} and b._metrics_pending["L1"]["buy_count"] == 9
        assert b.recent_launches[0]["buy_count"] == 9
        n = await b._flush_metrics()
        assert n == 2 and b.db.launches.bulk_write.await_count == 1 and b._metrics_pending == {}
        ops = b.db.launches.bulk_write.await_args.args[0]
        assert {op._filter["_id"] for op in ops} == {"L1", "L2"}
        assert await b._flush_metrics() == 0 and b.db.launches.bulk_write.await_count == 1
    asyncio.run(run())
