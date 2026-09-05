import logging
import asyncio
import time
import pandas as pd
from typing import Optional
from app.services.market_data.base import MarketDataProvider
from app.services.indicators import calculate_all_indicators
from app.services.signal_engine import evaluate_signal
from app.services.signal_snapshot import set_signal, get_signal, invalidate_symbol
from app.services.signal_snapshot import set_signal, get_signal
from app.models.schemas import SignalDirection
from app.core.market_session import now_ist, is_market_hours
from app.services.data_validation import data_status

logger = logging.getLogger(__name__)

MAX_PARALLEL_FETCHES = 8
SCAN_CACHE_TTL = 300  # 5 minutes


class MarketScanner:
    def __init__(self, provider: MarketDataProvider):
        self.provider = provider
        self._scan_cache: dict[str, tuple[float, list[dict]]] = {}

    async def _get_market_context(self) -> dict:
        context = {
            "nifty_trend": "UNKNOWN",
            "nifty_change_pct": 0,
            "banknifty_change_pct": 0,
            "market_open": is_market_hours(),
        }
        try:
            nifty = await self.provider.get_market_index("NIFTY50")
            context["nifty_change_pct"] = nifty.get("change_pct", 0)
            if nifty.get("change_pct", 0) > 0.3:
                context["nifty_trend"] = "BULLISH"
            elif nifty.get("change_pct", 0) < -0.3:
                context["nifty_trend"] = "BEARISH"
            context["nifty_data_status"] = nifty.get("data_status", "UNKNOWN")
        except Exception:
            pass
        try:
            banknifty = await self.provider.get_market_index("BANKNIFTY")
            context["banknifty_change_pct"] = banknifty.get("change_pct", 0)
            context["banknifty_data_status"] = banknifty.get("data_status", "UNKNOWN")
        except Exception:
            pass
        return context

    async def _fetch_stock_data(self, inst: dict) -> dict:
        symbol = inst["symbol"]
        base = {
            "symbol": symbol,
            "name": inst.get("name", symbol),
            "sector": inst.get("sector", "Unknown"),
        }
        try:
            combined = await self.provider.get_quote_and_bars(symbol, timeframe="5m")
            return {**base, "_df": combined["df"], "_quote": combined["quote"], "_error": None}
        except Exception as e:
            return {**base, "_df": None, "_quote": None, "_error": str(e)}

    def _process_stock(self, inst: dict, fetch_result: dict, market_ctx: dict) -> dict:
        symbol = fetch_result["symbol"]
        base = {
            "symbol": symbol,
            "name": fetch_result["name"],
            "sector": fetch_result["sector"],
        }

        if fetch_result["_error"]:
            return {**base, "price": 0, "change_pct": 0, "volume": 0,
                    "signal": "ERROR", "confidence": 0, "data_status": "UNAVAILABLE",
                    "error": fetch_result["_error"]}

        df = fetch_result["_df"]
        quote = fetch_result["_quote"]

        if df is None or len(df) < 55:
            return {**base, "price": 0, "change_pct": 0, "volume": 0,
                    "signal": SignalDirection.NO_TRADE.value, "confidence": 0,
                    "data_status": "UNAVAILABLE", "reason": "Insufficient candle history"}

        df = calculate_all_indicators(df)
        row = df.iloc[-1]

        data_status_val = quote.get("data_status", "UNKNOWN")
        data_age = quote.get("data_age_seconds")
        should_trade, trade_reason = data_status.should_trade(symbol)

        if not should_trade:
            return {**base, "price": quote["price"], "change_pct": quote["change_pct"],
                    "volume": quote["volume"], "signal": SignalDirection.NO_TRADE.value,
                    "confidence": 0, "data_status": data_status_val,
                    "data_age_seconds": data_age, "reason": trade_reason}

        result = {**base, "price": quote["price"], "change_pct": quote["change_pct"],
                  "volume": quote["volume"], "data_status": data_status_val,
                  "data_age_seconds": data_age, "data_timestamp": quote.get("timestamp"),
                  "ema_9": round(float(row.get("ema_9", 0)), 2) if pd.notna(row.get("ema_9")) else None,
                  "ema_20": round(float(row.get("ema_20", 0)), 2) if pd.notna(row.get("ema_20")) else None,
                  "ema_50": round(float(row.get("ema_50", 0)), 2) if pd.notna(row.get("ema_50")) else None,
                  "rsi": round(float(row.get("rsi_14", 0)), 1) if pd.notna(row.get("rsi_14")) else None,
                  "macd": round(float(row.get("macd", 0)), 2) if pd.notna(row.get("macd")) else None,
                  "atr": round(float(row.get("atr_14", 0)), 2) if pd.notna(row.get("atr_14")) else None,
                  "adx": round(float(row.get("adx_14", 0)), 1) if pd.notna(row.get("adx_14")) else None,
                  "vwap": round(float(row.get("vwap", 0)), 2) if pd.notna(row.get("vwap")) else None,
                  "relative_volume": round(float(row.get("relative_volume", 0)), 1) if pd.notna(row.get("relative_volume")) else None,
                  "distance_from_vwap": round(float(row.get("distance_from_vwap", 0)), 2) if pd.notna(row.get("distance_from_vwap")) else None,
                  "trend": self._get_trend(row)}

        signal = evaluate_signal(
            df, symbol, 
            data_source=self.provider.data_source_label, 
            market_context=market_ctx,
            data_age_seconds=data_age,
            data_status=data_status_val,
            market_data_timestamp=quote.get("timestamp"),
        )
        if signal:
            result["signal"] = signal["direction"]
            result["confidence"] = signal["confidence"]
            result["signal_data"] = signal
            # Store signal snapshot in shared store for stock_detail to use
            set_signal(symbol, {
                "direction": signal["direction"],
                "confidence": signal["confidence"],
                "signal_data": signal,
                "signal_generated_at": signal.get("signal_generated_at"),
                "data_timestamp": signal.get("market_data_timestamp"),
                "data_age_seconds": data_age,
                "data_status": data_status_val,
                "timeframe": "5m",
                "snapshot_id": f"{symbol}_{int(time.time())}",
            })
        else:
            result["signal"] = SignalDirection.NO_TRADE.value
            result["confidence"] = 0
            result["signal_data"] = None
            # Clear any stale snapshot for this symbol
            invalidate_symbol(symbol)
            result["signal_snapshot"] = {
                "signal_generated_at": None,
                "data_timestamp": None,
                "candle_timestamp": None,
                "data_status": data_status_val,
                "data_age_seconds": data_age,
                "timeframe": "5m",
                "snapshot_id": f"{symbol}_{int(time.time())}",
            }

        return result

    async def scan_universe(self, universe: str = "NIFTY50") -> list[dict]:
        cache_key = universe
        now = time.time()
        if cache_key in self._scan_cache:
            cached_time, cached_data = self._scan_cache[cache_key]
            if now - cached_time < SCAN_CACHE_TTL:
                return cached_data

        instruments = await self.provider.get_instruments(universe)
        market_ctx = await self._get_market_context()

        sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

        async def bounded_fetch(inst):
            async with sem:
                return await self._fetch_stock_data(inst)

        fetch_results = await asyncio.gather(*[bounded_fetch(inst) for inst in instruments])
        results = [self._process_stock(inst, fr, market_ctx) for inst, fr in zip(instruments, fetch_results)]
        self._scan_cache[cache_key] = (now, results)
        return results

    async def scan_symbol(self, symbol: str) -> Optional[dict]:
        try:
            market_ctx = await self._get_market_context()
            combined = await self.provider.get_quote_and_bars(symbol, timeframe="5m")
            df = combined["df"]
            quote = combined["quote"]

            if df is None or len(df) < 55:
                return None

            df = calculate_all_indicators(df)
            row = df.iloc[-1]

            data_status_val = quote.get("data_status", "UNKNOWN")
            data_age = quote.get("data_age_seconds")
            should_trade, trade_reason = data_status.should_trade(symbol)

            signal = evaluate_signal(
                df, symbol,
                data_source=self.provider.data_source_label,
                market_context=market_ctx,
                data_age_seconds=data_age,
                data_status=data_status_val,
                market_data_timestamp=quote.get("timestamp"),
            )

            return {
                "symbol": symbol,
                "price": quote["price"],
                "change_pct": quote["change_pct"],
                "volume": quote["volume"],
                "data_status": data_status_val,
                "data_age_seconds": data_age,
                "data_timestamp": quote.get("timestamp"),
                "should_trade": should_trade,
                "trade_reason": trade_reason if not should_trade else None,
                "indicators": {
                    "ema_9": round(float(row.get("ema_9", 0)), 2),
                    "ema_20": round(float(row.get("ema_20", 0)), 2),
                    "ema_50": round(float(float(row.get("ema_50", 0))), 2),
                    "rsi": round(float(row.get("rsi_14", 0)), 1),
                    "macd": round(float(row.get("macd", 0)), 2),
                    "macd_signal": round(float(row.get("macd_signal", 0)), 2),
                    "atr": round(float(row.get("atr_14", 0)), 2),
                    "adx": round(float(row.get("adx_14", 0)), 1),
                    "vwap": round(float(row.get("vwap", 0)), 2),
                    "bb_upper": round(float(row.get("bb_upper", 0)), 2),
                    "bb_lower": round(float(row.get("bb_lower", 0)), 2),
                    "relative_volume": round(float(row.get("relative_volume", 0)), 1),
                    "distance_from_vwap": round(float(row.get("distance_from_vwap", 0)), 2),
                },
                "market_context": market_ctx,
                "signal": signal,
            }
        except Exception as e:
            return {"symbol": symbol, "error": str(e)}

    def _get_trend(self, row) -> str:
        ema_9 = row.get("ema_9", None)
        ema_20 = row.get("ema_20", None)
        ema_50 = row.get("ema_50", None)
        if pd.isna(ema_9) or pd.isna(ema_20) or pd.isna(ema_50):
            return "UNKNOWN"
        if ema_9 > ema_20 > ema_50:
            return "BULLISH"
        elif ema_9 < ema_20 < ema_50:
            return "BEARISH"
        elif ema_9 > ema_20:
            return "MILDLY BULLISH"
        elif ema_9 < ema_20:
            return "MILDLY BEARISH"
        return "NEUTRAL"
