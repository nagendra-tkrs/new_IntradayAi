"""Phase 6 — realistic SHORT/SELL paper-trading accounting (cash-secured model).

Prior behavior credited opening a SHORT's full sale proceeds to raw `cash`, so
the portfolio's cash field was inflated by the short's notional even though
`total_value` was correct. Phase 6 keeps the proceeds in the ledger (to fund
the later buy-back) but exposes clearly-named cash-collateral fields so
available cash and equity are never inflated:

 - reserved_margin : committed capital of EVERY open position (LONG disbursed
   notional AND SHORT 100% cash collateral) plus every pending order's entry
   notional (contract section 7.4)
 - available_cash  : initial + realized PnL − reserved (committed + pending),
   i.e. the true buying power; SHORT proceeds never inflate it
 - short_liability : mark-to-market buy-back cost of every open short

Guarantees preserved:
 - SHORT opening never inflates equity: total_value == initial + realized + unrealized
 - SHORT P&L: realized (entry-exit)*qty at close, unrealized (entry-current)*qty
 - An oversized SHORT (notional > available cash) is rejected and its pending
   order restored, exactly like an oversized LONG (no leveraged/self-funding
   short). 100% cash collateral means no leverage anywhere.
 - BUY/LONG accounting, entry auto-fill, SL/Target monitor, and per-user
   isolation are unchanged (regression tests at the bottom mirror them).
"""

import pytest
from app.core.config import settings

INIT = settings.INITIAL_CAPITAL


def _eq(pt, field, value, user_id=None):
    s = pt.get_portfolio_summary(user_id=user_id)
    assert s[field] == pytest.approx(value), f"{field}={s[field]} != {value}"


def _equity_invariant(pt, user_id=None):
    s = pt.get_portfolio_summary(user_id=user_id)
    assert s["total_value"] == pytest.approx(INIT + s["total_pnl"] + s["unrealized_pnl"])


# ── A. Short open: no cash/equity inflation ─────────────────────────────
def test_phase6_short_open_does_not_inflate_cash_or_equity():
    """Opening a SHORT credits proceeds to raw cash but commits that exact
    notional as collateral, so available cash drops by the margin (proceeds
    never inflate buying power) while total equity stays at initial."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SHORT", 10, 100.0, 105.0, 90.0)
    fill = pt.fill_order(r["order_id"])
    assert fill["status"] == "filled"
    # proceeds credited to ledger
    assert pt.cash == pytest.approx(INIT + 10 * 100.0)
    # ...but fully committed, so available cash DROPS by the margin (never
    # inflated by short proceeds) and equity stays at initial.
    _eq(pt, "reserved_margin", 10 * 100.0)
    _eq(pt, "available_cash", INIT - 10 * 100.0)
    _eq(pt, "short_liability", 10 * 100.0)
    _eq(pt, "total_value", INIT)
    _eq(pt, "unrealized_pnl", 0.0)
    _eq(pt, "total_pnl", 0.0)
    _equity_invariant(pt)


# ── B. Short profit close ───────────────────────────────────────────────
def test_phase6_short_profit_close_releases_collateral_realizes_pnl():
    """Closing a profitable SHORT releases its collision, pays the buy-back out
    of the ledger, and recognizes (entry-exit)*qty as realized profit."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SHORT", 10, 100.0, 105.0, 90.0)
    pt.fill_order(r["order_id"])
    close = pt.close_position(r["order_id"], 90.0)
    assert close["pnl"] == pytest.approx(100.0)
    assert close["trade"]["result"] == "WIN"
    # collateral released, short gone
    _eq(pt, "reserved_margin", 0.0)
    _eq(pt, "available_cash", INIT + 100.0)
    _eq(pt, "total_pnl", 100.0)
    _eq(pt, "total_value", INIT + 100.0)
    _equity_invariant(pt)


# ── C. Short loss close ─────────────────────────────────────────────────
def test_phase6_short_loss_close_releases_collateral_realizes_pnl():
    """Closing a losing SHORT pays the higher buy-back out of the ledger and
    recognizes (entry-exit)*qty (negative) as realized loss."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SHORT", 10, 100.0, 105.0, 90.0)
    pt.fill_order(r["order_id"])
    close = pt.close_position(r["order_id"], 110.0)
    assert close["pnl"] == pytest.approx(-100.0)
    assert close["trade"]["result"] == "LOSS"
    _eq(pt, "reserved_margin", 0.0)
    _eq(pt, "available_cash", INIT - 100.0)
    _eq(pt, "total_pnl", -100.0)
    _eq(pt, "total_value", INIT - 100.0)
    _equity_invariant(pt)


# ── D. Short unrealized both ways ───────────────────────────────────────
def test_phase6_short_unrealized_pnl_both_directions():
    """Unrealized = (entry-current)*qty while the short is open and equity
    tracks it via the invariant."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SHORT", 10, 100.0, 105.0, 90.0)
    pt.fill_order(r["order_id"])
    pt.update_prices({"TCS": 90.0})   # price falls -> profit
    _eq(pt, "unrealized_pnl", 100.0)
    _eq(pt, "total_value", INIT + 100.0)
    _equity_invariant(pt)
    pt.update_prices({"TCS": 110.0})  # price rises -> loss
    _eq(pt, "unrealized_pnl", -100.0)
    _eq(pt, "total_value", INIT - 100.0)
    _equity_invariant(pt)


