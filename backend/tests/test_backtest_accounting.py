"""Backtest accounting / equity / metric regression tests.

Focused tests for the capital-accounting and performance-metric fixes in
``app.services.backtesting``:

  * entry/exit cash conservation — the committed entry notional is restored on
    exit, so ``cash_after_exit == cash_before_entry + net_pnl``
  * open-position equity == cash + marked-to-market position value (equity does
    not collapse by the notional while a position is open)
  * final reconciliation — ``final_capital == initial_capital + net P&L``
  * max drawdown derived from the corrected equity curve (no fake ~90% DD)
  * Sharpe / Sortino annualized with ``sqrt(252)`` for daily bars
  * transaction costs charged exactly once per applicable leg
  * position sizing does not collapse after prior round trips
  * signal / evaluation sequence is driven only by ``evaluate_row_signal`` and
    is independent of the accounting fix

Deterministic: indicator calculation and the shared per-row signal decision
(``evaluate_row_signal``) are mocked, so there is no network / DB / indicator
state and the tests exercise ONLY the accounting + metric code paths.
"""
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.models.schemas import SignalDirection, SignalScore, TradeSetup
from app.services.backtesting import BacktestEngine

INITIAL = 1_000_000.0
BAR_INDEX = "__bar_index__"


def _frame(n=70):
    """Flat ₹100 bars; individual tests punch in the price levels they need."""
    ts = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [1_000_000] * n,
            BAR_INDEX: list(range(n)),
        }
    )


def _long_decision(entry, stop, target):
    return {
        "direction": SignalDirection.LONG,
        "confidence": 80.0,
        "signal_score": SignalScore(total=80.0),
        "setup": TradeSetup(
            entry=entry, stop_loss=stop, target_1=target, target_2=target
        ),
        "reasons": [],
        "risks": [],
    }


def _short_decision(entry, stop, target):
    return {
        "direction": SignalDirection.SHORT,
        "confidence": 80.0,
        "signal_score": SignalScore(total=80.0),
        "setup": TradeSetup(
            entry=entry, stop_loss=stop, target_1=target, target_2=target
        ),
        "reasons": [],
        "risks": [],
    }


def _no_trade():
    return {
        "direction": SignalDirection.NO_TRADE,
        "confidence": 0.0,
        "signal_score": SignalScore(),
        "setup": TradeSetup(),
        "reasons": [],
        "risks": [],
    }


class _SignalPlan:
    """``evaluate_row_signal`` mock: LONG / SHORT at the given bar indices, else
    NO_TRADE.

    Records the bar indices at which the engine evaluated a signal so tests can
    assert the evaluation sequence is driven by position state (i.e. signals),
    not by cash/accounting.
    """

    def __init__(
        self,
        long_bars=(),
        short_bars=(),
        stop=95.0,
        target=110.0,
        short_stop=105.0,
        short_target=90.0,
    ):
        self.long_bars = set(long_bars)
        self.short_bars = set(short_bars)
        self.stop = stop
        self.target = target
        self.short_stop = short_stop
        self.short_target = short_target
        self.calls = []

    def __call__(self, row, market_context=None, strategy_version=None):
        idx = int(row[BAR_INDEX])
        self.calls.append(idx)
        if idx in self.long_bars:
            return _long_decision(row["open"], self.stop, self.target)
        if idx in self.short_bars:
            return _short_decision(row["open"], self.short_stop, self.short_target)
        return _no_trade()


def _run(df, plan, capital=INITIAL):
    with patch("app.services.backtesting.calculate_all_indicators", lambda d: d), patch(
        "app.services.backtesting.evaluate_row_signal", plan
    ):
        return BacktestEngine(initial_capital=capital).run(df, "TEST")


def _drawdown(eq, start_peak):
    peak = start_peak
    dd = 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak * 100)
    return dd


# ── Test 1 — entry/exit cash conservation ────────────────────────────────────
def test_entry_exit_cash_conservation():
    df = _frame()
    plan = _SignalPlan([55])  # enter at bar 56 open, target hit at bar 60
    df.loc[60, "high"] = 112.0
    df.loc[60, "close"] = 111.0

    r = _run(df, plan)

    assert r["total_trades"] == 1
    trade = r["trades"][0]
    assert trade["net_pnl"] != 0.0

    # Invariant: cash_after_exit == initial_capital + net_realized_pnl.
    assert r["final_capital"] == pytest.approx(INITIAL + trade["net_pnl"], abs=0.01)

    # The final equity point is flat and reconciles with final capital.
    last = r["equity_curve"][-1]
    assert last["has_position"] is False
    assert last["cash"] == pytest.approx(r["final_capital"], abs=0.01)
    assert last["equity"] == pytest.approx(r["final_capital"], abs=0.01)


