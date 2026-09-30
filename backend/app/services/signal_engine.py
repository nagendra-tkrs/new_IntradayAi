import pandas as pd
import numpy as np
import math
from typing import Optional
from datetime import datetime
from app.models.schemas import (
    SignalDirection, SignalScore, DirectionalEvidence, TradeSetup,
    SignalExplanation, DataSource
)
from app.services.indicators import calculate_all_indicators
from app.core.market_session import now_ist
from app.services.strategy_config import StrategyVersion, get_strategy
import uuid

CRITICAL_INDICATORS = ["atr_14", "adx_14", "vwap", "rsi_14", "relative_volume"]

# ────────────────────────────────────────────────────────────────────────────
# Signal Engine V2 — directional-evidence contract
# ────────────────────────────────────────────────────────────────────────────
# Component weights (preserved from V1):
#     Trend 20 · Momentum 15 · Volume 15 · VWAP 15 · Price Action 15 ·
#     Market Context 10 · Risk Quality 10  (Σ = 100)
#
# Risk Quality is a direction-NEUTRAL tradability gate, not a directional
# contributor. The six directional components sum to 90.
#
# Every directional component reduces to a signed net value on [-1, +1]:
#     net_i > 0  → bullish evidence      (long_i  = net_i, short_i = 0)
#     net_i < 0  → bearish evidence      (short_i = -net_i, long_i = 0)
#     net_i = 0  → neutral / missing     (both sides 0 — nothing fabricated)
#
# Aggregation:
#     net         = Σ W_i · net_i                       (range [-90, +90])
#     long_total  = Σ W_i · max(net_i, 0)               (range [0, +90])
#     short_total = Σ W_i · max(-net_i, 0)              (range [0, +90])
#     total       = clamp(55.0 + 0.5 · net, 0, 100)     (classic 0..100 band
#                                                        scale; 55 is the
#                                                        NO_TRADE centre)
#     direction   = determine_direction(total)          (unchanged V1 bands:
#                                                        80/75/65/55/45/30)
#
# Conflict rule (contradictory evidence → NO_TRADE):
#     min(long_total, short_total) >= CONFLICT_MIN_EVIDENCE → both directions
#     carry meaningful opposing evidence → conflict → NO_TRADE. A symbol with
#     strong contradictory evidence never becomes LONG/SHORT merely because
#     one side edges out the other.
#
# Classic-scale component scores surfaced for compatibility / display:
#     score_i = W_i · (net_i + 1) / 2   → range [0, W_i], neutral = W_i / 2
#
# All sub-inputs are sanitized: NaN / ±Inf / missing values become NEUTRAL
# (net_i contribution 0) — never bullish, never bearish, never substituted.
# ────────────────────────────────────────────────────────────────────────────

# Weight of each directional component (bulk of the 100-point budget).
DIRECTIONAL_WEIGHTS = {
    "trend": 20.0,
    "momentum": 15.0,
    "volume": 15.0,
    "vwap": 15.0,
    "price_action": 15.0,
    "market_context": 10.0,
}
RISK_QUALITY_WEIGHT = 10.0
DIRECTIONAL_WEIGHT_TOTAL = sum(DIRECTIONAL_WEIGHTS.values())  # 90

# V2 aggregation constants (documented ranges).
NEUTRAL_TOTAL = 55.0   # classic-scale centre of the NO_TRADE band (55-64)
NET_TO_TOTAL = 0.5     # total = 55 + 0.5·net  →  net ∈ [-90, +90] ↦ [10, 100]
CONFLICT_MIN_EVIDENCE = 20.0  # both long_total AND short_total ≥ 20 → NO_TRADE

# Scale mapping for commonly used indicators.
VWAP_SATURATION_PCT = 2.0   # ±2% from VWAP saturates vwap net at ±1
MOMENTUM_RSI_BAND = 30.0    # RSI deviation from 50 that saturates at ±1
MOMENTUM_ROC_BAND = 3.0     # ±3% ROC-5 saturates the ROC sub-evidence at ±1
MARKET_MAGNITUDE_PCT = 0.5  # ±0.5% NIFTY move saturates market context at ±1
VOLUME_PARTICIPATION_BAND = 2.0  # relvol-1.0 ↦ 0, relvol ≥ 3.0x ↦ full
TREND_STRONG_ADX = 25.0     # ADX ≥ 25 → full trend strength
TREND_MID_ADX = 15.0        # ADX ∈ [15, 25) → 0.7 strength
TREND_WEAK_STRENGTH = 0.5   # ADX < 15 → 0.5 strength
TREND_NO_ADX_STRENGTH = 0.6  # ADX missing → partial strength (still symmetric)


