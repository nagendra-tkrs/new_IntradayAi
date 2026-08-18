import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional
from app.services.indicators import calculate_all_indicators
from app.services.signal_engine import evaluate_signal, compute_trade_setup
from app.models.schemas import SignalDirection
from app.core.market_session import IST
import uuid


class BacktestEngine:
    def __init__(
        self,
        initial_capital: float = 1_000_000.0,
        brokerage_pct: float = 0.03,
        stt_pct: float = 0.025,
        slippage_pct: float = 0.05,
        max_positions: int = 1,
    ):
        self.initial_capital = initial_capital
        self.brokerage_pct = brokerage_pct
        self.stt_pct = stt_pct
        self.slippage_pct = slippage_pct
        self.max_positions = max_positions

    def run(
        self, df: pd.DataFrame, symbol: str, strategy: str = "multi_factor"
    ) -> dict:
        if len(df) < 60:
            return {"error": "Insufficient data for backtest", "trades": []}

        capital = self.initial_capital
        equity_curve = []
        trades = []
        open_position = None
        max_equity = capital
        max_drawdown = 0.0
        wins = 0
        losses = 0
        total_profit = 0.0
        total_loss = 0.0

        for i in range(55, len(df)):
            window = df.iloc[:i+1].copy()
            window = calculate_all_indicators(window)
            current_row = window.iloc[-1]
            price = current_row["close"]
            unrealized = 0.0

            if open_position:
                if open_position["direction"] == "LONG":
                    unrealized = (price - open_position["entry"]) * open_position["quantity"]
                else:
                    unrealized = (open_position["entry"] - price) * open_position["quantity"]

            current_equity = capital + unrealized
            equity_curve.append({
                "timestamp": str(current_row.get("timestamp", i)),
                "equity": round(current_equity, 2),
            })
            if current_equity > max_equity:
                max_equity = current_equity
            dd = (max_equity - current_equity) / max_equity * 100 if max_equity > 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd

            if open_position:
                should_exit = False
                exit_price = price
                if open_position["direction"] == "LONG":
                    if price <= open_position["stop_loss"]:
                        should_exit = True
                        exit_price = open_position["stop_loss"]
                    elif price >= open_position["target_1"]:
                        should_exit = True
                        exit_price = open_position["target_1"]
                else:
                    if price >= open_position["stop_loss"]:
                        should_exit = True
                        exit_price = open_position["stop_loss"]
                    elif price <= open_position["target_1"]:
                        should_exit = True
                        exit_price = open_position["target_1"]

                if should_exit:
                    entry = open_position["entry"]
                    qty = open_position["quantity"]
                    slippage = exit_price * self.slippage_pct / 100
                    actual_exit = exit_price - slippage if open_position["direction"] == "LONG" else exit_price + slippage
                    cost = (entry * qty * self.brokerage_pct / 100) + (actual_exit * qty * self.brokerage_pct / 100)
                    stt = (entry * qty * self.stt_pct / 100) + (actual_exit * qty * self.stt_pct / 100)

                    if open_position["direction"] == "LONG":
                        pnl = (actual_exit - entry) * qty - cost - stt
                    else:
                        pnl = (entry - actual_exit) * qty - cost - stt

                    capital += pnl + entry * qty
                    if pnl > 0:
                        wins += 1
                        total_profit += pnl
                    else:
                        losses += 1
                        total_loss += abs(pnl)

                    trades.append({
                        "entry_time": str(open_position.get("entry_time", "")),
                        "exit_time": str(current_row.get("timestamp", "")),
                        "direction": open_position["direction"],
                        "entry": entry,
                        "exit": round(actual_exit, 2),
                        "quantity": qty,
                        "pnl": round(pnl, 2),
                        "cost": round(cost + stt, 2),
                    })
                    open_position = None

            if open_position is None:
                signal = evaluate_signal(window, symbol, strategy=strategy)
                if signal and signal["direction"] != "NO TRADE":
                    direction = signal["direction"]
                    entry_price = price + (price * self.slippage_pct / 100 if direction == "LONG" else -price * self.slippage_pct / 100)
                    setup = compute_trade_setup(current_row, direction)
                    if setup.risk_reward_ratio >= 1.2:
                        risk = abs(entry_price - setup.stop_loss)
                        if risk > 0:
                            qty = int((capital * 0.02) / risk)
                            max_qty = int(capital * 0.9 / entry_price)
                            qty = min(qty, max_qty)
                            if qty > 0:
                                capital -= qty * entry_price
                                open_position = {
                                    "direction": direction,
                                    "entry": entry_price,
                                    "stop_loss": setup.stop_loss,
                                    "target_1": setup.target_1,
                                    "quantity": qty,
                                    "entry_time": str(current_row.get("timestamp", "")),
                                }

        total_trades = len(trades)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        avg_win = total_profit / wins if wins > 0 else 0
        avg_loss = total_loss / losses if losses > 0 else 0
        profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')
        expectancy = (total_profit - total_loss) / total_trades if total_trades > 0 else 0

        total_pnl = sum(t["pnl"] for t in trades)
        final_capital = self.initial_capital + total_pnl

        returns = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i-1]["equity"]
            curr = equity_curve[i]["equity"]
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

        return {
            "id": uuid.uuid4().hex[:16],
            "symbol": symbol,
            "strategy": strategy,
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "final_capital": round(final_capital, 2),
            "initial_capital": self.initial_capital,
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else 999.0,
            "max_drawdown": round(max_drawdown, 2),
            "sharpe_ratio": round(float(sharpe), 2),
            "sortino_ratio": round(float(sortino), 2),
            "expectancy": round(expectancy, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "trades": trades,
            "equity_curve": equity_curve,
        }
