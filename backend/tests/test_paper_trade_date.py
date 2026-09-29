"""Paper Trading trade-date regression tests.

The Trade History UI renders a Date column from the trade's real timestamp.
These tests lock in the API contract it depends on: ``/api/paper/trades``
serialization (``_trade_rows_to_dicts``) must expose the existing
``entry_time`` / ``exit_time`` fields unchanged and must return an empty string
(never crash) when a historical record genuinely has no timestamp.

The Date value is display-only — no P&L / accounting / lifecycle logic is
touched.
"""
from datetime import datetime
from types import SimpleNamespace

from app.api.trading import _trade_rows_to_dicts


def _row(**overrides):
    base = dict(
        id="t1",
        symbol="RELIANCE",
        direction="LONG",
        entry_price=100.0,
        exit_price=110.0,
        quantity=10,
        stop_loss=95.0,
        target_1=115.0,
        target_2=120.0,
        entry_time=datetime(2026, 9, 24, 10, 35, 0),
        exit_time=datetime(2026, 9, 24, 15, 0, 0),
        pnl=100.0,
        status="closed",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_timestamps_are_exposed():
    """A trade with timestamps returns them to the client."""
    d = _trade_rows_to_dicts([_row()])[0]
    assert d["entry_time"] == "2026-09-24 10:35:00"
    assert d["exit_time"] == "2026-09-24 15:00:00"


def test_missing_exit_timestamp_is_safe():
    """A historical record without an exit timestamp returns '' (no crash)."""
    d = _trade_rows_to_dicts([_row(exit_time=None)])[0]
    assert d["exit_time"] == ""
    # entry timestamp still present
    assert d["entry_time"] == "2026-09-24 10:35:00"


def test_missing_entry_timestamp_is_safe():
    d = _trade_rows_to_dicts([_row(entry_time=None)])[0]
    assert d["entry_time"] == ""


def test_existing_trade_fields_unchanged():
    """Symbol / side / qty / entry / exit / P&L / status stay intact."""
    d = _trade_rows_to_dicts([_row()])[0]
    assert d["symbol"] == "RELIANCE"
    assert d["direction"] == "LONG"
    assert d["quantity"] == 10
    assert d["entry_price"] == 100.0
    assert d["exit_price"] == 110.0
    assert d["stop_loss"] == 95.0
    assert d["target_1"] == 115.0
    assert d["target_2"] == 120.0
    assert d["pnl"] == 100.0
    assert d["status"] == "closed"
    assert d["result"] == "WIN"


def test_result_labels_unchanged():
    assert _trade_rows_to_dicts([_row(pnl=50.0)])[0]["result"] == "WIN"
    assert _trade_rows_to_dicts([_row(pnl=-50.0)])[0]["result"] == "LOSS"
    assert _trade_rows_to_dicts([_row(pnl=0.0)])[0]["result"] == "BREAKEVEN"
