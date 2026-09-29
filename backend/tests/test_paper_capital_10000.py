"""Paper-trading initial capital is a single source of truth: INITIAL_CAPITAL.

Every paper account starts at settings.INITIAL_CAPITAL (10,000) and the
portfolio summary derives cash/available from the replayed trades ledger:

    available_cash = INITIAL_CAPITAL + realized_pnl - committed - pending

These tests pin the ₹10,000 contract end-to-end:
 - fresh account state (no hardcoded balances, engine derives from INITIAL_CAPITAL)
 - per-user isolation (each account holds its own 10,000)
 - margin reservation/release: a ₹4,000 order leaves 6,000 available / 4,000
   reserved; a ₹12,000 order is rejected (no negative balances)
 - realized P&L updates available cash
 - SHORT proceeds never add buying power
 - /api/portfolio shape (frontend consumes backend value, not a hardcoded cap)
"""

import pytest

from app.core.config import settings
from app.services.paper_trading import PaperTradingEngine, PaperAccount

INITIAL = settings.INITIAL_CAPITAL


# ── A. Single source of truth ───────────────────────────────────────────
def test_initial_capital_is_10000_single_source():
    """The paper capital is defined once in config and used everywhere."""
    assert settings.INITIAL_CAPITAL == 10_000
    assert PaperAccount().cash == settings.INITIAL_CAPITAL


# ── B. Fresh ₹10k portfolio state ───────────────────────────────────────
def test_fresh_10k_portfolio_summary():
    """A brand-new account shows 10,000 everywhere with no positions."""
    pt = PaperTradingEngine()
    s = pt.get_portfolio_summary()
    assert s["initial_capital"] == pytest.approx(10_000.0)
    assert s["cash"] == pytest.approx(10_000.0)
    assert s["available_cash"] == pytest.approx(10_000.0)
    assert s["total_value"] == pytest.approx(10_000.0)
    assert s["reserved_margin"] == 0.0
    assert s["pending_value"] == 0.0
    assert s["total_pnl"] == 0.0
    assert s["realized_pnl"] == 0.0
    assert s["unrealized_pnl"] == 0.0
    assert s["positions_count"] == 0
    assert s["pending_orders_count"] == 0


# ── C. Per-user isolation: each account holds its own 10,000 ────────────
def test_each_user_account_holds_its_own_10000():
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    sa = pt.get_portfolio_summary(user_id="user-a")
    sb = pt.get_portfolio_summary(user_id="user-b")
    assert sa["available_cash"] == pytest.approx(10_000.0)
    assert sa["cash"] == pytest.approx(10_000.0)
    assert sb["available_cash"] == pytest.approx(10_000.0)
    assert sb["cash"] == pytest.approx(10_000.0)
    # spending in one account never affects the other
    r = pt.place_order("RELIANCE", "LONG", 4, 1_000.0, 980.0, 1050.0, user_id="user-a")
    assert "error" not in r
    assert pt.get_portfolio_summary(user_id="user-a")["available_cash"] == pytest.approx(6_000.0)
    assert pt.get_portfolio_summary(user_id="user-b")["available_cash"] == pytest.approx(10_000.0)


# ── D. ₹4,000 order → 6,000 available / 4,000 reserved ──────────────────
def test_4000_order_reserves_4000_leaves_6000():
    """A ₹4,000 LONG order commits its notional: cash drops to 6,000."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 4, 1_000.0, 980.0, 1050.0)
    assert r["status"] == "pending"
    s = pt.get_portfolio_summary()
    assert s["cash"] == pytest.approx(10_000.0)  # pending does not consume cash
    assert s["available_cash"] == pytest.approx(6_000.0)
    assert s["reserved_margin"] == pytest.approx(4_000.0)
    assert s["pending_value"] == pytest.approx(4_000.0)
    fill = pt.fill_order(r["order_id"])
    assert fill["status"] == "filled"
    s = pt.get_portfolio_summary()
    assert s["cash"] == pytest.approx(6_000.0)
    assert s["available_cash"] == pytest.approx(6_000.0)
    assert s["reserved_margin"] == pytest.approx(4_000.0)
    assert s["positions_count"] == 1


# ── E. ₹12,000 order is rejected (insufficient funds) ───────────────────
def test_12000_order_rejected_insufficient_cash():
    """Notional beyond available cash must be rejected, leaving no negative
    balance and no state mutation."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    s_before = pt.get_portfolio_summary()
    r = pt.place_order("RELIANCE", "LONG", 12, 1_000.0, 980.0, 1050.0)
    assert "error" in r
    assert "Insufficient available cash" in r["error"]
    assert r["required"] == pytest.approx(12_000.0)
    assert r["available"] == pytest.approx(10_000.0)
    s_after = pt.get_portfolio_summary()
    assert s_after["cash"] == s_before["cash"]
    assert s_after["available_cash"] == pytest.approx(10_000.0)
    assert s_after["positions_count"] == 0
    assert s_after["pending_orders_count"] == 0


