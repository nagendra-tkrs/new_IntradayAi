"""
Phase 6: Ablation Testing - Extended

Tests the contribution of each STRONG filter AND key parameters.
"""
import asyncio
import pandas as pd
import numpy as np
from typing import Optional
from app.services.strategy_config import StrategyVersion, STRATEGY_REGISTRY, get_strategy
from app.services.backtesting import BacktestEngine


class AblationBacktestEngine(BacktestEngine):
    def __init__(self, initial_capital=1_000_000.0, strategy_version='v1',
                 custom_strategy: Optional[StrategyVersion] = None):
        super().__init__(initial_capital=initial_capital, strategy_version=strategy_version)
        self.custom_strategy = custom_strategy
    
    def run(self, df: pd.DataFrame, symbol: str, strategy: str = "multi_factor",
            strategy_version: Optional[str] = None,
            start_date=None, end_date=None, max_bars=None) -> dict:
        if self.custom_strategy:
            old_v1 = STRATEGY_REGISTRY.get('v1')
            STRATEGY_REGISTRY['v1'] = self.custom_strategy
            try:
                return super().run(df, symbol, strategy=strategy,
                                   strategy_version='v1',
                                   start_date=start_date, end_date=end_date, max_bars=max_bars)
            finally:
                if old_v1:
                    STRATEGY_REGISTRY['v1'] = old_v1
        return super().run(df, symbol, strategy=strategy,
                           strategy_version=strategy_version or self.strategy_version,
                           start_date=start_date, end_date=end_date, max_bars=max_bars)


class AblationStrategy(StrategyVersion):
    """Strategy with individual filter overrides for ablation testing."""
    
    def __init__(self, name, description, enable_rr=True, enable_adx=True,
                 enable_relvol=True, enable_vwap=True, enable_rsi=True,
                 **base_kwargs):
        super().__init__(version=name, description=description, **base_kwargs)
        self.enable_rr = enable_rr
        self.enable_adx = enable_adx
        self.enable_relvol = enable_relvol
        self.enable_vwap = enable_vwap
        self.enable_rsi = enable_rsi
    
    def should_accept_strong(self, adx, rel_vol, rsi, price, vwap, direction):
        rejections = []
        if self.enable_adx and adx < self.strong_min_adx:
            rejections.append(f"ADX below {self.strong_min_adx}")
        if self.enable_relvol and rel_vol < self.strong_min_rel_vol:
            rejections.append(f"RelVol below {self.strong_min_rel_vol}")
        if self.enable_vwap:
            from app.models.schemas import SignalDirection as SD
            if direction == SD.STRONG_LONG and price <= vwap:
                rejections.append("Price below VWAP for long")
            if direction == SD.STRONG_SHORT and price >= vwap:
                rejections.append("Price above VWAP for short")
        if self.enable_rsi:
            from app.models.schemas import SignalDirection as SD
            if direction == SD.STRONG_LONG and rsi > self.strong_max_rsi_long:
                rejections.append(f"RSI above {self.strong_max_rsi_long}")
            if direction == SD.STRONG_SHORT and rsi < self.strong_max_rsi_short:
                rejections.append(f"RSI below {self.strong_max_rsi_short}")
        return len(rejections) == 0, rejections
    
    def get_min_rr(self, direction):
        if direction.is_strong and not self.enable_rr:
            return 0.0
        return super().get_min_rr(direction)


def get_base_params():
    return {
        'strong_long_threshold': 80.0,
        'long_threshold': 75.0,
        'weak_long_threshold': 65.0,
        'no_trade_threshold': 55.0,
        'weak_short_threshold': 45.0,
        'short_threshold': 30.0,
        'strong_short_threshold': 0.0,
        'min_rr_normal': 1.2,
        'min_rr_strong': 2.0,
        'strong_atr_mult': 3.0,
        'normal_atr_mult': 2.0,
        'strong_min_adx': 25.0,
        'strong_min_rel_vol': 1.0,
        'strong_max_rsi_long': 75.0,
        'strong_max_rsi_short': 25.0,
        'stop_loss_atr_mult': 1.5,
        'risk_per_trade_pct': 2.0,
        'min_score': 45.0,
    }


