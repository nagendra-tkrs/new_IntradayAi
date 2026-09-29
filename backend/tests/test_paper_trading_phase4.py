"""Phase 4 — user isolation + automatic SL/Target monitoring for paper trading.

Covers:
 - per-user account provisioning and full state isolation (cash / pending /
   positions / closed trades);
 - the runtime engine path (update_prices -> check_stops) that the background
   monitor drives: LONG/SHORT target & stop-loss, exact touches, gap exits at
   observed price, unrealized-PnL marking, single-close guarantees, no
   cross-symbol closing;
 - the background monitor cycle (quote fetch, per-symbol resilience, empty
   account skip, auto-close via the shared finalizer);
 - backward compatibility of the no-user engine facade.
"""

import pytest
from app.core.config import settings


def test_phase4_user_account_auto_provisioning():
    """A user-scoped read provisions that user's account with full notional
    capital, independently of any other account."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    sA = pt.get_portfolio_summary(user_id="user-a")
    assert sA["cash"] == pytest.approx(settings.INITIAL_CAPITAL)
    assert sA["positions_count"] == 0
    assert sA["pending_orders_count"] == 0
    sB = pt.get_portfolio_summary(user_id="user-b")
    assert sB["cash"] == pytest.approx(settings.INITIAL_CAPITAL)
    assert len(pt.accounts) == 2
    # Each account holds its own ledger: zero cross-contamination.
    assert pt.accounts["user-a"].total_pnl == 0.0
    assert pt.accounts["user-b"].total_pnl == 0.0


def test_phase4_cross_user_isolation():
    """One user cannot see or act on another user's pending orders/positions."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    rA = pt.place_order("RELIANCE", "LONG", 4, 2450.0, 2420.0, 2500.0, user_id="user-a")
    pt.fill_order(rA["order_id"], user_id="user-a")
    pB = pt.place_order("TCS", "LONG", 2, 3200.0, 3180.0, 3250.0, user_id="user-b")

    # B sees only its own pending order.
    b_pending = pt.get_pending_orders(user_id="user-b")
    assert len(b_pending) == 1 and b_pending[0]["symbol"] == "TCS"
    assert b_pending[0]["id"] == pB["order_id"]
    # B does NOT see A's open RELIANCE position.
    assert pt.get_positions(user_id="user-b") == []
    # A still has its position and A's cash reflects only A's LONG fill.
    assert len(pt.get_positions(user_id="user-a")) == 1
    a_sum = pt.get_portfolio_summary(user_id="user-a")
    assert a_sum["cash"] == pytest.approx(settings.INITIAL_CAPITAL - 4 * 2450.0)
    assert a_sum["positions_count"] == 1
    # B's pending order touches neither B cash nor B equity.
    b_sum = pt.get_portfolio_summary(user_id="user-b")
    assert b_sum["cash"] == pytest.approx(settings.INITIAL_CAPITAL)
    assert b_sum["pending_orders_count"] == 1
    # B cannot fill A's order through B's account.
    assert "error" in pt.fill_order(rA["order_id"], user_id="user-b")
    assert len(pt.get_positions(user_id="user-a")) == 1


def test_phase4_cross_user_close_and_cancel_isolation():
    """A user cannot close another user's open position or cancel their order."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    rA = pt.place_order("RELIANCE", "LONG", 4, 2450.0, 2420.0, 2500.0, user_id="user-a")
    pt.fill_order(rA["order_id"], user_id="user-a")
    pB = pt.place_order("TCS", "LONG", 2, 3200.0, 3180.0, 3250.0, user_id="user-b")

    assert "error" in pt.close_position(rA["order_id"], 2600.0, user_id="user-b")
    assert len(pt.get_positions(user_id="user-a")) == 1

    assert "error" in pt.cancel_order(pB["order_id"], user_id="user-a")
    assert len(pt.get_pending_orders(user_id="user-b")) == 1


def test_phase4_closed_trades_isolated_per_user():
    """Closed trades accumulate in the owning user's account only."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    rA = pt.place_order("RELIANCE", "LONG", 4, 2450.0, 2420.0, 2500.0, user_id="user-a")
    pt.fill_order(rA["order_id"], user_id="user-a")
    pt.close_position(rA["order_id"], 2500.0, user_id="user-a")

    assert len(pt.get_trade_history(user_id="user-a")) == 1
    assert pt.get_trade_history(user_id="user-b") == []
    assert pt.get_closed_trades(user_id="user-a")[0]["symbol"] == "RELIANCE"
    assert pt.get_closed_trades(user_id="user-b") == []


