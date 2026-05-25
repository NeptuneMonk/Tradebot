"""
Auto-tuner regression tests. Verifies:
  - getPriorityFeeEstimate path is preferred when it returns a value
  - Falls back to getRecentPrioritizationFees if Helius estimate fails
  - NORMAL floor is enforced on quiet networks
  - PriorityFeeLevels response shape ({"high": N}) is parsed correctly
"""
import asyncio
import os
from unittest.mock import patch, AsyncMock

# Stub env before importing speed_modes (its deps may touch env)
os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

from speed_modes import PriorityFeeAutoTuner, PRESETS  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_helius_estimate_priorityFeeEstimate_shape():
    """API returns {"priorityFeeEstimate": <num>} — direct read."""
    tuner = PriorityFeeAutoTuner()
    mock_resp = {"result": {"priorityFeeEstimate": 750_000}}
    with patch("solana_client.rpc_call", new=AsyncMock(return_value=mock_resp)):
        v = _run(tuner._fetch_helius_priority_estimate())
    assert v == 750_000


def test_helius_estimate_priorityFeeLevels_shape():
    """API returns {"priorityFeeLevels": {"high": <num>}} — parse high tier."""
    tuner = PriorityFeeAutoTuner()
    mock_resp = {"result": {"priorityFeeLevels": {
        "min": 0, "low": 100, "medium": 50_000, "high": 1_200_000,
        "veryHigh": 5_000_000, "unsafeMax": 50_000_000,
    }}}
    with patch("solana_client.rpc_call", new=AsyncMock(return_value=mock_resp)):
        v = _run(tuner._fetch_helius_priority_estimate())
    assert v == 1_200_000


def test_helius_estimate_quiet_network_clamps_to_normal_floor():
    """When the network is quiet (estimate < NORMAL preset), enforce
    the NORMAL floor so we still land within 1-2 slots."""
    tuner = PriorityFeeAutoTuner()
    mock_resp = {"result": {"priorityFeeEstimate": 50}}  # absurdly low
    with patch("solana_client.rpc_call", new=AsyncMock(return_value=mock_resp)):
        v = _run(tuner._fetch_helius_priority_estimate())
    assert v == PRESETS["normal"][0]


def test_helius_estimate_returns_none_on_error():
    """RPC error → None; caller falls back to getRecentPrioritizationFees."""
    tuner = PriorityFeeAutoTuner()
    with patch("solana_client.rpc_call", new=AsyncMock(side_effect=RuntimeError("boom"))):
        v = _run(tuner._fetch_helius_priority_estimate())
    assert v is None


def test_helius_estimate_returns_none_on_empty_result():
    """API returned no estimate field → None."""
    tuner = PriorityFeeAutoTuner()
    with patch("solana_client.rpc_call", new=AsyncMock(return_value={"result": {}})):
        v = _run(tuner._fetch_helius_priority_estimate())
    assert v is None


def test_helius_estimate_passes_pump_account_keys():
    """Request body MUST include accountKeys for Pump.fun + PumpSwap so
    Helius's estimate reflects our actual transaction workload, not
    network-wide noise."""
    tuner = PriorityFeeAutoTuner()
    captured = {}

    async def fake_rpc(method, params):
        captured["method"] = method
        captured["params"] = params
        return {"result": {"priorityFeeEstimate": 500_000}}

    with patch("solana_client.rpc_call", new=fake_rpc):
        _run(tuner._fetch_helius_priority_estimate())

    assert captured["method"] == "getPriorityFeeEstimate"
    body = captured["params"][0]
    assert "accountKeys" in body
    keys = body["accountKeys"]
    # Pump.fun + PumpSwap program ids
    assert "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P" in keys
    assert "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA" in keys
    assert body["options"] == {"priorityLevel": "High", "recommended": True}
