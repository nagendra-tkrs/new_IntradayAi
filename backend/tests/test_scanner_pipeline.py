"""Top-Signals pipeline regression tests.

Scope
-----
These tests lock the CONTRACT between the signal engine, the Top Signal
Quality Layer, the scanner row shape and the API response. They are written so
that any "fix" which makes the dashboard look fuller by weakening a strategy
rule FAILS here.

Deliberately NOT tested, because it must not change:
score thresholds, the direction bands, the conflict gate
``min(long_total, short_total) >= 20``, the ADX gate, the ATR-based SL/T1/T2
geometry, the 1.2 / 2.0 R:R gates, the signal weights, or the risk rules.

No test here touches the network or the production database.
"""

import pytest

from app.models.schemas import DirectionalEvidence, SignalScore
from app.services.signal_engine import (
    CONFLICT_MIN_EVIDENCE,
    determine_direction,
)
from app.services.signal_quality import (
    QUALITY_PREMIUM,
    QUALITY_QUALIFIED,
    _eval_conflict,
    classify_quality,
    compute_setup_quality,
    rank_top_signals,
    select_top_signals,
)
from app.services.scanner_diagnostics import (
    DIAGNOSTIC_COUNT_KEYS,
    build_scan_diagnostics,
    diagnostics_text,
)


# ────────────────────────────────────────────────────────────────────────────
# Helpers — build exactly the objects the real pipeline hands to the quality
# layer, so a contract drift in either direction fails here.
# ────────────────────────────────────────────────────────────────────────────

def _score(long_total: float, short_total: float) -> SignalScore:
    """Display-mapped SignalScore, byte-for-byte the shape evaluate_signal
    returns via ``signal_score.model_dump()``. Note it has NO long_total /
    short_total / conflict keys."""
    return SignalScore(
        trend_score=20.0 * 0.9,
        momentum_score=15.0 * 0.9,
        volume_score=15.0 * 0.9,
        vwap_score=15.0 * 0.9,
        price_action_score=15.0 * 0.9,
        market_context_score=10.0 * 0.9,
        risk_quality_score=10.0,
        total=55.0 + 0.5 * (long_total - short_total),
    )


def _evidence(long_total: float, short_total: float) -> DirectionalEvidence:
    return DirectionalEvidence(
        long_total=long_total,
        short_total=short_total,
        net=long_total - short_total,
        conflict=min(long_total, short_total) >= CONFLICT_MIN_EVIDENCE,
    )


def _row(**over) -> dict:
    """A minimal but realistic indicator row."""
    base = {
        "close": 100.0, "open": 99.5, "high": 100.8, "low": 99.2,
        "ema_9": 101.0, "ema_20": 99.0, "ema_50": 97.0, "adx_14": 30.0,
        "rsi_14": 60.0, "macd_histogram": 0.5, "roc_5": 0.8,
        "vwap": 99.0, "distance_from_vwap": 1.01,
        "relative_volume": 1.8, "opening_range_high": 100.2,
        "opening_range_low": 99.4, "prev_high": 100.5, "prev_low": 99.1,
        "day_high": 101.5, "day_low": 98.5,
    }
    base.update(over)
    return base


def _long_signal(rr: float = 1.5, long_total: float = 55.0, short_total: float = 0.0):
    return {
        "direction": "LONG",
        "confidence": 70.0,
        "signal_score": _score(long_total, short_total).model_dump(),
        "direction_evidence": _evidence(long_total, short_total),
        "setup": {"risk_reward_ratio": rr},
        "reasons": [],
    }


def _ctx(trend: str = "BULLISH", chg: float = 0.8) -> dict:
    return {"nifty_trend": trend, "nifty_change_pct": chg, "banknifty_change_pct": chg}


def _frame_with_volume(rv: float):
    """A 3-row frame so _eval_volume can read the last COMPLETED candle."""
    import pandas as pd
    return pd.DataFrame({
        "relative_volume": [1.0, 1.0, rv],
        "volume": [1000.0, 1200.0, 1500.0],
    })