# ── E. Short breakeven ──────────────────────────────────────────────────
def test_phase6_short_breakeven_close():
    """Closing at entry price yields zero P&L and no equity change."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SHORT", 10, 100.0, 105.0, 90.0)
    pt.fill_order(r["order_id"])
    close = pt.close_position(r["order_id"], 100.0)
    assert close["pnl"] == pytest.approx(0.0)
    assert close["trade"]["result"] == "BREAKEVEN"
    _eq(pt, "total_pnl", 0.0)
    _eq(pt, "total_value", INIT)
    _equity_invariant(pt)


# ── F. Multiple shorts ──────────────────────────────────────────────────
def test_phase6_multiple_shorts_reserve_independently():
    """Each open short reserves its own notional; total reserved = sum."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r1 = pt.place_order("A", "SHORT", 5, 100.0, 105.0, 90.0)
    r2 = pt.place_order("B", "SHORT", 10, 200.0, 210.0, 185.0)
    pt.fill_order(r1["order_id"])
    pt.fill_order(r2["order_id"])
    reserved = 5 * 100.0 + 10 * 200.0
    _eq(pt, "reserved_margin", reserved)
    # available cash = INIT − sum of committed collateral (short proceeds never
    # inflate buying power; each reservation reduces it 1:1)
    _eq(pt, "available_cash", INIT - reserved)
    _eq(pt, "total_value", INIT)
    _equity_invariant(pt)
    # closing one releases only that short's collateral
    pt.close_position(r1["order_id"], 95.0)
    _eq(pt, "reserved_margin", 10 * 200.0)
    _eq(pt, "available_cash", INIT + (100.0 - 95.0) * 5 - 10 * 200.0)
    _eq(pt, "total_pnl", 25.0)
    _equity_invariant(pt)


# ── G. BUY + SHORT mix ──────────────────────────────────────────────────
def test_phase6_buy_plus_short_mix_equity_invariant():
    """Simultaneous LONG + SHORT keep the equity invariant; short collateral
    reduces available cash but not equity."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    rl = pt.place_order("RL", "LONG", 10, 100.0, 95.0, 110.0)
    pt.fill_order(rl["order_id"])
    rs = pt.place_order("TC", "SHORT", 5, 200.0, 205.0, 190.0)
    pt.fill_order(rs["order_id"])
    # LONG paid 1000 from cash, then SHORT sold 1000 -> cash back to start
    _eq(pt, "cash", INIT)
    # reserved_margin = ALL committed open capital: LONG 1000 + SHORT 1000
    _eq(pt, "reserved_margin", 5 * 200.0 + 10 * 100.0)
    _eq(pt, "available_cash", INIT - 5 * 200.0 - 10 * 100.0)
    _eq(pt, "total_value", INIT)
    _equity_invariant(pt)
    pt.update_prices({"RL": 110.0, "TC": 190.0})
    _eq(pt, "unrealized_pnl", 100.0 + 50.0)
    _eq(pt, "total_value", INIT + 150.0)
    _equity_invariant(pt)


# ── H. Auto-fill SHORT + SL + Target1 ───────────────────────────────────
def test_phase6_auto_fill_short_then_stop_and_target():
    """A pending SELL auto-fills below its entry, then the resulting SHORT is
    monitored by SL (>=) and Target1 (<=) with the collateral accounting."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SELL", 5, 1694.0, 1705.0, 1680.0, user_id="u")
    # market above entry -> no fill
    assert pt.check_entry_triggers({"TCS": 1700.0}, user_id="u") == []
    # market <= entry -> auto-fill at configured entry
    fills = pt.check_entry_triggers({"TCS": 1693.0}, user_id="u")
    assert len(fills) == 1
    assert pt.get_positions(user_id="u")[0]["direction"] == "SHORT"
    reserved = 5 * 1694.0
    _eq(pt, "reserved_margin", reserved, user_id="u")
    _eq(pt, "total_value", INIT, user_id="u")

    # SL: price >= SL (1705) triggers a losing close
    exits = pt.check_stops({"TCS": 1705.0}, user_id="u")
    assert len(exits) == 1
    assert exits[0]["trade"]["result"] == "LOSS"
    _eq(pt, "total_pnl", (1694.0 - 1705.0) * 5, user_id="u")
    _eq(pt, "reserved_margin", 0.0, user_id="u")
    _equity_invariant(pt, user_id="u")

    # new SELL then TARGET1: price <= target (1680) triggers a winning close
    r2 = pt.place_order("TCS", "SELL", 5, 1694.0, 1705.0, 1680.0, user_id="u")
    pt.check_entry_triggers({"TCS": 1690.0}, user_id="u")
    assert len(pt.get_positions(user_id="u")) == 1
    exits = pt.check_stops({"TCS": 1680.0}, user_id="u")
    assert len(exits) == 1
    assert exits[0]["trade"]["result"] == "WIN"
    _eq(pt, "total_pnl", (1694.0 - 1705.0) * 5 + (1694.0 - 1680.0) * 5, user_id="u")
    _eq(pt, "reserved_margin", 0.0, user_id="u")
    _equity_invariant(pt, user_id="u")


