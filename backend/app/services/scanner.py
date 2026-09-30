import logging
import asyncio
import time
import pandas as pd
from typing import Optional
from app.services.market_data.base import MarketDataProvider
from app.services.indicators import calculate_all_indicators
from app.services.signal_engine import evaluate_signal
from app.services.strategy_config import LIVE_STRATEGY_VERSION
from app.services.signal_quality import compute_setup_quality
from app.services.signal_snapshot import set_signal, get_signal, invalidate_symbol
from app.services.signal_snapshot import set_signal, get_signal
from app.models.schemas import SignalDirection
from app.core.market_session import now_ist, is_market_hours
from app.core.config import settings
from app.services.context_24h import compute_24h_context
from app.services import signal_store
from app.services.data_validation import data_status
import app.services.active_universe as active_universe

logger = logging.getLogger(__name__)

MAX_PARALLEL_FETCHES = 8
SCAN_CACHE_TTL = 300  # 5 minutes


_QUALITY_FIELDS = (
    "setup_quality_score", "setup_quality", "confirmation_count",
    "confirmation_total", "top_signal_eligible", "risk_reward_ratio",
    "top_signal_rank", "quality_detail",
)


def _attach_quality(result: dict, quality: dict) -> dict:
    """Copy the additive Top-Signal quality fields onto a scanner result row.

    The base signal (``signal`` / ``signal_data`` / ``confidence``) is never
    touched — the quality layer is purely additive.
    """
    for k in _QUALITY_FIELDS:
        result[k] = quality.get(k)
    return result


def _run_profit_selection(
    result: dict,
    signal: Optional[dict],
    row,
    market_ctx: Optional[dict],
    candle_ts=None,
    baseline_decision: str = "TRADE",
) -> Optional[dict]:
    """Run the Profit Selection Layer in SHADOW mode and record the decision.

    Placement: AFTER signal generation + setup quality, BEFORE the risk engine.
    It does not recompute the signal score, does not alter the entry/stop/
    targets, and by default does not block anything — the decision is written
    to the shadow ledger so a future promotion can be measured against what
    production actually did.

    Returns the decision dict (also placed on ``result['profit_selection']``),
    or None when there is no signal to judge.
    """
    if not signal:
        return None
    try:
        from app.services.profit_selection import (
            SelectionConfig, evaluate_selection, shadow_record,
        )
        from app.services.profit_selection_store import persist_selection_decision

        cfg = SelectionConfig.from_settings(settings)
        # SHADOW MODE IS STRUCTURAL: the layer can only ever return TRADE or
        # SKIP for a hypothetical, and it is applied to production only in
        # ENFORCE, which is itself guarded by assert_promotable.
        indicator_values = {
            "adx_14": _safe_float(row, "adx_14"),
            "relative_volume": _safe_float(row, "relative_volume"),
            "atr_14": _safe_float(row, "atr_14"),
            "vwap": _safe_float(row, "vwap"),
            "close": _safe_float(row, "close"),
        }
        decision = evaluate_selection(
            {
                **signal,
                "signal_quality": result.get("setup_quality"),
                "setup_quality_score": result.get("setup_quality_score"),
            },
            config=cfg,
            indicator_values=indicator_values,
            market_context=market_ctx,
            entry_time=candle_ts or signal.get("timestamp"),
        )
        record = shadow_record(
            {**signal,
             "signal_quality": result.get("setup_quality"),
             "setup_quality_score": result.get("setup_quality_score")},
            decision,
            signal_id=signal.get("id"),
        )
        persist_selection_decision(record, baseline_decision=baseline_decision)
        payload = decision.to_dict()
        result["profit_selection"] = payload
        return payload
    except Exception as e:
        # Measurement must never break a scan.
        logger.warning("profit selection shadow failed: %s", e)
        return None


