"""Shared signal snapshot store for scanner/stock-detail consistency.

This module provides a simple in-memory store for the latest signal snapshot
per symbol. Both the scanner and stock_detail endpoint read/write to this
store so that they can share signal data without duplicating calculation.

The store keyed by symbol contains:
- direction, confidence, signal_data from evaluate_signal()
- signal_generated_at timestamp (when the signal was generated)
- data_timestamp (the quote timestamp the signal was based on)
- data_age_seconds (age of the data used)
- data_status (LIVE/RECENT/DELAYED/STALE)
- timeframe used (5m)
- snapshot_id for deterministic identification

The validity window is derived from the project's existing freshness thresholds
(LIVE < 5min, RECENT < 15min, DELAYED < 60min, STALE > 60min). A signal is
considered "valid" if its data_age_seconds is below the LIVE threshold (5 minutes),
matching SCAN_CACHE_TTL in scanner.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Any

IST = timezone(timedelta(hours=5, minutes=30))

# Validity windows (matching yfinance freshness thresholds + SCAN_CACHE_TTL)
SIGNAL_VALIDITY_SECONDS = 300  # 5 minutes — matches SCAN_CACHE_TTL in scanner.py
SIGNAL_STALE_AFTER_SECONDS = 900  # 15 minutes — RECENT zone
SIGNAL_DELAYED_AFTER_SECONDS = 3600  # 1 hour — DELAYED zone
SIGNAL_MAX_AGE_SECONDS = 86400  # 24 hours — maximum before discard

# In-memory store: symbol -> signal snapshot
_signal_store: dict[str, dict[str, Any]] = {}


def _now_ist() -> datetime:
    return datetime.now(IST)


def _is_signal_valid(snapshot: dict | None) -> bool:
    """Check whether a signal snapshot is still considered valid."""
    if snapshot is None:
        return False
    age = (_now_ist() - datetime.fromisoformat(snapshot["signal_generated_at"])).total_seconds() if snapshot.get("signal_generated_at") else float("inf")
    return age <= SIGNAL_VALIDITY_SECONDS


def set_signal(symbol: str, snapshot: dict[str, Any]) -> None:
    """Store a signal snapshot for the given symbol.

    The snapshot should contain at minimum:
    - direction: str — SignalDirection value
    - confidence: float — 0-100
    - signal_generated_at: str — ISO timestamp
    - data_timestamp: str — ISO timestamp of the quote data
    - data_age_seconds: float — age of data in seconds
    - data_status: str — LIVE/RECENT/DELAYED/STALE
    - timeframe: str — e.g. "5m"
    - snapshot_id: str — deterministic identifier
    """
    _signal_store[symbol] = snapshot


def get_signal(symbol: str) -> dict[str, Any] | None:
    """Retrieve the latest signal snapshot for the given symbol.

    Returns None if no snapshot exists or the snapshot has expired.
    """
    snapshot = _signal_store.get(symbol)
    if snapshot is None:
        return None
    if not _is_signal_valid(snapshot):
        # Snapshot expired — remove it so callers get a clean None
        _signal_store.pop(symbol, None)
        return None
    return snapshot


def invalidate_symbol(symbol: str) -> None:
    """Remove a symbol's signal snapshot (e.g. on major data refresh)."""
    _signal_store.pop(symbol, None)


def get_all_signals() -> dict[str, dict[str, Any]]:
    """Return a copy of all stored signal snapshots (for diagnostics)."""
    return dict(_signal_store)


# Expose the validity windows for use by other modules
__all__ = [
    "set_signal",
    "get_signal",
    "invalidate_symbol",
    "get_all_signals",
    "SIGNAL_VALIDITY_SECONDS",
    "SIGNAL_STALE_AFTER_SECONDS",
    "SIGNAL_DELAYED_AFTER_SECONDS",
    "SIGNAL_MAX_AGE_SECONDS",
]