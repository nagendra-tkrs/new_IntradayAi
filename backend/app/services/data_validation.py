import logging
from datetime import datetime, timedelta
from typing import Optional
from app.core.market_session import now_ist, IST

logger = logging.getLogger(__name__)


class TickValidator:
    def __init__(self):
        self._last_prices: dict[str, float] = {}
        self._last_volumes: dict[str, int] = {}
        self._stale_threshold_seconds: int = 300
        self._max_price_jump_pct: float = 10.0

    def validate_tick(self, symbol: str, tick: dict) -> tuple[bool, list[str]]:
        errors = []
        price = tick.get("last_price", 0)
        if price <= 0:
            errors.append(f"Invalid price: {price}")
        if price > 10_000_000:
            errors.append(f"Abnormal price: {price}")
        volume = tick.get("volume", 0)
        if volume < 0:
            errors.append(f"Negative volume: {volume}")
        instrument_token = tick.get("instrument_token")
        if not instrument_token:
            errors.append("Missing instrument_token")
        ts = tick.get("timestamp")
        if ts:
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts)
                except ValueError:
                    errors.append(f"Invalid timestamp format: {ts}")
                    ts = None
            if ts:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=IST)
                now = now_ist()
                if ts > now + timedelta(minutes=5):
                    errors.append(f"Future timestamp: {ts}")
                age = (now - ts).total_seconds()
                if age > self._stale_threshold_seconds:
                    errors.append(f"Stale data: {age:.0f}s old")
        if symbol in self._last_prices and self._last_prices[symbol] > 0:
            prev_price = self._last_prices[symbol]
            if prev_price > 0:
                pct_change = abs(price - prev_price) / prev_price * 100
                if pct_change > self._max_price_jump_pct:
                    errors.append(f"Abnormal price jump: {pct_change:.1f}%")
        if errors:
            logger.warning(f"Tick validation failed for {symbol}: {errors}")
        return (len(errors) == 0, errors)

    def update_state(self, symbol: str, tick: dict):
        price = tick.get("last_price", 0)
        if price > 0:
            self._last_prices[symbol] = price
        volume = tick.get("volume", 0)
        if volume >= 0:
            self._last_volumes[symbol] = volume


class DataQualityTracker:
    def __init__(self):
        self._events: list[dict] = []
        self._quality_by_symbol: dict[str, dict] = {}

    def record_event(self, symbol: str, event_type: str, details: str, severity: str = "info"):
        event = {
            "symbol": symbol,
            "event_type": event_type,
            "details": details,
            "severity": severity,
            "timestamp": now_ist().isoformat(),
        }
        self._events.append(event)
        if len(self._events) > 1000:
            self._events = self._events[-500:]
        if symbol not in self._quality_by_symbol:
            self._quality_by_symbol[symbol] = {
                "total_ticks": 0,
                "valid_ticks": 0,
                "invalid_ticks": 0,
                "stale_events": 0,
                "quality_score": 100.0,
            }
        q = self._quality_by_symbol[symbol]
        q["total_ticks"] += 1
        if event_type == "valid_tick":
            q["valid_ticks"] += 1
        elif event_type == "invalid_tick":
            q["invalid_ticks"] += 1
        elif event_type == "stale_data":
            q["stale_events"] += 1
        if q["total_ticks"] > 0:
            q["quality_score"] = round((q["valid_ticks"] / q["total_ticks"]) * 100, 1)

    def get_quality(self, symbol: str) -> dict:
        return self._quality_by_symbol.get(symbol, {
            "total_ticks": 0, "valid_ticks": 0, "invalid_ticks": 0,
            "stale_events": 0, "quality_score": 0.0,
        })

    def get_recent_events(self, limit: int = 50) -> list[dict]:
        return self._events[-limit:]


class DataStatusManager:
    def __init__(self):
        self._mode: str = "mock"
        self._connection_status: str = "disconnected"
        self._data_source: str = "SIMULATED"
        self._last_tick_time: Optional[datetime] = None
        self._data_latency_ms: Optional[float] = None
        self._signals_paused: bool = False
        self._pause_reason: str = ""
        self._historical_warmed_up: bool = False
        self._instruments_loaded: bool = False
        self._symbol_freshness: dict[str, str] = {}

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str):
        if value not in ("yfinance", "live", "paper", "historical", "mock"):
            raise ValueError(f"Invalid mode: {value}")
        self._mode = value

    @property
    def is_live(self) -> bool:
        return self._mode in ("live", "yfinance") and self._connection_status == "connected"

    @property
    def signals_paused(self) -> bool:
        return self._signals_paused

    @property
    def pause_reason(self) -> str:
        return self._pause_reason

    def pause_signals(self, reason: str):
        self._signals_paused = True
        self._pause_reason = reason
        logger.warning(f"Signals paused: {reason}")

    def resume_signals(self):
        self._signals_paused = False
        self._pause_reason = ""
        logger.info("Signals resumed")

    def update_connection(self, status: str):
        self._connection_status = status
        if status == "connected" and self._mode in ("live", "yfinance"):
            self._signals_paused = False
            self._pause_reason = ""
        elif status == "disconnected" and self._mode in ("live", "yfinance"):
            self.pause_signals("Data provider disconnected")

    def update_tick_time(self, tick_time: datetime):
        self._last_tick_time = tick_time

    def update_latency(self, latency_ms: float):
        self._data_latency_ms = latency_ms

    def update_symbol_freshness(self, symbol: str, status: str):
        self._symbol_freshness[symbol] = status

    def get_symbol_freshness(self, symbol: str) -> str:
        return self._symbol_freshness.get(symbol, "UNKNOWN")

    def should_trade(self, symbol: str) -> tuple[bool, str]:
        if self._signals_paused:
            return False, f"Signals paused: {self._pause_reason}"
        freshness = self._symbol_freshness.get(symbol, "UNKNOWN")
        if freshness in ("UNAVAILABLE", "STALE"):
            return False, f"Data quality failure: {freshness}"
        return True, "OK"

    def get_status_dict(self) -> dict:
        return {
            "mode": self._mode,
            "connection_status": self._connection_status,
            "data_source": self._data_source,
            "last_tick_time": self._last_tick_time.isoformat() if self._last_tick_time else None,
            "data_latency_ms": round(self._data_latency_ms, 1) if self._data_latency_ms else None,
            "signals_paused": self._signals_paused,
            "pause_reason": self._pause_reason,
            "historical_warmed_up": self._historical_warmed_up,
            "instruments_loaded": self._instruments_loaded,
            "symbol_freshness": dict(self._symbol_freshness),
        }


tick_validator = TickValidator()
data_quality = DataQualityTracker()
data_status = DataStatusManager()
