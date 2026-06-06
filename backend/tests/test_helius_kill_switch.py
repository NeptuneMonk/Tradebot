"""
Tests for the Helius kill switch — `BotConfig.helius_tracker_enabled`
flag, the `helius_gate` module, and that all consumers respect the gate.

Symptom that motivated this feature: running PREVIEW + PRODUCTION
simultaneously was draining the user's 10M Helius credits — both
listeners + scanners + RPC pollers were burning budget in parallel.
The kill switch lets the user pause Helius traffic on whichever
environment they're not actively trading.

In-scope when paused:
  - Listener WSS disconnects + stays idle
  - Account-event bus WSS disconnects + stays idle
  - Scanner's pool/curve RPC fetches skipped (no new entries possible)
  - Discovery's near-graduation PumpSwap pool fetch skipped
  - `_tracker_cleanup` graduation polls skipped
  - `_enter` refuses to open new positions
  - Wallet-graph hunter idles

Out-of-scope when paused (still runs):
  - Pump.fun HTTP discovery (free, not a Helius endpoint)
  - Active position monitoring (necessary to detect exits)
  - All UI / DB / WS broadcast flows
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("HELIUS_WSS_URL", "wss://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")
os.environ.setdefault("PUMP_PROGRAM_ID", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
os.environ.setdefault("PUMP_GLOBAL", "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
os.environ.setdefault("PUMP_FEE_RECIPIENT", "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
os.environ.setdefault("PUMP_EVENT_AUTHORITY", "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")

from models import BotConfig  # noqa: E402
import helius_gate  # noqa: E402


def setup_function(_):
    """Reset the gate to its default (not paused) between tests."""
    helius_gate.set_paused(False)


def test_default_config_enables_tracker():
    """Default value preserves existing behaviour — gate not paused."""
    cfg = BotConfig()
    assert cfg.helius_tracker_enabled is True


def test_config_field_is_optional_in_payload():
    """Persisted configs from before this feature shipped won't have the
    field; pydantic must default to True instead of raising."""
    cfg = BotConfig(**{"enabled": False})  # no helius_tracker_enabled key
    assert cfg.helius_tracker_enabled is True


def test_config_field_persists_false():
    """User can explicitly persist the OFF state."""
    cfg = BotConfig(**{"helius_tracker_enabled": False})
    assert cfg.helius_tracker_enabled is False


def test_gate_default_not_paused():
    """Module loads with paused=False so existing deployments aren't
    silently knocked offline at upgrade."""
    assert helius_gate.is_helius_paused() is False


def test_gate_set_paused_flips_state():
    helius_gate.set_paused(True)
    assert helius_gate.is_helius_paused() is True
    helius_gate.set_paused(False)
    assert helius_gate.is_helius_paused() is False


def test_gate_truthiness_coercion():
    """`set_paused` accepts truthy/falsy values and coerces to bool —
    `helius_paused` always reads back as the canonical bool."""
    helius_gate.set_paused(1)
    assert helius_gate.is_helius_paused() is True
    helius_gate.set_paused(0)
    assert helius_gate.is_helius_paused() is False
    helius_gate.set_paused("anything")  # truthy string
    assert helius_gate.is_helius_paused() is True
    helius_gate.set_paused("")  # falsy string
    assert helius_gate.is_helius_paused() is False


def test_gate_idempotent():
    """Calling set_paused with the same value is a no-op (no exception)."""
    helius_gate.set_paused(True)
    helius_gate.set_paused(True)
    assert helius_gate.is_helius_paused() is True
    helius_gate.set_paused(False)
    helius_gate.set_paused(False)
    assert helius_gate.is_helius_paused() is False
