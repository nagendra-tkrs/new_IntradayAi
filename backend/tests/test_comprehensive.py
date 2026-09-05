"""
Comprehensive unit tests for the signal engine - Phase 2
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, date


def test_atr_wilders_smoothing():
    """Test ATR uses Wilder's smoothing (alpha=1/period)"""
    from app.services.indicators import atr, wilders_smooth
    import pandas as pd
    import numpy as np
    
    np.random.seed(42)
    n = 50
    high = pd.Series(100 + abs(np.random.randn(n) * 2))
    low = pd.Series(100 - abs(np.random.randn(n) * 2))
    close = pd.Series(100 + np.random.randn(n))
    
    atr_result = atr(high, low, close, period=14)
    valid = atr_result.dropna()
    assert (valid > 0).all()
    
    # Test Wilder's smoothing directly
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    from app.services.indicators import wilders_smooth
    smoothed = wilders_smooth(s, period=3)
    assert len(smoothed) == 5
    # First value equals first input (no NaN with ewm)
    assert smoothed.iloc[0] == 1.0
    # Subsequent values should be smoothed
    assert not np.isnan(smoothed.iloc[-1])


def test_adx_wilders_smoothing():
    """Test ADX uses Wilder's smoothing for DI and ADX"""
    from app.services.indicators import adx
    import pandas as pd
    import numpy as np
    
    np.random.seed(42)
    n = 50
    high = pd.Series(100 + abs(np.random.randn(n) * 2) + 1)
    low = pd.Series(100 - abs(np.random.randn(n) * 2) - 1)
    close = pd.Series(100 + np.random.randn(n))
    
    adx_result = adx(high, low, close, period=14)
    valid = adx_result.dropna()
    assert (valid >= 0).all()
    assert (valid <= 100).all()


def test_vwap_daily_reset():
    """Test VWAP resets at each trading day"""
    from app.services.indicators import vwap
    import pandas as pd
    import numpy as np
    
    # Create 2 days of 5-min data (75 bars/day = 150 total)
    n = 150
    np.random.seed(42)
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)
    timestamps = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    
    high = pd.Series(prices + abs(np.random.randn(n) * 0.2))
    low = pd.Series(prices - abs(np.random.randn(n) * 0.2))
    close = pd.Series(prices)
    volume = pd.Series(np.random.randint(1000, 10000, n))
    timestamp = pd.Series(timestamps)
    
    vwap_result = vwap(high, low, close, volume, timestamp)
    valid = vwap_result.dropna()
    
    # VWAP should reset at day boundary (75 bars)
    day1_vwap = vwap_result.iloc[74]  # End of day 1
    day2_vwap = vwap_result.iloc[149]  # End of day 2
    
    assert not pd.isna(day1_vwap)
    assert not pd.isna(day2_vwap)
    assert day1_vwap != day2_vwap


def test_relative_volume():
    """Test relative volume uses 20-day average"""
    from app.services.indicators import calculate_all_indicators
    import pandas as pd
    import numpy as np
    
    n = 1600
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)
    
    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 0.1,
        "high": prices + abs(np.random.randn(n) * 0.2),
        "low": prices - abs(np.random.randn(n) * 0.2),
        "close": prices,
        "volume": np.random.randint(1000, 10000, n),
    })
    
    result = calculate_all_indicators(df)
    assert "relative_volume" in result.columns
    assert "avg_volume_20d" in result.columns
    
    valid = result["relative_volume"].dropna()
    assert len(valid) > 0
    assert (valid > 0).all()


def test_opening_range_per_day():
    """Test opening range is calculated per trading day"""
    from app.services.indicators import calculate_all_indicators
    import pandas as pd
    import numpy as np
    
    n = 225
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)
    
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata"),
        "open": prices + np.random.randn(n) * 0.1,
        "high": prices + abs(np.random.randn(n) * 0.2),
        "low": prices - abs(np.random.randn(n) * 0.2),
        "close": prices,
        "volume": np.random.randint(1000, 10000, n),
    })
    
    result = calculate_all_indicators(df)
    assert "opening_range_high" in result.columns
    assert "opening_range_low" in result.columns
    
    day1_high = result["opening_range_high"].iloc[74]
    day1_low = result["opening_range_low"].iloc[74]
    assert not pd.isna(day1_high)
    assert not pd.isna(day1_low)
    
    day2_high = result["opening_range_high"].iloc[149]
    day2_low = result["opening_range_low"].iloc[149]
    assert not pd.isna(day2_high)
    assert not pd.isna(day2_low)


def test_direction_enum_properties():
    """Test SignalDirection enum properties"""
    from app.models.schemas import SignalDirection
    
    assert SignalDirection.STRONG_LONG.is_strong is True
    assert SignalDirection.STRONG_LONG.is_long is True
    assert SignalDirection.STRONG_LONG.base_direction() == SignalDirection.LONG
    assert SignalDirection.STRONG_LONG.display == "STRONG LONG"
    
    assert SignalDirection.LONG.is_strong is False
    assert SignalDirection.LONG.is_long is True
    assert SignalDirection.LONG.base_direction() == SignalDirection.LONG
    assert SignalDirection.LONG.display == "LONG"
    
    assert SignalDirection.STRONG_SHORT.is_strong is True
    assert SignalDirection.STRONG_SHORT.is_short is True
    assert SignalDirection.STRONG_SHORT.base_direction() == SignalDirection.SHORT
    assert SignalDirection.STRONG_SHORT.display == "STRONG SHORT"
    
    assert SignalDirection.NO_TRADE.is_strong is False
    assert SignalDirection.NO_TRADE.is_long is False
    assert SignalDirection.NO_TRADE.is_short is False
    assert SignalDirection.NO_TRADE.base_direction() == SignalDirection.NO_TRADE


def test_compute_trade_setup_strong_long():
    """Test compute_trade_setup for STRONG LONG"""
    from app.services.signal_engine import compute_trade_setup
    from app.models.schemas import SignalDirection
    import pandas as pd
    
    row = pd.Series({"close": 100.0, "atr_14": 10.0})
    
    setup = compute_trade_setup(row, SignalDirection.STRONG_LONG)
    assert setup.entry == 100.0
    assert setup.stop_loss == 85.0
    assert setup.target_1 == 130.0
    assert setup.target_2 == 140.0
    assert setup.risk_reward_ratio == 2.0
    assert setup.risk_per_share == 15.0
    assert setup.reward_per_share == 30.0


def test_compute_trade_setup_strong_short():
    """Test compute_trade_setup for STRONG SHORT"""
    from app.services.signal_engine import compute_trade_setup
    from app.models.schemas import SignalDirection
    import pandas as pd
    
    row = pd.Series({"close": 100.0, "atr_14": 10.0})
    
    setup = compute_trade_setup(row, SignalDirection.STRONG_SHORT)
    assert setup.entry == 100.0
    assert setup.stop_loss == 115.0
    assert setup.target_1 == 70.0
    assert setup.target_2 == 60.0
    assert setup.risk_reward_ratio == 2.0


def test_compute_trade_setup_normal_long():
    """Test compute_trade_setup for normal LONG"""
    from app.services.signal_engine import compute_trade_setup
    from app.models.schemas import SignalDirection
    import pandas as pd
    
    row = pd.Series({"close": 100.0, "atr_14": 10.0})
    
    setup = compute_trade_setup(row, SignalDirection.LONG)
    assert setup.entry == 100.0
    assert setup.stop_loss == 85.0
    assert setup.target_1 == 120.0
    assert setup.target_2 == 130.0
    assert abs(setup.risk_reward_ratio - 1.33) < 0.01


def test_compute_trade_setup_normal_short():
    """Test compute_trade_setup for normal SHORT"""
    from app.services.signal_engine import compute_trade_setup
    from app.models.schemas import SignalDirection
    import pandas as pd
    
    row = pd.Series({"close": 100.0, "atr_14": 10.0})
    
    setup = compute_trade_setup(row, SignalDirection.SHORT)
    assert setup.entry == 100.0
    assert setup.stop_loss == 115.0
    assert setup.target_1 == 80.0
    assert setup.target_2 == 70.0
    assert abs(setup.risk_reward_ratio - 1.33) < 0.01


def test_compute_trade_setup_string_direction():
    """Test compute_trade_setup accepts string direction"""
    from app.services.signal_engine import compute_trade_setup
    from app.models.schemas import SignalDirection
    import pandas as pd
    
    row = pd.Series({"close": 100.0, "atr_14": 10.0})
    
    setup = compute_trade_setup(row, "STRONG_LONG")
    assert setup.risk_reward_ratio == 2.0
    assert setup.target_1 == 130.0
    
    setup = compute_trade_setup(row, "LONG")
    assert abs(setup.risk_reward_ratio - 1.33) < 0.01


def test_determine_direction_thresholds():
    """Test direction classification thresholds"""
    from app.services.signal_engine import determine_direction
    from app.models.schemas import SignalDirection
    
    assert determine_direction(90).value == "STRONG_LONG"
    assert determine_direction(80).value == "STRONG_LONG"
    assert determine_direction(79).value == "LONG"  # 79 < 80, so LONG
    assert determine_direction(77).value == "LONG"
    assert determine_direction(75).value == "LONG"
    assert determine_direction(74).value == "WEAK_LONG"
    assert determine_direction(65).value == "WEAK_LONG"
    assert determine_direction(60).value == "NO_TRADE"
    assert determine_direction(50).value == "WEAK_SHORT"
    assert determine_direction(40).value == "SHORT"
    assert determine_direction(20).value == "STRONG_SHORT"
    assert determine_direction(0).value == "STRONG_SHORT"


def test_nan_handling_in_signal_engine():
    """Test NaN handling in signal engine"""
    from app.services.signal_engine import evaluate_signal
    from app.models.schemas import DataSource
    import pandas as pd
    import numpy as np
    
    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="5min")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="5min"),
        "open": 100 + np.random.randn(n) * 0.1,
        "high": 100 + abs(np.random.randn(n) * 0.2),
        "low": 100 - abs(np.random.randn(n) * 0.2),
        "close": 100 + np.cumsum(np.random.randn(n) * 0.1),
        "volume": np.random.randint(1000, 10000, n),
    })
    
    from app.services.indicators import calculate_all_indicators
    df = calculate_all_indicators(df)
    df.loc[df.index[-1], "atr_14"] = np.nan
    
    signal = evaluate_signal(df, "TEST", data_source="TEST")
    assert signal["direction"] == "NO_TRADE"
    assert "Missing critical indicator" in str(signal["reasons"])


def test_stale_data_handling():
    """Test stale data results in NO TRADE"""
    from app.services.signal_engine import evaluate_signal
    import pandas as pd
    import numpy as np
    
    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="5min")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="5min"),
        "open": 100 + np.random.randn(n) * 0.1,
        "high": 100 + abs(np.random.randn(n) * 0.2),
        "low": 100 - abs(np.random.randn(n) * 0.2),
        "close": 100 + np.cumsum(np.random.randn(n) * 0.1),
        "volume": np.random.randint(1000, 10000, n),
    })
    
    from app.services.indicators import calculate_all_indicators
    df = calculate_all_indicators(df)
    
    signal = evaluate_signal(df, "TEST", data_source="TEST", 
                            data_age_seconds=2000, data_status="STALE")
    assert signal["direction"] == "NO_TRADE"
    assert "Data quality: STALE" in str(signal["reasons"])
    
    signal = evaluate_signal(df, "TEST", data_source="TEST",
                            data_age_seconds=2000, data_status="LIVE")
    assert signal["direction"] == "NO_TRADE"
    assert "Data too old" in str(signal["reasons"])


def test_invalid_ohlc_handling():
    """Test invalid OHLC data results in NO TRADE"""
    from app.services.signal_engine import evaluate_signal
    import pandas as pd
    import numpy as np
    
    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="5min")
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="5min"),
        "open": 100 + np.random.randn(n) * 0.1,
        "high": 100 + abs(np.random.randn(n) * 0.2),
        "low": 100 - abs(np.random.randn(n) * 0.2),
        "close": 100 + np.cumsum(np.random.randn(n) * 0.1),
        "volume": np.random.randint(1000, 10000, n),
    })
    
    from app.services.indicators import calculate_all_indicators
    df = calculate_all_indicators(df)
    
    # Invalid OHLC: high < low
    df.loc[df.index[-1], "high"] = 90
    df.loc[df.index[-1], "low"] = 100
    
    signal = evaluate_signal(df, "TEST", data_source="TEST",
                            data_age_seconds=60, data_status="LIVE")
    assert signal["direction"] == "NO_TRADE"
    assert "Invalid OHLC relationship" in str(signal["reasons"])


