"""Paper-trading cash & margin accounting regression tests.

Covers the authoritative account-summary model (contract sections 2/6/7.2/7.4):
  total_value     == initial_capital + realized_pnl + unrealized_pnl
  reserved_margin  = committed capital of EVERY open position (LONG disbursed
                     notional AND SHORT 100% cash collateral) + pending orders
  available_cash   = initial_capital + realized_pnl − reserved_margin
                     (true buying power; SHORT sale proceeds never inflate it,
                     and Available Cash never goes negative)
  pending create -> reserve ; pending fill   -> transfer (never double-count)
  pending cancel -> release ; close position -> release + realize P&L
  realized P&L replayed from the persistent SQLite ledger at account creation
  open positions + pending orders persist across restarts (persist=True engine)
so /api/portfolio, /api/paper/trades and /api/paper/performance agree.
"""
import os
import sqlite3
import tempfile

import pytest

from app.core.config import settings
from app.services.paper_trading import PaperTradingEngine, load_trades_seed

INITIAL = settings.INITIAL_CAPITAL


def _equity_invariant(summary):
    return summary["total_value"] == pytest.approx(
        INITIAL + summary["realized_pnl"] + summary["unrealized_pnl"]
    )


# ── A. Pending LONG reserves its entry notional (no cash/equity change) ─────
def test_a_pending_long_reserves_margin():
    pt = PaperTradingEngine(db_path=None)
    r = pt.place_order("LX", "LONG", 100, 100.0, 95.0, 110.0)
    s = pt.get_portfolio_summary(user_id=None)
    assert r["status"] == "pending"
    assert s["cash"] == pytest.approx(INITIAL)
    assert s["total_value"] == pytest.approx(INITIAL)
    assert s["pending_value"] == pytest.approx(10_000.0)
    assert s["reserved_margin"] == pytest.approx(10_000.0)
    assert s["available_cash"] == pytest.approx(0.0)
    assert _equity_invariant(s)


# ── B. Pending SHORT reserves its entry notional ─────────────────────────────
def test_b_pending_short_reserves_margin():
    pt = PaperTradingEngine(db_path=None)
    r = pt.place_order("SX", "SHORT", 50, 100.0, 105.0, 90.0)
    s = pt.get_portfolio_summary(user_id=None)
    assert r["status"] == "pending"
    assert s["reserved_margin"] == pytest.approx(5_000.0)
    assert s["available_cash"] == pytest.approx(INITIAL - 5_000.0)
    assert _equity_invariant(s)


# ── C. Filling a LONG transfers its reservation (no double count) ───────────
def test_c_fill_long_transfers_reservation():
    pt = PaperTradingEngine(db_path=None)
    r = pt.place_order("LX", "LONG", 100, 100.0, 95.0, 110.0)
    before = pt.get_portfolio_summary(user_id=None)
    # Available cash excludes the pending reservation, so it is unchanged when
    # the same commitment is paid out of cash on fill (reservation magnitude
    # carries over: pending -> committed open capital, never double-counted).
    fill = pt.fill_order(r["order_id"])
    assert "error" not in fill
    after = pt.get_portfolio_summary(user_id=None)
    assert pt.cash == pytest.approx(INITIAL - 10_000.0)
    assert after["reserved_margin"] == pytest.approx(10_000.0)
    assert after["available_cash"] == pytest.approx(before["available_cash"])
    assert after["total_value"] == pytest.approx(before["total_value"])
    assert after["positions_count"] == 1
    assert _equity_invariant(after)


# ── D. Filling a SHORT transfers its reservation to collateral ──────────────
def test_d_fill_short_transfers_reservation():
    pt = PaperTradingEngine(db_path=None)
    r = pt.place_order("SX", "SHORT", 50, 100.0, 105.0, 90.0)
    before = pt.get_portfolio_summary(user_id=None)
    fill = pt.fill_order(r["order_id"])
    assert "error" not in fill
    after = pt.get_portfolio_summary(user_id=None)
    # Proceeds credited, same notional now locked as short collateral.
    assert pt.cash == pytest.approx(INITIAL + 5_000.0)
    assert after["reserved_margin"] == pytest.approx(5_000.0)
    # Reservation magnitude carried over - not doubled.
    assert after["reserved_margin"] == pytest.approx(before["reserved_margin"])
    assert after["total_value"] == pytest.approx(INITIAL)
    assert _equity_invariant(after)


