"""Legacy vs Profit Capture — read-only performance comparison tests.

The comparison answers one question from the recorded paper ledger:

    How does the implemented Profit Capture exit flow
    (Entry → T1 partial → protection → T2/trailing → final exit)
    compare with the previous 100%-at-T1 exit flow (Entry → T1 → full exit)
    using the same symbol / entry / quantity / T1 per trade?

Everything here exercises the pure comparison module over serialized ledger
rows (the ``/api/paper/trades`` shape). It never writes to a ledger, never
places orders, and never recomputes recorded P&L — the authoritative ``pnl``
per row is reused. Hypothetical legacy P&L is derived ONLY from the recorded
target_1 / initial_quantity (Entry → T1 → 100% exit), or equals the actual
outcome when T1 was never reached (identical stop-loss / manual path under
both models).

Deterministic example locked in by the spec (LONG and SHORT):

    Entry ₹100, qty 10, T1 ₹105, T2 ₹107
    Legacy       10 × (105 − 100) = ₹50
    Profit Cap.  5 × (105 − 100) = ₹25  +  5 × (107 − 100) = ₹35  → ₹60
    Difference = ₹10

These are deterministic calculation tests, NOT proof of real-world
profitability. ``data_source`` stays ``"paper_trading"`` and the UI shows
"Insufficient data for performance comparison." when ``has_data`` is false.
"""
import pytest

from app.services.performance_comparison import (
    build_trade_groups,
    compare_legacy_vs_profit_capture,
)
from app.services.paper_trading import PaperTradingEngine


def _trade(**overrides):
    """A serialized ledger row (the ``_trade_rows_to_dicts`` shape) with
    realistic profit-capture defaults, overridable per test."""
    base = {
        "id": "t1",
        "position_id": "pos1",
        "symbol": "RELIANCE",
        "direction": "LONG",
        "entry_price": 100.0,
        "exit_price": 105.0,
        "quantity": 5,
        "stop_loss": 95.0,
        "target_1": 105.0,
        "target_2": 107.0,
        "entry_time": "2026-09-25 09:35:00",
        "exit_time": "2026-09-25 10:05:00",
        "pnl": 25.0,
        "result": "WIN",
        "status": "closed",
        "exit_reason": "T1_PARTIAL",
        "exit_quantity": 5,
        "initial_quantity": 10,
        "remaining_quantity": 5,
        "t1_exit_price": 105.0,
        "t1_exit_quantity": 5,
        "t1_realized_pnl": 25.0,
    }
    base.update(overrides)
    return base


def _final_row(**overrides):
    """A full-close row for the same position (T2/trailing/SL/manual/target)."""
    base = _trade(
        id="t2",
        exit_reason="T2_FINAL",
        exit_price=107.0,
        exit_quantity=5,
        initial_quantity=10,
        remaining_quantity=0,
        pnl=35.0,
        exit_time="2026-09-25 10:30:00",
    )
    base.update(overrides)
    return base


# ── Spec-mandated deterministic example (LONG + SHORT) ───────────────────
def test_deterministic_long_example():
    """LONG: entry 100 / qty 10 / T1 105 / T2 107 → Legacy ₹50, PC ₹60, Δ ₹10."""
    trades = [
        _trade(),
        _final_row(),
    ]
    r = compare_legacy_vs_profit_capture(trades)

    assert r["data_source"] == "paper_trading"
    assert r["has_data"] is True
    assert r["models"]["legacy"] == "Entry → T1 → 100% Exit"
    assert "T1 Partial" in r["models"]["profit_capture"]

    pc = r["profit_capture"]["all"]
    legacy = r["legacy"]["all"]
    assert legacy["total_trades"] == 1
    assert pc["total_trades"] == 1
    assert legacy["winning_trades"] == 1 and legacy["losing_trades"] == 0
    assert legacy["total_pnl"] == 50.0
    assert pc["total_pnl"] == 60.0
    assert legacy["average_pnl"] == 50.0
    assert pc["average_pnl"] == 60.0
    assert r["difference"]["all"]["pnl"] == 10.0
    assert r["difference"]["all"]["average_pnl"] == 10.0

    em = r["exit_metrics"]
    assert em["total_trades"] == 1
    assert em["t1_hit_count"] == 1
    assert em["t1_partial_count"] == 1
    assert em["t1_partial_percent"] == 100.0
    assert em["t2_final_count"] == 1
    assert em["better_than_legacy_count"] == 1
    assert em["worse_than_legacy_count"] == 0
    assert em["equal_to_legacy_count"] == 0


