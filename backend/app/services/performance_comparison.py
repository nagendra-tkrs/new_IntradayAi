"""Legacy vs Profit Capture — read-only performance comparison.

Answers one question from real recorded paper-trading data:

    How does the implemented Profit Capture exit flow
    (Entry → T1 partial → protection → T2/trailing → final exit)
    compare with the previous 100%-at-T1 exit flow
    (Entry → T1 → full exit), using the same symbol / entry / quantity / T1
    for each completed trade?

Everything here is pure calculation over the serialized ledger rows that the
``/api/paper/trades`` endpoint already returns (the ``_trade_rows_to_dicts``
shape, including the additive profit-capture fields). It NEVER:

    * places or modifies orders/positions (no second order is created),
    * writes anything into the trades ledger (no fake legacy rows are stored),
    * recomputes realized P&L — the authoritative ``pnl`` per ledger row is
      reused verbatim.

Legacy-hypothetical rule (deterministic, per completed position):

    * If Target 1 was reached by that position (any T1_PARTIAL / TARGET_1 /
      T2_FINAL / TRAILING_STOP row, or a pre-profit-capture single row whose
      realized exit crossed T1 in the profit direction) → the legacy result is
      the P&L the ORIGINAL quantity would have realized exiting 100% at the
      T1 price.
    * Otherwise (stopped out or manually closed before T1 — the same stop-loss
      and manual-close paths existed under the legacy model) → the legacy
      result equals the position's actual outcome.

Directional P&L for both models::

    LONG  profit = (exit − entry) × qty
    SHORT profit = (entry − exit) × qty

No profitability claim is made here — this is measurement only. Outcomes are
reported as observed on the recorded paper ledger; when there is not enough
real data the API reports ``has_data: false`` and the UI shows "Insufficient
data for performance comparison."
"""

from datetime import date, datetime
from typing import Optional

# IST wall-clock convention for a trade's realization date (same semantics as
# the /api/paper/trades date grouping).
from app.core.market_session import IST

# Exit-reason contract (see profit_capture.py).
T1_PARTIAL = "T1_PARTIAL"
TARGET_1 = "TARGET_1"          # legacy full close at T1 (100% at T1 config)
T2_FINAL = "T2_FINAL"
TRAILING_STOP = "TRAILING_STOP"
STOP_LOSS = "STOP_LOSS"
MANUAL_CLOSE = "MANUAL_CLOSE"

# A row with any of these exit reasons proves the position reached Target 1.
_T1_EVIDENCE = {T1_PARTIAL, TARGET_1, T2_FINAL, TRAILING_STOP}

_EP = 0.005  # P&L tie tolerance (1 paisa / rounding slack)

_LONG_DIRS = ("LONG", "BUY")
_SHORT_DIRS = ("SHORT", "SELL")


def _direction_factor(direction) -> int:
    d = str(direction or "").upper()
    if d in _LONG_DIRS:
        return 1
    if d in _SHORT_DIRS:
        return -1
    return 0


def _pnl_directional(entry: float, exit_price: float, qty: float, direction) -> float:
    """Directional P&L formula (used ONLY for the hypothetical legacy result;
    recorded Profit Capture P&L always comes from the ledger's ``pnl``)."""
    return _direction_factor(direction) * (exit_price - entry) * qty


def _is_realized(trade: dict) -> bool:
    """Ledger rows are completed/closed trades (same rule as the trade-history
    module). Open positions never appear in realized reporting."""
    return trade.get("status") in (None, "", "closed")


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _exit_ist_date(trade: dict) -> Optional[date]:
    """IST calendar date on which a trade was realized (its final exit date).

    Mirrors ``app.api.trading._exit_ist_date`` exactly so the comparison uses
    the same date grouping the rest of the app uses (naive ledger values are
    IST wall-clock; aware values convert to IST). Returns None when there is
    no usable exit timestamp (the trade is then never placed on a date axis)."""
    raw = trade.get("exit_time") or ""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    else:
        dt = dt.astimezone(IST)
    return dt.date()