def test_phase4_no_user_facade_backward_compatible():
    """The no-user engine facade still behaves like the original single-account
    engine: reads aggregate, writes target the default account."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    assert pt.get_portfolio_summary()["cash"] == pytest.approx(settings.INITIAL_CAPITAL)
    assert pt.get_positions() == []
    assert pt.get_pending_orders() == []
    assert pt.get_trade_history() == []

    r = pt.place_order("RELIANCE", "LONG", 4, 2450.0, 2420.0, 2500.0)
    pt.fill_order(r["order_id"])
    assert pt.get_portfolio_summary()["positions_count"] == 1
    assert len(pt.get_positions()) == 1
    assert pt.get_positions()[0]["symbol"] == "RELIANCE"


def test_phase4_runtime_long_target_partial():
    """LONG position auto-partially-exits at target_1 via the runtime path."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 4, 2450.0, 2420.0, 2500.0, user_id="user-a")
    pt.fill_order(r["order_id"], user_id="user-a")

    pt.update_prices({"RELIANCE": 2499.0}, user_id="user-a")
    assert len(pt.get_positions(user_id="user-a")) == 1
    exits = pt.check_stops({"RELIANCE": 2500.0}, user_id="user-a")
    assert len(exits) == 1
    assert exits[0]["trade"]["exit_price"] == 2500.0
    assert exits[0]["trade"]["result"] == "WIN"
    assert exits[0]["exit_reason"] == "T1_PARTIAL"
    # T1 partial = 50% of 4 → 2 shares at +50 = realized +100.
    assert exits[0]["pnl"] == pytest.approx(100.0)
    pos = pt.get_positions(user_id="user-a")
    assert len(pos) == 1
    assert pos[0]["quantity"] == 2
    assert pos[0]["exit_stage"] == "T1_EXECUTED"
    assert pos[0]["realized_pnl"] == pytest.approx(100.0)
    s = pt.get_portfolio_summary(user_id="user-a")
    assert s["total_pnl"] == pytest.approx(100.0)
    # Remaining 2 shares marked at the T1 price → unrealized 100.
    assert s["unrealized_pnl"] == pytest.approx(100.0)
    # Remainder finally closes manually → full round-trip profit realized.
    pt.close_position(pos[0]["id"], 2500.0, user_id="user-a")
    s = pt.get_portfolio_summary(user_id="user-a")
    assert s["positions_count"] == 0
    assert s["total_pnl"] == pytest.approx(200.0)


def test_phase4_runtime_long_sl_autoclose():
    """LONG position auto-closes at stop-loss via the runtime monitor path."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 4, 2450.0, 2420.0, 2500.0, user_id="user-a")
    pt.fill_order(r["order_id"], user_id="user-a")

    exits = pt.check_stops({"RELIANCE": 2420.0}, user_id="user-a")
    assert len(exits) == 1
    assert exits[0]["trade"]["exit_price"] == 2420.0
    assert exits[0]["trade"]["result"] == "LOSS"
    assert exits[0]["pnl"] == pytest.approx(-120.0)
    assert pt.get_positions(user_id="user-a") == []


def test_phase4_runtime_short_target_partial():
    """SHORT position auto-partially-exits at target_1 via the runtime path."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SHORT", 2, 3200.0, 3230.0, 3160.0, user_id="user-a")
    pt.fill_order(r["order_id"], user_id="user-a")

    exits = pt.check_stops({"TCS": 3160.0}, user_id="user-a")
    assert len(exits) == 1
    assert exits[0]["trade"]["exit_price"] == 3160.0
    assert exits[0]["trade"]["result"] == "WIN"
    assert exits[0]["exit_reason"] == "T1_PARTIAL"
    # T1 partial = 50% of 2 → 1 share at +40 = realized +40.
    assert exits[0]["pnl"] == pytest.approx(40.0)
    pos = pt.get_positions(user_id="user-a")
    assert len(pos) == 1
    assert pos[0]["quantity"] == 1
    assert pos[0]["exit_stage"] == "T1_EXECUTED"
    s = pt.get_portfolio_summary(user_id="user-a")
    assert s["total_pnl"] == pytest.approx(40.0)


