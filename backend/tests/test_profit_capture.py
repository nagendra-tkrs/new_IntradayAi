"""Profit Capture layer tests (§15).

LONG + SHORT scenarios for the controlled profit-capture flow:

    T1 partial exit (default 50%)            -> T1_PARTIAL
    T1 -> Target 2 final                     -> T2_FINAL
    T1 -> protective/trailing stop           -> TRAILING_STOP
    SL hit before T1 (full close)            -> STOP_LOSS
    T1 + T2 crossed in ONE price update      -> T1_PARTIAL + T2_FINAL (100%)
    manual close after partial               -> remaining qty + realized P&L
    legacy 100%-at-T1 configuration          -> TARGET_1 (backward compat)

Plus risk-scenario sizing (quality multipliers, REJECTED/WEAK no-trade, base/cap
rules, SL-distance sizing shrink) and persistence round-trips.

These tests verify the *routing* and *accounting contract* (quantities, realized
P&L, stops, ledger rows). No profitability claim is asserted anywhere.
"""
import json
import sqlite3
import pytest
from app.services.paper_trading import PaperTradingEngine, ensure_profit_capture_schema
from app.services.profit_capture import (
    EXIT_REASON_MANUAL_CLOSE,
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_T1_PARTIAL,
    EXIT_REASON_T2_FINAL,
    EXIT_REASON_TARGET_1,
    EXIT_REASON_TRAILING_STOP,
    STAGE_ACTIVE,
    STAGE_T1_EXECUTED,
    ProfitCaptureConfig,
)
from app.services.risk_engine import RiskEngine, RiskConfig


def make_engine(**cfg):
    return PaperTradingEngine(profit_config=ProfitCaptureConfig(**cfg))


def open_long(pt, entry=100.0, sl=98.0, t1=105.0, t2=107.0, qty=10, symbol="LONGX", atr=None):
    r = pt.place_order(symbol, "LONG", qty, entry, sl, t1, t2, atr=atr)
    assert r["status"] == "pending"
    f = pt.fill_order(r["order_id"])
    assert f["status"] == "filled"
    return r["order_id"]


def open_short(pt, entry=100.0, sl=102.0, t1=95.0, t2=93.0, qty=10, symbol="SHORTX", atr=None):
    r = pt.place_order(symbol, "SHORT", qty, entry, sl, t1, t2, atr=atr)
    assert r["status"] == "pending"
    f = pt.fill_order(r["order_id"])
    assert f["status"] == "filled"
    return r["order_id"]


# ── LONG scenarios ───────────────────────────────────────────────────────
def test_long_t1_partial_contract():
    """LONG reaches T1 → 50% partial exit, protection + trailing armed."""
    pt = make_engine()
    oid = open_long(pt)  # 10 @100, SL 98, T1 105, T2 107
    exits = pt.check_stops({"LONGX": 105.0})
    assert len(exits) == 1
    e = exits[0]
    assert e["exit_reason"] == EXIT_REASON_T1_PARTIAL
    assert e["partial"] is True
    assert e["exit_quantity"] == 5.0
    assert e["remaining"] == 5.0
    assert e["pnl"] == pytest.approx((105.0 - 100.0) * 5)
    assert e["trade"]["exit_price"] == 105.0
    assert e["trade"]["result"] == "WIN"
    assert e["trade"]["initial_quantity"] == 10.0
    assert e["trade"]["remaining_quantity"] == 5.0

    pos = pt.get_positions()[0]
    assert pos["quantity"] == 5.0
    assert pos["exit_stage"] == STAGE_T1_EXECUTED
    assert pos["realized_pnl"] == pytest.approx(25.0)
    assert pos["trailing_active"] is True
    # Default RANGE trailing (mult 0.5 x |T1-entry|): stop ratcheted on the
    # same observed price to 105 - 2.5 = 102.5 (protection already moved to 100).
    assert pos["stop_loss"] == pytest.approx(102.5)

    assert pt.get_portfolio_summary()["total_pnl"] == pytest.approx(25.0)

    # Remainder later closed → full round-trip realized.
    close = pt.close_position(pos["id"], 107.0)
    assert close["pnl"] == pytest.approx((107.0 - 100.0) * 5)
    assert pt.get_portfolio_summary()["total_pnl"] == pytest.approx(60.0)
    assert pt.get_positions() == []