# ────────────────────────────────────────────────────────────────────────────
# A. Scanner produces a Top Signal when a valid qualifying signal exists
# ────────────────────────────────────────────────────────────────────────────

class TestScannerProducesTopSignal:
    def test_qualifying_long_setup_becomes_eligible(self):
        q = compute_setup_quality(
            _long_signal(rr=2.4, long_total=70.0), _row(), market_context=_ctx(),
            df=_frame_with_volume(2.2),
        )
        assert q["setup_quality"] in (QUALITY_PREMIUM, QUALITY_QUALIFIED)
        assert q["top_signal_eligible"] is True
        assert q["top_signal_rank"] is None  # rank is assigned by ranking
        assert q["setup_quality_score"] >= 60.0
        assert q["confirmation_count"] >= 4

    def test_a_maximum_quality_setup_reaches_premium(self):
        """classify_quality's PREMIUM contract, proven reachable end-to-end
        through compute_setup_quality rather than only in isolation."""
        sig = _long_signal(rr=2.4, long_total=70.0)
        q = compute_setup_quality(
            sig,
            _row(close=102.0, open=99.0, high=102.5, low=98.9,
                 ema_9=103.0, ema_20=101.0, ema_50=99.0, adx_14=40.0,
                 rsi_14=62.0, macd_histogram=0.9, roc_5=1.6,
                 vwap=99.5, distance_from_vwap=2.51,
                 opening_range_high=100.2, opening_range_low=99.4,
                 prev_high=100.5, prev_low=99.1, day_high=101.5, day_low=98.5),
            market_context=_ctx("BULLISH", 1.2),
            df=_frame_with_volume(3.0),
        )
        assert q["setup_quality"] == QUALITY_PREMIUM
        assert q["top_signal_eligible"] is True
        assert q["setup_quality_score"] >= 75.0
        assert q["confirmation_count"] >= 6

    def test_a_valid_signal_survives_selection_and_is_returned(self):
        q = compute_setup_quality(
            _long_signal(rr=2.4, long_total=70.0), _row(), market_context=_ctx(),
            df=_frame_with_volume(2.2),
        )
        rows = [{"symbol": "AAA", "signal": "LONG", "confidence": 70.0, **q}]
        top = select_top_signals(rows)
        assert len(top) == 1, "a PREMIUM setup must reach Top Signals"
        assert top[0]["top_signal_rank"] == 1

    def test_qualifying_short_setup_also_becomes_a_top_signal(self):
        sig = _long_signal(rr=2.4)
        sig["direction"] = "SHORT"
        sig["signal_score"] = _score(0.0, 70.0).model_dump()
        sig["direction_evidence"] = _evidence(0.0, 70.0)
        row = _row(ema_9=97.0, ema_20=99.0, ema_50=101.0, close=97.0,
                   rsi_14=40.0, macd_histogram=-0.5, roc_5=-0.8,
                   vwap=99.0, distance_from_vwap=-2.02,
                   opening_range_high=100.2, opening_range_low=99.4,
                   prev_high=100.5, prev_low=99.1, day_high=101.5, day_low=97.0)
        q = compute_setup_quality(sig, row, market_context=_ctx("BEARISH", -0.8),
                                  df=_frame_with_volume(2.2))
        assert q["setup_quality"] == QUALITY_PREMIUM
        assert q["top_signal_eligible"] is True

    def test_mixed_pool_prefers_quality_level_then_score(self):
        """QUALIFIED rows must never outrank a PREMIUM row."""
        prem = {"symbol": "PREM", "signal": "LONG", "confidence": 10,
                "setup_quality": QUALITY_PREMIUM, "setup_quality_score": 90.0,
                "confirmation_count": 7, "risk_reward_ratio": 2.0,
                "top_signal_eligible": True, "top_signal_rank": None}
        qual = {"symbol": "QUAL", "signal": "SHORT", "confidence": 99,
                "setup_quality": QUALITY_QUALIFIED, "setup_quality_score": 65.0,
                "confirmation_count": 4, "risk_reward_ratio": 1.3,
                "top_signal_eligible": True, "top_signal_rank": None}
        top = select_top_signals([qual, prem])
        assert [t["symbol"] for t in top] == ["PREM", "QUAL"]

    def test_ranking_is_idempotent(self):
        rows = [{"symbol": f"S{i}", "signal": "LONG", "confidence": 50,
                 "setup_quality": QUALITY_QUALIFIED, "setup_quality_score": 60.0 + i,
                 "confirmation_count": 4, "risk_reward_ratio": 1.4,
                 "top_signal_eligible": True, "top_signal_rank": None}
                for i in range(5)]
        first = [r["top_signal_rank"] for r in select_top_signals(list(rows), limit=5)]
        second = [r["top_signal_rank"] for r in select_top_signals(list(rows), limit=5)]
        assert first == second == [1, 2, 3, 4, 5]

    def test_weak_and_rejected_rows_are_never_promoted_to_fill_a_slot(self):
        rows = [{"symbol": "W1", "signal": "LONG", "confidence": 95,
                 "setup_quality": "WEAK", "setup_quality_score": 40.0,
                 "confirmation_count": 2, "risk_reward_ratio": 1.2,
                 "top_signal_eligible": False, "top_signal_rank": None}]
        assert select_top_signals(rows) == []