# ── I. Insufficient capital for a short ─────────────────────────────────
def test_phase6_oversized_short_rejected_pending_restored():
    """A SHORT whose entry notional exceeds available cash is rejected at
    PLACEMENT (before any state changes); cash, positions and pending order
    are left intact. No leveraged/self-funding short exists."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    cash_before = pt.cash
    # available cash = INIT; notional 1,200,000 > INIT (even though raw cash
    # after a hypothetically-filed short would cover it, buying power must not)
    r = pt.place_order("X", "SHORT", 12_000, 100.0, 105.0, 90.0)
    assert "error" in r
    assert r["error"].startswith("Insufficient available cash")
    assert pt.cash == cash_before
    assert pt.get_positions() == []
    assert pt.get_pending_orders() == []
    assert pt.get_trade_history() == []


# ── J. Isolation: reserved margin isolated per user ─────────────────────
def test_phase6_short_collateral_isolated_per_user():
    """Available cash and reserved margin are computed per user, never leaked
    across accounts."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SHORT", 5, 100.0, 105.0, 90.0, user_id="a")
    pt.fill_order(r["order_id"], user_id="a")
    _eq(pt, "reserved_margin", 500.0, user_id="a")
    _eq(pt, "available_cash", INIT - 500.0, user_id="a")
    # user-b untouched
    _eq(pt, "reserved_margin", 0.0, user_id="b")
    _eq(pt, "available_cash", INIT, user_id="b")
    # aggregate reflects only the real short (each account holds its own INIT)
    _eq(pt, "reserved_margin", 500.0)
    _eq(pt, "available_cash", 2 * INIT - 500.0)


# ── K. Non-persistent engines do not restore state ──────────────────────
def test_phase6_short_accounting_not_persisted_across_instances():
    """A NON-persistent engine keeps P&L/positions only in memory: a fresh
    engine starts cleared (no short collateral, no positions, no pending orders
    carried over). Persistent engines (the API singleton) restore state instead
    — covered by the accounting persistence tests."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SHORT", 5, 100.0, 105.0, 90.0, user_id="a")
    pt.fill_order(r["order_id"], user_id="a")
    assert pt.get_portfolio_summary(user_id="a")["reserved_margin"] == pytest.approx(500.0)

    fresh = PaperTradingEngine()
    _eq(fresh, "reserved_margin", 0.0, user_id="a")
    _eq(fresh, "available_cash", INIT, user_id="a")


# ── L. Equity invariant round trip full LONG+SHORT ──────────────────────
def test_phase6_equity_invariant_full_roundtrip():
    """Remains: total_value == initial + realized + unrealized through a full
    LONG + SHORT round-trip."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    rl = pt.place_order("LX", "LONG", 10, 100.0, 95.0, 110.0)
    pt.fill_order(rl["order_id"])
    _equity_invariant(pt)
    rs = pt.place_order("SX", "SHORT", 10, 100.0, 105.0, 90.0)
    pt.fill_order(rs["order_id"])
    _equity_invariant(pt)
    pt.update_prices({"LX": 110.0, "SX": 90.0})
    _equity_invariant(pt)
    pt.close_position(rl["order_id"], 110.0)
    _equity_invariant(pt)
    pt.close_position(rs["order_id"], 90.0)
    _eq(pt, "total_value", INIT + 200.0)
    _eq(pt, "total_pnl", 200.0)
    _equity_invariant(pt)