def test_long_t1_to_t2_final():
    """LONG T1 partial then T2 final exit records both ledger rows."""
    pt = make_engine()
    oid = open_long(pt)
    exits1 = pt.check_stops({"LONGX": 105.0})
    assert len(exits1) == 1 and exits1[0]["exit_reason"] == EXIT_REASON_T1_PARTIAL

    exits2 = pt.check_stops({"LONGX": 107.0})
    assert len(exits2) == 1
    e = exits2[0]
    assert e["exit_reason"] == EXIT_REASON_T2_FINAL
    assert e["pnl"] == pytest.approx((107.0 - 100.0) * 5)
    assert pt.get_positions() == []
    assert pt.get_portfolio_summary()["total_pnl"] == pytest.approx(25.0 + 35.0)

    rows = pt.get_closed_trades()
    assert len(rows) == 2
    assert [r["exit_reason"] for r in rows] == [EXIT_REASON_T1_PARTIAL, EXIT_REASON_T2_FINAL]
    assert rows[0]["position_id"] == rows[1]["position_id"] == oid
    assert rows[0]["exit_quantity"] == 5.0 and rows[1]["exit_quantity"] == 5.0
    # The two rows together account for exactly the original position size.
    assert rows[0]["exit_quantity"] + rows[1]["exit_quantity"] == pytest.approx(10.0)


def test_long_t1_to_trailing_stop():
    """LONG T1 partial then trailing stop chases the remainder (never loosens)."""
    pt = make_engine()
    open_long(pt, t2=120.0)  # T2 far away so the trailing stop governs
    ex1 = pt.check_stops({"LONGX": 105.0})
    assert ex1[0]["exit_reason"] == EXIT_REASON_T1_PARTIAL

    # Uptick: trailing ratchets 105 -> 110 (stop 107.5), no exit.
    assert pt.check_stops({"LONGX": 110.0}) == []
    pos = pt.get_positions()[0]
    assert pos["stop_loss"] == pytest.approx(110.0 - 0.5 * 5.0)

    # Slight pullback: candidate 105.5 < 107.5 → stop never loosens.
    assert pt.check_stops({"LONGX": 108.0}) == []
    assert pt.get_positions()[0]["stop_loss"] == pytest.approx(107.5)

    # Drop through the trailing stop → TRAILING_STOP final exit.
    ex2 = pt.check_stops({"LONGX": 107.0})
    assert len(ex2) == 1
    assert ex2[0]["exit_reason"] == EXIT_REASON_TRAILING_STOP
    assert ex2[0]["pnl"] == pytest.approx((107.0 - 100.0) * 5)
    assert pt.get_positions() == []
    assert pt.get_portfolio_summary()["total_pnl"] == pytest.approx(25.0 + 35.0)


def test_long_sl_before_t1_full_close():
    """LONG price hits SL before T1 → full STOP_LOSS close, stage stays ACTIVE."""
    pt = make_engine()
    oid = open_long(pt)
    exits = pt.check_stops({"LONGX": 97.0})
    assert len(exits) == 1
    e = exits[0]
    assert e["exit_reason"] == EXIT_REASON_STOP_LOSS
    assert e["trade"]["result"] == "LOSS"
    assert e["pnl"] == pytest.approx((97.0 - 100.0) * 10)
    assert e["trade"]["exit_quantity"] == 10.0
    assert pt.get_positions() == []
    assert pt.get_portfolio_summary()["total_pnl"] == pytest.approx(-30.0)


