"""AI traceability: strategy_version stamping, signal outcome lifecycle, and
signal_id preservation across the paper-trade lifecycle.

Scope of this file
-----------------
The live scanner used to call ``evaluate_signal()`` WITHOUT a strategy version,
so every persisted ``signals`` row carried ``strategy_version = NULL`` and the
AI-traceability chain could not name the configuration that produced a
recommendation. These tests lock three things down:

  1. PARITY / SAFETY - passing ``LIVE_STRATEGY_VERSION`` is behaviour-neutral.
     Every direction band, ATR multiplier, min-R:R and STRONG filter that the
     version supplies is byte-for-byte the engine's existing no-version default,
     and ``evaluate_signal`` returns an identical decision either way. This is
     the guard that makes the change additive instead of strategic.
  2. STAMPING - a newly scanned signal is persisted with a non-NULL
     ``strategy_version`` and ``outcome = 'pending'``.
  3. LIFECYCLE - a genuinely completed AI-linked paper trade resolves the
     signal's ``outcome`` from the REALIZED P&L, while manual / unlinked trades
     stay unlinked and never resolve anything.

No test in this file writes to the live ledger: every persistence call is
pointed at a temp DB, and the scanner test intercepts ``persist_signal`` so the
real database is never touched.
"""

import asyncio
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as SyncSession

from app.core.config import settings
from app.core.database import Base
from app.models.models import User
from app.services import signal_store
from app.services.indicators import calculate_all_indicators
from app.services.profit_capture import (
    PROTECT_MOVE_TO_ENTRY,
    TRAILING_RANGE,
    ProfitCaptureConfig,
)
from app.services.risk_engine import RiskConfig, RiskEngine
from app.services.signal_engine import (
    _resolve_setup_multipliers,
    determine_direction,
    evaluate_signal,
)
from app.services.strategy_config import LIVE_STRATEGY_VERSION, get_strategy

USER = "bbbbbbbbbbbbbbbb"
EMAIL = "trace@test.local"
SID = "sigtrace1234567"   # exactly 16 chars (signals.id is VARCHAR(16))
SYMBOL = "TRACETEST"


# ────────────────────────────────────────────────────────────────────────────
# deterministic market data that yields a real (non-NO_TRADE) LONG
# ────────────────────────────────────────────────────────────────────────────

def _bullish_bars(seed=2, drift=0.10, n=400):
    """A seeded, reproducible uptrend with pullbacks.

    A perfectly monotonic series makes the RSI implementation return NaN (no
    down-closes -> no valid average loss), which the engine correctly rejects as
    a missing critical indicator. Random-with-drift keeps every indicator finite.
    """
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.randn(n) + drift)
    dates = pd.date_range("2026-09-29 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame({
        "timestamp": dates,
        "open": close,
        "high": close + np.abs(rng.randn(n)) * 0.6 + 0.1,
        "low": close - np.abs(rng.randn(n)) * 0.6 - 0.1,
        "close": close,
        "volume": rng.randint(20000, 90000, n).astype(int),
    })


def _bullish_df():
    return calculate_all_indicators(_bullish_bars())


def _eval_kwargs(df):
    return dict(
        data_source="yfinance",
        market_context={"nifty_trend": "BULLISH", "nifty_change_pct": 0.8},
        data_age_seconds=5,
        data_status="LIVE",
        market_data_timestamp=df["timestamp"].iloc[-1],
    )


# ────────────────────────────────────────────────────────────────────────────
# 1. PARITY / SAFETY - the version label must not move any decision
# ────────────────────────────────────────────────────────────────────────────

def test_live_strategy_version_is_a_registered_version():
    sv = get_strategy(LIVE_STRATEGY_VERSION)
    assert sv is not None, "LIVE_STRATEGY_VERSION must name a registered version"
    assert sv.version == LIVE_STRATEGY_VERSION


def test_direction_bands_identical_with_live_version():
    """Every 0-100 score maps to the same direction with and without the label."""
    sv = get_strategy(LIVE_STRATEGY_VERSION)
    for half in range(0, 201):
        score = half / 2.0
        assert sv.determine_direction(score) is determine_direction(score), score


@pytest.mark.parametrize("is_strong", [False, True])
def test_setup_geometry_identical_with_live_version(is_strong):
    sv = get_strategy(LIVE_STRATEGY_VERSION)
    assert _resolve_setup_multipliers(sv, is_strong) == _resolve_setup_multipliers(None, is_strong)


