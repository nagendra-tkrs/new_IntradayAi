import math
import os
import sqlite3
import uuid
from typing import Optional
from app.core.config import settings
from app.core.market_session import now_ist


def _resolve_sqlite_path(db_url: str) -> Optional[str]:
    """Extract the SQLite file path from a SQLAlchemy async URL.

    Handles ``sqlite+aiosqlite:///./intradayai.db`` (relative) and
    ``/C:/...`` style Windows absolute paths."""
    if not db_url or "sqlite" not in db_url:
        return None
    idx = db_url.find("///")
    if idx == -1:
        return None
    path = db_url[idx + 3:]
    if path.startswith("/") and not os.path.exists(path):
        candidate = path[1:]  # Windows absolute path looks like /C:/...
        if os.path.exists(candidate):
            path = candidate
    return path or None


def load_trades_seed(user_id: str, db_path: Optional[str] = None) -> tuple[float, list[dict]]:
    """Read-only replay of the persistent closed-trade ledger for one user.

    The SQLite ``trades`` table is the only durable record of closed paper
    trades (open positions/pending orders live in memory only). A freshly
    created in-memory account replays this ledger so its cash, realized P&L
    and closed-trade history agree with ``/api/paper/trades`` and
    ``/api/paper/performance`` across process restarts. Never writes."""
    if not user_id:
        return 0.0, []
    if db_path is None:
        db_path = _resolve_sqlite_path(settings.DATABASE_URL)
    if not db_path or not os.path.exists(db_path):
        return 0.0, []
    try:
        uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5)
    except Exception:
        return 0.0, []
    try:
        rows = con.execute(
            "SELECT id, symbol, direction, entry_price, exit_price, quantity, "
            "stop_loss, target_1, target_2, entry_time, exit_time, pnl, status "
            "FROM trades WHERE user_id = ? ORDER BY exit_time",
            (user_id,),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        con.close()

    trades = []
    total = 0.0
    for r in rows:
        pnl = float(r[11] or 0.0)
        total += pnl
        exit_price = r[4] if r[4] is not None else r[3]
        trades.append({
            "id": r[0],
            "symbol": r[1],
            "direction": r[2],
            "entry_price": r[3],
            "exit_price": exit_price,
            "quantity": r[5],
            "stop_loss": r[6] or 0.0,
            "target_1": r[7] or 0.0,
            "target_2": r[8] or 0.0,
            "opened_at": r[9] or "",
            "exit_time": r[10] or "",
            "current_price": exit_price,
            "unrealized_pnl": 0.0,
            "pnl": round(pnl, 2),
            "result": "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN"),
            "status": r[12] or "closed",
            "user_id": user_id,
            "filled_at": r[9] or "",
            "fees": 0.0,
            "slippage": 0.0,
        })
    return float(total or 0.0), trades


def _resolve_sqlite_path(db_url: str) -> Optional[str]:
    """Extract the SQLite file path from a SQLAlchemy async URL.

    Handles ``sqlite+aiosqlite:///./intradayai.db`` (relative) and
    ``/C:/...`` style Windows absolute paths."""
    if not db_url or "sqlite" not in db_url:
        return None
    idx = db_url.find("///")
    if idx == -1:
        return None
    path = db_url[idx + 3:]
    if path.startswith("/") and not os.path.exists(path):
        candidate = path[1:]  # Windows absolute path looks like /C:/...
        if os.path.exists(candidate):
            path = candidate
    return path or None


def _ensure_state_schema(con: sqlite3.Connection):
    """Create the durable open-position / pending-order tables if missing and
    migrate older tables that lack later-added columns (idempotent)."""
    con.execute(_POSITIONS_DDL)
    con.execute(_PENDING_DDL)
    for table, cols in {
        _PENDING_TABLE: {"unrealized_pnl": "REAL DEFAULT 0"},
    }.items():
        existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in cols.items():
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _row_to_position(row) -> dict:
    return {
        "id": row[0],
        "symbol": row[2],
        "direction": row[3],
        "quantity": row[4],
        "entry_price": row[5],
        "current_price": row[6] if row[6] is not None else row[5],
        "stop_loss": row[7] or 0.0,
        "target_1": row[8] or 0.0,
        "target_2": row[9] or 0.0,
        "unrealized_pnl": row[10] or 0.0,
        "opened_at": row[11] or "",
        "status": row[12] or "open",
        "user_id": row[1],
        "filled_at": row[13],
    }


