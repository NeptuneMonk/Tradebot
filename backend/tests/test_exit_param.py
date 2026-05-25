"""
Targeted unit tests for TradingBot._exit_param — the per-trade override
reader that backs the Phase 2 greylist live mode (TP/SL/trail can differ
per open position based on the creator's strategy tier).

We deliberately avoid spinning up the full TradingBot (which pulls in
Helius, Mongo, the listener, etc.) — instead we call the method via
the unbound function so the test stays fast and offline.
"""
import os

os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("HELIUS_WSS_URL", "wss://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("PUMP_PROGRAM_ID", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
os.environ.setdefault("PUMP_GLOBAL", "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
os.environ.setdefault("PUMP_FEE_RECIPIENT", "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV")
os.environ.setdefault("PUMP_EVENT_AUTHORITY", "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
os.environ.setdefault("PUMPSWAP_PROGRAM_ID", "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
os.environ.setdefault("PUMPSWAP_GLOBAL_CONFIG", "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw")
os.environ.setdefault("PUMPSWAP_PROTOCOL_FEE_RECIPIENT", "JCRGumoE9Qi5BBgULTgdgTLjSgkCMSbF62ZZfGs84JeU")
os.environ.setdefault("PUMPSWAP_EVENT_AUTHORITY", "GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR")
os.environ.setdefault("WALLET_KEYPAIR_PATH", "/tmp/test_wallet.json")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from bot import BotState  # noqa: E402


def _bot():
    """Construct a bare BotState without starting any tasks."""
    return BotState(db=None)


def test_exit_param_falls_back_to_default_when_no_overrides():
    bot = _bot()
    slot = {"trade": {"id": "x"}}
    assert bot._exit_param(slot, "tp_pct", 20.0) == 20.0
    assert bot._exit_param(slot, "sl_pct", 15.0) == 15.0


def test_exit_param_falls_back_when_overrides_empty():
    bot = _bot()
    slot = {"greylist_overrides": {}}
    assert bot._exit_param(slot, "tp_pct", 20.0) == 20.0


def test_exit_param_returns_override_when_present():
    bot = _bot()
    slot = {"greylist_overrides": {"tp_pct": 35.0, "sl_pct": 12.0}}
    assert bot._exit_param(slot, "tp_pct", 20.0) == 35.0
    assert bot._exit_param(slot, "sl_pct", 15.0) == 12.0


def test_exit_param_falls_back_when_key_missing_from_overrides():
    """Partial override dicts shouldn't suppress other defaults."""
    bot = _bot()
    slot = {"greylist_overrides": {"tp_pct": 35.0}}
    assert bot._exit_param(slot, "tp_pct", 20.0) == 35.0
    assert bot._exit_param(slot, "sl_pct", 15.0) == 15.0  # not in overrides


def test_exit_param_handles_none_value_in_overrides():
    """None means 'no opinion' — must fall through to the default."""
    bot = _bot()
    slot = {"greylist_overrides": {"tp_pct": None}}
    assert bot._exit_param(slot, "tp_pct", 20.0) == 20.0


def test_exit_param_handles_malformed_slot():
    bot = _bot()
    # An empty dict and a totally missing key must both not crash
    assert bot._exit_param({}, "tp_pct", 7.5) == 7.5
    assert bot._exit_param(None, "tp_pct", 7.5) == 7.5


def test_exit_param_independent_per_slot():
    """Two simultaneously open trades must NOT see each other's overrides."""
    bot = _bot()
    slot_a = {"greylist_overrides": {"tp_pct": 35.0}}
    slot_b = {"greylist_overrides": {"tp_pct": 25.0}}
    slot_c = {}  # standard
    assert bot._exit_param(slot_a, "tp_pct", 20.0) == 35.0
    assert bot._exit_param(slot_b, "tp_pct", 20.0) == 25.0
    assert bot._exit_param(slot_c, "tp_pct", 20.0) == 20.0