def test_deterministic_short_example():
    """SHORT mirror: entry 100 / qty 10 / T1 95 / T2 93 → Legacy ₹50, PC ₹60."""
    trades = [
        _trade(
            id="s1", position_id="spos1", direction="SHORT",
            stop_loss=102.0, target_1=95.0, target_2=93.0,
            exit_price=95.0, exit_quantity=5, remaining_quantity=5,
            pnl=25.0,  # (100 − 95) × 5
            exit_time="2026-09-25 10:05:00",
        ),
        _final_row(
            id="s2", position_id="spos1", direction="SHORT",
            stop_loss=102.0, target_1=95.0, target_2=93.0,
            exit_reason="T2_FINAL", exit_price=93.0, exit_quantity=5,
            remaining_quantity=0, pnl=35.0,  # (100 − 93) × 5
            exit_time="2026-09-25 10:30:00",
        ),
    ]
    r = compare_legacy_vs_profit_capture(trades)
    assert r["legacy"]["all"]["total_pnl"] == 50.0
    assert r["profit_capture"]["all"]["total_pnl"] == 60.0
    assert r["difference"]["all"]["pnl"] == 10.0


# ── Exit-flow scenarios (LONG and SHORT) ─────────────────────────────────
def test_long_trailing_stop():
    """LONG T1 partial → trailing stop final."""
    trades = [
        _trade(),
        _final_row(exit_reason="TRAILING_STOP", exit_price=107.0, pnl=35.0),
    ]
    r = compare_legacy_vs_profit_capture(trades)
    assert r["profit_capture"]["all"]["total_pnl"] == 60.0
    assert r["legacy"]["all"]["total_pnl"] == 50.0
    assert r["exit_metrics"]["trailing_stop_count"] == 1


def test_short_trailing_stop():
    """SHORT T1 partial → trailing stop final."""
    trades = [
        _trade(id="s1", position_id="spos1", direction="SHORT", stop_loss=102.0,
               target_1=95.0, target_2=93.0, exit_price=95.0, exit_quantity=5,
               remaining_quantity=5, pnl=25.0, exit_time="2026-09-25 10:05:00"),
        _final_row(id="s2", position_id="spos1", direction="SHORT", stop_loss=102.0,
                   target_1=95.0, target_2=93.0, exit_reason="TRAILING_STOP",
                   exit_price=94.5, exit_quantity=5, remaining_quantity=0,
                   pnl=27.5,  # (100 − 94.5) × 5
                   exit_time="2026-09-25 10:30:00"),
    ]
    r = compare_legacy_vs_profit_capture(trades)
    assert r["profit_capture"]["all"]["total_pnl"] == pytest.approx(52.5)
    assert r["legacy"]["all"]["total_pnl"] == 50.0
    assert r["exit_metrics"]["trailing_stop_count"] == 1


def test_stop_loss_before_t1_long():
    """LONG stopped out before T1 → both models record the same stop-loss."""
    trades = [
        _trade(exit_reason="STOP_LOSS", exit_price=97.0, exit_quantity=10,
               initial_quantity=10, remaining_quantity=0, pnl=-30.0,
               status="closed"),
    ]
    r = compare_legacy_vs_profit_capture(trades)
    assert r["legacy"]["all"]["total_pnl"] == -30.0
    assert r["profit_capture"]["all"]["total_pnl"] == -30.0
    assert r["difference"]["all"]["pnl"] == 0.0
    assert r["exit_metrics"]["t1_hit_count"] == 0
    assert r["exit_metrics"]["stop_loss_count"] == 1
    assert r["exit_metrics"]["equal_to_legacy_count"] == 1


def test_stop_loss_before_t1_short():
    """SHORT stopped out before T1 → both models record the same loss."""
    trades = [
        _trade(id="s1", position_id="spos1", direction="SHORT", stop_loss=103.0,
               target_1=97.0, target_2=95.0, exit_reason="STOP_LOSS",
               exit_price=103.0, exit_quantity=10, initial_quantity=10,
               remaining_quantity=0, pnl=-30.0,  # (100 − 103) × 10
               status="closed"),
    ]
    r = compare_legacy_vs_profit_capture(trades)
    assert r["legacy"]["all"]["total_pnl"] == -30.0
    assert r["profit_capture"]["all"]["total_pnl"] == -30.0
    assert r["exit_metrics"]["stop_loss_count"] == 1