def _parse_filter_date(value: Optional[str]) -> Optional[date]:
    """YYYY-MM-DD (IST) range bound; None when absent or malformed."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _final_row(rows: list[dict]) -> Optional[dict]:
    """The row that closed the position: latest exit_time, preferring the row
    whose remaining quantity is 0 (same-tick T1+T2 double exits share a time)."""
    def key(row):
        remaining = row.get("remaining_quantity")
        is_final = 0 if remaining is None else (0 if remaining == 0 else 1)
        dt = _parse_dt(row.get("exit_time"))
        return (dt or datetime.min, is_final, row.get("exit_quantity") or 0)

    return max(rows, key=key) if rows else None


def build_trade_groups(trades: list[dict]) -> list[dict]:
    """Group realized ledger rows into completed trades.

    Rows of one Profit Capture position share ``position_id`` (in
    details_json); pre-profit-capture legacy rows carry none, so each realizes
    as its own single-row trade keyed by its row id. Every group exposes the
    fields needed for the comparison:

    * pc_pnl — recorded Profit Capture result (sum of the authoritative rows)
    * legacy_pnl — the hypothetical 100%-at-T1 result for the same position
    * total_quantity, direction, entry, target_1, exit_date, close_reason,
      t1_hit
    """
    groups: dict[str, list[dict]] = {}
    for trade in trades:
        if not _is_realized(trade):
            continue
        key = trade.get("position_id") or trade.get("id")
        if not key:
            continue
        groups.setdefault(str(key), []).append(trade)

    built = []
    for rows in groups.values():
        group = _make_group(rows)
        if group is not None:
            built.append(group)
    built.sort(key=lambda g: (g["exit_dt"] or datetime.min, g["position_id"] or ""))
    return built


def _make_group(rows: list[dict]) -> Optional[dict]:
    rows = sorted(rows, key=lambda r: _parse_dt(r.get("exit_time")) or datetime.min)
    final = _final_row(rows)
    exit_date = _exit_ist_date(final) if final else None
    # No usable realization date → excluded from time-based reporting.
    if exit_date is None:
        return None

    first = rows[0]
    direction = first.get("direction")
    entry = first.get("entry_price") or 0.0
    target_1 = first.get("target_1") or 0.0
    total_qty = max(
        (row.get("initial_quantity") or row.get("quantity") or 0 for row in rows),
        default=0,
    )
    total_exit_qty = sum(row.get("exit_quantity") or row.get("quantity") or 0 for row in rows)
    pc_pnl = sum(float(row.get("pnl") or 0.0) for row in rows)

    reasons = {row.get("exit_reason") for row in rows if row.get("exit_reason")}
    t1_hit = bool(reasons & _T1_EVIDENCE)
    if not t1_hit and not reasons and target_1 and final:
        # Legacy-era single row without profit-capture metadata: infer whether
        # its realized exit crossed T1 in the profit direction.
        factor = _direction_factor(direction)
        exit_price = final.get("exit_price") or 0.0
        if factor > 0:
            t1_hit = exit_price >= target_1
        elif factor < 0:
            t1_hit = exit_price <= target_1

    if target_1 and t1_hit:
        legacy_pnl = _pnl_directional(entry, target_1, total_qty, direction)
    else:
        # T1 never reached (or no T1 defined): the legacy model would have hit
        # the same stop-loss / manual path → same outcome as recorded.
        legacy_pnl = pc_pnl

    return {
        "position_id": first.get("position_id") or first.get("id"),
        "symbol": first.get("symbol"),
        "direction": direction,
        "entry_price": entry,
        "target_1": target_1,
        "total_quantity": total_qty,
        "total_exit_qty": total_exit_qty,
        "exit_date": exit_date,
        "exit_dt": _parse_dt(final.get("exit_time")),
        "close_reason": final.get("exit_reason"),
        "t1_hit": t1_hit,
        "had_partial": T1_PARTIAL in reasons,
        "pc_pnl": round(pc_pnl, 2),
        "legacy_pnl": round(legacy_pnl, 2),
    }


def _model_stats(pnls: list[float]) -> dict:
    """Trade/risk metrics for one model (legacy or profit capture) over a set
    of completed trades. Zero-P&L trades count in ``total_trades`` but in
    neither win nor loss; ``total_loss`` is the positive magnitude of the
    losing trades (same convention as /api/paper/trades summary); profit
    factor is null when there were no losing trades."""
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = float(sum(pnls))
    gross_profit = float(sum(wins))
    gross_loss = float(sum(-p for p in losses))

    # Peak-to-trough drawdown over the cumulative equity curve (chronological
    # input order), as a positive magnitude.
    peak = 0.0
    max_dd = 0.0
    cum = 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        drop = peak - cum
        if drop > max_dd:
            max_dd = drop

    max_consec = 0
    run = 0
    for p in pnls:
        if p < 0:
            run += 1
            max_consec = max(max_consec, run)
        else:
            run = 0

    return {
        "total_trades": n,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "total_pnl": round(total, 2),
        "average_pnl": round(total / n, 2) if n else 0.0,
        "average_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "average_loss": round(gross_loss / len(losses), 2) if losses else 0.0,
        "total_loss": round(gross_loss, 2),
        "largest_win": round(max(wins), 2) if wins else 0.0,
        "largest_loss": round(min(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown": round(max_dd, 2),
        "max_consecutive_losses": max_consec,
    }


def _difference(legacy: dict, pc: dict) -> dict:
    """Profit Capture minus Legacy, per metric (section 9 shape)."""
    def pf_diff(pf_legacy, pf_pc):
        if pf_legacy is None or pf_pc is None:
            return None
        return round(pf_pc - pf_legacy, 2)

    return {
        "pnl": round(pc["total_pnl"] - legacy["total_pnl"], 2),
        "average_pnl": round(pc["average_pnl"] - legacy["average_pnl"], 2),
        "win_rate": round(pc["win_rate"] - legacy["win_rate"], 1),
        "profit_factor": pf_diff(legacy["profit_factor"], pc["profit_factor"]),
        "max_drawdown": round(pc["max_drawdown"] - legacy["max_drawdown"], 2),
    }


def _exit_metrics(groups: list[dict]) -> dict:
    better = worse = equal = 0
    for g in groups:
        if g["pc_pnl"] > g["legacy_pnl"] + _EP:
            better += 1
        elif g["pc_pnl"] < g["legacy_pnl"] - _EP:
            worse += 1
        else:
            equal += 1
    n = len(groups)
    t2 = sum(1 for g in groups if g["close_reason"] == T2_FINAL)
    trail = sum(1 for g in groups if g["close_reason"] == TRAILING_STOP)
    sl = sum(1 for g in groups if g["close_reason"] == STOP_LOSS)
    manual = sum(1 for g in groups if g["close_reason"] == MANUAL_CLOSE)
    t1_full = sum(1 for g in groups if g["close_reason"] == TARGET_1)
    partials = sum(1 for g in groups if g["had_partial"])
    probes = sum(1 for g in groups if g["t1_hit"])
    return {
        "total_trades": n,
        "t1_hit_count": probes,
        "t1_partial_count": partials,
        "t1_partial_percent": round(partials / n * 100, 1) if n else 0.0,
        "t2_final_count": t2,
        "trailing_stop_count": trail,
        "stop_loss_count": sl,
        "manual_close_count": manual,
        "t1_full_close_count": t1_full,
        "better_than_legacy_count": better,
        "worse_than_legacy_count": worse,
        "equal_to_legacy_count": equal,
    }


def _split(groups: list[dict]):
    l = [g["legacy_pnl"] for g in groups]
    p = [g["pc_pnl"] for g in groups]
    legacy = _model_stats(l)
    pc = _model_stats(p)
    return legacy, pc, _difference(legacy, pc)


def compare_legacy_vs_profit_capture(
    trades: list[dict],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """Compute the read-only Legacy vs Profit Capture comparison.

    ``trades`` must be serialized ledger rows (the ``/api/paper/trades``
    shape). Optional ``start_date`` / ``end_date`` (YYYY-MM-DD IST, inclusive)
    filter by the IST realization (final exit) date. Never writes or mutates
    anything — purely derived metrics.

    ``data_source`` is always ``"paper_trading"``: every number is derived
    from the real recorded paper ledger. Tests exercise this module directly
    with fixtures and never feed the comparison API; the UI shows "Insufficient
    data" when ``has_data`` is false.
    """
    groups = build_trade_groups(trades)
    d0 = _parse_filter_date(start_date)
    d1 = _parse_filter_date(end_date)
    if d0 is not None or d1 is not None:
        def keep(g):
            d = g["exit_date"]
            if d0 is not None and d < d0:
                return False
            if d1 is not None and d > d1:
                return False
            return True

        groups = [g for g in groups if keep(g)]

    all_legacy, all_pc, all_diff = _split(groups)
    long_g = [g for g in groups if str(g["direction"] or "").upper() in _LONG_DIRS]
    short_g = [g for g in groups if str(g["direction"] or "").upper() in _SHORT_DIRS]
    l_legacy, l_pc, l_diff = _split(long_g)
    s_legacy, s_pc, s_diff = _split(short_g)

    return {
        "period": {
            "start": start_date or None,
            "end": end_date or None,
        },
        "data_source": "paper_trading",
        "has_data": len(groups) > 0,
        "models": {
            "legacy": "Entry → T1 → 100% Exit",
            "profit_capture": "Entry → T1 Partial → Protection → T2 / Trailing",
        },
        "legacy": {"all": all_legacy, "long": l_legacy, "short": s_legacy},
        "profit_capture": {"all": all_pc, "long": l_pc, "short": s_pc},
        "difference": {"all": all_diff, "long": l_diff, "short": s_diff},
        "exit_metrics": _exit_metrics(groups),
    }