async def run_ablation_for_symbol(df, symbol, initial_capital=1_000_000):
    """Run all ablation variants for a single symbol."""
    results = {}
    base = get_base_params()
    all_filters = ['RR', 'ADX', 'RelVol', 'VWAP', 'RSI']
    
    # Full strategy
    full = AblationStrategy('full', 'All STRONG filters',
        enable_rr=True, enable_adx=True, enable_relvol=True,
        enable_vwap=True, enable_rsi=True, **base)
    results['full'] = run_bt(df, symbol, initial_capital, full)
    
    # Score only (no STRONG filters)
    score_only = AblationStrategy('score_only', 'Score >= 80 only',
        enable_rr=False, enable_adx=False, enable_relvol=False,
        enable_vwap=False, enable_rsi=False, **base)
    results['score_only'] = run_bt(df, symbol, initial_capital, score_only)
    
    # Remove each filter individually
    for fname in all_filters:
        enable = {f: True for f in all_filters}
        enable[fname] = False
        strat = AblationStrategy(
            f'no_{fname}', f'Without {fname} filter',
            enable_rr=enable['RR'], enable_adx=enable['ADX'],
            enable_relvol=enable['RelVol'], enable_vwap=enable['VWAP'],
            enable_rsi=enable['RSI'], **base)
        results[f'no_{fname}'] = run_bt(df, symbol, initial_capital, strat)
    
    # Parameter sensitivity tests
    for min_score in [75.0, 80.0, 85.0]:
        strat = AblationStrategy(
            f'minscore_{min_score}', f'min_score={min_score}',
            enable_rr=True, enable_adx=True, enable_relvol=True,
            enable_vwap=True, enable_rsi=True,
            **{**base, 'min_score': min_score})
        results[f'minscore_{min_score}'] = run_bt(df, symbol, initial_capital, strat)
    
    for rr in [1.5, 2.0, 2.5, 3.0]:
        strat = AblationStrategy(
            f'minrr_{rr}', f'min_rr_strong={rr}',
            enable_rr=True, enable_adx=True, enable_relvol=True,
            enable_vwap=True, enable_rsi=True,
            **{**base, 'min_rr_strong': rr})
        results[f'minrr_{rr}'] = run_bt(df, symbol, initial_capital, strat)
    
    return results


def run_bt(df, symbol, initial_capital, strat):
    bt = AblationBacktestEngine(initial_capital=initial_capital, custom_strategy=strat)
    result = bt.run(df, symbol)
    return result['performance']


async def main():
    from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider
    
    provider = YFinanceMarketDataProvider()
    symbols = ['RELIANCE.NS', 'TCS.NS', 'HDFCBANK.NS']
    
    print("=" * 80)
    print("PHASE 6: ABLATION TESTING (Extended)")
    print("Testing contribution of STRONG filters and parameter sensitivity")
    print("=" * 80)
    
    all_results = {}
    
    for symbol in symbols:
        clean = symbol.replace('.NS', '')
        df = await provider.get_intraday_bars(symbol, timeframe='5m')
        
        if df.empty or len(df) < 60:
            continue
        
        print(f"\n{'='*60}")
        print(f"Symbol: {clean} ({len(df)} bars)")
        print(f"{'='*60}")
        
        results = await run_ablation_for_symbol(df, clean)
        all_results[clean] = results
        
        print(f"\n  {'Variant':<25} {'Trades':>7} {'Win%':>7} {'PF':>8} {'Exp':>8} {'AvgR':>8} {'MaxDD':>8}")
        print(f"  {'-'*70}")
        for name, perf in results.items():
            print(f"  {name:<25} {perf['total_trades']:>7} {perf['win_rate']:>6.1f}% "
                  f"{perf['profit_factor']:>8.2f} {perf['expectancy']:>8.2f} "
                  f"{perf['avg_r_multiple']:>8.3f} {perf['max_drawdown']:>7.2f}%")
    
    # Summary analysis
    print(f"\n{'='*80}")
    print("ABLATION SUMMARY: STRONG FILTER CONTRIBUTES")
    print(f"{'='*80}")
    
    for symbol, results in all_results.items():
        full = results['full']
        score_only = results['score_only']
        
        full_strong_trades = sum(1 for name in results if name.startswith('full') or name == 'full')
        
        # Check if any STRONG signals were generated
        print(f"\n{symbol}:")
        print(f"  Full:       {full['total_trades']} trades, PF={full['profit_factor']:.2f}, Exp={full['expectancy']:.2f}")
        print(f"  Score only: {score_only['total_trades']} trades, PF={score_only['profit_factor']:.2f}, Exp={score_only['expectancy']:.2f}")
        
        if full['total_trades'] == score_only['total_trades']:
            print(f"  -> STRONG filters had NO effect (no STRONG signals were generated)")
            print(f"  -> The signal score rarely reaches 80; most signals are LONG/SHORT")
        
        # Parameter sensitivity
        min_score_results = {k: v for k, v in results.items() if k.startswith('minscore_')}
        if min_score_results:
            print(f"\n  Min score sensitivity:")
            for name, perf in sorted(min_score_results.items()):
                print(f"    {name}: {perf['total_trades']} trades, PF={perf['profit_factor']:.2f}, Exp={perf['expectancy']:.2f}")
        
        min_rr_results = {k: v for k, v in results.items() if k.startswith('minrr_')}
        if min_rr_results:
            print(f"\n  Min R:R sensitivity:")
            for name, perf in sorted(min_rr_results.items()):
                print(f"    {name}: {perf['total_trades']} trades, PF={perf['profit_factor']:.2f}, Exp={perf['expectancy']:.2f}")


if __name__ == '__main__':
    asyncio.run(main())
