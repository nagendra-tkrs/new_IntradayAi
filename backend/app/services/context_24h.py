"""24-hour market-context helper — optional additive context (Mode B).

This module answers one question from the bars the scanner ALREADY fetched:

    "Given the last ``window_hours`` CLOCK hours of 5-minute candles ending at
     the decision candle, what is the broader market/trend/volatility context?"

Contract (hard rules, enforced in code and tests):

  * Window semantics: the window is ``[decision_ts - window_hours, decision_ts]``
    in the Indian market timezone (Asia/Kolkata — the provider already
    converts bars to IST). ``window_hours`` is CLOCK hours, never trading
    hours; weekends, holidays, market-close gaps and missing candles simply
    reduce the number of available bars. NO synthetic candle is ever created.

  * Look-ahead safety: only candles with ``timestamp <= decision_ts`` are read
    (the decision timestamp defaults to the last candle in the frame). The
    provider's indicators (EMA / ATR / RSI / VWAP / relative_volume) are
    causal — their value at a bar depends only on bars at-or-before it — so
    slicing an already-computed indicator frame cannot leak future data. For
    an explicit ``decision_ts`` earlier than the frame end, rows AFTER the
    decision are excluded before any statistic is computed (tests cover this).

  * Non-fabricating: missing / non-finite inputs yield neutral fields and a
    ``net`` contribution of 0. A window with fewer than 2 candles reports
    ``net = 0.0`` and ``sufficient = False`` (there is nothing to measure).

The signed ``net`` on [-1, +1] (bullish +, bearish −) is a blend of:

  * ``trend_net`` — where the current close sits vs the 50-EMA in ATR units,
    clipped at ±1 (price/EMA distance beyond 1 ATR saturates).
  * ``range_net`` — where the close sits inside the window's high/low range:
    (range_position − 0.5) × 2, clipped at ±1.
  * ``net = clip(0.5·trend_net + 0.5·range_net, −1, 1)``

The engine consumes ``net`` through the existing market-context component
ONLY when ``USE_24H_CONTEXT=true`` (the caller passes ``context_24h`` into
``evaluate_signal``/``evaluate_row_signal``). With the flag off, nothing in
this module runs and the existing signal is byte-for-byte identical.
"""

from typing import Optional

import numpy as np
import pandas as pd

from app.core.market_session import IST

# A full NSE session at the 5-minute interval (09:15-15:30 IST) is 75 bars.
FULL_SESSION_BARS = 75


def _to_ist(ts) -> Optional[pd.Timestamp]:
    """Coerce a timestamp to an Asia/Kolkata-aware pandas Timestamp (None on
    failure). Naive values are attached to IST — the app's wall-clock
    convention for provider bars / trade timestamps."""
    if ts is None:
        return None
    try:
        out = pd.Timestamp(ts)
    except (TypeError, ValueError):
        return None
    if out.tzinfo is None:
        out = out.tz_localize(IST)
    else:
        out = out.tz_convert(IST)
    return out


def _finite(value, default=np.nan) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(v):
        return default
    return v


def window_bounds(decision_ts, window_hours: float = 24.0):
    """Return ``(window_start, decision_ts)`` as IST-aware pandas Timestamps.

    ``decision_ts`` may be a datetime / pd.Timestamp / ISO string; when None
    it falls back to the current IST time (used only for standalone probes —
    the scanner always passes the decision candle's timestamp)."""
    end = _to_ist(decision_ts)
    if end is None:
        end = pd.Timestamp.now(tz=IST)
    start = end - pd.Timedelta(hours=max(0.0, float(window_hours)))
    return start, end


