"""Backtest Phase 2 regression tests.

Covers the four issues fixed after the accounting phase:

  * strategy selection end-to-end (request -> API -> engine -> strategy config),
    with validation of unknown strategies (HTTP 400) and no silent fallback
  * default backtest period (frontend default 365; explicit days still honoured)
  * persistence / history (POST stores a row, GET returns it, unknown id 404)
  * authentication + per-user ownership isolation

Deterministic: the market-data provider and the shared per-row signal decision
are mocked, and all persistence runs against a TEMP SQLite database — the real
``intradayai.db`` is never touched.
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as SyncSession

from app.api.auth import create_token
from app.core.database import Base, get_db
from app.main import app
from app.models.models import Backtest, User
from app.models.schemas import SignalDirection, SignalScore, TradeSetup

USER_A = "aaaaaaaaaaaaaaaa"
USER_B = "bbbbbbbbbbbbbbbb"
BAR_INDEX = "__bar_index__"


# ── deterministic market data + signal decision ──────────────────────────────
def _frame(n=70):
    ts = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [1_000_000] * n,
            BAR_INDEX: list(range(n)),
        }
    )


def _long_decision(entry, stop, target):
    return {
        "direction": SignalDirection.LONG,
        "confidence": 80.0,
        "signal_score": SignalScore(total=80.0),
        "setup": TradeSetup(entry=entry, stop_loss=stop, target_1=target, target_2=target),
        "reasons": [],
        "risks": [],
    }


def _no_trade():
    return {
        "direction": SignalDirection.NO_TRADE,
        "confidence": 0.0,
        "signal_score": SignalScore(),
        "setup": TradeSetup(),
        "reasons": [],
        "risks": [],
    }


def _signal_plan(long_bars=(55,)):
    def _call(row, market_context=None, strategy_version=None):
        if int(row[BAR_INDEX]) in set(long_bars):
            return _long_decision(row["open"], 95.0, 110.0)
        return _no_trade()

    return _call


class _FakeProvider:
    def __init__(self, df):
        self.df = df

    async def get_ohlcv(self, symbol, timeframe="1d", days=5):
        return self.df


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def env(tmp_path):
    """Temp DB + users + TestClient with overridden DB dependency."""
    db_file = tmp_path / "test_backtest.db"
    sync_engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(sync_engine)
    with SyncSession(sync_engine) as s:
        s.add(User(id=USER_A, email="a@test.local", name="A"))
        s.add(User(id=USER_B, email="b@test.local", name="B"))
        s.commit()
    sync_engine.dispose()

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    Session = async_sessionmaker(async_engine, expire_on_commit=False)

    async def _override_get_db():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db

    import app.api.backtest_api as backtest_api

    original_provider = backtest_api._provider
    backtest_api._provider = _FakeProvider(_frame())

    client = TestClient(app)
    try:
        yield {"client": client, "Session": Session, "engine": async_engine}
    finally:
        backtest_api._provider = original_provider
        app.dependency_overrides.clear()


def _auth(user_id, email):
    return {"Authorization": f"Bearer {create_token(user_id, email)}"}


def _run(client, headers, **body):
    payload = {"symbol": "RELIANCE", "days": 365, "initial_capital": 1_000_000}
    payload.update(body)
    with patch("app.services.backtesting.calculate_all_indicators", lambda d: d), patch(
        "app.services.backtesting.evaluate_row_signal", _signal_plan()
    ):
        return client.post("/api/backtest", json=payload, headers=headers)


# ── strategy selection ───────────────────────────────────────────────────────
@pytest.mark.parametrize("version", ["v1", "v2", "v3", "v2_candidate", "v3_candidate", "v4_candidate"])
def test_strategy_version_reaches_engine(env, version):
    """The selected strategy is validated and passed into the engine's config."""
    from app.services import strategy_config

    seen = []
    real_get = strategy_config.get_strategy

    def spy(v="v1"):
        seen.append(v)
        return real_get(v)

    with patch("app.services.backtesting.get_strategy", spy):
        resp = _run(env["client"], _auth(USER_A, "a@test.local"), strategy_version=version)

    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy_version"] == version
    assert body["strategy"] == version
    assert seen == [version]  # engine actually requested the selected config


def test_legacy_strategy_field_is_accepted(env):
    resp = _run(env["client"], _auth(USER_A, "a@test.local"), strategy="v2_candidate")
    assert resp.status_code == 200
    assert resp.json()["strategy_version"] == "v2_candidate"


def test_canonical_field_wins_over_legacy(env):
    resp = _run(
        env["client"],
        _auth(USER_A, "a@test.local"),
        strategy_version="v3_candidate",
        strategy="v1",
    )
    assert resp.status_code == 200
    assert resp.json()["strategy_version"] == "v3_candidate"


def test_default_strategy_only_when_unspecified(env):
    resp = _run(env["client"], _auth(USER_A, "a@test.local"))
    assert resp.status_code == 200
    assert resp.json()["strategy_version"] == "v1"


