"""
Phase 11: Daily Report Generation

Generates a comprehensive daily report covering:
1. Current strategy version
2. Signal distribution (STRONG_LONG/SHORT/LONG/SHORT/NO_TRADE)
3. Performance summary (win rate, PF, expectancy, avg R, max DD)
4. STRONG-specific performance
5. Symbol breakdown
6. Full active parameters
7. Walk-forward validation
8. Candidate strategies (validation vs OOS)
9. Deployment & rollback status
10. Recommendations
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Optional
from app.services.strategy_config import STRATEGY_REGISTRY, get_strategy
from app.services.strategy_rollback import StrategyVersionHistory
from app.core.market_session import now_ist


class DailyReportGenerator:
    """
    Generates daily performance and strategy evaluation reports.
    """

    def __init__(self, report_dir: str = None, version_history: StrategyVersionHistory = None):
        self.report_dir = report_dir or os.path.join(
            os.path.dirname(__file__), '..', 'data', 'reports'
        )
        os.makedirs(self.report_dir, exist_ok=True)
        self.version_history = version_history or StrategyVersionHistory()

    def generate_report(self,
                       date: Optional[str] = None,
                       stats: Optional[dict] = None,
                       wf_results: Optional[dict] = None,
                       comparison: Optional[dict] = None,
                       capital: float = 1_000_000,
                       data_range: Optional[dict] = None) -> str:
        """
        Generate a comprehensive daily report.

        Returns:
            Path to generated report file
        """
        date_str = date or now_ist().date().isoformat()

        current_version = self.version_history.get_current_deployed() or 'v1'
        sv = get_strategy(current_version)

        # Build report content
        lines = []
        lines.append(f"{'='*80}")
        lines.append(f"DAILY TRADING REPORT - {date_str}")
        lines.append(f"{'='*80}")
        lines.append("")

        # Section 1: Current Strategy Version
        lines.append("1. CURRENT STRATEGY VERSION")
        lines.append(f"{'-'*40}")
        lines.append(f"  Version: {current_version}")
        lines.append(f"  Description: {sv.description}")
        lines.append("")

        # Section 2: Signal Distribution
        lines.append("2. SIGNAL DISTRIBUTION")
        lines.append(f"{'-'*40}")
        signal_counts = self._compute_signal_distribution(stats)
        if signal_counts:
            for direction, count in sorted(signal_counts.items()):
                lines.append(f"  {direction}: {count}")
            total_signals = sum(signal_counts.values())
            lines.append(f"  TOTAL: {total_signals}")
        else:
            lines.append("  No signal data available")
        lines.append("")

        # Section 3: Performance Summary (aggregate)
        lines.append("3. PERFORMANCE SUMMARY")
        lines.append(f"{'-'*40}")
        if stats and 'current' in stats:
            current = stats['current']
            lines.append(f"  Total Trades: {current.get('total_trades', 0)}")
            lines.append(f"  Total P&L: {current.get('total_pnl', 0):.2f}")
            lines.append(f"  Avg Profit Factor: {current.get('avg_profit_factor', 0):.2f}")
            lines.append(f"  Avg Expectancy: {current.get('avg_expectancy', 0):.2f}")
        else:
            lines.append("  No performance data available")
        lines.append("")

        # Section 4: STRONG-Specific Performance
        lines.append("4. STRONG SIGNAL PERFORMANCE")
        lines.append(f"{'-'*40}")
        strong_stats = self._compute_strong_performance(stats)
        if strong_stats:
            lines.append(f"  STRONG trades: {strong_stats['count']}")
            lines.append(f"  Win Rate: {strong_stats['win_rate']:.1f}%")
            lines.append(f"  Expectancy: {strong_stats['expectancy']:.2f}")
            lines.append(f"  Profit Factor: {strong_stats['profit_factor']:.2f}")
            lines.append(f"  Avg R:Multiple: {strong_stats['avg_r']:.2f}")
        else:
            lines.append("  No STRONG signal trades found")
        lines.append("")

        # Section 5: Symbol Breakdown
        lines.append("5. SYMBOL BREAKDOWN")
        lines.append(f"{'-'*40}")
        if stats:
            for symbol, s in stats.items():
                if symbol == 'current':
                    continue
                lines.append(f"  {symbol}:")
                lines.append(f"    Trades: {s['total_trades']}")
                lines.append(f"    Win Rate: {s['win_rate']:.1f}%")
                lines.append(f"    Profit Factor: {s['profit_factor']:.2f}")
                lines.append(f"    Expectancy: {s['expectancy']:.2f}")
                lines.append(f"    Max Drawdown: {s['max_drawdown']:.1f}%")
                lines.append(f"    Avg R:Multiple: {s['avg_r_multiple']:.2f}")
        else:
            lines.append("  No symbol data available")
        lines.append("")

        # Section 6: Full Active Parameters
        lines.append("6. ACTIVE STRATEGY PARAMETERS")
        lines.append(f"{'-'*40}")
        lines.append(f"  --- Score Thresholds ---")
        lines.append(f"  STRONG Long:  >= {sv.strong_long_threshold}")
        lines.append(f"  LONG:         >= {sv.long_threshold}")
        lines.append(f"  WEAK Long:    >= {sv.weak_long_threshold}")
        lines.append(f"  NO_TRADE:     >= {sv.no_trade_threshold}")
        lines.append(f"  WEAK Short:   >= {sv.weak_short_threshold}")
        lines.append(f"  SHORT:        >= {sv.short_threshold}")
        lines.append(f"  STRONG Short: <  {sv.short_threshold}")
        lines.append(f"  Min Score:    {sv.min_score}")
        lines.append(f"  --- Risk/Reward ---")
        lines.append(f"  STRONG Min R:R: {sv.min_rr_strong}")
        lines.append(f"  Normal Min R:R: {sv.min_rr_normal}")
        lines.append(f"  --- ATR Multipliers ---")
        lines.append(f"  STRONG Target: {sv.strong_atr_mult}x ATR")
        lines.append(f"  Normal Target: {sv.normal_atr_mult}x ATR")
        lines.append(f"  Stop Loss:     {sv.stop_loss_atr_mult}x ATR")
        lines.append(f"  --- STRONG Quality Filters ---")
        lines.append(f"  Min ADX: {sv.strong_min_adx}")
        lines.append(f"  Min RelVol: {sv.strong_min_rel_vol}")
        lines.append(f"  Max RSI (Long): {sv.strong_max_rsi_long}")
        lines.append(f"  Min RSI (Short): {sv.strong_max_rsi_short}")
        lines.append(f"  --- Risk Management ---")
        lines.append(f"  Risk Per Trade: {sv.risk_per_trade_pct}%")
        lines.append(f"  Capital: {capital:.0f}")
        lines.append("")

        # Section 7: Walk-Forward Validation
        lines.append("7. WALK-FORWARD VALIDATION")
        lines.append(f"{'-'*40}")
        if wf_results:
            for symbol, folds in wf_results.items():
                lines.append(f"  {symbol}:")
                for fold in folds:
                    lines.append(f"    Cycle {fold['fold']}: PnL={fold['test_pnl']:.2f}, "
                                  f"PF={fold['test_pf']:.2f}, "
                                  f"Trades={fold['test_trades']}")
        else:
            lines.append("  No walk-forward results")
        lines.append("")

        # Section 8: Candidate Strategies
        lines.append("8. CANDIDATE STRATEGIES")
        lines.append(f"{'-'*40}")
        if comparison:
            lines.append(f"  Production: {current_version}")
            lines.append(f"  {'Candidate':<20} {'Decision':<10} {'Avg PF':<10} {'Avg PnL':<12} {'WF':<5}")
            lines.append(f"  {'-'*60}")
            for cname, comp in comparison.items():
                decision = comp.get('decision', 'UNKNOWN')
                wf_sym = comp.get('wf_positive_symbols', 0)
                wf_total = comp.get('wf_total_symbols', 0)
                wf_str = f"{wf_sym}/{wf_total}"
                lines.append(f"  {cname:<20} {decision:<10} {comp.get('avg_pf', 0):<10.2f} "
                             f"{comp.get('avg_pnl', 0):<12.2f} {wf_str:<5}")
        else:
            lines.append("  No candidate comparisons")
        lines.append("")

        # Section 9: Deployment & Rollback Status
        lines.append("9. DEPLOYMENT STATUS")
        lines.append(f"{'-'*40}")
        deployments = self.version_history.get_all_deployments()
        if deployments:
            lines.append(f"  Current: {deployments[-1]['version']} "
                          f"(deployed {deployments[-1]['deployed_at']})")
        else:
            lines.append("  No deployments recorded")

        rollbacks = self.version_history.get_rollback_history()
        lines.append(f"  Rollback events: {len(rollbacks)}")
        if rollbacks:
            for rb in rollbacks[-3:]:
                lines.append(f"    {rb['from_version']} -> {rb['to_version']} "
                              f"({rb['reason']})")
        lines.append("")

        # Section 10: Data Period
        lines.append("10. DATA PERIOD")
        lines.append(f"{'-'*40}")
        if data_range:
            lines.append(f"  Source: {data_range.get('source', 'yfinance')}")
            lines.append(f"  Period: {data_range.get('start', 'N/A')} to {data_range.get('end', 'N/A')}")
            lines.append(f"  Timeframe: {data_range.get('timeframe', '5m')}")
            lines.append(f"  Total Bars: {data_range.get('total_bars', 'N/A')}")
        else:
            lines.append("  Data range not specified")
        lines.append("")

        # Section 11: Recommendations
        lines.append("11. RECOMMENDATIONS")
        lines.append(f"{'-'*40}")
        recommendations = self._generate_recommendations(stats, wf_results, comparison)
        for rec in recommendations:
            status = "PASS" if rec['status_ok'] else "FAIL"
            lines.append(f"  [{status}] {rec['check']}: {rec['recommendation']}")
        lines.append("")

        lines.append(f"{'='*80}")
        lines.append("END OF REPORT")
        lines.append(f"{'='*80}")

        report_text = "\n".join(lines)

        # Save report
        report_file = os.path.join(self.report_dir, f'report_{date_str}.txt')
        with open(report_file, 'w') as f:
            f.write(report_text)

        # Also save as JSON
        json_file = os.path.join(self.report_dir, f'report_{date_str}.json')
        report_data = {
            'date': date_str,
            'strategy_version': current_version,
            'generated_at': now_ist().isoformat(),
            'data_range': data_range,
            'stats': stats,
            'signal_distribution': signal_counts,
            'strong_performance': strong_stats,
            'wf_results': wf_results,
            'comparison': comparison,
            'recommendations': recommendations,
        }
        with open(json_file, 'w') as f:
            json.dump(report_data, f, indent=2, default=str)

        return report_file

    def _compute_signal_distribution(self, stats: dict) -> dict:
        """Count signals by direction across all symbols."""
        counts = {}
        if not stats:
            return counts
        for symbol, s in stats.items():
            if symbol == 'current':
                continue
            by_dir = s.get('by_direction', {})
            for direction, data in by_dir.items():
                counts[direction] = counts.get(direction, 0) + data.get('count', 0)
        return counts

    def _compute_strong_performance(self, stats: dict) -> Optional[dict]:
        """Compute aggregate performance for STRONG signals only."""
        if not stats:
            return None

        strong_trades = 0
        strong_wins = 0
        strong_pnl = 0
        strong_profit = 0
        strong_loss = 0
        strong_r_sum = 0

        for symbol, s in stats.items():
            if symbol == 'current':
                continue
            by_dir = s.get('by_direction', {})
            for direction, data in by_dir.items():
                if 'STRONG' not in direction:
                    continue
                count = data.get('count', 0)
                wins = data.get('wins', 0)
                pnl = data.get('total_pnl', 0)
                strong_trades += count
                strong_wins += wins
                strong_pnl += pnl
                if pnl > 0:
                    strong_profit += pnl
                else:
                    strong_loss += abs(pnl)

        if strong_trades == 0:
            return None

        win_rate = (strong_wins / strong_trades * 100) if strong_trades > 0 else 0
        avg_r = strong_pnl / strong_trades if strong_trades > 0 else 0
        pf = strong_profit / strong_loss if strong_loss > 0 else 999.0
        expectancy = strong_pnl / strong_trades

        return {
            'count': strong_trades,
            'wins': strong_wins,
            'win_rate': win_rate,
            'total_pnl': strong_pnl,
            'profit_factor': pf,
            'expectancy': expectancy,
            'avg_r': avg_r,
        }

    def _generate_recommendations(self, stats: dict, wf_results: dict,
                                  comparison: dict) -> List[dict]:
        """Generate actionable recommendations based on performance."""
        recommendations = []

        if stats and 'current' in stats:
            current = stats['current']

            pf = current.get('avg_profit_factor', 0)
            recommendations.append({
                'check': 'Profit Factor >= 1.5',
                'status_ok': pf >= 1.5,
                'recommendation': f"PF={pf:.2f}" + (
                    "" if pf >= 1.5 else ". Consider reviewing strategy risk/reward logic"
                ),
            })

            total_trades = current.get('total_trades', 0)
            recommendations.append({
                'check': 'Trade frequency > 5/day',
                'status_ok': total_trades >= 5,
                'recommendation': f"Trades={total_trades}" + (
                    "" if total_trades >= 5 else ". Strategy may be too restrictive"
                ),
            })

            exp = current.get('avg_expectancy', 0)
            recommendations.append({
                'check': 'Positive expectancy',
                'status_ok': exp > 0,
                'recommendation': f"Exp={exp:.2f}" + (
                    "" if exp > 0 else ". Review signal quality and exit logic"
                ),
            })

            symbol_stats = {k: v for k, v in stats.items() if k != 'current'}
            positive_count = sum(1 for s in symbol_stats.values()
                                if s.get('expectancy', 0) > 0)
            total_count = len(symbol_stats)
            recommendations.append({
                'check': 'Multi-symbol consistency',
                'status_ok': positive_count >= total_count * 0.5,
                'recommendation': f"{positive_count}/{total_count} symbols positive" + (
                    "" if positive_count >= total_count * 0.5
                    else ". Investigate symbol-specific issues"
                ),
            })
        else:
            recommendations.append({
                'check': 'Performance data available',
                'status_ok': False,
                'recommendation': 'No performance statistics recorded for today',
            })

        if comparison:
            better_candidates = [c for c, v in comparison.items() if v.get('is_better')]
            recommendations.append({
                'check': 'Candidate evaluation',
                'status_ok': len(better_candidates) == 0,
                'recommendation': (
                    f"{len(better_candidates)} candidate(s) ready for promotion"
                    if better_candidates else "All candidates rejected - current strategy validated"
                ),
            })

        return recommendations
