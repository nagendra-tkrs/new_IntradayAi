"""24-Hour market-context (Mode B) tests — additive, reversible, look-ahead-safe.

Contract under test:

  * ``compute_24h_context`` slices ``[decision_ts - window, decision_ts]`` in IST
    out of the bars the scanner already fetched; NO synthetic candle is ever
    created (weekends/holidays/gaps simply reduce the bar count).
  * Only bars at-or-before the decision timestamp enter the window (look-ahead
    ban). ``net = clip(0.5·trend_net + 0.5·range_net, −1, 1)``; fewer than 2
    candles → ``net = 0`` and ``sufficient = False`` (nothing to measure).
  * ``evaluate_row_signal`` / ``evaluate_signal`` WITHOUT ``context_24h`` are
    byte-identical to the pre-feature engine; WITH a finite 24H net the market
    context changes but the TRADE SETUP (entry / SL / T1 / T2 / R:R) and Risk
    Quality are produced by the unchanged strategy — never by the 24H context.
  * ``signal_store`` schema migration is additive; persist/upsert, usage and
    outcome updates work; ``signal_id`` survives the paper-order → position
    persistence round trip only when explicitly supplied.
  * ``context_report`` gates on real data, reports agreement, derives realized
    metrics from the trades ledger only, and marks the 24H side pending.
"""

import json
import os
import sqlite3
import tempfile

import numpy as np
import pandas as pd
import pytest

from app.core.config import settings
from app.core.market_session import IST
from app.services.context_24h import compute_24h_context, window_bounds
from app.services.context_report import build_context_comparison
from app.services.signal_engine import (
    evaluate_row_signal,
    evaluate_signal,
    _net_market_context,
)
from app.services import signal_store
from app.services.paper_trading import (
    PaperTradingEngine,
    load_account_state,
    persist_account_state,
)
from app.models.schemas import SignalDirection


# ────────────────────────────────────────────────────────────────────────────
# Fixtures / synthetic data
# ────────────────────────────────────────────────────────────────────────────

def _base_row() -> pd.Series:
    """Fully populated, direction-NEUTRAL row (every net component = 0)."""
    return pd.Series({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
        "ema_9": 100.0, "ema_20": 100.0, "ema_50": 100.0,
        "atr_14": 1.5, "adx_14": 18.0, "relative_volume": 1.0,
        "vwap": 100.0, "rsi_14": 50.0,
        "macd": 0.0, "macd_signal": 0.0, "macd_histogram": 0.0,
        "roc_5": 0.0, "distance_from_vwap": 0.0,
        "prev_high": 102.0, "prev_low": 98.0,
        "opening_range_high": 102.0, "opening_range_low": 98.0,
        "bb_upper": 103.0, "bb_lower": 97.0,
        "volatility_20": 0.3,
    })


def _row(**overrides) -> pd.Series:
    row = _base_row()
    for key, value in overrides.items():
        row[key] = value
    return row


def _mkctx(trend: str = "NEUTRAL", change: float = 0.0) -> dict:
    return {"nifty_trend": trend, "nifty_change_pct": change,
            "banknifty_change_pct": change}


def _bull_row() -> pd.Series:
    """Directionally bullish row with VALID OHLC (high >= max(o,c), etc.)."""
    return _row(open=102.5, high=104.0, low=102.0, close=103.0,
                ema_50=100.0, rsi_14=62.0, adx_14=22.0,
                vwap=100.0, relative_volume=1.3, distance_from_vwap=0.02)


def _bars(n: int, start, step_min: int = 5) -> pd.DataFrame:
    """Continuous synthetic 5-minute bar frame (IST-aware timestamps)."""
    ts = [start + pd.Timedelta(minutes=step_min * i) for i in range(n)]
    closes = 100.0 + 2.0 * np.sin(np.arange(n) / 5.0)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) + 0.3
    lows = np.minimum(opens, closes) - 0.3
    return pd.DataFrame({
        "timestamp": pd.DatetimeIndex(ts),
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": 1000 + (np.arange(n) % 7) * 100,
        "atr_14": np.full(n, 0.5),
        "relative_volume": np.full(n, 1.0),
        "ema_50": np.full(n, 100.0),
        "ema_20": np.full(n, 100.0),
    })


