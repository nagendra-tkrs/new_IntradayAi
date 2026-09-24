"""Signal Engine V2 tests — symmetric directional-evidence layer (Phase 6).

Contract under test (see ``signal_engine.compute_directional_evidence`` and
the module docstring in ``signal_engine.py``):

  Weights (preserved from V1):
      Trend 20 · Momentum 15 · Volume 15 · VWAP 15 · Price Action 15 ·
      Market Context 10 · Risk Quality 10
  Risk Quality is a direction-NEUTRAL tradability gate (not a directional
  contributor); the six directional components sum to 90.

  Every directional component reduces to a signed net on [-1, +1]:
      net_i > 0 → bullish (long_i = net_i, short_i = 0)
      net_i < 0 → bearish (short_i = -net_i, long_i = 0)
      net_i = 0 → neutral / missing (BOTH sides 0 — never fabricated)

  Aggregation:
      long_total  = Σ W·max(net_i, 0),  short_total = Σ W·max(-net_i, 0)  ∈ [0,90]
      net         = long_total - short_total                              ∈ [-90,+90]
      total       = clamp(55 + net/2, 0, 100)            (band scale, NO_TRADE centred on 55)
      direction   = determine_direction(total)           (V1 bands 80/75/65/55/45/30)
  Conflict rule: min(long_total, short_total) ≥ 20 → NO_TRADE.

Golden cases A–J pin the V2 contract: each defines a deterministic synthetic
row and the EXPECTED direction / scores / classification, derived BY HAND
from the formulas above (not tuned against the implementation).

Equal opportunity, not equal counts: mirrored evidence must produce mirrored
component nets and mirror classifications about total 55 — but the engine
never forces LONG count == SHORT count anywhere.
"""

import math

import numpy as np
import pandas as pd
import pytest

from app.models.schemas import DirectionalEvidence, SignalDirection
from app.services.indicators import calculate_all_indicators
from app.services.signal_engine import (
    DIRECTIONAL_WEIGHTS,
    RISK_QUALITY_WEIGHT,
    NEUTRAL_TOTAL,
    NET_TO_TOTAL,
    CONFLICT_MIN_EVIDENCE,
    compute_directional_evidence,
    determine_direction,
    evaluate_row_signal,
    evaluate_signal,
    score_vwap,
    score_market_context,
    _net_price_action,
    _net_trend,
)

# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _base_row() -> pd.Series:
    """Fully populated, direction-NEUTRAL row (every net component = 0)."""
    return pd.Series({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
        "ema_9": 100.0, "ema_20": 100.0, "ema_50": 100.0,
        "atr_14": 1.5, "adx_14": 18.0, "relative_volume": 1.0,
        "vwap": 100.0, "rsi_14": 50.0,
        "macd": 0.0, "macd_signal": 0.0, "macd_histogram": 0.0,
        "roc_5": 0.0, "distance_from_vwap": 0.0,
        "prev_high": 102.0, "prev_low": 98.0,
        "opening_range_high": 102.0, "opening_range_low": 98.0,
        "bb_upper": 103.0, "bb_lower": 97.0,
        "volatility_20": 0.3,
    })


def _row(**overrides) -> pd.Series:
    row = _base_row()
    for key, value in overrides.items():
        row[key] = value
    return row


def _ctx(trend: str = "NEUTRAL", change: float = 0.0) -> dict:
    return {"nifty_trend": trend, "nifty_change_pct": change,
            "banknifty_change_pct": change}


def _dec(row: pd.Series, ctx: dict | None = "DEFAULT") -> dict:
    """evaluate_row_signal with an optional market context (never None-by-default)."""
    if ctx == "DEFAULT":
        ctx = None
    d = evaluate_row_signal(row, market_context=ctx)
    assert d is not None, "decision should never be None for a populated row"
    return d


def _finite_fields(d: dict) -> list[str]:
    """Return names of scalar fields in a decision that must be finite."""
    score = d["signal_score"]
    ev = d["direction_evidence"]
    fields = {
        "score.total": score.total,
        "score.trend_score": score.trend_score,
        "score.momentum_score": score.momentum_score,
        "score.volume_score": score.volume_score,
        "score.vwap_score": score.vwap_score,
        "score.price_action_score": score.price_action_score,
        "score.market_context_score": score.market_context_score,
        "score.risk_quality_score": score.risk_quality_score,
        "confidence": d["confidence"],
        "ev.long_total": ev.long_total,
        "ev.short_total": ev.short_total,
        "ev.net": ev.net,
        "ev.trend_long": ev.trend_long, "ev.trend_short": ev.trend_short,
        "ev.momentum_long": ev.momentum_long, "ev.momentum_short": ev.momentum_short,
        "ev.volume_long": ev.volume_long, "ev.volume_short": ev.volume_short,
        "ev.vwap_long": ev.vwap_long, "ev.vwap_short": ev.vwap_short,
        "ev.price_action_long": ev.price_action_long, "ev.price_action_short": ev.price_action_short,
        "ev.market_context_long": ev.market_context_long, "ev.market_context_short": ev.market_context_short,
        "ev.risk_quality": ev.risk_quality,
    }
    bad = [name for name, v in fields.items() if not math.isfinite(float(v))]
    return bad


# ────────────────────────────────────────────────────────────────────────────
# Golden case A — strong bullish → STRONG_LONG
# ────────────────────────────────────────────────────────────────────────────


