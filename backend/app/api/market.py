from fastapi import APIRouter, HTTPException
from app.services.market_data.provider_factory import get_provider, get_data_status
from app.services.scanner import MarketScanner
from app.services.indicators import calculate_all_indicators
from app.services.data_validation import data_status
from app.core.market_session import is_market_hours

router = APIRouter(prefix="/api", tags=["market"])

_provider = None
_scanner = None


def _get_provider():
    global _provider, _scanner
    if _provider is None:
        _provider = get_provider()
        _scanner = MarketScanner(_provider)
    return _provider


def _get_scanner():
    _get_provider()
    return _scanner


@router.get("/market/status")
async def market_status():
    provider = _get_provider()
    base_status = await provider.get_market_status()
    ds = get_data_status()
    return {
        **base_status,
        **ds,
        "disclaimer": "Market data supplied through yfinance may be delayed, incomplete, or subject to provider limitations. This application does not guarantee real-time execution-quality market data or trading accuracy.",
    }


@router.get("/market/data-status")
async def data_connection_status():
    ds = get_data_status()
    ds["disclaimer"] = "Market data supplied through yfinance may be delayed, incomplete, or subject to provider limitations."
    return ds


@router.get("/market/index/{name}")
async def market_index(name: str = "NIFTY50"):
    return await _get_provider().get_market_index(name)


@router.get("/stocks")
async def list_stocks(universe: str = "NIFTY50"):
    return await _get_provider().get_instruments(universe)


@router.get("/stocks/{symbol}")
async def stock_detail(symbol: str):
    provider = _get_provider()
    scanner = _get_scanner()
    quote = await provider.get_quote(symbol)
    df = await provider.get_intraday_bars(symbol)

    data_status_val = quote.get("data_status", "UNKNOWN")
    data_age = quote.get("data_age_seconds")
    should_trade, trade_reason = data_status.should_trade(symbol)

    if df is not None and len(df) > 0:
        df = calculate_all_indicators(df)
        indicators = {}
        last = df.iloc[-1]
        import pandas as pd
        import numpy as np
        for col in ["ema_9", "ema_20", "ema_50", "rsi_14", "macd", "macd_signal",
                     "macd_histogram", "atr_14", "adx_14", "vwap", "bb_upper", "bb_lower",
                     "relative_volume", "distance_from_vwap", "volatility_20", "roc_5"]:
            val = last.get(col, None)
            if val is not None and not (isinstance(val, float) and (pd.isna(val) or np.isnan(val))):
                indicators[col] = round(float(val), 2)
            else:
                indicators[col] = None
        chart_data = df[["timestamp", "open", "high", "low", "close", "volume",
                         "vwap", "ema_9", "ema_20", "ema_50"]].tail(100).to_dict(orient="records")
        for row in chart_data:
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
                elif pd.isna(v) if isinstance(v, float) else False:
                    row[k] = None
    else:
        indicators = {}
        chart_data = []

    from app.services.signal_engine import evaluate_signal
    signal = None
    if df is not None and len(df) >= 55 and should_trade:
        market_ctx = await scanner._get_market_context()
        signal = evaluate_signal(
            df, symbol,
            data_source=provider.data_source_label,
            market_context=market_ctx,
            data_age_seconds=data_age,
            data_status=data_status_val,
        )

    return {
        "quote": quote,
        "indicators": indicators,
        "chart_data": chart_data,
        "signal": signal,
        "data_status": get_data_status(),
        "data_freshness": {
            "source": "yfinance",
            "status": data_status_val,
            "data_age_seconds": data_age,
            "should_trade": should_trade,
            "trade_reason": trade_reason,
        },
    }


@router.get("/stocks/{symbol}/chart")
async def stock_chart(symbol: str, days: int = 1):
    df = await _get_provider().get_ohlcv(symbol, days=days)
    if df is None or len(df) == 0:
        return {"data": [], "data_status": "UNAVAILABLE"}
    df = calculate_all_indicators(df)
    import pandas as pd
    records = df[["timestamp", "open", "high", "low", "close", "volume",
                  "vwap", "ema_9", "ema_20", "ema_50"]].to_dict(orient="records")
    for row in records:
        for k, v in row.items():
            if hasattr(v, 'isoformat'):
                row[k] = v.isoformat()
            elif isinstance(v, float) and pd.isna(v):
                row[k] = None
    return {
        "data": records,
        "data_status": "AVAILABLE",
        "source": "yfinance",
    }


@router.get("/scanner")
async def run_scanner(universe: str = "NIFTY50"):
    provider = _get_provider()
    scanner = _get_scanner()
    results = await scanner.scan_universe(universe)
    top_signals = [r for r in results if r.get("signal") not in ("NO TRADE", "ERROR", None)]
    top_signals.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    ds = get_data_status()
    return {
        "total_scanned": len(results),
        "signals_found": len(top_signals),
        "data_source": provider.data_source_label,
        "data_status": ds,
        "signals_paused": data_status.signals_paused,
        "results": results,
        "top_signals": top_signals[:10],
        "disclaimer": "Market data supplied through yfinance may be delayed. Signal scores are not guaranteed accuracy.",
    }