def _tmp_db() -> str:
    tmp = tempfile.mkdtemp()
    return os.path.join(tmp, "test_24h.db")


@pytest.fixture()
def tmp_db():
    path = _tmp_db()
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


# ────────────────────────────────────────────────────────────────────────────
# Reversibility: config default off
# ────────────────────────────────────────────────────────────────────────────

def test_use_24h_context_defaults_off():
    # Reversible-by-default: the flag must be OFF so today's behavior is
    # byte-identical until a user opts in.
    assert settings.USE_24H_CONTEXT is False
    assert settings.CONTEXT_24H_WINDOW_HOURS == 24.0


def test_window_bounds_24_clock_hours():
    decision = pd.Timestamp("2026-09-29 13:50:00", tz=IST)
    start, end = window_bounds(decision, 24.0)
    assert end == decision
    assert (end - start) == pd.Timedelta(hours=24)
    assert start.tzinfo is not None  # tz-aware IST


# ────────────────────────────────────────────────────────────────────────────
# compute_24h_context — window, tz, gaps, insufficiency, look-ahead
# ────────────────────────────────────────────────────────────────────────────

def test_window_slices_last_24_clock_hours():
    # 48 hours of continuous 5-minute bars: window must include exactly the
    # 288 bars in the last 24h + the decision bar itself (inclusive ends).
    start = pd.Timestamp("2026-09-28 00:00:00", tz=IST)
    df = _bars(576, start)  # 48h * 12 bars/hr
    decision = df["timestamp"].iloc[-1]
    ctx = compute_24h_context(df, decision_ts=decision, window_hours=24.0)
    assert ctx["candles"] == 289
    assert ctx["sufficient"] is True
    assert pd.Timestamp(ctx["decision_timestamp"]) == decision
    assert pd.Timestamp(ctx["first_timestamp"]) >= df["timestamp"].iloc[0]


def test_timezone_naive_timestamps_attached_to_ist():
    # Naive (no tz) timestamps must be interpreted as IST wall-clock, the app
    # convention for provider bars. The decision/start ISO strings show +05:30.
    start = pd.Timestamp("2026-09-29 09:15:00")  # naive
    df = _bars(80, start)
    df["timestamp"] = df["timestamp"].dt.tz_localize(None)
    decision = pd.Timestamp("2026-09-29 13:50:00")  # naive
    ctx = compute_24h_context(df, decision_ts=decision, window_hours=24.0)
    assert ctx["decision_timestamp"].endswith("+05:30")
    assert ctx["window_start"].endswith("+05:30")
    assert ctx["last_timestamp"].endswith("+05:30")


def test_overnight_gap_reduces_candles_no_synthetic_bars():
    # Tuesday session (75 bars) + Wednesday session (first 60 bars). The
    # overnight gap (last Tue bar 15:25 → Wed 09:15) is real missing market
    # time: the window must simply contain fewer bars — never fabricate any.
    tue = pd.Timestamp("2026-09-29 09:15:00", tz=IST)  # Tuesday
    wed = pd.Timestamp("2026-09-30 09:15:00", tz=IST)  # Wednesday
    df = pd.concat([_bars(75, tue), _bars(60, wed)], ignore_index=True)
    decision = df["timestamp"].iloc[-1]  # Wed 14:10 (last provided 5m start)
    ctx = compute_24h_context(df, decision_ts=decision, window_hours=24.0)
    # Window = [Wed 14:10 − 24h = Tue 14:10, Wed 14:10] → Wed's 60 bars +
    # Tue 14:10..15:25 (16 bars) = 76 real bars, no synthetic ones.
    assert ctx["candles"] == 76
    assert ctx["trading_dates"] == ["2026-09-29", "2026-09-30"]
    assert "2 session(s)" in ctx["session_coverage"]
    assert ctx["candles"] == len(df[(df["timestamp"] >= pd.Timestamp(ctx["window_start"]))
                                    & (df["timestamp"] <= pd.Timestamp(ctx["decision_timestamp"]))])
    assert ctx["close_now"] is not None and ctx["close_24h_ago"] is not None


