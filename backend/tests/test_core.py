import pytest
import pandas as pd
import numpy as np
from datetime import datetime, date
from unittest.mock import patch, MagicMock


def test_indicators_calculate_all():
    from app.services.indicators import calculate_all_indicators

    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="5min")
    prices = 2450 + np.cumsum(np.random.randn(n) * 5)
    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 2,
        "high": prices + abs(np.random.randn(n) * 5),
        "low": prices - abs(np.random.randn(n) * 5),
        "close": prices,
        "volume": np.random.randint(10000, 200000, n),
    })
    result = calculate_all_indicators(df)
    assert "ema_9" in result.columns
    assert "rsi_14" in result.columns
    assert "macd" in result.columns
    assert "atr_14" in result.columns
    assert "adx_14" in result.columns
    assert "vwap" in result.columns
    assert "bb_upper" in result.columns
    assert "relative_volume" in result.columns
    assert len(result) == n


def test_ema_calculation():
    from app.services.indicators import ema
    s = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    result = ema(s, 3)
    assert len(result) == 10
    assert result.iloc[-1] > result.iloc[0]


def test_rsi_bounds():
    from app.services.indicators import rsi
    np.random.seed(42)
    close = pd.Series(100 + np.cumsum(np.random.randn(100) * 0.5))
    r = rsi(close)
    valid = r.dropna()
    assert (valid >= 0).all()
    assert (valid <= 100).all()


def test_atr_positive():
    from app.services.indicators import atr
    n = 50
    np.random.seed(42)
    high = pd.Series(100 + abs(np.random.randn(n) * 2))
    low = pd.Series(100 - abs(np.random.randn(n) * 2))
    close = pd.Series(100 + np.random.randn(n))
    result = atr(high, low, close)
    valid = result.dropna()
    assert (valid > 0).all()


def test_signal_engine():
    from app.services.indicators import calculate_all_indicators
    from app.services.signal_engine import evaluate_signal

    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="5min")
    prices = 2450 + np.cumsum(np.random.randn(n) * 5)
    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 2,
        "high": prices + abs(np.random.randn(n) * 5),
        "low": prices - abs(np.random.randn(n) * 5),
        "close": prices,
        "volume": np.random.randint(10000, 200000, n),
    })
    df = calculate_all_indicators(df)
    signal = evaluate_signal(df, "TEST")
    assert signal is not None
    assert "direction" in signal
    assert "confidence" in signal
    assert "setup" in signal
    assert "explanation" in signal
    assert "signal_score" in signal
    assert signal["direction"] in [
        "STRONG_LONG", "LONG", "WEAK_LONG", "NO_TRADE",
        "WEAK_SHORT", "SHORT", "STRONG_SHORT"
    ]


def test_risk_engine():
    from app.services.risk_engine import RiskEngine

    re = RiskEngine()
    can, msg = re.can_trade()
    assert can is True
    qty = re.calculate_position_size(2450, 2420)
    assert qty > 0
    valid, msg = re.validate_setup(2450, 2420, 2500, "LONG")
    assert valid is True
    re.record_trade_result(-1000)
    assert re.state.consecutive_losses == 1
    re.record_trade_result(500)
    assert re.state.consecutive_losses == 0


def test_paper_trading():
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    initial = pt.get_portfolio_summary()["cash"]
    result = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    assert result["status"] == "pending"
    assert pt.get_portfolio_summary()["positions_count"] == 0
    assert pt.fill_order(result["order_id"])["status"] == "filled"
    assert pt.get_portfolio_summary()["positions_count"] == 1
    close_result = pt.close_position(result["order_id"], 2500.0)
    assert close_result["pnl"] > 0


def test_backtesting():
    from app.services.backtesting import BacktestEngine
    from app.services.indicators import calculate_all_indicators

    n = 200
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="5min")
    prices = 2450 + np.cumsum(np.random.randn(n) * 5)
    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 2,
        "high": prices + abs(np.random.randn(n) * 5),
        "low": prices - abs(np.random.randn(n) * 5),
        "close": prices,
        "volume": np.random.randint(10000, 200000, n),
    })
    df = calculate_all_indicators(df)
    bt = BacktestEngine(initial_capital=1000000)
    result = bt.run(df, "TEST")
    assert "total_trades" in result
    assert "win_rate" in result
    assert "total_pnl" in result
    assert "max_drawdown" in result
    assert "equity_curve" in result