# ── E. Cancelling a LONG releases its reservation ───────────────────────────
def test_e_cancel_long_releases_reservation():
    pt = PaperTradingEngine(db_path=None)
    r = pt.place_order("LX", "LONG", 100, 100.0, 95.0, 110.0)
    assert pt.get_portfolio_summary(user_id=None)["reserved_margin"] == pytest.approx(10_000.0)
    pt.cancel_order(r["order_id"])
    s = pt.get_portfolio_summary(user_id=None)
    assert s["reserved_margin"] == 0.0
    assert s["available_cash"] == pytest.approx(INITIAL)
    assert s["pending_orders_count"] == 0
    assert _equity_invariant(s)


# ── F. Cancelling a SHORT releases its reservation ──────────────────────────
def test_f_cancel_short_releases_reservation():
    pt = PaperTradingEngine(db_path=None)
    r = pt.place_order("SX", "SHORT", 50, 100.0, 105.0, 90.0)
    assert pt.get_portfolio_summary(user_id=None)["reserved_margin"] == pytest.approx(5_000.0)
    pt.cancel_order(r["order_id"])
    s = pt.get_portfolio_summary(user_id=None)
    assert s["reserved_margin"] == 0.0
    assert s["available_cash"] == pytest.approx(INITIAL)
    assert _equity_invariant(s)


# ── G. Closing a SHORT releases collateral and realizes P&L ────────────────
def test_g_close_short_releases_margin_and_realizes():
    pt = PaperTradingEngine(db_path=None)
    r = pt.place_order("SX", "SHORT", 50, 100.0, 105.0, 90.0)
    pt.fill_order(r["order_id"])
    pt.update_prices({"SX": 90.0})
    s_open = pt.get_portfolio_summary(user_id=None)
    assert s_open["unrealized_pnl"] == pytest.approx(500.0)
    assert s_open["reserved_margin"] == pytest.approx(5_000.0)

    close = pt.close_position(r["order_id"], 90.0)
    assert close["pnl"] == pytest.approx(500.0)
    s = pt.get_portfolio_summary(user_id=None)
    assert pt.cash == pytest.approx(INITIAL + 500.0)
    assert s["reserved_margin"] == 0.0  # collateral released
    assert s["realized_pnl"] == pytest.approx(500.0)
    assert s["unrealized_pnl"] == 0.0
    assert _equity_invariant(s)


# ── H. Closing a LONG realizes P&L and keeps equity invariant ──────────────
def test_h_close_long_realizes():
    pt = PaperTradingEngine(db_path=None)
    r = pt.place_order("LX", "LONG", 100, 100.0, 95.0, 110.0)
    pt.fill_order(r["order_id"])
    pt.update_prices({"LX": 105.0})
    assert pt.get_portfolio_summary(user_id=None)["reserved_margin"] == pytest.approx(10_000.0)
    close = pt.close_position(r["order_id"], 105.0)
    assert close["pnl"] == pytest.approx(500.0)
    s = pt.get_portfolio_summary(user_id=None)
    assert pt.cash == pytest.approx(INITIAL + 500.0)
    assert s["realized_pnl"] == pytest.approx(500.0)
    assert _equity_invariant(s)


# ── I. Placement gate + aggregate reservations; fill respects commitments ─────
def test_i_pending_aggregation_and_fill_gate():
    pt = PaperTradingEngine(db_path=None)
    # Pending A (7k) reserves buying power; B (2.5k) still fits (9.5k ≤ 10k);
    # a further C (2k) would push available negative and is REJECTED at
    # placement (contract: Available Cash never goes negative).
    ra = pt.place_order("A", "LONG", 700, 10.0, 9.0, 12.0)  # 7k
    rb = pt.place_order("B", "LONG", 250, 10.0, 9.0, 12.0)  # 2.5k
    s = pt.get_portfolio_summary(user_id=None)
    assert s["pending_value"] == pytest.approx(9_500.0)
    assert s["reserved_margin"] == pytest.approx(9_500.0)
    assert s["available_cash"] == pytest.approx(500.0)

    # Placement gate: C (2k) > available (500) -> rejected, nothing changed.
    cash_before = pt.cash
    rc = pt.place_order("C", "LONG", 200, 10.0, 9.0, 12.0)
    assert "error" in rc
    assert rc["error"].startswith("Insufficient available cash")
    assert pt.cash == cash_before
    assert s["pending_value"] == pytest.approx(9_500.0)  # unchanged
    assert {o["id"] for o in pt.get_pending_orders()} == {ra["order_id"], rb["order_id"]}

    # Edit gate: growing B to 4k (needs 4k > 500 available) is rejected too.
    redit = pt.edit_order(rb["order_id"], quantity=400)
    assert "error" in redit
    assert {o["id"] for o in pt.get_pending_orders()} == {ra["order_id"], rb["order_id"]}

    # Releasing B frees 2.5k; A now fills for its own 7k.
    pt.cancel_order(rb["order_id"])
    fill = pt.fill_order(ra["order_id"])
    assert "error" not in fill
    assert pt.cash == pytest.approx(INITIAL - 7_000.0)
    s2 = pt.get_portfolio_summary(user_id=None)
    assert s2["reserved_margin"] == pytest.approx(7_000.0)  # open LONG capital
    assert s2["positions_count"] == 1
    assert _equity_invariant(s2)


