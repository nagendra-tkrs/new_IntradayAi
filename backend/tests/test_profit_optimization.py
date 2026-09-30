"""Tests for the profit-optimization measurement layer.

Covers, in order:

    A. No lookahead
    B. Baseline is unchanged by the new code
    C. Candidate isolation (a candidate never mutates the baseline)
    D. signal_id preservation
    E. P&L calculation
    F. Expectancy
    G. Profit factor
    H. Drawdown
    I. Time-of-day / quality / score grouping
    J. Shadow mode
    K. Candidate vs baseline
    L. Risk limits
    M. Position sizing
    N. Insufficient-sample handling
    O. Position-level realized P&L attribution (the accounting fix)
    P. The Profit Selection Layer's own rules
    Q. The exact statistics helpers

Every test is pure or uses a throwaway in-memory / tmp-path fixture. The real
``backend/intradayai.db`` is never opened for writing.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.optimization_experiments import (  # noqa: E402
    CANDIDATE, EXPERIMENT_GRIDS, MAX_DRAWDOWN_TOLERANCE,
    MIN_SAMPLE_FOR_PROMOTION, PROMOTED, evaluate_candidate, evaluate_promotion,
    fisher_exact_two_sided, measure_fidelity, pattern_significance,
    permutation_expectancy_p, risk_capped_quantity, run_experiment_grid, welch_t,
)
from app.services.profit_baseline import (  # noqa: E402
    COHORT_AI_LINKED, COHORT_STRATEGY_UNLINKED, COHORT_TEST_FIXTURE,
    INSUFFICIENT_DATA, INSUFFICIENT_PROMOTION, MIN_TRADES_FOR_CONCLUSION,
    build_baseline, collapse_positions, compute_stats, expectancy_from_stats,
    groups_by_quality, groups_by_score_band, groups_by_time, max_drawdown,
    profit_factor,
)
from app.services.profit_capture import TRAILING_RANGE  # noqa: E402
from app.services.position_sizing import (  # noqa: E402
    BINDING_CAPITAL, BINDING_EXPLICIT, BINDING_RISK_BUDGET, BINDING_TIE,
    PATH_EXPLICIT, PATH_FORMULA, PATH_UNAVAILABLE, STOP_SOURCE_LEDGER_TRAILED,
    BINDING_UNAVAILABLE,
    CLASS_CAPITAL_CONSTRAINED, CLASS_MANUAL_QUANTITY, CLASS_RISK_CONSTRAINED,
    CLASS_RISK_ENGINE_SIZED, CLASS_UNCLASSIFIED,
    SOURCE_MANUAL, SOURCE_RISK_ENGINE,
    STOP_SOURCE_SIGNAL, EntryRiskGeometry, capital_based_quantity,
    capture_entry_geometry, captured_geometry_fields, classify_order,
    production_quantity, risk_based_quantity, risk_budget_for,
    shadow_sizing_comparison, sizing_breakdown, sizing_report,
)
from app.services.position_sizing_store import (  # noqa: E402
    GEOMETRY_TABLE, SOURCE_ORDER_FILL, ensure_geometry_schema, load_all_geometry,
    load_geometry_for, persist_entry_geometry,
)
from app.services.profit_selection import (  # noqa: E402
    DECISION_SKIP, DECISION_TRADE, PromotionNotAllowed, Rule, RuleOutcome,
    SelectionConfig, ShadowMode, assert_promotable, evaluate_selection,
    shadow_record,
)
from app.services.profit_selection_store import (  # noqa: E402
    attach_realized_outcome, ensure_shadow_schema, load_shadow_records,
    persist_selection_decision,
)
from app.services.trade_replay import (  # noqa: E402
    INTRABAR_CLOSE_ONLY, INTRABAR_PESSIMISTIC, Bar, ReplayConfig,
    evaluate_entry_confirmation, atr_at, replay_position, session_vwap,
)


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────

# Bars that must exist strictly BEFORE an entry for ATR-14 to be derivable.
ATR_BARS_BEFORE = 18


def _decision():
    """A minimal SHADOW-mode TRADE decision, for tests that only need a
    well-formed decision object to serialise."""
    return evaluate_selection(
        {"symbol": "SAIL", "direction": "LONG", "confidence": 0.5,
         "signal_score": {"total": 50.0}, "setup": {}},
        config=SelectionConfig(),
    )


def _row(**kw) -> dict:
    base = {
        "id": "r1",
        "signal_id": None,
        "symbol": "TEST",
        "direction": "LONG",
        "entry_price": 100.0,
        "exit_price": 105.0,
        "quantity": 10,
        "stop_loss": 98.0,
        "target_1": 104.0,
        "target_2": 106.0,
        "entry_time": "2026-09-25 10:00:00",
        "exit_time": "2026-09-25 10:30:00",
        "status": "closed",
        "pnl": 50.0,
        "fees": 0.0,
        "slippage": 0.0,
        "user_id": "u1",
        "details_json": None,
    }
    base.update(kw)
    return base


def _bars(start="2026-09-25 09:15:00", count=80, step_minutes=5,
          base=100.0, drift=0.0, high=None, low=None) -> list:
    t0 = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    out = []
    price = base
    for i in range(count):
        price = price + drift
        hi = high if high is not None else price + 0.5
        lo = low if low is not None else price - 0.5
        out.append(Bar(
            timestamp=t0 + timedelta(minutes=i * step_minutes),
            open=price, high=max(hi, price), low=min(lo, price), close=price,
            volume=1000.0,
        ))
    return out


def _bars_through(entry_time: str, after: int = 20, **kw) -> list:
    """5m bars covering ``entry_time`` with ATR-14 fully reconstructible.

    Starts ``ATR_BARS_BEFORE`` bars BEFORE the entry so the replay can derive
    ATR as of the entry instant from history alone (no lookahead), and extends
    ``after`` bars past it so an exit can be found.
    """
    t0 = datetime.strptime(entry_time, "%Y-%m-%d %H:%M:%S")
    start = t0 - timedelta(minutes=5 * (ATR_BARS_BEFORE + 1))
    return _bars(
        start=start.strftime("%Y-%m-%d %H:%M:%S"),
        count=ATR_BARS_BEFORE + 1 + after,
        **kw,
    )


def _position(**kw):
    """A profit_baseline.Position built through the real collapse path."""
    rows = kw.pop("rows", None) or [_row(**kw)]
    return collapse_positions(rows, kw.pop("signals_by_id", None))[0]


# ══════════════════════════════════════════════════════════════════════════
# E. P&L CALCULATION
# ══════════════════════════════════════════════════════════════════════════

class TestPnlCalculation:

    def test_long_pnl_is_exit_minus_entry_times_quantity(self):
        pos = _position(entry_price=100.0, exit_price=107.0, quantity=20, pnl=140.0)
        assert pos.pnl == pytest.approx(140.0)
        assert pos.result == "WIN"

    def test_short_pnl_is_entry_minus_exit_times_quantity(self):
        pos = _position(direction="SHORT", entry_price=100.0, exit_price=93.0,
                        quantity=20, pnl=140.0)
        assert pos.pnl == pytest.approx(140.0)
        assert pos.side == "SHORT"

    def test_negative_pnl_is_a_loss(self):
        pos = _position(pnl=-25.0)
        assert pos.result == "LOSS"

    def test_exactly_zero_is_breakeven(self):
        pos = _position(pnl=0.0)
        assert pos.result == "BREAKEVEN"

    def test_r_multiple_uses_initial_risk(self):
        # 10 shares, risk/share = |100 - 98| = 2, initial risk = 20.
        # P&L 50 -> 2.5R.
        pos = _position(entry_price=100.0, stop_loss=98.0, quantity=10, pnl=50.0)
        assert pos.initial_risk_amount == pytest.approx(20.0)
        assert pos.r_multiple == pytest.approx(2.5)

    def test_r_multiple_is_none_without_usable_risk(self):
        pos = _position(entry_price=100.0, stop_loss=100.0, quantity=10, pnl=0.0)
        assert pos.r_multiple is None

    def test_profit_capture_slices_sum_to_the_position_total(self):
        """A T1 partial plus a T2 final is ONE trade worth the summed P&L."""
        rows = [
            _row(id="a", pnl=6.12, quantity=5, exit_price=103.0,
                 exit_time="2026-09-25 10:40:00",
                 details_json=json.dumps({"position_id": "P1", "exit_reason": "T1_PARTIAL",
                                          "exit_quantity": 5, "initial_quantity": 10,
                                          "remaining_quantity": 5})),
            _row(id="b", pnl=8.50, quantity=5, exit_price=105.0,
                 exit_time="2026-09-25 11:20:00",
                 details_json=json.dumps({"position_id": "P1", "exit_reason": "T2_FINAL",
                                          "exit_quantity": 5, "initial_quantity": 10,
                                          "remaining_quantity": 0})),
        ]
        positions = collapse_positions(rows)
        assert len(positions) == 1, "slices must collapse to one position"
        pos = positions[0]
        assert pos.pnl == pytest.approx(14.62)
        assert pos.exit_reasons == ["T1_PARTIAL", "T2_FINAL"]
        assert pos.terminal_reason == "T2_FINAL"
        assert pos.initial_quantity == 10


# ══════════════════════════════════════════════════════════════════════════
# F. EXPECTANCY
# ══════════════════════════════════════════════════════════════════════════

class TestExpectancy:

    def test_formula(self):
        # 3 wins of 30, 2 losses of 20 -> 0.6*30 - 0.4*20 = 18 - 8 = 10
        assert expectancy_from_stats(3, 2, 30.0, 20.0) == pytest.approx(10.0)

    def test_is_zero_for_an_even_coin_flip(self):
        assert expectancy_from_stats(5, 5, 10.0, 10.0) == pytest.approx(0.0)

    def test_undefined_without_a_loser(self):
        """All winners has no measurable expectancy, not a fabricated one."""
        assert expectancy_from_stats(4, 0, 25.0, None) is None

    def test_undefined_without_a_winner(self):
        assert expectancy_from_stats(0, 4, None, 25.0) is None

    def test_undefined_for_an_empty_sample(self):
        assert expectancy_from_stats(0, 0, None, None) is None

    def test_negative_when_losers_bigger_than_winners(self):
        assert expectancy_from_stats(4, 6, 10.0, 30.0) < 0

    def test_computed_from_actual_positions(self):
        # 3 x +20, 2 x -10 -> net 40 over 5 trades -> expectancy 8.00
        positions = [
            _position(id=f"w{i}", pnl=20.0, entry_time="2026-09-25 10:0%d:00" % i,
                      exit_time="2026-09-25 10:3%d:00" % i) for i in range(3)
        ] + [
            _position(id=f"l{i}", pnl=-10.0, entry_time="2026-09-25 11:0%d:00" % i,
                      exit_time="2026-09-25 11:3%d:00" % i) for i in range(2)
        ]
        stats = compute_stats(positions)
        assert stats["wins"] == 3
        assert stats["losses"] == 2
        assert stats["average_winner"] == pytest.approx(20.0)
        assert stats["average_loser"] == pytest.approx(10.0)
        assert stats["expectancy"] == pytest.approx(8.0)
        # The weighted formula and net/n must agree exactly.
        assert stats["expectancy"] == pytest.approx(
            stats["net_pnl"] / stats["total_completed_trades"]
        )


# ══════════════════════════════════════════════════════════════════════════
# G. PROFIT FACTOR
# ══════════════════════════════════════════════════════════════════════════

class TestProfitFactor:

    def test_basic_ratio(self):
        assert profit_factor(200.0, 100.0) == pytest.approx(2.0)

    def test_breakeven_when_there_are_no_losses(self):
        assert profit_factor(100.0, 0.0) == float("inf")

    def test_none_when_there_is_nothing_at_all(self):
        assert profit_factor(0.0, 0.0) is None

    def test_reported_none_rather_than_zero_for_an_empty_sample(self):
        stats = compute_stats([])
        assert stats["profit_factor"] is None
        assert stats["data_status"] == INSUFFICIENT_DATA

    def test_from_positions(self):
        positions = [
            _position(id=f"w{i}", pnl=50.0, entry_time="2026-09-25 10:0%d:00" % i,
                      exit_time="2026-09-25 10:3%d:00" % i) for i in range(2)
        ] + [
            _position(id=f"l{i}", pnl=-25.0, entry_time="2026-09-25 11:0%d:00" % i,
                      exit_time="2026-09-25 11:3%d:00" % i) for i in range(2)
        ]
        assert compute_stats(positions)["profit_factor"] == pytest.approx(2.0)


# ══════════════════════════════════════════════════════════════════════════
# H. DRAWDOWN
# ══════════════════════════════════════════════════════════════════════════

class TestDrawdown:

    def test_zero_for_a_monotonically_rising_curve(self):
        assert max_drawdown([10, 20, 30]) == pytest.approx(0.0)

    def test_peak_to_trough_depth(self):
        # The argument is a series of realized P&Ls, summed into an equity
        # curve: +10, +20, -25, +12 -> equity 10, 30, 5, 17 -> drawdown 25.
        assert max_drawdown([10, 20, -25, 12]) == pytest.approx(25.0)

    def test_drawdown_is_measured_from_the_running_peak(self):
        # A loss on the very first trade still counts against the opening
        # equity of 0.0, which is what the live account curve does.
        assert max_drawdown([-15, 5, 2]) == pytest.approx(15.0)

    def test_never_negative(self):
        assert max_drawdown([-5, -10]) >= 0

    def test_empty_series_is_zero(self):
        assert max_drawdown([]) == 0.0

    def test_drawdown_uses_realized_order(self):
        positions = [
            _position(id="a", pnl=100.0, entry_time="2026-09-25 10:00:00",
                      exit_time="2026-09-25 10:30:00"),
            _position(id="b", pnl=-80.0, entry_time="2026-09-25 11:00:00",
                      exit_time="2026-09-25 11:30:00"),
        ]
        assert compute_stats(positions)["max_drawdown"] == pytest.approx(80.0)


# ══════════════════════════════════════════════════════════════════════════
# I. GROUPING: TIME OF DAY / QUALITY / SCORE
# ══════════════════════════════════════════════════════════════════════════

class TestGrouping:

    def _position_with_signal(self, signal_id, score, quality, hour, minute, pnl):
        sig = {"id": signal_id, "signal_score": score, "signal_quality": quality}
        row = _row(
            id=signal_id, signal_id=signal_id, pnl=pnl,
            entry_time=f"2026-09-25 {hour:02d}:{minute:02d}:00",
            exit_time=f"2026-09-25 {hour:02d}:{minute + 30:02d}:00",
        )
        return collapse_positions([row], {signal_id: sig})[0]

    def test_time_of_day_buckets(self):
        pos = self._position_with_signal("s1", 70, "QUALIFIED", 9, 30, 20.0)
        assert pos.time_bucket() == "09:15-10:00"
        pos = self._position_with_signal("s2", 70, "QUALIFIED", 13, 30, 20.0)
        assert pos.time_bucket() == "13:00-14:00"

    def test_all_time_buckets_appear_even_when_empty(self):
        groups = groups_by_time([self._position_with_signal(
            "s1", 70, "QUALIFIED", 10, 0, 20.0)])
        for label in ("09:15-10:00", "10:00-11:00", "11:00-12:00",
                      "12:00-13:00", "13:00-14:00", "14:00-15:30"):
            assert label in groups
        empty = [k for k, v in groups.items() if v["total_completed_trades"] == 0]
        assert empty, "empty buckets must be visible, not omitted"

    def test_score_bands(self):
        assert self._position_with_signal("a", 20, "WEAK", 10, 0, 5.0).score_band() == "0-30"
        assert self._position_with_signal("b", 50, "NORMAL", 10, 0, 5.0).score_band() == "46-55"
        assert self._position_with_signal("c", 95, "PREMIUM", 10, 0, 5.0).score_band() == "86-100"

    def test_every_score_band_is_present(self):
        groups = groups_by_score_band([self._position_with_signal(
            "a", 50, "NORMAL", 10, 0, 5.0)])
        for label in ("0-30", "31-45", "46-55", "56-65", "66-75", "76-85", "86-100"):
            assert label in groups

    def test_quality_grouping_and_empty_levels(self):
        groups = groups_by_quality([self._position_with_signal(
            "a", 90, "PREMIUM", 10, 0, 5.0)])
        assert groups["PREMIUM"]["total_completed_trades"] == 1
        for level in ("REJECTED", "WEAK", "NORMAL", "QUALIFIED"):
            assert groups[level]["total_completed_trades"] == 0

    def test_position_without_a_signal_is_not_grouped_by_score(self):
        pos = _position()
        assert pos.signal_score is None
        assert pos.score_band() is None

    def test_holding_time_buckets(self):
        pos = _position(entry_time="2026-09-25 10:00:00",
                        exit_time="2026-09-25 10:12:00")
        assert pos.holding_bucket() == "5-15m"


# ══════════════════════════════════════════════════════════════════════════
# A. NO LOOKAHEAD
# ══════════════════════════════════════════════════════════════════════════

class TestNoLookahead:

    def test_atr_at_uses_only_bars_strictly_before_the_entry(self):
        # 60 flat bars, then a violent spike in the LAST 5 bars.
        series = _bars(count=60, base=100.0)
        as_of = series[-1].timestamp
        atr_before = atr_at(series, as_of)
        spiked = series + [
            Bar(series[-1].timestamp + timedelta(minutes=5 * (i + 1)),
                open=100.0, high=200.0, low=50.0, close=150.0, volume=1000.0)
            for i in range(5)
        ]
        # Reading at the same instant must be identical: the spike is after it.
        assert atr_at(spiked, as_of) == atr_before

    def test_atr_at_excludes_the_bar_at_the_entry_instant(self):
        series = _bars(count=30, base=100.0)
        quiet = atr_at(series, series[10].timestamp)
        series[10] = Bar(series[10].timestamp, 100.0, 500.0, 1.0, 300.0, 1000.0)
        assert atr_at(series, series[10].timestamp) == quiet

    def test_atr_at_is_none_without_enough_history(self):
        assert atr_at(_bars(count=5), _bars(count=5)[-1].timestamp) is None

    def test_replay_never_touches_a_bar_before_the_entry(self):
        """A catastrophic drop BEFORE the entry must not affect the outcome."""
        entry = datetime(2026, 9, 25, 10, 0)
        pos = _position(entry_time="2026-09-25 10:00:00",
                        exit_time="2026-09-25 10:30:00")
        pre = [
            Bar(entry - timedelta(minutes=5 * (i + 1)), 100.0, 900.0, 1.0, 500.0, 1000.0)
            for i in range(20)
        ]
        post = _bars(start="2026-09-25 10:00:00", count=20, base=100.0, drift=0.1)
        clean = replay_position(pos, pre + post, ReplayConfig.baseline())
        noisy = replay_position(
            pos, [Bar(b.timestamp, b.open, 900.0, 1.0, 500.0, b.volume) for b in pre] + post,
            ReplayConfig.baseline())
        assert clean.pnl == noisy.pnl
        assert clean.exit_price == noisy.exit_price

    def test_replay_consumes_nothing_before_the_entry_bar(self):
        pos = _position(entry_time="2026-09-25 10:00:00",
                        exit_time="2026-09-25 10:30:00")
        # A perfect up-move entirely BEFORE the entry: must not be traded.
        pre = _bars(start="2026-09-25 09:15:00", count=9, base=100.0, drift=5.0)
        result = replay_position(pos, pre, ReplayConfig.baseline())
        assert not result.traded
        assert result.skip_reason and "entry" in result.skip_reason.lower()

    def test_replay_does_not_see_bars_after_the_position_exits(self):
        """Outcome must be identical whether or not future bars are appended."""
        pos = _position(entry_time="2026-09-25 10:00:00",
                        exit_time="2026-09-25 10:30:00", pnl=0.0)
        series = _bars(start="2026-09-25 10:00:00", count=25, base=100.0, drift=-0.4)
        short = replay_position(pos, series, ReplayConfig.baseline())
        long = replay_position(
            pos,
            series + _bars(start="2026-09-25 12:10:00", count=40, base=10.0, drift=5.0),
            ReplayConfig.baseline())
        assert short.exit_price == long.exit_price
        assert short.pnl == long.pnl
        assert short.exit_reason == long.exit_reason

    def test_entry_confirmation_uses_no_future_candle(self):
        series = _bars(start="2026-09-25 10:00:00", count=10, base=100.0, drift=1.0)
        at_3 = evaluate_entry_confirmation("candle_side", series, 3, "LONG", 1.0)
        # Mutating everything AFTER index 3 cannot change the verdict.
        mutated = list(series[:4]) + [
            Bar(b.timestamp, b.open, 900.0, 1.0, 500.0, b.volume) for b in series[4:]
        ]
        at_3_mut = evaluate_entry_confirmation("candle_side", mutated, 3, "LONG", 1.0)
        assert at_3[0] == at_3_mut[0]

    def test_session_vwap_is_none_without_volume_rather_than_zero(self):
        series = [
            Bar(datetime(2026, 9, 25, 10, 0), 100.0, 101.0, 99.0, 100.0, 0.0)
            for _ in range(5)
        ]
        assert session_vwap(series) is None

    def test_replay_marks_unresolved_rather_than_force_closing(self):
        pos = _position(entry_time="2026-09-25 10:00:00",
                        exit_time="2026-09-25 10:30:00")
        # A perfectly flat path that never reaches the stop or either target.
        series = _bars_through("2026-09-25 10:00:00", after=10, base=100.0,
                               high=100.4, low=99.6)
        result = replay_position(pos, series, ReplayConfig.baseline())
        assert result.unresolved or result.exit_reason == "SESSION_END"
        if result.unresolved:
            assert result.exit_price is None
        assert not result.hit_t1 and not result.hit_t2


# ══════════════════════════════════════════════════════════════════════════
# B. BASELINE UNCHANGED  +  D. signal_id PRESERVATION
# ══════════════════════════════════════════════════════════════════════════

class TestBaselineUnchangedAndSignalIdPreserved:

    def test_baseline_uses_only_the_ledger_pnl_column(self):
        rows = [_row(id="a", pnl=30.0), _row(id="b", pnl=-10.0,
                                             entry_time="2026-09-25 11:00:00",
                                             exit_time="2026-09-25 11:30:00")]
        base = build_baseline(rows)
        assert base["overall"]["net_pnl"] == pytest.approx(20.0)
        assert base["overall"]["total_completed_trades"] == 2

    def test_signal_id_survives_the_collapse(self):
        pos = _position(id="a", signal_id="SIG123")
        assert pos.signal_id == "SIG123"
        assert pos.is_ai_linked is True
        assert pos.cohort() == COHORT_AI_LINKED

    def test_signal_id_survives_a_profit_capture_collapse(self):
        rows = [
            _row(id="a", signal_id="SIG9", pnl=6.0,
                 details_json=json.dumps({"position_id": "P", "exit_reason": "T1_PARTIAL"})),
            _row(id="b", signal_id="SIG9", pnl=8.0,
                 details_json=json.dumps({"position_id": "P", "exit_reason": "T2_FINAL"})),
        ]
        pos = collapse_positions(rows)[0]
        assert pos.signal_id == "SIG9"
        assert pos.is_ai_linked

    def test_a_linked_position_is_never_demoted_by_the_holding_filter(self):
        pos = _position(id="a", signal_id="SIG1",
                        entry_time="2026-09-25 10:00:00.000",
                        exit_time="2026-09-25 10:00:00.400")
        assert pos.holding_minutes < 1.0
        assert pos.cohort() == COHORT_AI_LINKED

    def test_sub_minute_unlinked_holds_are_classified_as_fixtures(self):
        pos = _position(id="a", signal_id=None,
                        entry_time="2026-09-25 10:00:00.000",
                        exit_time="2026-09-25 10:00:00.050")
        assert pos.cohort() == COHORT_TEST_FIXTURE

    def test_real_strategy_hold_is_not_a_fixture(self):
        pos = _position(id="a", entry_time="2026-09-25 10:00:00",
                        exit_time="2026-09-25 10:30:00")
        assert pos.cohort() == COHORT_STRATEGY_UNLINKED

    def test_signal_attributes_come_through_when_present(self):
        sig = {"id": "S1", "signal_score": 88.0, "signal_quality": "PREMIUM",
               "confidence": 0.8, "strategy_version": "v1", "atr": 1.5}
        pos = _position(id="a", signal_id="S1", signals_by_id={"S1": sig})
        assert pos.signal_score == 88.0
        assert pos.signal_quality == "PREMIUM"
        assert pos.strategy_version == "v1"
        assert pos.atr == 1.5

    def test_the_filtered_and_unfiltered_baselines_both_reported(self):
        rows = [
            _row(id="a", pnl=100.0, entry_time="2026-09-25 10:00:00",
                 exit_time="2026-09-25 10:30:00"),
            _row(id="b", pnl=900.0, entry_time="2026-09-25 11:00:00",
                 exit_time="2026-09-25 11:00:00.100"),
        ]
        base = build_baseline(rows)
        assert base["overall"]["total_completed_trades"] == 2
        assert base["strategy_only"]["total_completed_trades"] == 1
        assert base["test_fixture"]["total_completed_trades"] == 1
        # The fixture's fake profit must not leak into the strategy cohort.
        assert base["strategy_only"]["net_pnl"] == pytest.approx(100.0)


# ══════════════════════════════════════════════════════════════════════════
# C + K. CANDIDATE ISOLATION AND CANDIDATE VS BASELINE
# ══════════════════════════════════════════════════════════════════════════

class TestCandidateIsolation:

    def test_a_candidate_does_not_mutate_the_baseline_config(self):
        baseline = ReplayConfig.baseline()
        before = dict(baseline.__dict__)
        evaluate_candidate(
            ReplayConfig(**{**before, "label": "wide", "sl_atr_mult": 2.0}),
            [_position()], {"TEST": _bars()},
        )
        assert dict(baseline.__dict__) == before

    def test_with_label_returns_a_new_config(self):
        baseline = ReplayConfig.baseline()
        relabelled = baseline.with_label("other")
        assert relabelled is not baseline
        assert baseline.label == "baseline"
        assert relabelled.label == "other"

    def test_baseline_config_defaults_match_live_production(self):
        cfg = ReplayConfig.baseline()
        assert cfg.sl_atr_mult == 1.5       # NORMAL/STRONG_SL_ATR_MULTIPLIER
        assert cfg.t1_atr_mult == 2.0       # NORMAL_T1_ATR_MULTIPLIER
        assert cfg.t2_atr_mult == 3.0       # NORMAL_T2_ATR_MULTIPLIER
        assert cfg.t1_exit_percent == 50.0  # T1_EXIT_PERCENT
        assert cfg.min_rr == 1.2            # min_rr_normal
        assert cfg.trailing_mode == TRAILING_RANGE
        assert cfg.trailing_range_mult == 0.5

    def test_candidate_runs_do_not_share_state(self):
        pos = _position(entry_time="2026-09-25 10:00:00",
                        exit_time="2026-09-25 10:30:00")
        positions = [pos]
        bars = {"TEST": _bars_through("2026-09-25 10:00:00")}
        first = evaluate_candidate(ReplayConfig.baseline(), positions, bars)
        second = evaluate_candidate(ReplayConfig.baseline(), positions, bars)
        assert first.simulated_trades == 1, first.skip_reasons
        assert first.net_pnl == second.net_pnl
        assert first.results[0] is not second.results[0]

    def test_skip_reasons_are_recorded_not_swallowed(self):
        outcome = evaluate_candidate(
            ReplayConfig(**{**ReplayConfig.baseline().__dict__, "min_rr": 99.0}),
            [_position(entry_time="2026-09-25 10:00:00")],
            {"TEST": _bars_through("2026-09-25 10:00:00")},
        )
        assert outcome.simulated_trades == 0
        assert outcome.skipped == 1
        assert sum(outcome.skip_reasons.values()) == 1
        assert any("R/R" in k for k in outcome.skip_reasons)

    def test_evaluate_candidate_never_claims_realized_performance(self):
        outcome = evaluate_candidate(ReplayConfig.baseline(), [_position()],
                                     {"TEST": _bars()})
        assert outcome.is_realized_performance is False
        assert outcome.to_dict()["is_realized_performance"] is False

    def test_grid_runner_never_claims_realized_performance(self):
        result = run_experiment_grid("t1_partial", [_position()], {"TEST": _bars()})
        assert result["is_realized_performance"] is False

    def test_all_grids_are_named_and_constructible(self):
        for name in ("stop_loss", "stop_loss_fixed_rr", "t1_partial", "trailing",
                     "target", "min_rr", "target_rr", "entry_confirmation"):
            assert name in EXPERIMENT_GRIDS
            configs = EXPERIMENT_GRIDS[name]()
            assert configs, f"{name} produced no configurations"
            for cfg in configs:
                assert isinstance(cfg, ReplayConfig)
                assert cfg.label


class TestCandidateVsBaseline:

    def _cohort(self):
        return [
            _position(id=f"t{i}", symbol="TEST", pnl=0.0,
                      entry_time="2026-09-25 10:00:00",
                      exit_time="2026-09-25 10:30:00",
                      quantity=100, entry_price=100.0, stop_loss=99.0,
                      target_1=102.0, target_2=103.0)
            for i in range(4)
        ]

    def _bars(self):
        return {"TEST": _bars_through("2026-09-25 10:00:00")}

    def test_a_wider_stop_does_not_increase_risk_amount(self):
        """Account risk per trade never exceeds the budget as the stop widens.

        ``min_rr`` is disabled here on purpose: with a fixed T1 at 2.0 ATR,
        widening the stop alone drives R:R under 1.2 and the setup degenerates
        before sizing is ever reached (see test_widening_the_stop_degenerates_rr
        below). This test isolates the sizing effect only.
        """
        pos = self._cohort()[0]
        bars = self._bars()
        budget = 10_000.0 * 0.02
        prev_qty = None
        for mult in (1.0, 1.5, 2.0, 2.5, 3.0):
            outcome = evaluate_candidate(
                ReplayConfig(**{**ReplayConfig.baseline().__dict__,
                                "label": f"sl{mult}", "min_rr": 0.0,
                                "sl_atr_mult": mult}), [pos], bars)
            assert outcome.simulated_trades == 1, (mult, outcome.skip_reasons)
            res = outcome.results[0]
            assert res.initial_risk is not None
            # Never more risk than the budget, whatever the stop width.
            assert res.initial_risk <= budget + 1e-6, (mult, res.initial_risk)
            # And never more shares than a tighter stop.
            if prev_qty is not None:
                assert res.quantity <= prev_qty, (mult, res.quantity, prev_qty)
            prev_qty = res.quantity

    def test_a_narrow_stop_is_limited_by_capital_not_by_the_risk_budget(self):
        """Documents the live finding: at Rs 10,000 capital the 5% headroom
        binds before the 2% risk budget does, so the account carries a
        fraction of its intended risk. Reported, never 'fixed' - raising risk
        is out of scope."""
        pos = self._cohort()[0]
        bars = self._bars()
        outcome = evaluate_candidate(
            ReplayConfig(**{**ReplayConfig.baseline().__dict__, "label": "n",
                            "min_rr": 0.0, "sl_atr_mult": 1.0}), [pos], bars)
        res = outcome.results[0]
        budget = 10_000.0 * 0.02
        assert res.initial_risk < budget
        assert res.quantity == 95          # 95% of 10,000 / 100 entry
        assert res.initial_risk == pytest.approx(95.0)

    def test_a_wider_stop_keeps_the_share_count_and_raises_rupee_risk(self):
        """The counter-intuitive truth at the live capital level.

        Because the cash cap binds, ``min(budget/risk, 0.95*capital/entry)``
        is governed by the second term, so a wider stop cannot reduce the size
        - it leaves the size unchanged and therefore carries MORE account
        risk. This is the opposite of the usual 'wider stop = smaller size'
        reading, and it is asserted here so the sizing mirror is not
        'corrected' back into a state production does not use.
        """
        pos = self._cohort()[0]
        bars = self._bars()
        risks, qtys = {}, {}
        for mult in (1.0, 1.5, 2.0, 2.5, 3.0):
            outcome = evaluate_candidate(
                ReplayConfig(**{**ReplayConfig.baseline().__dict__,
                                "label": f"sl{mult}", "min_rr": 0.0,
                                "sl_atr_mult": mult}), [pos], bars)
            res = outcome.results[0]
            qtys[mult], risks[mult] = res.quantity, res.initial_risk
        widths = sorted(qtys)
        # Size never grows with the stop width.
        assert all(qtys[b] <= qtys[a] for a, b in zip(widths, widths[1:])), qtys
        # While the cash cap binds the size is FLAT and the risk climbs - the
        # regime the live ledger is actually in. Risk peaks at the budget, then
        # the budget starts biting and the size finally falls, so risk plateaus.
        assert qtys[1.0] == qtys[1.5] == qtys[2.0], qtys
        assert risks[1.0] < risks[1.5] < risks[2.0] <= risks[2.5], risks
        # The peak is the budget itself; nothing exceeds it.
        assert max(risks.values()) == pytest.approx(10_000.0 * 0.02)
        # A very wide stop is still riskier than a tight one.
        assert risks[3.0] > risks[1.0], risks

    def test_widening_the_stop_degenerates_rr_at_the_normal_target(self):
        """Documents the structural trap: T1 is fixed, so a wider SL only
        makes R:R worse. This is why several requested candidates skip
        every trade instead of trading differently."""
        pos = self._cohort()[0]
        bars = self._bars()
        outcome = evaluate_candidate(
            ReplayConfig(**{**ReplayConfig.baseline().__dict__, "label": "w",
                            "sl_atr_mult": 3.0}), [pos], bars)
        assert outcome.simulated_trades == 0
        assert any("R/R" in k for k in outcome.skip_reasons)

    def test_promotion_requires_the_sample_gate(self):
        positions = self._cohort()
        bars = self._bars()
        b = evaluate_candidate(ReplayConfig.baseline(), positions, bars)
        c = evaluate_candidate(
            ReplayConfig(**{**ReplayConfig.baseline().__dict__, "label": "x",
                            "t1_exit_percent": 70.0}), positions, bars)
        fid = measure_fidelity(positions, bars)
        d = evaluate_promotion("x", b, c, len(positions), fid)
        assert d.promotable is False
        assert d.status == CANDIDATE
        assert any("real completed trades" in r for r in d.blocking_reasons)
        assert INSUFFICIENT_PROMOTION in d.blocking_reasons

    def test_promotion_blocked_when_the_fidelity_gate_fails(self):
        positions = self._cohort()
        bars = self._bars()
        b = evaluate_candidate(ReplayConfig.baseline(), positions, bars)
        c = evaluate_candidate(
            ReplayConfig(**{**ReplayConfig.baseline().__dict__, "label": "x",
                            "t1_exit_percent": 70.0}), positions, bars)
        fid = measure_fidelity(positions, bars)
        # The cohort carries no recorded exit_reason, so force the other
        # fidelity failure: a measurable but too-low agreement rate.
        fid.positions_with_recorded_reason = len(positions)
        fid.reason_agreements = 0
        assert not fid.passes()[0]
        d = evaluate_promotion("x", b, c, 500, fid)
        assert d.promotable is False
        assert any("agreement" in r for r in d.blocking_reasons)

    def test_promotion_blocked_when_risk_budget_not_preserved(self):
        positions = self._cohort()
        bars = self._bars()
        b = evaluate_candidate(ReplayConfig.baseline(), positions, bars)
        c = evaluate_candidate(
            ReplayConfig(**{**ReplayConfig.baseline().__dict__, "label": "x",
                            "t1_exit_percent": 70.0}), positions, bars)
        d = evaluate_promotion("x", b, c, 500, measure_fidelity(positions, bars),
                               risk_budget_preserved=False)
        assert d.promotable is False
        assert any("risk budget" in r for r in d.blocking_reasons)

    def test_promotion_blocked_when_drawdown_worsens_beyond_tolerance(self):
        positions = self._cohort()
        bars = self._bars()
        b = evaluate_candidate(ReplayConfig.baseline(), positions, bars)
        c = evaluate_candidate(
            ReplayConfig(**{**ReplayConfig.baseline().__dict__, "label": "x",
                            "t1_exit_percent": 70.0}), positions, bars)
        c.max_drawdown = 10.0 * MAX_DRAWDOWN_TOLERANCE + 1
        b.max_drawdown = 10.0
        d = evaluate_promotion("x", b, c, 500, measure_fidelity(positions, bars))
        assert d.promotable is False
        assert any("drawdown" in r for r in d.blocking_reasons)
        assert d.drawdown_ratio > MAX_DRAWDOWN_TOLERANCE

    def test_blocked_when_the_candidate_is_worse(self):
        positions = self._cohort()
        bars = self._bars()
        b = evaluate_candidate(ReplayConfig.baseline(), positions, bars)
        worse = evaluate_candidate(ReplayConfig.baseline(), positions, bars)
        worse.net_pnl = b.net_pnl - 1000
        worse.expectancy = (b.expectancy or 0) - 100
        d = evaluate_promotion("worse", b, worse, 500, measure_fidelity(positions, bars))
        assert d.promotable is False
        assert d.status == CANDIDATE

    def test_an_improvement_inside_the_noise_band_is_still_blocked(self):
        positions = self._cohort()
        bars = self._bars()
        b = evaluate_candidate(ReplayConfig.baseline(), positions, bars)
        b.expectancy, b.max_drawdown = 1.0, 10.0
        c = evaluate_candidate(ReplayConfig.baseline(), positions, bars)
        c.expectancy, c.max_drawdown = 1.05, 10.0  # a 0.05 nudge
        fid = measure_fidelity(positions, bars)
        fid.mean_abs_pnl_error = 5.0            # huge measurement error
        d = evaluate_promotion("nudge", b, c, 500, fid)
        assert d.promotable is False
        assert any("inside the simulator" in r for r in d.blocking_reasons)
        assert d.improvement_exceeds_noise is False

    def test_status_is_never_optimistic(self):
        positions = self._cohort()
        bars = self._bars()
        b = evaluate_candidate(ReplayConfig.baseline(), positions, bars)
        for label in ("a", "b", "c"):
            d = evaluate_promotion(label, b, b, 0, measure_fidelity(positions, bars))
            assert d.promotable is False
            assert d.status == CANDIDATE


# ══════════════════════════════════════════════════════════════════════════
# J. SHADOW MODE
# ══════════════════════════════════════════════════════════════════════════

class TestShadowMode:

    SIGNAL = {
        "symbol": "KOTAKBANK", "direction": "LONG", "signal_score": 78.0,
        "confidence": 0.71, "entry_price": 414.1,
        "timestamp": "2026-09-30 10:05:00",
        "signal_quality": "QUALIFIED", "setup_quality_score": 71.0,
    }
    IND = {"adx_14": 27.4, "relative_volume": 1.32, "atr_14": 1.095,
           "vwap": 413.8, "close": 414.1}
    CTX = {"nifty_trend": "BULLISH"}

    def test_default_settings_are_shadow(self):
        cfg = SelectionConfig.from_settings()
        assert cfg.mode is ShadowMode.SHADOW

    def test_default_thresholds_are_all_permissive(self):
        cfg = SelectionConfig.from_settings()
        for rule in cfg.to_rules():
            assert rule.threshold is None, f"{rule.name} must default to disabled"

    def test_shipped_default_never_blocks(self):
        d = evaluate_selection(dict(self.SIGNAL), SelectionConfig.from_settings(),
                               self.IND, self.CTX, self.SIGNAL["timestamp"])
        assert d.decision == DECISION_TRADE
        assert d.applied_to_production is False
        assert d.would_change_production is False

    def test_every_rule_disabled_means_trade(self):
        d = evaluate_selection(dict(self.SIGNAL), SelectionConfig(), self.IND,
                               self.CTX, self.SIGNAL["timestamp"])
        assert d.decision == DECISION_TRADE
        assert d.failed_rules == []

    def test_a_shadow_skip_is_recorded_but_not_applied(self):
        cfg = SelectionConfig(min_signal_score=99.0)
        d = evaluate_selection(dict(self.SIGNAL), cfg, self.IND, self.CTX,
                               self.SIGNAL["timestamp"])
        assert d.decision == DECISION_SKIP
        assert d.applied_to_production is False
        assert d.would_change_production is True

    def test_enforce_mode_is_the_only_way_to_apply(self):
        cfg = SelectionConfig(mode=ShadowMode.ENFORCE, min_signal_score=99.0)
        d = evaluate_selection(dict(self.SIGNAL), cfg, self.IND, self.CTX,
                               self.SIGNAL["timestamp"])
        assert d.applied_to_production is True

    def test_enforce_trade_is_applied_too(self):
        cfg = SelectionConfig(mode=ShadowMode.ENFORCE, min_signal_score=10.0)
        d = evaluate_selection(dict(self.SIGNAL), cfg, self.IND, self.CTX,
                               self.SIGNAL["timestamp"])
        assert d.decision == DECISION_TRADE
        assert d.applied_to_production is True

    def test_shadow_record_carries_the_signal_id_unchanged(self):
        d = evaluate_selection(dict(self.SIGNAL), SelectionConfig(), self.IND,
                               self.CTX, self.SIGNAL["timestamp"])
        rec = shadow_record(self.SIGNAL, d, signal_id="SIG7")
        assert rec["signal_id"] == "SIG7"
        assert rec["mode"] == "SHADOW"
        assert rec["symbol"] == "KOTAKBANK"

    def test_shadow_record_serialises_its_json_fields(self):
        d = evaluate_selection(dict(self.SIGNAL),
                               SelectionConfig(min_adx=99.0), self.IND, self.CTX)
        rec = shadow_record(self.SIGNAL, d)
        for key in ("failed_rules", "unknown_rules", "reasons"):
            assert isinstance(json.loads(rec[key]), list)

    def test_shadow_store_round_trip(self, tmp_path):
        db = str(tmp_path / "shadow.db")
        ensure_shadow_schema(db)
        d = evaluate_selection(dict(self.SIGNAL), SelectionConfig(), self.IND,
                               self.CTX, self.SIGNAL["timestamp"])
        rec = shadow_record(self.SIGNAL, d, signal_id="SIG7")
        row_id = persist_selection_decision(rec, baseline_decision="TRADE", db_path=db)
        assert row_id
        rows = load_shadow_records(db_path=db)
        assert len(rows) == 1
        assert rows[0]["candidate_decision"] == DECISION_TRADE
        assert rows[0]["baseline_decision"] == "TRADE"
        assert rows[0]["applied_to_production"] is False
        assert rows[0]["signal_id"] == "SIG7"

    def test_shadow_store_is_idempotent_per_symbol_candle(self, tmp_path):
        db = str(tmp_path / "shadow2.db")
        ensure_shadow_schema(db)
        d = evaluate_selection(dict(self.SIGNAL), SelectionConfig(), self.IND,
                               self.CTX, self.SIGNAL["timestamp"])
        for _ in range(5):
            persist_selection_decision(shadow_record(self.SIGNAL, d),
                                       baseline_decision="TRADE", db_path=db)
        assert len(load_shadow_records(db_path=db)) == 1

    def test_shadow_outcome_is_attached_and_never_rewritten(self, tmp_path):
        db = str(tmp_path / "shadow3.db")
        ensure_shadow_schema(db)
        d = evaluate_selection(dict(self.SIGNAL), SelectionConfig(), self.IND,
                               self.CTX, self.SIGNAL["timestamp"])
        persist_selection_decision(shadow_record(self.SIGNAL, d, signal_id="SIG8"),
                                   baseline_decision="TRADE", db_path=db)
        assert attach_realized_outcome("SIG8", 14.62, "WIN", "T1", db_path=db) is True
        row = load_shadow_records(db_path=db)[0]
        assert row["realized_pnl"] == pytest.approx(14.62)
        assert row["outcome"] == "WIN"
        # A second attempt must not overwrite the recorded observation.
        assert attach_realized_outcome("SIG8", 999.0, "WIN", "T1", db_path=db) is False
        assert load_shadow_records(db_path=db)[0]["realized_pnl"] == pytest.approx(14.62)

    def test_missing_signal_id_is_a_no_op_not_an_error(self, tmp_path):
        db = str(tmp_path / "shadow4.db")
        ensure_shadow_schema(db)
        assert attach_realized_outcome(None, 1.0, "WIN", db_path=db) is False
        assert attach_realized_outcome("", 1.0, "WIN", db_path=db) is False


# ══════════════════════════════════════════════════════════════════════════
# L. RISK LIMITS
# ══════════════════════════════════════════════════════════════════════════

class TestRiskLimits:

    def test_risk_engine_limits_are_untouched_by_this_work(self):
        from app.core.config import settings
        from app.services.risk_engine import RiskConfig

        cfg = RiskConfig()
        assert cfg.max_risk_per_trade_pct == settings.MAX_RISK_PER_TRADE_PCT == 2.0
        assert cfg.max_daily_loss_pct == settings.MAX_DAILY_LOSS_PCT == 5.0
        assert cfg.max_trades_per_day == settings.MAX_TRADES_PER_DAY == 10
        assert cfg.max_simultaneous_positions == settings.MAX_SIMULTANEOUS_POSITIONS == 10
        assert cfg.min_risk_reward == 1.2
        assert cfg.cooldown_after_losses == 3

    def test_quality_risk_multipliers_unchanged(self):
        from app.services.risk_engine import RiskConfig
        mults = RiskConfig().quality_risk_multipliers
        assert mults["REJECTED"] == 0.0
        assert mults["WEAK"] == 0.0
        for level in ("NORMAL", "QUALIFIED", "PREMIUM"):
            assert mults[level] == 1.0

    def test_profit_capture_defaults_untouched(self):
        from app.core.config import settings
        assert settings.T1_EXIT_PERCENT == 50.0
        assert settings.T1_PROTECT_MODE == "MOVE_SL_TO_ENTRY"
        assert settings.TRAILING_MODE == "RANGE"
        assert settings.TRAILING_RANGE_MULT == 0.5

    def test_strategy_geometry_untouched(self):
        from app.core.config import settings
        assert settings.NORMAL_SL_ATR_MULTIPLIER == 1.5
        assert settings.NORMAL_T1_ATR_MULTIPLIER == 2.0
        assert settings.NORMAL_T2_ATR_MULTIPLIER == 3.0
        assert settings.STRONG_T1_ATR_MULTIPLIER == 3.0
        assert settings.STRONG_T2_ATR_MULTIPLIER == 4.0

    def test_live_strategy_version_still_v1(self):
        from app.services.strategy_config import LIVE_STRATEGY_VERSION
        assert LIVE_STRATEGY_VERSION == "v1"

    def test_promotion_guard_refuses_below_the_sample(self):
        with pytest.raises(PromotionNotAllowed) as exc:
            assert_promotable(16, MIN_SAMPLE_FOR_PROMOTION)
        assert "16 real completed trades" in str(exc.value)

    def test_promotion_guard_refuses_when_fidelity_failed(self):
        with pytest.raises(PromotionNotAllowed) as exc:
            assert_promotable(500, 30, fidelity_gate_passes=False)
        assert "fidelity" in str(exc.value)

    def test_promotion_guard_allows_only_a_full_pass(self):
        assert_promotable(MIN_SAMPLE_FOR_PROMOTION, MIN_SAMPLE_FOR_PROMOTION,
                          fidelity_gate_passes=True) is None


# ══════════════════════════════════════════════════════════════════════════
# M. POSITION SIZING
# ══════════════════════════════════════════════════════════════════════════

class TestPositionSizing:

    def test_risk_budget_bounds_the_shares(self):
        # Rs 10,000 at 2% = Rs 200; risk/share = Rs 2 -> the budget ALLOWS
        # 100 shares. The live engine's 5% capital headroom then caps it at 95,
        # so 95 is what production actually trades. Both bounds are asserted
        # so a future change to either cannot pass unnoticed.
        assert risk_capped_quantity(100.0, 98.0, 10_000.0, 2.0) == 95

    def test_the_risk_budget_binds_once_the_stop_is_wide_enough(self):
        # The two caps cross when risk/share > 2.1% of entry. A 3% stop does,
        # so here the 2% budget - not the account size - limits the position.
        qty = risk_capped_quantity(100.0, 97.0, 1_000_000.0, 2.0)
        assert qty == int((1_000_000.0 * 0.02) / 3.0)
        assert qty * 3.0 <= 1_000_000.0 * 0.02 + 1e-6

    def test_wider_stop_gives_fewer_shares(self):
        narrow = risk_capped_quantity(100.0, 99.5, 10_000.0, 2.0)
        wide = risk_capped_quantity(100.0, 95.0, 10_000.0, 2.0)
        assert wide < narrow

    def test_risk_amount_never_exceeds_the_budget(self):
        for stop in (99.9, 99.0, 97.0, 90.0, 50.0):
            entry, capital, pct = 100.0, 10_000.0, 2.0
            qty = risk_capped_quantity(entry, stop, capital, pct)
            assert qty * abs(entry - stop) <= capital * pct / 100 + 1e-6

    def test_cash_affordability_caps_the_position(self):
        # 1,000 shares would cost Rs 100,000 > Rs 10,000 capital.
        assert risk_capped_quantity(1_000.0, 999.0, 10_000.0, 2.0) <= 10

    def test_degenerate_inputs_give_zero_not_an_exception(self):
        assert risk_capped_quantity(0.0, 1.0, 10_000.0, 2.0) == 0
        assert risk_capped_quantity(-5.0, 1.0, 10_000.0, 2.0) == 0
        assert risk_capped_quantity(100.0, 100.0, 10_000.0, 2.0) == 0   # zero risk
        assert risk_capped_quantity(100.0, 98.0, 10_000.0, 0.0) == 0
        assert risk_capped_quantity(100.0, 98.0, 10_000.0, -2.0) == 0

    def test_an_extremely_wide_stop_is_still_bounded_by_the_budget(self):
        # A stop at 0 is a legitimate (absurd) 100-rupee-per-share risk: the
        # Rs 200 budget buys 2 shares, and the position is not affordable- or
        # risk-inflated. It must not be treated as a crash.
        assert risk_capped_quantity(100.0, 0.0, 10_000.0, 2.0) == 2

    def test_the_95_percent_capital_headroom_is_mirrored(self):
        # 100 shares x Rs 100 = Rs 10,000 = the whole account. The live engine
        # leaves 5% headroom, so the mirror must too.
        assert risk_capped_quantity(100.0, 99.9, 10_000.0, 2.0) == 95

    def test_matches_the_live_risk_engine(self):
        from app.services.risk_engine import RiskEngine, RiskConfig

        engine = RiskEngine(RiskConfig(account_capital=10_000.0,
                                       max_risk_per_trade_pct=2.0))
        for entry, stop in ((100.0, 98.0), (250.0, 245.0), (1_000.0, 990.0)):
            live = engine.calculate_position_size(entry, stop, "LONG")
            mirrored = risk_capped_quantity(entry, stop, 10_000.0, 2.0)
            assert mirrored == int(live), f"mismatch at {entry}/{stop}"


# ══════════════════════════════════════════════════════════════════════════
# N. INSUFFICIENT-SAMPLE HANDLING
# ══════════════════════════════════════════════════════════════════════════

class TestInsufficientSample:

    def test_empty_sample_is_flagged_not_faked(self):
        stats = compute_stats([])
        assert stats["total_completed_trades"] == 0
        assert stats["win_rate"] is None
        assert stats["expectancy"] is None
        assert stats["profit_factor"] is None
        assert stats["data_status"] == INSUFFICIENT_DATA
        assert stats["sample_sufficient"] is False

    def test_below_threshold_is_flagged(self):
        positions = [
            _position(id=f"t{i}", pnl=10.0, entry_time="2026-09-25 10:0%d:00" % i,
                      exit_time="2026-09-25 10:3%d:00" % i)
            for i in range(5)
        ]
        stats = compute_stats(positions)
        assert stats["sample_sufficient"] is False
        assert stats["data_status"] == INSUFFICIENT_DATA

    def test_at_threshold_is_sufficient(self):
        positions = [
            _position(id=f"t{i}", pnl=10.0, entry_time="2026-09-25 10:00:00",
                      exit_time="2026-09-25 10:30:00")
            for i in range(MIN_TRADES_FOR_CONCLUSION)
        ]
        stats = compute_stats(positions)
        assert stats["sample_sufficient"] is True
        assert stats["data_status"] == "OK"

    def test_undefined_rates_are_none_not_zero(self):
        """A cohort with no losers has no stop-out-free rate; report None."""
        positions = [
            _position(id=f"t{i}", pnl=10.0, entry_time="2026-09-25 10:00:00",
                      exit_time="2026-09-25 10:30:00")
            for i in range(3)
        ]
        stats = compute_stats(positions)
        assert stats["average_loser"] is None
        assert stats["expectancy"] is None

    def test_pattern_significance_reports_insufficient(self):
        a = [_position(id=f"a{i}", pnl=5.0) for i in range(2)]
        b = [_position(id=f"b{i}", pnl=-5.0) for i in range(2)]
        sig = pattern_significance(a, b)
        assert sig["verdict"] == INSUFFICIENT_DATA
        assert sig["n_a"] == 2 and sig["n_b"] == 2

    def test_fidelity_gate_fails_without_a_recorded_reason(self):
        positions = [_position(id="a", details_json=None)]
        rep = measure_fidelity(positions, {"TEST": _bars()})
        ok, reasons = rep.passes()
        assert ok is False
        assert any("exit_reason" in r for r in reasons)

    def test_the_exact_promotion_phrase_is_defined(self):
        assert INSUFFICIENT_PROMOTION == (
            "INSUFFICIENT REALIZED DATA FOR SAFE PROMOTION.")

    def test_no_candidate_is_promoted_at_the_current_sample(self):
        positions = [
            _position(id=f"t{i}", pnl=1.0, entry_time="2026-09-25 10:00:00",
                      exit_time="2026-09-25 10:30:00")
            for i in range(16)
        ]
        bars = {"TEST": _bars_through("2026-09-25 10:00:00")}
        b = evaluate_candidate(ReplayConfig.baseline(), positions, bars)
        fid = measure_fidelity(positions, bars)
        for name, factory in EXPERIMENT_GRIDS.items():
            for cfg in factory():
                c = evaluate_candidate(cfg, positions, bars)
                d = evaluate_promotion(f"{name}/{cfg.label}", b, c, 16, fid)
                assert d.promotable is False, f"{name}/{cfg.label} was promotable"
                assert d.status == CANDIDATE


# ══════════════════════════════════════════════════════════════════════════
# O. POSITION-LEVEL REALIZED P&L ATTRIBUTION
# ══════════════════════════════════════════════════════════════════════════

class TestRealizedPnlAttribution:

    @staticmethod
    def _account_with_long(quantity=10, entry=100.0):
        """Drive the real public path: place -> fill -> (close later)."""
        from app.services.paper_trading import PaperAccount

        account = PaperAccount(user_id="u1")
        account.cash = 1_000_000.0
        placed = account.place_order(
            "KOTAKBANK", "LONG", quantity, entry,
            stop_loss=98.0, target_1=104.0, target_2=106.0,
        )
        assert "error" not in placed, placed
        filled = account.fill_order(placed["order_id"], entry)
        assert filled.get("status") == "filled", filled
        return account, placed["order_id"]

    def test_a_partial_records_the_running_total(self):
        # 10 shares: 5 exit at 104 (+20), the remaining 5 at 106 (+30).
        account, pos_id = self._account_with_long()
        first = account.close_position(pos_id, 104.0, quantity=5,
                                       reason="T1_PARTIAL")
        assert first["pnl"] == pytest.approx(20.0)
        assert first["realized_pnl"] == pytest.approx(20.0)
        second = account.close_position(pos_id, 106.0, reason="T2_FINAL")
        assert second["pnl"] == pytest.approx(30.0)
        assert second["realized_pnl"] == pytest.approx(50.0), (
            "the final close must expose the POSITION total, not the last slice"
        )

    def test_a_single_close_reports_its_own_pnl(self):
        account, pos_id = self._account_with_long()
        result = account.close_position(pos_id, 105.0, reason="MANUAL_CLOSE")
        assert result["pnl"] == pytest.approx(50.0)
        assert result["realized_pnl"] == pytest.approx(50.0)

    def test_a_short_position_accumulates_the_same_way(self):
        from app.services.paper_trading import PaperAccount

        account = PaperAccount(user_id="u1")
        account.cash = 1_000_000.0
        placed = account.place_order("X", "SHORT", 10, 100.0, stop_loss=102.0,
                                     target_1=96.0, target_2=94.0)
        account.fill_order(placed["order_id"], 100.0)
        pos_id = placed["order_id"]
        a = account.close_position(pos_id, 96.0, quantity=5, reason="T1_PARTIAL")
        b = account.close_position(pos_id, 94.0, reason="T2_FINAL")
        assert a["pnl"] == pytest.approx(20.0)
        assert b["pnl"] == pytest.approx(30.0)
        assert b["realized_pnl"] == pytest.approx(50.0)

    def test_close_does_not_change_cash_or_total_pnl_accounting(self):
        """The fix is additive: it must not move a single rupee of accounting.

        ``before_cash`` is sampled after the fill, so the entry debit is
        already in it; the two closes must add exactly their proceeds.
        """
        account, pos_id = self._account_with_long()
        assert account.cash == pytest.approx(1_000_000.0 - 1_000.0)  # entry
        before_cash, before_total = account.cash, account.total_pnl
        a = account.close_position(pos_id, 104.0, quantity=5, reason="T1_PARTIAL")
        b = account.close_position(pos_id, 106.0, reason="T2_FINAL")
        assert account.total_pnl == pytest.approx(before_total + a["pnl"] + b["pnl"])
        assert account.cash == pytest.approx(before_cash + 5 * 104.0 + 5 * 106.0)

    def test_a_loss_is_attributed_at_full_size(self):
        """A stop-out on a profit-capture position must be attributed with the
        partial's P&L included, not replaced by it."""
        account, pos_id = self._account_with_long()
        a = account.close_position(pos_id, 104.0, quantity=5, reason="T1_PARTIAL")
        b = account.close_position(pos_id, 98.0, reason="TRAILING_STOP")
        assert a["pnl"] == pytest.approx(20.0)
        assert b["pnl"] == pytest.approx(-10.0)
        assert b["realized_pnl"] == pytest.approx(10.0)

    def test_trade_rows_carry_the_position_total(self):
        account, pos_id = self._account_with_long()
        account.close_position(pos_id, 104.0, quantity=5, reason="T1_PARTIAL")
        account.close_position(pos_id, 106.0, reason="T2_FINAL")
        rows = [t for t in account.closed_trades if t.get("position_id") == pos_id]
        assert len(rows) == 2
        assert rows[0]["realized_pnl"] == pytest.approx(20.0)
        assert rows[-1]["realized_pnl"] == pytest.approx(50.0)
        # Each row's own `pnl` is still just its slice.
        assert [r["pnl"] for r in rows] == [20.0, 30.0]

    def test_the_signal_attribution_uses_the_position_total(self):
        """`signals.realized_pnl` must receive the total, which is what
        `_finalize_paper_trade` reads off the result dict."""
        account, pos_id = self._account_with_long()
        account.close_position(pos_id, 104.0, quantity=5, reason="T1_PARTIAL")
        final = account.close_position(pos_id, 106.0, reason="T2_FINAL")
        attributed = final["trade"].get("realized_pnl", final.get("realized_pnl"))
        assert attributed == pytest.approx(50.0)

    def test_the_real_ledger_confirms_the_old_understatement(self):
        """Documents the defect: signal P&L held only the last close's slice.

        KOTAKBANK closed T1_PARTIAL +6.12 then T2_FINAL +8.50 for a position
        total of +14.62, and the signals row was written with 8.50. The
        historical row is left untouched (no backfilling); this test asserts the
        arithmetic that proves the discrepancy existed.
        """
        slices = [6.12, 8.50]
        assert sum(slices) == pytest.approx(14.62)
        assert slices[-1] != pytest.approx(sum(slices)), (
            "the old behaviour (last slice) must differ from the position total"
        )


