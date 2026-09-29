"""Persistent traceability store for AI recommendations (Mode A) and the
24-hour-context shadow comparisons (Mode B).

What this module does (all additive — nothing already in the production DB is
ever deleted, rewritten, or reset):

  * ``ensure_signal_schema`` — idempotent additive migration:
      - adds ``signals.signal_quality / quantity / risk_amount`` (the live
        ``signals`` table already exists; historical rows stay untouched),
      - adds ``signal_id`` to ``paper_positions`` / ``paper_pending_orders``
        so the AI→order→position link survives server restarts,
      - creates ``signal_24h_comparisons`` (Mode B shadow ledger).
  * ``persist_signal`` — writes the CURRENT (Mode A) recommendation per
    (symbol, decision candle); one row, never duplicated.
  * ``persist_24h_comparison`` — records the Mode A vs Mode B shadow pair at
    the same candle (the 24H variant never places paper orders).
  * ``update_signal_usage`` / ``mark_signal_outcome`` — update a signal row
    only when an order explicitly links its ``signal_id`` (the link the
    scanner pages now send with every order placed from a recommendation).

Conventions
-----------
* ``signals.timestamp`` stores the DECISION CANDLE's start time (IST, e.g.
  "2026-09-29 13:50:00") — the exact 5-minute bar the recommendation was made
  on — so repeated scans of the same candle can never create duplicate rows.
  ``signal_generated_at`` (wall clock) is kept inside ``indicator_scores``
  JSON.
* Writes use a synchronous sqlite3 connection resolved from
  ``settings.DATABASE_URL`` (same resolver the paper-trading persistence
  layer uses), because the scanner's per-symbol processing pipeline is
  synchronous.
"""

import json
import logging
import os
import sqlite3
import uuid
from typing import Optional

from app.core.config import settings
from app.services.paper_trading import _resolve_sqlite_path

logger = logging.getLogger(__name__)

_SIGNALS_TABLE = "signals"
_COMPARISONS_TABLE = "signal_24h_comparisons"
_POSITIONS_TABLE = "paper_positions"
_PENDING_TABLE = "paper_pending_orders"

# Additive columns (idempotent ALTER when missing).
_SIGNALS_ADD_COLUMNS = {
    "signal_quality": "VARCHAR(20)",
    "quantity": "FLOAT",
    "risk_amount": "FLOAT",
    "atr": "FLOAT",
    "strategy_version": "VARCHAR(20)",
    "risk_percent": "FLOAT",
}
_POSITION_ADD_COLUMNS = {"signal_id": "VARCHAR(16)"}

# Mirrors app.models.models.Signal column-for-column (plus the additive cols).
_SIGNALS_DDL = (
    "CREATE TABLE IF NOT EXISTS signals ("
    "id VARCHAR(16) NOT NULL, "
    "symbol VARCHAR(50) NOT NULL, "
    "timestamp DATETIME NOT NULL, "
    "direction VARCHAR(20) NOT NULL, "
    "signal_score FLOAT NOT NULL, "
    "confidence FLOAT NOT NULL, "
    "entry_price FLOAT, "
    "stop_loss FLOAT, "
    "target_1 FLOAT, "
    "target_2 FLOAT, "
    "risk_reward FLOAT, "
    "strategy VARCHAR(100), "
    "explanation TEXT, "
    "indicator_scores TEXT, "
    "market_context TEXT, "
    "data_source VARCHAR(20), "
    "outcome VARCHAR(20), "
    "realized_pnl FLOAT, "
    "holding_duration INTEGER, "
    "signal_quality VARCHAR(20), "
    "quantity FLOAT, "
    "risk_amount FLOAT, "
    "atr FLOAT, "
    "strategy_version VARCHAR(20), "
    "risk_percent FLOAT, "
    "PRIMARY KEY (id))"
)

