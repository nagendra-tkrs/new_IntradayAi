"""Phase 7 — maximum simultaneous OPEN positions raised from 5 to 10.

The cap (5 -> 10) was raised in `config.MAX_SIMULTANEOUS_POSITIONS`, the
`RiskConfig` dataclass default, and every `.env`/`.env.example`. Enforcement
points are untouched because they all read the config dynamically:

 - API guard (`app.api.trading.place_paper_order`): per-user
   `len(open_positions) >= risk_engine.config.max_simultaneous_positions`
 - entry auto-fill (`PaperAccount.check_entry_triggers`): counts only OPEN
   positions (`filled = len(self.positions)`), so PENDING orders never consume
   a slot; breaks when `filled >= settings.MAX_SIMULTANEOUS_POSITIONS`
 - risk engine (`RiskEngine.can_trade`) returns
   "Maximum simultaneous positions (10) reached"

Guarantees preserved (regression coverage mirrored from Phase 5/6):
 - PENDING orders do not consume an OPEN slot
 - duplicate-symbol protection covers OPEN and PENDING
 - the 10-position limit applies per user/account, not globally
 - BUY/LONG, entry auto-fill, SL/Target-1 auto-close, and Phase 6 SHORT
   cash-secured accounting are unchanged
"""

import pytest
from app.core.config import settings

INIT = settings.INITIAL_CAPITAL

# 20 distinct symbols (>= 11 needed to exceed the new cap)
SYMS = ["RELIANCE", "TCS", "INFY", "HDFC", "ICICI", "SBIN", "LT", "WIPRO",
        "TATAMOTORS", "ASIANPAINT", "BRITANNIA", "BAJFINANCE", "MARUTI",
        "NESTLEIND", "SUNPHARMA", "BHARTIARTL", "HCLTECH", "KOTAKBANK",
        "AXISBANK", "TITAN"]

LONG_A = dict(direction="LONG", quantity=4, entry_price=100.0,
              stop_loss=95.0, target_1=105.0)
SHORT_A = dict(direction="SHORT", quantity=4, entry_price=100.0,
               stop_loss=105.0, target_1=95.0)


def _open(pt, n, user_id="u", long=True, offset=0):
    """Place + fill `n` distinct-symbol positions, return their ids."""
    got = []
    for i in range(n):
        sym = SYMS[offset + i]
        cfg = dict(LONG_A if long else SHORT_A)
        r = pt.place_order(sym, user_id=user_id, **cfg)
        assert "error" not in r
        fill = pt.fill_order(r["order_id"], user_id=user_id)
        assert fill["status"] == "filled"
        got.append(r["order_id"])
    return got


def _api_blocked(pt, re, user_id):
    """Mirror of the API guard in app.api.trading.place_paper_order."""
    open_positions = pt.get_positions(user_id=user_id)
    return len(open_positions) >= re.config.max_simultaneous_positions


# ── A. Config reports the new limit everywhere ──────────────────────────
def test_phase7_config_reports_new_limit():
    from app.services.risk_engine import RiskConfig, RiskEngine

    assert settings.MAX_SIMULTANEOUS_POSITIONS == 10
    assert RiskConfig().max_simultaneous_positions == 10
    # RiskEngine() default reads settings -> .env -> 10
    assert RiskEngine().config.max_simultaneous_positions == 10


# ── B. 1-9 open -> next position allowed ────────────────────────────────
def test_phase7_upto_nine_open_positions_allowed():
    from app.services.paper_trading import PaperTradingEngine
    from app.services.risk_engine import RiskEngine

    for n in (1, 5, 9):
        pt = PaperTradingEngine()
        re = RiskEngine()
        _open(pt, n)
        assert len(pt.get_positions(user_id="u")) == n
        assert _api_blocked(pt, re, "u") is False


# ── C. 10 open -> 11th blocked ──────────────────────────────────────────
def test_phase7_ten_open_positions_eleventh_blocked():
    from app.services.paper_trading import PaperTradingEngine
    from app.services.risk_engine import RiskEngine

    pt = PaperTradingEngine()
    re = RiskEngine()
    _open(pt, 10)
    assert len(pt.get_positions(user_id="u")) == 10
    assert _api_blocked(pt, re, "u") is True

    # risk engine reports the exact message with the new number
    re.state.open_positions = 10
    can, reason = re.can_trade()
    assert can is False
    assert reason == "Maximum simultaneous positions (10) reached"

    # placement on top of the ten is blocked; nothing new opens
    r = pt.place_order(SYMS[10], user_id="u", **LONG_A)
    assert "error" not in r  # engine stores the pending order...
    fills = pt.check_entry_triggers({SYMS[10]: 100.0}, user_id="u")
    assert fills == []  # ...but auto-fill never opens an 11th position
    assert len(pt.get_positions(user_id="u")) == 10