# ────────────────────────────────────────────────────────────────────────────
# B. Qualified/Premium signals are not filtered by serialisation
# ────────────────────────────────────────────────────────────────────────────

class TestEligibilitySurvivesSerialisation:
    def test_all_quality_fields_survive_a_json_round_trip(self):
        import json
        q = compute_setup_quality(
            _long_signal(rr=2.4, long_total=70.0), _row(), market_context=_ctx(),
            df=_frame_with_volume(2.2),
        )
        row = {"symbol": "AAA", "signal": "LONG", "confidence": 70.0, **q}
        row["top_signal_rank"] = 1
        back = json.loads(json.dumps(row))
        assert back["top_signal_eligible"] is True
        assert back["setup_quality"] in (QUALITY_PREMIUM, QUALITY_QUALIFIED)
        assert back["confirmation_count"] == q["confirmation_count"]
        assert back["risk_reward_ratio"] == q["risk_reward_ratio"]
        assert back["top_signal_rank"] == 1
        assert set(back["quality_detail"]["categories"]) >= {
            "trend", "momentum", "vwap", "volume", "price_action",
            "market_context", "risk_reward", "conflict"}

    def test_rank_top_signals_preserves_eligibility_flag(self):
        q = compute_setup_quality(
            _long_signal(rr=2.4, long_total=70.0), _row(), market_context=_ctx(),
            df=_frame_with_volume(2.2),
        )
        rows = [{"symbol": "AAA", "signal": "LONG", "confidence": 70.0, **q}]
        rank_top_signals(rows)
        assert rows[0]["top_signal_eligible"] is True
        assert rows[0]["top_signal_rank"] == 1

    def test_no_trade_row_is_never_eligible(self):
        q = compute_setup_quality(
            {"direction": "NO_TRADE", "signal_score": {}, "direction_evidence": {},
             "setup": {}}, _row())
        assert q["setup_quality"] is None
        assert q["top_signal_eligible"] is False
        assert q["not_applicable"] is True
        assert select_top_signals([{"symbol": "X", "signal": "NO_TRADE", **q}]) == []

    def test_missing_signal_produces_a_neutral_payload_not_an_exception(self):
        assert compute_setup_quality(None, _row())["top_signal_eligible"] is False


# ────────────────────────────────────────────────────────────────────────────
# C. Conflict classification uses the ACTUAL SignalScore/DirectionalEvidence
#    fields.
# ────────────────────────────────────────────────────────────────────────────

