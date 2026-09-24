"""Phase 3 — Universe Manager database & persistence implementation tests.

Persistence-only scope (no Rotation Engine, no scoring, no scheduler, no
auto-approval). Every test runs against a TEMP-file SQLite database created via
``Base.metadata.create_all`` — the real ``intradayai.db`` is never touched.

Coverage (Q12 ss17 / Q13 ss20 future-test map):
* schema: new tables registered; DB-level constraints (partial unique index
  exactly-one-ACTIVE; UNIQUE(universe_version_id, symbol));
* candidate pool: create/validate lifecycle; refresh-failure retains previous
  validated pool (Q10); terminal reject;
* draft creation: one consistent snapshot; validation gate (counts, duplicates,
  overlap, missing scores, unvalidated pool) -> REJECTED with all errors;
* state machine: DRAFT->VALIDATED->APPROVED->ACTIVE->SUPERSEDED; approved
  evidence block; VALIDATED != APPROVED; forbidden transitions refused;
* atomic activation: exactly one ACTIVE; failed activation preserves current;
* market-hours guard (09:15-15:30 IST): activation/rollback refused in-market;
* rollback: valid historical target re-activates; DRAFT/REJECTED never; events
  append-only.
"""
import asyncio
import json
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import market_session
from app.core.config import settings
from app.core.database import Base
from app.models.universe_models import ROTATION_SCORE_COMPONENT_WEIGHTS
from app.repositories import universe_repository as urepo
from app.repositories.universe_repository import (
    CandidatePoolVersionNotFound,
    UniverseMarketHoursError,
    UniverseStateTransitionError,
    UniverseValidationError,
)
from app.services.universe_state_service import UniverseStateService

TZ = market_session.IST
# 2026-09-23 = Wednesday, not an NSE holiday (2026 calendar, market_session.py)
PRE_OPEN = datetime(2026, 9, 23, 9, 0, 0, tzinfo=TZ)
IN_MARKET = datetime(2026, 9, 23, 11, 0, 0, tzinfo=TZ)
WEEKEND = datetime(2026, 9, 26, 9, 0, 0, tzinfo=TZ)  # Saturday


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_members(core_count=30, rotation_count=50):
    members = []
    for i in range(1, core_count + 1):
        members.append(
            {
                "symbol": f"C{i:02d}",
                "category": "CORE",
                "rank": i,
                "selection_reason": f"core retention {i}",
            }
        )
    for i in range(1, rotation_count + 1):
        members.append(
            {
                "symbol": f"R{i:02d}",
                "category": "ROTATION",
                "rank": i,
                "selection_reason": f"rotation rank {i}",
            }
        )
    return members


def _make_scores(members):
    scores = {}
    for m in members:
        row = {"total_rotation_score": 60.0}
        for component, weight in ROTATION_SCORE_COMPONENT_WEIGHTS.items():
            row[component] = weight * 0.5
        scores[m["symbol"]] = row
    return scores


def _make_eligibility(members):
    return {
        m["symbol"]: {"eligibility_status": "ELIGIBLE", "data_quality_state": "SUFFICIENT"}
        for m in members
    }