def _safe_float(row, key) -> Optional[float]:
    try:
        import math

        value = row.get(key) if row is not None else None
        if value is None:
            return None
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


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
        if inst.get("classification"):
            base["classification"] = inst["classification"]
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
        if fetch_result.get("classification"):
            base["classification"] = fetch_result["classification"]

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

        # Optional 24H context (Mode B). Computed read-only on every scan so a
        # result can carry it regardless of the flag; it only influences the
        # signal when USE_24H_CONTEXT=true (shadow evaluation below). The
        # decision timestamp is the decision CANDLE's timestamp (never the wall
        # clock) so no bar after the decision can leak into the window.
        decision_ts = row.get("timestamp") if "timestamp" in df.columns else None
        ctx24 = compute_24h_context(df, decision_ts=decision_ts,
                                    window_hours=settings.CONTEXT_24H_WINDOW_HOURS)

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
                  "context_24h": ctx24,
                  "trend": self._get_trend(row)}

        signal = evaluate_signal(
            df, symbol, 
            data_source=self.provider.data_source_label, 
            market_context=market_ctx,
            data_age_seconds=data_age,
            data_status=data_status_val,
            market_data_timestamp=quote.get("timestamp"),
            strategy_version=LIVE_STRATEGY_VERSION,
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

        # Top Signal Quality Layer (additive): never modifies the base signal.
        _attach_quality(result, compute_setup_quality(signal, row, market_context=market_ctx, df=df))

        # Traceability (Mode A) + optional 24H-context SHADOW (Mode B). The
        # shadow never places paper orders — only the current signal does.
        if signal:
            signal_store.persist_signal(signal, candle_ts=decision_ts,
                                        market_ctx=market_ctx,
                                        quality=result.get("setup_quality"))
            # Profit Selection Layer (SHADOW). Sits after quality, before the
            # risk engine; records a TRADE/SKIP decision but blocks nothing.
            _run_profit_selection(result, signal, row, market_ctx,
                                  candle_ts=decision_ts,
                                  baseline_decision="TRADE")
        if signal and settings.USE_24H_CONTEXT:
            signal_24h = evaluate_signal(
                df, symbol,
                data_source=self.provider.data_source_label,
                market_context=market_ctx,
                data_age_seconds=data_age,
                data_status=data_status_val,
                market_data_timestamp=quote.get("timestamp"),
                context_24h=ctx24,
                strategy_version=LIVE_STRATEGY_VERSION,
            )
            if signal_24h:
                q24 = compute_setup_quality(signal_24h, row, market_context=market_ctx, df=df)
                result["signal_24h"] = {
                    "direction": signal_24h["direction"],
                    "confidence": signal_24h["confidence"],
                    "signal_data": signal_24h,
                    "context_24h": ctx24,
                }
                signal_store.persist_24h_comparison(
                    signal, signal_24h, ctx24, candle_ts=decision_ts,
                    quality=result.get("setup_quality"),
                    quality24=q24.get("setup_quality"),
                )

        return result

    async def scan_universe(self, universe: Optional[str] = None) -> list[dict]:
        """Scan the requested universe.

        Phase 1: the ACTIVE live universe (``LIVE_UNIVERSE=rotation_80`` or the
        ``ACTIVE`` sentinel) is resolved through
        ``active_universe.get_active_trading_universe()`` — the single source
        of truth — and ALL 80 symbols are processed (never a fallback). When a
        valid ACTIVE universe is unavailable this raises
        ``ActiveUniverseUnavailable`` (FAIL CLOSED); the legacy
        ``NIFTY50``/``NIFTY100``/``BANKNIFTY`` provider paths remain intact for
        explicit requests.
        """
        if universe is None or active_universe.is_active_universe_request(universe):
            resolution = await active_universe.get_active_trading_universe()
            instruments = resolution["stocks"]
            universe_key = f"active:{resolution.get('universe_version_id')}"
        else:
            instruments = await self.provider.get_instruments(universe)
            universe_key = universe
        cache_key = universe_key
        now = time.time()
        if cache_key in self._scan_cache:
            cached_time, cached_data = self._scan_cache[cache_key]
            if now - cached_time < SCAN_CACHE_TTL:
                return cached_data

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

            decision_ts = row.get("timestamp") if "timestamp" in df.columns else None
            ctx24 = compute_24h_context(df, decision_ts=decision_ts,
                                        window_hours=settings.CONTEXT_24H_WINDOW_HOURS)

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
                strategy_version=LIVE_STRATEGY_VERSION,
            )

            result = {
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
                "context_24h": ctx24,
                "signal": signal,
            }
            # Top Signal Quality Layer (additive): never modifies the base signal.
            _attach_quality(result, compute_setup_quality(signal, row, market_context=market_ctx, df=df))
            # Traceability + Mode B shadow (see _process_stock). Persisted only
            # when the data gate allows trading, matching scan_universe.
            if signal:
                # Recorded whether or not production traded it: a signal the
                # data gate rejected is exactly the case where a future
                # selection rule has something to say.
                _run_profit_selection(
                    result, signal, row, market_ctx, candle_ts=decision_ts,
                    baseline_decision="TRADE" if should_trade else "SKIP",
                )
            if signal and should_trade:
                signal_store.persist_signal(signal, candle_ts=decision_ts,
                                            market_ctx=market_ctx,
                                            quality=result.get("setup_quality"))
            if signal and should_trade and settings.USE_24H_CONTEXT:
                signal_24h = evaluate_signal(
                    df, symbol,
                    data_source=self.provider.data_source_label,
                    market_context=market_ctx,
                    data_age_seconds=data_age,
                    data_status=data_status_val,
                    market_data_timestamp=quote.get("timestamp"),
                    context_24h=ctx24,
                    strategy_version=LIVE_STRATEGY_VERSION,
                )
                if signal_24h:
                    q24 = compute_setup_quality(signal_24h, row, market_context=market_ctx, df=df)
                    result["signal_24h"] = {
                        "direction": signal_24h["direction"],
                        "confidence": signal_24h["confidence"],
                        "signal_data": signal_24h,
                        "context_24h": ctx24,
                    }
                    signal_store.persist_24h_comparison(
                        signal, signal_24h, ctx24, candle_ts=decision_ts,
                        quality=result.get("setup_quality"),
                        quality24=q24.get("setup_quality"),
                    )
            return result
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