def test_long_t1_t2_crossed_in_one_update():
    """T1 + T2 crossed in ONE price update → partial then final, exactly 100%."""
    pt = make_engine()
    oid = open_long(pt)
    exits = pt.check_stops({"LONGX": 108.0})
    assert len(exits) == 2
    assert [e["exit_reason"] for e in exits] == [EXIT_REASON_T1_PARTIAL, EXIT_REASON_T2_FINAL]
    assert exits[0]["pnl"] == pytest.approx((108.0 - 100.0) * 5)
    assert exits[1]["pnl"] == pytest.approx((108.0 - 100.0) * 5)
    total_exited = sum(e["trade"]["exit_quantity"] for e in exits)
    assert total_exited == pytest.approx(10.0)
    assert pt.get_positions() == []
    assert pt.get_portfolio_summary()["total_pnl"] == pytest.approx(80.0)
    rows = pt.get_closed_trades()
    assert len(rows) == 2
    # In-memory rows carry the position id; the API layer assigns each
    # persisted Trade row a unique id per exit (partials regenerate it).
    assert {r["position_id"] for r in rows} == {oid}
    assert rows[0]["exit_quantity"] + rows[1]["exit_quantity"] == pytest.approx(10.0)


def test_long_protect_move_to_entry_only():
    """MOVE_SL_TO_ENTRY protection (no trailing): stop = entry after T1."""
    pt = make_engine(trailing_mode="NONE")
    open_long(pt)
    exits = pt.check_stops({"LONGX": 105.0})
    assert exits[0]["exit_reason"] == EXIT_REASON_T1_PARTIAL
    pos = pt.get_positions()[0]
    assert pos["stop_loss"] == pytest.approx(100.0)  # exactly entry
    # A subsequent dip to 99 hits the protective stop → TRAILING_STOP.
    ex2 = pt.check_stops({"LONGX": 99.0})
    assert len(ex2) == 1
    assert ex2[0]["exit_reason"] == EXIT_REASON_TRAILING_STOP
    assert ex2[0]["pnl"] == pytest.approx((99.0 - 100.0) * 5)
    assert pt.get_positions() == []


def test_long_protect_plus_buffer():
    """PLUS_BUFFER protection adds buffer*ATR above entry for LONG."""
    pt = make_engine(protect_mode="MOVE_SL_TO_ENTRY_PLUS_BUFFER", protect_buffer_atr=0.5, trailing_mode="NONE")
    open_long(pt, atr=2.0)
    exits = pt.check_stops({"LONGX": 105.0})
    assert exits[0]["exit_reason"] == EXIT_REASON_T1_PARTIAL
    assert pt.get_positions()[0]["stop_loss"] == pytest.approx(101.0)  # 100 + 0.5*2


def test_long_keep_existing_sl():
    """KEEP_EXISTING_SL keeps the original stop; after T1 an SL hit is TRAILING_STOP."""
    pt = make_engine(protect_mode="KEEP_EXISTING_SL", trailing_mode="NONE")
    open_long(pt, sl=98.0)
    exits = pt.check_stops({"LONGX": 105.0})
    assert exits[0]["exit_reason"] == EXIT_REASON_T1_PARTIAL
    assert pt.get_positions()[0]["stop_loss"] == pytest.approx(98.0)
    ex2 = pt.check_stops({"LONGX": 98.0})
    assert len(ex2) == 1 and ex2[0]["exit_reason"] == EXIT_REASON_TRAILING_STOP


# ── SHORT scenarios (mirror of LONG) ────────────────────────────────────
def test_short_t1_partial_contract():
    """SHORT reaches T1 → 50% partial exit, protection moved to entry."""
    pt = make_engine(trailing_mode="NONE")
    oid = open_short(pt)  # 10 @100, SL 102, T1 95, T2 93
    exits = pt.check_stops({"SHORTX": 95.0})
    assert len(exits) == 1
    e = exits[0]
    assert e["exit_reason"] == EXIT_REASON_T1_PARTIAL
    assert e["pnl"] == pytest.approx((100.0 - 95.0) * 5)
    assert e["trade"]["result"] == "WIN"
    pos = pt.get_positions()[0]
    assert pos["quantity"] == 5.0
    assert pos["exit_stage"] == STAGE_T1_EXECUTED
    assert pos["realized_pnl"] == pytest.approx(25.0)
    assert pos["stop_loss"] == pytest.approx(100.0)  # moved to entry

    # T2 final exit on a later tick.
    ex2 = pt.check_stops({"SHORTX": 93.0})
    assert len(ex2) == 1
    assert ex2[0]["exit_reason"] == EXIT_REASON_T2_FINAL
    assert ex2[0]["pnl"] == pytest.approx((100.0 - 93.0) * 5)
    assert pt.get_positions() == []
    assert pt.get_portfolio_summary()["total_pnl"] == pytest.approx(60.0)


