"""AI-linked paper-trading performance monitor — read-only measurement.

This module answers, strictly from the *persisted* paper-trading ledger and the
persisted AI recommendation snapshot:

    For completed, AI-linked paper trades only, how did
    AI Recommendation → Entry → SL/T1/T2/R:R → Profit Capture → Final P&L
    actually perform?

Design constraints (all enforced here):

* **Measurement only.** Nothing in this module places/modifies orders,
  positions, signals or ledger rows. It is pure calculation over dicts in the
  ``/api/paper/trades`` shape (the ``_trade_rows_to_dicts`` +
  ``_attach_ai_decisions`` contract).
* **AI-linked only.** A trade is part of the measured cohort only when its
  ``signal_id`` is present AND resolves to a persisted ``signals`` row
  (``ai_available`` is true). Manual/test/unresolved rows are kept in a
  separate bucket and NEVER enter the performance or Profit-Capture math.
* **No legacy/pre-Phase-2 trades in the comparison.** AI linkage is only ever
  written by the traceability code, so every AI-linked row is post-feature by
  construction. Historical rows with ``signal_id = NULL`` are excluded.
* **No profitability claim on small samples.** Until at least
  ``MIN_TRADES_FOR_CONCLUSION`` completed AI-linked trades exist, the report
  says exactly: "Insufficient real Profit Capture data for performance
  conclusion." Raw observed values are still reported as measurements.

The legacy hypothetical rule (full original quantity exiting at T1) is reused
verbatim from :mod:`app.services.performance_comparison` so the two surfaces
can never disagree.
"""

from __future__ import annotations

from typing import Optional

from app.services.performance_comparison import build_trade_groups

# ---------------------------------------------------------------------------
# constants / contracts
# ---------------------------------------------------------------------------

#: Minimum completed AI-linked trades before any performance conclusion is
#: drawn. Below this the mandated "insufficient data" sentence is emitted.
MIN_TRADES_FOR_CONCLUSION = 30

INSUFFICIENT_DATA_SENTENCE = (
    "Insufficient real Profit Capture data for performance conclusion."
)

#: The exact fields a completed AI-linked trade must carry (requirement: every
#: new AI-driven paper trade is verified). Values may legitimately be falsy
#: (e.g. a 0.0 component score) — verification checks presence, not truthiness.
REQUIRED_TRADE_FIELDS = {
    "signal_id": ["signal_id"],
    "ai_direction": ["ai_direction"],
    "ai_score": ["ai_score"],
    "signal_quality": ["signal_quality"],
    "score_components": [
        "ai_trend_score",
        "ai_momentum_score",
        "ai_volume_score",
        "ai_vwap_score",
        "ai_price_action_score",
        "ai_market_context_score",
        "ai_risk_quality_score",
    ],
    "original_entry": ["original_entry"],
    "original_stop_loss": ["original_stop_loss"],
    "original_target_1": ["original_target_1"],
    "original_target_2": ["original_target_2"],
    "original_risk_reward": ["original_risk_reward"],
    "actual_entry": ["actual_entry"],
    "exit_reason": ["exit_reason"],
    "realized_pnl": ["realized_pnl"],
}

_LONG_DIRS = ("LONG", "BUY")
_SHORT_DIRS = ("SHORT", "SELL")

#: Target 1 evidence exit reasons (mirrors profit_capture / comparison module).
_T1_EVIDENCE = {"T1_PARTIAL", "TARGET_1", "T2_FINAL", "TRAILING_STOP"}

#: AI-score bands, aligned to the deterministic direction bands where useful.
SCORE_BANDS = (
    (0.0, 50.0, "<50"),
    (50.0, 65.0, "50-64"),
    (65.0, 75.0, "65-74"),
    (75.0, 85.0, "75-84"),
    (85.0, 1e9, ">=85"),
)


# ---------------------------------------------------------------------------
# cohort separation + field verification
# ---------------------------------------------------------------------------

def _is_ai_linked(trade: dict) -> bool:
    """True only when the row carries a signal_id AND the persisted signal
    resolved (the enrichment sets ``ai_available``)."""
    return bool(trade.get("signal_id")) and bool(trade.get("ai_available"))


