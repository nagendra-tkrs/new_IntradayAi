"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import Header from "@/components/Header";
import {
  getPortfolio, getPositions, getTradeHistory, getPendingOrders,
  editPendingOrder, fillPendingOrder, cancelPendingOrder,
  closePosition, getStocks, getPerformance,
} from "@/lib/api";
import type { Instrument, PaperPosition, PaperTrade, PendingOrder, Performance, Portfolio } from "@/lib/types";

export default function PaperTradingPage() {
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [positions, setPositions] = useState<PaperPosition[]>([]);
  const [pendingOrders, setPendingOrders] = useState<PendingOrder[]>([]);
  const [trades, setTrades] = useState<PaperTrade[]>([]);
  const [stocks, setStocks] = useState<Instrument[]>([]);
  const [loading, setLoading] = useState(true);
  const [performance, setPerformance] = useState<Performance | null>(null);
  const [editId, setEditId] = useState<string | null>(null);
  const [editFields, setEditFields] = useState<{ entry_price: string; stop_loss: string; target_1: string; quantity: string }>({
    entry_price: "", stop_loss: "", target_1: "", quantity: "",
  });
  const [editMsg, setEditMsg] = useState<{ id: string; type: "success" | "error"; text: string } | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const refreshRef = useRef<number | null>(null);
  const isMountedRef = useRef(true);

  const loadData = useCallback(async () => {
    try {
      const results = await Promise.allSettled([
        getPortfolio(), getPositions(), getTradeHistory(50), getPendingOrders(), getPerformance(),
      ]);
      if (isMountedRef.current) {
        if (results[0].status === "fulfilled") setPortfolio(results[0].value);
        if (results[1].status === "fulfilled") setPositions(results[1].value.positions || []);
        if (results[2].status === "fulfilled") setTrades(results[2].value.trades || []);
        if (results[3].status === "fulfilled") setPendingOrders(results[3].value.orders || []);
        if (results[4].status === "fulfilled") setPerformance(results[4].value);
      }
      try {
        const s = await getStocks();
        if (isMountedRef.current) setStocks(s || []);
      } catch {}
    } catch (e) {
      console.error(e);
    } finally {
      if (isMountedRef.current) setLoading(false);
    }
  }, []);

  const loadDataRef = useRef(loadData);
  loadDataRef.current = loadData;

  useEffect(() => {
    isMountedRef.current = true;
    loadDataRef.current();
    refreshRef.current = window.setInterval(() => loadDataRef.current(), 30000);
    return () => {
      isMountedRef.current = false;
      if (refreshRef.current) clearInterval(refreshRef.current);
    };
  }, []);

  function startEdit(order: PendingOrder) {
    setEditId(order.id);
    setEditFields({
      entry_price: String(order.entry_price || ""),
      stop_loss: String(order.stop_loss || ""),
      target_1: String(order.target_1 || ""),
      quantity: String(order.quantity || 1),
    });
    setEditMsg(null);
  }

  function cancelEdit() {
    setEditId(null);
    setEditMsg(null);
  }

  async function saveEdit(orderId: string) {
    setSaving(orderId);
    setEditMsg(null);
    try {
      const ep = parseFloat(editFields.entry_price);
      const sl = parseFloat(editFields.stop_loss);
      const t1 = parseFloat(editFields.target_1);
      const qty = parseInt(editFields.quantity) || 1;
      if (!ep || !sl || !t1 || qty <= 0) {
        setEditMsg({ id: orderId, type: "error", text: "All fields must be valid positive numbers." });
        setSaving(null);
        return;
      }
      await editPendingOrder(orderId, {
        entry_price: ep,
        stop_loss: sl,
        target_1: t1,
        quantity: qty,
      });
      setEditMsg({ id: orderId, type: "success", text: "Order updated." });
      setEditId(null);
      loadData();
    } catch (e) {
      const msg = e instanceof Error && typeof e.message === "string" ? e.message : "Update failed";
      setEditMsg({ id: orderId, type: "error", text: msg });
    } finally {
      setSaving(null);
    }
  }

  async function handleFill(orderId: string) {
    setSaving(orderId);
    try {
      await fillPendingOrder(orderId);
      loadData();
    } catch (e) {
      setEditMsg({ id: orderId, type: "error", text: e instanceof Error && e.message ? e.message : "Fill failed" });
    } finally {
      setSaving(null);
    }
  }

  async function handleCancel(orderId: string) {
    setSaving(orderId);
    try {
      await cancelPendingOrder(orderId);
      loadData();
    } catch (e) {
      setEditMsg({ id: orderId, type: "error", text: e instanceof Error && e.message ? e.message : "Cancel failed" });
    } finally {
      setSaving(null);
    }
  }

  async function handleClose(posId: string) {
    try {
      await closePosition(posId);
      loadData();
    } catch (e) {
      console.error(e);
    }
  }

  function computeRR(entry: number, sl: number, t1: number): string {
    const risk = Math.abs(entry - sl);
    const reward = Math.abs(t1 - entry);
    if (risk <= 0) return "N/A";
    return (reward / risk).toFixed(2);
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
          <p className="text-sm text-gray-500 mt-1">Virtual portfolio with paper order execution. No real money at risk.</p>
        </div>

        {portfolio && (
          <div className="grid grid-cols-2 sm:grid-cols-6 gap-3 mb-6">
            <div className="card">
              <div className="text-[10px] text-gray-500 uppercase tracking-wider">Total Value</div>
              <div className="text-xl font-bold text-white mt-1">₹{portfolio.total_value?.toLocaleString()}</div>
            </div>
            <div className="card">
              <div className="text-[10px] text-gray-500 uppercase tracking-wider">Available Cash</div>
              <div className="text-xl font-bold text-blue-400 mt-1">₹{portfolio.available_cash?.toLocaleString()}</div>
            </div>
            <div className="card">
              <div className="text-[10px] text-gray-500 uppercase tracking-wider">Reserved Margin</div>
              <div className="text-xl font-bold text-purple-400 mt-1">₹{portfolio.reserved_margin?.toLocaleString()}</div>
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
              <div className="text-[10px] text-gray-500 uppercase tracking-wider">Positions / Pending</div>
              <div className="text-xl font-bold text-white mt-1">{portfolio.positions_count} / {portfolio.pending_orders_count || 0}</div>
            </div>
          </div>
        )}

        {performance && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
            {(["daily", "weekly", "monthly", "all_time"] as const).map((period) => (
              <div key={period} className="card">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">{period === "all_time" ? "All Time" : period.charAt(0).toUpperCase() + period.slice(1)}</div>
                <div className="flex items-center gap-3 mb-1">
                  <span className="text-lg font-bold text-white">{performance[period]?.count || 0}</span>
                  <span className="text-[10px] text-gray-500">trades</span>
                </div>
                <div className="flex items-center gap-2 text-xs">
                  <span className="text-green-400 font-semibold">{performance[period]?.wins || 0}W</span>
                  <span className="text-red-400 font-semibold">{performance[period]?.losses || 0}L</span>
                  <span className="text-blue-400 font-semibold">{performance[period]?.win_rate || 0}%</span>
                </div>
                <div className={`text-sm font-bold mt-1 ${(performance[period]?.total_pnl || 0) >= 0 ? "text-green-400" : "text-red-400"}`}>
                  ₹{performance[period]?.total_pnl?.toLocaleString() || 0}
                </div>
              </div>
            ))}
          </div>
        )}

        {pendingOrders.length > 0 && (
          <div className="card mb-6">
            <h3 className="text-sm font-semibold text-amber-400 mb-3 uppercase tracking-wider">Pending Orders ({pendingOrders.length})</h3>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Direction</th>
                  <th className="num">Qty</th>
                  <th className="num">Entry</th>
                  <th className="num">Stop Loss</th>
                  <th className="num">Target</th>
                  <th className="num">R:R</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {pendingOrders.map((order) => {
                  const isEditing = editId === order.id;
                  const msg = editMsg?.id === order.id ? editMsg : null;
                  const isSaving = saving === order.id;
                  const rr = isEditing
                    ? computeRR(parseFloat(editFields.entry_price) || 0, parseFloat(editFields.stop_loss) || 0, parseFloat(editFields.target_1) || 0)
                    : computeRR(order.entry_price, order.stop_loss, order.target_1);
                  return (
                    <tr key={order.id}>
                      <td className="font-semibold text-white">{order.symbol}</td>
                      <td>
                        <span className={`text-xs font-bold ${order.direction === "LONG" ? "text-green-400" : "text-red-400"}`}>
                          {order.direction}
                        </span>
                      </td>
                      <td className="num">
                        {isEditing ? (
                          <input type="number" value={editFields.quantity} onChange={(e) => setEditFields({ ...editFields, quantity: e.target.value })}
                            className="w-16 px-1 py-0.5 bg-[#111827] border border-[#2d3548] rounded text-white text-xs" />
                        ) : order.quantity}
                      </td>
                      <td className="num">
                        {isEditing ? (
                          <input type="number" step="0.01" value={editFields.entry_price} onChange={(e) => setEditFields({ ...editFields, entry_price: e.target.value })}
                            className="w-24 px-1 py-0.5 bg-[#111827] border border-[#2d3548] rounded text-white text-xs" />
                        ) : `₹${order.entry_price?.toLocaleString()}`}
                      </td>
                      <td className="num">
                        {isEditing ? (
                          <input type="number" step="0.01" value={editFields.stop_loss} onChange={(e) => setEditFields({ ...editFields, stop_loss: e.target.value })}
                            className="w-24 px-1 py-0.5 bg-[#111827] border border-[#2d3548] rounded text-white text-xs" />
                        ) : `₹${order.stop_loss?.toLocaleString()}`}
                      </td>
                      <td className="num">
                        {isEditing ? (
                          <input type="number" step="0.01" value={editFields.target_1} onChange={(e) => setEditFields({ ...editFields, target_1: e.target.value })}
                            className="w-24 px-1 py-0.5 bg-[#111827] border border-[#2d3548] rounded text-white text-xs" />
                        ) : `₹${order.target_1?.toLocaleString()}`}
                      </td>
                      <td className="num font-semibold text-blue-400">{rr}</td>
                      <td>
                        <span className="inline-block px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-500/20 text-amber-400">
                          PENDING
                        </span>
                      </td>
                      <td>
                        <div className="flex items-center gap-1">
                          {isEditing ? (
                            <>
                              <button onClick={() => saveEdit(order.id)} disabled={isSaving}
                                className="px-2 py-1 bg-green-600 text-white text-[10px] rounded font-semibold hover:bg-green-500 disabled:opacity-50">
                                {isSaving ? "..." : "Save"}
                              </button>
                              <button onClick={cancelEdit} disabled={isSaving}
                                className="px-2 py-1 bg-[#1a1f2e] text-gray-400 text-[10px] rounded font-semibold border border-[#2d3548]">
                                Cancel
                              </button>
                            </>
                          ) : (
                            <>
                              <button onClick={() => startEdit(order)} disabled={isSaving}
                                className="px-2 py-1 bg-blue-600/20 text-blue-400 text-[10px] rounded font-semibold hover:bg-blue-600/30">
                                Edit
                              </button>
                              <button onClick={() => handleFill(order.id)} disabled={isSaving}
                                className="px-2 py-1 bg-green-600/20 text-green-400 text-[10px] rounded font-semibold hover:bg-green-600/30 disabled:opacity-50">
                                Fill
                              </button>
                              <button onClick={() => handleCancel(order.id)} disabled={isSaving}
                                className="px-2 py-1 bg-red-600/20 text-red-400 text-[10px] rounded font-semibold hover:bg-red-600/30 disabled:opacity-50">
                                Cancel
                              </button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {editMsg && (
              <div className={`mt-2 px-3 py-2 rounded-lg text-xs ${editMsg.type === "error" ? "bg-red-500/10 border border-red-500/30 text-red-400" : "bg-green-500/10 border border-green-500/30 text-green-400"}`}>
                {editMsg.text}
              </div>
            )}
          </div>
        )}

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
                  <th className="num">SL</th>
                  <th className="num">Target</th>
                  <th className="num">P&L</th>
                  <th className="num">P&L %</th>
                  <th>Status</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos) => {
                  const pnl = pos.unrealized_pnl || 0;
                  const pnlPct = pos.entry_price ? ((pnl / (pos.entry_price * pos.quantity)) * 100) : 0;
                  return (
                    <tr key={pos.id}>
                      <td className="font-semibold text-white">{pos.symbol}</td>
                      <td>
                        <span className={`text-xs font-bold ${pos.direction === "LONG" ? "text-green-400" : "text-red-400"}`}>{pos.direction}</span>
                      </td>
                      <td className="num">{pos.quantity}</td>
                      <td className="num">₹{pos.entry_price?.toLocaleString()}</td>
                      <td className="num font-semibold">₹{pos.current_price?.toLocaleString()}</td>
                      <td className="num text-xs">₹{pos.stop_loss?.toLocaleString()}</td>
                      <td className="num text-xs">₹{pos.target_1?.toLocaleString()}</td>
                      <td className={`num font-bold text-sm ${pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                        {pnl >= 0 ? "+" : ""}₹{pnl.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                      </td>
                      <td className={`num text-xs font-semibold ${pnlPct >= 0 ? "text-green-400" : "text-red-400"}`}>
                        {pnlPct >= 0 ? "+" : ""}{pnlPct.toFixed(2)}%
                      </td>
                      <td>
                        <span className="inline-block px-2 py-0.5 rounded-full text-[10px] font-bold bg-green-500/20 text-green-400">
                          OPEN
                        </span>
                      </td>
                      <td>
                        <button onClick={() => handleClose(pos.id)}
                          className="px-2 py-1 bg-red-600/20 text-red-400 text-xs rounded font-semibold hover:bg-red-600/30">
                          Close
                        </button>
                      </td>
                    </tr>
                  );
                })}
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
                  <th className="num">SL</th>
                  <th className="num">Target</th>
                  <th className="num">Realized P&L</th>
                  <th>Result</th>
                  <th>Exit Time</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t, i) => (
                  <tr key={i}>
                    <td className="font-semibold text-white">{t.symbol}</td>
                    <td>
                      <span className={`text-xs font-bold ${t.direction === "LONG" ? "text-green-400" : "text-red-400"}`}>{t.direction}</span>
                    </td>
                    <td className="num">₹{t.entry_price?.toLocaleString()}</td>
                    <td className="num">₹{t.exit_price?.toLocaleString()}</td>
                    <td className="num">{t.quantity}</td>
                    <td className="num text-xs">₹{t.stop_loss?.toLocaleString()}</td>
                    <td className="num text-xs">₹{t.target_1?.toLocaleString()}</td>
                    <td className={`num font-bold ${(t.pnl || 0) >= 0 ? "text-green-400" : "text-red-400"}`}>
                      {t.pnl >= 0 ? "+" : ""}₹{t.pnl?.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </td>
                    <td>
                      <span className={`inline-block px-2 py-0.5 rounded-full text-[10px] font-bold ${
                        t.result === "WIN" ? "bg-green-500/20 text-green-400" :
                        t.result === "LOSS" ? "bg-red-500/20 text-red-400" :
                        "bg-gray-500/20 text-gray-400"
                      }`}>{t.result || (t.pnl >= 0 ? "WIN" : "LOSS")}</span>
                    </td>
                    <td className="text-[10px] text-gray-500">
                      {t.exit_time ? new Date(t.exit_time).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true, timeZone: "Asia/Kolkata" }) : "-"}
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
