import random
import math
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import numpy as np
from app.services.market_data.base import MarketDataProvider
from app.core.market_session import now_ist, IST

NIFTY50_STOCKS = [
    {"symbol": "RELIANCE", "name": "Reliance Industries", "sector": "Oil & Gas", "base_price": 2450.0},
    {"symbol": "TCS", "name": "Tata Consultancy Services", "sector": "IT", "base_price": 3680.0},
    {"symbol": "HDFCBANK", "name": "HDFC Bank", "sector": "Banking", "base_price": 1620.0},
    {"symbol": "INFY", "name": "Infosys", "sector": "IT", "base_price": 1480.0},
    {"symbol": "ICICIBANK", "name": "ICICI Bank", "sector": "Banking", "base_price": 1085.0},
    {"symbol": "HINDUNILVR", "name": "Hindustan Unilever", "sector": "FMCG", "base_price": 2380.0},
    {"symbol": "SBIN", "name": "State Bank of India", "sector": "Banking", "base_price": 625.0},
    {"symbol": "BHARTIARTL", "name": "Bharti Airtel", "sector": "Telecom", "base_price": 1340.0},
    {"symbol": "KOTAKBANK", "name": "Kotak Mahindra Bank", "sector": "Banking", "base_price": 1780.0},
    {"symbol": "ITC", "name": "ITC Limited", "sector": "FMCG", "base_price": 465.0},
    {"symbol": "LT", "name": "Larsen & Toubro", "sector": "Infrastructure", "base_price": 3250.0},
    {"symbol": "AXISBANK", "name": "Axis Bank", "sector": "Banking", "base_price": 1120.0},
    {"symbol": "BAJFINANCE", "name": "Bajaj Finance", "sector": "Finance", "base_price": 6780.0},
    {"symbol": "MARUTI", "name": "Maruti Suzuki", "sector": "Auto", "base_price": 11200.0},
    {"symbol": "TATAMOTORS", "name": "Tata Motors", "sector": "Auto", "base_price": 685.0},
    {"symbol": "SUNPHARMA", "name": "Sun Pharma", "sector": "Pharma", "base_price": 1620.0},
    {"symbol": "ASIANPAINT", "name": "Asian Paints", "sector": "Consumer", "base_price": 2850.0},
    {"symbol": "HCLTECH", "name": "HCL Technologies", "sector": "IT", "base_price": 1560.0},
    {"symbol": "TITAN", "name": "Titan Company", "sector": "Consumer", "base_price": 3420.0},
    {"symbol": "ADANIENT", "name": "Adani Enterprises", "sector": "Conglomerate", "base_price": 2950.0},
    {"symbol": "WIPRO", "name": "Wipro", "sector": "IT", "base_price": 465.0},
    {"symbol": "ULTRACEMCO", "name": "UltraTech Cement", "sector": "Cement", "base_price": 9850.0},
    {"symbol": "ONGC", "name": "Oil & Natural Gas Corp", "sector": "Oil & Gas", "base_price": 245.0},
    {"symbol": "TATASTEEL", "name": "Tata Steel", "sector": "Metals", "base_price": 145.0},
    {"symbol": "NTPC", "name": "NTPC", "sector": "Power", "base_price": 340.0},
    {"symbol": "POWERGRID", "name": "Power Grid Corp", "sector": "Power", "base_price": 285.0},
    {"symbol": "BAJAJFINSV", "name": "Bajaj Finserv", "sector": "Finance", "base_price": 1620.0},
    {"symbol": "NESTLEIND", "name": "Nestle India", "sector": "FMCG", "base_price": 2450.0},
    {"symbol": "TECHM", "name": "Tech Mahindra", "sector": "IT", "base_price": 1380.0},
    {"symbol": "DRREDDY", "name": "Dr. Reddy's Labs", "sector": "Pharma", "base_price": 5820.0},
    {"symbol": "COALINDIA", "name": "Coal India", "sector": "Mining", "base_price": 420.0},
    {"symbol": "BAJAJ-AUTO", "name": "Bajaj Auto", "sector": "Auto", "base_price": 8950.0},
    {"symbol": "HEROMOTOCO", "name": "Hero MotoCorp", "sector": "Auto", "base_price": 4650.0},
    {"symbol": "DIVISLAB", "name": "Divi's Labs", "sector": "Pharma", "base_price": 3680.0},
    {"symbol": "BRITANNIA", "name": "Britannia Industries", "sector": "FMCG", "base_price": 5280.0},
    {"symbol": "CIPLA", "name": "Cipla", "sector": "Pharma", "base_price": 1420.0},
    {"symbol": "EICHERMOT", "name": "Eicher Motors", "sector": "Auto", "base_price": 4280.0},
    {"symbol": "APOLLOHOSP", "name": "Apollo Hospitals", "sector": "Healthcare", "base_price": 6250.0},
    {"symbol": "GRASIM", "name": "Grasim Industries", "sector": "Cement", "base_price": 2340.0},
    {"symbol": "JSWSTEEL", "name": "JSW Steel", "sector": "Metals", "base_price": 860.0},
]