# ── Test 2 — open-position equity == cash + position value ───────────────────
def test_open_position_equity_is_cash_plus_market_value():
    df = _frame()
    plan = _SignalPlan([55], stop=1.0, target=200.0)  # never exits

    r = _run(df, plan)

    assert r["total_trades"] == 0
    assert any(p["has_position"] for p in r["equity_curve"])

    for p in r["equity_curve"]:
        assert p["equity"] == pytest.approx(p["cash"] + p["position_value"], abs=0.01)

    entry_pt = next(p for p in r["equity_curve"] if p["has_position"])
    assert entry_pt["position_value"] > 0
    # The old bug dropped equity by the full notional on entry; it must not.
    assert entry_pt["equity"] > 0.99 * INITIAL
    # Unrealized P&L is position value minus cost basis, not the notional.
    assert abs(entry_pt["unrealized_pnl"]) < 0.01 * INITIAL


# ── Test 3 — final capital reconciliation when flat ──────────────────────────
def test_final_capital_reconciliation_when_flat():
    df = _frame()
    plan = _SignalPlan([55])
    df.loc[60, "high"] = 112.0
    df.loc[60, "close"] = 111.0

    r = _run(df, plan)

    assert r["final_capital"] == pytest.approx(INITIAL + r["total_pnl"], abs=0.01)
    # Cumulative realized P&L equals the sum of trade net P&L.
    assert r["total_pnl"] == pytest.approx(
        sum(t["net_pnl"] for t in r["trades"]), abs=0.01
    )


# ── Test 4 — max drawdown from corrected equity ──────────────────────────────
def test_max_drawdown_uses_corrected_equity():
    df = _frame()
    plan = _SignalPlan([55])
    df.loc[60, "high"] = 112.0
    df.loc[60, "close"] = 111.0

    r = _run(df, plan)

    eq = [p["equity"] for p in r["equity_curve"]]
    expected = _drawdown(eq, INITIAL)
    assert r["max_drawdown"] == pytest.approx(expected, abs=0.01)
    # Open-position notional must not manufacture a huge fake drawdown.
    assert r["max_drawdown"] < 1.0


# ── Test 5 & 6 — Sharpe / Sortino annualization = sqrt(252) ──────────────────
def test_sharpe_and_sortino_daily_annualization():
    df = _frame()
    # Trade 1 wins, trades 2 & 3 lose — both positive and negative returns
    # exist, with >=2 downside observations so Sortino's downside std is defined.
    plan = _SignalPlan([55, 61, 67])
    df.loc[60, "high"] = 112.0
    df.loc[60, "close"] = 111.0
    df.loc[66, "low"] = 94.0
    df.loc[66, "close"] = 95.0
    df.loc[69, "low"] = 94.0
    df.loc[69, "close"] = 95.0

    r = _run(df, plan)
    assert r["total_trades"] == 3

    eq = [p["equity"] for p in r["equity_curve"]]
    rets = np.array(
        [(eq[j] - eq[j - 1]) / eq[j - 1] for j in range(1, len(eq)) if eq[j - 1] > 0]
    )
    ann = np.sqrt(252)  # daily bars — NOT sqrt(252 * 75)

    exp_sharpe = (rets.mean() / rets.std()) * ann
    assert r["sharpe_ratio"] == pytest.approx(round(float(exp_sharpe), 2), abs=0.01)

    downside = rets[rets < 0]
    assert len(downside) > 0 and downside.std() > 0
    exp_sortino = (rets.mean() / downside.std()) * ann
    assert r["sortino_ratio"] == pytest.approx(round(float(exp_sortino), 2), abs=0.01)


def test_annualization_is_timeframe_aware():
    df = _frame()
    plan = _SignalPlan([55])
    df.loc[60, "high"] = 112.0
    df.loc[60, "close"] = 111.0

    with patch("app.services.backtesting.calculate_all_indicators", lambda d: d), patch(
        "app.services.backtesting.evaluate_row_signal", plan
    ):
        daily = BacktestEngine(initial_capital=INITIAL, periods_per_year=252).run(
            df, "TEST"
        )
        intraday = BacktestEngine(initial_capital=INITIAL, periods_per_year=252 * 75).run(
            df, "TEST"
        )

    assert abs(daily["sharpe_ratio"]) < abs(intraday["sharpe_ratio"])
    ratio = intraday["sharpe_ratio"] / daily["sharpe_ratio"]
    assert ratio == pytest.approx(np.sqrt(75), rel=0.02)


