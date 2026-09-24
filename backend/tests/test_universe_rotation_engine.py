"""Phase 4 — Universe / Rotation Engine tests (categories A–L).

DRAFT-ONLY shadow engine scope. The engine NEVER approves / supersedes /
activates; the legacy scanner keeps running off ``UNIVERSE_SOURCE="legacy"``
and the 40-stock ``NSE_UNIVERSES`` are untouched. NO scheduler and no startup
hook invokes the engine. Every test uses a TEMP-file SQLite database created
via ``Base.metadata.create_all`` — the real ``intradayai.db`` is never opened.

Category map (Phase 2 §1–§22 / Phase 4 §15–§19):
  A  Rotation Score weights / bounds / reproducibility
  B  normalization (percentile, minmax, trapezoid, absolute dq, safe_div)
  C  candidate eligibility stages (tradability → liquidity → volume → ATR →
     data quality → signal quality), market-closed neutrality,
     PROVIDER_FAILURE ⇒ INELIGIBLE
  D  LONG/SHORT replay independence + sample gates (INSUFFICIENT_SAMPLE ⇒ 0,
     never 100−LONG)
  E  Core = 30 stability-based (NOT top-30 by Rotation Score) + hard gates +
     retention bonus + legacy-mode fresh selection
  F  Rotation = top-50 deterministic ranking + anti-churn ladder
     (MONITOR / REVIEW / REPLACE / cap / cooldown / margin) — pure functions
  G  disjointness + total = 80 (30 + 50)
  I  anti-churn end-to-end (retention, promotion to Core, emergency removal)
  J  draft lifecycle (INSUFFICIENT_CANDIDATES persists nothing; single-transaction
     atomicity)
  K  persistence (30/50/80 rows, config_hash, scores within weights, additive
     non-member eligibility rows, `get_universe_version_detail`, validation gate
     ⇒ VALIDATED — never APPROVED/ACTIVE)
  L  regression (legacy scanner + engine not wired into runtime)
"""
import asyncio
import hashlib
import random
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import Base
from app.models.universe_models import (
    ROTATION_SCORE_COMPONENT_WEIGHTS,
    UniverseMembership,
    UniverseVersion,
)
from app.services import (
    rotation_eligibility as elig,
    rotation_replay as replay,
    rotation_scoring as scoring,
    rotation_selection as selection,
)
from app.services.market_data.base import MarketDataProvider
from app.services.rotation_config import RotationEngineConfig
from app.services.universe_rotation_engine import (
    ENGINE_VERSION,
    UniverseRotationEngine,
)
from app.services.universe_state_service import UniverseStateService

# ---------------------------------------------------------------------------
# shared constants
# ---------------------------------------------------------------------------
# 2026-08-28 (Friday) 18:00 IST → 2-week window [2026-08-14 .. 08-28].
# Confirmed NSE trading days in the window (official 2026 calendar): 17,18,19,
# 20,21,24,25,26,27,28 (10 sessions). 2026-08-22 is a Saturday.
# Phase 5C calendar correction: the original 2026-09-11→09-25 window contained
# an official holiday (2026-09-14 Ganesh Chaturthi) which would have collapsed
# the fixture window to 9 sessions — below min_observation_sessions=10 — so the
# window moved to this holiday-free fortnight to keep the fixtures' 10-session
# semantics under the corrected official calendar.
CALC = datetime(2026, 8, 28, 18, 0)
EFFECTIVE_FROM = datetime(2026, 9, 4, 9, 15)
SATURDAY = date(2026, 8, 22)

WEIGHTS = dict(ROTATION_SCORE_COMPONENT_WEIGHTS)
WEIGHT_TOTAL = sum(WEIGHTS.values())  # 100


# ---------------------------------------------------------------------------
# canonical configuration (all thresholds explicit — tests never read defaults)
# ---------------------------------------------------------------------------
def _cfg(**overrides):
    base = dict(
        core_size=30,
        rotation_size=50,
        candidate_pool_min=150,
        candidate_pool_max=250,
        window_weeks=2,
        min_observation_sessions=10,
        expected_bars_per_session=75,
        signal_warmup_bars=55,
        liquidity_floor=50_000_000.0,
        volume_floor=500_000.0,
        volume_cv_max=2.5,
        atr_min_pct=0.003,
        atr_max_pct=0.05,
        atr_opt_low_pct=0.006,
        atr_opt_high_pct=0.02,
        min_data_quality=0.6,
        min_signal_sample_long=8,
        min_signal_sample_short=8,
        min_frequency_floor=0.0,
        core_trading_consistency_pct=99.0,
        core_volume_cv_max=2.5,
        core_review_consecutive_periods=2,
        core_replacement_score_delta=5.0,
        core_retention_bonus=0.03,
        anti_churn_score_delta=5.0,
        rotation_retention_floor=45.0,
        replacement_cooldown_weeks=2,
        cooldown_override_delta=10.0,
        max_churn_per_cycle=10,
    )
    base.update(overrides)
    return RotationEngineConfig(**base)


# ---------------------------------------------------------------------------
# deterministic fixture provider (NEVER process-random)
# ---------------------------------------------------------------------------
class _FixtureProvider(MarketDataProvider):
    """Process-stable deterministic OHLCV fixture.

    Seed is derived from ``sha1(f"{symbol}:fx")`` so the SAME symbol produces
    the SAME frame on every run/process — usable in assertions (unlike the
    mock/yfinance providers who use process-randomized ``hash()``).

    ``params`` keys: base_price, vol (per-bar vol), drift (per-bar drift),
    volume_mult, base_volume, malformed (bool → inject malformed bars),
    sessions (use only the last N session dates), dates_override (explicit
    bar dates), fail (raise on fetch.).
    """

    def __init__(self, symbols, session_dates, bars_per_session=75):
        self._symbols = symbols
        self._dates = list(session_dates)
        self._bars = bars_per_session

    @property
    def data_source_label(self):
        return "FIXTURE"

    async def get_quote(self, symbol):
        return {"symbol": symbol, "price": 1000.0}

    async def get_ohlcv(self, symbol, timeframe="5m", days=5):
        p = self._symbols.get(symbol)
        if p is None:
            return pd.DataFrame()
        if p.get("fail"):
            raise RuntimeError(f"simulated provider failure for {symbol}")
        dates = p.get("dates_override") or self._dates
        n_sessions = int(p.get("sessions", 0) or 0)
        if n_sessions and n_sessions < len(dates):
            dates = dates[-n_sessions:]
        rows = []
        rng = random.Random(hashlib.sha1(f"{symbol}:fx".encode()).hexdigest())
        base = float(p.get("base_price", 1000.0))
        vol = float(p.get("vol", 0.004))
        drift = float(p.get("drift", 0.0))
        mult = float(p.get("volume_mult", 1.0))
        base_volume = float(p.get("base_volume", 100_000))
        malformed = bool(p.get("malformed", False))
        for day in dates:
            price = float(base)
            for i in range(self._bars):
                ret = rng.gauss(drift, vol)
                o = price
                c = price * (1 + ret)
                h = max(o, c) * (1 + abs(rng.gauss(0, vol * 0.5)))
                l = min(o, c) * (1 - abs(rng.gauss(0, vol * 0.5)))
                v = max(0, int(base_volume * mult * (1 + rng.gauss(0, 0.15))))
                if malformed and i == self._bars // 2:
                    # violates h >= max(o, c) → malformed bar
                    h = max(o, c) * 0.5
                ts = datetime(day.year, day.month, day.day, 9, 15) + timedelta(
                    minutes=5 * i
                )
                rows.append(
                    {
                        "timestamp": ts,
                        "open": o,
                        "high": h,
                        "low": l,
                        "close": c,
                        "volume": v,
                    }
                )
                price = c
        return pd.DataFrame(rows)

    async def get_intraday_bars(self, symbol, timeframe="5m"):
        return await self.get_ohlcv(symbol, timeframe)

    async def get_market_status(self):
        return {"status": "open"}

    async def get_instruments(self, universe="NIFTY50"):
        return []

    async def get_market_index(self, index_name="NIFTY"):
        return {}


