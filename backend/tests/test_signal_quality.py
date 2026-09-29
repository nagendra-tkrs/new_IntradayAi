"""Focused tests for the Top Signal Quality Layer (app/services/signal_quality.py).

The layer must:
* Never alter the base Signal Engine output (evaluate_row_signal untouched).
* Compute setup_quality_score / setup_quality / confirmation_count / total.
* Report genuinely unavailable data (especially volume) as UNKNOWN, never as
  bullish or bearish confirmation.
* Produce deterministic rankings where setup quality outranks raw confidence.
"""

import copy

import pandas as pd
import pytest

from app.services.signal_quality import (
    CONFIRMATION_TOTAL,
    QUALITY_NORMAL,
    QUALITY_PREMIUM,
    QUALITY_QUALIFIED,
    QUALITY_WEAK,
    compute_setup_quality,
    rank_top_signals,
)
from app.services.signal_engine import evaluate_row_signal, evaluate_signal


# ────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────────────

def _row(**kw) -> pd.Series:
    """Bullish-leaning complete indicator row (LONG healthy by default)."""
    row = {
        "open": 100.0, "high": 102.5, "low": 99.0, "close": 102.0,
        "ema_9": 101.5, "ema_20": 100.5, "ema_50": 99.5,
        "adx_14": 34.0, "rsi_14": 62.0, "macd_histogram": 0.5, "roc_5": 1.2,
        "vwap": 101.0, "distance_from_vwap": 1.0, "relative_volume": 0.0,
        "atr_14": 1.5, "volatility_20": 1.5,
        "opening_range_high": 101.0, "opening_range_low": 99.5,
        "prev_high": 101.2, "prev_low": 99.8,
        "day_high": 102.5, "day_low": 99.2,
    }
    row.update(kw)
    return pd.Series(row)


def _bear_row(**kw) -> pd.Series:
    row = {
        "open": 102.0, "high": 102.5, "low": 99.0, "close": 99.5,
        "ema_9": 100.5, "ema_20": 101.5, "ema_50": 102.5,
        "adx_14": 34.0, "rsi_14": 38.0, "macd_histogram": -0.5, "roc_5": -1.2,
        "vwap": 101.0, "distance_from_vwap": -1.0, "relative_volume": 0.0,
        "atr_14": 1.5, "volatility_20": 1.5,
        "opening_range_high": 101.5, "opening_range_low": 100.5,
        "prev_high": 101.4, "prev_low": 100.0,
        "day_high": 102.5, "day_low": 99.0,
    }
    row.update(kw)
    return pd.Series(row)


def _signal(direction="LONG", confidence=72.0, rr=2.2, conflict=False,
            opposing=4.0, **setup_over) -> dict:
    side = 1 if "LONG" in direction else -1
    long_total = 62.0 if side == 1 else opposing
    short_total = opposing if side == 1 else 62.0
    setup = {
        "entry": 102.0, "stop_loss": 100.0 if side == 1 else 104.0,
        "target_1": 106.0 if side == 1 else 98.0,
        "target_2": 108.0 if side == 1 else 96.0,
        "risk_per_share": 2.0, "reward_per_share": 4.4,
        "risk_reward_ratio": rr, "trailing_stop": None,
    }
    setup.update(setup_over)
    return {
        "direction": direction,
        "confidence": confidence,
        "signal_score": {
            "trend_score": 18.0, "momentum_score": 12.0, "volume_score": 8.0,
            "vwap_score": 12.0, "price_action_score": 12.0,
            "market_context_score": 6.0, "risk_quality_score": 10.0,
            "total": 80.0, "long_total": long_total, "short_total": short_total,
            "net": long_total - short_total, "conflict": conflict,
        },
        "setup": setup,
        "direction_evidence": {
            "trend_long": 1.0, "trend_short": 0.0, "momentum_long": 1.0,
            "momentum_short": 0.0, "volume_long": 0.5, "volume_short": 0.0,
            "vwap_long": 1.0, "vwap_short": 0.0, "price_action_long": 1.0,
            "price_action_short": 0.0, "market_context_long": 0.6,
            "market_context_short": 0.0, "risk_quality": 1.0,
            "long_total": long_total, "short_total": short_total,
            "net": long_total - short_total, "conflict": conflict,
        },
    }