def load_account_state(user_id: str, db_path: Optional[str] = None) -> tuple[list[dict], list[dict]]:
    """Read-open persistent state: open positions + pending orders for one user.

    Both tables are authoritative for their entities across restarts; the
    in-memory PaperAccount is rebuilt from here + the closed-trade ledger."""
    if not user_id:
        return [], []
    if db_path is None:
        db_path = _resolve_sqlite_path(settings.DATABASE_URL)
    if not db_path or not os.path.exists(db_path):
        return [], []
    positions: list[dict] = []
    pending: list[dict] = []
    try:
        uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5)
    except Exception:
        return [], []
    try:
        rows = con.execute(
            "SELECT id, user_id, symbol, direction, quantity, entry_price, "
            "current_price, stop_loss, target_1, target_2, unrealized_pnl, "
            "opened_at, status, filled_at FROM paper_positions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        positions = [_row_to_position(r) for r in rows]
        rows = con.execute(
            "SELECT id, user_id, symbol, direction, quantity, entry_price, "
            "current_price, stop_loss, target_1, target_2, unrealized_pnl, "
            "opened_at, status, filled_at FROM paper_pending_orders WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        pending = [_row_to_position(r) for r in rows if r[12] in (None, "", "pending")]
    except Exception:
        positions, pending = [], []
    finally:
        con.close()
    return positions, pending


def persist_account_state(
    user_id: str,
    positions: list[dict],
    pending: list[dict],
    db_path: Optional[str] = None,
):
    """Write-through snapshot of one user's open positions + pending orders.

    Idempotent: replaces every row for the user (the in-memory account is the
    working copy; durable rows mirror it exactly, so stale rows can never
    survive a mutation). Runs in a single transaction."""
    if not user_id:
        return
    if db_path is None:
        db_path = _resolve_sqlite_path(settings.DATABASE_URL)
    if not db_path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    con = sqlite3.connect(db_path, timeout=10)
    try:
        _ensure_state_schema(con)
        with con:
            con.execute("DELETE FROM paper_positions WHERE user_id = ?", (user_id,))
            con.execute("DELETE FROM paper_pending_orders WHERE user_id = ?", (user_id,))
            for p in positions:
                con.execute(
                    "INSERT INTO paper_positions (id, user_id, symbol, direction, "
                    "quantity, entry_price, current_price, stop_loss, target_1, "
                    "target_2, unrealized_pnl, opened_at, status, filled_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        p.get("id", ""), user_id, p.get("symbol", ""),
                        p.get("direction", ""), p.get("quantity", 0),
                        p.get("entry_price", 0), p.get("current_price", p.get("entry_price", 0)),
                        p.get("stop_loss", 0.0), p.get("target_1", 0.0),
                        p.get("target_2", 0.0), p.get("unrealized_pnl", 0.0),
                        p.get("opened_at", "") or "", p.get("status", "open"),
                        p.get("filled_at"),
                    ),
                )
            for o in pending:
                con.execute(
                    "INSERT INTO paper_pending_orders (id, user_id, symbol, direction, "
                    "quantity, entry_price, current_price, stop_loss, target_1, "
                    "target_2, unrealized_pnl, opened_at, status, filled_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        o.get("id", ""), user_id, o.get("symbol", ""),
                        o.get("direction", ""), o.get("quantity", 0),
                        o.get("entry_price", 0), o.get("current_price", o.get("entry_price", 0)),
                        o.get("stop_loss", 0.0), o.get("target_1", 0.0),
                        o.get("target_2", 0.0), o.get("unrealized_pnl", 0.0),
                        o.get("opened_at", "") or "", o.get("status", "pending"),
                        None,
                    ),
                )
    finally:
        con.close()


