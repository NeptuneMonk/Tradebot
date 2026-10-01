"""Rolling no-momentum: the peak must keep improving by `no_momentum_min_mfe_pct` every `no_momentum_after_s`."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from exits import no_momentum_stalled


def _cfg(**kw):
    return SimpleNamespace(**{"no_momentum_exit_enabled": True, "no_momentum_after_s": 2, "no_momentum_min_mfe_pct": 10.0, **kw})


def test_first_check_is_peak_vs_entry_like_the_old_one_shot():
    pos = {}
    assert no_momentum_stalled(_cfg(), pos, 100.0, 1.9, 1.0, 1.05) is None       # too early
    assert round(no_momentum_stalled(_cfg(), pos, 100.0, 2.0, 1.0, 1.05), 6) == 5.0   # +5% < 10% → stalled
    assert pos["_nm_checked"] is True and pos["_nm_peak_ref"] == 1.05


def test_user_case_peaks_12_then_hovers_6_to_12_is_sold_at_the_next_window():
    """Peak +12% passes the first check; two seconds later the peak has not improved by 10% → stalled."""
    pos = {}
    assert no_momentum_stalled(_cfg(), pos, 100.0, 2.0, 1.0, 1.12) is None       # +12% ≥ 10%: has momentum
    assert no_momentum_stalled(_cfg(), pos, 101.0, 3.0, 1.0, 1.12) is None       # inside the window: no check yet
    assert no_momentum_stalled(_cfg(), pos, 102.0, 4.0, 1.0, 1.12) == 0.0        # peak unchanged for 2s → stalled
    pos = {}
    no_momentum_stalled(_cfg(), pos, 100.0, 2.0, 1.0, 1.12)
    assert no_momentum_stalled(_cfg(), pos, 102.0, 4.0, 1.0, 1.18) is not None   # +5.4% vs ref 1.12 → still stalled


def test_keeps_running_while_the_peak_keeps_improving():
    pos = {}
    assert no_momentum_stalled(_cfg(), pos, 100.0, 2.0, 1.0, 1.12) is None
    assert no_momentum_stalled(_cfg(), pos, 102.0, 4.0, 1.0, 1.25) is None       # +11.6% vs 1.12
    assert no_momentum_stalled(_cfg(), pos, 104.0, 6.0, 1.0, 1.40) is None       # +12% vs 1.25
    assert pos["_nm_peak_ref"] == 1.40


def test_disabled_or_zero_window_never_checks():
    assert no_momentum_stalled(_cfg(no_momentum_exit_enabled=False), {}, 100.0, 50.0, 1.0, 1.0) is None
    assert no_momentum_stalled(_cfg(no_momentum_after_s=0), {}, 100.0, 50.0, 1.0, 1.0) is None


def test_peak_below_entry_is_treated_as_zero_gain():
    pos = {}
    assert no_momentum_stalled(_cfg(), pos, 100.0, 2.0, 1.0, 0.9) == 0.0
    assert pos["_nm_peak_ref"] == 1.0