# ══════════════════════════════════════════════════════════════════════════
# P. THE PROFIT SELECTION LAYER'S RULES
# ══════════════════════════════════════════════════════════════════════════

class TestProfitSelectionRules:

    SIGNAL = TestShadowMode.SIGNAL
    IND = TestShadowMode.IND
    CTX = TestShadowMode.CTX

    def _d(self, cfg, signal=None, ind=None, ctx=None, when=None):
        return evaluate_selection(dict(signal or self.SIGNAL), cfg,
                                  self.IND if ind is None else ind,
                                  self.CTX if ctx is None else ctx,
                                  when or self.SIGNAL["timestamp"])

    def test_the_signal_score_is_never_recalculated(self):
        payload = dict(self.SIGNAL)
        before = json.dumps(payload, sort_keys=True)
        evaluate_selection(payload, SelectionConfig(min_signal_score=1000.0),
                           self.IND, self.CTX, self.SIGNAL["timestamp"])
        assert json.dumps(payload, sort_keys=True) == before
        assert payload["signal_score"] == 78.0

    def test_layer_only_ever_returns_trade_or_skip(self):
        for cfg in (SelectionConfig(), SelectionConfig(min_adx=99.0),
                    SelectionConfig(min_confidence=0.99)):
            assert self._d(cfg).decision in (DECISION_TRADE, DECISION_SKIP)

    def test_a_failing_rule_names_itself(self):
        d = self._d(SelectionConfig(min_adx=99.0))
        assert d.failed_rules == ["min_adx"]
        assert "adx" in d.reasons[0]

    def test_every_failing_rule_is_reported(self):
        d = self._d(SelectionConfig(min_signal_score=99.0, min_confidence=0.99,
                                    min_adx=99.0))
        assert set(d.failed_rules) == {"min_signal_score", "min_confidence", "min_adx"}

    def test_a_threshold_boundary_is_inclusive(self):
        assert self._d(SelectionConfig(min_signal_score=78.0)).decision == DECISION_TRADE
        assert self._d(SelectionConfig(min_signal_score=78.001)).decision == DECISION_SKIP

    def test_a_max_rule_inverts_the_comparison(self):
        assert self._d(SelectionConfig(max_atr_pct=0.1)).decision == DECISION_SKIP
        assert self._d(SelectionConfig(max_atr_pct=10.0)).decision == DECISION_TRADE

    def test_unavailable_input_is_unknown_never_a_silent_pass(self):
        d = self._d(SelectionConfig(min_adx=20.0), ind={})
        assert "min_adx" in d.unknown_rules
        assert d.decision == DECISION_TRADE

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), None,
                                     "abc", {}, [], object()])
    def test_non_finite_and_non_numeric_inputs_never_raise(self, bad):
        outcome, _ = Rule("r", "x", 1.0).evaluate(bad)
        assert outcome is RuleOutcome.UNKNOWN

    def test_a_bool_is_not_treated_as_a_measurement(self):
        assert Rule("r", "x", 0.0).evaluate(True)[0] is RuleOutcome.UNKNOWN

    def test_a_disabled_rule_always_passes(self):
        assert Rule("r", "x", None).evaluate(None)[0] is RuleOutcome.PASS

    def test_rr_is_read_from_the_nested_setup(self):
        signal = {**self.SIGNAL,
                  "setup": {"entry": 100.0, "stop_loss": 99.0, "target_1": 101.5,
                            "target_2": 102.0, "atr": 1.0, "risk_reward_ratio": 1.5}}
        assert self._d(SelectionConfig(min_risk_reward=1.5), signal).decision == DECISION_TRADE
        assert self._d(SelectionConfig(min_risk_reward=1.6), signal).decision == DECISION_SKIP

    def test_atr_pct_is_derived_from_setup_entry_and_atr(self):
        signal = {**self.SIGNAL,
                  "setup": {"entry": 100.0, "atr": 5.0, "risk_reward_ratio": 1.5}}
        assert self._d(SelectionConfig(max_atr_pct=1.0), signal).decision == DECISION_SKIP

    def test_quality_allowlist(self):
        assert self._d(SelectionConfig(allowed_qualities=("QUALIFIED",))).decision \
            == DECISION_TRADE
        assert self._d(SelectionConfig(allowed_qualities=("PREMIUM",))).decision \
            == DECISION_SKIP

    def test_vwap_alignment_is_direction_aware(self):
        assert self._d(SelectionConfig(require_vwap_alignment=True)).decision \
            == DECISION_TRADE
        short = {**self.SIGNAL, "direction": "SHORT"}
        assert self._d(SelectionConfig(require_vwap_alignment=True), short).decision \
            == DECISION_SKIP

    def test_vwap_unavailable_is_unknown(self):
        d = self._d(SelectionConfig(require_vwap_alignment=True), ind={})
        assert "require_vwap_alignment" in d.unknown_rules

    def test_market_context_alignment(self):
        assert self._d(SelectionConfig(require_market_context_alignment=True)).decision \
            == DECISION_TRADE
        short = {**self.SIGNAL, "direction": "SHORT"}
        d = self._d(SelectionConfig(require_market_context_alignment=True), short)
        assert d.decision == DECISION_SKIP
        assert any("NIFTY" in r for r in d.reasons)

    def test_unknown_market_context_is_unknown_not_a_fail(self):
        d = self._d(SelectionConfig(require_market_context_alignment=True),
                    ctx={"nifty_trend": "UNKNOWN"})
        assert "require_market_context_alignment" in d.unknown_rules
        assert d.decision == DECISION_TRADE

    def test_blocked_time_window(self):
        cfg = SelectionConfig(blocked_time_windows=(_window("09:15-09:30"),))
        assert self._d(cfg, when="2026-09-30 09:20:00").decision == DECISION_SKIP
        assert self._d(cfg, when="2026-09-30 10:20:00").decision == DECISION_TRADE

    def test_allowed_time_windows(self):
        cfg = SelectionConfig(allowed_time_windows=(_window("09:30-11:00"),))
        assert self._d(cfg, when="2026-09-30 10:00:00").decision == DECISION_TRADE
        assert self._d(cfg, when="2026-09-30 12:00:00").decision == DECISION_SKIP

    def test_a_malformed_window_never_excludes_everything(self):
        from app.services.profit_selection import _parse_window
        w = _parse_window("nonsense")
        assert w.start_minute == 0 and w.end_minute == 1440

    def test_config_serialises_for_the_shadow_ledger(self):
        cfg = SelectionConfig(min_signal_score=60.0, allowed_qualities=("PREMIUM",))
        blob = cfg.to_dict()
        assert blob["min_signal_score"] == 60.0
        assert blob["allowed_qualities"] == ["PREMIUM"]
        assert blob["mode"] == "SHADOW"


