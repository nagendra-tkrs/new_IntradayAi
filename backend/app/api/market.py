from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.market_data.provider_factory import get_provider, get_data_status
from app.services.scanner import MarketScanner
from app.services.indicators import calculate_all_indicators
from app.services.data_validation import data_status
from app.core.market_session import is_market_hours
from app.core.database import get_db
from app.models.models import User, UserTradeSetup
from app.api.auth import get_current_user

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
async def stock_detail(
    symbol: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    provider = _get_provider()
    scanner = _get_scanner()
    combined = await provider.get_quote_and_bars(symbol)
    quote = combined["quote"]
    df = combined["df"]

    user_setup_result = await db.execute(
        select(UserTradeSetup).where(
            UserTradeSetup.user_id == user.id,
            UserTradeSetup.symbol == symbol,
        )
    )
    user_setup = user_setup_result.scalar_one_or_none()
    if user_setup is not None and user_setup.override_active:
        user_trade_setup = {
            "symbol": user_setup.symbol,
            "entry": user_setup.entry,
            "stop_loss": user_setup.stop_loss,
            "target": user_setup.target,
            "risk_reward": user_setup.risk_reward,
            "direction": user_setup.direction,
            "override_active": True,
            "user_setup_updated_at": user_setup.updated_at.isoformat() if user_setup.updated_at else None,
        }
    else:
        user_trade_setup = {
            "symbol": symbol,
            "entry": None,
            "stop_loss": None,
            "target": None,
            "risk_reward": None,
            "direction": None,
            "override_active": False,
            "user_setup_updated_at": None,
        }

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
    from app.services.signal_snapshot import get_signal, invalidate_symbol
    signal = None
    # Check for a valid shared signal snapshot first
    snapshot = get_signal(symbol)
    if snapshot is not None:
        # Snapshot is valid (within 5 min freshness window per SCAN_CACHE_TTL)
        # Use the snapshot's signal if data quality is acceptable
        data_status_val = quote.get("data_status", "UNKNOWN")
        data_age = quote.get("data_age_seconds")
        should_trade, _ = data_status.should_trade(symbol)
        if should_trade and snapshot.get("data_age_seconds", 999) <= 300:
            # Use the cached snapshot signal — signal stays tied to its originating data
            direction = snapshot["direction"]
            confidence = snapshot["confidence"]
            signal = type('SignalObj', (object,), {"direction": direction, "confidence": confidence})()
        else:
            # Snapshot expired or data quality failed — fall through to recalculate
            signal = None
    if signal is None and df is not None and len(df) >= 55 and should_trade:
        market_ctx = await scanner._get_market_context()
        signal = evaluate_signal(
            df, symbol,
            data_source=provider.data_source_label,
            market_context=market_ctx,
            data_age_seconds=data_age,
            data_status=data_status_val,
            market_data_timestamp=quote.get("timestamp"),
        )

    return {
        "quote": quote,
        "indicators": indicators,
        "chart_data": chart_data,
        "signal": signal,
        "user_trade_setup": user_trade_setup,
        "data_status": get_data_status(),
        "data_freshness": {
            "source": "yfinance",
            "status": data_status_val,
            "data_age_seconds": data_age,
            "should_trade": should_trade,
            "trade_reason": trade_reason,
        },
    }


def _aggregate_ohlcv(df, target_interval_min):
    import pandas as pd
    if df is None or len(df) == 0:
        return df
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is not None:
        df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata")
    df = df.set_index("timestamp")
    rule = f"{target_interval_min}min"
    agg = df.resample(rule).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["open"])
    agg = agg.reset_index()
    return agg


SUPPORTED_INTERVALS = {
    "1m": ("1m", 1),
    "5m": ("5m", 5),
    "10m": ("5m", 10),
    "15m": ("15m", 15),
    "20m": ("5m", 20),
    "30m": ("15m", 30),
    "1h": ("1h", 60),
    "2h": ("1h", 120),
    "10h": ("1h", 600),
}


@router.get("/stocks/{symbol}/chart")
async def stock_chart(symbol: str, days: int = 1, interval: str = "5m"):
    provider = _get_provider()
    interval_config = SUPPORTED_INTERVALS.get(interval)
    if interval_config is None:
        interval_config = ("5m", 5)
        interval = "5m"
    provider_tf, target_min = interval_config
    if days <= 1:
        provider_tf = "1m" if target_min <= 1 else "5m"
    elif days <= 5:
        provider_tf = "5m"
    elif days <= 20:
        provider_tf = "15m"
    else:
        provider_tf = "1h"
    if target_min <= 5:
        provider_tf = "1m" if days <= 1 else "5m"
    df = await provider.get_intraday_bars(symbol, timeframe=provider_tf)
    if df is None or len(df) == 0:
        return {"data": [], "data_status": "UNAVAILABLE"}
    if target_min > 5:
        df = _aggregate_ohlcv(df, target_min)
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
        "interval": interval,
        "days": days,
    }


@router.get("/scanner")
async def run_scanner(universe: str = "NIFTY50"):
    provider = _get_provider()
    scanner = _get_scanner()
    results = await scanner.scan_universe(universe)
    top_signals = [r for r in results if r.get("signal") not in ("NO_TRADE", "ERROR", None)]
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
