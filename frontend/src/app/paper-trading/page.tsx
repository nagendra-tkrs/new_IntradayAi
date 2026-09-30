"use client";

import { Fragment, useEffect, useState, useCallback, useRef, type ReactNode } from "react";
import Header from "@/components/Header";
import {
  getPortfolio, getPositions, getTradeHistory, getPendingOrders,
  editPendingOrder, fillPendingOrder, cancelPendingOrder,
  closePosition, getStocks, getPerformance, getPerformanceComparison,
  getContextComparison,
} from "@/lib/api";
import type { Instrument, PaperPosition, PaperTrade, PendingOrder, Performance, Portfolio, TradeHistoryGroup, TradeHistoryResponse, TradeHistorySummary, LegacyProfitCompareResponse, ContextComparison } from "@/lib/types";
import { formatTradeDate, todayISTDate } from "@/lib/dates";

/** Human label for a recorded exit reason (module scope: shared by the trade
 * table and the AI Recommendation panel). */
function exitReasonLabel(reason?: string | null): string {
  switch (reason) {
    case "T1_PARTIAL": return "T1 Partial";
    case "T2_FINAL": return "Target 2";
    case "TRAILING_STOP": return "Trailing SL";
    case "STOP_LOSS": return "Stop Loss";
    case "TARGET_1": return "Target 1";
    case "MANUAL_CLOSE": return "Manual";
    default: return reason || "—";
  }
}

