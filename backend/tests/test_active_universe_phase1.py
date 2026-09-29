"""Phase 1 — active live universe (rotation_80) tests.

Covers the Phase 1 contract:

* Universe resolver: the ACTIVE 80/30/50 universe is the single source of truth
  (``get_active_trading_universe``); FAIL CLOSED (``ACTIVE_UNIVERSE_UNAVAILABLE``)
  when no ACTIVE version exists or it violates the shape gate (total 80 /
  core 30 / rotation 50 / 0 duplicates / no CORE-ROTATION overlap / valid NSE
  symbols) — it never silently falls back to the 40-stock NIFTY50 universe.
* API: ``/api/stocks`` returns 80 with CORE/ROTATION classification;
  ``/api/scanner`` returns ``total_scanned = 80`` and processes ALL 80 symbols.
* Cache: ``yfinance_provider.get_instruments`` is keyed per universe — a
  NIFTY50 request never poisons NIFTY100/BANKNIFTY (and vice versa).

Persistence tests run against TEMP-file SQLite databases (strictly *never* the
real ``intradayai.db``). Endpoint handlers are invoked directly (the repo has
no TestClient infrastructure) with patched dependencies: the resolver, the
provider, and the sector/persist side-effects are stubbed so no network and no
database writes happen outside the temp DB.
"""
import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import market_session
from app.core.config import settings
from app.core.database import Base
from app.models.universe_models import ROTATION_SCORE_COMPONENT_WEIGHTS
from app.models.universe_models import UniverseCategory
from app.services import active_universe as active_universe_module
from app.services import rotation_eligibility as elig
from app.services.active_universe import (
    ACTIVE_UNIVERSE_UNAVAILABLE,
    ActiveUniverseUnavailable,
    get_active_trading_universe,
    is_active_universe_request,
)
from app.services.scanner import MarketScanner
from app.services.universe_state_service import UniverseStateService

