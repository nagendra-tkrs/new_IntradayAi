"""
Strategy versioning system for IntradayAI.
Each strategy version encapsulates signal thresholds, risk filters,
and backtest parameters for a particular engine configuration.
"""
from typing import Optional
from app.models.schemas import SignalDirection


class StrategyVersion:
    """Configuration for a specific strategy version."""

    def __init__(
        self,
        version: str,
        strong_long_threshold: float = 80.0,
        long_threshold: float = 75.0,
        weak_long_threshold: float = 65.0,
        no_trade_threshold: float = 55.0,
        weak_short_threshold: float = 45.0,
        short_threshold: float = 30.0,
        strong_short_threshold: float = 0.0,
        min_rr_normal: float = 1.2,
        min_rr_strong: float = 2.0,
        strong_atr_mult: float = 3.0,
        normal_atr_mult: float = 2.0,
        strong_min_adx: float = 25.0,
        strong_min_rel_vol: float = 1.0,
        strong_max_rsi_long: float = 75.0,
        strong_max_rsi_short: float = 25.0,
        stop_loss_atr_mult: float = 1.5,
        risk_per_trade_pct: float = 2.0,
        min_score: float = 45.0,
        description: str = "",
        # Profit-capture / geometry overrides. All are OPTIONAL: None keeps the
        # version's existing semantics (T2 = T1 + 1 ATR via the +1.0 in
        # compute_trade_setup, SL = stop_loss_atr_mult for both strengths).
        normal_sl_atr_mult: Optional[float] = None,
        strong_sl_atr_mult: Optional[float] = None,
        normal_target_2_atr_mult: Optional[float] = None,
        strong_target_2_atr_mult: Optional[float] = None,
    ):
        self.version = version
        self.strong_long_threshold = strong_long_threshold
        self.long_threshold = long_threshold
        self.weak_long_threshold = weak_long_threshold
        self.no_trade_threshold = no_trade_threshold
        self.weak_short_threshold = weak_short_threshold
        self.short_threshold = short_threshold
        self.strong_short_threshold = strong_short_threshold
        self.min_rr_normal = min_rr_normal
        self.min_rr_strong = min_rr_strong
        self.strong_atr_mult = strong_atr_mult
        self.normal_atr_mult = normal_atr_mult
        self.strong_min_adx = strong_min_adx
        self.strong_min_rel_vol = strong_min_rel_vol
        self.strong_max_rsi_long = strong_max_rsi_long
        self.strong_max_rsi_short = strong_max_rsi_short
        self.stop_loss_atr_mult = stop_loss_atr_mult
        self.risk_per_trade_pct = risk_per_trade_pct
        self.min_score = min_score
        self.description = description
        self.normal_sl_atr_mult = normal_sl_atr_mult
        self.strong_sl_atr_mult = strong_sl_atr_mult
        self.normal_target_2_atr_mult = normal_target_2_atr_mult
        self.strong_target_2_atr_mult = strong_target_2_atr_mult

    def sl_atr_mult_for(self, is_strong: bool) -> Optional[float]:
        """Per-strength stop-loss ATR multiplier override, or None to keep the
        version's shared ``stop_loss_atr_mult`` semantics."""
        return (self.strong_sl_atr_mult if is_strong else self.normal_sl_atr_mult) or None

    def target_1_atr_mult_for(self, is_strong: bool) -> float:
        return self.strong_atr_mult if is_strong else self.normal_atr_mult

    def target_2_atr_mult_for(self, is_strong: bool) -> Optional[float]:
        """Per-strength Target-2 ATR multiplier override, or None to keep the
        legacy ``T2 = T1 + 1 ATR`` geometry."""
        return (self.strong_target_2_atr_mult if is_strong else self.normal_target_2_atr_mult) or None

    def determine_direction(self, total_score: float) -> SignalDirection:
        if total_score >= self.strong_long_threshold:
            return SignalDirection.STRONG_LONG
        elif total_score >= self.long_threshold:
            return SignalDirection.LONG
        elif total_score >= self.weak_long_threshold:
            return SignalDirection.WEAK_LONG
        elif total_score >= self.no_trade_threshold:
            return SignalDirection.NO_TRADE
        elif total_score >= self.weak_short_threshold:
            return SignalDirection.WEAK_SHORT
        elif total_score >= self.short_threshold:
            return SignalDirection.SHORT
        else:
            return SignalDirection.STRONG_SHORT

    def should_accept_strong(self, adx, rel_vol, rsi, price, vwap, direction):
        rejections = []
        if adx < self.strong_min_adx:
            rejections.append(f"ADX below {self.strong_min_adx}")
        if rel_vol < self.strong_min_rel_vol:
            rejections.append(f"RelVol below {self.strong_min_rel_vol}")
        if direction == SignalDirection.STRONG_LONG and price <= vwap:
            rejections.append("Price below VWAP for long")
        if direction == SignalDirection.STRONG_SHORT and price >= vwap:
            rejections.append("Price above VWAP for short")
        if direction == SignalDirection.STRONG_LONG and rsi > self.strong_max_rsi_long:
            rejections.append(f"RSI above {self.strong_max_rsi_long}")
        if direction == SignalDirection.STRONG_SHORT and rsi < self.strong_max_rsi_short:
            rejections.append(f"RSI below {self.strong_max_rsi_short}")
        return len(rejections) == 0, rejections

    def get_atr_mult(self, direction: SignalDirection) -> float:
        if direction.is_strong:
            return self.strong_atr_mult
        return self.normal_atr_mult

    def get_min_rr(self, direction: SignalDirection) -> float:
        if direction.is_strong:
            return self.min_rr_strong
        return self.min_rr_normal

    def model_dump(self) -> dict:
        return {
            "version": self.version,
            "thresholds": {
                "strong_long": self.strong_long_threshold,
                "long": self.long_threshold,
                "weak_long": self.weak_long_threshold,
                "no_trade": self.no_trade_threshold,
                "weak_short": self.weak_short_threshold,
                "short": self.short_threshold,
                "strong_short": self.strong_short_threshold,
            },
            "risk": {
                "min_rr_normal": self.min_rr_normal,
                "min_rr_strong": self.min_rr_strong,
                "strong_atr_mult": self.strong_atr_mult,
                "normal_atr_mult": self.normal_atr_mult,
                "stop_loss_atr_mult": self.stop_loss_atr_mult,
                "risk_per_trade_pct": self.risk_per_trade_pct,
                "min_score": self.min_score,
            },
            "filters": {
                "strong_min_adx": self.strong_min_adx,
                "strong_min_rel_vol": self.strong_min_rel_vol,
                "strong_max_rsi_long": self.strong_max_rsi_long,
                "strong_max_rsi_short": self.strong_max_rsi_short,
            },
            "description": self.description,
        }


