"""Creator Verdict — ONE holistic creator gate for every entry path.

Fuses the data points the bot already collects into a single decision per candidate:
  • creator greylist   — `greylist_blacklisted`, `greylist_out_of_band`, rug pattern, decayed score
  • our own book       — closed / parked trades on this creator's launches (losses with no wins)
  • creator solvency   — dev ALREADY OUT inside the launch window = FAVOR (no later dump overhang); mid-dump = CAUTION
  • creator audit      — when the master gate is enabled and the bucket carries a failed audit
  • smart wallets      — top-trader buys on THIS launch (favour; can lift a CAUTION; a manual smart wallet can lift a BLOCK)

Levels: BLOCK (refuse) · CAUTION (half size unless ≥ N smart wallets are in) · NEUTRAL · FAVOR (size boost from greylist tier).
Scanner, smart-wallet, re-entry and add-on buys must pass it; operator manual buys get the verdict back with a `force` escape hatch.
"""
from __future__ import annotations

import time

BLOCK_PATTERNS = {"untradeable_rug", "unpredictable_rug"}
CAUTION_PATTERNS = {"slow_rug_tradeable", "bimodal_dump_tradeable", "fake_hype_tradeable"}
LEVELS = ("block", "caution", "neutral", "favor")

_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_S = 20.0


def _cfg(cfg, key, default):
    v = getattr(cfg, key, None)
    return default if v is None else v


def smart_hit_counts(bucket: dict | None, ttl_s: float = 900.0) -> tuple[int, int]:
    """(total, manual) distinct smart wallets that bought this launch within ttl_s."""
    now = time.time()
    hits = (bucket or {}).get("smart_hits") or {}
    live = [h for h in hits.values() if now - float(h.get("ts") or 0) <= ttl_s]
    return len(live), sum(1 for h in live if h.get("kind") == "manual")


def resolve(level: str, reasons: list[str], cfg, *, smart_total: int, smart_manual: int, action: str,
            trigger_kind: str | None) -> tuple[str, float, list[str]]:
    """Apply the smart-wallet overrides and the caution sizing. Returns (level, size_mult, reasons)."""
    reasons = list(reasons)
    if level == "block" and action == "smart_wallet" and trigger_kind == "manual" and bool(_cfg(cfg, "creator_block_manual_smart_override", True)):
        level = "caution"
        reasons.append("override: triggered by a smart wallet YOU added (manual) → caution instead of block")
    if level == "caution":
        need = int(_cfg(cfg, "creator_caution_smart_override_n", 3))
        if smart_total >= need:
            level = "neutral"
            reasons.append(f"override: {smart_total} smart wallets in (≥{need}) → caution lifted")
            return level, 1.0, reasons
        return level, float(_cfg(cfg, "creator_caution_size_mult", 0.5)), reasons
    return level, 1.0, reasons


async def compute(db, cfg, mint: str, creator: str | None, bucket: dict | None, *, action: str = "scan",
                  trigger_kind: str | None = None, use_cache: bool = True) -> dict:
    """Full verdict for one candidate. Cheap: one `creators` read + one bounded `trades` read, cached 20 s per creator."""
    smart_total, smart_manual = smart_hit_counts(bucket)
    if not creator:
        level, size_mult, reasons = resolve("neutral", ["creator unknown"], cfg, smart_total=smart_total, smart_manual=smart_manual,
                                            action=action, trigger_kind=trigger_kind)
        return {"level": level, "size_mult": size_mult, "reasons": reasons, "creator": None, "smart_total": smart_total, "smart_manual": smart_manual}

    base = None
    if use_cache:
        c = _CACHE.get(creator)
        if c and time.time() - c[0] < CACHE_S:
            base = c[1]
    if base is None:
        base = await _base_verdict(db, cfg, creator)
        _CACHE[creator] = (time.time(), base)
        if len(_CACHE) > 5000:
            for k in list(_CACHE)[:2500]:
                _CACHE.pop(k, None)

    level, reasons = base["level"], list(base["reasons"])
    # tape-side (per launch, not cached). Operator preference: any dev selling inside the launch window is a plus —
    # what is sold cannot be dumped later. Fully out (≥ creator_dev_out_pct) → FAVOR; partial → noted, never a penalty.
    sold_q = float((bucket or {}).get("creator_sold_quote") or 0.0)
    if sold_q > 0:
        pct = _creator_sold_pct(bucket)
        if pct is not None and pct >= float(_cfg(cfg, "creator_dev_out_pct", 70.0)):
            reasons.append(f"dev already out: sold {pct:.0f}% of their stake inside the launch window (no dump overhang)")
            if level == "neutral":
                level = "favor"
        else:
            reasons.append(f"dev selling early: {pct:.0f}% of their stake gone ({sold_q:.2f} SOL) — that part can't be dumped later"
                           if pct is not None else f"dev selling early ({sold_q:.2f} SOL so far) — that part can't be dumped later")
    audit = (bucket or {}).get("creator_audit") or {}
    if bool(_cfg(cfg, "creator_audit_enabled", False)) and audit.get("verdict") in ("fail", "block"):
        level, reasons = "block", reasons + [f"creator audit failed: {audit.get('reason') or audit.get('failed') or 'see audit'}"]
    if smart_total:
        reasons.append(f"{smart_total} smart wallet{'s' if smart_total != 1 else ''} bought ({smart_manual} manual)")
        if level == "neutral":
            level = "favor"
    level, size_mult, reasons = resolve(level, reasons, cfg, smart_total=smart_total, smart_manual=smart_manual, action=action, trigger_kind=trigger_kind)
    if level == "favor" and base.get("greylist_size_mult"):
        size_mult = float(base["greylist_size_mult"])
    return {"level": level, "size_mult": round(size_mult, 3), "reasons": reasons, "creator": creator, "pattern": base.get("pattern"),
            "score": base.get("score"), "own_wins": base.get("own_wins"), "own_losses": base.get("own_losses"), "own_win_rate_pct": base.get("own_win_rate_pct"),
            "smart_total": smart_total, "smart_manual": smart_manual, "ts": time.time()}


