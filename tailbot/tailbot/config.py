"""Runtime config. Secrets stay in the environment: HELIUS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields

from .sweep import SWEEP_WALLET

# key -> (caster, lo, hi). Values are the human units used by /set (percents, seconds, SOL).
SETTABLE: dict[str, tuple] = {
    "pot_pct_per_buy": (float, 0.1, 100.0),
    "hard_cap_pct": (float, 0.1, 100.0),
    "max_deployed_pct": (float, 1.0, 100.0),
    "per_leader_pct": (float, 0.1, 100.0),
    "per_leader_max_positions": (int, 1, 10),
    "min_leader_buy_sol": (float, 0.0, 100.0),
    "min_mcap_usd": (float, 0.0, 1e9),
    "min_pumpswap_liq_usd": (float, 0.0, 1e9),
    "max_round_trip_impact_pct": (float, 0.0, 100.0),
    "flip_window_s": (float, 0.0, 3600.0),
    "sol_reserve": (float, 0.0, 10.0),
    "min_our_buy_sol": (float, 0.0, 10.0),
    "chase_premium_pct": (float, 0.0, 100.0),
    "chase_recheck_s": (float, 0.5, 120.0),
    "chase_window_s": (float, 0.5, 300.0),
    "stop_loss_pct": (float, 1.0, 95.0),
    "trail_pct": (float, 1.0, 90.0),
    "trail_arm_pct": (float, 1.0, 1000.0),
    "momentum_arm_pct": (float, 0.0, 1000.0),
    "momentum_drop_ratio": (float, 0.05, 1.0),
    "momentum_short_s": (float, 5.0, 600.0),
    "momentum_baseline_s": (float, 10.0, 3600.0),
    "momentum_min_baseline_sol": (float, 0.0, 1000.0),
    "daily_loss_pct": (float, 1.0, 100.0),
    "leader_loss_streak": (int, 1, 50),
    "leader_window_trades": (int, 2, 200),
    "leader_window_pnl_pct": (float, -99.0, 0.0),
    "sweep_profit_pct": (float, 0.0, 100.0),
    "sweep_min_sol": (float, 0.0, 10.0),
    "paper_extra_slip_bps": (int, 0, 5000),
    "paper_tx_fee_sol": (float, 0.0, 0.1),
    "hold_prompt_s": (float, 60.0, 864000.0),
    "feed_down_s": (float, 5.0, 3600.0),
}


@dataclass
class Config:
    execution_mode: str = "paper"
    live_enabled: bool = False
    leaders: list[str] = field(default_factory=list)
    max_leaders: int = 3
    paper_pot_sol: float = 1.0
    pot_pct_per_buy: float = 10.0
    hard_cap_pct: float = 20.0
    max_deployed_pct: float = 60.0
    per_leader_pct: float = 20.0
    per_leader_max_positions: int = 2
    min_leader_buy_sol: float = 0.1
    min_mcap_usd: float = 0.0
    min_pumpswap_liq_usd: float = 0.0
    max_round_trip_impact_pct: float = 25.0
    flip_window_s: float = 30.0
    sol_reserve: float = 0.05
    min_our_buy_sol: float = 0.05
    chase_premium_pct: float = 15.0
    chase_recheck_s: float = 5.0
    chase_window_s: float = 15.0
    backfill_limit: int = 25
    stop_loss_pct: float = 35.0
    trail_pct: float = 25.0
    trail_arm_pct: float = 50.0
    momentum_arm_pct: float = 20.0
    momentum_drop_ratio: float = 0.40
    momentum_short_s: float = 30.0
    momentum_baseline_s: float = 180.0
    momentum_min_baseline_sol: float = 1.0
    daily_loss_pct: float = 15.0
    leader_loss_streak: int = 5
    leader_window_trades: int = 20
    leader_window_pnl_pct: float = -25.0
    sweep_profit_pct: float = 50.0
    sweep_min_sol: float = 0.01
    sweep_wallet: str = SWEEP_WALLET
    paper_extra_slip_bps: int = 100
    paper_tx_fee_sol: float = 0.00008
    pumpfun_fee_bps: int = 100
    pumpswap_fee_bps: int = 30
    hold_prompt_s: float = 86_400.0
    feed_down_s: float = 60.0
    helius_soft_ceiling: float = 8_000.0
    helius_hard_ceiling: float = 25_000.0
    sellall_pin: str = "change-me"
    db_path: str = "data/tailbot.sqlite"
    mark_every_s: float = 15.0
    native_sol_dust_lamports: int = 20_000_000


def load_config(path: str | None) -> Config:
    raw: dict = {}
    if path and os.path.exists(path):
        with open(path) as fh:
            raw = json.load(fh)
    cfg = Config()
    known = {f.name for f in fields(cfg)}
    for key, value in raw.items():
        if key in known:
            setattr(cfg, key, value)
    cfg.leaders = [str(item).strip() for item in (cfg.leaders or []) if str(item).strip()]
    if len(cfg.leaders) > int(cfg.max_leaders):
        raise SystemExit(f"config has {len(cfg.leaders)} leaders; the maximum is {cfg.max_leaders}")
    cfg.execution_mode = str(cfg.execution_mode or "paper").lower()
    cfg.live_enabled = bool(cfg.live_enabled)
    return cfg


def apply_saved_overrides(cfg: Config, kv_get) -> None:
    for key, (cast, _lo, _hi) in SETTABLE.items():
        raw = kv_get(f"risk.{key}")
        if raw is None or raw == "":
            continue
        setattr(cfg, key, cast(raw))


def set_param(cfg: Config, key: str, value: str, kv_set) -> str:
    if key not in SETTABLE:
        known = ", ".join(sorted(SETTABLE))
        return f"unknown key {key}. settable: {known}"
    cast, lo, hi = SETTABLE[key]
    try:
        parsed = cast(value)
    except (TypeError, ValueError):
        return f"{key} is not a {cast.__name__}"
    if parsed < lo or parsed > hi:
        return f"{key} must be between {lo:g} and {hi:g}"
    if key == "momentum_baseline_s" and parsed <= float(cfg.momentum_short_s):
        return "momentum_baseline_s must be longer than momentum_short_s"
    if key == "momentum_short_s" and parsed >= float(cfg.momentum_baseline_s):
        return "momentum_short_s must be shorter than momentum_baseline_s"
    if key == "hard_cap_pct" and parsed + 1e-9 < float(cfg.pot_pct_per_buy):
        # Still allowed: sizing takes the minimum. Tell the operator.
        pass
    setattr(cfg, key, parsed)
    kv_set(f"risk.{key}", str(parsed))
    return f"{key} = {parsed:g}" if isinstance(parsed, float) else f"{key} = {parsed}"


def load_env_file(path: str) -> None:
    """Fill missing env vars from a local file. Does not override the process environment."""
    if not path or not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