def _finite(value, default=np.nan) -> float:
    """Return the value as a float when finite (not NaN / ±Inf), else default.

    This is the sanitisation gate for every V2 sub-input: non-finite values
    become "missing" and therefore contribute NEUTRAL evidence — no NaN/Inf
    can reach a signed component."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _clip(value: float, lo: float, hi: float) -> float:
    """Clamp to [lo, hi]; any non-finite input maps to the neutral centre 0.0."""
    if not math.isfinite(value):
        return 0.0
    return max(lo, min(hi, value))


def _normalize(value: float, min_val: float, max_val: float) -> float:
    if max_val == min_val:
        return 0.0
    return max(0.0, min(1.0, (value - min_val) / (max_val - min_val)))


def score_trend(row: pd.Series) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    ema_9 = row.get("ema_9", np.nan)
    ema_20 = row.get("ema_20", np.nan)
    ema_50 = row.get("ema_50", np.nan)
    price = row.get("close", np.nan)
    if pd.isna(ema_9) or pd.isna(ema_20) or pd.isna(ema_50):
        return 0.0, ["Insufficient data for trend"]
    if ema_9 > ema_20 > ema_50:
        score = 20
        reasons.append("Strong bullish EMA alignment (9>20>50)")
    elif ema_9 > ema_20:
        score = 12
        reasons.append("Short-term bullish trend (EMA 9>20)")
    elif ema_9 < ema_20 < ema_50:
        score = 0
        reasons.append("Strong bearish EMA alignment (9<20<50)")
    elif ema_9 < ema_20:
        score = 6
        reasons.append("Short-term bearish trend (EMA 9<20)")
    adx = row.get("adx_14", np.nan)
    if not pd.isna(adx):
        if adx > 25:
            reasons.append(f"Strong trend (ADX {adx:.0f})")
            score = min(20, score + 2)
        elif adx < 15:
            reasons.append(f"Weak trend (ADX {adx:.0f})")
            score = max(0, score - 3)
    return score, reasons


def score_momentum(row: pd.Series) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    rsi = row.get("rsi_14", 50)
    macd_hist = row.get("macd_histogram", 0)
    roc = row.get("roc_5", 0)
    if pd.isna(rsi):
        return 0.0, ["Insufficient data for momentum"]
    if 40 <= rsi <= 60:
        score += 5
        reasons.append(f"RSI neutral ({rsi:.0f})")
    elif 30 <= rsi < 40:
        score += 8
        reasons.append(f"RSI recovering ({rsi:.0f})")
    elif rsi < 30:
        score += 3
        reasons.append(f"RSI oversold ({rsi:.0f}) - caution")
    elif 60 < rsi <= 70:
        score += 8
        reasons.append(f"RSI strong ({rsi:.0f})")
    elif rsi > 70:
        score += 3
        reasons.append(f"RSI overbought ({rsi:.0f}) - caution")
    if not pd.isna(macd_hist):
        if macd_hist > 0:
            score += 5
            reasons.append("MACD momentum positive")
        else:
            score += 1
            reasons.append("MACD momentum negative")
    if not pd.isna(roc):
        if abs(roc) > 1:
            score += 2
            reasons.append(f"Rate of change {roc:.1f}%")
    return min(15, score), reasons


def score_volume(row: pd.Series) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    rel_vol = row.get("relative_volume", 1.0)
    if pd.isna(rel_vol):
        return 0.0, ["Insufficient volume data"]
    if rel_vol > 2.0:
        score = 15
        reasons.append(f"Very high relative volume ({rel_vol:.1f}x)")
    elif rel_vol > 1.5:
        score = 12
        reasons.append(f"High relative volume ({rel_vol:.1f}x)")
    elif rel_vol > 1.0:
        score = 8
        reasons.append(f"Above-average volume ({rel_vol:.1f}x)")
    elif rel_vol > 0.5:
        score = 4
        reasons.append(f"Average volume ({rel_vol:.1f}x)")
    else:
        score = 1
        reasons.append(f"Low volume ({rel_vol:.1f}x) - caution")
    return score, reasons


def score_vwap(row: pd.Series) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    dist = row.get("distance_from_vwap", 0)
    vwap_val = row.get("vwap", np.nan)
    price = row.get("close", np.nan)
    if pd.isna(dist) or pd.isna(vwap_val) or vwap_val == 0:
        return 0.0, ["VWAP not available"]
    if dist > 0:
        score = 12 + min(3, dist * 2)
        reasons.append(f"Price above VWAP (+{dist:.1f}%)")
    else:
        score = 3 + max(0, 12 + dist * 2)
        reasons.append(f"Price below VWAP ({dist:.1f}%)")
    return min(15, max(0, score)), reasons


def score_price_action(row: pd.Series) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    price = row.get("close", 0)
    or_high = row.get("opening_range_high", np.nan)
    or_low = row.get("opening_range_low", np.nan)
    prev_high = row.get("prev_high", np.nan)
    prev_low = row.get("prev_low", np.nan)
    if pd.isna(or_high) or pd.isna(or_low):
        return 7.5, ["Opening range not established"]
    if price > or_high:
        score += 8
        reasons.append("Breakout above opening range")
    elif price < or_low:
        score += 8
        reasons.append("Breakdown below opening range")
    else:
        score += 4
        reasons.append("Within opening range")
    if not pd.isna(prev_high) and price > prev_high:
        score += 4
        reasons.append("Above previous high")
    if not pd.isna(prev_low) and price < prev_low:
        score += 4
        reasons.append("Below previous low")
    bb_upper = row.get("bb_upper", np.nan)
    bb_lower = row.get("bb_lower", np.nan)
    if not pd.isna(bb_upper) and price > bb_upper:
        score += 3
        reasons.append("Above Bollinger Band upper")
    elif not pd.isna(bb_lower) and price < bb_lower:
        score += 3
        reasons.append("Below Bollinger Band lower")
    return min(15, score), reasons


def score_market_context(row: pd.Series, market_context: dict | None = None) -> tuple[float, list[str]]:
    if market_context is None:
        return 5.0, ["Market context neutral (no index data available)"]

    score = 5.0
    reasons = []

    nifty_trend = market_context.get("nifty_trend", "UNKNOWN")
    nifty_change_pct = market_context.get("nifty_change_pct", 0)
    banknifty_change_pct = market_context.get("banknifty_change_pct", 0)

    if nifty_trend == "BULLISH":
        score += 3
        reasons.append(f"NIFTY trending bullish ({nifty_change_pct:+.1f}%)")
    elif nifty_trend == "BEARISH":
        score -= 2
        reasons.append(f"NIFTY trending bearish ({nifty_change_pct:+.1f}%)")
    else:
        reasons.append(f"NIFTY neutral ({nifty_change_pct:+.1f}%)")

    if nifty_change_pct > 0.5:
        score += 2
        reasons.append("Strong positive market breadth")
    elif nifty_change_pct < -0.5:
        score -= 1
        reasons.append("Negative market breadth")

    return max(0, min(10, score)), reasons


def score_risk_quality(row: pd.Series) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    atr_val = row.get("atr_14", np.nan)
    price = row.get("close", np.nan)
    vol = row.get("volatility_20", np.nan)
    if pd.isna(atr_val) or pd.isna(price) or price == 0:
        return 5.0, ["Insufficient data for risk assessment"]
    atr_pct = (atr_val / price) * 100
    if 0.3 < atr_pct < 2.0:
        score = 10
        reasons.append(f"Good volatility (ATR {atr_pct:.1f}%)")
    elif atr_pct <= 0.3:
        score = 3
        reasons.append(f"Low volatility (ATR {atr_pct:.1f}%) - limited opportunity")
    elif atr_pct >= 2.0:
        score = 5
        reasons.append(f"High volatility (ATR {atr_pct:.1f}%) - elevated risk")
    return score, reasons


# ────────────────────────────────────────────────────────────────────────────
# V2 — symmetric directional (net) evidence components
# ────────────────────────────────────────────────────────────────────────────
# Each _net_* helper returns (net, reasons) with net ∈ [-1, +1] (bullish +,
# bearish -, 0 neutral/missing). Missing or non-finite sub-inputs contribute
# 0 — the engine never fabricates a direction.
# ────────────────────────────────────────────────────────────────────────────


def _net_trend(row: pd.Series) -> tuple[float, list[str]]:
    """Trend evidence on [-1, +1].

    Inputs   : close, ema_9, ema_20, ema_50, adx_14.
    Bullish  : price > ema_9 > ema_20 > ema_50   (or partial ordering).
    Bearish  : price < ema_9 < ema_20 < ema_50   (or partial ordering).
    Neutral  : mixed / crossed EMA ordering (both sides near 0).
    Strength : ADX ≥ 25 → 1.0, [15, 25) → 0.7, < 15 → 0.5, missing → 0.6.
    Missing  : any of price / ema_9 / ema_20 / ema_50 → 0 (neutral).
    """
    ema_9 = _finite(row.get("ema_9"))
    ema_20 = _finite(row.get("ema_20"))
    ema_50 = _finite(row.get("ema_50"))
    price = _finite(row.get("close"))
    if pd.isna(ema_9) or pd.isna(ema_20) or pd.isna(ema_50) or pd.isna(price):
        return 0.0, ["Insufficient data for trend"]
    bull = sum([price > ema_9, ema_9 > ema_20, ema_20 > ema_50])
    bear = sum([price < ema_9, ema_9 < ema_20, ema_20 < ema_50])
    alignment = (bull - bear) / 3.0
    adx = _finite(row.get("adx_14"))
    if pd.isna(adx):
        strength = TREND_NO_ADX_STRENGTH
    elif adx >= TREND_STRONG_ADX:
        strength = 1.0
    elif adx >= TREND_MID_ADX:
        strength = 0.7
    else:
        strength = TREND_WEAK_STRENGTH
    net = round(alignment * strength, 6)
    reasons = []
    if bull == 0 and bear == 0:
        reasons.append("Neutral EMA alignment")
    elif bull > bear:
        reasons.append(f"Bullish trend evidence ({bull}/3 alignment)")
    elif bear > bull:
        reasons.append(f"Bearish trend evidence ({bear}/3 alignment)")
    else:
        reasons.append("Neutral EMA alignment")
    if not pd.isna(adx):
        reasons.append(f"ADX {adx:.0f}")
    return net, reasons


def _net_momentum(row: pd.Series) -> tuple[float, list[str]]:
    """Momentum evidence on [-1, +1] — symmetric MIX of RSI / MACD / ROC.

    Inputs   : rsi_14, macd_histogram, roc_5, close.
    Bullish  : RSI > 50, MACD histogram > 0, ROC > 0.
    Bearish  : RSI < 50, MACD histogram < 0, ROC < 0.
    Neutral  : RSI ≈ 50 / histogram ≈ 0 / ROC ≈ 0 → net ≈ 0.
    Missing  : any sub-input missing/non-finite contributes 0; when ALL
               momentum inputs are unavailable → 0 (neutral) with a reason.
    Sub-weights (documented): RSI 50% · MACD histogram 30% · ROC 20%.
    """
    close = _finite(row.get("close"))
    rsi_net = 0.0
    rsi = _finite(row.get("rsi_14"))
    if not pd.isna(rsi):
        rsi_net = _clip((rsi - 50.0) / MOMENTUM_RSI_BAND, -1.0, 1.0)

    macd_net = 0.0
    macd_hist = _finite(row.get("macd_histogram"))
    if not pd.isna(macd_hist) and not pd.isna(close) and close > 0:
        macd_net = _clip(macd_hist / (close * 0.01), -1.0, 1.0)

    roc_net = 0.0
    roc = _finite(row.get("roc_5"))
    if not pd.isna(roc):
        roc_net = _clip(roc / MOMENTUM_ROC_BAND, -1.0, 1.0)

    available = sum([
        not pd.isna(rsi),
        (not pd.isna(macd_hist)) and (not pd.isna(close)) and close > 0,
        not pd.isna(roc),
    ])
    if available == 0:
        return 0.0, ["Insufficient data for momentum"]
    net = round(0.5 * rsi_net + 0.3 * macd_net + 0.2 * roc_net, 6)
    if net > 0.05:
        reasons = ["Positive momentum evidence"]
    elif net < -0.05:
        reasons = ["Negative momentum evidence"]
    else:
        reasons = ["Neutral momentum"]
    return net, reasons


def _net_volume(row: pd.Series) -> tuple[float, list[str]]:
    """Volume evidence on [-1, +1] — volume CONFIRMS the bar direction.

    Inputs   : relative_volume, open, close.
    Bullish  : above-average volume on a bullish bar (close > open).
    Bearish  : above-average volume on a bearish bar (close < open).
    Neutral  : average volume (rel_vol ≤ 1.0 → 0 participation), flat bar,
               or missing relative_volume → 0.
    """
    rel_vol = _finite(row.get("relative_volume"))
    if pd.isna(rel_vol):
        return 0.0, ["Insufficient volume data"]
    participation = _clip((rel_vol - 1.0) / VOLUME_PARTICIPATION_BAND, 0.0, 1.0)
    close = _finite(row.get("close"))
    open_ = _finite(row.get("open"))
    if pd.isna(close) or pd.isna(open_):
        return 0.0, ["Volume unconfirmed (missing bar direction)"]
    if close > open_:
        net = participation
        reason = f"Volume confirms bullish move ({rel_vol:.1f}x)"
    elif close < open_:
        net = -participation
        reason = f"Volume confirms bearish move ({rel_vol:.1f}x)"
    else:
        net = 0.0
        reason = "Volume neutral (flat bar)"
    return round(net, 6), [reason]


def _net_vwap(row: pd.Series) -> tuple[float, list[str]]:
    """VWAP evidence on [-1, +1] — above VWAP bullish, below VWAP bearish.

    Inputs   : distance_from_vwap (%), vwap, close.
    Bullish  : price above VWAP (dist > 0).       Bearish: below (dist < 0).
    Neutral  : price ≈ VWAP (|dist| < 0.1%), or missing/zero VWAP → 0.
    Scaling  : ±VWAP_SATURATION_PCT (2%) saturates at ±1.
    """
    dist = _finite(row.get("distance_from_vwap"))
    vwap_val = _finite(row.get("vwap"))
    if pd.isna(dist) or pd.isna(vwap_val) or vwap_val == 0:
        return 0.0, ["VWAP not available"]
    net = round(_clip(dist / VWAP_SATURATION_PCT, -1.0, 1.0), 6)
    if net > 0.05:
        reason = f"Price above VWAP (+{dist:.1f}%)"
    elif net < -0.05:
        reason = f"Price below VWAP ({dist:.1f}%)"
    else:
        reason = "Price at VWAP (neutral)"
    return net, [reason]


def _net_price_action(row: pd.Series) -> tuple[float, list[str]]:
    """Price-action evidence on [-1, +1] — symmetric breakout/breakdown.

    Inputs   : close, opening_range_high/low, prev_high/low, bb_upper/lower.
    Bullish  : price above opening-range high / previous high / BB upper.
    Bearish  : price below opening-range low / previous low / BB lower.
    Neutral  : within reference levels, or no reference levels → 0.
    Scaling  : net = (bull_signals - bear_signals) / 3.
    """
    price = _finite(row.get("close"))
    if pd.isna(price):
        return 0.0, ["Insufficient data for price action"]
    bull = 0
    bear = 0
    present = 0
    for key_up, key_dn in (
        ("opening_range_high", "opening_range_low"),
        ("prev_high", "prev_low"),
        ("bb_upper", "bb_lower"),
    ):
        up = _finite(row.get(key_up))
        dn = _finite(row.get(key_dn))
        if not pd.isna(up):
            present += 1
            if price > up:
                bull += 1
        if not pd.isna(dn):
            if price < dn:
                bear += 1
    if present == 0:
        return 0.0, ["Opening range not established"]
    if bull == 0 and bear == 0:
        return 0.0, ["Price action neutral (within reference levels)"]
    net = round((bull - bear) / 3.0, 6)
    if bull > bear:
        reason = "Breakout price action"
    elif bear > bull:
        reason = "Breakdown price action"
    else:
        reason = "Mixed price action"
    return net, [reason]


def _net_market_context(market_context: dict | None) -> tuple[float, list[str]]:
    """Market-context evidence on [-1, +1] — NIFTY trend sign, symmetric.

    Bullish  : nifty_trend == BULLISH      → +magnitude.
    Bearish  : nifty_trend == BEARISH      → -magnitude.
    Neutral  : nifty_trend NEUTRAL/UNKNOWN, magnitude 0, or missing → 0
               (a missing context never creates an implicit LONG bias).
    Scaling  : magnitude = min(|nifty_change_pct| / 0.5, 1).

    Optional additive 24-hour symbol context (Mode B / USE_24H_CONTEXT=true):
    when ``market_context["symbol_24h"]`` carries a finite signed ``net`` on
    [-1, +1] (from ``compute_24h_context``), it is ADDED to the NIFTY net and
    capped at ±1 so the component can never exceed its 10-point budget. When
    no symbol_24h is present the function is byte-identical to the original.
    """
    if not market_context:
        return 0.0, ["Market context neutral (no index data available)"]
    trend = market_context.get("nifty_trend", "UNKNOWN")
    change = _finite(market_context.get("nifty_change_pct"))
    if pd.isna(change):
        change = 0.0
    magnitude = _clip(abs(change) / MARKET_MAGNITUDE_PCT, 0.0, 1.0)
    if trend == "BULLISH":
        net = magnitude
    elif trend == "BEARISH":
        net = -magnitude
    else:
        net = 0.0
    if net > 1e-9:
        reasons = [f"NIFTY bullish context ({change:+.1f}%)"]
    elif net < -1e-9:
        reasons = [f"NIFTY bearish context ({change:+.1f}%)"]
    else:
        reasons = ["NIFTY context neutral"]

    symbol_24h = market_context.get("symbol_24h")
    if symbol_24h is not None:
        raw = symbol_24h.get("net") if isinstance(symbol_24h, dict) else symbol_24h
        if isinstance(raw, dict):
            raw = raw.get("net")
        sym_net = _clip(_finite(raw), -1.0, 1.0)
        if not pd.isna(raw) and abs(sym_net) > 1e-9:
            net = _clip(net + sym_net, -1.0, 1.0)
            reasons.append(f"Symbol 24H context ({sym_net:+.2f})")
    return round(net, 6), reasons


def _risk_quality_v2(row: pd.Series) -> tuple[bool, float, list[str]]:
    """Direction-NEUTRAL risk gate.

    Returns (risk_ok, quality, reasons). quality ∈ {1.0 good, 0.5 elevated,
    0.3 limited} from ATR% volatility. risk_ok is False only when ATR or
    price is missing/non-finite/non-positive — the trade cannot be sized,
    so the decision becomes NO_TRADE (see evaluate_row_signal).
    """
    atr_val = _finite(row.get("atr_14"))
    price = _finite(row.get("close"))
    if pd.isna(atr_val) or pd.isna(price) or price <= 0:
        return False, 0.0, ["Insufficient data for risk assessment"]
    atr_pct = (atr_val / price) * 100.0
    if 0.3 < atr_pct < 2.0:
        return True, 1.0, [f"Good volatility (ATR {atr_pct:.1f}%)"]
    if atr_pct <= 0.3:
        return True, 0.3, [f"Low volatility (ATR {atr_pct:.1f}%) - limited opportunity"]
    return True, 0.5, [f"High volatility (ATR {atr_pct:.1f}%) - elevated risk"]


def compute_directional_evidence(
    row: pd.Series,
    market_context: dict | None = None,
) -> dict:
    """V2 directional-evidence aggregation for a single row.

    Returns a dict containing:
        nets / reasons      per-component signed nets on [-1, +1] + reasons
        long_total          weighted LONG evidence  on [0, 90]
        short_total         weighted SHORT evidence on [0, 90]
        net                 signed net evidence     on [-90, +90] (+ bullish)
        conflict            True when BOTH long_total and short_total ≥ 20
        risk_ok / risk_quality / risk_reasons   direction-neutral risk gate
        total               classic 0..100 band-scale total (55 + net/2)
        evidence            DirectionalEvidence model (structured contract)
    """
    nets: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}

    nets["trend"], reasons["trend"] = _net_trend(row)
    nets["momentum"], reasons["momentum"] = _net_momentum(row)
    nets["volume"], reasons["volume"] = _net_volume(row)
    nets["vwap"], reasons["vwap"] = _net_vwap(row)
    nets["price_action"], reasons["price_action"] = _net_price_action(row)
    nets["market_context"], reasons["market_context"] = _net_market_context(market_context)

    risk_ok, risk_quality, risk_reasons = _risk_quality_v2(row)

    # Defensive sanity: every net must be finite and inside [-1, +1].
    for key, val in nets.items():
        if not math.isfinite(val) or not (-1.0 <= val <= 1.0):
            nets[key] = 0.0
            reasons[key].append(f"{key.replace('_', ' ')} evidence reset to neutral") 

    long_total = sum(
        DIRECTIONAL_WEIGHTS[k] * max(v, 0.0) for k, v in nets.items()
    )
    short_total = sum(
        DIRECTIONAL_WEIGHTS[k] * max(-v, 0.0) for k, v in nets.items()
    )
    net = long_total - short_total
    conflict = min(long_total, short_total) >= CONFLICT_MIN_EVIDENCE

    evidence = DirectionalEvidence(
        trend_long=max(nets["trend"], 0.0),
        trend_short=max(-nets["trend"], 0.0),
        momentum_long=max(nets["momentum"], 0.0),
        momentum_short=max(-nets["momentum"], 0.0),
        volume_long=max(nets["volume"], 0.0),
        volume_short=max(-nets["volume"], 0.0),
        vwap_long=max(nets["vwap"], 0.0),
        vwap_short=max(-nets["vwap"], 0.0),
        price_action_long=max(nets["price_action"], 0.0),
        price_action_short=max(-nets["price_action"], 0.0),
        market_context_long=max(nets["market_context"], 0.0),
        market_context_short=max(-nets["market_context"], 0.0),
        risk_quality=risk_quality,
        long_total=round(long_total, 4),
        short_total=round(short_total, 4),
        net=round(net, 4),
        conflict=conflict,
    )

    total = _clip(NEUTRAL_TOTAL + NET_TO_TOTAL * net, 0.0, 100.0)

    return {
        "nets": nets,
        "reasons": reasons,
        "long_total": long_total,
        "short_total": short_total,
        "net": net,
        "conflict": conflict,
        "risk_ok": risk_ok,
        "risk_quality": risk_quality,
        "risk_reasons": risk_reasons,
        "total": float(total),
        "evidence": evidence,
    }


def _resolve_setup_multipliers(strategy_version, is_strong: bool) -> tuple[float, float, float]:
    """Resolve the SL / T1 / T2 ATR multipliers for one bar.

    Precedence is: strategy-version override > settings default. The legacy
    geometry — a single ``stop_loss_atr_mult`` shared by both strengths and a
    ``T2 = T1 + 1 ATR`` spacing — is preserved byte-for-byte whenever a version
    sets no override (v1..v4 all leave the new fields None)."""
    if strategy_version is None:
        from app.core.config import settings
        sl = settings.STRONG_SL_ATR_MULTIPLIER if is_strong else settings.NORMAL_SL_ATR_MULTIPLIER
        t1 = settings.STRONG_T1_ATR_MULTIPLIER if is_strong else settings.NORMAL_T1_ATR_MULTIPLIER
        t2 = settings.STRONG_T2_ATR_MULTIPLIER if is_strong else settings.NORMAL_T2_ATR_MULTIPLIER
        return sl, t1, t2
    sl = strategy_version.sl_atr_mult_for(is_strong) or strategy_version.stop_loss_atr_mult
    t1 = strategy_version.target_1_atr_mult_for(is_strong)
    t2 = strategy_version.target_2_atr_mult_for(is_strong)
    if t2 is None:
        # Legacy spacing: Target 2 sits exactly one ATR past Target 1.
        t2 = t1 + 1.0
    return sl, t1, t2


def compute_trade_setup(row: pd.Series, direction: SignalDirection | str, strategy_version=None) -> TradeSetup:
    price = row.get("close", 0)
    atr_val = row.get("atr_14", 0)
    if pd.isna(atr_val) or atr_val == 0:
        atr_val = price * 0.01

    dir_str = direction.value if isinstance(direction, SignalDirection) else str(direction)
    is_strong = dir_str == "STRONG_LONG" or dir_str == "STRONG_SHORT"

    stop_mult, target_mult, target_2_mult = _resolve_setup_multipliers(strategy_version, is_strong)

    if dir_str in ("LONG", "STRONG_LONG", "WEAK_LONG"):
        entry = price
        stop_loss = round(price - stop_mult * atr_val, 2)
        target_1 = round(price + target_mult * atr_val, 2)
        target_2 = round(price + target_2_mult * atr_val, 2)
    elif dir_str in ("SHORT", "STRONG_SHORT", "WEAK_SHORT"):
        entry = price
        stop_loss = round(price + stop_mult * atr_val, 2)
        target_1 = round(price - target_mult * atr_val, 2)
        target_2 = round(price - target_2_mult * atr_val, 2)
    else:
        return TradeSetup()
    risk = abs(entry - stop_loss)
    reward_1 = abs(target_1 - entry)
    reward_2 = abs(target_2 - entry)
    rr_1 = round(reward_1 / risk, 2) if risk > 0 else 0
    return TradeSetup(
        entry=round(entry, 2),
        stop_loss=stop_loss,
        target_1=target_1,
        target_2=target_2,
        risk_per_share=round(risk, 2),
        reward_per_share=round(reward_1, 2),
        risk_reward_ratio=rr_1,
    )


def determine_direction(total_score: float) -> SignalDirection:
    # Raised thresholds for higher quality signals
    if total_score >= 80:
        return SignalDirection.STRONG_LONG
    elif total_score >= 75:
        return SignalDirection.LONG
    elif total_score >= 65:
        return SignalDirection.WEAK_LONG
    elif total_score >= 55:
        return SignalDirection.NO_TRADE
    elif total_score >= 45:
        return SignalDirection.WEAK_SHORT
    elif total_score >= 30:
        return SignalDirection.SHORT
    else:
        return SignalDirection.STRONG_SHORT


def compute_confidence(score: SignalScore, direction: SignalDirection) -> float:
    if direction == SignalDirection.NO_TRADE:
        return 0.0
    base = score.total
    alignment_bonus = 0
    if direction in (SignalDirection.STRONG_LONG, SignalDirection.LONG):
        if score.trend_score > 14:
            alignment_bonus += 5
        if score.volume_score > 10:
            alignment_bonus += 3
        if score.vwap_score > 10:
            alignment_bonus += 3
    elif direction in (SignalDirection.STRONG_SHORT, SignalDirection.SHORT):
        if score.trend_score < 6:
            alignment_bonus += 5
        if score.volume_score > 10:
            alignment_bonus += 3
    confidence = min(100, base * 0.7 + alignment_bonus + 10)
    return round(confidence, 1)


def evaluate_row_signal(
    row: pd.Series,
    market_context: dict | None = None,
    strategy_version=None,
    context_24h: Optional[dict] = None,
) -> Optional[dict]:
    """Pure per-row signal decision — the single source of truth for direction,
    confidence, trade setup, and explanation.

    Used by BOTH `evaluate_signal` (live/scanner/stock detail) and the backtest
    engine so every consumer derives identical signals from the same candle.

    ``context_24h`` (optional, Mode B / USE_24H_CONTEXT=true) is the dict from
    ``compute_24h_context``. When it carries a finite signed ``net`` it is
    ADDED to the NIFTY market context (see ``_net_market_context``); a None
    value produces byte-identical behavior to the current engine. The trade
    setup (entry/SL/T1/T2/R:R) is ALWAYS computed by the existing strategy —
    the 24H context never creates its own SL/T1/T2 system.

    Returns a dict with keys:
        direction, confidence, signal_score, setup, reasons, risks
    or None when critical indicator / OHLC data is missing.
    """
    # Check critical indicators
    for ind in CRITICAL_INDICATORS:
        val = row.get(ind, np.nan)
        if pd.isna(val) or (isinstance(val, float) and np.isnan(val)):
            return {
                "direction": SignalDirection.NO_TRADE,
                "confidence": 0.0,
                "signal_score": SignalScore(),
                "setup": TradeSetup(),
                "reasons": [f"Missing critical indicator: {ind}"],
                "risks": ["Incomplete indicator data"],
                "direction_evidence": DirectionalEvidence(),
                "context_24h": context_24h,
            }

    # Check OHLC validity
    o = row.get("open", np.nan)
    h = row.get("high", np.nan)
    l = row.get("low", np.nan)
    c = row.get("close", np.nan)
    if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
        return {
            "direction": SignalDirection.NO_TRADE,
            "confidence": 0.0,
            "signal_score": SignalScore(),
            "setup": TradeSetup(),
            "reasons": ["Invalid OHLC data"],
            "risks": ["Invalid price data"],
            "direction_evidence": DirectionalEvidence(),
            "context_24h": context_24h,
        }
    if not (o > 0 and h > 0 and l > 0 and c > 0):
        return {
            "direction": SignalDirection.NO_TRADE,
            "confidence": 0.0,
            "signal_score": SignalScore(),
            "setup": TradeSetup(),
            "reasons": ["Non-positive price"],
            "risks": ["Invalid price data"],
            "direction_evidence": DirectionalEvidence(),
            "context_24h": context_24h,
        }
    if not (h >= max(o, c) and l <= min(o, c)):
        return {
            "direction": SignalDirection.NO_TRADE,
            "confidence": 0.0,
            "signal_score": SignalScore(),
            "setup": TradeSetup(),
            "reasons": ["Invalid OHLC relationship"],
            "risks": ["Invalid price data"],
            "direction_evidence": DirectionalEvidence(),
            "context_24h": context_24h,
        }

    # Mode B: fold the signed 24H net into the market-context slot. When absent
    # (USE_24H_CONTEXT=false) ctx is exactly `market_context` — no behavior
    # change to the existing signal.
    ctx = market_context
    if context_24h is not None:
        netv = context_24h.get("net") if isinstance(context_24h, dict) else context_24h
        if isinstance(netv, (int, float)) and math.isfinite(netv):
            ctx = {**(market_context or {}), "symbol_24h": context_24h}

    ev = compute_directional_evidence(row, ctx)
    nets = ev["nets"]
    reasons = ev["reasons"]
    total = ev["total"]
    signal_score = SignalScore(
        trend_score=DIRECTIONAL_WEIGHTS["trend"] * (nets["trend"] + 1.0) / 2.0,
        momentum_score=DIRECTIONAL_WEIGHTS["momentum"] * (nets["momentum"] + 1.0) / 2.0,
        volume_score=DIRECTIONAL_WEIGHTS["volume"] * (nets["volume"] + 1.0) / 2.0,
        vwap_score=DIRECTIONAL_WEIGHTS["vwap"] * (nets["vwap"] + 1.0) / 2.0,
        price_action_score=DIRECTIONAL_WEIGHTS["price_action"] * (nets["price_action"] + 1.0) / 2.0,
        market_context_score=DIRECTIONAL_WEIGHTS["market_context"] * (nets["market_context"] + 1.0) / 2.0,
        risk_quality_score=RISK_QUALITY_WEIGHT * ev["risk_quality"],
        total=total,
    )

    if strategy_version is not None:
        direction = strategy_version.determine_direction(total)
    else:
        direction = determine_direction(total)

    # Risk gate: without valid ATR/price the trade cannot be sized — the
    # decision is NO_TRADE regardless of directional evidence.
    if not ev["risk_ok"]:
        return {
            "direction": SignalDirection.NO_TRADE,
            "confidence": 0.0,
            "signal_score": signal_score,
            "setup": TradeSetup(),
            "reasons": list(ev["risk_reasons"]),
            "risks": ["Risk data invalid - cannot size a trade"],
            "direction_evidence": ev["evidence"],
            "context_24h": context_24h,
        }

    # Conflict rule: strong LONG and strong SHORT evidence simultaneously →
    # NO_TRADE (a symbol is never pushed into a direction by slim margin when
    # the evidence is contradictory).
    if ev["conflict"]:
        return {
            "direction": SignalDirection.NO_TRADE,
            "confidence": 0.0,
            "signal_score": signal_score,
            "setup": TradeSetup(),
            "reasons": [
                f"Conflicting evidence: LONG {ev['long_total']:.1f} vs "
                f"SHORT {ev['short_total']:.1f}"
            ],
            "risks": ["Strong contradictory LONG and SHORT evidence"],
            "direction_evidence": ev["evidence"],
            "context_24h": context_24h,
        }

    if direction == SignalDirection.NO_TRADE:
        return {
            "direction": direction,
            "confidence": 0.0,
            "signal_score": signal_score,
            "setup": TradeSetup(),
            "reasons": [
                f"No clear signal - net evidence {ev['net']:+.1f} "
                "(multiple confirmations not met)"
            ],
            "risks": ["Insufficient confluence for a quality setup"],
            "direction_evidence": ev["evidence"],
            "context_24h": context_24h,
        }

    is_strong = direction in (SignalDirection.STRONG_LONG, SignalDirection.STRONG_SHORT)

    confidence = compute_confidence(signal_score, direction)
    setup = compute_trade_setup(row, direction, strategy_version)

    adx_val = row.get("adx_14", np.nan)
    rel_vol = row.get("relative_volume", np.nan)
    vwap = row.get("vwap", np.nan)
    price = row.get("close", np.nan)
    rsi_val = row.get("rsi_14", np.nan)

    if strategy_version is not None:
        min_rr_strong = strategy_version.min_rr_strong
        min_rr_normal = strategy_version.min_rr_normal
    else:
        min_rr_strong = 2.0
        min_rr_normal = 1.2

    rr_ok_strong = setup.risk_reward_ratio >= min_rr_strong
    rr_ok_normal = setup.risk_reward_ratio >= min_rr_normal

    strong_rejections = []
    if is_strong:
        if pd.isna(adx_val) or adx_val < (strategy_version.strong_min_adx if strategy_version else 25):
            strong_rejections.append("ADX < 25")
        if pd.isna(rel_vol) or rel_vol < (strategy_version.strong_min_rel_vol if strategy_version else 1.0):
            strong_rejections.append("RelVol < 1.0")
        if not pd.isna(vwap) and not pd.isna(price):
            if direction == SignalDirection.STRONG_LONG and price <= vwap:
                strong_rejections.append("Price <= VWAP")
            if direction == SignalDirection.STRONG_SHORT and price >= vwap:
                strong_rejections.append("Price >= VWAP")
        if not pd.isna(rsi_val):
            if direction == SignalDirection.STRONG_LONG and rsi_val > (strategy_version.strong_max_rsi_long if strategy_version else 75):
                strong_rejections.append("RSI > 75")
            if direction == SignalDirection.STRONG_SHORT and rsi_val < (strategy_version.strong_max_rsi_short if strategy_version else 25):
                strong_rejections.append("RSI < 25")

    strong_quality_ok = is_strong and rr_ok_strong and not strong_rejections
    normal_quality_ok = rr_ok_normal

    all_reasons = (
        reasons["trend"] + reasons["momentum"] + reasons["volume"]
        + reasons["vwap"] + reasons["price_action"] + reasons["market_context"]
    )
    all_risks = list(ev["risk_reasons"])

    if strong_quality_ok:
        if confidence < 60:
            all_risks.append("Low confidence score")
    elif normal_quality_ok:
        # Fall back to normal LONG/SHORT, preserving the original direction's
        # polarity (fixes the previous inversion bug where LONG/WEAK_LONG leaked
        # into the SHORT branch).
        if is_strong:
            direction = (
                SignalDirection.LONG
                if direction == SignalDirection.STRONG_LONG
                else SignalDirection.SHORT
            )
        confidence = compute_confidence(signal_score, direction)
        setup = compute_trade_setup(row, direction, strategy_version)
        if confidence < 60:
            all_risks.append("Low confidence score (downgraded from STRONG)")
    else:
        direction = SignalDirection.NO_TRADE
        confidence = 0.0
        setup = TradeSetup()
        if is_strong and not rr_ok_strong:
            all_reasons = [f"Setup rejected: risk/reward below STRONG threshold ({min_rr_strong})"]
        elif is_strong and strong_rejections:
            all_reasons = ["Strong signal rejected: " + "; ".join(strong_rejections)]
        else:
            all_reasons = [f"Setup rejected: risk/reward below minimum threshold ({min_rr_normal})"]
        all_risks = ["Poor risk/reward ratio"]

    return {
        "direction": direction,
        "confidence": confidence,
        "signal_score": signal_score,
        "setup": setup,
        "reasons": all_reasons,
        "risks": all_risks,
        "direction_evidence": ev["evidence"],
        "context_24h": context_24h,
    }


def evaluate_signal(
    df: pd.DataFrame,
    symbol: str,
    strategy: str = "multi_factor",
    data_source: DataSource = DataSource.MOCK,
    market_context: dict | None = None,
    data_age_seconds: int | None = None,
    data_status: str = "UNKNOWN",
    market_data_timestamp: datetime | None = None,
    strategy_version=None,
    context_24h: Optional[dict] = None,
) -> Optional[dict]:
    # Data quality checks
    if data_status in ("STALE", "UNAVAILABLE", "DELAYED"):
        return {"direction": SignalDirection.NO_TRADE, "confidence": 0.0,
                "reasons": [f"Data quality: {data_status} (age: {data_age_seconds}s)"],
                "risks": ["Stale or delayed data"]}
    
    if data_age_seconds is not None and data_age_seconds > 1800:  # 30 min
        return {"direction": SignalDirection.NO_TRADE, "confidence": 0.0,
                "reasons": [f"Data too old: {data_age_seconds}s"],
                "risks": ["Excessive data age"]}
    
    if len(df) < 55:
        return {"direction": SignalDirection.NO_TRADE, "confidence": 0.0,
                "reasons": ["Insufficient candle history"],
                "risks": ["Insufficient data"]}
    
    row = df.iloc[-1]
    # ``strategy_version`` may be a registered version NAME (e.g. "v1") or an
    # already-resolved StrategyVersion instance. Resolving once here means the
    # returned ``strategy_version`` label is the real registered name, so a
    # persisted signals row can never carry NULL just because the caller passed
    # a string. None keeps the historical no-version behaviour untouched.
    if strategy_version is None:
        sv = None
    elif isinstance(strategy_version, StrategyVersion):
        sv = strategy_version
    else:
        sv = get_strategy(strategy_version)
    decision = evaluate_row_signal(row, market_context=market_context, strategy_version=sv,
                                   context_24h=context_24h)

    if decision is None:
        return {
            "direction": SignalDirection.NO_TRADE,
            "confidence": 0.0,
            "reasons": ["Insufficient data for signal"],
            "risks": ["Insufficient data"],
        }

    direction = decision["direction"]
    confidence = decision["confidence"]
    signal_score = decision["signal_score"]
    setup = decision["setup"]
    explanation = SignalExplanation(reasons=decision["reasons"], risks=decision["risks"])

    indicator_values = {}
    for col in ["ema_9", "ema_20", "ema_50", "rsi_14", "macd", "macd_signal",
                "macd_histogram", "atr_14", "adx_14", "vwap", "bb_upper", "bb_lower",
                "relative_volume", "distance_from_vwap", "volatility_20"]:
        val = row.get(col, None)
        if val is not None and not (isinstance(val, float) and np.isnan(val)):
            indicator_values[col] = round(float(val), 2)
        else:
            indicator_values[col] = None

    signal_generated_at = now_ist()
    entry_ts = signal_generated_at
    sl_ts = signal_generated_at
    target_ts = signal_generated_at

    if market_data_timestamp is not None:
        if isinstance(market_data_timestamp, str):
            market_data_ts_str = market_data_timestamp
        else:
            market_data_ts_str = market_data_timestamp.isoformat()
    else:
        market_data_ts_str = None

    return {
        "id": uuid.uuid4().hex[:16],
        "symbol": symbol,
        "timestamp": signal_generated_at.isoformat(),
        "direction": direction.value,
        "confidence": confidence,
        "signal_score": signal_score.model_dump(),
        "setup": setup.model_dump(),
        "explanation": explanation.model_dump(),
        "reasons": explanation.reasons,
        "risks": explanation.risks,
        "direction_evidence": decision["direction_evidence"],
        "strategy": strategy,
        "strategy_version": getattr(sv, "version", None),
        "data_source": data_source,
        "indicator_values": indicator_values,
        "market_data_timestamp": market_data_ts_str,
        "signal_generated_at": signal_generated_at.isoformat(),
        "entry_updated_at": entry_ts.isoformat(),
        "stop_loss_updated_at": sl_ts.isoformat(),
        "target_updated_at": target_ts.isoformat(),
        "last_updated_at": signal_generated_at.isoformat(),
        "context_24h": context_24h,
    }