def test_golden_a_strong_bullish_row_classifies_strong_long():
    """Documented hand-derivation for the strong bullish row:

        trend     A=(c>e9?0)(e9>e20?1)(e20>e50?1)=2, B=1           → +1/3     × 20  = +6.667
        momentum  rsi 60→+1/3, macd 0.5/1.05→+0.476, roc 1.5/3→+0.5  → +0.410  × 15  = +6.143
        volume    rel 2.5 → 0.75 participation, bull bar              → +0.750  × 15  = +11.25
        vwap      dist 5.0% → clip +1                                → +1.000  × 15  = +15.0
        price act above OR-high & prev-high → +2/3                   → +0.667  × 15  = +10.0
        context   BULLISH +1.0 → +1                                  → +1.000  × 10  = +10.0
        net ≈ +59.06  →  total = 55 + 29.53 ≈ 84.53  →  STRONG_LONG
    """
    row = _row(
        open=100.0, high=106.0, low=99.0, close=105.0,
        ema_9=110.0, ema_20=105.0, ema_50=100.0,
        atr_14=2.0, adx_14=30.0, relative_volume=2.5,
        vwap=100.0, rsi_14=60.0,
        macd=0.3, macd_signal=0.2, macd_histogram=0.5,
        roc_5=1.5, distance_from_vwap=5.0,
        prev_high=103.0, prev_low=97.0,
        opening_range_high=104.0, opening_range_low=96.0,
        bb_upper=110.0, bb_lower=90.0,
    )
    d = _dec(row, _ctx("BULLISH", 1.0))

    assert d["direction"] == SignalDirection.STRONG_LONG
    assert d["confidence"] > 0
    assert d["direction_evidence"].conflict is False
    assert d["signal_score"].total == pytest.approx(84.53, rel=1e-3)
    assert d["direction_evidence"].long_total == pytest.approx(59.06, rel=1e-2)
    assert d["direction_evidence"].short_total == pytest.approx(0.0, abs=1e-6)
    assert d["direction_evidence"].net == pytest.approx(59.06, rel=1e-2)
    # Component nets (documented).
    ev = d["direction_evidence"]
    assert ev.trend_long == pytest.approx(1 / 3, rel=1e-3) and ev.trend_short == 0
    assert ev.momentum_long == pytest.approx(0.40952, rel=1e-3) and ev.momentum_short == 0
    assert ev.volume_long == pytest.approx(0.75, rel=1e-3) and ev.volume_short == 0
    assert ev.vwap_long == pytest.approx(1.0, abs=1e-6) and ev.vwap_short == 0
    assert ev.price_action_long == pytest.approx(2 / 3, rel=1e-3) and ev.price_action_short == 0
    assert ev.market_context_long == pytest.approx(1.0, abs=1e-6) and ev.market_context_short == 0
    # Classic-scale component scores (symmetric: w·(net+1)/2).
    s = d["signal_score"]
    assert s.trend_score == pytest.approx(20 * (1 / 3 + 1) / 2, rel=1e-3)
    assert s.momentum_score == pytest.approx(15 * (0.40952 + 1) / 2, rel=1e-3)
    assert s.volume_score == pytest.approx(15 * 0.875, rel=1e-3)
    assert s.vwap_score == pytest.approx(15.0, abs=1e-6)
    assert s.price_action_score == pytest.approx(12.5, rel=1e-3)
    assert s.market_context_score == pytest.approx(10.0, abs=1e-6)
    assert s.risk_quality_score == pytest.approx(10.0, abs=1e-6)
    assert _finite_fields(d) == []


# ────────────────────────────────────────────────────────────────────────────
# Golden case B — strong bearish → STRONG_SHORT
# ────────────────────────────────────────────────────────────────────────────


def test_golden_b_strong_bearish_row_classifies_strong_short():
    """Documented hand-derivation:

        trend     A=1, B=2                          → -1/3   × 20 =  -6.667
        momentum  rsi 40→-1/3, macd -0.5/0.95, roc -0.5 → -0.425  × 15 =  -6.37
        volume    rel 2.5, bear bar                 → -0.75  × 15 = -11.25
        vwap      dist -5.0%                        → -1.0   × 15 = -15.0
        price act below OR-low, prev-low, BB-low → -3/3   × 15 = -15.0
        context   BEARISH -1.0                      → -1.0   × 10 = -10.0
        net ≈ -64.29  →  total = 55 - 32.14 ≈ 22.86  →  STRONG_SHORT
    """
    row = _row(
        open=100.0, high=101.0, low=94.0, close=95.0,
        ema_9=93.0, ema_20=97.0, ema_50=101.0,
        atr_14=1.5, adx_14=30.0, relative_volume=2.5,
        vwap=100.0, rsi_14=40.0,
        macd=-0.4, macd_signal=-0.2, macd_histogram=-0.5,
        roc_5=-1.5, distance_from_vwap=-5.0,
        prev_high=99.0, prev_low=97.0,
        opening_range_high=99.0, opening_range_low=98.0,
        bb_upper=105.0, bb_lower=96.0,
    )
    d = _dec(row, _ctx("BEARISH", -1.0))

    assert d["direction"] == SignalDirection.STRONG_SHORT
    assert d["confidence"] > 0
    assert d["direction_evidence"].conflict is False
    assert d["signal_score"].total == pytest.approx(22.86, rel=1e-2)
    assert d["direction_evidence"].short_total == pytest.approx(64.29, rel=1e-2)
    assert d["direction_evidence"].long_total == pytest.approx(0.0, abs=1e-6)
    ev = d["direction_evidence"]
    assert ev.trend_short == pytest.approx(1 / 3, rel=1e-3) and ev.trend_long == 0
    assert ev.momentum_short == pytest.approx(0.42456, rel=1e-3) and ev.momentum_long == 0
    assert ev.volume_short == pytest.approx(0.75, rel=1e-3) and ev.volume_long == 0
    assert ev.vwap_short == pytest.approx(1.0, abs=1e-6) and ev.vwap_long == 0
    assert ev.price_action_short == pytest.approx(1.0, abs=1e-6) and ev.price_action_long == 0
    assert ev.market_context_short == pytest.approx(1.0, abs=1e-6) and ev.market_context_long == 0
    assert _finite_fields(d) == []


# ────────────────────────────────────────────────────────────────────────────
# Golden case C — neutral → NO_TRADE
# ────────────────────────────────────────────────────────────────────────────