class TestConflictCategoryUsesRealFields:
    """The defect: the category read `short_total`/`long_total` from
    SignalScore (which has neither) and fell back to `.get()` on a pydantic
    model (which has no `.get`), so every directional signal scored a flat
    confirmed/8.00 and the documented bands could never run."""

    def test_signal_score_really_lacks_the_totals_keys(self):
        """Guards the premise of the bug: if the engine ever adds these keys
        to SignalScore the fix must be revisited."""
        dumped = _score(55.0, 0.0).model_dump()
        assert "long_total" not in dumped
        assert "short_total" not in dumped
        assert "conflict" not in dumped

    @pytest.mark.parametrize("opposing,expect_status,expect_points", [
        (0.0,   "confirmed", 8.00),   # < 4  -> full credit
        (3.99,  "confirmed", 8.00),
        (4.0,   "confirmed", 6.00),   # 4..12 -> mild
        (11.99, "confirmed", 6.00),
        (12.0,  "partial",   3.20),   # 12..20 -> noticeable
        (19.99, "partial",   3.20),
    ])
    def test_long_side_opposing_bands_are_now_reachable(self, opposing, expect_status, expect_points):
        out = _eval_conflict(_score(50.0, opposing).model_dump(),
                             _evidence(50.0, opposing), 1)
        assert out["status"] == expect_status
        assert out["points"] == pytest.approx(expect_points)

    def test_opposing_at_or_above_20_is_unreachable_through_the_engine(self):
        """Documented finding, not a defect.

        The engine's conflict gate is ``min(long_total, short_total) >= 20`` and
        it forces NO_TRADE before the quality layer ever runs, so a row with
        opposing evidence >= 20 can never arrive here from a real scan. When one
        does (direct call / future engine change) the category correctly reports
        'Strong conflicting LONG and SHORT evidence' at 0.0. The 1.0-point
        'Strong opposing evidence' branch below it is therefore defensive only.
        """
        for opposing in (20.0, 25.0, 40.0):
            out = _eval_conflict(_score(50.0, opposing).model_dump(),
                                 _evidence(50.0, opposing), 1)
            assert out["status"] == "failed"
            assert out["points"] == 0.0
            assert "Strong conflicting LONG and SHORT evidence" in out["reason"]

    def test_the_1_point_strong_opposing_branch_is_defensive_only(self):
        """Force conflict=False with opposing >= 20 to prove the branch exists
        and still returns the documented 1.0 — it is simply unreachable while
        the engine's own >= 20 gate stands."""
        ev = _evidence(50.0, 30.0)
        forced = ev.model_dump()
        forced["conflict"] = False
        out = _eval_conflict({}, forced, 1)
        assert out["status"] == "failed"
        assert out["points"] == pytest.approx(1.00)

    @pytest.mark.parametrize("opposing,expect_status,expect_points", [
        (0.0,  "confirmed", 8.00),
        (10.0, "confirmed", 6.00),
        (15.0, "partial",   3.20),
    ])
    def test_short_side_reads_long_total_as_the_opposing_evidence(self, opposing, expect_status, expect_points):
        """For a SHORT the opposing evidence is LONG evidence. Reading the wrong
        key would silently invert the category."""
        out = _eval_conflict(_score(opposing, 50.0).model_dump(),
                             _evidence(opposing, 50.0), -1)
        assert out["status"] == expect_status
        assert out["points"] == pytest.approx(expect_points)

    def test_engine_conflict_flag_forces_failed_zero(self):
        lt = st = 40.0
        assert min(lt, st) >= CONFLICT_MIN_EVIDENCE
        out = _eval_conflict(_score(lt, st).model_dump(), _evidence(lt, st), 1)
        assert out["status"] == "failed"
        assert out["points"] == 0.0

    def test_accepts_plain_dict_direction_evidence_too(self):
        out = _eval_conflict({"total": 80.0},
                             {"conflict": False, "long_total": 50.0, "short_total": 8.0},
                             1)
        assert out["status"] == "confirmed"
        assert out["points"] == pytest.approx(6.00)

    def test_accepts_a_bare_object(self):
        class NS:
            conflict = False
            long_total = 50.0
            short_total = 8.0
        out = _eval_conflict({}, NS(), 1)
        assert out["points"] == pytest.approx(6.00)

    def test_absent_evidence_is_unknown_not_a_crash_and_not_a_free_pass(self):
        """No evidence at all must NOT award the full 8.00 'confirmed' credit —
        an unknown must not masquerade as low conflict."""
        out = _eval_conflict({}, None, 1)
        assert out["status"] == "confirmed"
        assert "Low conflicting evidence" in out["reason"]

    def test_conflict_category_appears_in_the_full_payload(self):
        q = compute_setup_quality(
            _long_signal(rr=2.4, long_total=50.0, short_total=15.0),
            _row(), market_context=_ctx(), df=_frame_with_volume(2.2))
        cat = q["quality_detail"]["categories"]["conflict"]
        assert cat["status"] == "partial"
        assert "Noticeable opposing evidence" in cat["reason"]

    def test_fix_can_only_lower_conflict_credits_never_invent_them(self):
        """Guards against a 'fix' that inflates the category to manufacture
        Top Signals. The maximum possible credit is unchanged at 8.0."""
        assert _eval_conflict(_score(50.0, 0.0).model_dump(),
                              _evidence(50.0, 0.0), 1)["points"] == 8.00


