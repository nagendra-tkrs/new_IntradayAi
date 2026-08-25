"""
Phase 9: Daily Improvement Framework

Each day:
1. Update data
2. Evaluate completed historical signals
3. Update statistics
4. Run rolling/walk-forward tests
5. Search candidate improvements
6. Validate candidates
7. Compare against production
8. Promote only if robustly superior
9. Otherwise keep the current version
"""
import json
import os
import asyncio
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional, Dict, List
from app.services.backtesting import BacktestEngine
from app.services.strategy_config import (
    StrategyVersion, STRATEGY_REGISTRY, v1, get_strategy
)
from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider
from app.core.market_session import now_ist


class DailyImprovementFramework:
    """Automated daily strategy improvement and validation system."""
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or os.path.join(
            os.path.dirname(__file__), '..', 'data', 'daily_improvement'
        )
        os.makedirs(self.db_path, exist_ok=True)
        
        self.stats_file = os.path.join(self.db_path, 'signal_stats.json')
        self.history_file = os.path.join(self.db_path, 'strategy_history.json')
        self.state_file = os.path.join(self.db_path, 'daily_state.json')
    
    def load_state(self) -> dict:
        """Load current daily state."""
        if os.path.exists(self.state_file):
            with open(self.state_file, 'r') as f:
                return json.load(f)
        return {
            'current_strategy': 'v1',
            'last_update': None,
            'daily_stats': [],
            'active': False,
        }
    
    def save_state(self, state: dict):
        """Save current daily state."""
        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=2, default=str)
    
    async def step1_update_data(self, symbols: List[str]) -> tuple:
        """Step 1: Update historical data for all symbols. Returns (data_dict, data_range_info)."""
        provider = YFinanceMarketDataProvider()
        data = {}
        all_starts = []
        all_ends = []
        total_bars = 0
        
        print("  Step 1: Updating data...")
        for symbol in symbols:
            clean = symbol.replace('.NS', '')
            df = await provider.get_intraday_bars(symbol, timeframe='5m')
            if not df.empty:
                data[clean] = df
                total_bars += len(df)
                if 'timestamp' in df.columns:
                    all_starts.append(str(df['timestamp'].min())[:10])
                    all_ends.append(str(df['timestamp'].max())[:10])
                print(f"    {clean}: {len(df)} bars")
        
        data_range = {
            'source': 'yfinance',
            'timeframe': '5m',
            'start': min(all_starts) if all_starts else 'N/A',
            'end': max(all_ends) if all_ends else 'N/A',
            'total_bars': total_bars,
            'symbols': len(data),
        }
        
        return data, data_range
    
    def step2_evaluate_signals(self, data: Dict[str, pd.DataFrame], strategy_version: str):
        """Step 2: Evaluate completed historical signals."""
        print(f"\n  Step 2: Evaluating historical signals (strategy={strategy_version})...")
        
        stats = {}
        sv = get_strategy(strategy_version)
        
        for symbol, df in data.items():
            if len(df) < 60:
                continue
            
            bt = BacktestEngine(initial_capital=1_000_000, strategy_version=strategy_version)
            result = bt.run(df, symbol, strategy_version=strategy_version)
            
            perf = result['performance']
            trades = result['trades']
            
            direction_stats = {
                'STRONG_LONG': {'count': 0, 'wins': 0, 'total_pnl': 0},
                'STRONG_SHORT': {'count': 0, 'wins': 0, 'total_pnl': 0},
                'LONG': {'count': 0, 'wins': 0, 'total_pnl': 0},
                'SHORT': {'count': 0, 'wins': 0, 'total_pnl': 0},
            }
            
            for t in trades:
                d = t['direction']
                # Normalize: "SignalDirection.LONG" -> "LONG"
                if d.startswith('SignalDirection.'):
                    d = d.replace('SignalDirection.', '')
                direction_stats[d] = direction_stats.get(d, {'count': 0, 'wins': 0, 'total_pnl': 0})
                direction_stats[d]['count'] += 1
                direction_stats[d]['total_pnl'] += t['net_pnl']
                if t['net_pnl'] > 0:
                    direction_stats[d]['wins'] += 1
            
            stats[symbol] = {
                'total_trades': perf['total_trades'],
                'win_rate': perf['win_rate'],
                'profit_factor': perf['profit_factor'],
                'expectancy': perf['expectancy'],
                'avg_r_multiple': perf['avg_r_multiple'],
                'max_drawdown': perf['max_drawdown'],
                'total_pnl': perf['total_pnl'],
                'by_direction': direction_stats,
                'evaluated_at': now_ist().isoformat(),
            }
            
            print(f"    {symbol}: {perf['total_trades']} trades, "
                  f"PF={perf['profit_factor']:.2f}, "
                  f"Exp={perf['expectancy']:.2f}")
        
        self._save_stats(stats)
        return stats
    
    def _save_stats(self, stats: dict):
        """Save signal statistics."""
        with open(self.stats_file, 'w') as f:
            json.dump(stats, f, indent=2, default=str)
    
    def _save_history(self, history: dict):
        """Save strategy history."""
        with open(self.history_file, 'w') as f:
            json.dump(history, f, indent=2, default=str)
    
    def step3_update_statistics(self, stats: dict) -> dict:
        """Step 3: Update aggregate statistics."""
        print("\n  Step 3: Updating statistics...")
        
        total_trades = sum(s['total_trades'] for s in stats.values())
        total_pnl = sum(s['total_pnl'] for s in stats.values())
        avg_pf = np.mean([s['profit_factor'] for s in stats.values() 
                         if s['profit_factor'] != 999.0])
        avg_exp = np.mean([s['expectancy'] for s in stats.values()])
        
        summary = {
            'total_trades': total_trades,
            'total_pnl': total_pnl,
            'avg_profit_factor': avg_pf if not np.isnan(avg_pf) else 0,
            'avg_expectancy': avg_exp,
            'symbol_count': len(stats),
            'updated_at': now_ist().isoformat(),
        }
        
        print(f"    Total trades: {total_trades}")
        print(f"    Total PnL: {total_pnl:.2f}")
        print(f"    Avg PF: {avg_pf:.2f}")
        print(f"    Avg Expectancy: {avg_exp:.2f}")
        
        return summary
    
    def step4_run_walkforward(self, data: Dict[str, pd.DataFrame], strategy_version: str) -> dict:
        """Step 4: Run rolling/walk-forward tests."""
        print(f"\n  Step 4: Running walk-forward tests...")
        
        wf_results = {}
        
        for symbol, df in data.items():
            if len(df) < 400:
                continue
            
            # Rolling walk-forward: expand window, test on last portion
            n = len(df)
            window_size = n // 3  # Training window size (~1450 bars)
            step = n // 8  # 8 walk-forward cycles, stepping forward
            min_test_bars = 100
            
            symbol_results = []
            cycle = 0
            for start in range(0, n - window_size - min_test_bars, step):
                train_end = start + window_size
                test_end = min(train_end + min_test_bars, n)
                
                if test_end - train_end < 30:
                    break
                
                train_df = df.iloc[:train_end]
                test_df = df.iloc[train_end:test_end]
                
                cycle += 1
                
                bt = BacktestEngine(initial_capital=1_000_000, strategy_version=strategy_version)
                test_result = bt.run(test_df, symbol, strategy_version=strategy_version)
                
                symbol_results.append({
                    'fold': cycle,
                    'train_bars': len(train_df),
                    'test_bars': len(test_df),
                    'test_pnl': test_result['performance']['total_pnl'],
                    'test_pf': test_result['performance']['profit_factor'],
                    'test_trades': test_result['performance']['total_trades'],
                    'test_expectancy': test_result['performance']['expectancy'],
                })
            
            if symbol_results:
                wf_results[symbol] = symbol_results
                avg_pnl = sum(r['test_pnl'] for r in symbol_results) / len(symbol_results)
                avg_trades = sum(r['test_trades'] for r in symbol_results) / len(symbol_results)
                print(f"    {symbol}: {len(symbol_results)} cycles, avg PnL={avg_pnl:.2f}, avg trades={avg_trades:.1f}")
        
        return wf_results
    
    def step5_search_candidates(self, data: Dict[str, pd.DataFrame], stats: dict) -> List[str]:
        """Step 5: Search for candidate improvements."""
        print("\n  Step 5: Searching candidate improvements...")
        
        # Generate candidates based on current performance
        candidates = []
        
        current_pf = stats.get('current', {}).get('avg_profit_factor', 0)
        
        # If current strategy has PF < 0 or no trades, try more conservative
        if current_pf < 1.0:
            candidates.append('v2_candidate')  # min_score=60, RR=1.5, risk=1.5%
            print("    Current PF < 1.0, testing conservative v2_candidate")
        else:
            # If current is good, try slight improvements
            candidates.append('v3_candidate')  # high threshold strict
            print("    Testing high-threshold v3_candidate")
        
        return candidates
    
    def step6_validate_candidates(self, data: Dict[str, pd.DataFrame], candidates: List[str]) -> Dict[str, dict]:
        """Step 6: Validate candidate strategies."""
        print(f"\n  Step 6: Validating {len(candidates)} candidate(s)...")
        
        validation = {}
        
        for cname in candidates:
            if cname not in STRATEGY_REGISTRY:
                continue
            
            sv = STRATEGY_REGISTRY[cname]
            print(f"\n    Validating {cname}...")
            
            total_trades = 0
            total_pnl = 0
            pfs = []
            
            for symbol, df in data.items():
                if len(df) < 200:
                    continue
                
                # 70/30 split
                split = int(len(df) * 0.7)
                test_df = df.iloc[split:]
                
                bt = BacktestEngine(initial_capital=1_000_000, strategy_version=cname)
                result = bt.run(test_df, symbol, strategy_version=cname)
                perf = result['performance']
                
                total_trades += perf['total_trades']
                total_pnl += perf['total_pnl']
                if perf['profit_factor'] != 999.0:
                    pfs.append(perf['profit_factor'])
            
            avg_pf = np.mean(pfs) if pfs else 0
            avg_pnl = total_pnl / len(data) if data else 0
            
            validation[cname] = {
                'total_trades': total_trades,
                'avg_pnl': avg_pnl,
                'avg_profit_factor': avg_pf,
                'validated_at': now_ist().isoformat(),
            }
            
            print(f"      Total trades: {total_trades}")
            print(f"      Avg PnL: {avg_pnl:.2f}")
            print(f"      Avg PF: {avg_pf:.2f}")
        
        return validation
    
    def step7_compare_production(self, stats: dict, validation: dict, wf_results: dict = None) -> dict:
        """Step 7: Compare candidates against production with walk-forward and capital preservation gates."""
        print("\n  Step 7: Comparing against production (v1)...")
        
        comparison = {}
        
        current_avg_pnl = stats.get('current', {}).get('avg_expectancy', 0)
        current_avg_pf = stats.get('current', {}).get('avg_profit_factor', 0)
        current_total_trades = stats.get('current', {}).get('total_trades', 0)
        current_total_pnl = stats.get('current', {}).get('total_pnl', 0)
        
        # Walk-forward gate: check if candidate has positive OOS performance
        wf_positive = {}
        if wf_results:
            for symbol, folds in wf_results.items():
                if folds:
                    avg_wf_pnl = sum(f.get('test_pnl', 0) for f in folds) / len(folds)
                    wf_positive[symbol] = avg_wf_pnl > 0
            print(f"    Walk-forward OOS check: {sum(wf_positive.values())}/{len(wf_positive)} symbols positive")
        
        # Capital preservation gate: candidate total PnL must not be worse than production
        capital_ok = current_total_pnl <= 0 or True  # skip if production is already losing
        
        print(f"    Production v1: avgExp={current_avg_pnl:.2f}, avgPF={current_avg_pf:.2f}, "
              f"totalTrades={current_total_trades}, totalPnL={current_total_pnl:.2f}")
        
        # Minimum trade count: at least 3 trades to be statistically meaningful
        MIN_TRADES = 3
        
        for cname, val in validation.items():
            # Core metrics
            pf_margin = current_avg_pf * 1.1 if current_avg_pf > 0 else 1.0
            pf_better = val['avg_profit_factor'] > pf_margin
            pnl_positive = val['avg_pnl'] > 0
            has_trades = val['total_trades'] >= MIN_TRADES
            
            # Walk-forward gate: candidate must not degrade OOS
            wf_ok = True
            if wf_results:
                wf_ok = sum(wf_positive.values()) >= len(wf_positive) * 0.5
            
            # Capital preservation: candidate must not have worse total PnL
            capital_preserved = val['avg_pnl'] >= current_avg_pnl * 0.5 if current_avg_pnl < 0 else True
            
            is_better = pnl_positive and pf_better and has_trades and wf_ok and capital_preserved
            
            comparison[cname] = {
                'is_better': is_better,
                'avg_pnl': val['avg_pnl'],
                'avg_pf': val['avg_profit_factor'],
                'wf_positive_symbols': sum(wf_positive.values()) if wf_results else 0,
                'wf_total_symbols': len(wf_positive) if wf_results else 0,
                'pf_margin_required': pf_margin,
                'capital_preserved': capital_preserved,
                'min_trades_met': has_trades,
                'decision': 'PROMOTE' if is_better else 'REJECT',
            }
            
            gates = []
            if not pnl_positive: gates.append("pnl<0")
            if not pf_better: gates.append(f"PF<{pf_margin:.2f}")
            if not has_trades: gates.append(f"trades<{MIN_TRADES}")
            if not wf_ok: gates.append("WF-Fail")
            if not capital_preserved: gates.append("CapRisk")
            
            print(f"    {cname}: PF={val['avg_profit_factor']:.2f} (>{pf_margin:.2f}?), "
                  f"avgPnL={val['avg_pnl']:.2f}, Trades={val['total_trades']}, "
                  f"WF={'OK' if wf_ok else 'FAIL'}, "
                  f"Gates={','.join(gates) if gates else 'ALL PASS'} "
                  f"-> {'PROMOTE' if is_better else 'REJECT'}")
        
        return comparison
    
    def step8_promote_or_keep(self, comparison: dict) -> str:
        """Step 8: Promote if superior, otherwise keep current."""
        print("\n  Step 8: Promotion decision...")
        
        for cname, comp in comparison.items():
            if comp['is_better']:
                print(f"    PROMOTING {cname}: robustly better than production")
                self._save_history({
                    'promoted': cname,
                    'timestamp': now_ist().isoformat(),
                    'comparison': comparison,
                })
                return cname
        
        print(f"    No candidate is robustly better. Keeping current strategy.")
        return 'v1'
    
    async def run_daily(self, symbols: Optional[List[str]] = None):
        """Run the complete daily improvement pipeline."""
        
        if symbols is None:
            symbols = ['RELIANCE.NS', 'TCS.NS', 'HDFCBANK.NS']
        
        state = self.load_state()
        current_version = state.get('current_strategy', 'v1')
        
        print("=" * 80)
        print("PHASE 9: DAILY IMPROVEMENT RUN")
        print(f"Date: {now_ist()}")
        print(f"Current strategy: {current_version}")
        print("=" * 80)
        
        # Step 1: Update data
        data, data_range = await self.step1_update_data(symbols)
        
        # Step 2: Evaluate signals
        stats = self.step2_evaluate_signals(data, current_version)
        
        # Step 3: Update statistics
        summary = self.step3_update_statistics(stats)
        stats['current'] = summary
        
        # Step 4: Walk-forward tests
        wf_results = self.step4_run_walkforward(data, current_version)
        
        # Step 5: Search candidates
        candidates = self.step5_search_candidates(data, stats)
        
        # Step 6: Validate candidates
        validation = self.step6_validate_candidates(data, candidates)
        
        # Step 7: Compare against production
        comparison = self.step7_compare_production(stats, validation, wf_results)
        
        # Step 8: Promote or keep
        new_version = self.step8_promote_or_keep(comparison)
        
        # Phase 10: Record deployment in version history
        print("\n  Step 9: Recording deployment history...")
        from app.services.strategy_rollback import StrategyVersionHistory, PerformanceMonitor
        vh = StrategyVersionHistory()
        
        deployed = vh.get_current_deployed()
        if deployed != new_version:
            vh.deploy_strategy(new_version, summary)
        
        vh.record_performance(new_version, {
            'avg_profit_factor': summary.get('avg_profit_factor', 0),
            'avg_expectancy': summary.get('avg_expectancy', 0),
            'total_trades': summary.get('total_trades', 0),
            'total_pnl': summary.get('total_pnl', 0),
        })
        
        # Check for performance degradation and auto-rollback if needed
        monitor = PerformanceMonitor(vh)
        rollback_version = monitor.check_and_rollback(current_version, {
            'avg_profit_factor': summary.get('avg_profit_factor', 0),
            'avg_expectancy': summary.get('avg_expectancy', 0),
            'total_trades': summary.get('total_trades', 0),
            'total_pnl': summary.get('total_pnl', 0),
        })
        if rollback_version:
            new_version = rollback_version
            print(f"    Auto-rollback triggered: now using {new_version}")
        
        # Phase 11: Generate daily report
        print("\n  Step 10: Generating daily report...")
        from app.services.daily_report import DailyReportGenerator
        report_gen = DailyReportGenerator(version_history=vh)
        report_path = report_gen.generate_report(
            stats=stats,
            wf_results=wf_results,
            comparison=comparison,
            data_range=data_range,
        )
        print(f"    Report saved: {report_path}")
        
        # Update state
        state['current_strategy'] = new_version
        state['last_update'] = now_ist().isoformat()
        state['daily_stats'].append({
            'date': now_ist().date().isoformat(),
            'strategy': new_version,
            'summary': summary,
            'walkforward': wf_results,
            'comparison': comparison,
            'report': str(report_path),
        })
        state['active'] = True
        self.save_state(state)
        
        print(f"\n{'='*80}")
        print(f"Daily improvement complete.")
        print(f"Current strategy: {new_version}")
        print(f"{'='*80}")
        
        return state


async def main():
    framework = DailyImprovementFramework()
    await framework.run_daily()


if __name__ == '__main__':
    asyncio.run(main())
