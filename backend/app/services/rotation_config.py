"""Shared configuration for the Universe / Rotation Engine (Phase 4).

A single immutable configuration object consumed by every pure engine module
(eligibility, replay, scoring, selection) so calibration values live in ONE
place and are explicitly passed to every computation — no hidden thresholds.

Production source: ``app.core.config.settings`` (``RotationEngineConfig.from_settings``).
Tests build the dataclass directly (or via ``from_settings``) to keep fixture
thresholds explicit and deterministic.

CALIBRATION NOTICE: every numeric default below mirrors ``settings`` and is
designated TO BE CALIBRATED in Phase 2 §22 (Q1–Q3, Q5–Q7, Q14). The seven
Rotation Score weights are FIXED in ``universe_models.ROTATION_SCORE_COMPONENT_WEIGHTS``
and are intentionally NOT part of this object.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Optional

from app.core.config import settings


@dataclass(frozen=True)
class RotationEngineConfig:
    # --- target sizes (fixed architecture 30/50/80; Phase 2 §4) ------------
    core_size: int = 30
    rotation_size: int = 50
    candidate_pool_min: int = 150  # design target, NOT a requirement (§5.1)
    candidate_pool_target: int = 200  # design target centre (Phase 5B §3)
    candidate_pool_max: int = 250  # design target, NOT a requirement (§5.1)

    # --- evaluation window ---------------------------------------------------
    window_weeks: int = 4              # [TO BE CALIBRATED — Q7]
    min_observation_sessions: int = 10  # [TO BE CALIBRATED — Q7]
    expected_bars_per_session: int = 75  # 5m bars/day [TO BE CALIBRATED — Q14]
    signal_warmup_bars: int = 55         # matches backtesting.signal_start_idx

    # --- hard-filter thresholds (TO BE CALIBRATED) ----------------------------
    liquidity_floor: float = 50_000_000.0   # ₹ median daily traded value [Q1]
    volume_floor: float = 500_000.0         # mean daily volume [Q2]
    volume_cv_max: float = 2.5              # [Q2]
    atr_min_pct: float = 0.0008            # 5m-bar %ATR floor (0.08%); Phase 5A scale-corrected [Q3]
    atr_max_pct: float = 0.05               # [Q3]
    atr_opt_low_pct: float = 0.006          # [Q3]
    atr_opt_high_pct: float = 0.02          # [Q3]
    min_data_quality: float = 0.6           # S6/F7 hard floor (0..1)
    min_frequency_floor: float = 0.0        # F4 floor — 0 by design (§6 F4)

    # --- directional quality sample gates (TO BE CALIBRATED — Q5) ------------
    min_signal_sample_long: int = 10
    min_signal_sample_short: int = 10

    # --- Core stability (Phase 2 §4.1/§4.2 — sub-weights TO BE CALIBRATED) ---
    core_trading_consistency_pct: float = 99.0
    core_volume_cv_max: float = 2.5
    core_review_consecutive_periods: int = 2
    core_replacement_score_delta: float = 5.0
    core_retention_bonus: float = 0.03

    # --- anti-churn (Phase 2 §11 — TO BE CALIBRATED — Q6) --------------------
    anti_churn_score_delta: float = 5.0
    rotation_retention_floor: float = 45.0
    replacement_cooldown_weeks: int = 2
    cooldown_override_delta: float = 10.0
    max_churn_per_cycle: int = 10

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """Deterministic snapshot of every field (sorted) — used for the
        persisted ``config_hash`` so a draft is reproducible from its config."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_settings(cls, s: Optional[object] = None) -> "RotationEngineConfig":
        s = s or settings
        return cls(
            core_size=s.CORE_SIZE,
            rotation_size=s.ROTATION_SIZE,
            candidate_pool_min=s.CANDIDATE_POOL_MIN,
            candidate_pool_target=s.CANDIDATE_POOL_TARGET,
            candidate_pool_max=s.CANDIDATE_POOL_MAX,
            window_weeks=s.EVALUATION_WINDOW_WEEKS,
            min_observation_sessions=s.MIN_OBSERVATION_SESSIONS,
            expected_bars_per_session=s.EXPECTED_BARS_PER_SESSION,
            signal_warmup_bars=s.SIGNAL_WARMUP_BARS,
            liquidity_floor=s.LIQUIDITY_FLOOR,
            volume_floor=s.VOLUME_FLOOR,
            volume_cv_max=s.VOLUME_CV_MAX,
            atr_min_pct=s.ATR_MIN_PCT,
            atr_max_pct=s.ATR_MAX_PCT,
            atr_opt_low_pct=s.ATR_OPT_LOW_PCT,
            atr_opt_high_pct=s.ATR_OPT_HIGH_PCT,
            min_data_quality=s.MIN_DATA_QUALITY,
            min_signal_sample_long=s.MIN_SIGNAL_SAMPLE_LONG,
            min_signal_sample_short=s.MIN_SIGNAL_SAMPLE_SHORT,
            min_frequency_floor=s.MIN_FREQUENCY_FLOOR,
            core_trading_consistency_pct=s.CORE_TRADING_CONSISTENCY_PCT,
            core_volume_cv_max=s.CORE_VOLUME_CV_MAX,
            core_review_consecutive_periods=s.CORE_REVIEW_CONSECUTIVE_PERIODS,
            core_replacement_score_delta=s.CORE_REPLACEMENT_SCORE_DELTA,
            core_retention_bonus=s.CORE_RETENTION_BONUS,
            anti_churn_score_delta=s.ANTI_CHURN_SCORE_DELTA,
            rotation_retention_floor=s.ROTATION_RETENTION_FLOOR,
            replacement_cooldown_weeks=s.REPLACEMENT_COOLDOWN_WEEKS,
            cooldown_override_delta=s.COOLDOWN_OVERRIDE_DELTA,
            max_churn_per_cycle=s.MAX_CHURN_PER_CYCLE,
        )