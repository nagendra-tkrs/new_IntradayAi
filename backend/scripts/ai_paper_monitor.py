"""Generate the AI-linked paper-trading performance monitoring report.

Measurement only. This runner reads the LIVE paper-trading SQLite ledger and
the persisted AI recommendation snapshots and writes the monitoring report
(see :mod:`app.services.ai_performance_monitor`) to
``audit-reports/ai-linked-paper-performance-monitoring.md``.

It NEVER places/modifies orders, positions, signals or ledger rows — it opens
the database read-only and only serializes + computes.

The trade/signal rows are serialized through the SAME functions the
``/api/paper/trades`` endpoint uses (``_trade_rows_to_dicts`` +
``_attach_ai_decisions``) so the report can never drift from what the API
serves: every metric is computed over the exact persisted field contract.

Usage (run from the ``backend`` directory)::

    python scripts/ai_paper_monitor.py
    python scripts/ai_paper_monitor.py --json           # print the raw JSON
    python scripts/ai_paper_monitor.py --out path.md    # custom output path
    python scripts/ai_paper_monitor.py --db other.db     # custom ledger
"""

import argparse
import json
import os
import sqlite3
import sys
from types import SimpleNamespace

# Make `app` importable when this file is run directly from scripts/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.services.ai_performance_monitor import (
    compute_monitoring,
    render_markdown,
)
from app.services.paper_trading import _resolve_sqlite_path

# Reuse the production serializers so field names/parsing can never diverge
# from the served API. Importing this module has no DB side effects (the paper
# engine constructor does not connect; accounts are created lazily).
from app.api.trading import _attach_ai_decisions, _trade_rows_to_dicts

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_DEFAULT_OUT = os.path.join(
    _REPO_ROOT, "audit-reports", "ai-linked-paper-performance-monitoring.md"
)

_TRADE_FIELDS = (
    "id", "signal_id", "symbol", "direction", "entry_price", "exit_price",
    "quantity", "stop_loss", "target_1", "target_2", "entry_time",
    "exit_time", "status", "pnl", "user_id", "details_json",
)
_SIGNAL_FIELDS = (
    "id", "symbol", "timestamp", "direction", "signal_score", "confidence",
    "entry_price", "stop_loss", "target_1", "target_2", "risk_reward",
    "strategy", "strategy_version", "data_source", "indicator_scores", "atr",
    "signal_quality", "quantity", "risk_amount", "risk_percent",
)


def _namespace(row, fields):
    """Build a namespace with None defaults so a serialized row is robust to a
    column that does not exist in an older ledger."""
    values = {f: None for f in fields}
    values.update({k: row[k] for k in row.keys()})
    return SimpleNamespace(**values)


def load_live_trades(db_path: str) -> list[dict]:
    """Read the live ledger read-only and return serialized, AI-enriched rows.

    Only realized (closed) rows are returned — open positions never enter
    realized reporting.
    """
    if not db_path or not os.path.exists(db_path):
        raise SystemExit(f"Ledger not found: {db_path}")

    abs_path = os.path.abspath(db_path)
    uri = "file:" + abs_path.replace("\\", "/") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        # Read-only probe: never issues a write, falls back only if the SQLite
        # build cannot open this file read-only (e.g. WAL recovery needed).
        con.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        con = sqlite3.connect(abs_path)
        con.row_factory = sqlite3.Row
    try:
        signal_rows = con.execute("SELECT * FROM signals").fetchall()
        trade_rows = con.execute(
            "SELECT * FROM trades ORDER BY exit_time"
        ).fetchall()
    finally:
        con.close()

    signals = {
        r["id"]: _namespace(r, _SIGNAL_FIELDS) for r in signal_rows
    }
    trade_objs = [
        _namespace(r, _TRADE_FIELDS)
        for r in trade_rows
        if (r["status"] in (None, "", "closed"))
    ]
    trades = _trade_rows_to_dicts(trade_objs)
    return _attach_ai_decisions(trades, signals)


def load_collection_stats(db_path: str, trades: list[dict]) -> dict:
    """Read-only collection status: how much AI decision data is on record and
    how much of it is already linked to a completed trade."""
    abs_path = os.path.abspath(db_path)
    con = sqlite3.connect("file:" + abs_path.replace("\\", "/") + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        totals = con.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN signal_score IS NOT NULL THEN 1 ELSE 0 END) AS scored, "
            "SUM(CASE WHEN strategy_version IS NOT NULL THEN 1 ELSE 0 END) AS versioned "
            "FROM signals"
        ).fetchone()
        linked = {t["signal_id"] for t in trades if t.get("signal_id")}
    finally:
        con.close()
    persisted = int(totals["n"] or 0)
    return {
        "ai_recommendations_persisted": persisted,
        "with_score": int(totals["scored"] or 0),
        "with_strategy_version": int(totals["versioned"] or 0),
        "linked_to_a_trade": len(linked),
        "not_yet_linked_to_a_trade": max(persisted - len(linked), 0),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="SQLite ledger path")
    parser.add_argument("--out", default=_DEFAULT_OUT, help="report output path")
    parser.add_argument(
        "--json", action="store_true", help="print the raw report JSON"
    )
    args = parser.parse_args(argv)

    db_path = args.db or _resolve_sqlite_path(settings.DATABASE_URL)
    trades = load_live_trades(db_path)
    report = compute_monitoring(trades, initial_capital=settings.INITIAL_CAPITAL)
    report["db_path"] = os.path.abspath(db_path)
    try:
        report["collection"] = load_collection_stats(db_path, trades)
    except sqlite3.Error:
        # A ledger without a signals table still yields a valid cohort report.
        report["collection"] = None

    if args.json:
        print(json.dumps(report, indent=2, default=str))

    markdown = render_markdown(report)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(markdown)

    cohort = report["cohort"]
    print(
        f"AI-linked positions: {cohort['ai_linked_positions']} "
        f"(rows {cohort['ai_linked_rows']}); "
        f"manual/unlinked positions: {cohort['manual_or_unlinked_positions']}"
    )
    if not report["sufficient_for_conclusion"]:
        print(report["conclusion"])
    print(f"Report written: {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
