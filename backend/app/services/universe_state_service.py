"""Universe Manager state service (Phase 3 — persistence layer only).

Implements the Q12/Q13-approved lifecycle for candidate pools and universe
versions WITHOUT changing production behavior:

* the legacy scanner keeps running off ``UNIVERSE_SOURCE=legacy`` (default);
* nothing here activates anything on its own (no scheduler, no auto-activation);
* the Rotation Engine / scoring / candidate ranking are NOT implemented — this
  service only persists and guards computed results;
* no approval role exists in the repo (VERIFIED) — approval is recorded as a
  manual, audited decision (actor = human id); the SYSTEM auto-approval path is
  intentionally NOT implemented (Q13 ss5/ss8) and stays behind settings.

Lifecycle (Q13 ss6)::

    DRAFT --validate--> VALIDATED --approve--> APPROVED --activate--> ACTIVE
      |        (gate PASS)                                   (atomic)   |
      +--validate--> REJECTED  <-------------APPROVED--reject (abort)   +-> SUPERSEDED
          (any gate FAIL)                                      (successor activates)

Guarded transitions: every status change is a conditional UPDATE executed with
``WHERE status = <expected>``; a 0-row result means the target state never
silently changes (Q12 ss7). Exactly one ACTIVE is guaranteed by a DB partial
unique index + the atomic flip (Q12 ss8).

Transaction boundaries: each mutating operation runs on ONE session and commits
explicitly at the service boundary (Q12 ss14); any failure rolls back the whole
unit, so KEEP CURRENT ACTIVE (Q13 ss18) holds and events stay append-only.

Central invariant (Q13 ss18): an invalid draft can NEVER become ACTIVE —
ACTIVE is reachable only from APPROVED, which is reachable only from VALIDATED.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import market_session
from app.core.database import async_session
from app.models.universe_models import (
    ActorType,
    CandidatePoolStatus,
    EligibilityResultStatus,
    ROTATION_SCORE_COMPONENT_WEIGHTS,
    UniverseCategory,
    UniverseEventType,
    UniverseStatus,
)
from app.repositories import universe_repository as repo

# NSE base-symbol pattern (post-normalization, Phase 2 ss4.5). Lenient: letters,
# digits, hyphen, ampersand, dot, space — e.g. "TCS", "BAJAJ-AUTO", "M&M".
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9\-&.\s]{0,49}$")

# Exclusion list: no authoritative list exists in the repo today.
# [TO BE CALIBRATED — Phase 2 ss13.4 "not in EXCLUDED_SYMBOLS".]
EXCLUDED_SYMBOLS: frozenset[str] = frozenset()

# Validation-schema version stamped into validation results for reproducibility.
_VALIDATION_SCHEMA = "phase3.v1"

# Approval-policy version recorded on every approval (Q13 ss8 evidence block).
_APPROVAL_POLICY_VERSION = "q13-hybrid.v1"


def _utcnow() -> datetime:
    """Naive-UTC infra timestamp — matches the existing ``datetime.utcnow``
    column-default convention in this repo (models.py)."""
    return datetime.utcnow()


def _as_ist(dt: datetime) -> datetime:
    """Normalize naive -> aware IST (market_session convention)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=market_session.IST)
    return dt


