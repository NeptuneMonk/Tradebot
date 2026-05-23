"""
Pydantic models for API contracts and MongoDB persistence.
"""
from datetime import datetime, timezone
from typing import Literal, Optional
from pydantic import BaseModel, Field, ConfigDict
import uuid


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class BotConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = False
    live_trading: bool = False
    # Sizing
    min_trade_usd: float = 0.50
    max_trade_usd: float = 1.00
    slippage_bps: int = 500          # 5% entry slippage
    # Risk / exit behaviour
    daily_kill_switch_usd: float = 20.00
    priority_fee_microlamports: int = 500_000
    # Speed mode tuner: bundles priority_fee + slippage_bps + exit_slippage_bps
    # into named presets. UI exposes a 0-5 slider; "auto" adapts dynamically to
    # current network congestion via Helius getRecentPrioritizationFees.
    # Values: eco | normal | fast | aggressive | turbo | auto
    # When set to anything other than "manual", the resolved values overwrite
    # priority_fee_microlamports / slippage_bps / exit_slippage_bps at runtime.
    speed_mode: str = "manual"
    hold_max_seconds: int = 60       # user-requested: keep at 60s
    take_profit_pct: float = 35.0    # winners observed averaging ~+37%
    stop_loss_pct: float = 20.0      # tight cap, enforced by fast-exit
    trailing_stop_pct: float = 10.0  # lock in profits once peak appears
    # Partial take-profit: sell partial_tp_pct of the position at TP, ride the
    # remainder with a tightened trailing stop (driven by the 26x lift signal
    # showing TP exits dominate winners 66% vs 3% in losers).
    # Set partial_tp_pct=0 to disable (full exit at TP — old behaviour).
    partial_tp_pct: float = 50.0          # sell 50% at TP
    partial_tp_trail_tighten_pct: float = 5.0  # tighten trailing to 5% after partial
    exit_slippage_bps: int = 500     # separate exit slippage ensures exits fill on dumps
    # Entry filters (applied to scanner_momentum entries; reentry uses its own size logic)
    min_curve_liquidity_sol: float = 12.0  # skip thin/dead launches
    min_buyers_for_entry: int = 3          # require real interest
    max_concurrent_positions: int = 8      # diversification cap
    # Per-band entry filter overrides for the "new" momentum band (age < scanner_min_age_minutes).
    # The base fields above apply to the "seasoned" band.
    min_curve_liquidity_sol_new: float = 20.0
    min_buyers_for_entry_new: int = 8
    # Momentum scanner — 81% of recent profitable trades came from here
    scanner_enabled: bool = True
    scanner_window_hours: int = 4
    # Seasoning floor: only consider tokens older than this many minutes.
    # Filters out fresh-launch volatility the sniper already handles.
    scanner_min_age_minutes: int = 180
    scanner_interval_s: int = 15
    scanner_min_growth_pct: float = 20.0
    scanner_recent_inflow_window_s: int = 300
    scanner_min_recent_inflow_sol: float = 3.0
    scanner_holder_velocity_window_s: int = 60
    scanner_min_new_buyers: int = 5
    # Per-band scanner gate overrides for the "new" band (age < seasoning).
    # Defaults are tighter than the seasoned band to handle fresh-launch volatility.
    scanner_min_growth_pct_new: float = 50.0
    scanner_min_recent_inflow_sol_new: float = 5.0
    scanner_min_new_buyers_new: int = 10
    # Seasoned-band-only gates (use Pump.fun API data since Helius mempool
    # doesn't reach PumpSwap pools). Polled via the discovery refresh task.
    scanner_min_mc_usd_seasoned: float = 30000.0      # $30K market cap floor
    scanner_min_mc_velocity_5m_pct_seasoned: float = 5.0  # +5% MC change over 5min
    # Discovery: only seed tokens whose last trade is fresher than this (minutes).
    # Set 0 to disable the freshness gate.
    scanner_discovery_max_idle_minutes: int = 5
    # Entry velocity gate (pattern-mining insight: "stop-loss exits dominate
    # losers 39% vs winners 2%" → most losers are "dead cat" entries where the
    # token already peaked). Require >= scanner_entry_velocity_min_pct change
    # over scanner_entry_velocity_window_s seconds RIGHT BEFORE entry. Skipped
    # silently if we don't yet have enough samples to span the window.
    # Set scanner_entry_velocity_min_pct to a large negative (e.g., -999) to
    # effectively disable the gate.
    scanner_entry_velocity_window_s: int = 30
    scanner_entry_velocity_min_pct: float = 0.0
    # Socials gate (pattern-mining insight: tokens with active replies + a
    # working twitter/telegram link have a meaningfully higher floor MC than
    # zero-engagement launches). When ON, refuse entry unless the mint has at
    # least one social link AND reply_count >= gate_min_reply_count.
    gate_socials_required: bool = False
    gate_min_reply_count: int = 50
    # Re-entry on winners
    reentry_enabled: bool = True
    reentry_max_attempts: int = 2
    reentry_pullback_pct: float = 25.0
    reentry_window_seconds: int = 300
    reentry_size_multiplier: float = 0.5