# ── Edge cases ───────────────────────────────────────────────────────────
def test_same_tick_t1_t2_crossed():
    """Price jumps beyond T1 AND T2 in one update → T1 partial + T2 final,
    exactly 100% closed, no duplicate exit, no remaining quantity."""
    trades = [
        _trade(exit_price=108.0, exit_quantity=5, remaining_quantity=5,
               pnl=40.0,  # (108 − 100) × 5
               exit_time="2026-09-25 10:05:00.000001"),
        _final_row(exit_price=108.0, exit_quantity=5, initial_quantity=10,
                   remaining_quantity=0, pnl=40.0,  # (108 − 100) × 5
                   exit_time="2026-09-25 10:05:00.000002"),
    ]
    groups = build_trade_groups(trades)
    assert len(groups) == 1
    g = groups[0]
    assert g["total_quantity"] == 10
    assert g["total_exit_qty"] == 10  # every share accounted for exactly once
    assert g["pc_pnl"] == 80.0
    assert g["legacy_pnl"] == 50.0
    final = max(trades, key=lambda t: t["exit_time"])
    assert final["remaining_quantity"] == 0

    r = compare_legacy_vs_profit_capture(trades)
    assert r["profit_capture"]["all"]["total_pnl"] == 80.0
    assert r["legacy"]["all"]["total_pnl"] == 50.0
    assert r["exit_metrics"]["t2_final_count"] == 1
    assert r["exit_metrics"]["t1_partial_count"] == 1


def test_manual_close_after_t1_partial():
    """T1 partial then manual close of the remainder."""
    trades = [
        _trade(),
        _final_row(exit_reason="MANUAL_CLOSE", exit_price=107.0, pnl=35.0),
    ]
    r = compare_legacy_vs_profit_capture(trades)
    assert r["profit_capture"]["all"]["total_pnl"] == 60.0
    assert r["legacy"]["all"]["total_pnl"] == 50.0
    assert r["exit_metrics"]["manual_close_count"] == 1
    assert r["exit_metrics"]["t1_hit_count"] == 1


def test_zero_remaining_quantity_and_no_duplicates():
    """A fully closed position has zero remaining quantity and its rows merge
    into exactly one comparison trade (duplicate-exit prevention)."""
    trades = [
        _trade(),
        _final_row(),
        _trade(id="other", position_id="pos2", symbol="TCS", direction="LONG",
               entry_price=200.0, exit_price=202.0, target_1=205.0,
               stop_loss=195.0, exit_reason="STOP_LOSS", exit_quantity=10,
               initial_quantity=10, remaining_quantity=0, pnl=-30.0,
               exit_time="2026-09-25 11:00:00"),
    ]
    groups = build_trade_groups(trades)
    assert len(groups) == 2  # two positions → two trades, never three rows
    pos1 = next(g for g in groups if g["position_id"] == "pos1")
    assert pos1["total_exit_qty"] == pos1["total_quantity"] == 10
    assert pos1["pc_pnl"] == 60.0


def test_multiple_partial_records_one_position():
    """Grouping is row-count agnostic: several rows of one position merge into
    a single comparison trade summing the authoritative P&L."""
    trades = [
        _trade(),  # T1 partial 5 @105 → +25
        _final_row(id="t3", position_id="pos1", exit_reason="MANUAL_CLOSE",
                   exit_price=103.0, exit_quantity=3, remaining_quantity=0,
                   pnl=9.0,  # (103 − 100) × 3
                   exit_time="2026-09-25 10:32:00"),
        # A same-position intermediate manual row is merged too (defensive).
        _final_row(id="t2", position_id="pos1", exit_reason="MANUAL_CLOSE",
                   exit_price=103.0, exit_quantity=2, remaining_quantity=0,
                   pnl=6.0,  # (103 − 100) × 2
                   exit_time="2026-09-25 10:31:00"),
    ]
    r = compare_legacy_vs_profit_capture(trades)
    assert r["profit_capture"]["all"]["total_trades"] == 1
    assert r["profit_capture"]["all"]["total_pnl"] == 40.0  # 25 + 6 + 9
    assert r["legacy"]["all"]["total_pnl"] == 50.0  # 10 × (105 − 100)
    assert r["difference"]["all"]["pnl"] == -10.0
    assert r["exit_metrics"]["worse_than_legacy_count"] == 1