STRATEGY_REGISTRY: dict[str, StrategyVersion] = {}


def register_strategy(sv: StrategyVersion):
    STRATEGY_REGISTRY[sv.version] = sv


def get_strategy(version: str = "v1") -> StrategyVersion:
    return STRATEGY_REGISTRY.get(version, STRATEGY_REGISTRY.get("v1"))


v1 = StrategyVersion(
    version="v1",
    strong_long_threshold=80.0,
    long_threshold=75.0,
    weak_long_threshold=65.0,
    no_trade_threshold=55.0,
    weak_short_threshold=45.0,
    short_threshold=30.0,
    strong_short_threshold=0.0,
    min_rr_normal=1.2,
    min_rr_strong=2.0,
    strong_atr_mult=3.0,
    normal_atr_mult=2.0,
    strong_min_adx=25.0,
    strong_min_rel_vol=1.0,
    strong_max_rsi_long=75.0,
    strong_max_rsi_short=25.0,
    stop_loss_atr_mult=1.5,
    risk_per_trade_pct=2.0,
    min_score=45.0,
    description="Baseline: STRONG threshold 80, normal RR 1.2, STRONG RR 2.0",
)
register_strategy(v1)


v2 = StrategyVersion(
    version="v2",
    strong_long_threshold=85.0,
    long_threshold=80.0,
    weak_long_threshold=70.0,
    no_trade_threshold=60.0,
    weak_short_threshold=50.0,
    short_threshold=25.0,
    strong_short_threshold=0.0,
    min_rr_normal=1.5,
    min_rr_strong=2.5,
    strong_atr_mult=3.0,
    normal_atr_mult=2.0,
    strong_min_adx=25.0,
    strong_min_rel_vol=1.5,
    strong_max_rsi_long=70.0,
    strong_max_rsi_short=30.0,
    stop_loss_atr_mult=1.5,
    risk_per_trade_pct=1.5,
    min_score=60.0,
    description="Conservative: higher thresholds, tighter risk",
)
register_strategy(v2)


