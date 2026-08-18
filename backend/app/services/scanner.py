import logging
import asyncio
import pandas as pd
from typing import Optional
from app.services.market_data.base import MarketDataProvider
from app.services.indicators import calculate_all_indicators
from app.services.signal_engine import evaluate_signal
from app.core.market_session import now_ist, is_market_hours
from app.services.data_validation import data_status

logger = logging.getLogger(__name__)

MAX_PARALLEL_FETCHES = 8


class MarketScanner:
    def __init__(self, provider: MarketDataProvider):
        self.provider = provider

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
            df = await self.provider.get_intraday_bars(symbol, timeframe="5m")
            quote = await self.provider.get_quote(symbol)
            return {**base, "_df": df, "_quote": quote, "_error": None}
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
                    "signal": "NO TRADE", "confidence": 0,
                    "data_status": "UNAVAILABLE", "reason": "Insufficient candle history"}

        df = calculate_all_indicators(df)
        row = df.iloc[-1]

        data_status_val = quote.get("data_status", "UNKNOWN")
        data_age = quote.get("data_age_seconds")
        should_trade, trade_reason = data_status.should_trade(symbol)

        if not should_trade:
            return {**base, "price": quote["price"], "change_pct": quote["change_pct"],
                    "volume": quote["volume"], "signal": "NO TRADE", "confidence": 0,
                    "data_status": data_status_val, "data_age_seconds": data_age,
                    "reason": trade_reason}

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

        signal = evaluate_signal(df, symbol, data_source=self.provider.data_source_label, market_context=market_ctx)
        if signal:
            result["signal"] = signal["direction"]
            result["confidence"] = signal["confidence"]
            result["signal_data"] = signal
        else:
            result["signal"] = "NO TRADE"
            result["confidence"] = 0
            result["signal_data"] = None

        return result

    async def scan_universe(self, universe: str = "NIFTY50") -> list[dict]:
        instruments = await self.provider.get_instruments(universe)
        market_ctx = await self._get_market_context()

        sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

        async def bounded_fetch(inst):
            async with sem:
                return await self._fetch_stock_data(inst)

        fetch_results = await asyncio.gather(*[bounded_fetch(inst) for inst in instruments])
        return [self._process_stock(inst, fr, market_ctx) for inst, fr in zip(instruments, fetch_results)]

    async def scan_symbol(self, symbol: str) -> Optional[dict]:
        try:
            market_ctx = await self._get_market_context()
            df = await self.provider.get_intraday_bars(symbol, timeframe="5m")
            if df is None or len(df) < 55:
                return None

            df = calculate_all_indicators(df)
            quote = await self.provider.get_quote(symbol)
            row = df.iloc[-1]

            data_status_val = quote.get("data_status", "UNKNOWN")
            data_age = quote.get("data_age_seconds")
            should_trade, trade_reason = data_status.should_trade(symbol)

            signal = evaluate_signal(
                df, symbol,
                data_source=self.provider.data_source_label,
                market_context=market_ctx,
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