def test_locked_strategy_constants_unchanged():
    """Freeze the live strategy geometry/risk gates this task must not touch."""
    sv = get_strategy(LIVE_STRATEGY_VERSION)
    # SL 1.5x ATR both strengths; normal T1 2.0 / strong T1 3.0; T2 = T1 + 1 ATR.
    assert _resolve_setup_multipliers(None, False) == (1.5, 2.0, 3.0)
    assert _resolve_setup_multipliers(None, True) == (1.5, 3.0, 4.0)
    assert settings.NORMAL_SL_ATR_MULTIPLIER == 1.5
    assert settings.STRONG_SL_ATR_MULTIPLIER == 1.5
    assert settings.NORMAL_T1_ATR_MULTIPLIER == 2.0
    assert settings.STRONG_T1_ATR_MULTIPLIER == 3.0
    assert settings.NORMAL_T2_ATR_MULTIPLIER == 3.0
    assert settings.STRONG_T2_ATR_MULTIPLIER == 4.0
    # Minimum R:R is T1-based: 1.2 normal / 2.0 strong.
    assert (sv.min_rr_normal, sv.min_rr_strong) == (1.2, 2.0)
    # STRONG admission filters.
    assert (sv.strong_min_adx, sv.strong_min_rel_vol) == (25.0, 1.0)
    assert (sv.strong_max_rsi_long, sv.strong_max_rsi_short) == (75.0, 25.0)


def test_evaluate_signal_decision_identical_with_and_without_live_version():
    """The strongest guarantee: only the label differs, the decision does not."""
    df = _bullish_df()
    kwargs = _eval_kwargs(df)
    unlabelled = evaluate_signal(df, SYMBOL, **kwargs)
    labelled = evaluate_signal(df, SYMBOL, strategy_version=LIVE_STRATEGY_VERSION, **kwargs)

    # Sanity: the fixture really does produce a tradable signal, otherwise this
    # test would pass trivially on two identical NO_TRADE dicts.
    assert unlabelled["direction"] == "LONG"
    assert unlabelled["direction"] == labelled["direction"]

    for key in ("direction", "confidence", "signal_score", "setup",
                "reasons", "risks", "direction_evidence"):
        assert unlabelled[key] == labelled[key], key

    assert unlabelled["strategy_version"] is None
    assert labelled["strategy_version"] == LIVE_STRATEGY_VERSION


# ────────────────────────────────────────────────────────────────────────────
# 2. STAMPING - a newly scanned signal is versioned and starts pending
# ────────────────────────────────────────────────────────────────────────────

class _CaptureStore:
    """Stand-in for signal_store that records calls instead of writing."""

    def __init__(self):
        self.persisted = []
        self.shadows = []

    def persist_signal(self, signal, candle_ts=None, market_ctx=None,
                       quality=None, db_path=None):
        self.persisted.append({"signal": signal, "candle_ts": candle_ts,
                               "market_ctx": market_ctx, "quality": quality})

    def persist_24h_comparison(self, *a, **kw):
        self.shadows.append((a, kw))

    def ensure_signal_schema(self, *a, **kw):
        pass

    def update_signal_usage(self, *a, **kw):
        pass

    def mark_signal_outcome(self, *a, **kw):
        pass


def test_scanner_persists_signal_with_strategy_version(monkeypatch):
    """Drive the real _process_stock and assert what it hands to persist_signal."""
    import app.services.scanner as scanner_mod

    capture = _CaptureStore()
    monkeypatch.setattr(scanner_mod, "signal_store", capture)

    class _DummyProvider:
        data_source_label = "dummy"

    scanner = scanner_mod.MarketScanner(_DummyProvider())
    df = _bullish_df()
    quote = {
        "price": float(df["close"].iloc[-1]),
        "change_pct": 1.2,
        "volume": int(df["volume"].iloc[-1]),
        "data_status": "LIVE",
        "data_age_seconds": 5,
        "timestamp": df["timestamp"].iloc[-1],
    }
    result = scanner._process_stock(
        {"symbol": SYMBOL, "name": SYMBOL, "sector": "Test"},
        {"symbol": SYMBOL, "name": SYMBOL, "sector": "Test",
         "_df": df, "_quote": quote, "_error": None},
        {"nifty_trend": "BULLISH", "nifty_change_pct": 0.8},
    )

    assert result["signal"] == "LONG"
    assert len(capture.persisted) == 1, "a tradable scan must persist exactly one signal"
    persisted = capture.persisted[0]["signal"]
    assert persisted["strategy_version"] == LIVE_STRATEGY_VERSION
    assert persisted["strategy_version"] is not None
    # The real, engine-generated id is what would reach the signals table.
    assert isinstance(persisted.get("id"), str) and persisted["id"]