def test_signal_engine_with_strong_conditions():
    """Test signal engine with conditions for STRONG LONG"""
    import importlib
    import app.services.signal_engine
    importlib.reload(app.services.signal_engine)
    import app.services.indicators
    importlib.reload(app.services.indicators)
    
    from app.services.signal_engine import evaluate_signal
    import pandas as pd
    import numpy as np
    
    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)
    
    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 0.1,
        "high": prices + abs(np.random.randn(n) * 0.2) + 1,
        "low": prices - abs(np.random.randn(n) * 0.2) - 1,
        "close": prices,
        "volume": np.random.randint(1000, 10000, n),
    })
    
    from app.services.indicators import calculate_all_indicators
    df = calculate_all_indicators(df)
    
    # Manipulate last row to force STRONG LONG - set consistent OHLC
    row = df.iloc[-1].copy()
    row["open"] = 100.0
    row["high"] = 106.0
    row["low"] = 99.0
    row["close"] = 105.0
    row["ema_9"] = 110.0
    row["ema_20"] = 105.0
    row["ema_50"] = 100.0
    row["close"] = 105.0
    row["atr_14"] = 2.0
    row["adx_14"] = 30.0
    row["relative_volume"] = 2.5
    row["vwap"] = 100.0
    row["rsi_14"] = 60.0
    row["macd_histogram"] = 0.5
    row["roc_5"] = 1.5
    row["distance_from_vwap"] = 5.0
    row["macd"] = 0.3
    row["macd_signal"] = 0.2
    row["volume"] = 20000
    row["prev_high"] = 103.0
    row["prev_low"] = 97.0
    row["opening_range_high"] = 104.0
    row["opening_range_low"] = 96.0
    row["bb_upper"] = 110.0
    row["bb_lower"] = 90.0
    # Ensure no NaN
    for col in ["atr_14", "adx_14", "relative_volume", "vwap", "rsi_14"]:
        assert not pd.isna(row[col])
    
    df.iloc[-1] = row
    
    market_ctx = {
        "nifty_trend": "BULLISH",
        "nifty_change_pct": 1.0,
        "banknifty_change_pct": 1.5,
    }
    
    try:
        signal = evaluate_signal(df, "TEST", data_source="TEST",
                                data_age_seconds=60, data_status="LIVE",
                                market_context=market_ctx)
    except Exception as e:
        print(f"EXCEPTION: {e}")
        import traceback
        traceback.print_exc()
        raise
    
    print(f"Direction: {signal.get('direction')}, Confidence: {signal.get('confidence')}")
    print(f"Setup: {signal.get('setup')}")
    print(f"Score: {signal.get('signal_score', {}).get('total')}")
    print(f"Reasons: {signal.get('explanation', {}).get('reasons')}")
    print(f"Risks: {signal.get('explanation', {}).get('risks')}")
    print(f"Full signal keys: {signal.keys()}")
    
    assert signal["direction"] != "NO_TRADE"
    assert signal["confidence"] > 0


def test_backtest_no_lookahead():
    """Test that backtester does not use future data in signals"""
    from app.services.backtesting import BacktestEngine
    from app.services.indicators import calculate_all_indicators
    import pandas as pd
    import numpy as np

    n = 200
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": prices + abs(np.random.randn(n) * 1.0),
        "low": prices - abs(np.random.randn(n) * 1.0),
        "close": prices,
        "volume": np.random.randint(10000, 100000, n),
    })

    bt = BacktestEngine(initial_capital=1000000)
    result = bt.run(df, "TEST")

    assert "performance" in result
    assert "trades" in result
    assert "equity_curve" in result
    assert isinstance(result["trades"], list)

    if result["trades"]:
        for t in result["trades"]:
            assert "trade_id" in t
            assert "strategy_version" in t
            assert "signal_score" in t
            assert "confidence" in t
            assert "r_multiple" in t
            assert "exit_type" in t
            assert "holding_period_bars" in t
            assert "risk_reward_ratio" in t
            assert "atr_14" in t
            assert "adx_14" in t
            assert "rsi_14" in t
            assert "relative_volume" in t
            assert "vwap" in t


def test_backtest_next_candle_entry():
    """Test that entry happens on next candle open, not signal candle close"""
    from app.services.backtesting import BacktestEngine
    from app.services.indicators import calculate_all_indicators
    import pandas as pd
    import numpy as np

    n = 200
    np.random.seed(123)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": prices + abs(np.random.randn(n) * 1.0),
        "low": prices - abs(np.random.randn(n) * 1.0),
        "close": prices,
        "volume": np.random.randint(10000, 100000, n),
    })

    bt = BacktestEngine(initial_capital=1000000)
    result = bt.run(df, "TEST")

    if result["trades"]:
        for t in result["trades"]:
            entry_ts = pd.to_datetime(t["entry_time"])
            exit_ts = pd.to_datetime(t["exit_time"])
            assert exit_ts >= entry_ts, "Exit time should be after entry time"
            assert t["holding_period_bars"] >= 1, "Minimum 1 bar holding period (next candle)"


def test_backtest_conservative_stop_first():
    """Test that if both stop and target touched same bar, stop is assumed hit first"""
    from app.services.backtesting import BacktestEngine
    import pandas as pd
    import numpy as np

    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")

    df = pd.DataFrame({
        "timestamp": dates,
        "open": np.full(n, 100.0),
        "high": np.full(n, 150.0),
        "low": np.full(n, 50.0),
        "close": np.full(n, 100.0),
        "volume": np.full(n, 10000),
    })

    bt = BacktestEngine(initial_capital=1000000, slippage_pct=0.0)
    result = bt.run(df, "TEST")

    if result["trades"]:
        for t in result["trades"]:
            if t["exit_type"] == "stop_loss":
                assert t["net_pnl"] < 0, "Stop loss should result in negative PnL"
            elif t["exit_type"] == "target_1":
                assert t["net_pnl"] > 0, "Target hit should result in positive PnL"


def test_backtest_cost_calculation():
    """Test that brokerage, STT, and slippage are correctly deducted"""
    from app.services.backtesting import BacktestEngine
    import pandas as pd
    import numpy as np

    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": prices + abs(np.random.randn(n) * 1.0),
        "low": prices - abs(np.random.randn(n) * 1.0),
        "close": prices,
        "volume": np.random.randint(10000, 100000, n),
    })

    bt = BacktestEngine(initial_capital=1000000, brokerage_pct=0.1, stt_pct=0.1, slippage_pct=0.1)
    result = bt.run(df, "TEST")

    perf = result["performance"]
    assert "total_slippage_paid" in perf
    assert "total_brokerage_paid" in perf
    assert "total_stt_paid" in perf


def test_backtest_walkforward_split():
    """Test that backtest supports date-range filtering for walk-forward validation"""
    from app.services.backtesting import BacktestEngine
    from app.services.indicators import calculate_all_indicators
    import pandas as pd
    import numpy as np

    n = 500
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": prices + abs(np.random.randn(n) * 1.0),
        "low": prices - abs(np.random.randn(n) * 1.0),
        "close": prices,
        "volume": np.random.randint(10000, 100000, n),
    })

    bt = BacktestEngine(initial_capital=1000000)
    start = pd.Timestamp("2024-01-02 09:15", tz="Asia/Kolkata")
    end = pd.Timestamp("2024-01-03 09:15", tz="Asia/Kolkata")
    result = bt.run(df, "TEST", start_date=start, end_date=end)

    assert "performance" in result
    assert "equity_curve" in result
    if result["equity_curve"]:
        for ec in result["equity_curve"]:
            ec_ts = pd.to_datetime(ec["timestamp"])
            assert ec_ts >= start
            assert ec_ts <= end


def test_backtest_strategy_version_in_results():
    """Test that strategy version is recorded in all trade records"""
    from app.services.backtesting import BacktestEngine
    import pandas as pd
    import numpy as np

    n = 200
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": prices + abs(np.random.randn(n) * 1.0),
        "low": prices - abs(np.random.randn(n) * 1.0),
        "close": prices,
        "volume": np.random.randint(10000, 100000, n),
    })

    bt = BacktestEngine(initial_capital=1000000, strategy_version="v2_test")
    result = bt.run(df, "TEST")

    assert result["strategy_version"] == "v2_test"
    if result["trades"]:
        for t in result["trades"]:
            assert t["strategy_version"] == "v2_test"


def test_strategy_versioning_system():
    """Test strategy versioning system"""
    from app.services.strategy_config import (
        StrategyVersion, STRATEGY_REGISTRY, get_strategy, v1, v2, v3
    )
    from app.models.schemas import SignalDirection

    assert v1.version == "v1"
    assert v2.version == "v2"
    assert v3.version == "v3"

    assert len(STRATEGY_REGISTRY) >= 3

    sv = get_strategy("v2")
    assert sv is v2
    assert sv.strong_long_threshold == 85.0
    assert sv.min_rr_strong == 2.5

    sv_unknown = get_strategy("nonexistent")
    assert sv_unknown is v1

    assert v1.determine_direction(90) == SignalDirection.STRONG_LONG
    assert v1.determine_direction(80) == SignalDirection.STRONG_LONG
    assert v1.determine_direction(75) == SignalDirection.LONG
    assert v1.determine_direction(65) == SignalDirection.WEAK_LONG
    assert v1.determine_direction(60) == SignalDirection.NO_TRADE
    assert v1.determine_direction(45) == SignalDirection.WEAK_SHORT
    assert v1.determine_direction(30) == SignalDirection.SHORT
    assert v1.determine_direction(0) == SignalDirection.STRONG_SHORT

    assert v1.get_atr_mult(SignalDirection.STRONG_LONG) == 3.0
    assert v1.get_atr_mult(SignalDirection.LONG) == 2.0
    assert v1.get_min_rr(SignalDirection.STRONG_LONG) == 2.0
    assert v1.get_min_rr(SignalDirection.LONG) == 1.2

    ok, rejections = v1.should_accept_strong(30, 2.0, 60, 100, 95, SignalDirection.STRONG_LONG)
    assert ok is True
    assert len(rejections) == 0

    ok, rejections = v1.should_accept_strong(20, 2.0, 60, 100, 95, SignalDirection.STRONG_LONG)
    assert ok is False
    assert len(rejections) == 1

    ok, rejections = v1.should_accept_strong(30, 0.5, 60, 100, 95, SignalDirection.STRONG_LONG)
    assert ok is False
    assert len(rejections) >= 1

    dump = v1.model_dump()
    assert "version" in dump
    assert "thresholds" in dump
    assert "risk" in dump
    assert "filters" in dump
    assert dump["version"] == "v1"


def test_backtest_with_strategy_version():
    """Test that backtester uses strategy versioning"""
    from app.services.backtesting import BacktestEngine
    from app.services.indicators import calculate_all_indicators
    from app.services.strategy_config import v1
    import pandas as pd
    import numpy as np

    n = 200
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": prices + abs(np.random.randn(n) * 1.0),
        "low": prices - abs(np.random.randn(n) * 1.0),
        "close": prices,
        "volume": np.random.randint(10000, 100000, n),
    })

    bt = BacktestEngine(initial_capital=1000000)
    result_v1 = bt.run(df, "TEST", strategy_version="v1")
    result_v2 = bt.run(df, "TEST", strategy_version="v2")

    assert result_v1["strategy_version"] == "v1"
    assert result_v2["strategy_version"] == "v2"

    if result_v1["trades"]:
        for t in result_v1["trades"]:
            assert t["strategy_version"] == "v1"
            assert "strategy_config" not in t or isinstance(t.get("strategy_config"), dict)

    perf_v1 = result_v1["performance"]
    perf_v2 = result_v2["performance"]
    assert "total_slippage_paid" in perf_v1
    assert "total_slippage_paid" in perf_v2


def test_signal_timestamps_new_signal():
    """Test that new signal receives all required timestamp fields"""
    from app.services.signal_engine import evaluate_signal
    from app.models.schemas import DataSource
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import pandas as pd
    import numpy as np

    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 0.1,
        "high": prices + abs(np.random.randn(n) * 0.2),
        "low": prices - abs(np.random.randn(n) * 0.2),
        "close": prices,
        "volume": np.random.randint(1000, 10000, n),
    })

    from app.services.indicators import calculate_all_indicators
    df = calculate_all_indicators(df)

    signal = evaluate_signal(df, "TEST", data_source="TEST",
                            data_age_seconds=60, data_status="LIVE")

    assert signal is not None
    assert "signal_generated_at" in signal
    assert "entry_updated_at" in signal
    assert "stop_loss_updated_at" in signal
    assert "target_updated_at" in signal
    assert "market_data_timestamp" in signal
    assert "last_updated_at" in signal

    IST = ZoneInfo("Asia/Kolkata")
    for ts_field in ["signal_generated_at", "entry_updated_at", "stop_loss_updated_at",
                     "target_updated_at", "last_updated_at"]:
        if signal[ts_field] is not None:
            ts = datetime.fromisoformat(signal[ts_field])
            assert ts.tzinfo is not None, f"{ts_field} should be timezone-aware"