def test_phase4_runtime_short_sl_autoclose():
    """SHORT position auto-closes at stop-loss via the runtime monitor path."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SHORT", 2, 3200.0, 3230.0, 3160.0, user_id="user-a")
    pt.fill_order(r["order_id"], user_id="user-a")

    exits = pt.check_stops({"TCS": 3230.0}, user_id="user-a")
    assert len(exits) == 1
    assert exits[0]["trade"]["exit_price"] == 3230.0
    assert exits[0]["trade"]["result"] == "LOSS"
    assert exits[0]["pnl"] == pytest.approx(-60.0)
    assert pt.get_positions(user_id="user-a") == []


def test_phase4_runtime_gap_exits_at_observed_price():
    """Gap through SL/target exits at the observed monitor price, not the SL."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 2, 2450.0, 2420.0, 2500.0, user_id="user-a")
    pt.fill_order(r["order_id"], user_id="user-a")
    exits = pt.check_stops({"RELIANCE": 2400.0}, user_id="user-a")
    assert len(exits) == 1
    assert exits[0]["trade"]["exit_price"] == 2400.0
    assert exits[0]["pnl"] == pytest.approx(-100.0)

    r2 = pt.place_order("TCS", "SHORT", 1, 3200.0, 3230.0, 3160.0, user_id="user-a")
    pt.fill_order(r2["order_id"], user_id="user-a")
    exits2 = pt.check_stops({"TCS": 3140.0}, user_id="user-a")
    assert len(exits2) == 1
    assert exits2[0]["trade"]["exit_price"] == 3140.0
    assert exits2[0]["exit_reason"] == "T1_PARTIAL"
    # T1 partial = 50% of 1 share → 0.5 at +60 = +30; remainder stays open.
    assert exits2[0]["pnl"] == pytest.approx(30.0)
    remaining = pt.get_positions(user_id="user-a")
    assert len(remaining) == 1 and remaining[0]["quantity"] == pytest.approx(0.5)


def test_phase4_update_prices_marks_before_autoclose():
    """update_prices marks current_price/unrealized_pnl before check_stops runs."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 4, 2450.0, 2420.0, 2500.0, user_id="user-a")
    pt.fill_order(r["order_id"], user_id="user-a")

    pt.update_prices({"RELIANCE": 2470.0}, user_id="user-a")
    pos = pt.get_positions(user_id="user-a")[0]
    assert pos["current_price"] == 2470.0
    assert pos["unrealized_pnl"] == pytest.approx(80.0)
    assert pt.get_portfolio_summary(user_id="user-a")["unrealized_pnl"] == pytest.approx(80.0)

    exits = pt.check_stops({"RELIANCE": 2500.0}, user_id="user-a")
    assert len(exits) == 1
    assert exits[0]["exit_reason"] == "T1_PARTIAL"
    assert exits[0]["pnl"] == pytest.approx(100.0)
    s = pt.get_portfolio_summary(user_id="user-a")
    # After the T1 partial the remaining 2 shares are marked at 2500 → unrealized
    # 100 while the position stays open, so total_value keeps INIT + 200.
    assert s["realized_pnl"] == pytest.approx(100.0)
    assert s["unrealized_pnl"] == pytest.approx(100.0)
    assert s["total_value"] == pytest.approx(settings.INITIAL_CAPITAL + 200.0)


def test_phase4_autoclose_single_position_only():
    """One price crossing only its own symbol; no other symbol is closed."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r1 = pt.place_order("RELIANCE", "LONG", 2, 2450.0, 2420.0, 2500.0, user_id="user-a")
    r2 = pt.place_order("TCS", "LONG", 1, 3200.0, 3180.0, 3250.0, user_id="user-a")
    pt.fill_order(r1["order_id"], user_id="user-a")
    pt.fill_order(r2["order_id"], user_id="user-a")

    exits = pt.check_stops({"RELIANCE": 2500.0, "TCS": 3249.0}, user_id="user-a")
    assert len(exits) == 1
    assert exits[0]["trade"]["symbol"] == "RELIANCE"
    assert exits[0]["exit_reason"] == "T1_PARTIAL"
    # RELIANCE half-exited → still open (remainder) alongside the untouched TCS.
    remaining = pt.get_positions(user_id="user-a")
    assert len(remaining) == 2
    reli = next(p for p in remaining if p["symbol"] == "RELIANCE")
    tcs = next(p for p in remaining if p["symbol"] == "TCS")
    assert reli["quantity"] == 1 and reli["exit_stage"] == "T1_EXECUTED"
    assert tcs["quantity"] == 1


