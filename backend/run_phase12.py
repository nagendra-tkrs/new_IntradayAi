"""
Run Phase 12: Capital Preservation Final Review
Tests all symbols with v1 and generates the comprehensive review.
"""
import asyncio
import os
from app.services.backtesting import BacktestEngine
from app.services.capital_preservation import CapitalPreservationReview
from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider


async def main():
    symbols = ['RELIANCE.NS', 'TCS.NS', 'HDFCBANK.NS']
    provider = YFinanceMarketDataProvider()
    
    results = {}
    
    for symbol in symbols:
        clean = symbol.replace('.NS', '')
        print(f"Loading {clean}...")
        df = await provider.get_intraday_bars(symbol, timeframe='5m')
        
        key = f"{clean}_v1"
        bt = BacktestEngine(initial_capital=1_000_000, strategy_version='v1')
        result = bt.run(df, symbol, strategy_version='v1')
        results[key] = result
    
    review = CapitalPreservationReview()
    review_report = review.run_full_review(results)
    
    print("\n\nCapital Preservation Report saved to:")
    print(os.path.join(os.path.dirname(__file__), 'app', 'data', 'review', 'capital_preservation_review.json'))


asyncio.run(main())
