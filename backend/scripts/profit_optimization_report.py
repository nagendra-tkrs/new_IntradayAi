"""Read-only profit-optimization report for IntradayAI.

    python scripts/profit_optimization_report.py

Opens the database with ``file:...?mode=ro`` and writes nothing, so it cannot
alter the ledger it reports on.

Sections
--------
1. REALIZED BASELINE - the twenty baseline statistics from actual closed
   trades, split into cohorts (AI-linked / strategy-unlinked / test fixtures).
2. ATTRIBUTION - where the losses are, grouped by direction, time of day,
   score band, quality, exit reason and holding time.
3. PATTERN SIGNIFICANCE - exact tests. A pattern that cannot be distinguished
   from noise is reported as such.
4. REPLAY FIDELITY - whether the simulator reproduces the real exits. Without
   this passing, no candidate below is interpretable.
5. CANDIDATE GRIDS - simulated (never executed) results for every candidate
   configuration, under BOTH intrabar assumptions.
6. PROMOTION GATE - why each candidate stays CANDIDATE.
7. SHADOW LEDGER - what the Profit Selection Layer has been recording.
8. RISK-BUDGET UTILIZATION - which bound actually set each position's size, the
   utilization of the configured 2% budget, and a SHADOW (never promoted)
   comparison of current sizing against risk-budget-driven sizing.

Every candidate table is labelled SIMULATED. Realized and simulated numbers are
never placed in the same column, and no simulated figure is ever described as
performance.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.services.market_data.provider_factory import get_provider  # noqa: E402
from app.services.optimization_experiments import (  # noqa: E402
    EXPERIMENT_GRIDS, evaluate_promotion, measure_fidelity, pattern_significance,
    run_experiment_grid,
)
from app.services.profit_baseline import (  # noqa: E402
    COHORT_AI_LINKED, COHORT_STRATEGY_UNLINKED, INSUFFICIENT_PROMOTION,
    build_baseline, collapse_positions,
)
from app.services.trade_replay import (  # noqa: E402
    INTRABAR_CLOSE_ONLY, INTRABAR_PESSIMISTIC, Bar,
)

# The moment ``_run_profit_selection`` began deriving ``baseline_decision``
# from the signal direction instead of hardcoding "TRADE". Shadow rows with an
# earlier ``generated_at`` carry the old, mislabelled baseline and are excluded
# from the candidate-vs-baseline comparison (never deleted or rewritten).
BASELINE_DECISION_FIXED_AT = "2026-09-30T14:36:00"

RULE = "=" * 100
THIN = "-" * 100


def _resolve_db_path() -> str:
    url = str(getattr(settings, "DATABASE_URL", "") or "")
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            return url[len(prefix):]
    if url.startswith("sqlite://"):
        return url[len("sqlite://"):]
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "intradayai.db"
    )


def _read_only(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _bar_line(label: str, stats: dict) -> str:
    pf = stats.get("profit_factor")
    exp = stats.get("expectancy")
    return (
        f"  {label:22s} n={stats['total_completed_trades']:<4d}"
        f" wr={(stats['win_rate'] if stats['win_rate'] is not None else float('nan')):.3f}"
        f" net={stats['net_pnl']:>10.2f}"
        f" exp={(exp if exp is not None else float('nan')):>8.3f}"
        f" pf={(pf if pf is not None else float('nan')):>7.3f}"
        f" aw={(stats['average_winner'] or 0):>8.2f}"
        f" al={(stats['average_loser'] or 0):>8.2f}"
        f" dd={stats['max_drawdown']:>8.2f}"
        f"  {stats['data_status']}"
    )


async def _load_bars(symbols, days: int, cache_path: Optional[str]) -> dict:
    """Fetch 5m bars. Reuses a local JSON cache when provided."""
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        print(f"  bars: {len(raw)} symbols from cache {cache_path}")
        return {
            sym: [
                Bar(
                    timestamp=datetime.strptime(b["t"], "%Y-%m-%d %H:%M:%S"),
                    open=b["o"], high=b["h"], low=b["l"], close=b["c"], volume=b["v"],
                )
                for b in rows
            ]
            for sym, rows in raw.items()
        }
    provider = get_provider()
    out: dict = {}
    for sym in symbols:
        try:
            df = await provider.get_ohlcv(sym, timeframe="5m", days=days)
        except Exception as e:
            print(f"  bars: {sym} FAILED ({e})")
            continue
        if df is None or len(df) == 0:
            print(f"  bars: {sym} EMPTY")
            continue
        rows = []
        for _, r in df.iterrows():
            ts = r["timestamp"]
            if getattr(ts, "tzinfo", None) is not None:
                ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
            rows.append(
                Bar(
                    timestamp=ts.replace(microsecond=0),
                    open=float(r["open"]), high=float(r["high"]),
                    low=float(r["low"]), close=float(r["close"]),
                    volume=float(r.get("volume") or 0),
                )
            )
        out[sym] = rows
    print(f"  bars: {len(out)} symbols fetched from the live provider")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7,
                    help="days of 5m history for the replay (default 7)")
    ap.add_argument("--bars-cache", default=None,
                    help="path to a bars.json cache; skips network fetching")
    ap.add_argument("--json-out", default=None,
                    help="also write the full machine-readable report here")
    ap.add_argument("--no-bars", action="store_true",
                    help="baseline + attribution only; skip replay and candidates")
    args = ap.parse_args()

    db_path = _resolve_db_path()
    print(RULE)
    print("INTRAFYAI - PROFIT OPTIMIZATION REPORT (READ-ONLY)")
    print(RULE)
    print(f"  database : {db_path}")
    print(f"  generated: {datetime.now().isoformat(timespec='seconds')}")
    print(f"  mode     : Profit Selection Layer = "
          f"{getattr(settings, 'PROFIT_SELECTION_MODE', 'SHADOW')}")

    if not os.path.exists(db_path):
        print(f"  ERROR: database not found at {db_path}")
        return 1

    conn = _read_only(db_path)
    trades = [dict(r) for r in conn.execute("SELECT * FROM trades")]
    signals = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM signals")}
    shadow_rows: list = []
    try:
        shadow_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM signal_profit_selection_shadow "
            "ORDER BY candle_ts DESC LIMIT 500")]
    except sqlite3.Error:
        pass
    conn.close()

    # ---------------------------------------------------------------- 1
    base = build_baseline(trades, signals)
    print()
    print(RULE)
    print("1. REALIZED BASELINE  (from trades.pnl - actual closed positions)")
    print(RULE)
    print(f"  ledger rows closed        : {base['ledger_rows_closed']}")
    print(f"  positions after collapsing: {base['positions_after_collapse']}"
          f"   (profit-capture slices summed)")
    print(f"  cohorts                   : {base['cohort_sizes']}")
    print(f"  sample required for a conclusion: "
          f"{base['min_trades_for_conclusion']}")
    print()
    print(_bar_line("OVERALL (unfiltered)", base["overall"]))
    print(_bar_line("STRATEGY COHORT", base["strategy_only"]))
    print(_bar_line("  AI-linked", base["ai_linked"]))
    print(_bar_line("  strategy-unlinked", base["strategy_unlinked"]))
    print(_bar_line("  test fixtures (excluded)", base["test_fixture"]))
    print()
    st = base["strategy_only"]
    print(f"  gross profit {st['gross_profit']:.2f} | gross loss {st['gross_loss']:.2f} "
          f"| net {st['net_pnl']:+.2f}")
    print(f"  exit-reason mix           : {st['exit_reason_counts']}")
    print(f"  avg R multiple            : {st['average_risk_reward']}")

    # ---------------------------------------------------------------- 2
    print()
    print(RULE)
    print("2. ATTRIBUTION - where the money is lost (strategy cohort)")
    print(RULE)
    for name in ("direction", "time_of_day", "exit_reason", "holding_time",
                 "score_band", "quality", "symbol"):
        groups = base["groups"][name]
        print(f"\n  -- by {name} --")
        for label, stats in groups.items():
            if stats["total_completed_trades"] == 0:
                print(f"  {label:22s} n=0    {stats['data_status']}")
                continue
            print(_bar_line(label, stats))

    # ---------------------------------------------------------------- 3
    print()
    print(RULE)
    print("3. PATTERN SIGNIFICANCE - is the apparent pattern real? (exact tests)")
    print(RULE)
    positions = [
        p for p in collapse_positions(trades, signals)
        if p.cohort() in (COHORT_AI_LINKED, COHORT_STRATEGY_UNLINKED)
    ]
    patterns = {
        "LONG vs SHORT": (
            [p for p in positions if p.side == "LONG"],
            [p for p in positions if p.side == "SHORT"],
        ),
        "held <30m vs >=30m": (
            [p for p in positions if p.holding_minutes < 30],
            [p for p in positions if p.holding_minutes >= 30],
        ),
        "morning vs later": (
            [p for p in positions
             if p.time_bucket() in ("09:15-10:00", "10:00-11:00")],
            [p for p in positions
             if p.time_bucket() not in ("09:15-10:00", "10:00-11:00")],
        ),
        "stopped-out vs target": (
            [p for p in positions if p.terminal_reason == "STOP_LOSS"],
            [p for p in positions
             if p.terminal_reason in ("T2_FINAL", "TARGET_1")],
        ),
    }
    pattern_json = {}
    for name, (a, b) in patterns.items():
        sig = pattern_significance(a, b)
        pattern_json[name] = sig
        print(f"\n  {name}")
        print(f"    n={sig['n_a']} vs {sig['n_b']}   "
              f"mean P&L {sig['mean_pnl_a']} vs {sig['mean_pnl_b']}")
        print(f"    Fisher exact p = {sig['fisher_exact_p']}   "
              f"permutation p = {sig['permutation_p']}   "
              f"Welch t = {sig['welch_t']}")
        print(f"    significant at 0.05: {sig['significant_at_0_05']}"
              f"   verdict: {sig['verdict']}")

    payload: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "database": db_path,
        "baseline": base,
        "patterns": pattern_json,
    }

    if args.no_bars:
        _print_sizing(positions, payload)
        _print_verdict(base)
        if args.json_out:
            _dump(payload, args.json_out)
        return 0

    # ---------------------------------------------------------------- 4
    print()
    print(RULE)
    print("4. REPLAY FIDELITY - can the simulator reproduce the real exits?")
    print(RULE)
    symbols = sorted({p.symbol for p in positions})
    print("  fetching 5m history for the replay...")
    bars = asyncio.run(_load_bars(symbols, args.days, args.bars_cache))
    if not bars:
        print("  no bars available; candidate evaluation cannot run")
        _print_verdict(base)
        return 0

    fidelity = {}
    for assumption in (INTRABAR_PESSIMISTIC, INTRABAR_CLOSE_ONLY):
        rep = measure_fidelity(positions, bars, assumption)
        fidelity[assumption] = rep.to_dict()
        rate = rep.reason_agreement_rate
        sign = rep.sign_agreement_rate
        print(f"  {assumption:12s} exit-reason agreement = "
              f"{(rate if rate is not None else float('nan')):.0%} "
              f"({rep.reason_agreements}/{rep.positions_with_recorded_reason})"
              f"   P&L sign = {sign:.0%} ({rep.sign_agreements}/{rep.positions})"
              f"   mean |error| = {rep.mean_abs_pnl_error:.2f}/trade")
    gate_ok, gate_reasons = measure_fidelity(positions, bars).passes()
    print(f"  FIDELITY GATE: {'PASS' if gate_ok else 'FAIL'}")
    for r in gate_reasons:
        print(f"    - {r}")
    payload["fidelity"] = fidelity
    payload["fidelity_gate"] = {"passes": gate_ok, "reasons": gate_reasons}

    # ---------------------------------------------------------------- 5
    print()
    print(RULE)
    print("5. CANDIDATE GRIDS  *** SIMULATED - NOT REALIZED PERFORMANCE ***")
    print(RULE)
    print("  Position size is re-derived under the UNCHANGED risk budget AND the")
    print("  unchanged 5% capital headroom, so every candidate is compared at the")
    print("  size production would actually take. Note that at the live Rs 10,000")
    print("  capital the cash cap - not the risk budget - binds, so a wider stop")
    print("  does NOT shrink the position; it carries MORE rupee risk, bounded by")
    print("  the Rs 200 budget. The 'qty' column is flat across stop widths for")
    print("  exactly this reason.")
    print("  Every grid is run under BOTH intrabar assumptions. A candidate that")
    print("  only wins under one of them is an artefact of bar resolution.")
    payload["grids"] = {}
    decisions = []
    for grid_name in EXPERIMENT_GRIDS:
        result = run_experiment_grid(grid_name, positions, bars)
        payload["grids"][grid_name] = {
            "baseline": {
                a: result["baseline"][a].to_dict()
                for a in (INTRABAR_PESSIMISTIC, INTRABAR_CLOSE_ONLY)
            },
            "candidates": {
                a: [c.to_dict() for c in result["candidates"][a]]
                for a in (INTRABAR_PESSIMISTIC, INTRABAR_CLOSE_ONLY)
            },
            "is_realized_performance": False,
        }
        print(f"\n  -- {grid_name} --")
        for assumption in (INTRABAR_PESSIMISTIC, INTRABAR_CLOSE_ONLY):
            b = result["baseline"][assumption]
            print(f"     [{assumption}]")
            print(f"       {'config':30s} {'n':>3s} {'skip':>4s} {'wr':>6s} "
                  f"{'net':>9s} {'exp':>8s} {'pf':>8s} {'dd':>8s} {'qty':>6s}")
            print(f"       {'BASELINE (simulated)':30s} {b.simulated_trades:3d} "
                  f"{b.skipped:4d} {(b.win_rate or 0):6.2f} {b.net_pnl:9.2f} "
                  f"{(b.expectancy if b.expectancy is not None else float('nan')):8.3f} "
                  f"{(b.profit_factor if b.profit_factor is not None else float('nan')):>8} "
                  f"{b.max_drawdown:8.2f} {(b.average_quantity or 0):6.1f}")
            for c in result["candidates"][assumption]:
                print(f"       {c.label:30s} {c.simulated_trades:3d} {c.skipped:4d} "
                      f"{(c.win_rate or 0):6.2f} {c.net_pnl:9.2f} "
                      f"{(c.expectancy if c.expectancy is not None else float('nan')):8.3f} "
                      f"{(c.profit_factor if c.profit_factor is not None else float('nan')):>8} "
                      f"{c.max_drawdown:8.2f} {(c.average_quantity or 0):6.1f}")
                d = evaluate_promotion(
                    f"{grid_name}/{c.label}/{assumption}", b, c,
                    len(positions), measure_fidelity(positions, bars, assumption),
                )
                decisions.append({"grid": grid_name, "assumption": assumption,
                                  "label": c.label, **d.to_dict()})

    # ---------------------------------------------------------------- 6
    print()
    print(RULE)
    print("6. PROMOTION GATE")
    print(RULE)
    promotable = [d for d in decisions if d["promotable"]]
    print(f"  candidates evaluated : {len(decisions)}")
    print(f"  promotable           : {len(promotable)}")
    tally: dict = {}
    for d in decisions:
        for reason in d["blocking_reasons"]:
            tally[reason.split(";")[0]] = tally.get(reason.split(";")[0], 0) + 1
    print("\n  why candidates are blocked:")
    for reason, count in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"    {count:3d}x  {reason}")
    payload["decisions"] = decisions

    # ---------------------------------------------------------------- 7
    print()
    print(RULE)
    print("7. PROFIT SELECTION LAYER - shadow ledger")
    print(RULE)
    print(f"  mode : {getattr(settings, 'PROFIT_SELECTION_MODE', 'SHADOW')}")
    print(f"  rows : {len(shadow_rows)}")
    if shadow_rows:
        from collections import Counter

        # Rows written before this timestamp were persisted with a hardcoded
        # baseline_decision="TRADE", which mislabels every NO_TRADE signal as a
        # baseline agreement on a trade that never happened. The rows are LEFT
        # IN PLACE (the shadow ledger is append-only audit evidence and is never
        # rewritten), but they are excluded from the candidate-vs-baseline
        # comparison so no conclusion is drawn from known-wrong labels.
        legacy = [r for r in shadow_rows
                  if str(r.get("generated_at") or "") < BASELINE_DECISION_FIXED_AT]
        current = [r for r in shadow_rows
                   if str(r.get("generated_at") or "") >= BASELINE_DECISION_FIXED_AT]

        print(f"  candidate decisions : "
              f"{dict(Counter(r.get('candidate_decision') for r in current))}")
        print(f"  baseline decisions  : "
              f"{dict(Counter(r.get('baseline_decision') for r in current))}")
        eligible = [r for r in current
                    if str(r.get("direction") or "").upper() not in
                    ("", "NO_TRADE", "NONE")]
        print(f"  eligible signals (actionable direction): {len(eligible)}")
        print(f"  NO_TRADE rows recorded (never tradable) : "
              f"{len(current) - len(eligible)}")
        scored = [r for r in eligible if r.get("signal_score") is not None]
        print(f"  eligible rows carrying a signal score  : {len(scored)}"
              f"   (NULL means the score was unavailable, not zero)")
        disagree = [r for r in eligible
                    if r.get("candidate_decision") != r.get("baseline_decision")]
        print(f"  eligible rows where the layer would have differed: {len(disagree)}")
        applied = [r for r in current if r.get("applied_to_production")]
        print(f"  rows that were APPLIED to production: {len(applied)}"
              f"   (must be 0 in SHADOW mode)")
        resolved = [r for r in current if r.get("outcome")]
        print(f"  rows with a realized outcome attached: {len(resolved)}")
        if legacy:
            print()
            print(f"  NOTE: {len(legacy)} earlier rows predate the "
                  f"baseline_decision fix ({BASELINE_DECISION_FIXED_AT}) and are")
            print("  EXCLUDED from the comparison above. They are retained, not "
                  "rewritten.")
    else:
        print("  no shadow rows yet - the layer records on the next scan")
    payload["shadow"] = {
        "mode": getattr(settings, "PROFIT_SELECTION_MODE", "SHADOW"),
        "rows": len(shadow_rows),
        "eligible": len([r for r in shadow_rows
                         if str(r.get("generated_at") or "") >= BASELINE_DECISION_FIXED_AT
                         and str(r.get("direction") or "").upper() not in
                         ("", "NO_TRADE", "NONE")]),
        "legacy_rows_excluded": len([r for r in shadow_rows
                                     if str(r.get("generated_at") or "")
                                     < BASELINE_DECISION_FIXED_AT]),
        "applied_to_production": len([r for r in shadow_rows
                                      if r.get("applied_to_production")]),
    }

    # ---------------------------------------------------------------- 8
    _print_sizing(positions, payload)

    _print_verdict(base)
    if args.json_out:
        _dump(payload, args.json_out)
    return 0


def _print_sizing(real_positions, payload: dict) -> None:
    """Section 8: risk-budget utilization and the SHADOW sizing comparison."""
    from app.services.position_sizing import sizing_report

    print()
    print(RULE)
    print("8. RISK-BUDGET UTILIZATION  (measurement only - sizing is unchanged)")
    print(RULE)

    # Test-fixture rows (sub-second API smoke tests) are excluded, matching
    # section 1's strategy cohort, so the two sections always agree on which
    # positions are real.
    real = [
        p for p in (real_positions or [])
        if p.cohort() in (COHORT_AI_LINKED, COHORT_STRATEGY_UNLINKED)
    ]
    if not real:
        print("  no real positions available")
        return

    sizing = sizing_report(
        real,
        account_capital=float(getattr(settings, "INITIAL_CAPITAL", 10_000.0)),
        risk_pct=float(getattr(settings, "MAX_RISK_PER_TRADE_PCT", 2.0)),
    )
    payload["sizing"] = sizing

    def n(x, d=2):
        return "-" if x is None else f"{x:.{d}f}"

    print(f"  configured risk budget   : Rs {sizing['risk_budget']:.2f}"
          f"  ({sizing['risk_pct']}% of Rs {sizing['account_capital']:.0f})")
    print(f"  positions analysed       : {sizing['positions_usable']}"
          f"  (of {sizing['positions_total']} real)")
    print()
    print("  HOW EACH POSITION GOT ITS SIZE")
    print(f"    via RiskEngine.calculate_position_size : "
          f"{sizing['via_risk_engine_formula']}")
    print(f"    via a caller-supplied quantity         : "
          f"{sizing['via_explicit_quantity']}"
          f"   ({n(sizing['explicit_quantity_pct'], 1)}%)")
    print()
    print("  WHICH BOUND BINDS WHEN THE FORMULA RAN")
    print(f"    capital / affordability cap : "
          f"{sizing['capital_constrained']}"
          f"   ({n(sizing['capital_constrained_pct'], 1)}% of all, "
          f"{n(sizing['capital_constrained_pct_of_formula'], 1)}% of formula-sized)")
    print(f"    risk budget                 : {sizing['risk_constrained']}")
    print()
    print("  RISK ACTUALLY CARRIED")
    print(f"    mean intended risk          : Rs {n(sizing['mean_intended_risk'])}")
    print(f"    mean actual risk            : Rs {n(sizing['mean_actual_risk'])}")
    print(f"    mean budget utilization     : {n(sizing['mean_utilization_percent'])}%")
    print(f"    mean formula utilization    : "
          f"{n(sizing['mean_budget_utilization_percent'])}%")
    rel, un = sizing["reliable_only"], sizing["unreliable_geometry_only"]
    print()
    print("  GEOMETRY PROVENANCE  (the two must not be read as one number)")
    print(f"    exact (entry-time signal stop) : n={rel['n']}"
          f"  mean actual risk Rs {n(rel['mean_actual_risk'])}"
          f"  util {n(rel['mean_utilization_percent'])}%")
    print(f"    ESTIMATE (trailed ledger stop) : n={un['n']}"
          f"  mean actual risk Rs {n(un['mean_actual_risk'])}"
          f"  util {n(un['mean_utilization_percent'])}%")
    print("    an unlinked position has no signal row, so its stop is read from a")
    print("    closed ledger row that Profit Capture may already have moved to")
    print("    breakeven. Those rupee-risk figures are indicative, not exact.")
    print()
    print("  PER POSITION")
    hdr = (f"    {'symbol':<11} {'entry':>9} {'rps':>7} {'riskQ':>6} {'capQ':>6} "
           f"{'formQ':>6} {'actQ':>6} {'actRisk':>8} {'intRisk':>8} {'util%':>7}  path")
    print(hdr)
    print("    " + "-" * (len(hdr) - 4))
    for b in sizing["breakdowns"]:
        print(f"    {b['symbol'][:11]:<11} {b['entry_price']:>9.2f} "
              f"{(b['risk_per_share'] or 0):>7.2f} "
              f"{b['risk_based_quantity']:>6} {b['capital_based_quantity']:>6} "
              f"{b['formula_quantity']:>6} {str(b['actual_quantity']):>6} "
              f"{n(b['actual_rupee_risk']):>8} {n(b['intended_rupee_risk']):>8} "
              f"{n(b['utilization_percent'], 2):>7}  {b['sizing_path']}")

    sh = sizing["shadow"]
    print()
    print(RULE)
    print("  SHADOW COMPARISON - CURRENT SIZING vs RISK-BUDGET-DRIVEN SIZING")
    print(RULE)
    print(f"  {sh['label']}")
    print(f"  {sh['note']}")
    print(f"    positions compared           : {sh['positions_compared']}"
          f"   (of {sizing['positions_usable']} usable; baseline and shadow P&L"
          f" below cover these only)")
    print(f"    mean quantity multiple      : {n(sh['mean_quantity_multiple'])}x")
    print(f"    mean exposure multiple      : {n(sh['mean_exposure_multiple'])}x")
    print(f"    mean SHADOW rupee risk      : Rs {n(sh['mean_shadow_rupee_risk'])}")
    print(f"    mean SHADOW utilization     : "
          f"{n(sh['mean_shadow_utilization_percent'])}%")
    print(f"    realized P&L (as traded)    : Rs {sh['baseline_pnl']:.2f}")
    print(f"    SHADOW P&L (hypothetical)   : Rs {sh['shadow_pnl']:.2f}")
    print(f"    SHADOW delta                : Rs {sh['shadow_pnl_delta']:.2f}")
    print()
    print(f"  NOT PROMOTED: {sh['reason']}")
    print("  Sizing is a risk-INCREASING change. The real ledger is untouched and")
    print("  no shadow figure above is recorded as realized P&L.")


def _print_verdict(base: dict) -> None:
    st = base["strategy_only"]
    linked = base["ai_linked"]
    print()
    print(RULE)
    print("VERDICT")
    print(RULE)
    print(f"  real completed positions (strategy cohort) : "
          f"{st['total_completed_trades']}")
    print(f"  of which AI-linked                        : "
          f"{linked['total_completed_trades']}")
    print(f"  required for a conclusion                 : "
          f"{base['min_trades_for_conclusion']}")
    print()
    if st["total_completed_trades"] < base["min_trades_for_conclusion"]:
        print(f"  {INSUFFICIENT_PROMOTION}")
    print("  No candidate configuration has been promoted.")
    print("  Every candidate remains labelled CANDIDATE and is recorded in")
    print("  the shadow ledger / experiment grids above, awaiting real outcomes.")


def _dump(payload: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    raise SystemExit(main())
