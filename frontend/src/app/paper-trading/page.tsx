"use client";

import { useEffect, useState } from "react";
import Header from "@/components/Header";
import { getPortfolio, getPositions, getTradeHistory, placePaperOrder, closePosition, getStocks } from "@/lib/api";

export default function PaperTradingPage() {
  const [portfolio, setPortfolio] = useState<any>(null);
  const [positions, setPositions] = useState<any[]>([]);
  const [trades, setTrades] = useState<any[]>([]);
  const [stocks, setStocks] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [orderSymbol, setOrderSymbol] = useState("");
  const [orderDir, setOrderDir] = useState("LONG");
  const [orderQty, setOrderQty] = useState(10);
  const [orderMsg, setOrderMsg] = useState<string | null>(null);

  useEffect(() => {
    loadData();
  }, []);

  async function loadData() {
    try {
      setLoading(true);
      const [p, pos, t, s] = await Promise.all([
        getPortfolio(), getPositions(), getTradeHistory(50), getStocks()
      ]);
      setPortfolio(p);
      setPositions(pos.positions || []);
      setTrades(t.trades || []);
      setStocks(s || []);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }

  async function handleOrder() {
    try {
      setOrderMsg(null);
      await placePaperOrder({ symbol: orderSymbol, direction: orderDir, quantity: orderQty });
      setOrderMsg(`${orderDir} order placed for ${orderQty} shares of ${orderSymbol}`);
      loadData();
    } catch (e: any) {
      setOrderMsg(`Error: ${e.message}`);
    }
  }

  async function handleClose(posId: string) {
    try {
      await closePosition(posId);
      loadData();
    } catch (e: any) {
      console.error(e);
    }
  }

  return (
    <div className="min-h-screen bg-[#0a0e17]">
      <Header />
      <main className="max-w-[1440px] mx-auto px-4 py-6">
        <div className="mb-6">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold text-white">Paper Trading</h1>
            <span className="px-2 py-0.5 bg-blue-500/10 border border-blue-500/30 rounded text-xs text-blue-400 font-bold">
              DATA: yfinance | EXECUTION: PAPER
            </span>
          </div>
          <p className="text-sm text-gray-500 mt-1">Virtual portfolio with paper order execution. No real money at risk. Data from yfinance may be delayed.</p>
        </div>

        {portfolio && (
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 mb-6">
            <div className="card">
              <div className="text-[10px] text-gray-500 uppercase tracking-wider">Total Value</div>
              <div className="text-xl font-bold text-white mt-1">₹{portfolio.total_value?.toLocaleString()}</div>
            </div>
            <div className="card">
              <div className="text-[10px] text-gray-500 uppercase tracking-wider">Cash</div>
              <div className="text-xl font-bold text-blue-400 mt-1">₹{portfolio.cash?.toLocaleString()}</div>
            </div>
            <div className="card">
              <div className="text-[10px] text-gray-500 uppercase tracking-wider">Total P&L</div>
              <div className={`text-xl font-bold mt-1 ${(portfolio.total_pnl || 0) >= 0 ? "text-green-400" : "text-red-400"}`}>
                ₹{portfolio.total_pnl?.toLocaleString()}
              </div>
            </div>
            <div className="card">
              <div className="text-[10px] text-gray-500 uppercase tracking-wider">Unrealized</div>
              <div className={`text-xl font-bold mt-1 ${(portfolio.unrealized_pnl || 0) >= 0 ? "text-green-400" : "text-red-400"}`}>
                ₹{portfolio.unrealized_pnl?.toLocaleString()}
              </div>
            </div>
            <div className="card">
              <div className="text-[10px] text-gray-500 uppercase tracking-wider">Positions</div>
              <div className="text-xl font-bold text-white mt-1">{portfolio.positions_count}</div>
            </div>
          </div>
        )}

        <div className="card mb-6">
          <h3 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wider">Place Order</h3>
          <div className="flex items-center gap-3 flex-wrap">
            <select value={orderSymbol} onChange={(e) => setOrderSymbol(e.target.value)} className="px-3 py-2 bg-[#111827] border border-[#2d3548] rounded-lg text-white text-sm min-w-[150px]">
              <option value="">Select Stock</option>
              {stocks.map((s: any) => <option key={s.symbol} value={s.symbol}>{s.symbol}</option>)}
            </select>
            <select value={orderDir} onChange={(e) => setOrderDir(e.target.value)} className="px-3 py-2 bg-[#111827] border border-[#2d3548] rounded-lg text-white text-sm">
              <option value="LONG">LONG</option>
              <option value="SHORT">SHORT</option>
            </select>
            <input type="number" value={orderQty} onChange={(e) => setOrderQty(parseInt(e.target.value) || 1)} className="w-24 px-3 py-2 bg-[#111827] border border-[#2d3548] rounded-lg text-white text-sm" placeholder="Qty" />
            <button onClick={handleOrder} disabled={!orderSymbol} className="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm font-bold rounded-lg disabled:opacity-50">
              Place Order
            </button>
          </div>
          {orderMsg && (
            <div className={`mt-2 text-xs ${orderMsg.startsWith("Error") ? "text-red-400" : "text-green-400"}`}>{orderMsg}</div>
          )}
        </div>

        {positions.length > 0 && (
          <div className="card mb-6">
            <h3 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wider">Open Positions</h3>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Direction</th>
                  <th className="num">Qty</th>
                  <th className="num">Entry</th>
                  <th className="num">Current</th>
                  <th className="num">P&L</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos) => (
                  <tr key={pos.id}>
                    <td className="font-semibold text-white">{pos.symbol}</td>
                    <td><span className={`text-xs font-bold ${pos.direction === "LONG" ? "text-green-400" : "text-red-400"}`}>{pos.direction}</span></td>
                    <td className="num">{pos.quantity}</td>
                    <td className="num">₹{pos.entry_price?.toLocaleString()}</td>
                    <td className="num">₹{pos.current_price?.toLocaleString()}</td>
                    <td className={`num font-semibold ${(pos.unrealized_pnl || 0) >= 0 ? "text-green-400" : "text-red-400"}`}>
                      ₹{pos.unrealized_pnl?.toLocaleString()}
                    </td>
                    <td><button onClick={() => handleClose(pos.id)} className="px-2 py-1 bg-red-600/20 text-red-400 text-xs rounded font-semibold hover:bg-red-600/30">Close</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="card">
          <h3 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wider">Trade History</h3>
          {trades.length > 0 ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Direction</th>
                  <th className="num">Entry</th>
                  <th className="num">Exit</th>
                  <th className="num">Qty</th>
                  <th className="num">P&L</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t: any, i: number) => (
                  <tr key={i}>
                    <td className="font-semibold text-white">{t.symbol}</td>
                    <td><span className={`text-xs font-bold ${t.direction === "LONG" ? "text-green-400" : "text-red-400"}`}>{t.direction}</span></td>
                    <td className="num">₹{t.entry_price?.toLocaleString()}</td>
                    <td className="num">₹{t.exit_price?.toLocaleString()}</td>
                    <td className="num">{t.quantity}</td>
                    <td className={`num font-semibold ${(t.pnl || 0) >= 0 ? "text-green-400" : "text-red-400"}`}>
                      ₹{t.pnl?.toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-gray-500 text-sm text-center py-6">No trades yet</p>
          )}
        </div>
      </main>
    </div>
  );
}