# ── J. Equity invariant across full round-trip with pending orders ─────────
def test_j_invariant_full_roundtrip_with_pending():
    pt = PaperTradingEngine(db_path=None)
    pl = pt.place_order("LX", "LONG", 50, 100.0, 95.0, 110.0)
    ps = pt.place_order("SX", "SHORT", 50, 100.0, 105.0, 90.0)
    assert _equity_invariant(pt.get_portfolio_summary(user_id=None))
    pt.fill_order(pl["order_id"])
    assert _equity_invariant(pt.get_portfolio_summary(user_id=None))
    pt.fill_order(ps["order_id"])
    assert _equity_invariant(pt.get_portfolio_summary(user_id=None))
    pt.update_prices({"LX": 110.0, "SX": 90.0})
    assert _equity_invariant(pt.get_portfolio_summary(user_id=None))
    pt.close_position(pl["order_id"], 110.0)
    assert _equity_invariant(pt.get_portfolio_summary(user_id=None))
    pt.close_position(ps["order_id"], 90.0)
    s = pt.get_portfolio_summary(user_id=None)
    assert s["total_value"] == pytest.approx(INITIAL + 1_000.0)
    assert s["realized_pnl"] == pytest.approx(1_000.0)
    assert _equity_invariant(s)


# ── K. Persistent ledger replay at account creation ────────────────────────
def _make_ledger(trades):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE trades (id TEXT PRIMARY KEY, user_id TEXT, symbol TEXT, "
        "direction TEXT, entry_price REAL, exit_price REAL, quantity INTEGER, "
        "stop_loss REAL, target_1 REAL, target_2 REAL, entry_time TEXT, "
        "exit_time TEXT, status TEXT, pnl REAL)"
    )
    for t in trades:
        con.execute(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", t
        )
    con.commit()
    con.close()
    return path


def test_k_realized_replayed_from_db_ledger():
    """A fresh account replays the persistent SQLite trades ledger: realized
    P&L, cash and closed-trade history are seeded so the equity invariant and
    /paper/trades / /paper/performance / /api/portfolio agree across restarts."""
    legacy = [
        # (id, user, sym, dir, entry, exit, qty, sl, t1, t2, ent_t, ext_t, status, pnl)
        ("t1", "u", "POWERGRID", "LONG", 268.1, 267.25, 100, 0, 0, 0, "a", "b", "closed", -85.0),
        ("t2", "u", "AXISBANK", "SHORT", 1252.5, 1248.7, 100, 0, 0, 0, "a", "b", "closed", 380.0),
        ("t3", "u", "ICICIBANK", "LONG", 1399.8, 1396.6, 100, 0, 0, 0, "a", "b", "closed", -320.0),
        ("t4", "u", "JSWSTEEL", "LONG", 1311.9, 1308.9, 100, 0, 0, 0, "a", "b", "closed", -300.0),
        ("t5", "u", "TATASTEEL", "SHORT", 189.26, 188.35, 100, 0, 0, 0, "a", "b", "closed", 91.0),
    ]
    path = _make_ledger(legacy)
    try:
        realized, trades = load_trades_seed("u", db_path=path)
        assert realized == pytest.approx(-234.0)
        assert len(trades) == 5

        pt = PaperTradingEngine(db_path=path)
        s = pt.get_portfolio_summary(user_id="u")
        assert s["cash"] == pytest.approx(INITIAL - 234.0)
        assert s["realized_pnl"] == pytest.approx(-234.0)
        assert s["total_pnl"] == pytest.approx(-234.0)
        assert s["unrealized_pnl"] == 0.0
        assert s["total_value"] == pytest.approx(INITIAL - 234.0)
        assert _equity_invariant(s)
        assert len(pt.get_trade_history(user_id="u")) == 5

        # A new close this session keeps memory == DB (seed + delta).
        r = pt.place_order("NEW", "LONG", 90, 100.0, 95.0, 110.0, user_id="u")
        pt.fill_order(r["order_id"], user_id="u")
        pt.close_position(r["order_id"], 110.0, user_id="u")
        s2 = pt.get_portfolio_summary(user_id="u")
        assert s2["realized_pnl"] == pytest.approx(666.0)  # -234 + 900
        assert _equity_invariant(s2)
    finally:
        os.remove(path)