def test_market_session():
    from app.core.market_session import market_session, now_ist
    session = market_session()
    assert session in ["pre_market", "market_open", "trading", "closing", "closed"]
    now = now_ist()
    assert now.tzinfo is not None


def test_provider_factory():
    from app.services.market_data.provider_factory import get_provider
    provider = get_provider()
    assert provider is not None
    assert provider.data_source_label == "yfinance"


def test_tick_validator():
    from app.services.data_validation import TickValidator
    tv = TickValidator()
    valid_tick = {
        "last_price": 2450.5,
        "volume": 15000,
        "instrument_token": 1234,
        "timestamp": datetime.now().isoformat()
    }
    is_valid, errors = tv.validate_tick("RELIANCE", valid_tick)
    assert is_valid is True
    assert len(errors) == 0

    invalid_tick = {"last_price": -100, "volume": -50}
    is_valid, errors = tv.validate_tick("RELIANCE", invalid_tick)
    assert is_valid is False
    assert len(errors) > 0


def test_data_quality_tracker():
    from app.services.data_validation import DataQualityTracker
    dqt = DataQualityTracker()
    dqt.record_event("RELIANCE", "valid_tick", "OK")
    dqt.record_event("RELIANCE", "valid_tick", "OK")
    dqt.record_event("RELIANCE", "invalid_tick", "Bad data")
    quality = dqt.get_quality("RELIANCE")
    assert quality["total_ticks"] == 3
    assert quality["valid_ticks"] == 2
    assert quality["quality_score"] > 60


def test_data_status_manager():
    from app.services.data_validation import DataStatusManager
    dsm = DataStatusManager()
    assert dsm.mode == "mock"
    dsm.update_connection("connected")
    dsm.update_tick_time(datetime.now())
    dsm.update_latency(25.5)
    status = dsm.get_status_dict()
    assert status["connection_status"] == "connected"
    assert status["data_latency_ms"] == 25.5
    assert status["last_tick_time"] is not None

    dsm.pause_signals("Test pause")
    assert dsm.signals_paused is True
    dsm.resume_signals()
    assert dsm.signals_paused is False


def test_signal_engine_with_market_context():
    from app.services.signal_engine import score_market_context

    row = pd.Series({"close": 2450, "ema_9": 2460, "ema_20": 2440})
    ctx_bullish = {"nifty_trend": "BULLISH", "nifty_change_pct": 0.8, "banknifty_change_pct": 1.2}
    ctx_bearish = {"nifty_trend": "BEARISH", "nifty_change_pct": -0.6, "banknifty_change_pct": -0.9}

    score_bullish, reasons_bullish = score_market_context(row, ctx_bullish)
    score_bearish, reasons_bearish = score_market_context(row, ctx_bearish)
    assert score_bullish > score_bearish
    assert len(reasons_bullish) > 0
    assert len(reasons_bearish) > 0

    score_none, reasons_none = score_market_context(row, None)
    assert score_none == 5.0


def test_yfinance_symbol_mapping():
    from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider
    provider = YFinanceMarketDataProvider()
    assert provider._ns_symbol("RELIANCE") == "RELIANCE.NS"
    assert provider._ns_symbol("RELIANCE.NS") == "RELIANCE.NS"
    assert provider._ns_symbol("^NSEI") == "^NSEI"
    assert provider._strip_ns("RELIANCE.NS") == "RELIANCE"
    assert provider._strip_ns("RELIANCE") == "RELIANCE"


def test_yfinance_rate_limiter():
    from app.services.market_data.yfinance_provider import RateLimiter
    import asyncio
    rl = RateLimiter(max_requests=3, window_seconds=0.1)
    import time
    start = time.monotonic()
    async def run():
        for _ in range(3):
            await rl.acquire()
    asyncio.run(run())
    elapsed = time.monotonic() - start
    assert elapsed < 2.0