def test_short_t1_to_trailing_stop():
    """SHORT trailing stop ratchets DOWN only (candidate < old), never loosens."""
    pt = make_engine()
    open_short(pt, t2=80.0)
    ex1 = pt.check_stops({"SHORTX": 95.0})
    assert ex1[0]["exit_reason"] == EXIT_REASON_T1_PARTIAL
    # Down move to 90 → trailing stop = 90 + 2.5 = 92.5.
    assert pt.check_stops({"SHORTX": 90.0}) == []
    assert pt.get_positions()[0]["stop_loss"] == pytest.approx(92.5)
    # Pullback to 91: candidate 93.5 > 92.5 → unchanged (never loosens).
    assert pt.check_stops({"SHORTX": 91.0}) == []
    assert pt.get_positions()[0]["stop_loss"] == pytest.approx(92.5)
    # Up through the trailing stop → TRAILING_STOP.
    ex2 = pt.check_stops({"SHORTX": 93.0})
    assert len(ex2) == 1 and ex2[0]["exit_reason"] == EXIT_REASON_TRAILING_STOP
    assert ex2[0]["pnl"] == pytest.approx((100.0 - 93.0) * 5)
    assert pt.get_positions() == []


def test_short_sl_before_t1_full_close():
    """SHORT price hits SL before T1 → full STOP_LOSS close."""
    pt = make_engine()
    open_short(pt)
    exits = pt.check_stops({"SHORTX": 103.0})
    assert len(exits) == 1
    assert exits[0]["exit_reason"] == EXIT_REASON_STOP_LOSS
    assert exits[0]["trade"]["result"] == "LOSS"
    assert exits[0]["pnl"] == pytest.approx((100.0 - 103.0) * 10)
    assert pt.get_positions() == []


def test_short_t1_t2_crossed_in_one_update():
    """SHORT T1 + T2 crossed in one update → 2 exits, exactly 100%."""
    pt = make_engine()
    oid = open_short(pt)
    exits = pt.check_stops({"SHORTX": 92.0})
    assert len(exits) == 2
    assert [e["exit_reason"] for e in exits] == [EXIT_REASON_T1_PARTIAL, EXIT_REASON_T2_FINAL]
    assert exits[0]["pnl"] == pytest.approx((100.0 - 92.0) * 5)
    assert exits[1]["pnl"] == pytest.approx((100.0 - 92.0) * 5)
    assert sum(e["trade"]["exit_quantity"] for e in exits) == pytest.approx(10.0)
    assert pt.get_positions() == []
    assert len(pt.get_closed_trades()) == 2