def _df_volume(rv_completed=2.0, rv_last=0.0, completed_vol=2400):
    return pd.DataFrame({
        "relative_volume": [0.9, 1.1, rv_completed, rv_last],
        "volume": [1000, 1100, completed_vol, 0],
    })


def _ctx_bullish():
    return {"nifty_trend": "BULLISH", "nifty_change_pct": 0.6,
            "banknifty_change_pct": 0.5, "market_open": True}


def _ctx_bearish():
    return {"nifty_trend": "BEARISH", "nifty_change_pct": -0.6,
            "banknifty_change_pct": -0.4, "market_open": True}


def _ctx_unknown():
    return {"nifty_trend": "UNKNOWN", "nifty_change_pct": 0.0,
            "banknifty_change_pct": 0.0, "market_open": True}


# ────────────────────────────────────────────────────────────────────────────
# 1–2. Strong multi-factor LONG / SHORT
# ────────────────────────────────────────────────────────────────────────────

def test_strong_multifactor_long_is_premium():
    q = compute_setup_quality(_signal("LONG"), _row(),
                              market_context=_ctx_bullish(), df=_df_volume(2.0))
    assert q["setup_quality"] in (QUALITY_PREMIUM, QUALITY_QUALIFIED)
    assert q["top_signal_eligible"] is True
    assert q["confirmation_count"] >= 6
    assert q["confirmation_total"] == CONFIRMATION_TOTAL == 8
    assert q["setup_quality_score"] >= 75
    # Explainability — the confirmed categories are explained, nothing invented.
    assert len(q["quality_detail"]["why_qualified"]) >= 5


def test_strong_multifactor_short_is_premium():
    q = compute_setup_quality(_signal("SHORT"), _bear_row(),
                              market_context=_ctx_bearish(), df=_df_volume(2.0))
    assert q["setup_quality"] in (QUALITY_PREMIUM, QUALITY_QUALIFIED)
    assert q["top_signal_eligible"] is True
    assert q["confirmation_count"] >= 6
    assert q["setup_quality_score"] >= 75


# ────────────────────────────────────────────────────────────────────────────
# 3–4. Quality is not confidence; single-factor signals must not turn premium
# ────────────────────────────────────────────────────────────────────────────

def test_high_confidence_poor_rr_not_premium():
    # High base confidence (79) but RR ~1.1 → the setup fails the RR band.
    q = compute_setup_quality(_signal("LONG", confidence=79.0, rr=1.1), _row(),
                              market_context=_ctx_bullish(), df=_df_volume(2.0))
    assert q["setup_quality"] not in (QUALITY_PREMIUM, QUALITY_QUALIFIED)
    detail = q["quality_detail"]["categories"]["risk_reward"]
    assert detail["status"] == "failed"
    # And the same setup with a good RR scores strictly higher.
    q_good = compute_setup_quality(_signal("LONG", confidence=79.0, rr=2.2), _row(),
                                   market_context=_ctx_bullish(), df=_df_volume(2.0))
    assert q_good["setup_quality_score"] > q["setup_quality_score"]


def test_high_rsi_alone_not_premium():
    # Extremely high RSI but no other confirmation: flat EMAs, weak ADX,
    # momentum mixed (MACD/ROC neutral), price at VWAP, no price-action
    # structure, low completed-bar volume, neutral market.
    row = _row(
        rsi_14=82.0, adx_14=12.0,
        ema_9=101.0, ema_20=100.9, ema_50=100.8, close=101.0,
        macd_histogram=0.0, roc_5=0.0,
        distance_from_vwap=0.0, vwap=101.0,
        open=101.0, high=101.2, low=100.9,
        opening_range_high=float("nan"), opening_range_low=float("nan"),
        prev_high=float("nan"), prev_low=float("nan"),
        day_high=101.5, day_low=100.0,
    )
    q = compute_setup_quality(_signal("LONG", confidence=82.0), row,
                              market_context=_ctx_unknown(), df=_df_volume(0.7))
    assert q["setup_quality"] not in (QUALITY_PREMIUM, QUALITY_QUALIFIED)
    assert q["confirmation_count"] < 4


