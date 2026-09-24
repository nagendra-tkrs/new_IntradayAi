"""Universe Management persistence models (Phase 3).

Implements the Q12/Q13-approved persistence entities for the Universe Manager:

* candidate pool versions (Q10 HYBRID linkage)
* universe versions (lifecycle state machine)
* universe memberships (CORE / ROTATION, version-scoped)
* rotation score components (computed results only - never scoring logic)
* eligibility results (why a candidate was included/excluded)
* append-only universe events (audit trail)

All tables are purely ADDITIVE: they are created by the existing
``Base.metadata.create_all()`` startup mechanism and are NOT consumed by the
legacy scanner (``UNIVERSE_SOURCE=legacy`` remains the default). No existing
table is modified.

Status/enum values are enforced at the application layer (the repository has
no CHECK-constraint precedent - Phase 2 ss13.4 / Q12 ss13), while the
exactly-one-ACTIVE invariant is enforced at the database level by a SQLite
partial unique index.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from app.core.database import Base
from app.models.models import gen_id


class UniverseStatus:
    """Lifecycle states for a universe version (Phase 2 ss13 / Q13 ss6)."""
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"
    VALID = frozenset({DRAFT, VALIDATED, APPROVED, ACTIVE, SUPERSEDED, REJECTED})


class CandidatePoolStatus:
    """Lifecycle states for a candidate-pool version (Q10 Q12)."""
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"
    VALID = frozenset({DRAFT, VALIDATED, REJECTED, SUPERSEDED})


class UniverseCategory:
    CORE = "CORE"
    ROTATION = "ROTATION"
    VALID = frozenset({CORE, ROTATION})


class EligibilityResultStatus:
    """Possible eligibility evaluation results (Phase 3 ss5.E)."""
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    VALID = frozenset({ELIGIBLE, INELIGIBLE, INSUFFICIENT_DATA})


class FilterResult:
    """Per-filter outcomes inside an eligibility result."""
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ActorType:
    SYSTEM = "SYSTEM"
    USER = "USER"


class UniverseEventType:
    """Event vocabulary supporting the Q13 approval workflow (append-only)."""
    CANDIDATE_POOL_CREATED = "CANDIDATE_POOL_CREATED"
    CANDIDATE_POOL_VALIDATED = "CANDIDATE_POOL_VALIDATED"
    CANDIDATE_POOL_REJECTED = "CANDIDATE_POOL_REJECTED"
    CANDIDATE_POOL_REFRESH_FAILED = "CANDIDATE_POOL_REFRESH_FAILED"
    UNIVERSE_DRAFT_CREATED = "UNIVERSE_DRAFT_CREATED"
    UNIVERSE_VALIDATED = "UNIVERSE_VALIDATED"
    UNIVERSE_VALIDATION_FAILED = "UNIVERSE_VALIDATION_FAILED"
    UNIVERSE_APPROVED = "UNIVERSE_APPROVED"
    UNIVERSE_REJECTED = "UNIVERSE_REJECTED"
    UNIVERSE_ACTIVATION_REQUESTED = "UNIVERSE_ACTIVATION_REQUESTED"
    UNIVERSE_ACTIVATED = "UNIVERSE_ACTIVATED"
    UNIVERSE_ACTIVATION_FAILED = "UNIVERSE_ACTIVATION_FAILED"
    UNIVERSE_SUPERSEDED = "UNIVERSE_SUPERSEDED"
    UNIVERSE_ROLLBACK_REQUESTED = "UNIVERSE_ROLLBACK_REQUESTED"
    UNIVERSE_ROLLED_BACK = "UNIVERSE_ROLLED_BACK"
    STATE_TRANSITION_DENIED = "STATE_TRANSITION_DENIED"
    EMERGENCY_OVERRIDE_REQUESTED = "EMERGENCY_OVERRIDE_REQUESTED"


class CandidatePoolVersion(Base):
    """One validated snapshot/version of the candidate stock pool (Q10 HYBRID).

    Immutable once VALIDATED: the state service exposes no UPDATE path for
    validated pools; a failed refresh never deletes/empties the previous
    validated pool (Q10 retain-on-failure rule).
    """

    __tablename__ = "universe_candidate_pools"
    __table_args__ = (
        UniqueConstraint("source", "version", name="uqx_candidate_pool_source_version"),
        Index("ix_universe_candidate_pools_status", "status"),
    )

    id = Column(String(16), primary_key=True, default=gen_id)
    version = Column(String(50), nullable=False)
    source = Column(String(50), nullable=False, default="local")
    source_reference = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False, default=CandidatePoolStatus.DRAFT)
    symbol_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    validated_at = Column(DateTime, nullable=True)
    validation_status = Column(String(30), nullable=True)
    error_info = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)


class UniverseVersion(Base):
    """One calculated 80-stock universe candidate with its full lifecycle state.

    DB-level guarantees:
    * exactly one ACTIVE version (SQLite partial unique index on status='ACTIVE')
    * unique ``version`` (per-source counter stability / draft idempotency)
    * version-scoped membership: any reader resolving the ACTIVE version sees
      exactly that version's full membership set (no mixed-universe blends).
    """

    __tablename__ = "universe_versions"
    __table_args__ = (
        UniqueConstraint("version", name="uqx_universe_versions_version"),
        Index("ix_universe_versions_status", "status"),
        Index("ix_universe_versions_pool", "candidate_pool_version_id"),
        Index(
            "ix_universe_versions_single_active",
            "status",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
        ),
    )

    id = Column(String(16), primary_key=True, default=gen_id)
    version = Column(String(50), nullable=False)
    candidate_pool_version_id = Column(
        String(16),
        ForeignKey("universe_candidate_pools.id"),
        nullable=False,
    )
    status = Column(String(20), nullable=False, default=UniverseStatus.DRAFT)

    # counts (set at draft creation; re-validated by validate/universe gate)
    total_count = Column(Integer, nullable=True)
    core_count = Column(Integer, nullable=True)
    rotation_count = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    calculated_at = Column(DateTime, nullable=True)
    validated_at = Column(DateTime, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    activated_at = Column(DateTime, nullable=True)
    superseded_at = Column(DateTime, nullable=True)
    effective_from = Column(DateTime, nullable=True)
    effective_until = Column(DateTime, nullable=True)

    # validation + approval evidence (Q13 ss8/ss9 — VALIDATED != APPROVED)
    validation_result_ref = Column(Text, nullable=True)
    approval_actor = Column(String(64), nullable=True)
    approval_actor_type = Column(String(20), nullable=True)
    approval_reason = Column(Text, nullable=True)
    approval_policy_version = Column(String(50), nullable=True)
    rejection_reason = Column(Text, nullable=True)
    config_hash = Column(String(64), nullable=True)


class UniverseMembership(Base):
    """Which stocks belong to a universe version (CORE/ROTATION).

    Version-scoped and immutable once the version leaves DRAFT. ``rank`` is
    unique within each category (CORE 1..30, ROTATION 1..50) — enforced at the
    application layer; duplicates and overlap are blocked at draft creation.
    """

    __tablename__ = "universe_members"
    __table_args__ = (
        UniqueConstraint("universe_version_id", "symbol", name="uqx_universe_member_symbol"),
        Index("ix_universe_members_category", "category"),
    )

    id = Column(String(16), primary_key=True, default=gen_id)
    universe_version_id = Column(
        String(16),
        ForeignKey("universe_versions.id"),
        nullable=False,
    )
    symbol = Column(String(50), nullable=False)
    category = Column(String(10), nullable=False)
    rank = Column(Integer, nullable=False)
    rotation_score = Column(Float, nullable=True)
    selection_reason = Column(Text, nullable=True)
    previous_category = Column(String(10), nullable=True)
    previous_rank = Column(Integer, nullable=True)
    previous_score = Column(Float, nullable=True)
    retention_reason = Column(String(50), nullable=True)
    replacement_reason = Column(String(50), nullable=True)


class UniverseRotationScore(Base):
    """Rotation Score component values for a membership (Phase 2 ss6).

    Stores the CALCULATED result only — the scoring computation lives in the
    future Rotation Engine, never here. Approved weight bounds (Phase 2 ss6):
    liquidity 20, volume 15, volatility 15, signal-freq 10, LONG 15, SHORT 15,
    data 10 — component ranges are application-validated at draft creation.
    """

    __tablename__ = "universe_member_scores"
    __table_args__ = (
        UniqueConstraint("universe_version_id", "symbol", name="uqx_universe_score_symbol"),
    )

    id = Column(String(16), primary_key=True, default=gen_id)
    universe_version_id = Column(
        String(16),
        ForeignKey("universe_versions.id"),
        nullable=False,
    )
    symbol = Column(String(50), nullable=False)
    liquidity_score = Column(Float, nullable=True)
    volume_quality_score = Column(Float, nullable=True)
    volatility_score = Column(Float, nullable=True)
    signal_frequency_score = Column(Float, nullable=True)
    long_quality_score = Column(Float, nullable=True)
    short_quality_score = Column(Float, nullable=True)
    data_quality_score = Column(Float, nullable=True)
    total_rotation_score = Column(Float, nullable=True)


class UniverseEligibility(Base):
    """Eligibility evaluation result per symbol for a universe calculation.

    Captures per-filter outcomes (S2..S7 stage results) and data-quality state.
    ``INSUFFICIENT_DATA`` is a first-class status — missing data is never
    converted into a fake zero-quality measurement (Phase 2 ss3.2 / ss8).
    """

    __tablename__ = "universe_eligibility"
    __table_args__ = (
        Index("ix_universe_eligibility_uv_symbol", "universe_version_id", "symbol"),
    )

    id = Column(String(16), primary_key=True, default=gen_id)
    universe_version_id = Column(
        String(16),
        ForeignKey("universe_versions.id"),
        nullable=False,
    )
    symbol = Column(String(50), nullable=False)
    eligibility_status = Column(String(20), nullable=False)
    tradability = Column(String(20), nullable=True)
    liquidity = Column(String(20), nullable=True)
    volume = Column(String(20), nullable=True)
    volatility = Column(String(20), nullable=True)
    data_quality = Column(String(20), nullable=True)
    signal_quality = Column(String(20), nullable=True)
    failure_reason = Column(Text, nullable=True)
    data_quality_state = Column(String(30), nullable=True)
    evaluated_at = Column(DateTime, default=datetime.utcnow)


class UniverseEvent(Base):
    """Append-only event/audit record (Q13 ss16).

    Events are INSERT-only: the repository exposes no update or delete path for
    events, and historical events are never mutated.
    """

    __tablename__ = "universe_events"
    __table_args__ = (
        Index("ix_universe_events_uv", "universe_version_id"),
        Index("ix_universe_events_created", "created_at"),
        Index("ix_universe_events_type", "event_type"),
    )

    id = Column(String(16), primary_key=True, default=gen_id)
    event_type = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    actor = Column(String(64), nullable=False, default="SYSTEM")
    actor_type = Column(String(20), nullable=False, default=ActorType.SYSTEM)
    universe_version_id = Column(
        String(16),
        ForeignKey("universe_versions.id"),
        nullable=True,
    )
    candidate_pool_version_id = Column(
        String(16),
        ForeignKey("universe_candidate_pools.id"),
        nullable=True,
    )
    previous_active_universe_id = Column(String(16), nullable=True)
    new_active_universe_id = Column(String(16), nullable=True)
    reason = Column(Text, nullable=True)
    result_status = Column(String(30), nullable=True)
    metadata_json = Column(Text, nullable=True)


# Rotations score component weight bounds (Phase 2 ss6 — approved weights).
ROTATION_SCORE_COMPONENT_WEIGHTS = {
    "liquidity_score": 20,
    "volume_quality_score": 15,
    "volatility_score": 15,
    "signal_frequency_score": 10,
    "long_quality_score": 15,
    "short_quality_score": 15,
    "data_quality_score": 10,
}