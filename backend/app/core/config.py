import os
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    DATABASE_URL: str = "sqlite+aiosqlite:///./intradayai.db"
    JWT_SECRET: str = "change-this-to-a-random-secret"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440

    # yfinance | live | historical | mock
    MARKET_DATA_MODE: str = "yfinance"
    MARKET_DATA_PROVIDER: str = "yfinance"

    KITE_API_KEY: str = ""
    KITE_API_SECRET: str = ""
    KITE_ACCESS_TOKEN: str = ""

    REDIS_URL: str = "redis://localhost:6379/0"

    # yfinance settings
    YF_RATE_LIMIT_REQUESTS: int = 5
    YF_RATE_LIMIT_WINDOW: float = 1.0
    YF_CACHE_TTL_QUOTES: int = 60
    YF_CACHE_TTL_BARS: int = 300
    YF_DEFAULT_UNIVERSE: str = "NIFTY50"
    YF_DEFAULT_TIMEFRAME: str = "5m"
    YF_MAX_REQUESTS_PER_BATCH: int = 10

    INITIAL_CAPITAL: float = 10_000.0
    MAX_RISK_PER_TRADE_PCT: float = 2.0
    MAX_DAILY_LOSS_PCT: float = 5.0
    MAX_TRADES_PER_DAY: int = 10
    MAX_SIMULTANEOUS_POSITIONS: int = 10
    PAPER_TRADING_MONITOR_INTERVAL_SECONDS: float = 5.0
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8020
    CORS_ORIGINS: str = '["http://localhost:3005"]'

    GOOGLE_CLIENT_ID: str = "678365536626-2ago6n13j2a71e36jp0gi33gj2jfr30f.apps.googleusercontent.com"
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "https://trading.tksrproductservices.com/api/auth/google/callback"
    FRONTEND_URL: str = "http://localhost:3005"

    # Universe Manager (Phase 3) — persistence-layer settings. Defaults preserve
    # the legacy 40-stock behavior byte-for-byte: UNIVERSE_SOURCE=legacy keeps
    # the scanner on the hardcoded NIFTY50 path; auto-approval is OFF (and is
    # not even implemented in Phase 3 — the settings document the Q13 ss5
    # design). AUTO_APPROVE_AFTER_CYCLES is [TO BE CALIBRATED].
    UNIVERSE_SOURCE: str = "legacy"
    AUTO_APPROVE_IF_VALID: bool = False
    AUTO_APPROVE_AFTER_CYCLES: int = 4

    # ------------------------------------------------------------------
    # Universe / Rotation Engine (Phase 4) — DRAFT-only engine settings.
    #
    # The engine computes and persists a DRAFT universe ONLY. It never
    # approves, never activates, and never switches the live scanner: the
    # scanner stays on the legacy 40-stock path (UNIVERSE_SOURCE="legacy"
    # above remains the default; nothing in the engine reads or replaces
    # NSE_UNIVERSES). ROTATION_ENGINE_ENABLED gates execution and defaults
    # to False — no scheduler and no startup hook invokes the engine.
    #
    # CALIBRATION NOTICE (Phase 2 §22 Q1–Q3/Q5–Q7/Q14): every numeric
    # threshold below is a documented CALIBRATION PLACEHOLDER. It is
    # configurable here and is NOT a production-approved value. The seven
    # Rotation Score weights are FIXED by Phase 2 §6 and live in
    # universe_models.ROTATION_SCORE_COMPONENT_WEIGHTS — they are never
    # overridden here.
    ROTATION_ENGINE_ENABLED: bool = False
    CORE_SIZE: int = 30
    ROTATION_SIZE: int = 50

    # Active live trading universe selection (Phase 1). Conceptually SEPARATE
    # from ROTATION_ENGINE_ENABLED: this setting only decides which universe the
    # live scanner consumes (resolved by app.services.active_universe); it never
    # schedules or triggers rotation-engine calculation.
    #
    #   "rotation_80" (default) — the ACTIVE 30 Core + 50 Rotation universe
    #     version persisted in universe_versions/universe_members. If no valid
    #     80-stock ACTIVE version exists the scanner FAILS CLOSED with
    #     ACTIVE_UNIVERSE_UNAVAILABLE (never silently falls back to 40).
    #   "legacy_NIFTY50"        — restore the previous 40-stock NIFTY50 scanner
    #     behavior (legacy provider path; maintained for rollback only).
    LIVE_UNIVERSE: str = "rotation_80"
    CANDIDATE_POOL_MIN: int = 150  # design target, NOT a requirement (Phase 2 §5.1)
    CANDIDATE_POOL_TARGET: int = 200  # design target centre (Phase 5B §3)
    CANDIDATE_POOL_MAX: int = 250  # design target, NOT a requirement (Phase 2 §5.1)

    EVALUATION_WINDOW_WEEKS: int = 4      # [TO BE CALIBRATED — Q7]
    MIN_OBSERVATION_SESSIONS: int = 10    # [TO BE CALIBRATED — Q7]
    EXPECTED_BARS_PER_SESSION: int = 75   # 5m bars/day [TO BE CALIBRATED — Q14]
    SIGNAL_WARMUP_BARS: int = 55          # matches backtesting.signal_start_idx / scanner len<55

    LIQUIDITY_FLOOR: float = 50_000_000.0  # ₹ median daily traded value [TO BE CALIBRATED — Q1]
    VOLUME_FLOOR: float = 500_000.0        # mean daily share volume [TO BE CALIBRATED — Q2]
    VOLUME_CV_MAX: float = 2.5             # [TO BE CALIBRATED — Q2]
    ATR_MIN_PCT: float = 0.0008            # 5m-bar %ATR floor (0.08%); Phase 5A scale-corrected [Q3]
    ATR_MAX_PCT: float = 0.05              # 5.00% band high [TO BE CALIBRATED — Q3]
    ATR_OPT_LOW_PCT: float = 0.006         # plateau low [TO BE CALIBRATED — Q3]
    ATR_OPT_HIGH_PCT: float = 0.02         # plateau high [TO BE CALIBRATED — Q3]

    MIN_DATA_QUALITY: float = 0.6          # S6/F7 hard floor (0..1) [TO BE CALIBRATED]
    MIN_SIGNAL_SAMPLE_LONG: int = 10       # LONG evidence gate [TO BE CALIBRATED — Q5]
    MIN_SIGNAL_SAMPLE_SHORT: int = 10      # SHORT evidence gate [TO BE CALIBRATED — Q5]
    MIN_FREQUENCY_FLOOR: float = 0.0       # F4 floor — 0 by design (Phase 2 §6 F4)

    CORE_TRADING_CONSISTENCY_PCT: float = 99.0   # sessions with volume>0 [TO BE CALIBRATED — §4.1]
    CORE_VOLUME_CV_MAX: float = 2.5              # core-only stability CV [TO BE CALIBRATED — §4.1]
    CORE_REVIEW_CONSECUTIVE_PERIODS: int = 2     # §4.3 removal path (b)
    CORE_REPLACEMENT_SCORE_DELTA: float = 5.0    # [TO BE CALIBRATED — Q6]
    CORE_RETENTION_BONUS: float = 0.03           # stability bonus for retained Core [TO BE CALIBRATED]

    ANTI_CHURN_SCORE_DELTA: float = 5.0          # [TO BE CALIBRATED — Q6]
    ROTATION_RETENTION_FLOOR: float = 45.0       # [TO BE CALIBRATED — Q6]
    REPLACEMENT_COOLDOWN_WEEKS: int = 2          # [TO BE CALIBRATED — Q6]
    COOLDOWN_OVERRIDE_DELTA: float = 10.0        # [TO BE CALIBRATED — Q6]
    MAX_CHURN_PER_CYCLE: int = 10                # [TO BE CALIBRATED — Q6]

    # ------------------------------------------------------------------
    # Profit Capture layer (paper trading) — controlled profit-taking.
    #
    # Conservative defaults; every value is configurable via .env. Semantics:
    #   T1_EXIT_PERCENT           % of the position that exits at Target 1
    #                             (50% = half stays running). 100 disables the
    #                             partial and restores the legacy full-close.
    #   T1_PROTECT_MODE           stop-loss handling the instant T1 executes:
    #                             MOVE_SL_TO_ENTRY (default; stop = entry, never
    #                             loosened), MOVE_SL_TO_ENTRY_PLUS_BUFFER
    #                             (entry +/- buffer*ATR), KEEP_EXISTING_SL.
    #   T1_PROTECT_BUFFER_ATR     buffer (in ATRs) for the PLUS_BUFFER mode.
    #   TRAILING_MODE             trailing stop on the REMAINING quantity:
    #                             RANGE (mult * |T1-entry|), PERCENT (% of the
    #                             observed price), ATR (mult * ATR), NONE.
    #   TRAILING_RANGE_MULT / TRAILING_PERCENT / TRAILING_ATR_MULT  distances.
    # A trailing/protective stop NEVER loosens (LONG only rises, SHORT only
    # falls) and an SL hit after a T1 partial is recorded as TRAILING_STOP.
    T1_EXIT_PERCENT: float = 50.0
    T1_PROTECT_MODE: str = "MOVE_SL_TO_ENTRY"
    T1_PROTECT_BUFFER_ATR: float = 0.5
    TRAILING_MODE: str = "RANGE"
    TRAILING_RANGE_MULT: float = 0.5
    TRAILING_PERCENT: float = 1.0
    TRAILING_ATR_MULT: float = 1.0

    # ------------------------------------------------------------------
    # Signal setup ATR multipliers (entry/stop/T1/T2 geometry). These mirror
    # the current hardcoded semantics exactly by default:
    #   stop = price +/- stop_mult*ATR ; T1 = price +/- target_mult*ATR ;
    #   T2   = price +/- (target_mult + 1.0)*ATR   (i.e. T2 = T1 + 1 ATR)
    # The T2/T1/SL *strategy-version* fields (strategy_config.py) override
    # these per version when set; v1..v4 output stays byte-identical.
    NORMAL_SL_ATR_MULTIPLIER: float = 1.5
    STRONG_SL_ATR_MULTIPLIER: float = 1.5
    NORMAL_T1_ATR_MULTIPLIER: float = 2.0
    STRONG_T1_ATR_MULTIPLIER: float = 3.0
    NORMAL_T2_ATR_MULTIPLIER: float = 3.0   # = normal T1 + 1 ATR (legacy)
    STRONG_T2_ATR_MULTIPLIER: float = 4.0   # = strong T1 + 1 ATR (legacy)

    # ------------------------------------------------------------------
    # Adaptive stop-loss seam. Default-off: ADAPTIVE_SL_MODE="FIXED" keeps the
    # fixed ATR stop (NORMAL/STRONG_SL_ATR_MULTIPLIER). "ATR_SNAP" is a seam for
    # future per-bar ATR adjustment and changes NO behavior today. Position
    # sizing already shrinks quantity as SL distance grows, so account risk per
    # trade stays capped regardless of the mode.
    ADAPTIVE_SL_MODE: str = "FIXED"
    ADAPTIVE_SL_ATR_MULT: float = 1.5

    # ------------------------------------------------------------------
    # Quality-based position sizing (paper trading). Effective risk per trade =
    #   BASE_RISK_PER_TRADE_PCT * setup_quality_multiplier * strength_multiplier
    # capped at MAX_RISK_PER_TRADE_PCT (above). Conservative defaults: base =
    # cap = 2.0, every quality multi 1.0, REJECTED/WEAK multi 0.0 (no trade).
    # Because the multiplier defaults are all 1.0 this changes NO sizing unless
    # a caller explicitly supplies setup_quality/signal_strength metadata.
    BASE_RISK_PER_TRADE_PCT: float = 2.0
    STRONG_SIGNAL_RISK_MULTIPLIER: float = 1.0
    QUALITY_RISK_MULTIPLIERS: str = (
        '{"REJECTED": 0.0, "WEAK": 0.0, "NORMAL": 1.0, "QUALIFIED": 1.0, "PREMIUM": 1.0}'
    )

    # ------------------------------------------------------------------
    # 24-Hour market-context experiment (A/B testing seam).
    #
    #   USE_24H_CONTEXT=false (default) — the scanner behaves EXACTLY as today:
    #   evaluate_signal receives no context_24h and the engine is
    #   byte-identical. No trade ever originates from the 24H variant.
    #
    #   USE_24H_CONTEXT=true — every scanned symbol ALSO computes a shadow
    #   "24H context" recommendation (Mode B) from the last
    #   CONTEXT_24H_WINDOW_HOURS clock hours of 5-minute candles ending at the
    #   decision candle. It is fed into the SAME engine as additional
    #   market-context evidence and is recorded for comparison; the Mode A
    #   (current) signal still drives all paper orders.
    #
    # CONTEXT_24H_WINDOW_HOURS is by definition CLOCK hours (never "24 trading
    # hours"). No candle is manufactured for closed market time: the window
    # simply contains whatever real candles Yahoo returns within it.
    USE_24H_CONTEXT: bool = False
    CONTEXT_24H_WINDOW_HOURS: float = 24.0


settings = Settings()