# Mirrors app.models.models.Signal24hComparison column-for-column.
_COMPARISONS_DDL = (
    "CREATE TABLE IF NOT EXISTS signal_24h_comparisons ("
    "id VARCHAR(16) NOT NULL, "
    "symbol VARCHAR(50) NOT NULL, "
    "candle_ts DATETIME NOT NULL, "
    "generated_at DATETIME, "
    "current_direction VARCHAR(20), "
    "current_score FLOAT, "
    "current_confidence FLOAT, "
    "current_quality VARCHAR(20), "
    "current_entry FLOAT, "
    "current_sl FLOAT, "
    "current_t1 FLOAT, "
    "current_t2 FLOAT, "
    "current_rr FLOAT, "
    "ctx24_direction VARCHAR(20), "
    "ctx24_score FLOAT, "
    "ctx24_confidence FLOAT, "
    "ctx24_quality VARCHAR(20), "
    "ctx24_entry FLOAT, "
    "ctx24_sl FLOAT, "
    "ctx24_t1 FLOAT, "
    "ctx24_t2 FLOAT, "
    "ctx24_rr FLOAT, "
    "context_24h TEXT, "
    "current_evidence TEXT, "
    "ctx24_evidence TEXT, "
    "agreement INTEGER DEFAULT 0, "
    "PRIMARY KEY (id))"
)

_schema_ensured = False


def _conn(db_path: Optional[str] = None) -> Optional[sqlite3.Connection]:
    """Open a write connection to the traceability ledger.

    Guard: when no explicit ``db_path`` is given AND the process is running
    under pytest, writes are skipped (None). Pre-existing phase-1-6 scanner
    tests (test_comprehensive.py) drive ``MarketScanner`` over synthetic
    symbols (A/B/C/S0..S24); without this guard every full-suite run would
    persist fake recommendations into the shared production ledger
    (backend/intradayai.db). Production (uvicorn) is never under pytest, so
    the scanner always persists there. Tests that want real persistence pass
    an explicit ``db_path`` (tmp) and bypass the guard."""
    if db_path is None:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return None
        db_path = _resolve_sqlite_path(settings.DATABASE_URL)
    if not db_path:
        return None
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    return sqlite3.connect(db_path, timeout=10)


def ensure_signal_schema(db_path: Optional[str] = None) -> None:
    """Idempotent additive migration (module docstring). Safe to run at every
    startup and lazily before any write."""
    conn = _conn(db_path)
    if conn is None:
        return
    try:
        conn.execute(_SIGNALS_DDL)
        conn.execute(_COMPARISONS_DDL)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table, cols in {
            _SIGNALS_TABLE: _SIGNALS_ADD_COLUMNS,
            _POSITIONS_TABLE: _POSITION_ADD_COLUMNS,
            _PENDING_TABLE: _POSITION_ADD_COLUMNS,
        }.items():
            if table not in tables:
                continue  # table not present yet (fresh DB) — nothing to migrate
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, ddl in cols.items():
                if name not in existing:
                    try:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    except Exception as e:
                        logger.warning(
                            "ensure_signal_schema ALTER %s.%s failed: %s", table, name, e
                        )
        conn.commit()
    except Exception as e:  # never block trading on traceability issues
        logger.warning("ensure_signal_schema failed: %s", e)
    finally:
        conn.close()