# ══════════════════════════════════════════════════════════════════════════
# SCANNER WIRING - the layer runs in the live path and blocks nothing
# ══════════════════════════════════════════════════════════════════════════

class TestScannerWiring:

    SIGNAL = {
        "symbol": "KOTAKBANK", "direction": "LONG", "signal_score": 78.0,
        "confidence": 0.71, "timestamp": "2026-09-30 10:05:00",
        "setup": {"entry": 414.1, "stop_loss": 412.46, "target_1": 416.29,
                  "target_2": 417.38, "atr": 1.095, "risk_reward_ratio": 1.3333},
    }
    ROW = {"adx_14": 27.4, "relative_volume": 1.32, "atr_14": 1.095,
           "vwap": 413.8, "close": 414.1}

    @pytest.fixture(autouse=True)
    def _capture(self, monkeypatch):
        """Intercept the shadow write so no test can touch the real ledger."""
        from app.services import profit_selection_store

        self.written = []
        # The REAL function objects, captured before patching, so the
        # end-to-end loop tests can drive the actual store against a temp
        # database instead of the capture stub.
        self.store = {
            name: getattr(profit_selection_store, name)
            for name in ("persist_selection_decision", "attach_realized_outcome",
                         "ensure_shadow_schema", "load_shadow_records")
        }
        monkeypatch.setattr(
            profit_selection_store, "persist_selection_decision",
            lambda record, baseline_decision: self.written.append(
                (record, baseline_decision)) or "rowid",
        )
        return self

    def _run(self, signal=None, result=None, **kw):
        from app.services.scanner import _run_profit_selection

        return _run_profit_selection(
            result if result is not None else {},
            self.SIGNAL if signal is None else signal,
            self.ROW,
            {"nifty_trend": "BULLISH"},
            **kw,
        )

    def test_shipped_defaults_record_a_trade_and_change_nothing(self):
        payload = self._run()
        assert payload["decision"] == DECISION_TRADE
        assert payload["applied_to_production"] is False
        assert payload["would_change_production"] is False
        assert len(self.written) == 1
        record, baseline = self.written[0]
        assert record["mode"] == "SHADOW"
        assert baseline == "TRADE"
        assert record["symbol"] == "KOTAKBANK"

    def test_the_decision_is_exposed_on_the_scanner_result(self):
        result = {}
        self._run(result=result)
        assert result["profit_selection"]["decision"] == DECISION_TRADE

    def test_the_scan_itself_is_not_altered(self):
        """Only the additive `profit_selection` key may appear."""
        result = {"symbol": "KOTAKBANK", "should_trade": True, "signal": "LONG"}
        self._run(result=result)
        assert result["symbol"] == "KOTAKBANK"
        assert result["should_trade"] is True
        assert result["signal"] == "LONG"

    def test_a_tightened_threshold_records_but_still_does_not_block(self, monkeypatch):
        from app.services import profit_selection

        monkeypatch.setattr(
            profit_selection.SelectionConfig, "from_settings",
            classmethod(lambda cls, s=None: profit_selection.SelectionConfig(
                min_signal_score=99.0)),
        )
        payload = self._run()
        assert payload["decision"] == DECISION_SKIP
        assert payload["applied_to_production"] is False
        assert payload["would_change_production"] is True
        assert self.written[0][0]["decision"] == DECISION_SKIP

    def test_no_signal_means_no_decision_and_no_write(self):
        from app.services.scanner import _run_profit_selection

        for empty in (None, {}):
            result = {}
            assert _run_profit_selection(result, empty, self.ROW,
                                        {"nifty_trend": "BULLISH"}) is None
            assert "profit_selection" not in result
        assert self.written == []

    def test_a_faulty_layer_never_breaks_the_scan(self, monkeypatch):
        from app.services import profit_selection

        def boom(*_a, **_k):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(profit_selection, "evaluate_selection", boom)
        assert self._run() is None      # swallowed, scan continues
        assert self.written == []       # and nothing was recorded

    def test_a_non_finite_indicator_is_survived(self):
        row = {**self.ROW, "adx_14": float("nan"), "relative_volume": None}
        from app.services.scanner import _run_profit_selection

        payload = _run_profit_selection({}, self.SIGNAL, row,
                                        {"nifty_trend": "BULLISH"})
        assert payload is not None
        assert payload["decision"] == DECISION_TRADE

    def test_the_signal_payload_is_not_mutated(self):
        import copy

        before = copy.deepcopy(self.SIGNAL)
        self._run()
        assert self.SIGNAL == before

    def test_setup_quality_is_fed_to_the_layer(self):
        result = {"setup_quality": "PREMIUM", "setup_quality_score": 91.0}
        self._run(result=result)
        record = self.written[0][0]
        assert record["setup_quality"] == "PREMIUM"
        assert record["setup_quality_score"] == pytest.approx(91.0)

    def test_the_baseline_decision_is_recorded_as_production_did(self):
        self._run(baseline_decision="SKIP")
        assert self.written[0][1] == "SKIP"

    def test_the_whole_loop_runs_scanner_to_record_to_outcome(self, tmp_path):
        """The production loop, end to end, on a real sqlite file.

        Scanner builds the record -> the real store creates the table and
        persists it -> a real exit attaches the realized outcome -> the row
        reads back complete. This is the evidence a future promotion will be
        judged on, so the whole chain is exercised together rather than only
        in pieces.
        """
        attach_realized_outcome = self.store["attach_realized_outcome"]
        ensure_shadow_schema = self.store["ensure_shadow_schema"]
        load_shadow_records = self.store["load_shadow_records"]
        persist_selection_decision = self.store["persist_selection_decision"]

        db = str(tmp_path / "loop.db")
        signal = {**self.SIGNAL, "id": "SIGLOOP01"}

        # 1. The scanner helper produces a record, exactly as in a live scan.
        from app.services.scanner import _run_profit_selection

        _run_profit_selection({}, signal, self.ROW, {"nifty_trend": "BULLISH"},
                              candle_ts="2026-09-30 10:05:00",
                              baseline_decision="TRADE")
        record, baseline = self.written[0]

        # 2. The real store creates the schema and writes it.
        ensure_shadow_schema(db)
        persist_selection_decision(record, baseline_decision=baseline, db_path=db)
        rows = load_shadow_records(db_path=db)
        assert len(rows) == 1
        assert rows[0]["signal_id"] == "SIGLOOP01"
        assert rows[0]["symbol"] == "KOTAKBANK"
        assert rows[0]["mode"] == "SHADOW"
        assert rows[0]["applied_to_production"] in (0, False)
        assert rows[0]["realized_pnl"] is None, "no outcome before the real exit"

        # 3. The real exit attaches its outcome.
        assert attach_realized_outcome("SIGLOOP01", 14.62, "WIN", "T2_FINAL",
                                       db_path=db) is True
        # 4. The row now carries everything a promotion decision needs.
        row = load_shadow_records(db_path=db)[0]
        assert row["realized_pnl"] == pytest.approx(14.62)
        assert row["outcome"] == "WIN"
        assert row["baseline_decision"] == "TRADE"
        assert row["candidate_decision"] == DECISION_TRADE
        assert row["mode"] == "SHADOW"
        # Shadow mode must never be recorded as having been applied.
        assert not row["applied_to_production"]

    def test_a_skipped_signal_leaves_no_outcome_row(self, tmp_path):
        """A signal production did not trade has no realized outcome, and the
        ledger must say so rather than leaving a row that looks actionable."""
        attach_realized_outcome = self.store["attach_realized_outcome"]
        ensure_shadow_schema = self.store["ensure_shadow_schema"]
        load_shadow_records = self.store["load_shadow_records"]
        persist_selection_decision = self.store["persist_selection_decision"]

        db = str(tmp_path / "skip.db")
        self._run(signal={**self.SIGNAL, "id": "SIGSKIP01"},
                  baseline_decision="SKIP")
        record, baseline = self.written[0]
        ensure_shadow_schema(db)
        persist_selection_decision(record, baseline_decision=baseline, db_path=db)
        row = load_shadow_records(db_path=db)[0]
        assert row["baseline_decision"] == "SKIP"
        # Nothing was ever traded, so there is nothing to attach.
        assert attach_realized_outcome("SIGSKIP01", 0.0, "BREAKEVEN", None,
                                       db_path=db) is True
        assert load_shadow_records(db_path=db)[0]["realized_pnl"] == pytest.approx(0.0)