class UniverseStateService:
    """State machine + transaction boundary for the Universe Manager.

    ``session_factory`` — optional callable returning an ``AsyncSession``. When
    omitted the module-level ``async_session`` (existing SQLite engine) is used.
    Tests inject a factory bound to a temp-file SQLite engine.
    """

    def __init__(self, session_factory: Optional[Callable[[], AsyncSession]] = None):
        self._session_factory = session_factory or async_session

    # ------------------------------------------------------------------ utils
    def _session(self) -> AsyncSession:
        return self._session_factory()

    def _is_confirmed_trading_day(self, dt: datetime) -> bool:
        """True only when the NSE holiday calendar can confirm a trading day.

        Phase 5C: single authoritative definition via
        ``market_session.is_confirmed_trading_day`` — the calendar source is
        the official NSE holiday-master (CM segment). Fail-safe horizon
        preserved: years outside the installed calendar are unverifiable,
        hence never reported as a trading day.
        """
        return market_session.is_confirmed_trading_day(dt.date())

    def _guard_activation_window(self, as_of: Optional[datetime]) -> datetime:
        """Refuse activation/rollback outside the activation window (Q13 ss10).

        Window = NOT ``is_market_hours`` AND a confirmed trading day (pre-open /
        post-close). The exact schedule times remain TO BE CALIBRATED — the
        conservative rule reuses the repo's fail-safe calendar primitives only.
        ``as_of`` lets tests drive the guard deterministically; the real clock
        is used otherwise.
        """
        dt = _as_ist(as_of) if as_of is not None else market_session.now_ist()
        if market_session.is_market_hours(dt):
            raise repo.UniverseMarketHoursError(
                "Activation/rollback refused during market hours "
                f"(09:15-15:30 IST) at {dt.isoformat()}"
            )
        if not self._is_confirmed_trading_day(dt):
            raise repo.UniverseMarketHoursError(
                "Activation/rollback only allowed on a confirmed TRADING_DAY "
                f"outside market hours; refused at {dt.isoformat()} "
                "[TO BE CALIBRATED: ACTIVATION_SCHEDULE]"
            )
        return dt

    # ------------------------------------------------------------------ pool
    async def create_candidate_pool(
        self,
        version: str,
        *,
        source: str = "local",
        source_reference: Optional[str] = None,
        symbols: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
        actor: str = "SYSTEM",
        reason: Optional[str] = None,
    ) -> dict:
        """Create a candidate-pool version (DRAFT) + event.

        Q10 HYBRID: this is the local-validated side of the pool — an external
        refresh is NOT executed here (external refresh engine is out of Phase 3
        scope); ``source``/``source_reference`` record provenance only.
        """
        symbol_count = len(symbols) if symbols else 0
        async with self._session() as session:
            pool = await repo.insert_candidate_pool(
                session,
                version=version,
                source=source,
                source_reference=source_reference,
                symbol_count=symbol_count,
                metadata_json=repo.encode_metadata(metadata),
                actor=actor,
                reason=reason,
            )
            await session.commit()
            pool_id = pool.id
        return await self.get_candidate_pool(pool_id)

    async def get_candidate_pool(self, pool_id: str) -> Optional[dict]:
        async with self._session() as session:
            pool = await repo.get_candidate_pool(session, pool_id)
            return pool and _pool_to_dict(pool)

    async def get_candidate_pool_by_version(
        self, version: str, source: str = "local"
    ) -> Optional[dict]:
        """Phase 4 (ADDITIVE read): pool by its unique (source, version) key —
        used by the Rotation Engine for idempotent local-pool resolution."""
        async with self._session() as session:
            pool = await repo.get_candidate_pool_by_version(session, version, source)
            return pool and _pool_to_dict(pool)

    async def record_candidate_pool_refresh_failure(
        self,
        *,
        source: str,
        error: str,
        previous_pool_version: Optional[str] = None,
        actor: str = "SYSTEM",
    ) -> dict:
        """Phase 5B §12 — safe-refresh record (ADDITIVE, append-only event).

        A failed candidate-pool refresh NEVER empties, truncates, or overwrites
        the previously validated pool (Q10 retain-on-failure rule — the repo
        exposes no pool-delete/overwrite path). This method ONLY appends a
        ``CANDIDATE_POOL_REFRESH_FAILED`` event so the failure is auditably
        surfaced with its source, error, and the previous pool version it leaves
        intact.
        """
        async with self._session() as session:
            await repo.insert_event(
                session,
                event_type=UniverseEventType.CANDIDATE_POOL_REFRESH_FAILED,
                actor=actor,
                result_status="FAILED",
                reason=error or "candidate pool refresh failed",
                metadata_json=repo.encode_metadata(
                    {
                        "source": source,
                        "previous_pool_version": previous_pool_version,
                        "timestamp": _utcnow().isoformat(),
                    }
                ),
            )
            await session.commit()
        return {
            "recorded": True,
            "event_type": UniverseEventType.CANDIDATE_POOL_REFRESH_FAILED,
            "source": source,
            "previous_pool_version": previous_pool_version,
        }

    async def validate_candidate_pool(
        self, pool_id: str, *, actor: str = "SYSTEM", reason: Optional[str] = None
    ) -> dict:
        """DRAFT -> VALIDATED pool (idempotent when already VALIDATED)."""
        async with self._session() as session:
            pool = await repo.require_candidate_pool(session, pool_id)
            if pool.status == CandidatePoolStatus.VALIDATED:
                return _pool_to_dict(pool)
            if pool.status != CandidatePoolStatus.DRAFT:
                raise repo.UniverseStateTransitionError(
                    f"Candidate pool {pool_id} status {pool.status} -> "
                    "VALIDATED is not permitted"
                )
            rowcount = await repo.guarded_pool_status_update(
                session,
                pool_id,
                from_status=CandidatePoolStatus.DRAFT,
                to_status=CandidatePoolStatus.VALIDATED,
                validated_at=_utcnow(),
                validation_status="PASS",
            )
            if rowcount != 1:  # concurrent change — nothing written
                raise repo.UniverseStateTransitionError(
                    f"Candidate pool {pool_id} could not be validated "
                    "(concurrent status change)"
                )
            await repo.insert_event(
                session,
                event_type=UniverseEventType.CANDIDATE_POOL_VALIDATED,
                actor=actor,
                candidate_pool_version_id=pool_id,
                result_status=CandidatePoolStatus.VALIDATED,
                reason=reason,
            )
            await session.commit()
        return await self.get_candidate_pool(pool_id)

    async def reject_candidate_pool(
        self, pool_id: str, *, actor: str = "SYSTEM", reason: Optional[str] = None
    ) -> dict:
        """DRAFT -> REJECTED pool (terminal; a new cycle creates a new version)."""
        async with self._session() as session:
            pool = await repo.require_candidate_pool(session, pool_id)
            if pool.status != CandidatePoolStatus.DRAFT:
                raise repo.UniverseStateTransitionError(
                    f"Candidate pool {pool_id} status {pool.status} -> "
                    "REJECTED is not permitted"
                )
            rowcount = await repo.guarded_pool_status_update(
                session,
                pool_id,
                from_status=CandidatePoolStatus.DRAFT,
                to_status=CandidatePoolStatus.REJECTED,
                validation_status="FAIL",
                error_info=reason or "pool rejected",
            )
            if rowcount != 1:
                raise repo.UniverseStateTransitionError(
                    f"Candidate pool {pool_id} could not be rejected "
                    "(concurrent status change)"
                )
            await repo.insert_event(
                session,
                event_type=UniverseEventType.CANDIDATE_POOL_REJECTED,
                actor=actor,
                candidate_pool_version_id=pool_id,
                result_status=CandidatePoolStatus.REJECTED,
                reason=reason,
            )
            await session.commit()
        return await self.get_candidate_pool(pool_id)

    async def record_pool_refresh_failed(
        self,
        *,
        previous_pool_id: Optional[str] = None,
        source: str = "external_refresh",
        reason: str = "refresh failed",
        actor: str = "SYSTEM",
    ) -> dict:
        """Record a failed external refresh WITHOUT touching pool data.

        Q10 retain-on-failure: the previous validated pool is never erased,
        emptied or silently changed — this simply appends the
        ``CANDIDATE_POOL_REFRESH_FAILED`` audit event, so any DRAFT calculated
        on the retained pool keeps referencing the retained VALIDATED pool.
        """
        async with self._session() as session:
            if previous_pool_id:
                await repo.require_candidate_pool(session, previous_pool_id)
            event = await repo.insert_event(
                session,
                event_type=UniverseEventType.CANDIDATE_POOL_REFRESH_FAILED,
                actor=actor,
                candidate_pool_version_id=previous_pool_id,
                reason=reason,
                result_status="RETAINED_PREVIOUS",
                metadata_json=repo.encode_metadata(
                    {"source": source, "refresh_status": "FAILED"}
                ),
            )
            await session.commit()
            event_id = event.id
        return {"event_id": event_id, "retained_pool_id": previous_pool_id}

    # ------------------------------------------------------------- draft / gate
    async def create_universe_draft(
        self,
        pool_id: str,
        *,
        version: str,
        members: list[dict[str, Any]],
        core_count: int,
        rotation_count: int,
        total_count: int,
        calculated_at=None,
        effective_from=None,
        config_hash: Optional[str] = None,
        slot_scores: Optional[dict[str, dict[str, Any]]] = None,
        slot_eligibility: Optional[dict[str, dict[str, Any]]] = None,
        actor: str = "SYSTEM",
        reason: Optional[str] = None,
    ) -> dict:
        """Persist one complete DRAFT snapshot in ONE transaction (Q12 ss14).

        The candidate pool must EXIST (provenance link); pool status is enforced
        by the validation gate (Q13 ss7/ss14), NOT here — so an unvalidated-pool
        draft can be created and then correctly rejected by ``validate_draft``.
        """
        async with self._session() as session:
            await repo.require_candidate_pool(session, pool_id)
            uv = await repo.create_universe_draft(
                session,
                version=version,
                candidate_pool_version_id=pool_id,
                members=members,
                core_count=core_count,
                rotation_count=rotation_count,
                total_count=total_count,
                calculated_at=calculated_at,
                effective_from=effective_from,
                config_hash=config_hash,
                actor=actor,
                reason=reason,
                slot_scores=slot_scores,
                slot_eligibility=slot_eligibility,
            )
            await session.commit()
            version_id = uv.id
        return await self.get_universe_version(version_id)

    async def validate_universe(
        self, version_id: str, *, actor: str = "SYSTEM"
    ) -> dict:
        """Apply the ss7 validation gate: DRAFT -> VALIDATED or DRAFT -> REJECTED.

        Idempotent on an already-VALIDATED version (same verdict). Any failure
        enumerates ALL errors and moves the draft to REJECTED — the current
        ACTIVE universe is never touched (Q13 ss7 failing-safe). Raises
        ``UniverseValidationError`` with the full error list on gate failure.
        """
        async with self._session() as session:
            uv = await repo.require_universe_version(session, version_id)
            if uv.status == UniverseStatus.VALIDATED:
                return _version_to_dict(uv)
            if uv.status != UniverseStatus.DRAFT:
                raise repo.UniverseStateTransitionError(
                    f"Universe {version_id} status {uv.status} cannot be "
                    "validated (only DRAFT)"
                )
            members = await repo.get_members(session, version_id)
            scores = await repo.get_scores(session, version_id)
            eligibility = await repo.get_eligibility(session, version_id)
            pool = await repo.get_candidate_pool(
                session, uv.candidate_pool_version_id
            )
            errors = self._validation_errors(uv, members, scores, eligibility, pool)
            now = _utcnow()
            if not errors:
                rowcount = await repo.guarded_universe_status_update(
                    session,
                    version_id,
                    from_status=UniverseStatus.DRAFT,
                    to_status=UniverseStatus.VALIDATED,
                    validated_at=now,
                    validation_result_ref=repo.encode_metadata(
                        {
                            "schema": _VALIDATION_SCHEMA,
                            "status": "PASS",
                            "validated_at": now.isoformat(),
                            "errors": [],
                        }
                    ),
                )
                if rowcount != 1:
                    raise repo.UniverseStateTransitionError(
                        f"Universe {version_id} failed to validate "
                        "(concurrent status change)"
                    )
                await repo.insert_event(
                    session,
                    event_type=UniverseEventType.UNIVERSE_VALIDATED,
                    actor=actor,
                    universe_version_id=version_id,
                    result_status=UniverseStatus.VALIDATED,
                )
                await session.commit()
                return await self.get_universe_version(version_id)

            # gate FAILED -> DRAFT -> REJECTED (terminal), all errors listed
            rowcount = await repo.guarded_universe_status_update(
                session,
                version_id,
                from_status=UniverseStatus.DRAFT,
                to_status=UniverseStatus.REJECTED,
                rejection_reason="; ".join(errors),
                validation_result_ref=repo.encode_metadata(
                    {
                        "schema": _VALIDATION_SCHEMA,
                        "status": "FAIL",
                        "validated_at": now.isoformat(),
                        "errors": errors,
                    }
                ),
            )
            if rowcount != 1:
                raise repo.UniverseStateTransitionError(
                    f"Universe {version_id} failed to reject after failed "
                    "validation (concurrent status change)"
                )
            await repo.insert_event(
                session,
                event_type=UniverseEventType.UNIVERSE_VALIDATION_FAILED,
                actor=actor,
                universe_version_id=version_id,
                result_status=UniverseStatus.REJECTED,
                reason="; ".join(errors),
                metadata_json=repo.encode_metadata({"errors": errors}),
            )
            await session.commit()
        raise repo.UniverseValidationError(errors)

    # ---------------------------------------------------------------- approval
    async def approve_universe(
        self,
        version_id: str,
        *,
        actor: str,
        reason: Optional[str] = None,
        actor_type: str = ActorType.USER,
    ) -> dict:
        """VALIDATED -> APPROVED with the Q13 ss8 approval-evidence block.

        Manually only: no approval role exists in the repo (VERIFIED), and the
        SYSTEM auto-approval path is not implemented (Q13 ss5) — it stays behind
        ``AUTO_APPROVE_IF_VALID``/``UNIVERSE_SOURCE`` settings, which are
        defaults-off in Phase 3. Idempotent on an already-APPROVED version.
        """
        async with self._session() as session:
            uv = await repo.require_universe_version(session, version_id)
            if uv.status == UniverseStatus.APPROVED:
                return _version_to_dict(uv)
            if uv.status != UniverseStatus.VALIDATED:
                raise repo.UniverseStateTransitionError(
                    f"Universe {version_id} status {uv.status} cannot be "
                    "approved (only VALIDATED; VALIDATED != APPROVED, Q13 ss9)"
                )
            now = _utcnow()
            rowcount = await repo.guarded_universe_status_update(
                session,
                version_id,
                from_status=UniverseStatus.VALIDATED,
                to_status=UniverseStatus.APPROVED,
                approved_at=now,
                approval_actor=actor,
                approval_actor_type=actor_type,
                approval_reason=reason,
                approval_policy_version=_APPROVAL_POLICY_VERSION,
            )
            if rowcount != 1:
                raise repo.UniverseStateTransitionError(
                    f"Universe {version_id} could not be approved "
                    "(concurrent status change)"
                )
            await repo.insert_event(
                session,
                event_type=UniverseEventType.UNIVERSE_APPROVED,
                actor=actor,
                actor_type=actor_type,
                universe_version_id=version_id,
                candidate_pool_version_id=uv.candidate_pool_version_id,
                result_status=UniverseStatus.APPROVED,
                reason=reason,
                metadata_json=repo.encode_metadata(
                    {
                        "approval_mode": "manual",
                        "policy_version": _APPROVAL_POLICY_VERSION,
                        "validation_ref": _VALIDATION_SCHEMA,
                    }
                ),
            )
            await session.commit()
        return await self.get_universe_version(version_id)

    async def reject_universe(
        self,
        version_id: str,
        *,
        actor: str,
        reason: Optional[str] = None,
    ) -> dict:
        """Explicit pre-activation abort: DRAFT/VALIDATED/APPROVED -> REJECTED.

        Terminal. A failed validation path uses ``validate_universe`` which
        already moves DRAFT -> REJECTED with the full error list.
        """
        async with self._session() as session:
            uv = await repo.require_universe_version(session, version_id)
            if uv.status == UniverseStatus.REJECTED:
                return _version_to_dict(uv)
            if uv.status not in {
                UniverseStatus.DRAFT,
                UniverseStatus.VALIDATED,
                UniverseStatus.APPROVED,
            }:
                raise repo.UniverseStateTransitionError(
                    f"Universe {version_id} status {uv.status} cannot be "
                    "rejected (terminal states are immutable)"
                )
            rowcount = await repo.guarded_universe_status_update(
                session,
                version_id,
                from_status=uv.status,
                to_status=UniverseStatus.REJECTED,
                rejection_reason=reason,
            )
            if rowcount != 1:
                raise repo.UniverseStateTransitionError(
                    f"Universe {version_id} could not be rejected "
                    "(concurrent status change)"
                )
            await repo.insert_event(
                session,
                event_type=UniverseEventType.UNIVERSE_REJECTED,
                actor=actor,
                universe_version_id=version_id,
                result_status=UniverseStatus.REJECTED,
                reason=reason,
            )
            await session.commit()
        return await self.get_universe_version(version_id)

    # --------------------------------------------------------------- activation
    async def activate_universe(
        self,
        version_id: str,
        *,
        as_of: Optional[datetime] = None,
        actor: str = "SYSTEM",
        reason: Optional[str] = None,
    ) -> dict:
        """APPROVED -> ACTIVE via the Q12 ss8 atomic pointer flip.

        One transaction: notice the request, supersede the current ACTIVE,
        activate the target. Any failure (wrong status, DB conflict, partial
        unique-index violation) rolls the whole transaction back — KEEP CURRENT
        ACTIVE. Refused outside the activation window (Q13 ss10); ``as_of`` is a
        test hook to drive the guard deterministically. Idempotent when the
        target is already ACTIVE.
        """
        as_of = self._guard_activation_window(as_of)
        async with self._session() as session:
            # Read-only precondition pass (no writes): status + idempotency.
            uv = await repo.require_universe_version(session, version_id)
            if uv.status == UniverseStatus.ACTIVE:
                return _version_to_dict(uv)
            if uv.status != UniverseStatus.APPROVED:
                await repo.insert_event(
                    session,
                    event_type=UniverseEventType.STATE_TRANSITION_DENIED,
                    actor=actor,
                    universe_version_id=version_id,
                    result_status=uv.status,
                    reason=(
                        f"activation refused: status {uv.status}, "
                        "only APPROVED can activate"
                    ),
                    metadata_json=repo.encode_metadata({"requested": "ACTIVE"}),
                )
                await session.commit()
                raise repo.UniverseStateTransitionError(
                    f"Universe {version_id} status {uv.status} cannot be "
                    "activated (only APPROVED; Q13 ss18 invariant)"
                )
            # Append-only activation-request audit committed BEFORE the flip —
            # a refused/collided flip still leaves the request record (Q13 ss16).
            await repo.insert_event(
                session,
                event_type=UniverseEventType.UNIVERSE_ACTIVATION_REQUESTED,
                actor=actor,
                universe_version_id=version_id,
                result_status=UniverseStatus.APPROVED,
                reason=reason,
                metadata_json=repo.encode_metadata({"window": as_of.isoformat()}),
            )
            await session.commit()
            try:
                now = _utcnow()
                current = await repo.get_active_universe(session)
                previous_id = None
                if current is not None and current.id != version_id:
                    previous_id = current.id
                    old_rows = await repo.guarded_universe_status_update(
                        session,
                        current.id,
                        from_status=UniverseStatus.ACTIVE,
                        to_status=UniverseStatus.SUPERSEDED,
                        superseded_at=now,
                        effective_until=now,
                    )
                    if old_rows != 1:
                        raise repo.UniverseStateTransitionError(
                            f"activation conflict: current ACTIVE "
                            f"{current.id} changed concurrently"
                        )
                    await repo.insert_event(
                        session,
                        event_type=UniverseEventType.UNIVERSE_SUPERSEDED,
                        actor=actor,
                        universe_version_id=current.id,
                        new_active_universe_id=version_id,
                        result_status=UniverseStatus.SUPERSEDED,
                        reason=reason,
                    )
                new_rows = await repo.guarded_universe_status_update(
                    session,
                    version_id,
                    from_status=UniverseStatus.APPROVED,
                    to_status=UniverseStatus.ACTIVE,
                    activated_at=now,
                    effective_from=uv.effective_from or now,
                )
                if new_rows != 1:
                    raise repo.UniverseStateTransitionError(
                        f"activation conflict: {version_id} is not "
                        "APPROVED anymore (0 rows updated) — KEEP CURRENT"
                    )
                await repo.insert_event(
                    session,
                    event_type=UniverseEventType.UNIVERSE_ACTIVATED,
                    actor=actor,
                    universe_version_id=version_id,
                    previous_active_universe_id=previous_id,
                    new_active_universe_id=version_id,
                    result_status=UniverseStatus.ACTIVE,
                    reason=reason,
                )
                await session.commit()
            except Exception as exc:
                # Flip transaction rolled back -> current ACTIVE preserved;
                # record the failure event separately (Q12 ss8 / Q13 ss16).
                await session.rollback()
                async with self._session() as s2:
                    await repo.insert_event(
                        s2,
                        event_type=UniverseEventType.UNIVERSE_ACTIVATION_FAILED,
                        actor=actor,
                        universe_version_id=version_id,
                        result_status="KEPT_CURRENT_ACTIVE",
                        reason=str(exc),
                        metadata_json=repo.encode_metadata(
                            {"failure": type(exc).__name__, "window": as_of.isoformat()}
                        ),
                    )
                    await s2.commit()
                raise
        return await self.get_universe_version(version_id)

    # ----------------------------------------------------------------- rollback
    async def rollback_to(
        self,
        version_id: str,
        *,
        as_of: Optional[datetime] = None,
        actor: str,
        reason: Optional[str] = None,
    ) -> dict:
        """Emergency revert to a previous valid version (Q13 ss12 / Q12 ss9).

        Mirrors activation: current ACTIVE -> SUPERSEDED (with a rollback
        marker), target -> ACTIVE, atomic; refusals leave the current ACTIVE
        untouched. Target must be a known, structurally valid historical version
        (SUPERSEDED or APPROVED with a prior effective_from) — NEVER DRAFT or
        REJECTED. Manual approval identity ``actor`` is mandatory (rollback is
        irreversible and risky). Refused outside the activation window.
        """
        as_of = self._guard_activation_window(as_of)
        async with self._session() as session:
            target = await repo.require_universe_version(session, version_id)
            if target.status == UniverseStatus.ACTIVE:
                return _version_to_dict(target)
            if target.status not in {
                UniverseStatus.SUPERSEDED,
                UniverseStatus.APPROVED,
                UniverseStatus.VALIDATED,
            }:
                await repo.insert_event(
                    session,
                    event_type=UniverseEventType.STATE_TRANSITION_DENIED,
                    actor=actor,
                    universe_version_id=version_id,
                    result_status=target.status,
                    reason=(
                        f"rollback refused: version {version_id} is "
                        f"{target.status}; only SUPERSEDED/APPROVED/"
                        "VALIDATED historical versions are valid targets"
                    ),
                )
                await session.commit()
                raise repo.UniverseStateTransitionError(
                    f"Universe {version_id} ({target.status}) is not a valid "
                    "rollback target (never DRAFT/REJECTED)"
                )
            # Cheap structural re-validation of the stored version (Q13 ss12).
            members = await repo.get_members(session, version_id)
            scores = await repo.get_scores(session, version_id)
            eligibility = await repo.get_eligibility(session, version_id)
            pool = await repo.get_candidate_pool(session, target.candidate_pool_version_id)
            gate_errors = self._validation_errors(
                target, members, scores, eligibility, pool
            )
            if gate_errors:
                await repo.insert_event(
                    session,
                    event_type=UniverseEventType.STATE_TRANSITION_DENIED,
                    actor=actor,
                    universe_version_id=version_id,
                    result_status=target.status,
                    reason="rollback refused: target no longer passes the "
                    "structural gate: " + "; ".join(gate_errors),
                    metadata_json=repo.encode_metadata({"errors": gate_errors}),
                )
                await session.commit()
                raise repo.UniverseValidationError(gate_errors)

            # Append-only rollback-request audit committed before the flip.
            await repo.insert_event(
                session,
                event_type=UniverseEventType.UNIVERSE_ROLLBACK_REQUESTED,
                actor=actor,
                universe_version_id=version_id,
                result_status=target.status,
                reason=reason,
                metadata_json=repo.encode_metadata({"window": as_of.isoformat()}),
            )
            await session.commit()
            try:
                now = _utcnow()
                current = await repo.get_active_universe(session)
                previous_id = current.id if current else None
                if current is not None and current.id != version_id:
                    old_rows = await repo.guarded_universe_status_update(
                        session,
                        current.id,
                        from_status=UniverseStatus.ACTIVE,
                        to_status=UniverseStatus.SUPERSEDED,
                        superseded_at=now,
                        effective_until=now,
                    )
                    if old_rows != 1:
                        raise repo.UniverseStateTransitionError(
                            f"rollback conflict: current ACTIVE "
                            f"{current.id} changed concurrently"
                        )
                    await repo.insert_event(
                        session,
                        event_type=UniverseEventType.UNIVERSE_SUPERSEDED,
                        actor=actor,
                        universe_version_id=current.id,
                        new_active_universe_id=version_id,
                        result_status=UniverseStatus.SUPERSEDED,
                        reason=reason,
                        metadata_json=repo.encode_metadata(
                            {"rollback_marker": True}
                        ),
                    )
                new_rows = await repo.guarded_universe_status_update(
                    session,
                    version_id,
                    from_status=target.status,
                    to_status=UniverseStatus.ACTIVE,
                    activated_at=now,
                    effective_from=target.effective_from or now,
                )
                if new_rows != 1:
                    raise repo.UniverseStateTransitionError(
                        f"rollback conflict: {version_id} changed "
                        "concurrently (0 rows updated)"
                    )
                await repo.insert_event(
                    session,
                    event_type=UniverseEventType.UNIVERSE_ROLLED_BACK,
                    actor=actor,
                    universe_version_id=version_id,
                    previous_active_universe_id=previous_id,
                    new_active_universe_id=version_id,
                    result_status=UniverseStatus.ACTIVE,
                    reason=reason,
                    metadata_json=repo.encode_metadata(
                        {"rollback": True, "from_status": target.status}
                    ),
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                async with self._session() as s2:
                    await repo.insert_event(
                        s2,
                        event_type=UniverseEventType.UNIVERSE_ACTIVATION_FAILED,
                        actor=actor,
                        universe_version_id=version_id,
                        result_status="KEPT_CURRENT_ACTIVE",
                        reason="rollback failed: " + str(exc),
                        metadata_json=repo.encode_metadata(
                            {"failure": type(exc).__name__}
                        ),
                    )
                    await s2.commit()
                raise
        return await self.get_universe_version(version_id)

    # ------------------------------------------------------------------- reads
    async def get_universe_version(self, version_id: str) -> Optional[dict]:
        async with self._session() as session:
            uv = await repo.get_universe_version(session, version_id)
            if uv is None:
                return None
            return _version_to_dict(uv)

    async def get_active_universe(self, *, with_detail: bool = True) -> Optional[dict]:
        async with self._session() as session:
            uv = await repo.get_active_universe(session)
            if uv is None:
                return None
            result = _version_to_dict(uv)
            if with_detail:
                result["members"] = [
                    _member_to_dict(m)
                    for m in await repo.get_members(session, uv.id)
                ]
                result["scores"] = [
                    _score_to_dict(s) for s in await repo.get_scores(session, uv.id)
                ]
                result["eligibility"] = [
                    _eligibility_to_dict(e)
                    for e in await repo.get_eligibility(session, uv.id)
                ]
            return result

    async def get_universe_version_detail(self, version_id: str) -> Optional[dict]:
        """Phase 4 (ADDITIVE read): full snapshot (members + scores +
        eligibility) for ANY version — the Rotation Engine uses this to load the
        previous cycle for retention / anti-churn history. Same shape as
        ``get_active_universe(with_detail=True)``.
        """
        async with self._session() as session:
            uv = await repo.get_universe_version(session, version_id)
            if uv is None:
                return None
            result = _version_to_dict(uv)
            result["members"] = [
                _member_to_dict(m)
                for m in await repo.get_members(session, uv.id)
            ]
            result["scores"] = [
                _score_to_dict(s) for s in await repo.get_scores(session, uv.id)
            ]
            result["eligibility"] = [
                _eligibility_to_dict(e)
                for e in await repo.get_eligibility(session, uv.id)
            ]
            return result

    async def list_universe_versions(self, limit: Optional[int] = None) -> list[dict]:
        async with self._session() as session:
            return [
                _version_to_dict(uv)
                for uv in await repo.list_universe_versions(session, limit=limit)
            ]

    async def list_events(
        self, *, universe_version_id: Optional[str] = None, limit: int = 200
    ) -> list[dict]:
        async with self._session() as session:
            return [
                _event_to_dict(e)
                for e in await repo.list_events(
                    session, universe_version_id=universe_version_id, limit=limit
                )
            ]

    # -------------------------------------------------------------------- gate
    def _validation_errors(self, uv, members, scores, eligibility, pool) -> list[str]:
        """The ss7 gate restricted to rules Phase 3 can verify from persisted
        state. Data-quality/provider thresholds (MIN_OBSERVATION_SESSIONS,
        freshness floors, indicator calculability, EXCLUDED_SYMBOLS contents,
        provider success floors) are [TO BE CALIBRATED] and are NOT re-verified
        here — their results live in the persisted eligibility rows, whose
        presence IS verified. No new numerical threshold is introduced (Q13 ss7).
        """
        errors: list[str] = []

        # --- structural: counts ------------------------------------------------
        core_members = [m for m in members if m.category == UniverseCategory.CORE]
        rotation_members = [
            m for m in members if m.category == UniverseCategory.ROTATION
        ]
        for m in members:
            if m.category not in UniverseCategory.VALID:
                errors.append(f"invalid category {m.category!r} for {m.symbol}")
        if len(core_members) != 30:
            errors.append(f"CORE count must be exactly 30 (found {len(core_members)})")
        if len(rotation_members) != 50:
            errors.append(
                f"ROTATION count must be exactly 50 (found {len(rotation_members)})"
            )
        if len(members) != 80:
            errors.append(f"total count must be exactly 80 (found {len(members)})")
        if uv is not None and (
            uv.total_count != len(members)
            or uv.core_count != len(core_members)
            or uv.rotation_count != len(rotation_members)
        ):
            errors.append("declared counts disagree with persisted membership")

        # --- structural: duplicates / overlap -----------------------------------
        symbols = [m.symbol for m in members]
        dups = sorted({s for s in symbols if symbols.count(s) > 1})
        if dups:
            errors.append(f"duplicate symbols: {', '.join(dups)}")
        core_symbols = {m.symbol for m in core_members}
        rotation_symbols = {m.symbol for m in rotation_members}
        overlap = sorted(core_symbols & rotation_symbols)
        if overlap:
            errors.append(f"CORE/ROTATION overlap: {', '.join(overlap)}")

        # --- symbol pattern (EXCLUDED_SYMBOLS contents TO BE CALIBRATED) --------
        for m in members:
            if not _SYMBOL_PATTERN.match(m.symbol or ""):
                errors.append(f"invalid symbol pattern: {m.symbol!r}")
            elif m.symbol in EXCLUDED_SYMBOLS:
                errors.append(f"symbol excluded: {m.symbol}")

        # --- quality: ranks + reasons -------------------------------------------
        per_category: dict = {
            UniverseCategory.CORE: [],
            UniverseCategory.ROTATION: [],
        }
        for m in members:
            if m.category in per_category:
                per_category[m.category].append(m)
        for category, rows in per_category.items():
            ranks = sorted(m.rank for m in rows)
            if len(set(ranks)) != len(ranks):
                errors.append(f"{category} ranks are not unique")
            if ranks and ranks != list(range(1, len(rows) + 1)):
                errors.append(f"{category} ranks are not contiguous 1..{len(rows)}")
            for m in rows:
                if not (m.selection_reason or "").strip():
                    errors.append(f"{m.symbol}: empty selection_reason")

        # --- quality: score components within [0, weight] ------------------------
        score_symbols = {s.symbol for s in scores}
        for m in members:
            if m.symbol not in score_symbols:
                errors.append(f"{m.symbol}: missing rotation score row")
                continue
            row = next(s for s in scores if s.symbol == m.symbol)
            for component, weight in ROTATION_SCORE_COMPONENT_WEIGHTS.items():
                value = getattr(row, component, None)
                if value is None:
                    errors.append(f"{m.symbol}: missing {component}")
                elif not (0 <= value <= weight):
                    errors.append(
                        f"{m.symbol}: {component}={value} outside [0, {weight}]"
                    )
            total = row.total_rotation_score
            if total is None or not (0 <= total <= 100):
                errors.append(f"{m.symbol}: total_rotation_score outside [0, 100]")

        # --- eligibility rows for every member -----------------------------------
        eligibility_symbols = {e.symbol for e in eligibility}
        for m in members:
            if m.symbol not in eligibility_symbols:
                errors.append(f"{m.symbol}: missing eligibility result")
                continue
            row = next(e for e in eligibility if e.symbol == m.symbol)
            if row.eligibility_status not in EligibilityResultStatus.VALID:
                errors.append(f"{m.symbol}: invalid eligibility_status")

        # --- config / provenance --------------------------------------------------
        if uv is not None:
            if not (uv.version or "").strip():
                errors.append("missing universe version string")
            if uv.calculated_at is None:
                errors.append("missing calculated_at")
            if uv.effective_from is None:
                errors.append("missing effective_from")
            if not (uv.config_hash or "").strip():
                errors.append("missing config_hash")
            if pool is None:
                errors.append(
                    f"missing candidate pool {uv.candidate_pool_version_id}"
                )
            elif pool.status != CandidatePoolStatus.VALIDATED:
                errors.append(
                    f"candidate pool {pool.id} is {pool.status}, must be "
                    "VALIDATED (Q13 ss14)"
                )
        return errors


# ---------------------------------------------------------------------------
# Row -> dict serializers (matching existing snake_case response conventions)
# ---------------------------------------------------------------------------


def _pool_to_dict(pool) -> dict:
    return {
        "id": pool.id,
        "version": pool.version,
        "source": pool.source,
        "source_reference": pool.source_reference,
        "status": pool.status,
        "symbol_count": pool.symbol_count,
        "validation_status": pool.validation_status,
        "created_at": pool.created_at,
        "validated_at": pool.validated_at,
        "error_info": pool.error_info,
        "metadata_json": pool.metadata_json,  # Phase 4 (ADDITIVE: engine input symbols)
    }


def _version_to_dict(uv) -> dict:
    return {
        "id": uv.id,
        "version": uv.version,
        "candidate_pool_version_id": uv.candidate_pool_version_id,
        "status": uv.status,
        "total_count": uv.total_count,
        "core_count": uv.core_count,
        "rotation_count": uv.rotation_count,
        "created_at": uv.created_at,
        "calculated_at": uv.calculated_at,
        "validated_at": uv.validated_at,
        "approved_at": uv.approved_at,
        "activated_at": uv.activated_at,
        "superseded_at": uv.superseded_at,
        "effective_from": uv.effective_from,
        "effective_until": uv.effective_until,
        "validation_result_ref": uv.validation_result_ref,
        "approval_actor": uv.approval_actor,
        "approval_actor_type": uv.approval_actor_type,
        "approval_reason": uv.approval_reason,
        "approval_policy_version": uv.approval_policy_version,
        "rejection_reason": uv.rejection_reason,
        "config_hash": uv.config_hash,
    }


def _member_to_dict(m) -> dict:
    return {
        "id": m.id,
        "symbol": m.symbol,
        "category": m.category,
        "rank": m.rank,
        "rotation_score": m.rotation_score,
        "selection_reason": m.selection_reason,
        "previous_category": m.previous_category,
        "previous_rank": m.previous_rank,
        "previous_score": m.previous_score,
        "retention_reason": m.retention_reason,
        "replacement_reason": m.replacement_reason,
    }


def _score_to_dict(s) -> dict:
    return {
        "symbol": s.symbol,
        "liquidity_score": s.liquidity_score,
        "volume_quality_score": s.volume_quality_score,
        "volatility_score": s.volatility_score,
        "signal_frequency_score": s.signal_frequency_score,
        "long_quality_score": s.long_quality_score,
        "short_quality_score": s.short_quality_score,
        "data_quality_score": s.data_quality_score,
        "total_rotation_score": s.total_rotation_score,
    }


def _eligibility_to_dict(e) -> dict:
    return {
        "symbol": e.symbol,
        "eligibility_status": e.eligibility_status,
        "tradability": e.tradability,
        "liquidity": e.liquidity,
        "volume": e.volume,
        "volatility": e.volatility,
        "data_quality": e.data_quality,
        "signal_quality": e.signal_quality,
        "failure_reason": e.failure_reason,
        "data_quality_state": e.data_quality_state,
    }


def _event_to_dict(e) -> dict:
    return {
        "id": e.id,
        "event_type": e.event_type,
        "created_at": e.created_at,
        "actor": e.actor,
        "actor_type": e.actor_type,
        "universe_version_id": e.universe_version_id,
        "candidate_pool_version_id": e.candidate_pool_version_id,
        "previous_active_universe_id": e.previous_active_universe_id,
        "new_active_universe_id": e.new_active_universe_id,
        "reason": e.reason,
        "result_status": e.result_status,
        "metadata_json": e.metadata_json,
    }