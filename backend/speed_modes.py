"""
Speed mode presets — bundles priority fee + slippage into named tiers.

The bot exposes a single slider in the UI. Each tier corresponds to a typical
Solana network condition:

  eco        : quiet network, every cent counts
  normal     : balanced default
  fast       : active hours
  aggressive : pump.fun frenzy / NFT-mint contention
  turbo      : must-land MEV-territory
  auto       : dynamically tuned from Helius getRecentPrioritizationFees
  manual     : user controls raw inputs (legacy path, no auto-overwrite)

`speed_mode_resolve` returns the effective (priority_fee_microlamports,
slippage_bps, exit_slippage_bps) triple for a given mode. The bot calls this
right before submitting any tx so live changes propagate immediately.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from solana_client import LAMPORTS_PER_SOL

logger = logging.getLogger("speed_modes")

# Base Solana per-signature fee (always paid, independent of priority)
BASE_SIG_FEE_LAMPORTS = 5_000

# Default compute-unit limits used in the bot. PumpFun bonding-curve trades use
# the default 200k, PumpSwap AMM trades bump to 400k (see bot.py _enter).
CU_PUMPFUN = 200_000
CU_PUMPSWAP = 400_000

# Preset tiers: (priority_fee_microlamports, slippage_bps, exit_slippage_bps)
# Slippage values bumped 2026-05-24: micro-stake Pump.fun launches move fast
# enough that a 500bps quote-to-land lag was failing with TooMuchSolRequired
# (6002) — wider tolerance lets buys land at the cost of ~$0.01 worst case.
PRESETS: dict[str, tuple[int, int, int]] = {
    "eco":        (100_000,  800, 800),
    "normal":     (300_000,  1000, 1000),
    "fast":       (700_000,  1500, 1500),
    "aggressive": (1_500_000, 2000, 2000),
    "turbo":      (3_000_000, 2500, 2500),
}

# Ordered slider value -> mode name (UI uses index 0..5; index 5 == "auto")
SLIDER_ORDER = ["eco", "normal", "fast", "aggressive", "turbo", "auto"]


def speed_mode_resolve(
    mode: str,
    fallback_priority: int,
    fallback_slip: int,
    fallback_exit_slip: int,
    auto_priority_cache: Optional[int] = None,
) -> tuple[int, int, int]:
    """Return effective (priority_fee, slippage_bps, exit_slippage_bps) for a mode.

    `auto_priority_cache` is the last value resolved by the auto-tuner background
    task (None if it hasn't run yet — fall back to NORMAL preset for safety).
    """
    if mode in PRESETS:
        return PRESETS[mode]
    if mode == "auto":
        # Use cached network-tuned value or fall back to NORMAL
        prio = auto_priority_cache if auto_priority_cache is not None else PRESETS["normal"][0]
        # Slippage in auto mode tracks priority tier — higher network heat
        # implies more volatility and we need wider slippage tolerance.
        slip = _slip_for_priority(prio)
        return prio, slip, slip
    # "manual" or any unrecognised value — keep the user's raw config
    return fallback_priority, fallback_slip, fallback_exit_slip


def _slip_for_priority(priority_fee_microlamports: int) -> int:
    """Map a priority-fee value to a reasonable slippage tier."""
    if priority_fee_microlamports < 200_000:
        return 300
    if priority_fee_microlamports < 500_000:
        return 400
    if priority_fee_microlamports < 1_000_000:
        return 500
    if priority_fee_microlamports < 2_000_000:
        return 700
    return 1000


def estimate_tx_fee_sol(priority_fee_microlamports: int, compute_units: int) -> float:
    """Estimated SOL cost of a single tx given priority fee + CU limit.
    base sig fee + (priority µLamp × CU / 1e6) lamports."""
    priority_lamports = (priority_fee_microlamports * compute_units) // 1_000_000
    total_lamports = BASE_SIG_FEE_LAMPORTS + priority_lamports
    return total_lamports / LAMPORTS_PER_SOL


class PriorityFeeAutoTuner:
    """Polls Helius getRecentPrioritizationFees every POLL_S seconds and exposes
    the 75th-percentile fee as `current_value`. Used when speed_mode='auto'.

    Falls back to the NORMAL preset on errors so trading never stalls.
    """

    POLL_S = 30.0

    def __init__(self):
        self.current_value: Optional[int] = None
        self.last_poll_ts: float = 0.0
        self._task: Optional[asyncio.Task] = None

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        # Brief stagger so the listener subscribes first
        await asyncio.sleep(8.0)
        while True:
            try:
                # Prefer Helius's own recommendation API (context-aware for
                # our Pump.fun + PumpSwap workload); fall back to the
                # network-wide p75 if Helius errors or returns no value.
                v = await self._fetch_helius_priority_estimate()
                if v is None:
                    v = await self._fetch_p75_priority_fee()
                if v is not None:
                    # Clamp into the same range as the presets so users don't
                    # get surprised by extreme network outliers.
                    v = max(PRESETS["eco"][0], min(PRESETS["turbo"][0], v))
                    self.current_value = v
                    self.last_poll_ts = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"auto-tuner poll failed: {e}")
            await asyncio.sleep(self.POLL_S)

    async def _fetch_helius_priority_estimate(self) -> Optional[int]:
        """Helius's `getPriorityFeeEstimate` returns a recommended
        priority fee tuned for the supplied accountKeys (i.e. the
        programs our tx will actually write to). This is more accurate
        than the generic network p75 because it weighs recent fees
        paid by txs touching the same accounts we will touch.

        Returns microlamports/CU on success, None on any failure
        (caller falls back to the network-wide p75).
        """
        from solana_client import rpc_call
        # Use HIGH priority level — that's the closest match to "AUTO"
        # in our preset ladder (between FAST and AGGRESSIVE).
        try:
            res = await rpc_call(
                "getPriorityFeeEstimate",
                [{
                    # Programs our trade txs hit. Helius weighs these to compute
                    # an estimate that actually matches our workload.
                    "accountKeys": [
                        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump.fun
                        "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap
                    ],
                    "options": {"priorityLevel": "High", "recommended": True},
                }],
            )
        except Exception as e:
            logger.debug(f"getPriorityFeeEstimate failed: {e}")
            return None
        result = res.get("result") or {}
        # API can return either {"priorityFeeEstimate": <num>} or
        # {"priorityFeeLevels": {"high": <num>, ...}} depending on options.
        est = result.get("priorityFeeEstimate")
        if est is None:
            levels = result.get("priorityFeeLevels") or {}
            est = levels.get("high")
        if est is None:
            return None
        v = int(est)
        # Even when Helius says ~0 (very calm block), keep at NORMAL floor
        # so we still land cleanly in 1-2 slots.
        if v < PRESETS["normal"][0]:
            v = PRESETS["normal"][0]
        return v

    async def _fetch_p75_priority_fee(self) -> Optional[int]:
        """Use Helius's getRecentPrioritizationFees and return the 75th
        percentile of recent slots' priority fees (in microlamports per CU)."""
        # solana_client imports at top would create a cycle; defer
        from solana_client import rpc_call
        try:
            res = await rpc_call("getRecentPrioritizationFees", [])
        except Exception:
            return None
        rows = res.get("result") or []
        if not rows:
            return None
        fees = sorted(int(r.get("prioritizationFee") or 0) for r in rows)
        if not fees:
            return None
        # 75th percentile
        idx = int(len(fees) * 0.75)
        idx = min(idx, len(fees) - 1)
        p75 = fees[idx]
        # Most slots are 0; bump to NORMAL when network is quiet so we still
        # land in 1-2 slots.
        if p75 < PRESETS["normal"][0]:
            p75 = PRESETS["normal"][0]
        return p75


# Global singleton — the bot wires this into BotState
auto_tuner = PriorityFeeAutoTuner()