NIFTY50_INDICES = [
    {"symbol": "NIFTY50", "name": "Nifty 50", "base_price": 23450.0},
    {"symbol": "BANKNIFTY", "name": "Bank Nifty", "base_price": 51200.0},
]


class MockMarketDataProvider(MarketDataProvider):
    def __init__(self):
        self._quote_cache: dict[str, dict] = {}

    @property
    def data_source_label(self) -> str:
        return "SIMULATED"

    def _get_stock_base(self, symbol: str) -> dict:
        for s in NIFTY50_STOCKS:
            if s["symbol"] == symbol:
                return s
        return {"symbol": symbol, "name": symbol, "sector": "Unknown", "base_price": 1000.0}

    async def get_quote(self, symbol: str) -> dict:
        base = self._get_stock_base(symbol)
        now = now_ist()
        seed = hash(symbol + str(now.hour) + str(now.minute // 5))
        rng = random.Random(seed)
        price = base["base_price"] * (1 + rng.uniform(-0.03, 0.03))
        prev_close = base["base_price"] * (1 + rng.uniform(-0.02, 0.02))
        change = price - prev_close
        change_pct = (change / prev_close) * 100 if prev_close else 0
        return {
            "symbol": symbol,
            "price": round(price, 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "volume": int(rng.uniform(500000, 5000000)),
            "high": round(price * 1.012, 2),
            "low": round(price * 0.988, 2),
            "open": round(prev_close * (1 + rng.uniform(-0.005, 0.005)), 2),
            "timestamp": now.isoformat(),
            "data_source": "SIMULATED",
        }

    async def get_intraday_bars(self, symbol: str, timeframe: str = "5m") -> pd.DataFrame:
        base = self._get_stock_base(symbol)
        now = now_ist()
        seed = hash(symbol)
        rng = random.Random(seed)
        bars = []
        base_price = base["base_price"]
        current = base_price * (1 + rng.uniform(-0.02, 0.02))
        n_bars = 75
        for i in range(n_bars):
            ts = now - timedelta(minutes=5 * (n_bars - i))
            vol_base = int(rng.uniform(30000, 200000))
            if 9 <= ts.hour <= 10:
                vol_base = int(vol_base * 1.8)
            drift = rng.gauss(0, 0.002)
            trend_component = 0.0005 * math.sin(i / 20)
            current *= (1 + drift + trend_component)
            high = current * (1 + abs(rng.gauss(0, 0.003)))
            low = current * (1 - abs(rng.gauss(0, 0.003)))
            open_p = current * (1 + rng.gauss(0, 0.001))
            bars.append({
                "timestamp": ts,
                "open": round(open_p, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(current, 2),
                "volume": vol_base,
            })
        df = pd.DataFrame(bars)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    async def get_ohlcv(self, symbol: str, timeframe: str = "5m", days: int = 5) -> pd.DataFrame:
        base = self._get_stock_base(symbol)
        all_bars = []
        for day_offset in range(days):
            seed = hash(symbol) + day_offset
            rng = random.Random(seed)
            day_date = now_ist().date() - timedelta(days=day_offset)
            price = base["base_price"] * (1 + rng.uniform(-0.05, 0.05))
            for i in range(75):
                ts = datetime(day_date.year, day_date.month, day_date.day, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i)
                vol_base = int(rng.uniform(30000, 200000))
                drift = rng.gauss(0, 0.002)
                price *= (1 + drift)
                high = price * (1 + abs(rng.gauss(0, 0.003)))
                low = price * (1 - abs(rng.gauss(0, 0.003)))
                open_p = price * (1 + rng.gauss(0, 0.001))
                all_bars.append({
                    "timestamp": ts,
                    "open": round(open_p, 2),
                    "high": round(high, 2),
                    "low": round(low, 2),
                    "close": round(price, 2),
                    "volume": vol_base,
                })
        df = pd.DataFrame(all_bars)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    async def get_market_status(self) -> dict:
        from app.core.market_session import market_session
        session = market_session()
        return {
            "session": session,
            "is_open": session in ("market_open", "trading"),
            "data_source": "SIMULATED",
            "timestamp": now_ist().isoformat(),
        }

    async def get_instruments(self, universe: str = "NIFTY50") -> list[dict]:
        return [
            {"symbol": s["symbol"], "name": s["name"], "exchange": "NSE", "sector": s["sector"], "universe": universe}
            for s in NIFTY50_STOCKS
        ]

    async def get_market_index(self, index_name: str = "NIFTY50") -> dict:
        for idx in NIFTY50_INDICES:
            if idx["symbol"] == index_name:
                now = now_ist()
                rng = random.Random(hash(index_name + str(now.hour)))
                price = idx["base_price"] * (1 + rng.uniform(-0.01, 0.01))
                change = idx["base_price"] * rng.uniform(-0.008, 0.008)
                return {
                    "symbol": idx["symbol"],
                    "name": idx["name"],
                    "price": round(price, 2),
                    "change": round(change, 2),
                    "change_pct": round(change / idx["base_price"] * 100, 2),
                    "data_source": "SIMULATED",
                }
        return {"symbol": index_name, "price": 0, "change": 0, "change_pct": 0, "data_source": "SIMULATED"}