# ── L. SHORT reservation carried, never doubled, across fill+close ─────────
def test_l_short_reserve_never_doubled():
    pt = PaperTradingEngine(db_path=None)
    # Two identical 2k short orders: whatever their stage (pending/open), the
    # total reservation is exactly the sum of the two entry notionals - never
    # multiplied, never lost.
    n = 20
    r1 = pt.place_order("S1", "SHORT", n, 100.0, 105.0, 90.0)
    r2 = pt.place_order("S2", "SHORT", n, 100.0, 105.0, 90.0)
    # Both pending: reservation = 4k (committed notional).
    assert pt.get_portfolio_summary(user_id=None)["reserved_margin"] == pytest.approx(4_000.0)
    # Fill one: 2k moves from pending commitment to open collateral; the SUM
    # is unchanged (2k collateral + 2k still pending).
    pt.fill_order(r1["order_id"])
    assert pt.get_portfolio_summary(user_id=None)["reserved_margin"] == pytest.approx(4_000.0)
    # Fill the second: both now open, reservation still 4k - not 8k.
    pt.fill_order(r2["order_id"])
    s2 = pt.get_portfolio_summary(user_id=None)
    assert s2["reserved_margin"] == pytest.approx(4_000.0)
    assert s2["cash"] == pytest.approx(INITIAL + 4_000.0)
    assert s2["available_cash"] == pytest.approx(INITIAL - 4_000.0)
    assert _equity_invariant(s2)

    # Closing one releases exactly that position's collateral.
    pt.close_position(r1["order_id"], 100.0)
    s3 = pt.get_portfolio_summary(user_id=None)
    assert s3["reserved_margin"] == pytest.approx(2_000.0)
    assert s3["cash"] == pytest.approx(INITIAL + 2_000.0)
    assert _equity_invariant(s3)
    pt.close_position(r2["order_id"], 100.0)
    s4 = pt.get_portfolio_summary(user_id=None)
    assert s4["reserved_margin"] == 0.0
    assert s4["total_value"] == pytest.approx(INITIAL)
    assert _equity_invariant(s4)


# ── Live-scenario regression (memory +190 vs SQLite -44 divergence) ─────────
def test_live_scenario_regression_realized_agrees_with_db():
    """Reproduces the observed split-brain: 12 DB trades sum -44, but a memory
    account holding only the last 7 (+190) omits the 5 pre-restart trades (-234).
    After seeding, realized == SQLite sum and the equity invariant holds."""
    seven = [
        ("w1", "u", "WIPRO", "SHORT", 168.14, 167.34, 100, 0, 0, 0, "a", "b", "closed", 80.0),
        ("w2", "u", "TECHM", "LONG", 1532.1, 1523.0, 100, 0, 0, 0, "a", "b", "closed", -910.0),
        ("w3", "u", "ASIANPAINT", "SHORT", 2479.5, 2469.8, 100, 0, 0, 0, "a", "b", "closed", 970.0),
        ("w4", "u", "SBILIFE", "SHORT", 1684.3, 1677.1, 100, 0, 0, 0, "a", "b", "closed", 720.0),
        ("w5", "u", "CIPLA", "SHORT", 1376.9, 1371.5, 100, 0, 0, 0, "a", "b", "closed", 540.0),
        ("w6", "u", "LT", "SHORT", 3919.5, 3935.8, 100, 0, 0, 0, "a", "b", "closed", -1630.0),
        ("w7", "u", "BAJFINANCE", "SHORT", 1046.2, 1042.0, 100, 0, 0, 0, "a", "b", "closed", 420.0),
    ]
    five = [
        ("t1", "u", "POWERGRID", "LONG", 268.1, 267.25, 100, 0, 0, 0, "a", "b", "closed", -85.0),
        ("t2", "u", "AXISBANK", "SHORT", 1252.5, 1248.7, 100, 0, 0, 0, "a", "b", "closed", 380.0),
        ("t3", "u", "ICICIBANK", "LONG", 1399.8, 1396.6, 100, 0, 0, 0, "a", "b", "closed", -320.0),
        ("t4", "u", "JSWSTEEL", "LONG", 1311.9, 1308.9, 100, 0, 0, 0, "a", "b", "closed", -300.0),
        ("t5", "u", "TATASTEEL", "SHORT", 189.26, 188.35, 100, 0, 0, 0, "a", "b", "closed", 91.0),
    ]
    assert round(sum(t[13] for t in seven), 2) == pytest.approx(190.0)
    assert round(sum(t[13] for t in five), 2) == pytest.approx(-234.0)

    path = _make_ledger(seven + five)
    try:
        # Memory-only view BEFORE the fix would report +190 (7 trades).
        pt = PaperTradingEngine(db_path=path)
        s = pt.get_portfolio_summary(user_id="u")
        assert s["realized_pnl"] == pytest.approx(-44.0)   # == SQLite sum
        assert s["total_value"] == pytest.approx(INITIAL - 44.0)
        assert len(pt.get_closed_trades(user_id="u")) == 12
        assert _equity_invariant(s)

        # Open two shorts and mark them so unrealized is live too.
        s1 = pt.place_order("CIPLA", "SHORT", 5, 1372.5, 1375.3, 1368.76, user_id="u")
        s2 = pt.place_order("TCS", "SHORT", 1, 2199.8, 2204.57, 2193.45, user_id="u")
        pt.fill_order(s1["order_id"], user_id="u")
        pt.fill_order(s2["order_id"], user_id="u")
        pt.update_prices({"CIPLA": 1371.5, "TCS": 2198.6}, user_id="u")
        sc = pt.get_portfolio_summary(user_id="u")
        assert sc["realized_pnl"] == pytest.approx(-44.0)
        assert sc["unrealized_pnl"] == pytest.approx(5.0 + 1.2)
        assert sc["total_value"] == pytest.approx(INITIAL - 44.0 + 5.0 + 1.2)
        assert sc["reserved_margin"] == pytest.approx(5 * 1372.5 + 1 * 2199.8)
        # Contract formula: short proceeds NEVER inflate buying power.
        assert sc["available_cash"] == pytest.approx(INITIAL - 44.0 - (5 * 1372.5 + 1 * 2199.8))
        assert _equity_invariant(sc)
    finally:
        os.remove(path)


