import logging
from datetime import timedelta
import pandas as pd
from app.services.market_data.base import MarketDataProvider
from app.core.market_session import now_ist

logger = logging.getLogger(__name__)


class HistoricalMarketDataProvider(MarketDataProvider):
    def __init__(self, data_dir: str = "./data/historical"):
        self._data_dir = data_dir
        self._cache: dict[str, pd.DataFrame] = {}

    @property
    def data_source_label(self) -> str:
        return "HISTORICAL"

    async def get_quote(self, symbol: str) -> dict:
        df = await self.get_intraday_bars(symbol)
        if df is not None and len(df) > 0:
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else last
            price = float(last["close"])
            prev_close = float(prev["close"])
            return {
                "symbol": symbol,
                "price": price,
                "change": round(price - prev_close, 2),
                "change_pct": round(((price - prev_close) / prev_close * 100) if prev_close else 0, 2),
                "volume": int(last.get("volume", 0)),
                "high": float(last.get("high", price)),
                "low": float(last.get("low", price)),
                "open": float(last.get("open", price)),
                "timestamp": str(last.get("timestamp", "")),
                "data_source": "HISTORICAL",
            }
        return {"symbol": symbol, "price": 0, "change": 0, "change_pct": 0, "volume": 0, "data_source": "HISTORICAL"}

    async def get_intraday_bars(self, symbol: str, timeframe: str = "5m") -> pd.DataFrame:
        if symbol in self._cache:
            return self._cache[symbol]
        return pd.DataFrame()

    async def get_ohlcv(self, symbol: str, timeframe: str = "5m", days: int = 5) -> pd.DataFrame:
        return await self.get_intraday_bars(symbol, timeframe)

    async def get_market_status(self) -> dict:
        from app.core.market_session import market_session
        session = market_session()
        return {"session": session, "is_open": False, "data_source": "HISTORICAL"}

    async def get_instruments(self, universe: str = "NIFTY50") -> list[dict]:
        return []

    async def get_market_index(self, index_name: str = "NIFTY50") -> dict:
        return {"symbol": index_name, "price": 0, "change": 0, "change_pct": 0, "data_source": "HISTORICAL"}