def test_golden_c_neutral_row_is_no_trade():
    """All component inputs neutral (price == EMAs, RSI 50, rel vol 1.0,
    price at VWAP, within reference levels, no market context) → net 0,
    total 55 → NO_TRADE with zero confidence."""
    row = _base_row()
    d = _dec(row, _ctx("NEUTRAL", 0.0))

    assert d["direction"] == SignalDirection.NO_TRADE
    assert d["confidence"] == 0.0
    assert d["signal_score"].total == pytest.approx(NEUTRAL_TOTAL, abs=1e-6)
    ev = d["direction_evidence"]
    assert ev.net == pytest.approx(0.0, abs=1e-6)
    assert ev.long_total == pytest.approx(0.0, abs=1e-6)
    assert ev.short_total == pytest.approx(0.0, abs=1e-6)
    assert ev.conflict is False
    # Classic-scale neutral = weight / 2, symmetric for every component.
    assert d["signal_score"].trend_score == pytest.approx(10.0, abs=1e-6)
    assert d["signal_score"].momentum_score == pytest.approx(7.5, abs=1e-6)
    assert d["signal_score"].volume_score == pytest.approx(7.5, abs=1e-6)
    assert d["signal_score"].vwap_score == pytest.approx(7.5, abs=1e-6)
    assert d["signal_score"].price_action_score == pytest.approx(7.5, abs=1e-6)
    assert d["signal_score"].market_context_score == pytest.approx(5.0, abs=1e-6)
    assert _finite_fields(d) == []


def test_golden_c_reason_is_no_clear_signal():
    row = _base_row()
    d = _dec(row, _ctx("NEUTRAL", 0.0))
    assert d["reasons"][0].startswith("No clear signal - net evidence")
    assert "multiple confirmations not met" in d["reasons"][0]


# ────────────────────────────────────────────────────────────────────────────
# Golden case D — conflict (bullish trend/VWAP vs bearish momentum/volume) → NO_TRADE
# ────────────────────────────────────────────────────────────────────────────


def test_golden_d_conflicting_evidence_is_no_trade():
    """Long evidence: trend +6.67, vwap +15, price action +5  →  L = 26.67
       Short evidence: momentum 12.5, volume 11.25            →  S = 23.75
       min(L,S) = 23.75 ≥ 20 → conflict → NO_TRADE (never forced LONG/SHORT
       by a slim margin on contradictory evidence)."""
    row = _row(
        open=107.0, high=108.0, low=104.0, close=105.0,
        ema_9=110.0, ema_20=105.0, ema_50=100.0,
        atr_14=1.5, adx_14=30.0, relative_volume=2.5,
        vwap=100.0, rsi_14=30.0,
        macd=-0.4, macd_signal=-0.1, macd_histogram=-2.0,
        roc_5=-5.0, distance_from_vwap=4.0,
        prev_high=104.0, prev_low=96.0,
        opening_range_high=106.0, opening_range_low=100.0,
        bb_upper=112.0, bb_lower=92.0,
    )
    d = _dec(row, _ctx("NEUTRAL", 0.0))

    assert d["direction"] == SignalDirection.NO_TRADE
    assert d["confidence"] == 0.0
    ev = d["direction_evidence"]
    assert ev.conflict is True
    assert ev.long_total == pytest.approx(26.67, rel=1e-2)
    assert ev.short_total == pytest.approx(23.75, rel=1e-2)
    assert ev.net == pytest.approx(2.92, rel=1e-2)
    # short_total renders 23.7 (component nets are rounded to 6 dp before the
    # weighted aggregate: momentum -0.833333×15 = 12.499995 + 11.25).
    assert d["reasons"][0].startswith("Conflicting evidence: LONG 26.7 vs SHORT 23.7")
    assert "contradictory" in " ".join(d["risks"]).lower()
    assert _finite_fields(d) == []


# ────────────────────────────────────────────────────────────────────────────
# Golden case E — conflict (bearish trend/VWAP vs bullish momentum/volume) → NO_TRADE
# ────────────────────────────────────────────────────────────────────────────


def test_golden_e_mirrored_conflict_is_no_trade():
    """Mirror of case D: Short trend/vwap vs long momentum/volume.
       L = 23.75, S = 21.67 → min ≥ 20 → conflict → NO_TRADE."""
    row = _row(
        open=93.0, high=96.0, low=92.0, close=95.0,
        ema_9=90.0, ema_20=95.0, ema_50=100.0,
        atr_14=1.5, adx_14=30.0, relative_volume=2.5,
        vwap=100.0, rsi_14=70.0,
        macd=0.4, macd_signal=0.2, macd_histogram=2.0,
        roc_5=5.0, distance_from_vwap=-4.0,
        prev_high=96.0, prev_low=88.0,
        opening_range_high=97.0, opening_range_low=90.0,
        bb_upper=100.0, bb_lower=88.0,
    )
    d = _dec(row, _ctx("NEUTRAL", 0.0))

    assert d["direction"] == SignalDirection.NO_TRADE
    assert d["direction_evidence"].conflict is True
    assert d["direction_evidence"].long_total == pytest.approx(23.75, rel=1e-2)
    assert d["direction_evidence"].short_total == pytest.approx(21.67, rel=1e-2)
    assert d["direction_evidence"].net == pytest.approx(2.0833, rel=1e-3)
    assert _finite_fields(d) == []


# ────────────────────────────────────────────────────────────────────────────
# Golden cases F–H — missing critical data → NO_TRADE (critical gate)
# ────────────────────────────────────────────────────────────────────────────


def test_golden_f_missing_relative_volume_is_no_trade():
    row = _row(relative_volume=np.nan)
    d = _dec(row, None)
    assert d["direction"] == SignalDirection.NO_TRADE
    assert d["reasons"] == ["Missing critical indicator: relative_volume"]
    assert d["direction_evidence"].conflict is False
    assert _finite_fields(d) == []


def test_golden_g_missing_vwap_is_no_trade():
    row = _row(vwap=np.nan, distance_from_vwap=np.nan)
    d = _dec(row, None)
    assert d["direction"] == SignalDirection.NO_TRADE
    assert d["reasons"] == ["Missing critical indicator: vwap"]
    assert _finite_fields(d) == []


def test_golden_h_insufficient_data_atr_is_no_trade():
    row = _row(atr_14=np.nan)
    d = _dec(row, None)
    assert d["direction"] == SignalDirection.NO_TRADE
    assert d["reasons"] == ["Missing critical indicator: atr_14"]
    assert _finite_fields(d) == []


# ────────────────────────────────────────────────────────────────────────────
# Golden case I — NaN in a non-critical indicator → neutral, no NaN in scores
# ────────────────────────────────────────────────────────────────────────────