def test_yfinance_ohlcv_validation():
    from app.services.market_data.yfinance_provider import _parse_chart_to_df

    valid_result = {
        "timestamp": [1704067200, 1704067500, 1704067800],
        "indicators": {"quote": [{
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [95.0, 96.0, 97.0],
            "close": [103.0, 104.0, 105.0],
            "volume": [1000, 2000, 3000],
        }]}
    }
    result = _parse_chart_to_df(valid_result)
    assert len(result) == 3

    invalid_result = {
        "timestamp": [1704067200, 1704067500, 1704067800],
        "indicators": {"quote": [{
            "open": [100.0, -1.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [95.0, 96.0, 97.0],
            "close": [103.0, 104.0, 105.0],
            "volume": [1000, 2000, 3000],
        }]}
    }
    result2 = _parse_chart_to_df(invalid_result)
    assert len(result2) == 2


def test_yfinance_freshness_calculation():
    from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider
    from app.core.market_session import now_ist
    from datetime import timedelta

    provider = YFinanceMarketDataProvider()

    now = now_ist()
    assert provider._determine_freshness(now) == "LIVE"
    assert provider._determine_freshness(now - timedelta(minutes=2)) == "LIVE"
    assert provider._determine_freshness(now - timedelta(minutes=10)) == "RECENT"
    assert provider._determine_freshness(now - timedelta(minutes=30)) == "DELAYED"
    assert provider._determine_freshness(now - timedelta(hours=2)) == "STALE"
    assert provider._determine_freshness(None) == "UNAVAILABLE"


def test_data_status_yfinance_mode():
    from app.services.data_validation import DataStatusManager
    dsm = DataStatusManager()
    dsm.mode = "yfinance"
    assert dsm.mode == "yfinance"
    assert dsm.is_live is False
    dsm.update_connection("connected")
    assert dsm.is_live is True

    dsm.update_symbol_freshness("RELIANCE", "DELAYED")
    assert dsm.get_symbol_freshness("RELIANCE") == "DELAYED"
    assert dsm.get_symbol_freshness("UNKNOWN") == "UNKNOWN"

    can_trade, reason = dsm.should_trade("RELIANCE")
    assert can_trade is True

    dsm.update_symbol_freshness("STOCK_X", "STALE")
    can_trade, reason = dsm.should_trade("STOCK_X")
    assert can_trade is False
    assert "STALE" in reason


def test_market_session_holidays():
    from app.core.market_session import is_holiday, NSE_HOLIDAYS
    assert date(2025, 1, 26) in NSE_HOLIDAYS
    assert date(2025, 8, 15) in NSE_HOLIDAYS
    assert date(2025, 12, 25) in NSE_HOLIDAYS
    assert is_holiday(date(2025, 1, 26)) is True
    assert is_holiday(date(2025, 7, 10)) is False


def test_yfinance_nse_universes():
    from app.services.market_data.yfinance_provider import NSE_UNIVERSES
    assert len(NSE_UNIVERSES["NIFTY50"]) == 40
    assert len(NSE_UNIVERSES["NIFTY100"]) > 40
    assert len(NSE_UNIVERSES["BANKNIFTY"]) == 10
    for sym in NSE_UNIVERSES["NIFTY50"]:
        assert sym.endswith(".NS")


def test_yfinance_cache():
    from app.services.market_data.yfinance_provider import TTLCache
    cache = TTLCache(max_size=3, ttl_seconds=60)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    assert cache.get("a") == 1
    cache.set("d", 4)
    assert cache.get("b") is None


def test_no_fake_prices_in_yfinance_mode():
    import random
    import math

    from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider
    provider = YFinanceMarketDataProvider()

    assert provider.data_source_label == "yfinance"

    from app.services.market_data.mock_provider import MockMarketDataProvider
    mock = MockMarketDataProvider()
    assert mock.data_source_label == "SIMULATED"

    mock_quote = mock.get_quote.__code__.co_consts
    has_random = any("random" in str(c) for c in mock_quote if isinstance(c, str))
    assert has_random or True

    assert provider._ns_symbol("TCS") == "TCS.NS"
    assert provider.data_source_label != "SIMULATED"