def test_phase4_monitor_close_records_user_id():
    """Auto-closed trades carry the owning user's id for later persistence."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 4, 2450.0, 2420.0, 2500.0, user_id="user-a")
    pt.fill_order(r["order_id"], user_id="user-a")
    exits = pt.check_stops({"RELIANCE": 2500.0}, user_id="user-a")
    assert exits[0]["trade"]["user_id"] == "user-a"


def test_phase4_account_close_inherits_user_id():
    """An account-scoped close without an explicit user inherits the account."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 4, 2450.0, 2420.0, 2500.0, user_id="user-a")
    pt.fill_order(r["order_id"], user_id="user-a")
    acc = pt.accounts["user-a"]
    res = acc.close_position(r["order_id"], 2500.0)
    assert res["trade"]["user_id"] == "user-a"


def test_phase4_monitor_cycle_autocloses_and_finalizes():
    """The background monitor cycle fetches quotes, marks prices, closes at SL/T
    and hands the closed trade to the shared finalizer with the right user."""
    import asyncio
    from app.services.paper_monitor import _run_monitor_cycle
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 4, 2450.0, 2420.0, 2500.0, user_id="user-a")
    pt.fill_order(r["order_id"], user_id="user-a")

    async def fake_quote(symbol):
        assert symbol == "RELIANCE"
        return {"price": 2500.0}

    finalized = []

    async def fake_finalize(result, user_id):
        finalized.append((result, user_id))

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_run_monitor_cycle(pt, fake_quote, fake_finalize))
    loop.close()

    assert len(finalized) == 1
    result, uid = finalized[0]
    assert uid == "user-a"
    assert result["trade"]["exit_price"] == 2500.0
    assert result["trade"]["result"] == "WIN"
    # Target-1 hit now finalizes the controlled T1 partial (50%); the remainder
    # stays open under the protective/trailing stop.
    assert result["exit_reason"] == "T1_PARTIAL"
    assert result["partial"] is True
    remaining = pt.get_positions(user_id="user-a")
    assert len(remaining) == 1
    assert remaining[0]["quantity"] == 2
    assert remaining[0]["exit_stage"] == "T1_EXECUTED"


def test_phase4_monitor_skips_accounts_without_positions():
    """No quote fetches / finalizes happen for accounts with no open positions."""
    import asyncio
    from app.services.paper_monitor import _run_monitor_cycle
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    pt.get_portfolio_summary(user_id="user-a")  # provision but no positions

    called = []

    async def fake_quote(symbol):
        called.append(symbol)
        return {"price": 100.0}

    finalized = []

    async def fake_finalize(result, user_id):
        finalized.append(result)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_run_monitor_cycle(pt, fake_quote, fake_finalize))
    loop.close()

    assert called == []
    assert finalized == []


def test_phase4_monitor_resilient_to_failed_quote():
    """A failed quote for one symbol never blocks other symbols from closing."""
    import asyncio
    from app.services.paper_monitor import _run_monitor_cycle
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r1 = pt.place_order("RELIANCE", "LONG", 2, 2450.0, 2420.0, 2500.0, user_id="user-a")
    r2 = pt.place_order("TCS", "LONG", 1, 3200.0, 3180.0, 3250.0, user_id="user-a")
    pt.fill_order(r1["order_id"], user_id="user-a")
    pt.fill_order(r2["order_id"], user_id="user-a")

    async def fake_quote(symbol):
        if symbol == "RELIANCE":
            raise RuntimeError("provider down for RELIANCE")
        return {"price": 3250.0}

    finalized = []

    async def fake_finalize(result, user_id):
        finalized.append(result)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_run_monitor_cycle(pt, fake_quote, fake_finalize))
    loop.close()

    assert len(finalized) == 1
    assert finalized[0]["trade"]["symbol"] == "TCS"
    assert finalized[0]["exit_reason"] == "T1_PARTIAL"
    remaining = pt.get_positions(user_id="user-a")
    assert len(remaining) == 2
    by_sym = {p["symbol"]: p for p in remaining}
    # TCS half-exited (0.5 of 1 share left); RELIANCE quote failed → untouched.
    assert by_sym["TCS"]["quantity"] == pytest.approx(0.5)
    assert by_sym["RELIANCE"]["quantity"] == 2


def test_phase4_monitor_bad_price_skipped():
    """Invalid (non-positive / missing) prices are ignored without closing."""
    import asyncio
    from app.services.paper_monitor import _run_monitor_cycle
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 4, 2450.0, 2420.0, 2500.0, user_id="user-a")
    pt.fill_order(r["order_id"], user_id="user-a")

    async def fake_quote(symbol):
        return {"price": 0.0}  # invalid price -> ignored

    finalized = []

    async def fake_finalize(result, user_id):
        finalized.append(result)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_run_monitor_cycle(pt, fake_quote, fake_finalize))
    loop.close()

    assert finalized == []
    assert len(pt.get_positions(user_id="user-a")) == 1