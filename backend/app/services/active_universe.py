"""Active live trading universe resolver (Phase 1).

Single backend source of truth for the live scanner's working universe.

Phase 1 contract
================

* The live scanner consumes ``get_active_trading_universe()`` — the 80-stock
  ACTIVE version (30 Core + 50 Rotation) persisted by the existing rotation/
  universe machinery (``universe_versions`` / ``universe_members``). Nothing
  else defines the active universe; the 80 symbols are NOT duplicated in the
  scanner or in any API layer.
* The selection is settings-driven via ``settings.LIVE_UNIVERSE``
  (``"rotation_80"``) and is conceptually separate from
  ``ROTATION_ENGINE_ENABLED`` (calculation/scheduling).
* FAIL CLOSED: if no valid ACTIVE 80/30/50 version exists the resolver raises
  ``ActiveUniverseUnavailable`` with a structured
  ``ACTIVE_UNIVERSE_UNAVAILABLE`` payload — it never silently falls back to the
  legacy 40-stock NIFTY50 universe.

No rotation-engine logic lives here; the engine is only invoked explicitly by
``scripts/activate_live_universe.py`` (no scheduler, no startup hook).
"""
from __future__ import annotations

import logging
from typing import Optional

from app.core.config import settings
from app.models.universe_models import UniverseCategory
from app.services import rotation_eligibility as elig
from app.services.market_data.sector_map import get_name, get_sector
from app.services.provider_aliases import provider_symbol
from app.services.universe_state_service import UniverseStateService

logger = logging.getLogger(__name__)

ACTIVE_UNIVERSE_UNAVAILABLE = "ACTIVE_UNIVERSE_UNAVAILABLE"

# Request values that mean "the live/active universe" (settings-driven).
_ACTIVE_REQUEST_KEYS = frozenset({"", "active", "rotation_80"})

# Expected shape of the live universe (fixed architecture, Phase 2 §4).
TARGET_TOTAL = 80
TARGET_CORE = 30
TARGET_ROTATION = 50


def is_active_universe_request(universe: Optional[str]) -> bool:
    """True when a scanner/API ``universe`` parameter refers to the active
    live universe (default / sentinel / configured ``LIVE_UNIVERSE``), rather
    than an explicit legacy universe such as ``NIFTY50``."""
    key = (universe or "").strip().lower()
    if key in _ACTIVE_REQUEST_KEYS:
        return True
    configured = (settings.LIVE_UNIVERSE or "").strip().lower()
    return bool(configured) and key == configured


class ActiveUniverseUnavailable(Exception):
    """Raised when the configured live universe is not available (FAIL CLOSED).

    ``payload`` is the structured ``ACTIVE_UNIVERSE_UNAVAILABLE`` body served by
    the API layer (HTTP 503) so callers never silently scan the wrong universe.
    """

    def __init__(self, reason: str, checks: Optional[dict] = None):
        self.reason = reason
        self.checks = checks or {}
        self.payload = {
            "status": ACTIVE_UNIVERSE_UNAVAILABLE,
            "reason": reason,
            "checks": self.checks,
        }
        super().__init__(reason)


