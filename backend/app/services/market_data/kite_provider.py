import asyncio
import logging
import time
import json
from datetime import datetime, timedelta, date
from typing import Optional
from collections import defaultdict
import pandas as pd
import numpy as np
from app.services.market_data.base import MarketDataProvider
from app.core.market_session import now_ist, IST

logger = logging.getLogger(__name__)

NIFTY50_INSTRUMENTS = {
    "RELIANCE": {"name": "Reliance Industries", "sector": "Oil & Gas", "exchange": "NSE"},
    "TCS": {"name": "Tata Consultancy Services", "sector": "IT", "exchange": "NSE"},
    "HDFCBANK": {"name": "HDFC Bank", "sector": "Banking", "exchange": "NSE"},
    "INFY": {"name": "Infosys", "sector": "IT", "exchange": "NSE"},
    "ICICIBANK": {"name": "ICICI Bank", "sector": "Banking", "exchange": "NSE"},
    "HINDUNILVR": {"name": "Hindustan Unilever", "sector": "FMCG", "exchange": "NSE"},
    "SBIN": {"name": "State Bank of India", "sector": "Banking", "exchange": "NSE"},
    "BHARTIARTL": {"name": "Bharti Airtel", "sector": "Telecom", "exchange": "NSE"},
    "KOTAKBANK": {"name": "Kotak Mahindra Bank", "sector": "Banking", "exchange": "NSE"},
    "ITC": {"name": "ITC Limited", "sector": "FMCG", "exchange": "NSE"},
    "LT": {"name": "Larsen & Toubro", "sector": "Infrastructure", "exchange": "NSE"},
    "AXISBANK": {"name": "Axis Bank", "sector": "Banking", "exchange": "NSE"},
    "BAJFINANCE": {"name": "Bajaj Finance", "sector": "Finance", "exchange": "NSE"},
    "MARUTI": {"name": "Maruti Suzuki", "sector": "Auto", "exchange": "NSE"},
    "TATAMOTORS": {"name": "Tata Motors", "sector": "Auto", "exchange": "NSE"},
    "SUNPHARMA": {"name": "Sun Pharma", "sector": "Pharma", "exchange": "NSE"},
    "ASIANPAINT": {"name": "Asian Paints", "sector": "Consumer", "exchange": "NSE"},
    "HCLTECH": {"name": "HCL Technologies", "sector": "IT", "exchange": "NSE"},
    "TITAN": {"name": "Titan Company", "sector": "Consumer", "exchange": "NSE"},
    "ADANIENT": {"name": "Adani Enterprises", "sector": "Conglomerate", "exchange": "NSE"},
    "WIPRO": {"name": "Wipro", "sector": "IT", "exchange": "NSE"},
    "ULTRACEMCO": {"name": "UltraTech Cement", "sector": "Cement", "exchange": "NSE"},
    "ONGC": {"name": "Oil & Natural Gas Corp", "sector": "Oil & Gas", "exchange": "NSE"},
    "TATASTEEL": {"name": "Tata Steel", "sector": "Metals", "exchange": "NSE"},
    "NTPC": {"name": "NTPC", "sector": "Power", "exchange": "NSE"},
    "POWERGRID": {"name": "Power Grid Corp", "sector": "Power", "exchange": "NSE"},
    "BAJAJFINSV": {"name": "Bajaj Finserv", "sector": "Finance", "exchange": "NSE"},
    "NESTLEIND": {"name": "Nestle India", "sector": "FMCG", "exchange": "NSE"},
    "TECHM": {"name": "Tech Mahindra", "sector": "IT", "exchange": "NSE"},
    "DRREDDY": {"name": "Dr. Reddy's Labs", "sector": "Pharma", "exchange": "NSE"},
    "COALINDIA": {"name": "Coal India", "sector": "Mining", "exchange": "NSE"},
    "BAJAJ-AUTO": {"name": "Bajaj Auto", "sector": "Auto", "exchange": "NSE"},
    "HEROMOTOCO": {"name": "Hero MotoCorp", "sector": "Auto", "exchange": "NSE"},
    "DIVISLAB": {"name": "Divi's Labs", "sector": "Pharma", "exchange": "NSE"},
    "BRITANNIA": {"name": "Britannia Industries", "sector": "FMCG", "exchange": "NSE"},
    "CIPLA": {"name": "Cipla", "sector": "Pharma", "exchange": "NSE"},
    "EICHERMOT": {"name": "Eicher Motors", "sector": "Auto", "exchange": "NSE"},
    "APOLLOHOSP": {"name": "Apollo Hospitals", "sector": "Healthcare", "exchange": "NSE"},
    "GRASIM": {"name": "Grasim Industries", "sector": "Cement", "exchange": "NSE"},
    "JSWSTEEL": {"name": "JSW Steel", "sector": "Metals", "exchange": "NSE"},
}


