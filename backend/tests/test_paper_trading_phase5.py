"""Phase 5 — automatic entry trigger / auto-fill for pending paper orders.

Extends the runtime monitor path (check_entry_triggers -> fill_order) so:
 - a pending BUY/LONG order auto-fills once market_price >= entry_price;
 - a pending SELL/SHORT order auto-fills once market_price <= entry_price;
 - the fill always executes at the order's configured entry price;
 - the newly opened position then flows through the existing SL/Target_1
   monitoring exactly like a manually filled position.

This is the smallest extension of the Phase 1-4 paper-trading behavior: no
changes to buy/sell validation, P&L, cash accounting, portfolio valuation,
risk logic or per-user isolation.
"""

import pytest


def test_phase5_buy_below_entry_stays_pending():
    """BUY entry 1694 with market 1693 -> order remains pending."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "BUY", 10, 1694.0, 1685.0, 1710.0, user_id="user-a")
    fills = pt.check_entry_triggers({"RELIANCE": 1693.0}, user_id="user-a")
    assert fills == []
    assert len(pt.get_pending_orders(user_id="user-a")) == 1
    assert pt.get_positions(user_id="user-a") == []


def test_phase5_buy_exactly_at_entry_autofills():
    """BUY entry 1694 with market 1694 -> auto-fill."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "BUY", 10, 1694.0, 1685.0, 1710.0, user_id="user-a")
    fills = pt.check_entry_triggers({"RELIANCE": 1694.0}, user_id="user-a")
    assert len(fills) == 1
    assert fills[0]["status"] == "filled"
    assert pt.get_pending_orders(user_id="user-a") == []
    assert len(pt.get_positions(user_id="user-a")) == 1


def test_phase5_buy_above_entry_autofills():
    """BUY entry 1694 with market 1695 -> auto-fill."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "BUY", 10, 1694.0, 1685.0, 1710.0, user_id="user-a")
    fills = pt.check_entry_triggers({"RELIANCE": 1695.0}, user_id="user-a")
    assert len(fills) == 1
    assert fills[0]["status"] == "filled"


def test_phase5_sell_above_entry_stays_pending():
    """SELL entry 1694 with market 1695 -> order remains pending."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SELL", 5, 1694.0, 1705.0, 1680.0, user_id="user-a")
    fills = pt.check_entry_triggers({"TCS": 1695.0}, user_id="user-a")
    assert fills == []
    assert len(pt.get_pending_orders(user_id="user-a")) == 1
    assert pt.get_positions(user_id="user-a") == []


def test_phase5_sell_exactly_at_entry_autofills():
    """SELL entry 1694 with market 1694 -> auto-fill."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SELL", 5, 1694.0, 1705.0, 1680.0, user_id="user-a")
    fills = pt.check_entry_triggers({"TCS": 1694.0}, user_id="user-a")
    assert len(fills) == 1
    assert fills[0]["status"] == "filled"


def test_phase5_sell_below_entry_autofills():
    """SELL entry 1694 with market 1693 -> auto-fill."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SELL", 5, 1694.0, 1705.0, 1680.0, user_id="user-a")
    fills = pt.check_entry_triggers({"TCS": 1693.0}, user_id="user-a")
    assert len(fills) == 1
    assert fills[0]["status"] == "filled"


def test_phase5_autofill_creates_correct_open_position():
    """Auto-filled order becomes a normal open position with its configured
    entry price — market above entry does NOT change the execution price. The
    BUY direction is canonicalized to LONG so the existing SL/Target monitor
    applies unchanged."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "BUY", 10, 1694.0, 1685.0, 1710.0, user_id="user-a")
    fills = pt.check_entry_triggers({"RELIANCE": 1697.0}, user_id="user-a")
    assert len(fills) == 1
    pos = pt.get_positions(user_id="user-a")
    assert len(pos) == 1
    assert pos[0]["status"] == "open"
    assert pos[0]["symbol"] == "RELIANCE"
    assert pos[0]["direction"] == "LONG"
    assert pos[0]["entry_price"] == 1694.0
    assert pos[0]["stop_loss"] == 1685.0
    assert pos[0]["target_1"] == 1710.0
    assert pos[0]["filled_at"] is not None
    assert pt.get_pending_orders(user_id="user-a") == []


def test_phase5_autofill_uses_configured_entry_for_shorts():
    """SELL entry 1694 with market deeper below (1690) -> fill stays at 1694;
    the SELL direction is canonicalized to SHORT for the existing monitor."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SELL", 5, 1694.0, 1705.0, 1680.0, user_id="user-a")
    fills = pt.check_entry_triggers({"TCS": 1690.0}, user_id="user-a")
    assert len(fills) == 1
    pos = pt.get_positions(user_id="user-a")[0]
    assert pos["direction"] == "SHORT"
    assert pos["entry_price"] == 1694.0


