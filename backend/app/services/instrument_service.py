"""Instrument sector persistence and backfill.

Provides a safe, best-effort layer that persists resolved company sector data
into the `instruments` table so that:

1. Sectors survive restarts / are not re-fetched on every frontend render.
2. Existing companies with a missing/Unknown sector are backfilled from the
   project's available metadata without overwriting a valid existing sector.
3. A failed external metadata lookup never breaks the company response and never
   wipes a valid stored sector ("Unknown" is never written over a real value).

Priority used when resolving a company's sector (highest first):
  1. Company DB sector (already stored, valid value)
  2. Live metadata provider (yfinance quoteSummary) via SectorResolver
  3. Static NSE sector map (already-available project data)
  4. "Unknown" only if no reliable data exists.

All DB writes here are best-effort: any failure is logged and swallowed so the
caller (provider.get_instruments) is never blocked.
"""

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session
from app.models.models import Instrument
from app.services.market_data.sector_map import NSE_SECTOR_MAP, NSE_NAME_MAP

logger = logging.getLogger(__name__)


def resolve_sector(symbol: str, live_sector: str | None = None) -> str:
    """Resolve a company sector following the priority cascade.

    `live_sector` is the value obtained from the live metadata provider (may be
    None or "Unknown" if unavailable). This function returns the best available
    sector, defaulting only to "Unknown" when no reliable source exists.
    """
    if live_sector and live_sector != "Unknown":
        return live_sector
    key = (symbol or "").upper()
    if key.endswith(".NS"):
        key = key[:-3]
    return NSE_SECTOR_MAP.get(key, "Unknown")


async def upsert_instrument(db: AsyncSession, symbol: str, name: str, sector: str) -> Instrument:
    """Insert or update an instrument row. A valid existing sector is never
    overwritten by "Unknown"; a resolved real sector always updates the row."""
    key = (symbol or "").upper()
    if key.endswith(".NS"):
        key = key[:-3]

    result = await db.execute(select(Instrument).where(Instrument.symbol == key))
    inst = result.scalar_one_or_none()

    if inst is None:
        inst = Instrument(
            symbol=key,
            name=name or NSE_NAME_MAP.get(key, key),
            exchange="NSE",
            sector=sector,
            is_active=True,
            universe="NIFTY50",
        )
        db.add(inst)
        return inst

    if name:
        inst.name = name or inst.name
    # Never downgrade a valid stored sector to "Unknown".
    stored = (inst.sector or "").strip()
    if stored and stored != "Unknown":
        inst.sector = stored
    elif sector and sector != "Unknown":
        inst.sector = sector
    return inst


async def persist_sectors(instruments: list[dict]) -> None:
    """Best-effort upsert of a list of instrument dicts (with 'symbol', 'name',
    'sector') into the instruments table. Never blocks the caller on failure."""
    if not instruments:
        return
    try:
        async with async_session() as db:
            for inst in instruments:
                try:
                    await upsert_instrument(
                        db,
                        inst.get("symbol", ""),
                        inst.get("name", ""),
                        inst.get("sector", "Unknown"),
                    )
                except Exception as e:
                    logger.debug(f"persist instrument failed for {inst.get('symbol')}: {e}")
                    continue
            await db.commit()
    except Exception as e:
        logger.warning(f"instrument persistence failed: {e}")


async def backfill_missing_sectors() -> None:
    """Backfill existing instrument rows whose sector is missing or 'Unknown'.
    Never overwrites a valid stored sector. Best-effort, never blocks startup."""
    try:
        async with async_session() as db:
            result = await db.execute(select(Instrument))
            instruments = result.scalars().all()
            for inst in instruments:
                stored = (inst.sector or "").strip()
                if stored and stored != "Unknown":
                    continue  # keep valid sector
                sector = resolve_sector(inst.symbol, None)
                if sector != "Unknown":
                    inst.sector = sector
            await db.commit()
    except Exception as e:
        logger.warning(f"sector backfill failed: {e}")


async def backfill_known_sectors() -> None:
    """Upsert the static known sectors (from project metadata) so the instruments
    table has a sector for every known NSE symbol even before live enrichment."""
    try:
        async with async_session() as db:
            for symbol, sector in NSE_SECTOR_MAP.items():
                try:
                    await upsert_instrument(
                        db, symbol, NSE_NAME_MAP.get(symbol, symbol), sector
                    )
                except Exception as e:
                    logger.debug(f"backfill known sector failed for {symbol}: {e}")
                    continue
            await db.commit()
    except Exception as e:
        logger.warning(f"known sector backfill failed: {e}")
