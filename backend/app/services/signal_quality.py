"""Top Signal Quality Layer (additive quality/ranking over Signal Engine V2).

This module is a SEPARATE, purely additive layer on top of the existing
Signal Engine V2. It never modifies the base signal: `evaluate_row_signal` /
`evaluate_signal` are untouched and keep producing the exact same
`direction` / `confidence` / `signal_score` / `setup` for identical input.

What this layer adds, per scanned symbol:

    setup_quality_score     0..100  quality of the complete trade setup
    setup_quality           PREMIUM / QUALIFIED / NORMAL / WEAK / REJECTED
    confirmation_count      how many of the 8 confirmation categories fully
                            confirmed (unknown categories are reported, never
                            auto-passed)
    confirmation_total      fixed at 8 (the confirmation categories)
    top_signal_eligible     True for PREMIUM / QUALIFIED (ranked candidates)
    top_signal_rank         1..n assigned by `rank_top_signals` (deterministic)
    risk_reward_ratio       setup RR surfaced for deterministic ranking
    quality_detail          per-category status + why / limitations text

Design rules enforced here:

* No fabricated values. Missing / non-finite inputs yield NEUTRAL or UNKNOWN
  categories; missing volume is never treated as bullish or bearish and never
  inflates the score.
* Volume is evaluated on the LAST COMPLETED candle (yfinance reports volume=0
  for the still-forming 5m candle, which makes the naive last-row
  `relative_volume` read ~0.0 during live hours — see the investigation).
  When the completed-bar volume is unavailable or structurally zero the
  category is UNKNOWN and contributes 0 points.
* Scores are bounded [0, 100]; ranking is deterministic (quality level →
  quality score → confirmations → confidence → RR → symbol).
* The existing signal engine thresholds, RR gate, weights and bands are never
  changed; this layer only measures the setup it was given.
"""

import math
from typing import Optional

import pandas as pd

# Category max-points budget — sums to 100. Each category is independent and
# direction-aware (it measures confirmation of the *base signal's* side).
_TREND_MAX = 18.0
_MOMENTUM_MAX = 14.0
_VWAP_MAX = 12.0
_VOLUME_MAX = 12.0
_PRICE_ACTION_MAX = 14.0
_MARKET_CONTEXT_MAX = 8.0
_RR_MAX = 14.0
_CONFLICT_MAX = 8.0

CATEGORIES = (
    "trend", "momentum", "vwap", "volume",
    "price_action", "market_context", "risk_reward", "conflict",
)
CONFIRMATION_TOTAL = len(CATEGORIES)  # 8

QUALITY_PREMIUM = "PREMIUM"
QUALITY_QUALIFIED = "QUALIFIED"
QUALITY_NORMAL = "NORMAL"
QUALITY_WEAK = "WEAK"
QUALITY_REJECTED = "REJECTED"
QUALITY_LEVELS = (QUALITY_PREMIUM, QUALITY_QUALIFIED, QUALITY_NORMAL, QUALITY_WEAK, QUALITY_REJECTED)
_LEVEL_ORDER = {
    QUALITY_PREMIUM: 5,
    QUALITY_QUALIFIED: 4,
    QUALITY_NORMAL: 3,
    QUALITY_WEAK: 2,
    QUALITY_REJECTED: 1,
}
_ELIGIBLE_LEVELS = (QUALITY_PREMIUM, QUALITY_QUALIFIED)

# RR quality bands (measuring factor only — NOT the engine's own RR gate).
_RR_STRONG = 2.0
_RR_GOOD = 1.5
_RR_ACCEPTABLE = 1.2

_VWAP_SATURATION_PCT = 2.0  # mirror of the engine's ±2% VWAP saturation