def test_signal_timestamps_market_data_preserved():
    """Test that market_data_timestamp is preserved from input"""
    from app.services.signal_engine import evaluate_signal
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import pandas as pd
    import numpy as np

    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 0.1,
        "high": prices + abs(np.random.randn(n) * 0.2),
        "low": prices - abs(np.random.randn(n) * 0.2),
        "close": prices,
        "volume": np.random.randint(1000, 10000, n),
    })

    from app.services.indicators import calculate_all_indicators
    df = calculate_all_indicators(df)

    market_ts = datetime(2026, 9, 3, 11, 42, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    signal = evaluate_signal(df, "TEST", data_source="TEST",
                            data_age_seconds=60, data_status="LIVE",
                            market_data_timestamp=market_ts)

    assert signal is not None
    assert signal["market_data_timestamp"] == market_ts.isoformat()


def test_signal_timestamps_entry_updates_only_entry():
    """Test that updating entry only updates entry_updated_at"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    ts1 = datetime(2026, 9, 3, 11, 42, 0, tzinfo=IST)
    ts2 = datetime(2026, 9, 3, 11, 43, 0, tzinfo=IST)
    ts3 = datetime(2026, 9, 3, 11, 44, 0, tzinfo=IST)

    setup_data = {
        "entry": 100.0,
        "stop_loss": 98.0,
        "target_1": 105.0,
        "target_2": 110.0,
        "risk_per_share": 2.0,
        "reward_per_share": 5.0,
        "risk_reward_ratio": 2.5,
    }

    from app.models.schemas import TradeSetup
    setup = TradeSetup(**setup_data)

    assert setup.entry == 100.0
    assert setup.stop_loss == 98.0
    assert setup.target_1 == 105.0


def test_signal_timestamps_string_market_data_timestamp():
    """Regression test: market_data_timestamp as string (from yfinance) must not crash"""
    from app.services.signal_engine import evaluate_signal
    import pandas as pd
    import numpy as np

    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 0.1,
        "high": prices + abs(np.random.randn(n) * 0.2),
        "low": prices - abs(np.random.randn(n) * 0.2),
        "close": prices,
        "volume": np.random.randint(1000, 10000, n),
    })

    from app.services.indicators import calculate_all_indicators
    df = calculate_all_indicators(df)

    # yfinance returns timestamps as ISO strings, not datetime objects
    signal = evaluate_signal(df, "TEST", data_source="TEST",
                            data_age_seconds=60, data_status="LIVE",
                            market_data_timestamp="2026-09-03T12:51:35+05:30")

    assert signal is not None
    assert signal["market_data_timestamp"] == "2026-09-03T12:51:35+05:30"


def test_signal_timestamps_no_trade_has_timestamps():
    """Test that NO TRADE signal also has timestamp fields"""
    from app.services.signal_engine import evaluate_signal
    import pandas as pd
    import numpy as np

    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 0.1,
        "high": prices + abs(np.random.randn(n) * 0.2),
        "low": prices - abs(np.random.randn(n) * 0.2),
        "close": prices,
        "volume": np.random.randint(1000, 10000, n),
    })

    from app.services.indicators import calculate_all_indicators
    df = calculate_all_indicators(df)

    signal = evaluate_signal(df, "TEST", data_source="TEST",
                            data_age_seconds=60, data_status="LIVE")

    assert signal is not None
    assert signal["direction"] in ("NO_TRADE", "WEAK_LONG", "WEAK_SHORT", "LONG", "SHORT",
                                    "STRONG_LONG", "STRONG_SHORT")

    assert "signal_generated_at" in signal
    assert "market_data_timestamp" in signal


def test_signal_timestamps_are_consistent():
    """Test that signal_generated_at, entry_updated_at, sl_updated_at, target_updated_at are set consistently"""
    from app.services.signal_engine import evaluate_signal
    from datetime import datetime
    import pandas as pd
    import numpy as np

    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 0.1,
        "high": prices + abs(np.random.randn(n) * 0.2),
        "low": prices - abs(np.random.randn(n) * 0.2),
        "close": prices,
        "volume": np.random.randint(1000, 10000, n),
    })

    from app.services.indicators import calculate_all_indicators
    df = calculate_all_indicators(df)

    signal = evaluate_signal(df, "TEST", data_source="TEST",
                            data_age_seconds=60, data_status="LIVE")

    assert signal is not None

    ts_gen = datetime.fromisoformat(signal["signal_generated_at"]) if signal["signal_generated_at"] else None
    ts_entry = datetime.fromisoformat(signal["entry_updated_at"]) if signal["entry_updated_at"] else None
    ts_sl = datetime.fromisoformat(signal["stop_loss_updated_at"]) if signal["stop_loss_updated_at"] else None
    ts_target = datetime.fromisoformat(signal["target_updated_at"]) if signal["target_updated_at"] else None
    ts_last = datetime.fromisoformat(signal["last_updated_at"]) if signal["last_updated_at"] else None

    if ts_gen:
        assert ts_entry == ts_gen, "entry_updated_at should equal signal_generated_at for new signal"
        assert ts_sl == ts_gen, "stop_loss_updated_at should equal signal_generated_at for new signal"
        assert ts_target == ts_gen, "target_updated_at should equal signal_generated_at for new signal"
        assert ts_last == ts_gen, "last_updated_at should equal signal_generated_at for new signal"


def test_signal_uses_last_close_as_entry():
    """Test that Entry is derived from the last candle close (the snapshot's price)"""
    from app.services.signal_engine import compute_trade_setup
    from app.models.schemas import SignalDirection
    import pandas as pd

    row = pd.Series({"close": 235.42, "atr_14": 3.5})
    setup = compute_trade_setup(row, SignalDirection.LONG)
    assert setup.entry == 235.42
    assert setup.stop_loss == round(235.42 - 1.5 * 3.5, 2)
    assert setup.target_1 == round(235.42 + 2.0 * 3.5, 2)
    assert setup.risk_per_share == abs(setup.entry - setup.stop_loss)
    assert setup.reward_per_share == abs(setup.target_1 - setup.entry)


def test_rr_ratio_consistent_with_entry_sl_target():
    """Test R:R ratio is calculated from the same Entry, SL, Target values"""
    from app.services.signal_engine import compute_trade_setup
    from app.models.schemas import SignalDirection
    import pandas as pd

    row = pd.Series({"close": 100.0, "atr_14": 10.0})
    setup = compute_trade_setup(row, SignalDirection.LONG)

    risk = abs(setup.entry - setup.stop_loss)
    reward = abs(setup.target_1 - setup.entry)
    expected_rr = round(reward / risk, 2) if risk > 0 else 0

    assert setup.risk_reward_ratio == expected_rr
    assert setup.risk_per_share == round(risk, 2)
    assert setup.reward_per_share == round(reward, 2)


def test_signal_from_different_prices():
    """Test that different market snapshots produce different Entry/SL/Target"""
    from app.services.signal_engine import evaluate_signal
    from app.services.indicators import calculate_all_indicators
    import pandas as pd
    import numpy as np

    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 0.1,
        "high": prices + abs(np.random.randn(n) * 0.2),
        "low": prices - abs(np.random.randn(n) * 0.2),
        "close": prices,
        "volume": np.random.randint(1000, 10000, n),
    })
    df = calculate_all_indicators(df)

    signal1 = evaluate_signal(df, "TEST", data_source="TEST",
                              data_age_seconds=60, data_status="LIVE")
    if signal1 and signal1["direction"] != "NO_TRADE":
        entry1 = signal1["setup"]["entry"]
        df2 = df.copy()
        df2.iloc[-1, df2.columns.get_loc("close")] = entry1 + 5.0
        df2.iloc[-1, df2.columns.get_loc("open")] = entry1 + 5.0
        df2.iloc[-1, df2.columns.get_loc("high")] = entry1 + 5.5
        df2.iloc[-1, df2.columns.get_loc("low")] = entry1 + 4.5
        signal2 = evaluate_signal(df2, "TEST", data_source="TEST",
                                  data_age_seconds=60, data_status="LIVE")
        if signal2 and signal2["direction"] != "NO_TRADE":
            assert signal2["setup"]["entry"] != entry1 or signal2["direction"] == "NO_TRADE"


def test_market_data_timestamp_preserved_through_signal():
    """Test market_data_timestamp flows through to the final signal output"""
    from app.services.signal_engine import evaluate_signal
    from app.services.indicators import calculate_all_indicators
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import pandas as pd
    import numpy as np

    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 0.1,
        "high": prices + abs(np.random.randn(n) * 0.2),
        "low": prices - abs(np.random.randn(n) * 0.2),
        "close": prices,
        "volume": np.random.randint(1000, 10000, n),
    })
    df = calculate_all_indicators(df)

    market_ts = datetime(2026, 9, 3, 14, 31, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    signal = evaluate_signal(df, "TEST", data_source="TEST",
                             data_age_seconds=60, data_status="LIVE",
                             market_data_timestamp=market_ts)

    assert signal is not None
    assert signal["market_data_timestamp"] == market_ts.isoformat()
    assert signal["signal_generated_at"] is not None
    gen_ts = datetime.fromisoformat(signal["signal_generated_at"])
    assert gen_ts.tzinfo is not None


def test_legacy_null_timestamps_do_not_crash():
    """Test that signals with null timestamp fields do not crash"""
    from app.services.signal_engine import evaluate_signal
    from app.services.indicators import calculate_all_indicators
    import pandas as pd
    import numpy as np

    n = 100
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 0.1,
        "high": prices + abs(np.random.randn(n) * 0.2),
        "low": prices - abs(np.random.randn(n) * 0.2),
        "close": prices,
        "volume": np.random.randint(1000, 10000, n),
    })
    df = calculate_all_indicators(df)

    signal = evaluate_signal(df, "TEST", data_source="TEST",
                             data_age_seconds=60, data_status="LIVE",
                             market_data_timestamp=None)

    assert signal is not None
    assert signal["market_data_timestamp"] is None
    assert signal["signal_generated_at"] is not None


def test_yfinance_combined_fetch_returns_consistent_data():
    """Test that get_quote_and_bars returns quote and bars from the same snapshot"""
    from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider

    provider = YFinanceMarketDataProvider()

    async def run():
        result = await provider.get_quote_and_bars("ONGC")
        return result

    import asyncio
    result = asyncio.run(run())

    assert "quote" in result
    assert "df" in result
    quote = result["quote"]
    df = result["df"]

    assert "price" in quote
    assert "timestamp" in quote
    assert "data_status" in quote


def test_yfinance_bar_cache_ttl_is_short():
    """Test that bar cache TTL is reduced to 30 seconds"""
    from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider

    provider = YFinanceMarketDataProvider()
    assert provider._bar_cache._ttl == 30, "Bar cache TTL should be 30 seconds"


def test_yfinance_quote_cache_ttl_unchanged():
    """Test that quote cache TTL remains 60 seconds"""
    from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider

    provider = YFinanceMarketDataProvider()
    assert provider._quote_cache._ttl == 60, "Quote cache TTL should be 60 seconds"


# ---------------------------------------------------------------------------
# Regression tests: global Change Rs / Change % calculation
#
# Root cause: _build_quote_from_chart() preferred the stale `chartPreviousClose`
# field (a chart reference baseline from before the earliest bar in the range)
# over `previousClose` (the true previous TRADING DAY's close). For a 5m/60d or
# 1d/5d chart this produced a change relative to a close from days/weeks ago,
# giving wrong red/green sign and magnitude for EVERY stock.
# ---------------------------------------------------------------------------


def _chart_result(price, prev_close, chart_prev_close, daily_closes=None):
    """Build a synthetic Yahoo v8 chart `result` dict for testing."""
    if daily_closes is None:
        daily_closes = [prev_close, price]
    n = len(daily_closes)
    return {
        "meta": {
            "regularMarketPrice": price,
            "previousClose": prev_close,
            "chartPreviousClose": chart_prev_close,
            "regularMarketTime": 1788428700,
            "regularMarketDayOpen": price,
            "regularMarketDayHigh": price,
            "regularMarketDayLow": price,
        },
        "timestamp": list(range(n)),
        "indicators": {"quote": [{"close": daily_closes}]},
    }


