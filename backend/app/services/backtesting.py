import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional
from app.services.indicators import calculate_all_indicators
from app.services.signal_engine import evaluate_row_signal
from app.models.schemas import SignalDirection
from app.core.market_session import IST
from app.services.strategy_config import get_strategy, StrategyVersion
import uuid


class BacktestEngine:
    def __init__(
        self,
        initial_capital: float = 1_000_000.0,
        brokerage_pct: float = 0.03,
        stt_pct: float = 0.025,
        slippage_pct: float = 0.05,
        max_positions: int = 1,
        risk_per_trade_pct: float = 0.02,
        strategy_version: str = 'v1',
        periods_per_year: int = 252,
    ):
        self.initial_capital = initial_capital
        self.brokerage_pct = brokerage_pct
        self.stt_pct = stt_pct
        self.slippage_pct = slippage_pct
        self.max_positions = max_positions
        self.risk_per_trade_pct = risk_per_trade_pct
        self.strategy_version = strategy_version
        # Annualization periods for Sharpe/Sortino. The API path feeds daily
        # yfinance bars, so the default is 252 trading days per year. Override
        # for other bar frequencies.
        self.periods_per_year = periods_per_year

    def _generate_trade_id(self) -> str:
        return uuid.uuid4().hex[:16]

    def _empty_performance(self) -> dict:
        return {
            'total_trades': 0,
            'wins': 0,
            'losses': 0,
            'win_rate': 0.0,
            'total_pnl': 0.0,
            'final_capital': self.initial_capital,
            'initial_capital': self.initial_capital,
            'profit_factor': 0.0,
            'max_drawdown': 0.0,
            'sharpe_ratio': 0.0,
            'sortino_ratio': 0.0,
            'expectancy': 0.0,
            'avg_win': 0.0,
            'avg_loss': 0.0,
            'avg_r_multiple': 0.0,
            'total_bars': 0,
            'bars_traded': 0,
            'total_slippage_paid': 0.0,
            'total_brokerage_paid': 0.0,
            'total_stt_paid': 0.0,
        }

    def run(
        self,
        df: pd.DataFrame,
        symbol: str,
        strategy: str = "multi_factor",
        strategy_version: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        max_bars: Optional[int] = None,
    ) -> dict:
        if len(df) < 60:
            return {
                'id': self._generate_trade_id(),
                'symbol': symbol,
                'strategy': strategy,
                'strategy_version': strategy_version or self.strategy_version,
                'error': 'Insufficient data for backtest',
                'trades': [],
                'total_trades': 0,
                'equity_curve': [],
                'performance': self._empty_performance(),
            }

        sv = get_strategy(strategy_version or self.strategy_version)

        if max_bars is not None:
            df = df.iloc[: min(max_bars, len(df))]

        if start_date is not None and 'timestamp' in df.columns:
            ts = pd.to_datetime(df['timestamp'])
            mask = ts >= start_date
            df = df[mask].copy()
        if end_date is not None and 'timestamp' in df.columns:
            ts = pd.to_datetime(df['timestamp'])
            mask = ts <= end_date
            df = df[mask].copy()

        df = df.copy()
        df = calculate_all_indicators(df)
        df_reset = df.reset_index(drop=True)

        cash = self.initial_capital
        equity_curve = []
        trades = []
        open_position: Optional[dict] = None
        max_equity = cash
        max_drawdown = 0.0
        wins = 0
        losses = 0
        total_profit = 0.0
        total_loss = 0.0
        total_slippage_paid = 0.0
        total_brokerage_paid = 0.0
        total_stt_paid = 0.0
        total_r_multiple = 0.0

        signal_start_idx = 55

        for i in range(signal_start_idx, len(df_reset)):
            current_row = df_reset.iloc[i]
            current_ts = current_row.get('timestamp', i)
            current_close = current_row['close']
            current_open = current_row['open']
            current_high = current_row['high']
            current_low = current_row['low']

            if open_position is not None:
                pos = open_position
                direction = pos['direction']
                entry_price = pos['entry']
                stop_loss = pos['stop_loss']
                target = pos['target']
                quantity = pos['quantity']
                entry_time = pos['entry_time']
                signal_info = pos['signal_info']
                setup_info = pos['setup_info']
                trade_id = pos['trade_id']

                exit_triggered = False
                exit_price = None
                exit_type = None
                exit_bar = i

                is_long = direction in (
                    SignalDirection.STRONG_LONG,
                    SignalDirection.LONG,
                    SignalDirection.WEAK_LONG,
                )

                if is_long:
                    if current_low <= stop_loss:
                        exit_price = stop_loss
                        exit_type = 'stop_loss'
                        exit_triggered = True
                    elif current_high >= target:
                        exit_price = target
                        exit_type = 'target_1'
                        exit_triggered = True

                    if not exit_triggered:
                        if current_high >= target and current_low <= stop_loss:
                            exit_price = stop_loss
                            exit_type = 'stop_loss'
                            exit_triggered = True
                else:
                    if current_high >= stop_loss:
                        exit_price = stop_loss
                        exit_type = 'stop_loss'
                        exit_triggered = True
                    elif current_low <= target:
                        exit_price = target
                        exit_type = 'target_1'
                        exit_triggered = True

                    if not exit_triggered:
                        if current_low <= target and current_high >= stop_loss:
                            exit_price = stop_loss
                            exit_type = 'stop_loss'
                            exit_triggered = True

                if exit_triggered:
                    if is_long:
                        slip = exit_price * self.slippage_pct / 100
                        actual_exit = exit_price - slip
                    else:
                        slip = exit_price * self.slippage_pct / 100
                        actual_exit = exit_price + slip

                    entry_slip = entry_price * self.slippage_pct / 100
                    if is_long:
                        actual_entry = entry_price + entry_slip
                    else:
                        actual_entry = entry_price - entry_slip

                    gross_pnl = (
                        (actual_exit - actual_entry) * quantity
                        if is_long
                        else (actual_entry - actual_exit) * quantity
                    )
                    cost = (
                        (actual_entry * quantity * self.brokerage_pct / 100)
                        + (actual_exit * quantity * self.brokerage_pct / 100)
                    )
                    stt_cost = (
                        (actual_entry * quantity * self.stt_pct / 100)
                        + (actual_exit * quantity * self.stt_pct / 100)
                    )
                    net_pnl = gross_pnl - cost - stt_cost

                    total_slippage_paid += slip * quantity
                    total_brokerage_paid += cost
                    total_stt_paid += stt_cost

                    if net_pnl > 0:
                        wins += 1
                        total_profit += net_pnl
                    else:
                        losses += 1
                        total_loss += abs(net_pnl)

                    risk = abs(entry_price - stop_loss)
                    actual_r = (
                        net_pnl / (risk * quantity) if risk > 0 and quantity > 0 else 0.0
                    )
                    total_r_multiple += actual_r

                    exit_ts = (
                        str(df_reset.iloc[exit_bar].get('timestamp', exit_bar))
                        if exit_bar < len(df_reset)
                        else str(current_ts)
                    )

                    trades.append({
                        'trade_id': trade_id,
                        'strategy_version': strategy_version or self.strategy_version,
                        'symbol': symbol,
                        'timestamp': str(current_ts),
                        'signal_bar': entry_time,
                        'entry_time': entry_time,
                        'exit_time': exit_ts,
                        'direction': pos['signal_info'].get('direction'),
                        'signal_score': signal_info.get('score', 0),
                        'confidence': signal_info.get('confidence', 0),
                        'entry': round(actual_entry, 2),
                        'exit': round(actual_exit, 2),
                        'stop_loss': stop_loss,
                        'target': target,
                        'quantity': quantity,
                        'gross_pnl': round(gross_pnl, 2),
                        'net_pnl': round(net_pnl, 2),
                        'cost': round(cost + stt_cost, 2),
                        'exit_type': exit_type,
                        'holding_period_bars': exit_bar - pos['entry_bar'],
                        'r_multiple': round(actual_r, 2),
                        'atr_14': setup_info.get('atr_14', 0),
                        'adx_14': setup_info.get('adx_14', 0),
                        'rsi_14': setup_info.get('rsi_14', 0),
                        'relative_volume': setup_info.get('relative_volume', 0),
                        'vwap': setup_info.get('vwap', 0),
                        'risk_reward_ratio': setup_info.get('risk_reward_ratio', 0),
                        'strategy_config': setup_info.get('strategy_config', {}),
                        'exit_bar': exit_bar,
                    })

                    # Return the committed notional and add the realized P&L.
                    # net_pnl already includes the round-trip brokerage + STT, so
                    # this yields cash_after == cash_before_entry + net_pnl for
                    # BOTH long and short positions (a short's collateral is the
                    # entry notional, which must be returned on buy-back).
                    cash += actual_entry * quantity + net_pnl
                    open_position = None

            if open_position is None and i + 1 < len(df_reset):
                next_idx = i + 1
                current_row_eval = df_reset.iloc[i]

                # Row-level signal decision — single source of truth shared with
                # live signal generation (scanner / stock detail / dashboard).
                # This centralizes scoring, direction, strong-quality checks,
                # STRONG->normal downgrade and R:R enforcement in one place so the
                # backtest can never diverge from the live signal engine.
                decision = evaluate_row_signal(
                    current_row_eval,
                    market_context=None,
                    strategy_version=sv,
                )
                if decision is None:
                    continue

                direction_enum = decision["direction"]
                if direction_enum == SignalDirection.NO_TRADE:
                    continue

                setup = decision["setup"]
                confidence = decision["confidence"]
                score_total = decision["signal_score"].total
                direction_str = direction_enum.value

                entry_row = df_reset.iloc[next_idx]
                entry_price = entry_row['open']
                signal_row = current_row

                is_long_signal = direction_enum.is_long

                risk = abs(entry_price - setup.stop_loss)
                if risk <= 0:
                    continue

                max_risk = cash * (sv.risk_per_trade_pct / 100.0)
                qty = int(max_risk / risk)
                max_qty_by_capital = int((cash * 0.1) / entry_price)
                qty = min(qty, max_qty_by_capital)

                if qty <= 0:
                    continue

                target = setup.target_1

                entry_slip = entry_price * self.slippage_pct / 100
                if is_long_signal:
                    actual_entry = entry_price + entry_slip
                else:
                    actual_entry = entry_price - entry_slip

                cash -= actual_entry * qty

                signal_info = {
                    'score': score_total,
                    'confidence': confidence,
                    'direction': direction_str,
                }
                setup_info = {
                    'atr_14': signal_row.get('atr_14', 0),
                    'adx_14': signal_row.get('adx_14', 0),
                    'rsi_14': signal_row.get('rsi_14', 0),
                    'relative_volume': signal_row.get('relative_volume', 0),
                    'vwap': signal_row.get('vwap', 0),
                    'risk_reward_ratio': setup.risk_reward_ratio,
                    'strategy_version': strategy_version or self.strategy_version,
                    'strategy_config': sv.model_dump(),
                }

                open_position = {
                        'direction': direction_enum,
                        'entry': entry_price,
                        'stop_loss': setup.stop_loss,
                        'target': target,
                        'target_2': setup.target_2,
                        'quantity': qty,
                        'entry_time': str(entry_row.get('timestamp', next_idx)),
                        'entry_bar': next_idx,
                        'signal_info': signal_info,
                        'setup_info': setup_info,
                        'trade_id': self._generate_trade_id(),
                        'actual_entry': actual_entry,
                    }

            if open_position:
                pos = open_position
                position_value = current_close * pos['quantity']
                position_cost_basis = pos['actual_entry'] * pos['quantity']
                unrealized = position_value - position_cost_basis
            else:
                position_value = 0.0
                unrealized = 0.0

            # Equity = available cash + marked-to-market position value. While a
            # position is open the committed notional is held in position_value,
            # so equity does not collapse merely because cash was reduced.
            current_equity = cash + position_value
            equity_curve.append({
                'timestamp': str(current_ts),
                'equity': round(current_equity, 2),
                'cash': round(cash, 2),
                'position_value': round(position_value, 2),
                'unrealized_pnl': round(unrealized, 2),
                'bar_index': i,
                'has_position': open_position is not None,
            })

            if current_equity > max_equity:
                max_equity = current_equity
            dd = (
                (max_equity - current_equity) / max_equity * 100
                if max_equity > 0
                else 0
            )
            if dd > max_drawdown:
                max_drawdown = dd

        total_trades = len(trades)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        avg_win = total_profit / wins if wins > 0 else 0
        avg_loss = total_loss / losses if losses > 0 else 0
        profit_factor = (
            total_profit / total_loss if total_loss > 0 else float('inf')
        )
        expectancy = (
            (total_profit - total_loss) / total_trades if total_trades > 0 else 0
        )
        avg_r_multiple = total_r_multiple / total_trades if total_trades > 0 else 0

        total_pnl = sum(t['net_pnl'] for t in trades)
        final_capital = self.initial_capital + total_pnl

        returns = []
        for j in range(1, len(equity_curve)):
            prev = equity_curve[j - 1]['equity']
            curr = equity_curve[j]['equity']
            if prev > 0:
                returns.append((curr - prev) / prev)
        returns = np.array(returns)
        sharpe = 0.0
        sortino = 0.0
        if len(returns) > 1 and returns.std() > 0:
            ann = np.sqrt(self.periods_per_year)
            sharpe = (returns.mean() / returns.std()) * ann
            downside = returns[returns < 0]
            if len(downside) > 0 and downside.std() > 0:
                sortino = (returns.mean() / downside.std()) * ann

        performance = {
            'total_trades': total_trades,
            'wins': wins,
            'losses': losses,
            'win_rate': round(win_rate, 1),
            'total_pnl': round(total_pnl, 2),
            'final_capital': round(final_capital, 2),
            'initial_capital': self.initial_capital,
            'profit_factor': round(profit_factor, 2)
            if profit_factor != float('inf')
            else 999.0,
            'max_drawdown': round(max_drawdown, 2),
            'sharpe_ratio': round(float(sharpe), 2),
            'sortino_ratio': round(float(sortino), 2),
            'expectancy': round(expectancy, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'avg_r_multiple': round(avg_r_multiple, 3),
            'total_bars': len(df_reset),
            'total_slippage_paid': round(total_slippage_paid, 2),
            'total_brokerage_paid': round(total_brokerage_paid, 2),
            'total_stt_paid': round(total_stt_paid, 2),
        }

        return {
            'id': self._generate_trade_id(),
            'symbol': symbol,
            'strategy': strategy,
            'strategy_version': strategy_version or self.strategy_version,
            'total_trades': total_trades,
            'wins': wins,
            'losses': losses,
            'win_rate': round(win_rate, 1),
            'total_pnl': round(total_pnl, 2),
            'final_capital': round(final_capital, 2),
            'initial_capital': self.initial_capital,
            'profit_factor': round(profit_factor, 2)
            if profit_factor != float('inf')
            else 999.0,
            'max_drawdown': round(max_drawdown, 2),
            'sharpe_ratio': round(float(sharpe), 2),
            'sortino_ratio': round(float(sortino), 2),
            'expectancy': round(expectancy, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'avg_r_multiple': round(avg_r_multiple, 3),
            'performance': performance,
            'trades': trades,
            'equity_curve': equity_curve,
        }