def separate_ai_linked(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split serialized ledger rows into (ai_linked, manual_or_unlinked).

    ``manual_or_unlinked`` covers both manual/test trades (no ``signal_id``)
    and rows whose ``signal_id`` does not resolve to a signals row — i.e. any
    row that must NOT enter AI performance measurement.
    """
    ai, other = [], []
    for t in trades:
        (ai if _is_ai_linked(t) else other).append(t)
    return ai, other


def verify_ai_trade_fields(trades: list[dict]) -> dict:
    """Verify the required fields are persisted on every AI-linked row.

    Returns a per-trade breakdown plus a summary. Only rows that are already
    AI-linked are checked (there is nothing to verify on manual rows).
    """
    per_trade = []
    missing_counts: dict[str, int] = {}
    complete = 0
    for t in trades:
        missing = []
        for label, keys in REQUIRED_TRADE_FIELDS.items():
            absent = [k for k in keys if t.get(k) is None]
            if absent:
                missing.append({"field": label, "missing_keys": absent})
        status = "COMPLETE" if not missing else "INCOMPLETE"
        if not missing:
            complete += 1
        for m in missing:
            for k in m["missing_keys"]:
                missing_counts[k] = missing_counts.get(k, 0) + 1
        per_trade.append({
            "trade_id": t.get("id"),
            "signal_id": t.get("signal_id"),
            "symbol": t.get("symbol"),
            "status": status,
            "missing": missing,
        })
    return {
        "trades_checked": len(trades),
        "complete_count": complete,
        "incomplete_count": len(trades) - complete,
        "missing_field_counts": missing_counts,
        "per_trade": per_trade,
    }


# ---------------------------------------------------------------------------
# position grouping (PC rows share position_id)
# ---------------------------------------------------------------------------

def _key(trade: dict) -> Optional[str]:
    k = trade.get("position_id") or trade.get("id")
    return str(k) if k else None


def build_ai_positions(ai_trades: list[dict]) -> list[dict]:
    """Group AI-linked realized rows into completed positions.

    Rows produced by Profit Capture (T1 partial + T2/trailing final) share one
    ``position_id`` and therefore become ONE trade with the summed realized P&L.
    Each position carries the linked AI recommendation snapshot taken from its
    rows (all rows of one position share the same ``signal_id`` by design).
    """
    groups: dict[str, list[dict]] = {}
    for t in ai_trades:
        k = _key(t)
        if not k:
            continue
        groups.setdefault(k, []).append(t)

    # Reuse the established grouping + legacy rule so numbers can't diverge.
    legacy_by_pos = {
        str(g["position_id"]): g
        for g in build_trade_groups(ai_trades)
    }

    positions = []
    for pos_id, rows in groups.items():
        rows = sorted(rows, key=lambda r: str(r.get("exit_time") or ""))
        final = rows[-1]
        reasons = {r.get("exit_reason") for r in rows if r.get("exit_reason")}
        pc_pnl = round(sum(float(r.get("pnl") or 0.0) for r in rows), 2)
        legacy = legacy_by_pos.get(pos_id)
        ref = rows[0]
        # Authoritative original quantity: the legacy group computes
        # max(initial_quantity or quantity) across the position's rows; fall
        # back to the ref row for robustness.
        total_qty = legacy["total_quantity"] if legacy else max(
            (r.get("initial_quantity") or r.get("quantity") or 0 for r in rows),
            default=0,
        )
        positions.append({
            "position_id": pos_id,
            "symbol": ref.get("symbol"),
            "direction": ref.get("direction"),
            "entry_price": ref.get("entry_price"),
            "exit_price": final.get("exit_price"),
            "exit_time": final.get("exit_time"),
            "total_quantity": total_qty,
            "rows": len(rows),
            "pnl": pc_pnl,
            "signal_id": ref.get("signal_id"),
            "ai_score": ref.get("ai_score"),
            "signal_quality": ref.get("signal_quality"),
            "original_risk_reward": ref.get("original_risk_reward"),
            "original_entry": ref.get("original_entry"),
            "original_target_1": ref.get("original_target_1"),
            "original_stop_loss": ref.get("original_stop_loss"),
            "exit_reason": final.get("exit_reason"),
            "had_partial": "T1_PARTIAL" in reasons,
            "t1_hit": bool(reasons & _T1_EVIDENCE),
            "pc_pnl": pc_pnl,
            "legacy_pnl": legacy["legacy_pnl"] if legacy else pc_pnl,
        })
    positions.sort(key=lambda p: str(p.get("exit_time") or ""))
    return positions


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------

def _stats(pnls: list[float]) -> dict:
    """Trade/risk metrics for a set of realized position P&Ls (chronological).

    Zero-P&L positions count in ``total_trades`` but in neither win nor loss.
    ``max_drawdown`` is the peak-to-trough drop of the cumulative P&L curve.
    """
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = float(sum(pnls))
    gross_profit = float(sum(wins))
    gross_loss = float(sum(-p for p in losses))
    peak = cum = max_dd = 0.0
    consec = run = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        run = run + 1 if p < 0 else 0
        consec = max(consec, run)
    return {
        "total_trades": n,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": n - len(wins) - len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "total_pnl": round(total, 2),
        "average_pnl": round(total / n, 2) if n else 0.0,
        "average_profit": round(gross_profit / len(wins), 2) if wins else 0.0,
        "average_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown": round(max_dd, 2),
        "max_consecutive_losses": consec,
        "largest_win": round(max(wins), 2) if wins else 0.0,
        "largest_loss": round(min(losses), 2) if losses else 0.0,
    }


def _rate(count: int, n: int) -> dict:
    return {"count": count, "rate": round(count / n * 100, 1) if n else 0.0}


def _direction_bucket(positions: list[dict], dirs: tuple[str, ...]) -> dict:
    subset = [p for p in positions if str(p.get("direction") or "").upper() in dirs]
    return _stats([p["pnl"] for p in subset])


def _bucket(positions: list[dict], key_fn) -> dict:
    out: dict[str, dict] = {}
    for p in positions:
        label = key_fn(p)
        if label is None:
            label = "UNKNOWN"
        out.setdefault(str(label), []).append(p["pnl"])
    return {k: _stats(v) for k, v in out.items()}


def _score_band(score) -> Optional[str]:
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    for lo, hi, label in SCORE_BANDS:
        if lo <= s < hi:
            return label
    return ">=85"


def compute_monitoring(trades: list[dict], initial_capital: float = 10_000.0) -> dict:
    """Full monitoring report for the AI-linked cohort.

    ``trades`` is the serialized ledger (``/api/paper/trades`` shape, AI
    enrichment attached). Only AI-linked rows are measured; the rest are
    counted separately.
    """
    ai_rows, other_rows = separate_ai_linked(trades)
    positions = build_ai_positions(ai_rows)
    pnls = [p["pnl"] for p in positions]
    stats = _stats(pnls)

    total_pnl = stats["total_pnl"]
    return_pct = round(total_pnl / initial_capital * 100, 2) if initial_capital else 0.0

    # Deployed notional (original entry × original position quantity), context
    # only — the headline Return % is measured against the notional paper
    # capital.
    deployed = 0.0
    for p in positions:
        deployed += abs(float(p.get("entry_price") or 0.0)) * float(
            p.get("total_quantity") or 0
        )
    deployed_return_pct = (
        round(total_pnl / deployed * 100, 2) if deployed > 0 else None
    )

    # R:R average over the persisted original R:R values.
    rrs = [float(p["original_risk_reward"]) for p in positions
           if p.get("original_risk_reward") is not None]
    avg_rr = round(sum(rrs) / len(rrs), 2) if rrs else None

    n = len(positions)
    t1 = sum(1 for p in positions if p["t1_hit"])
    t2 = sum(1 for p in positions if p.get("exit_reason") == "T2_FINAL")
    trailing = sum(1 for p in positions if p.get("exit_reason") == "TRAILING_STOP")
    sl = sum(1 for p in positions if p.get("exit_reason") == "STOP_LOSS")
    manual = sum(1 for p in positions if p.get("exit_reason") == "MANUAL_CLOSE")

    # Legacy comparison (completed positions; no pre-PC rows in the cohort).
    legacy_total = round(sum(p["legacy_pnl"] for p in positions), 2)
    additional = round(total_pnl - legacy_total, 2)
    additional_pct = (
        round(additional / abs(legacy_total) * 100, 2) if legacy_total != 0 else None
    )

    return {
        "data_source": "paper_trading ledger (AI-linked only)",
        "initial_capital": initial_capital,
        "min_trades_for_conclusion": MIN_TRADES_FOR_CONCLUSION,
        "sufficient_for_conclusion": n >= MIN_TRADES_FOR_CONCLUSION,
        "conclusion": None if n >= MIN_TRADES_FOR_CONCLUSION else INSUFFICIENT_DATA_SENTENCE,
        "cohort": {
            "ai_linked_positions": n,
            "ai_linked_rows": len(ai_rows),
            "manual_or_unlinked_positions": len({
                _key(t) for t in other_rows if _key(t)
            }),
            "manual_or_unlinked_rows": len(other_rows),
        },
        "verification": verify_ai_trade_fields(ai_rows),
        "performance": {
            **stats,
            "return_pct": return_pct,
            "return_pct_basis": f"total P&L / initial capital ({initial_capital:g})",
            "deployed_notional": round(deployed, 2),
            "return_on_deployed_pct": deployed_return_pct,
            "average_rr": avg_rr,
            "average_rr_count": len(rrs),
        },
        "exit_stats": {
            "total_trades": n,
            "t1_hit": _rate(t1, n),
            "t2_final": _rate(t2, n),
            "trailing_stop": _rate(trailing, n),
            "stop_loss": _rate(sl, n),
            "manual_close": _rate(manual, n),
            "t1_partial": _rate(sum(1 for p in positions if p["had_partial"]), n),
        },
        "long": _direction_bucket(positions, _LONG_DIRS),
        "short": _direction_bucket(positions, _SHORT_DIRS),
        "by_signal_quality": _bucket(positions, lambda p: p.get("signal_quality")),
        "by_score_range": _bucket(positions, lambda p: _score_band(p.get("ai_score"))),
        "score_vs_pnl": [
            {
                "symbol": p.get("symbol"),
                "signal_id": p.get("signal_id"),
                "ai_score": p.get("ai_score"),
                "signal_quality": p.get("signal_quality"),
                "ai_direction": None,  # direction is the position direction below
                "direction": p.get("direction"),
                "original_risk_reward": p.get("original_risk_reward"),
                "exit_reason": p.get("exit_reason"),
                "exit_price": p.get("exit_price"),
                "pnl": p.get("pnl"),
                "result": "WIN" if p["pnl"] > 0 else ("LOSS" if p["pnl"] < 0 else "BREAKEVEN"),
            }
            for p in positions
        ],
        "legacy_comparison": {
            "cohort_positions": n,
            "actual_profit_capture_pnl": total_pnl,
            "legacy_hypothetical_pnl": legacy_total,
            "additional_pnl": additional,
            "additional_pnl_pct": additional_pct,
            "per_position": [
                {
                    "position_id": p["position_id"],
                    "symbol": p.get("symbol"),
                    "direction": p.get("direction"),
                    "t1_hit": p["t1_hit"],
                    "exit_reason": p.get("exit_reason"),
                    "actual_pc_pnl": p["pc_pnl"],
                    "legacy_hypothetical_pnl": p["legacy_pnl"],
                    "additional_pnl": round(p["pc_pnl"] - p["legacy_pnl"], 2),
                }
                for p in positions
            ],
        },
    }


# ---------------------------------------------------------------------------
# markdown rendering
# ---------------------------------------------------------------------------

def _money(v) -> str:
    if v is None:
        return "n/a"
    return f"{float(v):,.2f}"


def _pct(v) -> str:
    if v is None:
        return "n/a"
    return f"{float(v):.1f}%"


def _stats_line(label: str, s: dict) -> str:
    pf = s.get("profit_factor")
    pf_s = "n/a" if pf is None else f"{pf:.2f}"
    return (
        f"| {label} | {s.get('total_trades', 0)} | {_money(s.get('total_pnl'))} "
        f"| {_pct(s.get('win_rate'))} | {pf_s} | {_money(s.get('average_pnl'))} "
        f"| {_money(s.get('average_profit'))} | {_money(s.get('average_loss'))} "
        f"| {_money(s.get('max_drawdown'))} |"
    )


_STATS_HEADER = (
    "| Cohort | Trades | Total P&L | Win rate | Profit factor | Avg P&L "
    "| Avg profit | Avg loss | Max DD |\n"
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|"
)


def render_markdown(report: dict) -> str:
    """Render the monitoring report dict as the deliverables document.

    Contains the 15 items requested by the monitoring phase (current trade
    count → data limitations) plus the per-trade field verification and the
    manual/test separation. No profitability claim is made below the sample
    threshold — the mandated "insufficient data" sentence is shown instead.
    """
    perf = report["performance"]
    cohort = report["cohort"]
    exit_stats = report["exit_stats"]
    legacy = report["legacy_comparison"]
    verification = report["verification"]
    n = cohort["ai_linked_positions"]

    lines: list[str] = []
    add = lines.append

    add("# AI-Linked Paper-Trading Performance Monitoring")
    add("")
    add(
        "Measurement-only report over **real persisted paper-trading data**. "
        "Cohort = completed trades whose `signal_id` resolves to a persisted "
        "AI recommendation. Strategies, thresholds, R:R, SL/T1/T2, sizing, "
        "risk limits, Profit Capture, trailing and execution are unchanged."
    )
    add("")
    add(
        f"- Data source: `{report['data_source']}`  "
        f"(plus persisted `signals` evidence)"
    )
    add(
        f"- Notional paper capital used for Return %: "
        f"{report['initial_capital']:,.0f}"
    )
    add(
        f"- Minimum completed AI-linked trades for a performance conclusion: "
        f"{report['min_trades_for_conclusion']}"
    )
    add("")

    if not report["sufficient_for_conclusion"]:
        add("> **" + INSUFFICIENT_DATA_SENTENCE + "**")
        add(">")
        add(
            f"> Only {n} completed AI-linked trade(s) are on record; at least "
            f"{report['min_trades_for_conclusion']} are required before any "
            "Profit-Capture performance conclusion is drawn. All figures below "
            "are raw measurements, not conclusions."
        )
    else:
        add(
            "> Sample size is sufficient "
            f"({n} >= {report['min_trades_for_conclusion']}) for an initial "
            "analysis. Conclusions remain descriptive of the observed paper "
            "ledger only."
        )
    add("")

    add("## 1. Current number of real AI-linked trades")
    add("")
    add(f"- Completed AI-linked positions: **{n}**")
    add(f"- Ledger rows belonging to those positions: {cohort['ai_linked_rows']}")
    add(
        f"- Manual/test/unlinked positions (excluded from all AI metrics): "
        f"{cohort['manual_or_unlinked_positions']} "
        f"({cohort['manual_or_unlinked_rows']} rows)"
    )
    collection = report.get("collection")
    if collection:
        add(
            f"- AI recommendations persisted so far: "
            f"{collection['ai_recommendations_persisted']} "
            f"(with score: {collection['with_score']}, with strategy version: "
            f"{collection['with_strategy_version']}); linked to a trade: "
            f"{collection['linked_to_a_trade']}, still unlinked: "
            f"{collection['not_yet_linked_to_a_trade']}"
        )
    add("")

    add("## 2. Total P&L")
    add("")
    add(f"**{_money(perf['total_pnl'])}**")
    add("")

    add("## 3. Return %")
    add("")
    add(
        f"**{_pct(perf['return_pct'])}** "
        f"({perf['return_pct_basis']}); deployed notional "
        f"{_money(perf['deployed_notional'])}, return on deployed notional "
        f"{_pct(perf['return_on_deployed_pct'])}."
    )
    add("")

    add("## 4. Win rate")
    add("")
    add(
        f"**{_pct(perf['win_rate'])}** "
        f"({perf['winning_trades']} win / {perf['losing_trades']} loss / "
        f"{perf['breakeven_trades']} breakeven)."
    )
    add("")

    add("## 5. Profit factor")
    add("")
    pf = perf["profit_factor"]
    add(
        f"**{'n/a' if pf is None else f'{pf:.2f}'}** "
        f"(gross profit {_money(perf['gross_profit'])} / gross loss "
        f"{_money(perf['gross_loss'])})."
    )
    add("")

    add("## 6. Maximum drawdown")
    add("")
    add(
        f"**{_money(perf['max_drawdown'])}** peak-to-trough on the "
        "chronological cumulative P&L curve of the AI-linked cohort."
    )
    add("")

    add("## 7. Legacy hypothetical P&L")
    add("")
    add(
        f"**{_money(legacy['legacy_hypothetical_pnl'])}** — same entries, same "
        "original quantity, 100% exit at the original T1 (pre-Profit-Capture "
        "model). AI-linked only, so no pre-Profit-Capture historical trades are "
        "included."
    )
    add("")

    add("## 8. Actual Profit Capture P&L")
    add("")
    add(f"**{_money(legacy['actual_profit_capture_pnl'])}**")
    add("")

    add("## 9. Additional P&L")
    add("")
    add(
        f"**{_money(legacy['additional_pnl'])}** "
        "(Actual Profit Capture P&L − Legacy hypothetical P&L)."
    )
    add("")

    add("## 10. Additional P&L %")
    add("")
    add(
        f"**{_pct(legacy['additional_pnl_pct'])}** "
        "(Additional P&L / |Legacy hypothetical P&L| × 100)."
    )
    add("")

    add("## 11. AI score vs P&L")
    add("")
    add(
        "Per completed AI-linked trade: AI score → AI direction → original R:R "
        "→ actual exit → actual P&L."
    )
    add("")
    svp = report["score_vs_pnl"]
    if svp:
        add(
            "| Symbol | Signal ID | AI score | Quality | Direction | Orig R:R "
            "| Exit | Exit reason | P&L | Result |"
        )
        add("|---|---|---:|---|---|---:|---:|---|---:|---|")
        for r in svp:
            add(
                f"| {r['symbol']} | {r['signal_id']} | {r['ai_score']} "
                f"| {r['signal_quality']} | {r['direction']} "
                f"| {r['original_risk_reward']} | {r['exit_price']} "
                f"| {r['exit_reason']} | {_money(r['pnl'])} | {r['result']} |"
            )
    else:
        add("_No completed AI-linked trades yet._")
    add("")

    add("### Performance by AI score range")
    add("")
    add(_STATS_HEADER)
    for band, s in report["by_score_range"].items():
        add(_stats_line(band, s))
    add("")

    add("## 12. T1 / T2 / trailing / SL statistics")
    add("")
    add("| Outcome | Count | Rate |")
    add("|---|---:|---:|")
    for key, label in (
        ("t1_hit", "T1 reached (partial or final evidence)"),
        ("t1_partial", "T1 partial executed"),
        ("t2_final", "Closed at T2 (final)"),
        ("trailing_stop", "Closed by trailing stop"),
        ("stop_loss", "Closed by stop-loss"),
        ("manual_close", "Closed manually"),
    ):
        add(
            f"| {label} | {exit_stats[key]['count']} "
            f"| {_pct(exit_stats[key]['rate'])} |"
        )
    add("")

    add("## 13. LONG vs SHORT performance")
    add("")
    add(_STATS_HEADER)
    add(_stats_line("LONG", report["long"]))
    add(_stats_line("SHORT", report["short"]))
    add("")

    add("## 14. Signal-quality performance")
    add("")
    add(_STATS_HEADER)
    for quality, s in report["by_signal_quality"].items():
        add(_stats_line(quality, s))
    add("")

    add("## Field verification (requirement: every new AI-linked trade)")
    add("")
    add(
        f"Checked {verification['trades_checked']} AI-linked row(s): "
        f"{verification['complete_count']} COMPLETE, "
        f"{verification['incomplete_count']} INCOMPLETE."
    )
    add("")
    if verification["missing_field_counts"]:
        add("Missing fields observed:")
        add("")
        for field, count in sorted(verification["missing_field_counts"].items()):
            add(f"- `{field}`: {count}")
    else:
        add(
            "All required fields (`signal_id`, AI direction, AI score, signal "
            "quality, score components, original entry/SL/T1/T2/R:R, actual "
            "entry, exit reason, realized P&L) are persisted whenever an "
            "AI-linked row exists."
        )
    add("")

    add("## 15. Data limitations")
    add("")
    add(
        "- This report reflects only what is persisted in the live paper "
        "ledger; it cannot reconstruct AI linkage for trades created before "
        "traceability existed (their `signal_id` is NULL and they are "
        "excluded)."
    )
    add(
        "- `trades.signal_id` is populated only by recommendation-driven paper "
        "orders created after the traceability feature; manual orders stay "
        "NULL by design."
    )
    add(
        "- Outcomes are paper results at recorded fills/fees; they are not "
        "live-capital results and cannot be extrapolated."
    )
    add(
        "- Below the sample threshold the raw values here carry no statistical "
        "significance."
    )
    add("")
    add(
        "_This document is measurement only. It does not predict future "
        "profit, does not assert a fixed profit percentage, and does not "
        "modify any trading behaviour._"
    )
    add("")
    return "\n".join(lines)