def _worse(a: str, b: str) -> str:
    return a if LEVELS.index(a) <= LEVELS.index(b) else b


def _creator_sold_pct(bucket: dict | None) -> float | None:
    try:
        from creator_solvency import creator_sold_pct
        pct, _proxy = creator_sold_pct(bucket or {}, None)
        return float(pct)
    except Exception:
        return None


async def _base_verdict(db, cfg, creator: str) -> dict:
    reasons: list[str] = []
    level = "neutral"
    gc = await db.creators.find_one({"_id": creator}, {"_id": 0, "greylist_blacklisted": 1, "greylist_out_of_band": 1, "greylist_pattern": 1,
                                                       "greylist_score": 1, "greylist_score_updated_at": 1, "greylist_n_failed": 1,
                                                       "tokens_created": 1, "tokens_failed": 1}) or {}
    pattern = gc.get("greylist_pattern")
    score = None
    greylist_size_mult = None
    if gc.get("greylist_blacklisted"):
        level, reasons = "block", reasons + ["greylist: blacklisted creator"]
    if pattern in BLOCK_PATTERNS:
        level, reasons = "block", reasons + [f"greylist pattern: {pattern}"]
    elif pattern in CAUTION_PATTERNS:
        level = _worse(level, "caution")
        reasons.append(f"greylist pattern: {pattern} (tradeable but dumps)")
    if gc.get("greylist_out_of_band"):
        level = _worse(level, "caution")
        reasons.append("greylist: out-of-band creator")
    if gc.get("greylist_score"):
        try:
            from creator_greylist import apply_decay, recommended_strategy, strategy_overrides
            score = float(apply_decay(gc.get("greylist_score"), gc.get("greylist_score_updated_at")))
            strat = recommended_strategy(score)
            if strat != "standard" and level == "neutral" and not gc.get("greylist_blacklisted"):
                level = "favor"
                greylist_size_mult = float(strategy_overrides(strat).get("size_mult") or 1.0)
                reasons.append(f"greylist: {strat} creator (score {score:.0f})")
        except Exception:
            pass
    # our own book on this creator
    rows = await db.trades.find({"creator": creator, "status": {"$in": ["closed", "exit_failed_terminal"]}},
                                {"_id": 0, "pnl_usd": 1, "dead_tape_sold": 1}).sort("exit_time", -1).limit(50).to_list(50)
    # a DEAD TAPE? flag the operator chose to SELL (not dismiss) is a loss on this creator whatever the P&L
    wins = sum(1 for r in rows if float(r.get("pnl_usd") or 0) > 0 and not r.get("dead_tape_sold"))
    losses = sum(1 for r in rows if float(r.get("pnl_usd") or 0) < 0 or r.get("dead_tape_sold"))
    n = wins + losses
    min_n = int(_cfg(cfg, "creator_min_trades_for_rate", 6))
    min_rate = float(_cfg(cfg, "creator_min_win_rate_pct", 40.0))
    win_rate = (wins / n * 100.0) if n > 0 else None
    if n >= min_n and win_rate < min_rate:
        level, reasons = "block", reasons + [f"our book: {wins}/{n} wins = {win_rate:.0f}% < {min_rate:g}% on this creator"]
    elif n >= min_n and level == "neutral":
        level = "favor"
        reasons.append(f"our book: {wins}/{n} wins = {win_rate:.0f}% ≥ {min_rate:g}% on this creator")
    elif 0 < n < min_n:
        reasons.append(f"our book: {wins}/{n} wins so far ({min_n - n} more to judge)")
    return {"level": level, "reasons": reasons, "pattern": pattern, "score": round(score, 1) if score is not None else None,
            "own_wins": wins, "own_losses": losses, "own_win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
            "greylist_size_mult": greylist_size_mult}


def clear_cache(creator: str | None = None) -> None:
    if creator:
        _CACHE.pop(creator, None)
    else:
        _CACHE.clear()