def test_scanner_persists_versioned_shadow_signal_when_enabled(monkeypatch):
    """The 24H shadow comparison must be versioned too, so both ledger rows
    name the same running configuration."""
    import app.services.scanner as scanner_mod

    capture = _CaptureStore()
    monkeypatch.setattr(scanner_mod, "signal_store", capture)
    monkeypatch.setattr(settings, "USE_24H_CONTEXT", True)

    class _DummyProvider:
        data_source_label = "dummy"

    scanner = scanner_mod.MarketScanner(_DummyProvider())
    df = _bullish_df()
    quote = {
        "price": float(df["close"].iloc[-1]), "change_pct": 1.2,
        "volume": int(df["volume"].iloc[-1]), "data_status": "LIVE",
        "data_age_seconds": 5, "timestamp": df["timestamp"].iloc[-1],
    }
    scanner._process_stock(
        {"symbol": SYMBOL, "name": SYMBOL, "sector": "Test"},
        {"symbol": SYMBOL, "name": SYMBOL, "sector": "Test",
         "_df": df, "_quote": quote, "_error": None},
        {"nifty_trend": "BULLISH", "nifty_change_pct": 0.8},
    )
    assert len(capture.shadows) == 1
    shadow_args = capture.shadows[0][0]
    shadow_signal = shadow_args[1]
    assert shadow_signal["strategy_version"] == LIVE_STRATEGY_VERSION


def test_persisted_signal_row_has_version_and_pending_outcome(tmp_path):
    """persist_signal writes the version and initializes outcome = pending."""
    db = str(tmp_path / "stamped.db")
    df = _bullish_df()
    signal = evaluate_signal(df, SYMBOL, strategy_version=LIVE_STRATEGY_VERSION,
                             **_eval_kwargs(df))
    signal_store.persist_signal(signal, candle_ts=df["timestamp"].iloc[-1],
                                market_ctx={"nifty_trend": "BULLISH"},
                                quality="QUALIFIED", db_path=db)

    con = sqlite3.connect(db)
    row = con.execute("SELECT strategy_version, outcome, realized_pnl, "
                      "holding_duration FROM signals WHERE id = ?",
                      (signal["id"],)).fetchone()
    con.close()
    assert row is not None
    assert row[0] == LIVE_STRATEGY_VERSION      # NOT NULL
    assert row[1] == "pending"                 # starts pending
    assert row[2] == 0.0
    assert row[3] == 0


def test_every_scanner_evaluate_signal_call_passes_the_version():
    """Source guard: no scanner call site may regress to an unversioned signal."""
    import inspect
    import re

    import app.services.scanner as scanner_mod

    src = inspect.getsource(scanner_mod)
    calls = re.findall(r"evaluate_signal\((?:[^()]|\([^()]*\))*\)", src, re.S)
    assert len(calls) >= 4, f"expected 4 evaluate_signal call sites, found {len(calls)}"
    for call in calls:
        assert "strategy_version=LIVE_STRATEGY_VERSION" in call, call


# ────────────────────────────────────────────────────────────────────────────
# 3. OUTCOME LIFECYCLE - resolved only by a real completed linked trade
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def finalize_env(tmp_path, monkeypatch):
    """Temp-DB sandbox for _finalize_paper_trade (live ledger untouched)."""
    import app.core.database as database_mod
    import app.api.trading as trading_mod

    db_file = tmp_path / "trace_finalize.db"
    sqlite_path = str(db_file)
    sync_engine = create_engine(f"sqlite:///{sqlite_path}")
    Base.metadata.create_all(sync_engine)
    with SyncSession(sync_engine) as s:
        s.add(User(id=USER, email=EMAIL, name="Trace"))
        s.commit()
    sync_engine.dispose()

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{sqlite_path}")
    Session = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr(database_mod, "async_session", Session)

    # Force the outcome write into the temp DB (the real helper resolves the
    # LIVE database when no db_path is supplied).
    real_mark = signal_store.mark_signal_outcome

    def _mark(signal_id, pnl, holding_minutes, db_path=None):
        return real_mark(signal_id, pnl, holding_minutes, db_path=sqlite_path)

    monkeypatch.setattr(signal_store, "mark_signal_outcome", _mark)
    # Fresh risk engine + empty engine so global paper state is untouched.
    monkeypatch.setattr(trading_mod, "risk_engine", RiskEngine())
    monkeypatch.setattr(trading_mod, "paper_engine",
                        type("E", (), {"get_positions": staticmethod(lambda user_id=None: [])})())

    yield {"sqlite_path": sqlite_path, "finalize": trading_mod._finalize_paper_trade}
    asyncio.run(async_engine.dispose())


