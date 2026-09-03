import pandas as pd
import numpy as np
from typing import Optional
from datetime import datetime
from app.models.schemas import (
    SignalDirection, SignalScore, TradeSetup,
    SignalExplanation, DataSource
)
from app.services.indicators import calculate_all_indicators
from app.core.market_session import now_ist
import uuid


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


def compute_trade_setup(row: pd.Series, direction: SignalDirection | str, strategy_version=None) -> TradeSetup:
    price = row.get("close", 0)
    atr_val = row.get("atr_14", 0)
    if pd.isna(atr_val) or atr_val == 0:
        atr_val = price * 0.01

    dir_str = direction.value if isinstance(direction, SignalDirection) else str(direction)
    is_strong = dir_str == "STRONG_LONG" or dir_str == "STRONG_SHORT"

    if strategy_version is not None:
        stop_mult = strategy_version.stop_loss_atr_mult
        target_mult = strategy_version.strong_atr_mult if is_strong else strategy_version.normal_atr_mult
    else:
        stop_mult = 1.5
        target_mult = 3.0 if is_strong else 2.0

    if dir_str in ("LONG", "STRONG_LONG", "WEAK_LONG"):
        entry = price
        stop_loss = round(price - stop_mult * atr_val, 2)
        target_1 = round(price + target_mult * atr_val, 2)
        target_2 = round(price + (target_mult + 1.0) * atr_val, 2)
    elif dir_str in ("SHORT", "STRONG_SHORT", "WEAK_SHORT"):
        entry = price
        stop_loss = round(price + stop_mult * atr_val, 2)
        target_1 = round(price - target_mult * atr_val, 2)
        target_2 = round(price - (target_mult + 1.0) * atr_val, 2)
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


