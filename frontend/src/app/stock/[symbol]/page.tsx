"use client";

import { useEffect, useState, use } from "react";
import Header from "@/components/Header";
import MiniChart from "@/components/MiniChart";
import IndicatorPanel from "@/components/IndicatorPanel";
import SignalCard from "@/components/SignalCard";
import { getStockDetail, getStockChart, placePaperOrder } from "@/lib/api";

export default function StockDetailPage({ params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = use(params);
  const [detail, setDetail] = useState<any>(null);
  const [chartData, setChartData] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(1);
  const [orderQty, setOrderQty] = useState(1);
  const [orderMsg, setOrderMsg] = useState<string | null>(null);

  useEffect(() => {
    loadData();
  }, [symbol, days]);

  async function loadData() {
    try {
      setLoading(true);
      const [d, c] = await Promise.all([
        getStockDetail(symbol),
        getStockChart(symbol, days),
      ]);
      setDetail(d);
      setChartData(c.data || []);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }

  async function handlePaperOrder(direction: string) {
    try {
      setOrderMsg(null);
      const signal = detail?.signal;
      const setup = signal?.setup || {};
      await placePaperOrder({
        symbol,
        direction,
        quantity: orderQty,
        entry_price: setup.entry || detail?.quote?.price,
        stop_loss: setup.stop_loss,
        target_1: setup.target_1,
        target_2: setup.target_2,
      });
      setOrderMsg(`${direction} order placed for ${orderQty} shares of ${symbol}`);
    } catch (e: any) {
      setOrderMsg(`Error: ${e.message}`);
    }
  }

  if (loading) {
    return (
      <div className="min-h-screen bg-[#0a0e17]">
        <Header />
        <div className="flex items-center justify-center py-20">
          <div className="w-10 h-10 border-4 border-blue-500/30 border-t-blue-500 rounded-full animate-spin" />
        </div>
      </div>
    );
  }

  const quote = detail?.quote || {};
  const signal = detail?.signal;
  const indicators = detail?.indicators || {};
  const setup = signal?.setup || {};

  return (
    <div className="min-h-screen bg-[#0a0e17]">
      <Header />
      <main className="max-w-[1440px] mx-auto px-4 py-6">
        <div className="flex items-start justify-between mb-6">
          <div>
            <h1 className="text-3xl font-bold text-white">{symbol}</h1>
            <div className="flex items-center gap-4 mt-2">
              <span className="text-2xl font-bold">₹{quote.price?.toLocaleString()}</span>
              <span className={`text-lg font-semibold ${quote.change >= 0 ? "text-green-400" : "text-red-400"}`}>
                {quote.change >= 0 ? "+" : ""}{quote.change?.toFixed(2)} ({quote.change_pct?.toFixed(2)}%)
              </span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-500">Paper Trade Qty:</span>
            <input
              type="number"
              value={orderQty}
              onChange={(e) => setOrderQty(parseInt(e.target.value) || 1)}
              className="w-20 px-2 py-1 bg-[#111827] border border-[#2d3548] rounded text-white text-sm"
            />
            <button onClick={() => handlePaperOrder("LONG")} className="px-3 py-1.5 bg-green-600 hover:bg-green-500 text-white text-sm font-bold rounded-lg">
              BUY
            </button>
            <button onClick={() => handlePaperOrder("SHORT")} className="px-3 py-1.5 bg-red-600 hover:bg-red-500 text-white text-sm font-bold rounded-lg">
              SELL
            </button>
          </div>
        </div>

        {orderMsg && (
          <div className={`mb-4 px-3 py-2 rounded-lg text-xs ${
            orderMsg.startsWith("Error") ? "bg-red-500/10 border border-red-500/30 text-red-400" : "bg-green-500/10 border border-green-500/30 text-green-400"
          }`}>{orderMsg}</div>
        )}

        <div className="mb-2 px-3 py-2 bg-blue-500/10 border border-blue-500/30 rounded-lg text-xs text-blue-400">
          Data sourced via yfinance. Prices may be delayed. This is a paper-trading analysis tool, not financial advice.
        </div>

        <div className="mb-4">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-xs text-gray-500">Timeframe:</span>
            {[1, 3, 5, 10].map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`px-2.5 py-1 text-xs rounded-lg font-semibold ${
                  days === d ? "bg-blue-600 text-white" : "bg-[#1a1f2e] text-gray-400 hover:bg-[#222839]"
                }`}
              >
                {d}D
              </button>
            ))}
          </div>
          <div className="card">
            <MiniChart
              data={chartData}
              height={350}
              entryLine={setup.entry}
              stopLine={setup.stop_loss}
              targetLine={setup.target_1}
            />
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
          <div className="lg:col-span-2">
            <h3 className="text-sm font-semibold text-gray-400 mb-2 uppercase tracking-wider">Technical Indicators</h3>
            <IndicatorPanel indicators={indicators} />
          </div>
          <div>
            {signal && signal.direction !== "NO TRADE" ? (
              <SignalCard signal={signal} />
            ) : (
              <div className="card">
                <h3 className="text-sm font-semibold text-gray-400 mb-2 uppercase tracking-wider">Current Signal</h3>
                <div className="text-center py-6">
                  <span className="inline-block px-3 py-1 rounded-full text-xs font-bold badge-no-trade">NO TRADE</span>
                  <p className="text-xs text-gray-500 mt-3">
                    Multiple confirmations not met. High-quality NO TRADE is preferable to a low-quality setup.
                  </p>
                </div>
              </div>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