def available_cash_for(
    initial_capital: float,
    realized: float,
    committed_open: float,
    pending_committed: float,
) -> float:
    """Contract formula: buying power never includes short-sale proceeds."""
    return initial_capital + realized - committed_open - pending_committed


class PaperAccount:
    """Per-user paper-trading account.

    Owns one user's cash ledger, pending orders, open positions and closed-trade
    history. All paper-trading logic (order placement, fills, closes, price
    marking, stop-loss/target monitoring) operates on a single account so one
    user's portfolio can never leak into another user's. Each account starts
    with the full documented notional capital (INITIAL_CAPITAL).
    """

    def __init__(self, user_id: Optional[str] = None):
        self.user_id = user_id
        self.cash: float = settings.INITIAL_CAPITAL
        self.pending_orders: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}
        self.closed_trades: list[dict] = []
        self.total_pnl: float = 0.0

    def seed_history(self, realized: float, trades: list[dict]):
        """Replay the persistent closed-trade ledger into a fresh account.

        The engine reads the SQLite ``trades`` table (read-only) at account
        creation and applies it here so cash and realized P&L include every
        closed trade the user made before this process started. Open positions
        and pending orders are replayed separately by restore_state()."""
        if trades:
            self.closed_trades = list(trades)
        self.total_pnl = float(realized or 0.0)
        self.cash = settings.INITIAL_CAPITAL + self.total_pnl

    def restore_state(self, positions: list[dict], pending: list[dict]):
        """Rebuild open positions + pending orders from persistent rows.

        Called at account creation (server restart) after seed_history(). Cash
        is reconstructed from the ledger plus the restored open positions:
        longs have already disbursed their entry notional, shorts still hold
        their sale proceeds. Unrealized P&L is recomputed from the last
        persisted mark (the 5s monitor refreshes it soon after)."""
        for p in positions:
            p["user_id"] = p.get("user_id") or self.user_id
            qty = p["quantity"]
            ep = p["entry_price"]
            cp = p.get("current_price") or ep
            p["current_price"] = cp
            if p.get("direction") in ("SHORT", "SELL"):
                p["unrealized_pnl"] = round((ep - cp) * qty, 2)
            else:
                p["unrealized_pnl"] = round((cp - ep) * qty, 2)
            self.positions[p["id"]] = p
        for o in pending:
            o["user_id"] = o.get("user_id") or self.user_id
            o["unrealized_pnl"] = 0.0
            self.pending_orders[o["id"]] = o
        self.cash = settings.INITIAL_CAPITAL + self.total_pnl
        for p in self.positions.values():
            notional = p["quantity"] * p["entry_price"]
            if p.get("direction") in ("SHORT", "SELL"):
                self.cash += notional
            else:
                self.cash -= notional

    def _position_value(self, pos: dict) -> float:
        """Signed market contribution of one open position to portfolio value.

        LONG/BUY positions add their market value; SHORT/SELL positions are a
        liability whose notional proceeds are already inside cash, so they
        subtract their buy-back market value. The direction sign is internal;
        no public field's meaning is changed."""
        value = pos["quantity"] * pos["current_price"]
        if pos.get("direction") in ("SHORT", "SELL"):
            return -value
        return value

    def _reserved_margin(self, pos: dict) -> float:
        """Cash reserved as collateral against one open short.

        Under the cash-secured (full-notch) model every open SHORT/SELL locks
        collateral equal to its notional at entry so Available Cash can never
        be inflated by sale proceeds and equity never claims unrealised value
        doesn't exist."""
        return pos["quantity"] * pos["entry_price"]

    def _short_liability(self, pos: dict) -> float:
        """Mark-to-market buy-back liability of one open SHORT/SELL.

        Gross replacement cost = current_price × quantity.  The equity
        invariant (total_value = cash + Σ position_value) holds because
        _position_value returns -qty×current for shorts and cash already
        contains the sale proceeds."""
        return pos["quantity"] * pos["current_price"]

    def _collateral_locked(self) -> float:
        return sum(
            self._reserved_margin(pos) for pos in self.positions.values()
            if pos.get("direction") in ("SHORT", "SELL")
        )

    def _pending_committed(self) -> float:
        """Capital committed by unfilled orders (entry notional each).

        Placing a pending order earmarks its entry notional so it cannot be
        spent elsewhere before the order fills or is cancelled. The same
        notional is reused as short collateral (SHORT) or becomes a cash
        disbursement (LONG) on fill - never reserved twice."""
        return sum(
            o["quantity"] * o.get("entry_price", 0)
            for o in self.pending_orders.values()
        )

    def _reserved_total(self) -> float:
        """Total cash unavailable for reuse: short collateral + pending orders."""
        return self._collateral_locked() + self._pending_committed()

    def _committed_capital(self) -> float:
        """Capital committed by every open position (LONG or SHORT).

        Under the cash-secured contract, both LONG disbursements and SHORT
        collateral are subtracted from buying power. Available cash is
        never inflated by short-sale proceeds."""
        return sum(
            pos["quantity"] * pos.get("entry_price", 0)
            for pos in self.positions.values()
        )

    def _available_cash(self) -> float:
        """Contract formula: buying power excludes short-sale proceeds entirely."""
        return available_cash_for(
            settings.INITIAL_CAPITAL,
            self.total_pnl,
            self._committed_capital(),
            self._pending_committed(),
        )

    def get_portfolio_summary(self) -> dict:
        unrealized = sum(
            self._unrealized_pnl(pos) for pos in self.positions.values()
        )
        total_value = self.cash + sum(
            self._position_value(pos) for pos in self.positions.values()
        )
        committed_open = self._committed_capital()
        pending_value = self._pending_committed()
        reserved = committed_open + pending_value
        short_liability = sum(
            self._short_liability(pos) for pos in self.positions.values()
            if pos.get("direction") in ("SHORT", "SELL")
        )
        return {
            "cash": round(self.cash, 2),
            "total_value": round(total_value, 2),
            "reserved_margin": round(reserved, 2),
            "available_cash": round(self._available_cash(), 2),
            "short_liability": round(short_liability, 2),
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
        if entry_price is None or entry_price <= 0:
            return {"error": "Invalid entry price"}
        # Directional relational validation (BUY/LONG: SL < Entry < Target;
        # SELL/SHORT: Target < Entry < SL). The backend is the final authority
        # and must reject invalid orders BEFORE any state (positions/cash/orders)
        # is modified, even if frontend validation was bypassed.
        direction = (direction or "").upper()
        is_long = direction in ("LONG", "BUY")
        if stop_loss and stop_loss > 0 and target_1 and target_1 > 0:
            if is_long:
                if not (stop_loss < entry_price < target_1):
                    return {"error": "Invalid LONG setup: stop loss must be below entry price and target must be above entry price."}
            else:
                if not (target_1 < entry_price < stop_loss):
                    return {"error": "Invalid SHORT setup: stop loss must be above entry price and target must be below entry price."}
        position_id = uuid.uuid4().hex[:16]
        # Contract gate: placing an order must not make Available Cash negative.
        # The entry notional it reserves (LONG disbursement or SHORT collateral)
        # must fit inside the current buying power.
        notional = quantity * entry_price
        available = self._available_cash()
        if notional > available + 1e-9:
            return {
                "error": "Insufficient available cash for order",
                "required": round(notional, 2),
                "available": round(available, 2),
            }
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
        # Contract gate: the edited reservation must still fit the buying power
        # (this order's current reservation is released before the check).
        old_notional = order["quantity"] * order["entry_price"]
        new_notional = new_qty * new_entry
        available_without_this = self._available_cash() + old_notional
        if new_notional > available_without_this + 1e-9:
            return {
                "error": "Insufficient available cash for order",
                "required": round(new_notional, 2),
                "available": round(available_without_this, 2),
            }
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

        quantity = order["quantity"]
        entry_price = order["entry_price"]
        total_cost = quantity * entry_price
        is_long = order["direction"] in ("LONG", "BUY")
        # Contract gate for BOTH directions: the fill must not push Available
        # Cash below zero. The order was already popped, so its pending
        # reservation is not counted any more; on success it becomes
        # committed-open capital instead (never reserved twice).
        available_cash = self._available_cash()
        if total_cost > available_cash + 1e-9:
            self.pending_orders[position_id] = order
            return {
                "error": "Insufficient available cash",
                "required": round(total_cost, 2),
                "available": round(available_cash, 2),
            }
        if is_long:
            self.cash -= total_cost
        else:
            self.cash += total_cost
        order["status"] = "open"
        order["filled_at"] = now_ist().isoformat()
        self.positions[position_id] = order
        return {"order_id": position_id, "status": "filled", "position": order}

    def cancel_order(self, position_id: str) -> dict:
        if position_id not in self.pending_orders:
            return {"error": "Order not found"}
        self.pending_orders.pop(position_id)
        return {"order_id": position_id, "status": "cancelled"}

    def get_pending_orders(self) -> list[dict]:
        return list(self.pending_orders.values())

    def close_position(self, position_id: str, exit_price: float, user_id: Optional[str] = None) -> dict:
        if position_id not in self.positions:
            return {"error": "Position not found"}
        pos = self.positions.pop(position_id)
        quantity = pos["quantity"]
        entry_price = pos["entry_price"]
        proceeds = quantity * exit_price
        if pos["direction"] in ("LONG", "BUY"):
            self.cash += proceeds
            pnl = (exit_price - entry_price) * quantity
        else:
            self.cash -= proceeds
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
            "user_id": user_id or self.user_id or pos.get("user_id"),
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

    def check_entry_triggers(self, prices: dict[str, float]) -> list[dict]:
        """Auto-fill pending orders whose entry condition is reached.

        A LONG/BUY order triggers when market_price >= entry_price; a
        SELL/SHORT order triggers when market_price <= entry_price. Eligible
        orders fill through the existing fill_order lifecycle at the order's
        configured entry price. Only pending orders are eligible, cash is not
        touched before the fill, open-position rules (duplicate symbol, max
        simultaneous positions) are respected, and invalid market prices never
        trigger a fill.
        """
        fills = []
        open_symbols = {pos["symbol"] for pos in self.positions.values()}
        filled = len(self.positions)
        for pos_id, order in list(self.pending_orders.items()):
            if order.get("status") != "pending":
                continue
            symbol = order["symbol"]
            if symbol in open_symbols:
                continue
            if symbol not in prices:
                continue
            try:
                price = float(prices[symbol])
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(price) and price > 0):
                continue
            if order["direction"] in ("LONG", "BUY"):
                if price < order["entry_price"]:
                    continue
            else:
                if price > order["entry_price"]:
                    continue
            if filled >= settings.MAX_SIMULTANEOUS_POSITIONS:
                break
            result = self.fill_order(pos_id)
            if "error" not in result:
                if order["direction"] in ("LONG", "BUY"):
                    self.positions[pos_id]["direction"] = "LONG"
                else:
                    self.positions[pos_id]["direction"] = "SHORT"
                open_symbols.add(symbol)
                filled += 1
                fills.append(result)
        return fills

    def get_positions(self) -> list[dict]:
        return list(self.positions.values())

    def get_trade_history(self, limit: int = 50) -> list[dict]:
        return self.closed_trades[-limit:]

    def _unrealized_pnl(self, pos: dict) -> float:
        return pos.get("unrealized_pnl", 0.0)