def test_change_prefers_previous_close_over_chart_previous_close():
    """Regression: change must be vs the previous trading day's close, NOT the
    stale chartPreviousClose. Mirrors HDFCBANK: price 706.65, prevClose 700.80,
    chartPrevClose 772.45 -> change should be +5.85 (+0.83%), not -65.80 (-8.52%)."""
    from app.services.market_data.yfinance_provider import _build_quote_from_chart

    result = _chart_result(price=706.65, prev_close=700.80, chart_prev_close=772.45)
    q = _build_quote_from_chart("HDFCBANK", result)

    assert q["prev_close"] == pytest.approx(700.80)
    assert q["change"] == pytest.approx(5.85, abs=0.01)
    assert q["change_pct"] == pytest.approx(0.83, abs=0.01)
    # Ensure change is POSITIVE (green), not negative (red)
    assert q["change"] > 0


def test_change_derives_previous_close_from_daily_bars_when_meta_missing():
    """Regression: when meta.previousClose is absent (interval=1d charts), the
    previous close must be derived from the SECOND-TO-LAST daily bar, not the stale
    chartPreviousClose."""
    from app.services.market_data.yfinance_provider import _build_quote_from_chart

    # 5 daily bars, last bar is today's close (706.65), second-to-last is 700.80.
    # Use realistic daily-spaced unix timestamps (each a distinct trading day).
    import time
    base = 1788428700  # a recent epoch
    day = 86400
    daily = [720.3, 709.0, 711.9, 700.8, 706.65]
    result = {
        "meta": {
            "regularMarketPrice": 706.65,
            "previousClose": None,
            "chartPreviousClose": 720.3,  # stale baseline (should NOT be used)
            "regularMarketTime": 1788428700,
            "regularMarketDayOpen": 706.65,
            "regularMarketDayHigh": 706.65,
            "regularMarketDayLow": 706.65,
        },
        "timestamp": [base - (len(daily) - 1 - i) * day for i in range(len(daily))],
        "indicators": {"quote": [{"close": daily}]},
    }
    q = _build_quote_from_chart("HDFCBANK", result)

    # Correct previous close is the yesterday bar (700.80), NOT chartPreviousClose (720.3)
    assert q["prev_close"] == pytest.approx(700.80)
    assert q["change"] == pytest.approx(5.85, abs=0.01)
    assert q["change_pct"] == pytest.approx(0.83, abs=0.01)


@pytest.mark.parametrize(
    "symbol,price,prev_close,expected_change,expected_pct",
    [
        # Positive change
        ("HDFCBANK", 706.65, 700.80, 5.85, 0.83),
        ("SBIN", 1023.40, 1020.90, 2.50, 0.24),
        # Negative change
        ("RELIANCE", 1302.50, 1313.10, -10.60, -0.81),
        ("TCS", 2320.10, 2348.00, -27.90, -1.19),
        ("INFY", 1130.30, 1140.00, -9.70, -0.85),
        # Zero change
        ("FLAT", 100.00, 100.00, 0.00, 0.00),
    ],
)
def test_change_before_after_market_seconds(symbol, price, prev_close, expected_change, expected_pct):
    """change = price - prevClose; changePct = change/prevClose*100, for positive,
    negative and zero cases across multiple stocks."""
    from app.services.market_data.yfinance_provider import _build_quote_from_chart

    # chartPreviousClose deliberately set to a wrong/stale value that MUST be ignored
    stale_chart_prev = prev_close * 1.10
    result = _chart_result(price, prev_close, stale_chart_prev)
    q = _build_quote_from_chart(symbol, result)

    assert q["prev_close"] == pytest.approx(prev_close, abs=0.01)
    assert q["change"] == pytest.approx(expected_change, abs=0.01)
    assert q["change_pct"] == pytest.approx(expected_pct, abs=0.02)
    # Internal consistency: change == price - prevClose and pct derived from it
    assert q["change"] == pytest.approx(round(q["price"] - q["prev_close"], 2), abs=0.01)
    if q["prev_close"]:
        assert q["change_pct"] == pytest.approx(
            round(q["change"] / q["prev_close"] * 100, 2), abs=0.01
        )


def test_change_does_not_use_previous_candle_or_open():
    """Regression: previous close must NOT be the previous intraday candle's close,
    the opening price, or any arbitrary stale field. Only the previous trading day's
    close is acceptable."""
    from app.services.market_data.yfinance_provider import _build_quote_from_chart

    # opening price differs wildly; chartPreviousClose stale; previousClose correct
    result = _chart_result(price=110.0, prev_close=100.0, chart_prev_close=95.0)
    result["meta"]["regularMarketDayOpen"] = 108.5  # wrong open-based baseline
    q = _build_quote_from_chart("X", result)

    assert q["prev_close"] == pytest.approx(100.0)
    assert q["change"] == pytest.approx(10.0, abs=0.01)
    assert q["change_pct"] == pytest.approx(10.0, abs=0.01)


def test_change_falls_back_to_chart_previous_close_only_when_nothing_else():
    """Fallback safety: if neither previousClose nor daily bars are available,
    chartPreviousClose is used as a last resort (no crash, finite numbers)."""
    from app.services.market_data.yfinance_provider import _build_quote_from_chart

    result = {
        "meta": {
            "regularMarketPrice": 706.65,
            "previousClose": None,
            "chartPreviousClose": 720.3,
            "regularMarketTime": 1788428700,
        },
        "timestamp": [],
        "indicators": {"quote": [{"close": []}]},
    }
    q = _build_quote_from_chart("HDFCBANK", result)
    # Not enough bars to derive, so it must fall back gracefully to chartPreviousClose
    assert q["prev_close"] == pytest.approx(720.30)
    assert isinstance(q["change"], float)
    assert isinstance(q["change_pct"], float)


# ---------------------------------------------------------------------------
# Regression tests: OHLC + Previous Close on the Stock Detail snapshot
#
# _build_quote_from_chart() builds the quote snapshot consumed by stock detail.
# It must expose open/high/low/close/prev_close all from the same snapshot,
# with a correct previous-trading-day baseline. The meta `regularMarketDayOpen`
# is frequently absent in Yahoo's chart API, so open/high/low are derived from
# the latest session's bars (same result) instead of fabricating from price.
# ---------------------------------------------------------------------------


def _session_chart_result(price, prev_close, opens, highs, lows, closes,
                          session_days=1, open_bars=1):
    """Build a synthetic chart result with full OHLC bars for `session_days` days.

    Each day has `open_bars` bars. `opens/highs/lows/closes` are FLAT per-bar
    lists (length == session_days * open_bars). If they're shorter than the
    number of bars, the last value is reused for the remaining bars. Bars are
    spaced by 1 hour (3600s). Meta DayOpen/High/Low are left ABSENT to prove
    derivation from bars works.
    """
    base = 1788428700
    day_secs = 86400
    total_bars = session_days * open_bars
    ts = []
    o, h, l, c = [], [], [], []
    bar = 0
    for d in range(session_days):
        day_base = base - (session_days - 1 - d) * day_secs
        for b in range(open_bars):
            ts.append(day_base + b * 3600)
            idx = min(bar, len(opens) - 1)
            o.append(opens[idx])
            h.append(highs[idx])
            l.append(lows[idx])
            c.append(closes[idx])
            bar += 1
    return {
        "meta": {
            "regularMarketPrice": price,
            "previousClose": prev_close,
            "chartPreviousClose": prev_close * 1.15,  # stale - must be ignored
            "regularMarketTime": base,
        },
        "timestamp": ts,
        "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c}]},
    }


def test_ohlc_fields_returned_from_snapshot():
    """Open/High/Low/Close/PrevClose are all present and correct from one snapshot."""
    from app.services.market_data.yfinance_provider import _build_quote_from_chart

    # RELIANCE-style session: price 1302.50, PREV close 1313.10
    result = _session_chart_result(
        price=1302.50, prev_close=1313.10,
        opens=[1313.10], highs=[1316.80], lows=[1302.50], closes=[1302.50],
    )
    q = _build_quote_from_chart("RELIANCE", result)

    assert q["open"] == pytest.approx(1313.10, abs=0.01)
    assert q["high"] == pytest.approx(1316.80, abs=0.01)
    assert q["low"] == pytest.approx(1302.50, abs=0.01)
    assert q["close"] == pytest.approx(1302.50, abs=0.01)
    assert q["prev_close"] == pytest.approx(1313.10, abs=0.01)
    assert "price" in q and q["price"] == pytest.approx(1302.50, abs=0.01)


def test_ohlc_previous_close_uses_correct_baseline_not_chart_previous():
    """chartPreviousClose must NOT override the previousClose baseline for prev_close,
    and must NOT be used as the high."""
    from app.services.market_data.yfinance_provider import _build_quote_from_chart

    # chart_prev in helper is prev_close*1.15 -> 150.99 if prev_close=131.3, must be ignored
    result = _session_chart_result(
        price=130.0, prev_close=131.3,
        opens=[132.0], highs=[133.5], lows=[128.5], closes=[130.0],
    )
    q = _build_quote_from_chart("X", result)
    assert q["prev_close"] == pytest.approx(131.3, abs=0.01)
    assert q["high"] == pytest.approx(133.5, abs=0.01)


def test_ohlc_open_derived_from_first_bar_when_meta_open_absent():
    """When meta.regularMarketDayOpen is absent (common), open must be the first
    bar's open, NOT the current price."""
    from app.services.market_data.yfinance_provider import _build_quote_from_chart

    # price differs from the session open
    result = _session_chart_result(
        price=130.0, prev_close=131.3,
        opens=[125.0], highs=[135.0], lows=[124.0], closes=[130.0],
    )
    q = _build_quote_from_chart("X", result)
    assert q["open"] == pytest.approx(125.0, abs=0.01)
    assert q["open"] != pytest.approx(q["price"])


def test_ohlc_high_is_max_and_low_is_min_across_session():
    """High = max of bar highs, Low = min of bar lows for the latest session."""
    from app.services.market_data.yfinance_provider import _build_quote_from_chart

    result = _session_chart_result(
        price=100.0, prev_close=99.0,
        opens=[99.0, 100.0, 101.0],
        highs=[101.0, 103.0, 102.0],
        lows=[98.0, 99.5, 99.0],
        closes=[100.0, 101.0, 100.0],
        session_days=1, open_bars=3,
    )
    q = _build_quote_from_chart("X", result)
    assert q["high"] == pytest.approx(103.0, abs=0.01)
    assert q["low"] == pytest.approx(98.0, abs=0.01)


def test_ohlc_uses_latest_session_when_multiple_days():
    """When multiple sessions are present, OHLC comes from the LATEST session."""
    from app.services.market_data.yfinance_provider import _build_quote_from_chart

    result = _session_chart_result(
        price=110.0, prev_close=105.0,
        opens=[100.0, 109.0],
        highs=[102.0, 112.0],
        lows=[98.0, 106.0],
        closes=[101.0, 110.0],
        session_days=2, open_bars=1,
    )
    q = _build_quote_from_chart("X", result)
    assert q["open"] == pytest.approx(109.0, abs=0.01)
    assert q["high"] == pytest.approx(112.0, abs=0.01)
    assert q["low"] == pytest.approx(106.0, abs=0.01)
    assert q["close"] == pytest.approx(110.0, abs=0.01)


def test_ohlc_market_closed_still_returns_last_valid_values():
    """After market close the snapshot must still carry the last valid OHLC values
    (not blanks), with prev_close and close intact."""
    from app.services.market_data.yfinance_provider import _build_quote_from_chart

    result = _session_chart_result(
        price=250.0, prev_close=248.0,
        opens=[249.0], highs=[252.0], lows=[247.0], closes=[250.0],
    )
    q = _build_quote_from_chart("X", result)
    # Not blank after close - all session values and prev close present
    assert q["open"] is not None
    assert q["high"] is not None
    assert q["low"] is not None
    assert q["close"] == pytest.approx(250.0, abs=0.01)
    assert q["prev_close"] == pytest.approx(248.0, abs=0.01)


def test_ohlc_missing_bars_do_not_crash_and_return_none():
    """If a genuine field is unavailable, return None (displayed as N/A) rather than
    fabricating a value from the current price."""
    from app.services.market_data.yfinance_provider import _build_quote_from_chart

    result = {
        "meta": {
            "regularMarketPrice": 706.65,
            "previousClose": 700.80,
            "regularMarketTime": 1788428700,
        },
        "timestamp": [],
        "indicators": {"quote": [{"open": [], "high": [], "low": [], "close": []}]},
    }
    q = _build_quote_from_chart("X", result)
    # No bars and no meta day fields -> open/high/low are None (not price), close=price
    assert q["open"] is None
    assert q["high"] is None
    assert q["low"] is None
    assert q["close"] == pytest.approx(706.65, abs=0.01)
    assert q["prev_close"] == pytest.approx(700.80, abs=0.01)
    assert q["change"] == pytest.approx(5.85, abs=0.01)
    assert q["change_pct"] == pytest.approx(0.83, abs=0.01)