# ── M. BUY/LONG accounting regression ───────────────────────────────────
def test_phase6_buy_long_accounting_still_invariant():
    """Opening a LONG debits cash (no reservation); closing credits proceeds;
    realized P&L = (exit-entry)*qty."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    rb = pt.place_order("RELIANCE", "BUY", 10, 100.0, 98.0, 110.0)
    pt.fill_order(rb["order_id"])
    _eq(pt, "cash", INIT - 1000.0)
    # LONG committed capital IS reserved (contract: ALL open positions reserve)
    _eq(pt, "reserved_margin", 1000.0)
    _eq(pt, "available_cash", INIT - 1000.0)
    pt.close_position(rb["order_id"], 110.0)
    _eq(pt, "cash", INIT + 100.0)
    _eq(pt, "total_pnl", 100.0)
    _eq(pt, "total_value", INIT + 100.0)
    _equity_invariant(pt)


# ── N. Auto-fill regression (BUY) ───────────────────────────────────────
def test_phase6_buy_auto_fill_regression():
    """BUY still auto-fills at >= entry and its LONG equity is unchanged."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "BUY", 10, 1694.0, 1685.0, 1710.0, user_id="u")
    assert pt.check_entry_triggers({"RELIANCE": 1693.0}, user_id="u") == []
    fills = pt.check_entry_triggers({"RELIANCE": 1694.0}, user_id="u")
    assert len(fills) == 1
    assert pt.get_positions(user_id="u")[0]["direction"] == "LONG"
    _eq(pt, "total_value", INIT, user_id="u")
    # LONG committed capital is reserved like any open position
    _eq(pt, "reserved_margin", 10 * 1694.0, user_id="u")


# ── O. SL/Target regression (LONG) ──────────────────────────────────────
def test_phase6_long_sl_target_regression():
    """LONG SL (price <=) and Target1 (price >=) monitoring unchanged."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 100.0, 95.0, 110.0)
    pt.fill_order(r["order_id"])
    exits = pt.check_stops({"RELIANCE": 95.0})
    assert len(exits) == 1 and exits[0]["trade"]["result"] == "LOSS"
    _eq(pt, "total_pnl", -50.0)
    r2 = pt.place_order("RELIANCE", "LONG", 10, 100.0, 95.0, 110.0)
    pt.fill_order(r2["order_id"])
    exits = pt.check_stops({"RELIANCE": 110.0})
    assert len(exits) == 1 and exits[0]["trade"]["result"] == "WIN"
    _eq(pt, "total_pnl", -50.0 + 100.0)


# ── P. Pending orders reserve committed notional ─────────────────────────
def test_phase6_pending_orders_reserve_no_capital():
    """Pending (unfilled) orders do not consume cash or change equity, but each
    reserves its entry notional in reserved_margin (committed capital)."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    rl = pt.place_order("LX", "LONG", 500, 100.0, 95.0, 110.0)
    rs = pt.place_order("SX", "SHORT", 500, 100.0, 105.0, 90.0)
    _eq(pt, "reserved_margin", 100_000.0)
    _eq(pt, "available_cash", INIT - 100_000.0)
    _eq(pt, "total_value", INIT)
    assert pt.get_pending_orders() and rl and rs


# ── Q. SELL alias mirrors SHORT ─────────────────────────────────────────
def test_phase6_sell_alias_mirrors_short():
    """SELL uses identical cash-secured accounting (manual fills retain the raw
    SELL label while still consuming the short code path)."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SELL", 10, 100.0, 105.0, 90.0)
    fill = pt.fill_order(r["order_id"])
    assert fill["status"] == "filled"
    assert pt.get_positions()[0]["direction"] in ("SELL", "SHORT")
    _eq(pt, "reserved_margin", 1000.0)
    _eq(pt, "available_cash", INIT - 1000.0)
    _eq(pt, "total_value", INIT)
    pt.close_position(r["order_id"], 90.0)
    _eq(pt, "total_pnl", 100.0)
    _eq(pt, "total_value", INIT + 100.0)


# ── R. Collateral frees spendable capital for a subsequent LONG ─────────
def test_phase6_collateral_frees_available_cash_after_close():
    """After a short closes, its collateral becomes spendable for a LONG,
    proving available_cash (not raw cash) is the real spendable figure."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    rs = pt.place_order("S", "SHORT", 9_000, 100.0, 105.0, 90.0)  # notional 900,000
    pt.fill_order(rs["order_id"])
    _eq(pt, "available_cash", INIT - 900_000.0)
    _eq(pt, "reserved_margin", 900_000.0)
    pt.close_position(rs["order_id"], 90.0)  # win 90,000
    _eq(pt, "available_cash", INIT + 90_000.0)
    # A new LONG sized up to available cash now fits
    rl = pt.place_order("L", "LONG", 900, 1_000.0, 990.0, 1010.0)  # 900,000
    fill = pt.fill_order(rl["order_id"])
    assert fill["status"] == "filled"
    _eq(pt, "available_cash", (INIT + 90_000.0) - 900_000.0)
    _eq(pt, "reserved_margin", 900_000.0)
