"""Rotation engine — historical signal replay (Phase 4).

Replays the shared ``evaluate_row_signal`` (the same live/backtest logic the
scanner uses — verified single source of truth in Phase 1/2) over the 
evaluation window and derives the Phase 2 §9 signal metrics:

* session classification (VALID_EVALUATED / SESSION_INSUFFICIENT /
  SESSION_FAILURE) — stale/failed/insufficient sessions never enter any
  signal denominator and are never counted as NO_TRADE;
* signal frequency = signals / valid evaluated sessions (honest 0);
* independent LONG-family and SHORT-family quality evidence (frequency,
  consistency across weeks, data completeness during signals) with per-
  direction sample gates — LONG and SHORT are NEVER merged and NEVER derived
  from one another (Phase 2 §7/§8).

Outcome dimensions that require trade-level history (win rate, expectancy,
profit factor, false-signal rate — Phase 2 §7 dims 2–6, Open Question Q11) are
NOT fabricated here: they need persisted trade outcomes which do not exist in
the repo; the reported LONG/SHORT metrics are the observable replay subset and
this limitation is documented in PROJECT_REPORT.md.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Optional

import pandas as pd

from app.models.schemas import SignalDirection
from app.services import signal_engine
from app.services.indicators import calculate_all_indicators
from app.services.rotation_config import RotationEngineConfig
from app.services.rotation_eligibility import (
    SESSION_FAILURE,
    SESSION_INSUFFICIENT,
    VALID_EVALUATED,
    build_sessions,
    filter_session_frame,
)

LONG_FAMILY = {
    SignalDirection.STRONG_LONG.value,
    SignalDirection.LONG.value,
    SignalDirection.WEAK_LONG.value,
}
SHORT_FAMILY = {
    SignalDirection.STRONG_SHORT.value,
    SignalDirection.SHORT.value,
    SignalDirection.WEAK_SHORT.value,
}

_DATA_FAILURE_PREFIXES = (
    "Missing critical indicator",
    "Invalid OHLC",
    "Non-positive price",
    "Insufficient candle",
)


def _decision_value(decision: Optional[dict]):
    if not decision:
        return None
    d = decision.get("direction")
    return d.value if isinstance(d, SignalDirection) else d


def _is_data_failure(decision: Optional[dict]) -> bool:
    if decision is None:
        return True
    reasons = decision.get("reasons") or []
    return any(str(r).startswith(_DATA_FAILURE_PREFIXES) for r in reasons)


def _classify(decision: Optional[dict]) -> str:
    if _is_data_failure(decision):
        return SESSION_FAILURE
    d = _decision_value(decision)
    if d in LONG_FAMILY:
        return "LONG"
    if d in SHORT_FAMILY:
        return "SHORT"
    return "NO_TRADE"


def _iso_week_key(d: date):
    iso = d.isocalendar()
    return (iso.year, iso.week)


def _cv(values: list[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    m = sum(values) / len(values)
    if m <= 0:
        return None
    var = sum((x - m) ** 2 for x in values) / len(values)
    return math.sqrt(var) / m


def _consistency(weekly_counts: list) -> float:
    """1 - min(1, CV of weekly direction counts); no counts → 0."""
    c = _cv([float(x) for x in weekly_counts])
    if c is None:
        return 0.0
    return max(0.0, 1.0 - min(1.0, c))


def _dq_share(session_data: list) -> float:
    if not session_data:
        return 0.0
    return sum(session_data) / len(session_data)


def _raw_blend(freq: float, consistency: float, dq_share: float) -> float:
    """Documented blend — sub-weights TO BE CALIBRATED (Phase 2 §7 composite
    guard: no single dimension > 50%). 0.5 frequency + 0.3 consistency +
    0.2 data-completeness-during-signals = 1.0 max."""
    return 0.5 * freq + 0.3 * consistency + 0.2 * dq_share


def replay_window(
    df: Optional[pd.DataFrame],
    expected_dates: list[date],
    cfg: RotationEngineConfig,
    market_context: Optional[dict] = None,
) -> dict:
    """Replay signals over the window; return the Phase 4 signal metric dict.

    Deterministic: no randomness, no iteration-order dependence, session order
    is timestamp-sorted and symbols are handled by the caller in sorted order.
    """
    empty = {
        "valid_evaluated_sessions": 0,
        "long_count": 0,
        "short_count": 0,
        "no_trade_count": 0,
        "total_signal_count": 0,
        "frequency": None,
        "long_frequency": None,
        "short_frequency": None,
        "long_sample_ok": False,
        "short_sample_ok": False,
        "long_raw": None,
        "short_raw": None,
        "session_class_counts": {},
        "weekly_long_counts": [],
        "weekly_short_counts": [],
        "long_dq_share": None,
        "short_dq_share": None,
    }
    if df is None or len(df) == 0:
        return empty

    filtered = filter_session_frame(df, expected_dates)
    if filtered is None or len(filtered) == 0:
        return empty
    records, _ = build_sessions(df, expected_dates, cfg)
    indicators = calculate_all_indicators(filtered.copy())

    long_sessions: list[int] = []
    short_sessions: list[int] = []
    no_trade_count = 0
    fail_count = 0
    insufficient_count = 0
    long_bars_ratio: list[float] = []
    short_bars_ratio: list[float] = []
    weekly_long: dict = {}
    weekly_short: dict = {}

    for rec in records:
        if rec.session_class == SESSION_INSUFFICIENT:
            insufficient_count += 1
            continue
        if rec.session_class != VALID_EVALUATED or rec.end_index < 0:
            continue
        if rec.end_index >= len(indicators):
            continue
        row = indicators.iloc[rec.end_index]
        decision = signal_engine.evaluate_row_signal(
            row, market_context=market_context, strategy_version=None
        )
        kind = _classify(decision)
        bar_ratio = min(1.0, rec.n_bars / max(1, rec.expected_bars))
        if kind == "LONG":
            long_sessions.append(rec.end_index)
            long_bars_ratio.append(bar_ratio)
            key = _iso_week_key(rec.day)
            weekly_long[key] = weekly_long.get(key, 0) + 1
        elif kind == "SHORT":
            short_sessions.append(rec.end_index)
            short_bars_ratio.append(bar_ratio)
            key = _iso_week_key(rec.day)
            weekly_short[key] = weekly_short.get(key, 0) + 1
        elif kind == "NO_TRADE":
            no_trade_count += 1
        else:  # SESSION_FAILURE
            fail_count += 1

    valid = len(long_sessions) + len(short_sessions) + no_trade_count
    long_count = len(long_sessions)
    short_count = len(short_sessions)
    frequency = (long_count + short_count) / valid if valid else None
    long_freq = long_count / valid if valid else 0.0
    short_freq = short_count / valid if valid else 0.0

    return {
        "valid_evaluated_sessions": valid,
        "long_count": long_count,
        "short_count": short_count,
        "no_trade_count": no_trade_count,
        "total_signal_count": long_count + short_count,
        "frequency": frequency,
        "long_frequency": long_freq,
        "short_frequency": short_freq,
        "long_sample_ok": long_count >= cfg.min_signal_sample_long,
        "short_sample_ok": short_count >= cfg.min_signal_sample_short,
        "long_raw": _raw_blend(long_freq, _consistency(list(weekly_long.values())),
                               _dq_share(long_bars_ratio)) if valid else None,
        "short_raw": _raw_blend(short_freq, _consistency(list(weekly_short.values())),
                                _dq_share(short_bars_ratio)) if valid else None,
        "session_class_counts": {
            VALID_EVALUATED: valid,
            SESSION_INSUFFICIENT: insufficient_count,
            SESSION_FAILURE: fail_count,
        },
        "weekly_long_counts": sorted(weekly_long.values()),
        "weekly_short_counts": sorted(weekly_short.values()),
        "long_dq_share": _dq_share(long_bars_ratio),
        "short_dq_share": _dq_share(short_bars_ratio),
    }