def test_golden_i_nan_roc_is_neutral_not_noise():
    """Strong bullish row with roc_5 = NaN. The ROC sub-evidence contributes
    0 (neutral), the momentum net drops to 0.31 and stays finite; the final
    decision is still STRONG_LONG — NaN never injects a fake direction."""
    row = _row(
        open=100.0, high=106.0, low=99.0, close=105.0,
        ema_9=110.0, ema_20=105.0, ema_50=100.0,
        atr_14=2.0, adx_14=30.0, relative_volume=2.5,
        vwap=100.0, rsi_14=60.0,
        macd=0.3, macd_signal=0.2, macd_histogram=0.5,
        roc_5=np.nan, distance_from_vwap=5.0,
        prev_high=103.0, prev_low=97.0,
        opening_range_high=104.0, opening_range_low=96.0,
        bb_upper=110.0, bb_lower=90.0,
    )
    d = _dec(row, _ctx("BULLISH", 1.0))

    assert d["direction"] == SignalDirection.STRONG_LONG
    ev = d["direction_evidence"]
    # momentum = 0.5·(1/3) + 0.3·(0.5/1.05) + 0.2·0 ≈ 0.30952 (roc neutral)
    assert ev.momentum_long == pytest.approx(0.30952, rel=1e-3)
    assert d["signal_score"].momentum_score == pytest.approx(15 * (0.30952 + 1) / 2, rel=1e-3)
    assert d["signal_score"].total == pytest.approx(55 + (57.56) / 2, rel=1e-2)
    assert _finite_fields(d) == []


# ────────────────────────────────────────────────────────────────────────────
# Golden case J — Inf in an indicator → treated as MISSING, never fabricated
# ────────────────────────────────────────────────────────────────────────────


def test_golden_j_inf_rsi_is_treated_as_missing_not_direction():
    """rsi_14 = +Inf passes the critical-indicator check (Inf is not NaN) but
    the component layer treats it as missing → momentum 0. The final score
    stays finite and the direction is NO_TRADE (nothing fabricated from Inf)."""
    row = _row(rsi_14=float("inf"))
    d = _dec(row, None)

    assert d["direction"] == SignalDirection.NO_TRADE
    ev = d["direction_evidence"]
    assert ev.momentum_long == 0.0
    assert ev.momentum_short == 0.0
    assert d["signal_score"].total == pytest.approx(NEUTRAL_TOTAL, abs=1e-6)
    assert _finite_fields(d) == []


# ────────────────────────────────────────────────────────────────────────────
# Component-level directional contract
# ────────────────────────────────────────────────────────────────────────────


def test_missing_inputs_are_neutral_on_both_sides():
    """Every component with unusable inputs returns 0.0 net → BOTH evidence
    sides zero (no fabricated direction for missing data)."""
    row = _base_row().copy()
    row = pd.Series({k: np.nan for k in row.index})  # all inputs missing
    ev = compute_directional_evidence(row, market_context=None)
    for key in DIRECTIONAL_WEIGHTS:
        assert ev["nets"][key] == 0.0, key
    assert ev["long_total"] == 0.0 and ev["short_total"] == 0.0 and ev["net"] == 0.0
    assert ev["risk_ok"] is False  # ATR missing → gate blocks


def test_volume_confirms_direction_not_participation():
    """High volume alone is NOT bullish evidence — it confirms the bar's own
    direction. Same participation, opposite bar → opposite signed net."""
    bull = _row(relative_volume=2.5, open=99.0, close=101.0, high=102.0, low=98.5)
    bear = _row(relative_volume=2.5, open=101.0, close=99.0, high=102.0, low=98.5)
    flat = _row(relative_volume=2.5, open=100.0, close=100.0, high=101.0, low=99.0)
    low = _row(relative_volume=1.0, open=99.0, close=101.0, high=102.0, low=98.5)

    ev_bull = compute_directional_evidence(bull, None)
    ev_bear = compute_directional_evidence(bear, None)
    ev_flat = compute_directional_evidence(flat, None)
    ev_low = compute_directional_evidence(low, None)

    assert ev_bull["nets"]["volume"] == pytest.approx(0.75, rel=1e-3)
    assert ev_bear["nets"]["volume"] == pytest.approx(-0.75, rel=1e-3)
    assert ev_flat["nets"]["volume"] == 0.0  # flat bar → no confirmation
    assert ev_low["nets"]["volume"] == 0.0   # 1.0× average → 0 participation


def test_market_context_is_symmetric_and_missing_is_neutral():
    bull = compute_directional_evidence(_base_row(), _ctx("BULLISH", 1.0))
    bear = compute_directional_evidence(_base_row(), _ctx("BEARISH", -1.0))
    partial = compute_directional_evidence(_base_row(), _ctx("BULLISH", 0.25))
    none_ctx = compute_directional_evidence(_base_row(), None)
    neutral_ctx = compute_directional_evidence(_base_row(), _ctx("NEUTRAL", 0.0))

    assert bull["nets"]["market_context"] == pytest.approx(1.0, abs=1e-6)
    assert bear["nets"]["market_context"] == pytest.approx(-1.0, abs=1e-6)
    # 0.25% move → magnitude 0.5 (scaled, not binary)
    assert partial["nets"]["market_context"] == pytest.approx(0.5, abs=1e-6)
    assert none_ctx["nets"]["market_context"] == 0.0
    assert neutral_ctx["nets"]["market_context"] == 0.0


def test_trend_strength_scaling():
    """Same EMA alignment, different ADX → documented strength factors."""
    for adx, strength in ((30.0, 1.0), (18.0, 0.7), (10.0, 0.5), (np.nan, 0.6)):
        row = _row(close=106.0, ema_9=105.0, ema_20=104.0, ema_50=103.0, adx_14=adx)
        net, reasons = _net_trend(row)
        assert net == pytest.approx(strength, rel=1e-3), f"ADX={adx}"
        assert reasons  # always explainable
    # Bearish mirror flips the sign with equal magnitude.
    bear_row = _row(close=104.0, ema_9=105.0, ema_20=106.0, ema_50=107.0, adx_14=30.0)
    net_bear, _ = _net_trend(bear_row)
    assert net_bear == pytest.approx(-1.0, rel=1e-3)


