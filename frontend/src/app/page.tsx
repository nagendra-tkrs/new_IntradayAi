"use client";

import { useEffect, useState } from "react";
import Header from "@/components/Header";
import SignalCard from "@/components/SignalCard";
import { runScanner, getMarketStatus } from "@/lib/api";
import Link from "next/link";

export default function Dashboard() {
  const [scanner, setScanner] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        setLoading(true);
        const data = await runScanner();
        setScanner(data);
      } catch (e: any) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  return (
    <div className="min-h-screen bg-[#0a0e17]">
      <Header />
      <main className="max-w-[1440px] mx-auto px-4 py-6">
        <div className="mb-6">
          <h1 className="text-2xl font-bold text-white">Dashboard</h1>
          <p className="text-sm text-gray-500 mt-1">
            Intraday market scanner with multi-factor signal analysis
          </p>
          <div className="mt-2 px-3 py-2 bg-blue-500/10 border border-blue-500/30 rounded-lg text-xs text-blue-400">
            Data sourced via yfinance (Yahoo Finance). Prices may be delayed. This is not financial advice.
          </div>
        </div>

        {loading && (
          <div className="flex items-center justify-center py-20">
            <div className="text-center">
              <div className="w-12 h-12 border-4 border-blue-500/30 border-t-blue-500 rounded-full animate-spin mx-auto mb-4" />
              <p className="text-gray-400 text-sm">Scanning NIFTY50 universe...</p>
            </div>
          </div>
        )}

        {error && (
          <div className="card bg-red-500/10 border-red-500/30 text-red-400 text-sm">
            Error: {error}
          </div>
        )}

        {scanner && !loading && (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
              <div className="card">
                <div className="text-xs text-gray-500 uppercase tracking-wider">Stocks Scanned</div>
                <div className="text-xl font-bold text-white mt-1">{scanner.total_scanned}</div>
              </div>
              <div className="card">
                <div className="text-xs text-gray-500 uppercase tracking-wider">Signals Found</div>
                <div className="text-xl font-bold text-blue-400 mt-1">{scanner.signals_found}</div>
              </div>
              <div className="card">
                <div className="text-xs text-gray-500 uppercase tracking-wider">Data Source</div>
                <div className="text-xl font-bold text-yellow-400 mt-1">{scanner.data_source}</div>
              </div>
              <div className="card">
                <div className="text-xs text-gray-500 uppercase tracking-wider">Strong Signals</div>
                <div className="text-xl font-bold text-green-400 mt-1">
                  {scanner.top_signals?.filter((s: any) => s.confidence >= 80).length || 0}
                </div>
              </div>
            </div>

            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-bold text-white">Top Signals</h2>
              <Link href="/scanner" className="text-sm text-blue-400 hover:text-blue-300">
                View Full Scanner &rarr;
              </Link>
            </div>

            {scanner.top_signals && scanner.top_signals.length > 0 ? (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
                {scanner.top_signals.map((signal: any) => (
                  <SignalCard
                    key={signal.symbol}
                    signal={signal.signal_data}
                    onClick={() => window.location.href = `/stock/${signal.symbol}`}
                  />
                ))}
              </div>
            ) : (
              <div className="card text-center py-12 text-gray-500">
                <p className="text-lg mb-2">No strong signals detected</p>
                <p className="text-xs">The scanner evaluates multiple confirmations before generating signals.</p>
              </div>
            )}

            <div className="flex items-center justify-between mb-4 mt-8">
              <h2 className="text-lg font-bold text-white">All Stocks</h2>
            </div>
            <div className="card overflow-x-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Sector</th>
                    <th className="num">Price</th>
                    <th className="num">Change %</th>
                    <th className="num">Volume</th>
                    <th className="num">RSI</th>
                    <th className="num">Trend</th>
                    <th>Signal</th>
                    <th className="num">Confidence</th>
                  </tr>
                </thead>
                <tbody>
                  {scanner.results?.map((stock: any) => (
                    <tr key={stock.symbol} className="cursor-pointer" onClick={() => window.location.href = `/stock/${stock.symbol}`}>
                      <td>
                        <div className="font-semibold text-white">{stock.symbol}</div>
                        <div className="text-[10px] text-gray-500">{stock.name}</div>
                      </td>
                      <td className="text-xs text-gray-400">{stock.sector}</td>
                      <td className="num font-semibold">₹{stock.price?.toLocaleString()}</td>
                      <td className={`num font-semibold ${stock.change_pct >= 0 ? "text-green-400" : "text-red-400"}`}>
                        {stock.change_pct >= 0 ? "+" : ""}{stock.change_pct?.toFixed(2)}%
                      </td>
                      <td className="num text-xs">{stock.volume?.toLocaleString()}</td>
                      <td className={`num text-xs ${stock.rsi > 70 ? "text-red-400" : stock.rsi < 30 ? "text-green-400" : ""}`}>
                        {stock.rsi?.toFixed(1) || "-"}
                      </td>
                      <td className={`num text-xs font-semibold ${
                        stock.trend === "BULLISH" ? "text-green-400" :
                        stock.trend === "BEARISH" ? "text-red-400" : "text-gray-400"
                      }`}>{stock.trend || "-"}</td>
                      <td>
                        <span className={`inline-block px-2 py-0.5 rounded-full text-[10px] font-bold ${
                          stock.signal?.includes("LONG") ? "badge-long" :
                          stock.signal?.includes("SHORT") ? "badge-short" : "badge-no-trade"
                        }`}>{stock.signal}</span>
                      </td>
                      <td className={`num font-bold ${
                        stock.confidence >= 80 ? "text-green-400" :
                        stock.confidence >= 60 ? "text-blue-400" : "text-gray-400"
                      }`}>{stock.confidence || 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
