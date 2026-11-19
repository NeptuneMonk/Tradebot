"""Don't chase. If our quote is more than ``premium_pct`` above the leader's fill,
re-check every ``recheck_s`` until ``window_s`` has passed, then skip.
"""
from __future__ import annotations

from dataclasses import dataclass

from .quotes import Quote


@dataclass
class ChaseResult:
    ok: bool
    quote: Quote | None
    reason: str
    premium_pct: float | None = None


async def run_chase(
    *,
    leader_price: float,
    quote_fn,
    premium_pct: float,
    recheck_s: float,
    window_s: float,
    sleep,
    now,
    cancelled=None,
) -> ChaseResult:
    if leader_price <= 0:
        return ChaseResult(False, None, "leader fill unpriced")
    started = now()
    deadline = started + window_s
    last_reason = "no quote"
    last_quote = None
    while True:
        if cancelled and cancelled():
            return ChaseResult(False, last_quote, "leader flipped before entry")
        quote = None
        try:
            quote = await quote_fn()
        except Exception as exc:
            last_reason = f"quote failed: {exc}"
        if quote is not None and quote.price_sol_per_ui > 0:
            last_quote = quote
            premium = (quote.price_sol_per_ui / leader_price - 1.0) * 100.0
            if premium <= premium_pct:
                return ChaseResult(True, quote, "ok", premium)
            last_reason = f"quote {premium:.1f}% above leader fill (max {premium_pct:g}%)"
        t = now()
        if t >= deadline - 1e-9:
            return ChaseResult(False, last_quote, f"chase: {last_reason} after {window_s:g}s")
        await sleep(min(recheck_s, max(0.0, deadline - t)))