def test_missing_t2_manual_close_lower():
    """No T2 hit; remainder closed below T1 exit → PC result still uses
    recorded P&L; legacy uses the T1 full-exit hypothetical."""
    trades = [
        _trade(),
        _final_row(exit_reason="MANUAL_CLOSE", exit_price=102.0, pnl=10.0),
    ]
    r = compare_legacy_vs_profit_capture(trades)
    assert r["profit_capture"]["all"]["total_pnl"] == 35.0  # 25 + 10
    assert r["legacy"]["all"]["total_pnl"] == 50.0
    assert r["difference"]["all"]["pnl"] == -15.0
    assert r["exit_metrics"]["better_than_legacy_count"] == 0


def test_manual_close_before_t1_equality():
    """Manually closed before T1 → the same manual path existed under legacy,
    so both models record the identical outcome (equal count)."""
    trades = [
        _trade(exit_reason="MANUAL_CLOSE", exit_price=103.0, exit_quantity=10,
               initial_quantity=10, remaining_quantity=0, pnl=30.0,
               status="closed"),
    ]
    r = compare_legacy_vs_profit_capture(trades)
    assert r["legacy"]["all"]["total_pnl"] == 30.0
    assert r["profit_capture"]["all"]["total_pnl"] == 30.0
    assert r["exit_metrics"]["manual_close_count"] == 1
    assert r["exit_metrics"]["equal_to_legacy_count"] == 1
    assert r["exit_metrics"]["t1_hit_count"] == 0


def test_open_positions_never_counted():
    """An OPEN position row (status != closed) is excluded — realized
    reporting only (same rule as /api/paper/trades)."""
    trades = [
        _trade(status="open", pnl=15.0),
        _trade(id="open2", status="OPEN", pnl=20.0),
    ]
    r = compare_legacy_vs_profit_capture(trades)
    assert r["has_data"] is False
    assert r["legacy"]["all"]["total_trades"] == 0
    assert r["exit_metrics"]["total_trades"] == 0


def test_missing_completed_data_insufficient():
    """Empty ledger → has_data false, zero stats, but response shape intact."""
    r = compare_legacy_vs_profit_capture([])
    assert r["has_data"] is False
    assert r["data_source"] == "paper_trading"
    assert r["period"] == {"start": None, "end": None}
    for split in ("all", "long", "short"):
        assert r["legacy"][split]["total_trades"] == 0
        assert r["profit_capture"][split]["total_pnl"] == 0.0
        assert r["difference"][split]["pnl"] == 0.0
    assert r["exit_metrics"]["total_trades"] == 0


def test_legacy_row_t1_crossing_inference():
    """Pre-profit-capture rows (no position_id, no exit_reason): when the
    realized exit crossed T1 they map to the T1 full-exit hypothetical;
    otherwise the recorded outcome is used (same conditions)."""
    crossed = _trade(
        id="leg1", position_id=None, exit_reason=None, exit_price=105.0,
        exit_quantity=10, initial_quantity=10, remaining_quantity=0,
        pnl=50.0, status="closed",
    )
    not_crossed = _trade(
        id="leg2", position_id=None, exit_reason=None, exit_price=102.0,
        exit_quantity=10, initial_quantity=10, remaining_quantity=0,
        pnl=20.0, exit_time="2026-09-25 11:00:00", status="closed",
    )
    r = compare_legacy_vs_profit_capture([crossed, not_crossed])
    assert r["legacy"]["all"]["total_pnl"] == 70.0  # 50 + 20
    assert r["profit_capture"]["all"]["total_pnl"] == 70.0  # recorded
    assert r["exit_metrics"]["t1_hit_count"] == 1
    assert r["exit_metrics"]["equal_to_legacy_count"] == 2


def test_date_range_filtering():
    """start_date/end_date (YYYY-MM-DD IST) filter by the realization date."""
    sep24 = _trade(id="a", position_id="pa", exit_time="2026-09-24 10:05:00")
    sep25 = _trade(id="b", position_id="pb", exit_time="2026-09-25 10:05:00")
    r = compare_legacy_vs_profit_capture([sep24, sep25])
    assert r["profit_capture"]["all"]["total_trades"] == 2

    r24 = compare_legacy_vs_profit_capture(
        [sep24, sep25], start_date="2026-09-24", end_date="2026-09-24")
    assert r24["profit_capture"]["all"]["total_trades"] == 1
    assert r24["period"] == {"start": "2026-09-24", "end": "2026-09-24"}

    r_exclusive = compare_legacy_vs_profit_capture(
        [sep24, sep25], start_date="2026-09-26")
    assert r_exclusive["has_data"] is False

    r_malformed = compare_legacy_vs_profit_capture([sep24, sep25],
                                                   start_date="not-a-date")
    assert r_malformed["profit_capture"]["all"]["total_trades"] == 2