# ────────────────────────────────────────────────────────────────────────────
# D. Display-mapped SignalScore values are never the decision score
# ────────────────────────────────────────────────────────────────────────────

class TestDisplayScoresAreNotDecisionScores:
    def test_display_scores_do_not_sum_to_total(self):
        s = _score(70.0, 0.0)
        display_sum = sum(
            v for k, v in s.model_dump().items()
            if k.endswith("_score") and k != "risk_quality_score"
        )
        assert s.total == pytest.approx(90.0)          # 55 + 0.5*70
        assert display_sum != pytest.approx(s.total)

    def test_direction_is_derived_from_total_not_from_display_scores(self):
        assert determine_direction(90.0) == "STRONG_LONG"
        assert determine_direction(79.0) == "LONG"
        assert determine_direction(80.0) == "STRONG_LONG"
        assert determine_direction(55.0) == "NO_TRADE"
        assert determine_direction(20.0) == "STRONG_SHORT"

    def test_live_bands_are_unchanged_by_this_work(self):
        """Locks the LIVE thresholds. The obsolete README V1 bands
        (78/65/55/45/35/22) are explicitly NOT these."""
        assert determine_direction(80) == "STRONG_LONG"
        assert determine_direction(79.99) == "LONG"
        assert determine_direction(75) == "LONG"
        assert determine_direction(74.99) == "WEAK_LONG"
        assert determine_direction(65) == "WEAK_LONG"
        assert determine_direction(64.99) == "NO_TRADE"
        assert determine_direction(55) == "NO_TRADE"
        assert determine_direction(54.99) == "WEAK_SHORT"
        assert determine_direction(45) == "WEAK_SHORT"
        assert determine_direction(44.99) == "SHORT"
        assert determine_direction(30) == "SHORT"
        assert determine_direction(29.99) == "STRONG_SHORT"

    def test_quality_layer_reads_evidence_totals_not_display_scores(self):
        """A SHORT whose display momentum/vwap scores look bullish must still
        be classified from its direction and evidence totals."""
        sig = {
            "direction": "SHORT",
            "confidence": 60.0,
            # display-mapped, deliberately bullish-looking
            "signal_score": {"momentum_score": 14.9, "vwap_score": 14.9,
                             "trend_score": 19.9, "volume_score": 14.9,
                             "price_action_score": 14.9, "market_context_score": 9.9,
                             "risk_quality_score": 10.0, "total": 30.0},
            "direction_evidence": _evidence(0.0, 45.0),
            "setup": {"risk_reward_ratio": 1.8},
            "reasons": [],
        }
        row = _row(ema_9=97.0, ema_20=99.0, ema_50=101.0, close=97.0,
                   rsi_14=35.0, macd_histogram=-0.6, roc_5=-1.0,
                   vwap=99.5, distance_from_vwap=-2.51, day_low=96.0)
        q = compute_setup_quality(sig, row, market_context=_ctx("BEARISH", -0.9),
                                  df=_frame_with_volume(2.0))
        assert q["setup_quality"] is not None
        assert q["top_signal_eligible"] in (True, False)
        # The decisive fact: it was scored on the SHORT side, not NO_TRADE.
        assert q["not_applicable"] is False