async def _ctx(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'phase3.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = UniverseStateService(session_factory=factory)
    return service, engine, factory


async def _make_valid_version(service, *, version="w1", pool_version="pool-w1"):
    symbols = [f"S{i:02d}" for i in range(1, 81)]
    pool = await service.create_candidate_pool(
        pool_version, source="external_refresh", symbols=symbols
    )
    pool = await service.validate_candidate_pool(pool["id"])
    members = _make_members()
    draft = await service.create_universe_draft(
        pool["id"],
        version=version,
        members=members,
        core_count=30,
        rotation_count=50,
        total_count=80,
        calculated_at=datetime(2026, 9, 21, 18, 0),
        effective_from=datetime(2026, 9, 28, 9, 15),
        config_hash="cfg-abc-123",
        slot_scores=_make_scores(members),
        slot_eligibility=_make_eligibility(members),
    )
    return await service.validate_universe(draft["id"])


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# A. Schema + DB-level constraints
# ---------------------------------------------------------------------------
def test_a1_new_tables_and_partial_index_registered():
    tables = Base.metadata.tables
    for name in (
        "universe_candidate_pools",
        "universe_versions",
        "universe_members",
        "universe_member_scores",
        "universe_eligibility",
        "universe_events",
    ):
        assert name in tables, name
    uv = tables["universe_versions"]
    assert "ix_universe_versions_single_active" in {ix.name for ix in uv.indexes}


def test_a2_exactly_one_active_is_db_enforced(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        pool = await service.create_candidate_pool("pool-1", symbols=[])
        pool = await service.validate_candidate_pool(pool["id"])
        for vname in ("v1", "v2"):
            await service.create_universe_draft(
                pool["id"],
                version=vname,
                members=_make_members(),
                core_count=30,
                rotation_count=50,
                total_count=80,
                calculated_at=datetime(2026, 9, 21, 18, 0),
                effective_from=datetime(2026, 9, 28, 9, 15),
                config_hash="x",
            )
        # Force two ACTIVE rows directly — the partial unique index must reject.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE universe_versions SET status='ACTIVE' WHERE version='v1'")
                )
                await conn.execute(
                    text("UPDATE universe_versions SET status='ACTIVE' WHERE version='v2'")
                )
        await engine.dispose()

    _run(scenario())


def test_a3_membership_unique_per_universe_version(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        pool = await service.create_candidate_pool("pool-1", symbols=[])
        pool = await service.validate_candidate_pool(pool["id"])
        draft = await service.create_universe_draft(
            pool["id"],
            version="v1",
            members=_make_members(),
            core_count=30,
            rotation_count=50,
            total_count=80,
            calculated_at=datetime(2026, 9, 21, 18, 0),
            effective_from=datetime(2026, 9, 28, 9, 15),
            config_hash="x",
        )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO universe_members "
                        "(id, universe_version_id, symbol, category, rank) "
                        "VALUES (:id1, :uv, 'C01', 'CORE', 99)"
                    ),
                    {"id1": uuid4().hex[:16], "uv": draft["id"]},
                )
                await conn.execute(
                    text(
                        "INSERT INTO universe_members "
                        "(id, universe_version_id, symbol, category, rank) "
                        "VALUES (:id2, :uv, 'C01', 'CORE', 98)"
                    ),
                    {"id2": uuid4().hex[:16], "uv": draft["id"]},
                )
        await engine.dispose()

    _run(scenario())


def test_a4_legacy_defaults_preserved():
    assert settings.UNIVERSE_SOURCE == "legacy"
    assert settings.AUTO_APPROVE_IF_VALID is False
    assert settings.AUTO_APPROVE_AFTER_CYCLES >= 1  # TO BE CALIBRATED, present


