"""Durable, append-only ledger of ENTRY-TIME risk geometry.

Purpose
-------
Risk-budget utilization could only ever be measured approximately, because a
closed ``trades`` row records the stop as it stood at **exit**. Profit Capture
rewrites that stop: ``apply_t1_protection`` pulls it to entry the moment T1
fills, then ``update_trailing_stop`` chases it up. For any position that
reached T1, ``abs(entry_price - stop_loss)`` collapses toward zero, so the
original risk per share is simply not recoverable from the ledger.

This module closes that gap by capturing the geometry **once**, at the moment
an order actually becomes an open position, and storing it immutably.

What it stores
--------------
One row per filled position (``position_id`` primary key), containing the
entry-time levels, the sizing arithmetic that produced the order, and the
SHADOW counterfactual sized off the risk budget alone.

Why a separate table rather than more columns on ``trades``
-----------------------------------------------------------
1. ``trades`` is historical performance history. This task is forbidden to
   rewrite historical trade rows, and adding columns there would create a
   table where most rows are permanently NULL and some are populated - an
   invitation to read a historical row's NULL as "zero risk".
2. ``paper_positions`` is a write-through SNAPSHOT: every
   :func:`persist_account_state` call DELETEs the user's rows and re-INSERTs
   the live set, and a closed position's row disappears entirely. Geometry
   captured there would vanish at exactly the moment it becomes most useful.
3. A dedicated table is joinable to ``trades`` on ``position_id`` (which
   ``trading._paper_details`` already writes into ``details_json``) and is
   queryable in SQL, so a promoted risk policy can be evaluated against real
   entry-time arithmetic rather than a trailing-stop approximation.

Design rules
------------
1. **Additive only.** ``CREATE TABLE IF NOT EXISTS``. No existing table is
   altered, dropped or rebuilt.
2. **Insert-once.** The primary key is ``position_id`` and the write path uses
   ``INSERT OR IGNORE``. A capture can never be updated, so nothing that runs
   later in the position's life can overwrite the entry-time values - that is
   the whole guarantee this table exists to provide.
3. **No backfill.** Historical positions are absent by design. Absence means
   "geometry not captured", and callers must treat it as
   ``geometry_reliable = False`` rather than reconstructing a number.
4. **Never blocks trading.** Every write is wrapped; a failure logs and returns
   ``None`` and the order proceeds exactly as it would have.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from app.services.signal_store import _conn, ensure_signal_schema

logger = logging.getLogger(__name__)

GEOMETRY_TABLE = "position_risk_geometry"

#: Written at capture time so a reader can tell a row produced by the live
#: order/fill path from anything that might ever be derived later.
SOURCE_ORDER_FILL = "ORDER_FILL"

_GEOMETRY_DDL = (
    "CREATE TABLE IF NOT EXISTS position_risk_geometry ("
    "position_id VARCHAR(16) NOT NULL, "
    "user_id VARCHAR(16), "
    "signal_id VARCHAR(16), "
    "symbol VARCHAR(50) NOT NULL, "
    "direction VARCHAR(10), "
    "captured_at DATETIME, "
    "capture_source VARCHAR(20), "
    # -- entry-time levels, immutable for the life of the position
    "entry_price FLOAT, "
    "initial_stop_loss FLOAT, "
    "initial_target_1 FLOAT, "
    "initial_target_2 FLOAT, "
    "initial_risk_per_share FLOAT, "
    "initial_risk_amount FLOAT, "
    "initial_quantity FLOAT, "
    # -- the sizing arithmetic that produced this order
    "account_capital FLOAT, "
    "configured_risk_percent FLOAT, "
    "risk_budget FLOAT, "
    "risk_constraint_quantity INTEGER, "
    "capital_constraint_quantity INTEGER, "
    "allowed_quantity INTEGER, "
    "actual_quantity FLOAT, "
    "intended_risk FLOAT, "
    "actual_risk FLOAT, "
    "risk_budget_utilization FLOAT, "
    "order_classification VARCHAR(24), "
    "binding_constraint VARCHAR(20), "
    "quantity_source VARCHAR(20), "
    # -- SHADOW only. Never executed, never realized P&L.
    "shadow_quantity INTEGER, "
    "shadow_exposure FLOAT, "
    "shadow_initial_risk FLOAT, "
    "shadow_utilization FLOAT, "
    "current_exposure FLOAT, "
    "geometry_reliable INTEGER DEFAULT 1, "
    "PRIMARY KEY (position_id))"
)

#: Column order for the INSERT. Kept adjacent to the DDL so the two cannot drift.
_COLUMNS = (
    "position_id", "user_id", "signal_id", "symbol", "direction",
    "captured_at", "capture_source",
    "entry_price", "initial_stop_loss", "initial_target_1", "initial_target_2",
    "initial_risk_per_share", "initial_risk_amount", "initial_quantity",
    "account_capital", "configured_risk_percent", "risk_budget",
    "risk_constraint_quantity", "capital_constraint_quantity", "allowed_quantity",
    "actual_quantity", "intended_risk", "actual_risk", "risk_budget_utilization",
    "order_classification", "binding_constraint", "quantity_source",
    "shadow_quantity", "shadow_exposure", "shadow_initial_risk",
    "shadow_utilization", "current_exposure", "geometry_reliable",
)


def ensure_geometry_schema(db_path: Optional[str] = None) -> None:
    """Create the entry-geometry ledger if absent. Additive and idempotent."""
    ensure_signal_schema(db_path)
    conn = _conn(db_path)
    if conn is None:
        return
    try:
        conn.execute(_GEOMETRY_DDL)
        conn.commit()
    except Exception as e:  # never block trading on measurement issues
        logger.warning("ensure_geometry_schema failed: %s", e)
    finally:
        conn.close()


def persist_entry_geometry(
    geometry,
    db_path: Optional[str] = None,
) -> bool:
    """Insert one entry-time geometry row. Returns True when a row was written.

    Idempotent by ``position_id`` (``INSERT OR IGNORE``): a second call for the
    same position is a no-op, so re-filling, restarting, or a partially
    completed close can never mutate a value already captured.

    A row with no usable geometry (``geometry.usable`` False) is still written,
    with ``geometry_reliable = 0``. Recording the *fact* that the geometry could
    not be established is more honest than leaving no row and letting a reader
    assume the position predates the capture.
    """
    if geometry is None or not getattr(geometry, "position_id", None):
        return False
    ensure_geometry_schema(db_path)
    conn = _conn(db_path)
    if conn is None:
        return False
    values = dict(getattr(geometry, "to_row", lambda: {})())
    placeholders = ",".join("?" for _ in _COLUMNS)
    sql = (
        f"INSERT OR IGNORE INTO {GEOMETRY_TABLE} "
        f"({','.join(_COLUMNS)}) VALUES ({placeholders})"
    )
    try:
        with conn:
            cur = conn.execute(sql, tuple(values.get(c) for c in _COLUMNS))
        return bool(cur.rowcount)
    except Exception as e:  # never block trading on measurement issues
        logger.warning("persist_entry_geometry failed: %s", e)
        return False
    finally:
        conn.close()


def load_geometry_for(
    position_id: str, db_path: Optional[str] = None
) -> Optional[dict]:
    """Read the captured entry-time geometry for one position, or None."""
    if not position_id:
        return None
    ensure_geometry_schema(db_path)
    conn = _conn(db_path)
    if conn is None:
        return None
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT * FROM {GEOMETRY_TABLE} WHERE position_id = ?",
            (position_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.warning("load_geometry_for failed: %s", e)
        return None
    finally:
        conn.close()


def load_geometry_for_user(
    user_id: str, db_path: Optional[str] = None
) -> dict:
    """All captured entry geometry for one user, keyed by ``position_id``.

    Read-only, scoped to one user so a measurement endpoint never serves another
    account's rows. Returns {} when the user has no captured geometry - the
    caller must treat that as "never captured", not as "zero risk".
    """
    if not user_id:
        return {}
    ensure_geometry_schema(db_path)
    conn = _conn(db_path)
    if conn is None:
        return {}
    try:
        conn.row_factory = sqlite3.Row
        return {
            r["position_id"]: dict(r)
            for r in conn.execute(
                f"SELECT * FROM {GEOMETRY_TABLE} WHERE user_id = ?", (user_id,)
            )
        }
    except Exception as e:
        logger.warning("load_geometry_for_user failed: %s", e)
        return {}
    finally:
        conn.close()


def load_all_geometry(db_path: Optional[str] = None) -> dict:
    """All captured entry-time geometry, keyed by ``position_id``.

    Read-only convenience for the report. Positions absent from the result were
    never captured (or predate the capture) and MUST be treated as
    ``geometry_reliable = False`` - never reconstructed.
    """
    ensure_geometry_schema(db_path)
    conn = _conn(db_path)
    if conn is None:
        return {}
    try:
        conn.row_factory = sqlite3.Row
        return {
            r["position_id"]: dict(r)
            for r in conn.execute(f"SELECT * FROM {GEOMETRY_TABLE}")
        }
    except Exception as e:
        logger.warning("load_all_geometry failed: %s", e)
        return {}
    finally:
        conn.close()