# ── BUY/SELL aliases keep identical margin behavior ────────────────────────
def test_buy_sell_alias_reservation():
    pt = PaperTradingEngine(db_path=None)
    r = pt.place_order("X", "BUY", 60, 100.0, 95.0, 110.0)
    assert pt.get_portfolio_summary(user_id=None)["reserved_margin"] == pytest.approx(6_000.0)
    pt.fill_order(r["order_id"])
    rs = pt.place_order("Y", "SELL", 30, 100.0, 102.0, 95.0)
    # open LONG committed 6,000 + pending SELL 3,000 are BOTH reserved
    assert pt.get_portfolio_summary(user_id=None)["reserved_margin"] == pytest.approx(9_000.0)
    pt.fill_order(rs["order_id"])
    s = pt.get_portfolio_summary(user_id=None)
    # cash: -6000 (BUY) + 3000 (SELL proceeds) ; reserved 9,000 (LONG + SHORT)
    assert s["cash"] == pytest.approx(INITIAL - 3_000.0)
    assert s["reserved_margin"] == pytest.approx(9_000.0)
    assert s["available_cash"] == pytest.approx(INITIAL - 9_000.0)
    assert _equity_invariant(s)


# ── M. Available = initial + realized − reserved (never cash-derived) ────────
def test_available_cash_contract_formula():
    # Open short credits proceeds to cash AND reserves its entry notional; the
    # proceeds must NOT inflate buying power.
    pt = PaperTradingEngine(db_path=None)
    ro = pt.place_order("OP", "SHORT", 4, 1845.9, 1848.55, 1842.37)
    pt.fill_order(ro["order_id"])
    s1 = pt.get_portfolio_summary(user_id=None)
    assert s1["reserved_margin"] == pytest.approx(4 * 1845.9)
    assert s1["available_cash"] == pytest.approx(INITIAL - 4 * 1845.9)

    # An additional PENDING short must ALSO cut available cash by its notional.
    rp = pt.place_order("PN", "SHORT", 1, 1380.1, 1382.3, 1377.16)
    s2 = pt.get_portfolio_summary(user_id=None)
    assert s2["reserved_margin"] == pytest.approx(4 * 1845.9 + 1 * 1380.1)
    assert s2["available_cash"] == pytest.approx(s1["available_cash"] - 1 * 1380.1)
    assert s2["available_cash"] == pytest.approx(INITIAL - s2["reserved_margin"])
    assert _equity_invariant(s2)

    # Cancel releases the pending reserve -> available cash rises back.
    pt.cancel_order(rp["order_id"])
    s3 = pt.get_portfolio_summary(user_id=None)
    assert s3["reserved_margin"] == pytest.approx(4 * 1845.9)
    assert s3["available_cash"] == pytest.approx(s1["available_cash"])
    assert s3["available_cash"] == pytest.approx(INITIAL - s3["reserved_margin"])

    # Aggregate path (multiple users) uses the same contract formula per account.
    pt2 = PaperTradingEngine(db_path=None)
    ra = pt2.place_order("A", "SHORT", 10, 100.0, 102.0, 95.0, user_id="alice")
    pt2.fill_order(ra["order_id"], user_id="alice")
    pt2.place_order("B", "SHORT", 10, 200.0, 204.0, 196.0, user_id="bob")
    agg = pt2.get_portfolio_summary(user_id=None)
    assert agg["reserved_margin"] == pytest.approx(1_000.0 + 2_000.0)
    # alice: INIT−1000 (open SHORT) ; bob: INIT−2000 (pending SHORT)
    assert agg["available_cash"] == pytest.approx(2 * INITIAL - 3_000.0)


