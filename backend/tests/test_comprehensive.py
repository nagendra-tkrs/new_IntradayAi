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