from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from enum import Enum


class DataSource(str, Enum):
    LIVE = "live"
    DELAYED = "delayed"
    SIMULATED = "simulated"
    MOCK = "mock"


class SignalDirection(str, Enum):
    STRONG_LONG = "STRONG_LONG"
    LONG = "LONG"
    WEAK_LONG = "WEAK_LONG"
    NO_TRADE = "NO_TRADE"
    WEAK_SHORT = "WEAK_SHORT"
    SHORT = "SHORT"
    STRONG_SHORT = "STRONG_SHORT"

    @property
    def display(self) -> str:
        return self.value.replace("_", " ")

    @property
    def is_long(self) -> bool:
        return self in (SignalDirection.STRONG_LONG, SignalDirection.LONG, SignalDirection.WEAK_LONG)

    @property
    def is_short(self) -> bool:
        return self in (SignalDirection.STRONG_SHORT, SignalDirection.SHORT, SignalDirection.WEAK_SHORT)

    @property
    def is_strong(self) -> bool:
        return self in (SignalDirection.STRONG_LONG, SignalDirection.STRONG_SHORT)

    @property
    def is_weak(self) -> bool:
        return self in (SignalDirection.WEAK_LONG, SignalDirection.WEAK_SHORT)

    def base_direction(self) -> "SignalDirection":
        if self.is_strong:
            return SignalDirection.LONG if self == SignalDirection.STRONG_LONG else SignalDirection.SHORT
        if self.is_weak:
            return SignalDirection.LONG if self == SignalDirection.WEAK_LONG else SignalDirection.SHORT
        return self


class Quote(BaseModel):
    symbol: str
    price: float
    change: float = 0.0
    change_pct: float = 0.0
    volume: int = 0
    high: float = 0.0
    low: float = 0.0
    open: float = 0.0
    timestamp: datetime
    data_source: DataSource = DataSource.MOCK


class OHLCV(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class SignalScore(BaseModel):
    trend_score: float = 0.0
    momentum_score: float = 0.0
    volume_score: float = 0.0
    vwap_score: float = 0.0
    price_action_score: float = 0.0
    market_context_score: float = 0.0
    risk_quality_score: float = 0.0
    total: float = 0.0


class SignalExplanation(BaseModel):
    reasons: list[str] = []
    risks: list[str] = []


class TradeSetup(BaseModel):
    entry: float = 0.0
    stop_loss: float = 0.0
    target_1: float = 0.0
    target_2: float = 0.0
    trailing_stop: Optional[float] = None
    risk_per_share: float = 0.0
    reward_per_share: float = 0.0
    risk_reward_ratio: float = 0.0


class SignalResponse(BaseModel):
    id: str = ""
    symbol: str
    timestamp: datetime
    direction: SignalDirection
    confidence: float
    signal_score: SignalScore
    setup: TradeSetup
    explanation: SignalExplanation
    strategy: str
    data_source: DataSource
    indicator_values: dict = {}
    market_data_timestamp: Optional[datetime] = None
    signal_generated_at: Optional[datetime] = None
    entry_updated_at: Optional[datetime] = None
    stop_loss_updated_at: Optional[datetime] = None
    target_updated_at: Optional[datetime] = None
    last_updated_at: Optional[datetime] = None