def test_weekend_window_has_no_fabricated_bars():
    # Friday session + Monday session with a >24h weekend gap: a 24-clock-hour
    # window ending Monday 09:30 contains ONLY the 4 real Monday bars — the
    # weekend is treated as missing data, never topped up with fake candles.
    fri = pd.Timestamp("2026-09-25 09:15:00", tz=IST)  # Friday
    mon = pd.Timestamp("2026-09-28 09:15:00", tz=IST)  # Monday
    df = pd.concat([_bars(75, fri), _bars(60, mon)], ignore_index=True)
    decision = df["timestamp"].iloc[78]  # Mon 09:30
    ctx = compute_24h_context(df, decision_ts=decision, window_hours=24.0)
    assert ctx["candles"] == 4  # Mon 09:15, 09:20, 09:25, 09:30 only
    assert ctx["trading_dates"] == ["2026-09-28"]
    assert "1 session(s)" in ctx["session_coverage"]
    assert pd.Timestamp(ctx["first_timestamp"]) == pd.Timestamp("2026-09-28 09:15:00", tz=IST)


def test_insufficient_data_is_neutral():
    assert compute_24h_context(None)["candles"] == 0
    assert compute_24h_context(pd.DataFrame())["sufficient"] is False
    one = _bars(1, pd.Timestamp("2026-09-29 09:15:00", tz=IST))
    ctx = compute_24h_context(one, decision_ts=one["timestamp"].iloc[0], window_hours=24.0)
    assert ctx["candles"] == 1
    assert ctx["sufficient"] is False
    assert ctx["net"] == 0.0
    assert ctx["range_position"] in (0.5, None)


def test_lookahead_excludes_bars_after_decision():
    df = _bars(100, pd.Timestamp("2026-09-29 09:15:00", tz=IST))
    decision = df["timestamp"].iloc[80]  # decision BEFORE the frame end
    ctx = compute_24h_context(df, decision_ts=decision, window_hours=24.0)
    assert ctx["candles"] == 81  # bars 0..80 only
    assert pd.Timestamp(ctx["last_timestamp"]) <= decision
    # The last 19 bars (which "happen later") must NOT be counted.
    assert pd.Timestamp(ctx["last_timestamp"]) == df["timestamp"].iloc[80]


def test_24h_net_sign_and_bounds():
    # Price rising through the window: range_position near 1 and close above
    # ema_50 → positive net; bounds must stay within [-1, 1].
    start = pd.Timestamp("2026-09-29 09:15:00", tz=IST)
    df = _bars(75, start)
    df["close"] = 100.0 + np.arange(75) * 0.05  # steady climb
    df["high"] = df["close"] + 0.2
    df["low"] = df["close"] - 0.2
    df["open"] = df["close"] - 0.05
    df["ema_50"] = np.full(75, 100.0)
    ctx = compute_24h_context(df, decision_ts=df["timestamp"].iloc[-1], window_hours=24.0)
    assert -1.0 <= ctx["net"] <= 1.0
    assert ctx["net"] >= 0.0
    assert ctx["range_net"] >= 0.0


# ────────────────────────────────────────────────────────────────────────────
# Signal engine: flag OFF byte-identical / flag ON additive
# ────────────────────────────────────────────────────────────────────────────

