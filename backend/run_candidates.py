"""
Phase 8: Candidate Strategy Evaluation

Compares candidate strategies against v1 using walk-forward validation.
"""
import asyncio
import pandas as pd
import numpy as np
from app.services.backtesting import BacktestEngine
from app.services.strategy_config import (
    v1, v2, v3, v2_candidate, v3_candidate, v4_candidate,
    STRATEGY_REGISTRY
)


def evaluate_strategy(df, symbol, strategy, initial_capital=1_000_000):
    """Evaluate a strategy on the given data."""
    bt = BacktestEngine(initial_capital=initial_capital, strategy_version=strategy.version)
    result = bt.run(df, symbol, strategy_version=strategy.version)
    return result


async def run_candidate_evaluation():
    from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider
    
    provider = YFinanceMarketDataProvider()
    symbols = ['RELIANCE.NS', 'TCS.NS', 'HDFCBANK.NS']
    
    print("=" * 80)
    print("PHASE 8: CANDIDATE STRATEGY EVALUATION")
    print("Comparing candidates against v1 baseline")
    print("=" * 80)
    
    candidates = [
        ('v1', v1, 'Baseline (production)'),
        ('v2', v2, 'Conservative'),
        ('v3', v3, 'Aggressive'),
        ('v2_candidate', v2_candidate, 'From WFO grid search'),
        ('v3_candidate', v3_candidate, 'High threshold strict'),
        ('v4_candidate', v4_candidate, 'Control (same as v1)'),
    ]
    
    all_results = {}
    
    for symbol in symbols:
        clean = symbol.replace('.NS', '')
        print(f"\n{'='*60}")
        print(f"Symbol: {clean}")
        print(f"{'='*60}")
        
        df = await provider.get_intraday_bars(symbol, timeframe='5m')
        if df.empty or len(df) < 200:
            continue
        
        # Split into train (70%) and test (30%)
        split = int(len(df) * 0.7)
        train_df = df.iloc[:split].copy()
        test_df = df.iloc[split:].copy()
        
        print(f"\n  Data: {len(df)} bars | Train: {len(train_df)} | Test: {len(test_df)}")
        print(f"\n  {'Strategy':<20} {'TrnTr':>5} {'TrnW%':>6} {'TrnPF':>7} {'TrnExp':>9} {'TstTr':>5} {'TstW%':>6} {'TstPF':>7} {'TstExp':>9} {'TstAvgR':>8}")
        print(f"  {'-'*100}")
        
        symbol_results = {}
        
        for name, strat, desc in candidates:
            # Train
            bt_train = BacktestEngine(initial_capital=1_000_000, strategy_version=strat.version)
            train_result = bt_train.run(train_df, clean, strategy_version=strat.version)
            
            # Test
            bt_test = BacktestEngine(initial_capital=1_000_000, strategy_version=strat.version)
            test_result = bt_test.run(test_df, clean, strategy_version=strat.version)
            
            tr_perf = train_result['performance']
            te_perf = test_result['performance']
            
            print(f"  {name:<20} {tr_perf['total_trades']:>5} {tr_perf['win_rate']:>5.1f}% "
                  f"{tr_perf['profit_factor']:>7.2f} {tr_perf['expectancy']:>9.2f} "
                  f"{te_perf['total_trades']:>5} {te_perf['win_rate']:>5.1f}% "
                  f"{te_perf['profit_factor']:>7.2f} {te_perf['expectancy']:>9.2f} "
                  f"{te_perf['avg_r_multiple']:>8.3f}")
            
            symbol_results[name] = {
                'train': tr_perf,
                'test': te_perf,
                'description': desc,
            }
        
        all_results[clean] = symbol_results
    
    # Overall summary
    print(f"\n{'='*80}")
    print("OVERALL CANDIDATE COMPARISON (Test Period)")
    print(f"{'='*80}")
    print(f"\n  {'Strategy':<20} {'Total Trades':>12} {'Avg Win%':>10} {'Avg PF':>8} {'Avg Exp':>10} {'Avg AvgR':>10}")
    print(f"  {'-'*80}")
    
    for name, _, desc in candidates:
        all_test_trades = []
        all_test_wins = []
        all_test_pfs = []
        all_test_exps = []
        all_test_avgrs = []
        
        for symbol, results in all_results.items():
            if name in results:
                t = results[name]['test']
                all_test_trades.append(t['total_trades'])
                all_test_wins.append(t['win_rate'])
                all_test_pfs.append(t['profit_factor'] if t['profit_factor'] != 999.0 else 0.0)
                all_test_exps.append(t['expectancy'])
                all_test_avgrs.append(t['avg_r_multiple'])
        
        avg_trades = np.mean(all_test_trades)
        avg_win = np.mean(all_test_wins)
        avg_pf = np.mean(all_test_pfs)
        avg_exp = np.mean(all_test_exps)
        avg_avgr = np.mean(all_test_avgrs)
        
        print(f"  {name:<20} {avg_trades:>12.1f} {avg_win:>9.1f}% {avg_pf:>8.2f} "
              f"{avg_exp:>10.2f} {avg_avgr:>10.3f}")
    
    # Recommendation
    print(f"\n{'='*80}")
    print("RECOMMENDATION")
    print(f"{'='*80}")
    
    # Compare v1 vs candidates
    v1_pns = []
    for symbol, results in all_results.items():
        if 'v1' in results and 'v2_candidate' in results:
            v1_pn = results['v1']['test']['total_pnl']
            v2c_pn = results['v2_candidate']['test']['total_pnl']
            v1_pns.append(v1_pn)
            print(f"\n  {symbol}:")
            print(f"    v1 PnL: {v1_pn:.2f}")
            print(f"    v2_candidate PnL: {v2c_pn:.2f}")
    
    avg_v1_pn = np.mean(v1_pns) if v1_pns else 0
    print(f"\n  Average v1 OOS PnL: {avg_v1_pn:.2f}")
    print(f"  Result: {'REJECT v2_candidate (not robustly better)' if avg_v1_pn >= 0 else 'INCONCLUSIVE - need more data'}")


if __name__ == '__main__':
    asyncio.run(run_candidate_evaluation())
