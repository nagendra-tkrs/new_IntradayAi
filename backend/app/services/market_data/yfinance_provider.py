import logging
import time
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from collections import OrderedDict
from urllib.parse import quote as url_quote

import pandas as pd
import requests

from app.services.market_data.base import MarketDataProvider
from app.core.market_session import now_ist, IST

logger = logging.getLogger(__name__)

_YF_BASE = "https://query2.finance.yahoo.com"
_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

NSE_UNIVERSES = {
    "NIFTY50": [
        "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
        "HINDUNILVR.NS", "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "KOTAKBANK.NS",
        "LT.NS", "AXISBANK.NS", "BAJFINANCE.NS", "MARUTI.NS", "SUNPHARMA.NS",
        "TITAN.NS", "ASIANPAINT.NS", "HCLTECH.NS", "WIPRO.NS", "TATAMOTORS.NS",
        "ULTRACEMCO.NS", "ONGC.NS", "NTPC.NS", "POWERGRID.NS", "M&M.NS",
        "JSWSTEEL.NS", "TATASTEEL.NS", "ADANIENT.NS", "ADANIPORTS.NS", "TECHM.NS",
        "BAJAJFINSV.NS", "INDUSINDBK.NS", "GRASIM.NS", "HDFCLIFE.NS", "SBILIFE.NS",
        "DIVISLAB.NS", "DRREDDY.NS", "CIPLA.NS", "APOLLOHOSP.NS", "NESTLEIND.NS",
    ],
    "NIFTY100": [
        "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
        "HINDUNILVR.NS", "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "KOTAKBANK.NS",
        "LT.NS", "AXISBANK.NS", "BAJFINANCE.NS", "MARUTI.NS", "SUNPHARMA.NS",
        "TITAN.NS", "ASIANPAINT.NS", "HCLTECH.NS", "WIPRO.NS", "TATAMOTORS.NS",
        "ULTRACEMCO.NS", "ONGC.NS", "NTPC.NS", "POWERGRID.NS", "M&M.NS",
        "JSWSTEEL.NS", "TATASTEEL.NS", "ADANIENT.NS", "ADANIPORTS.NS", "TECHM.NS",
        "BAJAJFINSV.NS", "INDUSINDBK.NS", "GRASIM.NS", "HDFCLIFE.NS", "SBILIFE.NS",
        "DIVISLAB.NS", "DRREDDY.NS", "CIPLA.NS", "APOLLOHOSP.NS", "NESTLEIND.NS",
        "TATACONSUM.NS", "HONAUT.NS", "DABUR.NS", "BRITANNIA.NS", "COLPALPH.NS",
        "EICHERMOT.NS", "HEROMOTOCO.NS", "BAJAJ-AUTO.NS", "MARICO.NS", "ICICIPRULI.NS",
        "SHRIRAMFIN.NS", "LICI.NS", "IRCTC.NS", "PIDILITIND.NS", "ASTRAL.NS",
        "TRENT.NS", "PERSISTENT.NS", "COFORGE.NS", "MPHASIS.NS", "MINDTREE.NS",
    ],
    "BANKNIFTY": [
        "HDFCBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS", "AXISBANK.NS", "SBIN.NS",
        "INDUSINDBK.NS", "BANDHANBNK.NS", "FEDERALBNK.NS", "PNB.NS", "IDFCFIRSTB.NS",
    ],
}

NSE_INDEX_MAP = {
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "^NSEI": "^NSEI",
    "^NSEBANK": "^NSEBANK",
}

TIMEFRAME_MAP = {
    "1m": ("1m", "7d"),
    "3m": ("3m", "60d"),
    "5m": ("5m", "60d"),
    "15m": ("15m", "60d"),
    "1h": ("1h", "730d"),
    "1d": ("1d", "5y"),
}

FRESHNESS_THRESHOLDS = {
    "LIVE": 300,
    "RECENT": 900,
    "DELAYED": 3600,
    "STALE": 14400,
}


