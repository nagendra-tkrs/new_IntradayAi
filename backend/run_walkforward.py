"""
Phase 7: Walk-Forward Optimization Framework

Implements chronological: TRAIN -> VALIDATION -> TEST
with parameter grid search for strategy improvement.
"""
import asyncio
import pandas as pd
import numpy as np
from typing import Optional, List, Dict, Any, Callable
from itertools import product
from app.services.strategy_config import StrategyVersion, STRATEGY_REGISTRY
from app.services.backtesting import BacktestEngine
import copy


class WalkForwardOptimizer:
    """Walk-forward optimization framework for strategy parameter tuning."""
    
    def __init__(self, initial_capital: float = 1_000_000.0):
        self.initial_capital = initial_capital
    
    def split_walk_forward(
        self,
        df: pd.DataFrame,
        n_splits: int = 3,
        train_ratio: float = 0.5,
        val_ratio: float = 0.3,
    ) -> List[tuple]:
        """Split data into chronological train/validate/test windows."""
        n = len(df)
        splits = []
        window_size = n // n_splits
        
        for i in range(n_splits):
            start = i * window_size
            end = (i + 1) * window_size if i < n_splits - 1 else n
            
            train_end = int(start + (end - start) * train_ratio)
            val_end = int(start + (end - start) * (train_ratio + val_ratio))
            
            train_df = df.iloc[start:train_end].copy()
            val_df = df.iloc[train_end:val_end].copy()
            test_df = df.iloc[val_end:end].copy()
            
            splits.append((train_df, val_df, test_df))
        
        return splits
    
    def grid_search(
        self,
        train_df: pd.DataFrame,
        val_df: pd.DataFrame,
        param_grid: Dict[str, List],
        base_strategy: StrategyVersion,
        symbols: Optional[List[str]] = None,
    ) -> List[tuple]:
        """Grid search over parameter combinations."""
        param_names = list(param_grid.keys())
        param_values = list(param_grid.values())
        
        results = []
        
        for combo in product(*param_values):
            params = dict(zip(param_names, combo))
            
            strat = copy.deepcopy(base_strategy)
            for key, value in params.items():
                if hasattr(strat, key):
                    setattr(strat, key, value)
                elif key == 'min_score':
                    strat.min_score = value
                elif key == 'min_rr_strong':
                    strat.min_rr_strong = value
                elif key == 'min_rr_normal':
                    strat.min_rr_normal = value
                elif key == 'risk_per_trade_pct':
                    strat.risk_per_trade_pct = value
                elif key == 'min_score_for_trade':
                    strat.min_score = value
            
            strat.version = f"grid_{hash(str(combo)) % 10000}"
            strat.description = f"Grid search: {params}"
            
            # Evaluate on train
            bt_train = BacktestEngine(initial_capital=self.initial_capital, 
                                     strategy_version=strat.version)
            
            # Register custom strategy
            STRATEGY_REGISTRY[strat.version] = strat
            train_result = bt_train.run(train_df, 'TRAIN', strategy_version=strat.version)
            
            # Evaluate on validation
            bt_val = BacktestEngine(initial_capital=self.initial_capital,
                                   strategy_version=strat.version)
            val_result = bt_val.run(val_df, 'VAL', strategy_version=strat.version)
            
            train_perf = train_result['performance']
            val_perf = val_result['performance']
            
            # Scoring: prioritize OOS expectancy, then PF, then drawdown
            score = val_perf['expectancy'] * 0.5 + val_perf['profit_factor'] * 50 - val_perf['max_drawdown'] * 0.1
            
            results.append((params, train_perf, val_perf, score))
            
            # Cleanup
            if strat.version in STRATEGY_REGISTRY:
                del STRATEGY_REGISTRY[strat.version]
        
        results.sort(key=lambda x: x[3], reverse=True)
        return results
    
    def walk_forward_optimize(
        self,
        df: pd.DataFrame,
        symbol: str,
        param_grid: Dict[str, List],
        base_strategy: StrategyVersion,
        n_splits: int = 3,
    ) -> dict:
        """Run full walk-forward optimization."""
        
        splits = self.split_walk_forward(df, n_splits=n_splits)
        
        all_results = {
            'splits': [],
            'oos_results': [],
            'best_params_per_split': [],
        }
        
        for split_idx, (train_df, val_df, test_df) in enumerate(splits):
            print(f"\n  Split {split_idx + 1}/{n_splits}:")
            print(f"    Train: {len(train_df)} bars, Val: {len(val_df)} bars, Test: {len(test_df)} bars")
            
            if len(train_df) < 60 or len(val_df) < 60:
                print(f"    Skipping (insufficient data)")
                continue
            
            # Grid search on train+val
            combined = pd.concat([train_df, val_df], ignore_index=True)
            grid_results = self.grid_search(train_df, val_df, param_grid, base_strategy)
            
            if not grid_results:
                continue
            
            best_params, best_train, best_val, best_score = grid_results[0]
            all_results['best_params_per_split'].append({
                'split': split_idx + 1,
                'params': best_params,
                'val_pnl': best_val['total_pnl'],
                'val_pf': best_val['profit_factor'],
                'val_expectancy': best_val['expectancy'],
                'val_score': best_score,
                'val_trades': best_val['total_trades'],
            })
            
            print(f"    Best params: {best_params}")
            print(f"    Val: {best_val['total_trades']} trades, "
                  f"PF={best_val['profit_factor']:.2f}, "
                  f"Exp={best_val['expectancy']:.2f}")
            
            # Test on OOS
            if len(test_df) >= 60:
                test_strat = copy.deepcopy(base_strategy)
                for key, value in best_params.items():
                    setattr(test_strat, key, value)
                test_strat.version = f"oos_split_{split_idx+1}"
                test_strat.description = f"WF best params split {split_idx+1}"
                
                STRATEGY_REGISTRY[test_strat.version] = test_strat
                bt_test = BacktestEngine(initial_capital=self.initial_capital,
                                        strategy_version=test_strat.version)
                test_result = bt_test.run(test_df, symbol, strategy_version=test_strat.version)
                STRATEGY_REGISTRY.pop(test_strat.version, None)
                
                test_perf = test_result['performance']
                all_results['oos_results'].append({
                    'split': split_idx + 1,
                    'params': best_params,
                    'test_pnl': test_perf['total_pnl'],
                    'test_pf': test_perf['profit_factor'],
                    'test_win_rate': test_perf['win_rate'],
                    'test_trades': test_perf['total_trades'],
                    'test_avg_r': test_perf['avg_r_multiple'],
                    'test_max_dd': test_perf['max_drawdown'],
                })
                
                print(f"    OOS Test: {test_perf['total_trades']} trades, "
                      f"PF={test_perf['profit_factor']:.2f}, "
                      f"Exp={test_perf['expectancy']:.2f}")
            
            # Store top 5 results
            all_results['splits'].append({
                'split': split_idx + 1,
                'top5': [{
                    'params': r[0],
                    'val_pnl': r[2]['total_pnl'],
                    'val_pf': r[2]['profit_factor'],
                    'val_expectancy': r[2]['expectancy'],
                    'val_score': r[3],
                    'val_trades': r[2]['total_trades'],
                } for r in grid_results[:5]],
            })
        
        return all_results