# ── N. Headroom formulas match the verified live state (golden values) ───────
def test_headroom_formulas_match_verified_live_state():
    # Mirrors the live /api/portfolio state without touching the DB ledger:
    # realized -44 (persisted ledger), two open shorts marked to market.
    pt = PaperTradingEngine(db_path=None)
    pt._ensure_account(None).seed_history(-44.0, [])
    r1 = pt.place_order("SUNPHARMA", "SHORT", 4, 1845.9, 1848.55, 1842.37)
    r2 = pt.place_order("ICICIBANK", "SHORT", 1, 1380.1, 1382.3, 1377.16)
    pt.fill_order(r1["order_id"])
    pt.fill_order(r2["order_id"])
    pt.update_prices({"SUNPHARMA": 1846.3, "ICICIBANK": 1380.5})
    s = pt.get_portfolio_summary(user_id=None)

    reserved = 4 * 1845.9 + 1 * 1380.1
    unrealized = -1.6 + -0.4
    # Validated live values: total_value 9,954 / available 1,192.3
    # (available = INITIAL + realized − reserved under the contract).
    assert s["realized_pnl"] == pytest.approx(-44.0)
    assert s["unrealized_pnl"] == pytest.approx(unrealized)
    assert s["reserved_margin"] == pytest.approx(reserved)          # committed (LONG+SHORT)
    assert s["available_cash"] == pytest.approx(1_192.3)            # INITIAL + realized − reserved
    assert s["available_cash"] == pytest.approx(INITIAL - 44.0 - reserved)
    assert s["total_value"] == pytest.approx(9_954.0)               # INITIAL + realized + unrealized
    assert _equity_invariant(s)


# ── O. Golden scenario (contract Section 13) ────────────────────────────────
def _persist_closed_trade(path, trade, user_id):
    """Simulate the API-layer close-finalize write to the durable trades table
    (the engine persists open positions/pending orders; closed trades are
    committed to SQLite by the trading API like finalize_position does)."""
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade["id"], user_id, trade["symbol"], trade["direction"],
         trade["entry_price"], trade["exit_price"], trade["quantity"],
         trade["stop_loss"], trade["target_1"], trade["target_2"],
         trade["opened_at"], trade["exit_time"], "closed", trade["pnl"]),
    )
    con.commit()
    con.close()