TZ = market_session.IST
PRE_OPEN = datetime(2026, 9, 23, 9, 0, 0, tzinfo=TZ)  # Wed, not an NSE holiday


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# persistence helpers (temp DB only)
# ---------------------------------------------------------------------------
def _make_members(core_count=30, rotation_count=50, prefix="S"):
    members = []
    for i in range(1, core_count + 1):
        members.append(
            {"symbol": f"{prefix}C{i:02d}", "category": "CORE",
             "rank": i, "selection_reason": f"core {i}"}
        )
    for i in range(1, rotation_count + 1):
        members.append(
            {"symbol": f"{prefix}R{i:02d}", "category": "ROTATION",
             "rank": i, "selection_reason": f"rotation {i}"}
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
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'phase1.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = UniverseStateService(session_factory=factory)
    return service, engine, factory


async def _make_active_version(service, *, members=None, version="phase1-v1"):
    members = members or _make_members()
    pool = await service.create_candidate_pool(
        "pool-v1", source="phase5b-resolution",
        symbols=[m["symbol"] for m in members],
    )
    pool = await service.validate_candidate_pool(pool["id"])
    draft = await service.create_universe_draft(
        pool["id"],
        version=version,
        members=members,
        core_count=30,
        rotation_count=50,
        total_count=80,
        calculated_at=datetime(2026, 9, 23, 18, 0),  # post-close
        effective_from=datetime(2026, 9, 28, 9, 15),
        config_hash="cfg-abc-123",
        slot_scores=_make_scores(members),
        slot_eligibility=_make_eligibility(members),
    )
    validated = await service.validate_universe(draft["id"])
    assert validated["status"] == "VALIDATED"
    approved = await service.approve_universe(
        draft["id"], actor="phase1-tester", reason="test"
    )
    assert approved["status"] == "APPROVED"
    return await service.activate_universe(
        draft["id"], as_of=PRE_OPEN, actor="phase1-tester"
    )


async def _crafted_active(members, membership=None, eligibility=None):
    """A fake ``get_active_universe`` payload (dict shape only) for fail-closed
    resolver tests — intentionally malformed versions bypass the state machine."""
    return {
        "id": "bad-v1",
        "version": "bad-v1",
        "status": "ACTIVE",
        "members": members,
        "scores": [
            {"symbol": m["symbol"], "total_rotation_score": 60.0}
            for m in members
        ],
        "eligibility": [
            {"symbol": m["symbol"], "eligibility_status": "ELIGIBLE"}
            for m in members
        ],
    }


# ===========================================================================
# Universe resolver — OK path
# ===========================================================================
def test_resolver_ok_returns_80_30_50_with_classification(tmp_path):
    async def scenario():
        service, engine, factory = await _ctx(tmp_path)
        active = await _make_active_version(service)
        result = await get_active_trading_universe(
            state_service=service, enrich=False
        )
        assert result["status"] == "OK"
        assert result["source"] == settings.LIVE_UNIVERSE
        assert result["universe_version_id"] == active["id"]
        assert result["total"] == 80
        assert result["core"] == 30
        assert result["rotation"] == 50
        assert result["duplicate_symbols"] == []
        assert result["invalid_symbols"] == []

        stocks = result["stocks"]
        assert len(stocks) == 80
        symbols = [s["symbol"] for s in stocks]
        assert len(set(symbols)) == 80  # 0 duplicates
        for s in stocks:
            assert elig.is_valid_symbol(s["symbol"])
            assert s["exchange"] == "NSE"
            assert s["classification"] in UniverseCategory.VALID
        core = [s for s in stocks if s["classification"] == "CORE"]
        rotation = [s for s in stocks if s["classification"] == "ROTATION"]
        assert len(core) == 30 and len(rotation) == 50
        core_syms = {s["symbol"] for s in core}
        rot_syms = {s["symbol"] for s in rotation}
        assert core_syms.isdisjoint(rot_syms)
        await engine.dispose()

    _run(scenario())


def test_is_active_universe_request_sentinels():
    for value in (None, "", "ACTIVE", "active", "rotation_80",
                  settings.LIVE_UNIVERSE):
        assert is_active_universe_request(value) is True, value
    for value in ("NIFTY50", "NIFTY100", "BANKNIFTY", "nifty50"):
        assert is_active_universe_request(value) is False, value


# ===========================================================================
# Universe resolver — FAIL CLOSED
# ===========================================================================
def test_resolver_fail_closed_no_active_version(tmp_path):
    async def scenario():
        service, engine, factory = await _ctx(tmp_path)  # empty DB
        with pytest.raises(ActiveUniverseUnavailable) as excinfo:
            await get_active_trading_universe(
                state_service=service, enrich=False
            )
        payload = excinfo.value.payload
        assert payload["status"] == ACTIVE_UNIVERSE_UNAVAILABLE
        assert payload["checks"]["active_exists"] is False
        await engine.dispose()

    _run(scenario())


def _patch_active(service, payload):
    service.get_active_universe = AsyncMock(return_value=payload)


def test_resolver_fail_closed_wrong_total_count(tmp_path):
    async def scenario():
        service, engine, factory = await _ctx(tmp_path)
        members = _make_members()[:-1]  # 79 members (30 core + 49 rotation)
        _patch_active(service, await _crafted_active(members))
        with pytest.raises(ActiveUniverseUnavailable) as excinfo:
            await get_active_trading_universe(
                state_service=service, enrich=False
            )
        checks = excinfo.value.payload["checks"]
        assert checks["total"] == 79
        assert checks["rotation"] == 49
        assert "total != 80" in excinfo.value.reason
        await engine.dispose()

    _run(scenario())


def test_resolver_fail_closed_wrong_core_count(tmp_path):
    async def scenario():
        service, engine, factory = await _ctx(tmp_path)
        members = _make_members(core_count=29, rotation_count=51)  # 80 but 29/51
        _patch_active(service, await _crafted_active(members))
        with pytest.raises(ActiveUniverseUnavailable) as excinfo:
            await get_active_trading_universe(
                state_service=service, enrich=False
            )
        checks = excinfo.value.payload["checks"]
        assert checks["total"] == 80
        assert checks["core"] == 29 and checks["rotation"] == 51
        assert "core != 30" in excinfo.value.reason
        await engine.dispose()

    _run(scenario())


def test_resolver_fail_closed_duplicate_symbols(tmp_path):
    async def scenario():
        service, engine, factory = await _ctx(tmp_path)
        members = _make_members()
        # duplicate one rotation symbol (80 rows, but only 79 unique)
        members.append(dict(members[-1]))
        _patch_active(service, await _crafted_active(members))
        with pytest.raises(ActiveUniverseUnavailable) as excinfo:
            await get_active_trading_universe(
                state_service=service, enrich=False
            )
        checks = excinfo.value.payload["checks"]
        assert checks["total"] == 81
        assert checks["duplicate_symbols"], "duplicates must be detected"
        assert "duplicate symbols" in excinfo.value.reason
        await engine.dispose()

    _run(scenario())


def test_resolver_fail_closed_invalid_symbols(tmp_path):
    async def scenario():
        service, engine, factory = await _ctx(tmp_path)
        members = _make_members()
        members[10]["symbol"] = "BAD!"  # invalid NSE symbol
        _patch_active(service, await _crafted_active(members))
        with pytest.raises(ActiveUniverseUnavailable) as excinfo:
            await get_active_trading_universe(
                state_service=service, enrich=False
            )
        checks = excinfo.value.payload["checks"]
        assert "BAD!" in checks["invalid_symbols"]
        assert "invalid symbols" in excinfo.value.reason
        await engine.dispose()

    _run(scenario())


def test_resolver_fail_closed_core_rotation_overlap(tmp_path):
    async def scenario():
        service, engine, factory = await _ctx(tmp_path)
        members = _make_members()
        # put a CORE symbol into the ROTATION section (members[30:] = rotation)
        members[40]["symbol"] = members[0]["symbol"]  # SC01 in CORE and ROTATION
        _patch_active(service, await _crafted_active(members))
        with pytest.raises(ActiveUniverseUnavailable) as excinfo:
            await get_active_trading_universe(
                state_service=service, enrich=False
            )
        checks = excinfo.value.payload["checks"]
        assert checks["overlap"] == [members[0]["symbol"]]
        assert "overlap" in excinfo.value.reason
        await engine.dispose()

    _run(scenario())


# ===========================================================================
# Provider cache — keyed by universe (no cross-universe poisoning)
# ===========================================================================
def _instruments_isolation():
    return (
        patch("app.services.sector_resolver.sector_resolver.resolve_batch",
              AsyncMock(return_value={})),
        patch("app.services.instrument_service.persist_sectors",
              AsyncMock(return_value=None)),
    )


def test_instruments_cache_keyed_by_universe():
    from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider

    p1, p2 = _instruments_isolation()
    with p1, p2:
        provider = YFinanceMarketDataProvider()
        n50 = _run(provider.get_instruments("NIFTY50"))
        n100 = _run(provider.get_instruments("NIFTY100"))
        bn = _run(provider.get_instruments("BANKNIFTY"))

    # NIFTY50 must NOT have served its 40 to NIFTY100/BANKNIFTY (and vice versa).
    assert len(n50) == 40
    assert len(n100) == 60
    assert len(bn) == 10
    set50 = {i["symbol"] for i in n50}
    set100 = {i["symbol"] for i in n100}
    setbn = {i["symbol"] for i in bn}
    assert set50 != set100 and setbn != set100  # lengths already differ; sanity
    assert set(provider._instruments_cache) == {"NIFTY50", "NIFTY100", "BANKNIFTY"}
    cached = provider.get_cached_status()
    assert cached["instruments_loaded"] is True
    assert set(cached["instruments_universes_cached"]) == {
        "NIFTY50", "NIFTY100", "BANKNIFTY",
    }


def test_instruments_cache_reverse_order_no_poison():
    from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider

    p1, p2 = _instruments_isolation()
    with p1, p2:
        provider = YFinanceMarketDataProvider()
        bn = _run(provider.get_instruments("BANKNIFTY"))     # first request
        n50 = _run(provider.get_instruments("NIFTY50"))
        n100 = _run(provider.get_instruments("NIFTY100"))
    assert len(bn) == 10
    assert len(n50) == 40
    assert len(n100) == 60
    assert len(provider._instruments_cache) == 3


# ===========================================================================
# API — /api/stocks and /api/scanner
# ===========================================================================
def _resolution_payload():
    members = _make_members()
    stocks = []
    for m in sorted(members, key=lambda m: (m["category"] != "CORE", m["rank"])):
        stocks.append({
            "symbol": m["symbol"],
            "name": m["symbol"],
            "sector": "Unknown",
            "exchange": "NSE",
            "classification": m["category"],
            "rank": m["rank"],
            "rotation_score": 60.0,
            "universe": "rotation_80",
            "yfinance_symbol": f"{m['symbol']}.NS",
        })
    return {
        "status": "OK",
        "source": settings.LIVE_UNIVERSE,
        "universe_version_id": "live-v1",
        "universe_version": "phase1-live-v1",
        "total": 80,
        "core": 30,
        "rotation": 50,
        "duplicate_symbols": [],
        "invalid_symbols": [],
        "stocks": stocks,
    }


def test_api_stocks_returns_80_with_classification():
    from app.api import market as market_api

    with patch.object(active_universe_module, "get_active_trading_universe",
                      AsyncMock(return_value=_resolution_payload())):
        response = _run(market_api.list_stocks(universe=settings.LIVE_UNIVERSE))

    assert isinstance(response, list)
    assert len(response) == 80
    core = [s for s in response if s["classification"] == "CORE"]
    rotation = [s for s in response if s["classification"] == "ROTATION"]
    assert len(core) == 30 and len(rotation) == 50
    assert all(s["exchange"] == "NSE" for s in response)


def test_api_stocks_fails_closed_503_when_universe_unavailable():
    from app.api import market as market_api

    exc = ActiveUniverseUnavailable(
        "no ACTIVE universe version exists in universe_versions",
        {"active_exists": False},
    )
    with patch.object(active_universe_module, "get_active_trading_universe",
                      AsyncMock(side_effect=exc)):
        response = _run(market_api.list_stocks(universe=settings.LIVE_UNIVERSE))

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["status"] == ACTIVE_UNIVERSE_UNAVAILABLE


class _StubProvider:
    """Provider stub: serves quick STALE (no-trade) quotes so a scan of any
    symbol list is deterministic and offline."""

    data_source_label = "stub"

    def __init__(self):
        self.fetches = 0

    async def get_quote_and_bars(self, symbol, timeframe="5m"):
        self.fetches += 1
        df = _make_df(60)
        quote = {
            "price": 100.0,
            "change_pct": 0.0,
            "volume": 1000,
            "data_status": "STALE",
            "data_age_seconds": 99999,
            "timestamp": None,
        }
        return {"quote": quote, "df": df}

    async def get_market_index(self, index_name="NIFTY50"):
        return {"change_pct": 0.0, "data_status": "OK", "name": index_name}

    async def get_instruments(self, universe="NIFTY50"):
        return []


def _make_df(rows=60):
    start = datetime(2026, 9, 21, 9, 15)
    data = []
    price = 100.0
    for i in range(rows):
        ts = start.replace(minute=start.minute + 5 * i)
        data.append({
            "timestamp": ts,
            "open": price,
            "high": price * 1.001,
            "low": price * 0.999,
            "close": price,
            "volume": 100000,
        })
        price *= 1.0005
    df = pd.DataFrame(data)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def test_api_scanner_returns_total_scanned_80_and_processes_all():
    from app.api import market as market_api

    stub = _StubProvider()
    scanner = MarketScanner(stub)
    with patch.object(active_universe_module, "get_active_trading_universe",
                      AsyncMock(return_value=_resolution_payload())), \
         patch.object(market_api, "_get_provider", return_value=stub), \
         patch.object(market_api, "_get_scanner", return_value=scanner):
        response = _run(market_api.run_scanner(universe=settings.LIVE_UNIVERSE))

    assert response["total_scanned"] == 80
    assert response["signals_found"] >= 0
    assert response["universe"] == "rotation_80"
    assert response["active_universe_version"] == "phase1-live-v1"
    assert len(response["results"]) == 80
    # every one of the 80 symbols was actually fetched + processed
    assert stub.fetches == 80
    for r in response["results"]:
        assert r["classification"] in UniverseCategory.VALID
        assert r["signal"] in ("NO_TRADE", "ERROR", None)


def test_api_scanner_fails_closed_503_when_universe_unavailable():
    from app.api import market as market_api

    stub = _StubProvider()
    scanner = MarketScanner(stub)
    exc = ActiveUniverseUnavailable(
        "no ACTIVE universe version exists in universe_versions",
        {"active_exists": False},
    )
    with patch.object(active_universe_module, "get_active_trading_universe",
                      AsyncMock(side_effect=exc)), \
         patch.object(market_api, "_get_provider", return_value=stub), \
         patch.object(market_api, "_get_scanner", return_value=scanner):
        response = _run(market_api.run_scanner(universe=settings.LIVE_UNIVERSE))

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["status"] == ACTIVE_UNIVERSE_UNAVAILABLE
    assert stub.fetches == 0  # never scanned anything when locked closed


def test_legacy_universe_request_keeps_provider_path():
    """Explicit NIFTY50 request still serves the legacy 40 (backwards
    compatibility), independent of the active resolver."""

    class _Legacy:
        data_source_label = "legacy-stub"

        async def get_instruments(self, universe="NIFTY50"):
            return [{"symbol": f"N{i:02d}", "name": f"N{i:02d}",
                     "sector": "X", "exchange": "NSE",
                     "yfinance_symbol": f"N{i:02d}.NS"} for i in range(1, 41)]

        async def get_quote_and_bars(self, symbol, timeframe="5m"):
            raise AssertionError("legacy stocks endpoint must not fetch quotes")

    from app.api import market as market_api

    legacy = _Legacy()
    with patch.object(market_api, "_get_provider", return_value=legacy):
        response = _run(market_api.list_stocks(universe="NIFTY50"))
    assert isinstance(response, list) and len(response) == 40
    assert all("classification" not in s for s in response)