def test_row_without_exit_time_excluded():
    """A row without a usable exit timestamp never lands on the date axis."""
    r = compare_legacy_vs_profit_capture([_trade(exit_time=None)])
    assert r["has_data"] is False


# ── Metrics: win rate / profit factor / drawdown / consecutive losses ────
def test_metrics_winrate_pf_drawdown_consecutive_losses():
    """Deterministic metric values over a fixed sequence of trade P&Ls.

    Sequence (chronological): -100, -50, +50, +200, -120, +30
      wins 3 / losses 3 / win rate 50.0%
      total +10 → average 1.67
      gross profit 280 / gross loss 270 → profit factor 1.04
      equity: 0 → -100 → -150 → -100 → +100 → -20 → +10
      peak-to-trough max = 150.0 (peak 0 at start, trough -150)
      consecutive losses max = 2 (-100, -50)
    """
    seq = [
        # (exit_price, pnl) — single STOP_LOSS rows whose T1 was never reached,
        # so both models record the same P&L and the metrics mirror exactly.
        (90.0, -100.0),
        (95.0, -50.0),
        (105.0, 50.0),
        (120.0, 200.0),
        (88.0, -120.0),
        (103.0, 30.0),
    ]
    trades = []
    for i, (exit_price, pnl) in enumerate(seq):
        trades.append(_trade(
            id=f"m{i}", position_id=f"mpos{i}", exit_reason="STOP_LOSS",
            exit_price=exit_price, exit_quantity=10, initial_quantity=10,
            remaining_quantity=0, pnl=pnl, status="closed",
            exit_time=f"2026-09-25 10:{i:02d}:00",
        ))

    r = compare_legacy_vs_profit_capture(trades)
    for key in ("legacy", "profit_capture"):
        s = r[key]["all"]
        assert s["total_trades"] == 6
        assert s["winning_trades"] == 3
        assert s["losing_trades"] == 3
        assert s["win_rate"] == 50.0
        assert s["total_pnl"] == pytest.approx(10.0)
        assert s["average_pnl"] == pytest.approx(1.67)
        assert s["average_win"] == pytest.approx(93.33)   # 280 / 3
        assert s["average_loss"] == pytest.approx(90.0)   # 270 / 3
        assert s["total_loss"] == pytest.approx(270.0)
        assert s["largest_win"] == pytest.approx(200.0)
        assert s["largest_loss"] == pytest.approx(-120.0)
        assert s["profit_factor"] == pytest.approx(1.04)
        assert s["max_drawdown"] == pytest.approx(150.0)
        assert s["max_consecutive_losses"] == 2
    assert r["difference"]["all"]["pnl"] == 0.0


def test_profit_factor_null_without_losing_trades():
    """No losing trades → profit_factor null (frontend renders '—')."""
    r = compare_legacy_vs_profit_capture([
        _trade(),
        _final_row(exit_reason="T2_FINAL", exit_price=107.0, pnl=35.0),
    ])
    assert r["legacy"]["all"]["winning_trades"] == 1
    assert r["legacy"]["all"]["losing_trades"] == 0
    assert r["legacy"]["all"]["profit_factor"] is None
    assert r["profit_capture"]["all"]["profit_factor"] is None
    assert r["difference"]["all"]["profit_factor"] is None