def test_golden_scenario_section_13():
    """End-to-end Section-13 numbers: 10,000 initial, two shorts, a pending
    order, a cancel and one +50 short close (second short marked +20).

    2,000 SHORT -> avail 8,000 / reserved 2,000
    +1,000 SHORT -> avail 7,000 / reserved 3,000
    +500 pending-> avail 6,500 / reserved 3,500
    cancel      -> avail 7,000 / reserved 3,000
    close +50   -> avail 9,050 / reserved 1,000 / realized +50
    second short unrealized +20 -> total_value 10,070
    """
    path = _make_ledger([])
    try:
        pt = PaperTradingEngine(db_path=path, persist=True)
        gid = "g"

        # Step 1: SHORT 2,000 margin
        r1 = pt.place_order("G1", "SHORT", 20, 100.0, 105.0, 90.0, user_id=gid)
        s = pt.get_portfolio_summary(user_id=gid)
        assert s["available_cash"] == pytest.approx(8_000.0)
        assert s["reserved_margin"] == pytest.approx(2_000.0)
        pt.fill_order(r1["order_id"], user_id=gid)
        s = pt.get_portfolio_summary(user_id=gid)
        assert s["available_cash"] == pytest.approx(8_000.0)
        assert s["reserved_margin"] == pytest.approx(2_000.0)

        # Step 2: +SHORT 1,000
        r2 = pt.place_order("G2", "SHORT", 10, 100.0, 105.0, 90.0, user_id=gid)
        s = pt.get_portfolio_summary(user_id=gid)
        assert s["available_cash"] == pytest.approx(7_000.0)
        assert s["reserved_margin"] == pytest.approx(3_000.0)
        pt.fill_order(r2["order_id"], user_id=gid)
        s = pt.get_portfolio_summary(user_id=gid)
        assert s["available_cash"] == pytest.approx(7_000.0)
        assert s["reserved_margin"] == pytest.approx(3_000.0)

        # Step 3: +500 pending
        r3 = pt.place_order("G3", "SHORT", 5, 100.0, 105.0, 90.0, user_id=gid)
        s = pt.get_portfolio_summary(user_id=gid)
        assert s["available_cash"] == pytest.approx(6_500.0)
        assert s["reserved_margin"] == pytest.approx(3_500.0)

        # Step 4: cancel the pending
        pt.cancel_order(r3["order_id"], user_id=gid)
        s = pt.get_portfolio_summary(user_id=gid)
        assert s["available_cash"] == pytest.approx(7_000.0)
        assert s["reserved_margin"] == pytest.approx(3_000.0)

        # Step 5: close first SHORT (20 @100) at 97.5 -> +50 realized
        close = pt.close_position(r1["order_id"], 97.5, user_id=gid)
        assert close["pnl"] == pytest.approx(50.0)
        _persist_closed_trade(path, close["trade"], gid)
        s = pt.get_portfolio_summary(user_id=gid)
        assert s["available_cash"] == pytest.approx(9_050.0)
        assert s["reserved_margin"] == pytest.approx(1_000.0)
        assert s["realized_pnl"] == pytest.approx(50.0)

        # Step 6: second SHORT marked to +20 unrealized
        pt.update_prices({"G2": 98.0}, user_id=gid)
        s = pt.get_portfolio_summary(user_id=gid)
        assert s["unrealized_pnl"] == pytest.approx(20.0)
        assert s["total_value"] == pytest.approx(10_070.0)
        assert _equity_invariant(s)

        # Restart: remaining SHORT + realized +50 must survive. Price marks are
        # not durable by design (write reduction) — the 5s monitor refreshes
        # them; replay the mark here to model that refresh.
        fresh = PaperTradingEngine(db_path=path, persist=True)
        fresh.update_prices({"G2": 98.0}, user_id=gid)
        fs = fresh.get_portfolio_summary(user_id=gid)
        assert fs["positions_count"] == 1
        assert fs["pending_orders_count"] == 0
        assert fs["reserved_margin"] == pytest.approx(1_000.0)
        assert fs["available_cash"] == pytest.approx(9_050.0)
        assert fs["realized_pnl"] == pytest.approx(50.0)
        assert fs["unrealized_pnl"] == pytest.approx(20.0)
        assert fs["total_value"] == pytest.approx(10_070.0)
        assert _equity_invariant(fs)
    finally:
        os.remove(path)


# ── P. Open positions + pending orders persist across restart ───────────────
def test_p_persist_and_restart_restores_positions_pending():
    path = _make_ledger([])
    try:
        pt = PaperTradingEngine(db_path=path, persist=True)
        rl = pt.place_order("LX", "LONG", 60, 100.0, 95.0, 110.0, user_id="p")
        rs = pt.place_order("SX", "SHORT", 30, 100.0, 105.0, 90.0, user_id="p")
        pt.fill_order(rl["order_id"], user_id="p")
        # state: one open LONG + one pending SHORT, both durable.

        fresh = PaperTradingEngine(db_path=path, persist=True)
        s = fresh.get_portfolio_summary(user_id="p")
        assert s["positions_count"] == 1
        assert s["pending_orders_count"] == 1
        assert s["reserved_margin"] == pytest.approx(6_000.0 + 3_000.0)
        assert fresh.get_positions(user_id="p")[0]["symbol"] == "LX"
        assert fresh.get_pending_orders(user_id="p")[0]["symbol"] == "SX"
        # LONG disbursed from ledger cash; the pending SHORT committed nothing yet
        assert s["cash"] == pytest.approx(INITIAL - 6_000.0)
        assert _equity_invariant(s)
    finally:
        os.remove(path)