def test_legacy_multi_factor_placeholder_defaults(env):
    # Backward compatibility: the historical (unused) default value maps to the
    # default strategy rather than 400.
    resp = _run(env["client"], _auth(USER_A, "a@test.local"), strategy="multi_factor")
    assert resp.status_code == 200
    assert resp.json()["strategy_version"] == "v1"


def test_unknown_strategy_rejected_400(env):
    resp = _run(env["client"], _auth(USER_A, "a@test.local"), strategy_version="unknown_strategy")
    assert resp.status_code == 400
    assert "unknown_strategy" in resp.json()["detail"]


# ── default period ───────────────────────────────────────────────────────────
def test_explicit_days_accepted(env):
    for days in (30, 365):
        resp = _run(env["client"], _auth(USER_A, "a@test.local"), days=days)
        assert resp.status_code == 200, resp.text


def test_api_request_default_days_unchanged():
    from app.api.backtest_api import BacktestRequest

    assert BacktestRequest(symbol="X").days == 30


def test_frontend_default_days_is_365():
    page = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "app"
        / "backtest"
        / "page.tsx"
    )
    text = page.read_text(encoding="utf-8")
    assert "useState(365)" in text


# ── persistence / history ────────────────────────────────────────────────────
def test_post_persists_and_get_returns(env):
    resp = _run(env["client"], _auth(USER_A, "a@test.local"), strategy_version="v2_candidate")
    assert resp.status_code == 200
    result = resp.json()
    bid = result["id"]
    assert bid

    # Row exists in the temp DB with the owner + canonical strategy.
    async def _load():
        async with env["Session"]() as s:
            return await s.get(Backtest, bid)

    row = asyncio.run(_load())
    assert row is not None
    assert row.user_id == USER_A
    assert row.strategy == "v2_candidate"
    assert row.status == "completed"
    assert row.days == 365
    assert row.symbol == "RELIANCE"
    assert row.results_json

    # GET returns the saved payload with matching metrics.
    got = env["client"].get(f"/api/backtest/{bid}", headers=_auth(USER_A, "a@test.local"))
    assert got.status_code == 200
    saved = got.json()
    assert saved["id"] == bid
    assert saved["total_trades"] == result["total_trades"]
    assert saved["total_pnl"] == result["total_pnl"]
    assert saved["strategy_version"] == "v2_candidate"
    assert len(saved["trades"]) == len(result["trades"])
    assert len(saved["equity_curve"]) == len(result["equity_curve"])


def test_unknown_id_returns_404(env):
    got = env["client"].get("/api/backtest/doesnotexist0000", headers=_auth(USER_A, "a@test.local"))
    assert got.status_code == 404
    assert got.json()["detail"] == "Backtest not found"


def test_ownership_isolation(env):
    created = _run(env["client"], _auth(USER_B, "b@test.local"))
    assert created.status_code == 200
    bid = created.json()["id"]

    # Owner can read it.
    assert env["client"].get(f"/api/backtest/{bid}", headers=_auth(USER_B, "b@test.local")).status_code == 200
    # Another user cannot (404, existence not disclosed).
    other = env["client"].get(f"/api/backtest/{bid}", headers=_auth(USER_A, "a@test.local"))
    assert other.status_code == 404


# ── authentication ───────────────────────────────────────────────────────────
def test_post_requires_auth(env):
    resp = env["client"].post("/api/backtest", json={"symbol": "RELIANCE", "days": 365})
    assert resp.status_code == 401


def test_get_requires_auth(env):
    resp = env["client"].get("/api/backtest/someid0000000000")
    assert resp.status_code == 401


def test_post_with_valid_jwt_succeeds(env):
    resp = _run(env["client"], _auth(USER_A, "a@test.local"))
    assert resp.status_code == 200


def test_get_with_valid_jwt_succeeds(env):
    bid = _run(env["client"], _auth(USER_A, "a@test.local")).json()["id"]
    resp = env["client"].get(f"/api/backtest/{bid}", headers=_auth(USER_A, "a@test.local"))
    assert resp.status_code == 200


# ── additive schema migration ────────────────────────────────────────────────
def test_ensure_backtest_schema_adds_missing_columns(tmp_path):
    from app.services.backtest_store import ensure_backtest_schema

    db_file = tmp_path / "legacy.db"
    sync_engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    with sync_engine.begin() as conn:
        # Legacy table WITHOUT user_id / universe / days / status.
        conn.execute(
            text(
                "CREATE TABLE backtests (id VARCHAR(16) PRIMARY KEY, strategy VARCHAR(100) "
                "NOT NULL, symbol VARCHAR(50), start_date DATETIME, end_date DATETIME, "
                "initial_capital FLOAT, results_json TEXT)"
            )
        )
    sync_engine.dispose()

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    Session = async_sessionmaker(async_engine, expire_on_commit=False)

    async def _migrate_and_read():
        async with Session() as s:
            await ensure_backtest_schema(s)
            res = await s.execute(text("PRAGMA table_info(backtests)"))
            return {r[1] for r in res.fetchall()}

    cols = asyncio.run(_migrate_and_read())
    asyncio.run(async_engine.dispose())
    assert {"user_id", "universe", "days", "status"} <= cols