# ── Test 7 — transaction costs applied exactly once ──────────────────────────
def test_transaction_costs_applied_once():
    df = _frame()
    plan = _SignalPlan([55])
    df.loc[60, "high"] = 112.0
    df.loc[60, "close"] = 111.0

    r = _run(df, plan)

    trade = r["trades"][0]
    perf = r["performance"]
    # net = gross − round-trip (brokerage + STT), stored together in 'cost'.
    assert trade["net_pnl"] == pytest.approx(trade["gross_pnl"] - trade["cost"], abs=0.01)
    # The reported cost components sum to the per-trade cost exactly once.
    assert perf["total_brokerage_paid"] + perf["total_stt_paid"] == pytest.approx(
        trade["cost"], abs=0.01
    )
    # Portfolio reconciliation: gross − costs == net.
    assert r["total_pnl"] == pytest.approx(
        sum(t["gross_pnl"] for t in r["trades"])
        - perf["total_brokerage_paid"]
        - perf["total_stt_paid"],
        abs=0.05,
    )
    # Slippage is embedded in the actual fill prices (charged once per leg).
    assert trade["entry"] == pytest.approx(100.05, abs=0.01)
    assert trade["exit"] == pytest.approx(109.945, abs=0.01)
    assert perf["total_slippage_paid"] == pytest.approx(
        (110.0 - 109.945) * trade["quantity"], abs=0.5
    )


# ── Test 8 — signal sequence preserved & sizing does not collapse ────────────
def test_signal_sequence_and_sizing_stable():
    df = _frame()
    plan = _SignalPlan([55, 61])
    df.loc[60, "low"] = 94.0
    df.loc[60, "close"] = 95.0
    df.loc[66, "high"] = 112.0
    df.loc[66, "close"] = 111.0

    r = _run(df, plan)

    # Two LONG round trips with the exact exit types the mocked signals imply.
    assert [t["direction"] for t in r["trades"]] == ["LONG", "LONG"]
    assert [t["exit_type"] for t in r["trades"]] == ["stop_loss", "target_1"]
    assert [t["holding_period_bars"] for t in r["trades"]] == [4, 4]

    # Signals are evaluated only on flat bars (position state, not accounting).
    assert plan.calls == [55, 60, 61, 66, 67, 68]

    # Position sizing must not geometrically collapse after the first round trip.
    qty1, qty2 = r["trades"][0]["quantity"], r["trades"][1]["quantity"]
    assert qty2 >= 0.95 * qty1


def test_signal_sequence_independent_of_capital():
    """The trade/signal sequence is identical across capital levels; only the
    position size (and therefore P&L) scales. The accounting fix does not touch
    signal generation."""
    df = _frame()
    df.loc[60, "low"] = 94.0
    df.loc[60, "close"] = 95.0
    df.loc[66, "high"] = 112.0
    df.loc[66, "close"] = 111.0

    small = _run(df, _SignalPlan([55, 61]), capital=500_000.0)
    large = _run(df, _SignalPlan([55, 61]), capital=2_000_000.0)

    def sequence(res):
        return [
            (t["direction"], t["exit_type"], t["holding_period_bars"])
            for t in res["trades"]
        ]

    assert sequence(small) == sequence(large) == [
        ("LONG", "stop_loss", 4),
        ("LONG", "target_1", 4),
    ]
    assert small["trades"][0]["quantity"] != large["trades"][0]["quantity"]


# ── Test 9 — SHORT conservation (collateral notional returned on buy-back) ───
def test_short_position_cash_conservation():
    df = _frame()
    # SHORT at bar 55 -> enter bar 56 at 100 (stop 105, target 90); target hit
    # at bar 60 (low 89).
    plan = _SignalPlan(short_bars=[55])
    df.loc[60, "low"] = 89.0
    df.loc[60, "close"] = 90.0

    r = _run(df, plan)

    assert r["total_trades"] == 1
    trade = r["trades"][0]
    assert trade["direction"] == "SHORT"

    # The committed entry notional must be returned on buy-back, so the short's
    # cash must reconcile to initial + net P&L (the old formula leaked it).
    assert r["final_capital"] == pytest.approx(INITIAL + trade["net_pnl"], abs=0.01)
    last = r["equity_curve"][-1]
    assert last["has_position"] is False
    assert last["equity"] == pytest.approx(r["final_capital"], abs=0.01)


# ── Test 10 — mixed long/short, open-position reconciliation ─────────────────
def test_mixed_directions_no_cash_leak():
    df = _frame()
    # LONG at 55 (target 110 at bar 60), SHORT at 61 (target 90 at bar 66),
    # then a LONG left open to the final bar.
    plan = _SignalPlan(long_bars=[55, 67], short_bars=[61])
    df.loc[60, "high"] = 112.0
    df.loc[60, "close"] = 111.0
    df.loc[66, "low"] = 89.0
    df.loc[66, "close"] = 90.0

    r = _run(df, plan)

    assert [t["direction"] for t in r["trades"]] == ["LONG", "SHORT"]
    assert r["total_pnl"] == pytest.approx(
        sum(t["net_pnl"] for t in r["trades"]), abs=0.01
    )

    # With no cash leak, the last marked-to-market equity equals realized final
    # capital plus the still-open position's unrealized P&L.
    last = r["equity_curve"][-1]
    assert last["has_position"] is True
    assert last["equity"] - r["final_capital"] == pytest.approx(
        last["unrealized_pnl"], abs=0.01
    )