def test_engine_identical_without_context():
    # Flag OFF == the pre-feature engine: passing context_24h=None must not
    # change any decision, score, or setup field.
    row = _bull_row()
    plain = evaluate_row_signal(row, market_context=_mkctx())
    explicit_none = evaluate_row_signal(row, market_context=_mkctx(), context_24h=None)
    assert plain["direction"] == explicit_none["direction"]
    assert plain["confidence"] == explicit_none["confidence"]
    assert plain["signal_score"].model_dump() == explicit_none["signal_score"].model_dump()
    assert plain["setup"].model_dump() == explicit_none["setup"].model_dump()
    assert plain["reasons"] == explicit_none["reasons"]
    assert explicit_none["context_24h"] is None


def test_engine_net_zero_context_is_neutral():
    # A context with net=0 (insufficient data window) is equivalent to no
    # context — the additive blend must be idempotent at zero.
    row = _bull_row()
    plain = evaluate_row_signal(row, market_context=_mkctx())
    zero = evaluate_row_signal(row, market_context=_mkctx(),
                               context_24h={"net": 0.0, "candles": 0})
    assert plain["signal_score"].total == zero["signal_score"].total
    assert plain["direction"] == zero["direction"]


def test_engine_uses_finite_24h_net_additively():
    # Flag ON: a +1.0 24H net adds bullish evidence to a NEUTRAL NIFTY context
    # and pushes market-context score up without touching the trade setup.
    row = _bull_row()
    plain = evaluate_row_signal(row, market_context=_mkctx())
    ctx_24h = evaluate_row_signal(
        row, market_context=_mkctx(),
        context_24h={"net": 1.0, "candles": 200, "window_hours": 24.0},
    )
    assert ctx_24h["signal_score"].market_context_score > \
        plain["signal_score"].market_context_score
    assert ctx_24h["signal_score"].total != plain["signal_score"].total
    # Setup and Risk Quality are strategy-owned — the 24H context never
    # overrides SL / T1 / T2 / R:R.
    assert ctx_24h["setup"].model_dump() == plain["setup"].model_dump()
    assert ctx_24h["signal_score"].risk_quality_score == \
        plain["signal_score"].risk_quality_score
    assert ctx_24h["context_24h"] is not None


def test_evaluate_signal_accepts_context_and_passes_id():
    start = pd.Timestamp("2026-09-29 09:15:00", tz=IST)
    df = _bars(120, start)
    # Provide the remaining indicator columns (matching a compute_all_indicators
    # frame) so the last row is fully populated; evaluate_signal only consumes
    # the final bar.
    for col, val in {
        "ema_9": 100.0, "ema_20": 100.0, "adx_14": 22.0, "vwap": 100.0,
        "rsi_14": 55.0, "macd": 0.0, "macd_signal": 0.0,
        "macd_histogram": 0.0, "roc_5": 0.0, "distance_from_vwap": 0.0,
        "prev_high": 102.0, "prev_low": 98.0,
        "opening_range_high": 102.0, "opening_range_low": 98.0,
        "bb_upper": 103.0, "bb_lower": 97.0, "volatility_20": 0.3,
    }.items():
        df[col] = val
    df.at[df.index[-1], "close"] = 103.0
    df.at[df.index[-1], "high"] = 103.4
    df.at[df.index[-1], "low"] = 102.6
    sig_on = evaluate_signal(
        df, "TEST",
        data_source="yfinance",
        market_context=_mkctx(),
        data_age_seconds=0,
        data_status="OK",
        context_24h={"net": 0.8, "candles": 200},
    )
    assert sig_on is not None
    assert sig_on["context_24h"] == {"net": 0.8, "candles": 200}
    assert isinstance(sig_on["id"], str) and len(sig_on["id"]) == 16
    sig_off = evaluate_signal(
        df, "TEST",
        data_source="yfinance",
        market_context=_mkctx(),
        data_age_seconds=0,
        data_status="OK",
    )
    assert sig_off["context_24h"] is None


