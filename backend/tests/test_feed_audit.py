"""Feed audit follow-ups: a benched rh_pons book shows as `doctor-breaker` on the feed (no per-second skip spam), and a
creator whose sniped launch stopped out is ignored by the sniper for a cooldown."""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_rh_paper import make_state, hot_bucket, TOKEN


def test_benched_rh_book_marks_passing_tokens_as_doctor_breaker_without_entering():
    st = make_state()
    now = time.time()
    b = hot_bucket(st.rh_discovery, now)
    assert st.rh_paper._gates(TOKEN, b, now) is None
    st.live_doctor = SimpleNamespace(book_paused=lambda book: book == "rh_pons")
    st.rh_paper._scan_entries(now)
    assert b["gate_reason"] == "doctor-breaker" and TOKEN not in st.rh_paper._pending_entries
    assert st.rh_paper.stats["skip_reasons"] == {"curve:doctor-breaker": 1}
    st.live_doctor = SimpleNamespace(book_paused=lambda book: False)

    async def _go():
        st.rh_paper._scan_entries(now)
        await asyncio.sleep(0)
    import asyncio
    asyncio.run(_go())
    assert b["gate_reason"] == "pass" and (TOKEN in st.rh_paper._pending_entries or TOKEN in st.rh_paper.pending_buys)


def test_snipe_creator_cooldown_is_set_on_stop_loss_and_checked_by_the_sniper():
    src = Path(__file__).resolve().parents[1].joinpath("bot.py").read_text()
    assert 'self.creator_sl_cooldown_until[trade_doc["creator"]]' in src
    assert 'cc_until = self.creator_sl_cooldown_until.get(launch.creator or "", 0.0)' in src
    from models import BotConfig
    assert BotConfig().snipe_creator_cooldown_minutes == 30.0