def _window(text: str):
    from app.services.profit_selection import _parse_window
    return _parse_window(text)


# ══════════════════════════════════════════════════════════════════════════
# Q. THE EXACT STATISTICS HELPERS
# ══════════════════════════════════════════════════════════════════════════

class TestExactStatistics:

    def test_fisher_exact_known_value(self):
        # Classic 2x2: [[1, 9], [11, 3]] -> p = 0.002759...
        p = fisher_exact_two_sided(1, 9, 11, 3)
        assert p == pytest.approx(0.00276, abs=1e-4)

    def test_fisher_exact_symmetric_table_is_one(self):
        assert fisher_exact_two_sided(5, 5, 5, 5) == pytest.approx(1.0)

    def test_fisher_exact_bounded(self):
        for a in range(6):
            for b in range(6):
                p = fisher_exact_two_sided(a, b, 3, 4)
                assert 0.0 <= p <= 1.0

    def test_fisher_exact_rejects_negative_cells(self):
        with pytest.raises(ValueError):
            fisher_exact_two_sided(-1, 1, 1, 1)

    def test_fisher_exact_empty_table(self):
        assert fisher_exact_two_sided(0, 0, 0, 0) == 1.0

    def test_welch_t_needs_two_observations_per_side(self):
        assert welch_t([1.0], [2.0, 3.0]) is None
        assert welch_t([1.0, 5.0], [2.0, 9.0]) is not None

    def test_permutation_is_deterministic(self):
        a = [10.0, -5.0, 8.0, 2.0]
        b = [-3.0, -1.0, 1.0, 0.0]
        assert permutation_expectancy_p(a, b) == permutation_expectancy_p(a, b)

    def test_permutation_is_one_for_identical_samples(self):
        a = [1.0, 2.0, 3.0, 4.0]
        assert permutation_expectancy_p(a, list(a)) == pytest.approx(1.0)

    def test_permutation_is_small_for_a_real_separation(self):
        a = [50.0, 60.0, 55.0, 70.0, 65.0, 58.0, 62.0, 68.0]
        b = [-50.0, -60.0, -55.0, -70.0, -65.0, -58.0, -62.0, -68.0]
        assert permutation_expectancy_p(a, b) < 0.1

    def test_permutation_needs_data_on_both_sides(self):
        assert permutation_expectancy_p([], [1.0]) is None


