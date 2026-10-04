"""Phase 2 simple controls: operator book switches (scalp / hunt / runner) gate new entries and promotions."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from models import BotConfig
from bot import BotState


def test_book_switches_default_on_and_autopilot_default_off():
    c = BotConfig()
    assert c.book_scalp_enabled and c.book_hunt_enabled and c.book_runner_enabled
    assert c.autopilot_enabled is False
    assert BotConfig(book_runner_enabled=False).book_runner_enabled is False


def test_runner_switch_off_blocks_promotion():
    async def run():
        b = BotState.__new__(BotState)
        b.config = BotConfig(book_runner_enabled=False)
        slot = {"trade": {"book": "scalp", "r_usd": 1.0}}
        b.active_trades = {"m": slot}
        assert await b._try_promote("m", slot, 1.0, 50.0) is False
    asyncio.run(run())