def compute_24h_context(
    df: pd.DataFrame,
    decision_ts=None,
    window_hours: float = 24.0,
) -> dict:
    """Compute the last-``window_hours``-clock-hours context up to the decision
    candle. Read-only, look-ahead-safe, never fabricates candles.

    Parameters
    ----------
    df : indicator frame with a ``timestamp`` column (tz-aware Asia/Kolkata
        from the provider; naive timestamps are attached to IST defensively).
    decision_ts : the timestamp of the bar being decided on. Defaults to the
        LAST row of ``df`` (the normal scanner case).
    window_hours : clock hours of look-back (settings.CONTEXT_24H_WINDOW_HOURS).

    Returns a dict of context fields; the signed ``net``/``trend_net``/
    ``range_net`` are the only values the signal engine consumes.
    """
    out = {
        "window_hours": round(float(window_hours), 2),
        "decision_timestamp": None,
        "window_start": None,
        "candles": 0,
        "trading_dates": [],
        "first_timestamp": None,
        "last_timestamp": None,
        "session_coverage": "no candles in window",
        "sufficient": False,
        "lookahead_safe": True,
        "close_now": None,
        "close_24h_ago": None,
        "pct_change_24h": None,
        "range_high": None,
        "range_low": None,
        "range_position": None,
        "atr_pct": None,
        "volatility_state": None,
        "avg_relative_volume": None,
        "recent_relative_volume": None,
        "trend_net": 0.0,
        "range_net": 0.0,
        "net": 0.0,
    }
    if df is None or df.empty or "timestamp" not in df.columns:
        return out

    start, end = window_bounds(decision_ts, window_hours)
    out["decision_timestamp"] = end.isoformat()
    out["window_start"] = start.isoformat()

    work = df.copy()
    ts = pd.to_datetime(work["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST)
    else:
        ts = ts.dt.tz_convert(IST)
    work = work.assign(_ts=ts)

    # LOOK-AHEAD GUARD: only bars at-or-before the decision timestamp count.
    mask = (work["_ts"] >= start) & (work["_ts"] <= end)
    win = work[mask]
    if win.empty:
        return out

    first = win["_ts"].iloc[0]
    last = win["_ts"].iloc[-1]
    dates = sorted(win["_ts"].dt.date.unique())
    n = int(len(win))
    out.update({
        "candles": n,
        "trading_dates": [d.isoformat() for d in dates],
        "first_timestamp": first.isoformat(),
        "last_timestamp": last.isoformat(),
    })

    # Session coverage: count full 75-bar sessions vs partials inside the window.
    full = 0
    for d in dates:
        if int((win["_ts"].dt.date == d).sum()) >= FULL_SESSION_BARS:
            full += 1
    out["session_coverage"] = (
        f"{len(dates)} session(s) in window ({full} full, {len(dates) - full} partial)"
    )
    out["sufficient"] = n >= 2

    closes = pd.to_numeric(win["close"], errors="coerce").astype(float)
    lows = pd.to_numeric(win["low"], errors="coerce").astype(float)
    highs = pd.to_numeric(win["high"], errors="coerce").astype(float)

    close_now = _finite(closes.iloc[-1])
    close_ago = _finite(closes.iloc[0])
    if np.isfinite(close_now):
        out["close_now"] = round(close_now, 4)
    if np.isfinite(close_ago):
        out["close_24h_ago"] = round(close_ago, 4)
        if close_ago != 0:
            out["pct_change_24h"] = round((close_now - close_ago) / abs(close_ago) * 100.0, 4)

    r_hi = _finite(highs.max())
    r_lo = _finite(lows.min())
    if np.isfinite(r_hi):
        out["range_high"] = round(r_hi, 4)
    if np.isfinite(r_lo):
        out["range_low"] = round(r_lo, 4)
    if np.isfinite(r_hi) and np.isfinite(r_lo) and r_hi > r_lo and np.isfinite(close_now):
        pos = (close_now - r_lo) / (r_hi - r_lo)
        out["range_position"] = round(float(np.clip(pos, 0.0, 1.0)), 4)
    else:
        out["range_position"] = 0.5

    row = win.iloc[-1]
    atr = _finite(row.get("atr_14"))
    if np.isfinite(atr) and atr > 0 and np.isfinite(close_now) and close_now > 0:
        atr_pct = atr / close_now * 100.0
        out["atr_pct"] = round(atr_pct, 3)
        out["volatility_state"] = ("low" if atr_pct <= 0.3
                                   else ("high" if atr_pct >= 2.0 else "normal"))

    if "relative_volume" in win.columns:
        rv = pd.to_numeric(win["relative_volume"], errors="coerce")
        if rv.notna().any():
            out["avg_relative_volume"] = round(float(rv.dropna().mean()), 2)
            recent = rv.dropna().tail(5)
            if len(recent):
                out["recent_relative_volume"] = round(float(recent.mean()), 2)

    # Signed net evidence (see module docstring).
    trend_net = 0.0
    ema_col = None
    for col in ("ema_50", "ema_20"):
        if pd.notna(row.get(col)):
            ema_col = col
            break
    if ema_col is not None:
        ema_val = _finite(row.get(ema_col))
        if np.isfinite(ema_val) and np.isfinite(atr) and atr > 0 and np.isfinite(close_now):
            trend_net = float(np.clip((close_now - ema_val) / atr, -1.0, 1.0))
    pos_net = (float(out["range_position"]) - 0.5) * 2.0
    out["trend_net"] = round(float(np.clip(trend_net, -1.0, 1.0)), 6)
    out["range_net"] = round(float(np.clip(pos_net, -1.0, 1.0)), 6)
    if n >= 2:
        out["net"] = round(float(np.clip(0.5 * out["trend_net"] + 0.5 * out["range_net"], -1.0, 1.0)), 6)
    return out