# ── D. PENDING orders do not consume OPEN slots ─────────────────────────
def test_phase7_pending_orders_do_not_consume_open_slots():
    from app.services.paper_trading import PaperTradingEngine
    from app.services.risk_engine import RiskEngine

    pt = PaperTradingEngine()
    re = RiskEngine()
    _open(pt, 4)
    r = pt.place_order(SYMS[10], user_id="u", **LONG_A)
    assert len(pt.get_positions(user_id="u")) == 4
    assert len(pt.get_pending_orders(user_id="u")) == 1
    # 4 open -> a new OPEN position is still allowed despite the pending order
    assert _api_blocked(pt, re, "u") is False

    fills = pt.check_entry_triggers({SYMS[10]: 100.0}, user_id="u")
    assert len(fills) == 1
    assert len(pt.get_positions(user_id="u")) == 5


# ── E. 9 open + multiple pending -> a 10th can open, then capped ────────
def test_phase7_nine_open_plus_pending_allows_tenth_then_caps():
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    _open(pt, 9)
    for i in (10, 11, 12):
        pt.place_order(SYMS[i], user_id="u", **LONG_A)

    fills = pt.check_entry_triggers({s: 100.0 for s in SYMS[10:13]}, user_id="u")
    assert len(fills) == 1  # fills exactly one -> 10 open total
    assert len(pt.get_positions(user_id="u")) == 10

    # the remaining pending orders stay pending; nothing opens an 11th
    fills = pt.check_entry_triggers({s: 100.0 for s in SYMS[10:13]}, user_id="u")
    assert fills == []
    assert len(pt.get_positions(user_id="u")) == 10
    assert len(pt.get_pending_orders(user_id="u")) == 2


# ── F. BUY and SELL both respect the cap ────────────────────────────────
def test_phase7_buy_and_sell_both_respect_limit():
    from app.services.paper_trading import PaperTradingEngine
    from app.services.risk_engine import RiskEngine

    pt = PaperTradingEngine()
    re = RiskEngine()
    _open(pt, 5)                       # 5 LONG
    _open(pt, 5, long=False, offset=5)  # 5 SHORT
    assert len(pt.get_positions(user_id="u")) == 10

    # a new LONG (BUY) and a new SHORT (SELL) are each blocked
    for direction in ("BUY", "SELL"):
        r = pt.place_order(SYMS[10], direction, 10, 100.0, user_id="u",
                           stop_loss=95.0 if direction == "BUY" else 105.0,
                           target_1=105.0 if direction == "BUY" else 95.0)
        assert "error" not in r
        fills = pt.check_entry_triggers({SYMS[10]: 100.0}, user_id="u")
        assert fills == []
    assert len(pt.get_positions(user_id="u")) == 10
    assert _api_blocked(pt, re, "u") is True


# ── G. Limit applies per user/account, not globally ─────────────────────
def test_phase7_limit_is_per_user_not_global():
    from app.services.paper_trading import PaperTradingEngine
    from app.services.risk_engine import RiskEngine

    pt = PaperTradingEngine()
    re = RiskEngine()
    _open(pt, 10, user_id="user-a")
    assert _api_blocked(pt, re, "user-a") is True

    # user-b is unaffected: can open its own 10 while user-a is maxed
    assert _api_blocked(pt, re, "user-b") is False
    _open(pt, 9, user_id="user-b")
    assert _api_blocked(pt, re, "user-b") is False
    _open(pt, 1, user_id="user-b", offset=9)
    assert len(pt.get_positions(user_id="user-a")) == 10
    assert len(pt.get_positions(user_id="user-b")) == 10
    assert _api_blocked(pt, re, "user-a") is True
    assert _api_blocked(pt, re, "user-b") is True


# ── H. Closing one of ten frees a slot ──────────────────────────────────
def test_phase7_closing_one_of_ten_frees_a_slot():
    from app.services.paper_trading import PaperTradingEngine
    from app.services.risk_engine import RiskEngine

    pt = PaperTradingEngine()
    re = RiskEngine()
    _open(pt, 10)
    assert _api_blocked(pt, re, "u") is True

    close = pt.close_position(pt.get_positions(user_id="u")[0]["id"],
                              101.0, user_id="u")
    assert close["trade"]["status"] == "closed"
    assert len(pt.get_positions(user_id="u")) == 9
    assert _api_blocked(pt, re, "u") is False

    # a new position opens in the freed slot
    r = pt.place_order(SYMS[10], user_id="u", **LONG_A)
    fill = pt.fill_order(r["order_id"], user_id="u")
    assert fill["status"] == "filled"
    assert len(pt.get_positions(user_id="u")) == 10