class RateLimiter:
    def __init__(self, max_requests: int = 10, window_seconds: float = 1.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        while True:
            async with self._lock:
                now = time.monotonic()
                self._timestamps = [t for t in self._timestamps if now - t < self.window_seconds]
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return
                sleep_time = self.window_seconds - (now - self._timestamps[0])
            await asyncio.sleep(max(0, sleep_time))


class TTLCache:
    def __init__(self, max_size: int = 200, ttl_seconds: int = 300):
        self._cache: OrderedDict[str, tuple[float, any]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds

    def get(self, key: str) -> Optional[any]:
        if key in self._cache:
            ts, val = self._cache[key]
            if time.time() - ts < self._ttl:
                self._cache.move_to_end(key)
                return val
            del self._cache[key]
        return None

    def set(self, key: str, value: any):
        if key in self._cache:
            del self._cache[key]
        self._cache[key] = (time.time(), value)
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def invalidate(self, key: str):
        self._cache.pop(key, None)


_session = requests.Session()
_session.headers.update(_YF_HEADERS)


def _fetch_chart_sync(symbol: str, interval: str, period: str) -> dict:
    url = f"{_YF_BASE}/v8/finance/chart/{url_quote(symbol)}?interval={interval}&range={period}"
    r = _session.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    chart = data.get("chart", {})
    errors = chart.get("error")
    if errors:
        raise ValueError(f"Yahoo Finance error: {errors}")
    results = chart.get("result")
    if not results:
        raise ValueError(f"No data returned for {symbol}")
    return results[0]


def _parse_chart_to_df(result: dict) -> pd.DataFrame:
    timestamps = result.get("timestamp", [])
    if not timestamps:
        return pd.DataFrame()
    quotes = result.get("indicators", {}).get("quote", [{}])[0]
    df = pd.DataFrame({
        "Timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
        "Open": quotes.get("open", []),
        "High": quotes.get("high", []),
        "Low": quotes.get("low", []),
        "Close": quotes.get("close", []),
        "Volume": quotes.get("volume", []),
    })
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df = df[(df["Open"] > 0) & (df["High"] > 0) & (df["Low"] > 0) & (df["Close"] > 0)]
    df["Volume"] = df["Volume"].clip(lower=0).fillna(0).astype(int)
    df["Timestamp"] = df["Timestamp"].dt.tz_convert("Asia/Kolkata")
    df = df.rename(columns={"Timestamp": "timestamp", "Open": "open", "High": "high",
                             "Low": "low", "Close": "close", "Volume": "volume"})
    df = df[~df.index.duplicated(keep="first")]
    df = df.sort_index()
    return df


def _build_quote_from_chart(symbol: str, result: dict) -> dict:
    now = now_ist()
    meta = result.get("meta", {})
    price = float(meta.get("regularMarketPrice", 0))
    prev_close = float(meta.get("chartPreviousClose") or meta.get("previousClose") or price)
    change = price - prev_close if price and prev_close else 0
    change_pct = (change / prev_close * 100) if prev_close else 0
    regular_market_time = meta.get("regularMarketTime")
    last_ts = now
    if regular_market_time:
        last_ts = datetime.fromtimestamp(regular_market_time, tz=timezone.utc).astimezone(IST)
    return {
        "symbol": symbol,
        "price": round(price, 2),
        "change": round(change, 2),
        "change_pct": round(change_pct, 2),
        "open": round(float(meta.get("regularMarketDayOpen", price)), 2),
        "high": round(float(meta.get("regularMarketDayHigh", price)), 2),
        "low": round(float(meta.get("regularMarketDayLow", price)), 2),
        "prev_close": round(prev_close, 2),
        "volume": int(meta.get("regularMarketVolume", 0)),
        "fifty_two_week_high": round(float(meta.get("fiftyTwoWeekHigh", 0)), 2),
        "fifty_two_week_low": round(float(meta.get("fiftyTwoWeekLow", 0)), 2),
        "timestamp": last_ts.isoformat(),
        "received_at": now.isoformat(),
        "source": "yfinance",
        "data_age_seconds": round((now - last_ts).total_seconds(), 1) if isinstance(last_ts, datetime) else None,
    }


def _fetch_chart_and_quote_sync(symbol: str, interval: str = "5m", period: str = "5d") -> dict:
    result = _fetch_chart_sync(symbol, interval, period)
    quote = _build_quote_from_chart(symbol, result)
    df = _parse_chart_to_df(result)
    freshness = _determine_freshness_from_ts(quote.get("timestamp"))
    quote["data_status"] = freshness
    return {"quote": quote, "df": df}


def _determine_freshness_from_ts(ts_iso: Optional[str]) -> str:
    if ts_iso is None:
        return "UNAVAILABLE"
    try:
        ts = datetime.fromisoformat(ts_iso)
        now = now_ist()
        age = (now - ts).total_seconds()
        if age < FRESHNESS_THRESHOLDS["LIVE"]:
            return "LIVE"
        elif age < FRESHNESS_THRESHOLDS["RECENT"]:
            return "RECENT"
        elif age < FRESHNESS_THRESHOLDS["DELAYED"]:
            return "DELAYED"
        elif age < FRESHNESS_THRESHOLDS["STALE"]:
            return "STALE"
    except Exception:
        pass
    return "UNAVAILABLE"


class YFinanceMarketDataProvider(MarketDataProvider):
    def __init__(self):
        self._rate_limiter = RateLimiter(max_requests=10, window_seconds=1.0)
        self._quote_cache = TTLCache(max_size=200, ttl_seconds=60)
        self._bar_cache = TTLCache(max_size=200, ttl_seconds=300)
        self._instruments_cache: Optional[list[dict]] = None
        self._instruments_ts: Optional[float] = None
        self._last_data_times: dict[str, datetime] = {}
        self._data_status: dict[str, str] = {}

    @property
    def data_source_label(self) -> str:
        return "yfinance"

    def _ns_symbol(self, symbol: str) -> str:
        if symbol.endswith(".NS") or symbol.startswith("^"):
            return symbol
        return f"{symbol}.NS"

    def _strip_ns(self, symbol: str) -> str:
        if symbol.endswith(".NS"):
            return symbol[:-3]
        return symbol

    def _determine_freshness(self, timestamp: Optional[datetime]) -> str:
        if timestamp is None:
            return "UNAVAILABLE"
        now = now_ist()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=IST)
        age_seconds = (now - timestamp).total_seconds()
        if age_seconds < FRESHNESS_THRESHOLDS["LIVE"]:
            return "LIVE"
        elif age_seconds < FRESHNESS_THRESHOLDS["RECENT"]:
            return "RECENT"
        elif age_seconds < FRESHNESS_THRESHOLDS["DELAYED"]:
            return "DELAYED"
        elif age_seconds < FRESHNESS_THRESHOLDS["STALE"]:
            return "STALE"
        return "UNAVAILABLE"

    async def get_quote(self, symbol: str) -> dict:
        ns_sym = self._ns_symbol(symbol)
        cached = self._quote_cache.get(f"quote:{ns_sym}")
        if cached is not None:
            return cached
        try:
            await self._rate_limiter.acquire()
            raw = await asyncio.to_thread(_fetch_chart_sync, ns_sym, "1d", "5d")
            result = _build_quote_from_chart(symbol, raw)
            result["data_status"] = _determine_freshness_from_ts(result.get("timestamp"))
            self._quote_cache.set(f"quote:{ns_sym}", result)
            self._last_data_times[symbol] = datetime.fromisoformat(result["timestamp"])
            self._data_status[symbol] = result["data_status"]
            return result
        except Exception as e:
            logger.error(f"yfinance quote failed for {ns_sym}: {e}")
            now = now_ist()
            return {
                "symbol": symbol, "price": 0, "change": 0, "change_pct": 0,
                "open": 0, "high": 0, "low": 0, "prev_close": 0, "volume": 0,
                "timestamp": None, "received_at": now.isoformat(),
                "source": "yfinance", "data_age_seconds": None,
                "data_status": "UNAVAILABLE", "error": str(e),
            }

    async def get_intraday_bars(self, symbol: str, timeframe: str = "5m") -> pd.DataFrame:
        ns_sym = self._ns_symbol(symbol)
        cache_key = f"intraday:{ns_sym}:{timeframe}"
        cached = self._bar_cache.get(cache_key)
        if cached is not None:
            return cached

        tf_config = TIMEFRAME_MAP.get(timeframe)
        if tf_config is None:
            logger.error(f"Unsupported timeframe: {timeframe}")
            return pd.DataFrame()

        yf_interval, yf_period = tf_config
        try:
            await self._rate_limiter.acquire()
            raw = await asyncio.to_thread(_fetch_chart_sync, ns_sym, yf_interval, yf_period)
            df = _parse_chart_to_df(raw)
            if df.empty:
                return pd.DataFrame()

            meta = raw.get("meta", {})
            regular_market_time = meta.get("regularMarketTime")
            if regular_market_time:
                last_ts = datetime.fromtimestamp(regular_market_time, tz=timezone.utc).astimezone(IST)
                freshness = self._determine_freshness(last_ts)
                self._data_status[symbol] = freshness
                self._last_data_times[symbol] = last_ts

            self._bar_cache.set(cache_key, df)
            return df
        except Exception as e:
            logger.error(f"yfinance intraday failed for {ns_sym}: {e}")
            return pd.DataFrame()

    async def get_ohlcv(self, symbol: str, timeframe: str = "1d", days: int = 5) -> pd.DataFrame:
        ns_sym = self._ns_symbol(symbol)
        cache_key = f"ohlcv:{ns_sym}:{timeframe}:{days}"
        cached = self._bar_cache.get(cache_key)
        if cached is not None:
            return cached

        if timeframe == "1d":
            period = "5y" if days > 365 else "1y" if days > 60 else "60d"
            yf_interval = "1d"
        elif timeframe in ("5m", "15m", "1h"):
            period = f"{days}d"
            yf_interval = timeframe
        else:
            period = f"{days}d"
            yf_interval = "1d"

        try:
            await self._rate_limiter.acquire()
            raw = await asyncio.to_thread(_fetch_chart_sync, ns_sym, yf_interval, period)
            df = _parse_chart_to_df(raw)
            if not df.empty:
                self._bar_cache.set(cache_key, df)
            return df
        except Exception as e:
            logger.error(f"yfinance OHLCV failed for {ns_sym}: {e}")
            return pd.DataFrame()

    async def get_market_status(self) -> dict:
        now = now_ist()
        t = now.hour * 60 + now.minute
        is_weekday = now.weekday() < 5

        if not is_weekday:
            session = "closed"
            is_open = False
        elif t < 555:
            session = "pre_market"
            is_open = False
        elif t <= 915:
            session = "trading"
            is_open = True
        elif t <= 930:
            session = "closing"
            is_open = True
        else:
            session = "closed"
            is_open = False

        return {
            "session": session,
            "is_open": is_open,
            "data_source": "yfinance",
            "timestamp": now.isoformat(),
        }

    async def get_instruments(self, universe: str = "NIFTY50") -> list[dict]:
        now = time.time()
        if self._instruments_cache is not None and self._instruments_ts and (now - self._instruments_ts) < 3600:
            return self._instruments_cache

        symbols = NSE_UNIVERSES.get(universe, NSE_UNIVERSES["NIFTY50"])
        instruments = []
        for sym in symbols:
            clean = self._strip_ns(sym)
            instruments.append({
                "symbol": clean,
                "name": clean,
                "exchange": "NSE",
                "sector": "Unknown",
                "yfinance_symbol": sym,
            })

        self._instruments_cache = instruments
        self._instruments_ts = now
        return instruments

    async def get_market_index(self, index_name: str = "NIFTY50") -> dict:
        yf_sym = NSE_INDEX_MAP.get(index_name, NSE_INDEX_MAP.get("^NSEI", "^NSEI"))
        cache_key = f"index:{yf_sym}"
        cached = self._quote_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            await self._rate_limiter.acquire()
            raw = await asyncio.to_thread(_fetch_chart_sync, yf_sym, "1d", "5d")
            result = _build_quote_from_chart(index_name, raw)
            result["name"] = index_name
            result["data_status"] = _determine_freshness_from_ts(result.get("timestamp"))
            self._quote_cache.set(cache_key, result)
            return result
        except Exception as e:
            logger.error(f"yfinance index failed for {yf_sym}: {e}")
            now = now_ist()
            return {
                "name": index_name, "symbol": yf_sym,
                "price": 0, "change": 0, "change_pct": 0,
                "timestamp": None, "received_at": now.isoformat(),
                "source": "yfinance", "data_status": "UNAVAILABLE", "error": str(e),
            }

    def get_cached_status(self) -> dict:
        return {
            "mode": "yfinance",
            "data_source": "yfinance (direct API)",
            "instruments_loaded": self._instruments_cache is not None,
            "symbols_cached": len(self._quote_cache._cache),
            "bars_cached": len(self._bar_cache._cache),
        }