def test_price_action_neutral_without_reference_levels():
    row = _row(opening_range_high=np.nan, opening_range_low=np.nan,
               prev_high=np.nan, prev_low=np.nan,
               bb_upper=np.nan, bb_lower=np.nan)
    net, reasons = _net_price_action(row)
    assert net == 0.0
    assert len(reasons) == 1
    assert "Opening range" in reasons[0]


# ────────────────────────────────────────────────────────────────────────────
# Symmetry — equal opportunity (mirrored rows → mirrored nets & bands)
# ────────────────────────────────────────────────────────────────────────────


def test_symmetric_bull_bear_pair_mirrors_bands():
    """A hand-built symmetric pair: net_bear == -net_bear's bull counterpart
    componentwise, long_total/bull == short_total/bear, and the classic totals
    mirror about 55 (bull 73.48 = WEAK_LONG, bear 36.52 = SHORT). Counts need
    not be equal — opportunity must be."""
    bull = _row(
        open=99.0, high=101.0, low=98.0, close=100.0,
        ema_9=102.0, ema_20=101.0, ema_50=100.0,
        atr_14=1.5, adx_14=30.0, relative_volume=2.0,
        vwap=100.0, rsi_14=60.0,
        macd=0.3, macd_signal=0.2, macd_histogram=0.4,
        roc_5=1.0, distance_from_vwap=1.0,
        prev_high=101.0, prev_low=99.0,
        opening_range_high=101.0, opening_range_low=99.0,
        bb_upper=103.0, bb_lower=97.0,
    )
    bear = _row(
        open=101.0, high=102.0, low=98.0, close=100.0,
        ema_9=98.0, ema_20=99.0, ema_50=100.0,
        atr_14=1.5, adx_14=30.0, relative_volume=2.0,
        vwap=100.0, rsi_14=40.0,
        macd=-0.3, macd_signal=-0.2, macd_histogram=-0.4,
        roc_5=-1.0, distance_from_vwap=-1.0,
        prev_high=101.0, prev_low=99.0,
        opening_range_high=101.0, opening_range_low=99.0,
        bb_upper=103.0, bb_lower=97.0,
        volatility_20=0.3,
    )

    db = _dec(bull, _ctx("BULLISH", 1.0))
    ds = _dec(bear, _ctx("BEARISH", -1.0))

    eb, es = db["direction_evidence"], ds["direction_evidence"]
    # Componentwise mirror.
    for key in DIRECTIONAL_WEIGHTS:
        nb = db_ev_nets(db, key)
        ns = db_ev_nets(ds, key)
        assert nb == pytest.approx(-ns, rel=1e-6), key
    # Aggregates mirror.
    assert eb.long_total == pytest.approx(es.short_total, rel=1e-6)
    assert es.long_total == pytest.approx(eb.short_total, rel=1e-6)
    assert eb.net == pytest.approx(-es.net, rel=1e-6)
    # Classic totals mirror about 55.
    assert db["signal_score"].total + ds["signal_score"].total == pytest.approx(110.0, rel=1e-3)
    # Band classifications: bull → WEAK_LONG (73.48), bear → SHORT (36.52).
    assert db["signal_score"].total == pytest.approx(73.48, rel=1e-2)
    assert ds["signal_score"].total == pytest.approx(36.52, rel=1e-2)
    assert db["direction"] == SignalDirection.WEAK_LONG and db["direction"].is_long
    assert ds["direction"] == SignalDirection.SHORT and ds["direction"].is_short
    assert _finite_fields(db) == []
    assert _finite_fields(ds) == []


def db_ev_nets(d: dict, key: str) -> float:
    ev = d["direction_evidence"]
    return getattr(ev, f"{key}_long") - getattr(ev, f"{key}_short")


def test_every_component_reports_both_sides_across_mixed_regime():
    """Full-population invariant: across a deterministic mixed bull/bear/neutral
    market, EVERY directional component contributes evidence to BOTH directions
    at least once — i.e. each component is able to argue for a short, not just
    a long. This is the equal-opportunity guarantee at component level."""
    # market_context is supplied per-call (None here), never derived from the
    # frame — its symmetry is covered by test_market_context_is_symmetric_*.
    frame_components = [k for k in DIRECTIONAL_WEIGHTS if k != "market_context"]
    df = _market_frame(bull_bars=150, bear_bars=150, neutral_bars=150, seed=11)
    long_sum = {k: 0.0 for k in frame_components}
    short_sum = {k: 0.0 for k in frame_components}
    for i in range(55, len(df)):
        d = evaluate_row_signal(df.iloc[i], market_context=None)
        assert d is not None
        for key in frame_components:
            net = db_ev_nets(d, key)
            long_sum[key] += net if net > 0 else 0.0
            short_sum[key] += -net if net < 0 else 0.0
    for key in frame_components:
        assert long_sum[key] > 0.0, f"{key}: no long evidence in mixed regime"
        assert short_sum[key] > 0.0, f"{key}: no short evidence in mixed regime"


# ────────────────────────────────────────────────────────────────────────────
# Bounds, determinism, data quality gates
# ────────────────────────────────────────────────────────────────────────────