# ---------------------------------------------------------------------------
# B. Candidate pool lifecycle + Q10 retain-on-failure
# ---------------------------------------------------------------------------
def test_b1_pool_lifecycle_create_validate_idempotent(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        pool = await service.create_candidate_pool(
            "pool-v1",
            source="external_refresh",
            source_reference="https://example.com/scratch",
            symbols=["A", "B", "C"],
            metadata={"count": 3},
        )
        assert pool["status"] == "DRAFT"
        assert pool["symbol_count"] == 3
        assert pool["source"] == "external_refresh"

        validated = await service.validate_candidate_pool(pool["id"])
        assert validated["status"] == "VALIDATED"
        assert validated["validation_status"] == "PASS"

        again = await service.validate_candidate_pool(pool["id"])  # idempotent
        assert again["status"] == "VALIDATED"

        types = [e["event_type"] for e in await service.list_events()]
        assert types.count("CANDIDATE_POOL_CREATED") == 1
        assert types.count("CANDIDATE_POOL_VALIDATED") == 1
        await engine.dispose()

    _run(scenario())


def test_b2_refresh_failure_retains_previous_validated_pool(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        pool = await service.create_candidate_pool(
            "pool-v1", source="external_refresh", symbols=["A", "B", "C"]
        )
        pool = await service.validate_candidate_pool(pool["id"])

        result = await service.record_pool_refresh_failed(
            previous_pool_id=pool["id"],
            source="external_refresh",
            reason="provider timeout",
        )
        assert result["retained_pool_id"] == pool["id"]

        # retained pool untouched: still VALIDATED, same symbol count
        still = await service.get_candidate_pool(pool["id"])
        assert still["status"] == "VALIDATED"
        assert still["symbol_count"] == 3
        assert still["validation_status"] == "PASS"

        types = [e["event_type"] for e in await service.list_events()]
        assert "CANDIDATE_POOL_REFRESH_FAILED" in types

        with pytest.raises(CandidatePoolVersionNotFound):
            await service.record_pool_refresh_failed(previous_pool_id="does-not-exist")
        await engine.dispose()

    _run(scenario())


def test_b3_pool_reject_is_terminal(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        pool = await service.create_candidate_pool("pool-v1", symbols=["A"])
        rejected = await service.reject_candidate_pool(pool["id"], reason="bad source")
        assert rejected["status"] == "REJECTED"
        with pytest.raises(UniverseStateTransitionError):
            await service.validate_candidate_pool(pool["id"])
        events = await service.list_events()
        assert "CANDIDATE_POOL_REJECTED" in [e["event_type"] for e in events]
        await engine.dispose()

    _run(scenario())


# ---------------------------------------------------------------------------
# C. Draft creation + validation gate
# ---------------------------------------------------------------------------
def test_c1_full_draft_snapshot_persisted_and_validated(tmp_path):
    async def scenario():
        service, engine, factory = await _ctx(tmp_path)
        pool = await service.create_candidate_pool("pool-v1", symbols=[f"S{i:02d}" for i in range(1, 81)])
        pool = await service.validate_candidate_pool(pool["id"])
        members = _make_members()
        draft = await service.create_universe_draft(
            pool["id"],
            version="w1",
            members=members,
            core_count=30,
            rotation_count=50,
            total_count=80,
            calculated_at=datetime(2026, 9, 21, 18, 0),
            effective_from=datetime(2026, 9, 28, 9, 15),
            config_hash="cfg-1",
            slot_scores=_make_scores(members),
            slot_eligibility=_make_eligibility(members),
        )
        assert draft["status"] == "DRAFT"
        assert draft["candidate_pool_version_id"] == pool["id"]

        # one consistent snapshot: version row + 80 members + 80 scores + 80 eligibility
        async with factory() as session:
            rows = await urepo.get_members(session, draft["id"])
            scores = await urepo.get_scores(session, draft["id"])
            elig = await urepo.get_eligibility(session, draft["id"])
            assert len(rows) == 80
            assert len(scores) == 80
            assert len(elig) == 80

        validated = await service.validate_universe(draft["id"])
        assert validated["status"] == "VALIDATED"
        assert validated["validated_at"] is not None
        assert validated["validation_result_ref"] is not None
        await engine.dispose()

    _run(scenario())


def test_c2_wrong_counts_rejected_with_all_errors(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        pool = await service.create_candidate_pool("pool-v1", symbols=[])
        pool = await service.validate_candidate_pool(pool["id"])
        members = _make_members(core_count=29)  # 29 CORE + 50 ROTATION = 79
        draft = await service.create_universe_draft(
            pool["id"],
            version="bad-counts",
            members=members,
            core_count=29,
            rotation_count=50,
            total_count=79,
            calculated_at=datetime(2026, 9, 21, 18, 0),
            effective_from=datetime(2026, 9, 28, 9, 15),
            config_hash="x",
            slot_scores=_make_scores(members),
            slot_eligibility=_make_eligibility(members),
        )
        with pytest.raises(UniverseValidationError) as exc_info:
            await service.validate_universe(draft["id"])
        error_text = " ".join(exc_info.value.errors)
        assert "CORE count must be exactly 30" in error_text
        assert "total count must be exactly 80" in error_text

        rejected = await service.get_universe_version(draft["id"])
        assert rejected["status"] == "REJECTED"
        assert rejected["rejection_reason"]
        types = [e["event_type"] for e in await service.list_events()]
        assert "UNIVERSE_VALIDATION_FAILED" in types
        await engine.dispose()

    _run(scenario())


def test_c3_duplicate_symbols_and_overlap_blocked_at_db(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        pool = await service.create_candidate_pool("pool-v1", symbols=[])
        pool = await service.validate_candidate_pool(pool["id"])

        # duplicate: rotation R06 replaced by R05 (two members, same symbol)
        members = _make_members()
        members[35]["symbol"] = "R05"
        with pytest.raises(IntegrityError):
            await service.create_universe_draft(
                pool["id"],
                version="bad-dup",
                members=members,
                core_count=30,
                rotation_count=50,
                total_count=80,
                calculated_at=datetime(2026, 9, 21, 18, 0),
                effective_from=datetime(2026, 9, 28, 9, 15),
                config_hash="x",
                slot_scores=_make_scores(members),
                slot_eligibility=_make_eligibility(members),
            )

        # overlap: rotation R10 (index 39) == CORE C10
        members2 = _make_members()
        members2[39]["symbol"] = "C10"
        with pytest.raises(IntegrityError):
            await service.create_universe_draft(
                pool["id"],
                version="bad-overlap",
                members=members2,
                core_count=30,
                rotation_count=50,
                total_count=80,
                calculated_at=datetime(2026, 9, 21, 18, 0),
                effective_from=datetime(2026, 9, 28, 9, 15),
                config_hash="x",
                slot_scores=_make_scores(members2),
                slot_eligibility=_make_eligibility(members2),
            )

        # atomicity: neither failed draft persisted (Q12 ss10/ss14)
        versions = await service.list_universe_versions()
        assert versions == []
        assert await service.get_active_universe() is None
        await engine.dispose()

    _run(scenario())


def test_c4_missing_scores_and_eligibility_rejected(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        pool = await service.create_candidate_pool("pool-v1", symbols=[])
        pool = await service.validate_candidate_pool(pool["id"])
        members = _make_members()
        draft = await service.create_universe_draft(
            pool["id"],
            version="no-scores",
            members=members,
            core_count=30,
            rotation_count=50,
            total_count=80,
            calculated_at=datetime(2026, 9, 21, 18, 0),
            effective_from=datetime(2026, 9, 28, 9, 15),
            config_hash="x",
            # no slot_scores / slot_eligibility on purpose
        )
        with pytest.raises(UniverseValidationError) as exc_info:
            await service.validate_universe(draft["id"])
        error_text = " ".join(exc_info.value.errors)
        assert "missing rotation score row" in error_text
        assert "missing eligibility result" in error_text
        await engine.dispose()

    _run(scenario())


def test_c5_unvalidated_candidate_pool_cannot_validate_draft(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        pool = await service.create_candidate_pool("pool-unvalidated", symbols=[])
        members = _make_members()
        draft = await service.create_universe_draft(
            pool["id"],  # still DRAFT — never validated
            version="bad-pool",
            members=members,
            core_count=30,
            rotation_count=50,
            total_count=80,
            calculated_at=datetime(2026, 9, 21, 18, 0),
            effective_from=datetime(2026, 9, 28, 9, 15),
            config_hash="x",
            slot_scores=_make_scores(members),
            slot_eligibility=_make_eligibility(members),
        )
        with pytest.raises(UniverseValidationError) as exc_info:
            await service.validate_universe(draft["id"])
        assert any("VALIDATED" in e for e in exc_info.value.errors)
        await engine.dispose()

    _run(scenario())


def test_c6_rejected_history_preserved(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        pool = await service.create_candidate_pool("pool-v1", symbols=[])
        pool = await service.validate_candidate_pool(pool["id"])
        for vname in ("r1", "r2"):
            members = _make_members(core_count=5)
            draft = await service.create_universe_draft(
                pool["id"],
                version=vname,
                members=members,
                core_count=5,
                rotation_count=50,
                total_count=55,
                calculated_at=datetime(2026, 9, 21, 18, 0),
                effective_from=datetime(2026, 9, 28, 9, 15),
                config_hash="x",
            )
            with pytest.raises(UniverseValidationError):
                await service.validate_universe(draft["id"])
        versions = await service.list_universe_versions()
        assert {v["version"] for v in versions} == {"r1", "r2"}
        assert all(v["status"] == "REJECTED" for v in versions)  # never purged
        await engine.dispose()

    _run(scenario())


# ---------------------------------------------------------------------------
# D. State machine / approval evidence
# ---------------------------------------------------------------------------
def test_d1_full_lifecycle_to_superseded(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        v1 = await _make_valid_version(service, version="w1", pool_version="pool-w1")
        assert v1["status"] == "VALIDATED"

        approved = await service.approve_universe(v1["id"], actor="user-1", reason="reviewed manually")
        assert approved["status"] == "APPROVED"
        assert approved["approval_actor"] == "user-1"
        assert approved["approval_actor_type"] == "USER"
        assert approved["approval_policy_version"] == "q13-hybrid.v1"
        assert approved["approved_at"] is not None

        active = await service.activate_universe(v1["id"], as_of=PRE_OPEN)
        assert active["status"] == "ACTIVE"
        assert active["activated_at"] is not None

        v2 = await _make_valid_version(service, version="w2", pool_version="pool-w2")
        await service.approve_universe(v2["id"], actor="user-1", reason="next cycle")
        await service.activate_universe(v2["id"], as_of=PRE_OPEN)

        v1_after = await service.get_universe_version(v1["id"])
        assert v1_after["status"] == "SUPERSEDED"
        assert v1_after["superseded_at"] is not None
        details = await service.get_active_universe(with_detail=True)
        assert details["id"] == v2["id"]
        assert len(details["members"]) == 80
        assert {m["category"] for m in details["members"]} == {"CORE", "ROTATION"}
        await engine.dispose()

    _run(scenario())


def test_d2_validated_but_unapproved_never_flips_active(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        v1 = await _make_valid_version(service, version="w1", pool_version="pool-w1")
        assert await service.get_active_universe() is None  # nothing active yet

        with pytest.raises(UniverseStateTransitionError):
            await service.activate_universe(v1["id"], as_of=PRE_OPEN)
        assert await service.get_active_universe() is None
        assert (await service.get_universe_version(v1["id"]))["status"] == "VALIDATED"

        # missing approval keeps the CURRENT active (KEEP CURRENT ACTIVE)
        active = await service.activate_universe(
            (await service.approve_universe(v1["id"], actor="user-1"))["id"],
            as_of=PRE_OPEN,
        )
        assert active["status"] == "ACTIVE"
        v2 = await _make_valid_version(service, version="w2", pool_version="pool-w2")
        # v2 validated but NOT approved
        with pytest.raises(UniverseStateTransitionError):
            await service.activate_universe(v2["id"], as_of=PRE_OPEN)
        still = await service.get_active_universe()
        assert still["id"] == v1["id"]
        await engine.dispose()

    _run(scenario())


def test_d3_approval_requires_validated(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        pool = await service.create_candidate_pool("pool-v1", symbols=[])
        pool = await service.validate_candidate_pool(pool["id"])
        draft = await service.create_universe_draft(
            pool["id"],
            version="w1",
            members=_make_members(),
            core_count=30,
            rotation_count=50,
            total_count=80,
            calculated_at=datetime(2026, 9, 21, 18, 0),
            effective_from=datetime(2026, 9, 28, 9, 15),
            config_hash="x",
            slot_scores=_make_scores(_make_members()),
            slot_eligibility=_make_eligibility(_make_members()),
        )
        with pytest.raises(UniverseStateTransitionError):
            await service.approve_universe(draft["id"], actor="u1")  # DRAFT
        validated = await service.validate_universe(draft["id"])
        approved = await service.approve_universe(validated["id"], actor="u1")
        again = await service.approve_universe(approved["id"], actor="u1")  # idempotent
        assert again["status"] == "APPROVED"
        await engine.dispose()

    _run(scenario())


def test_d4_forbidden_transitions_leave_state_unchanged(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        v1 = await _make_valid_version(service, version="w1", pool_version="pool-w1")
        v1 = await service.approve_universe(v1["id"], actor="u1")
        await service.activate_universe(v1["id"], as_of=PRE_OPEN)

        # ACTIVE is terminal: cannot reject, cannot validate, cannot approve
        with pytest.raises(UniverseStateTransitionError):
            await service.reject_universe(v1["id"], actor="u1")
        with pytest.raises(UniverseStateTransitionError):
            await service.validate_universe(v1["id"])
        with pytest.raises(UniverseStateTransitionError):
            await service.approve_universe(v1["id"], actor="u1")
        assert (await service.get_universe_version(v1["id"]))["status"] == "ACTIVE"
        # idempotent re-activation of the SAME version returns current ACTIVE
        same = await service.activate_universe(v1["id"], as_of=PRE_OPEN)
        assert same["status"] == "ACTIVE"

        # a REJECTED version is terminal too
        pool = await service.create_candidate_pool("pool-v2", symbols=[])
        pool = await service.validate_candidate_pool(pool["id"])
        bad = await service.create_universe_draft(
            pool["id"],
            version="bad",
            members=_make_members(core_count=2),
            core_count=2,
            rotation_count=50,
            total_count=52,
            calculated_at=datetime(2026, 9, 21, 18, 0),
            effective_from=datetime(2026, 9, 28, 9, 15),
            config_hash="x",
        )
        with pytest.raises(UniverseValidationError):
            await service.validate_universe(bad["id"])
        with pytest.raises(UniverseStateTransitionError):
            await service.activate_universe(bad["id"], as_of=PRE_OPEN)
        await engine.dispose()

    _run(scenario())


def test_d5_failed_activation_preserves_current_active(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        v1 = await _make_valid_version(service, version="w1", pool_version="pool-w1")
        v1 = await service.approve_universe(v1["id"], actor="u1")
        await service.activate_universe(v1["id"], as_of=PRE_OPEN)

        v2 = await _make_valid_version(service, version="w2", pool_version="pool-w2")
        # v2 APPROVED but we force a stale/racing flip: approve then raw-demote v2
        await service.approve_universe(v2["id"], actor="u1")
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE universe_versions SET status='DRAFT' WHERE version='w2'")
            )
        with pytest.raises(UniverseStateTransitionError):
            await service.activate_universe(v2["id"], as_of=PRE_OPEN)

        still = await service.get_active_universe()
        assert still["id"] == v1["id"]  # KEEP CURRENT ACTIVE
        assert (await service.get_universe_version(v2["id"]))["status"] == "DRAFT"
        types = [e["event_type"] for e in await service.list_events()]
        assert "STATE_TRANSITION_DENIED" in types  # refusal is audited
        assert types.count("UNIVERSE_ACTIVATED") == 1  # only v1 ever activated
        await engine.dispose()

    _run(scenario())


# ---------------------------------------------------------------------------
# E. Market-hours guard
# ---------------------------------------------------------------------------
def test_e1_activation_refused_during_market_hours(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        v1 = await _make_valid_version(service, version="w1", pool_version="pool-w1")
        v1 = await service.approve_universe(v1["id"], actor="u1")
        with pytest.raises(UniverseMarketHoursError):
            await service.activate_universe(v1["id"], as_of=IN_MARKET)
        assert await service.get_active_universe() is None
        await engine.dispose()

    _run(scenario())


def test_e2_activation_refused_on_weekend(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        v1 = await _make_valid_version(service, version="w1", pool_version="pool-w1")
        v1 = await service.approve_universe(v1["id"], actor="u1")
        with pytest.raises(UniverseMarketHoursError):
            await service.activate_universe(v1["id"], as_of=WEEKEND)
        assert await service.get_active_universe() is None
        await engine.dispose()

    _run(scenario())


def test_e3_rollback_refused_during_market_hours(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        v1 = await _make_valid_version(service, version="w1", pool_version="pool-w1")
        await service.activate_universe(
            (await service.approve_universe(v1["id"], actor="u1"))["id"],
            as_of=PRE_OPEN,
        )
        with pytest.raises(UniverseMarketHoursError):
            await service.rollback_to(v1["id"], as_of=IN_MARKET, actor="admin-1")
        # current ACTIVE untouched
        assert (await service.get_active_universe())["id"] == v1["id"]
        await engine.dispose()

    _run(scenario())


# ---------------------------------------------------------------------------
# F. Rollback
# ---------------------------------------------------------------------------
def test_f1_rollback_to_previous_valid_version(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        v1 = await _make_valid_version(service, version="w1", pool_version="pool-w1")
        await service.activate_universe(
            (await service.approve_universe(v1["id"], actor="u1", reason="ok"))["id"],
            as_of=PRE_OPEN,
        )
        v2 = await _make_valid_version(service, version="w2", pool_version="pool-w2")
        await service.activate_universe(
            (await service.approve_universe(v2["id"], actor="u1", reason="ok"))["id"],
            as_of=PRE_OPEN,
        )
        assert (await service.get_active_universe())["id"] == v2["id"]

        rolled = await service.rollback_to(
            v1["id"], as_of=PRE_OPEN, actor="admin-1", reason="w2 bad batch"
        )
        assert rolled["status"] == "ACTIVE"
        assert (await service.get_active_universe())["id"] == v1["id"]
        assert (await service.get_universe_version(v2["id"]))["status"] == "SUPERSEDED"

        # roll back forward again — v2 (SUPERSEDED) is a valid historical target
        rolled2 = await service.rollback_to(v2["id"], as_of=PRE_OPEN, actor="admin-1")
        assert rolled2["status"] == "ACTIVE"
        assert (await service.get_active_universe())["id"] == v2["id"]

        types = [e["event_type"] for e in await service.list_events()]
        assert types.count("UNIVERSE_ROLLBACK_REQUESTED") == 2
        assert types.count("UNIVERSE_ROLLED_BACK") == 2

        # exactly one ACTIVE at every point: current active id is unambiguous
        active = await service.get_active_universe()
        assert active["id"] == v2["id"]
        await engine.dispose()

    _run(scenario())


def test_f2_rollback_never_targets_draft_or_rejected(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        v1 = await _make_valid_version(service, version="w1", pool_version="pool-w1")
        await service.activate_universe(
            (await service.approve_universe(v1["id"], actor="u1"))["id"],
            as_of=PRE_OPEN,
        )
        # draft target
        pool = await service.create_candidate_pool("pool-x", symbols=[])
        pool = await service.validate_candidate_pool(pool["id"])
        draft = await service.create_universe_draft(
            pool["id"],
            version="draft-x",
            members=_make_members(),
            core_count=30,
            rotation_count=50,
            total_count=80,
            calculated_at=datetime(2026, 9, 21, 18, 0),
            effective_from=datetime(2026, 9, 28, 9, 15),
            config_hash="x",
        )
        with pytest.raises(UniverseStateTransitionError):
            await service.rollback_to(draft["id"], as_of=PRE_OPEN, actor="admin-1")

        # rejected target
        bad_pool = await service.create_candidate_pool("pool-bad", symbols=[])
        bad_pool = await service.validate_candidate_pool(bad_pool["id"])
        bad = await service.create_universe_draft(
            bad_pool["id"],
            version="bad-x",
            members=_make_members(core_count=1),
            core_count=1,
            rotation_count=50,
            total_count=51,
            calculated_at=datetime(2026, 9, 21, 18, 0),
            effective_from=datetime(2026, 9, 28, 9, 15),
            config_hash="x",
        )
        with pytest.raises(UniverseValidationError):
            await service.validate_universe(bad["id"])
        with pytest.raises(UniverseStateTransitionError):
            await service.rollback_to(bad["id"], as_of=PRE_OPEN, actor="admin-1")

        assert (await service.get_active_universe())["id"] == v1["id"]
        await engine.dispose()

    _run(scenario())


def test_f3_rollback_target_is_revalidated(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        v1 = await _make_valid_version(service, version="w1", pool_version="pool-w1")
        await service.activate_universe(
            (await service.approve_universe(v1["id"], actor="u1"))["id"],
            as_of=PRE_OPEN,
        )
        v2 = await _make_valid_version(service, version="w2", pool_version="pool-w2")
        await service.activate_universe(
            (await service.approve_universe(v2["id"], actor="u1"))["id"],
            as_of=PRE_OPEN,
        )
        # corrupt v1's membership count -> revalidation must refuse the rollback
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM universe_members "
                    "WHERE universe_version_id = :vid AND symbol = 'R50'"
                ),
                {"vid": v1["id"]},
            )
        with pytest.raises(UniverseValidationError):
            await service.rollback_to(v1["id"], as_of=PRE_OPEN, actor="admin-1")
        assert (await service.get_active_universe())["id"] == v2["id"]  # unchanged
        await engine.dispose()

    _run(scenario())


# ---------------------------------------------------------------------------
# G. Append-only events
# ---------------------------------------------------------------------------
def test_g1_events_append_only_audit_trail(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        v1 = await _make_valid_version(service, version="w1", pool_version="pool-w1")
        await service.approve_universe(v1["id"], actor="u1", reason="first approval")
        await service.activate_universe(v1["id"], as_of=PRE_OPEN)
        events = await service.list_events()

        types = [e["event_type"] for e in events]
        assert types.count("CANDIDATE_POOL_CREATED") == 1
        assert types.count("CANDIDATE_POOL_VALIDATED") == 1
        assert types.count("UNIVERSE_DRAFT_CREATED") == 1
        assert types.count("UNIVERSE_VALIDATED") == 1
        assert types.count("UNIVERSE_APPROVED") == 1
        assert types.count("UNIVERSE_ACTIVATION_REQUESTED") == 1
        assert types.count("UNIVERSE_ACTIVATED") == 1

        approved = next(e for e in events if e["event_type"] == "UNIVERSE_APPROVED")
        meta = json.loads(approved["metadata_json"])
        assert meta["approval_mode"] == "manual"
        assert approved["candidate_pool_version_id"]  # Q10 provenance on evidence
        assert approved["actor"] == "u1"

        # events carry (universe_version_id, candidate_pool_version_id) linkage
        created = next(e for e in events if e["event_type"] == "UNIVERSE_DRAFT_CREATED")
        assert created["universe_version_id"] == v1["id"]
        assert created["candidate_pool_version_id"]
        await engine.dispose()

    _run(scenario())


def test_g2_events_grow_never_mutate(tmp_path):
    async def scenario():
        service, engine, _ = await _ctx(tmp_path)
        v1 = await _make_valid_version(service, version="w1", pool_version="pool-w1")
        await service.approve_universe(v1["id"], actor="u1")
        await service.activate_universe(v1["id"], as_of=PRE_OPEN)
        await service.record_pool_refresh_failed(previous_pool_id=None, reason="probe")
        events = await service.list_events()
        first_ids = [e["id"] for e in events]
        assert len(set(first_ids)) == len(first_ids)  # unique event ids
        # each event row keeps a stable event_type / no edits after append:
        for e in events:
            assert e["event_type"] in {
                "CANDIDATE_POOL_CREATED",
                "CANDIDATE_POOL_VALIDATED",
                "UNIVERSE_DRAFT_CREATED",
                "UNIVERSE_VALIDATED",
                "UNIVERSE_APPROVED",
                "UNIVERSE_ACTIVATION_REQUESTED",
                "UNIVERSE_ACTIVATED",
                "CANDIDATE_POOL_REFRESH_FAILED",
            }
        await engine.dispose()

    _run(scenario())


# ---------------------------------------------------------------------------
# H. Restart survival (Q12 ss16)
# ---------------------------------------------------------------------------
def test_h1_active_universe_survives_reconnect(tmp_path):
    async def scenario():
        service, engine, factory = await _ctx(tmp_path)
        v1 = await _make_valid_version(service, version="w1", pool_version="pool-w1")
        await service.activate_universe(
            (await service.approve_universe(v1["id"], actor="u1"))["id"],
            as_of=PRE_OPEN,
        )
        await engine.dispose()

        # simulate app restart: reopen the SAME file with a fresh engine
        engine2 = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'phase3.db'}")
        factory2 = async_sessionmaker(engine2, expire_on_commit=False)
        service2 = UniverseStateService(session_factory=factory2)
        active = await service2.get_active_universe(with_detail=False)
        assert active["id"] == v1["id"]
        assert active["status"] == "ACTIVE"
        await engine2.dispose()

    _run(scenario())