def _candle_str(ts) -> Optional[str]:
    """Normalize a decision-candle timestamp to the ledger's wall-clock string
    (e.g. "2026-09-29 13:50:00"), matching how SQLite stores DATETIME."""
    if ts is None:
        return None
    try:
        import pandas as pd
        dt = pd.Timestamp(ts)
    except Exception:
        return str(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _lazy_float(value) -> Optional[float]:
    """float() with nan/None guarding (used for optional numeric columns)."""
    if value is None:
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if val != val:  # NaN
        return None
    return val


def persist_signal(
    signal: dict,
    candle_ts,
    market_ctx: Optional[dict] = None,
    quality: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Persist one CURRENT (Mode A) recommendation, upserted per decision
    candle so repeated scans of the same 5-minute bar never duplicate."""
    if not signal or not signal.get("id"):
        return
    ensure_signal_schema(db_path)
    conn = _conn(db_path)
    if conn is None:
        return
    symbol = signal.get("symbol", "")
    candle_dt = _candle_str(candle_ts) or signal.get("timestamp")
    if not candle_dt:
        conn.close()
        return
    score = signal.get("signal_score") or {}
    setup = signal.get("setup") or {}
    explanation = signal.get("explanation") or {}
    indicator_payload, market_payload = _json_roundtrip_safe(
        signal, market_ctx, score, setup, explanation
    )
    data_source = signal.get("data_source")
    if hasattr(data_source, "value"):
        data_source = data_source.value
    try:
        with conn:
            conn.execute(
                "DELETE FROM signals WHERE symbol = ? AND timestamp = ?",
                (symbol, candle_dt),
            )
            conn.execute(
                "INSERT INTO signals (id, symbol, timestamp, direction, signal_score, "
                "confidence, entry_price, stop_loss, target_1, target_2, risk_reward, "
                "strategy, explanation, indicator_scores, market_context, data_source, "
                "outcome, realized_pnl, holding_duration, signal_quality, quantity, "
                "risk_amount, atr, strategy_version, risk_percent) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(signal["id"])[:16],
                    symbol,
                    candle_dt,
                    signal.get("direction", ""),
                    float(score.get("total", 0) or 0),
                    float(signal.get("confidence", 0) or 0),
                    setup.get("entry"),
                    setup.get("stop_loss"),
                    setup.get("target_1"),
                    setup.get("target_2"),
                    setup.get("risk_reward_ratio"),
                    signal.get("strategy"),
                    json.dumps(explanation, default=str),
                    json.dumps(indicator_payload, default=str),
                    json.dumps(market_payload, default=str),
                    data_source,
                    "pending",
                    0.0,
                    0,
                    quality,
                    None,
                    None,
                    _lazy_float(
                        (signal.get("indicator_values") or {}).get("atr_14")
                    ),
                    signal.get("strategy_version"),
                    None,
                ),
            )
    except Exception as e:
        logger.warning("persist_signal failed for %s: %s", symbol, e)
    finally:
        conn.close()


def _json_roundtrip_safe(signal, market_ctx, score, setup, explanation):
    """Build the JSON payloads for persist_signal (kept together for clarity)."""
    indicator_payload = {
        "trend_score": score.get("trend_score"),
        "momentum_score": score.get("momentum_score"),
        "volume_score": score.get("volume_score"),
        "vwap_score": score.get("vwap_score"),
        "price_action_score": score.get("price_action_score"),
        "market_context_score": score.get("market_context_score"),
        "risk_quality_score": score.get("risk_quality_score"),
        "signal_generated_at": signal.get("signal_generated_at") or signal.get("timestamp"),
        "data_timestamp": signal.get("market_data_timestamp"),
        "risk_reward_ratio": setup.get("risk_reward_ratio"),
        "risk_per_share": setup.get("risk_per_share"),
        "atr": _lazy_float((signal.get("indicator_values") or {}).get("atr_14")),
    }
    # Complete signed-direction evidence captured at decision time (never
    # recomputed later): per-component nets, long/short totals, the blended
    # net, conflict flag and the direction-neutral risk gate.
    ev = signal.get("direction_evidence")
    if ev is not None:
        try:
            dump = ev.model_dump() if hasattr(ev, "model_dump") else dict(ev)
        except Exception:
            dump = {}
        if dump:
            indicator_payload["nets"] = dump.get("nets")
            indicator_payload["net"] = _lazy_float(dump.get("net"))
            indicator_payload["long_total"] = _lazy_float(dump.get("long_total"))
            indicator_payload["short_total"] = _lazy_float(dump.get("short_total"))
            indicator_payload["conflict"] = bool(dump.get("conflict"))
            indicator_payload["evidence_risk_quality"] = dump.get("risk_quality")
    market_payload = {
        "nifty": market_ctx or {},
        "context_24h": signal.get("context_24h"),
        "explanation": explanation,
    }
    return indicator_payload, market_payload


def update_signal_usage(signal_id: Optional[str], quantity: Optional[float],
                        risk_amount: Optional[float], risk_percent: Optional[float] = None,
                        db_path: Optional[str] = None) -> None:
    """Record order-usage on a signals row when an order explicitly references
    its signal_id (quantity + risk_amount = qty × |entry − SL|, and the
    effective risk_percent = risk_amount / portfolio total value × 100)."""
    if not signal_id:
        return
    ensure_signal_schema(db_path)
    conn = _conn(db_path)
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                "UPDATE signals SET quantity = ?, risk_amount = ?, risk_percent = ? "
                "WHERE id = ?",
                (quantity, risk_amount, risk_percent, str(signal_id)[:16]),
            )
    except Exception as e:
        logger.warning("update_signal_usage failed: %s", e)
    finally:
        conn.close()


def mark_signal_outcome(signal_id: Optional[str], pnl: float,
                        holding_minutes: Optional[int], db_path: Optional[str] = None) -> None:
    """Mark the outcome of a trade that referenced a signal_id (WIN / LOSS /
    BREAKEVEN, realized P&L, holding duration in minutes)."""
    if not signal_id:
        return
    ensure_signal_schema(db_path)
    conn = _conn(db_path)
    if conn is None:
        return
    outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
    try:
        with conn:
            conn.execute(
                "UPDATE signals SET outcome = ?, realized_pnl = ?, holding_duration = ? "
                "WHERE id = ?",
                (outcome, round(float(pnl), 2), int(holding_minutes or 0), str(signal_id)[:16]),
            )
    except Exception as e:
        logger.warning("mark_signal_outcome failed: %s", e)
    finally:
        conn.close()


def direction_family(direction: Optional[str]) -> str:
    """Collapse signal directions to LONG / SHORT / NO_TRADE for agreement and
    per-side statistics."""
    d = str(direction or "").upper()
    if d in ("LONG", "STRONG_LONG", "WEAK_LONG", "BUY"):
        return "LONG"
    if d in ("SHORT", "STRONG_SHORT", "WEAK_SHORT", "SELL"):
        return "SHORT"
    return "NO_TRADE"


def _evidence_json(signal: Optional[dict]) -> str:
    if not signal:
        return ""
    evidence = signal.get("direction_evidence")
    try:
        if evidence is None:
            return ""
        dump = evidence.model_dump() if hasattr(evidence, "model_dump") else dict(evidence)
        return json.dumps({k: (float(v) if isinstance(v, (int, float)) else v)
                           for k, v in dump.items()}, default=str)
    except Exception:
        return ""


def persist_24h_comparison(
    signal: dict,
    signal_24h: dict,
    context_24h: Optional[dict],
    candle_ts,
    quality: Optional[str] = None,
    quality24: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Record the Mode A vs Mode B shadow pair at the same decision candle."""
    if not signal:
        return
    if not signal_24h:
        signal_24h = {"direction": "NO_TRADE", "confidence": 0.0,
                      "signal_score": {}, "setup": {}}
    ensure_signal_schema(db_path)
    conn = _conn(db_path)
    if conn is None:
        return
    symbol = signal.get("symbol", "")
    candle_dt = _candle_str(candle_ts) or signal.get("timestamp")
    if not candle_dt:
        conn.close()
        return
    s = signal.get("signal_score") or {}
    s24 = signal_24h.get("signal_score") or {}
    s_setup = signal.get("setup") or {}
    s24_setup = signal_24h.get("setup") or {}
    row_id = uuid.uuid4().hex[:16]
    try:
        with conn:
            conn.execute(
                "DELETE FROM signal_24h_comparisons WHERE symbol = ? AND candle_ts = ?",
                (symbol, candle_dt),
            )
            conn.execute(
                "INSERT INTO signal_24h_comparisons (id, symbol, candle_ts, generated_at, "
                "current_direction, current_score, current_confidence, current_quality, "
                "current_entry, current_sl, current_t1, current_t2, current_rr, "
                "ctx24_direction, ctx24_score, ctx24_confidence, ctx24_quality, "
                "ctx24_entry, ctx24_sl, ctx24_t1, ctx24_t2, ctx24_rr, "
                "context_24h, current_evidence, ctx24_evidence, agreement) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    symbol,
                    candle_dt,
                    signal.get("signal_generated_at") or signal.get("timestamp"),
                    signal.get("direction", ""),
                    float(s.get("total", 0) or 0),
                    float(signal.get("confidence", 0) or 0),
                    quality,
                    s_setup.get("entry"),
                    s_setup.get("stop_loss"),
                    s_setup.get("target_1"),
                    s_setup.get("target_2"),
                    s_setup.get("risk_reward_ratio"),
                    signal_24h.get("direction", ""),
                    float(s24.get("total", 0) or 0),
                    float(signal_24h.get("confidence", 0) or 0),
                    quality24,
                    s24_setup.get("entry"),
                    s24_setup.get("stop_loss"),
                    s24_setup.get("target_1"),
                    s24_setup.get("target_2"),
                    s24_setup.get("risk_reward_ratio"),
                    json.dumps(context_24h or {}, default=str),
                    _evidence_json(signal),
                    _evidence_json(signal_24h),
                    1 if direction_family(signal.get("direction")) ==
                    direction_family(signal_24h.get("direction")) else 0,
                ),
            )
    except Exception as e:
        logger.warning("persist_24h_comparison failed for %s: %s", symbol, e)
    finally:
        conn.close()