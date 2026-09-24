import json
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import select
from app.services.paper_trading import PaperTradingEngine
from app.services.risk_engine import RiskEngine, RiskConfig
from app.services.trade_setup import compute_risk_reward
from app.services.market_data.provider_factory import get_provider
from app.core.market_session import now_ist, is_market_hours
from app.core.database import get_db
from app.models.models import User
from app.api.auth import get_current_user

router = APIRouter(prefix="/api", tags=["trading"])
paper_engine = PaperTradingEngine(persist=True)
risk_engine = RiskEngine()

MAX_DUPLICATE_POSITIONS = 1

_provider = None

def _get_provider():
    global _provider
    if _provider is None:
        _provider = get_provider()
    return _provider


def _parse_dt(value):
    if not value:
        return None
    from datetime import datetime
    return datetime.fromisoformat(value)


class OrderRequest(BaseModel):
    symbol: str
    direction: str
    quantity: int
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_1: Optional[float] = None
    target_2: Optional[float] = None


class OrderEditRequest(BaseModel):
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_1: Optional[float] = None
    target_2: Optional[float] = None
    quantity: Optional[int] = None


class FillOrderRequest(BaseModel):
    fill_price: Optional[float] = None


class ClosePositionRequest(BaseModel):
    position_id: str
    exit_price: Optional[float] = None


async def _finalize_paper_trade(result: dict, user_id: str):
    """Shared close-finalization used by the manual close endpoint AND the
    automatic SL/Target monitor: persists the closed trade to SQLite and updates
    the shared risk engine (daily loss / consecutive-loss / position count)."""
    try:
        from app.core.database import async_session
        from app.models.models import Trade
        trade_dict = result.get("trade", {})
        async with async_session() as db:
            db_trade = Trade(
                id=trade_dict.get("id", ""),
                user_id=user_id,
                symbol=trade_dict.get("symbol", ""),
                direction=trade_dict.get("direction", ""),
                entry_price=trade_dict.get("entry_price", 0.0),
                exit_price=trade_dict.get("exit_price", 0.0),
                quantity=trade_dict.get("quantity", 1),
                stop_loss=trade_dict.get("stop_loss", 0.0),
                target_1=trade_dict.get("target_1", 0.0),
                target_2=trade_dict.get("target_2", 0.0),
                entry_time=_parse_dt(trade_dict.get("opened_at", "")),
                exit_time=_parse_dt(trade_dict.get("exit_time", "")),
                status=trade_dict.get("status", "closed"),
                pnl=trade_dict.get("pnl", 0.0),
                fees=trade_dict.get("fees", 0.0),
                slippage=trade_dict.get("slippage", 0.0),
                signal_id=trade_dict.get("signal_id"),
            )
            db.add(db_trade)
            await db.commit()
    except Exception as e:
        # Log but do not break the close flow; trade still in closed_trades
        print(f"Failed to persist trade to SQLite: {e}")
    pnl = result.get("pnl", 0)
    risk_engine.record_trade_result(pnl)
    risk_engine.state.open_positions = len(paper_engine.get_positions(user_id=user_id))


@router.get("/portfolio")
async def get_portfolio(user: User = Depends(get_current_user)):
    summary = paper_engine.get_portfolio_summary(user_id=user.id)
    positions = paper_engine.get_positions(user_id=user.id)
    return {**summary, "positions": positions}