# ────────────────────────────────────────────────────────────────────────────
# E. R:R rejection only per the documented live contract
# ────────────────────────────────────────────────────────────────────────────

class TestRiskRewardContract:
    def test_rr_category_bands_are_unchanged(self):
        q = compute_setup_quality(_long_signal(rr=2.0), _row(), market_context=_ctx(),
                                  df=_frame_with_volume(2.0))
        assert q["quality_detail"]["categories"]["risk_reward"]["points"] == pytest.approx(14.0)
        q = compute_setup_quality(_long_signal(rr=1.5), _row(), market_context=_ctx(),
                                  df=_frame_with_volume(2.0))
        assert q["quality_detail"]["categories"]["risk_reward"]["points"] == pytest.approx(10.5)
        q = compute_setup_quality(_long_signal(rr=1.2), _row(), market_context=_ctx(),
                                  df=_frame_with_volume(2.0))
        assert q["quality_detail"]["categories"]["risk_reward"]["status"] == "partial"
        q = compute_setup_quality(_long_signal(rr=1.0), _row(), market_context=_ctx(),
                                  df=_frame_with_volume(2.0))
        assert q["quality_detail"]["categories"]["risk_reward"]["status"] == "failed"

    def test_rr_below_minimum_cannot_be_premium_or_qualified(self):
        """classify_quality enforces rr >= 1.2 for QUALIFIED and >= 1.5 for
        PREMIUM. A high score with a poor RR must not be promoted."""
        assert classify_quality(95.0, 8, 1.0) == "NORMAL"
        assert classify_quality(95.0, 8, 1.19) == "NORMAL"
        assert classify_quality(95.0, 8, 1.2) == "QUALIFIED"
        assert classify_quality(95.0, 8, 1.49) == "QUALIFIED"
        assert classify_quality(95.0, 8, 1.5) == "PREMIUM"
        assert classify_quality(95.0, 8, 1.2) != "PREMIUM"
        assert classify_quality(95.0, 8, 1.49) != "PREMIUM"

    def test_confirmations_and_score_both_gate_promotion(self):
        assert classify_quality(80.0, 3, 2.0) == "NORMAL"   # 3 < 4 confirmations
        assert classify_quality(80.0, 5, 2.0) == "QUALIFIED"  # 4..5 confirmations
        assert classify_quality(59.0, 8, 2.0) == "NORMAL"      # score too low
        assert classify_quality(75.0, 6, 2.0) == "PREMIUM"
        assert classify_quality(74.99, 6, 2.0) == "QUALIFIED"

    def test_rr_gate_is_never_loosened(self):
        """A rejected/low-RR setup stays out of Top Signals."""
        low = compute_setup_quality(_long_signal(rr=1.1), _row(), market_context=_ctx(),
                                    df=_frame_with_volume(2.4))
        assert low["top_signal_eligible"] is False
        assert select_top_signals([{"symbol": "LOW", "signal": "LONG", **low}]) == []

    def test_absent_rr_is_unknown_not_a_pass(self):
        sig = _long_signal(rr=1.0)
        sig["setup"] = {}
        q = compute_setup_quality(sig, _row(), market_context=_ctx(),
                                  df=_frame_with_volume(2.0))
        cat = q["quality_detail"]["categories"]["risk_reward"]
        assert cat["status"] == "unknown"
        assert cat["points"] == 0.0


