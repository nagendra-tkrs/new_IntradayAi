"""NEW-TRADE MEASUREMENT REPORT (READ-ONLY).

Reports only properly-captured realized paper trades - those whose entry-time
risk geometry was written at fill and marked reliable - so the numbers describe
the corrected risk-engine flow rather than a mixture of it with the old
one-share defaults.

Strictly read-only: the database is opened in SQLite read-only mode, no order is
placed, no row is written, and nothing is generated. A trade with no captured
geometry is reported as NOT properly captured with a reason; it is never counted
as zero risk.

SHADOW vs REALIZED: shadow sizing figures are printed in their own labelled
section and are never summed into realized P&L, win rate, expectancy, profit
factor or drawdown. The historical ``promotable`` verdict is not touched here.

Usage
-----
    python scripts/trade_measurement_report.py
    python scripts/trade_measurement_report.py --json-out measurement.json
    python scripts/trade_measurement_report.py --limit 40
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.services.trade_measurement import (  # noqa: E402
    NOT_AVAILABLE,
    build_measurement_report,
)

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


def _num(value, fmt: str = "{:.2f}", na: str = NOT_AVAILABLE) -> str:
    if value is None or isinstance(value, str):
        return na
    try:
        return fmt.format(float(value))
    except (TypeError, ValueError):
        return na


def _stats_line(label: str, stats: dict, width: int = 26) -> str:
    n = stats.get("total_completed_trades", 0)
    return (
        f"  {label:<{width}s} n={n:<4d}"
        f" wr={_num(stats.get('win_rate'), '{:.3f}'):>7s}"
        f" net={_num(stats.get('net_pnl'), '{:>9.2f}'):>10s}"
        f" avg={_num(stats.get('average_pnl_per_trade'), '{:>7.3f}'):>8s}"
        f" exp={_num(stats.get('expectancy'), '{:>7.3f}'):>8s}"
        f" pf={_num(stats.get('profit_factor'), '{:>6.3f}'):>7s}"
        f" dd={_num(stats.get('max_drawdown'), '{:>7.2f}'):>8s}"
        f"  {stats.get('data_status', '')}"
    )


def _load(db_path: str):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        trades = [dict(r) for r in conn.execute("SELECT * FROM trades")]
        signals = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM signals")}
    finally:
        conn.close()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        geometry: dict = {}
        try:
            geometry = {
                r["position_id"]: dict(r)
                for r in conn.execute("SELECT * FROM position_risk_geometry")
            }
        except sqlite3.Error:
            geometry = {}
    finally:
        conn.close()
    return trades, signals, geometry


def _print_trade(t: dict) -> None:
    sig = t["signal"]
    geo = t["entry_geometry"]
    sz = t["sizing"]
    ex = t["exit"]
    print(f"\n  {t['symbol']}  {sig['direction']}  [{t['cohort']}]"
          f"{'' if t['properly_captured'] else '  NOT PROPERLY CAPTURED: ' + str(t['exclusion_reason'])}")
    print(f"    A. trade_id={t['trade_id']}  position_id={t['position_id']}"
          f"  signal_id={t['signal_id']}  strategy_version={t['strategy_version']}")
    print(f"    B. score={sig['score']}  confidence={sig['confidence']}"
          f"  setup_quality={sig['setup_quality']}  signal_ts={sig['signal_timestamp']}")
    print(f"    C. entry={_num(geo['entry_price'])}  initial_SL={_num(geo['initial_stop_loss'])}"
          f"  initial_T1={_num(geo['initial_target_1'])}  initial_T2={_num(geo['initial_target_2'])}")
    print(f"       risk/share={_num(geo['risk_per_share'])}  qty={_num(geo['quantity'], '{:.0f}')}"
          f"  initial_risk={_num(geo['initial_risk'])}  risk%={_num(geo['configured_risk_percent'], '{:.2f}')}")
    print(f"    D. sizing_mode={sz['sizing_mode']}  budget={_num(sz['risk_budget'])}"
          f"  risk_q={_num(sz['risk_constraint_quantity'], '{:.0f}')}"
          f"  capital_q={_num(sz['capital_constraint_quantity'], '{:.0f}')}"
          f"  allowed_q={_num(sz['allowed_quantity'], '{:.0f}')}")
    print(f"       binding={sz['binding_constraint']}"
          f"  risk_util={_num(sz['risk_utilization_percent'], '{:.1f}')}"
          f"  capital_util={_num(sz['capital_utilization_percent'], '{:.1f}')}")
    print(f"    E. exit={_num(ex['exit_price'])}  reason={ex['exit_reason']}"
          f"  held={_num(ex['holding_minutes'], '{:.1f}')}m"
          f"  T1={ex['t1_reached']}  trailing={ex['trailing_activated']}")
    print(f"       final_SL={_num(ex['final_stop_loss'])} (moved: {ex['stop_moved_from_entry']})"
          f"  realized_pnl={_num(ex['realized_pnl'])}  R={_num(ex['r_multiple'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json-out", default=None,
                    help="also write the full machine-readable report here")
    ap.add_argument("--limit", type=int, default=25,
                    help="how many per-trade records to print (default 25)")
    args = ap.parse_args()

    db_path = _resolve_db_path()
    print(RULE)
    print("INTRAFYAI - NEW-TRADE MEASUREMENT REPORT (REALIZED, READ-ONLY)")
    print(RULE)
    print(f"  database : {db_path}")
    print(f"  generated: {datetime.now().isoformat(timespec='seconds')}")
    if not os.path.exists(db_path):
        print("  ERROR: database not found")
        return 1

    trades, signals, geometry = _load(db_path)
    rep = build_measurement_report(trades, signals, geometry)

    cap = rep["capture"]
    print()
    print(RULE)
    print("1. CAPTURE STATUS - how much of the ledger is actually measurable")
    print(RULE)
    print(f"  closed ledger rows            : {cap['closed_ledger_rows']}")
    print(f"  positions after slice-collapse: {cap['positions_after_collapse']}")
    print(f"  PROPERLY CAPTURED             : {cap['properly_captured']}")
    print(f"  not properly captured         : {cap['not_properly_captured']}"
          f"   {cap['exclusion_reasons'] or ''}")
    print(f"  geometry rows in ledger       : {cap['geometry_rows_in_ledger']}"
          f" (reliable: {cap['geometry_rows_reliable']})")
    print(f"  RISK_ENGINE trades            : {cap['risk_engine_trades']}")
    print(f"  MANUAL trades                 : {cap['manual_trades']}")
    print(f"  AI-linked trades              : {cap['ai_linked_trades']}")
    print(f"  test-fixture trades (excluded): {cap['test_fixture_trades_excluded']}")
    print(f"  samples required to conclude  : {rep['min_trades_for_conclusion']}")
    print(f"  VERDICT                       : {rep['promotion_verdict']}")

    print()
    print(RULE)
    print("2. PERFORMANCE (properly captured trades only)")
    print(RULE)
    for label, key in (
        ("ALL PROPERLY CAPTURED", "all_properly_captured"),
        ("STRATEGY COHORT", "strategy_cohort"),
        ("AI-LINKED", "ai_linked"),
        ("test fixtures (excluded)", "test_fixture_excluded"),
    ):
        print(_stats_line(label, rep["performance"][key]))
    st = rep["performance"]["strategy_cohort"]
    print()
    print(f"  gross profit {_num(st['gross_profit'])} | gross loss {_num(st['gross_loss'])}"
          f" | net {_num(st['net_pnl'])}")
    print(f"  avg winner {_num(st['average_winner'])} | avg loser {_num(st['average_loser'])}")
    print(f"  avg holding {_num(st['average_holding_minutes'], '{:.2f}')}m"
          f" | median holding {_num(st['median_holding_minutes'], '{:.2f}')}"
          f"m | median pnl {_num(st['median_pnl'])}")
    print(f"  risk utilization (mean)      : {_num(st['risk_utilization_percent_mean'], '{:.1f}')}%")
    print(f"  capital utilization (mean)   : {_num(st['capital_utilization_percent_mean'], '{:.1f}')}%")
    print(f"  initial risk total           : {_num(st['initial_risk_total'])}"
          f"  (observed on {st['initial_risk_observed']}/{st['total_completed_trades']} trades)")
    print(f"  R multiple mean / median     : {_num(st['r_multiple_mean'])}"
          f" / {_num(st['r_multiple_median'])}"
          f"  (observed {st['r_multiple_observed']}, missing {st['r_multiple_missing']})")
    print(f"  return on capital (mean)     : {_num(st['return_on_capital_percent_mean'], '{:.3f}')}%")
    print(f"  exit-reason mix              : {st['exit_reason_counts']}")

    print()
    print(RULE)
    print("3. BREAKDOWNS (sample sizes shown; tiny samples are NOT ranked)")
    print(RULE)
    for name, groups in rep["breakdowns"]["strategy_cohort"].items():
        print(f"\n  -- by {name} --")
        for label, stats in groups.items():
            print(_stats_line(label, stats, width=22))

    print()
    print(RULE)
    print("4. SIZING MODE - RISK_ENGINE vs MANUAL (never merged)")
    print(RULE)
    print(f"  {rep['sizing_modes']['_note']}")
    for mode in ("RISK_ENGINE", "MANUAL", NOT_AVAILABLE):
        g = rep["sizing_modes"][mode]
        print()
        print(f"  {mode}: n={g['trades']}")
        print(f"    total pnl            : {_num(g.get('total_pnl'))}")
        print(f"    average pnl          : {_num(g.get('average_pnl'))}")
        print(f"    win rate             : {_num(g.get('win_rate'), '{:.3f}')}")
        print(f"    profit factor        : {_num(g.get('profit_factor'), '{:.3f}')}")
        print(f"    expectancy           : {_num(g.get('expectancy'), '{:.3f}')}")
        print(f"    avg risk utilization : {_num(g.get('average_risk_utilization_percent'), '{:.1f}')}%")
        print(f"    avg capital util     : {_num(g.get('average_capital_utilization_percent'), '{:.1f}')}%")
        n = g.get("normalized")
        if n:
            print(f"    NORMALIZED R mean    : {_num(n['r_multiple_mean'])}"
                  f"  (median {_num(n['r_multiple_median'])}, observed {n['r_multiple_observed']})")
            print(f"    NORMALIZED RoC mean  : {_num(n['return_on_capital_percent_mean'], '{:.3f}')}%")
            print(f"    NORMALIZED risk mean : {_num(n['initial_risk_mean'])}")
        print(f"    status               : {g.get('data_status', 'n/a')}")

    print()
    print(RULE)
    print("5. ATTRIBUTION - signal -> order -> position -> geometry -> trade -> P&L")
    print(RULE)
    attr = rep["attribution"]
    print(f"  chain  : {attr['chain']}")
    for k, v in attr["join_keys"].items():
        print(f"    {k:<16s}: {v}")
    print()
    for field, cov in attr["field_coverage"].items():
        pctv = _num(cov["completeness_percent"], "{:.1f}")
        print(f"  {field:<26s} present {cov['present']:>3d}/{cov['total']:<3d}"
              f"  missing {cov['missing']:>3d}  {pctv}%")
    gc = attr["geometry_consistency"]
    print()
    print(f"  geometry rows read                     : {gc['geometry_rows_read']}")
    print(f"  positions without geometry             : {gc['positions_without_geometry']}")
    print(f"  stops moved by Profit Capture          : {gc['stop_moved_by_profit_capture']}")
    print(f"  geometry self-consistent  [TRIPWIRE]   : {gc['geometry_self_consistent']}"
          f"   (incoherent rows: {gc['incoherent_geometry_rows']})")
    print(f"  frozen stop differs from live stop     : {gc['frozen_stop_differs_from_live']}")
    print(f"  frozen stop equals live stop           : {gc['frozen_stop_equals_live']}"
          f"   (normal when the stop never moved)")

    print()
    print(RULE)
    print("6. SHADOW - REFERENCE ONLY, NEVER REALIZED")
    print(RULE)
    sh = rep["shadow"]
    print(f"  label                : {sh['labelled']}")
    print(f"  trades with shadow   : {sh['trades_with_shadow']}")
    print(f"  shadow initial risk  : {_num(sh['shadow_initial_risk_mean'])} (mean)")
    print(f"  realized initial risk: {_num(sh['realized_initial_risk_mean'])} (mean)")
    print(f"  booked as realized   : {sh['booked_as_realized_pnl']}")
    print(f"  historical promotable unchanged: {sh['historical_promotable_unchanged']}")

    print()
    print(RULE)
    print(f"7. PER-TRADE RECORDS (first {args.limit})")
    print(RULE)
    captured = [t for t in rep["trades"] if t["properly_captured"]]
    shown = captured[: args.limit] if captured else rep["trades"][: args.limit]
    if not shown:
        print("  (no completed trades to show)")
    for t in shown:
        _print_trade(t)
    if len(captured) > args.limit:
        print(f"\n  ... {len(captured) - args.limit} more properly-captured trades omitted "
              f"(--limit {args.limit})")

    print()
    print(RULE)
    print(f"VERDICT: {rep['promotion_verdict']}")
    print(RULE)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, default=str)
        print(f"  wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())