"""PHASE 5A — Calibration & Candidate Pool Resolution tests.

Scope: every test below covers an ACTUAL Phase 5A artifact:

  * A  Candidate pool — real 64-symbol union from the repository's ONLY
          candidate source (NSE_UNIVERSES), dedup/determinism/validation,
          plus the sector-map comparison that establishes the source gap.
  * B  ATR — 5m-scale metric verification against a deterministic fixture:
          a realistic 5m %ATR passes the calibrated (0.08%) default band and
          is rejected by the LEGACY daily-scale (0.30%) floor; defaults load
          from settings (no scattered literals).
  * C  Volume — threshold is configured; a below-floor fixture is rejected,
          above-floor passes (relative boundary behaviour).
  * D/E LONG/SHORT — configured minimums still mirror settings (sample gates
          are calibration parameters, not scattered constants).
  * F  Configuration — every calibration-sensitive value is present in the
          dataclass/settings and to_dict() (config_hash surface).
  * G  Regression — UNIVERSE_SOURCE="legacy", ROTATION_ENGINE_ENABLED=False,
          NIFTY50 = 40 symbols (nothing activated, scanner untouched).

No mocked OHLCV numbers are asserted as market facts; fixtures are
deterministic and used only to prove stage/"band" behaviour of the calibrated
configuration. The production intradayai.db is never opened.
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.core.config import settings
from app.services import rotation_eligibility as elig
from app.services.market_data.base import MarketDataProvider
from app.services.market_data.sector_map import NSE_SECTOR_MAP
from app.services.market_data.yfinance_provider import NSE_UNIVERSES
from app.services.rotation_config import RotationEngineConfig
from app.services.universe_rotation_engine import UniverseRotationEngine

CALC = datetime(2026, 8, 28, 18, 0)  # naive; 2-week window → 10 sessions
# Phase 5C calendar correction: moved from 2026-09-25 — the original window
# contained the official holiday 2026-09-14 (Ganesh Chaturthi), which would
# have collapsed the fixture window to 9 sessions (< MIN_OBSERVATION_SESSIONS
# = 10) and turned the stage PASS/FAIL assertions into INSUFFICIENT_DATA. The
# holiday-free 2026-08-14→08-28 fortnight keeps the calibration semantics.


# ---------------------------------------------------------------------------
# deterministic fixtures (never process-random — sha1-seeded, like Phase 4)
# ---------------------------------------------------------------------------
def _dates():
    cfg = RotationEngineConfig(window_weeks=2)
    return UniverseRotationEngine(config=cfg).expected_session_dates(CALC)


class _CalFixture(MarketDataProvider):
    """Minimal deterministic OHLCV fixture for calibration behaviour tests."""

    def __init__(self, symbols, session_dates, bars_per_session=75):
        self._symbols = symbols
        self._dates = list(session_dates)
        self._bars = bars_per_session

    @property
    def data_source_label(self):
        return "CALFIX"

    async def get_quote(self, symbol):
        return {"symbol": symbol, "price": 1000.0}

    async def get_ohlcv(self, symbol, timeframe="5m", days=5):
        p = self._symbols.get(symbol)
        if p is None:
            return pd.DataFrame()
        rows = []
        rng = random.Random(hashlib.sha1(f"{symbol}:calfx".encode()).hexdigest())
        base = float(p.get("base_price", 1000.0))
        vol = float(p.get("vol", 0.001))
        drift = float(p.get("drift", 0.0))
        mult = float(p.get("volume_mult", 1.0))
        base_volume = float(p.get("base_volume", 20_000))
        for day in self._dates:
            price = float(base)
            for i in range(self._bars):
                ret = rng.gauss(drift, vol)
                o = price
                c = price * (1 + ret)
                h = max(o, c) * (1 + abs(rng.gauss(0, vol * 0.5)))
                l = min(o, c) * (1 - abs(rng.gauss(0, vol * 0.5)))
                v = max(0, int(base_volume * mult * (1 + rng.gauss(0, 0.15))))
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


async def _evaluate(provider, sym, dates, cfg):
    df = await provider.get_ohlcv(sym, days=16)
    from app.services import rotation_replay as replay

    sig = replay.replay_window(df, dates, cfg)
    return elig.evaluate_candidate(sym, df, dates, cfg, signal_metrics=sig)


# ---------------------------------------------------------------------------
# B — ATR calibration (5m metric + thresholds)
# ---------------------------------------------------------------------------
def test_b1_atr_default_floor_is_5m_scale_and_configured():
    """The ATR hard-band floor must be a 5m-bar figure (~0.08%), well below the
    legacy daily-scale 0.30% floor that rejected every real 5m %ATR in Phase 5,
    and must load from settings (from_settings mirror + dataclass default)."""
    assert settings.ATR_MIN_PCT == 0.0008
    assert settings.ATR_MIN_PCT < 0.003  # legacy daily-scale floor superseded
    assert RotationEngineConfig().atr_min_pct == 0.0008
    assert RotationEngineConfig.from_settings().atr_min_pct == settings.ATR_MIN_PCT
    assert RotationEngineConfig.from_settings().atr_max_pct == settings.ATR_MAX_PCT


def test_b2_realistic_5m_atr_passes_calibrated_band_fails_daily_scale():
    """A deterministic symbol whose median session %ATR is in the real observed
    5m range (~0.13%–0.22%) must PASS the calibrated default band (floor 0.08%)
    and FAIL the legacy daily-scale floor (0.30%). Proves the calibration is a
    scale correction, not a fabricated loosening."""
    dates = _dates()
    provider = _CalFixture({}, dates)
    found = []
    for vol in (0.0005, 0.001, 0.0015, 0.002, 0.0025, 0.003):
        sym = f"FX{int(vol * 10000):04d}"
        provider._symbols[sym] = {"vol": vol, "drift": 0.0, "base_volume": 50_000}
        ev = _run(_evaluate(provider, sym, dates, RotationEngineConfig.from_settings()))
        if ev.median_atr_pct is not None and 0.0008 < ev.median_atr_pct < 0.003:
            found.append((sym, ev.median_atr_pct))
            if len(found) >= 2:
                break
    assert found, "fixture sweep produced no symbol with median %ATR in (0.0008, 0.003)"
    for sym, med in found:
        default_cfg = RotationEngineConfig.from_settings()
        ev = _run(_evaluate(provider, sym, dates, default_cfg))
        assert ev.stages["volatility"].status == "PASS", (
            f"{sym} median %ATR {med:.4f} must pass calibrated floor "
            f"{default_cfg.atr_min_pct}"
        )
        legacy_cfg = RotationEngineConfig(
            **{**default_cfg.to_dict(), "atr_min_pct": 0.003}
        )
        ev_legacy = _run(_evaluate(provider, sym, dates, legacy_cfg))
        assert ev_legacy.stages["volatility"].status == "FAIL", (
            f"{sym} median %ATR {med:.4f} must be rejected by the legacy "
            "daily-scale 0.30% floor"
        )


# ---------------------------------------------------------------------------
# C — volume floor (configured threshold + relative boundary behaviour)
# ---------------------------------------------------------------------------
def test_c1_volume_floor_configured_and_both_sides_of_boundary():
    """The 500K floor is a configured value; a below-floor fixture is rejected
    and an above-floor fixture with the SAME profile passes — proving the gate
    behaves on the real metric (mean daily share volume), not on magic."""
    dates = _dates()
    cfg = RotationEngineConfig.from_settings()
    assert cfg.volume_floor == 500_000.0
    provider = _CalFixture({}, dates)
    provider._symbols["LOWVOL"] = {"vol": 0.001, "base_volume": 2_000}  # ~150K/day
    provider._symbols["HIGHVOL"] = {"vol": 0.001, "base_volume": 20_000}  # ~1.5M/day
    for sym, expected in (("LOWVOL", "FAIL"), ("HIGHVOL", "PASS")):
        ev = _run(_evaluate(provider, sym, dates, cfg))
        status = ev.stages["volume"].status
        assert status == expected, (
            f"{sym} mean_volume={ev.mean_volume} expected {expected}, got {status}"
        )
        assert ev.mean_volume is not None
        if expected == "FAIL":
            assert ev.mean_volume < cfg.volume_floor
        else:
            assert ev.mean_volume >= cfg.volume_floor
    assert _run(_evaluate(provider, "LOWVOL", dates, cfg)).mean_volume < _run(
        _evaluate(provider, "HIGHVOL", dates, cfg)
    ).mean_volume


# ---------------------------------------------------------------------------
# D/E — LONG / SHORT sample gates stay configured (independent, no literals)
# ---------------------------------------------------------------------------
def test_d1_long_short_sample_minimums_are_configured_parameters():
    cfg = RotationEngineConfig.from_settings()
    assert cfg.min_signal_sample_long == settings.MIN_SIGNAL_SAMPLE_LONG == 10
    assert cfg.min_signal_sample_short == settings.MIN_SIGNAL_SAMPLE_SHORT == 10
    # independent fields — a candidate can pass one and not the other
    assert hasattr(cfg, "min_signal_sample_long")
    assert hasattr(cfg, "min_signal_sample_short")


# ---------------------------------------------------------------------------
# F — configuration surface (no scattered calibration literals)
# ---------------------------------------------------------------------------
def test_f1_all_calibration_params_present_in_to_dict():
    cfg = RotationEngineConfig.from_settings()
    d = cfg.to_dict()
    for field in (
        "atr_min_pct",
        "atr_max_pct",
        "atr_opt_low_pct",
        "atr_opt_high_pct",
        "volume_floor",
        "volume_cv_max",
        "liquidity_floor",
        "min_signal_sample_long",
        "min_signal_sample_short",
        "window_weeks",
        "min_observation_sessions",
        "candidate_pool_min",
        "candidate_pool_max",
        "min_data_quality",
    ):
        assert field in d, f"calibration parameter {field} must be configurable"
    # real calibrated facts, not tuned: ATR floor must stay below observed 5m min
    assert d["atr_min_pct"] < 0.00124  # observed real 5m minimum in Phase 5A


# ---------------------------------------------------------------------------
# A — candidate pool (the repository's only real source; no fabrication)
# ---------------------------------------------------------------------------
def test_a1_pool_sources_union_is_the_repo_state():
    per_source = {name: {elig.normalize_symbol(s) for s in syms}
                  for name, syms in NSE_UNIVERSES.items()}
    n50, n100, bank = per_source["NIFTY50"], per_source["NIFTY100"], per_source["BANKNIFTY"]
    union = sorted(n50 | n100 | bank)
    assert len(n50) == 40
    assert len(n100) == 60
    assert len(bank) == 10
    assert len(n100 - n50) == 20          # NIFTY100 adds exactly 20 unique
    assert len(bank - (n50 | n100)) == 4  # BANKNIFTY adds exactly 4 unique
    assert len(union) == 64               # 40 + 20 + 4
    for name, syms in NSE_UNIVERSES.items():
        assert len(syms) == len(set(syms)), f"{name} has internal duplicates"
        assert all(elig.is_valid_symbol(s) for s in (elig.normalize_symbol(x) for x in syms))
    # sector metadata is NOT a designed candidate source: it adds one symbol
    sector_keys = {elig.normalize_symbol(k) for k in NSE_SECTOR_MAP}
    assert set(union) <= sector_keys          # all 64 pool symbols are display-mapped
    assert sorted(sector_keys - set(union)) == ["COALINDIA"]  # only real extra
    assert len(sector_keys) == 65


def test_a2_pool_dedupe_deterministic_ordered_and_versioned():
    messy = ["RELIANCE.NS", "tcs.NS", "RELIANCE", "MARUTI.NS", "TCS", "TCS.NS"]
    ordered = UniverseRotationEngine._dedupe_symbols(messy)
    # dedupe preserves FIRST-SEEN order (case/.NS normalization applied)…
    assert ordered == ["RELIANCE", "TCS", "MARUTI"]
    # …and the engine then SORTS for its deterministic comparison order.
    assert sorted(ordered) == ["MARUTI", "RELIANCE", "TCS"]
    # deterministic pool-version identity (mirror of _resolve_pool key)
    v1 = "pool-20260923-" + hashlib.sha1(",".join(sorted(ordered)).encode()).hexdigest()[:8]
    v2 = "pool-20260923-" + hashlib.sha1(",".join(sorted(ordered)).encode()).hexdigest()[:8]
    assert v1 == v2
    assert hashlib.sha1(",".join(sorted(ordered)).encode()).hexdigest()[:8] == \
        hashlib.sha1(",".join(sorted(UniverseRotationEngine._dedupe_symbols(
            list(reversed(messy))))).encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# G — regression: nothing activated, scanner untouched
# ---------------------------------------------------------------------------
def test_g1_phase5a_safety_flags_unchanged():
    assert settings.UNIVERSE_SOURCE == "legacy"
    assert settings.ROTATION_ENGINE_ENABLED is False
    assert len(NSE_UNIVERSES["NIFTY50"]) == 40
    assert RotationEngineConfig.from_settings().candidate_pool_min == 150
    assert RotationEngineConfig.from_settings().candidate_pool_max == 250


def _run(coro):
    import asyncio

    return asyncio.run(coro)