@router.post("/paper/orders")
async def place_paper_order(order: OrderRequest, user: User = Depends(get_current_user)):
    open_positions = paper_engine.get_positions(user_id=user.id)
    if len(open_positions) >= risk_engine.config.max_simultaneous_positions:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum simultaneous positions ({risk_engine.config.max_simultaneous_positions}) reached",
        )

    open_symbols = {p["symbol"] for p in open_positions}
    pending_symbols = {p["symbol"] for p in paper_engine.get_pending_orders(user_id=user.id)}
    all_symbols = open_symbols | pending_symbols
    if order.symbol in all_symbols:
        raise HTTPException(
            status_code=400,
            detail=f"You already have an open or pending position in {order.symbol}",
        )

    can, reason = risk_engine.can_trade()
    if not can:
        raise HTTPException(status_code=400, detail=reason)

    entry_price = order.entry_price
    if entry_price is None:
        quote = await _get_provider().get_quote(order.symbol)
        entry_price = quote["price"]

    stop_loss = order.stop_loss or (entry_price * 0.97 if order.direction == "LONG" else entry_price * 1.03)
    target_1 = order.target_1 or (entry_price * 1.04 if order.direction == "LONG" else entry_price * 0.96)

    direction_label = "BUY" if order.direction.upper() in ("LONG", "BUY") else "SELL"
    setup_result = compute_risk_reward(entry_price, stop_loss, target_1, direction=direction_label)
    if not setup_result.valid:
        raise HTTPException(status_code=400, detail="; ".join(setup_result.errors))

    valid, msg = risk_engine.validate_setup(entry_price, stop_loss, target_1, order.direction)
    if not valid:
        raise HTTPException(status_code=400, detail=msg)

    qty = order.quantity
    if qty <= 0:
        qty = risk_engine.calculate_position_size(entry_price, stop_loss)
    if qty <= 0:
        raise HTTPException(status_code=400, detail="Position size is zero")

    result = paper_engine.place_order(
        symbol=order.symbol,
        direction=order.direction,
        quantity=qty,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target_1=target_1,
        target_2=order.target_2 or (entry_price * 1.06 if order.direction == "LONG" else entry_price * 0.94),
        user_id=user.id,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.patch("/paper/orders/{order_id}")
async def edit_paper_order(order_id: str, payload: OrderEditRequest, user: User = Depends(get_current_user)):
    order = paper_engine.get_pending_order(order_id, user_id=user.id)
    if not order:
        raise HTTPException(status_code=404, detail="Pending order not found")
    if order.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Cannot edit: order is already filled or closed")
    if order.get("user_id") and order["user_id"] != user.id:
        raise HTTPException(status_code=403, detail="Cannot edit another user's order")

    entry_price = payload.entry_price if payload.entry_price is not None else order["entry_price"]
    stop_loss = payload.stop_loss if payload.stop_loss is not None else order["stop_loss"]
    target_1 = payload.target_1 if payload.target_1 is not None else order["target_1"]
    quantity = payload.quantity if payload.quantity is not None else order["quantity"]

    direction_label = "BUY" if order["direction"].upper() in ("LONG", "BUY") else "SELL"
    setup_result = compute_risk_reward(entry_price, stop_loss, target_1, direction=direction_label)
    if not setup_result.valid:
        raise HTTPException(status_code=400, detail="; ".join(setup_result.errors))

    valid, msg = risk_engine.validate_setup(entry_price, stop_loss, target_1, order["direction"])
    if not valid:
        raise HTTPException(status_code=400, detail=msg)

    if quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be positive")

    result = paper_engine.edit_order(
        order_id, user_id=user.id,
        entry_price=payload.entry_price, stop_loss=payload.stop_loss,
        target_1=payload.target_1, target_2=payload.target_2,
        quantity=payload.quantity,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/paper/orders/{order_id}/fill")
async def fill_paper_order(order_id: str, req: FillOrderRequest = FillOrderRequest(), user: User = Depends(get_current_user)):
    order = paper_engine.get_pending_order(order_id, user_id=user.id)
    if not order:
        raise HTTPException(status_code=404, detail="Pending order not found")
    if order.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Order is already filled or closed")
    if order.get("user_id") and order["user_id"] != user.id:
        raise HTTPException(status_code=403, detail="Cannot fill another user's order")

    fill_price = req.fill_price
    if fill_price is None:
        try:
            quote = await _get_provider().get_quote(order["symbol"])
            fill_price = quote["price"]
        except Exception:
            fill_price = order["entry_price"]

    result = paper_engine.fill_order(order_id, fill_price=fill_price, user_id=user.id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    risk_engine.state.open_positions = len(paper_engine.get_positions(user_id=user.id))
    return result


@router.delete("/paper/orders/{order_id}")
async def cancel_paper_order(order_id: str, user: User = Depends(get_current_user)):
    order = paper_engine.get_pending_order(order_id, user_id=user.id)
    if not order:
        raise HTTPException(status_code=404, detail="Pending order not found")
    if order.get("user_id") and order["user_id"] != user.id:
        raise HTTPException(status_code=403, detail="Cannot cancel another user's order")
    result = paper_engine.cancel_order(order_id, user_id=user.id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/paper/pending")
async def get_pending_orders(user: User = Depends(get_current_user)):
    orders = paper_engine.get_pending_orders(user_id=user.id)
    return {"orders": orders}


@router.post("/paper/close")
async def close_paper_position(req: ClosePositionRequest, user: User = Depends(get_current_user)):
    exit_price = req.exit_price
    if exit_price is None:
        pos = paper_engine.get_position(req.position_id, user_id=user.id)
        if pos:
            quote = await _get_provider().get_quote(pos["symbol"])
            exit_price = quote["price"]
    if exit_price is None:
        raise HTTPException(status_code=400, detail="Cannot determine exit price")
    result = paper_engine.close_position(req.position_id, exit_price, user_id=user.id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    await _finalize_paper_trade(result, user.id)
    return result


@router.get("/paper/positions")
async def get_positions(user: User = Depends(get_current_user)):
    return {"positions": paper_engine.get_positions(user_id=user.id)}


def _trade_rows_to_dicts(db_trades) -> list[dict]:
    """Serialize SQLAlchemy Trade rows to the paper-trade dict shape shared by
    /paper/trades and /paper/performance so both use the same source of truth
    (the persistent SQLite ledger)."""
    trades = []
    for t in db_trades:
        trades.append({
            "id": t.id,
            "symbol": t.symbol,
            "direction": t.direction,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "quantity": t.quantity,
            "stop_loss": t.stop_loss,
            "target_1": t.target_1,
            "target_2": t.target_2,
            "entry_time": str(t.entry_time) if t.entry_time else "",
            "exit_time": str(t.exit_time) if t.exit_time else "",
            "pnl": t.pnl,
            "result": t.pnl > 0 and "WIN" or (t.pnl < 0 and "LOSS" or "BREAKEVEN"),
            "status": t.status,
        })
    return trades


async def _load_trades_from_db(user_id: str) -> list[dict]:
    from app.core.database import async_session
    from app.models.models import Trade
    async with async_session() as db:
        stmt = select(Trade).where(Trade.user_id == user_id).order_by(Trade.exit_time.desc())
        result = await db.execute(stmt)
        return _trade_rows_to_dicts(result.scalars().all())


@router.get("/paper/trades")
async def get_trade_history(limit: int = 50, user: User = Depends(get_current_user)):
    trades = await _load_trades_from_db(user.id)
    return {"trades": trades[:limit]}


@router.get("/paper/performance")
async def get_performance(user: User = Depends(get_current_user)):
    trades = await _load_trades_from_db(user.id)
    now = now_ist()
    daily_trades = []
    weekly_trades = []
    monthly_trades = []
    for t in trades:
        exit_time = t.get("exit_time", "")
        if not exit_time:
            continue
        try:
            from datetime import datetime
            trade_dt = datetime.fromisoformat(exit_time)
            if trade_dt.date() == now.date():
                daily_trades.append(t)
            if (now - trade_dt).days < 7:
                weekly_trades.append(t)
            if trade_dt.month == now.month and trade_dt.year == now.year:
                monthly_trades.append(t)
        except Exception:
            pass

    def calc_stats(trade_list):
        if not trade_list:
            return {"count": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0}
        wins = sum(1 for t in trade_list if t.get("pnl", 0) > 0)
        losses = sum(1 for t in trade_list if t.get("pnl", 0) < 0)
        total_pnl = sum(t.get("pnl", 0) for t in trade_list)
        return {
            "count": len(trade_list),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(trade_list) * 100, 1) if trade_list else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(trade_list), 2) if trade_list else 0,
        }

    return {
        "daily": calc_stats(daily_trades),
        "weekly": calc_stats(weekly_trades),
        "monthly": calc_stats(monthly_trades),
        "all_time": calc_stats(trades),
    }