def test_phase5_autofill_cash_accounting_correct():
    """LONG fill debits cash, SHORT fill credits cash — unchanged accounting."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r1 = pt.place_order("RELIANCE", "BUY", 10, 1694.0, 1685.0, 1710.0, user_id="user-a")
    pt.check_entry_triggers({"RELIANCE": 1695.0}, user_id="user-a")
    s = pt.get_portfolio_summary(user_id="user-a")
    assert s["cash"] == pytest.approx(1_000_000.0 - 10 * 1694.0)
    assert s["positions_count"] == 1

    r2 = pt.place_order("TCS", "SELL", 5, 1694.0, 1705.0, 1680.0, user_id="user-a")
    pt.check_entry_triggers({"TCS": 1693.0}, user_id="user-a")
    s2 = pt.get_portfolio_summary(user_id="user-a")
    assert s2["cash"] == pytest.approx(1_000_000.0 - 10 * 1694.0 + 5 * 1694.0)
    assert s2["positions_count"] == 2


def test_phase5_same_pending_order_cannot_fill_twice():
    """Repeated monitor cycles never double-fill a single pending order."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "BUY", 10, 1694.0, 1685.0, 1710.0, user_id="user-a")
    f1 = pt.check_entry_triggers({"RELIANCE": 1694.0}, user_id="user-a")
    f2 = pt.check_entry_triggers({"RELIANCE": 1694.0}, user_id="user-a")
    assert len(f1) == 1
    assert f2 == []
    assert len(pt.get_positions(user_id="user-a")) == 1
    assert pt.get_pending_orders(user_id="user-a") == []


def test_phase5_user_isolation_pending_fill():
    """One user's auto-fill never touches another user's account."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    rA = pt.place_order("RELIANCE", "BUY", 10, 1694.0, 1685.0, 1710.0, user_id="user-a")
    rB = pt.place_order("TCS", "BUY", 5, 1694.0, 1685.0, 1710.0, user_id="user-b")

    fills = pt.check_entry_triggers(
        {"RELIANCE": 1694.0, "TCS": 1694.0}, user_id="user-a"
    )
    assert len(fills) == 1
    assert pt.get_positions(user_id="user-a")[0]["symbol"] == "RELIANCE"
    assert pt.get_positions(user_id="user-b") == []
    assert len(pt.get_pending_orders(user_id="user-b")) == 1
    assert pt.get_portfolio_summary(user_id="user-b")["cash"] == pytest.approx(1_000_000.0)


def test_phase5_autofill_then_sl_target_still_works():
    """After auto-fill the existing SL/Target_1 monitoring still closes."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "BUY", 10, 1694.0, 1685.0, 1710.0, user_id="user-a")
    pt.check_entry_triggers({"RELIANCE": 1694.0}, user_id="user-a")
    assert len(pt.get_positions(user_id="user-a")) == 1

    exits = pt.check_stops({"RELIANCE": 1710.0}, user_id="user-a")
    assert len(exits) == 1
    assert exits[0]["trade"]["exit_price"] == 1710.0
    assert exits[0]["trade"]["result"] == "WIN"
    assert exits[0]["pnl"] == pytest.approx((1710.0 - 1694.0) * 10)
    assert pt.get_positions(user_id="user-a") == []


def test_phase5_autofill_then_sl_close():
    """After auto-fill, a SELL position still auto-closes at its stop-loss."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SELL", 5, 1694.0, 1705.0, 1680.0, user_id="user-a")
    pt.check_entry_triggers({"TCS": 1694.0}, user_id="user-a")
    exits = pt.check_stops({"TCS": 1705.0}, user_id="user-a")
    assert len(exits) == 1
    assert exits[0]["trade"]["result"] == "LOSS"
    assert exits[0]["pnl"] == pytest.approx((1694.0 - 1705.0) * 5)


def test_phase5_invalid_market_prices_do_not_trigger_fills():
    """Zero/negative/NaN/None/non-numeric prices never trigger a fill."""
    from app.services.paper_trading import PaperTradingEngine

    bad_prices = [0, -1, float("nan"), None, "garbage"]
    for bad in bad_prices:
        pt = PaperTradingEngine()
        r = pt.place_order("RELIANCE", "BUY", 10, 1694.0, 1685.0, 1710.0, user_id="user-a")
        fills = pt.check_entry_triggers({"RELIANCE": bad}, user_id="user-a")
        assert fills == []
        assert len(pt.get_pending_orders(user_id="user-a")) == 1
        assert pt.get_positions(user_id="user-a") == []
        assert pt.get_portfolio_summary(user_id="user-a")["cash"] == pytest.approx(1_000_000.0)


def test_phase5_autofill_insufficient_cash_leaves_order_pending():
    """An order whose notional exceeds Available Cash is rejected at PLACEMENT,
    so cash is never touched and no un-fillable pending order ever exists."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "BUY", 1_000_000, 1694.0, 1685.0, 1710.0, user_id="user-a")
    assert "error" in r
    assert r["error"].startswith("Insufficient available cash")
    fills = pt.check_entry_triggers({"RELIANCE": 1694.0}, user_id="user-a")
    assert fills == []
    assert pt.get_pending_orders(user_id="user-a") == []
    assert pt.get_positions(user_id="user-a") == []
    assert pt.get_portfolio_summary(user_id="user-a")["cash"] == pytest.approx(1_000_000.0)