def _finite(value, default=float("nan")) -> float:
    """Return a finite float, else default (missing)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _clip(value: float, lo: float, hi: float) -> float:
    if not math.isfinite(value):
        return lo
    return max(lo, min(hi, value))


def _side(direction: Optional[str]) -> int:
    """+1 for LONG-side signals, -1 for SHORT-side, 0 otherwise."""
    d = (direction or "").upper()
    if "LONG" in d:
        return 1
    if "SHORT" in d:
        return -1
    return 0


def _cat(status: str, points: float, reasons: list[str]) -> dict:
    return {"status": status, "points": round(float(points), 2), "reason": "; ".join(reasons)}


# ────────────────────────────────────────────────────────────────────────────
# Category evaluators (independent, direction-aware, non-fabricating)
# ────────────────────────────────────────────────────────────────────────────

def _eval_trend(row, side: int) -> dict:
    ema9 = _finite(row.get("ema_9"))
    ema20 = _finite(row.get("ema_20"))
    ema50 = _finite(row.get("ema_50"))
    adx = _finite(row.get("adx_14"))
    price = _finite(row.get("close"))
    if pd.isna(ema9) or pd.isna(ema20) or pd.isna(ema50) or pd.isna(price):
        return _cat("unknown", 0.0, ["Trend data unavailable (EMA missing)"])
    if side == 1:
        aligned = [ema9 > ema20, ema20 > ema50, price > ema9]
        contra = [ema9 < ema20, ema20 < ema50, price < ema9]
        label = "LONG"
    else:
        aligned = [ema9 < ema20, ema20 < ema50, price < ema9]
        contra = [ema9 > ema20, ema20 > ema50, price > ema9]
        label = "SHORT"
    ok = sum(aligned)
    bad = sum(contra)
    if pd.isna(adx):
        adx_factor = 0.6
        adx_note = "ADX unavailable"
    elif adx >= 25:
        adx_factor = 1.0
        adx_note = f"ADX {adx:.1f}"
    elif adx >= 15:
        adx_factor = 0.75
        adx_note = f"ADX {adx:.1f}"
    else:
        adx_factor = 0.45
        adx_note = f"ADX {adx:.1f}"
    if ok >= 2 and bad == 0:
        status = "confirmed" if ok == 3 else "partial"
        pts = _TREND_MAX * (0.62 * (ok / 3.0) + 0.38 * adx_factor)
        reasons = [
            f"{'Strong' if ok == 3 else 'Partial'} trend alignment ({ok}/3) for {label}"
        ]
        if not pd.isna(adx):
            reasons.append(f"ADX {adx:.1f}")
        return _cat(status, pts, reasons)
    if bad >= 2:
        return _cat("failed", 2.0, [f"Trend counters the {label} signal"])
    if ok == 1 and bad == 0:
        return _cat("partial", 8.0, [f"Weak trend alignment (1/3) for {label}"])
    return _cat("neutral", 4.0, ["Mixed EMA alignment"])


def _eval_momentum(row, side: int) -> dict:
    rsi = _finite(row.get("rsi_14"))
    macd_h = _finite(row.get("macd_histogram"))
    roc = _finite(row.get("roc_5"))
    close = _finite(row.get("close"))
    agree = 0
    disagree = 0
    avail = 0
    bits: list[str] = []
    if not pd.isna(rsi):
        avail += 1
        bullish = rsi > 50
        if bullish == (side == 1):
            agree += 1
            bits.append(f"RSI {rsi:.1f}")
        else:
            disagree += 1
            bits.append(f"RSI {rsi:.1f} opposes")
    if not pd.isna(macd_h) and not pd.isna(close):
        avail += 1
        bullish = macd_h > 0
        if bullish == (side == 1):
            agree += 1
            bits.append(f"MACD {'positive' if side == 1 else 'negative'}")
        else:
            disagree += 1
            bits.append(f"MACD {'negative' if side == 1 else 'positive'} opposes")
    if not pd.isna(roc):
        avail += 1
        bullish = roc > 0
        if bullish == (side == 1):
            agree += 1
            bits.append(f"ROC {roc:+.2f}%")
        else:
            disagree += 1
            bits.append("ROC opposes")
    if avail == 0:
        return _cat("unknown", 0.0, ["Momentum data unavailable"])
    ratio = agree / avail
    label = "bullish" if side == 1 else "bearish"
    if ratio >= 0.66 and disagree == 0:
        status = "confirmed"
        pts = _MOMENTUM_MAX * (0.3 + 0.7 * ratio)
        return _cat(status, pts, [f"Momentum {label} confirmed ({agree}/{avail})"] + bits)
    if ratio >= 0.5:
        return _cat("partial", _MOMENTUM_MAX * (0.3 + 0.7 * ratio),
                    [f"Momentum partially {label} ({agree}/{avail})"] + bits)
    if disagree >= 2:
        return _cat("failed", 2.0, [f"Momentum contradicts the {label} signal"] + bits)
    return _cat("neutral", _MOMENTUM_MAX * 0.30, ["Mixed momentum evidence"] + bits)


def _eval_vwap(row, side: int) -> dict:
    price = _finite(row.get("close"))
    vwap = _finite(row.get("vwap"))
    dist = _finite(row.get("distance_from_vwap"))
    if pd.isna(price) or pd.isna(vwap) or vwap <= 0:
        return _cat("unknown", 0.0, ["VWAP data unavailable"])
    if pd.isna(dist):
        dist = (price - vwap) / vwap * 100.0
    if abs(dist) < 0.05:
        return _cat("neutral", _VWAP_MAX * 0.25, ["Price at VWAP"])
    above = dist > 0
    if above == (side == 1):
        magnitude = _clip(abs(dist) / _VWAP_SATURATION_PCT, 0.0, 1.0)
        pts = _VWAP_MAX * (0.30 + 0.70 * magnitude)
        status = "confirmed" if abs(dist) >= 0.15 else "partial"
        side_word = "above" if above else "below"
        return _cat(status, pts, [f"Price {side_word} VWAP ({dist:+.2f}%)"])
    side_word = "above" if above else "below"
    return _cat("failed", 2.0, [f"Price {side_word} VWAP ({dist:+.2f}%) for a "
                                f"{'LONG' if side == 1 else 'SHORT'} signal"])


def _eval_volume(row, df) -> dict:
    """Volume confirmation — evaluated on the LAST COMPLETED candle.

    Live yfinance 5m bars report volume=0 for the still-forming candle (the
    probe showed completed bars with real volume, e.g. RELIANCE 227k/108k/
    101k while the current candle reads 0). Using the last *completed* bar
    avoids treating the incomplete candle as genuine low volume. When the
    completed-bar volume is missing or structurally zero, the category is
    UNKNOWN and contributes 0 points (never bullish/bearish, never inflated).
    """
    rv = float("nan")
    if df is not None and hasattr(df, "iloc") and len(df) >= 2:
        try:
            rv = _finite(df["relative_volume"].iloc[-2])
        except (KeyError, IndexError):
            rv = float("nan")
        if pd.isna(rv) or rv <= 0:
            # Distinguish "no volume exists at all" from "this bar happened to
            # be zero" by looking at the wider volume history.
            has_volume = False
            try:
                if len(df) > 2:
                    past = pd.to_numeric(df["volume"].iloc[:-2], errors="coerce")
                    has_volume = past.notna().any() and (past.fillna(0) > 0).mean() > 0.5
            except (KeyError, IndexError, TypeError):
                has_volume = False
            if not has_volume:
                return _cat("unknown", 0.0,
                            ["Volume data unavailable (provider limitation)"])
            return _cat("unknown", 0.0,
                        ["Last completed-candle volume unavailable — treated as UNKNOWN"])
    else:
        rv = _finite(row.get("relative_volume"))
        if pd.isna(rv) or rv <= 0:
            return _cat("unknown", 0.0,
                        ["Volume data unavailable — treated as UNKNOWN"])

    # Genuine participation measure on the completed bar (symmetric for LONG
    # and SHORT — high participation supports either side).
    if rv >= 1.5:
        status = "confirmed"
    elif rv >= 1.0:
        status = "partial"
    elif rv >= 0.6:
        status = "neutral"
    else:
        status = "failed"
    pts = _VOLUME_MAX * _clip((rv - 0.5) / 1.9, 0.0, 1.0)
    if status == "failed":
        pts = 1.5
    elif status == "neutral":
        pts = max(pts, 2.5)
    elif status == "partial":
        pts = max(pts, 5.0)
    return _cat(status, pts, [f"Relative volume {rv:.2f}x (completed candle)"])


def _eval_price_action(row, side: int) -> dict:
    close = _finite(row.get("close"))
    openp = _finite(row.get("open"))
    or_hi = _finite(row.get("opening_range_high"))
    or_lo = _finite(row.get("opening_range_low"))
    prev_hi = _finite(row.get("prev_high"))
    prev_lo = _finite(row.get("prev_low"))
    day_hi = _finite(row.get("day_high"))
    day_lo = _finite(row.get("day_low"))
    if pd.isna(close):
        return _cat("unknown", 0.0, ["Price action data unavailable"])
    label = "LONG" if side == 1 else "SHORT"
    agree = 0
    disagree = 0
    avail = 0
    notes: list[str] = []
    if not pd.isna(or_hi) and not pd.isna(or_lo):
        avail += 1
        if (side == 1 and close > or_hi) or (side == -1 and close < or_lo):
            agree += 1
            notes.append("opening-range breakout" if side == 1 else "opening-range breakdown")
        elif (side == 1 and close < or_lo) or (side == -1 and close > or_hi):
            disagree += 1
            notes.append("broke against the opening range")
        else:
            notes.append("inside opening range")
    if not pd.isna(prev_hi) and not pd.isna(prev_lo):
        if (side == 1 and close > prev_hi) or (side == -1 and close < prev_lo):
            agree += 1
            notes.append("new extreme vs prior bar")
        elif (side == 1 and close < prev_lo) or (side == -1 and close > prev_hi):
            disagree += 1
            notes.append("reversed vs prior bar")
        elif close != prev_hi and close != prev_lo:
            notes.append("within prior bar range")
        avail += 1
    if not pd.isna(openp) and close != openp:
        avail += 1
        if (close > openp) == (side == 1):
            agree += 1
            notes.append(f"{'bullish' if side == 1 else 'bearish'} candle")
        else:
            disagree += 1
            notes.append(f"{'bearish' if side == 1 else 'bullish'} candle opposes")
    if not pd.isna(day_hi) and not pd.isna(day_lo) and day_hi > day_lo:
        avail += 1
        pos = (close - day_lo) / (day_hi - day_lo)
        if side == 1 and pos >= 0.6:
            agree += 1
            notes.append(f"close in top {pos * 100:.0f}% of day range")
        elif side == -1 and pos <= 0.4:
            agree += 1
            notes.append(f"close in bottom {pos * 100:.0f}% of day range")
        elif side == 1 and pos <= 0.4:
            disagree += 1
            notes.append("close near day lows")
        elif side == -1 and pos >= 0.6:
            disagree += 1
            notes.append("close near day highs")
        else:
            notes.append("close mid-day-range")
    if avail == 0:
        return _cat("unknown", 0.0, ["Price action structure unavailable"])
    ratio = agree / avail
    if ratio >= 0.75 and disagree == 0:
        return _cat("confirmed", _PRICE_ACTION_MAX * (0.3 + 0.7 * ratio),
                    [f"Price action supports the {label} signal"] + notes)
    if ratio >= 0.5:
        return _cat("partial", _PRICE_ACTION_MAX * (0.3 + 0.7 * ratio),
                    [f"Price action partially supports the {label} signal"] + notes)
    if disagree >= 2:
        return _cat("failed", 2.0, [f"Price action opposes the {label} signal"] + notes)
    return _cat("neutral", _PRICE_ACTION_MAX * 0.30, ["Mixed price action structure"] + notes)


def _eval_market_context(market_context, side: int) -> dict:
    if not market_context:
        return _cat("unknown", 0.0, ["Market context unavailable — neutral"])
    trend = str(market_context.get("nifty_trend") or "UNKNOWN").upper()
    change = _finite(market_context.get("nifty_change_pct"))
    bank_change = _finite(market_context.get("banknifty_change_pct"))
    if trend not in ("BULLISH", "BEARISH") or pd.isna(change):
        return _cat("neutral", 0.0, ["Market context neutral/unknown"])
    aligned = (trend == "BULLISH") == (side == 1)
    magnitude = _clip(abs(change) / 0.5, 0.0, 1.0)
    nifty_line = f"NIFTY {trend.lower()} ({change:+.2f}%)"
    if aligned:
        status = "confirmed" if magnitude >= 0.6 else "partial"
        pts = _MARKET_CONTEXT_MAX * (0.4 + 0.6 * magnitude)
        reasons = [f"{nifty_line} aligns with the "
                   f"{'LONG' if side == 1 else 'SHORT'} signal"]
        if not pd.isna(bank_change) and abs(bank_change) >= 0.3:
            reasons.append(f"BANKNIFTY {bank_change:+.2f}%")
        return _cat(status, pts, reasons)
    return _cat("failed", 1.5,
                [f"{nifty_line} counters the {'LONG' if side == 1 else 'SHORT'} signal"])


def _eval_risk_reward(signal_setup) -> dict:
    if hasattr(signal_setup, "model_dump"):
        signal_setup = signal_setup.model_dump()  # pydantic model -> dict
    rr = _finite((signal_setup or {}).get("risk_reward_ratio"))
    if pd.isna(rr) or rr <= 0:
        return _cat("unknown", 0.0, ["Risk/reward not available"])
    if rr >= _RR_STRONG:
        return _cat("confirmed", _RR_MAX, [f"Risk/reward strong ({rr:.2f})"])
    if rr >= _RR_GOOD:
        return _cat("confirmed", _RR_MAX * 0.75, [f"Risk/reward good ({rr:.2f})"])
    if rr >= _RR_ACCEPTABLE:
        return _cat("partial", _RR_MAX * 0.45, [f"Risk/reward acceptable ({rr:.2f})"])
    return _cat("failed", 2.0, [f"Risk/reward poor ({rr:.2f})"])


def _eval_conflict(signal_score, direction_evidence, side: int) -> dict:
    score = signal_score or {}
    if hasattr(score, "model_dump"):
        score = score.model_dump()  # pydantic model -> dict
    evidence = direction_evidence or {}
    if hasattr(evidence, "model_dump"):
        evidence = evidence.model_dump()  # pydantic model -> dict
    conflict = bool(score.get("conflict")) or bool(evidence.get("conflict"))
    opposing = _finite(score.get("short_total" if side == 1 else "long_total"))
    if pd.isna(opposing):
        try:
            opposing = _finite(direction_evidence.get("long_total" if side == -1 else "short_total"))
        except AttributeError:
            opposing = float("nan")
    if conflict:
        return _cat("failed", 0.0, ["Strong conflicting LONG and SHORT evidence"])
    if pd.isna(opposing) or opposing < 4:
        return _cat("confirmed", _CONFLICT_MAX, ["Low conflicting evidence"])
    if opposing < 12:
        return _cat("confirmed", _CONFLICT_MAX * 0.75,
                    [f"Mild opposing evidence ({opposing:.1f})"])
    if opposing < 20:
        return _cat("partial", _CONFLICT_MAX * 0.4,
                    [f"Noticeable opposing evidence ({opposing:.1f})"])
    return _cat("failed", 1.0, [f"Strong opposing evidence ({opposing:.1f})"])


# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────

def classify_quality(score: float, confirmed: int, rr: float) -> str:
    """Classify setup quality. Measurement only — never changes the base signal."""
    if score >= 75.0 and confirmed >= 6 and rr >= _RR_GOOD:
        return QUALITY_PREMIUM
    if score >= 60.0 and confirmed >= 4 and rr >= _RR_ACCEPTABLE:
        return QUALITY_QUALIFIED
    if score >= 45.0:
        return QUALITY_NORMAL
    if score >= 28.0:
        return QUALITY_WEAK
    return QUALITY_REJECTED


def compute_setup_quality(
    signal: Optional[dict],
    row,
    market_context: Optional[dict] = None,
    df=None,
) -> dict:
    """Compute the Top-Signal quality fields for one base signal.

    Parameters
    ----------
    signal : the base signal dict returned by ``evaluate_signal`` (never
        mutated by this function).
    row    : last indicator row (pd.Series or dict-like).
    df     : optional full indicator frame — used to read the last COMPLETED
        candle for the volume category.
    """
    if not signal:
        return _neutral_payload()
    direction = (signal.get("direction") or "").upper()
    side = _side(direction)
    if side == 0:
        return _neutral_payload()

    signal_score = signal.get("signal_score") or {}
    direction_evidence = signal.get("direction_evidence") or {}
    signal_setup = signal.get("setup") or {}

    categories = {
        "trend": _eval_trend(row, side),
        "momentum": _eval_momentum(row, side),
        "vwap": _eval_vwap(row, side),
        "volume": _eval_volume(row, df),
        "price_action": _eval_price_action(row, side),
        "market_context": _eval_market_context(market_context, side),
        "risk_reward": _eval_risk_reward(signal_setup),
        "conflict": _eval_conflict(signal_score, direction_evidence, side),
    }

    score = round(sum(c["points"] for c in categories.values()), 2)
    confirmed = sum(1 for c in categories.values() if c["status"] == "confirmed")
    rr = _finite(signal_setup.get("risk_reward_ratio"))
    if pd.isna(rr):
        rr = 0.0
    level = classify_quality(score, confirmed, rr)
    eligible = level in _ELIGIBLE_LEVELS

    why: list[str] = []
    limitations: list[str] = []
    for name, cat in categories.items():
        if cat["status"] == "confirmed":
            why.append(cat["reason"])
        elif cat["status"] in ("partial", "neutral", "unknown", "failed"):
            limitations.append(cat["reason"])

    return {
        "setup_quality_score": score,
        "setup_quality": level,
        "confirmation_count": int(confirmed),
        "confirmation_total": CONFIRMATION_TOTAL,
        "top_signal_eligible": bool(eligible),
        "risk_reward_ratio": round(rr, 2),
        "top_signal_rank": None,
        "quality_detail": {
            "categories": {
                name: {
                    "status": cat["status"],
                    "points": cat["points"],
                    "reason": cat["reason"],
                }
                for name, cat in categories.items()
            },
            "why_qualified": why,
            "quality_limitations": limitations,
        },
        "not_applicable": False,
    }


def _neutral_payload() -> dict:
    return {
        "setup_quality_score": 0.0,
        "setup_quality": None,
        "confirmation_count": 0,
        "confirmation_total": CONFIRMATION_TOTAL,
        "top_signal_eligible": False,
        "risk_reward_ratio": None,
        "top_signal_rank": None,
        "quality_detail": {
            "categories": {},
            "why_qualified": [],
            "quality_limitations": ["No directional base signal"],
        },
        "not_applicable": True,
    }


def rank_top_signals(results: list) -> list:
    """Deterministically rank scanner rows by setup quality and assign
    ``top_signal_rank`` (1..n) to eligible rows (PREMIUM / QUALIFIED).

    Primary: setup quality level → setup quality score → confirmation count
    → base confidence → RR → symbol (stable tiebreak). Rows are sorted in
    place; the sorted list is returned.
    """
    def key(r: dict):
        level = r.get("setup_quality") or QUALITY_REJECTED
        return (
            -_LEVEL_ORDER.get(level, 1),
            -float(r.get("setup_quality_score") or 0),
            -int(r.get("confirmation_count") or 0),
            -float(r.get("confidence") or 0),
            -float(r.get("risk_reward_ratio") or 0),
            str(r.get("symbol") or ""),
        )

    ranked = sorted(results, key=key)
    rank = 0
    for r in ranked:
        if r.get("top_signal_eligible"):
            rank += 1
            r["top_signal_rank"] = rank
        else:
            r["top_signal_rank"] = None
    return ranked


def select_top_signals(results: list, limit: int = 3) -> list:
    """Current Top Signals: eligible rows of the LATEST scan, quality-ranked.

    Combined LONG/SHORT pool — never direction-split, never persisted across
    scans. Pipeline: current scan results → ``rank_top_signals`` (existing
    deterministic quality ranking) → eligible rows (PREMIUM / QUALIFIED) →
    ``top_signals[:limit]``. Idempotent on an already-ranked list: re-ranking
    the same dataset yields the same order and identical ranks.

    At most ``limit`` rows are returned; WEAK / REJECTED / NOT_ELIGIBLE rows
    are never promoted to fill a slot, and zero eligible rows yield [].
    """
    ranked = rank_top_signals(results)
    return [r for r in ranked if r.get("top_signal_rank") is not None][:limit]