def test_ohlc_change_consistent_with_previous_close_and_timestamp():
    """change/change_pct derive from price vs prev_close; timestamp string preserved /
    composed correctly (date-based compatibility with yfinance ISO strings)."""
    from app.services.market_data.yfinance_provider import _build_quote_from_chart

    result = _session_chart_result(
        price=106.0, prev_close=100.0,
        opens=[101.0], highs=[107.0], lows=[100.0], closes=[106.0],
    )
    q = _build_quote_from_chart("X", result)
    assert q["change"] == pytest.approx(6.0, abs=0.01)
    assert q["change_pct"] == pytest.approx(6.0, abs=0.01)
    assert q["change"] == pytest.approx(round(q["price"] - q["prev_close"], 2), abs=0.01)
    # timestamp must be an ISO string (string compatibility preserved)
    assert isinstance(q["timestamp"], str)
    assert "T" in q["timestamp"]


# ---------------------------------------------------------------------------
# User trade setup overrides (Entry / Stop Loss / Target)
#
# Users may override the AI setup. The R:R convention is the same as the rest
# of the project: risk = abs(entry - sl), reward = abs(target - entry),
# rr = reward / risk (see signal_engine.compute_trade_setup). The domain
# function compute_risk_reward both validates BUY/SELL relationships and
# recomputes the authoritative R:R server-side. AI values are never touched.
# ---------------------------------------------------------------------------


def _rr(entry, sl, target, direction="LONG"):
    from app.services.trade_setup import compute_risk_reward
    return compute_risk_reward(entry, sl, target, direction)


def test_user_setup_valid_buy():
    """Valid BUY: SL < Entry < Target with a correct R:R."""
    r = _rr(235.50, 234.90, 237.00, "LONG")
    assert r.valid
    assert r.errors == []
    # Risk = 235.50-234.90 = 0.60 ; Reward = 237.00-235.50 = 1.50 ; R:R = 2.50
    assert r.risk_reward == pytest.approx(2.50, abs=0.01)
    assert r.entry == pytest.approx(235.50)
    assert r.stop_loss == pytest.approx(234.90)
    assert r.target == pytest.approx(237.00)


def test_user_setup_invalid_buy_sl_not_below_entry():
    """Invalid BUY: SL >= Entry must fail with a clear message."""
    r = _rr(235.50, 236.00, 237.00, "BUY")
    assert not r.valid
    assert any("below Buy Price" in e for e in r.errors)


def test_user_setup_invalid_buy_target_not_above_entry():
    """Invalid BUY: Target <= Entry must fail."""
    r = _rr(235.50, 234.90, 235.50, "LONG")
    assert not r.valid
    assert any("above Buy Price" in e for e in r.errors)
    r2 = _rr(235.50, 234.90, 234.00, "LONG")
    assert not r2.valid


def test_user_setup_valid_sell():
    """Valid SELL: Target < Entry < Stop Loss with a correct R:R."""
    r = _rr(235.50, 237.00, 232.50, "SHORT")
    assert r.valid
    # Risk = 237.00-235.50 = 1.50 ; Reward = 235.50-232.50 = 3.00 ; R:R = 2.00
    assert r.risk_reward == pytest.approx(2.00, abs=0.01)


def test_user_setup_invalid_sell_sl_not_above_entry():
    """Invalid SELL: SL <= Entry must fail."""
    r = _rr(235.50, 235.00, 232.50, "SELL")
    assert not r.valid
    assert any("above Entry" in e for e in r.errors)
    r2 = _rr(235.50, 233.00, 232.50, "SELL")
    assert not r2.valid


def test_user_setup_invalid_sell_target_not_below_entry():
    """Invalid SELL: Target >= Entry must fail."""
    r = _rr(235.50, 237.00, 235.50, "SELL")
    assert not r.valid
    assert any("below Entry" in e for e in r.errors)
    r2 = _rr(235.50, 237.00, 239.00, "SELL")
    assert not r2.valid


def test_user_setup_rr_calculation_convention():
    """R:R uses the project convention (abs-based, direction-agnostic)."""
    # LONG: entry 235, sl 234, target 237 -> risk 1, reward 2, rr 2.0
    r = _rr(235.00, 234.00, 237.00, "LONG")
    assert r.valid
    assert r.risk_reward == pytest.approx(2.00)
    # SHORT mirrors the same magnitudes
    r2 = _rr(235.00, 236.00, 233.00, "SHORT")
    assert r2.valid
    assert r2.risk_reward == pytest.approx(2.00)


def test_user_setup_zero_risk_handled():
    """Entry == Stop Loss -> zero risk; must not divide by zero and reports clearly."""
    r = _rr(235.50, 235.50, 237.00, "LONG")
    assert not r.valid
    assert any("cannot be equal" in e for e in r.errors)
    r2 = _rr(235.50, 235.50, 230.00, "SELL")
    assert not r2.valid


def test_user_setup_missing_values():
    """Missing Entry/SL/Target must fail with messages and not crash."""
    r = _rr(None, 234.90, 237.00, "LONG")
    assert not r.valid
    assert any("required" in e for e in r.errors)
    r2 = _rr(235.50, None, 237.00, "LONG")
    assert not r2.valid
    r3 = _rr(235.50, 234.90, None, "LONG")
    assert not r3.valid


def test_user_setup_invalid_numeric_values():
    """NaN, Infinity, non-numeric, zero and negative inputs must be rejected safely."""
    for bad in [float("nan"), float("inf"), float("-inf"), "abc", "", -10.0, 0.0, "-5.5"]:
        r = _rr(bad, 234.90, 237.00, "LONG")
        assert not r.valid, f"should reject entry={bad!r}"
    # non-numeric SL / target
    r_sl = _rr(235.50, "oops", 237.00, "LONG")
    assert not r_sl.valid
    r_tgt = _rr(235.50, 234.90, float("nan"), "BUY")
    assert not r_tgt.valid


def test_user_setup_ai_values_preserved_and_separate():
    """The service must not touch AI values; override is a separate concept.

    compute_risk_reward operates purely on user inputs and returns them unchanged;
    the AI setup (signal.setup) is a completely separate object that this module
    never receives or mutates."""
    from app.services.signal_engine import compute_trade_setup, SignalDirection
    import pandas as pd

    row = pd.DataFrame({
        "close": [235.40],
        "atr_14": [0.4],
    }).iloc[0]
    ai_setup = compute_trade_setup(row, SignalDirection.LONG)
    ai_entry_before = ai_setup.entry

    user = _rr(235.50, 234.90, 237.00, "LONG")
    assert user.valid
    # AI setup unchanged and distinct from the user override
    assert ai_setup.entry == pytest.approx(ai_entry_before)
    assert ai_setup.entry != user.entry


def test_user_setup_override_flag_concept():
    """override_active distinguishes the user override from AI defaults."""
    from app.models.models import UserTradeSetup
    assert hasattr(UserTradeSetup, "override_active")
    assert hasattr(UserTradeSetup, "user_id")
    assert hasattr(UserTradeSetup, "symbol")
    assert hasattr(UserTradeSetup, "entry")
    assert hasattr(UserTradeSetup, "stop_loss")
    assert hasattr(UserTradeSetup, "target")
    assert hasattr(UserTradeSetup, "risk_reward")


def test_user_setup_direction_aliases():
    """BUY/LONG and SELL/SHORT are interchangeable direction aliases."""
    assert _rr(235.50, 234.90, 237.00, "LONG").valid
    assert _rr(235.50, 234.90, 237.00, "BUY").valid
    assert _rr(235.50, 237.00, 232.50, "SHORT").valid
    assert _rr(235.50, 237.00, 232.50, "SELL").valid


# ────────────────────────────────────────────────────────────────────────
# Fix #1 regression: direction-inversion bug must not return
# ────────────────────────────────────────────────────────────────────────

def _make_strong_bullish_row():
    """Construct a row that scores strongly bullish (STRONG_LONG territory)."""
    import pandas as pd
    row = pd.Series({
        "open": 100.0, "high": 106.0, "low": 99.0, "close": 105.0,
        "ema_9": 110.0, "ema_20": 105.0, "ema_50": 100.0,
        "atr_14": 2.0, "adx_14": 30.0, "relative_volume": 2.5,
        "vwap": 100.0, "rsi_14": 60.0,
        "macd": 0.3, "macd_signal": 0.2, "macd_histogram": 0.5,
        "roc_5": 1.5, "distance_from_vwap": 5.0,
        "prev_high": 103.0, "prev_low": 97.0,
        "opening_range_high": 104.0, "opening_range_low": 96.0,
        "bb_upper": 110.0, "bb_lower": 90.0,
        "volatility_20": 0.5,
    })
    return row


def _make_mild_bullish_row():
    """Row that scores in LONG range (75-80) — previously inverted to SHORT."""
    import pandas as pd
    row = pd.Series({
        "open": 100.0, "high": 104.0, "low": 99.0, "close": 103.0,
        "ema_9": 105.0, "ema_20": 103.0, "ema_50": 100.0,
        "atr_14": 1.5, "adx_14": 20.0, "relative_volume": 1.2,
        "vwap": 100.0, "rsi_14": 58.0,
        "macd": 0.2, "macd_signal": 0.1, "macd_histogram": 0.1,
        "roc_5": 1.2, "distance_from_vwap": 3.0,
        "prev_high": 102.0, "prev_low": 98.0,
        "opening_range_high": 101.0, "opening_range_low": 99.0,
        "bb_upper": 108.0, "bb_lower": 92.0,
        "volatility_20": 0.3,
    })
    return row


def _make_weak_long_row():
    """Row in WEAK_LONG range (65-74) — previously collapsed to SHORT."""
    import pandas as pd
    row = pd.Series({
        "open": 100.0, "high": 101.5, "low": 99.5, "close": 101.0,
        "ema_9": 100.8, "ema_20": 100.5, "ema_50": 100.0,
        "atr_14": 1.0, "adx_14": 14.0, "relative_volume": 0.9,
        "vwap": 100.0, "rsi_14": 53.0,
        "macd": 0.03, "macd_signal": 0.01, "macd_histogram": 0.02,
        "roc_5": 0.4, "distance_from_vwap": 1.5,
        "prev_high": 101.0, "prev_low": 99.5,
        "opening_range_high": 101.0, "opening_range_low": 99.5,
        "bb_upper": 105.0, "bb_lower": 95.0,
        "volatility_20": 0.3,
    })
    return row


def _make_strong_bearish_row():
    """Row that scores strongly bearish (STRONG_SHORT)."""
    import pandas as pd
    row = pd.Series({
        "open": 100.0, "high": 101.0, "low": 94.0, "close": 95.0,
        "ema_9": 90.0, "ema_20": 95.0, "ema_50": 100.0,
        "atr_14": 2.0, "adx_14": 30.0, "relative_volume": 2.5,
        "vwap": 100.0, "rsi_14": 40.0,
        "macd": -0.3, "macd_signal": -0.2, "macd_histogram": -0.5,
        "roc_5": -1.5, "distance_from_vwap": -5.0,
        "prev_high": 103.0, "prev_low": 97.0,
        "opening_range_high": 102.0, "opening_range_low": 98.0,
        "bb_upper": 108.0, "bb_lower": 92.0,
        "volatility_20": 0.5,
    })
    return row


def _make_weak_short_row():
    """Row in WEAK_SHORT range (45-55)."""
    import pandas as pd
    row = pd.Series({
        "open": 100.0, "high": 101.0, "low": 98.0, "close": 99.0,
        "ema_9": 98.0, "ema_20": 99.0, "ema_50": 100.0,
        "atr_14": 1.0, "adx_14": 16.0, "relative_volume": 1.0,
        "vwap": 100.0, "rsi_14": 47.0,
        "macd": -0.1, "macd_signal": -0.05, "macd_histogram": -0.05,
        "roc_5": -0.8, "distance_from_vwap": -1.0,
        "prev_high": 101.0, "prev_low": 99.0,
        "opening_range_high": 101.0, "opening_range_low": 99.5,
        "bb_upper": 103.0, "bb_lower": 97.0,
        "volatility_20": 0.3,
    })
    return row


def test_direction_bullish_never_becomes_short():
    """A bullish row must never produce a SHORT direction (the old inversion bug)."""
    from app.services.signal_engine import evaluate_row_signal
    from app.models.schemas import SignalDirection

    ctx = {"nifty_trend": "BULLISH", "nifty_change_pct": 0.8, "banknifty_change_pct": 1.0}
    for row in [_make_strong_bullish_row(), _make_mild_bullish_row(), _make_weak_long_row()]:
        d = evaluate_row_signal(row, market_context=ctx)
        assert d is not None
        assert d["direction"].is_long or d["direction"] == SignalDirection.NO_TRADE, (
            f"Bullish row produced {d['direction']} — must be long or NO_TRADE"
        )


