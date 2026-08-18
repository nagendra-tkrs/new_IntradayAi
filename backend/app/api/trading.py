import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.services.paper_trading import PaperTradingEngine
from app.services.risk_engine import RiskEngine, RiskConfig
from app.services.market_data.provider_factory import get_provider

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
    stop_loss = order.stop_loss or entry_price * 0.98
    target_1 = order.target_1 or entry_price * 1.02
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