def test_phase5_monitor_cycle_autofills_pending_and_opens_position():
    """The monitor cycle auto-fills an eligible pending order, calls the fill
    finalizer and does NOT finalize a trade for a mere fill."""
    import asyncio
    from app.services.paper_monitor import _run_monitor_cycle
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "BUY", 10, 1694.0, 1685.0, 1710.0, user_id="user-a")

    async def fake_quote(symbol):
        assert symbol == "RELIANCE"
        return {"price": 1694.0}

    closed = []
    filled = []

    async def fake_finalize(result, user_id):
        closed.append((result, user_id))

    async def fake_on_fill(result, user_id):
        filled.append((result, user_id))

    loop = asyncio.new_event_loop()
    loop.run_until_complete(
        _run_monitor_cycle(pt, fake_quote, fake_finalize, on_fill=fake_on_fill)
    )
    loop.close()

    assert len(filled) == 1
    assert filled[0][1] == "user-a"
    assert pt.get_pending_orders(user_id="user-a") == []
    positions = pt.get_positions(user_id="user-a")
    assert len(positions) == 1
    assert positions[0]["status"] == "open"
    assert positions[0]["entry_price"] == 1694.0
    assert positions[0]["current_price"] == 1694.0
    assert closed == []


def test_phase5_monitor_marks_filled_position_then_monitors_sl_target():
    """Same cycle: entry trigger fills the order, the position is marked to the
    observed price, then existing SL/Target_1 monitoring closes it."""
    import asyncio
    from app.services.paper_monitor import _run_monitor_cycle
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SELL", 5, 1694.0, 1705.0, 1680.0, user_id="user-a")

    async def fake_quote(symbol):
        return {"price": 1680.0}  # at/below entry AND at target_1

    closed = []

    async def fake_finalize(result, user_id):
        closed.append((result, user_id))

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_run_monitor_cycle(pt, fake_quote, fake_finalize))
    loop.close()

    assert pt.get_positions(user_id="user-a") == []
    assert len(closed) == 1
    assert closed[0][1] == "user-a"
    assert closed[0][0]["trade"]["exit_price"] == 1680.0
    assert closed[0][0]["trade"]["result"] == "WIN"
    assert closed[0][0]["pnl"] == pytest.approx((1694.0 - 1680.0) * 5)
    assert pt.get_portfolio_summary(user_id="user-a")["total_pnl"] == pytest.approx(70.0)


def test_phase5_monitor_uses_one_quote_per_symbol_across_users():
    """Multiple users / multiple pending orders: one quote per unique symbol,
    each user's orders evaluated against their own account's price."""
    import asyncio
    from app.services.paper_monitor import _run_monitor_cycle
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    pt.place_order("RELIANCE", "BUY", 10, 1694.0, 1685.0, 1710.0, user_id="user-a")
    pt.place_order("TCS", "SELL", 5, 1694.0, 1705.0, 1680.0, user_id="user-b")

    quotes = {"RELIANCE": 1694.0, "TCS": 1695.0}  # A triggers; B stays pending
    seen = []

    async def fake_quote(symbol):
        seen.append(symbol)
        return {"price": quotes[symbol]}

    closed = []
    filled = []

    async def fake_finalize(result, user_id):
        closed.append((result, user_id))

    async def fake_on_fill(result, user_id):
        filled.append((result, user_id))

    loop = asyncio.new_event_loop()
    loop.run_until_complete(
        _run_monitor_cycle(pt, fake_quote, fake_finalize, on_fill=fake_on_fill)
    )
    loop.close()

    assert set(seen) == {"RELIANCE", "TCS"}
    assert len(filled) == 1 and filled[0][1] == "user-a"
    assert len(pt.get_positions(user_id="user-a")) == 1
    assert pt.get_positions(user_id="user-b") == []
    assert len(pt.get_pending_orders(user_id="user-b")) == 1
    assert closed == []