class TickStore:
    def __init__(self, max_age_seconds: int = 300):
        self._ticks: dict[str, dict] = {}
        self._tick_history: dict[str, list[dict]] = defaultdict(list)
        self._candles: dict[str, list[dict]] = defaultdict(list)
        self._subscriptions: set[str] = set()
        self._max_age = max_age_seconds
        self._last_update: dict[str, float] = {}
        self._connection_status: str = "disconnected"
        self._last_tick_time: Optional[datetime] = None
        self._data_latency_ms: Optional[float] = None
        self._tick_count: int = 0
        self._validation_errors: int = 0

    @property
    def connection_status(self) -> str:
        return self._connection_status

    @property
    def last_tick_time(self) -> Optional[datetime]:
        return self._last_tick_time

    @property
    def data_latency_ms(self) -> Optional[float]:
        return self._data_latency_ms

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def validation_errors(self) -> int:
        return self._validation_errors

    def is_stale(self, symbol: str) -> bool:
        if symbol not in self._last_update:
            return True
        return (time.time() - self._last_update[symbol]) > self._max_age

    def store_tick(self, symbol: str, tick: dict) -> bool:
        if not self._validate_tick(tick):
            self._validation_errors += 1
            return False
        now = time.time()
        self._ticks[symbol] = tick
        self._last_update[symbol] = now
        self._last_tick_time = now_ist()
        self._tick_count += 1
        if "data_timestamp" in tick and tick["data_timestamp"]:
            try:
                server_now = time.time()
                tick_ts = float(tick["data_timestamp"])
                self._data_latency_ms = (server_now - tick_ts) * 1000
            except (ValueError, TypeError):
                pass
        self._tick_history[symbol].append({**tick, "_received": now})
        max_history = 5000
        if len(self._tick_history[symbol]) > max_history:
            self._tick_history[symbol] = self._tick_history[symbol][-max_history:]
        return True

    def get_latest_tick(self, symbol: str) -> Optional[dict]:
        return self._ticks.get(symbol)

    def get_tick_history(self, symbol: str, limit: int = 100) -> list[dict]:
        return self._tick_history.get(symbol, [])[-limit:]

    def _validate_tick(self, tick: dict) -> bool:
        price = tick.get("last_price", 0)
        if price <= 0:
            return False
        if price > 1000000:
            return False
        volume = tick.get("volume", 0)
        if volume < 0:
            return False
        if "instrument_token" not in tick:
            return False
        return True