# ---------------------------------------------------------------------------
# temp-SQLite context + runners
# ---------------------------------------------------------------------------
async def _ctx(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = UniverseStateService(session_factory=factory)
    return service, engine, factory


def _run(coro):
    return asyncio.run(coro)


def _engine_dates():
    return UniverseRotationEngine(config=_cfg()).expected_session_dates(CALC)


def _healthy(n, start=0):
    """n eligible symbols cycling 5 drift profiles × 6 volume multipliers —
    every symbol passes liquidity/volume/ATR/dq/signal gates."""
    syms = {}
    params = [
        {"drift": 0.006, "vol": 0.004},
        {"drift": -0.006, "vol": 0.004},
        {"drift": 0.0, "vol": 0.004},
        {"drift": 0.004, "vol": 0.006},
        {"drift": -0.004, "vol": 0.006},
    ]
    for i in range(n):
        p = dict(params[i % 5])
        p["volume_mult"] = [0.4, 0.7, 1.0, 1.6, 2.2, 3.0][i % 6]
        syms[f"SYM{i + start:03d}"] = p
    return syms


async def _evaluate(symbol, param, dates, cfg):
    prov = _FixtureProvider({symbol: param}, dates)
    df = await prov.get_ohlcv(symbol, days=16)
    metrics = replay.replay_window(df, dates, cfg)
    return elig.evaluate_candidate(symbol, df, dates, cfg, signal_metrics=metrics)


def _cand(
    symbol,
    score=60.0,
    dq=8.0,
    liq=1e9,
    vol_cv=0.2,
    atr=0.01,
    consistency=1.0,
    reliability=1.0,
):
    return selection.RotationCandidate(
        symbol=symbol,
        rotation_score=score,
        data_quality_score=dq,
        median_daily_value=liq,
        volume_cv=vol_cv,
        median_atr_pct=atr,
        trading_consistency=consistency,
        signal_reliability=reliability,
        expected_sessions=10,
        present_sessions=10,
    )


# ===========================================================================
# A — Rotation Score weights / bounds / reproducibility
# ===========================================================================
def test_a1_component_weights_exact_and_sum_100():
    assert WEIGHT_TOTAL == 100
    assert {
        "liquidity_score": 20,
        "volume_quality_score": 15,
        "volatility_score": 15,
        "signal_frequency_score": 10,
        "long_quality_score": 15,
        "short_quality_score": 15,
        "data_quality_score": 10,
    } == WEIGHTS


def test_a2_rotation_score_bounded_and_deterministic():
    components = {
        "liquidity_score": 20.0,
        "volume_quality_score": 15.0,
        "volatility_score": 15.0,
        "signal_frequency_score": 10.0,
        "long_quality_score": 15.0,
        "short_quality_score": 15.0,
        "data_quality_score": 10.0,
    }
    total = scoring.rotation_score(components)
    assert total == pytest.approx(100.0, abs=1e-6)
    assert 0.0 <= total <= 100.0
    # each component scorer is clamped to its own weight
    liq = scoring.liquidity_scores({"A": 5e12, "B": 1e9})
    assert liq["A"] == pytest.approx(20.0, abs=1e-6)
    freq = scoring.signal_frequency_scores({"A": 1.0, "B": 0.0})
    assert freq["A"] == pytest.approx(10.0, abs=1e-6)
    volty = scoring.volatility_scores({"A": 0.012}, _cfg())  # in plateau
    assert volty["A"] == pytest.approx(15.0, abs=1e-6)
    # same inputs → same total (reproducibility)
    assert scoring.rotation_score(components) == scoring.rotation_score(components)


def test_a3_engine_reproducible_across_runs(tmp_path):
    """Same deterministic inputs + fresh DBs → byte-identical member/score
    outputs, counts and config_hash (no injection of iteration/randomness)."""

    async def scenario():
        syms = _healthy(85)
        dates = _engine_dates()
        cfg = _cfg()
        prov = _FixtureProvider(syms, dates)

        s1, e1, _ = await _ctx(str(tmp_path / "r1.db"))
        s2, e2, _ = await _ctx(str(tmp_path / "r2.db"))
        try:
            eng1 = UniverseRotationEngine(state_service=s1, provider=prov, config=cfg)
            eng2 = UniverseRotationEngine(state_service=s2, provider=prov, config=cfg)
            r1 = await eng1.generate_draft(
                symbols=list(syms), version="det-v1",
                calculated_at=CALC, effective_from=EFFECTIVE_FROM,
            )
            r2 = await eng2.generate_draft(
                symbols=list(syms), version="det-v1",
                calculated_at=CALC, effective_from=EFFECTIVE_FROM,
            )
            assert r1["status"] == r2["status"] == "DRAFT_CREATED"
            assert r1["counts"] == r2["counts"]
            assert r1["config_hash"] == r2["config_hash"]
            assert r1["engine_version"] == r2["engine_version"] == ENGINE_VERSION
            d1 = await s1.get_universe_version_detail(r1["draft"]["id"])
            d2 = await s2.get_universe_version_detail(r2["draft"]["id"])
            m1 = [
                (m["symbol"], m["category"], m["rank"], round(m["rotation_score"], 6))
                for m in d1["members"]
            ]
            m2 = [
                (m["symbol"], m["category"], m["rank"], round(m["rotation_score"], 6))
                for m in d2["members"]
            ]
            assert m1 == m2
            sc1 = sorted(
                (s["symbol"],) + tuple(round(s[k], 4) for k in (
                    "liquidity_score", "volume_quality_score", "volatility_score",
                    "signal_frequency_score", "long_quality_score",
                    "short_quality_score", "data_quality_score",
                    "total_rotation_score",
                ))
                for s in d1["scores"]
            )
            sc2 = sorted(
                (s["symbol"],) + tuple(round(s[k], 4) for k in (
                    "liquidity_score", "volume_quality_score", "volatility_score",
                    "signal_frequency_score", "long_quality_score",
                    "short_quality_score", "data_quality_score",
                    "total_rotation_score",
                ))
                for s in d2["scores"]
            )
            assert sc1 == sc2
        finally:
            await e1.dispose()
            await e2.dispose()

    _run(scenario())


# ===========================================================================
# B — normalization
# ===========================================================================
def test_b1_percentile_ranks():
    assert scoring.percentile_ranks({}) == {}
    ranks = scoring.percentile_ranks({"A": 10.0, "B": 20.0, "C": 30.0})
    assert ranks["A"] == pytest.approx(0.0)
    assert ranks["B"] == pytest.approx(0.5)
    assert ranks["C"] == pytest.approx(1.0)
    # ties share the midpoint of their band
    ranks2 = scoring.percentile_ranks({"A": 5.0, "B": 5.0, "C": 10.0})
    assert ranks2["A"] == ranks2["B"] == pytest.approx(0.25)
    # non-finite / missing values are dropped from the population (never a score)
    dirty = scoring.percentile_ranks(
        {"A": 1.0, "B": float("nan"), "C": float("inf"), "D": None, "E": 2.0}
    )
    assert set(dirty) == {"A", "E"}
    assert dirty["E"] == 1.0
    # all-population-dirty → empty (no fabrication)
    assert scoring.percentile_ranks({"A": float("nan")}) == {}


def test_b2_minmax_normalize():
    assert scoring.minmax_normalize({}) == {}
    out = scoring.minmax_normalize({"A": 10.0, "B": 30.0})
    assert out["A"] == pytest.approx(0.0)
    assert out["B"] == pytest.approx(1.0)
    # all-equal (degenerate range) → midpoint, NOT a division-by-zero error
    flat = scoring.minmax_normalize({"A": 5.0, "B": 5.0, "C": 5.0})
    assert flat["A"] == flat["B"] == flat["C"] == pytest.approx(0.5)
    # NaN dropped; all-NaN → empty; single-value population → neutral 0.5
    assert scoring.minmax_normalize({"A": 1.0, "B": float("nan")}) == {"A": 0.5}
    assert scoring.minmax_normalize({"A": float("nan")}) == {}


def test_b3_trapezoid():
    cfg = _cfg()
    a, lo, hi, d = cfg.atr_min_pct, cfg.atr_opt_low_pct, cfg.atr_opt_high_pct, cfg.atr_max_pct
    assert scoring.trapezoid(0.012, a, lo, hi, d) == pytest.approx(1.0)  # plateau
    assert scoring.trapezoid(0.001, a, lo, hi, d) == pytest.approx(0.0)  # below min
    assert scoring.trapezoid(0.09, a, lo, hi, d) == pytest.approx(0.0)  # above max
    mid_up = scoring.trapezoid((a + lo) / 2, a, lo, hi, d)
    assert 0.0 < mid_up < 1.0
    mid_down = scoring.trapezoid((hi + d) / 2, a, lo, hi, d)
    assert 0.0 < mid_down < 1.0
    # degenerate band (a == lo) must still be finite, no div-by-zero: any x in
    # the collapsed (a, hi] region gets the flat 1.0; x == a boundary stays 0.
    assert scoring.trapezoid(0.005, 0.005, 0.005, 0.02, 0.05) == 0.0  # boundary
    assert scoring.trapezoid(0.01, 0.005, 0.005, 0.02, 0.05) == pytest.approx(1.0)
    assert scoring.trapezoid(float("nan"), a, lo, hi, d) == 0.0
    assert 0.0 <= scoring.trapezoid(1.0, a, lo, hi, d) <= 1.0


def test_b4_safe_div_no_boom():
    assert scoring.safe_div(1.0, 2.0) == pytest.approx(0.5)
    assert scoring.safe_div(1.0, 0.0) == 0.0
    assert scoring.safe_div(1.0, None) == 0.0
    assert scoring.safe_div(1.0, float("nan")) == 0.0
    assert scoring.safe_div(1.0, float("inf")) == 0.0
    assert scoring.safe_div(1.0, 2.0, default=9.0) == pytest.approx(0.5)


def test_b5_data_quality_absolute_not_pathological():
    # absolute mapping — a garbage cycle cannot re-normalize to 100
    hi = scoring.data_quality_scores({"A": 10.0, "B": 2.0})
    assert hi["A"] == pytest.approx(10.0) and hi["B"] == pytest.approx(2.0)
    lo = scoring.data_quality_scores({"A": 5.0, "B": 1.0})
    assert lo["A"] == pytest.approx(5.0) and lo["B"] == pytest.approx(1.0)
    # missing symbols earn nothing, never a strong score
    assert scoring.data_quality_scores({"A": 4.0}).get("ZZZ", 0.0) == 0.0


def test_b6_long_short_independent_and_insufficient_is_zero():
    ok = {"A": True, "B": True}
    l = scoring.long_quality_scores({"A": 1.0, "B": 0.5}, ok, weight=15)
    # SHORT percentile evaluated over a DIFFERENT population (own raw metrics) —
    # A tops LONG but bottoms SHORT, proving long_quality ≠ 100 − short_quality
    s = scoring.short_quality_scores({"A": 0.4, "B": 0.9}, ok, weight=15)
    assert l["A"] == pytest.approx(15.0) and l["B"] == pytest.approx(0.0)
    assert s["A"] == pytest.approx(0.0) and s["B"] == pytest.approx(15.0)
    # not an inversion pair: SHORT(A) is derived from the SHORT population
    # (rank-0) — a `100 − LONG` shortcut could never yield this split.
    # INSUFFICIENT_SAMPLE ⇒ component zeroed + flagged, not excluded/guessed
    mixed_ok = {"A": True, "B": False}
    l2 = scoring.long_quality_scores({"A": 1.0, "B": 0.9}, mixed_ok, weight=15)
    assert l2["B"] == 0.0  # insufficient sample → 0, never imputed
    assert "B" in l2  # still reported (flagged by evaluator), not dropped
    assert all(0.0 <= v <= 15.0 for v in l2.values())


# ===========================================================================
# C — candidate eligibility stages
# ===========================================================================
def test_c1_healthy_candidate_eligible_all_stages_pass():
    async def scenario():
        ev = await _evaluate("H1", {"drift": 0.004, "vol": 0.006}, _engine_dates(), _cfg())
        assert ev.eligibility_status == "ELIGIBLE"
        for name, st in ev.stages.items():
            assert st.status == "PASS", name
        assert ev.present_sessions == 10
        assert ev.valid_evaluated_sessions == 10
        assert ev.median_daily_value >= 50_000_000
        assert ev.mean_volume >= 500_000
        assert ev.median_atr_pct is not None and 0.003 <= ev.median_atr_pct <= 0.05
        assert ev.data_quality_score == pytest.approx(10.0)
        assert ev.trading_consistency == pytest.approx(1.0)
        assert ev.data_quality_state == "SUFFICIENT"

    _run(scenario())


def test_c2_missing_columns_ineligible_no_data():
    async def scenario():
        cfg = _cfg()
        dates = _engine_dates()
        prov = _FixtureProvider({"X": {"drift": 0.004}}, dates)
        df = await prov.get_ohlcv("X", days=16)
        df = df[["timestamp", "open", "close"]]
        ev = elig.evaluate_candidate("X", df, dates, cfg)
        assert ev.eligibility_status == "INELIGIBLE"
        assert ev.tradability.status == "FAIL"
        assert ev.tradability.reason.startswith("NO_DATA")
        assert "missing columns" in ev.tradability.reason

    _run(scenario())


def test_c3_low_liquidity_and_volume_ineligible():
    async def scenario():
        # volume_mult 0.001 → daily volume ≈ 7.5k (< 500k) AND daily value
        # ≈ ₹7.5M (< ₹50M)
        ev = await _evaluate("LOW", {"volume_mult": 0.001}, _engine_dates(), _cfg())
        assert ev.eligibility_status == "INELIGIBLE"
        assert ev.liquidity.status == "FAIL"
        assert "BELOW_FLOOR" in ev.liquidity.reason
        assert ev.volume.status == "FAIL"
        assert "BELOW_FLOOR" in ev.volume.reason

    _run(scenario())


def test_c4_empty_history_ineligible_no_data():
    async def scenario():
        ev = elig.evaluate_candidate("E1", pd.DataFrame(), _engine_dates(), _cfg())
        assert ev.eligibility_status == "INELIGIBLE"
        assert ev.tradability.status == "FAIL"
        assert ev.tradability.reason.startswith("NO_DATA")
        assert ev.data_quality_state == "INSUFFICIENT_DATA"

    _run(scenario())


def test_c5_saturday_only_bars_are_neutral_not_penalty():
    """Market-closed periods excluded from denominators: Saturday bars →
    0 sessions → INSUFFICIENT_DATA — NOT a failure, NOT PROVIDER_FAILURE."""
    async def scenario():
        ev = await _evaluate(
            "WKND", {"dates_override": [SATURDAY]}, _engine_dates(), _cfg()
        )
        assert ev.eligibility_status == "INSUFFICIENT_DATA"
        assert ev.tradability.status == "PASS"  # OHLCV history exists
        assert ev.present_sessions == 0
        assert ev.data_quality_state == "INSUFFICIENT_DATA"
        for name, st in ev.stages.items():
            if name == "tradability":
                continue
            assert st.status == "INSUFFICIENT_DATA", name
        assert ev.failure_reason is None or "PROVIDER_FAILURE" not in ev.failure_reason

    _run(scenario())


def test_c6_provider_exception_is_provider_failure():
    async def scenario():
        ev = elig.provider_failure_evaluation("FAILX", "simulated boom")
        assert ev.eligibility_status == "INELIGIBLE"
        assert ev.fetch_failed is True
        assert ev.data_quality_state == "PROVIDER_FAILURE"
        assert ev.failure_reason.startswith("PROVIDER_FAILURE")
        assert ev.tradability.status == "FAIL"

    _run(scenario())


def test_c7_few_sessions_is_insufficient_not_ineligible():
    """8 sessions < MIN_OBSERVATION_SESSIONS(10) → INSUFFICIENT_DATA (never a
    hard fail — missing data is first-class, not a penalty). Coverage is still
    healthy enough (0.8 ≥ 0.6) that the absolute DQ stage passes."""
    async def scenario():
        ev = await _evaluate("FEW", {"sessions": 8}, _engine_dates(), _cfg())
        assert ev.eligibility_status == "INSUFFICIENT_DATA"
        assert ev.present_sessions == 8
        assert ev.data_quality.status == "PASS"  # 8/10 sessions → dq_norm 0.8
        assert ev.liquidity.status == "INSUFFICIENT_DATA"
        assert ev.volume.status == "INSUFFICIENT_DATA"
        assert ev.volatility.status == "INSUFFICIENT_DATA"
        assert ev.signal_quality.status == "INSUFFICIENT_DATA"

    _run(scenario())


def test_c8_malformed_bars_penalized_but_not_auto_failed():
    async def scenario():
        ev = await _evaluate("MLF", {"malformed": True}, _engine_dates(), _cfg())
        assert ev.eligibility_status == "ELIGIBLE"
        assert ev.malformed_rate is not None and ev.malformed_rate > 0.0
        assert ev.data_quality_score is not None and ev.data_quality_score < 10.0
        assert ev.data_quality_score >= _cfg().min_data_quality * 10.0
        assert ev.data_quality.status == "PASS"

    _run(scenario())


# ===========================================================================
# D — LONG / SHORT replay
# ===========================================================================
def test_d1_long_rich_symbol_long_evidence_only():
    async def scenario():
        cfg = _cfg()
        dates = _engine_dates()
        prov = _FixtureProvider({"T": {"drift": 0.006, "vol": 0.004}}, dates)
        df = await prov.get_ohlcv("T", days=16)
        m = replay.replay_window(df, dates, cfg)
        assert m["valid_evaluated_sessions"] == 10
        assert m["long_count"] >= 8
        assert m["short_count"] == 0
        assert m["long_sample_ok"] is True
        assert m["short_sample_ok"] is False
        assert m["frequency"] is not None and m["frequency"] >= 0.8
        assert m["session_class_counts"].get("VALID_EVALUATED") == 10

    _run(scenario())


def test_d2_short_rich_symbol_short_evidence_only():
    async def scenario():
        cfg = _cfg()
        dates = _engine_dates()
        prov = _FixtureProvider({"T": {"drift": -0.006, "vol": 0.004}}, dates)
        df = await prov.get_ohlcv("T", days=16)
        m = replay.replay_window(df, dates, cfg)
        assert m["valid_evaluated_sessions"] == 10
        assert m["short_count"] >= 8
        assert m["long_count"] == 0
        assert m["short_sample_ok"] is True
        assert m["long_sample_ok"] is False

    _run(scenario())


def test_d3_replay_does_not_fabricate_trade_outcomes():
    """Win-rate / expectancy / profit factor need persisted trade outcomes
    (Phase 2 Q11) — the observable replay subset only is reported."""
    async def scenario():
        cfg = _cfg()
        dates = _engine_dates()
        prov = _FixtureProvider({"T": {"drift": 0.006, "vol": 0.004}}, dates)
        df = await prov.get_ohlcv("T", days=16)
        m = replay.replay_window(df, dates, cfg)
        for key in ("win_rate", "expectancy", "profit_factor", "false_signal_rate"):
            assert key not in m, key

    _run(scenario())


def test_d4_insufficient_sample_gate_flags_both_directions():
    async def scenario():
        cfg = _cfg()
        dates = _engine_dates()
        prov = _FixtureProvider({"T": {"sessions": 5}}, dates)
        df = await prov.get_ohlcv("T", days=16)
        m = replay.replay_window(df, dates, cfg)
        assert m["valid_evaluated_sessions"] == 5
        assert m["long_sample_ok"] is False
        assert m["short_sample_ok"] is False
        assert m["long_count"] <= 5  # can never exceed valid sessions
        assert m["no_trade_count"] == 0

    _run(scenario())


def test_d5_no_sessions_means_no_signals_no_trade():
    async def scenario():
        cfg = _cfg()
        dates = _engine_dates()
        prov = _FixtureProvider({"T": {"dates_override": [SATURDAY]}}, dates)
        df = await prov.get_ohlcv("T", days=16)
        m = replay.replay_window(df, dates, cfg)
        assert m["valid_evaluated_sessions"] == 0
        assert m["long_count"] == 0 and m["short_count"] == 0 and m["no_trade_count"] == 0
        assert m["frequency"] is None
        assert m["long_raw"] is None and m["short_raw"] is None
        assert m["session_class_counts"] == {}

    _run(scenario())


# ===========================================================================
# E — Core selection (stability, NOT top-30 by Rotation Score)
# ===========================================================================
def test_e1_core_selects_30_stability_sorted():
    cands = [_cand(f"S{i:02d}", score=float(100 - i)) for i in range(40)]
    core, res = selection.select_core(cands, _cfg())
    assert len(core) == 30
    assert res.core_shortfall == 0
    assert [c.rank for c in core] == list(range(1, 31))
    stabs = [c.stability_score for c in core]
    assert stabs == sorted(stabs, reverse=True)
    assert len({c.symbol for c in core}) == 30  # no duplicates


def test_e2_core_is_not_top30_by_rotation_score():
    cands = [_cand(f"H{i:02d}", score=float(90 - i)) for i in range(34)]
    cands.append(_cand("TOPSCO", score=95.0, consistency=0.90))  # fails core gate
    core, res = selection.select_core(cands, _cfg())
    core_syms = {c.symbol for c in core}
    assert "TOPSCO" not in core_syms  # highest Rotation Score, but not stable
    assert any(r["symbol"] == "TOPSCO" for r in res.core_rejections)
    assert len(core) == 30


def test_e3_core_retention_bonus_breaks_ties():
    a = _cand("A", score=50.0)
    b = _cand("B", score=60.0)  # identical stability inputs otherwise
    core, res = selection.select_core(
        [a, b], _cfg(), previous_core={"A": {"rank": 3, "score": 70.0}}
    )
    assert core[0].symbol == "A"  # previous Core member outranks identical B
    assert core[0].retention_reason and "retained" in core[0].retention_reason
    assert core[0].previous_rank == 3 and core[0].previous_score == 70.0


def test_e4_core_shortfall_when_passers_few():
    cands = [_cand(f"K{i:02d}") for i in range(20)]
    core, res = selection.select_core(cands, _cfg())
    assert len(core) == 20
    assert res.core_shortfall == 10


# ===========================================================================
# F — Rotation selection + anti-churn ladder (pure)
# ===========================================================================
def test_f1_fresh_universe_rotation_is_top50_in_rank_order():
    cands = [_cand(f"X{i:02d}", score=float(100 - i), liq=float(1e9 + i)) for i in range(60)]
    rot, res = selection.select_rotation(cands, _cfg())
    assert len(rot) == 50
    assert res.rotation_shortfall == 0
    assert [r.rank for r in rot] == list(range(1, 51))
    # roster follows rotation_sort_key DESC (score desc → liq desc → symbol asc)
    scores = [r.rotation_score for r in rot]
    assert scores == sorted(scores, reverse=True)
    assert len({r.symbol for r in rot}) == 50


def test_f2_rotation_shortfall_when_fewer_candidates():
    cands = [_cand(f"Y{i:02d}", score=60.0) for i in range(40)]
    rot, res = selection.select_rotation(cands, _cfg())
    assert len(rot) == 40
    assert res.rotation_shortfall == 10


def test_f3_tie_breaks_symbol_ascending():
    a = _cand("AAA", score=60.0, dq=8.0, liq=1e9)
    b = _cand("ZZZ", score=60.0, dq=8.0, liq=1e9)
    rot, res = selection.select_rotation([b, a], _cfg())
    assert [r.symbol for r in rot] == ["AAA", "ZZZ"]


def test_f4_default_keep_strong_incumbent_vs_higher_challenger():
    inc = _cand("INC", score=55.0)  # current ≥ floor(45), prev strong
    chs = [_cand(f"C{i:02d}", score=float(90 - i)) for i in range(8)]
    prev = {"INC": {"rank": 1, "score": 60.0}}
    rot, res = selection.select_rotation([inc] + chs, _cfg(), previous_rotation=prev)
    roster = [r.symbol for r in rot]
    assert roster[0] == "INC"  # retained despite being the lowest score
    assert res.rotation_shortfall == 41
    keep = [d for d in res.churn_decisions if d.get("symbol") == "INC"]
    assert keep and any(d.get("action") == "KEEP" for d in keep)


def test_f5_monitor_one_weak_period_is_kept():
    inc = _cand("MON", score=40.0)  # current < floor
    chs = [_cand(f"C{i:02d}", score=80.0) for i in range(8)]
    prev = {"MON": {"rank": 1, "score": 60.0}}  # previous cycle STRONG
    rot, res = selection.select_rotation([inc] + chs, _cfg(), previous_rotation=prev)
    assert "MON" in {r.symbol for r in rot}
    mon = [d for d in res.churn_decisions if d.get("symbol") == "MON"]
    assert any(d.get("state") == "MONITOR" for d in mon)


def test_f6_review_replace_with_margin_full_roster():
    """REVIEW incumbent (2 weak periods) displaced only when a challenger beats
    it by ANTI_CHURN_SCORE_DELTA — with a FULL previous roster so no open slot
    exists to soak up the challenger first."""
    cfg = _cfg()
    incs = [_cand(f"I{i:02d}", score=40.0) for i in range(4)]  # REVIEW
    strong = [_cand(f"S{i:02d}", score=55.0) for i in range(46)]  # protected
    chs = [_cand(f"J{i:02d}", score=78.0) for i in range(8)]  # margin 38 ≥ 5
    prev = {f"I{i:02d}": {"rank": i + 1, "score": 40.0} for i in range(4)}
    for i in range(46):
        prev[f"S{i:02d}"] = {"rank": 5 + i, "score": 55.0}
    rot, res = selection.select_rotation(incs + strong + chs, cfg, previous_rotation=prev)
    roster = {r.symbol for r in rot}
    assert len(rot) == 50
    assert all(f"S{i:02d}" in roster for i in range(46))
    for i in range(4):
        assert f"I{i:02d}" not in roster  # displaced
    rpl = [d for d in res.churn_decisions if d.get("action") == "REPLACE"]
    assert len(rpl) == 4
    assert rpl[0]["replaced"].startswith("I")
    assert rpl[0]["margin"] == pytest.approx(38.0)


def test_f7_marginal_challenger_does_not_displace():
    cfg = _cfg()
    incs = [_cand(f"M{i:02d}", score=44.0) for i in range(4)]  # REVIEW (prev weak)
    strong = [_cand(f"N{i:02d}", score=55.0) for i in range(46)]
    chs = [_cand(f"K{i:02d}", score=46.0) for i in range(8)]  # margin 2 < 5 → keep
    prev = {f"M{i:02d}": {"rank": i + 1, "score": 42.0} for i in range(4)}
    for i in range(46):
        prev[f"N{i:02d}"] = {"rank": 5 + i, "score": 55.0}
    rot, res = selection.select_rotation(incs + strong + chs, cfg, previous_rotation=prev)
    roster = {r.symbol for r in rot}
    assert all(f"M{i:02d}" in roster for i in range(4))  # kept (default-keep)
    assert not any(d.get("action") == "REPLACE" for d in res.churn_decisions)


def test_f8_churn_capped_at_max_churn_per_cycle():
    cfg = _cfg()
    incs = [_cand(f"I{i:02d}", score=40.0) for i in range(12)]  # 12 REVIEW
    strong = [_cand(f"S{i:02d}", score=55.0) for i in range(38)]
    chs = [_cand(f"J{i:02d}", score=90.0) for i in range(30)]  # margins ≫ delta
    prev = {f"I{i:02d}": {"rank": i + 1, "score": 40.0} for i in range(12)}
    for i in range(38):
        prev[f"S{i:02d}"] = {"rank": 13 + i, "score": 55.0}
    rot, res = selection.select_rotation(incs + strong + chs, cfg, previous_rotation=prev)
    roster = {r.symbol for r in rot}
    assert len(rot) == 50
    rpl = [d for d in res.churn_decisions if d.get("action") == "REPLACE"]
    assert len(rpl) == cfg.max_churn_per_cycle  # capped at 10
    assert sum(1 for i in range(12) if f"I{i:02d}" in roster) == 2  # 2 kept
    assert sum(1 for i in range(12) if f"J{i:02d}" in roster) == 10


def test_f9_cooldown_blocks_and_override_allows():
    cfg = _cfg()
    inc = _cand("CD", score=40.0)
    strong = [_cand(f"T{i:02d}", score=55.0) for i in range(49)]
    prev = {"CD": {"rank": 1, "score": 40.0}}
    for i in range(49):
        prev[f"T{i:02d}"] = {"rank": 2 + i, "score": 55.0}
    # margin 6 < cooldown_override_delta(10) → in-cooldown challenger blocked
    cd_chs = [_cand(f"C{i:02d}", score=46.0) for i in range(5)]
    rot1, res1 = selection.select_rotation(
        [inc] + strong + cd_chs, cfg,
        previous_rotation=prev, cooldown_symbols={f"C{i:02d}" for i in range(5)},
    )
    assert "CD" in {r.symbol for r in rot1}
    assert not any(d.get("action") == "REPLACE" for d in res1.churn_decisions)
    # margin 12 ≥ override delta(10) → in-cooldown challenger allowed
    ov_chs = [_cand(f"D{i:02d}", score=52.0) for i in range(5)]
    rot2, res2 = selection.select_rotation(
        [inc] + strong + ov_chs, cfg,
        previous_rotation=prev, cooldown_symbols={f"D{i:02d}" for i in range(5)},
    )
    assert "CD" not in {r.symbol for r in rot2}
    assert any(d.get("action") == "REPLACE" for d in res2.churn_decisions)


# ===========================================================================
# G + K — engine happy path: 80 = 30+50 disjoint, persisted, validates
# ===========================================================================
def test_g1_k3_engine_draft_80_disjoint_persisted_and_validated(tmp_path):
    async def scenario():
        syms = _healthy(85)  # 85 eligible → 80 members + 5 unselected survivors
        cfg = _cfg()
        prov = _FixtureProvider(syms, _engine_dates())
        service, engine, factory = await _ctx(str(tmp_path / "e2e.db"))
        eng = UniverseRotationEngine(state_service=service, provider=prov, config=cfg)
        r = await eng.generate_draft(
            symbols=list(syms), version="e2e-v1",
            calculated_at=CALC, effective_from=EFFECTIVE_FROM,
        )
        assert r["status"] == "DRAFT_CREATED"
        assert r["counts"]["eligible"] == 85 == r["counts"]["candidates"]
        assert r["selection"] is not None
        d = r["draft"]
        assert d["status"] == "DRAFT"
        assert d["core_count"] == 30 and d["rotation_count"] == 50 and d["total_count"] == 80
        assert d["config_hash"] and len(d["config_hash"]) > 8
        assert d["candidate_pool_version_id"] == r["pool"]["id"]
        assert r["previous_cycle"]["present"] is False  # legacy-mode fresh

        detail = await service.get_universe_version_detail(d["id"])
        members = detail["members"]
        core = [m for m in members if m["category"] == "CORE"]
        rotation = [m for m in members if m["category"] == "ROTATION"]
        # G — disjointness + exact counts
        assert len(core) == 30 and len(rotation) == 50
        core_syms = {m["symbol"] for m in core}
        rot_syms = {m["symbol"] for m in rotation}
        assert core_syms.isdisjoint(rot_syms)
        assert len({m["symbol"] for m in members}) == 80
        assert [m["rank"] for m in core] == list(range(1, 31))
        assert [m["rank"] for m in rotation] == list(range(1, 51))

        # K — scores persisted within per-component weights
        scores = detail["scores"]
        assert len(scores) == 80
        for s in scores:
            tot = 0.0
            for comp, weight in WEIGHTS.items():
                val = float(s[comp])
                assert 0.0 <= val <= weight + 1e-9, (s["symbol"], comp)
                tot += val
            assert s["total_rotation_score"] == pytest.approx(
                min(100.0, tot), abs=1e-6
            )

        # K — slot_eligibility persisted for EVERY pool symbol (incl. the 5
        # unselected survivors — additive non-member rows)
        eligibility = detail["eligibility"]
        assert len(eligibility) == 85
        elig_syms = {e["symbol"] for e in eligibility}
        assert set(syms) == elig_syms

        # K — draft lifecycle: VALIDATED via the Phase 3 gate, never APPROVED/ACTIVE
        v = await service.validate_universe(d["id"])
        assert v["status"] == "VALIDATED"
        assert v.get("activated_at") is None
        versions = await service.list_universe_versions()
        assert len(versions) == 1 and versions[0]["status"] == "VALIDATED"
        events = await service.list_events(universe_version_id=d["id"])
        assert any(ev["event_type"] in ("UNIVERSE_DRAFT_CREATED", "UNIVERSE_VALIDATED")
                   for ev in events)
        # pool status remains VALIDATED; no ACTIVE universe anywhere
        pool = await service.get_candidate_pool(r["pool"]["id"])
        assert pool["status"] == "VALIDATED"
        assert versions[0]["status"] != "ACTIVE"
        await engine.dispose()

    _run(scenario())


# ===========================================================================
# I — anti-churn end-to-end
# ===========================================================================
def test_i1_rotation_retention_promotion_and_emergency_removal(tmp_path):
    async def scenario():
        cfg = _cfg()
        dates = _engine_dates()
        syms = _healthy(85)
        syms["BRR01"] = {"drift": 0.006, "vol": 0.004, "volume_mult": 2.0}   # → Core (stable)
        syms["LOWLQ"] = {"drift": 0.006, "vol": 0.004, "volume_mult": 0.1}   # → Rotation keep
        service, engine, factory = await _ctx(str(tmp_path / "i1.db"))
        # previous cycle: rotation = [BRR01 strong, LOWLQ strong, GHOST99 weak]
        pp = await service.create_candidate_pool(
            "pool-prep", source="local",
            symbols=["BRR01", "LOWLQ", "GHOST99"],
            metadata={"symbols": ["BRR01", "LOWLQ", "GHOST99"]},
        )
        pp = await service.validate_candidate_pool(pp["id"])
        members = [
            {"symbol": "BRR01", "category": "ROTATION", "rank": 1, "rotation_score": 48.0},
            {"symbol": "LOWLQ", "category": "ROTATION", "rank": 2, "rotation_score": 52.0},
            {"symbol": "GHOST99", "category": "ROTATION", "rank": 3, "rotation_score": 40.0},
        ]
        await service.create_universe_draft(
            pp["id"], version="prev-v1", members=members,
            core_count=0, rotation_count=3, total_count=3,
            calculated_at=datetime(2026, 9, 11, 18, 0),
            effective_from=datetime(2026, 9, 18, 9, 15),
        )

        prov = _FixtureProvider(syms, dates)
        eng = UniverseRotationEngine(state_service=service, provider=prov, config=cfg)
        r = await eng.generate_draft(
            symbols=list(syms), version="i1-v1",
            calculated_at=CALC, effective_from=EFFECTIVE_FROM,
        )
        assert r["status"] == "DRAFT_CREATED"
        assert r["previous_cycle"]["present"] is True
        assert r["previous_cycle"]["rotation_count"] == 3

        detail = await service.get_universe_version_detail(r["draft"]["id"])
        members_by_cat = {}
        for m in detail["members"]:
            members_by_cat.setdefault(m["category"], {})[m["symbol"]] = m
        core_syms = set(members_by_cat.get("CORE", {}))
        rot_syms = set(members_by_cat.get("ROTATION", {}))

        acts: dict[str, list] = {}
        for d in r["selection"]["churn_decisions"]:
            acts.setdefault(d["symbol"], []).append(d.get("action"))

        # 1) BRR01 (previous Rotation, strong) → either kept in Rotation or
        #    promoted to Core — but NEVER emergency-removed (it is eligible).
        assert "BRR01" in core_syms | rot_syms
        assert "REMOVE" not in acts.get("BRR01", [])
        if "BRR01" in core_syms:
            assert "PROMOTED_TO_CORE" in acts.get("BRR01", [])
            assert acts.get("BRR01", [])[-1] == "PROMOTED_TO_CORE"
        else:
            assert "KEEP" in acts.get("BRR01", [])

        # 2) LOWLQ (previous Rotation, strong) retained in Rotation (too
        #    low-liquidity for Core) with a KEEP decision.
        assert "LOWLQ" in rot_syms
        assert "LOWLQ" not in core_syms
        assert "KEEP" in acts.get("LOWLQ", [])

        # 3) GHOST99 (previous Rotation, missing from the pool) is
        #    emergency-removed and never persisted.
        assert "GHOST99" not in core_syms and "GHOST99" not in rot_syms
        assert "REMOVE" in acts.get("GHOST99", [])

        # total still structurally exact
        assert len(core_syms) == 30 and len(rot_syms) == 50
        await engine.dispose()

    _run(scenario())


# ===========================================================================
# J — draft lifecycle / atomicity
# ===========================================================================
def test_j1_insufficient_candidates_persists_nothing(tmp_path):
    async def scenario():
        cfg = _cfg()
        dates = _engine_dates()
        # 20 healthy + 30 low-liquidity + 20 provider-fail + 10 weekend + 5 few
        syms = _healthy(20)
        for i in range(30):
            syms[f"LOW{i:03d}"] = {"volume_mult": 0.001}
        for i in range(20):
            syms[f"FAIL{i:03d}"] = {"fail": True}
        for i in range(10):
            syms[f"WKND{i:03d}"] = {"dates_override": [SATURDAY]}
        for i in range(5):
            syms[f"FEW{i:03d}"] = {"sessions": 5}
        assert len(syms) == 85

        service, engine, factory = await _ctx(str(tmp_path / "j1.db"))
        prov = _FixtureProvider(syms, dates)
        eng = UniverseRotationEngine(state_service=service, provider=prov, config=cfg)
        r = await eng.generate_draft(
            symbols=list(syms), version="j1-v1",
            calculated_at=CALC, effective_from=EFFECTIVE_FROM,
        )
        assert r["status"] == "INSUFFICIENT_CANDIDATES"
        assert r["draft"] is None
        assert r["counts"]["eligible"] == 20
        insuf = r["insufficiency"]
        assert insuf["required_core"] == 30 and insuf["required_rotation"] == 50
        assert insuf["missing_total"] == 60

        # explainable rejection reasons per cause family
        reasons = r["rejection_reasons"]
        assert any(
            x["symbol"].startswith("FAIL") and x["reason"].startswith("PROVIDER_FAILURE")
            for x in reasons
        )
        assert any(
            x["symbol"].startswith("LOW") and "BELOW_FLOOR" in x["reason"] for x in reasons
        )
        assert any(
            x["symbol"].startswith("WKND") and "INSUFFICIENT_DATA" in x["reason"]
            for x in reasons
        )

        # NOTHING persisted — no draft rows, no member rows (candidates are
        # never fabricated; a structurally-invalid draft never exists).
        async with factory() as session:
            n_versions = (
                await session.execute(select(func.count()).select_from(UniverseVersion))
            ).scalar_one()
            n_members = (
                await session.execute(select(func.count()).select_from(UniverseMembership))
            ).scalar_one()
        assert n_versions == 0
        assert n_members == 0
        await engine.dispose()

    _run(scenario())


def test_j2_single_transaction_atomicity_on_integrity_error(tmp_path):
    async def scenario():
        service, engine, factory = await _ctx(str(tmp_path / "j2.db"))
        pool = await service.create_candidate_pool(
            "pool-x", source="local",
            symbols=["A", "B", "C"],
            metadata={"symbols": ["A", "B", "C"]},
        )
        # duplicate member symbol → DB UNIQUE(universe_version_id, symbol) fires
        members = [
            {"symbol": "A", "category": "CORE", "rank": 1},
            {"symbol": "A", "category": "ROTATION", "rank": 2},
        ]
        with pytest.raises(IntegrityError):
            await service.create_universe_draft(
                pool["id"], version="dup-v1", members=members,
                core_count=1, rotation_count=1, total_count=2,
                calculated_at=CALC, effective_from=EFFECTIVE_FROM,
            )
        # the whole DRAFT snapshot rolled back — no partial rows
        async with factory() as session:
            n_versions = (
                await session.execute(select(func.count()).select_from(UniverseVersion))
            ).scalar_one()
            n_members = (
                await session.execute(select(func.count()).select_from(UniverseMembership))
            ).scalar_one()
        assert n_versions == 0
        assert n_members == 0
        await engine.dispose()

    _run(scenario())


# ===========================================================================
# K (cont.) — additive persistence behaviors
# ===========================================================================
def test_k1_eligibility_rows_include_non_members(tmp_path):
    """Phase 4 ADDITIVE change: non-member pool symbols get slot_eligibility
    rows (observability of the full input pool) while the Phase 3 member-only
    behavior is preserved when only members are supplied."""

    async def scenario():
        service, engine, factory = await _ctx(str(tmp_path / "k1.db"))
        pool = await service.create_candidate_pool(
            "pool-k1", source="local", symbols=["A", "B", "C"],
            metadata={"symbols": ["A", "B", "C"]},
        )
        members = [
            {"symbol": "A", "category": "CORE", "rank": 1},
            {"symbol": "B", "category": "ROTATION", "rank": 2},
        ]
        # non-member "C" included in slot_eligibility
        slot_elig = {
            "A": {"eligibility_status": "ELIGIBLE", "data_quality_state": "SUFFICIENT"},
            "B": {"eligibility_status": "ELIGIBLE", "data_quality_state": "SUFFICIENT"},
            "C": {"eligibility_status": "ELIGIBLE", "data_quality_state": "SUFFICIENT"},
        }
        d1 = await service.create_universe_draft(
            pool["id"], version="k1-ne", members=members,
            core_count=1, rotation_count=1, total_count=2,
            calculated_at=CALC, effective_from=EFFECTIVE_FROM,
            slot_eligibility=slot_elig,
        )
        detail1 = await service.get_universe_version_detail(d1["id"])
        assert {e["symbol"] for e in detail1["eligibility"]} == {"A", "B", "C"}

        # backward compatibility: member-only slot_eligibility → 2 rows
        d2 = await service.create_universe_draft(
            pool["id"], version="k1-mo", members=members,
            core_count=1, rotation_count=1, total_count=2,
            calculated_at=CALC, effective_from=EFFECTIVE_FROM,
            slot_eligibility={m["symbol"]: {"eligibility_status": "ELIGIBLE"}
                              for m in members},
        )
        detail2 = await service.get_universe_version_detail(d2["id"])
        assert {e["symbol"] for e in detail2["eligibility"]} == {"A", "B"}
        await engine.dispose()

    _run(scenario())


def test_k2_detail_available_for_any_version(tmp_path):
    async def scenario():
        service, engine, factory = await _ctx(str(tmp_path / "k2.db"))
        pool = await service.create_candidate_pool(
            "pool-k2", source="local", symbols=["A"],
            metadata={"symbols": ["A"]},
        )
        members = [{"symbol": "A", "category": "CORE", "rank": 1}]
        d = await service.create_universe_draft(
            pool["id"], version="k2-v1", members=members,
            core_count=1, rotation_count=0, total_count=1,
            calculated_at=CALC, effective_from=EFFECTIVE_FROM,
        )
        detail = await service.get_universe_version_detail(d["id"])
        assert detail is not None
        assert [m["symbol"] for m in detail["members"]] == ["A"]
        assert detail["scores"] == [] and detail["eligibility"] == []
        assert detail["status"] == "DRAFT"
        await engine.dispose()

    _run(scenario())


# ===========================================================================
# L — regression: legacy scanner + engine not wired into runtime
# ===========================================================================
def test_l1_legacy_defaults_unchanged():
    assert settings.UNIVERSE_SOURCE == "legacy"
    assert settings.ROTATION_ENGINE_ENABLED is False
    assert settings.CORE_SIZE == 30 and settings.ROTATION_SIZE == 50


def test_l2_legacy_nse_universe_untouched():
    from app.services.market_data.yfinance_provider import NSE_UNIVERSES

    nifty = NSE_UNIVERSES["NIFTY50"]
    assert isinstance(nifty, list)
    assert len(nifty) == 40
    assert len(set(nifty)) == 40


def test_l3_engine_not_wired_into_runtime():
    from pathlib import Path

    main_src = (
        Path(__file__).resolve().parents[1] / "app" / "main.py"
    ).read_text(encoding="utf-8")
    # main.py must not import the rotation engine / selection modules —
    # the shadow engine has no scheduler and no startup hook.
    for needle in (
        "universe_rotation_engine",
        "rotation_selection",
        "generate_draft",
    ):
        assert needle not in main_src, needle


def test_l4_from_settings_mirrors_settings_block():
    from app.core.config import settings as s

    cfg = RotationEngineConfig.from_settings()
    assert cfg.core_size == s.CORE_SIZE
    assert cfg.rotation_size == s.ROTATION_SIZE
    assert cfg.window_weeks == s.EVALUATION_WINDOW_WEEKS
    assert cfg.min_observation_sessions == s.MIN_OBSERVATION_SESSIONS
    assert cfg.liquidity_floor == s.LIQUIDITY_FLOOR
    assert cfg.volume_floor == s.VOLUME_FLOOR
    assert cfg.min_signal_sample_long == s.MIN_SIGNAL_SAMPLE_LONG
    assert cfg.min_signal_sample_short == s.MIN_SIGNAL_SAMPLE_SHORT
    assert cfg.rotation_retention_floor == s.ROTATION_RETENTION_FLOOR
    assert cfg.anti_churn_score_delta == s.ANTI_CHURN_SCORE_DELTA
    assert cfg.max_churn_per_cycle == s.MAX_CHURN_PER_CYCLE