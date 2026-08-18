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
    MAX_SIMULTANEOUS_POSITIONS: int = 5
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    CORS_ORIGINS: str = '["http://localhost:3000"]'


settings = Settings()