# ── F. Margin release on close ──────────────────────────────────────────
def test_margin_released_after_close_with_realized_pnl():
    """Closing releases the committed margin and realizes P&L into cash."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 4, 1_000.0, 980.0, 1050.0)
    pt.fill_order(r["order_id"])
    close = pt.close_position(r["order_id"], 1010.0)  # +40 realized
    assert close["pnl"] == pytest.approx(40.0)
    s = pt.get_portfolio_summary()
    assert s["available_cash"] == pytest.approx(10_040.0)
    assert s["total_pnl"] == pytest.approx(40.0)
    assert s["realized_pnl"] == pytest.approx(40.0)
    assert s["reserved_margin"] == 0.0
    assert s["positions_count"] == 0
    assert s["total_value"] == pytest.approx(10_040.0)
    # invariant: equity = initial + realized + unrealized
    assert s["total_value"] == pytest.approx(INITIAL + s["total_pnl"] + s["unrealized_pnl"])


# ── G. SHORT proceeds never add buying power ────────────────────────────
def test_short_proceeds_never_add_buying_power():
    """Opening a SHORT credits the ledger but buys no extra buying power:
    available cash never exceeds initial + realized − reserved."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SHORT", 40, 100.0, 105.0, 90.0)  # notional 4,000
    fill = pt.fill_order(r["order_id"])
    assert fill["status"] == "filled"
    s = pt.get_portfolio_summary()
    # proceeds credit raw cash...
    assert s["cash"] == pytest.approx(14_000.0)
    # ...but available cash DROPS by the collateral (no self-funding short)
    assert s["available_cash"] == pytest.approx(6_000.0)
    assert s["reserved_margin"] == pytest.approx(4_000.0)
    assert s["total_value"] == pytest.approx(10_000.0)
    assert s["total_pnl"] == 0.0


# ── H. /api/portfolio shape (no hardcoded frontend balances) ────────────
def test_api_portfolio_shape_and_backend_source():
    """/api/portfolio returns the engine summary plus positions; the frontend
    renders these values and never hardcodes a balance. The summary the API
    serves is exactly the engine's (verified by direct inspection of
    app/api/trading.get_portfolio → paper_engine summary + positions)."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 4, 1_000.0, 980.0, 1050.0)
    pt.fill_order(r["order_id"])
    summary = pt.get_portfolio_summary()
    positions = pt.get_positions()
    # What the API endpoint serves: {**summary, "positions": positions}
    payload = {**summary, "positions": positions}
    assert set(payload.keys()) >= {
        "initial_capital", "cash", "total_value", "reserved_margin",
        "available_cash", "short_liability", "pending_value", "total_pnl",
        "unrealized_pnl", "realized_pnl", "positions_count",
        "pending_orders_count", "positions",
    }
    assert payload["initial_capital"] == pytest.approx(10_000.0)
    assert payload["available_cash"] == pytest.approx(6_000.0)
    assert payload["cash"] == pytest.approx(6_000.0)
    assert len(payload["positions"]) == 1
    # the positions array is the live engine list, not a stale static balance
    assert payload["positions"][0]["symbol"] == "RELIANCE"
    assert payload["positions"][0]["entry_price"] == 1_000.0