def _seed_signal(sqlite_path, sig_id=SID, strategy_version=LIVE_STRATEGY_VERSION):
    signal_store.ensure_signal_schema(sqlite_path)
    con = sqlite3.connect(sqlite_path)
    con.execute(
        "INSERT INTO signals (id, symbol, timestamp, direction, signal_score, "
        "confidence, entry_price, stop_loss, target_1, target_2, risk_reward, "
        "strategy, explanation, indicator_scores, market_context, data_source, "
        "outcome, realized_pnl, holding_duration, signal_quality, strategy_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sig_id, SYMBOL, "2026-09-29 13:50:00", "LONG", 82.0, 0.79, 100.0, 98.0,
         105.0, 107.0, 2.5, "multi_factor", "{}", "{}", "{}", "yfinance",
         "pending", 0.0, 0, "QUALIFIED", strategy_version),
    )
    con.commit()
    con.close()


def _closed_result(pnl, exit_price, signal_id=SID, exit_reason="T2_FINAL",
                   opened="2026-09-29 13:55:00", exited="2026-09-29 14:10:00"):
    return {
        "pnl": pnl,
        "partial": False,
        "trade": {
            "id": "tradetest01",
            "symbol": SYMBOL,
            "direction": "LONG",
            "entry_price": 100.0,
            "exit_price": exit_price,
            "quantity": 10,
            "stop_loss": 98.0,
            "target_1": 105.0,
            "target_2": 107.0,
            "opened_at": opened,
            "exit_time": exited,
            "status": "closed",
            "pnl": pnl,
            "fees": 0.0,
            "slippage": 0.0,
            "result": "WIN" if pnl > 0 else "LOSS",
            "exit_reason": exit_reason,
            "signal_id": signal_id,
        },
    }


def _read_signal(sqlite_path, sig_id=SID):
    con = sqlite3.connect(sqlite_path)
    row = con.execute("SELECT outcome, realized_pnl, holding_duration, strategy_version "
                      "FROM signals WHERE id = ?", (sig_id,)).fetchone()
    con.close()
    return row


@pytest.mark.parametrize(
    "pnl,exit_price,expected",
    [
        (70.0, 107.0, "WIN"),        # profitable long
        (-20.0, 98.0, "LOSS"),       # stop-loss hit on the SAME long direction
        (0.0, 100.0, "BREAKEVEN"),   # flat exit
    ],
)
def test_completed_ai_linked_trade_resolves_signal_outcome(
        finalize_env, pnl, exit_price, expected):
    """Outcome is derived from REALIZED P&L of an actual completed trade."""
    path = finalize_env["sqlite_path"]
    _seed_signal(path)
    assert _read_signal(path)[0] == "pending"

    asyncio.run(finalize_env["finalize"](
        _closed_result(pnl, exit_price), USER))

    outcome, realized, holding, version = _read_signal(path)
    assert outcome == expected
    assert realized == pytest.approx(round(pnl, 2))
    assert holding == 15                      # 13:55 → 14:10
    assert version == LIVE_STRATEGY_VERSION   # untouched by resolution
    # The ledger row kept the same signal_id.
    con = sqlite3.connect(path)
    tid, sid = con.execute("SELECT id, signal_id FROM trades").fetchone()
    con.close()
    assert sid == SID


def test_outcome_is_not_inferred_from_direction(finalize_env):
    """A losing LONG resolves to LOSS - proof the outcome comes from P&L, not
    from the signal's direction."""
    path = finalize_env["sqlite_path"]
    _seed_signal(path)          # direction = LONG
    asyncio.run(finalize_env["finalize"](_closed_result(-20.0, 98.0), USER))
    assert _read_signal(path)[0] == "LOSS"


def test_unlinked_manual_trade_resolves_nothing(finalize_env):
    """A manual order (signal_id = None) must never resolve a signal outcome and
    must never fabricate a signal_id."""
    path = finalize_env["sqlite_path"]
    _seed_signal(path)
    result = _closed_result(50.0, 105.0, signal_id=None, exit_reason="MANUAL_CLOSE")
    asyncio.run(finalize_env["finalize"](result, USER))

    # The seeded signal is untouched: still pending, no realized P&L.
    assert _read_signal(path)[:3] == ("pending", 0.0, 0)
    con = sqlite3.connect(path)
    rows = con.execute("SELECT signal_id FROM trades").fetchall()
    con.close()
    assert rows == [(None,)]


