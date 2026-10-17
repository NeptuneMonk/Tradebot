"""EMERGENT_CHANGES #4 — seasoned-band MC ceiling."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import BotConfig  # noqa: E402


def test_config_has_seasoned_mc_ceiling_default_off():
    c = BotConfig()
    assert c.scanner_max_mc_usd_seasoned == 0.0 and c.scanner_max_mc_usd_new == 0.0


def test_scanner_source_gates_seasoned_mc_max():
    src = open(os.path.join(os.path.dirname(__file__), "..", "scanner.py")).read()
    assert src.count("scanner_max_mc_usd_seasoned") >= 2      # both the rank loop and the pre-rank pass