def test_all_golden_fields_within_documented_bounds():
    """Every golden row + the neutral base: all nets in [-1, 1], weighted
    evidence totals in [0, 90], classic total in [0, 100], confidence in
    [0, 100], risk quality in [0, 1]."""
    rows = [
        ("A", _row(open=100.0, high=106.0, low=99.0, close=105.0, ema_9=110.0,
                   ema_20=105.0, ema_50=100.0, atr_14=2.0, adx_14=30.0,
                   relative_volume=2.5, vwap=100.0, rsi_14=60.0, macd=0.3,
                   macd_signal=0.2, macd_histogram=0.5, roc_5=1.5,
                   distance_from_vwap=5.0, prev_high=103.0, prev_low=97.0,
                   opening_range_high=104.0, opening_range_low=96.0,
                   bb_upper=110.0, bb_lower=90.0), _ctx("BULLISH", 1.0)),
        ("B", _row(open=100.0, high=101.0, low=94.0, close=95.0, ema_9=93.0,
                   ema_20=97.0, ema_50=101.0, atr_14=1.5, adx_14=30.0,
                   relative_volume=2.5, vwap=100.0, rsi_14=40.0, macd=-0.4,
                   macd_signal=-0.2, macd_histogram=-0.5, roc_5=-1.5,
                   distance_from_vwap=-5.0, prev_high=99.0, prev_low=97.0,
                   opening_range_high=99.0, opening_range_low=98.0,
                   bb_upper=105.0, bb_lower=96.0), _ctx("BEARISH", -1.0)),
        ("C", _base_row(), _ctx("NEUTRAL", 0.0)),
        ("D", _row(open=107.0, high=108.0, low=104.0, close=105.0, ema_9=110.0,
                   ema_20=105.0, ema_50=100.0, atr_14=1.5, adx_14=30.0,
                   relative_volume=2.5, vwap=100.0, rsi_14=30.0, macd=-0.4,
                   macd_signal=-0.1, macd_histogram=-2.0, roc_5=-5.0,
                   distance_from_vwap=4.0, prev_high=104.0, prev_low=96.0,
                   opening_range_high=106.0, opening_range_low=100.0,
                   bb_upper=112.0, bb_lower=92.0), _ctx("NEUTRAL", 0.0)),
        ("I", _row(open=100.0, high=106.0, low=99.0, close=105.0, ema_9=110.0,
                   ema_20=105.0, ema_50=100.0, atr_14=2.0, adx_14=30.0,
                   relative_volume=2.5, vwap=100.0, rsi_14=60.0, macd=0.3,
                   macd_signal=0.2, macd_histogram=0.5, roc_5=np.nan,
                   distance_from_vwap=5.0, prev_high=103.0, prev_low=97.0,
                   opening_range_high=104.0, opening_range_low=96.0,
                   bb_upper=110.0, bb_lower=90.0), _ctx("BULLISH", 1.0)),
        ("J", _row(rsi_14=float("inf")), None),
    ]
    for label, row, ctx in rows:
        d = _dec(row, ctx)
        ev = d["direction_evidence"]
        assert 0.0 <= d["signal_score"].total <= 100.0, label
        assert 0.0 <= d["confidence"] <= 100.0, label
        assert 0.0 <= ev.long_total <= 90.0, label
        assert 0.0 <= ev.short_total <= 90.0, label
        assert 0.0 <= ev.risk_quality <= 1.0, label
        for key in DIRECTIONAL_WEIGHTS:
            net = db_ev_nets(d, key)
            assert -1.0 <= net <= 1.0, (label, key)
        assert _finite_fields(d) == [], label


def test_weights_structure_preserved():
    assert DIRECTIONAL_WEIGHTS == {
        "trend": 20.0, "momentum": 15.0, "volume": 15.0, "vwap": 15.0,
        "price_action": 15.0, "market_context": 10.0,
    }
    assert sum(DIRECTIONAL_WEIGHTS.values()) == 90.0
    assert RISK_QUALITY_WEIGHT == 10.0
    assert sum(DIRECTIONAL_WEIGHTS.values()) + RISK_QUALITY_WEIGHT == 100.0
    assert NEUTRAL_TOTAL == 55.0 and NET_TO_TOTAL == 0.5
    assert CONFLICT_MIN_EVIDENCE == 20.0


def test_extreme_all_bull_row_approaches_upper_bound():
    """All six directional components maxed out → net ≈ +88.5 → total ≈ 99.25
    (scoring extreme). RSI 74 keeps the STRONG filter happy; the direction
    remains STRONG_LONG."""
    row = _row(
        close=106.0, open=104.0, high=107.0, low=103.0,
        ema_9=105.0, ema_20=104.0, ema_50=103.0,
        atr_14=1.5, adx_14=40.0, relative_volume=4.0,
        vwap=100.0, rsi_14=74.0,
        macd=1.0, macd_signal=0.5, macd_histogram=1.06,
        roc_5=3.0, distance_from_vwap=5.0,
        prev_high=105.0, prev_low=101.0,
        opening_range_high=105.0, opening_range_low=101.0,
        bb_upper=105.5, bb_lower=100.5,
        volatility_20=0.5,
    )
    d = _dec(row, _ctx("BULLISH", 1.0))
    assert d["direction"] == SignalDirection.STRONG_LONG
    assert d["signal_score"].total == pytest.approx(99.25, rel=1e-2)


def test_extreme_all_bear_row_approaches_lower_bound():
    row = _row(
        close=94.0, open=96.0, high=97.0, low=93.0,
        ema_9=95.0, ema_20=96.0, ema_50=97.0,
        atr_14=1.5, adx_14=40.0, relative_volume=4.0,
        vwap=100.0, rsi_14=26.0,
        macd=-1.0, macd_signal=-0.5, macd_histogram=-0.94,
        roc_5=-3.0, distance_from_vwap=-5.0,
        prev_high=95.0, prev_low=94.5,
        opening_range_high=95.0, opening_range_low=94.5,
        bb_upper=99.0, bb_lower=95.5,
        volatility_20=0.5,
    )
    d = _dec(row, _ctx("BEARISH", -1.0))
    assert d["direction"] == SignalDirection.STRONG_SHORT
    assert d["signal_score"].total == pytest.approx(55 - 44.25, rel=1e-2)


def test_deterministic_same_input_same_decision():
    row = _row(open=100.0, high=106.0, low=99.0, close=105.0, ema_9=110.0,
               ema_20=105.0, ema_50=100.0, atr_14=2.0, adx_14=30.0,
               relative_volume=2.5, vwap=100.0, rsi_14=60.0, macd=0.3,
               macd_signal=0.2, macd_histogram=0.5, roc_5=1.5,
               distance_from_vwap=5.0, prev_high=103.0, prev_low=97.0,
               opening_range_high=104.0, opening_range_low=96.0,
               bb_upper=110.0, bb_lower=90.0)
    ctx = _ctx("BULLISH", 1.0)
    d1 = _dec(row, ctx)
    d2 = _dec(row, ctx)
    assert d1 == d2
    assert d1["direction_evidence"] == d2["direction_evidence"]


