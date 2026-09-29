import json
from datetime import datetime
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.services.backtest_store import (
    ensure_backtest_schema,
    get_owned_backtest,
    save_backtest,
)
from app.services.backtesting import BacktestEngine
from app.services.market_data.provider_factory import get_provider
from app.services.strategy_config import STRATEGY_REGISTRY

router = APIRouter(prefix="/api", tags=["backtest"])

# The canonical strategy selector is ``strategy_version``. ``strategy`` is kept
# as a legacy alias for backward compatibility. The pre-fix API defaulted
# ``strategy`` to "multi_factor" but never actually used it; that placeholder is
# treated as "unspecified" so existing clients keep working. It is NOT a
# registered strategy version and any other unknown value is rejected.
_LEGACY_UNSET_STRATEGY = "multi_factor"
_DEFAULT_STRATEGY_VERSION = "v1"

_provider = None


def _get_provider():
    global _provider
    if _provider is None:
        _provider = get_provider()
    return _provider


class BacktestRequest(BaseModel):
    symbol: str
    # Canonical selector (preferred); ``strategy`` accepted as a legacy alias.
    strategy_version: Optional[str] = None
    strategy: Optional[str] = None
    days: int = 30
    initial_capital: float = 1_000_000.0
    brokerage_pct: float = 0.03
    universe: Optional[str] = None


def resolve_strategy_version(req: BacktestRequest) -> str:
    """Normalize a request to a registered strategy version.

    ``strategy_version`` wins over the legacy ``strategy`` alias. An unspecified
    request (or the historical "multi_factor" placeholder) resolves to the
    default. Any other unregistered value is rejected with HTTP 400 — the engine
    never silently falls back to the default.
    """
    requested = req.strategy_version or req.strategy
    if requested is None or requested == _LEGACY_UNSET_STRATEGY:
        return _DEFAULT_STRATEGY_VERSION
    if requested not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {requested}")
    return requested


def _data_range(df: Optional[pd.DataFrame]) -> tuple[datetime, datetime]:
    """Best-effort date range for the persisted record."""
    if df is None or len(df) == 0 or "timestamp" not in df.columns:
        now = datetime.utcnow()
        return now, now
    start = pd.Timestamp(df["timestamp"].iloc[0]).to_pydatetime()
    end = pd.Timestamp(df["timestamp"].iloc[-1]).to_pydatetime()
    return start, end


@router.post("/backtest")
async def run_backtest(
    req: BacktestRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Validate BEFORE doing any work so an unknown strategy fails fast.
    strategy_version = resolve_strategy_version(req)
    try:
        df = await _get_provider().get_ohlcv(req.symbol, days=req.days)
        if df is None or len(df) < 60:
            raise HTTPException(status_code=400, detail="Insufficient historical data")
        engine = BacktestEngine(
            initial_capital=req.initial_capital,
            brokerage_pct=req.brokerage_pct,
            strategy_version=strategy_version,
        )
        result = engine.run(df, req.symbol, strategy=strategy_version)

        await ensure_backtest_schema(db)
        start, end = _data_range(df)
        await save_backtest(
            db,
            user_id=user.id,
            symbol=req.symbol,
            universe=req.universe,
            strategy_version=strategy_version,
            days=req.days,
            start_date=start,
            end_date=end,
            initial_capital=req.initial_capital,
            result=result,
        )
        return result
    except HTTPException:
        raise
    except Exception:
        # Never leak internal details / stack traces to the client.
        raise HTTPException(status_code=500, detail="Backtest execution failed")


@router.get("/backtest/{backtest_id}")
async def get_backtest(
    backtest_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await get_owned_backtest(db, backtest_id, user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    payload = json.loads(row.results_json) if row.results_json else {}
    # Guarantee the persisted identity/strategy are always present.
    payload.setdefault("id", row.id)
    payload.setdefault("strategy", row.strategy)
    payload.setdefault("strategy_version", row.strategy)
    return payload