class CandleAggregator:
    def __init__(self):
        self._current_candle: dict[str, dict] = {}
        self._completed_candles: dict[str, list[dict]] = defaultdict(list)

    def on_tick(self, symbol: str, tick: dict, timeframe_minutes: int = 5) -> Optional[dict]:
        price = tick.get("last_price", 0)
        volume = tick.get("volume", 0)
        ts = tick.get("timestamp") or now_ist()
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except ValueError:
                ts = now_ist()
        candle_start = self._get_candle_start(ts, timeframe_minutes)
        key = f"{symbol}_{timeframe_minutes}"
        current = self._current_candle.get(key)
        if current is None or current["timestamp"] != candle_start:
            completed = None
            if current is not None:
                completed = current.copy()
                self._completed_candles[symbol].append(current)
                max_candles = 2000
                if len(self._completed_candles[symbol]) > max_candles:
                    self._completed_candles[symbol] = self._completed_candles[symbol][-max_candles:]
            self._current_candle[key] = {
                "timestamp": candle_start,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "symbol": symbol,
            }
            return completed
        else:
            current["high"] = max(current["high"], price)
            current["low"] = min(current["low"], price)
            current["close"] = price
            current["volume"] = volume
            return None

    def get_completed_candles(self, symbol: str, limit: int = 200) -> list[dict]:
        return self._completed_candles.get(symbol, [])[-limit:]

    def get_current_candle(self, symbol: str, timeframe_minutes: int = 5) -> Optional[dict]:
        key = f"{symbol}_{timeframe_minutes}"
        return self._current_candle.get(key)

    def get_all_candles_as_df(self, symbol: str, timeframe_minutes: int = 5) -> pd.DataFrame:
        completed = self.get_completed_candles(symbol)
        current = self.get_current_candle(symbol, timeframe_minutes)
        all_candles = list(completed)
        if current:
            all_candles.append(current)
        if not all_candles:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(all_candles)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    @staticmethod
    def _get_candle_start(ts: datetime, timeframe_minutes: int) -> datetime:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        minutes = (ts.hour * 60 + ts.minute)
        candle_start_minutes = (minutes // timeframe_minutes) * timeframe_minutes
        hour = candle_start_minutes // 60
        minute = candle_start_minutes % 60
        return ts.replace(hour=hour, minute=minute, second=0, microsecond=0)


class DataConnectionManager:
    def __init__(self):
        self._connected: bool = False
        self._authenticated: bool = False
        self._last_connect_time: Optional[datetime] = None
        self._last_disconnect_time: Optional[datetime] = None
        self._reconnect_count: int = 0
        self._max_reconnect_attempts: int = 10
        self._reconnect_delay: float = 1.0
        self._max_reconnect_delay: float = 60.0

    @property
    def status(self) -> str:
        if self._connected and self._authenticated:
            return "connected"
        elif self._connected:
            return "authenticating"
        else:
            return "disconnected"

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    def on_connect(self):
        self._connected = True
        self._last_connect_time = now_ist()
        self._reconnect_count = 0
        self._reconnect_delay = 1.0
        logger.info("WebSocket connected")

    def on_disconnect(self):
        self._connected = False
        self._authenticated = False
        self._last_disconnect_time = now_ist()
        self._reconnect_count += 1
        logger.warning(f"WebSocket disconnected (reconnect #{self._reconnect_count})")

    def on_auth(self):
        self._authenticated = True
        logger.info("WebSocket authenticated")

    def get_next_reconnect_delay(self) -> float:
        delay = self._reconnect_delay
        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
        return delay

    def should_attempt_reconnect(self) -> bool:
        return self._reconnect_count < self._max_reconnect_attempts


class KiteConnectProvider(MarketDataProvider):
    def __init__(self, api_key: str, api_secret: str = "", access_token: str = ""):
        self._api_key = api_key
        self._api_secret = api_secret
        self._access_token = access_token
        self._kite = None
        self._ws = None
        self._tick_store = TickStore(max_age_seconds=300)
        self._candle_aggregator = CandleAggregator()
        self._conn_manager = DataConnectionManager()
        self._instrument_tokens: dict[str, int] = {}
        self._token_to_symbol: dict[int, str] = {}
        self._instruments_loaded = False
        self._historical_cache: dict[str, pd.DataFrame] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def data_source_label(self) -> str:
        if self._conn_manager.status == "connected":
            return "LIVE"
        elif self._access_token:
            return "LIVE (Disconnected)"
        else:
            return "NO DATA"

    @property
    def connection_status(self) -> str:
        return self._conn_manager.status

    @property
    def tick_store(self) -> TickStore:
        return self._tick_store

    @property
    def candle_aggregator(self) -> CandleAggregator:
        return self._candle_aggregator

    @property
    def connection_manager(self) -> DataConnectionManager:
        return self._conn_manager

    def _get_kite(self):
        if self._kite is None:
            from kiteconnect import KiteConnect
            self._kite = KiteConnect(api_key=self._api_key)
            if self._access_token:
                self._kite.set_access_token(self._access_token)
        return self._kite

    async def connect(self) -> bool:
        try:
            if not self._api_key or not self._access_token:
                logger.error("Kite Connect credentials not configured")
                return False
            kite = self._get_kite()
            profile = kite.profile()
            logger.info(f"Kite authenticated as: {profile.get('user_name', 'unknown')}")
            self._conn_manager.on_connect()
            self._conn_manager.on_auth()
            await self._load_instruments()
            return True
        except Exception as e:
            logger.error(f"Kite connection failed: {e}")
            self._conn_manager.on_disconnect()
            return False

    def connect_sync(self) -> bool:
        try:
            if not self._api_key or not self._access_token:
                logger.error("Kite Connect credentials not configured")
                return False
            kite = self._get_kite()
            profile = kite.profile()
            logger.info(f"Kite authenticated as: {profile.get('user_name', 'unknown')}")
            self._conn_manager.on_connect()
            self._conn_manager.on_auth()
            return True
        except Exception as e:
            logger.error(f"Kite connection failed: {e}")
            self._conn_manager.on_disconnect()
            return False

    async def _load_instruments(self):
        if self._instruments_loaded:
            return
        try:
            kite = self._get_kite()
            instruments = kite.instruments("NSE")
            for inst in instruments:
                symbol = inst.get("tradingsymbol", "")
                token = inst.get("instrument_token")
                if symbol and token and inst.get("exchange") == "NSE":
                    self._instrument_tokens[symbol] = token
                    self._token_to_symbol[token] = symbol
            nifty_tokens = []
            for sym in NIFTY50_INSTRUMENTS:
                if sym in self._instrument_tokens:
                    nifty_tokens.append(self._instrument_tokens[sym])
            self._instrument_tokens["NIFTY50"] = 256265
            self._instrument_tokens["BANKNIFTY"] = 260105
            self._token_to_symbol[256265] = "NIFTY50"
            self._token_to_symbol[260105] = "BANKNIFTY"
            self._instruments_loaded = True
            logger.info(f"Loaded {len(self._instrument_tokens)} instruments")
        except Exception as e:
            logger.error(f"Failed to load instruments: {e}")
            for sym, info in NIFTY50_INSTRUMENTS.items():
                self._instrument_tokens[sym] = hash(sym) % 900000 + 100000
                self._token_to_symbol[self._instrument_tokens[sym]] = sym

    def start_websocket(self):
        if self._loop is None:
            try:
                self._loop = asyncio.get_event_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)

        try:
            from kiteconnect import KiteTicker
            self._ws = KiteTicker(self._api_key, self._access_token)
            self._ws.on_connect = self._ws_on_connect
            self._ws.on_close = self._ws_on_close
            self._ws.on_error = self._ws_on_error
            self._ws.on_ticks = self._ws_on_ticks
            self._ws.on_reconnect = self._ws_on_reconnect
            self._ws.connect(threaded=True)
            logger.info("WebSocket thread started")
        except ImportError:
            logger.error("kiteconnect not installed. Run: pip install kiteconnect")
        except Exception as e:
            logger.error(f"WebSocket start failed: {e}")

    def stop_websocket(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._conn_manager.on_disconnect()

    def _ws_on_connect(self, ws, response):
        self._conn_manager.on_connect()
        tokens = list(self._instrument_tokens.values())
        ws.set_tokens(tokens)
        logger.info(f"WebSocket subscribed to {len(tokens)} instruments")

    def _ws_on_close(self, ws, code, reason):
        self._conn_manager.on_disconnect()
        logger.warning(f"WebSocket closed: {code} {reason}")
        if self._conn_manager.should_attempt_reconnect():
            delay = self._conn_manager.get_next_reconnect_delay()
            logger.info(f"Reconnecting in {delay}s...")
            if self._loop:
                asyncio.run_coroutine_threadsafe(self._reconnect_after_delay(delay), self._loop)

    def _ws_on_error(self, ws, code, reason):
        logger.error(f"WebSocket error: {code} {reason}")

    def _ws_on_reconnect(self, ws, attempt_count):
        logger.info(f"WebSocket reconnect attempt #{attempt_count}")
        self._conn_manager.on_connect()

    async def _reconnect_after_delay(self, delay: float):
        await asyncio.sleep(delay)
        if self._conn_manager.should_attempt_reconnect():
            self.start_websocket()

    def _ws_on_ticks(self, ws, ticks):
        for tick in ticks:
            try:
                token = tick.get("instrument_token")
                symbol = self._token_to_symbol.get(token)
                if not symbol:
                    continue
                normalized = {
                    "instrument_token": token,
                    "symbol": symbol,
                    "last_price": tick.get("last_price", 0),
                    "open": tick.get("open", 0),
                    "high": tick.get("high", 0),
                    "low": tick.get("low", 0),
                    "close": tick.get("close", 0),
                    "volume": tick.get("volume", 0),
                    "buy_quantity": tick.get("buy_quantity", 0),
                    "sell_quantity": tick.get("sell_quantity", 0),
                    "timestamp": tick.get("timestamp"),
                    "data_timestamp": tick.get("last_trade_time"),
                    "oi": tick.get("oi", 0),
                    "average_price": tick.get("average_price", 0),
                }
                self._tick_store.store_tick(symbol, normalized)
                self._candle_aggregator.on_tick(symbol, normalized, timeframe_minutes=5)
            except Exception as e:
                logger.error(f"Tick processing error: {e}")

    async def get_quote(self, symbol: str) -> dict:
        cached = self._tick_store.get_latest_tick(symbol)
        if cached and not self._tick_store.is_stale(symbol):
            return {
                "symbol": symbol,
                "price": cached["last_price"],
                "change": cached["last_price"] - cached.get("close", cached["last_price"]),
                "change_pct": ((cached["last_price"] - cached.get("close", cached["last_price"])) / cached.get("close", 1)) * 100,
                "volume": cached.get("volume", 0),
                "high": cached.get("high", 0),
                "low": cached.get("low", 0),
                "open": cached.get("open", 0),
                "timestamp": (cached.get("timestamp") or now_ist()).isoformat() if hasattr((cached.get("timestamp") or now_ist()), "isoformat") else str(cached.get("timestamp", now_ist())),
                "data_source": self.data_source_label,
            }
        try:
            kite = self._get_kite()
            token = self._instrument_tokens.get(symbol)
            if not token:
                return self._quote_unavailable(symbol)
            quote_data = kite.quote(f"NSE:{symbol}")
            q = quote_data.get(f"NSE:{symbol}", {})
            if not q:
                return self._quote_unavailable(symbol)
            last_price = q.get("last_price", 0)
            ohlc = q.get("ohlc", {})
            prev_close = ohlc.get("close", last_price)
            change = last_price - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0
            tick = {
                "instrument_token": token,
                "symbol": symbol,
                "last_price": last_price,
                "open": ohlc.get("open", 0),
                "high": ohlc.get("high", 0),
                "low": ohlc.get("low", 0),
                "close": prev_close,
                "volume": q.get("volume", 0),
                "timestamp": q.get("timestamp"),
                "data_timestamp": time.time(),
            }
            self._tick_store.store_tick(symbol, tick)
            return {
                "symbol": symbol,
                "price": last_price,
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "volume": q.get("volume", 0),
                "high": ohlc.get("high", 0),
                "low": ohlc.get("low", 0),
                "open": ohlc.get("open", 0),
                "timestamp": q.get("timestamp", now_ist().isoformat()),
                "data_source": self.data_source_label,
            }
        except Exception as e:
            logger.error(f"Quote fetch failed for {symbol}: {e}")
            return self._quote_unavailable(symbol)

    async def get_intraday_bars(self, symbol: str, timeframe: str = "5m") -> pd.DataFrame:
        tick_candles = self._candle_aggregator.get_all_candles_as_df(symbol)
        if len(tick_candles) >= 55:
            return tick_candles
        try:
            kite = self._get_kite()
            interval_map = {"1m": "minute", "3m": "minute", "5m": "5minute", "10m": "10minute", "15m": "15minute"}
            interval = interval_map.get(timeframe, "5minute")
            token = self._instrument_tokens.get(symbol)
            if not token:
                return tick_candles
            from_date = now_ist() - timedelta(days=5)
            to_date = now_ist()
            historical = kite.historical_data(token, from_date, to_date, interval)
            if not historical:
                return tick_candles
            df = pd.DataFrame(historical)
            df = df.rename(columns={"date": "timestamp"})
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").reset_index(drop=True)
            if len(tick_candles) > 0:
                live_dates = set(tick_candles["timestamp"].dt.floor("5min"))
                df["timestamp_floor"] = df["timestamp"].dt.floor("5min")
                df = df[~df["timestamp_floor"].isin(live_dates)]
                df = df.drop(columns=["timestamp_floor"], errors="ignore")
                combined = pd.concat([df, tick_candles], ignore_index=True)
                combined = combined.sort_values("timestamp").reset_index(drop=True)
                return combined
            return df
        except Exception as e:
            logger.error(f"Historical bars fetch failed for {symbol}: {e}")
            return tick_candles

    async def get_ohlcv(self, symbol: str, timeframe: str = "5m", days: int = 5) -> pd.DataFrame:
        try:
            kite = self._get_kite()
            interval_map = {"1m": "minute", "5m": "5minute", "15m": "15minute", "1d": "day"}
            interval = interval_map.get(timeframe, "5minute")
            token = self._instrument_tokens.get(symbol)
            if not token:
                return pd.DataFrame()
            from_date = now_ist() - timedelta(days=days + 2)
            to_date = now_ist()
            historical = kite.historical_data(token, from_date, to_date, interval)
            if not historical:
                return pd.DataFrame()
            df = pd.DataFrame(historical)
            df = df.rename(columns={"date": "timestamp"})
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").reset_index(drop=True)
            return df
        except Exception as e:
            logger.error(f"Historical OHLCV fetch failed for {symbol}: {e}")
            return pd.DataFrame()

    async def get_market_status(self) -> dict:
        from app.core.market_session import market_session
        session = market_session()
        return {
            "session": session,
            "is_open": session in ("market_open", "trading"),
            "data_source": self.data_source_label,
            "connection_status": self._conn_manager.status,
            "last_tick": self._tick_store.last_tick_time.isoformat() if self._tick_store.last_tick_time else None,
            "data_latency_ms": round(self._tick_store.data_latency_ms, 1) if self._tick_store.data_latency_ms else None,
            "tick_count": self._tick_store.tick_count,
            "validation_errors": self._tick_store.validation_errors,
            "timestamp": now_ist().isoformat(),
        }

    async def get_instruments(self, universe: str = "NIFTY50") -> list[dict]:
        result = []
        for sym, info in NIFTY50_INSTRUMENTS.items():
            result.append({
                "symbol": sym,
                "name": info["name"],
                "exchange": info["exchange"],
                "sector": info["sector"],
                "universe": universe,
                "instrument_token": self._instrument_tokens.get(sym),
            })
        return result

    async def get_market_index(self, index_name: str = "NIFTY50") -> dict:
        cached = self._tick_store.get_latest_tick(index_name)
        if cached and not self._tick_store.is_stale(index_name):
            last_price = cached["last_price"]
            prev_close = cached.get("close", last_price)
            change = last_price - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0
            return {
                "symbol": index_name,
                "price": round(last_price, 2),
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "data_source": self.data_source_label,
            }
        try:
            kite = self._get_kite()
            token = self._instrument_tokens.get(index_name)
            if not token:
                return {"symbol": index_name, "price": 0, "change": 0, "change_pct": 0, "data_source": "NO DATA"}
            quote_data = kite.quote(f"NSE:{index_name}")
            q = quote_data.get(f"NSE:{index_name}", {})
            if not q:
                return {"symbol": index_name, "price": 0, "change": 0, "change_pct": 0, "data_source": "NO DATA"}
            last_price = q.get("last_price", 0)
            ohlc = q.get("ohlc", {})
            prev_close = ohlc.get("close", last_price)
            change = last_price - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0
            tick = {
                "instrument_token": token,
                "symbol": index_name,
                "last_price": last_price,
                "open": ohlc.get("open", 0),
                "high": ohlc.get("high", 0),
                "low": ohlc.get("low", 0),
                "close": prev_close,
                "volume": q.get("volume", 0),
                "timestamp": q.get("timestamp"),
                "data_timestamp": time.time(),
            }
            self._tick_store.store_tick(index_name, tick)
            return {
                "symbol": index_name,
                "price": round(last_price, 2),
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "data_source": self.data_source_label,
            }
        except Exception as e:
            logger.error(f"Index fetch failed for {index_name}: {e}")
            return {"symbol": index_name, "price": 0, "change": 0, "change_pct": 0, "data_source": "NO DATA"}

    def _quote_unavailable(self, symbol: str) -> dict:
        return {
            "symbol": symbol,
            "price": 0,
            "change": 0,
            "change_pct": 0,
            "volume": 0,
            "high": 0,
            "low": 0,
            "open": 0,
            "timestamp": now_ist().isoformat(),
            "data_source": "NO DATA",
            "error": "Real-time market data credentials are not configured or connection is unavailable",
        }