# ── Q. Restart never duplicates a closed trade or its realized P&L ──────────
def test_q_restart_does_not_duplicate_closed_trade():
    path = _make_ledger([])
    try:
        pt = PaperTradingEngine(db_path=path, persist=True)
        r = pt.place_order("LX", "LONG", 100, 100.0, 95.0, 110.0, user_id="q")
        pt.fill_order(r["order_id"], user_id="q")
        close = pt.close_position(r["order_id"], 110.0, user_id="q")
        _persist_closed_trade(path, close["trade"], "q")

        fresh = PaperTradingEngine(db_path=path, persist=True)
        s = fresh.get_portfolio_summary(user_id="q")
        assert s["positions_count"] == 0
        assert s["realized_pnl"] == pytest.approx(1_000.0)  # replayed once
        assert len(fresh.get_trade_history(user_id="q")) == 1
        assert _equity_invariant(s)

        # Second restart: still once - the ledger is authoritative, never +2,000.
        fresh2 = PaperTradingEngine(db_path=path, persist=True)
        assert fresh2.get_portfolio_summary(user_id="q")["realized_pnl"] == pytest.approx(1_000.0)
        assert len(fresh2.get_trade_history(user_id="q")) == 1
    finally:
        os.remove(path)


# ── R. Cancel/fill transitions survive restart consistently ────────────────
def test_r_restart_reflects_cancel_and_fill_transitions():
    path = _make_ledger([])
    try:
        pt = PaperTradingEngine(db_path=path, persist=True)
        # A pending order cancelled BEFORE restart must not reappear.
        rc = pt.place_order("CX", "SHORT", 50, 100.0, 105.0, 90.0, user_id="r")
        pt.cancel_order(rc["order_id"], user_id="r")
        # A second order filled: open SHORT must survive.
        rk = pt.place_order("KF", "SHORT", 50, 100.0, 105.0, 90.0, user_id="r")
        pt.fill_order(rk["order_id"], user_id="r")

        fresh = PaperTradingEngine(db_path=path, persist=True)
        s = fresh.get_portfolio_summary(user_id="r")
        assert s["positions_count"] == 1
        assert s["pending_orders_count"] == 0
        assert fresh.get_positions(user_id="r")[0]["symbol"] == "KF"
        assert fresh.get_pending_orders(user_id="r") == []
        assert s["reserved_margin"] == pytest.approx(5_000.0)
        assert s["cash"] == pytest.approx(INITIAL + 5_000.0)  # short proceeds in ledger
        assert _equity_invariant(s)
    finally:
        os.remove(path)


# ── S. Multi-user persistence stays isolated across restart ────────────────
def test_s_multi_user_isolation_survives_restart():
    path = _make_ledger([])
    try:
        pt = PaperTradingEngine(db_path=path, persist=True)
        ra = pt.place_order("A", "SHORT", 10, 100.0, 105.0, 90.0, user_id="pa")
        pt.fill_order(ra["order_id"], user_id="pa")
        pb = pt.place_order("B", "LONG", 20, 100.0, 95.0, 110.0, user_id="pb")
        # pa: open SHORT, pb: pending LONG - each isolated in its own rows.

        fresh = PaperTradingEngine(db_path=path, persist=True)
        sa = fresh.get_portfolio_summary(user_id="pa")
        sb = fresh.get_portfolio_summary(user_id="pb")
        assert sa["positions_count"] == 1 and sa["pending_orders_count"] == 0
        assert sb["positions_count"] == 0 and sb["pending_orders_count"] == 1
        assert sa["reserved_margin"] == pytest.approx(1_000.0)
        assert sb["reserved_margin"] == pytest.approx(2_000.0)
        assert sa["available_cash"] == pytest.approx(INITIAL - 1_000.0)
        assert sb["available_cash"] == pytest.approx(INITIAL - 2_000.0)
        assert fresh.get_positions(user_id="pb") == []  # never leaked from pa
        assert fresh.get_pending_orders(user_id="pa") == []
    finally:
        os.remove(path)


# ── T. Durable tables mirror in-memory state exactly ───────────────────────
def test_t_db_rows_mirror_engine_state():
    path = _make_ledger([])
    try:
        pt = PaperTradingEngine(db_path=path, persist=True)
        r1 = pt.place_order("A", "LONG", 60, 100.0, 95.0, 110.0, user_id="t")
        r2 = pt.place_order("B", "SHORT", 30, 100.0, 105.0, 90.0, user_id="t")
        pt.fill_order(r1["order_id"], user_id="t")

        con = sqlite3.connect(path)
        pos = con.execute("SELECT symbol, direction, quantity, entry_price FROM paper_positions WHERE user_id='t'").fetchall()
        pen = con.execute("SELECT symbol, direction, quantity, entry_price FROM paper_pending_orders WHERE user_id='t'").fetchall()
        con.close()

        assert pos == [("A", "LONG", 60, 100.0)]
        assert pen == [("B", "SHORT", 30, 100.0)]
    finally:
        os.remove(path)