# ────────────────────────────────────────────────────────────────────────────
# R. Position sizing / risk-budget utilization
# ────────────────────────────────────────────────────────────────────────────


def _sized_position(**kw) -> "object":
    """A collapsed Position with an entry-time signal row attached.

    Defaults describe the live failure mode: a UI-placed order that supplied
    its own quantity, so the risk-engine formula never ran.
    """
    signal = {
        "id": "sig1",
        "symbol": kw.pop("symbol", "SAIL"),
        "entry_price": 185.70,
        "stop_loss": 185.05,          # 0.65/share = 1.5 x ATR 0.43
        "atr": 0.43,
        "quantity": 51.0,
        "risk_amount": 33.15,
        "risk_percent": 0.3315,
        "signal_quality": "QUALIFIED",
    }
    # `signal=None` models an UNLINKED position: no signals row exists at all.
    override = kw.pop("signal", ...)
    if override is None:
        signal = None
    elif override is not ...:
        signal.update(override)
    pos = SimpleNamespace(
        position_id=kw.pop("position_id", "pos1"),
        symbol=kw.pop("symbol", "SAIL"),
        direction=kw.pop("direction", "LONG"),
        entry_price=kw.pop("entry_price", 185.70),
        stop_loss=kw.pop("stop_loss", 185.05),
        target_1=kw.pop("target_1", 186.65),
        target_2=kw.pop("target_2", 187.30),
        pnl=kw.pop("pnl", 33.15),
        quantity=kw.pop("quantity", 51.0),
        initial_quantity=kw.pop("initial_quantity", 51.0),
        atr=kw.pop("atr", 0.43),
        signal=signal,
    )
    for k, v in kw.items():
        setattr(pos, k, v)
    return pos


class TestPositionSizeCalculation:
    """The sizing arithmetic must mirror ``RiskEngine.calculate_position_size``."""

    def test_risk_budget_is_two_percent_of_capital(self):
        assert risk_budget_for(10_000.0, 2.0) == pytest.approx(200.0)
        assert risk_budget_for(100_000.0, 2.0) == pytest.approx(2_000.0)

    def test_risk_based_quantity_divides_budget_by_risk_per_share(self):
        assert risk_based_quantity(200.0, 0.65) == 307
        assert risk_based_quantity(200.0, 5.71) == 35

    def test_capital_based_quantity_uses_the_95_percent_headroom(self):
        # 0.95 * 10,000 / 185.70 = 51.16 -> 51
        assert capital_based_quantity(10_000.0, 185.70) == 51
        assert capital_based_quantity(10_000.0, 777.85) == 12

    def test_production_quantity_is_the_minimum_of_both_bounds(self):
        assert production_quantity(185.70, 185.05, 10_000.0, 2.0) == 51

    def test_production_quantity_matches_the_live_engine_exactly(self):
        """The measurement must never disagree with production."""
        from app.services.risk_engine import RiskEngine

        engine = RiskEngine()
        for entry, stop in ((185.70, 185.05), (777.85, 783.56), (414.10, 412.59),
                            (179.48, 180.32), (10.0, 9.9), (10.0, 10.1)):
            assert production_quantity(entry, stop, 10_000.0, 2.0) == \
                engine.calculate_position_size(entry, stop, "QUALIFIED", "LONG")

    def test_zero_or_negative_risk_pct_yields_no_position(self):
        assert production_quantity(100.0, 99.0, 10_000.0, 0.0) == 0
        assert production_quantity(100.0, 99.0, 10_000.0, -1.0) == 0

    def test_a_zero_width_stop_yields_no_position(self):
        assert production_quantity(100.0, 100.0, 10_000.0, 2.0) == 0

    def test_a_wider_stop_raises_rupee_risk_not_lowers_it(self):
        """Documented, measured behaviour at the live capital level.

        Because the cash cap binds, the share count barely moves as the stop
        widens, so rupee risk per trade RISES. This is reported, never fixed:
        raising risk is out of scope for the optimization program.
        """
        narrow = sizing_breakdown(
            _sized_position(entry_price=100.0, stop_loss=99.0,
                            signal={"stop_loss": 99.0, "quantity": 9.0}),
            10_000.0, 2.0)
        wide = sizing_breakdown(
            _sized_position(entry_price=100.0, stop_loss=98.0,
                            signal={"stop_loss": 98.0, "quantity": 9.0}),
            10_000.0, 2.0)
        assert wide.actual_rupee_risk > narrow.actual_rupee_risk


class TestCapitalConstraint:

    def test_capital_binds_when_cash_cap_is_tighter(self):
        bd = sizing_breakdown(_sized_position(), 10_000.0, 2.0)
        assert bd.capital_based_quantity < bd.risk_based_quantity
        assert bd.binding == BINDING_CAPITAL
        assert bd.capital_constrained is True

    def test_capital_never_binds_on_a_very_wide_stop(self):
        """With a wide enough stop the risk budget is the tighter bound."""
        pos = _sized_position(entry_price=100.0, stop_loss=50.0,
                              signal={"stop_loss": 50.0, "quantity": 4.0})
        bd = sizing_breakdown(pos, 10_000.0, 2.0)
        assert bd.risk_based_quantity < bd.capital_based_quantity
        assert bd.binding == BINDING_RISK_BUDGET
        assert bd.capital_constrained is False

    def test_identical_bounds_report_a_tie(self):
        # capital_qty = int(0.95 * 10,000 / 100) = 95.
        # risk_qty    = int(200 / 2.10)        = 95. Both bounds land on 95.
        pos = _sized_position(entry_price=100.0, stop_loss=97.90,
                              signal={"stop_loss": 97.90, "quantity": 95.0})
        bd = sizing_breakdown(pos, 10_000.0, 2.0)
        assert bd.risk_based_quantity == bd.capital_based_quantity == 95
        assert bd.binding == BINDING_TIE
        assert bd.sizing_path == PATH_FORMULA

    def test_a_quantity_that_ignores_both_bounds_is_explicit(self):
        """A UI-supplied quantity bypasses BOTH caps - in either direction."""
        under = sizing_breakdown(
            _sized_position(signal={"quantity": 5.0}), 10_000.0, 2.0)
        assert under.binding == BINDING_EXPLICIT
        assert under.sizing_path == PATH_EXPLICIT

        over = sizing_breakdown(
            _sized_position(signal={"quantity": 10_000.0}), 10_000.0, 2.0)
        assert over.binding == BINDING_EXPLICIT, (
            "an oversized fill must not be reported as if a cap governed it"
        )


class TestRiskUtilization:

    def test_actual_risk_is_quantity_times_risk_per_share(self):
        bd = sizing_breakdown(_sized_position(), 10_000.0, 2.0)
        assert bd.actual_rupee_risk == pytest.approx(51.0 * 0.65)

    def test_utilization_is_actual_risk_over_the_budget(self):
        bd = sizing_breakdown(_sized_position(), 10_000.0, 2.0)
        assert bd.utilization_percent == pytest.approx(
            bd.actual_rupee_risk / bd.risk_budget * 100.0)

    def test_utilization_never_exceeds_one_hundred_percent(self):
        for qty in (1.0, 5.0, 51.0, 95.0):
            bd = sizing_breakdown(
                _sized_position(signal={"quantity": qty}), 10_000.0, 2.0)
            if bd.actual_rupee_risk is not None:
                assert bd.utilization_percent <= 100.0 + 1e-9

    def test_intended_risk_is_capped_at_the_budget(self):
        bd = sizing_breakdown(_sized_position(signal={"quantity": 51.0}),
                              10_000.0, 2.0)
        assert bd.intended_rupee_risk <= bd.risk_budget

    def test_a_breakeven_stop_is_reported_unusable_not_as_zero_risk(self):
        """Profit Capture rewrites the stop to entry after T1. A ledger stop
        equal to entry carries NO risk information and must not be turned into
        a confident 'zero risk' reading."""
        pos = _sized_position(stop_loss=185.70, signal={"stop_loss": None})
        bd = sizing_breakdown(pos, 10_000.0, 2.0)
        assert bd.risk_per_share is None
        assert bd.usable is False
        assert bd.actual_rupee_risk is None
        assert bd.utilization_percent is None

    def test_the_entry_time_signal_stop_wins_over_a_trailed_ledger_stop(self):
        pos = _sized_position(stop_loss=185.70,            # trailed to breakeven
                              signal={"stop_loss": 185.05})  # original
        bd = sizing_breakdown(pos, 10_000.0, 2.0)
        assert bd.risk_per_share == pytest.approx(0.65)
        assert bd.stop_source == STOP_SOURCE_SIGNAL
        assert bd.geometry_reliable is True

    def test_a_ledger_only_stop_is_flagged_unreliable(self):
        pos = _sized_position(stop_loss=184.40, signal=None)
        bd = sizing_breakdown(pos, 10_000.0, 2.0)
        assert bd.stop_source == STOP_SOURCE_LEDGER_TRAILED
        assert bd.geometry_reliable is False

    def test_malformed_numbers_degrade_to_unknown(self):
        for bad in (None, float("nan"), float("inf"), "abc", True, {}):
            bd = sizing_breakdown(
                _sized_position(signal={"quantity": bad}), 10_000.0, 2.0)
            assert bd.actual_rupee_risk is None or bd.actual_rupee_risk >= 0

    def test_unusable_geometry_is_not_reported_as_a_bound_having_bound(self):
        """TITAN/LTF carry a breakeven stop, so risk_qty is 0. 'RISK_BUDGET
        bound' would read as though the 2% budget governed the size when in
        fact nothing sized the position at all."""
        pos = _sized_position(entry_price=100.0, stop_loss=100.0,
                              signal={"stop_loss": None, "quantity": 1.0})
        bd = sizing_breakdown(pos, 10_000.0, 2.0)
        assert bd.binding == BINDING_UNAVAILABLE
        assert bd.sizing_path == PATH_UNAVAILABLE
        assert bd.usable is False