def test_unlinked_close_creates_no_signal_row(finalize_env):
    """Closing a manual trade must not manufacture a signals row."""
    path = finalize_env["sqlite_path"]
    result = _closed_result(50.0, 105.0, signal_id=None, exit_reason="MANUAL_CLOSE")
    asyncio.run(finalize_env["finalize"](result, USER))
    con = sqlite3.connect(path)
    count = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    con.close()
    assert count == 0


def test_mark_signal_outcome_is_a_noop_without_signal_id(tmp_path):
    """No signal_id -> no row invented, no crash."""
    db = str(tmp_path / "noop.db")
    signal_store.ensure_signal_schema(db)
    signal_store.mark_signal_outcome(None, 100.0, 5, db_path=db)
    signal_store.mark_signal_outcome("", 100.0, 5, db_path=db)
    con = sqlite3.connect(db)
    count = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    con.close()
    assert count == 0


# ────────────────────────────────────────────────────────────────────────────
# 4. RISK + PROFIT CAPTURE UNCHANGED
# ────────────────────────────────────────────────────────────────────────────

def test_risk_limits_unchanged():
    cfg = RiskConfig()
    assert cfg.max_risk_per_trade_pct == 2.0
    assert cfg.max_daily_loss_pct == 5.0
    assert cfg.max_trades_per_day == 10
    assert cfg.max_simultaneous_positions == 10
    assert cfg.min_risk_reward == 1.2
    assert cfg.cooldown_after_losses == 3
    # Quality multipliers: REJECTED/WEAK never trade, the rest are unscaled.
    assert cfg.quality_risk_multipliers["REJECTED"] == 0.0
    assert cfg.quality_risk_multipliers["WEAK"] == 0.0
    for level in ("NORMAL", "QUALIFIED", "PREMIUM"):
        assert cfg.quality_risk_multipliers[level] == 1.0


def test_profit_capture_config_unchanged():
    cfg = ProfitCaptureConfig.from_settings()
    assert cfg.t1_exit_percent == 50.0
    assert cfg.protect_mode == PROTECT_MOVE_TO_ENTRY
    assert cfg.trailing_mode == TRAILING_RANGE
    assert cfg.trailing_range_mult == 0.5
    assert cfg.remaining_percent == 50.0


# ────────────────────────────────────────────────────────────────────────────
# 5. MONITORING GATE UNCHANGED
# ────────────────────────────────────────────────────────────────────────────

def test_monitoring_sample_gate_still_thirty():
    from app.services.ai_performance_monitor import MIN_TRADES_FOR_CONCLUSION

    assert MIN_TRADES_FOR_CONCLUSION == 30


def test_monitoring_cohort_excludes_unlinked_trades():
    """Only rows that carry a signal_id AND resolve to a persisted signal count.

    This is the same predicate the monitor uses, so a manual/unlinked trade can
    never quietly inflate the AI-linked sample.
    """
    from app.services.ai_performance_monitor import separate_ai_linked

    trades = [
        # linked + resolved -> counts
        {"id": "a", "signal_id": SID, "ai_available": True, "pnl": 70.0},
        # manual order -> excluded
        {"id": "b", "signal_id": None, "ai_available": False, "pnl": 50.0},
        # signal_id present but the signal row is gone -> excluded
        {"id": "c", "signal_id": "missingid", "ai_available": False, "pnl": -10.0},
        # flagged available but no signal_id -> excluded
        {"id": "d", "signal_id": None, "ai_available": True, "pnl": 5.0},
    ]
    ai, other = separate_ai_linked(trades)
    assert [t["id"] for t in ai] == ["a"]
    assert [t["id"] for t in other] == ["b", "c", "d"]


def test_monitoring_reports_insufficient_after_one_completed_trade():
    """One genuine completed AI-linked trade must NOT unlock a conclusion."""
    from app.services.ai_performance_monitor import (
        compute_monitoring,
        separate_ai_linked,
    )

    trades = [{
        "id": "a", "symbol": SYMBOL, "direction": "LONG", "signal_id": SID,
        "ai_available": True, "pnl": 70.0, "status": "closed",
        "entry_price": 100.0, "exit_price": 107.0, "quantity": 10,
        "entry_time": "2026-09-29 13:55:00", "exit_time": "2026-09-29 14:10:00",
        "exit_reason": "T2_FINAL", "ai_direction": "LONG", "ai_score": 82.0,
    }]
    ai, other = separate_ai_linked(trades)
    report = compute_monitoring(ai + other)
    blob = json.dumps(report, default=str)
    assert "Insufficient real Profit Capture data for performance conclusion." in blob