# ── Direction separation ─────────────────────────────────────────────────
def test_long_and_short_calculated_separately():
    """LONG and SHORT are aggregated independently; combined figures don't
    leak across directions (spec section 6)."""
    long_trades = [
        _trade(id="l1", position_id="lpos", exit_price=105.0, exit_quantity=5,
               remaining_quantity=5, pnl=25.0,
               exit_time="2026-09-25 10:05:00"),
        _final_row(id="l2", position_id="lpos", exit_price=107.0,
                   exit_quantity=5, remaining_quantity=0, pnl=35.0,
                   exit_time="2026-09-25 10:30:00"),
    ]
    short_trades = [
        _trade(id="s1", position_id="spos", direction="SHORT", stop_loss=102.0,
               target_1=95.0, target_2=93.0, exit_price=95.0, exit_quantity=5,
               remaining_quantity=5, pnl=25.0,
               exit_time="2026-09-25 11:00:00"),
        _final_row(id="s2", position_id="spos", direction="SHORT",
                   stop_loss=102.0, target_1=95.0, target_2=93.0,
                   exit_reason="T2_FINAL", exit_price=93.0, exit_quantity=5,
                   remaining_quantity=0, pnl=35.0,
                   exit_time="2026-09-25 11:30:00"),
    ]
    r = compare_legacy_vs_profit_capture(long_trades + short_trades)

    assert r["profit_capture"]["all"]["total_trades"] == 2
    assert r["profit_capture"]["all"]["total_pnl"] == 120.0
    assert r["legacy"]["all"]["total_pnl"] == 100.0

    assert r["profit_capture"]["long"]["total_trades"] == 1
    assert r["profit_capture"]["long"]["total_pnl"] == 60.0
    assert r["legacy"]["long"]["total_pnl"] == 50.0

    assert r["profit_capture"]["short"]["total_trades"] == 1
    assert r["profit_capture"]["short"]["total_pnl"] == 60.0
    assert r["legacy"]["short"]["total_pnl"] == 50.0

    # Direction-specific sums exactly compose the combined figures.
    assert r["profit_capture"]["long"]["total_pnl"] + \
        r["profit_capture"]["short"]["total_pnl"] == \
        r["profit_capture"]["all"]["total_pnl"]


# ── Engine integration: real Profit Capture execution → comparison ───────
def test_engine_executed_flow_maps_to_comparison():
    """A REAL engine-run LONG (entry 100, qty 10, T1 105, T2 107) produces the
    two ledger rows (T1 partial + T2 final) which must compare as ₹60 vs ₹50."""
    pt = PaperTradingEngine()  # default = Profit Capture behavior (50% T1)
    placed = pt.place_order("LONGX", "LONG", 10, 100.0, 98.0, 105.0, 107.0)
    pt.fill_order(placed["order_id"])
    e1 = pt.check_stops({"LONGX": 105.0})
    assert e1[0]["exit_reason"] == "T1_PARTIAL"
    assert e1[0]["pnl"] == pytest.approx(25.0)
    e2 = pt.check_stops({"LONGX": 107.0})
    assert e2[0]["exit_reason"] == "T2_FINAL"
    assert e2[0]["pnl"] == pytest.approx(35.0)

    rows = [_to_row(e1[0]["trade"]), _to_row(e2[0]["trade"])]
    r = compare_legacy_vs_profit_capture(rows)
    assert r["profit_capture"]["all"]["total_trades"] == 1
    assert r["profit_capture"]["all"]["total_pnl"] == pytest.approx(60.0)
    assert r["legacy"]["all"]["total_pnl"] == pytest.approx(50.0)
    assert r["difference"]["all"]["pnl"] == pytest.approx(10.0)
    assert r["exit_metrics"]["t1_partial_count"] == 1
    assert r["exit_metrics"]["t2_final_count"] == 1


def _to_row(t):
    """Map an engine in-memory closed trade to the serialized ledger shape the
    comparison consumes (mirrors the fields persisted via _paper_details)."""
    return {
        "id": t.get("id"),
        "position_id": t.get("position_id"),
        "symbol": t.get("symbol"),
        "direction": t.get("direction"),
        "entry_price": t.get("entry_price"),
        "exit_price": t.get("exit_price"),
        "quantity": t.get("quantity"),
        "initial_quantity": t.get("initial_quantity"),
        "exit_quantity": t.get("exit_quantity"),
        "remaining_quantity": t.get("remaining_quantity"),
        "stop_loss": t.get("stop_loss"),
        "target_1": t.get("target_1"),
        "target_2": t.get("target_2"),
        "entry_time": str(t.get("opened_at")),
        "exit_time": t.get("exit_time"),
        "pnl": t.get("pnl", 0.0),
        "result": t.get("result"),
        "status": "closed",
        "exit_reason": t.get("exit_reason"),
        "t1_exit_price": t.get("t1_exit_price"),
        "t1_exit_quantity": t.get("t1_exit_quantity"),
        "t1_realized_pnl": t.get("t1_realized_pnl"),
    }