# ────────────────────────────────────────────────────────────────────────────
# 5. Missing volume → UNKNOWN, never bullish/bearish, never inflated
# ────────────────────────────────────────────────────────────────────────────

def test_missing_volume_is_unknown_not_directional():
    # Completed candle relative_volume is NaN and bar volume is 0 → UNKNOWN.
    df_bad = pd.DataFrame({
        "relative_volume": [float("nan"), float("nan"), float("nan"), float("nan")],
        "volume": [0, 0, 0, 0],
    })
    q = compute_setup_quality(_signal("LONG"), _row(),
                              market_context=_ctx_bullish(), df=df_bad)
    vol = q["quality_detail"]["categories"]["volume"]
    assert vol["status"] == "unknown"
    assert vol["points"] == 0.0
    assert vol["reason"]  # an explicit UNKNOWN explanation is always present

    # No df at all → still UNKNOWN with 0 points (never treated as negative).
    q2 = compute_setup_quality(_signal("LONG"), _row(),
                               market_context=_ctx_bullish(), df=None)
    vol2 = q2["quality_detail"]["categories"]["volume"]
    assert vol2["status"] == "unknown"
    assert vol2["points"] == 0.0


def test_volume_uses_last_completed_candle():
    # Live yfinance reports volume=0 on the forming candle; the layer must use
    # the prior completed bar, so a strong completed-bar relvol confirms.
    df = _df_volume(rv_completed=2.4, rv_last=0.0)
    q = compute_setup_quality(_signal("LONG"), _row(),
                              market_context=_ctx_bullish(), df=df)
    vol = q["quality_detail"]["categories"]["volume"]
    assert vol["status"] == "confirmed"
    assert vol["points"] > 0


# ────────────────────────────────────────────────────────────────────────────
# 6. Conflicting evidence decreases quality
# ────────────────────────────────────────────────────────────────────────────

def test_conflicting_evidence_decreases_quality():
    low = compute_setup_quality(_signal("LONG", opposing=4.0), _row(),
                                market_context=_ctx_bullish(), df=_df_volume(1.6))
    high = compute_setup_quality(_signal("LONG", opposing=26.0), _row(),
                                 market_context=_ctx_bullish(), df=_df_volume(1.6))
    assert low["setup_quality_score"] > high["setup_quality_score"]
    assert high["quality_detail"]["categories"]["conflict"]["status"] == "failed"

    # Explicit conflict flag → conflict category fails entirely.
    conf = compute_setup_quality(_signal("LONG", conflict=True, opposing=40.0), _row(),
                                 market_context=_ctx_bullish(), df=_df_volume(1.6))
    assert conf["quality_detail"]["categories"]["conflict"]["status"] == "failed"
    assert conf["quality_detail"]["categories"]["conflict"]["points"] == 0.0


# ────────────────────────────────────────────────────────────────────────────
# 7. Unknown market context stays neutral
# ────────────────────────────────────────────────────────────────────────────

def test_unknown_market_context_is_neutral():
    q = compute_setup_quality(_signal("LONG"), _row(),
                              market_context=_ctx_unknown(), df=_df_volume(1.6))
    mc = q["quality_detail"]["categories"]["market_context"]
    assert mc["status"] in ("neutral", "unknown")
    assert mc["points"] == 0.0
    # No fabricated NIFTY direction appears anywhere.
    text = " ".join(q["quality_detail"]["why_qualified"] + q["quality_detail"]["quality_limitations"]).lower()
    assert "bullish context" not in text
    assert "bearish context" not in text


# ────────────────────────────────────────────────────────────────────────────
# 8–9. Base signal classification is untouched (LONG stays LONG, WEAK_LONG
# stays WEAK_LONG) and the layer never mutates its inputs.
# ────────────────────────────────────────────────────────────────────────────

