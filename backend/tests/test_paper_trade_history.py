"""Paper Trading — date-wise Trade History tests.

The Trade History page groups realized paper trades by the IST date on which
each trade was CLOSED (its exit timestamp), showing per-date trade/P&L
aggregates plus the individual trades. This module locks in the read-only
aggregation/grouping contract used by ``/api/paper/trades``:

  - grouping happens on the authoritative exit timestamp (IST calendar date),
    so cross-midnight trades are assigned to the date they were realized
  - the authoritative realized ``pnl`` field is reused verbatim — never
    recomputed from entry/exit prices
  - zero-P&L trades are counted in the total but in neither profit nor loss
  - open positions (status != "closed") never appear in realized history
  - dates render newest-first and All Dates aggregates equal the sum of the
    date groups
  - the existing flat ``trades`` response shape is preserved

No accounting, execution, or stored records are touched — pure reporting.
"""
from datetime import date

from app.api.trading import (
    _aggregate_trades,
    _build_date_groups,
    _build_trade_history_response,
    _exit_ist_date,
    _parse_filter_date,
)


def _trade(**overrides):
    """A serialized ledger trade dict (the shape ``_trade_rows_to_dicts``
    returns), with realistic defaults that can be overridden per test."""
    base = {
        "id": "t1",
        "symbol": "RELIANCE",
        "direction": "LONG",
        "entry_price": 100.0,
        "exit_price": 105.0,
        "quantity": 10,
        "stop_loss": 95.0,
        "target_1": 115.0,
        "target_2": 120.0,
        "entry_time": "2026-09-24 10:35:00",
        "exit_time": "2026-09-25 09:35:00",
        "pnl": 50.0,
        "result": "WIN",
        "status": "closed",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Phase 15 Test 1 — same-date grouping / aggregation
# ---------------------------------------------------------------------------
def test_same_date_grouping():
    """Trades on the same date are grouped together with correct aggregates."""
    trades = [
        _trade(id="a", pnl=500.0, exit_time="2026-09-25 10:00:00"),
        _trade(id="b", pnl=-200.0, exit_time="2026-09-25 11:00:00"),
        _trade(id="c", pnl=300.0, exit_time="2026-09-24 15:00:00"),
    ]
    groups = _build_date_groups(trades)
    assert [g["date"] for g in groups] == ["2026-09-25", "2026-09-24"]

    g25 = groups[0]
    assert g25["trade_count"] == 2
    assert g25["profitable_trade_count"] == 1
    assert g25["losing_trade_count"] == 1
    assert g25["total_profit"] == 500.0
    assert g25["total_loss"] == 200.0
    assert g25["net_pnl"] == 300.0
    assert [t["id"] for t in g25["trades"]] == ["a", "b"]

    g24 = groups[1]
    assert g24["trade_count"] == 1
    assert g24["total_profit"] == 300.0
    assert g24["total_loss"] == 0.0
    assert g24["net_pnl"] == 300.0


# ---------------------------------------------------------------------------
# Phase 15 Test 2 — cross-midnight trade assigned to close date
# ---------------------------------------------------------------------------
def test_cross_midnight_trade_groups_by_close_date():
    """A trade entered on 24 Sep and exited on 25 Sep is reported on 25 Sep."""
    trade = _trade(id="cm", entry_time="2026-09-24 15:20:00",
                   exit_time="2026-09-25 09:35:00", pnl=250.0)
    groups = _build_date_groups([trade])
    assert len(groups) == 1
    assert groups[0]["date"] == "2026-09-25"
    assert groups[0]["trades"][0]["id"] == "cm"
    assert groups[0]["trades"][0]["entry_time"] == "2026-09-24 15:20:00"


# ---------------------------------------------------------------------------
# Phase 15 Tests 3 & 4 — authoritative P&L displayed unchanged (long/short)
# ---------------------------------------------------------------------------
def test_long_pnl_displayed_unchanged():
    """The ledger's authoritative realized P&L passes through verbatim."""
    t = _trade(direction="LONG", entry_price=1450.5, exit_price=1462.0,
               quantity=10, pnl=115.0)
    day_trade = _build_date_groups([t])[0]["trades"][0]
    assert day_trade["pnl"] == 115.0
    assert day_trade["direction"] == "LONG"
    assert day_trade["entry_price"] == 1450.5
    assert day_trade["exit_price"] == 1462.0
    assert day_trade["quantity"] == 10


def test_short_pnl_displayed_unchanged():
    """Short realized P&L is displayed exactly as stored by the engine."""
    t = _trade(direction="SHORT", entry_price=200.0, exit_price=195.0,
               quantity=5, pnl=25.0)
    day_trade = _build_date_groups([t])[0]["trades"][0]
    assert day_trade["pnl"] == 25.0
    assert day_trade["direction"] == "SHORT"
    aggregate = _aggregate_trades([t])
    assert aggregate["total_profit"] == 25.0
    assert aggregate["total_loss"] == 0.0


# ---------------------------------------------------------------------------
# Phase 15 Test 5 — winning and losing trades separated
# ---------------------------------------------------------------------------
def test_winning_and_losing_trades_separated():
    trades = [
        _trade(id="w1", pnl=80.0),
        _trade(id="w2", pnl=20.0),
        _trade(id="l1", pnl=-30.0),
        _trade(id="l2", pnl=-10.0),
    ]
    s = _aggregate_trades(trades)
    assert s["total_trades"] == 4
    assert s["profitable_trade_count"] == 2
    assert s["losing_trade_count"] == 2
    assert s["total_profit"] == 100.0
    assert s["total_loss"] == 40.0
    assert s["net_pnl"] == 60.0


# ---------------------------------------------------------------------------
# Phase 15 Test 6 — zero P&L: counted as a trade, neither profit nor loss
# ---------------------------------------------------------------------------
def test_zero_pnl_is_neither_profit_nor_loss():
    trades = [
        _trade(id="z", pnl=0.0),
        _trade(id="w", pnl=50.0),
        _trade(id="l", pnl=-25.0),
    ]
    s = _aggregate_trades(trades)
    assert s["total_trades"] == 3
    assert s["profitable_trade_count"] == 1
    assert s["losing_trade_count"] == 1
    assert s["total_profit"] == 50.0
    assert s["total_loss"] == 25.0
    assert s["net_pnl"] == 25.0
    # BREAKEVEN never inflates the profit or loss buckets
    assert any(t["id"] == "z" for g in _build_date_groups(trades) for t in g["trades"])


# ---------------------------------------------------------------------------
# Phase 15 Test 7 — open positions excluded from realized history
# ---------------------------------------------------------------------------
def test_open_positions_excluded():
    open_pos = _trade(id="open1", pnl=999.0, status="open")
    closed = _trade(id="c1", pnl=100.0)
    s = _aggregate_trades([open_pos, closed])
    assert s["total_trades"] == 1
    assert s["net_pnl"] == 100.0
    groups = _build_date_groups([open_pos, closed])
    assert len(groups) == 1
    assert [t["id"] for t in groups[0]["trades"]] == ["c1"]


# ---------------------------------------------------------------------------
# Phase 15 Test 8 — newest date first
# ---------------------------------------------------------------------------
def test_date_ordering_newest_first():
    trades = [
        _trade(id="d1", exit_time="2026-09-24 10:00:00"),
        _trade(id="d2", exit_time="2026-09-26 10:00:00"),
        _trade(id="d3", exit_time="2026-09-25 10:00:00"),
    ]
    assert [g["date"] for g in _build_date_groups(trades)] == \
        ["2026-09-26", "2026-09-25", "2026-09-24"]


# ---------------------------------------------------------------------------
# Phase 15 Test 9 — IST date boundary / timezone handling
# ---------------------------------------------------------------------------
def test_ist_date_boundary():
    """Timestamps near midnight group by the INDIAN calendar date."""
    # Aware IST timestamps: exact midnight boundaries in IST.
    assert _exit_ist_date(_trade(exit_time="2026-09-25T00:30:00+05:30")) == date(2026, 9, 25)
    assert _exit_ist_date(_trade(exit_time="2026-09-24T23:59:00+05:30")) == date(2026, 9, 24)
    # Naive strings are IST wall-clock (ledger convention) — not UTC-shifted.
    assert _exit_ist_date(_trade(exit_time="2026-09-25 00:05:00")) == date(2026, 9, 25)
    # An aware UTC instant that lands on a different IST calendar day.
    assert _exit_ist_date(_trade(exit_time="2026-09-24T17:00:00+00:00")) == date(2026, 9, 24)   # 22:30 IST
    assert _exit_ist_date(_trade(exit_time="2026-09-24T19:00:00+00:00")) == date(2026, 9, 25)   # 00:30 IST
    # Grouping follows the same rule.
    groups = _build_date_groups([
        _trade(id="a", pnl=10.0, exit_time="2026-09-24T19:00:00+00:00"),  # 25 Sep IST
        _trade(id="b", pnl=20.0, exit_time="2026-09-24 23:59:59"),         # 24 Sep IST
    ])
    assert [g["date"] for g in groups] == ["2026-09-25", "2026-09-24"]


# ---------------------------------------------------------------------------
# Phase 15 Test 10 — All Dates aggregates equal the sum of date groups
# ---------------------------------------------------------------------------
def test_all_dates_aggregates_equal_sum_of_groups():
    trades = [
        _trade(id="a", pnl=500.0, exit_time="2026-09-25 10:00:00"),
        _trade(id="b", pnl=-200.0, exit_time="2026-09-25 11:00:00"),
        _trade(id="c", pnl=0.0, exit_time="2026-09-24 09:00:00"),
        _trade(id="d", pnl=75.5, exit_time="2026-09-24 14:00:00"),
        _trade(id="e", pnl=-12.25, exit_time="2026-09-23 16:00:00"),
    ]
    summary = _aggregate_trades(trades)
    groups = _build_date_groups(trades)
    assert summary["total_trades"] == sum(g["trade_count"] for g in groups)
    assert summary["profitable_trade_count"] == sum(g["profitable_trade_count"] for g in groups)
    assert summary["losing_trade_count"] == sum(g["losing_trade_count"] for g in groups)
    assert summary["total_profit"] == round(sum(g["total_profit"] for g in groups), 2)
    assert summary["total_loss"] == round(sum(g["total_loss"] for g in groups), 2)
    assert summary["net_pnl"] == round(sum(g["net_pnl"] for g in groups), 2)
    assert summary["total_profit"] == 575.5
    assert summary["total_loss"] == 212.25
    assert summary["net_pnl"] == 363.25


# ---------------------------------------------------------------------------
# API contract — existing flat trades preserved + reporting fields added
# ---------------------------------------------------------------------------
def test_response_keeps_legacy_trades_and_adds_reporting_fields():
    full = [
        _trade(id="a", pnl=500.0, exit_time="2026-09-25 10:00:00"),
        _trade(id="b", pnl=-200.0, exit_time="2026-09-24 11:00:00"),
        _trade(id="open1", pnl=999.0, exit_time="2026-09-24 12:00:00", status="open"),
    ]
    resp = _build_trade_history_response(full, limit=1)
    assert set(resp.keys()) == {"trades", "date_groups", "summary"}
    # Legacy flat list semantics unchanged: most recent first, capped by limit.
    assert [t["id"] for t in resp["trades"]] == ["a"]
    # Reporting fields cover ALL realized trades (open positions excluded).
    assert resp["summary"]["total_trades"] == 2
    assert resp["summary"]["net_pnl"] == 300.0
    assert [g["date"] for g in resp["date_groups"]] == ["2026-09-25", "2026-09-24"]
    assert resp["date_groups"][0]["trade_count"] == 1


def test_response_date_filter_narrows_every_field():
    full = [
        _trade(id="a", pnl=500.0, exit_time="2026-09-25 10:00:00"),
        _trade(id="b", pnl=-200.0, exit_time="2026-09-24 11:00:00"),
        _trade(id="c", pnl=100.0, exit_time="2026-09-24 14:00:00"),
    ]
    resp = _build_trade_history_response(full, limit=50, filter_date=date(2026, 9, 24))
    assert [t["id"] for t in resp["trades"]] == ["b", "c"]
    assert [g["date"] for g in resp["date_groups"]] == ["2026-09-24"]
    assert resp["summary"]["total_trades"] == 2
    assert resp["summary"]["total_profit"] == 100.0
    assert resp["summary"]["total_loss"] == 200.0
    assert resp["summary"]["net_pnl"] == -100.0


def test_response_date_filter_with_no_trades_is_empty_not_fabricated():
    full = [_trade(id="a", pnl=500.0, exit_time="2026-09-25 10:00:00")]
    resp = _build_trade_history_response(full, limit=50, filter_date=date(2026, 9, 20))
    assert resp["trades"] == []
    assert resp["date_groups"] == []
    assert resp["summary"] == {
        "total_trades": 0,
        "profitable_trade_count": 0,
        "losing_trade_count": 0,
        "total_profit": 0.0,
        "total_loss": 0.0,
        "net_pnl": 0.0,
    }


# ---------------------------------------------------------------------------
# Edge cases — missing timestamps & filter parsing (no crashes)
# ---------------------------------------------------------------------------
def test_missing_exit_timestamp_excluded_from_groups_without_crash():
    t = _trade(id="noexit", pnl=40.0, exit_time="", entry_time="2026-09-24 10:00:00")
    assert _exit_ist_date(t) is None
    assert _build_date_groups([t]) == []
    s = _aggregate_trades([t])
    assert s["total_trades"] == 0

    # Unparseable timestamps behave the same way.
    ugly = _trade(id="ugly", pnl=40.0, exit_time="not-a-timestamp")
    assert _exit_ist_date(ugly) is None
    assert _build_date_groups([ugly]) == []


def test_group_trades_keep_flat_trade_shape():
    """Each trade inside a date group preserves the existing API fields."""
    t = _trade(id="shape1", symbol="TCS", direction="SHORT", entry_price=4000.0,
               exit_price=3980.0, quantity=5, stop_loss=4050.0, target_1=3950.0,
               target_2=3900.0, entry_time="2026-09-24 10:35:00",
               exit_time="2026-09-24 15:14:00", pnl=100.0, result="WIN",
               status="closed")
    day_trade = _build_date_groups([t])[0]["trades"][0]
    for key in ("id", "symbol", "direction", "entry_price", "exit_price",
                "quantity", "stop_loss", "target_1", "target_2", "entry_time",
                "exit_time", "pnl", "result", "status"):
        assert day_trade[key] == t[key], key


def test_parse_filter_date():
    assert _parse_filter_date("2026-09-25") == date(2026, 9, 25)
    assert _parse_filter_date("garbage") is None
    assert _parse_filter_date("") is None
    assert _parse_filter_date("2026-99-45") is None  # invalid month/day