export default function PaperTradingPage() {
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [positions, setPositions] = useState<PaperPosition[]>([]);
  const [pendingOrders, setPendingOrders] = useState<PendingOrder[]>([]);
  const [tradeGroups, setTradeGroups] = useState<TradeHistoryGroup[]>([]);
  const [tradeSummary, setTradeSummary] = useState<TradeHistorySummary | null>(null);
  const [dateFilter, setDateFilter] = useState<"all" | "today" | "specific">("all");
  const [selectedDate, setSelectedDate] = useState<string>("");
  const [expandedDates, setExpandedDates] = useState<Set<string>>(new Set());
  const [expandedAi, setExpandedAi] = useState<Set<string>>(new Set());
  const [stocks, setStocks] = useState<Instrument[]>([]);
  const [loading, setLoading] = useState(true);
  const [performance, setPerformance] = useState<Performance | null>(null);
  const [comparison, setComparison] = useState<LegacyProfitCompareResponse | null>(null);
  const [contextComparison, setContextComparison] = useState<ContextComparison | null>(null);
  const [editId, setEditId] = useState<string | null>(null);
  const [editFields, setEditFields] = useState<{ entry_price: string; stop_loss: string; target_1: string; quantity: string }>({
    entry_price: "", stop_loss: "", target_1: "", quantity: "",
  });
  const [editMsg, setEditMsg] = useState<{ id: string; type: "success" | "error"; text: string } | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const refreshRef = useRef<number | null>(null);
  const isMountedRef = useRef(true);

  /** The IST date (if any) the Trade History view is filtered to. */
  const historyDateParam = useCallback((): string | undefined => {
    if (dateFilter === "all") return undefined;
    if (dateFilter === "today") return todayISTDate() || undefined;
    return selectedDate || undefined;
  }, [dateFilter, selectedDate]);

  const loadData = useCallback(async () => {
    try {
      const results = await Promise.allSettled([
        getPortfolio(), getPositions(), getTradeHistory(50, historyDateParam()), getPendingOrders(), getPerformance(), getPerformanceComparison(),
        getContextComparison(),
      ]);
      if (isMountedRef.current) {
        if (results[0].status === "fulfilled") setPortfolio(results[0].value);
        if (results[1].status === "fulfilled") setPositions(results[1].value.positions || []);
        if (results[2].status === "fulfilled") {
          const hist: TradeHistoryResponse = results[2].value;
          setTradeGroups(hist.date_groups || []);
          setTradeSummary(hist.summary || null);
          setExpandedDates((prev) => {
            const groups = hist.date_groups || [];
            if (groups.length === 0) return new Set();
            const keys = new Set(groups.map((g) => g.date));
            const changed = keys.size !== prev.size || [...keys].some((d) => !prev.has(d));
            if (!changed) return prev;
            // Default: latest date expanded, older dates collapsed.
            return new Set([groups[0].date]);
          });
        }
        if (results[3].status === "fulfilled") setPendingOrders(results[3].value.orders || []);
        if (results[4].status === "fulfilled") setPerformance(results[4].value);
        if (results[5].status === "fulfilled") setComparison(results[5].value);
        if (results[6].status === "fulfilled") setContextComparison(results[6].value);
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
  }, [historyDateParam]);

  const loadDataRef = useRef(loadData);
  loadDataRef.current = loadData;

  // Initial load + 30s auto-refresh. Depending on `historyDateParam` also
  // re-runs the effect (via the always-latest ref) and re-arms the interval
  // whenever the trade-history date filter changes, so the view reflects the
  // selected IST date.
  useEffect(() => {
    isMountedRef.current = true;
    loadDataRef.current();
    refreshRef.current = window.setInterval(() => loadDataRef.current(), 30000);
    return () => {
      isMountedRef.current = false;
      if (refreshRef.current) clearInterval(refreshRef.current);
    };
  }, [historyDateParam]);

  function startEdit(order: PendingOrder) {
    setEditId(order.id);
    setEditFields({
      entry_price: String(order.entry_price || ""),
      stop_loss: String(order.stop_loss || ""),
      target_1: String(order.target_1 || ""),
      // Pre-filled with the order's ACTUAL size so the box shows reality. The
      // old `|| 1` would have displayed 1 for a genuinely 0-quantity order; the
      // `?? ""` keeps an unknown quantity visibly unknown instead of inventing
      // a number, and an edit must supply a real one.
      quantity: order.quantity != null ? String(order.quantity) : "",
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
      // No `|| 1` fallback: clearing the field must not become a 1-share order.
      // A quantity is either a real positive integer or the edit is refused.
      const qty = parseInt(editFields.quantity);
      const qtyValid = Number.isInteger(qty) && qty > 0;
      if (!ep || !sl || !t1 || !qtyValid) {
        setEditMsg({
          id: orderId,
          type: "error",
          text: "All fields must be valid positive numbers, and quantity a whole number of shares.",
        });
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

  /** Human label for a position's profit-capture state (frontend badge). */
  function stageLabel(pos: PaperPosition): { text: string; color: string } {
    if (pos.exit_stage === "T1_EXECUTED") {
      if (pos.trailing_active) {
        return { text: "T1 PARTIAL · TRAILING ACTIVE", color: "bg-teal-500/20 text-teal-400" };
      }
      return { text: "T1 PARTIAL · T2 PENDING", color: "bg-amber-500/20 text-amber-400" };
    }
    return { text: "OPEN · T1 PENDING", color: "bg-blue-500/20 text-blue-400" };
  }

  function toggleDate(d: string) {
    setExpandedDates((prev) => {
      const next = new Set(prev);
      if (next.has(d)) next.delete(d);
      else next.add(d);
      return next;
    });
  }

  function toggleAi(id: string) {
    setExpandedAi((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function formatTime(value?: string | null): string {
    if (!value) return "-";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return "-";
    try {
      return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true, timeZone: "Asia/Kolkata" });
    } catch {
      return "-";
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

        {comparison && (
          <div className="card mb-6">
            <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
              <h3 className="text-sm font-semibold text-white uppercase tracking-wider">Legacy vs Profit Capture</h3>
              <span className="text-[10px] text-gray-500">{comparison.data_source === "paper_trading" ? "Based on recorded paper-trading data" : comparison.data_source}</span>
            </div>

            <div className="grid sm:grid-cols-2 gap-2 mb-4 text-xs">
              <div className="rounded border border-[#2d3548] p-3">
                <div className="text-gray-400 mb-1">Legacy Exit Model</div>
                <div className="text-gray-200">{comparison.models?.legacy ?? "Entry → T1 → 100% Exit"}</div>
              </div>
              <div className="rounded border border-[#2d3548] p-3">
                <div className="text-gray-400 mb-1">Profit Capture Model</div>
                <div className="text-gray-200">{comparison.models?.profit_capture ?? "Entry → T1 Partial → Protection → T2 / Trailing"}</div>
              </div>
            </div>

            {!comparison.has_data || !comparison.profit_capture?.all?.total_trades ? (
              <div className="text-sm text-gray-400 py-4">
                Insufficient data for performance comparison.
              </div>
            ) : (
              <ComparisonTable comparison={comparison} />
            )}
          </div>
        )}

        {contextComparison && <ContextComparisonCard report={contextComparison} />}

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
            <div className="overflow-x-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Direction</th>
                    <th className="num">Original Qty</th>
                    <th className="num">Remaining</th>
                    <th className="num">Entry</th>
                    <th className="num">Current</th>
                    <th className="num">SL</th>
                    <th className="num">T1</th>
                    <th className="num">T2</th>
                    <th className="num">Realized</th>
                    <th className="num">Unrealized</th>
                    <th className="num">Total P&L</th>
                    <th>Status</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((pos) => {
                    const realized = pos.realized_pnl || 0;
                    const unrealized = pos.unrealized_pnl || 0;
                    const totalPnl = realized + unrealized;
                    const stage = stageLabel(pos);
                    const originalQty = pos.initial_quantity ?? pos.quantity;
                    const hasPartial = (pos.exit_stage === "T1_EXECUTED");
                    return (
                      <tr key={pos.id}>
                        <td className="font-semibold text-white">{pos.symbol}</td>
                        <td>
                          <span className={`text-xs font-bold ${pos.direction === "LONG" ? "text-green-400" : "text-red-400"}`}>{pos.direction}</span>
                        </td>
                        <td className="num text-xs text-gray-400">{originalQty}</td>
                        <td className="num font-semibold">{pos.quantity}</td>
                        <td className="num">₹{pos.entry_price?.toLocaleString()}</td>
                        <td className="num font-semibold">₹{pos.current_price?.toLocaleString()}</td>
                        <td className="num text-xs">
                          ₹{pos.stop_loss?.toLocaleString()}
                          {hasPartial && pos.trailing_active && (
                            <span className="ml-1 text-[9px] font-bold text-teal-400">▲TRAIL</span>
                          )}
                          {hasPartial && !pos.trailing_active && (
                            <span className="ml-1 text-[9px] font-bold text-amber-400">▲PRTCT</span>
                          )}
                        </td>
                        <td className="num text-xs">
                          {hasPartial && pos.t1_exit_price ? (
                            <>
                              ₹{pos.t1_exit_price?.toLocaleString()}
                              <span className="block text-[9px] text-gray-500">qty {pos.t1_exit_quantity}</span>
                            </>
                          ) : (
                            <>₹{pos.target_1?.toLocaleString()}</>
                          )}
                        </td>
                        <td className="num text-xs">₹{pos.target_2?.toLocaleString() || "-"}</td>
                        <td className={`num text-xs font-semibold ${realized > 0 ? "text-green-400" : realized < 0 ? "text-red-400" : "text-gray-400"}`}>
                          {hasPartial ? (realized > 0 ? "+" : "") + realized.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "-"}
                        </td>
                        <td className={`num text-xs font-semibold ${unrealized > 0 ? "text-green-400" : unrealized < 0 ? "text-red-400" : "text-gray-400"}`}>
                          {unrealized > 0 ? "+" : ""}{unrealized.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        </td>
                        <td className={`num font-bold text-sm ${totalPnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                          {totalPnl >= 0 ? "+" : ""}₹{totalPnl.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        </td>
                        <td>
                          <span className={`inline-block px-2 py-0.5 rounded-full text-[10px] font-bold ${stage.color}`}>
                            {stage.text}
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
          </div>
        )}

        <div className="card">
          <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
            <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">Trade History</h3>
            <div className="flex flex-wrap items-center gap-2">
              {(["all", "today", "specific"] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setDateFilter(f)}
                  className={`px-3 py-1.5 rounded-full text-[11px] font-bold uppercase tracking-wider border transition-colors ${
                    dateFilter === f
                      ? "bg-blue-600/30 text-blue-400 border-blue-500/40"
                      : "bg-[#1a1f2e] text-gray-400 border-[#2d3548] hover:text-gray-200"
                  }`}
                >
                  {f === "all" ? "All Dates" : f === "today" ? "Today" : "Specific Date"}
                </button>
              ))}
              {dateFilter === "specific" && (
                <input
                  type="date"
                  value={selectedDate}
                  onChange={(e) => setSelectedDate(e.target.value)}
                  aria-label="Select trade date"
                  className="bg-[#111827] border border-[#2d3548] rounded px-2 py-1 text-xs text-white"
                />
              )}
            </div>
          </div>

          {tradeSummary && tradeSummary.total_trades > 0 && (
            <div className="grid grid-cols-2 sm:grid-cols-6 gap-3 mb-4">
              <div className="card">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider">Total Trades</div>
                <div className="text-xl font-bold text-white mt-1">{tradeSummary.total_trades}</div>
              </div>
              <div className="card">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider">Winning</div>
                <div className="text-xl font-bold text-green-400 mt-1">{tradeSummary.profitable_trade_count}</div>
              </div>
              <div className="card">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider">Losing</div>
                <div className="text-xl font-bold text-red-400 mt-1">{tradeSummary.losing_trade_count}</div>
              </div>
              <div className="card">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider">Total Profit</div>
                <div className="text-xl font-bold text-green-400 mt-1">₹{tradeSummary.total_profit?.toLocaleString()}</div>
              </div>
              <div className="card">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider">Total Loss</div>
                <div className="text-xl font-bold text-red-400 mt-1">₹{tradeSummary.total_loss?.toLocaleString()}</div>
              </div>
              <div className="card">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider">Net P&L</div>
                <div className={`text-xl font-bold mt-1 ${tradeSummary.net_pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                  {tradeSummary.net_pnl >= 0 ? "+" : ""}₹{tradeSummary.net_pnl?.toLocaleString()}
                </div>
              </div>
            </div>
          )}

          {tradeGroups.length > 0 ? (
            <div className="space-y-3">
              {tradeGroups.map((group) => {
                const isOpen = expandedDates.has(group.date);
                return (
                  <div key={group.date} className="border border-[#2d3548] rounded-lg overflow-hidden">
                    <button
                      onClick={() => toggleDate(group.date)}
                      aria-expanded={isOpen}
                      className="w-full flex flex-wrap items-center justify-between gap-x-3 gap-y-1 px-4 py-3 bg-[#111827] hover:bg-[#151b2b] transition-colors text-left"
                    >
                      <div className="flex items-center gap-3">
                        <span className={`text-xs text-gray-500 transition-transform ${isOpen ? "" : "rotate-180"}`}>▾</span>
                        <span className="text-sm font-semibold text-white">{formatTradeDate(group.date)}</span>
                      </div>
                      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
                        <span className="text-gray-400">{group.trade_count} {group.trade_count === 1 ? "trade" : "trades"}</span>
                        <span className="text-green-400 font-semibold">{group.profitable_trade_count}W</span>
                        <span className="text-red-400 font-semibold">{group.losing_trade_count}L</span>
                        <span className="text-green-400">+₹{group.total_profit?.toLocaleString()}</span>
                        <span className="text-red-400">-₹{group.total_loss?.toLocaleString()}</span>
                        <span className={`font-bold ${group.net_pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                          {group.net_pnl >= 0 ? "+" : ""}₹{group.net_pnl?.toLocaleString()}
                        </span>
                      </div>
                    </button>
                    {isOpen && (
                      <div className="overflow-x-auto">
                        <table className="data-table">
                          <thead>
                            <tr>
                              <th>Symbol</th>
                              <th>Direction</th>
                              <th className="num">Entry</th>
                              <th className="num">Exit</th>
                              <th className="num">Qty</th>
                              <th>Entry Time</th>
                              <th>Exit Time</th>
                              <th className="num">Realized P&L</th>
                              <th>Exit Reason</th>
                              <th>Result</th>
                              <th>AI</th>
                            </tr>
                          </thead>
                          <tbody>
                            {group.trades.map((t) => (
                              <Fragment key={t.id}>
                                <tr>
                                  <td className="font-semibold text-white">{t.symbol}</td>
                                  <td>
                                    <span className={`text-xs font-bold ${t.direction === "LONG" ? "text-green-400" : "text-red-400"}`}>{t.direction}</span>
                                  </td>
                                  <td className="num">₹{t.entry_price?.toLocaleString()}</td>
                                  <td className="num">₹{t.exit_price?.toLocaleString()}</td>
                                  <td className="num">{t.exit_quantity ?? t.quantity}</td>
                                  <td className="text-[10px] text-gray-500 whitespace-nowrap">{formatTime(t.entry_time)}</td>
                                  <td className="text-[10px] text-gray-500 whitespace-nowrap">{formatTime(t.exit_time)}</td>
                                  <td className={`num font-bold ${(t.pnl || 0) >= 0 ? "text-green-400" : "text-red-400"}`}>
                                    {t.pnl >= 0 ? "+" : ""}₹{t.pnl?.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                                  </td>
                                  <td className="text-[10px] text-gray-400 whitespace-nowrap">{exitReasonLabel(t.exit_reason)}</td>
                                  <td>
                                    <span className={`inline-block px-2 py-0.5 rounded-full text-[10px] font-bold ${
                                      t.result === "WIN" ? "bg-green-500/20 text-green-400" :
                                      t.result === "LOSS" ? "bg-red-500/20 text-red-400" :
                                      "bg-gray-500/20 text-gray-400"
                                    }`}>{t.result || (t.pnl >= 0 ? "WIN" : "LOSS")}</span>
                                  </td>
                                  <td className="text-center">
                                    {t.signal_id ? (
                                      <button
                                        onClick={() => toggleAi(t.id)}
                                        className={`px-2 py-1 rounded text-[10px] font-bold border transition-colors ${
                                          expandedAi.has(t.id)
                                            ? "bg-indigo-500/20 border-indigo-500/40 text-indigo-300"
                                            : "bg-indigo-500/10 border-indigo-500/30 text-indigo-400 hover:bg-indigo-500/20"
                                        }`}
                                      >
                                        {expandedAi.has(t.id) ? "Hide AI" : "AI"}
                                      </button>
                                    ) : (
                                      <span className="text-[10px] text-gray-600">—</span>
                                    )}
                                  </td>
                                </tr>
                                {expandedAi.has(t.id) && (
                                  <tr>
                                    <td colSpan={11} className="bg-[#0d1220] border-t border-gray-800">
                                      <AiTradePanel trade={t} />
                                    </td>
                                  </tr>
                                )}
                              </Fragment>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-gray-500 text-sm text-center py-6">
              {dateFilter === "all"
                ? "No completed paper trades yet."
                : "No completed paper trades for this date."}
            </p>
          )}
        </div>
      </main>
    </div>
  );
}

/** Read-only AI Recommendation detail panel for one closed paper trade.

 * ``AVAILABLE`` trades render the ORIGINAL persisted recommendation (direction,
 * score, quality, component breakdown, original setup) next to the ACTUAL
 * execution (fill, exit, realized P&L). Manual orders and pre-fix historical
 * rows (no signal_id, or a missing signal record) clearly show "Not available"
 * — nothing is ever fabricated from current market data.
 */
function AiTradePanel({ trade }: { trade: PaperTrade }) {
  const fmt = (n: number | null | undefined, digits = 2): string =>
    n === null || n === undefined ? "—" : n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

  const denom = (n: number | null | undefined): string =>
    n === null || n === undefined ? "—" : `₹${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  if (!trade.signal_id) {
    return (
      <div className="py-3 px-4">
        <div className="text-xs font-bold text-gray-300 mb-1">AI Recommendation</div>
        <div className="text-xs text-gray-500">
          AI Decision: <span className="text-gray-400 font-semibold">Not available</span> — manual order, or the signal was not persisted when this trade was created.
        </div>
      </div>
    );
  }
  if (!trade.ai_available) {
    return (
      <div className="py-3 px-4">
        <div className="text-xs font-bold text-gray-300 mb-1">AI Recommendation</div>
        <div className="text-xs text-gray-500">
          AI Decision: <span className="text-gray-400 font-semibold">Not available</span> — the linked signal record could not be found.
        </div>
      </div>
    );
  }

  const breakdown: { label: string; value: number | null | undefined }[] = [
    { label: "Trend", value: trade.ai_trend_score },
    { label: "Momentum", value: trade.ai_momentum_score },
    { label: "Volume", value: trade.ai_volume_score },
    { label: "VWAP", value: trade.ai_vwap_score },
    { label: "Price Action", value: trade.ai_price_action_score },
    { label: "Market Context", value: trade.ai_market_context_score },
    { label: "Risk Quality", value: trade.ai_risk_quality_score },
  ];
  const setup: { label: string; value: string }[] = [
    { label: "Entry", value: denom(trade.original_entry) },
    { label: "SL", value: denom(trade.original_stop_loss) },
    { label: "T1", value: denom(trade.original_target_1) },
    { label: "T2", value: denom(trade.original_target_2) },
    { label: "R:R", value: fmt(trade.original_risk_reward) },
    { label: "Qty", value: trade.original_quantity === null || trade.original_quantity === undefined ? "—" : String(trade.original_quantity) },
    { label: "Risk Amt", value: denom(trade.original_risk_amount) },
    { label: "Risk %", value: fmt(trade.original_risk_percent, 3) },
  ];
  const actual: { label: string; value: string }[] = [
    { label: "Actual Entry", value: denom(trade.actual_entry ?? trade.entry_price) },
    { label: "Actual Exit", value: denom(trade.actual_exit ?? trade.exit_price) },
    { label: "Realized P&L", value: `${(trade.realized_pnl ?? trade.pnl) >= 0 ? "+" : ""}${denom(trade.realized_pnl ?? trade.pnl)}` },
    { label: "Exit Reason", value: trade.exit_reason ? exitReasonLabel(trade.exit_reason) : (trade.result || "—") },
    { label: "Signal ID", value: trade.signal_id || "—" },
  ];

  return (
    <div className="py-4 px-5">
      <div className="flex items-center justify-between flex-wrap gap-2 mb-3">
        <div className="text-xs font-bold text-gray-300">AI Recommendation</div>
        <div className="flex items-center gap-2 text-[10px] text-gray-500">
          <span className="px-2 py-0.5 rounded-full bg-indigo-500/15 border border-indigo-500/30 text-indigo-300 font-bold">{trade.ai_direction || trade.direction}</span>
          <span className="text-gray-400">Score {fmt(trade.ai_score)}</span>
          {trade.signal_quality && <span className="text-gray-400">Quality: <span className="text-amber-300 font-semibold">{trade.signal_quality}</span></span>}
          {trade.ai_confidence !== null && trade.ai_confidence !== undefined && <span className="text-gray-400">Confidence {fmt(trade.ai_confidence * 100, 0)}%</span>}
          {(trade.ai_strategy_version || trade.ai_strategy) && <span className="text-gray-600">{trade.ai_strategy_version || trade.ai_strategy}</span>}
        </div>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Score Breakdown</div>
          <div className="grid grid-cols-1 gap-1">
            {breakdown.map((b) => (
              <div key={b.label} className="flex justify-between text-xs">
                <span className="text-gray-500">{b.label}</span>
                <span className="text-gray-300 font-semibold">{fmt(b.value)}</span>
              </div>
            ))}
          </div>
          {trade.ai_evidence?.net !== null && trade.ai_evidence?.net !== undefined && (
            <div className="flex justify-between text-xs mt-1 pt-1 border-t border-gray-800">
              <span className="text-gray-500">Net Evidence</span>
              <span className={`font-semibold ${(trade.ai_evidence.net || 0) >= 0 ? "text-green-400" : "text-red-400"}`}>
                {trade.ai_evidence.net! >= 0 ? "+" : ""}{fmt(trade.ai_evidence.net)}
              </span>
            </div>
          )}
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Original Setup</div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1">
            {setup.map((s) => (
              <div key={s.label} className="flex justify-between text-xs">
                <span className="text-gray-500">{s.label}</span>
                <span className="text-gray-300 font-semibold">{s.value}</span>
              </div>
            ))}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Actual Result</div>
          <div className="grid grid-cols-1 gap-1">
            {actual.map((a) => (
              <div key={a.label} className="flex justify-between text-xs">
                <span className="text-gray-500">{a.label}</span>
                <span className={a.label === "Realized P&L" ? `font-bold ${(trade.realized_pnl ?? trade.pnl) >= 0 ? "text-green-400" : "text-red-400"}` : "text-gray-300 font-semibold"}>{a.value}</span>
              </div>
            ))}
            {trade.t1_exit_price !== null && trade.t1_exit_price !== undefined && (
              <div className="flex justify-between text-xs">
                <span className="text-gray-500">T1 Partial</span>
                <span className="text-gray-300 font-semibold">{denom(trade.t1_exit_price)} × {trade.t1_exit_quantity ?? "—"} ({fmt(trade.t1_realized_pnl)} P&L)</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/** Read-only "Legacy vs Profit Capture" metrics table (``all`` split). */
function ComparisonTable({ comparison }: { comparison: LegacyProfitCompareResponse }) {
  const l = comparison.legacy?.all;
  const a = comparison.profit_capture?.all;
  const d = comparison.difference?.all;
  const em = comparison.exit_metrics;
  if (!l || !a || !d) return null;

  const fmt = (n: number | null | undefined, digits = 2): string =>
    n === null || n === undefined ? "—" : n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

  const diffClass = (v: number | null | undefined): string =>
    v === null || v === undefined || v === 0 ? "text-gray-400" : v > 0 ? "text-green-400" : "text-red-400";

  const signed = (v: number): string => (v > 0 ? "+" : "");

  const rows: { label: string; legacy: string; pc: string; diff?: ReactNode }[] = [
    { label: "Total Trades", legacy: fmt(l.total_trades, 0), pc: fmt(a.total_trades, 0) },
    {
      label: "Win Rate", legacy: `${fmt(l.win_rate, 1)}%`, pc: `${fmt(a.win_rate, 1)}%`,
      diff: <span className={diffClass(d.win_rate)}>{signed(d.win_rate)}{fmt(d.win_rate, 1)}pp</span>,
    },
    {
      label: "Total P&L", legacy: `₹${fmt(l.total_pnl)}`, pc: `₹${fmt(a.total_pnl)}`,
      diff: <span className={diffClass(d.pnl)}>{signed(d.pnl)}₹{fmt(d.pnl)}</span>,
    },
    {
      label: "Average P&L", legacy: `₹${fmt(l.average_pnl)}`, pc: `₹${fmt(a.average_pnl)}`,
      diff: <span className={diffClass(d.average_pnl)}>{signed(d.average_pnl)}₹{fmt(d.average_pnl)}</span>,
    },
    {
      label: "Profit Factor", legacy: fmt(l.profit_factor), pc: fmt(a.profit_factor),
      diff: <span className={diffClass(d.profit_factor)}>{d.profit_factor === null ? "—" : signed(d.profit_factor) + fmt(d.profit_factor)}</span>,
    },
    {
      label: "Max Drawdown", legacy: `₹${fmt(l.max_drawdown)}`, pc: `₹${fmt(a.max_drawdown)}`,
      diff: <span className={diffClass(d.max_drawdown)}>{signed(d.max_drawdown)}₹{fmt(d.max_drawdown)}</span>,
    },
    { label: "T1 Hits", legacy: "—", pc: fmt(em?.t1_hit_count, 0) },
    { label: "T2 Hits", legacy: "—", pc: fmt(em?.t2_final_count, 0) },
    { label: "Trailing Exits", legacy: "—", pc: fmt(em?.trailing_stop_count, 0) },
  ];

  return (
    <div className="overflow-x-auto">
      <table className="data-table">
        <thead>
          <tr>
            <th>Metric</th>
            <th className="num">Legacy</th>
            <th className="num">Profit Capture</th>
            <th className="num">Difference</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label}>
              <td className="text-gray-300">{r.label}</td>
              <td className="num text-gray-200">{r.legacy}</td>
              <td className="num text-white font-semibold">{r.pc}</td>
              <td className="num">{r.diff ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Read-only Current (Mode A) vs 24H-context shadow (Mode B) summary card. */
function ContextComparisonCard({ report }: { report: ContextComparison }) {
  const fmt = (n: number | null | undefined, digits = 2): string =>
    n === null || n === undefined ? "—" : n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const pct = (n: number | null | undefined): string =>
    n === null || n === undefined ? "—" : `${fmt(n * 100, 1)}%`;
  const pnlCls = (v: number): string => (v >= 0 ? "text-green-400" : "text-red-400");

  type SigStats = ContextComparison["current"]["signal_stats"];
  type Realized = ContextComparison["current"]["realized_stats"];

  const renderSignalBox = (label: string, s: SigStats, isCtx24: boolean) => (
    <div className="rounded border border-[#2d3548] p-3 text-xs">
      <div className={`mb-1 ${isCtx24 ? "text-fuchsia-300" : "text-sky-300"}`}>{label}</div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-gray-200">
        <span className="whitespace-nowrap">Signals: <b>{s.total}</b></span>
        <span>LONG <b>{s.long}</b></span>
        <span>SHORT <b>{s.short}</b></span>
        <span>No-trade <b>{s.no_trade}</b></span>
        <span>Avg score <b>{fmt(s.avg_score, 1)}</b></span>
      </div>
      {Object.keys(s.quality_distribution).length > 0 && (
        <div className="mt-1 text-[10px] text-gray-500">
          Quality: {Object.entries(s.quality_distribution).map(([q, c]) => `${q} ${c}`).join(" · ")}
        </div>
      )}
    </div>
  );

  const renderRealized = (r: Realized, pending: boolean, note?: string) => (
    <div className="rounded border border-[#2d3548] p-3 text-xs">
      <div className="text-gray-400 mb-1">Realized paper P&L (recorded ledger only)</div>
      {pending ? (
        <div className="text-amber-400/90">{note ?? "Pending — the 24H-context shadow never placed paper orders."}</div>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-1 text-gray-200">
          <span>Trades <b>{r.total_trades}</b></span>
          <span>Win rate <b>{pct(r.win_rate)}</b></span>
          <span className={pnlCls(r.realized_pnl)}>P&L <b>₹{fmt(r.realized_pnl)}</b></span>
          <span>Avg P&L <b>₹{fmt(r.avg_pnl)}</b></span>
          <span>Profit factor <b>{fmt(r.profit_factor)}</b></span>
          <span>Max DD <b>₹{fmt(r.max_drawdown)}</b></span>
          <span>Avg hold <b>{r.avg_holding_seconds === null || r.avg_holding_seconds === undefined ? "—" : `${fmt(r.avg_holding_seconds / 60, 1)}m`}</b></span>
          <span>LONG <b>{r.long.total_trades} · ₹{fmt(r.long.realized_pnl)}</b></span>
          <span>SHORT <b>{r.short.total_trades} · ₹{fmt(r.short.realized_pnl)}</b></span>
        </div>
      )}
      {!pending && Object.keys(r.exit_breakdown).length > 0 && (
        <div className="mt-1 text-[10px] text-gray-500">
          Exits: {Object.entries(r.exit_breakdown).map(([k, v]) => `${k} ${v}`).join(" · ")}
        </div>
      )}
    </div>
  );

  return (
    <div className="card mb-6">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <h3 className="text-sm font-semibold text-white uppercase tracking-wider">Current vs 24H-Context (Shadow)</h3>
        <span className="text-[10px] text-gray-500">
          {report.use_24h_context ? `24H-context active · ${fmt(report.window_hours, 0)}h window` : "24H-context OFF (shadow only)"}
        </span>
      </div>

      {!report.has_data ? (
        <div className="text-sm text-gray-400 py-4">
          No recorded recommendations yet — run the scanner during a session with data to populate the comparison.
        </div>
      ) : (
        <>
          <div className="grid sm:grid-cols-2 gap-2 mb-3">
            {renderSignalBox("Current engine (Mode A)", report.current.signal_stats, false)}
            {renderSignalBox("24H-context shadow (Mode B)", report.ctx24.signal_stats, true)}
          </div>

          <div className="grid sm:grid-cols-2 gap-2 mb-3">
            {renderRealized(report.current.realized_stats, report.current.realized_pending)}
            {renderRealized(report.ctx24.realized_stats ?? report.current.realized_stats, report.ctx24.realized_pending, report.ctx24.pending_note)}
          </div>

          <div className="flex flex-wrap items-center gap-x-6 gap-y-1 rounded border border-[#2d3548] p-3 text-xs mb-3">
            <div className="text-gray-400">Direction agreement (same candle)</div>
            <div className="text-gray-200">
              <b className="text-white">{report.agreement.agree_count}</b> / {report.agreement.total}
              <span className="ml-2 text-blue-400">{pct(report.agreement.agreement_rate)}</span>
            </div>
            <div className="text-gray-500">— same recommendation on both engines</div>
          </div>

          {report.caveats.length > 0 && (
            <ul className="text-[11px] text-gray-500 space-y-1 list-disc list-inside">
              {report.caveats.map((c, i) => <li key={i}>{c}</li>)}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
