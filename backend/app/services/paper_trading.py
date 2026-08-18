import uuid
from datetime import datetime
from typing import Optional
from app.core.config import settings
from app.core.market_session import now_ist


class PaperTradingEngine:
    def __init__(self):
        self.cash: float = settings.INITIAL_CAPITAL
        self.positions: dict[str, dict] = {}
        self.closed_trades: list[dict] = []
        self.total_pnl: float = 0.0

    def get_portfolio_summary(self) -> dict:
        unrealized = sum(
            self._unrealized_pnl(pos) for pos in self.positions.values()
        )
        total_value = self.cash + sum(
            pos["quantity"] * pos["current_price"] for pos in self.positions.values()
        )
        return {
            "cash": round(self.cash, 2),
            "total_value": round(total_value, 2),
            "positions_count": len(self.positions),
            "total_pnl": round(self.total_pnl, 2),
            "unrealized_pnl": round(unrealized, 2),
            "realized_pnl": round(self.total_pnl - unrealized, 2),
            "initial_capital": settings.INITIAL_CAPITAL,
        }

    def place_order(
        self, symbol: str, direction: str, quantity: int,
        entry_price: float, stop_loss: float = 0,
        target_1: float = 0, target_2: float = 0,
    ) -> dict:
        total_cost = quantity * entry_price
        if total_cost > self.cash:
            return {"error": "Insufficient cash", "required": total_cost, "available": self.cash}
        if quantity <= 0:
            return {"error": "Invalid quantity"}
        self.cash -= total_cost
        position_id = uuid.uuid4().hex[:16]
        position = {
            "id": position_id,
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "entry_price": entry_price,
            "current_price": entry_price,
            "stop_loss": stop_loss,
            "target_1": target_1,
            "target_2": target_2,
            "unrealized_pnl": 0.0,
            "opened_at": now_ist().isoformat(),
            "status": "open",
        }
        self.positions[position_id] = position
        return {"order_id": position_id, "status": "filled", "position": position}

    def close_position(self, position_id: str, exit_price: float) -> dict:
        if position_id not in self.positions:
            return {"error": "Position not found"}
        pos = self.positions.pop(position_id)
        quantity = pos["quantity"]
        entry_price = pos["entry_price"]
        proceeds = quantity * exit_price
        self.cash += proceeds
        if pos["direction"] == "LONG":
            pnl = (exit_price - entry_price) * quantity
        else:
            pnl = (entry_price - exit_price) * quantity
        self.total_pnl += pnl
        trade = {
            **pos,
            "exit_price": exit_price,
            "exit_time": now_ist().isoformat(),
            "pnl": round(pnl, 2),
            "status": "closed",
        }
        self.closed_trades.append(trade)
        return {"trade": trade, "pnl": round(pnl, 2)}

    def update_prices(self, prices: dict[str, float]):
        for pos_id, pos in self.positions.items():
            if pos["symbol"] in prices:
                new_price = prices[pos["symbol"]]
                pos["current_price"] = new_price
                if pos["direction"] == "LONG":
                    pos["unrealized_pnl"] = (new_price - pos["entry_price"]) * pos["quantity"]
                else:
                    pos["unrealized_pnl"] = (pos["entry_price"] - new_price) * pos["quantity"]

    def check_stops(self, prices: dict[str, float]) -> list[dict]:
        exits = []
        for pos_id, pos in list(self.positions.items()):
            symbol = pos["symbol"]
            if symbol not in prices:
                continue
            price = prices[symbol]
            if pos["direction"] == "LONG":
                if pos["stop_loss"] and price <= pos["stop_loss"]:
                    exits.append(self.close_position(pos_id, price))
                elif pos["target_1"] and price >= pos["target_1"]:
                    exits.append(self.close_position(pos_id, price))
            elif pos["direction"] == "SHORT":
                if pos["stop_loss"] and price >= pos["stop_loss"]:
                    exits.append(self.close_position(pos_id, price))
                elif pos["target_1"] and price <= pos["target_1"]:
                    exits.append(self.close_position(pos_id, price))
        return exits

    def get_positions(self) -> list[dict]:
        return list(self.positions.values())

    def get_trade_history(self, limit: int = 50) -> list[dict]:
        return self.closed_trades[-limit:]

    def _unrealized_pnl(self, pos: dict) -> float:
        return pos.get("unrealized_pnl", 0.0)