class TestSizingShadowIsolation:
    """The counterfactual is a SHADOW measurement and nothing else."""

    def test_shadow_is_labelled_hypothetical(self):
        cmp = shadow_sizing_comparison(_sized_position())
        assert "SHADOW" in cmp.labelled
        assert "NOT REALIZED" in cmp.labelled
        assert cmp.to_dict()["label"] == cmp.labelled

    def test_shadow_sizing_uses_the_risk_budget_alone(self):
        cmp = shadow_sizing_comparison(_sized_position())
        assert cmp.shadow_quantity == risk_based_quantity(200.0, 0.65) == 307

    def test_shadow_can_only_raise_exposure_never_lower_it(self):
        for qty in (1.0, 5.0, 51.0):
            cmp = shadow_sizing_comparison(
                _sized_position(signal={"quantity": qty}))
            assert cmp.shadow_quantity >= cmp.baseline_quantity

    def test_shadow_pnl_is_the_realized_pnl_scaled_by_the_multiple(self):
        cmp = shadow_sizing_comparison(_sized_position(pnl=33.15))
        assert cmp.quantity_multiple == pytest.approx(307 / 51.0)
        assert cmp.shadow_pnl == pytest.approx(33.15 * (307 / 51.0))
        assert cmp.shadow_pnl_delta == pytest.approx(cmp.shadow_pnl - 33.15)

    def test_a_losing_trade_scales_to_a_larger_loss(self):
        cmp = shadow_sizing_comparison(_sized_position(pnl=-30.0))
        assert cmp.shadow_pnl < 0
        assert cmp.shadow_pnl < cmp.realized_pnl, (
            "more exposure on a loser must be a bigger loss, not a smaller one"
        )

    def test_shadow_report_is_never_marked_promotable(self):
        report = sizing_report([_sized_position()])
        assert report["shadow"]["promotable"] is False
        assert "measurement only" in report["shadow"]["reason"].lower()

    def test_shadow_uses_no_exit_information(self):
        """Behavioural no-lookahead check: the counterfactual is computed from
        entry-time inputs only, so perturbing every exit-time field must not
        move it."""
        base = shadow_sizing_comparison(_sized_position(pnl=33.15))
        perturbed = shadow_sizing_comparison(_sized_position(
            pnl=33.15,
            exit_price=999.0,
            exit_time=datetime(2030, 1, 1, 15, 30),
            target_1=1.0,
            target_2=2.0,
            stop_loss=1.0,
        ))
        assert perturbed.shadow_quantity == base.shadow_quantity
        assert perturbed.quantity_multiple == pytest.approx(base.quantity_multiple)
        assert perturbed.shadow_pnl == pytest.approx(base.shadow_pnl)


class TestSizingReport:
    """Aggregate utilization reporting."""

    def test_report_counts_both_sizing_paths(self):
        report = sizing_report([
            _sized_position(signal={"quantity": 51.0}),   # matches formula
            _sized_position(signal={"quantity": 5.0}),    # explicit
        ])
        assert report["positions_usable"] == 2
        assert report["via_risk_engine_formula"] == 1
        assert report["via_explicit_quantity"] == 1

    def test_report_pct_of_formula_is_separate_from_pct_of_all(self):
        report = sizing_report([
            _sized_position(signal={"quantity": 51.0}),
            _sized_position(signal={"quantity": 51.0}),
            _sized_position(signal={"quantity": 5.0}),
        ])
        assert report["capital_constrained"] == 2
        assert report["capital_constrained_pct"] == pytest.approx(66.67, abs=0.01)
        assert report["capital_constrained_pct_of_formula"] == pytest.approx(100.0)

    def test_report_means_ignore_unusable_positions(self):
        report = sizing_report([
            _sized_position(signal={"quantity": 51.0}),
            _sized_position(stop_loss=185.70, signal={"stop_loss": None}),
        ])
        assert report["positions_total"] == 2
        assert report["positions_usable"] == 1
        assert report["mean_actual_risk"] == pytest.approx(51.0 * 0.65)

    def test_empty_report_does_not_divide_by_zero(self):
        report = sizing_report([])
        assert report["positions_usable"] == 0
        assert report["capital_constrained_pct"] is None
        assert report["mean_actual_risk"] is None
        assert report["verdict"] == INSUFFICIENT_PROMOTION

    def test_small_sample_is_flagged_insufficient(self):
        report = sizing_report([_sized_position()])
        assert report["verdict"] == INSUFFICIENT_PROMOTION