class PaperTradingEngine:
    """Container of per-user PaperAccount objects.

    Backwards-compatible facade over the original single-account engine:
     - methods called WITHOUT user_id behave like before (writes target a
       default account, order/position lookups resolve the owning account,
       reads aggregate across accounts);
     - every API endpoint passes an explicit user_id so accounts are fully
       isolated and one user can never read or mutate another user's state.
    """

    def __init__(self, db_path: Optional[str] = None, persist: bool = False):
        self.accounts: dict[Optional[str], PaperAccount] = {}
        self._db_path = db_path
        self._persist = bool(persist)

    def _ensure_account(self, user_id: Optional[str]) -> PaperAccount:
        if user_id not in self.accounts:
            acc = PaperAccount(user_id=user_id)
            if user_id:
                # Replay the durable closed-trade ledger (read-only) so a fresh
                # account agrees with /paper/trades, /paper/performance and the
                # portfolio summary across process restarts.
                realized, trades = load_trades_seed(user_id, db_path=self._db_path)
                if realized or trades:
                    acc.seed_history(realized, trades)
                if self._persist:
                    # Restart restore: open positions + pending orders are the
                    # durable source of truth; rebuild them so a restart never
                    # resets live paper-trading state.
                    positions, pending = load_account_state(user_id, db_path=self._db_path)
                    if positions or pending:
                        acc.restore_state(positions, pending)
            self.accounts[user_id] = acc
        return self.accounts[user_id]

    def _resolve_account(self, user_id: Optional[str], container: str, obj_id: str) -> PaperAccount:
        if user_id:
            return self._ensure_account(user_id)
        for acc in self.accounts.values():
            if obj_id in getattr(acc, container):
                return acc
        return self._ensure_account(None)

    def persist_snapshot(self, user_id: Optional[str] = None):
        """Write-through the in-memory open positions + pending orders to SQLite.

        Called by every state-changing operation (place/edit/fill/cancel/close)
        and by the price monitor after marking or auto-execution, so the durable
        tables always mirror the working account. Non-persistent engines (tests,
        default single-account mode) are untouched."""
        if not self._persist:
            return
        if user_id is not None:
            accs = [self.accounts[user_id]]
        else:
            accs = list(self.accounts.values())
        for acc in accs:
            if acc.user_id:
                persist_account_state(
                    acc.user_id,
                    list(acc.positions.values()),
                    list(acc.pending_orders.values()),
                    db_path=self._db_path,
                )

    @property
    def cash(self) -> float:
        return self._ensure_account(None).cash

    @property
    def total_pnl(self) -> float:
        return self._ensure_account(None).total_pnl

    def place_order(
        self, symbol: str, direction: str, quantity: int,
        entry_price: float, stop_loss: float = 0,
        target_1: float = 0, target_2: float = 0,
        user_id: Optional[str] = None,
    ) -> dict:
        acc = self._ensure_account(user_id)
        result = acc.place_order(
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            user_id=user_id,
        )
        if "error" not in result:
            self.persist_snapshot(acc.user_id)
        return result

    def edit_order(
        self, position_id: str, user_id: Optional[str] = None,
        entry_price: Optional[float] = None, stop_loss: Optional[float] = None,
        target_1: Optional[float] = None, target_2: Optional[float] = None,
        quantity: Optional[int] = None,
    ) -> dict:
        acc = self._resolve_account(user_id, "pending_orders", position_id)
        result = acc.edit_order(
            position_id, user_id=user_id,
            entry_price=entry_price, stop_loss=stop_loss,
            target_1=target_1, target_2=target_2, quantity=quantity,
        )
        if "error" not in result:
            self.persist_snapshot(acc.user_id)
        return result

    def fill_order(self, position_id: str, fill_price: Optional[float] = None, user_id: Optional[str] = None) -> dict:
        acc = self._resolve_account(user_id, "pending_orders", position_id)
        result = acc.fill_order(position_id, fill_price=fill_price)
        if "error" not in result:
            self.persist_snapshot(acc.user_id)
        return result

    def cancel_order(self, position_id: str, user_id: Optional[str] = None) -> dict:
        acc = self._resolve_account(user_id, "pending_orders", position_id)
        result = acc.cancel_order(position_id)
        if "error" not in result:
            self.persist_snapshot(acc.user_id)
        return result

    def get_pending_orders(self, user_id: Optional[str] = None) -> list[dict]:
        if user_id is not None:
            return self._ensure_account(user_id).get_pending_orders()
        return [o for acc in self.accounts.values() for o in acc.get_pending_orders()]

    def get_pending_order(self, order_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        if user_id is not None:
            return self._ensure_account(user_id).pending_orders.get(order_id)
        for acc in self.accounts.values():
            if order_id in acc.pending_orders:
                return acc.pending_orders[order_id]
        return None

    def close_position(self, position_id: str, exit_price: float, user_id: Optional[str] = None) -> dict:
        acc = self._resolve_account(user_id, "positions", position_id)
        result = acc.close_position(position_id, exit_price, user_id=user_id)
        if "error" not in result:
            self.persist_snapshot(acc.user_id)
        return result

    def update_prices(self, prices: dict[str, float], user_id: Optional[str] = None):
        if user_id is not None:
            acc = self._ensure_account(user_id)
            acc.update_prices(prices)
            self.persist_snapshot(acc.user_id)
            return
        if not self.accounts:
            self._ensure_account(None)
        for acc in self.accounts.values():
            acc.update_prices(prices)
            self.persist_snapshot(acc.user_id)

    def check_stops(self, prices: dict[str, float], user_id: Optional[str] = None) -> list[dict]:
        if user_id is not None:
            acc = self._ensure_account(user_id)
            exits = acc.check_stops(prices)
            self.persist_snapshot(acc.user_id)
            return exits
        if not self.accounts:
            self._ensure_account(None)
        exits = []
        for acc in self.accounts.values():
            exits.extend(acc.check_stops(prices))
            self.persist_snapshot(acc.user_id)
        return exits

    def check_entry_triggers(self, prices: dict[str, float], user_id: Optional[str] = None) -> list[dict]:
        if user_id is not None:
            acc = self._ensure_account(user_id)
            fills = acc.check_entry_triggers(prices)
            self.persist_snapshot(acc.user_id)
            return fills
        if not self.accounts:
            self._ensure_account(None)
        fills = []
        for acc in self.accounts.values():
            fills.extend(acc.check_entry_triggers(prices))
            self.persist_snapshot(acc.user_id)
        return fills

    def get_positions(self, user_id: Optional[str] = None) -> list[dict]:
        if user_id is not None:
            return self._ensure_account(user_id).get_positions()
        return [p for acc in self.accounts.values() for p in acc.get_positions()]

    def get_position(self, position_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        if user_id is not None:
            return self._ensure_account(user_id).positions.get(position_id)
        for acc in self.accounts.values():
            if position_id in acc.positions:
                return acc.positions[position_id]
        return None

    def get_trade_history(self, limit: int = 50, user_id: Optional[str] = None) -> list[dict]:
        if user_id is not None:
            return self._ensure_account(user_id).get_trade_history(limit=limit)
        trades = [t for acc in self.accounts.values() for t in acc.closed_trades]
        return trades[-limit:]

    def get_closed_trades(self, user_id: Optional[str] = None) -> list[dict]:
        if user_id is not None:
            return list(self._ensure_account(user_id).closed_trades)
        return [t for acc in self.accounts.values() for t in acc.closed_trades]

    def get_portfolio_summary(self, user_id: Optional[str] = None) -> dict:
        if user_id is not None:
            return self._ensure_account(user_id).get_portfolio_summary()
        accs = list(self.accounts.values())
        if not accs:
            accs = [self._ensure_account(None)]
        totals: dict[str, float] = {}
        for acc in accs:
            s = acc.get_portfolio_summary()
            for k, v in s.items():
                if k == "initial_capital":
                    continue
                if isinstance(v, (int, float)):
                    totals[k] = totals.get(k, 0.0) + float(v)
        totals["initial_capital"] = settings.INITIAL_CAPITAL
        totals["positions_count"] = int(totals.get("positions_count", 0))
        totals["pending_orders_count"] = int(totals.get("pending_orders_count", 0))
        return totals