async def get_active_trading_universe(
    *,
    state_service: Optional[UniverseStateService] = None,
    enrich: bool = True,
) -> dict:
    """Resolve + validate the ACTIVE 80-stock trading universe.

    Returns::

        {
            "status": "OK",
            "source": settings.LIVE_UNIVERSE,
            "universe_version_id": ...,
            "universe_version": ...,
            "total": 80, "core": 30, "rotation": 50,
            "duplicate_symbols": [], "invalid_symbols": [],
            "stocks": [
                {symbol, name, sector, exchange, classification,
                 rank, rotation_score, universe, yfinance_symbol}, ...
            ],
        }

    Raises:
        ActiveUniverseUnavailable: no ACTIVE version or the version violates
        the hard shape gate (total 80 / core 30 / rotation 50 / no duplicates /
        no CORE-ROTATION overlap / valid NSE symbols).
    """
    service = state_service if state_service is not None else UniverseStateService()
    active = await service.get_active_universe(with_detail=True)
    if active is None:
        raise ActiveUniverseUnavailable(
            "no ACTIVE universe version exists in universe_versions; run the "
            "explicit activation script (scripts/activate_live_universe.py)",
            {"active_exists": False},
        )

    members = active.get("members") or []
    scores = {s.get("symbol"): s for s in (active.get("scores") or [])}

    symbols = [m.get("symbol", "") for m in members]
    core = [m for m in members if m.get("category") == UniverseCategory.CORE]
    rotation = [m for m in members if m.get("category") == UniverseCategory.ROTATION]
    invalid_categories = sorted(
        {m.get("symbol") for m in members if m.get("category") not in UniverseCategory.VALID}
    )
    duplicates = sorted({s for s in symbols if symbols.count(s) > 1})
    core_set = {m.get("symbol") for m in core}
    rotation_set = {m.get("symbol") for m in rotation}
    overlap = sorted(core_set & rotation_set)
    invalid_symbols = sorted({s for s in symbols if s and not elig.is_valid_symbol(s)})

    checks = {
        "active_exists": True,
        "total": len(members),
        "core": len(core),
        "rotation": len(rotation),
        "duplicate_symbols": duplicates,
        "overlap": overlap,
        "invalid_symbols": invalid_symbols,
        "invalid_categories": invalid_categories,
    }

    problems: list[str] = []
    if invalid_categories:
        problems.append(f"invalid categories for {invalid_categories}")
    if len(members) != TARGET_TOTAL:
        problems.append(f"total != {TARGET_TOTAL} ({len(members)})")
    if len(core) != TARGET_CORE:
        problems.append(f"core != {TARGET_CORE} ({len(core)})")
    if len(rotation) != TARGET_ROTATION:
        problems.append(f"rotation != {TARGET_ROTATION} ({len(rotation)})")
    if duplicates:
        problems.append(f"duplicate symbols: {duplicates}")
    if overlap:
        problems.append(f"CORE/ROTATION overlap: {overlap}")
    if invalid_symbols:
        problems.append(f"invalid symbols: {invalid_symbols}")
    if problems:
        raise ActiveUniverseUnavailable(
            "ACTIVE universe version fails the 80/30/50 shape gate: "
            + "; ".join(problems),
            checks,
        )

    names: dict = {}
    if enrich:
        names = await _load_display_names(symbols)

    stocks: list[dict] = []
    ordered = sorted(
        members,
        key=lambda m: (
            0 if m.get("category") == UniverseCategory.CORE else 1,
            m.get("rank") or 0,
            m.get("symbol", ""),
        ),
    )
    for member in ordered:
        sym = member.get("symbol")
        score_row = scores.get(sym) or {}
        sector = get_sector(sym)
        stocks.append(
            {
                "symbol": sym,
                "name": names.get(sym) or get_name(sym) or sym,
                "sector": sector,
                "exchange": "NSE",
                "classification": member.get("category"),
                "rank": member.get("rank"),
                "rotation_score": score_row.get("total_rotation_score"),
                "universe": "rotation_80",
                "yfinance_symbol": f"{provider_symbol(sym)}.NS",
            }
        )

    return {
        "status": "OK",
        "source": settings.LIVE_UNIVERSE,
        "universe_version_id": active.get("id"),
        "universe_version": active.get("version"),
        "total": TARGET_TOTAL,
        "core": TARGET_CORE,
        "rotation": TARGET_ROTATION,
        "duplicate_symbols": [],
        "invalid_symbols": [],
        "stocks": stocks,
    }


async def _load_display_names(symbols: list[str]) -> dict:
    """Best-effort symbol -> display name from the ``instruments`` table.
    Any failure returns {} — callers fall back to the static sector map."""
    if not symbols:
        return {}
    try:
        from sqlalchemy import select

        from app.core.database import async_session
        from app.models.models import Instrument

        async with async_session() as db:
            rows = (
                await db.execute(
                    select(Instrument.symbol, Instrument.name).where(
                        Instrument.symbol.in_(symbols)
                    )
                )
            ).all()
        return {str(sym): str(name) for sym, name in rows if name}
    except Exception as exc:  # enrichment is best-effort, never blocks resolution
        logger.warning("active-universe display-name enrichment failed: %s", exc)
        return {}