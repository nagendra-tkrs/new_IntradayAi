from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
import pandas as pd


class MarketDataProvider(ABC):
    @abstractmethod
    async def get_quote(self, symbol: str) -> dict:
        pass

    @abstractmethod
    async def get_ohlcv(self, symbol: str, timeframe: str = "5m", days: int = 5) -> pd.DataFrame:
        pass

    @abstractmethod
    async def get_intraday_bars(self, symbol: str, timeframe: str = "5m") -> pd.DataFrame:
        pass

    @abstractmethod
    async def get_market_status(self) -> dict:
        pass

    @abstractmethod
    async def get_instruments(self, universe: str = "NIFTY50") -> list[dict]:
        pass

    @abstractmethod
    async def get_market_index(self, index_name: str = "NIFTY") -> dict:
        pass

    @property
    @abstractmethod
    def data_source_label(self) -> str:
        pass
