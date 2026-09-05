import uuid
from datetime import datetime
from typing import Optional
from app.core.config import settings
from app.core.market_session import now_ist


class PaperTradingEngine:
    def __init__(self):
        self.cash: float = settings.INITIAL_CAPITAL
        self.pending_orders: dict[str, dict] = {}
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
        pending_value = sum(
            o["quantity"] * o.get("entry_price", 0) for o in self.pending_orders.values()
        )
        return {
            "cash": round(self.cash, 2),
            "total_value": round(total_value, 2),
            "positions_count": len(self.positions),
            "pending_orders_count": len(self.pending_orders),
            "pending_value": round(pending_value, 2),
            "total_pnl": round(self.total_pnl, 2),
            "unrealized_pnl": round(unrealized, 2),
            "realized_pnl": round(self.total_pnl, 2),
            "initial_capital": settings.INITIAL_CAPITAL,
        }

    def place_order(
        self, symbol: str, direction: str, quantity: int,
        entry_price: float, stop_loss: float = 0,
        target_1: float = 0, target_2: float = 0,
        user_id: Optional[str] = None,
    ) -> dict:
        if quantity <= 0:
            return {"error": "Invalid quantity"}
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
            "status": "pending",
            "user_id": user_id,
            "filled_at": None,
        }
        self.pending_orders[position_id] = position
        return {"order_id": position_id, "status": "pending", "position": position}

    def edit_order(
        self, position_id: str, user_id: Optional[str] = None,
        entry_price: Optional[float] = None, stop_loss: Optional[float] = None,
        target_1: Optional[float] = None, target_2: Optional[float] = None,
        quantity: Optional[int] = None,
    ) -> dict:
        if position_id not in self.pending_orders:
            return {"error": "Order not found or already filled"}
        order = self.pending_orders[position_id]
        if order.get("status") != "pending":
            return {"error": "Cannot edit: order is already filled or closed"}
        if user_id and order.get("user_id") and order["user_id"] != user_id:
            return {"error": "Cannot edit another user's order"}
        new_entry = order["entry_price"] if entry_price is None else entry_price
        new_sl = order["stop_loss"] if stop_loss is None else stop_loss
        new_t1 = order["target_1"] if target_1 is None else target_1
        new_qty = order["quantity"] if quantity is None else quantity
        if new_entry is None or new_entry <= 0:
            return {"error": "Invalid entry price"}
        if new_qty is None or new_qty <= 0:
            return {"error": "Invalid quantity"}
        # Directional relational validation (BUY: SL < Entry < Target; SELL inverted)
        if new_sl is not None and new_sl > 0:
            if order.get("direction") in ("LONG", "BUY"):
                if not (new_sl < new_entry):
                    return {"error": "Stop Loss must be below Buy Price"}
            elif order.get("direction") in ("SHORT", "SELL"):
                if not (new_sl > new_entry):
                    return {"error": "Stop Loss must be above Entry"}
        if new_t1 is not None and new_t1 > 0:
            if order.get("direction") in ("LONG", "BUY"):
                if not (new_t1 > new_entry):
                    return {"error": "Target must be above Buy Price"}
            elif order.get("direction") in ("SHORT", "SELL"):
                if not (new_t1 < new_entry):
                    return {"error": "Target must be below Entry"}
        if entry_price is not None:
            order["entry_price"] = entry_price
            order["current_price"] = entry_price
        if stop_loss is not None:
            order["stop_loss"] = stop_loss
        if target_1 is not None:
            order["target_1"] = target_1
        if target_2 is not None:
            order["target_2"] = target_2
        if quantity is not None and quantity > 0:
            order["quantity"] = quantity
        return {"order_id": position_id, "status": "pending", "position": order}

    def fill_order(self, position_id: str, fill_price: Optional[float] = None) -> dict:
        if position_id not in self.pending_orders:
            return {"error": "Order not found"}
        order = self.pending_orders.pop(position_id)
        if order.get("status") != "pending":
            return {"error": "Order already processed"}

        if fill_price is not None:
            order["entry_price"] = fill_price
            order["current_price"] = fill_price

        total_cost = order["quantity"] * order["entry_price"]
        if total_cost > self.cash:
            self.pending_orders[position_id] = order
            return {"error": "Insufficient cash", "required": total_cost, "available": self.cash}

        self.cash -= total_cost
        order["status"] = "open"
        order["filled_at"] = now_ist().isoformat()
        self.positions[position_id] = order
        return {"order_id": position_id, "status": "filled", "position": order}

    def cancel_order(self, position_id: str) -> dict:
        if position_id not in self.pending_orders:
            return {"error": "Order not found"}
        order = self.pending_orders.pop(position_id)
        return {"order_id": position_id, "status": "cancelled"}

    def get_pending_orders(self) -> list[dict]:
        return list(self.pending_orders.values())

    def close_position(self, position_id: str, exit_price: float, user_id: str = None) -> dict:
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
        if pnl > 0:
            result = "WIN"
        elif pnl < 0:
            result = "LOSS"
        else:
            result = "BREAKEVEN"
        trade = {
            **pos,
            "exit_price": exit_price,
            "exit_time": now_ist().isoformat(),
            "pnl": round(pnl, 2),
            "result": result,
            "status": "closed",
            "user_id": user_id,
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