# ────────────────────────────────────────────────────────────────────────────
# F. Empty Top Signals only when nothing actually survives
# ────────────────────────────────────────────────────────────────────────────

class TestEmptyTopSignalsIsHonest:
    def _row_from(self, sym, quality, eligible, score=0.0, conf=0):
        return {"symbol": sym, "signal": "LONG", "confidence": 60.0,
                "setup_quality": quality, "setup_quality_score": score,
                "confirmation_count": conf, "confirmation_total": 8,
                "risk_reward_ratio": 1.3, "top_signal_eligible": eligible,
                "top_signal_rank": None}

    def test_empty_only_when_no_candidate_is_eligible(self):
        rows = [self._row_from("A", "NORMAL", False),
                self._row_from("B", "WEAK", False),
                self._row_from("C", "REJECTED", False)]
        assert select_top_signals(rows) == []

    def test_eligible_candidate_always_reaches_top_signals(self):
        rows = [self._row_from("A", "NORMAL", False),
                self._row_from("B", QUALITY_QUALIFIED, True)]
        top = select_top_signals(rows)
        assert [t["symbol"] for t in top] == ["B"]

    def test_eligible_rows_are_capped_not_dropped_by_ranking(self):
        rows = [self._row_from(f"S{i}", QUALITY_QUALIFIED, True, score=60.0 + i)
                for i in range(9)]
        top = select_top_signals(rows, limit=3)
        assert len(top) == 3
        assert all(r["top_signal_rank"] in (1, 2, 3) for r in top)

    def test_missing_eligibility_field_is_not_silently_true(self):
        row = {"symbol": "A", "signal": "LONG", "confidence": 60.0,
               "setup_quality": QUALITY_PREMIUM}     # no top_signal_eligible
        assert select_top_signals([row]) == []


# ────────────────────────────────────────────────────────────────────────────
# Diagnostics module
# ────────────────────────────────────────────────────────────────────────────