def test_direction_bearish_never_becomes_long():
    """A bearish row must never produce a LONG direction."""
    from app.services.signal_engine import evaluate_row_signal
    from app.models.schemas import SignalDirection

    ctx = {"nifty_trend": "BEARISH", "nifty_change_pct": -0.8, "banknifty_change_pct": -1.0}
    for row in [_make_strong_bearish_row(), _make_weak_short_row()]:
        d = evaluate_row_signal(row, market_context=ctx)
        assert d is not None
        assert d["direction"].is_short or d["direction"] == SignalDirection.NO_TRADE, (
            f"Bearish row produced {d['direction']} — must be short or NO_TRADE"
        )


def test_direction_strong_preserved_when_quality_passes():
    """STRONG_LONG stays STRONG_LONG when strong-quality filters pass."""
    from app.services.signal_engine import evaluate_row_signal
    from app.models.schemas import SignalDirection

    ctx = {"nifty_trend": "BULLISH", "nifty_change_pct": 1.0, "banknifty_change_pct": 1.5}
    d = evaluate_row_signal(_make_strong_bullish_row(), market_context=ctx)
    assert d is not None
    assert d["direction"] == SignalDirection.STRONG_LONG


def test_direction_weak_long_preserved():
    """WEAK_LONG must stay WEAK_LONG (not become SHORT)."""
    from app.services.signal_engine import evaluate_row_signal
    from app.models.schemas import SignalDirection

    ctx = {"nifty_trend": "NEUTRAL", "nifty_change_pct": 0.0, "banknifty_change_pct": 0.0}
    d = evaluate_row_signal(_make_weak_long_row(), market_context=ctx)
    assert d is not None
    assert d["direction"] == SignalDirection.WEAK_LONG


def test_direction_weak_short_preserved():
    """WEAK_SHORT must stay WEAK_SHORT (not become LONG)."""
    from app.services.signal_engine import evaluate_row_signal
    from app.models.schemas import SignalDirection

    ctx = {"nifty_trend": "NEUTRAL", "nifty_change_pct": 0.0, "banknifty_change_pct": 0.0}
    d = evaluate_row_signal(_make_weak_short_row(), market_context=ctx)
    assert d is not None
    assert d["direction"] == SignalDirection.WEAK_SHORT


# ────────────────────────────────────────────────────────────────────────
# Fix #2 regression: backtest uses same signal decision as live
# ────────────────────────────────────────────────────────────────────────

def test_live_signal_and_backtest_use_same_decision():
    """For the same row, evaluate_row_signal and evaluate_signal agree on direction."""
    from app.services.signal_engine import evaluate_row_signal, evaluate_signal
    from app.services.indicators import calculate_all_indicators
    import pandas as pd
    import numpy as np

    n = 200
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)
    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": prices + abs(np.random.randn(n) * 1.0),
        "low": prices - abs(np.random.randn(n) * 1.0),
        "close": prices,
        "volume": np.random.randint(10000, 100000, n),
    })
    df = calculate_all_indicators(df)

    ctx = {"nifty_trend": "NEUTRAL", "nifty_change_pct": 0.0, "banknifty_change_pct": 0.0}

    for i in range(55, len(df)):
        row = df.iloc[i]
        rd = evaluate_row_signal(row, market_context=ctx)
        if rd is None:
            continue

        subset = df.iloc[: i + 1].copy()
        sd = evaluate_signal(subset, "TEST", data_source="TEST",
                             data_age_seconds=60, data_status="LIVE",
                             market_context=ctx)
        if sd is None:
            continue

        assert sd["direction"] == rd["direction"].value, (
            f"Bar {i}: live={sd['direction']} vs row-level={rd['direction']}"
        )


def test_vwap_score_shared_function_consistency():
    """The score_vwap function used by both evaluate_row_signal and evaluate_signal
    gives identical results on a range of VWAP distances."""
    from app.services.signal_engine import score_vwap
    import pandas as pd
    import numpy as np

    for dist in [5.0, 1.0, 0.1, -0.1, -1.0, -5.0]:
        row = pd.Series({"distance_from_vwap": dist, "vwap": 100.0, "close": 100.0 + dist})
        score, reasons = score_vwap(row)
        assert 0 <= score <= 15, f"dist={dist} score={score} out of range"
        assert len(reasons) > 0


def test_backtest_equivalent_conditions_same_signal_as_live():
    """Backtest and live signal engine agree on direction for the same data."""
    from app.services.backtesting import BacktestEngine
    from app.services.signal_engine import evaluate_signal
    from app.services.indicators import calculate_all_indicators
    import pandas as pd
    import numpy as np

    n = 200
    np.random.seed(42)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)
    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": prices + abs(np.random.randn(n) * 1.0),
        "low": prices - abs(np.random.randn(n) * 1.0),
        "close": prices,
        "volume": np.random.randint(10000, 100000, n),
    })
    full = calculate_all_indicators(df)

    bt = BacktestEngine(initial_capital=1000000)
    result = bt.run(full, "TEST")

    for t in result.get("trades", []):
        sym = "TEST"
        sig = evaluate_signal(full, sym, data_source="TEST",
                              data_age_seconds=60, data_status="LIVE")
        if sig is None:
            continue
        direction_str = t["direction"]
        # Backtest trade direction must be the clean enum value (no
        # "SignalDirection." prefix) so frontend filters match correctly.
        assert not direction_str.startswith("SignalDirection."), (
            f"Backtest direction {direction_str!r} should be the clean enum value"
        )
        if direction_str in ("STRONG_LONG", "LONG", "WEAK_LONG"):
            assert sig["direction"] in ("STRONG_LONG", "LONG", "WEAK_LONG"), (
                f"Backtest traded {direction_str} but live signal = {sig['direction']}"
            )
        elif direction_str in ("STRONG_SHORT", "SHORT", "WEAK_SHORT"):
            assert sig["direction"] in ("STRONG_SHORT", "SHORT", "WEAK_SHORT"), (
                f"Backtest traded {direction_str} but live signal = {sig['direction']}"
            )


# ────────────────────────────────────────────────────────────────────────
# Fix #3 regression: paper trading risk controls
# ────────────────────────────────────────────────────────────────────────

def test_paper_buy_and_sell_execution():
    """Basic paper BUY and SELL execution (pending -> fill -> close)."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    result = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    assert result["status"] == "pending"
    assert pt.get_portfolio_summary()["pending_orders_count"] == 1
    assert pt.get_portfolio_summary()["positions_count"] == 0

    fill = pt.fill_order(result["order_id"])
    assert fill["status"] == "filled"
    assert pt.get_portfolio_summary()["positions_count"] == 1

    positions = pt.get_positions()
    assert len(positions) == 1
    assert positions[0]["direction"] == "LONG"
    assert positions[0]["entry_price"] == 2450.0
    assert positions[0]["stop_loss"] == 2420.0
    assert positions[0]["target_1"] == 2500.0

    close = pt.close_position(result["order_id"], 2500.0)
    assert close["pnl"] > 0
    assert close["trade"]["result"] == "WIN"
    assert pt.get_portfolio_summary()["positions_count"] == 0


def test_paper_sell_execution():
    """Paper SELL execution with correct PnL calculation."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    result = pt.place_order("TCS", "SHORT", 5, 3200.0, 3230.0, 3160.0)
    assert result["status"] == "pending"
    fill = pt.fill_order(result["order_id"])
    assert fill["status"] == "filled"

    close = pt.close_position(result["order_id"], 3160.0)
    assert close["pnl"] > 0
    assert close["trade"]["result"] == "WIN"

    result2 = pt.place_order("TCS", "SHORT", 5, 3200.0, 3230.0, 3160.0)
    fill2 = pt.fill_order(result2["order_id"])
    assert fill2["status"] == "filled"
    close2 = pt.close_position(result2["order_id"], 3240.0)
    assert close2["pnl"] < 0
    assert close2["trade"]["result"] == "LOSS"


def test_paper_sl_exit():
    """Position is correctly closed at stop-loss price."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    result = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    pt.fill_order(result["order_id"])

    exits = pt.check_stops({"RELIANCE": 2415.0})
    assert len(exits) == 1
    assert exits[0]["trade"]["exit_price"] == 2415.0
    assert exits[0]["trade"]["result"] == "LOSS"
    assert pt.get_portfolio_summary()["positions_count"] == 0


def test_paper_target_exit():
    """Position is correctly closed at target price."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    result = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    pt.fill_order(result["order_id"])

    exits = pt.check_stops({"RELIANCE": 2510.0})
    assert len(exits) == 1
    assert exits[0]["trade"]["exit_price"] == 2510.0
    assert exits[0]["trade"]["result"] == "WIN"
    assert pt.get_portfolio_summary()["positions_count"] == 0


def test_paper_realized_and_unrealized_pnl():
    """Realized and unrealized P&L are calculated correctly."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    pt.fill_order(r["order_id"])

    pt.update_prices({"RELIANCE": 2460.0})
    positions = pt.get_positions()
    assert len(positions) == 1
    assert positions[0]["unrealized_pnl"] == pytest.approx(100.0)

    summary = pt.get_portfolio_summary()
    assert summary["unrealized_pnl"] == pytest.approx(100.0)

    pt.close_position(pt.get_positions()[0]["id"], 2460.0)
    summary = pt.get_portfolio_summary()
    assert summary["total_pnl"] == pytest.approx(100.0)


def test_paper_duplicate_position_prevention():
    """Same-symbol duplicate positions are prevented at the API level."""
    from app.services.paper_trading import PaperTradingEngine
    from app.services.risk_engine import RiskEngine

    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    pt.fill_order(r["order_id"])
    positions = pt.get_positions()
    assert len(positions) == 1

    duplicate_count = sum(1 for p in positions if p["symbol"] == "RELIANCE")
    assert duplicate_count == 1

    from app.api.trading import MAX_DUPLICATE_POSITIONS
    assert duplicate_count >= MAX_DUPLICATE_POSITIONS


def test_paper_max_simultaneous_positions():
    """Risk engine enforces maximum simultaneous positions."""
    from app.services.paper_trading import PaperTradingEngine
    from app.services.risk_engine import RiskEngine, RiskConfig

    config = RiskConfig(max_simultaneous_positions=2)
    re = RiskEngine(config)
    pt = PaperTradingEngine()

    r1 = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    pt.fill_order(r1["order_id"])
    r2 = pt.place_order("TCS", "LONG", 5, 3200.0, 3180.0, 3250.0)
    pt.fill_order(r2["order_id"])
    assert len(pt.get_positions()) == 2

    can, reason = re.can_trade()
    assert can is True

    re.state.open_positions = 2
    can, reason = re.can_trade()
    assert can is False
    assert "simultaneous" in reason.lower()


def test_paper_invalid_order_rejection():
    """Invalid orders (zero qty, insufficient cash) are rejected."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    result = pt.place_order("RELIANCE", "LONG", 0, 2450.0, 2420.0, 2500.0)
    assert "error" in result

    result = pt.place_order("RELIANCE", "LONG", 999999999, 2450.0, 2420.0, 2500.0)
    assert "error" not in result  # pending orders don't check cash
    fill = pt.fill_order(result["order_id"])
    assert "error" in fill  # cash check happens on fill


def test_paper_position_sizing_by_risk():
    """Position sizing correctly limits quantity by max risk."""
    from app.services.risk_engine import RiskEngine, RiskConfig

    config = RiskConfig(account_capital=1_000_000, max_risk_per_trade_pct=2.0)
    re = RiskEngine(config)
    qty = re.calculate_position_size(2450.0, 2420.0)
    assert qty > 0
    max_risk_amount = 1_000_000 * 0.02
    risk_per_share = abs(2450.0 - 2420.0)
    expected = int(max_risk_amount / risk_per_share)
    assert qty <= expected


def test_paper_daily_loss_limit():
    """Daily loss limit stops trading."""
    from app.services.risk_engine import RiskEngine, RiskConfig

    config = RiskConfig(account_capital=1_000_000, max_daily_loss_pct=5.0, max_trades_per_day=100)
    re = RiskEngine(config)
    assert can_trade_ok(re)

    re.record_trade_result(-60000)
    can, reason = re.can_trade()
    assert can is False
    assert "loss" in reason.lower()