class ClassifierRules(BaseModel):
    model_config = ConfigDict(extra="ignore")
    fast_curve_fill_pct: float = 30.0
    fast_curve_window_s: int = 10
    many_buyers_count: int = 15
    many_buyers_window_s: int = 5
    low_inflow_sol: float = 0.5
    low_inflow_window_s: int = 8
    creator_rug_threshold: int = 1
    # Social trending threshold: abort entry if name's social_score < this value
    social_score_min: int = 0  # 0 = disabled


class Launch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    mint: str
    creator: str
    bonding_curve: str
    detected_at: datetime = Field(default_factory=now_utc)
    name: Optional[str] = None
    symbol: Optional[str] = None
    classifier_action: Optional[str] = None
    classifier_risk: Optional[int] = None
    classifier_reasons: list[str] = []
    signature: Optional[str] = None  # tx that created it
    # Live mempool metrics (updated for ~30s after detection)
    unique_buyers: int = 0
    sol_inflow: float = 0.0
    buy_count: int = 0
    curve_fill_pct: float = 0.0
    # Social trending score (0..100)
    social_score: int = 0
    social_sources: dict = {}
    entered: bool = False  # did the bot enter this trade?


class Trade(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    mint: str
    name: Optional[str] = None
    symbol: Optional[str] = None
    status: Literal["active", "closed", "failed"] = "active"
    mode: Literal["live", "paper"] = "paper"
    # Entry
    entry_time: datetime = Field(default_factory=now_utc)
    entry_sol: float = 0.0
    entry_usd: float = 0.0
    entry_tokens: float = 0.0
    entry_price_sol: float = 0.0  # SOL per token
    entry_sig: Optional[str] = None
    # Exit
    exit_time: Optional[datetime] = None
    exit_sol: float = 0.0
    exit_usd: float = 0.0
    exit_price_sol: float = 0.0
    exit_sig: Optional[str] = None
    exit_reason: Optional[str] = None
    # P/L
    pnl_sol: float = 0.0
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    # Trading-cost breakdown (estimated at tx-submit time using
    # priority_fee_microlamports × compute_unit_limit / 1e6 + base 5000 lamports
    # signature fee. Slippage cost computed from quoted-vs-actual fills).
    entry_fee_sol: float = 0.0
    exit_fee_sol: float = 0.0
    partial_fee_sol: float = 0.0
    speed_mode_at_entry: Optional[str] = None
    # Protocol routing fields — persisted so monitors can resume after a
    # backend restart. Without these, a re-spawned _monitor_position can't
    # route price polls / sell builds correctly.
    protocol: str = "pumpfun"  # "pumpfun" or "pumpswap"
    pumpswap_pool: Optional[str] = None
    # Classifier snapshot
    risk_score: int = 50
    classifier_action: Optional[str] = None


class WalletInfo(BaseModel):
    public_key: str
    sol_balance: float
    usd_balance: float
    sol_price_usd: float


class BotStatus(BaseModel):
    enabled: bool
    live_trading: bool
    kill_switch_tripped: bool
    listener_connected: bool
    daily_pnl_usd: float          # legacy combined (live + paper) — kept for compat
    daily_pnl_live_usd: float = 0.0   # real-money PnL (drives kill switch)
    daily_pnl_paper_usd: float = 0.0  # paper-mode simulated PnL
    daily_loss_usd: float  # positive number representing LIVE loss magnitude (kill-switch ref)
    daily_kill_switch_usd: float
    total_trades_today: int
    active_trade_count: int
    stopping_gracefully: bool = False
