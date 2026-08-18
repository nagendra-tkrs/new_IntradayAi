import logging
from app.services.market_data.base import MarketDataProvider
from app.core.config import settings
from app.services.data_validation import data_status

logger = logging.getLogger(__name__)

_kite_provider_instance = None
_yfinance_provider_instance = None


def get_provider() -> MarketDataProvider:
    mode = settings.MARKET_DATA_MODE.lower()
    data_status.mode = mode

    if mode == "yfinance":
        return _get_yfinance_provider()
    elif mode == "live":
        return _get_live_provider()
    elif mode == "historical":
        from app.services.market_data.historical_provider import HistoricalMarketDataProvider
        logger.info("Using HISTORICAL data provider")
        return HistoricalMarketDataProvider()
    else:
        from app.services.market_data.mock_provider import MockMarketDataProvider
        logger.info("Using MOCK (simulated) data provider")
        return MockMarketDataProvider()


def _get_yfinance_provider() -> MarketDataProvider:
    global _yfinance_provider_instance
    if _yfinance_provider_instance is not None:
        return _yfinance_provider_instance

    try:
        from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider
        provider = YFinanceMarketDataProvider()
        data_status.update_connection("connected")
        data_status._data_source = "yfinance"
        data_status.instruments_loaded = True
        logger.info("yfinance data provider initialized successfully (direct API)")
        _yfinance_provider_instance = provider
        return provider
    except Exception as e:
        logger.error(f"Failed to initialize yfinance provider: {e}")
        data_status.pause_signals(f"yfinance provider initialization error: {e}")
        from app.services.market_data.mock_provider import MockMarketDataProvider
        return MockMarketDataProvider()


def _get_live_provider() -> MarketDataProvider:
    global _kite_provider_instance
    if _kite_provider_instance is not None:
        return _kite_provider_instance

    api_key = settings.KITE_API_KEY
    api_secret = settings.KITE_API_SECRET
    access_token = settings.KITE_ACCESS_TOKEN

    if not api_key or not access_token:
        logger.error(
            "LIVE mode requested but KITE_API_KEY and/or KITE_ACCESS_TOKEN "
            "are not configured. No market data will be available. "
            "Set MARKET_DATA_MODE=mock to use simulated data."
        )
        data_status.pause_signals("Real-time market data credentials are not configured")
        data_status.update_connection("no_credentials")
        from app.services.market_data.mock_provider import MockMarketDataProvider
        return MockMarketDataProvider()

    try:
        from app.services.market_data.kite_provider import KiteConnectProvider
        provider = KiteConnectProvider(
            api_key=api_key,
            api_secret=api_secret,
            access_token=access_token,
        )
        connected = provider.connect_sync()
        if connected:
            provider.start_websocket()
            data_status.update_connection("connected")
            data_status._data_source = "LIVE"
            data_status.instruments_loaded = True
            logger.info("LIVE data provider initialized successfully")
        else:
            data_status.update_connection("failed")
            data_status.pause_signals("Kite Connect authentication failed")
            logger.error("Kite Connect connection failed")
        _kite_provider_instance = provider
        return provider
    except ImportError:
        logger.error("kiteconnect library not installed. Run: pip install kiteconnect")
        data_status.pause_signals("kiteconnect library not installed")
        from app.services.market_data.mock_provider import MockMarketDataProvider
        return MockMarketDataProvider()
    except Exception as e:
        logger.error(f"Failed to initialize Kite Connect: {e}")
        data_status.pause_signals(f"Provider initialization error: {e}")
        from app.services.market_data.mock_provider import MockMarketDataProvider
        return MockMarketDataProvider()


def get_data_status() -> dict:
    return data_status.get_status_dict()


def reset_provider():
    global _kite_provider_instance, _yfinance_provider_instance
    if _kite_provider_instance:
        try:
            _kite_provider_instance.stop_websocket()
        except Exception:
            pass
    _kite_provider_instance = None
    _yfinance_provider_instance = None
