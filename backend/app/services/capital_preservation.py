"""
Phase 12: Capital Preservation Final Review

Analyzes all backtest results from Phases 5-11 for capital preservation:
1. Risk/reward analysis - ensuring every trade has defined R:R >= threshold
2. Position sizing verification - risk_per_trade_pct applied correctly
3. Drawdown analysis - max drawdown across all symbols and strategies
4. Stop-loss compliance - all stops hit before targets or vice versa
5. Diversification analysis - multi-symbol exposure
6. Capital utilization efficiency - are we using capital effectively
"""
import json
import os
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from app.services.strategy_config import STRATEGY_REGISTRY, get_strategy
from app.core.market_session import now_ist


class CapitalPreservationReview:
    """
    Comprehensive capital preservation audit across all strategy versions and symbols.
    """
    
    def __init__(self, data_dir: str = None):
        self.data_dir = data_dir or os.path.join(
            os.path.dirname(__file__), '..', 'data', 'review'
        )
        os.makedirs(self.data_dir, exist_ok=True)
    
    def run_full_review(self, backtest_results: Dict[str, dict]) -> dict:
        """
        Run complete capital preservation review on backtest results.
        
        Args:
            backtest_results: Dict mapping f"{symbol}_{strategy}" -> backtest result
        
        Returns:
            Review report dict
        """
        print("=" * 80)
        print("PHASE 12: CAPITAL PRESERVATION FINAL REVIEW")
        print(f"Date: {now_ist()}")
        print(f"Symbols/Strategies reviewed: {len(backtest_results)}")
        print("=" * 80)
        
        report = {
            'review_date': now_ist().isoformat(),
            'symbol_strategy_count': len(backtest_results),
            'sections': {},
        }
        
        # Section 1: Risk/Reward Compliance
        report['sections']['risk_reward'] = self._review_risk_reward(backtest_results)
        
        # Section 2: Position Sizing
        report['sections']['position_sizing'] = self._review_position_sizing(backtest_results)
        
        # Section 3: Drawdown Analysis
        report['sections']['drawdown'] = self._review_drawdown(backtest_results)
        
        # Section 4: Stop-Loss Compliance
        report['sections']['stop_loss'] = self._review_stop_loss(backtest_results)
        
        # Section 5: Diversification
        report['sections']['diversification'] = self._review_diversification(backtest_results)
        
        # Section 6: Recommendations
        report['sections']['recommendations'] = self._generate_recommendations(
            report['sections']
        )
        
        # Save report
        report_file = os.path.join(self.data_dir, 'capital_preservation_review.json')
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        
        # Also save as text
        self._print_review(report)
        
        return report
    
    def _review_risk_reward(self, results: Dict[str, dict]) -> dict:
        """Review R:R compliance across all trades."""
        print("\n1. RISK/REWARD COMPLIANCE REVIEW")
        print("-" * 40)
        
        all_trades = []
        for key, result in results.items():
            for trade in result.get('trades', []):
                all_trades.append(trade)
        
        if not all_trades:
            return {'passed': True, 'total_trades': 0, 'violations': 0}
        
        rr_violations = []
        strong_rr_violations = []
        
        for trade in all_trades:
            exit_type = trade.get('exit_type', 'NO_EXIT')
            
            if exit_type == 'NO_EXIT':
                continue
            
            # Use the setup risk_reward_ratio (potential R:R)
            rr = trade.get('risk_reward_ratio', 0)
            direction = trade.get('direction', '')
            
            if direction.startswith('STRONG_'):
                if rr < 2.0:
                    strong_rr_violations.append({
                        'trade_id': trade['trade_id'],
                        'direction': direction,
                        'rr': rr,
                        'expected_min': 2.0,
                    })
            else:
                if rr < 1.2:
                    rr_violations.append({
                        'trade_id': trade['trade_id'],
                        'direction': direction,
                        'rr': rr,
                        'expected_min': 1.2,
                    })
        
        total_violations = len(rr_violations) + len(strong_rr_violations)
        passed = total_violations == 0
        
        print(f"  Total trades: {len(all_trades)}")
        print(f"  STRONG violations (R:R < 2.0): {len(strong_rr_violations)}")
        print(f"  NORMAL violations (R:R < 1.2): {len(rr_violations)}")
        print(f"  Result: {'PASS' if passed else 'FAIL'}")
        
        return {
            'passed': passed,
            'total_trades': len(all_trades),
            'strong_violations': strong_rr_violations,
            'normal_violations': rr_violations,
            'total_violations': total_violations,
        }
    
    def _review_position_sizing(self, results: Dict[str, dict]) -> dict:
        """Review position sizing and capital allocation."""
        print("\n2. POSITION SIZING REVIEW")
        print("-" * 40)
        
        all_trades = []
        for key, result in results.items():
            for trade in result.get('trades', []):
                all_trades.append(trade)
        
        if not all_trades:
            return {'passed': True, 'total_trades': 0}
        
        risk_violations = []
        
        for trade in all_trades:
            # Calculate risk percentage from trade data
            entry = trade.get('entry', 0)
            stop_loss = trade.get('stop_loss', 0)
            quantity = trade.get('quantity', 0)
            
            if entry > 0 and stop_loss > 0 and quantity > 0:
                risk_per_share = abs(entry - stop_loss)
                risk_amount = risk_per_share * quantity
                capital = 1_000_000  # initial capital
                risk_pct = (risk_amount / capital) * 100
                
                sv = get_strategy(trade.get('strategy_version', 'v1'))
                if risk_pct > sv.risk_per_trade_pct * 1.5:
                    risk_violations.append({
                        'trade_id': trade['trade_id'],
                        'risk_pct': risk_pct,
                        'configured_max': sv.risk_per_trade_pct,
                    })
            else:
                risk_pct = 0
        
        passed = len(risk_violations) == 0
        
        avg_risk_pct = np.mean([
            abs(t.get('entry', 0) - t.get('stop_loss', 0)) * t.get('quantity', 0) / 1_000_000 * 100
            for t in all_trades if t.get('entry') > 0 and t.get('stop_loss') > 0
        ]) if all_trades else 0
        max_risk_pct = max([
            abs(t.get('entry', 0) - t.get('stop_loss', 0)) * t.get('quantity', 0) / 1_000_000 * 100
            for t in all_trades if t.get('entry') > 0 and t.get('stop_loss') > 0
        ]) if all_trades else 0
        
        print(f"  Avg risk per trade: {avg_risk_pct:.2f}%")
        print(f"  Max risk per trade: {max_risk_pct:.2f}%")
        print(f"  Risk violations: {len(risk_violations)}")
        print(f"  Result: {'PASS' if passed else 'FAIL'}")
        
        return {
            'passed': passed,
            'avg_risk_pct': avg_risk_pct,
            'max_risk_pct': max_risk_pct,
            'violations': risk_violations,
            'total_trades': len(all_trades),
        }
    
    def _review_drawdown(self, results: Dict[str, dict]) -> dict:
        """Review drawdown across all strategies."""
        print("\n3. DRAWDOWN ANALYSIS")
        print("-" * 40)
        
        max_dd_overall = 0
        worst_strategy = None
        all_drawdowns = []
        
        for key, result in results.items():
            perf = result.get('performance', {})
            dd = perf.get('max_drawdown', 0)
            all_drawdowns.append(dd)
            
            if dd > max_dd_overall:
                max_dd_overall = dd
                worst_strategy = key
        
        avg_dd = np.mean(all_drawdowns) if all_drawdowns else 0
        
        # Check if max drawdown exceeds 30% (meaningful capital preservation)
        dd_excessive = max_dd_overall > 30.0
        
        print(f"  Max drawdown across all: {max_dd_overall:.1f}% ({worst_strategy})")
        print(f"  Avg drawdown: {avg_dd:.1f}%")
        print(f"  Excessive drawdown (>30%): {'YES - CAPITAL RISK' if dd_excessive else 'NO'}")
        print(f"  Result: {'FAIL' if dd_excessive else 'PASS'}")
        
        return {
            'passed': not dd_excessive,
            'max_drawdown': max_dd_overall,
            'worst_strategy': worst_strategy,
            'avg_drawdown': avg_dd,
            'all_drawdowns': all_drawdowns,
            'excessive_drawdown': dd_excessive,
        }
    
    def _review_stop_loss(self, results: Dict[str, dict]) -> dict:
        """Review stop-loss and target compliance."""
        print("\n4. STOP-LOSS COMPLIANCE")
        print("-" * 40)
        
        all_trades = []
        for key, result in results.items():
            for trade in result.get('trades', []):
                all_trades.append(trade)
        
        if not all_trades:
            return {'passed': True, 'total_trades': 0}
        
        hit_stop = sum(1 for t in all_trades if t.get('exit_type') == 'stop_loss')
        hit_target = sum(1 for t in all_trades if t.get('exit_type') == 'target_1')
        no_exit = sum(1 for t in all_trades if t.get('exit_type') == 'NO_EXIT')
        partial = sum(1 for t in all_trades if t.get('exit_type', '').startswith('target'))
        
        total_exited = hit_stop + hit_target + partial
        
        stop_rate = hit_stop / total_exited * 100 if total_exited > 0 else 0
        target_rate = hit_target / total_exited * 100 if total_exited > 0 else 0
        
        # Stop rate is expected to be high - it's a risk management feature
        # But if stop_rate > 80%, we're stopping out too often
        stop_rate_ok = stop_rate <= 80
        
        print(f"  Total trades: {len(all_trades)}")
        print(f"  Hit stop-loss: {hit_stop} ({stop_rate:.1f}%)")
        print(f"  Hit take-profit: {hit_target} ({target_rate:.1f}%)")
        print(f"  Partial exits: {partial}")
        print(f"  No exit (time-based): {no_exit}")
        print(f"  Result: {'PASS' if stop_rate_ok else 'FAIL - Stop rate too high'}")
        
        return {
            'passed': stop_rate_ok,
            'hit_stop': hit_stop,
            'hit_target': hit_target,
            'partial': partial,
            'no_exit': no_exit,
            'stop_rate_pct': stop_rate,
            'target_rate_pct': target_rate,
        }
    
    def _review_diversification(self, results: Dict[str, dict]) -> dict:
        """Review multi-symbol diversification."""
        print("\n5. DIVERSIFICATION ANALYSIS")
        print("-" * 40)
        
        symbol_stats = {}
        all_trades = []
        
        for key, result in results.items():
            parts = key.split('_')
            symbol = parts[0]
            strategy = '_'.join(parts[1:]) if len(parts) > 2 else parts[1]
            
            perf = result.get('performance', {})
            trades = result.get('trades', [])
            
            if symbol not in symbol_stats:
                symbol_stats[symbol] = {
                    'trade_count': 0,
                    'total_pnl': 0,
                    'strategies': set(),
                }
            
            symbol_stats[symbol]['trade_count'] += len(trades)
            symbol_stats[symbol]['total_pnl'] += perf.get('total_pnl', 0)
            symbol_stats[symbol]['strategies'].add(strategy)
            all_trades.extend(trades)
        
        num_symbols = len(symbol_stats)
        num_trades = len(all_trades)
        
        # Check: are we trading multiple symbols?
        diversified = num_symbols >= 2
        
        positive_symbols = sum(1 for s in symbol_stats.values() if s['total_pnl'] > 0)
        
        print(f"  Symbols traded: {num_symbols}")
        print(f"  Total trades: {num_trades}")
        print(f"  Positive P&L symbols: {positive_symbols}/{num_symbols}")
        print(f"  Diversified: {'YES' if diversified else 'NO'}")
        print(f"  Result: {'PASS' if diversified and positive_symbols > 0 else 'FAIL'}")
        
        return {
            'passed': diversified and positive_symbols > 0,
            'num_symbols': num_symbols,
            'positive_symbols': positive_symbols,
            'num_trades': num_trades,
            'symbol_stats': {k: {**v, 'strategies': list(v['strategies'])} 
                           for k, v in symbol_stats.items()},
        }
    
    def _generate_recommendations(self, sections: dict) -> List[str]:
        """Generate capital preservation recommendations."""
        recommendations = []
        
        rr = sections.get('risk_reward', {})
        ps = sections.get('position_sizing', {})
        dd = sections.get('drawdown', {})
        sl = sections.get('stop_loss', {})
        dv = sections.get('diversification', {})
        
        print("\n6. RECOMMENDATIONS")
        print("-" * 40)
        
        # R:R recommendations
        if not rr.get('passed', True):
            recommendations.append("VIOLATION: Trades with R:R below threshold detected. "
                                 "Review signal engine R:R calculations.")
        else:
            recommendations.append("OK: All trades meet minimum R:R requirements.")
        
        # Position sizing recommendations
        if ps.get('max_risk_pct', 0) > 2.0:
            recommendations.append(f"WARNING: Max risk per trade {ps['max_risk_pct']:.2f}% "
                                 f"exceeds recommended 2%. Consider reducing risk_per_trade_pct.")
        else:
            recommendations.append(f"OK: Risk per trade max {ps['max_risk_pct']:.2f}% within limits.")
        
        # Drawdown recommendations
        if dd.get('excessive_drawdown', False):
            recommendations.append(f"CRITICAL: Max drawdown {dd['max_drawdown']:.1f}% exceeds 30%. "
                                 f"Strategy has significant capital risk. Reduce position size or add filters.")
        elif dd.get('max_drawdown', 0) > 20:
            recommendations.append(f"CAUTION: Max drawdown {dd['max_drawdown']:.1f}% is elevated. "
                                 f"Consider reducing position size or adding volatility filters.")
        else:
            recommendations.append(f"OK: Max drawdown {dd['max_drawdown']:.1f}% is acceptable.")
        
        # Stop-loss recommendations
        if sl.get('stop_rate_pct', 0) > 70:
            recommendations.append(f"CAUTION: Stop rate is {sl['stop_rate_pct']:.1f}%. "
                                 f"Consider reviewing entry timing or volatility conditions.")
        else:
            recommendations.append(f"OK: Stop rate {sl['stop_rate_pct']:.1f}% within acceptable range.")
        
        # Diversification recommendations
        if not dv.get('diversified', False):
            recommendations.append("WARNING: Insufficient symbol diversification. "
                                 "Expand to at least 2-3 symbols.")
        elif dv.get('positive_symbols', 0) < dv.get('num_symbols', 1) * 0.5:
            recommendations.append(f"CAUTION: Only {dv['positive_symbols']}/{dv['num_symbols']} "
                                 f"symbols are profitable. Review symbol-specific issues.")
        else:
            recommendations.append(f"OK: Trading {dv['num_symbols']} symbols with "
                                 f"{dv['positive_symbols']} positive.")
        
        for rec in recommendations:
            print(f"  {rec}")
        
        return recommendations
    
    def _print_review(self, report: dict):
        """Print the full review report."""
        print(f"\n{'='*80}")
        print("CAPITAL PRESERVATION REVIEW SUMMARY")
        print(f"{'='*80}")
        
        sections = report.get('sections', {})
        
        all_passed = True
        for name, data in sections.items():
            if isinstance(data, dict) and 'passed' in data:
                passed = data['passed']
                if not passed:
                    all_passed = False
                print(f"  {name}: {'PASS' if passed else 'FAIL'}")
        
        print(f"\n  OVERALL: {'PASS' if all_passed else 'REVIEW REQUIRED'}")
        print(f"{'='*80}")
