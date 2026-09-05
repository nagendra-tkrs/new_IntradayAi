"use client";

import { useState } from "react";
import Header from "@/components/Header";
import { runBacktest, getStocks } from "@/lib/api";
import { useEffect } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";

export default function BacktestPage() {
  const [stocks, setStocks] = useState<any[]>([]);
  const [symbol, setSymbol] = useState("RELIANCE");
  const [days, setDays] = useState(30);
  const [capital, setCapital] = useState(1000000);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<any>(null);

  useEffect(() => {
    getStocks().then(setStocks).catch(() => {});
  }, []);

  async function handleBacktest() {
    try {
      setLoading(true);
      setResult(null);
      const r = await runBacktest({ symbol, days, initial_capital: capital });
      setResult(r);
    } catch (e: any) {
      alert("Error: " + e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen bg-[#0a0e17]">
      <Header />
      <main className="max-w-[1440px] mx-auto px-4 py-6">
        <div className="mb-6">
          <h1 className="text-2xl font-bold text-white">Backtesting</h1>
          <p className="text-sm text-gray-500">Test strategies on historical data with realistic transaction costs</p>
        </div>

        <div className="card mb-6">
          <div className="flex items-center gap-4 flex-wrap">
            <select value={symbol} onChange={(e) => setSymbol(e.target.value)} className="px-3 py-2 bg-[#111827] border border-[#2d3548] rounded-lg text-white text-sm min-w-[150px]">
              {stocks.map((s: any) => <option key={s.symbol} value={s.symbol}>{s.symbol}</option>)}
            </select>
            <div className="flex items-center gap-2">
              <label className="text-xs text-gray-500">Days:</label>
              <input type="number" value={days} onChange={(e) => setDays(parseInt(e.target.value) || 30)} className="w-20 px-2 py-2 bg-[#111827] border border-[#2d3548] rounded-lg text-white text-sm" />
            </div>
            <div className="flex items-center gap-2">
              <label className="text-xs text-gray-500">Capital:</label>
              <input type="number" value={capital} onChange={(e) => setCapital(parseInt(e.target.value) || 1000000)} className="w-32 px-2 py-2 bg-[#111827] border border-[#2d3548] rounded-lg text-white text-sm" />
            </div>
            <button onClick={handleBacktest} disabled={loading} className="px-5 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm font-bold rounded-lg disabled:opacity-50">
              {loading ? "Running..." : "Run Backtest"}
            </button>
          </div>
        </div>

        {result && !result.error && (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-3 mb-6">
              <div className="card">
                <div className="text-[10px] text-gray-500 uppercase">Total Trades</div>
                <div className="text-lg font-bold text-white">{result.total_trades}</div>
              </div>
              <div className="card">
                <div className="text-[10px] text-gray-500 uppercase">Win Rate</div>
                <div className={`text-lg font-bold ${result.win_rate >= 50 ? "text-green-400" : "text-red-400"}`}>{result.win_rate}%</div>
              </div>
              <div className="card">
                <div className="text-[10px] text-gray-500 uppercase">Total P&L</div>
                <div className={`text-lg font-bold ${result.total_pnl >= 0 ? "text-green-400" : "text-red-400"}`}>₹{result.total_pnl?.toLocaleString()}</div>
              </div>
              <div className="card">
                <div className="text-[10px] text-gray-500 uppercase">Profit Factor</div>
                <div className="text-lg font-bold text-blue-400">{result.profit_factor}</div>
              </div>
              <div className="card">
                <div className="text-[10px] text-gray-500 uppercase">Max Drawdown</div>
                <div className="text-lg font-bold text-red-400">{result.max_drawdown}%</div>
              </div>
              <div className="card">
                <div className="text-[10px] text-gray-500 uppercase">Sharpe Ratio</div>
                <div className="text-lg font-bold text-purple-400">{result.sharpe_ratio}</div>
              </div>
            </div>

            <div className="card mb-6">
              <h3 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wider">Equity Curve</h3>
              {result.equity_curve && result.equity_curve.length > 0 ? (
                <ResponsiveContainer width="100%" height={250}>
                  <LineChart data={result.equity_curve}>
                    <XAxis dataKey="timestamp" tick={{ fill: "#6b7280", fontSize: 10 }} axisLine={{ stroke: "#2d3548" }} tickLine={false} interval="preserveStartEnd" />
                    <YAxis domain={["auto", "auto"]} tick={{ fill: "#6b7280", fontSize: 10 }} axisLine={{ stroke: "#2d3548" }} tickLine={false} width={80} tickFormatter={(v: number) => `₹${(v/100000).toFixed(1)}L`} />
                    <Tooltip contentStyle={{ background: "#1a1f2e", border: "1px solid #2d3548", borderRadius: "8px", fontSize: "12px" }} />
                    <Line type="monotone" dataKey="equity" stroke="#3b82f6" strokeWidth={1.5} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <p className="text-gray-500 text-sm text-center py-6">No equity curve data</p>
              )}
            </div>

            <div className="card">
              <h3 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wider">Trade List</h3>
              {result.trades && result.trades.length > 0 ? (
                <div className="overflow-x-auto">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Direction</th>
                        <th className="num">Entry</th>
                        <th className="num">Exit</th>
                        <th className="num">Qty</th>
                        <th className="num">Cost</th>
                        <th className="num">P&L</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.trades.map((t: any, i: number) => (
                        <tr key={i}>
                          <td><span className={`text-xs font-bold ${String(t.direction).includes("LONG") ? "text-green-400" : "text-red-400"}`}>{String(t.direction).replace("SignalDirection.", "")}</span></td>
                          <td className="num">₹{t.entry?.toLocaleString()}</td>
                          <td className="num">₹{t.exit?.toLocaleString()}</td>
                          <td className="num">{t.quantity}</td>
                          <td className="num text-xs text-gray-400">₹{t.cost?.toFixed(2)}</td>
                          <td className={`num font-semibold ${(t.net_pnl || 0) >= 0 ? "text-green-400" : "text-red-400"}`}>
                            ₹{t.net_pnl?.toLocaleString()}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-gray-500 text-sm text-center py-6">No trades generated</p>
              )}
            </div>

            <div className="mt-4 px-3 py-2 bg-blue-500/10 border border-blue-500/30 rounded-lg text-xs text-blue-400">
              Backtest results use historical yfinance data. Past performance does not indicate future results. Transaction costs (brokerage 0.03%, STT 0.025%, slippage 0.05%) are included.
            </div>
          </>
        )}
      </main>
    </div>
  );
}
