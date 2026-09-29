from dataclasses import dataclass, field
from datetime import datetime, date
import json
from typing import Optional
from app.core.config import settings
from app.core.market_session import now_ist


@dataclass
class RiskConfig:
    account_capital: float = settings.INITIAL_CAPITAL
    max_risk_per_trade_pct: float = 2.0
    max_daily_loss_pct: float = 5.0
    max_trades_per_day: int = 10
    max_simultaneous_positions: int = 10
    min_risk_reward: float = 1.2
    cooldown_after_losses: int = 3
    # Quality-based sizing knobs. base_risk_per_trade_pct is the *applied* risk
    # scaled by the quality/strength multipliers and capped at
    # max_risk_per_trade_pct (the hard per-trade cap). Conservative defaults:
    # base = cap = 2.0 and every multiplier 1.0 → identical sizing to legacy
    # unless a caller explicitly supplies setup_quality / signal_strength.
    base_risk_per_trade_pct: float = 2.0
    strong_signal_multiplier: float = 1.0
    quality_risk_multipliers: dict = field(
        default_factory=lambda: {
            "REJECTED": 0.0,
            "WEAK": 0.0,
            "NORMAL": 1.0,
            "QUALIFIED": 1.0,
            "PREMIUM": 1.0,
        }
    )


@dataclass
class RiskState:
    daily_trades: int = 0
    daily_pnl: float = 0.0
    open_positions: int = 0
    consecutive_losses: int = 0
    last_trade_time: datetime | None = None


class RiskEngine:
    def __init__(self, config: RiskConfig | None = None):
        try:
            quality_mults = json.loads(settings.QUALITY_RISK_MULTIPLIERS)
        except (TypeError, ValueError):
            quality_mults = {}
        self.config = config or RiskConfig(
            account_capital=settings.INITIAL_CAPITAL,
            max_risk_per_trade_pct=settings.MAX_RISK_PER_TRADE_PCT,
            max_daily_loss_pct=settings.MAX_DAILY_LOSS_PCT,
            max_trades_per_day=settings.MAX_TRADES_PER_DAY,
            max_simultaneous_positions=settings.MAX_SIMULTANEOUS_POSITIONS,
            base_risk_per_trade_pct=settings.BASE_RISK_PER_TRADE_PCT,
            strong_signal_multiplier=settings.STRONG_SIGNAL_RISK_MULTIPLIER,
            quality_risk_multipliers=quality_mults or {
                "REJECTED": 0.0,
                "WEAK": 0.0,
                "NORMAL": 1.0,
                "QUALIFIED": 1.0,
                "PREMIUM": 1.0,
            },
        )
        self.state = RiskState()

    def reset_daily(self):
        self.state.daily_trades = 0
        self.state.daily_pnl = 0.0

    def can_trade(self) -> tuple[bool, str]:
        if self.state.daily_trades >= self.config.max_trades_per_day:
            return False, f"Maximum daily trades ({self.config.max_trades_per_day}) reached"
        if self.state.open_positions >= self.config.max_simultaneous_positions:
            return False, f"Maximum simultaneous positions ({self.config.max_simultaneous_positions}) reached"
        max_loss = self.config.account_capital * (self.config.max_daily_loss_pct / 100)
        if self.state.daily_pnl <= -max_loss:
            return False, f"Daily loss limit reached ({self.config.max_daily_loss_pct}%)"
        if self.state.consecutive_losses >= self.config.cooldown_after_losses:
            return False, f"Cooldown active: {self.state.consecutive_losses} consecutive losses"
        return True, "OK"

    def effective_risk_per_trade_pct(
        self, setup_quality: Optional[str] = None, signal_strength: Optional[str] = None
    ) -> float:
        """Effective per-trade risk % = base * quality-mult * strength-mult,
        never exceeding the hard max_risk_per_trade_pct cap.

        ``setup_quality`` is the signal-quality level (PREMIUM/QUALIFIED/NORMAL/
        WEAK/REJECTED). Unknown levels default to a 1.0 multiplier so nothing
        outside the known set ever shrinks sizing. REJECTED/WEAK map to 0.0, so
        the returned risk is 0 and the caller must reject the order."""
        q_mult = 1.0
        if setup_quality:
            q_mult = self.config.quality_risk_multipliers.get(str(setup_quality).upper(), 1.0)
        s_mult = 1.0
        if signal_strength:
            s_mult = (
                self.config.strong_signal_multiplier
                if str(signal_strength).upper() in ("STRONG", "STRONG_LONG", "STRONG_SHORT")
                else 1.0
            )
        eff = self.config.base_risk_per_trade_pct * q_mult * s_mult
        return min(max(0.0, eff), self.config.max_risk_per_trade_pct)

    def calculate_position_size(
        self,
        entry: float,
        stop_loss: float,
        setup_quality: Optional[str] = None,
        signal_strength: Optional[str] = None,
    ) -> int:
        risk_pct = self.effective_risk_per_trade_pct(setup_quality, signal_strength)
        if risk_pct <= 0:
            return 0
        max_risk_amount = self.config.account_capital * (risk_pct / 100)
        risk_per_share = abs(entry - stop_loss)
        if risk_per_share <= 0:
            return 0
        shares = int(max_risk_amount / risk_per_share)
        max_affordable = int(self.config.account_capital / entry)
        shares = min(shares, max_affordable)
        if shares > 0:
            total_cost = shares * entry
            if total_cost > self.config.account_capital * 0.95:
                shares = int((self.config.account_capital * 0.95) / entry)
        return max(0, shares)

    def validate_setup(self, entry: float, stop_loss: float, target_1: float, direction: str) -> tuple[bool, str]:
        risk = abs(entry - stop_loss)
        reward = abs(target_1 - entry)
        if risk <= 0:
            return False, "Invalid: zero risk"
        rr = reward / risk
        if rr < self.config.min_risk_reward:
            return False, f"Risk/reward {rr:.2f} below minimum {self.config.min_risk_reward}"
        return True, f"Risk/reward {rr:.2f} acceptable"

    def record_trade_result(self, pnl: float):
        self.state.daily_trades += 1
        self.state.daily_pnl += pnl
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0
        self.state.last_trade_time = now_ist()