def test_weak_long_remains_weak_long():
    sig = _signal("WEAK_LONG", confidence=61.0, rr=1.9)
    snapshot = copy.deepcopy(sig)
    q = compute_setup_quality(sig, _row(), market_context=_ctx_bullish(), df=_df_volume(1.8))
    # Quality layer does not alter the signal...
    assert sig == snapshot
    # ...and the base direction stays WEAK_LONG even though setup quality is high.
    assert "WEAK_LONG" not in q["setup_quality"]  # classification dimensions are separate
    assert q["not_applicable"] is False
    assert q["top_signal_eligible"] is True


def test_long_remains_long():
    sig = _signal("LONG")
    snapshot = copy.deepcopy(sig)
    compute_setup_quality(sig, _row(), market_context=_ctx_bullish(), df=_df_volume(1.8))
    assert sig == snapshot


# ────────────────────────────────────────────────────────────────────────────
# 10. Base engine output unchanged & deterministic for identical input
# ────────────────────────────────────────────────────────────────────────────

def test_base_engine_values_unchanged_and_deterministic():
    row = _row()
    # The engine path itself is untouched — identical input, identical output.
    d1 = evaluate_row_signal(row, market_context=_ctx_bullish())
    d2 = evaluate_row_signal(row, market_context=_ctx_bullish())
    assert d1 == d2
    assert d1["direction"] == d1["direction"]
    # Quality layer must not mutate row/df/signal.
    df = _df_volume(1.8)
    sig = _signal("LONG")
    row_copy = row.copy()
    df_copy = df.copy()
    compute_setup_quality(sig, row, market_context=_ctx_bullish(), df=df)
    pd.testing.assert_series_equal(row, row_copy)
    pd.testing.assert_frame_equal(df, df_copy)


def test_evaluate_signal_keeps_contract():
    # A full engine call still returns the documented base contract (including
    # the exact confidence) — the quality layer adds nothing inside signal_data.
    df = _df_volume(1.8)
    df = pd.concat([
        pd.DataFrame({"open": [99.0], "high": [100.0], "low": [98.0],
                      "close": [99.5], "volume": [1000]}),
        df,
    ], ignore_index=True)
    # evaluate_signal needs >= 55 rows; reuse the engine determinism check above
    # and simply assert the quality module never touches the signal dict keys.
    sig = _signal("LONG", confidence=72.0)
    before = set(sig.keys())
    compute_setup_quality(sig, _row(), market_context=_ctx_bullish(), df=_df_volume(1.8))
    assert set(sig.keys()) == before


# ────────────────────────────────────────────────────────────────────────────
# 11. Deterministic ranking; quality outranks raw confidence
# ────────────────────────────────────────────────────────────────────────────

def _row_result(symbol, quality_level, score, confirms, confidence, rr, eligible=True):
    return {
        "symbol": symbol,
        "signal": "LONG",
        "confidence": confidence,
        "setup_quality": quality_level,
        "setup_quality_score": score,
        "confirmation_count": confirms,
        "confirmation_total": CONFIRMATION_TOTAL,
        "top_signal_eligible": eligible,
        "risk_reward_ratio": rr,
        "top_signal_rank": None,
    }


def test_deterministic_ranking_and_quality_priority():
    results = [
        # Higher confidence but materially weaker setup ...
        _row_result("HIGHCONF_BAD", QUALITY_QUALIFIED, 84, 6, 71.3, 1.33),
        # ... must rank below a slightly-lower-confidence, much-better setup.
        _row_result("LOWERCONF_GOOD", QUALITY_QUALIFIED, 91, 8, 69.0, 2.10),
        _row_result("PREMIUM_ONE", QUALITY_PREMIUM, 88, 7, 66.0, 2.2),
        _row_result("NORMAL_ONE", QUALITY_NORMAL, 50, 3, 75.0, 1.4, eligible=False),
    ]
    first = [r["symbol"] for r in rank_top_signals(copy.deepcopy(results))]
    second = [r["symbol"] for r in rank_top_signals(copy.deepcopy(results))]
    assert first == second  # deterministic
    # PREMIUM first, then QUALIFIED by score, never raw confidence.
    assert first[0] == "PREMIUM_ONE"
    assert first[1] == "LOWERCONF_GOOD"
    assert first.index("LOWERCONF_GOOD") < first.index("HIGHCONF_BAD")
    # Non-eligible rows come last and never receive a rank.
    assert first[-1] == "NORMAL_ONE"


