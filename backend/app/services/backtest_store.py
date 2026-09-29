"""Persistence helpers for completed backtests.

Reuses the existing ``backtests`` table / ORM model and the shared async DB
session — no second database system is introduced. The helper mirrors the
project's established additive-migration convention (see
``paper_trading._ensure_state_schema``): later-added columns are applied with
idempotent ``ALTER TABLE`` statements so a pre-existing SQLite database keeps
working without a destructive rebuild.
"""
import json
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Backtest

# Columns added to ``backtests`` after the original table shipped. Applied
# additively and idempotently; existing rows keep working (nullable / defaulted).
_ADDITIVE_COLUMNS = {
    "user_id": "VARCHAR(16)",
    "universe": "VARCHAR(50)",
    "days": "INTEGER",
    "status": "VARCHAR(20)",
}


async def ensure_backtest_schema(db: AsyncSession) -> None:
    """Idempotently add later-added columns to a pre-existing ``backtests`` table.

    A fresh database created by ``Base.metadata.create_all`` already has every
    column, so this is a no-op there. Only SQLite needs the manual ``ALTER``.
    """
    bind = db.get_bind()
    dialect = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect != "sqlite":
        return

    result = await db.execute(text("PRAGMA table_info(backtests)"))
    existing = {row[1] for row in result.fetchall()}
    if not existing:
        # Table not created yet; create_all will build it with the full schema.
        return

    changed = False
    for name, ddl in _ADDITIVE_COLUMNS.items():
        if name not in existing:
            await db.execute(text(f"ALTER TABLE backtests ADD COLUMN {name} {ddl}"))
            changed = True
    if changed:
        await db.commit()


async def save_backtest(
    db: AsyncSession,
    *,
    user_id: Optional[str],
    symbol: str,
    universe: Optional[str],
    strategy_version: str,
    days: int,
    start_date: datetime,
    end_date: datetime,
    initial_capital: float,
    result: dict,
) -> Backtest:
    """Persist a completed backtest and return the stored row.

    The full engine result (metrics + trades + equity curve) is stored in
    ``results_json`` so ``GET`` can return the exact saved payload.
    """
    row = Backtest(
        id=result["id"],
        user_id=user_id,
        symbol=symbol,
        universe=universe,
        strategy=strategy_version,
        days=days,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        final_capital=result.get("final_capital"),
        total_trades=result.get("total_trades", 0),
        win_rate=result.get("win_rate", 0.0),
        profit_factor=result.get("profit_factor", 0.0),
        max_drawdown=result.get("max_drawdown", 0.0),
        sharpe_ratio=result.get("sharpe_ratio", 0.0),
        expectancy=result.get("expectancy", 0.0),
        total_pnl=result.get("total_pnl", 0.0),
        status="completed",
        results_json=json.dumps(result),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def get_owned_backtest(
    db: AsyncSession, backtest_id: str, user_id: str
) -> Optional[Backtest]:
    """Return the backtest only if it exists AND belongs to ``user_id``.

    Returning ``None`` for another user's record (rather than 403) avoids
    disclosing that the record exists.
    """
    row = await db.get(Backtest, backtest_id)
    if row is None or row.user_id != user_id:
        return None
    return row