# ── Manual close after partial ───────────────────────────────────────────
def test_manual_close_after_partial():
    """Manual partial close after T1 keeps the remainder and tracks realized P&L."""
    pt = make_engine()
    oid = open_long(pt)
    pt.check_stops({"LONGX": 105.0})  # T1 partial: 5 remaining, realized 25
    pos = pt.get_positions()[0]
    assert pos["quantity"] == 5.0

    # Manual partial close of 2 shares at 103.
    close = pt.close_position(pos["id"], 103.0, quantity=2)
    assert close["partial"] is True
    assert close["exit_quantity"] == 2.0
    assert close["remaining"] == 3.0
    assert close["pnl"] == pytest.approx((103.0 - 100.0) * 2)
    assert close["exit_reason"] == EXIT_REASON_MANUAL_CLOSE

    pos = pt.get_positions()[0]
    assert pos["quantity"] == 3.0
    assert pos["realized_pnl"] == pytest.approx(25.0 + 6.0)

    # Full close of the remaining 3 at 103.
    close2 = pt.close_position(pos["id"], 103.0)
    assert not close2.get("partial")
    assert close2["pnl"] == pytest.approx((103.0 - 100.0) * 3)
    assert pt.get_positions() == []
    assert pt.get_portfolio_summary()["total_pnl"] == pytest.approx(25.0 + 6.0 + 9.0)
    rows = pt.get_closed_trades()
    assert len(rows) == 3
    # Original position, shared across all three realized rows via position_id.
    assert {r["position_id"] for r in rows} == {oid}
    assert [r["exit_quantity"] for r in rows] == [5.0, 2.0, 3.0]
    assert [r["remaining_quantity"] for r in rows] == [5.0, 3.0, 0.0]


# ── Legacy full-close at T1 (backward compat) ───────────────────────────
def test_legacy_full_close_contract():
    pt = PaperTradingEngine(profit_config=ProfitCaptureConfig.legacy_full_close())
    oid = open_long(pt)
    exits = pt.check_stops({"LONGX": 105.0})
    assert len(exits) == 1
    e = exits[0]
    assert e["exit_reason"] == EXIT_REASON_TARGET_1
    assert e["pnl"] == pytest.approx((105.0 - 100.0) * 10)
    assert pt.get_positions() == []
    assert pt.get_portfolio_summary()["total_pnl"] == pytest.approx(50.0)


# ── Risk sizing scenarios ────────────────────────────────────────────────
def test_quality_rejected_weak_never_trade():
    re = RiskEngine()
    assert re.calculate_position_size(100.0, 98.0, setup_quality="REJECTED") == 0
    assert re.calculate_position_size(100.0, 98.0, setup_quality="WEAK") == 0
    assert re.effective_risk_per_trade_pct(setup_quality="REJECTED") == 0.0


def test_quality_defaults_match_legacy_sizing():
    re = RiskEngine()
    plain = re.calculate_position_size(100.0, 98.0)
    for q in ("NORMAL", "QUALIFIED", "PREMIUM"):
        assert re.calculate_position_size(100.0, 98.0, setup_quality=q) == plain
    # Unknown quality never blocks: defaults to 1.0.
    assert re.calculate_position_size(100.0, 98.0, setup_quality="MYSTERY_BAND") == plain
    assert re.calculate_position_size(100.0, 98.0, signal_strength="LONG") == plain


def test_risk_base_capped_by_max():
    re = RiskEngine(RiskConfig(max_risk_per_trade_pct=2.0, base_risk_per_trade_pct=3.0))
    assert re.effective_risk_per_trade_pct() == pytest.approx(2.0)  # capped down
    re2 = RiskEngine(RiskConfig(max_risk_per_trade_pct=4.0, base_risk_per_trade_pct=2.0))
    assert re2.effective_risk_per_trade_pct() == pytest.approx(2.0)


def test_strong_signal_multiplier_scales_size():
    re = RiskEngine(RiskConfig(max_risk_per_trade_pct=4.0, strong_signal_multiplier=1.5))
    eff = re.effective_risk_per_trade_pct(signal_strength="STRONG_LONG")
    assert eff == pytest.approx(3.0)  # 2.0 base x 1.5, under the 4.0 cap
    # Entry 100 / SL 96.5 → risk/share 3.5; caps (affordability, 95%) don't bind.
    base = re.calculate_position_size(100.0, 96.5)            # int(200/3.5) = 57
    strong = re.calculate_position_size(100.0, 96.5, signal_strength="STRONG_LONG")  # int(300/3.5) = 85
    assert base == 57
    assert strong == pytest.approx(int(base * 1.5))  # 85


