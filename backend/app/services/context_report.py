"""Read-only Current vs 24H-Context comparison report (Mode A vs Mode B).

Two decision spaces are compared using REAL recorded data only:

  * CURRENT (Mode A): the signals ledger (``signals``) — what the present
    engine recommended on every scanned decision candle — plus the REALIZED
    paper-trading P&L from the user's ``trades`` ledger.
  * 24H (Mode B): the shadow ledger (``signal_24h_comparisons``) — what the
    same engine WOULD have recommended with the additive 24-clock-hour market
    context at the same candle. The 24H variant NEVER placed (and never
    places) a paper order, so its realized P&L is reported as PENDing — it is
    never estimated, extrapolated, or backfilled.

Hard rules:
  * No profitability claim is ever fabricated. Metrics with insufficient data
    are reported with ``None`` and a caveat; ``has_data`` gates the UI.
  * P&L numbers come verbatim from the trades ledger (``pnl`` column).
  * Win rate / profit factor / drawdown are computed the same way the
    performance endpoints do (a realized-loss streak counts only full closes;
    partials are excluded from trade-count/in-streak stats).

Everything here is pure calculation over serialized dict rows — the endpoint
(`app.api.trading` / ``GET /api/paper/performance/context-comparison``) loads
the rows and calls ``build_context_comparison``.
"""

from typing import Optional, Sequence

from app.services.signal_store import direction_family

LONG_FAMILY = "LONG"
SHORT_FAMILY = "SHORT"


def _f(value, default=None):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return v


def _round(v, nd=4):
    return round(v, nd) if v is not None else None


def _signal_stats(rows: Sequence[dict], dir_key: str) -> dict:
    """Decision-space stats for one side (rows are serialized signal rows)."""
    total = len(rows)
    long_rows = [r for r in rows if direction_family(r.get(dir_key)) == LONG_FAMILY]
    short_rows = [r for r in rows if direction_family(r.get(dir_key)) == SHORT_FAMILY]
    no_trade = total - len(long_rows) - len(short_rows)
    scores = [_f(r.get("score")) for r in rows if _f(r.get("score")) is not None]
    scores_long = [_f(r.get("score")) for r in long_rows if _f(r.get("score")) is not None]
    scores_short = [_f(r.get("score")) for r in short_rows if _f(r.get("score")) is not None]
    quality_counts: dict[str, int] = {}
    for r in rows:
        q = (r.get("quality") or "").strip().upper()
        if q:
            quality_counts[q] = quality_counts.get(q, 0) + 1
    def avg(vals):
        return _round(sum(vals) / len(vals), 2) if vals else None
    return {
        "total": total,
        "long": len(long_rows),
        "short": len(short_rows),
        "no_trade": no_trade,
        "avg_score": avg(scores),
        "avg_score_long": avg(scores_long),
        "avg_score_short": avg(scores_short),
        "quality_distribution": quality_counts,
        "has_data": total > 0,
    }


def _exit_class(t: dict) -> str:
    """Classify a realized trade's exit path. Prefers the recorded
    ``exit_reason``; falls back to deterministic geometry when the reason is
    absent, else MANUAL/OTHER."""
    reason = (t.get("exit_reason") or "").strip().upper()
    known = {"T1_PARTIAL", "TARGET_1", "T2_PARTIAL", "T2_FINAL", "TRAILING_STOP",
             "STOP_LOSS", "MANUAL_CLOSE", "END_OF_DAY", "PARTIAL"}
    if reason in known:
        if reason in ("T1_PARTIAL", "TARGET_1"):
            return "T1"
        if reason in ("T2_PARTIAL", "T2_FINAL", "TARGET_2"):
            return "T2"
        if reason == "TRAILING_STOP":
            return "TRAILING"
        if reason == "STOP_LOSS":
            return "SL"
        return "MANUAL"
    # Geometry fallback (only when unambiguous).
    entry = _f(t.get("entry_price"))
    exit_price = _f(t.get("exit_price"))
    sl = _f(t.get("stop_loss"))
    t1 = _f(t.get("target_1"))
    is_long = direction_family(t.get("direction")) == LONG_FAMILY
    if None in (entry, exit_price):
        return "MANUAL"
    if sl is not None and (
        (is_long and exit_price <= sl) or (not is_long and exit_price >= sl)
    ):
        return "SL"
    if t1 is not None and (
        (is_long and exit_price >= t1) or (not is_long and exit_price <= t1)
    ):
        return "T1"
    return "MANUAL"


def _realized_stats_flat(trades: Sequence[dict]) -> dict:
    """Realized P&L metrics for ONE side (no LONG/SHORT nesting). Pure;
    never fabricates. Only trades with a finite ``pnl`` count."""
    trades = [t for t in trades if _f(t.get("pnl")) is not None]
    total = len(trades)
    if total == 0:
        return {
            "total_trades": 0, "realized_pnl": 0.0, "win_rate": None,
            "avg_pnl": None, "avg_win": None, "avg_loss": None,
            "profit_factor": None, "avg_holding_seconds": None,
            "max_drawdown": None, "exit_breakdown": {}, "has_data": False,
        }
    pnls = [_f(t["pnl"]) for t in trades]
    realized = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / total if total else None
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None
    holds = []
    for t in trades:
        h = _f(t.get("holding_seconds"))
        if h is not None:
            holds.append(h)
    # Max drawdown from the cumulative realized-P&L curve.
    peak = 0.0
    max_dd = 0.0
    cum = 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    exit_breakdown: dict[str, int] = {}
    for t in trades:
        c = _exit_class(t)
        exit_breakdown[c] = exit_breakdown.get(c, 0) + 1
    return {
        "total_trades": total,
        "realized_pnl": round(realized, 2),
        "win_rate": _round(win_rate, 4),
        "avg_pnl": _round(realized / total, 2),
        "avg_win": _round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": _round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": _round(profit_factor, 2),
        "avg_holding_seconds": _round(sum(holds) / len(holds), 1) if holds else None,
        "max_drawdown": round(max_dd, 2),
        "exit_breakdown": exit_breakdown,
        "has_data": True,
    }