def test_net_market_context_blend_math():
    neutral = _net_market_context({
        "nifty_trend": "NEUTRAL", "nifty_change_pct": 0.0,
        "symbol_24h": {"net": 0.5},
    })
    assert neutral[0] == pytest.approx(0.5)
    assert any("Symbol 24H context" in r for r in neutral[1])
    bullish = _net_market_context({
        "nifty_trend": "BULLISH", "nifty_change_pct": 0.5,
        "symbol_24h": {"net": -1.0},
    })
    assert bullish[0] == pytest.approx(0.0)  # +1.0 + (−1.0) → clipped to 0
    without24 = _net_market_context({"nifty_trend": "BULLISH", "nifty_change_pct": 0.5})
    assert without24[0] == pytest.approx(1.0)  # unchanged no symbol_24h
    nan_ctx = _net_market_context({
        "nifty_trend": "BEARISH", "nifty_change_pct": -0.5,
        "symbol_24h": {"net": float("nan")},  # non-finite → ignored
    })
    assert nan_ctx[0] == pytest.approx(-1.0)


# ────────────────────────────────────────────────────────────────────────────
# signal_store — schema, persistence, usage, outcome
# ────────────────────────────────────────────────────────────────────────────

def _fake_signal(direction="LONG", score_total=72.0, sig_id="sigabc1234567890"):
    return {
        "id": sig_id,
        "symbol": "TEST",
        "timestamp": "2026-09-29 13:50:00",
        "direction": direction,
        "confidence": 0.82,
        "signal_score": {
            "total": score_total, "trend_score": 18.0, "momentum_score": 12.0,
            "volume_score": 11.0, "vwap_score": 11.0, "price_action_score": 10.0,
            "market_context_score": 5.0, "risk_quality_score": 8.0,
        },
        "setup": {"entry": 100.0, "stop_loss": 98.0, "target_1": 105.0,
                  "target_2": 107.0, "risk_reward_ratio": 2.5},
        "explanation": {"reasons": ["Trend up"], "risks": ["Volatile"]},
        "strategy": "multi_factor",
        "data_source": "yfinance",
        "signal_generated_at": "2026-09-29 13:51:20",
        "direction_evidence": None,
        "context_24h": {"net": 0.6, "candles": 74},
    }


def test_schema_migration_is_additive(tmp_db):
    # Build an OLD-shape signals table and run the migration: the new columns
    # appear, old rows survive, and the shadow table is created.
    con = sqlite3.connect(tmp_db)
    con.execute("CREATE TABLE signals (id VARCHAR(16) PRIMARY KEY, symbol VARCHAR(50), "
                "timestamp DATETIME, direction VARCHAR(20), signal_score FLOAT)")
    con.execute("INSERT INTO signals VALUES ('oldrow1', 'AAA', '2026-01-01 09:15:00', 'LONG', 66.0)")
    con.execute("CREATE TABLE paper_positions (id VARCHAR(16) PRIMARY KEY, symbol VARCHAR(50))")
    con.commit()
    con.close()
    signal_store.ensure_signal_schema(tmp_db)
    con = sqlite3.connect(tmp_db)
    sig_cols = {r[1] for r in con.execute("PRAGMA table_info(signals)").fetchall()}
    pos_cols = {r[1] for r in con.execute("PRAGMA table_info(paper_positions)").fetchall()}
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"signal_quality", "quantity", "risk_amount"} <= sig_cols
    assert "signal_id" in pos_cols
    assert "signal_24h_comparisons" in tables
    old = con.execute("SELECT symbol, direction FROM signals WHERE id='oldrow1'").fetchone()
    assert old == ("AAA", "LONG")
    con.close()


