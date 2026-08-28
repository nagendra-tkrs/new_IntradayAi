import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.services.paper_trading import PaperTradingEngine
from app.services.risk_engine import RiskEngine, RiskConfig
from app.services.market_data.provider_factory import get_provider
from app.core.market_session import now_ist

router = APIRouter(prefix="/api", tags=["trading"])
paper_engine = PaperTradingEngine()
risk_engine = RiskEngine()

_provider = None

def _get_provider():
    global _provider
    if _provider is None:
        _provider = get_provider()
    return _provider


class OrderRequest(BaseModel):
    symbol: str
    direction: str
    quantity: int
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_1: Optional[float] = None
    target_2: Optional[float] = None


class ClosePositionRequest(BaseModel):
    position_id: str
    exit_price: Optional[float] = None


@router.get("/portfolio")
async def get_portfolio():
    summary = paper_engine.get_portfolio_summary()
    positions = paper_engine.get_positions()
    return {**summary, "positions": positions}


@router.post("/paper/orders")
async def place_paper_order(order: OrderRequest):
    can, reason = risk_engine.can_trade()
    if not can:
        raise HTTPException(status_code=400, detail=reason)
    entry_price = order.entry_price
    if entry_price is None:
        quote = await _get_provider().get_quote(order.symbol)
        entry_price = quote["price"]
    stop_loss = order.stop_loss or (entry_price * 0.97 if order.direction == "LONG" else entry_price * 1.03)
    target_1 = order.target_1 or (entry_price * 1.04 if order.direction == "LONG" else entry_price * 0.96)
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
        target_2=order.target_2 or entry_price * 1.04,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/paper/close")
async def close_paper_position(req: ClosePositionRequest):
    exit_price = req.exit_price
    if exit_price is None:
        pos = paper_engine.positions.get(req.position_id)
        if pos:
            quote = await _get_provider().get_quote(pos["symbol"])
            exit_price = quote["price"]
    if exit_price is None:
        raise HTTPException(status_code=400, detail="Cannot determine exit price")
    result = paper_engine.close_position(req.position_id, exit_price)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    pnl = result.get("pnl", 0)
    risk_engine.record_trade_result(pnl)
    return result


@router.get("/paper/positions")
async def get_positions():
    return {"positions": paper_engine.get_positions()}


@router.get("/paper/trades")
async def get_trade_history(limit: int = 50):
    return {"trades": paper_engine.get_trade_history(limit)}


@router.get("/paper/performance")
async def get_performance():
    trades = paper_engine.closed_trades
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