def test_risk_quality_is_gate_not_direction_maker():
    """Wildly different (but valid) ATR does not change the direction — risk
    filters tradability, it never manufactures direction."""
    base = dict(
        open=100.0, high=106.0, low=99.0, close=105.0, ema_9=110.0,
        ema_20=105.0, ema_50=100.0, adx_14=30.0, relative_volume=2.5,
        vwap=100.0, rsi_14=60.0, macd=0.3, macd_signal=0.2,
        macd_histogram=0.5, roc_5=1.5, distance_from_vwap=5.0,
        prev_high=103.0, prev_low=97.0, opening_range_high=104.0,
        opening_range_low=96.0, bb_upper=110.0, bb_lower=90.0,
    )
    ctx = _ctx("BULLISH", 1.0)
    # ATR 0.3 → 0.29% (confined/limited); RR stays 3/1.5 = 2.0 for STRONG here
    # (a much tinier ATR would drop RR below 2.0 via rounding and downgrade the
    # strength — that is the RR gate, not the risk-quality handle).
    low_atr = _dec(_row(atr_14=0.3, **base), ctx)
    good_atr = _dec(_row(atr_14=2.0, **base), ctx)   # ATR ~1.9% → good
    high_atr = _dec(_row(atr_14=10.0, **base), ctx)  # ATR ~9.5% → elevated
    for d in (low_atr, good_atr, high_atr):
        assert d["direction"] == SignalDirection.STRONG_LONG
    assert low_atr["direction_evidence"].risk_quality == pytest.approx(0.3)
    assert good_atr["direction_evidence"].risk_quality == pytest.approx(1.0)
    assert high_atr["direction_evidence"].risk_quality == pytest.approx(0.5)


def test_single_component_cannot_create_fake_signal():
    """A single maxed component (VWAP +15) nets to total 62.5 → NO_TRADE: a
    multifactor signal still needs confirmation (V1's 'multiple confirmations'
    rationale preserved — but now expressed through symmetric evidence)."""
    row = _row(distance_from_vwap=5.0)
    d = _dec(row, None)
    assert d["direction"] == SignalDirection.NO_TRADE
    assert d["direction_evidence"].net == pytest.approx(15.0, rel=1e-3)
    assert d["signal_score"].total == pytest.approx(62.5, rel=1e-3)


def test_no_nan_inf_leaks_from_corrupted_frame():
    """Random-walk frame with the final 12 bars corrupted by NaN / ±Inf combos:
    every decision must still be fully finite and valid."""
    df = _market_frame(bull_bars=80, bear_bars=80, neutral_bars=40, seed=5)
    for offset in range(12):
        r = df.iloc[-1].copy()
        corrupt = {k: r.get(k) for k in
                   ("rsi_14", "macd_histogram", "roc_5", "atr_14", "adx_14",
                    "vwap", "relative_volume", "distance_from_vwap", "close")}
        choice = offset % len(corrupt)
        key = list(corrupt)[choice]
        r[key] = float("inf") if offset % 2 else np.nan
        d = evaluate_row_signal(r, market_context=None)
        assert d is not None
        assert _finite_fields(d) == []
        assert d["direction"] in (
            SignalDirection.STRONG_LONG, SignalDirection.LONG,
            SignalDirection.WEAK_LONG, SignalDirection.NO_TRADE,
            SignalDirection.WEAK_SHORT, SignalDirection.SHORT,
            SignalDirection.STRONG_SHORT,
        )
        assert d["direction_evidence"].conflict is False or d["direction"] == SignalDirection.NO_TRADE


# ────────────────────────────────────────────────────────────────────────────
# No future-data leakage
# ────────────────────────────────────────────────────────────────────────────


def test_row_decision_equals_prefix_live_decision_no_future_leakage():
    """For every qualifying bar, evaluate_row_signal(row_i) == the live signal
    computed on the prefix ending at bar i. A bar's decision must never depend
    on data that occurs later (evaluate_signal on the FULL frame looks at the
    same row — the engine cannot see the future)."""
    df = _market_frame(bull_bars=120, bear_bars=120, neutral_bars=60, seed=21)
    checked = 0
    for i in range(55, len(df), 7):
        row = df.iloc[i]
        rd = evaluate_row_signal(row, market_context=None)
        prefix = df.iloc[: i + 1].copy()
        sd = evaluate_signal(prefix, "TEST", data_source="TEST",
                             data_age_seconds=60, data_status="LIVE",
                             market_context=None)
        assert sd is not None
        assert sd["direction"] == rd["direction"].value, f"bar {i}"
        assert sd["signal_score"]["total"] == pytest.approx(
            rd["signal_score"].total, rel=1e-6, abs=1e-6), f"bar {i}"
        checked += 1
    assert checked > 10


# ────────────────────────────────────────────────────────────────────────────
# API contract (additive, backward compatible)
# ────────────────────────────────────────────────────────────────────────────


def test_api_contract_shape_and_directional_evidence_type():
    row = _row(open=100.0, high=106.0, low=99.0, close=105.0, ema_9=110.0,
               ema_20=105.0, ema_50=100.0, atr_14=2.0, adx_14=30.0,
               relative_volume=2.5, vwap=100.0, rsi_14=60.0, macd=0.3,
               macd_signal=0.2, macd_histogram=0.5, roc_5=1.5,
               distance_from_vwap=5.0, prev_high=103.0, prev_low=97.0,
               opening_range_high=104.0, opening_range_low=96.0,
               bb_upper=110.0, bb_lower=90.0)
    d = _dec(row, _ctx("BULLISH", 1.0))
    assert set(d) == {"direction", "confidence", "signal_score", "setup",
                      "reasons", "risks", "direction_evidence"}
    assert isinstance(d["direction_evidence"], DirectionalEvidence)
    assert isinstance(d["signal_score"].total, float)
    assert d["setup"].entry > 0 and d["setup"].risk_reward_ratio >= 0

    # DirectionalEvidence serialises cleanly (pydantic round-trip).
    dumped = d["direction_evidence"].model_dump()
    rebuilt = DirectionalEvidence.model_validate(dumped)
    assert rebuilt == d["direction_evidence"]
    assert "conflict" in dumped and "net" in dumped

    # determine_direction(signal_score.total) reproduces the band direction
    # whenever no conflict / downgrade intervened.
    assert determine_direction(d["signal_score"].total) == d["direction"]