def test_persist_signal_single_row_per_candle(tmp_db):
    sig = _fake_signal()
    candle = pd.Timestamp("2026-09-29 13:50:00", tz=IST)
    signal_store.persist_signal(sig, candle_ts=candle, market_ctx=_mkctx(),
                                quality="QUALIFIED", db_path=tmp_db)
    signal_store.persist_signal(sig, candle_ts=candle, market_ctx=_mkctx(),
                                quality="QUALIFIED", db_path=tmp_db)  # upsert
    con = sqlite3.connect(tmp_db)
    rows = con.execute(
        "SELECT symbol, timestamp, direction, signal_score, signal_quality, "
        "quantity, risk_amount FROM signals WHERE symbol='TEST'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "TEST"
    assert rows[0][2] == "LONG"
    assert rows[0][3] == pytest.approx(72.0)
    assert rows[0][4] == "QUALIFIED"
    assert rows[0][5] is None  # usage not yet recorded
    ind = json.loads(con.execute(
        "SELECT indicator_scores FROM signals WHERE symbol='TEST'").fetchone()[0])
    assert ind["trend_score"] == 18.0
    mark = json.loads(con.execute(
        "SELECT market_context FROM signals WHERE symbol='TEST'").fetchone()[0])
    assert mark["context_24h"]["net"] == 0.6
    con.close()


def test_persist_24h_comparison_and_agreement(tmp_db):
    sig = _fake_signal(direction="LONG")
    sig24 = _fake_signal(direction="SHORT", score_total=48.0, sig_id="otherid123456789")
    candle = pd.Timestamp("2026-09-29 13:50:00", tz=IST)
    signal_store.persist_24h_comparison(sig, sig24, {"net": 0.6}, candle_ts=candle,
                                        quality="QUALIFIED", quality24="NORMAL",
                                        db_path=tmp_db)
    con = sqlite3.connect(tmp_db)
    row = con.execute("SELECT current_direction, ctx24_direction, agreement, "
                      "ctx24_quality FROM signal_24h_comparisons").fetchone()
    assert row[0] == "LONG" and row[1] == "SHORT"
    assert row[2] == 0  # disagreement
    assert row[3] == "NORMAL"
    con.close()
    # Same (symbol, candle) re-persist must not duplicate.
    signal_store.persist_24h_comparison(sig, sig24, {"net": 0.6}, candle_ts=candle,
                                        quality="QUALIFIED", quality24="NORMAL",
                                        db_path=tmp_db)
    con = sqlite3.connect(tmp_db)
    assert con.execute("SELECT COUNT(*) FROM signal_24h_comparisons").fetchone()[0] == 1
    con.close()


def test_update_usage_and_mark_outcome(tmp_db):
    sig = _fake_signal()
    signal_store.persist_signal(sig, candle_ts="2026-09-29 13:50:00", db_path=tmp_db)
    signal_store.update_signal_usage("sigabc1234567890", 10, 20.0, db_path=tmp_db)
    signal_store.mark_signal_outcome("sigabc1234567890", 45.5, 7, db_path=tmp_db)
    con = sqlite3.connect(tmp_db)
    row = con.execute("SELECT quantity, risk_amount, outcome, realized_pnl, "
                      "holding_duration FROM signals WHERE id='sigabc1234567890'").fetchone()
    assert row == (10, 20.0, "WIN", 45.5, 7)
    con.close()


def test_place_order_carries_signal_id_and_persists(tmp_db):
    engine = PaperTradingEngine(db_path=tmp_db, persist=False)
    r = engine.place_order("TEST", "LONG", 10, 100.0, 98.0, 105.0, 107.0,
                           user_id="u24h", signal_id="sigabc1234567890")
    assert "error" not in r
    assert r["position"]["signal_id"] == "sigabc1234567890"
    # Restart persistence round-trip through the position table.
    persist_account_state("u24h", [r["position"]], [], db_path=tmp_db)
    positions, _ = load_account_state("u24h", db_path=tmp_db)
    assert any(p.get("id") == r["position"]["id"] and p.get("signal_id") == "sigabc1234567890"
               for p in positions)
    # An order WITHOUT signal_id must persist as None (legacy orders unaffected).
    r2 = engine.place_order("TEST2", "SHORT", 5, 200.0, 205.0, 190.0, user_id="u24h")
    assert r2["position"].get("signal_id") is None


# ────────────────────────────────────────────────────────────────────────────
# context_report — metrics per §13, no fabricated claims
# ────────────────────────────────────────────────────────────────────────────

def test_report_empty_has_no_data():
    report = build_context_comparison([], [], [], use_24h_context=False,
                                      window_hours=24.0)
    assert report["has_data"] is False
    assert report["data_source"] == "signals_ledger_vs_24h_shadow"
    assert report["use_24h_context"] is False
    assert report["ctx24"]["realized_stats"] is None
    assert report["ctx24"]["realized_pending"] is True
    assert "pending" in report["ctx24"]["pending_note"].lower()
    assert report["current"]["realized_stats"]["has_data"] is False
    assert report["agreement"]["agreement_rate"] is None


def test_report_signal_stats_and_agreement():
    cur = [
        {"symbol": "A", "direction": "LONG", "score": 70.0, "quality": "QUALIFIED"},
        {"symbol": "B", "direction": "SHORT", "score": 62.0, "quality": "NORMAL"},
        {"symbol": "C", "direction": "NO_TRADE", "score": 52.0, "quality": None},
    ]
    comps = [
        {"current_direction": "LONG", "ctx24_direction": "LONG", "ctx24_score": 71.0,
         "ctx24_quality": "QUALIFIED"},
        {"current_direction": "LONG", "ctx24_direction": "SHORT", "ctx24_score": 58.0,
         "ctx24_quality": "NORMAL"},
    ]
    report = build_context_comparison(cur, comps, [], use_24h_context=True,
                                      window_hours=24.0)
    assert report["has_data"] is True
    assert report["use_24h_context"] is True
    st = report["current"]["signal_stats"]
    assert st["total"] == 3 and st["long"] == 1 and st["short"] == 1 and st["no_trade"] == 1
    assert st["avg_score"] == pytest.approx(61.33, abs=0.01)
    assert st["quality_distribution"].get("QUALIFIED") == 1
    assert report["ctx24"]["signal_stats"]["total"] == 2
    assert report["ctx24"]["realized_pending"] is True
    assert report["agreement"]["total"] == 2
    assert report["agreement"]["agree_count"] == 1
    assert report["agreement"]["agreement_rate"] == pytest.approx(0.5)


def test_report_realized_stats_from_ledger_only():
    trades = [
        {"symbol": "A", "direction": "LONG", "entry_price": 100.0, "exit_price": 106.0,
         "stop_loss": 97.0, "target_1": 104.0, "pnl": 60.0, "status": "closed",
         "exit_reason": "TARGET_1"},
        {"symbol": "B", "direction": "SHORT", "entry_price": 100.0, "exit_price": 103.0,
         "stop_loss": 103.0, "target_1": 97.0, "pnl": -30.0, "status": "closed",
         "exit_reason": "STOP_LOSS"},
        {"symbol": "C", "direction": "LONG", "entry_price": 50.0, "exit_price": 51.0,
         "stop_loss": 48.0, "target_1": 52.0, "pnl": 10.0, "status": "closed",
         "exit_reason": None},
        {"symbol": "D", "direction": "LONG", "entry_price": 10.0, "pnl": 5.0,
         "status": "open"},  # open positions are NOT realized
    ]
    report = build_context_comparison([], [], trades, use_24h_context=False)
    rs = report["current"]["realized_stats"]
    assert rs["has_data"] is True
    assert rs["total_trades"] == 3
    assert rs["realized_pnl"] == pytest.approx(40.0)
    assert rs["win_rate"] == pytest.approx(2 / 3, abs=0.001)  # 0.6667 (rounded)
    assert rs["long"]["total_trades"] == 2
    assert rs["short"]["total_trades"] == 1
    assert rs["exit_breakdown"].get("T1") == 1
    assert rs["exit_breakdown"].get("SL") == 1
    assert rs["exit_breakdown"].get("MANUAL") == 1
    assert report["ctx24"]["realized_stats"] is None