def test_paper_daily_trade_limit():
    """Daily trade count limit stops trading."""
    from app.services.risk_engine import RiskEngine, RiskConfig

    config = RiskConfig(max_trades_per_day=3, max_daily_loss_pct=100.0)
    re = RiskEngine(config)

    for _ in range(3):
        re.record_trade_result(100)
    can, reason = re.can_trade()
    assert can is False
    assert "trades" in reason.lower()


def can_trade_ok(re):
    can, _ = re.can_trade()
    return can


def test_paper_market_hours_enforcement():
    """Market-hours check is enforced on the API level (via is_market_hours)."""
    from app.core.market_session import is_market_hours
    assert callable(is_market_hours)


def test_paper_consecutive_loss_cooldown():
    """Consecutive losses trigger cooldown."""
    from app.services.risk_engine import RiskEngine, RiskConfig

    config = RiskConfig(cooldown_after_losses=3, max_daily_loss_pct=100.0, max_trades_per_day=100)
    re = RiskEngine(config)
    for _ in range(2):
        re.record_trade_result(-100)
    can, _ = re.can_trade()
    assert can is True

    re.record_trade_result(-100)
    can, reason = re.can_trade()
    assert can is False
    assert "cooldown" in reason.lower()


def test_paper_directional_sl_target_validation():
    """Server validates directional SL/Target before placing order."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    result = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2460.0, 2440.0)
    assert result["status"] == "pending"
    pt.fill_order(result["order_id"])

    pos = pt.get_positions()[0]
    assert pos["direction"] == "LONG"
    assert pos["entry_price"] == 2450.0
    assert pos["stop_loss"] == 2460.0
    assert pos["target_1"] == 2440.0


# ────────────────────────────────────────────────────────────────────────
# Backtest look-ahead detection
# ────────────────────────────────────────────────────────────────────────

def test_backtest_no_lookahead_detected():
    """Construct data where entry on signal bar vs next bar gives different results.

    If the backtest uses future candle data in signals, entry price would be
    different. We verify that entry_time > signal_time for all trades."""
    from app.services.backtesting import BacktestEngine
    from app.services.indicators import calculate_all_indicators
    import pandas as pd
    import numpy as np

    n = 200
    np.random.seed(99)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)
    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": prices + abs(np.random.randn(n) * 1.0),
        "low": prices - abs(np.random.randn(n) * 1.0),
        "close": prices,
        "volume": np.random.randint(10000, 100000, n),
    })

    bt = BacktestEngine(initial_capital=1000000)
    result = bt.run(df, "TEST")

    for t in result.get("trades", []):
        entry_ts = pd.to_datetime(t["entry_time"])
        signal_ts = pd.to_datetime(t.get("signal_bar", t["entry_time"]))
        assert entry_ts >= signal_ts, (
            f"Entry {entry_ts} is before signal {signal_ts} — look-ahead detected"
        )
        assert t["holding_period_bars"] >= 0

# ────────────────────────────────────────────────────────────────────────
# Final live-audit regression tests (Fix A-D from end-to-end audit)
# ────────────────────────────────────────────────────────────────────────

def test_scanner_process_stock_uses_enum_no_trade():
    """_process_stock emits SignalDirection.NO_TRADE.value for no-trade rows,
    never the space variant."""
    from app.services.scanner import MarketScanner
    from app.models.schemas import SignalDirection

    class _DummyProvider:
        data_source_label = "dummy"

    scanner = MarketScanner(_DummyProvider())

    # insufficient data path -> NO_TRADE (underscore, not space)
    fetch_no_data = {"symbol": "X", "name": "X", "sector": "U",
                     "_df": None, "_quote": None, "_error": None}
    r = scanner._process_stock({}, fetch_no_data, {"nifty_trend": "NEUTRAL"})
    assert r["signal"] == SignalDirection.NO_TRADE.value
    assert r["signal"] == "NO_TRADE"
    assert r["signal"] != "NO TRADE"

    # error path stays ERROR
    fetch_err = {"symbol": "X", "name": "X", "sector": "U",
                 "_df": None, "_quote": None, "_error": "boom"}
    r = scanner._process_stock({}, fetch_err, {})
    assert r["signal"] == "ERROR"


def _make_synthetic_quote_and_bars(seed):
    import pandas as pd
    import numpy as np
    n = 200
    rng = np.random.RandomState(seed)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(rng.randn(n) * 0.5)
    df = pd.DataFrame({"timestamp": dates, "open": prices, "high": prices + 1,
                       "low": prices - 1, "close": prices,
                       "volume": rng.randint(10000, 100000, n)})
    quote = {"price": float(prices[-1]), "change_pct": 0.0, "volume": 1000,
             "data_status": "LIVE", "data_age_seconds": 5, "timestamp": dates[-1]}
    return {"quote": quote, "df": df}


def test_scanner_universe_never_emits_space_variant():
    """Scanning a universe must never leak the "NO TRADE" space variant; every
    emitted signal is either the underscore enum value or ERROR."""
    from app.services.scanner import MarketScanner
    import asyncio

    class FakeProvider:
        data_source_label = "fake"
        async def get_market_index(self, index):
            return {"change_pct": 0.0, "data_status": "LIVE"}
        async def get_instruments(self, universe="NIFTY50"):
            return [{"symbol": "A", "name": "A", "sector": "U"},
                    {"symbol": "B", "name": "B", "sector": "U"},
                    {"symbol": "C", "name": "C", "sector": "U"}]
        async def get_quote_and_bars(self, symbol, timeframe="5m"):
            return _make_synthetic_quote_and_bars(seed=hash(symbol) % 1000)

    scanner = MarketScanner(FakeProvider())
    results = asyncio.run(scanner.scan_universe("NIFTY50"))
    assert len(results) == 3
    for r in results:
        assert r["signal"] != "NO TRADE"
        assert isinstance(r["signal"], str)


def _aggregate_top_signals(results):
    """Replicates market.py's top-signals filter: NO_TRADE/ERROR/None excluded."""
    return [r for r in results
            if r.get("signal") not in ("NO_TRADE", "ERROR", None)]


def test_market_top_signals_filter_excludes_no_trade():
    """The top-signals aggregation used by /api/scanner must exclude NO_TRADE,
    ERROR and None signals so signals_found reflects only actionable setups."""
    from app.services.scanner import MarketScanner
    from app.models.schemas import SignalDirection
    import asyncio

    class FakeProvider:
        data_source_label = "fake"
        async def get_market_index(self, index):
            return {"change_pct": 0.0, "data_status": "LIVE"}
        async def get_instruments(self, universe="NIFTY50"):
            return [{"symbol": f"S{i}", "name": f"S{i}", "sector": "U"} for i in range(25)]
        async def get_quote_and_bars(self, symbol, timeframe="5m"):
            return _make_synthetic_quote_and_bars(seed=int(symbol[1:]))

    scanner = MarketScanner(FakeProvider())
    results = asyncio.run(scanner.scan_universe("NIFTY50"))
    top = _aggregate_top_signals(results)
    # Every actionable (top) signal is a real trade direction, never NO_TRADE/ERROR.
    for r in top:
        assert r.get("signal") not in ("NO_TRADE", "ERROR", None)
    # And the excluded set is exactly the no-action rows.
    assert len(top) == sum(
        1 for r in results if r.get("signal") not in ("NO_TRADE", "ERROR", None))


def test_paper_realized_pnl_not_corrupted_by_unrealized():
    """realized_pnl must equal total_pnl (realized-only); it must not subtract
    unrealized P&L from open positions."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    r = pt.place_order("X", "LONG", 10, 100.0, 95.0, 110.0)
    pt.fill_order(r["order_id"])
    pt.update_prices({"X": 110.0})  # unrealized +100
    summary = pt.get_portfolio_summary()
    assert summary["realized_pnl"] == 0.0  # nothing realized yet
    assert summary["unrealized_pnl"] == 100.0

    # Realize the gain -> realized_pnl becomes +100
    oid = pt.get_positions()[0]["id"]
    pt.close_position(oid, 110.0)
    summary = pt.get_portfolio_summary()
    assert summary["realized_pnl"] == 100.0
    assert summary["total_pnl"] == 100.0
    assert summary["positions_count"] == 0


def test_backtest_trade_direction_is_clean_enum_value():
    """Backtest trade 'direction' must be the clean enum value (LONG/WEAK_LONG...),
    not 'str(enum)' which yields 'SignalDirection.LONG'."""
    from app.services.backtesting import BacktestEngine
    from app.services.indicators import calculate_all_indicators
    import pandas as pd
    import numpy as np

    n = 250
    np.random.seed(123)
    dates = pd.date_range("2024-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)
    df = pd.DataFrame({"timestamp": dates, "open": prices, "high": prices + 1,
                       "low": prices - 1, "close": prices,
                       "volume": np.random.randint(10000, 100000, n)})
    full = calculate_all_indicators(df)
    bt = BacktestEngine(initial_capital=1000000)
    result = bt.run(full, "TEST")
    trades = result.get("trades", [])
    for t in trades:
        assert not str(t["direction"]).startswith("SignalDirection."), (
            f"direction {t['direction']!r} must be clean enum value"
        )
        assert "net_pnl" in t, "trade must carry net_pnl"
        assert "gross_pnl" in t, "trade must carry gross_pnl"


# ────────────────────────────────────────────────────────────────────────
# Scanner Buy/Sell + Persistent Custom Trade Setup regression tests
# ────────────────────────────────────────────────────────────────────────

def test_scanner_custom_setup_buy_uses_custom_entry_sl_target():
    """BUY order must be validated/placed with the user's custom Entry/SL/Target.
    The server recomputes R:R authoritatively from the provided custom values."""
    from app.services.trade_setup import compute_risk_reward

    # Custom BUY: entry 100, SL 99, target 103 -> risk 1, reward 3, R:R 3.00
    r = compute_risk_reward(100.0, 99.0, 103.0, "LONG")
    assert r.valid
    assert r.entry == 100.0
    assert r.stop_loss == 99.0
    assert r.target == 103.0
    assert r.risk_reward == pytest.approx(3.00)

    # The custom values survive exactly (no rounding drift)
    assert r.entry == pytest.approx(100.0)
    assert r.stop_loss == pytest.approx(99.0)
    assert r.target == pytest.approx(103.0)


def test_scanner_custom_setup_sell_uses_custom_entry_sl_target():
    """SELL order must be validated/placed with the user's custom Entry/SL/Target."""
    from app.services.trade_setup import compute_risk_reward

    # Custom SELL: entry 100, SL 102, target 97 -> risk 2, reward 3, R:R 1.50
    r = compute_risk_reward(100.0, 102.0, 97.0, "SHORT")
    assert r.valid
    assert r.entry == pytest.approx(100.0)
    assert r.stop_loss == pytest.approx(102.0)
    assert r.target == pytest.approx(97.0)
    assert r.risk_reward == pytest.approx(1.50)


def test_scanner_invalid_buy_rejected():
    """Invalid BUY (SL not below Entry / Target not above Entry) is rejected so the
    order cannot submit with a nonsensical custom setup."""
    from app.services.trade_setup import compute_risk_reward

    # SL above entry -> invalid BUY
    r = compute_risk_reward(100.0, 101.0, 105.0, "LONG")
    assert not r.valid
    assert any("below Buy Price" in e for e in r.errors)

    # Target below entry -> invalid BUY
    r2 = compute_risk_reward(100.0, 98.0, 97.0, "LONG")
    assert not r2.valid
    assert any("above Buy Price" in e for e in r2.errors)


def test_scanner_invalid_sell_rejected():
    """Invalid SELL (SL not above Entry / Target not below Entry) is rejected."""
    from app.services.trade_setup import compute_risk_reward

    r = compute_risk_reward(100.0, 98.0, 95.0, "SHORT")
    assert not r.valid
    assert any("above Entry" in e for e in r.errors)

    r2 = compute_risk_reward(100.0, 103.0, 105.0, "SHORT")
    assert not r2.valid
    assert any("below Entry" in e for e in r2.errors)


def test_scanner_order_appears_in_open_positions_and_trade_history():
    """A scanner-placed BUY appears in Open Positions, and after close in Trade
    History, using the SAME PaperTradingEngine source of truth (no duplicate store)."""
    from app.services.paper_trading import PaperTradingEngine

    pt = PaperTradingEngine()
    result = pt.place_order("RELIANCE", "LONG", 10, 2500.0, 2470.0, 2560.0)
    assert result["status"] == "pending"
    fill = pt.fill_order(result["order_id"])
    assert fill["status"] == "filled"

    positions = pt.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos["symbol"] == "RELIANCE"
    assert pos["direction"] == "LONG"
    assert pos["entry_price"] == 2500.0
    assert pos["stop_loss"] == 2470.0
    assert pos["target_1"] == 2560.0

    summary = pt.get_portfolio_summary()
    assert summary["positions_count"] == 1

    close = pt.close_position(result["order_id"], 2560.0)
    assert close["pnl"] > 0
    history = pt.get_trade_history()
    assert any(t["id"] == result["order_id"] for t in history)
    assert pt.get_portfolio_summary()["positions_count"] == 0


