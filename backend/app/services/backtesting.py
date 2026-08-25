import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional
from app.services.indicators import calculate_all_indicators
from app.services.signal_engine import evaluate_signal, compute_trade_setup
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
    ):
        self.initial_capital = initial_capital
        self.brokerage_pct = brokerage_pct
        self.stt_pct = stt_pct
        self.slippage_pct = slippage_pct
        self.max_positions = max_positions
        self.risk_per_trade_pct = risk_per_trade_pct
        self.strategy_version = strategy_version

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

        capital = self.initial_capital
        equity_curve = []
        trades = []
        open_position: Optional[dict] = None
        max_equity = capital
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
                        'direction': str(direction),
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

                    capital += net_pnl
                    open_position = None

            if open_position is None and i + 1 < len(df_reset):
                next_idx = i + 1
                current_row_eval = df_reset.iloc[i]
                
                # Fast inline signal evaluation (avoids DataFrame copy per bar)
                row = current_row_eval
                critical_indicators = ["atr_14", "adx_14", "vwap", "rsi_14", "relative_volume"]
                skip_signal = False
                for ind in critical_indicators:
                    val = row.get(ind, np.nan)
                    if pd.isna(val) or (isinstance(val, float) and np.isnan(val)):
                        skip_signal = True
                        break
                if skip_signal:
                    continue
                
                o = row.get("open", np.nan)
                h = row.get("high", np.nan)
                l = row.get("low", np.nan)
                c = row.get("close", np.nan)
                if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
                    continue
                if not (o > 0 and h > 0 and l > 0 and c > 0):
                    continue
                if not (h >= max(o, c) and l <= min(o, c)):
                    continue
                
                # Score components
                ema_9 = row.get("ema_9", np.nan)
                ema_20 = row.get("ema_20", np.nan)
                ema_50 = row.get("ema_50", np.nan)
                adx = row.get("adx_14", np.nan)
                rsi = row.get("rsi_14", 50)
                macd_hist = row.get("macd_histogram", 0)
                roc = row.get("roc_5", 0)
                rel_vol = row.get("relative_volume", 1.0)
                vwap_val = row.get("vwap", np.nan)
                price = c
                dist = row.get("distance_from_vwap", 0)
                or_high = row.get("opening_range_high", np.nan)
                or_low = row.get("opening_range_low", np.nan)
                prev_high = row.get("prev_high", np.nan)
                prev_low = row.get("prev_low", np.nan)
                bb_upper = row.get("bb_upper", np.nan)
                bb_lower = row.get("bb_lower", np.nan)
                vol_20 = row.get("volatility_20", np.nan)
                
                # Trend score (max 20)
                trend_score = 0.0
                if not pd.isna(ema_9) and not pd.isna(ema_20) and not pd.isna(ema_50):
                    if ema_9 > ema_20 > ema_50:
                        trend_score = 20
                    elif ema_9 > ema_20:
                        trend_score = 12
                    elif ema_9 < ema_20 < ema_50:
                        trend_score = 0
                    elif ema_9 < ema_20:
                        trend_score = 6
                if not pd.isna(adx):
                    if adx > 25:
                        trend_score = min(20, trend_score + 2)
                    elif adx < 15:
                        trend_score = max(0, trend_score - 3)
                
                # Momentum score (max 15)
                momentum_score = 0.0
                if not pd.isna(rsi):
                    if 40 <= rsi <= 60:
                        momentum_score += 5
                    elif 30 <= rsi < 40:
                        momentum_score += 8
                    elif rsi < 30:
                        momentum_score += 3
                    elif 60 < rsi <= 70:
                        momentum_score += 8
                    elif rsi > 70:
                        momentum_score += 3
                if not pd.isna(macd_hist):
                    if macd_hist > 0:
                        momentum_score += 5
                    else:
                        momentum_score += 1
                if not pd.isna(roc):
                    if abs(roc) > 1:
                        momentum_score += 2
                momentum_score = min(15, momentum_score)
                
                # Volume score (max 15)
                volume_score = 0.0
                if pd.isna(rel_vol):
                    volume_score = 0.0
                elif rel_vol > 2.0:
                    volume_score = 15
                elif rel_vol > 1.5:
                    volume_score = 12
                elif rel_vol > 1.0:
                    volume_score = 8
                elif rel_vol > 0.5:
                    volume_score = 4
                else:
                    volume_score = 1
                
                # VWAP score (max 15)
                vwap_score = 0.0
                if pd.isna(dist) or pd.isna(vwap_val) or vwap_val == 0:
                    vwap_score = 0.0
                elif dist > 0:
                    vwap_score = min(15, 12 + min(3, dist * 2))
                else:
                    vwap_score = min(15, max(0, 12 + dist * 2))
                
                # Price action score (max 15)
                pa_score = 0.0
                if pd.isna(or_high) or pd.isna(or_low):
                    pa_score = 7.5
                else:
                    if price > or_high:
                        pa_score += 8
                    elif price < or_low:
                        pa_score += 8
                    else:
                        pa_score += 4
                if not pd.isna(prev_high) and price > prev_high:
                    pa_score += 4
                if not pd.isna(prev_low) and price < prev_low:
                    pa_score += 4
                if not pd.isna(bb_upper) and price > bb_upper:
                    pa_score += 3
                elif not pd.isna(bb_lower) and price < bb_lower:
                    pa_score += 3
                pa_score = min(15, pa_score)
                
                # Market context score (max 10)
                ctx_score = 5.0
                
                # Risk quality score (max 10)
                risk_score = 0.0
                if pd.isna(row.get("atr_14", np.nan)) or pd.isna(price) or price == 0:
                    risk_score = 5.0
                else:
                    atr_val = row.get("atr_14", np.nan)
                    atr_pct = (atr_val / price) * 100
                    if 0.3 < atr_pct < 2.0:
                        risk_score = 10
                    elif atr_pct <= 0.3:
                        risk_score = 3
                    elif atr_pct >= 2.0:
                        risk_score = 5
                
                total = trend_score + momentum_score + volume_score + vwap_score + pa_score + ctx_score + risk_score
                
                classified = sv.determine_direction(total)
                if classified == SignalDirection.NO_TRADE:
                    continue
                if classified.is_weak:
                    continue
                if total < sv.min_score:
                    continue
                
                direction_enum = classified
                direction_str = direction_enum.value
                score_total = total

                entry_row = df_reset.iloc[next_idx]
                entry_price = entry_row['open']
                signal_row = current_row

                row_for_setup = signal_row.copy()
                setup = compute_trade_setup(row_for_setup, direction_enum, sv)

                min_rr = sv.get_min_rr(direction_enum)
                if setup.risk_reward_ratio < min_rr:
                    continue

                is_long_signal = direction_enum.is_long

                is_strong = direction_enum.is_strong
                if is_strong:
                    adx_val = signal_row.get('adx_14', 0)
                    rel_vol = signal_row.get('relative_volume', 0)
                    rsi_val = signal_row.get('rsi_14', 50)
                    vwap_val = signal_row.get('vwap', 0)
                    ok, rejections = sv.should_accept_strong(
                        adx_val, rel_vol, rsi_val, entry_price, vwap_val, direction_enum
                    )
                    if not ok:
                        continue

                risk = abs(entry_price - setup.stop_loss)
                if risk <= 0:
                    continue

                max_risk = capital * (sv.risk_per_trade_pct / 100.0)
                qty = int(max_risk / risk)
                max_qty_by_capital = int((capital * 0.1) / entry_price)
                qty = min(qty, max_qty_by_capital)

                if qty <= 0:
                    continue

                target = setup.target_1

                entry_slip = entry_price * self.slippage_pct / 100
                if is_long_signal:
                    actual_entry = entry_price + entry_slip
                else:
                    actual_entry = entry_price - entry_slip

                capital -= actual_entry * qty

                signal_info = {
                    'score': score_total,
                    'confidence': round(min(100, score_total * 0.7 + 10), 1),
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
                if pos['direction'] in (
                    SignalDirection.STRONG_LONG,
                    SignalDirection.LONG,
                    SignalDirection.WEAK_LONG,
                ):
                    unrealized = (current_close - pos['entry']) * pos['quantity']
                else:
                    unrealized = (pos['entry'] - current_close) * pos['quantity']
            else:
                unrealized = 0.0

            current_equity = capital + unrealized
            equity_curve.append({
                'timestamp': str(current_ts),
                'equity': round(current_equity, 2),
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
            sharpe = (returns.mean() / returns.std()) * np.sqrt(252 * 75)
            downside = returns[returns < 0]
            if len(downside) > 0 and downside.std() > 0:
                sortino = (returns.mean() / downside.std()) * np.sqrt(252 * 75)

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