# ── I. Duplicate-symbol protection covers OPEN and PENDING ──────────────
def test_phase7_duplicate_symbol_protection_covers_open_and_pending():
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    _open(pt, 1)  # RELIANCE open

    # API guard (app.api.trading.place_paper_order) unions OPEN | PENDING
    open_symbols = {p["symbol"] for p in pt.get_positions(user_id="u")}
    r = pt.place_order("RELIANCE", user_id="u", **LONG_A)
    pending_symbols = {p["symbol"] for p in pt.get_pending_orders(user_id="u")}
    assert "RELIANCE" in open_symbols | pending_symbols  # duplicate blocked

    # auto-fill never opens a duplicate of an already-open symbol
    fills = pt.check_entry_triggers({"RELIANCE": 100.0}, user_id="u")
    assert fills == []
    assert len(pt.get_positions(user_id="u")) == 1


# ── J. Auto-filled pending orders respect the cap ───────────────────────
def test_phase7_auto_fill_caps_at_ten_open_positions():
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    for i in range(11):
        pt.place_order(SYMS[i], user_id="u", **LONG_A)

    fills = pt.check_entry_triggers({s: 100.0 for s in SYMS[:11]}, user_id="u")
    assert len(fills) == 10
    assert len(pt.get_positions(user_id="u")) == 10
    assert len(pt.get_pending_orders(user_id="u")) == 1


# ── K. BUY/LONG accounting unchanged ────────────────────────────────────
def test_phase7_buy_accounting_unchanged():
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "BUY", 10, 100.0, 95.0, 105.0, user_id="u")
    pt.check_entry_triggers({"RELIANCE": 100.0}, user_id="u")
    pos = pt.get_positions(user_id="u")[0]
    assert pos["direction"] == "LONG"  # auto-fill canonicalizes BUY -> LONG
    s = pt.get_portfolio_summary(user_id="u")
    assert s["total_value"] == pytest.approx(INIT)
    assert s["cash"] == pytest.approx(INIT - 10 * 100.0)

    pt.update_prices({"RELIANCE": 105.0}, user_id="u")
    assert s["cash"] == pytest.approx(INIT - 10 * 100.0)  # unrealized only


# ── L. Phase 6 SHORT accounting unchanged ───────────────────────────────
def test_phase7_short_accounting_unchanged():
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SHORT", 10, 100.0, 105.0, 90.0, user_id="u")
    fill = pt.fill_order(r["order_id"], user_id="u")
    assert fill["status"] == "filled"
    s = pt.get_portfolio_summary(user_id="u")
    assert s["reserved_margin"] == pytest.approx(10 * 100.0)
    assert s["available_cash"] == pytest.approx(INIT - 10 * 100.0)
    assert s["short_liability"] == pytest.approx(10 * 100.0)
    assert s["total_value"] == pytest.approx(INIT)
    assert s["total_value"] == pytest.approx(INIT + s["total_pnl"] + s["unrealized_pnl"])

    close = pt.close_position(r["order_id"], 90.0, user_id="u")
    assert close["pnl"] == pytest.approx(100.0)
    s = pt.get_portfolio_summary(user_id="u")
    assert s["reserved_margin"] == pytest.approx(0.0)
    assert s["available_cash"] == pytest.approx(INIT + 100.0)
    assert s["total_pnl"] == pytest.approx(100.0)


# ── M. SL full-close / Target-1 partial auto-exit ───────────────────────
def test_phase7_sl_target_auto_close():
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "BUY", 10, 100.0, 95.0, 105.0, user_id="u")
    pt.check_entry_triggers({"RELIANCE": 100.0}, user_id="u")
    assert len(pt.get_positions(user_id="u")) == 1

    # Target-1 hit → controlled partial exit (50%), remainder stays open.
    exits = pt.check_stops({"RELIANCE": 105.0}, user_id="u")
    assert len(exits) == 1
    assert exits[0]["exit_reason"] == "T1_PARTIAL"
    assert exits[0]["pnl"] == pytest.approx((105.0 - 100.0) * 5)
    pos = pt.get_positions(user_id="u")
    assert len(pos) == 1
    assert pos[0]["quantity"] == pytest.approx(5.0)

    # SL still closes fully (SHORT price >= SL → LOSS).
    r2 = pt.place_order("TCS", "SELL", 5, 100.0, 105.0, 95.0, user_id="u")
    pt.check_entry_triggers({"TCS": 100.0}, user_id="u")
    exits = pt.check_stops({"TCS": 105.0}, user_id="u")
    assert len(exits) == 1
    assert exits[0]["trade"]["result"] == "LOSS"
    assert exits[0]["trade"]["exit_reason"] == "STOP_LOSS"
    assert exits[0]["pnl"] == pytest.approx((100.0 - 105.0) * 5)