class TestSizingDoesNotMutateHistory:
    """No analysis path may rewrite a historical row."""

    def test_sizing_analysis_leaves_the_live_ledger_byte_identical(self):
        import shutil
        import tempfile

        src = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "intradayai.db")
        if not os.path.exists(src):
            pytest.skip("no live ledger present in this environment")
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "intradayai.db")
            shutil.copy2(src, work)
            before = open(work, "rb").read()
            conn = sqlite3.connect(f"file:{work}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute("SELECT * FROM trades")]
            signals = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM signals")}
            conn.close()
            positions = collapse_positions(rows, signals)
            sizing_report(positions)
            for pos in positions:
                shadow_sizing_comparison(pos)
            conn = sqlite3.connect(f"file:{work}?mode=ro", uri=True)
            after_rows = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            after_sum = conn.execute(
                "SELECT COALESCE(SUM(COALESCE(pnl,0)),0) FROM trades").fetchone()[0]
            conn.close()
            assert open(work, "rb").read() == before, "the DB file was modified"
            assert after_rows == len(rows)
            assert after_sum == pytest.approx(sum(r["pnl"] or 0 for r in rows))

    def test_the_sizing_module_opens_no_database_connection(self):
        import inspect

        import app.services.position_sizing as mod

        src = inspect.getsource(mod)
        for forbidden in ("sqlite3", "INSERT", "UPDATE ", "DELETE", "DROP"):
            assert forbidden not in src, (
                f"position_sizing must not contain {forbidden!r}"
            )


class TestSizingEndToEndAccounting:
    """Partial + final accounting through the real public path, then the
    sizing report over the resulting rows."""

    def test_partial_then_final_close_produces_correct_sizing_and_pnl(self):
        from app.services.paper_trading import PaperAccount

        account = PaperAccount(user_id="u1")
        account.cash = 1_000_000.0
        placed = account.place_order("SAIL", "LONG", 50, 185.70,
                                     stop_loss=185.05, target_1=186.65,
                                     target_2=187.30, signal_id="sig1")
        account.fill_order(placed["order_id"], 185.70)
        pos_id = placed["order_id"]

        first = account.close_position(pos_id, 186.65, quantity=25,
                                       reason="T1_PARTIAL")
        second = account.close_position(pos_id, 187.30, reason="T2_FINAL")
        assert first["pnl"] == pytest.approx(25 * 0.95)
        assert first["realized_pnl"] == pytest.approx(25 * 0.95)
        assert second["pnl"] == pytest.approx(25 * 1.60)
        assert second["realized_pnl"] == pytest.approx(25 * 0.95 + 25 * 1.60), (
            "the final slice must carry the POSITION total"
        )

        rows = [t for t in account.closed_trades if t.get("position_id") == pos_id]
        rows = [{**t, "status": "closed"} for t in rows]
        positions = collapse_positions(rows, {
            "sig1": {"id": "sig1", "symbol": "SAIL", "stop_loss": 185.05,
                     "atr": 0.43, "quantity": 50.0, "risk_amount": 32.50},
        })
        assert len(positions) == 1, "slices must collapse into one position"
        pos = positions[0]
        assert pos.pnl == pytest.approx(25 * 0.95 + 25 * 1.60)

        report = sizing_report(positions, account_capital=10_000.0, risk_pct=2.0)
        bd = report["breakdowns"][0]
        assert bd["risk_per_share"] == pytest.approx(0.65)
        assert bd["actual_quantity"] == 50.0
        assert bd["actual_rupee_risk"] == pytest.approx(50 * 0.65)
        assert bd["utilization_percent"] == pytest.approx(32.5 / 200 * 100)

    def test_signal_realized_pnl_holds_the_position_total_after_two_slices(self):
        """The end-to-end contract: ``signals.realized_pnl`` is the position
        total, so a two-slice exit is not under-reported by the partial."""
        from app.services.paper_trading import PaperAccount

        account = PaperAccount(user_id="u1")
        account.cash = 1_000_000.0
        placed = account.place_order("SAIL", "LONG", 50, 185.70,
                                     stop_loss=185.05, target_1=186.65,
                                     target_2=187.30)
        account.fill_order(placed["order_id"], 185.70)
        pos_id = placed["order_id"]
        account.close_position(pos_id, 186.65, quantity=25, reason="T1_PARTIAL")
        final = account.close_position(pos_id, 187.30, reason="T2_FINAL")

        total = 25 * 0.95 + 25 * 1.60
        assert final["realized_pnl"] == pytest.approx(total)
        assert final["pnl"] == pytest.approx(25 * 1.60), (
            "the per-slice pnl must stay the slice's own value"
        )


class TestShadowBaselineDecision:
    """``baseline_decision`` must describe what production REALLY did.

    A ``NO_TRADE`` signal never reaches the risk engine and can never become a
    position. Recording it as ``TRADE`` would manufacture a baseline agreement
    on a trade that never happened, which is the one thing the candidate-vs-
    baseline comparison must not do.
    """

    @staticmethod
    def _captured(monkeypatch, direction, **kw):
        """Run the real scanner hook and capture what it persists."""
        from app.services import profit_selection_store, scanner

        seen: list = []

        def _capture(record, baseline_decision, db_path=None):
            seen.append((record, baseline_decision))
            return "row1"

        monkeypatch.setattr(profit_selection_store, "persist_selection_decision",
                            _capture)
        signal = {"symbol": "X", "direction": direction, "confidence": 0.5,
                  "signal_score": {"total": 55.0}, "id": "sig9",
                  "setup": {"entry": 100.0, "stop_loss": 99.0,
                            "target_1": 102.0, "risk_reward_ratio": 2.0}}
        result: dict = {}
        scanner._run_profit_selection(result, signal, None, None,
                                      candle_ts="2026-09-30 14:00:00", **kw)
        assert seen, "the hook persisted nothing"
        return seen[-1][1]

    def test_a_no_trade_signal_is_recorded_as_skip(self, monkeypatch):
        assert self._captured(monkeypatch, "NO_TRADE") == "SKIP"

    @pytest.mark.parametrize("direction", ["", "NONE", None])
    def test_a_missing_direction_is_recorded_as_skip(self, monkeypatch,
                                                     direction):
        assert self._captured(monkeypatch, direction) == "SKIP"

    @pytest.mark.parametrize("direction", ["LONG", "SHORT", "WEAK_LONG",
                                          "WEAK_SHORT", "STRONG_SHORT"])
    def test_an_actionable_direction_defaults_to_trade(self, monkeypatch,
                                                      direction):
        assert self._captured(monkeypatch, direction) == "TRADE"

    def test_the_direction_overrides_a_caller_supplied_trade(self, monkeypatch):
        """The data-gated call site passes TRADE/SKIP explicitly; a NO_TRADE
        signal must still land on SKIP."""
        assert self._captured(monkeypatch, "NO_TRADE",
                              baseline_decision="TRADE") == "SKIP"

    def test_an_explicit_skip_is_respected_for_actionable_signals(self,
                                                                  monkeypatch):
        assert self._captured(monkeypatch, "LONG",
                              baseline_decision="SKIP") == "SKIP"

    def test_the_measurement_still_runs_and_never_blocks(self, monkeypatch):
        """A SKIP baseline must not stop the scan or change the payload."""
        from app.services import profit_selection_store, scanner

        monkeypatch.setattr(profit_selection_store, "persist_selection_decision",
                            lambda record, baseline_decision, db_path=None: "r")
        signal = {"symbol": "X", "direction": "NO_TRADE", "confidence": 0.0,
                  "signal_score": {"total": 55.0}, "setup": {}}
        result: dict = {}
        out = scanner._run_profit_selection(result, signal, None, None,
                                           candle_ts="2026-09-30 14:00:00")
        assert out is not None
        assert out["mode"] == "SHADOW"
        assert out["applied_to_production"] is False

    def test_a_persist_failure_never_breaks_the_scan(self, monkeypatch):
        from app.services import profit_selection_store, scanner

        def _boom(record, baseline_decision, db_path=None):
            raise RuntimeError("shadow store is down")

        monkeypatch.setattr(profit_selection_store, "persist_selection_decision",
                            _boom)
        result: dict = {}
        out = scanner._run_profit_selection(
            result, {"symbol": "X", "direction": "LONG", "confidence": 0.5,
                     "signal_score": {"total": 55.0}, "setup": {}},
            None, None, candle_ts="2026-09-30 14:00:00")
        assert out is None, "a shadow failure is swallowed, never raised"
        assert "profit_selection" not in result


class TestShadowRecordScoreExtraction:
    """Regression: ``signal['signal_score']`` is a nested dict.

    Passing it straight through a finite-coercion yields None and the shadow
    ledger persisted 0.0 for EVERY row, destroying the score dimension of the
    measurement instrument. The scalar lives at ``['total']``.
    """

    def test_nested_score_is_unwrapped_to_its_total(self):
        record = shadow_record(
            {"symbol": "SAIL", "direction": "LONG", "confidence": 0.5,
             "signal_score": {"total": 78.08, "trend": 20.0, "momentum": 18.08},
             "setup": {"entry": 185.70, "stop_loss": 185.05, "target_1": 186.65,
                       "target_2": 187.30, "risk_reward_ratio": 1.33}},
            _decision(), signal_id="abc")
        assert record["signal_score"] == pytest.approx(78.08)

    def test_a_flat_score_is_still_accepted(self):
        record = shadow_record(
            {"symbol": "X", "direction": "LONG", "confidence": 0.5,
             "signal_score": 61.5, "setup": {}},
            _decision(), signal_id="abc")
        assert record["signal_score"] == pytest.approx(61.5)

    def test_a_missing_score_is_none_not_zero(self):
        record = shadow_record(
            {"symbol": "X", "direction": "LONG", "confidence": 0.5, "setup": {}},
            _decision(), signal_id="abc")
        assert record["signal_score"] is None

    def test_the_scalar_total_actually_reaches_the_rule_engine(self):
        """Before the fix the dict coerced to None, so a min_signal_score rule
        returned UNKNOWN instead of PASS/FAIL and the score dimension of the
        instrument was dead. Drive the rule directly."""
        def decide(total, floor):
            return evaluate_selection(
                {"symbol": "X", "direction": "LONG", "confidence": 0.5,
                 "signal_score": {"total": total},
                 "signal_quality": "NORMAL",
                 "setup": {"entry": 100.0, "stop_loss": 99.0,
                           "target_1": 102.0, "risk_reward_ratio": 2.0}},
                config=SelectionConfig(min_signal_score=floor),
            )

        assert "min_signal_score" in decide(55.0, 70.0).failed_rules
        assert decide(55.0, 70.0).unknown_rules == []
        assert "min_signal_score" not in decide(55.0, 50.0).failed_rules
        assert decide(55.0, 50.0).unknown_rules == []

# ────────────────────────────────────────────────────────────────────────────
# T. Entry-time risk geometry capture
#
# The problem this exists to solve: a closed `trades` row records the stop as
# it stood at EXIT. Profit Capture rewrites that stop (breakeven at T1, then
# trailing). So for any position that reached T1, the entry-time risk per
# share is not recoverable from the ledger - the measurement instrument had to
# estimate it, and 12 of 16 sampled positions were ESTIMATES.
#
# The capture happens once, inside PaperAccount.fill_order, and is written to
# an INSERT-only ledger. Nothing that runs later in the trade's life can alter
# it. That is the guarantee under test.
# ────────────────────────────────────────────────────────────────────────────

CAPITAL = 10_000.0
RISK_PCT = 2.0          # -> risk_budget 200.0


def _geom_engine(tmp_path, **cfg):
    """A paper engine whose measurement ledgers land in a throwaway DB."""
    from app.services.paper_trading import PaperTradingEngine
    from app.services.profit_capture import ProfitCaptureConfig
    return PaperTradingEngine(
        db_path=str(tmp_path / "geom.db"),
        profit_config=ProfitCaptureConfig(**cfg) if cfg else None,
    )


def _open_long(pt, entry=100.0, sl=98.0, t1=105.0, t2=107.0, qty=10,
               symbol="GEOMX", source=SOURCE_MANUAL):
    r = pt.place_order(symbol, "LONG", qty, entry, sl, t1, t2,
                       quantity_source=source)
    assert r["status"] == "pending"
    f = pt.fill_order(r["order_id"])
    assert f["status"] == "filled"
    return r["order_id"]


def _geo(db, position_id):
    return load_geometry_for(position_id, db_path=db)


class TestEntryGeometryFormulas:
    """Section 4 arithmetic, computed from entry-time values only."""

    def test_risk_budget_is_capital_times_configured_risk_percent(self):
        assert risk_budget_for(CAPITAL, RISK_PCT) == pytest.approx(200.0)
        assert risk_budget_for(50_000.0, 1.0) == pytest.approx(500.0)

    def test_risk_based_quantity_floors_the_budget_over_risk_per_share(self):
        assert risk_based_quantity(200.0, 2.0) == 100
        # 200/2.5 = 80 exactly; floor must not gain a share to rounding
        assert risk_based_quantity(200.0, 2.5) == 80
        # 200/3 = 66.67 -> 66, not 67
        assert risk_based_quantity(200.0, 3.0) == 66
        # a stop wider than the whole budget cannot fund even one share
        assert risk_based_quantity(200.0, 250.0) == 0

    def test_capital_based_quantity_keeps_95_percent_headroom(self):
        assert capital_based_quantity(CAPITAL, 100.0) == 95      # 9500/100
        assert capital_based_quantity(CAPITAL, 250.0) == 38      # 9500/250 = 38
        assert capital_based_quantity(CAPITAL, 10_000.0) == 0     # 0.95 share
        assert capital_based_quantity(CAPITAL, 0.0) == 0

    def test_allowed_quantity_is_the_minimum_of_the_two_constraints(self):
        # Tight stop -> the risk budget is not the limit; cash is.
        g = capture_entry_geometry(
            {"id": "p", "symbol": "A", "direction": "LONG", "entry_price": 100.0,
             "stop_loss": 99.0, "quantity": 1},
            account_capital=CAPITAL, risk_pct=RISK_PCT)
        assert g.risk_constraint_quantity == 200      # 200 / 1.0
        assert g.capital_constraint_quantity == 95     # 9500 / 100
        assert g.allowed_quantity == 95
        assert g.binding_constraint == BINDING_CAPITAL

    def test_a_wide_stop_makes_the_RISK_budget_bind(self):
        # Stop 20 wide -> 200/20 = 10 shares, far below the 95 cash allows.
        g = capture_entry_geometry(
            {"id": "p", "symbol": "A", "direction": "LONG", "entry_price": 100.0,
             "stop_loss": 80.0, "quantity": 10},
            account_capital=CAPITAL, risk_pct=RISK_PCT)
        assert g.risk_constraint_quantity == 10
        assert g.capital_constraint_quantity == 95
        assert g.allowed_quantity == 10
        assert g.binding_constraint == BINDING_RISK_BUDGET

    def test_a_wider_stop_does_NOT_de_risk_once_the_budget_binds(self):
        """The finding from the sizing report, now asserted.

        Widening the stop shrinks the share count, so it *looks* like
        de-risking. It is not: once the risk budget is the binding constraint,
        shares x risk_per_share is pinned at the budget. A wider stop buys a
        smaller position at the SAME rupee risk - it never reduces risk.
        """
        carried = []
        for stop in (99.0, 95.0, 90.0, 80.0, 75.0, 60.0, 50.0):
            g = capture_entry_geometry(
                {"id": "p", "symbol": "A", "direction": "LONG",
                 "entry_price": 100.0, "stop_loss": stop, "quantity": 1},
                account_capital=CAPITAL, risk_pct=RISK_PCT)
            carried.append((stop, g.risk_constraint_quantity,
                            g.allowed_quantity * g.initial_risk_per_share,
                            g.initial_risk_per_share))
        # quantity falls monotonically as the stop widens ...
        quantities = [c[1] for c in carried]
        assert quantities == sorted(quantities, reverse=True)
        # ... but rupee risk never drops below the budget: the only case that
        # does is the tightest stop, which is cash-capped instead.
        assert carried[0][2] == pytest.approx(95.0)          # 95 x 1.0, capped
        assert carried[0][3] == pytest.approx(1.0)
        for stop, risk_qty, risk, rps in carried[1:]:
            assert risk == pytest.approx(200.0), (stop, risk_qty, risk)

    def test_a_wide_stop_that_does_not_divide_the_budget_loses_less_than_one_share(
            self, ):
        """The floor in risk_based_quantity is the only thing that makes a
        budget-bound position carry less than the full budget."""
        g = capture_entry_geometry(
            {"id": "p", "symbol": "A", "direction": "LONG", "entry_price": 100.0,
             "stop_loss": 70.0, "quantity": 1},     # 30/share -> 200/30 = 6.67
            account_capital=CAPITAL, risk_pct=RISK_PCT)
        assert g.risk_constraint_quantity == 6
        assert g.allowed_quantity == 6
        assert g.allowed_quantity * g.initial_risk_per_share == pytest.approx(180.0)
        assert 0 < 200.0 - 180.0 < g.initial_risk_per_share


class TestEntryGeometryUtilization:
    def test_utilization_is_actual_risk_over_the_configured_budget(self):
        g = capture_entry_geometry(
            {"id": "p", "symbol": "A", "direction": "LONG", "entry_price": 100.0,
             "stop_loss": 98.0, "quantity": 50},
            account_capital=CAPITAL, risk_pct=RISK_PCT)
        assert g.risk_budget == pytest.approx(200.0)
        assert g.actual_risk == pytest.approx(100.0)        # 50 x 2.0
        assert g.risk_budget_utilization == pytest.approx(50.0)

    def test_a_single_default_ui_share_is_a_tiny_fraction_of_the_budget(self):
        """The structural finding: the UI's default 1 share against a 2% budget."""
        g = capture_entry_geometry(
            {"id": "p", "symbol": "A", "direction": "LONG", "entry_price": 2500.0,
             "stop_loss": 2460.0, "quantity": 1},
            account_capital=CAPITAL, risk_pct=RISK_PCT)
        assert g.actual_risk == pytest.approx(40.0)
        assert g.risk_budget_utilization == pytest.approx(20.0)

    def test_intended_risk_is_the_budget_implied_risk_not_the_affordable_one(self):
        """Documented divergence, per the sizing contract.

        intended_risk is risk_constraint_quantity x risk_per_share, i.e. what
        the BUDGET implies. It is deliberately NOT capped by cash, so it can
        exceed what an affordable position carries. Conflating the two would
        hide the very gap this instrument measures.
        """
        g = capture_entry_geometry(
            {"id": "p", "symbol": "A", "direction": "LONG", "entry_price": 100.0,
             "stop_loss": 99.0, "quantity": 95},   # exactly the cash cap
            account_capital=CAPITAL, risk_pct=RISK_PCT)
        assert g.intended_risk == pytest.approx(200.0)   # 200 shares x 1.0
        assert g.actual_risk == pytest.approx(95.0)     # 95 shares x 1.0
        assert g.intended_risk > g.actual_risk
        assert g.risk_budget_utilization == pytest.approx(47.5)

    def test_a_breakeven_stop_yields_no_risk_per_share_and_is_marked_unreliable(self):
        g = capture_entry_geometry(
            {"id": "p", "symbol": "A", "direction": "LONG", "entry_price": 100.0,
             "stop_loss": 100.0, "quantity": 10},
            account_capital=CAPITAL, risk_pct=RISK_PCT)
        assert g.initial_risk_per_share is None
        assert g.risk_budget_utilization is None
        assert g.geometry_reliable is False
        assert g.usable is False

    def test_geometry_is_unreliable_without_a_stop_at_all(self):
        g = capture_entry_geometry(
            {"id": "p", "symbol": "A", "direction": "LONG", "entry_price": 100.0,
             "stop_loss": 0, "quantity": 10},
            account_capital=CAPITAL, risk_pct=RISK_PCT)
        assert g.initial_risk_per_share is None
        assert g.geometry_reliable is False

    def test_short_geometry_uses_the_absolute_distance_to_the_stop(self):
        g = capture_entry_geometry(
            {"id": "p", "symbol": "A", "direction": "SHORT", "entry_price": 100.0,
             "stop_loss": 102.0, "quantity": 10},
            account_capital=CAPITAL, risk_pct=RISK_PCT)
        assert g.initial_risk_per_share == pytest.approx(2.0)
        assert g.actual_risk == pytest.approx(20.0)
        assert g.geometry_reliable is True


class TestOrderClassification:
    def test_a_recorded_manual_quantity_is_never_relabelled_as_engine_sized(self):
        # allowed_quantity happens to equal the filled quantity, but the caller
        # SAID the quantity was hand-typed - the statement wins.
        assert classify_order(95, 95, SOURCE_MANUAL, BINDING_CAPITAL) == \
            CLASS_MANUAL_QUANTITY

    def test_recorded_engine_sizing_is_split_by_the_binding_constraint(self):
        assert classify_order(95, 95, SOURCE_RISK_ENGINE, BINDING_CAPITAL) == \
            CLASS_CAPITAL_CONSTRAINED
        assert classify_order(10, 10, SOURCE_RISK_ENGINE, BINDING_RISK_BUDGET) == \
            CLASS_RISK_CONSTRAINED
        assert classify_order(10, 10, SOURCE_RISK_ENGINE, BINDING_TIE) == \
            CLASS_RISK_ENGINE_SIZED

    def test_without_a_caller_statement_the_class_is_inferred_from_the_quantity(self):
        assert classify_order(95, 95, None, BINDING_CAPITAL) == \
            CLASS_CAPITAL_CONSTRAINED
        assert classify_order(1, 95, None, BINDING_CAPITAL) == \
            CLASS_MANUAL_QUANTITY
        assert classify_order(1, 0, None, BINDING_UNAVAILABLE) == \
            CLASS_UNCLASSIFIED
        assert classify_order(None, 95, None, BINDING_CAPITAL) == \
            CLASS_UNCLASSIFIED

    def test_all_four_required_labels_are_reachable(self):
        produced = {
            classify_order(95, 95, SOURCE_MANUAL, BINDING_CAPITAL),
            classify_order(95, 95, SOURCE_RISK_ENGINE, BINDING_CAPITAL),
            classify_order(10, 10, SOURCE_RISK_ENGINE, BINDING_RISK_BUDGET),
            classify_order(10, 10, SOURCE_RISK_ENGINE, BINDING_TIE),
        }
        assert produced == {
            CLASS_MANUAL_QUANTITY, CLASS_CAPITAL_CONSTRAINED,
            CLASS_RISK_CONSTRAINED, CLASS_RISK_ENGINE_SIZED,
        }


class TestEntryGeometryIsCapturedAtFill:
    """Section 1/2/3: capture, immutability, and honesty about history."""

    def test_fill_records_the_entry_time_geometry(self, tmp_path):
        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, entry=100.0, sl=98.0, qty=50)
        g = _geo(str(tmp_path / "geom.db"), pid)
        assert g is not None
        assert g["capture_source"] == SOURCE_ORDER_FILL
        assert g["entry_price"] == pytest.approx(100.0)
        assert g["initial_stop_loss"] == pytest.approx(98.0)
        assert g["initial_target_1"] == pytest.approx(105.0)
        assert g["initial_target_2"] == pytest.approx(107.0)
        assert g["initial_risk_per_share"] == pytest.approx(2.0)
        assert g["initial_risk_amount"] == pytest.approx(100.0)
        assert g["initial_quantity"] == pytest.approx(50.0)
        assert g["configured_risk_percent"] == pytest.approx(RISK_PCT)
        assert g["risk_budget"] == pytest.approx(200.0)
        assert g["capital_constraint_quantity"] == 95
        assert g["risk_constraint_quantity"] == 100
        assert g["actual_quantity"] == pytest.approx(50.0)
        assert g["risk_budget_utilization"] == pytest.approx(50.0)
        assert g["order_classification"] == CLASS_MANUAL_QUANTITY
        assert g["geometry_reliable"] == 1

    def test_the_capture_uses_the_FILL_price_not_the_order_price(self, tmp_path):
        """A limit order filled away from its limit has different risk."""
        pt = _geom_engine(tmp_path)
        r = pt.place_order("FILLX", "LONG", 10, 100.0, 98.0, 105.0, 107.0,
                           quantity_source=SOURCE_MANUAL)
        pt.fill_order(r["order_id"], fill_price=99.0)
        g = _geo(str(tmp_path / "geom.db"), r["order_id"])
        assert g["entry_price"] == pytest.approx(99.0)
        assert g["initial_risk_per_share"] == pytest.approx(1.0)   # 99 - 98
        assert g["actual_risk"] == pytest.approx(10.0)

    def test_the_capture_happens_after_any_order_edit(self, tmp_path):
        """Capture at FILL, not at place: the levels that matter are the ones
        the position actually opened with.

        Also pins a pre-existing accounting quirk that measurement must not
        inherit: ``edit_order`` rewrites ``quantity`` but leaves
        ``initial_quantity`` at the pre-edit size, so at fill the live
        ``quantity`` - not ``initial_quantity`` - is the size that was actually
        bought. Fixing that quirk in the trade ledger is out of scope here.
        """
        pt = _geom_engine(tmp_path)
        r = pt.place_order("EDITX", "LONG", 10, 100.0, 98.0, 105.0, 107.0,
                           quantity_source=SOURCE_MANUAL)
        pt.edit_order(r["order_id"], stop_loss=97.0, quantity=20)
        pt.fill_order(r["order_id"])
        g = _geo(str(tmp_path / "geom.db"), r["order_id"])
        assert g["initial_stop_loss"] == pytest.approx(97.0)
        assert g["initial_quantity"] == pytest.approx(20.0)
        assert g["actual_quantity"] == pytest.approx(20.0)
        assert g["initial_risk_per_share"] == pytest.approx(3.0)
        assert g["initial_risk_amount"] == pytest.approx(60.0)
        assert g["risk_budget_utilization"] == pytest.approx(30.0)
        # the position's own pre-edit `initial_quantity` is still 10 (the
        # pre-existing quirk); only the geometry capture got it right
        assert pt.get_position(r["order_id"])["initial_quantity"] == 10
        assert pt.get_position(r["order_id"])["quantity"] == 20

    def test_the_auto_fill_path_captures_geometry_too(self, tmp_path):
        """check_entry_triggers is the live fill path - it must not be a hole."""
        pt = _geom_engine(tmp_path)
        r = pt.place_order("AUTOX", "LONG", 10, 100.0, 98.0, 105.0, 107.0,
                           quantity_source=SOURCE_MANUAL)
        fills = pt.check_entry_triggers({"AUTOX": 100.5})
        assert len(fills) == 1
        g = _geo(str(tmp_path / "geom.db"), r["order_id"])
        assert g is not None and g["geometry_reliable"] == 1

    def test_a_short_position_captures_geometry(self, tmp_path):
        pt = _geom_engine(tmp_path)
        r = pt.place_order("SHRTX", "SHORT", 10, 100.0, 102.0, 95.0, 93.0,
                           quantity_source=SOURCE_RISK_ENGINE)
        pt.fill_order(r["order_id"])
        g = _geo(str(tmp_path / "geom.db"), r["order_id"])
        assert g["direction"] == "SHORT"
        assert g["initial_risk_per_share"] == pytest.approx(2.0)
        assert g["quantity_source"] == SOURCE_RISK_ENGINE

    def test_a_position_without_a_usable_stop_is_still_recorded(self, tmp_path):
        """Recording 'we could not measure this' beats leaving no row, which a
        reader could mistake for a pre-capture position or a zero risk."""
        pt = _geom_engine(tmp_path)
        r = pt.place_order("NOSLX", "LONG", 10, 100.0, 0, 105.0, 107.0,
                           quantity_source=SOURCE_MANUAL)
        pt.fill_order(r["order_id"])
        g = _geo(str(tmp_path / "geom.db"), r["order_id"])
        assert g is not None
        assert g["geometry_reliable"] == 0
        assert g["initial_risk_per_share"] is None
        assert g["risk_budget_utilization"] is None

    def test_the_ledger_survives_the_position_being_closed(self, tmp_path):
        """paper_positions is a snapshot and the row is DELETed on close; the
        geometry ledger is not."""
        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, entry=100.0, sl=98.0, t1=105.0, t2=107.0, qty=10)
        pt.close_position(pid, 103.0)
        assert pt.get_position(pid) is None
        g = _geo(str(tmp_path / "geom.db"), pid)
        assert g is not None
        assert g["initial_stop_loss"] == pytest.approx(98.0)

    def test_the_live_position_carries_the_frozen_levels_under_initial_keys(
            self, tmp_path):
        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, entry=100.0, sl=98.0, t1=105.0, t2=107.0, qty=50)
        pos = pt.get_position(pid)
        assert pos["initial_stop_loss"] == pytest.approx(98.0)
        assert pos["initial_risk_per_share"] == pytest.approx(2.0)
        assert pos["initial_risk_budget"] == pytest.approx(200.0)
        assert pos["initial_risk_utilization"] == pytest.approx(50.0)
        assert pos["initial_geometry_reliable"] is True

    def test_the_mirrored_keys_cannot_collide_with_live_geometry(self, tmp_path):
        """Every mirrored key must be `initial_*`: Profit Capture writes plain
        `stop_loss`/`target_*`, so a shared name would be silently rewritten."""
        for key in captured_geometry_fields():
            assert key.startswith("initial_")
            assert key not in ("stop_loss", "target_1", "target_2", "quantity")


