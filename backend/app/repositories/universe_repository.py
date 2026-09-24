"""Session-scoped persistence operations for the Universe Manager (Phase 3).

Pure persistence layer: every function here executes SQL against the SAME
SQLite backend used by the rest of the app (``app.core.database.async_session``
/ existing ``Base``). It never commits and never opens its own transaction —
the state service owns the explicit transaction boundaries (Q12 ss14) and calls
these functions inside ``async with session.begin()`` blocks.

Guarded transitions (Q12 ss7 / Q13 ss6): status changes are executed as
conditional UPDATEs (``UPDATE ... SET status=:to WHERE id=:id AND status=:from``)
and return the affected-row count, so a competing or erroneous transition
affects zero rows and the invariant "target state never silently changes" holds
by construction. Exactly-one-ACTIVE is additionally guaranteed at the DB level
by a SQLite partial unique index (``universe_versions``, ss13).

Immutability (Q12 ss10): no UPDATE path exists here for membership, scores,
eligibility or event rows — corrections are a new DRAFT, never a rewrite.

Referential integrity is enforced at the application layer (SQLite FK
enforcement is off in this repo); every FK-valued insert goes through a lookup
helper here.
"""
from __future__ import annotations

import json
from typing import Any, Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.universe_models import (
    ActorType,
    CandidatePoolStatus,
    CandidatePoolVersion,
    UniverseEligibility,
    UniverseEvent,
    UniverseEventType,
    UniverseMembership,
    UniverseRotationScore,
    UniverseStatus,
    UniverseVersion,
)


class UniverseError(Exception):
    """Base class for all Universe Manager persistence/state errors."""


class UniverseVersionNotFound(UniverseError):
    def __init__(self, version_id: str):
        self.version_id = version_id
        super().__init__(f"Universe version not found: {version_id}")


class CandidatePoolVersionNotFound(UniverseError):
    def __init__(self, pool_id: str):
        self.pool_id = pool_id
        super().__init__(f"Candidate pool version not found: {pool_id}")


class CandidatePoolError(UniverseError):
    """A candidate-pool operation violated a pool-level rule."""


class UniverseStateTransitionError(UniverseError):
    """A guarded state transition was refused (wrong current status)."""