def test_evaluate_signal_exposes_direction_evidence():
    df = _market_frame(bull_bars=120, bear_bars=120, neutral_bars=60, seed=31)
    sig = evaluate_signal(df, "TEST", data_source="TEST",
                          data_age_seconds=60, data_status="LIVE",
                          market_context=None)
    assert sig is not None
    assert "direction_evidence" in sig
    assert isinstance(sig["direction_evidence"], DirectionalEvidence)
    assert sig["direction"] in (
        "STRONG_LONG", "LONG", "WEAK_LONG", "NO_TRADE",
        "WEAK_SHORT", "SHORT", "STRONG_SHORT",
    )
    # Consistency: a NO_TRADE decision carries zero confidence.
    if sig["direction"] == "NO_TRADE":
        assert sig["confidence"] == 0.0


def test_legacy_helpers_unchanged_and_disconnected_from_v2_path():
    """V1 scalar helpers stay available and keep their documented V1 behaviour
    (score_vwap ∈ [0,15]; score_market_context(None) == 5.0). They are no
    longer used by the engine's decision path — V2 uses the symmetric nets."""
    row = _row(distance_from_vwap=2.0, vwap=100.0, close=102.0)
    vwap_score, vw_reasons = score_vwap(row)
    assert 0.0 <= vwap_score <= 15.0
    assert len(vw_reasons) > 0
    mc_score, mc_reasons = score_market_context(row, None)
    assert mc_score == 5.0
    assert len(mc_reasons) > 0


# ────────────────────────────────────────────────────────────────────────────
# Directionality diagnostic (deterministic; records distribution)
# ────────────────────────────────────────────────────────────────────────────


def _market_frame(bull_bars: int, bear_bars: int, neutral_bars: int,
                  seed: int, drift_val: float = 0.0015,
                  noise: float = 0.0020) -> pd.DataFrame:
    """Deterministic mixed-regime 5-min frame: bullish drift, then bearish
    drift, then flat chop.

    Drift is kept realistic (<=0.15%/bar): sustained ±1% moves drive
    avg_gain/avg_loss to extremes so RSI goes NaN (pre-existing indicator
    quirk) and the engine conservatively gates such frames to NO_TRADE — that
    robustness is covered separately by test_no_nan_inf_leaks_*."""
    rng = np.random.default_rng(seed)
    n = bull_bars + bear_bars + neutral_bars
    drift = np.concatenate([
        np.full(bull_bars, drift_val),
        np.full(bear_bars, -drift_val),
        np.zeros(neutral_bars),
    ])
    rets = drift + rng.normal(0.0, noise, n)
    close = 100.0 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[100.0], close[:-1]]) * (1.0 + rng.normal(0, 0.001, n))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0, 0.002, n)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0, 0.002, n)))
    volume = rng.integers(10000, 80000, n).astype(float)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    df = pd.DataFrame({"timestamp": dates, "open": open_, "high": high,
                       "low": low, "close": close, "volume": volume})
    return calculate_all_indicators(df)


def test_v2_directionality_diagnostic():
    """Deterministic diagnostic over a mixed regime.

    ASSERTS: both direction families are achievable (equal opportunity — the
    V1 bug was that SHORT was effectively unreachable); NO_TRADE occurs in the
    flat regime; every bar's score is finite.

    RECORDS (printed, for the Phase 6 report): counts, classic-total
    distribution and mean |component| contributions per side. The numbers are
    NOT thresholds and are not asserted to balance."""
    df = _market_frame(bull_bars=150, bear_bars=150, neutral_bars=150, seed=9)
    counts = {d: 0 for d in ("LONG", "SHORT", "NO_TRADE")}
    scores = []
    comp_long = {k: 0.0 for k in DIRECTIONAL_WEIGHTS}
    comp_short = {k: 0.0 for k in DIRECTIONAL_WEIGHTS}
    n_long = 0
    n_short = 0

    for i in range(55, len(df)):
        d = evaluate_row_signal(df.iloc[i], market_context=None)
        assert d is not None and _finite_fields(d) == []
        label = d["direction"].value
        if label.endswith("LONG"):
            family = "LONG"
        elif label.endswith("SHORT"):
            family = "SHORT"
        else:
            family = "NO_TRADE"
        counts[family] += 1
        scores.append(d["signal_score"].total)
        ev = d["direction_evidence"]
        for key in DIRECTIONAL_WEIGHTS:
            net = db_ev_nets(d, key)
            if net > 0:
                comp_long[key] += net
                n_long += 1 if family == "LONG" else 0
            elif net < 0:
                comp_short[key] += -net
                n_short += 1 if family == "SHORT" else 0

    # Equal opportunity: both families must be reachable on this regime.
    assert counts["LONG"] > 0
    assert counts["SHORT"] > 0
    assert counts["NO_TRADE"] > 0  # flat regime yields NO_TRADE bars
    # Loose sanity: direction bands cover the full spectrum of the 0..100 scale.
    assert min(scores) >= 0.0 and max(scores) <= 100.0

    print(f"\n[V2 diagnostic] direction counts (seed=9): {counts}")
    print(f"[V2 diagnostic] classic total  min={min(scores):.2f} "
          f"median={float(np.median(scores)):.2f} mean={float(np.mean(scores)):.2f} "
          f"max={max(scores):.2f}")
    print(f"[V2 diagnostic] LONG bars={n_long}  SHORT bars={n_short}")
    print(f"[V2 diagnostic] mean long contribution per component: "
          f"{ {k: float(comp_long[k]) / max(n_long, 1) for k in DIRECTIONAL_WEIGHTS} }")
    print(f"[V2 diagnostic] mean short contribution per component: "
          f"{ {k: float(comp_short[k]) / max(n_short, 1) for k in DIRECTIONAL_WEIGHTS} }")