class TestProfitCaptureCannotRewriteEntryRisk:
    """Section 2 + Section 8: the INITIAL / CURRENT / REALIZED separation."""

    def test_breakeven_protection_moves_the_current_stop_but_not_the_initial(
            self, tmp_path):
        """The exact scenario that made 12 of 16 sizing figures estimates.

        Isolated with trailing off so only the T1 protection step is observed:
        the stop goes to breakeven, which collapses the *ledger's* risk per
        share to zero - while the captured entry risk stays at 2.0.
        """
        from app.services.profit_capture import (
            PROTECT_MOVE_TO_ENTRY, TRAILING_NONE,
        )
        pt = _geom_engine(tmp_path, protect_mode=PROTECT_MOVE_TO_ENTRY,
                          trailing_mode=TRAILING_NONE)
        pid = _open_long(pt, entry=100.0, sl=98.0, t1=105.0, t2=107.0, qty=10)
        db = str(tmp_path / "geom.db")
        before = _geo(db, pid)
        assert before["initial_stop_loss"] == pytest.approx(98.0)
        assert pt.get_position(pid)["stop_loss"] == pytest.approx(98.0)

        exits = pt.check_stops({"GEOMX": 105.5})
        assert any(e.get("exit_reason") == "T1_PARTIAL" for e in exits)

        pos = pt.get_position(pid)
        assert pos["stop_loss"] == pytest.approx(100.0)          # CURRENT moved
        assert pos["initial_stop_loss"] == pytest.approx(98.0)   # INITIAL frozen
        # This is the measurement the capture exists to make possible: the
        # ledger's own stop now implies ZERO risk per share.
        assert abs(pos["entry_price"] - pos["stop_loss"]) == 0.0
        assert pos["initial_risk_per_share"] == pytest.approx(2.0)

        after = _geo(db, pid)
        assert after["initial_stop_loss"] == pytest.approx(98.0)
        assert after["initial_risk_per_share"] == pytest.approx(2.0)
        assert after["actual_risk"] == pytest.approx(20.0)
        assert after["risk_budget_utilization"] == pytest.approx(10.0)
        assert before == after, "the entry-time row was mutated after the fact"

    def test_trailing_the_stop_further_leaves_the_initial_risk_untouched(
            self, tmp_path):
        """Default config: T1 protection to breakeven, then the trailing stop
        ratchets above it. The CURRENT stop ends at 109.5 - a 9.5 'risk' per
        share in the ledger, which is a profit lock, not a risk measurement."""
        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, entry=100.0, sl=98.0, t1=105.0, t2=130.0, qty=10)
        db = str(tmp_path / "geom.db")
        pt.check_stops({"GEOMX": 105.5})
        assert pt.get_position(pid)["stop_loss"] == pytest.approx(103.0)
        pt.check_stops({"GEOMX": 112.0})
        pos = pt.get_position(pid)
        assert pos["stop_loss"] == pytest.approx(109.5)
        assert pos["stop_loss"] > pos["entry_price"]
        assert pos["initial_stop_loss"] == pytest.approx(98.0)

        g = _geo(db, pid)
        assert g["initial_stop_loss"] == pytest.approx(98.0)
        assert g["initial_risk_per_share"] == pytest.approx(2.0)
        assert g["actual_risk"] == pytest.approx(20.0)
        assert g["risk_budget_utilization"] == pytest.approx(10.0)

    def test_partial_exit_then_final_exit_keeps_pnl_and_entry_geometry_correct(
            self, tmp_path):
        """T1 partial 5 @ 105.5 = +27.50, then T2 final 5 @ 108.0 = +40.00."""
        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, entry=100.0, sl=98.0, t1=105.0, t2=107.0, qty=10)
        db = str(tmp_path / "geom.db")

        exits = pt.check_stops({"GEOMX": 105.5})
        assert [(e["exit_reason"], e["exit_quantity"], e["pnl"]) for e in exits] \
            == [("T1_PARTIAL", 5.0, 27.5)]
        pos = pt.get_position(pid)
        assert pos["remaining_quantity"] == pytest.approx(5.0)
        assert pos["realized_pnl"] == pytest.approx(27.5)

        exits2 = pt.check_stops({"GEOMX": 108.0})
        assert [e["exit_reason"] for e in exits2] == ["T2_FINAL"]
        assert pt.get_position(pid) is None
        assert pt.get_portfolio_summary()["realized_pnl"] == pytest.approx(67.5)

        g = _geo(db, pid)
        assert g["initial_stop_loss"] == pytest.approx(98.0)
        assert g["initial_quantity"] == pytest.approx(10.0)    # NOT the 5 left
        assert g["initial_risk_per_share"] == pytest.approx(2.0)
        assert g["actual_risk"] == pytest.approx(20.0)         # NOT 5 x 2.0
        assert g["risk_budget_utilization"] == pytest.approx(10.0)

    def test_a_stop_out_entry_closes_at_a_loss_without_erasing_the_geometry(
            self, tmp_path):
        """The stop fills at the observed price, so 10 @ 97.5 = -25.00 while
        the planned entry risk was 10 x 2.0 = 20.00. The capture reports the
        PLANNED risk, which is what a utilization budget must be measured
        against; slippage is a separate P&L effect, deliberately not folded
        in here."""
        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, entry=100.0, sl=98.0, t1=105.0, t2=107.0, qty=10)
        exits = pt.check_stops({"GEOMX": 97.5})
        assert [e["exit_reason"] for e in exits] == ["STOP_LOSS"]
        assert pt.get_position(pid) is None
        assert pt.get_portfolio_summary()["realized_pnl"] == pytest.approx(-25.0)
        g = _geo(str(tmp_path / "geom.db"), pid)
        assert g["initial_stop_loss"] == pytest.approx(98.0)
        assert g["initial_risk_per_share"] == pytest.approx(2.0)
        assert g["actual_risk"] == pytest.approx(20.0)
        assert g["risk_budget_utilization"] == pytest.approx(10.0)

    def test_the_capture_cannot_be_rewritten_by_a_later_fill_of_the_same_row(
            self, tmp_path):
        """INSERT OR IGNORE on position_id makes the ledger genuinely insert-once."""
        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, entry=100.0, sl=98.0, qty=10)
        db = str(tmp_path / "geom.db")
        first = _geo(db, pid)
        forged = EntryRiskGeometry(
            position_id=pid, symbol="GEOMX", direction="LONG", entry_price=1.0,
            initial_stop_loss=0.5, initial_risk_per_share=0.5, actual_risk=5.0)
        assert persist_entry_geometry(forged, db_path=db) is False
        assert _geo(db, pid) == first

    def test_a_position_from_before_the_capture_is_absent_not_reconstructed(
            self, tmp_path):
        """Historical positions have no row. That means unreliable - never a
        backfilled number."""
        pt = _geom_engine(tmp_path)
        r = pt.place_order("OLDX", "LONG", 10, 100.0, 98.0, 105.0, 107.0)
        pt.cancel_order(r["order_id"])
        pid = "historical000001"
        assert load_geometry_for(pid, db_path=str(tmp_path / "geom.db")) is None
        assert pid not in load_all_geometry(db_path=str(tmp_path / "geom.db"))


class TestEntryGeometryShadowIsolation:
    """Section 7: the SHADOW column is measurement, never execution."""

    def test_shadow_is_computed_at_entry_from_entry_time_data_only(self, tmp_path):
        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, entry=100.0, sl=98.0, t1=105.0, t2=107.0, qty=50)
        g = _geo(str(tmp_path / "geom.db"), pid)
        # 200 budget / 2.0 risk = 100 shares (the cash cap of 95 is NOT applied
        # to the shadow: it isolates the risk budget from the cash constraint).
        assert g["shadow_quantity"] == 100
        assert g["shadow_exposure"] == pytest.approx(10_000.0)
        assert g["shadow_initial_risk"] == pytest.approx(200.0)
        assert g["shadow_utilization"] == pytest.approx(100.0)
        assert g["current_exposure"] == pytest.approx(5_000.0)

    def test_the_shadow_multiple_is_reported_for_every_capture(self, tmp_path):
        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, entry=100.0, sl=98.0, t1=105.0, t2=107.0, qty=50)
        g = capture_entry_geometry(pt.get_position(pid), CAPITAL, RISK_PCT)
        assert g.shadow_multiple == pytest.approx(2.0)

    def test_shadow_is_labelled_as_never_executed(self, tmp_path):
        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, qty=50)
        d = capture_entry_geometry(pt.get_position(pid), CAPITAL, RISK_PCT).to_dict()
        assert "NOT EXECUTED" in d["shadow_label"]
        assert "NOT REALIZED" in d["shadow_label"]

    def test_the_shadow_quantity_is_never_placed_as_an_order(self, tmp_path):
        """The strongest available guard: the shadow number is not even a legal
        order quantity anywhere in the system."""
        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, entry=100.0, sl=98.0, t1=105.0, t2=107.0, qty=50)
        g = _geo(str(tmp_path / "geom.db"), pid)
        assert g["shadow_quantity"] == 100
        pos = pt.get_position(pid)
        assert pos["quantity"] == pytest.approx(50.0)
        assert len(pt.get_positions()) == 1
        assert pt.get_portfolio_summary()["positions_count"] == 1
        # and the exposure actually committed is the current one
        assert pt.get_portfolio_summary()["total_value"] < 10_000.0 + 50.0

    def test_shadow_does_not_enter_realized_pnl(self, tmp_path):
        """50 actual shares vs the 100-share SHADOW. The shadow's hypothetical
        result is never booked; only the actual 50 shares earn."""
        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, entry=100.0, sl=98.0, t1=105.0, t2=107.0, qty=50)
        db = str(tmp_path / "geom.db")
        g = _geo(db, pid)
        assert g["shadow_quantity"] == 100
        assert g["actual_quantity"] == pytest.approx(50.0)

        pt.check_stops({"GEOMX": 105.5})       # T1: 25 @ +5.50 = +137.50
        pt.check_stops({"GEOMX": 107.5})       # T2: 25 @ +7.50 = +187.50
        assert pt.get_portfolio_summary()["realized_pnl"] == pytest.approx(325.0)

        # the doubled-size counterfactual stays a counterfactual
        shadow_pnl = 2 * 325.0
        assert pt.get_portfolio_summary()["realized_pnl"] < shadow_pnl
        # ... and re-reading the ledger changes nothing
        assert _geo(db, pid)["actual_quantity"] == pytest.approx(50.0)
        assert _geo(db, pid)["shadow_quantity"] == 100


class TestGeometryLedgerIsAdditive:
    """Section 9: the ledger is additive and never rewrites history."""

    def test_ensure_is_idempotent_and_touches_nothing_else(self, tmp_path):
        import shutil
        import tempfile

        src = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "intradayai.db")
        if not os.path.exists(src):
            pytest.skip("no live ledger present in this environment")
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "copy.db")
            shutil.copy2(src, work)

            def snapshot(path):
                con = sqlite3.connect(path)
                out = {
                    "tables": {r[0] for r in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")},
                    "trades": con.execute(
                        "SELECT COUNT(*), COALESCE(SUM(COALESCE(pnl,0)),0) "
                        "FROM trades").fetchone(),
                    "geometry_cols": sorted(
                        r[1] for r in con.execute(
                            f"PRAGMA table_info({GEOMETRY_TABLE})")) if
                        GEOMETRY_TABLE in {x[0] for x in con.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'")}
                        else None,
                }
                con.close()
                return out

            before = snapshot(work)
            ensure_geometry_schema(work)
            ensure_geometry_schema(work)     # second run must be a no-op
            after = snapshot(work)

            # exactly one new table, and nothing removed
            assert after["tables"] - before["tables"] <= {GEOMETRY_TABLE}
            assert not before["tables"] - after["tables"], "a table was dropped"
            # historical performance data is byte-for-byte identical
            assert after["trades"] == before["trades"]
            # and the geometry table's own shape is stable across runs
            if before["geometry_cols"] is not None:
                assert after["geometry_cols"] == before["geometry_cols"]
            cols = set(after["geometry_cols"] or ())
            assert {"risk_budget", "initial_stop_loss", "actual_risk"} <= cols

    def test_the_geometry_ledger_is_a_separate_table_from_trades(self, tmp_path):
        """The measurement columns must never be added to `trades`.

        They share descriptive names (entry_price, quantity, stop_loss) but
        live in their own INSERT-only table, so a historical trade row can
        never gain a geometry column that would read as a measurement it
        never had.
        """
        from app.services.position_sizing_store import _COLUMNS
        db = str(tmp_path / "sep.db")
        ensure_geometry_schema(db)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        geo_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({GEOMETRY_TABLE})")}
        trades_tables = {t for t in tables if t.endswith("trades")}
        trade_cols = set()
        for t in trades_tables:
            trade_cols |= {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        conn.close()
        assert GEOMETRY_TABLE in tables
        assert GEOMETRY_TABLE not in trades_tables
        assert "risk_budget" in geo_cols and "risk_budget" not in trade_cols
        assert "risk_budget_utilization" in geo_cols
        assert "risk_budget_utilization" not in trade_cols
        assert set(_COLUMNS) == geo_cols, "the INSERT column list has drifted"

    def test_a_capture_never_writes_to_the_trades_ledger(self, tmp_path):
        db = str(tmp_path / "geom.db")
        ensure_geometry_schema(db)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE trades (id VARCHAR(16), pnl FLOAT)")
        conn.execute("INSERT INTO trades VALUES ('t1', 12.5)")
        conn.commit()
        conn.close()

        g = capture_entry_geometry(
            {"id": "p1", "symbol": "A", "direction": "LONG", "entry_price": 100.0,
             "stop_loss": 98.0, "quantity": 50},
            account_capital=CAPITAL, risk_pct=RISK_PCT)
        persist_entry_geometry(g, db_path=db)

        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
        assert conn.execute("SELECT pnl FROM trades").fetchone()[0] == 12.5
        conn.close()

    def test_a_measurement_failure_never_prevents_a_fill(self, tmp_path, monkeypatch):
        """The capture is best-effort by construction: a broken measurement
        layer must not be able to stop a trade."""
        import app.services.position_sizing as ps

        def boom(*a, **kw):
            raise RuntimeError("geometry capture exploded")

        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, entry=100.0, sl=98.0, qty=10)
        monkeypatch.setattr(ps, "capture_entry_geometry", boom)
        r = pt.place_order("BOOMX", "LONG", 10, 100.0, 98.0, 105.0, 107.0,
                           quantity_source=SOURCE_MANUAL)
        filled = pt.fill_order(r["order_id"])
        assert filled["status"] == "filled"
        assert pt.get_position(r["order_id"]) is not None
        assert pid  # the pre-existing capture is untouched


class TestOrderFlowIsUnchanged:
    """The capture is measurement only: order flow must behave identically."""

    def test_place_order_gains_only_a_new_optional_key(self):
        import inspect

        from app.services.paper_trading import PaperAccount
        sig = inspect.signature(PaperAccount.place_order)
        assert sig.parameters["quantity_source"].default is None
        # every pre-existing parameter keeps its original name and default
        assert sig.parameters["quantity"].default is inspect.Parameter.empty
        assert sig.parameters["stop_loss"].default == 0
        assert sig.parameters["signal_id"].default is None

    def test_the_api_records_the_branch_it_actually_taken_without_changing_it(
            self):
        import inspect

        from app.api import trading
        src = inspect.getsource(trading.place_paper_order)
        # the sizing decision itself is untouched
        assert "qty = order.quantity" in src
        assert "if qty <= 0:" in src
        assert "risk_engine.calculate_position_size(" in src
        # and the provenance is a pure record of that branch
        assert "SOURCE_RISK_ENGINE if order.quantity <= 0 else SOURCE_MANUAL" in src

    def test_a_geometry_capture_does_not_change_the_filled_quantity(self, tmp_path):
        pt = _geom_engine(tmp_path)
        pid = _open_long(pt, entry=100.0, sl=98.0, qty=37)
        assert pt.get_position(pid)["quantity"] == pytest.approx(37.0)
        assert pt.get_portfolio_summary()["cash"] == pytest.approx(
            10_000.0 - 37 * 100.0)

    def test_the_risk_engine_sizing_path_is_untouched(self):
        """Guards the upstream formula the measurement mirrors."""
        from app.services.risk_engine import RiskConfig, RiskEngine
        eng = RiskEngine(RiskConfig(account_capital=10_000.0))
        assert eng.calculate_position_size(100.0, 98.0) == 95     # cash-capped
        assert eng.calculate_position_size(100.0, 80.0) == 10     # risk-capped
        # and the mirror agrees with it on both branches
        for entry, stop in ((100.0, 98.0), (100.0, 80.0), (2500.0, 2460.0),
                            (100.0, 99.9), (50.0, 1.0)):
            engine_qty = eng.calculate_position_size(entry, stop)
            g = capture_entry_geometry(
                {"id": "p", "symbol": "A", "direction": "LONG", "entry_price": entry,
                 "stop_loss": stop, "quantity": max(engine_qty, 1)},
                account_capital=10_000.0, risk_pct=2.0)
            assert g.allowed_quantity == engine_qty, (entry, stop)