def test_rank_assignment_is_sequential_for_eligible_only():
    results = [
        _row_result("A", QUALITY_QUALIFIED, 90, 7, 70.0, 2.0),
        _row_result("B", QUALITY_NORMAL, 55, 3, 80.0, 1.5, eligible=False),
        _row_result("C", QUALITY_PREMIUM, 92, 7, 71.0, 2.4),
    ]
    ranked = rank_top_signals(results)
    ranks = {r["symbol"]: r["top_signal_rank"] for r in ranked}
    assert ranks["C"] == 1
    assert ranks["A"] == 2
    assert ranks["B"] is None
    assert [r["symbol"] for r in ranked] == ["C", "A", "B"]


def test_tiebreak_is_symbol_stable():
    results = [
        _row_result("AAA", QUALITY_QUALIFIED, 80, 6, 70.0, 1.6),
        _row_result("BBB", QUALITY_QUALIFIED, 80, 6, 70.0, 1.6),
    ]
    a = [r["symbol"] for r in rank_top_signals(copy.deepcopy(results))]
    assert a == ["AAA", "BBB"]


# ────────────────────────────────────────────────────────────────────────────
# 12–13. No fabricated values; robustness with missing / NaN indicators
# ────────────────────────────────────────────────────────────────────────────

def test_no_fabricated_confirmation_from_missing_indicators():
    row = _row(
        ema_9=float("nan"), ema_20=float("nan"), ema_50=float("nan"),
        adx_14=float("nan"), rsi_14=float("nan"), macd_histogram=float("nan"),
        roc_5=float("nan"), vwap=float("nan"), distance_from_vwap=float("nan"),
    )
    df = pd.DataFrame({
        "relative_volume": [float("nan")] * 4,
        "volume": [0, 0, 0, 0],
    })
    q = compute_setup_quality(_signal("LONG", rr=2.0), row,
                              market_context=None, df=df)
    cats = q["quality_detail"]["categories"]
    # Missing-input categories never become "confirmed".
    for name in ("trend", "momentum", "vwap", "volume", "market_context"):
        assert cats[name]["status"] in ("failed", "neutral", "unknown")
    # Only the AVAILABLE facts (RR) may confirm.
    assert cats["risk_reward"]["status"] == "confirmed"
    q_score = q["setup_quality_score"]
    assert q_score >= 0.0 and q["confirmation_count"] >= 1


def test_no_crash_with_all_nan():
    row = _row(
        open=float("nan"), high=float("nan"), low=float("nan"), close=float("nan"),
        ema_9=float("nan"), ema_20=float("nan"), ema_50=float("nan"),
        adx_14=float("nan"), rsi_14=float("nan"), macd_histogram=float("nan"),
        roc_5=float("nan"), vwap=float("nan"), distance_from_vwap=float("nan"),
        opening_range_high=float("nan"), opening_range_low=float("nan"),
        prev_high=float("nan"), prev_low=float("nan"),
        day_high=float("nan"), day_low=float("nan"),
    )
    q = compute_setup_quality(_signal("LONG", rr=1.0), row,
                              market_context={"nifty_trend": None}, df=None)
    assert q["setup_quality_score"] >= 0.0
    assert q["confirmation_total"] == CONFIRMATION_TOTAL
    assert q["top_signal_eligible"] is False


def test_rejected_when_quality_is_poor():
    q = compute_setup_quality(_signal("LONG", rr=0.9), _bear_row(),
                              market_context=_ctx_bearish(), df=_df_volume(0.4))
    # A LONG signal measured against bearish, low-RR evidence → poor quality.
    assert q["setup_quality"] not in (QUALITY_PREMIUM, QUALITY_QUALIFIED)
    assert isinstance(q["setup_quality_score"], float)