def test_sl_distance_shrinks_size_risk_capped():
    """Wider SL → smaller quantity; the per-trade risk amount stays constant."""
    re = RiskEngine()  # 2.0% risk on default capital
    qty_tight = re.calculate_position_size(200.0, 190.0)  # risk/share 10
    qty_wide = re.calculate_position_size(200.0, 150.0)   # risk/share 50
    assert qty_tight == 20
    assert qty_wide == 4
    # Account risk stays 2% regardless of stop distance.
    assert qty_tight * 10.0 == qty_wide * 50.0


def test_risk_engine_positional_compat():
    """Existing two-arg call convention still works after the extension."""
    re = RiskEngine()
    assert re.calculate_position_size(100.0, 98.0) > 0


# ── Persistence / schema ─────────────────────────────────────────────────
def test_ensure_profit_capture_schema_idempotent(tmp_path):
    db = str(tmp_path / "pc.db")
    ensure_profit_capture_schema(db)
    ensure_profit_capture_schema(db)  # second run must be a no-op
    con = sqlite3.connect(db)
    try:
        for table in ("trades", "paper_positions", "paper_pending_orders"):
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
            assert cols, f"{table} exists"
        trades_cols = {r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()}
        assert "details_json" in trades_cols
        pos_cols = {r[1] for r in con.execute("PRAGMA table_info(paper_positions)").fetchall()}
        pend_cols = {r[1] for r in con.execute("PRAGMA table_info(paper_pending_orders)").fetchall()}
        assert "profit_meta" in pos_cols and "profit_meta" in pend_cols
    finally:
        con.close()


def test_persist_roundtrip_after_t1_partial(tmp_path):
    db = str(tmp_path / "pcpt.db")
    ensure_profit_capture_schema(db)
    pt = PaperTradingEngine(db_path=db, persist=True)
    r = pt.place_order("LONGX", "LONG", 10, 100.0, 98.0, 105.0, 107.0, user_id="u", atr=2.0)
    pt.fill_order(r["order_id"], user_id="u")
    exits = pt.check_stops({"LONGX": 105.0}, user_id="u")
    assert exits[0]["exit_reason"] == EXIT_REASON_T1_PARTIAL

    # In production the API finalizer writes the partial exit to the trades
    # ledger (the engine itself only persists open positions / pending orders).
    # Model that durable row so the restart-reconciled total_pnl includes it.
    t = exits[0]["trade"]
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO trades (id, user_id, symbol, direction, entry_price, exit_price,"
        " quantity, stop_loss, target_1, target_2, entry_time, exit_time, status, pnl)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (t["id"], "u", t["symbol"], t["direction"], t["entry_price"], t["exit_price"],
         t["quantity"], t["stop_loss"], t["target_1"], t["target_2"],
         t["opened_at"], t["exit_time"], t["status"], t["pnl"]),
    )
    con.commit()
    con.close()

    # Fresh engine over the same durable rows must restore the partial state.
    fresh = PaperTradingEngine(db_path=db, persist=True)
    acc = fresh._ensure_account("u")
    restored = list(acc.positions.values())
    assert len(restored) == 1
    p = restored[0]
    assert p["quantity"] == pytest.approx(5.0)
    assert p["exit_stage"] == STAGE_T1_EXECUTED
    assert p["t1_exit_price"] == pytest.approx(105.0)
    assert p["t1_exit_quantity"] == pytest.approx(5.0)
    assert p["realized_pnl"] == pytest.approx(25.0)
    assert p["trailing_active"] is True
    assert p["atr_ref"] == pytest.approx(2.0)
    # The restored account's realized total matches the partial's P&L.
    assert acc.total_pnl == pytest.approx(25.0)


def test_price_marks_do_not_persist_when_clean(tmp_path):
    """Write reduction: pure price marks never mark the account dirty."""
    db = str(tmp_path / "pcw.db")
    ensure_profit_capture_schema(db)
    pt = PaperTradingEngine(db_path=db, persist=True)
    open_long(pt)
    acc = pt.accounts.get(None)
    acc._dirty = False
    acc.update_prices({"LONGX": 102.0})
    assert acc._dirty is False  # marks only
    acc.check_stops({"LONGX": 102.0})  # no exit, trailing untouched at ACTIVE
    assert acc._dirty is False