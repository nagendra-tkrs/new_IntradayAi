"""PHASE 5B DIAGNOSTIC — Core stability gate focused tests.

Covers the exact behaviors the Phase 5B diagnostic measured (dry-run found all
117 eligible candidates at trading_consistency=0.95 on the pinned snapshot):

* trading_consistency: numerator/denominator decomposition, zero-volume and
  missing sessions, market-closed (weekend/holiday) denominator isolation,
  boundary at exactly 99%, below 99%, provider failure (None).
* select_core gates: consistency failure, volume-CV failure, ATR failure,
  multiple simultaneous failures (merged rejection), all gates passing.
* Rotation independence: a candidate rejected by the Core gates can still be
  selected for Rotation (Core gates are Core-only, never inherited).

All tests deterministic; no DB, no network, no config changes.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from app.core import market_session
from app.services import rotation_eligibility as elig
from app.services import rotation_selection as selection
from app.services.rotation_config import RotationEngineConfig

IST = market_session.IST  # production IST tz (UTC+5:30)

# --- canonical explicit config (tests never read defaults) -------------------
def _cfg(**overrides):
    base = dict(
        core_size=30,
        rotation_size=50,
        candidate_pool_min=150,
        candidate_pool_max=250,
        window_weeks=2,
        min_observation_sessions=10,
        expected_bars_per_session=75,
        signal_warmup_bars=55,
        liquidity_floor=50_000_000.0,
        volume_floor=500_000.0,
        volume_cv_max=2.5,
        atr_min_pct=0.0008,
        atr_max_pct=0.05,
        atr_opt_low_pct=0.006,
        atr_opt_high_pct=0.02,
        min_data_quality=0.6,
        min_signal_sample_long=8,
        min_signal_sample_short=8,
        min_frequency_floor=0.0,
        core_trading_consistency_pct=99.0,
        core_volume_cv_max=2.5,
        core_review_consecutive_periods=2,
        core_replacement_score_delta=5.0,
        core_retention_bonus=0.03,
        anti_churn_score_delta=5.0,
        rotation_retention_floor=45.0,
        replacement_cooldown_weeks=2,
        cooldown_override_delta=10.0,
        max_churn_per_cycle=10,
    )
    base.update(overrides)
    return RotationEngineConfig(**base)


# --- synthetic frame helpers -------------------------------------------------
def _session_frame(
    day: date,
    n_bars: int = 75,
    volume: float = 1_000_000.0,
    open_p: float = 100.0,
    close_p: float = 101.0,
) -> pd.DataFrame:
    """Deterministic 5-minute session for one trading day (09:15→15:30 IST)."""
    starts = [datetime.combine(day, time(9, 15)) + i * timedelta(minutes=5)
              for i in range(n_bars)]
    ts = pd.Series([t.replace(tzinfo=IST) for t in starts], name="timestamp")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [open_p] * n_bars,
            "high": [max(open_p, close_p)] * n_bars,
            "low": [min(open_p, close_p)] * n_bars,
            "close": [close_p] * n_bars,
            "volume": [volume] * n_bars,
        }
    )


def _multi_day_frame(days, **kw):
    parts = [_session_frame(d, **kw) for d in days]
    return pd.concat(parts, ignore_index=True)


def _trading_days(days: list[date]) -> list[date]:
    """Confirmed-trading-day subset of the given dates."""
    return [d for d in days if elig.is_confirmed_trading_day(d)]


def _evaluated_consistency(df, days, cfg=None) -> float:
    """trading_consistency as computed by evaluate_candidate over df."""
    cfg = cfg or _cfg()
    expected = _trading_days(days)
    ev = elig.evaluate_candidate("TEST", df, expected, cfg, signal_metrics=None)
    return ev.trading_consistency


def _cand(
    symbol="S",
    score=60.0,
    dq=8.0,
    liq=1e9,
    vol_cv=0.2,
    atr=0.01,
    consistency=1.0,
    reliability=1.0,
):
    return selection.RotationCandidate(
        symbol=symbol,
        rotation_score=score,
        data_quality_score=dq,
        median_daily_value=liq,
        volume_cv=vol_cv,
        median_atr_pct=atr,
        trading_consistency=consistency,
        signal_reliability=reliability,
        expected_sessions=10,
        present_sessions=10,
    )


TUE = date(2026, 9, 15)
WED = date(2026, 9, 16)
THU = date(2026, 9, 17)
FRI = date(2026, 9, 18)
SAT = date(2026, 9, 19)  # Saturday — not a trading day
MON = date(2026, 9, 21)


# ===========================================================================
# A — trading_consistency: numerator / denominator / missing data
# ===========================================================================
def test_a1_full_coverage_is_1_0():
    days = [TUE, WED]
    df = _multi_day_frame(days)
    assert _evaluated_consistency(df, days) == pytest.approx(1.0)


def test_a2_one_missing_session_penalizes_denominator():
    days = [TUE, WED]
    df = _session_frame(TUE)  # THU expected but absent from provider data
    assert _evaluated_consistency(df, days) == pytest.approx(0.5)


def test_a3_zero_volume_session_is_not_counted_in_numerator():
    days = [TUE, WED, THU]
    df = pd.concat(
        [_session_frame(TUE), _session_frame(WED, volume=0.0), _session_frame(THU)],
        ignore_index=True,
    )
    # 2 of 3 expected days traded volume > 0
    assert _evaluated_consistency(df, days) == pytest.approx(2 / 3)


def test_a4_market_closed_days_excluded_from_denominator():
    days = [TUE, WED, SAT]  # Saturday inside the window
    expected = _trading_days(days)
    assert expected == [TUE, WED]  # weekend NOT in the denominator
    df = _multi_day_frame([TUE, WED])
    assert _evaluated_consistency(df, days) == pytest.approx(1.0)


def test_a5_repo_calendar_holiday_excluded_from_denominator():
    # 2026-01-26 Republic Day is in the repo NSE_HOLIDAYS
    jan = date(2026, 1, 26)
    jan2 = date(2026, 1, 27)
    days = [jan, jan2]
    expected = _trading_days(days)
    assert expected == [jan2]  # holiday not a trading day
    df = _session_frame(jan2)
    assert _evaluated_consistency(df, days) == pytest.approx(1.0)


def test_a6_provider_failure_yields_none_consistency():
    ev = elig.provider_failure_evaluation("LOST", "boom")
    assert ev.trading_consistency is None
    # select_core treats None consistency as a hard fail
    core, res = selection.select_core([_cand("LOST", consistency=None)], _cfg())
    assert core == []
    assert any("N/A" in r["reasons"] for r in res.core_rejections)


def test_a7_calendar_fix_2026_09_14_is_official_holiday():
    """Phase 5C calendar correction: 2026-09-14 (Ganesh Chaturthi) IS in the
    official NSE CM holiday-master, so it is excluded from the trading-day
    denominator. This reverses the Phase 5B pinned assertion in the same change
    that introduced the authoritative calendar source (the 0.95 → 1.00 fix is
    the calendar source, not the formula)."""
    assert elig.is_confirmed_trading_day(date(2026, 9, 14)) is False
    assert market_session.is_holiday(date(2026, 9, 14)) is True


# ===========================================================================
# B — Core gate boundaries (99% comparison operator is `*100 < 99.0` → fail)
# ===========================================================================
def test_b1_exactly_99_pct_passes():
    core, res = selection.select_core([_cand("EXACT", consistency=0.99)], _cfg())
    assert [c.symbol for c in core] == ["EXACT"]
    assert res.core_rejections == []


def test_b2_below_99_pct_fails():
    core, res = selection.select_core(
        [_cand("LOW", consistency=0.989999)], _cfg()
    )
    assert core == []
    assert any(r["symbol"] == "LOW" for r in res.core_rejections)
    assert "trading consistency 98.9999%" in res.core_rejections[0]["reasons"]


def test_b3_volume_cv_above_2_5_fails_only_that_gate():
    core, res = selection.select_core(
        [_cand("HICV", consistency=1.0, vol_cv=3.1)], _cfg()
    )
    assert core == []
    assert any("volume CV 3.10 > 2.5" in r["reasons"] for r in res.core_rejections)


def test_b4_volume_cv_none_passes_core_gate():
    core, res = selection.select_core(
        [_cand("NOCCV", consistency=1.0, vol_cv=None)], _cfg()
    )
    assert [c.symbol for c in core] == ["NOCCV"]


def test_b5_atr_unavailable_fails_only_that_gate():
    core, res = selection.select_core(
        [_cand("NOATR", consistency=1.0, atr=None)], _cfg()
    )
    assert core == []
    assert any("median %ATR unavailable" in r["reasons"] for r in res.core_rejections)


def test_b6_multiple_simultaneous_failures_merged():
    core, res = selection.select_core(
        [_cand("BOTH", consistency=0.90, vol_cv=4.0, atr=None)], _cfg()
    )
    assert core == []
    assert len(res.core_rejections) == 1
    reasons = res.core_rejections[0]["reasons"]
    assert "trading consistency" in reasons
    assert "volume CV" in reasons
    assert "median %ATR unavailable" in reasons


def test_b7_all_gates_passing_selects_and_shortfall_zero():
    cands = [_cand(f"S{i:02d}", consistency=1.0) for i in range(30)]
    core, res = selection.select_core(cands, _cfg())
    assert len(core) == 30
    assert res.core_shortfall == 0


# ===========================================================================
# C — Rotation independence (Core gates never leak into Rotation)
# ===========================================================================
def test_c1_core_rejected_candidate_still_rotation_eligible():
    # Highest rotation score but Core-rejected (consistency 0.5, CV 9.9).
    high = _cand("CHURN", score=100.0, consistency=0.5, vol_cv=9.9)
    low = _cand("STABLE", score=40.0, consistency=1.0)
    core, res = selection.select_core([high, low], _cfg())
    assert [c.symbol for c in core] == ["STABLE"]  # CHURN fails the Core gate
    assert any(r["symbol"] == "CHURN" for r in res.core_rejections)
    # Rotation selects CHURN first by rotation score, ignoring Core gates.
    rotation, _ = selection.select_rotation([low, high], _cfg())
    assert {c.symbol for c in rotation} == {"CHURN", "STABLE"}
    assert rotation[0].symbol == "CHURN"  # top rotation score wins, no core gate


def test_c2_rotation_has_no_consistency_or_atr_gate():
    a = _cand("A", score=50.0, consistency=0.10, atr=None)
    b = _cand("B", score=49.0, consistency=1.0)
    rotation, _ = selection.select_rotation([a, b], _cfg())
    assert {c.symbol for c in rotation} == {"A", "B"}
    assert rotation[0].symbol == "A"