class UniverseValidationError(UniverseError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("Universe validation failed: " + "; ".join(self.errors))


class UniverseMarketHoursError(UniverseError):
    """Activation/rollback attempted outside the permitted activation window."""


# ---------------------------------------------------------------------------
# Candidate pool
# ---------------------------------------------------------------------------


async def insert_candidate_pool(
    session: AsyncSession,
    *,
    version: str,
    source: str = "local",
    source_reference: Optional[str] = None,
    symbol_count: int = 0,
    metadata_json: Optional[str] = None,
    actor: str = "SYSTEM",
    actor_type: str = ActorType.SYSTEM,
    reason: Optional[str] = None,
) -> CandidatePoolVersion:
    """Insert a new candidate-pool version (DRAFT) + ``CANDIDATE_POOL_CREATED`` event."""
    pool = CandidatePoolVersion(
        version=version,
        source=source or "local",
        source_reference=source_reference,
        status=CandidatePoolStatus.DRAFT,
        symbol_count=symbol_count,
        metadata_json=metadata_json,
    )
    session.add(pool)
    await session.flush()
    session.add(
        UniverseEvent(
            event_type=UniverseEventType.CANDIDATE_POOL_CREATED,
            actor=actor,
            actor_type=actor_type,
            candidate_pool_version_id=pool.id,
            result_status=CandidatePoolStatus.DRAFT,
            reason=reason,
            metadata_json=metadata_json,
        )
    )
    return pool


async def get_candidate_pool(
    session: AsyncSession, pool_id: str
) -> Optional[CandidatePoolVersion]:
    return await session.get(CandidatePoolVersion, pool_id)


async def get_candidate_pool_by_version(
    session: AsyncSession, version: str, source: str
) -> Optional[CandidatePoolVersion]:
    """Phase 4 (ADDITIVE read): find a pool by its unique (source, version) key
    — lets the Rotation Engine reuse a previously created pool instead of
    colliding on the UNIQUE(source, version) constraint."""
    from sqlalchemy import select

    result = await session.execute(
        select(CandidatePoolVersion).where(
            CandidatePoolVersion.source == source,
            CandidatePoolVersion.version == version,
        )
    )
    return result.scalars().first()


async def require_candidate_pool(
    session: AsyncSession, pool_id
) -> CandidatePoolVersion:
    pool = await get_candidate_pool(session, pool_id)
    if pool is None:
        raise CandidatePoolVersionNotFound(pool_id)
    return pool


async def guarded_pool_status_update(
    session: AsyncSession,
    pool_id: str,
    *,
    from_status: str,
    to_status: str,
    **fields: Any,
) -> int:
    """Conditional pool status UPDATE; returns affected-row count (0 = conflict)."""
    fields = dict(fields)
    fields["status"] = to_status
    result = await session.execute(
        update(CandidatePoolVersion)
        .where(
            CandidatePoolVersion.id == pool_id,
            CandidatePoolVersion.status == from_status,
        )
        .values(**fields)
    )
    return result.rowcount or 0


async def count_pools_by_status(
    session: AsyncSession, status: str
) -> int:
    result = await session.execute(
        select(func.count()).select_from(CandidatePoolVersion).where(
            CandidatePoolVersion.status == status
        )
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Universe versions + memberships + scores + eligibility (one draft snapshot)
# ---------------------------------------------------------------------------


async def create_universe_draft(
    session: AsyncSession,
    *,
    version: str,
    candidate_pool_version_id: str,
    members: Sequence[dict[str, Any]],
    core_count: int,
    rotation_count: int,
    total_count: int,
    calculated_at,
    effective_from,
    config_hash: Optional[str] = None,
    actor: str = "SYSTEM",
    actor_type: str = ActorType.SYSTEM,
    reason: Optional[str] = None,
    slot_scores: Optional[dict[str, dict[str, Any]]] = None,
    slot_eligibility: Optional[dict[str, dict[str, Any]]] = None,
) -> UniverseVersion:
    """Insert one complete DRAFT snapshot in a single transaction.

    ``members``          : list of dicts ``{symbol, category, rank,
                           rotation_score?, selection_reason?, previous_*?}``.
    ``slot_scores``      : ``{symbol: {liquidity_score, volume_quality_score,
                           volatility_score, signal_frequency_score,
                           long_quality_score, short_quality_score,
                           data_quality_score, total_rotation_score}}`` (optional
                           — validation checks presence; incomplete drafts are
                           PERMITTED here so ``validate_draft`` can reject them).
    ``slot_eligibility`` : ``{symbol: {eligibility_status, tradability, ...}}``
                           (optional — same rationale).

    This helper never commits: the caller's `session.begin()` wraps the version
    row + every member/score/eligibility row + the draft event into ONE commit
    (Q12 ss14 — a partial draft must never exist).
    """
    uv = UniverseVersion(
        version=version,
        candidate_pool_version_id=candidate_pool_version_id,
        status=UniverseStatus.DRAFT,
        total_count=total_count,
        core_count=core_count,
        rotation_count=rotation_count,
        calculated_at=calculated_at,
        effective_from=effective_from,
        config_hash=config_hash,
    )
    session.add(uv)
    await session.flush()

    for m in members:
        session.add(
            UniverseMembership(
                universe_version_id=uv.id,
                symbol=m["symbol"],
                category=m["category"],
                rank=m["rank"],
                rotation_score=m.get("rotation_score"),
                selection_reason=m.get("selection_reason"),
                previous_category=m.get("previous_category"),
                previous_rank=m.get("previous_rank"),
                previous_score=m.get("previous_score"),
                retention_reason=m.get("retention_reason"),
                replacement_reason=m.get("replacement_reason"),
            )
        )
        if slot_scores and m["symbol"] in slot_scores:
            s = slot_scores[m["symbol"]]
            session.add(
                UniverseRotationScore(
                    universe_version_id=uv.id,
                    symbol=m["symbol"],
                    liquidity_score=s.get("liquidity_score"),
                    volume_quality_score=s.get("volume_quality_score"),
                    volatility_score=s.get("volatility_score"),
                    signal_frequency_score=s.get("signal_frequency_score"),
                    long_quality_score=s.get("long_quality_score"),
                    short_quality_score=s.get("short_quality_score"),
                    data_quality_score=s.get("data_quality_score"),
                    total_rotation_score=s.get("total_rotation_score"),
                )
            )
        if slot_eligibility and m["symbol"] in slot_eligibility:
            e = slot_eligibility[m["symbol"]]
            session.add(
                UniverseEligibility(
                    universe_version_id=uv.id,
                    symbol=m["symbol"],
                    eligibility_status=e.get("eligibility_status", "ELIGIBLE"),
                    tradability=e.get("tradability"),
                    liquidity=e.get("liquidity"),
                    volume=e.get("volume"),
                    volatility=e.get("volatility"),
                    data_quality=e.get("data_quality"),
                    signal_quality=e.get("signal_quality"),
                    failure_reason=e.get("failure_reason"),
                    data_quality_state=e.get("data_quality_state"),
                )
            )

    # Phase 4 (ADDITIVE — backward compatible): persist an eligibility row for
    # every evaluated candidate that was NOT selected (ineligible / insufficient
    # data), so the version-scoped eligibility ledger explains every candidate's
    # outcome, not just the selected 80. Phase 3 callers pass slot_eligibility
    # only for member symbols, in which case this loop inserts nothing extra.
    member_symbols = {m["symbol"] for m in members}
    if slot_eligibility:
        for sym, e in slot_eligibility.items():
            if sym in member_symbols:
                continue
            session.add(
                UniverseEligibility(
                    universe_version_id=uv.id,
                    symbol=sym,
                    eligibility_status=e.get("eligibility_status", "ELIGIBLE"),
                    tradability=e.get("tradability"),
                    liquidity=e.get("liquidity"),
                    volume=e.get("volume"),
                    volatility=e.get("volatility"),
                    data_quality=e.get("data_quality"),
                    signal_quality=e.get("signal_quality"),
                    failure_reason=e.get("failure_reason"),
                    data_quality_state=e.get("data_quality_state"),
                )
            )

    session.add(
        UniverseEvent(
            event_type=UniverseEventType.UNIVERSE_DRAFT_CREATED,
            actor=actor,
            actor_type=actor_type,
            universe_version_id=uv.id,
            candidate_pool_version_id=candidate_pool_version_id,
            result_status=UniverseStatus.DRAFT,
            reason=reason,
        )
    )
    return uv


async def get_universe_version(
    session: AsyncSession, version_id: str
) -> Optional[UniverseVersion]:
    return await session.get(UniverseVersion, version_id)


async def require_universe_version(session: AsyncSession, version_id) -> UniverseVersion:
    uv = await get_universe_version(session, version_id)
    if uv is None:
        raise UniverseVersionNotFound(version_id)
    return uv


async def guarded_universe_status_update(
    session: AsyncSession,
    version_id: str,
    *,
    from_status: str,
    to_status: str,
    **fields: Any,
) -> int:
    """Conditional universe-version status UPDATE; 0 rows returned = conflict."""
    fields = dict(fields)
    fields["status"] = to_status
    result = await session.execute(
        update(UniverseVersion)
        .where(
            UniverseVersion.id == version_id,
            UniverseVersion.status == from_status,
        )
        .values(**fields)
    )
    return result.rowcount or 0


async def get_active_universe(session: AsyncSession) -> Optional[UniverseVersion]:
    result = await session.execute(
        select(UniverseVersion).where(UniverseVersion.status == UniverseStatus.ACTIVE).limit(2)
    )
    rows = result.scalars().all()
    if not rows:
        return None
    return rows[0]


async def list_universe_versions(
    session: AsyncSession, limit: Optional[int] = None
) -> Sequence[UniverseVersion]:
    q = select(UniverseVersion).order_by(UniverseVersion.created_at.desc())
    if limit:
        q = q.limit(limit)
    return (await session.execute(q)).scalars().all()


async def get_members(session: AsyncSession, version_id: str) -> Sequence[UniverseMembership]:
    result = await session.execute(
        select(UniverseMembership)
        .where(UniverseMembership.universe_version_id == version_id)
        .order_by(UniverseMembership.category, UniverseMembership.rank)
    )
    return result.scalars().all()


async def get_scores(session: AsyncSession, version_id: str) -> Sequence[UniverseRotationScore]:
    result = await session.execute(
        select(UniverseRotationScore).where(
            UniverseRotationScore.universe_version_id == version_id
        )
    )
    return result.scalars().all()


async def get_eligibility(
    session: AsyncSession, version_id: str
) -> Sequence[UniverseEligibility]:
    result = await session.execute(
        select(UniverseEligibility).where(
            UniverseEligibility.universe_version_id == version_id
        )
    )
    return result.scalars().all()


# ---------------------------------------------------------------------------
# Append-only events (Q13 ss16)
# ---------------------------------------------------------------------------


async def insert_event(
    session: AsyncSession,
    *,
    event_type: str,
    actor: str = "SYSTEM",
    actor_type: str = ActorType.SYSTEM,
    universe_version_id: Optional[str] = None,
    candidate_pool_version_id: Optional[str] = None,
    previous_active_universe_id: Optional[str] = None,
    new_active_universe_id: Optional[str] = None,
    reason: Optional[str] = None,
    result_status: Optional[str] = None,
    metadata_json: Optional[str] = None,
) -> UniverseEvent:
    event = UniverseEvent(
        event_type=event_type,
        actor=actor,
        actor_type=actor_type,
        universe_version_id=universe_version_id,
        candidate_pool_version_id=candidate_pool_version_id,
        previous_active_universe_id=previous_active_universe_id,
        new_active_universe_id=new_active_universe_id,
        reason=reason,
        result_status=result_status,
        metadata_json=metadata_json,
    )
    session.add(event)
    return event


async def list_events(
    session: AsyncSession,
    *,
    universe_version_id: Optional[str] = None,
    limit: int = 200,
) -> Sequence[UniverseEvent]:
    q = select(UniverseEvent).order_by(UniverseEvent.created_at, UniverseEvent.id)
    if universe_version_id:
        q = q.where(UniverseEvent.universe_version_id == universe_version_id)
    return (await session.execute(q.limit(limit))).scalars().all()


def encode_metadata(payload: Optional[dict]) -> Optional[str]:
    """Serialize a dict to the JSON ``*_json`` column convention (None-safe)."""
    if payload is None:
        return None
    return json.dumps(payload, sort_keys=True, default=str)