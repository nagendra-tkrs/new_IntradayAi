"""Tests for the AI-linked paper-trading performance monitor.

The monitor is measurement-only over the serialized ``/api/paper/trades`` dict
contract. These tests build that contract directly (no DB) and assert:

* only AI-linked rows are measured; manual/unlinked rows are separated out and
  never touch performance or Profit-Capture math;
* Profit Capture multi-row positions (T1 partial + T2 final sharing a
  ``position_id``/``signal_id``) count as ONE trade with summed P&L and the
  correct legacy hypothetical (full original quantity at T1);
* the required persisted fields are verified per row;
* the sample-size rule suppresses conclusions below 30 trades.
"""

from app.services.ai_performance_monitor import (
    INSUFFICIENT_DATA_SENTENCE,
    MIN_TRADES_FOR_CONCLUSION,
    _score_band,
    build_ai_positions,
    compute_monitoring,
    render_markdown,
    separate_ai_linked,
    verify_ai_trade_fields,
)


def _ai_trade(**overrides):
    """A complete AI-linked realized row (the enriched API dict shape)."""
    base = {
        "id": "t1",
        "signal_id": "sig1",
        "ai_available": True,
        "symbol": "TCS",
        "direction": "LONG",
        "entry_price": 100.0,
        "exit_price": 110.0,
        "quantity": 10,
        "stop_loss": 98.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "entry_time": "2026-09-25T09:20:00",
        "exit_time": "2026-09-25T10:00:00",
        "pnl": 50.0,
        "status": "closed",
        "position_id": None,
        "exit_reason": "T2_FINAL",
        "exit_quantity": 5,
        "initial_quantity": 10,
        "remaining_quantity": 0,
        "actual_entry": 100.0,
        "realized_pnl": 50.0,
        "ai_direction": "LONG",
        "ai_score": 80.0,
        "signal_quality": "HIGH",
        "ai_trend_score": 20.0,
        "ai_momentum_score": 15.0,
        "ai_volume_score": 10.0,
        "ai_vwap_score": 8.0,
        "ai_price_action_score": 12.0,
        "ai_market_context_score": 10.0,
        "ai_risk_quality_score": 5.0,
        "original_entry": 100.0,
        "original_stop_loss": 98.0,
        "original_target_1": 110.0,
        "original_target_2": 120.0,
        "original_risk_reward": 2.5,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# cohort separation
# ---------------------------------------------------------------------------

def test_manual_and_unresolved_rows_are_separated():
    trades = [
        _ai_trade(id="a", signal_id="sig1", ai_available=True),
        _ai_trade(id="b", signal_id=None, ai_available=False),
        # signal_id present but the signal row did not resolve
        _ai_trade(id="c", signal_id="missing", ai_available=False),
    ]
    ai, other = separate_ai_linked(trades)
    assert [t["id"] for t in ai] == ["a"]
    assert [t["id"] for t in other] == ["b", "c"]


def test_manual_rows_never_enter_metrics():
    manual = _ai_trade(
        id="m1", signal_id=None, ai_available=False, pnl=9999.0,
        exit_reason="TARGET_1",
    )
    report = compute_monitoring([manual], initial_capital=10_000)
    assert report["cohort"]["ai_linked_positions"] == 0
    assert report["cohort"]["manual_or_unlinked_positions"] == 1
    assert report["performance"]["total_pnl"] == 0.0
    assert report["legacy_comparison"]["legacy_hypothetical_pnl"] == 0.0


# ---------------------------------------------------------------------------
# field verification
# ---------------------------------------------------------------------------

def test_verify_fields_flags_missing_values():
    good = _ai_trade(id="good")
    bad = _ai_trade(id="bad", ai_score=None, original_target_2=None)
    result = verify_ai_trade_fields([good, bad])
    assert result["trades_checked"] == 2
    assert result["complete_count"] == 1
    assert result["incomplete_count"] == 1
    assert "ai_score" in result["missing_field_counts"]
    assert "original_target_2" in result["missing_field_counts"]
    bad_row = next(r for r in result["per_trade"] if r["trade_id"] == "bad")
    assert bad_row["status"] == "INCOMPLETE"


def test_zero_component_scores_are_not_treated_as_missing():
    ok = _ai_trade(id="z", ai_trend_score=0.0, ai_momentum_score=0.0)
    result = verify_ai_trade_fields([ok])
    assert result["complete_count"] == 1
    assert result["incomplete_count"] == 0


# ---------------------------------------------------------------------------
# Profit Capture lifecycle grouping + legacy comparison
# ---------------------------------------------------------------------------

def _pc_lifecycle_rows():
    """One LONG PC position (T1 partial + T2 final) + one SHORT stop-out."""
    r1 = _ai_trade(
        id="r1", position_id="p1", signal_id="sig1",
        exit_reason="T1_PARTIAL", exit_price=110.0, pnl=50.0,
        exit_quantity=5, remaining_quantity=5,
        exit_time="2026-09-25T10:00:00",
        ai_score=82.0, signal_quality="HIGH", original_risk_reward=2.5,
    )
    r2 = _ai_trade(
        id="r2", position_id="p1", signal_id="sig1",
        exit_reason="T2_FINAL", exit_price=120.0, pnl=100.0,
        exit_quantity=5, remaining_quantity=0,
        exit_time="2026-09-25T11:00:00",
        ai_score=82.0, signal_quality="HIGH", original_risk_reward=2.5,
    )
    short = _ai_trade(
        id="r3", position_id="p2", signal_id="sig2",
        direction="SHORT", ai_direction="SHORT",
        entry_price=200.0, stop_loss=205.0, target_1=190.0, target_2=180.0,
        exit_price=205.0, exit_reason="STOP_LOSS", pnl=-50.0,
        quantity=10, initial_quantity=10, exit_quantity=10,
        remaining_quantity=0, exit_time="2026-09-25T09:30:00",
        ai_score=60.0, signal_quality="WEAK", original_risk_reward=1.5,
    )
    return [r1, r2, short]


def test_pc_rows_collapse_to_one_trade_with_summed_pnl():
    positions = build_ai_positions(_pc_lifecycle_rows())
    assert len(positions) == 2
    p1 = next(p for p in positions if p["position_id"] == "p1")
    assert p1["rows"] == 2
    assert p1["pnl"] == 150.0
    assert p1["total_quantity"] == 10
    assert p1["had_partial"] is True
    assert p1["t1_hit"] is True


def test_legacy_hypothetical_is_full_quantity_at_t1():
    report = compute_monitoring(_pc_lifecycle_rows(), initial_capital=10_000)
    legacy = report["legacy_comparison"]
    # LONG: legacy = (110 - 100) * 10 = 100; SHORT stop-out legacy == actual -50.
    assert legacy["legacy_hypothetical_pnl"] == 50.0
    assert legacy["actual_profit_capture_pnl"] == 100.0
    assert legacy["additional_pnl"] == 50.0
    assert legacy["additional_pnl_pct"] == 100.0


def test_metrics_match_hand_computed_values():
    report = compute_monitoring(_pc_lifecycle_rows(), initial_capital=10_000)
    perf = report["performance"]
    assert report["cohort"]["ai_linked_positions"] == 2
    assert report["cohort"]["ai_linked_rows"] == 3
    assert perf["total_pnl"] == 100.0
    assert perf["win_rate"] == 50.0
    assert perf["profit_factor"] == 3.0
    assert perf["average_profit"] == 150.0
    assert perf["average_loss"] == -50.0
    assert perf["max_drawdown"] == 50.0
    assert perf["average_rr"] == 2.0
    # Return % is against notional paper capital.
    assert perf["return_pct"] == 1.0

    ex = report["exit_stats"]
    assert ex["t1_hit"]["count"] == 1 and ex["t1_hit"]["rate"] == 50.0
    assert ex["t1_partial"]["count"] == 1
    assert ex["t2_final"]["count"] == 1
    assert ex["trailing_stop"]["count"] == 0
    assert ex["stop_loss"]["count"] == 1

    assert report["long"]["total_pnl"] == 150.0
    assert report["short"]["total_pnl"] == -50.0
    assert report["by_signal_quality"]["HIGH"]["total_pnl"] == 150.0
    assert report["by_signal_quality"]["WEAK"]["total_pnl"] == -50.0
    assert report["by_score_range"]["75-84"]["total_pnl"] == 150.0
    assert report["by_score_range"]["50-64"]["total_pnl"] == -50.0


def test_trailing_stop_classified_as_t1_evidence():
    row = _ai_trade(
        id="tr", position_id="pt", exit_reason="TRAILING_STOP",
        exit_price=108.0, pnl=40.0, remaining_quantity=0,
    )
    report = compute_monitoring([row])
    assert report["exit_stats"]["trailing_stop"]["count"] == 1
    assert report["exit_stats"]["t1_hit"]["count"] == 1


def test_historical_unlinked_trade_excluded_from_legacy_comparison():
    """A pre-feature row (NULL signal_id) with a T1 exit must not affect the
    AI-linked legacy/PC math."""
    ai = _ai_trade(
        id="ai", position_id="pa", signal_id="sigA",
        exit_reason="T2_FINAL", exit_price=120.0, pnl=100.0,
        remaining_quantity=0,
    )
    historical = _ai_trade(
        id="h1", position_id="ph", signal_id=None, ai_available=False,
        exit_reason="TARGET_1", exit_price=110.0, pnl=500.0,
        remaining_quantity=0,
    )
    report = compute_monitoring([ai, historical])
    assert report["cohort"]["ai_linked_positions"] == 1
    legacy = report["legacy_comparison"]
    assert legacy["actual_profit_capture_pnl"] == 100.0
    # Only the AI-linked position's legacy result is included.
    assert legacy["legacy_hypothetical_pnl"] == 100.0


# ---------------------------------------------------------------------------
# sample-size gate
# ---------------------------------------------------------------------------

def _n_ai_positions(n):
    return [
        _ai_trade(
            id=f"t{i}", position_id=f"p{i}", signal_id=f"sig{i}",
            exit_reason="STOP_LOSS", exit_price=98.0, pnl=-20.0,
            remaining_quantity=0, exit_time=f"2026-09-25T10:{i:02d}:00",
        )
        for i in range(n)
    ]


def test_below_threshold_emits_insufficient_sentence():
    report = compute_monitoring(_n_ai_positions(MIN_TRADES_FOR_CONCLUSION - 1))
    assert report["sufficient_for_conclusion"] is False
    assert report["conclusion"] == INSUFFICIENT_DATA_SENTENCE


def test_at_threshold_conclusion_allowed():
    report = compute_monitoring(_n_ai_positions(MIN_TRADES_FOR_CONCLUSION))
    assert report["sufficient_for_conclusion"] is True
    assert report["conclusion"] is None
    assert report["performance"]["total_trades"] == MIN_TRADES_FOR_CONCLUSION


# ---------------------------------------------------------------------------
# score bands + rendering
# ---------------------------------------------------------------------------

def test_score_band_boundaries():
    assert _score_band(49.9) == "<50"
    assert _score_band(50) == "50-64"
    assert _score_band(64.9) == "50-64"
    assert _score_band(65) == "65-74"
    assert _score_band(74.9) == "65-74"
    assert _score_band(75) == "75-84"
    assert _score_band(85) == ">=85"
    assert _score_band(None) is None


def test_render_markdown_includes_all_deliverables():
    report = compute_monitoring(_pc_lifecycle_rows())
    md = render_markdown(report)
    for heading in (
        "## 1. Current number of real AI-linked trades",
        "## 2. Total P&L",
        "## 3. Return %",
        "## 4. Win rate",
        "## 5. Profit factor",
        "## 6. Maximum drawdown",
        "## 7. Legacy hypothetical P&L",
        "## 8. Actual Profit Capture P&L",
        "## 9. Additional P&L",
        "## 10. Additional P&L %",
        "## 11. AI score vs P&L",
        "## 12. T1 / T2 / trailing / SL statistics",
        "## 13. LONG vs SHORT performance",
        "## 14. Signal-quality performance",
        "## 15. Data limitations",
    ):
        assert heading in md
    assert INSUFFICIENT_DATA_SENTENCE in md


def test_render_markdown_reports_sufficient_sample():
    report = compute_monitoring(_n_ai_positions(MIN_TRADES_FOR_CONCLUSION))
    md = render_markdown(report)
    assert "Sample size is sufficient" in md