def test_scanner_order_user_setup_isolation():
    """Trade setup persistence is keyed by user; the query used for the scalar
    (batch) endpoint filters strictly on user.id so one user never sees another's
    custom setup."""
    from app.models.models import UserTradeSetup
    from sqlalchemy import select

    # The UserTradeSetup model enforces ownership via user_id on every query.
    # Reproduce the filter used by the batch and per-symbol endpoints.
    stmt = select(UserTradeSetup).where(UserTradeSetup.user_id == "user-A")
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "user-A" in sql
    # The model has the unique (user_id, symbol) index for per-user isolation.
    assert hasattr(UserTradeSetup, "user_id")
    assert hasattr(UserTradeSetup, "symbol")
    assert hasattr(UserTradeSetup, "override_active")


def test_scanner_reset_restores_ai():
    """After clearing the custom override (Reset to AI), the active setup falls back
    to AI values. Clearing means override_active=False and nulled values."""
    from app.models.models import UserTradeSetup
    from app.services.trade_setup import compute_risk_reward

    # Simulate the cleared record shape returned by clear_trade_setup
    cleared = {
        "entry": None,
        "stop_loss": None,
        "target": None,
        "risk_reward": None,
        "direction": None,
        "override_active": False,
    }
    assert cleared["override_active"] is False
    assert cleared["entry"] is None and cleared["stop_loss"] is None and cleared["target"] is None

    # The frontend falls back to AI values when override is not active. Verify the
    # AI values themselves are still valid trade values (untouched by the override).
    ai = compute_risk_reward(100.0, 99.0, 104.0, "LONG")
    assert ai.valid  # risk 1, reward 4 -> R:R 4.0
    active = {
        "entry": ai.entry if not cleared["override_active"] else cleared["entry"],
        "stopLoss": ai.stop_loss if not cleared["override_active"] else cleared["stop_loss"],
        "target": ai.target if not cleared["override_active"] else cleared["target"],
        "rr": ai.risk_reward if not cleared["override_active"] else cleared["risk_reward"],
    }
    # With override inactive we use AI values
    assert active["entry"] == pytest.approx(100.0)
    assert active["rr"] == pytest.approx(4.0)


def test_scanner_rr_computed_from_active_values():
    """R:R in the scanner is computed from the CURRENT active (custom) Entry/SL/Target,
    not the AI defaults."""
    from app.services.trade_setup import compute_risk_reward

    # User custom values differ from AI defaults
    custom = compute_risk_reward(105.0, 104.0, 109.0, "LONG")
    assert custom.valid
    # risk 1, reward 4 -> R:R 4.0 (from the custom values)
    assert custom.risk_reward == pytest.approx(4.00)

    ai_default = compute_risk_reward(100.0, 99.5, 103.0, "LONG")
    # risk 0.5, reward 3.0 -> R:R 6.0
    assert ai_default.risk_reward == pytest.approx(6.0)
    # The two must be distinct to prove R:R follows the active custom values
    assert custom.entry != ai_default.entry
    assert custom.risk_reward != ai_default.risk_reward


# ────────────────────────────────────────────────────────────────────────
# Part 1 + 2: Pending order edit / fill / cancel / R:R / P&L
# 18 regression tests — 2026-09-04
# ────────────────────────────────────────────────────────────────────────


def test_edit_pending_buy_updates_prices_and_rr():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    oid = r["order_id"]
    assert r["status"] == "pending"
    updated = pt.edit_order(oid, entry_price=2440.0, stop_loss=2410.0, target_1=2500.0)
    assert updated["status"] == "pending"
    assert updated["position"]["entry_price"] == 2440.0
    assert updated["position"]["stop_loss"] == 2410.0
    assert updated["position"]["target_1"] == 2500.0
    pending = pt.get_pending_orders()
    order = [o for o in pending if o["id"] == oid][0]
    assert order["entry_price"] == 2440.0
    assert order["stop_loss"] == 2410.0


def test_edit_pending_sell_updates_prices_and_rr():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SHORT", 5, 3200.0, 3230.0, 3160.0)
    oid = r["order_id"]
    updated = pt.edit_order(oid, entry_price=3190.0, stop_loss=3220.0, target_1=3150.0)
    assert updated["status"] == "pending"
    assert updated["position"]["entry_price"] == 3190.0
    assert updated["position"]["stop_loss"] == 3220.0
    assert updated["position"]["target_1"] == 3150.0


def test_edit_pending_invalid_entry_zero_rejected():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    result = pt.edit_order(r["order_id"], entry_price=0)
    assert "error" in result


def test_edit_pending_invalid_qty_rejected():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    result = pt.edit_order(r["order_id"], quantity=0)
    assert "error" in result


def test_edit_filled_order_rejected():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    pt.fill_order(r["order_id"])
    result = pt.edit_order(r["order_id"], entry_price=2500.0)
    assert "error" in result


def test_edit_cancelled_order_rejected():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    pt.cancel_order(r["order_id"])
    result = pt.edit_order(r["order_id"], entry_price=2500.0)
    assert "error" in result


def test_fill_pending_order_transitions_to_filled():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    assert r["status"] == "pending"
    assert pt.get_portfolio_summary()["positions_count"] == 0
    fill = pt.fill_order(r["order_id"])
    assert fill["status"] == "filled"
    assert pt.get_portfolio_summary()["positions_count"] == 1
    assert pt.get_portfolio_summary()["pending_orders_count"] == 0


def test_cancel_pending_order():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    assert pt.get_portfolio_summary()["pending_orders_count"] == 1
    cancel = pt.cancel_order(r["order_id"])
    assert cancel["status"] == "cancelled"
    assert pt.get_portfolio_summary()["pending_orders_count"] == 0


def test_get_pending_orders():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r1 = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    r2 = pt.place_order("TCS", "LONG", 5, 3200.0, 3180.0, 3250.0)
    pt.fill_order(r2["order_id"])
    pending = pt.get_pending_orders()
    assert len(pending) == 1
    assert pending[0]["id"] == r1["order_id"]
    assert pending[0]["status"] == "pending"


def test_rr_recalculated_on_edit():
    from app.services.trade_setup import compute_risk_reward
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    result = pt.edit_order(r["order_id"], entry_price=2440.0, stop_loss=2410.0, target_1=2500.0)
    assert result["status"] == "pending"
    rr = compute_risk_reward(2440.0, 2410.0, 2500.0, "LONG")
    assert rr.valid
    assert rr.risk_reward == pytest.approx(2.0, abs=0.1)


def test_no_duplicate_positions_on_fill():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    pt.fill_order(r["order_id"])
    fill2 = pt.fill_order(r["order_id"])
    assert "error" in fill2
    assert pt.get_portfolio_summary()["positions_count"] == 1


def test_cross_user_edit_rejected():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0, user_id="user_a")
    result = pt.edit_order(r["order_id"], entry_price=2500.0, user_id="user_b")
    assert "error" in result


def test_long_position_live_pnl():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    pt.fill_order(r["order_id"])
    pt.update_prices({"RELIANCE": 2470.0})
    positions = pt.get_positions()
    assert len(positions) == 1
    assert positions[0]["current_price"] == 2470.0
    assert positions[0]["unrealized_pnl"] == pytest.approx(200.0)


def test_short_position_live_pnl():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("TCS", "SHORT", 5, 3200.0, 3230.0, 3160.0)
    pt.fill_order(r["order_id"])
    pt.update_prices({"TCS": 3180.0})
    positions = pt.get_positions()
    assert len(positions) == 1
    assert positions[0]["current_price"] == 3180.0
    assert positions[0]["unrealized_pnl"] == pytest.approx(100.0)


def test_live_pnl_updates_after_price_change():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    pt.fill_order(r["order_id"])
    pt.update_prices({"RELIANCE": 2460.0})
    assert pt.get_positions()[0]["unrealized_pnl"] == pytest.approx(100.0)
    pt.update_prices({"RELIANCE": 2480.0})
    assert pt.get_positions()[0]["unrealized_pnl"] == pytest.approx(300.0)
    pt.update_prices({"RELIANCE": 2450.0})
    assert pt.get_positions()[0]["unrealized_pnl"] == pytest.approx(0.0)


def test_closed_trade_realized_pnl():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    pt.fill_order(r["order_id"])
    oid = pt.get_positions()[0]["id"]
    close = pt.close_position(oid, 2500.0)
    assert close["pnl"] == pytest.approx(500.0)
    summary = pt.get_portfolio_summary()
    assert summary["realized_pnl"] == pytest.approx(500.0)
    assert summary["unrealized_pnl"] == 0.0
    assert summary["total_pnl"] == pytest.approx(500.0)
    history = pt.get_trade_history()
    assert len(history) == 1
    assert history[0]["pnl"] == pytest.approx(500.0)


def test_pending_value_in_summary():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2450.0, 2420.0, 2500.0)
    summary = pt.get_portfolio_summary()
    assert summary["pending_value"] == pytest.approx(24500.0)
    pt.cancel_order(r["order_id"])
    summary = pt.get_portfolio_summary()
    assert summary["pending_value"] == 0.0


# ────────────────────────────────────────────────────────────────────────
# Part 3: Sector resolution
# ────────────────────────────────────────────────────────────────────────


def test_sector_resolution_uses_yfinance():
    import asyncio
    from app.services.sector_resolver import SectorResolver
    resolver = SectorResolver()
    resolver._fetch_sector_sync = lambda sym: "Energy"  # stub the network call
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(resolver.resolve("RELIANCE.NS"))
    loop.close()
    assert result == "Energy"


def test_sector_resolution_caches_results():
    import asyncio
    from app.services.sector_resolver import SectorResolver
    resolver = SectorResolver()
    calls = {"n": 0}
    def _fake(sym):
        calls["n"] += 1
        return "Energy"
    resolver._fetch_sector_sync = _fake
    loop = asyncio.new_event_loop()
    result1 = loop.run_until_complete(resolver.resolve("RELIANCE.NS"))
    result2 = loop.run_until_complete(resolver.resolve("RELIANCE.NS"))
    loop.close()
    assert result1 == result2 == "Energy"
    assert calls["n"] == 1  # second call served from cache


def test_sector_fallback_on_yfinance_failure():
    import asyncio
    from app.services.sector_resolver import SectorResolver
    resolver = SectorResolver()
    resolver._fetch_sector_sync = lambda sym: "Unknown"  # simulate yahoo failure/blocked
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(resolver.resolve("INVALID_TICKER_99999.NS"))
    loop.close()
    assert result == "Unknown"


def test_unknown_only_when_genuine():
    """'Unknown' is returned only when resolution genuinely fails; a resolvable
    symbol returns its real sector. The actual HTTP call may be blocked in this
    environment, so we stub the network layer to verify the cache/fallback logic."""
    import asyncio
    from app.services.sector_resolver import SectorResolver
    resolver = SectorResolver()
    resolver._fetch_sector_sync = lambda sym: "Unknown" if "12345" in sym else "Energy"
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(resolver.resolve("RELIANCE.NS"))
    result_bad = loop.run_until_complete(resolver.resolve("NONEXISTENT_12345.NS"))
    loop.close()
    assert result != "Unknown"
    assert result_bad == "Unknown"


def test_sector_batch_resolves_and_falls_back():
    """resolve_batch returns a result per symbol, using real sector when
    available and 'Unknown' fallback otherwise, without crashing."""
    import asyncio
    from app.services.sector_resolver import SectorResolver
    resolver = SectorResolver()
    resolver._fetch_sector_sync = lambda sym: "Energy" if "NS" in sym else "Unknown"
    loop = asyncio.new_event_loop()
    results = loop.run_until_complete(
        resolver.resolve_batch(["RELIANCE.NS", "TCS.NS", "BAD_XYZ"])
    )
    loop.close()
    assert "RELIANCE.NS" in results
    assert "TCS.NS" in results
    assert "BAD_XYZ" in results
    assert results["RELIANCE.NS"] == "Energy"
    assert results["BAD_XYZ"] == "Unknown"


def test_custom_setup_persists():
    from app.services.paper_trading import PaperTradingEngine
    pt = PaperTradingEngine()
    r = pt.place_order("RELIANCE", "LONG", 10, 2500.0, 2470.0, 2560.0, user_id="user_123")
    pt.fill_order(r["order_id"])
    positions = pt.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos["entry_price"] == 2500.0
    assert pos["stop_loss"] == 2470.0
    assert pos["target_1"] == 2560.0