v3 = StrategyVersion(
    version="v3",
    strong_long_threshold=75.0,
    long_threshold=70.0,
    weak_long_threshold=60.0,
    no_trade_threshold=50.0,
    weak_short_threshold=40.0,
    short_threshold=35.0,
    strong_short_threshold=0.0,
    min_rr_normal=1.0,
    min_rr_strong=1.8,
    strong_atr_mult=2.5,
    normal_atr_mult=2.0,
    strong_min_adx=20.0,
    strong_min_rel_vol=0.8,
    strong_max_rsi_long=80.0,
    strong_max_rsi_short=20.0,
    stop_loss_atr_mult=1.5,
    risk_per_trade_pct=2.5,
    min_score=35.0,
    description="Aggressive: lower thresholds, higher risk",
)
register_strategy(v3)


v2_candidate = StrategyVersion(
    version="v2_candidate",
    strong_long_threshold=80.0,
    long_threshold=75.0,
    weak_long_threshold=65.0,
    no_trade_threshold=55.0,
    weak_short_threshold=45.0,
    short_threshold=30.0,
    strong_short_threshold=0.0,
    min_rr_normal=1.5,
    min_rr_strong=2.0,
    strong_atr_mult=3.0,
    normal_atr_mult=2.0,
    strong_min_adx=25.0,
    strong_min_rel_vol=1.0,
    strong_max_rsi_long=75.0,
    strong_max_rsi_short=25.0,
    stop_loss_atr_mult=1.5,
    risk_per_trade_pct=1.5,
    min_score=60.0,
    description="Candidate: min_score=60, RR=1.5, risk=1.5% (from WFO grid search)",
)
register_strategy(v2_candidate)


v3_candidate = StrategyVersion(
    version="v3_candidate",
    strong_long_threshold=85.0,
    long_threshold=80.0,
    weak_long_threshold=70.0,
    no_trade_threshold=60.0,
    weak_short_threshold=50.0,
    short_threshold=25.0,
    strong_short_threshold=0.0,
    min_rr_normal=2.0,
    min_rr_strong=2.5,
    strong_atr_mult=3.0,
    normal_atr_mult=2.0,
    strong_min_adx=25.0,
    strong_min_rel_vol=1.5,
    strong_max_rsi_long=70.0,
    strong_max_rsi_short=30.0,
    stop_loss_atr_mult=1.5,
    risk_per_trade_pct=1.5,
    min_score=75.0,
    description="Candidate: high score threshold 85, strict filters, low risk",
)
register_strategy(v3_candidate)


v4_candidate = StrategyVersion(
    version="v4_candidate",
    strong_long_threshold=80.0,
    long_threshold=75.0,
    weak_long_threshold=65.0,
    no_trade_threshold=55.0,
    weak_short_threshold=45.0,
    short_threshold=30.0,
    strong_short_threshold=0.0,
    min_rr_normal=1.2,
    min_rr_strong=2.0,
    strong_atr_mult=3.0,
    normal_atr_mult=2.0,
    strong_min_adx=25.0,
    strong_min_rel_vol=1.0,
    strong_max_rsi_long=75.0,
    strong_max_rsi_short=25.0,
    stop_loss_atr_mult=1.5,
    risk_per_trade_pct=2.0,
    min_score=45.0,
    description="Candidate: same as v1 (control for comparison)",
)
register_strategy(v4_candidate)
