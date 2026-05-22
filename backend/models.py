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
    hold_max_seconds: int = 60       # user-requested: keep at 60s
    take_profit_pct: float = 35.0    # winners observed averaging ~+37%
    stop_loss_pct: float = 20.0      # tight cap, enforced by fast-exit
    trailing_stop_pct: float = 10.0  # lock in profits once peak appears
    exit_slippage_bps: int = 500     # separate exit slippage ensures exits fill on dumps
    # Entry filters
    min_curve_liquidity_sol: float = 12.0  # skip thin/dead launches
    min_buyers_for_entry: int = 3          # require real interest
    max_concurrent_positions: int = 8      # diversification cap
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
    # Discovery: only seed tokens whose last trade is fresher than this (minutes).
    # Set 0 to disable the freshness gate.
    scanner_discovery_max_idle_minutes: int = 5
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
    daily_pnl_usd: float
    daily_loss_usd: float  # positive number representing loss magnitude
    daily_kill_switch_usd: float
    total_trades_today: int
    active_trade_count: int
