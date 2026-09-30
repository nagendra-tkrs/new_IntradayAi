"""Persistent shadow ledger for the Profit Selection Layer (Phase 11).

Purpose
-------
Record, for every signal the live scanner produces, what the Profit Selection
Layer *would* have decided alongside what actually happened. Nothing in this
module blocks, filters, or alters a production order — it is a write-only
observation ledger that accumulates the evidence a future promotion would
need.

What it stores
--------------
One row per (symbol, decision candle), mirroring the ``signal_24h_comparisons``
convention already used by ``app.services.signal_store``:

    baseline_*   what production decided / did (always, unchanged)
    candidate_*  what the Profit Selection Layer would have decided
    mode         SHADOW or ENFORCE at the time of the record
    applied      False unless the layer was genuinely ENFORCEing (never yet)
    outcome_*    filled in later from the REALIZED ledger, so a future
                 promotion can compare "layer skipped" against "the trade
                 that was actually taken anyway, and what it earned"

Design rules
------------
1. Read-only with respect to trading. This module never calls the order
   endpoints, the risk engine or the paper engine.
2. Idempotent per (symbol, candle_ts) via DELETE-then-INSERT, matching
   ``persist_24h_comparison``, so repeated scans of the same candle cannot
   inflate the sample.
3. Additive only. ``CREATE TABLE IF NOT EXISTS``; no existing table is
   altered, dropped or rewritten. Historical rows are never updated except
   their own ``outcome_*`` columns, which only ever move from NULL to a
   realized observation.
4. Never blocks trading: every write is wrapped so an exception logs and
   returns.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

from app.services.signal_store import _conn, _candle_str, ensure_signal_schema

logger = logging.getLogger(__name__)

_SHADOW_TABLE = "signal_profit_selection_shadow"

_SHADOW_DDL = (
    "CREATE TABLE IF NOT EXISTS signal_profit_selection_shadow ("
    "id VARCHAR(16) NOT NULL, "
    "signal_id VARCHAR(16), "
    "symbol VARCHAR(50) NOT NULL, "
    "candle_ts DATETIME NOT NULL, "
    "generated_at DATETIME, "
    "direction VARCHAR(20), "
    "signal_score FLOAT, "
    "confidence FLOAT, "
    "setup_quality VARCHAR(20), "
    "setup_quality_score FLOAT, "
    "entry_price FLOAT, "
    "stop_loss FLOAT, "
    "target_1 FLOAT, "
    "target_2 FLOAT, "
    "risk_reward FLOAT, "
    "baseline_decision VARCHAR(20), "
    "candidate_decision VARCHAR(20), "
    "mode VARCHAR(20), "
    "applied_to_production INTEGER DEFAULT 0, "
    "would_change_production INTEGER DEFAULT 0, "
    "failed_rules TEXT, "
    "unknown_rules TEXT, "
    "reasons TEXT, "
    "realized_pnl FLOAT, "
    "outcome VARCHAR(20), "
    "outcome_trade_id VARCHAR(16), "
    "resolved_at DATETIME, "
    "PRIMARY KEY (id))"
)


def ensure_shadow_schema(db_path: Optional[str] = None) -> None:
    """Create the shadow ledger if absent. Additive and idempotent."""
    ensure_signal_schema(db_path)
    conn = _conn(db_path)
    if conn is None:
        return
    try:
        conn.execute(_SHADOW_DDL)
        conn.commit()
    except Exception as e:  # never block trading on measurement issues
        logger.warning("ensure_shadow_schema failed: %s", e)
    finally:
        conn.close()


def persist_selection_decision(
    record: dict,
    baseline_decision: str,
    db_path: Optional[str] = None,
) -> Optional[str]:
    """Write one baseline-vs-candidate shadow row. Returns the row id.

    ``record`` is the flattened shape produced by
    ``app.services.profit_selection.shadow_record``. ``baseline_decision`` is
    what production actually did with the signal (``TRADE``/``SKIP``/``NONE``).
    """
    symbol = record.get("symbol")
    candle = _candle_str(record.get("timestamp"))
    if not symbol or not candle:
        return None
    ensure_shadow_schema(db_path)
    conn = _conn(db_path)
    if conn is None:
        return None
    row_id = uuid.uuid4().hex[:16]
    try:
        with conn:
            conn.execute(
                f"DELETE FROM {_SHADOW_TABLE} WHERE symbol = ? AND candle_ts = ?",
                (symbol, candle),
            )
            conn.execute(
                f"INSERT INTO {_SHADOW_TABLE} ("
                "id, signal_id, symbol, candle_ts, generated_at, direction, "
                "signal_score, confidence, setup_quality, setup_quality_score, "
                "entry_price, stop_loss, target_1, target_2, risk_reward, "
                "baseline_decision, candidate_decision, mode, "
                "applied_to_production, would_change_production, failed_rules, "
                "unknown_rules, reasons) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    record.get("signal_id"),
                    symbol,
                    candle,
                    record.get("timestamp"),
                    record.get("direction"),
                    record.get("signal_score"),
                    record.get("confidence"),
                    record.get("setup_quality"),
                    record.get("setup_quality_score"),
                    record.get("entry_price"),
                    record.get("stop_loss"),
                    record.get("target_1"),
                    record.get("target_2"),
                    record.get("risk_reward"),
                    baseline_decision,
                    record.get("decision"),
                    record.get("mode"),
                    1 if record.get("applied_to_production") else 0,
                    1 if record.get("would_change_production") else 0,
                    record.get("failed_rules") or json.dumps([]),
                    record.get("unknown_rules") or json.dumps([]),
                    record.get("reasons") or json.dumps([]),
                ),
            )
    except Exception as e:
        logger.warning("persist_selection_decision failed: %s", e)
        return None
    finally:
        conn.close()
    return row_id


def attach_realized_outcome(
    signal_id: str,
    realized_pnl: float,
    outcome: str,
    trade_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Record the REALIZED result of the trade that the signal produced.

    Called from the same place that already updates ``signals.outcome`` so the
    shadow ledger and the signal row always agree. Only fills columns that are
    currently NULL, so it can never rewrite a recorded observation.
    """
    if not signal_id:
        return False
    conn = _conn(db_path)
    if conn is None:
        return False
    try:
        with conn:
            cur = conn.execute(
                f"UPDATE {_SHADOW_TABLE} SET realized_pnl = ?, outcome = ?, "
                f"outcome_trade_id = ?, resolved_at = ? "
                f"WHERE signal_id = ? AND outcome IS NULL",
                (
                    float(realized_pnl),
                    outcome,
                    trade_id,
                    datetime.now().isoformat(timespec="seconds"),
                    str(signal_id)[:16],
                ),
            )
        return cur.rowcount > 0
    except Exception as e:
        logger.warning("attach_realized_outcome failed: %s", e)
        return False
    finally:
        conn.close()


def load_shadow_records(
    limit: int = 500, db_path: Optional[str] = None
) -> list[dict]:
    """Read back shadow rows (newest first). Read-only."""
    conn = _conn(db_path)
    if conn is None:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM {_SHADOW_TABLE} ORDER BY candle_ts DESC, symbol LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            for key in ("failed_rules", "unknown_rules", "reasons"):
                try:
                    item[key] = json.loads(item.get(key) or "[]")
                except (TypeError, ValueError):
                    item[key] = []
            for key in ("applied_to_production", "would_change_production"):
                item[key] = bool(item.get(key))
            out.append(item)
        return out
    except Exception as e:
        logger.warning("load_shadow_records failed: %s", e)
        return []
    finally:
        conn.close()
