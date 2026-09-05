from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.market_session import now_ist
from app.models.models import User, UserTradeSetup
from app.api.auth import get_current_user
from app.services.trade_setup import compute_risk_reward

router = APIRouter(prefix="/api", tags=["trade-setup"])


class TradeSetupRequest(BaseModel):
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    direction: Optional[str] = None


def _serialize(setup: UserTradeSetup) -> dict:
    return {
        "symbol": setup.symbol,
        "entry": setup.entry,
        "stop_loss": setup.stop_loss,
        "target": setup.target,
        "risk_reward": setup.risk_reward,
        "direction": setup.direction,
        "override_active": setup.override_active,
        "user_setup_updated_at": setup.updated_at.isoformat() if setup.updated_at else None,
    }


async def _get_setup(db: AsyncSession, user_id: str, symbol: str) -> Optional[UserTradeSetup]:
    result = await db.execute(
        select(UserTradeSetup).where(
            UserTradeSetup.user_id == user_id,
            UserTradeSetup.symbol == symbol,
        )
    )
    return result.scalar_one_or_none()


@router.get("/stocks/{symbol}/trade-setup")
async def get_trade_setup(
    symbol: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the authenticated user's saved custom trade setup for a symbol.

    When none exists, returns a null setup (override_active=False) so the frontend
    can fall back to the AI values. One user can only ever see their own setup."""
    setup = await _get_setup(db, user.id, symbol)
    if setup is None:
        return {
            "symbol": symbol,
            "entry": None,
            "stop_loss": None,
            "target": None,
            "risk_reward": None,
            "direction": None,
            "override_active": False,
            "user_setup_updated_at": None,
        }
    return _serialize(setup)


@router.patch("/stocks/{symbol}/trade-setup")
async def update_trade_setup(
    symbol: str,
    payload: TradeSetupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Validate, compute R:R (authoritative server-side) and persist the user's
    custom trade setup as an override. The AI setup is never modified.

    Clearing: sending entry=stop_loss=target=None (or nulls) removes the override."""
    direction = (payload.direction or "LONG").upper()

    if payload.entry is None and payload.stop_loss is None and payload.target is None:
        await db.execute(
            delete(UserTradeSetup).where(
                UserTradeSetup.user_id == user.id,
                UserTradeSetup.symbol == symbol,
            )
        )
        await db.commit()
        return {
            "symbol": symbol,
            "entry": None,
            "stop_loss": None,
            "target": None,
            "risk_reward": None,
            "direction": direction,
            "override_active": False,
            "user_setup_updated_at": None,
        }

    result = compute_risk_reward(payload.entry, payload.stop_loss, payload.target, direction)
    if not result.valid:
        raise HTTPException(status_code=400, detail=result.errors)

    setup = await _get_setup(db, user.id, symbol)
    if setup is None:
        setup = UserTradeSetup(
            user_id=user.id,
            symbol=symbol,
            entry=result.entry,
            stop_loss=result.stop_loss,
            target=result.target,
            risk_reward=result.risk_reward,
            direction=direction,
            override_active=True,
            updated_at=now_ist(),
        )
        db.add(setup)
    else:
        setup.entry = result.entry
        setup.stop_loss = result.stop_loss
        setup.target = result.target
        setup.risk_reward = result.risk_reward
        setup.direction = direction
        setup.override_active = True
        setup.updated_at = now_ist()

    await db.commit()
    await db.refresh(setup)
    return _serialize(setup)


@router.delete("/stocks/{symbol}/trade-setup")
async def clear_trade_setup(
    symbol: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove the user's custom override for a symbol (reset to AI values)."""
    await db.execute(
        delete(UserTradeSetup).where(
            UserTradeSetup.user_id == user.id,
            UserTradeSetup.symbol == symbol,
        )
    )
    await db.commit()
    return {
        "symbol": symbol,
        "entry": None,
        "stop_loss": None,
        "target": None,
        "risk_reward": None,
        "direction": None,
        "override_active": False,
        "user_setup_updated_at": None,
    }


@router.get("/trade-setups")
async def get_all_user_setups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return all of the authenticated user's custom trade setups keyed by symbol.

    This batch endpoint is used by the Scanner page so the frontend can load
    every override in a single request and merge them with scan results
    without N+1 per-symbol calls."""
    result = await db.execute(
        select(UserTradeSetup).where(UserTradeSetup.user_id == user.id)
    )
    setups = result.scalars().all()
    return {
        "setups": {s.symbol: _serialize(s) for s in setups}
    }
