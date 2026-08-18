from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.services.market_data.provider_factory import get_provider
from app.services.backtesting import BacktestEngine

router = APIRouter(prefix="/api", tags=["backtest"])

_provider = None

def _get_provider():
    global _provider
    if _provider is None:
        _provider = get_provider()
    return _provider


class BacktestRequest(BaseModel):
    symbol: str
    strategy: str = "multi_factor"
    days: int = 30
    initial_capital: float = 1_000_000.0
    brokerage_pct: float = 0.03


@router.post("/backtest")
async def run_backtest(req: BacktestRequest):
    try:
        df = await _get_provider().get_ohlcv(req.symbol, days=req.days)
        if df is None or len(df) < 60:
            raise HTTPException(status_code=400, detail="Insufficient historical data")
        engine = BacktestEngine(
            initial_capital=req.initial_capital,
            brokerage_pct=req.brokerage_pct,
        )
        result = engine.run(df, req.symbol, strategy=req.strategy)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/backtest/{backtest_id}")
async def get_backtest(backtest_id: str):
    return {"error": "Backtest history not yet implemented"}
