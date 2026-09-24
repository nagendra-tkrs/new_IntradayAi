from dataclasses import dataclass, field
from datetime import datetime, date
from app.core.config import settings
from app.core.market_session import now_ist


@dataclass
class RiskConfig:
    account_capital: float = 1_000_000.0
    max_risk_per_trade_pct: float = 2.0
    max_daily_loss_pct: float = 5.0
    max_trades_per_day: int = 10
    max_simultaneous_positions: int = 10
    min_risk_reward: float = 1.2
    cooldown_after_losses: int = 3


@dataclass
class RiskState:
    daily_trades: int = 0
    daily_pnl: float = 0.0
    open_positions: int = 0
    consecutive_losses: int = 0
    last_trade_time: datetime | None = None


class RiskEngine:
    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig(
            account_capital=settings.INITIAL_CAPITAL,
            max_risk_per_trade_pct=settings.MAX_RISK_PER_TRADE_PCT,
            max_daily_loss_pct=settings.MAX_DAILY_LOSS_PCT,
            max_trades_per_day=settings.MAX_TRADES_PER_DAY,
            max_simultaneous_positions=settings.MAX_SIMULTANEOUS_POSITIONS,
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

    def calculate_position_size(self, entry: float, stop_loss: float) -> int:
        max_risk_amount = self.config.account_capital * (self.config.max_risk_per_trade_pct / 100)
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