def evaluate_signal(
    df: pd.DataFrame,
    symbol: str,
    strategy: str = "multi_factor",
    data_source: DataSource = DataSource.MOCK,
    market_context: dict | None = None,
    data_age_seconds: int | None = None,
    data_status: str = "UNKNOWN",
    market_data_timestamp: datetime | None = None,
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
    
    # Check critical indicators
    row = df.iloc[-1]
    critical_indicators = ["atr_14", "adx_14", "vwap", "rsi_14", "relative_volume"]
    for ind in critical_indicators:
        val = row.get(ind, np.nan)
        if pd.isna(val) or (isinstance(val, float) and np.isnan(val)):
            return {"direction": SignalDirection.NO_TRADE, "confidence": 0.0,
                    "reasons": [f"Missing critical indicator: {ind}"],
                    "risks": ["Incomplete indicator data"]}
    
    # Check OHLC validity
    o, h, l, c = row.get("open", np.nan), row.get("high", np.nan), row.get("low", np.nan), row.get("close", np.nan)
    if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
        return {"direction": SignalDirection.NO_TRADE, "confidence": 0.0,
                "reasons": ["Invalid OHLC data"],
                "risks": ["Invalid price data"]}
    if not (o > 0 and h > 0 and l > 0 and c > 0):
        return {"direction": SignalDirection.NO_TRADE, "confidence": 0.0,
                "reasons": ["Non-positive price"],
                "risks": ["Invalid price data"]}
    if not (h >= max(o, c) and l <= min(o, c)):
        return {"direction": SignalDirection.NO_TRADE, "confidence": 0.0,
                "reasons": ["Invalid OHLC relationship"],
                "risks": ["Invalid price data"]}
    
    trend_score, trend_reasons = score_trend(row)
    momentum_score, momentum_reasons = score_momentum(row)
    volume_score, volume_reasons = score_volume(row)
    vwap_score, vwap_reasons = score_vwap(row)
    pa_score, pa_reasons = score_price_action(row)
    ctx_score, ctx_reasons = score_market_context(row, market_context)
    risk_score, risk_reasons = score_risk_quality(row)
    total = trend_score + momentum_score + volume_score + vwap_score + pa_score + ctx_score + risk_score
    signal_score = SignalScore(
        trend_score=trend_score,
        momentum_score=momentum_score,
        volume_score=volume_score,
        vwap_score=vwap_score,
        price_action_score=pa_score,
        market_context_score=ctx_score,
        risk_quality_score=risk_score,
        total=total,
    )
    direction = determine_direction(total)
    if direction == SignalDirection.NO_TRADE:
        confidence = 0.0
        setup = TradeSetup()
        all_reasons = ["No clear signal - multiple confirmations not met"]
        all_risks = ["Insufficient confluence for a quality setup"]
    else:
        # Try STRONG signal quality checks first
        is_strong = direction in (SignalDirection.STRONG_LONG, SignalDirection.STRONG_SHORT)
        
        confidence = compute_confidence(signal_score, direction)
        setup = compute_trade_setup(row, direction)
        
        adx_val = row.get("adx_14", np.nan)
        rel_vol = row.get("relative_volume", np.nan)
        vwap = row.get("vwap", np.nan)
        price = row.get("close", np.nan)
        rsi_val = row.get("rsi_14", np.nan)
        
        # Check R:R first
        min_rr_strong = 2.0
        min_rr_normal = 1.2
        rr_ok_strong = setup.risk_reward_ratio >= min_rr_strong
        rr_ok_normal = setup.risk_reward_ratio >= min_rr_normal
        
        # Check STRONG quality filters
        strong_rejections = []
        if is_strong:
            if pd.isna(adx_val) or adx_val < 25:
                strong_rejections.append("ADX < 25")
            if pd.isna(rel_vol) or rel_vol < 1.0:
                strong_rejections.append("RelVol < 1.0")
            if not pd.isna(vwap) and not pd.isna(price):
                if direction == SignalDirection.STRONG_LONG and price <= vwap:
                    strong_rejections.append("Price <= VWAP")
                if direction == SignalDirection.STRONG_SHORT and price >= vwap:
                    strong_rejections.append("Price >= VWAP")
            if not pd.isna(rsi_val):
                if direction == SignalDirection.STRONG_LONG and rsi_val > 75:
                    strong_rejections.append("RSI > 75")
                if direction == SignalDirection.STRONG_SHORT and rsi_val < 25:
                    strong_rejections.append("RSI < 25")
        
        # Determine final direction
        strong_quality_ok = is_strong and rr_ok_strong and not strong_rejections
        normal_quality_ok = rr_ok_normal
        
        if strong_quality_ok:
            # Keep STRONG signal
            all_reasons = trend_reasons + momentum_reasons + volume_reasons + vwap_reasons + pa_reasons
            all_risks = risk_reasons
            if confidence < 60:
                all_risks.append("Low confidence score")
        elif normal_quality_ok:
            # Fall back to normal LONG/SHORT
            direction = SignalDirection.LONG if direction == SignalDirection.STRONG_LONG else SignalDirection.SHORT
            confidence = compute_confidence(signal_score, direction)
            setup = compute_trade_setup(row, direction)
            all_reasons = trend_reasons + momentum_reasons + volume_reasons + vwap_reasons + pa_reasons
            all_risks = risk_reasons
            if confidence < 60:
                all_risks.append("Low confidence score (downgraded from STRONG)")
        else:
            direction = SignalDirection.NO_TRADE
            confidence = 0.0
            setup = TradeSetup()
            if is_strong and not rr_ok_strong:
                all_reasons = [f"Setup rejected: risk/reward below STRONG threshold (2.0)"]
            elif is_strong and strong_rejections:
                all_reasons = ["Strong signal rejected: " + "; ".join(strong_rejections)]
            else:
                all_reasons = [f"Setup rejected: risk/reward below minimum threshold (1.2)"]
            all_risks = ["Poor risk/reward ratio"]
    explanation = SignalExplanation(reasons=all_reasons, risks=all_risks)
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
        "strategy": strategy,
        "data_source": data_source,
        "indicator_values": indicator_values,
        "market_data_timestamp": market_data_ts_str,
        "signal_generated_at": signal_generated_at.isoformat(),
        "entry_updated_at": entry_ts.isoformat(),
        "stop_loss_updated_at": sl_ts.isoformat(),
        "target_updated_at": target_ts.isoformat(),
        "last_updated_at": signal_generated_at.isoformat(),
    }
