"""Reset all paper-trading accounts back to a clean INITIAL_CAPITAL state.

Wipes the durable paper-trading ledgers so every account replays a fresh
INITIAL_CAPITAL (10,000) cash balance:

 - trades                 : closed paper trades (realized P&L seed)
 - paper_positions        : open positions snapshot
 - paper_pending_orders   : pending orders snapshot

The DB is backed up to a timestamped ``.bak`` BEFORE any deletion.

Usage:
    python scripts/reset_paper_accounts.py            # dry-run (default)
    python scripts/reset_paper_accounts.py --dry-run  # explicit dry-run
    python scripts/reset_paper_accounts.py --yes      # really wipe

Run from the ``backend`` directory so ``app`` and the relative SQLite path
resolve exactly like the API server does.
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

# Make `app` importable when this file is run directly from scripts/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.services.paper_trading import _resolve_sqlite_path

TABLES = ("trades", "paper_positions", "paper_pending_orders")


def _row_counts(con: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in TABLES:
        try:
            counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[t] = 0
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be deleted without changing anything")
    parser.add_argument("--yes", action="store_true",
                        help="Actually perform the reset (no confirmation prompt)")
    args = parser.parse_args()

    if not settings.DATABASE_URL or "sqlite" not in settings.DATABASE_URL:
        print(f"Not a SQLite database: {settings.DATABASE_URL!r}")
        return 2

    db_path = _resolve_sqlite_path(settings.DATABASE_URL)
    if not db_path or not os.path.exists(db_path):
        print(f"Database not found at {db_path!r}")
        return 2

    print(f"Database: {os.path.abspath(db_path)}")
    print(f"INITIAL_CAPITAL = {settings.INITIAL_CAPITAL}")

    con = sqlite3.connect(db_path, timeout=10)
    try:
        counts = _row_counts(con)
        total = sum(counts.values())
        for t in TABLES:
            print(f"  {t:<22} {counts[t]} row(s)")
        print(f"  {'TOTAL':<22} {total} row(s)")

        if args.dry_run:
            print("\nDRY RUN — nothing changed. Re-run with --yes to reset.")
            return 0

        if not args.yes:
            answer = input(f"\nDelete {total} row(s) and start every account at "
                           f"{settings.INITIAL_CAPITAL:.2f} initial capital? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                return 0

        # Safety: require a positive count before deleting (never wipe a
        # pristine DB by accident in the interactive path).
        if total <= 0:
            print("\nNothing to delete — accounts are already clean. No backup made.")
            return 0

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"{db_path}.reset_{stamp}.bak"
        shutil.copy2(db_path, backup)
        print(f"\nBackup written: {backup}")

        with con:
            for t in TABLES:
                con.execute(f"DELETE FROM {t}")
        print("Reset complete: trades, paper_positions and paper_pending_orders "
              "are now empty.")
        print("Every paper account now replays a fresh "
              f"{settings.INITIAL_CAPITAL:.2f} cash balance (no trades ledger to "
              "apply, no positions/pending orders to restore).")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())