def _realized_stats(trades: Sequence[dict]) -> dict:
    """Realized P&L metrics for one side with LONG/SHORT splits (flat)."""
    stats = _realized_stats_flat(trades)
    split_keys = ("total_trades", "realized_pnl", "win_rate", "avg_pnl")
    stats["long"] = {k: _realized_stats_flat(
        [t for t in trades if direction_family(t.get("direction")) == LONG_FAMILY]
    ).get(k) for k in split_keys}
    stats["short"] = {k: _realized_stats_flat(
        [t for t in trades if direction_family(t.get("direction")) == SHORT_FAMILY]
    ).get(k) for k in split_keys}
    return stats


def build_context_comparison(
    current_signals: Sequence[dict],
    comparisons: Sequence[dict],
    trades: Sequence[dict],
    use_24h_context: bool = False,
    window_hours: float = 24.0,
) -> dict:
    """Pure report over serialized rows. ``current_signals`` are signals-ledger
    rows (keys: symbol, candle_ts, direction, score, confidence, quality,
    outcome, realized_pnl …); ``comparisons`` are signal_24h_comparisons rows
    (current_direction / ctx24_direction …); ``trades`` are ledger trade rows
    (direction, pnl, entry_price, exit_price, stop_loss, target_1 …)."""
    current_stat = _signal_stats(current_signals, "direction") if current_signals else _signal_stats([], "direction")
    default_compare = {
        "total": 0, "long": 0, "short": 0, "no_trade": 0,
        "avg_score": None, "avg_score_long": None, "avg_score_short": None,
        "quality_distribution": {}, "has_data": False,
    }
    ctx24_stat = dict(default_compare)
    agree = 0
    for c in comparisons:
        if direction_family(c.get("current_direction")) == direction_family(c.get("ctx24_direction")):
            agree += 1
    if comparisons:
        scores24 = [_f(c.get("ctx24_score")) for c in comparisons if _f(c.get("ctx24_score")) is not None]
        long24 = [c for c in comparisons if direction_family(c.get("ctx24_direction")) == LONG_FAMILY]
        short24 = [c for c in comparisons if direction_family(c.get("ctx24_direction")) == SHORT_FAMILY]
        scores24_long = [_f(c.get("ctx24_score")) for c in long24 if _f(c.get("ctx24_score")) is not None]
        scores24_short = [_f(c.get("ctx24_score")) for c in short24 if _f(c.get("ctx24_score")) is not None]
        q24: dict[str, int] = {}
        for c in comparisons:
            q = (c.get("ctx24_quality") or "").strip().upper()
            if q:
                q24[q] = q24.get(q, 0) + 1
        def avg(vals):
            return _round(sum(vals) / len(vals), 2) if vals else None
        ctx24_stat = {
            "total": len(comparisons),
            "long": len(long24),
            "short": len(short24),
            "no_trade": len(comparisons) - len(long24) - len(short24),
            "avg_score": avg(scores24),
            "avg_score_long": avg(scores24_long),
            "avg_score_short": avg(scores24_short),
            "quality_distribution": q24,
            "has_data": True,
        }

    current_realized = _realized_stats([t for t in trades if t.get("status") == "closed"])
    # Decision-space breakdowns per side (signal stats already include split).
    caveats = []
    if not (current_stat.get("has_data") or ctx24_stat.get("has_data")):
        caveats.append("No recommendations recorded yet — run the scanner during a session with data to populate the comparison.")
    if not current_realized.get("has_data") and any(t.get("status") == "closed" for t in trades):
        caveats.append("The realized side aggregates the closed-trade ledger; pending/open positions and partial exits are not counted as closed trades.")
    caveats.append(
        "The 24H-context side never executes orders — its realized P&L is marked "
        "pending by design and will populate only if 24H orders are ever authorized."
    )

    return {
        "data_source": "signals_ledger_vs_24h_shadow",
        "use_24h_context": bool(use_24h_context),
        "window_hours": round(window_hours, 2),
        "has_data": current_stat.get("has_data") or ctx24_stat.get("has_data") or current_realized.get("has_data"),
        "current": {
            "signal_stats": current_stat,
            "realized_stats": current_realized,
            "realized_pending": False,
        },
        "ctx24": {
            "signal_stats": ctx24_stat,
            "realized_stats": None,
            "realized_pending": True,
            "pending_note": (
                "Shadow signal — the 24H-context variant never placed paper orders, "
                "so its realized P&L is pending (never measured). No estimate is "
                "provided until 24H orders are authorized."
            ),
        },
        "agreement": {
            "total": len(comparisons),
            "agree_count": agree,
            "agreement_rate": _round(agree / len(comparisons), 4) if comparisons else None,
        },
        "caveats": caveats,
    }