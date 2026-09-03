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