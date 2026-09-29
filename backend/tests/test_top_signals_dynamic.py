"""Dynamic Top 3 (combined LONG/SHORT) tests for ``select_top_signals``.

The Top 3 are recomputed from the CURRENT scan's eligible signals only:
* combined LONG + SHORT pool (no direction separation, no direction balance),
* at most 3 (never padded with WEAK / REJECTED / NOT_ELIGIBLE rows),
* deterministic, and fully replaced by the freshest scan's own ranking.
"""

import copy

from app.services.signal_quality import (
    CONFIRMATION_TOTAL,
    QUALITY_PREMIUM,
    QUALITY_QUALIFIED,
    QUALITY_WEAK,
    select_top_signals,
)


def _row(symbol, quality_level, score, confirms, confidence, rr, signal="LONG", eligible=True):
    return {
        "symbol": symbol,
        "signal": signal,
        "confidence": confidence,
        "setup_quality": quality_level,
        "setup_quality_score": score,
        "confirmation_count": confirms,
        "confirmation_total": CONFIRMATION_TOTAL,
        "top_signal_eligible": eligible,
        "risk_reward_ratio": rr,
        "top_signal_rank": None,
    }


def _scan_a():
    """Quality scores: A=80, B=75, C=70, D=60."""
    return [
        _row("D", QUALITY_QUALIFIED, 60, 6, 60.0, 1.6),
        _row("A", QUALITY_QUALIFIED, 80, 7, 72.0, 2.0),
        _row("C", QUALITY_QUALIFIED, 70, 6, 70.0, 1.8),
        _row("B", QUALITY_QUALIFIED, 75, 6, 71.0, 1.7),
    ]


def _scan_b():
    """Quality scores: A=55, B=50, C=45, D=85."""
    return [
        _row("D", QUALITY_QUALIFIED, 85, 8, 74.0, 2.4),
        _row("A", QUALITY_QUALIFIED, 55, 5, 68.0, 1.5),
        _row("C", QUALITY_QUALIFIED, 45, 4, 65.0, 1.2),
        _row("B", QUALITY_QUALIFIED, 50, 4, 66.0, 1.1),
    ]


def _symbols(results):
    return [r["symbol"] for r in results]


# ────────────────────────────────────────────────────────────────────────────
# Test 1 — Top 3 changes with scores
# ────────────────────────────────────────────────────────────────────────────

def test_top_3_changes_with_scores():
    assert _symbols(select_top_signals(_scan_a())) == ["A", "B", "C"]
    assert _symbols(select_top_signals(_scan_b())) == ["D", "A", "B"]


# ────────────────────────────────────────────────────────────────────────────
# Test 2 — Direction does not affect ranking (no LONG/SHORT balancing)
# ────────────────────────────────────────────────────────────────────────────

def test_direction_does_not_affect_ranking():
    results = [
        _row("A", QUALITY_QUALIFIED, 70, 6, 70.0, 2.0, signal="LONG"),
        _row("B", QUALITY_QUALIFIED, 68, 6, 66.0, 1.9, signal="SHORT"),
        _row("C", QUALITY_QUALIFIED, 66, 5, 64.0, 1.7, signal="SHORT"),
        _row("D", QUALITY_QUALIFIED, 65, 5, 63.0, 1.6, signal="LONG"),
    ]
    assert _symbols(select_top_signals(results)) == ["A", "B", "C"]


# ────────────────────────────────────────────────────────────────────────────
# Tests 3 & 4 — all one direction is fine; no forced balance
# ────────────────────────────────────────────────────────────────────────────

def test_all_short_returns_three_shorts():
    results = [
        _row("A", QUALITY_QUALIFIED, 70, 6, 70.0, 2.0, signal="SHORT"),
        _row("B", QUALITY_QUALIFIED, 68, 6, 66.0, 1.9, signal="SHORT"),
        _row("C", QUALITY_QUALIFIED, 66, 5, 64.0, 1.7, signal="SHORT"),
        _row("D", QUALITY_QUALIFIED, 60, 4, 60.0, 1.5, signal="SHORT"),
    ]
    top = select_top_signals(results)
    assert _symbols(top) == ["A", "B", "C"]
    assert all(r["signal"] == "SHORT" for r in top)


def test_all_long_returns_three_longs():
    results = [
        _row("A", QUALITY_QUALIFIED, 70, 6, 70.0, 2.0, signal="LONG"),
        _row("B", QUALITY_QUALIFIED, 68, 6, 66.0, 1.9, signal="LONG"),
        _row("C", QUALITY_QUALIFIED, 66, 5, 64.0, 1.7, signal="LONG"),
        _row("D", QUALITY_QUALIFIED, 60, 4, 60.0, 1.5, signal="LONG"),
    ]
    top = select_top_signals(results)
    assert _symbols(top) == ["A", "B", "C"]
    assert all(r["signal"] == "LONG" for r in top)


# ────────────────────────────────────────────────────────────────────────────
# Test 5 — Only two eligible: return 2, never promote a weak third
# ────────────────────────────────────────────────────────────────────────────

def test_only_two_eligible_returns_two_not_three():
    results = [
        _row("A", QUALITY_QUALIFIED, 70, 6, 70.0, 2.0),
        _row("B", QUALITY_QUALIFIED, 68, 6, 66.0, 1.9),
        # Higher confidence but NOT eligible — must never be promoted.
        _row("WEAK_HIGH_CONF", QUALITY_WEAK, 30, 2, 92.0, 0.8, eligible=False),
    ]
    assert _symbols(select_top_signals(results)) == ["A", "B"]


# ────────────────────────────────────────────────────────────────────────────
# Test 6 — No eligible signals → []
# ────────────────────────────────────────────────────────────────────────────

def test_no_eligible_signals_returns_empty():
    results = [
        _row("A", QUALITY_WEAK, 30, 2, 61.0, 0.8, eligible=False),
        _row("B", QUALITY_WEAK, 25, 1, 55.0, 0.6, eligible=False),
        _row("C", QUALITY_WEAK, 20, 1, 50.0, 0.5, eligible=False),
    ]
    assert select_top_signals(results) == []


# ────────────────────────────────────────────────────────────────────────────
# Test 7 — A fresh scan fully replaces the previous Top 3 (no retention)
# ────────────────────────────────────────────────────────────────────────────

def test_fresh_scan_replaces_previous_top_3():
    first = select_top_signals(_scan_a())
    assert _symbols(first) == ["A", "B", "C"]
    # Selection is stateless: the next scan's dataset defines the next Top 3.
    second = select_top_signals(_scan_b())
    assert _symbols(second) == ["D", "A", "B"]
    assert _symbols(second) != _symbols(first)


# ────────────────────────────────────────────────────────────────────────────
# Test 8 — Deterministic ranking: identical input → identical Top 3 + order
# ────────────────────────────────────────────────────────────────────────────

def test_deterministic_ranking_identical_input():
    results = [
        _row("MIX3", QUALITY_QUALIFIED, 66, 5, 64.0, 1.7, signal="SHORT"),
        _row("MIX1", QUALITY_PREMIUM, 80, 7, 72.0, 2.2, signal="LONG"),
        _row("MIX2", QUALITY_QUALIFIED, 75, 6, 70.0, 2.0, signal="SHORT"),
        _row("MIX4", QUALITY_QUALIFIED, 61, 4, 62.0, 1.5, signal="LONG"),
    ]
    first = _symbols(select_top_signals(copy.deepcopy(results)))
    second = _symbols(select_top_signals(copy.deepcopy(results)))
    assert first == second
    assert first == ["MIX1", "MIX2", "MIX3"]