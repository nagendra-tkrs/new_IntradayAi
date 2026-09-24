"""PHASE 5B — Candidate Pool Resolution & Expansion tests.

Scope (maps to Phase 5B §19 requirements):

  * A Source aggregation — multiple sources combine; attribution retained;
    duplicates deduplicated; invalid symbols rejected; fail-safe on fetch.
  * B Normalization — equivalent symbol forms resolve to one canonical symbol.
  * C Target validation — 150/200/250 boundaries; <150 → INSUFFICIENT_SOURCE;
    >250 → configured policy (reported, NEVER silently truncated).
  * D Provider aliases — canonical → provider mapping WITHOUT changing
    canonical identity; wire boundary uses the alias; pool keeps canonical.
  * E Refresh failure — a failed refresh does NOT erase the previous validated
    pool, does NOT empty the active pool, and records a refresh-failure event.
  * F Versioning — identical snapshot → same pool hash/version; changed
    snapshot → new version.
  * G Determinism — two runs against the same input produce identical order,
    hash, counts, and source attribution (also source-order independent).
  * H Regression — UNIVERSE_SOURCE="legacy", ROTATION_ENGINE_ENABLED=False,
    NIFTY50 still 40 symbols, ATR_MIN_PCT still 0.0008, NSE_UNIVERSES and the
    scanner surface untouched.
  * I Persistence bridge — ``create_pool_from_resolution`` stores a VALIDATED
    Phase 3 pool with attribution metadata that the Rotation Engine consumes by
    pool_id (candidate pool stays separate from the active universe).

All fixtures are SYNTHETIC (used to prove resolver mechanics only); the real
repo universe (64) is asserted because it is static repository data. The real
NSE-archive measurement (union = 207) is covered in the Phase 5B dry-run, not in
unit tests — no network in tests.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import Base
from app.repositories import universe_repository as urepo
from app.services import candidate_pool as cpool
from app.services import provider_aliases as paliases
from app.services.candidate_pool import CandidateSource, SourceFetch
from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider, NSE_UNIVERSES
from app.services.rotation_config import RotationEngineConfig
from app.services.universe_rotation_engine import UniverseRotationEngine
from app.services.universe_state_service import UniverseStateService

CALC = datetime(2026, 9, 23, 18, 0)  # naive UTC — pinned as-of date


def _src(key, kind="file", rank=3, version="v0"):
    return CandidateSource(key=key, label=f"src:{key}", kind=kind, rank=rank, version=version)


def _fetcher(*symbols, version="v0"):
    return lambda: SourceFetch(symbols=tuple(symbols), version=version)


def _resolve(sources, fetchers, **kw):
    return cpool.resolve_candidate_pool(sources, fetchers, calc_dt=CALC, **kw)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# A — source aggregation
# ---------------------------------------------------------------------------
def test_a1_multiple_sources_combine_with_attribution():
    repo = _src("NSE_UNIVERSES", kind="repository", rank=0)
    file = _src("CONFIGURED_FILE", rank=3)
    res = _resolve(
        [repo, file],
        {"NSE_UNIVERSES": _fetcher("RELIANCE", "TCS"), "CONFIGURED_FILE": _fetcher("TCS", "HDFCBANK")},
        pool_min=1, pool_target=2, pool_max=5,
    )
    assert res.symbols == ["RELIANCE", "TCS", "HDFCBANK"]  # ordered by (rank, canonical)
    assert res.attribution["TCS"] == ["NSE_UNIVERSES", "CONFIGURED_FILE"]  # both retained
    assert res.attribution["RELIANCE"] == ["NSE_UNIVERSES"]
    assert res.attribution["HDFCBANK"] == ["CONFIGURED_FILE"]
    assert res.stats["raw_count"] == 4
    assert res.stats["final_count"] == 3
    assert res.target_status == "PASS"


def test_a2_duplicates_deduped_and_counted():
    file1 = _src("F1", rank=1)
    file2 = _src("F2", rank=2)
    res = _resolve(
        [file1, file2],
        {"F1": _fetcher("ABC", "ABC", "DEF"), "F2": _fetcher("ABC")},
        pool_min=1, pool_target=2, pool_max=5,
    )
    assert res.symbols == ["ABC", "DEF"]
    assert res.stats["duplicate_count"] == 2  # 1 intra-source + 1 cross-source
    assert res.stats["normalized_count"] == 2


def test_a3_invalid_symbols_rejected_and_source_failures_recorded():
    repo = _src("NSE_UNIVERSES", kind="repository", rank=0)
    file = _src("F1", rank=1)

    def boom():
        raise RuntimeError("network down")

    res = _resolve(
        [repo, file],
        {"NSE_UNIVERSES": boom, "F1": _fetcher("VALID", "!!bad!!", "", "  ")},
        pool_min=1, pool_target=1, pool_max=5,
    )
    assert res.symbols == ["VALID"]  # invalid/empty excluded, never fabricated
    assert res.stats["invalid_count"] == 3
    assert len(res.failures) == 1
    assert res.failures[0]["source"] == "NSE_UNIVERSES"
    assert "network down" in res.failures[0]["error"]
    assert res.target_status == "PASS"


# ---------------------------------------------------------------------------
# B — normalization
# ---------------------------------------------------------------------------
def test_b1_equivalent_forms_resolve_to_one_canonical():
    src = _src("F1", rank=1)
    res = _resolve(
        [src],
        {"F1": _fetcher("BAJAJFINANCE", "bajajfinance.ns", "NSE:BAJAJFINANCE", "NS:BAJAJFINANCE")},
        pool_min=1, pool_target=1, pool_max=5,
    )
    assert res.symbols == ["BAJAJFINANCE"]
    assert res.stats["duplicate_count"] == 3
    assert res.stats["final_count"] == 1
    # canonical identity is never the .NS / NSE:-prefixed form
    assert all(not s.endswith(".NS") and ":" not in s for s in res.symbols)


# ---------------------------------------------------------------------------
# C — target validation
# ---------------------------------------------------------------------------
def _bulk(n):
    src = _src("BULK", rank=1)
    return _resolve([src], {"BULK": _fetcher(*[f"T{i:04d}" for i in range(1, n + 1)])},
                    pool_min=150, pool_target=200, pool_max=250)


def test_c1_target_boundaries():
    cases = [(150, "PASS"), (200, "PASS"), (250, "PASS"), (149, "INSUFFICIENT_SOURCE")]
    for n, expected in cases:
        assert _bulk(n).target_status == expected, f"N={n} expected {expected}"
    over = _bulk(251)
    assert over.target_status == "OVER_TARGET_POLICY"
    assert len(over.symbols) == 251  # reported, NEVER silently truncated
    assert over.target["final_count"] == 251


# ---------------------------------------------------------------------------
# D — provider aliases (canonical identity preserved)
# ---------------------------------------------------------------------------
def test_d1_alias_resolution_preserves_canonical_identity():
    assert paliases.PROVIDER_ALIAS_VERSION == "phase5b.v1"
    assert paliases.provider_symbol("BAJAJFINANCE") == "BAJFINANCE"
    assert paliases.provider_symbol("COLPALPH") == "COLPAL"
    # pass-through for symbols with no alias; canonical form untouched
    assert paliases.provider_symbol("RELIANCE") == "RELIANCE"
    assert paliases.provider_symbol("bajajfinance") == "BAJFINANCE"
    # the map itself never rewrites the canonical base
    assert "BAJAJFINANCE" in paliases.PROVIDER_SYMBOL_ALIASES
    assert paliases.PROVIDER_SYMBOL_ALIASES["BAJAJFINANCE"] != "BAJAJFINANCE"


def test_d2_wire_boundary_uses_alias_but_pool_keeps_canonical():
    p = YFinanceMarketDataProvider()
    assert p._ns_symbol("COLPALPH") == "COLPAL.NS"
    assert p._ns_symbol("BAJAJFINANCE") == "BAJFINANCE.NS"
    assert p._ns_symbol("RELIANCE") == "RELIANCE.NS"
    assert p._ns_symbol("^NSEI") == "^NSEI"  # indices pass through
    # alias is NOT applied to the candidate pool layer (canonical stays)
    src = _src("F1", rank=1)
    res = _resolve([src], {"F1": _fetcher("BAJAJFINANCE", "COLPALPH")},
                   pool_min=1, pool_target=2, pool_max=5)
    assert res.symbols == ["BAJAJFINANCE", "COLPALPH"]
    assert res.attribution == {"BAJAJFINANCE": ["F1"], "COLPALPH": ["F1"]}


# ---------------------------------------------------------------------------
# E — refresh failure (safe retain-on-failure)
# ---------------------------------------------------------------------------
async def _ctx(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'phase5b.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return UniverseStateService(session_factory=factory), engine


def test_e1_failed_refresh_preserves_previous_validated_pool(tmp_path):
    async def scenario():
        service, engine = await _ctx(tmp_path)
        # previous validated pool (Phase 3 model — the thing that must survive)
        prev = await service.create_candidate_pool(
            "pool-20260923-abcdef12", source="local", symbols=["RELIANCE", "TCS"]
        )
        prev = await service.validate_candidate_pool(prev["id"])

        # refresh cycle: external source fails, repo succeeds → non-empty pool
        repo = _src("NSE_UNIVERSES", kind="repository", rank=0)
        ext = _src("NIFTY200", kind="external", rank=1)

        def boom():
            raise RuntimeError("archive unreachable")

        res = _resolve([repo, ext],
                       {"NSE_UNIVERSES": _fetcher("RELIANCE", "TCS"), "NIFTY200": boom},
                       pool_min=1, pool_target=2, pool_max=5)
        assert res.symbols == ["RELIANCE", "TCS"]
        assert len(res.failures) == 1 and res.failures[0]["source"] == "NIFTY200"

        # failed refresh is explicitly recorded as a Phase 3 event
        record = await service.record_candidate_pool_refresh_failure(
            source="NIFTY200",
            error="archive unreachable",
            previous_pool_version=prev["version"],
        )
        assert record["recorded"] is True

        # the previously validated pool is still there and VALIDATED
        after = await service.get_candidate_pool_by_version(prev["version"], "local")
        assert after is not None and after["status"] == "VALIDATED"

        # the failure event exists in the append-only log
        async with service._session() as session:
            events = await urepo.list_events(session)
        assert any(
            e.event_type == "CANDIDATE_POOL_REFRESH_FAILED" and
            e.reason == "archive unreachable"
            for e in events
        )
        await engine.dispose()

    _run_async(scenario())


def test_e2_total_refresh_failure_never_fabricates_a_pool():
    a = _src("A", rank=1)
    b = _src("B", rank=2)

    def boom1():
        raise RuntimeError("A down")

    def boom2():
        raise RuntimeError("B down")

    res = _resolve([a, b], {"A": boom1, "B": boom2},
                   pool_min=150, pool_target=200, pool_max=250)
    assert res.symbols == []
    assert res.target_status == "INSUFFICIENT_SOURCE"
    assert len(res.failures) == 2
    assert res.target["final_count"] == 0


# ---------------------------------------------------------------------------
# F — versioning (deterministic, same-snapshot idempotent)
# ---------------------------------------------------------------------------
def test_f1_identical_snapshot_same_version_and_pool_hash():
    src = _src("NSE_UNIVERSES", kind="repository", rank=0)
    fetchers = {"NSE_UNIVERSES": _fetcher("RELIANCE", "TCS", "MARUTI")}
    r1 = _resolve([src], fetchers, pool_min=1, pool_target=3, pool_max=5)
    r2 = _resolve([src], fetchers, pool_min=1, pool_target=3, pool_max=5)
    assert r1.version == r2.version
    assert r1.pool_hash == r2.pool_hash
    # same convention as the Phase 4 engine
    assert r1.version == cpool.pool_version(CALC, r1.symbols)
    assert r1.pool_hash == cpool.compute_pool_hash(r1.symbols)
    assert r1.version.startswith("pool-20260923-") and len(r1.version.rsplit("-", 1)[1]) == 8


def test_f2_changed_snapshot_produces_new_version():
    src = _src("F1", rank=1)
    base = _resolve([src], {"F1": _fetcher("RELIANCE", "TCS")},
                    pool_min=1, pool_target=2, pool_max=5)
    grown = _resolve([src], {"F1": _fetcher("RELIANCE", "TCS", "HDFCBANK")},
                     pool_min=1, pool_target=3, pool_max=5)
    assert grown.pool_hash != base.pool_hash
    assert grown.version != base.version


# ---------------------------------------------------------------------------
# G — determinism
# ---------------------------------------------------------------------------
def test_g1_two_runs_produce_identical_pool_and_sources_shuffled():
    sources = [
        _src("NSE_UNIVERSES", kind="repository", rank=0, version="phase3-static"),
        _src("NIFTY200", kind="external", rank=1, version="20260923"),
        _src("CONFIGURED_FILE", kind="file", rank=3, version="v9"),
    ]
    fetchers = {
        "NSE_UNIVERSES": _fetcher(*[f"R{i:03d}" for i in range(1, 20)], version="phase3-static"),
        "NIFTY200": _fetcher(*([f"R{i:03d}" for i in range(1, 20)] + [f"N{i:03d}" for i in range(200)]), version="20260923"),
        "CONFIGURED_FILE": _fetcher("CUSTOM", "CUSTOM2", version="v9"),
    }
    r1 = _resolve(sources, fetchers, pool_min=1, pool_target=50, pool_max=300)
    # same inputs again — byte-for-byte identical output
    r2 = _resolve(sources, fetchers, pool_min=1, pool_target=50, pool_max=300)
    assert r1.symbols == r2.symbols
    assert r1.pool_hash == r2.pool_hash
    assert r1.attribution == r2.attribution
    assert r1.source_versions == r2.source_versions
    assert r1.stats == r2.stats
    # source-insertion order must not matter (ordering is by rank+key)
    r3 = _resolve(list(reversed(sources)), fetchers, pool_min=1, pool_target=50, pool_max=300)
    assert r3.symbols == r1.symbols
    assert r3.pool_hash == r1.pool_hash
    assert r3.attribution == r1.attribution


# ---------------------------------------------------------------------------
# H — regression (nothing activated, scanner/calibration untouched)
# ---------------------------------------------------------------------------
def test_h1_phase5b_safety_flags_unchanged():
    assert settings.UNIVERSE_SOURCE == "legacy"
    assert settings.ROTATION_ENGINE_ENABLED is False
    assert len(NSE_UNIVERSES["NIFTY50"]) == 40
    assert settings.ATR_MIN_PCT == 0.0008          # Phase 5A calibration preserved
    assert settings.CANDIDATE_POOL_MIN == 150
    assert settings.CANDIDATE_POOL_TARGET == 200
    assert settings.CANDIDATE_POOL_MAX == 250
    cfg = RotationEngineConfig.from_settings()
    assert cfg.atr_min_pct == 0.0008
    assert cfg.candidate_pool_target == 200
    # to_dict exposes the new target on the config_hash surface
    assert cfg.to_dict()["candidate_pool_target"] == 200


def test_h2_real_repo_union_64_and_archive_shape_reaches_target():
    # the repository's real universe data (static repo truth) — still 64 unique
    per_source = {name: {cpool.normalize_candidate_symbol(s) for s in syms}
                  for name, syms in NSE_UNIVERSES.items()}
    union = sorted(per_source["NIFTY50"] | per_source["NIFTY100"] | per_source["BANKNIFTY"])
    assert len(union) == 64

    # representative external source mirrors the REAL NSE-archive measurement:
    # 200 symbols, 57 overlapping the repo pool, 143 new → union 207 (fits target)
    repo_symbols = union
    new_symbols = sorted({f"ZZ{i:04d}" for i in range(1, 144)})
    ext = sorted(set(new_symbols) | set(repo_symbols[:57]))
    assert len(ext) == 200 and len(set(ext) & set(repo_symbols)) == 57

    repo_src = _src("NSE_UNIVERSES", kind="repository", rank=0)
    ext_src = _src("NIFTY200", kind="external", rank=1)
    res = _resolve(
        [repo_src, ext_src],
        {"NSE_UNIVERSES": _fetcher(*repo_symbols, version="phase3-static"),
         "NIFTY200": _fetcher(*ext, version="20260923")},
    )
    assert res.target_status == "PASS"
    assert res.stats["final_count"] == 207
    assert 150 <= res.stats["final_count"] <= 250
    # attribution: overlaps carry BOTH sources; new symbols carry NIFTY200 only
    assert res.attribution[repo_symbols[0]] == ["NSE_UNIVERSES", "NIFTY200"]
    assert res.attribution[new_symbols[0]] == ["NIFTY200"]
    assert all(not s.endswith(".NS") for s in res.symbols)  # canonical everywhere


# ---------------------------------------------------------------------------
# I — persistence bridge (Phase 3 pool consumed by the engine, not activated)
# ---------------------------------------------------------------------------
def test_i1_pool_from_resolution_is_validated_and_consumable_separate_from_active(tmp_path):
    async def scenario():
        service, engine = await _ctx(tmp_path)
        repo = _src("NSE_UNIVERSES", kind="repository", rank=0)
        file = _src("F1", rank=1)
        res = _resolve(
            [repo, file],
            {"NSE_UNIVERSES": _fetcher("RELIANCE"), "F1": _fetcher("TCS", "HDFCBANK")},
            pool_min=1, pool_target=3, pool_max=5,
        )
        pool = await cpool.create_pool_from_resolution(
            res, service, source="phase5b-test", reason="test bridge"
        )
        assert pool["status"] == "VALIDATED"
        assert pool["symbol_count"] == 3
        meta = json.loads(pool["metadata_json"])
        assert meta["symbols"] == ["RELIANCE", "HDFCBANK", "TCS"]  # (min-rank, canonical)
        assert meta["source_attribution"]["TCS"] == {"sources": ["F1"]}
        assert meta["source_attribution"]["RELIANCE"] == {"sources": ["NSE_UNIVERSES"]}
        assert meta["pool_hash"] == res.pool_hash

        # the rotation engine consumes the pool by pool_id (candidate pool → engine input)
        provider = _EmptyProvider()
        cfg = RotationEngineConfig(min_observation_sessions=10)
        engine_obj = UniverseRotationEngine(provider=provider, config=cfg, state_service=service)
        result = await engine_obj.generate_draft(pool_id=pool["id"], calculated_at=CALC)
        assert result["status"] in ("INSUFFICIENT_CANDIDATES", "DRAFT_CREATED")
        # an 80-stock universe can never be produced from a 3-symbol pool
        assert result["pool"]["id"] == pool["id"]
        assert result["insufficiency"] is not None or result["counts"]["eligible"] <= 3
        await engine.dispose()

    from app.services.market_data.base import MarketDataProvider

    class _EmptyProvider(MarketDataProvider):
        @property
        def data_source_label(self):
            return "EMPTY"

        async def get_quote(self, symbol):
            return {"symbol": symbol, "price": 0.0}

        async def get_ohlcv(self, symbol, timeframe="5m", days=5):
            import pandas as pd
            return pd.DataFrame()

        async def get_intraday_bars(self, symbol, timeframe="5m"):
            import pandas as pd
            return pd.DataFrame()

        async def get_market_status(self):
            return {"status": "closed"}

        async def get_instruments(self, universe="NIFTY50"):
            return []

        async def get_market_index(self, index_name="NIFTY"):
            return {}

    _run_async(scenario())


# ---------------------------------------------------------------------------
def _run_async(coro):
    return asyncio.run(coro)