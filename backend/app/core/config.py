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

    INITIAL_CAPITAL: float = 1_000_000.0
    MAX_RISK_PER_TRADE_PCT: float = 2.0
    MAX_DAILY_LOSS_PCT: float = 5.0
    MAX_TRADES_PER_DAY: int = 10
    MAX_SIMULTANEOUS_POSITIONS: int = 10
    PAPER_TRADING_MONITOR_INTERVAL_SECONDS: float = 5.0
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    CORS_ORIGINS: str = '["http://localhost:3000"]'

    GOOGLE_CLIENT_ID: str = "678365536626-2ago6n13j2a71e36jp0gi33gj2jfr30f.apps.googleusercontent.com"
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "https://trading.tksrproductservices.com/api/auth/google/callback"
    FRONTEND_URL: str = "http://localhost:3000"

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


settings = Settings()