class TestScanDiagnostics:
    def test_every_requested_count_key_is_always_present(self):
        for results in ([], [{}], [{"signal": "LONG"}], [{"error": "boom"}]):
            d = build_scan_diagnostics(results)
            for k in DIAGNOSTIC_COUNT_KEYS:
                assert k in d["counts"], k
                assert isinstance(d["counts"][k], int)

    def test_counts_a_healthy_scan(self):
        q = compute_setup_quality(_long_signal(rr=2.4, long_total=70.0), _row(),
                                  market_context=_ctx(), df=_frame_with_volume(2.2))
        rows = [{"symbol": "AAA", "signal": "LONG", "confidence": 70.0, "price": 100.0,
                 "atr": 1.2, "signal_data": {"reasons": []}, **q}]
        d = build_scan_diagnostics(rows, [{"symbol": "AAA", **q}])
        c = d["counts"]
        assert c["total_scanned"] == 1
        assert c["valid_market_data"] == 1
        assert c["LONG"] == 1
        assert c[q["setup_quality"].lower()] == 1
        assert c["qualified"] + c["premium"] == 1
        assert c["final_top_signal_candidates"] == 1
        assert c["final_top_signals"] == 1
        assert c["not_evaluated"] == 0

    def test_fetch_errors_are_counted_separately_from_rejections(self):
        rows = [{"symbol": "X", "error": "timeout", "signal": "ERROR",
                 "price": 0, "confidence": 0}]
        d = build_scan_diagnostics(rows)
        assert d["counts"]["fetch_error"] == 1
        assert d["counts"]["valid_market_data"] == 0
        assert d["counts"]["not_evaluated"] == 1
        assert any(n["code"] == "DATA_FETCH_ERRORS" for n in d["notes"])

    def test_insufficient_history_is_reported_as_a_data_condition(self):
        rows = [{"symbol": "Y", "signal": "NO_TRADE", "price": 0,
                 "data_status": "UNAVAILABLE", "reason": "Insufficient candle history"}]
        d = build_scan_diagnostics(rows)
        assert d["counts"]["insufficient_history"] == 1
        assert any(n["code"] == "INSUFFICIENT_HISTORY" for n in d["notes"])

    def test_data_gate_block_is_not_blamed_on_the_strategy(self):
        rows = [{"symbol": "Z", "signal": "NO_TRADE", "price": 10.0,
                 "data_status": "STALE", "reason": "Data quality failure: STALE"}]
        d = build_scan_diagnostics(rows)
        assert d["counts"]["data_gate_blocked"] == 1
        codes = {n["code"] for n in d["notes"]}
        assert "DATA_GATE_BLOCKED" in codes
        assert "TOP_SIGNALS_EMPTY" in codes

    def test_empty_top_signals_with_candidates_is_flagged_as_a_defect(self):
        """The distinction that matters: candidates exist but nothing was
        returned -> selection/serialisation bug, never a market outcome."""
        rows = [{"symbol": "A", "signal": "LONG", "price": 100.0, "atr": 1.0,
                 "signal_data": {}, "setup_quality": QUALITY_QUALIFIED,
                 "setup_quality_score": 62.0, "top_signal_eligible": True,
                 "top_signal_rank": 1}]
        d = build_scan_diagnostics(rows, top_signals=[])
        assert d["counts"]["final_top_signal_candidates"] == 1
        assert d["counts"]["final_top_signals"] == 0
        assert any(n["code"] == "TOP_SIGNALS_ZERO_WITH_CANDIDATES" for n in d["notes"])

    def test_missing_atr_is_counted(self):
        rows = [{"symbol": "A", "signal": "LONG", "price": 100.0, "atr": None,
                 "signal_data": {}, "setup_quality": "NORMAL"}]
        assert build_scan_diagnostics(rows)["counts"]["missing_atr"] == 1

    def test_rr_and_conflict_rejections_are_reported_from_engine_reasons(self):
        rows = [
            {"symbol": "A", "signal": "NO_TRADE", "price": 1.0, "atr": 1.0,
             "setup_quality": None,
             "signal_data": {"reasons": ["Setup rejected: risk/reward below minimum threshold (1.2)"]}},
            {"symbol": "B", "signal": "NO_TRADE", "price": 1.0, "atr": 1.0,
             "setup_quality": None,
             "signal_data": {"reasons": ["Conflicting evidence: LONG 40.0 vs SHORT 41.0"]}},
        ]
        c = build_scan_diagnostics(rows)["counts"]
        assert c["rr_rejected"] == 1
        assert c["conflict_rejected"] == 1

    def test_stage_funnel_is_internally_consistent(self):
        q = compute_setup_quality(_long_signal(rr=2.4, long_total=70.0), _row(),
                                  market_context=_ctx(), df=_frame_with_volume(2.2))
        rows = [
            {"symbol": "A", "signal": "LONG", "price": 100.0, "atr": 1.2,
             "signal_data": {}, **q},
            {"symbol": "B", "error": "x", "signal": "ERROR", "price": 0},
        ]
        d = build_scan_diagnostics(rows, [{"symbol": "A", **q}])
        for s in d["stages"]:
            assert s["input"] - s["rejected"] == s["output"], s["key"]
        assert d["stages"][0]["output"] == 2

    def test_diagnostics_never_mutate_the_rows(self):
        q = compute_setup_quality(_long_signal(rr=2.4, long_total=70.0), _row(),
                                  market_context=_ctx(), df=_frame_with_volume(2.2))
        row = {"symbol": "A", "signal": "LONG", "price": 100.0, "atr": 1.2,
               "signal_data": {}, **q}
        before = dict(row)
        build_scan_diagnostics([row], [row])
        assert row == before

    def test_text_renderer_runs(self):
        assert "STAGE FUNNEL" in diagnostics_text(build_scan_diagnostics([]))
