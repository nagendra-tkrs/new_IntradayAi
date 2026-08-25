"""
Phase 5: Baseline Backtest

Runs v1 strategy on all available data and produces a comprehensive baseline report.
Uses yfinance 5-minute intraday data (~60 calendar days).
"""
import asyncio
import json
import os
import pandas as pd
from app.services.backtesting import BacktestEngine
from app.services.market_data.yfinance_provider import YFinanceMarketDataProvider
from app.core.market_session import now_ist


async def main():
    symbols = ['RELIANCE.NS', 'TCS.NS', 'HDFCBANK.NS']
    provider = YFinanceMarketDataProvider()

    print("=" * 80)
    print("PHASE 5: BASELINE BACKTEST")
    print(f"Date: {now_ist()}")
    print(f"Strategy: v1 (production)")
    print(f"Symbols: {', '.join(s.replace('.NS', '') for s in symbols)}")
    print("=" * 80)

    all_results = {}
    all_trades = []
    data_info = {}

    for symbol in symbols:
        clean = symbol.replace('.NS', '')
        print(f"\n{'-'*60}")
        print(f"  {clean}")
        print(f"{'-'*60}")

        df = await provider.get_intraday_bars(symbol, timeframe='5m')
        if df.empty:
            print(f"  No data available for {clean}")
            continue

        start_date = str(df['timestamp'].min())[:10] if 'timestamp' in df.columns else 'N/A'
        end_date = str(df['timestamp'].max())[:10] if 'timestamp' in df.columns else 'N/A'
        print(f"  Data: {start_date} to {end_date} ({len(df)} bars)")

        data_info[clean] = {
            'start': start_date,
            'end': end_date,
            'bars': len(df),
        }

        bt = BacktestEngine(initial_capital=1_000_000, strategy_version='v1')
        result = bt.run(df, symbol, strategy_version='v1')
        perf = result['performance']
        trades = result['trades']

        all_results[clean] = perf
        all_trades.extend(trades)

        print(f"  Trades: {perf['total_trades']}")
        print(f"  Win Rate: {perf['win_rate']:.1f}%")
        print(f"  Profit Factor: {perf['profit_factor']:.2f}")
        print(f"  Expectancy: {perf['expectancy']:.2f}")
        print(f"  Avg R:Multiple: {perf['avg_r_multiple']:.2f}")
        print(f"  Max Drawdown: {perf['max_drawdown']:.1f}%")
        print(f"  Total PnL: {perf['total_pnl']:.2f}")

        # Direction breakdown
        by_dir = {}
        for t in trades:
            d = t['direction']
            if d not in by_dir:
                by_dir[d] = {'count': 0, 'wins': 0, 'pnl': 0}
            by_dir[d]['count'] += 1
            if t['net_pnl'] > 0:
                by_dir[d]['wins'] += 1
            by_dir[d]['pnl'] += t['net_pnl']

        print(f"\n  By Direction:")
        for d, data in sorted(by_dir.items()):
            wr = data['wins'] / data['count'] * 100 if data['count'] > 0 else 0
            print(f"    {d}: {data['count']} trades, {wr:.1f}% win, PnL={data['pnl']:.2f}")

    # Aggregate summary
    print(f"\n{'='*80}")
    print("AGGREGATE SUMMARY")
    print(f"{'='*80}")
    total_trades = sum(p['total_trades'] for p in all_results.values())
    total_pnl = sum(p['total_pnl'] for p in all_results.values())
    avg_pf = sum(p['profit_factor'] for p in all_results.values() if p['profit_factor'] < 999) / max(1, sum(1 for p in all_results.values() if p['profit_factor'] < 999))
    avg_exp = sum(p['expectancy'] for p in all_results.values()) / max(1, len(all_results))
    avg_dd = sum(p['max_drawdown'] for p in all_results.values()) / max(1, len(all_results))

    print(f"  Total Trades: {total_trades}")
    print(f"  Total PnL: {total_pnl:.2f}")
    print(f"  Avg Profit Factor: {avg_pf:.2f}")
    print(f"  Avg Expectancy: {avg_exp:.2f}")
    print(f"  Avg Max Drawdown: {avg_dd:.1f}%")

    # Aggregate direction breakdown
    agg_by_dir = {}
    for t in all_trades:
        d = t['direction']
        if d not in agg_by_dir:
            agg_by_dir[d] = {'count': 0, 'wins': 0, 'pnl': 0}
        agg_by_dir[d]['count'] += 1
        if t['net_pnl'] > 0:
            agg_by_dir[d]['wins'] += 1
        agg_by_dir[d]['pnl'] += t['net_pnl']

    print(f"\n  By Direction (aggregate):")
    for d, data in sorted(agg_by_dir.items()):
        wr = data['wins'] / data['count'] * 100 if data['count'] > 0 else 0
        print(f"    {d}: {data['count']} trades, {wr:.1f}% win, PnL={data['pnl']:.2f}")

    # Save results
    output = {
        'date': now_ist().isoformat(),
        'strategy': 'v1',
        'data_info': data_info,
        'aggregate': {
            'total_trades': total_trades,
            'total_pnl': total_pnl,
            'avg_profit_factor': avg_pf,
            'avg_expectancy': avg_exp,
            'avg_max_drawdown': avg_dd,
        },
        'by_symbol': all_results,
        'by_direction': agg_by_dir,
    }

    out_dir = os.path.join(os.path.dirname(__file__), 'app', 'data', 'review')
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, 'baseline_results.json')
    with open(out_file, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved to: {out_file}")

    print(f"\n  DATA LIMITATION: yfinance 5-min intraday data covers ~60 calendar days.")
    print(f"  This is NOT 2019-2024. Results are based on the available period only.")
    print(f"  For longer history, use a paid data provider (e.g., Kite, Upstox, TrueData).")


if __name__ == '__main__':
    asyncio.run(main())