async def run_walkforward():
    """Run walk-forward optimization on available data."""
    from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider
    from app.services.strategy_config import v1
    
    provider = YFinanceMarketDataProvider()
    symbols = ['RELIANCE.NS', 'TCS.NS', 'HDFCBANK.NS']
    
    print("=" * 80)
    print("PHASE 7: WALK-FORWARD OPTIMIZATION")
    print("=" * 80)
    
    optimizer = WalkForwardOptimizer(initial_capital=1_000_000)
    
    # Parameter grid to search
    param_grid = {
        'min_score': [60.0, 70.0, 75.0, 80.0],
        'min_rr_normal': [1.2, 1.5, 2.0],
        'risk_per_trade_pct': [1.0, 1.5, 2.0],
    }
    
    all_oos = []
    
    for symbol in symbols:
        clean = symbol.replace('.NS', '')
        print(f"\n{'='*60}")
        print(f"Optimizing {clean}")
        print(f"{'='*60}")
        
        df = await provider.get_intraday_bars(symbol, timeframe='5m')
        if df.empty or len(df) < 200:
            print(f"  Insufficient data")
            continue
        
        results = optimizer.walk_forward_optimize(
            df, clean, param_grid, v1, n_splits=3
        )
        
        all_oos.extend(results['oos_results'])
        
        # Summary
        if results['oos_results']:
            print(f"\n  OOS Summary for {clean}:")
            for r in results['oos_results']:
                print(f"    Split {r['split']}: PF={r['test_pf']:.2f}, "
                      f"Exp={r['test_pnl']:.2f}, Trades={r['test_trades']}, "
                      f"AvgR={r['test_avg_r']:.3f}")
    
    print(f"\n{'='*80}")
    print("OVERALL OPTIMIZATION SUMMARY")
    print(f"{'='*80}")
    
    if all_oos:
        pfs = [r['test_pf'] for r in all_oos if r['test_pf'] != 999.0]
        exp = [r['test_pnl'] for r in all_oos]
        trades = [r['test_trades'] for r in all_oos]
        
        print(f"Total OOS evaluations: {len(all_oos)}")
        print(f"Avg profit factor: {np.mean(pfs) if pfs else 'N/A'}")
        print(f"Avg PnL: {np.mean(exp):.2f}")
        print(f"Avg trades: {np.mean(trades):.1f}")
        print(f"Best params found: {all_oos[0]['params'] if all_oos else 'None'}")


if __name__ == '__main__':
    asyncio.run(run_walkforward())
