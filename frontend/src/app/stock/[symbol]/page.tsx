"use client";

import { useEffect, useState, use, useCallback, useMemo, useRef } from "react";
import { useRouter } from "next/navigation";
import Header from "@/components/Header";
import MiniChart from "@/components/MiniChart";
import IndicatorPanel from "@/components/IndicatorPanel";
import SignalCard from "@/components/SignalCard";
import { getStockDetail, getStockChart, placePaperOrder, saveTradeSetup, clearTradeSetup } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const DATE_RANGES = [
  { value: 1, label: "1D" },
  { value: 3, label: "3D" },
  { value: 5, label: "5D" },
  { value: 10, label: "10D" },
];

const INTERVALS = [
  { value: "1m", label: "1m" },
  { value: "5m", label: "5m" },
  { value: "10m", label: "10m" },
  { value: "20m", label: "20m" },
  { value: "30m", label: "30m" },
  { value: "1h", label: "1h" },
  { value: "2h", label: "2h" },
  { value: "10h", label: "10h" },
];

const QUICK_RANGES = [
  { value: "auto", label: "Auto" },
  { value: "1h", label: "1H" },
  { value: "2h", label: "2H" },
  { value: "4h", label: "4H" },
  { value: "6h", label: "6H" },
  { value: "12h", label: "12H" },
  { value: "1d", label: "1D" },
  { value: "2d", label: "2D" },
  { value: "3d", label: "3D" },
  { value: "custom", label: "Custom" },
];

const DETAIL_REFRESH_INTERVAL = 60000;

function isFinitePositive(n: number | string | null | undefined): n is number {
  if (n === null || n === undefined) return false;
  const v = typeof n === "string" ? parseFloat(n) : n;
  return typeof v === "number" && Number.isFinite(v) && v > 0;
}

function computeRiskReward(entry: number, stopLoss: number, target: number, isLong: boolean) {
  const risk = Math.abs(entry - stopLoss);
  const reward = Math.abs(target - entry);
  if (!isFinitePositive(risk) || risk <= 0) return null;
  const rr = reward / risk;
  return Number.isFinite(rr) ? Math.round(rr * 100) / 100 : null;
}

function validateUserSetup(
  entry: number | null,
  stopLoss: number | null,
  target: number | null,
  isLong: boolean,
): string[] {
  const errors: string[] = [];
  if (entry == null || !isFinitePositive(entry)) errors.push("Buy price is required.");
  if (stopLoss == null || !isFinitePositive(stopLoss)) errors.push("Stop Loss is required.");
  if (target == null || !isFinitePositive(target)) errors.push("Target is required.");
  if (errors.length > 0) return errors;

  const e = entry as number;
  const sl = stopLoss as number;
  const tg = target as number;
  if (e === sl) {
    errors.push("Entry and Stop Loss cannot be equal.");
    return errors;
  }
  if (isLong) {
    if (sl > e) errors.push("SL must be below Buy Price for a BUY setup.");
    if (tg < e) errors.push("Target must be above Buy Price for a BUY setup.");
  } else {
    if (sl < e) errors.push("SL must be above Entry price for a SELL setup.");
    if (tg > e) errors.push("Target must be below Entry price for a SELL setup.");
  }
  return errors;
}

export default function StockDetailPage({ params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = use(params);
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const [detail, setDetail] = useState<any>(null);
  const [chartData, setChartData] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(1);
  const [interval, setInterval] = useState("5m");
  const [orderQty, setOrderQty] = useState(1);
  const [orderMsg, setOrderMsg] = useState<string | null>(null);
  const [quickRange, setQuickRange] = useState("auto");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const [showCustomRange, setShowCustomRange] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const intervalRef = useRef<number | null>(null);
  const isMountedRef = useRef(true);
  const [userSetup, setUserSetup] = useState<{ entry: number | null; stopLoss: number | null; target: number | null; riskReward: number | null } | null>(null);
  const [editing, setEditing] = useState<{ entry: string; stopLoss: string; target: string } | null>(null);
  const [overrideActive, setOverrideActive] = useState(false);
  const [setupErrors, setSetupErrors] = useState<string[]>([]);
  const [savingSetup, setSavingSetup] = useState(false);
  const [setupMsg, setSetupMsg] = useState<string | null>(null);

  const loadData = useCallback(async (isAutoRefresh = false) => {
    try {
      if (!isAutoRefresh) setLoading(true);
      const [d, c] = await Promise.all([
        getStockDetail(symbol),
        getStockChart(symbol, days, interval),
      ]);
      if (isMountedRef.current) {
        setDetail(d);
        setChartData(c.data || []);
        setLastRefresh(new Date());
      }
    } catch (e) {
      console.error(e);
    } finally {
      if (isMountedRef.current) setLoading(false);
    }
  }, [symbol, days, interval]);

  const loadDataRef = useRef(loadData);
  loadDataRef.current = loadData;

  useEffect(() => {
    if (!authLoading && !user) {
      router.replace("/login");
    }
  }, [user, authLoading, router]);

  useEffect(() => {
    if (authLoading || !user) return;
    isMountedRef.current = true;
    loadDataRef.current();
    const id = window.setInterval(() => { loadDataRef.current(true); }, DETAIL_REFRESH_INTERVAL);
    intervalRef.current = id;
    return () => {
      isMountedRef.current = false;
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [authLoading, user, symbol, days, interval]);

  const aiSetup = useMemo(() => {
    const s = detail?.signal?.setup || {};
    return {
      entry: s.entry != null ? Number(s.entry) : null,
      stopLoss: s.stop_loss != null ? Number(s.stop_loss) : null,
      target: s.target_1 != null ? Number(s.target_1) : null,
      riskReward: s.risk_reward_ratio != null ? Number(s.risk_reward_ratio) : null,
    };
  }, [detail]);

  const signalDir = detail?.signal?.direction || "";
  const isLongSignal = signalDir.toUpperCase().includes("LONG");

  const activeSetup = overrideActive && userSetup ? userSetup : aiSetup;

  useEffect(() => {
    if (!detail) return;
    const ust = detail.user_trade_setup;
    if (ust && ust.override_active && ust.entry != null && ust.stop_loss != null && ust.target != null) {
      const us = {
        entry: Number(ust.entry),
        stopLoss: Number(ust.stop_loss),
        target: Number(ust.target),
        riskReward: ust.risk_reward != null ? Number(ust.risk_reward) : null,
      };
      setUserSetup(us);
      setOverrideActive(true);
      setEditing({ entry: String(us.entry), stopLoss: String(us.stopLoss), target: String(us.target) });
    } else {
      setOverrideActive(false);
      setUserSetup(null);
      const ai = aiSetup;
      setEditing({
        entry: ai.entry != null ? String(ai.entry) : "",
        stopLoss: ai.stopLoss != null ? String(ai.stopLoss) : "",
        target: ai.target != null ? String(ai.target) : "",
      });
      setSetupErrors([]);
    }
  }, [detail, symbol]);

  function handleSetupField(field: "entry" | "stopLoss" | "target", value: string) {
    setEditing((prev) => ({ ...(prev || { entry: "", stopLoss: "", target: "" }), [field]: value }));
  }

  async function applyUserSetup() {
    if (savingSetup) return;
    const s = detail?.signal?.setup || {};
    const base = {
      entry: aiSetup.entry ?? Number(detail?.quote?.price ?? 0),
      stopLoss: aiSetup.stopLoss ?? 0,
      target: aiSetup.target ?? 0,
    };
    const fields = {
      entry: editing?.entry ?? String(base.entry),
      stopLoss: editing?.stopLoss ?? String(base.stopLoss),
      target: editing?.target ?? String(base.target),
    };
    const entryNum = parseFloat(fields.entry);
    const slNum = parseFloat(fields.stopLoss);
    const tgtNum = parseFloat(fields.target);
    const errors = validateUserSetup(
      isFinitePositive(entryNum) ? entryNum : null,
      isFinitePositive(slNum) ? slNum : null,
      isFinitePositive(tgtNum) ? tgtNum : null,
      isLongSignal,
    );
    setSetupErrors(errors);
    if (errors.length > 0) return;

    setSavingSetup(true);
    setSetupMsg(null);
    try {
      const saved = await saveTradeSetup(symbol, {
        entry: entryNum,
        stop_loss: slNum,
        target: tgtNum,
        direction: isLongSignal ? "LONG" : "SHORT",
      });
      const us = {
        entry: Number(saved.entry),
        stopLoss: Number(saved.stop_loss),
        target: Number(saved.target),
        riskReward: saved.risk_reward != null ? Number(saved.risk_reward) : null,
      };
      setUserSetup(us);
      setOverrideActive(true);
      setSetupMsg("Trade setup applied. Chart and paper trade now use your custom values.");
    } catch (e: any) {
      const errs = Array.isArray(e?.message) ? e.message : [e?.message || "Failed to save trade setup."];
      setSetupErrors(errs.filter((x: any) => typeof x === "string"));
    } finally {
      setSavingSetup(false);
    }
  }

  async function resetUserSetup() {
    setSavingSetup(true);
    setSetupMsg(null);
    try {
      await clearTradeSetup(symbol);
      setOverrideActive(false);
      setUserSetup(null);
      setEditing(null);
      setSetupErrors([]);
      setSetupMsg("Reset to AI values. User override removed.");
    } catch (e: any) {
      setSetupErrors([e?.message || "Failed to reset trade setup."]);
    } finally {
      setSavingSetup(false);
    }
  }

  const liveEditingRR = useMemo(() => {
    if (!editing) return null;
    const entry = parseFloat(editing.entry);
    const sl = parseFloat(editing.stopLoss);
    const tgt = parseFloat(editing.target);
    if (!isFinitePositive(entry) || !isFinitePositive(sl) || !isFinitePositive(tgt)) return null;
    return computeRiskReward(entry, sl, tgt, isLongSignal);
  }, [editing, isLongSignal]);

  const filteredChartData = useMemo(() => {
    if (quickRange === "auto" || !chartData.length) return chartData;
    if (quickRange === "custom") {
      if (!customFrom || !customTo) return chartData;
      const from = new Date(customFrom).getTime();
      const to = new Date(customTo).getTime();
      return chartData.filter((d: any) => {
        const t = new Date(d.timestamp).getTime();
        return t >= from && t <= to;
      });
    }
    const now = new Date(chartData[chartData.length - 1]?.timestamp || Date.now());
    let cutoffMs = 0;
    if (quickRange === "1h") cutoffMs = 60 * 60 * 1000;
    else if (quickRange === "2h") cutoffMs = 2 * 60 * 60 * 1000;
    else if (quickRange === "4h") cutoffMs = 4 * 60 * 60 * 1000;
    else if (quickRange === "6h") cutoffMs = 6 * 60 * 60 * 1000;
    else if (quickRange === "12h") cutoffMs = 12 * 60 * 60 * 1000;
    else if (quickRange === "1d") cutoffMs = 24 * 60 * 60 * 1000;
    else if (quickRange === "2d") cutoffMs = 2 * 24 * 60 * 60 * 1000;
    else if (quickRange === "3d") cutoffMs = 3 * 24 * 60 * 60 * 1000;
    const cutoff = new Date(now.getTime() - cutoffMs);
    return chartData.filter((d: any) => new Date(d.timestamp) >= cutoff);
  }, [chartData, quickRange, customFrom, customTo]);

  async function handlePaperOrder(direction: string) {
    try {
      setOrderMsg(null);
      const signal = detail?.signal;
      const setup = signal?.setup || {};
      if (overrideActive && activeSetup.entry != null) {
        await placePaperOrder({
          symbol,
          direction,
          quantity: orderQty,
          entry_price: activeSetup.entry,
          stop_loss: activeSetup.stopLoss ?? undefined,
          target_1: activeSetup.target ?? undefined,
        });
      } else {
        await placePaperOrder({
          symbol,
          direction,
          quantity: orderQty,
          entry_price: setup.entry || detail?.quote?.price,
          stop_loss: setup.stop_loss,
          target_1: setup.target_1,
          target_2: setup.target_2,
        });
      }
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
            <div className="flex flex-wrap gap-x-6 gap-y-1 mt-3 text-sm">
              <span className="text-gray-400">Open <span className="text-white font-semibold">{quote.open != null ? `₹${quote.open.toLocaleString()}` : "N/A"}</span></span>
              <span className="text-gray-400">High <span className="text-white font-semibold">{quote.high != null ? `₹${quote.high.toLocaleString()}` : "N/A"}</span></span>
              <span className="text-gray-400">Low <span className="text-white font-semibold">{quote.low != null ? `₹${quote.low.toLocaleString()}` : "N/A"}</span></span>
              <span className="text-gray-400">Close <span className="text-white font-semibold">{quote.close != null ? `₹${quote.close.toLocaleString()}` : "N/A"}</span></span>
              <span className="text-gray-400">Prev Close <span className="text-white font-semibold">{quote.prev_close != null ? `₹${quote.prev_close.toLocaleString()}` : "N/A"}</span></span>
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

        {setupMsg && (
          <div className="mb-4 px-3 py-2 rounded-lg text-xs bg-green-500/10 border border-green-500/30 text-green-400">{setupMsg}</div>
        )}

        <div className="card mb-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">Trade Setup</h3>
            {overrideActive && (
              <span className="inline-block px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-amber-500/15 border border-amber-500/30 text-amber-400">
                User Override Active
              </span>
            )}
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="bg-[#111827] rounded-lg p-3">
              <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">AI Recommended Setup</p>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
                <span className="text-gray-400">AI Entry</span>
                <span className="text-white font-semibold text-right">{aiSetup.entry != null ? `₹${aiSetup.entry.toLocaleString()} (Recommended)` : "N/A"}</span>
                <span className="text-gray-400">AI Stop Loss</span>
                <span className="text-white font-semibold text-right">{aiSetup.stopLoss != null ? `₹${aiSetup.stopLoss.toLocaleString()}` : "N/A"}</span>
                <span className="text-gray-400">AI Target</span>
                <span className="text-white font-semibold text-right">{aiSetup.target != null ? `₹${aiSetup.target.toLocaleString()}` : "N/A"}</span>
                <span className="text-gray-400">AI R:R</span>
                <span className="text-white font-semibold text-right">{aiSetup.riskReward != null ? aiSetup.riskReward.toFixed(2) : "N/A"}</span>
              </div>
            </div>

            <div className="bg-[#0d1220] rounded-lg p-3">
              <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">
                {overrideActive ? "User Custom Setup" : isLongSignal ? "Planned Buy / Entry" : "Planned Entry"}
                {quote.price != null && <span className="text-gray-600 normal-case font-normal ml-1">(Current Mkt ₹{quote.price.toLocaleString()})</span>}
              </p>
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <label className="text-xs text-gray-400 w-20 shrink-0">{isLongSignal ? "Buy Price" : "Entry"}</label>
                  <div className="relative flex-1">
                    <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-500 text-sm">₹</span>
                    <input
                      type="number"
                      step="0.01"
                      inputMode="decimal"
                      value={editing?.entry ?? (activeSetup.entry != null ? String(activeSetup.entry) : "")}
                      onChange={(e) => handleSetupField("entry", e.target.value)}
                      className="w-full pl-7 pr-2 py-1.5 bg-[#111827] border border-[#2d3548] rounded text-white text-sm"
                      placeholder="235.50"
                    />
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <label className="text-xs text-gray-400 w-20 shrink-0">Stop Loss</label>
                  <div className="relative flex-1">
                    <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-500 text-sm">₹</span>
                    <input
                      type="number"
                      step="0.01"
                      inputMode="decimal"
                      value={editing?.stopLoss ?? (activeSetup.stopLoss != null ? String(activeSetup.stopLoss) : "")}
                      onChange={(e) => handleSetupField("stopLoss", e.target.value)}
                      className="w-full pl-7 pr-2 py-1.5 bg-[#111827] border border-[#2d3548] rounded text-white text-sm"
                      placeholder="234.90"
                    />
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <label className="text-xs text-gray-400 w-20 shrink-0">Target</label>
                  <div className="relative flex-1">
                    <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-500 text-sm">₹</span>
                    <input
                      type="number"
                      step="0.01"
                      inputMode="decimal"
                      value={editing?.target ?? (activeSetup.target != null ? String(activeSetup.target) : "")}
                      onChange={(e) => handleSetupField("target", e.target.value)}
                      className="w-full pl-7 pr-2 py-1.5 bg-[#111827] border border-[#2d3548] rounded text-white text-sm"
                      placeholder="237.00"
                    />
                  </div>
                </div>
                <div className="flex items-center justify-between pt-1">
                  <span className="text-xs text-gray-400">R:R</span>
                  <span className="text-sm font-bold text-blue-400">
                    {liveEditingRR != null
                      ? liveEditingRR.toFixed(2)
                      : (activeSetup.riskReward != null ? activeSetup.riskReward.toFixed(2) : "N/A")}
                  </span>
                </div>
              </div>
            </div>
          </div>

          {setupErrors.length > 0 && (
            <div className="mt-3 space-y-1">
              {setupErrors.map((err, i) => (
                <p key={i} className="text-xs text-red-400">&#9888; {err}</p>
              ))}
            </div>
          )}

          <div className="flex flex-wrap gap-2 mt-4">
            <button
              onClick={applyUserSetup}
              disabled={savingSetup}
              className="px-4 py-2 bg-green-600 hover:bg-green-500 disabled:opacity-50 text-white text-sm font-bold rounded-lg"
            >
              {savingSetup ? "Saving..." : "Apply Trade Setup"}
            </button>
            <button
              onClick={resetUserSetup}
              disabled={savingSetup}
              className="px-4 py-2 bg-[#1a1f2e] hover:bg-[#222839] disabled:opacity-50 text-gray-300 text-sm font-bold rounded-lg border border-[#2d3548]"
            >
              Reset to AI Values
            </button>
          </div>
          {!overrideActive && (
            <p className="text-[10px] text-gray-500 mt-2">
              Editing a planned setup does not execute a trade. Use the Paper Trade buttons above to place an order.
            </p>
          )}
        </div>

        <div className="mb-2 px-3 py-2 bg-blue-500/10 border border-blue-500/30 rounded-lg text-xs text-blue-400 flex items-center justify-between">
          <span>Data sourced via yfinance. Prices may be delayed. This is a paper-trading analysis tool, not financial advice.</span>
          <span className="flex items-center gap-2">
            {quote.timestamp && (
              <span className="text-blue-400/80">
                Mkt: {new Date(quote.timestamp).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true, timeZone: "Asia/Kolkata" })}
              </span>
            )}
            {lastRefresh && <span>Last: {lastRefresh.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true, timeZone: "Asia/Kolkata" })}</span>}
          </span>
        </div>

        <div className="mb-4">
          <div className="flex flex-wrap items-center gap-4 mb-3">
            <div className="flex items-center gap-2">
              <span className="text-xs text-gray-500">Date Range:</span>
              {DATE_RANGES.map((dr) => (
                <button
                  key={dr.value}
                  onClick={() => setDays(dr.value)}
                  className={`px-2.5 py-1 text-xs rounded-lg font-semibold ${
                    days === dr.value ? "bg-blue-600 text-white" : "bg-[#1a1f2e] text-gray-400 hover:bg-[#222839]"
                  }`}
                >
                  {dr.label}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-gray-500">Interval:</span>
              {INTERVALS.map((iv) => (
                <button
                  key={iv.value}
                  onClick={() => setInterval(iv.value)}
                  className={`px-2.5 py-1 text-xs rounded-lg font-semibold ${
                    interval === iv.value ? "bg-blue-600 text-white" : "bg-[#1a1f2e] text-gray-400 hover:bg-[#222839]"
                  }`}
                >
                  {iv.label}
                </button>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <span className="text-xs text-gray-500">Quick Range:</span>
            {QUICK_RANGES.map((qr) => (
              <button
                key={qr.value}
                onClick={() => {
                  setQuickRange(qr.value);
                  setShowCustomRange(qr.value === "custom");
                }}
                className={`px-2 py-1 text-[10px] rounded font-semibold ${
                  quickRange === qr.value ? "bg-blue-600 text-white" : "bg-[#1a1f2e] text-gray-400 hover:bg-[#222839]"
                }`}
              >
                {qr.label}
              </button>
            ))}
          </div>
          {showCustomRange && (
            <div className="flex items-center gap-2 mb-3">
              <span className="text-xs text-gray-500">From:</span>
              <input
                type="datetime-local"
                value={customFrom}
                onChange={(e) => setCustomFrom(e.target.value)}
                className="px-2 py-1 bg-[#111827] border border-[#2d3548] rounded text-white text-xs"
              />
              <span className="text-xs text-gray-500">To:</span>
              <input
                type="datetime-local"
                value={customTo}
                onChange={(e) => setCustomTo(e.target.value)}
                className="px-2 py-1 bg-[#111827] border border-[#2d3548] rounded text-white text-xs"
              />
              <button
                onClick={() => { setQuickRange("auto"); setShowCustomRange(false); }}
                className="px-2 py-1 text-[10px] rounded bg-gray-600 text-white hover:bg-gray-500"
              >
                Reset
              </button>
            </div>
          )}
          <div className="card">
            {filteredChartData.length === 0 ? (
              <div className="flex items-center justify-center text-gray-500" style={{ height: 350 }}>
                No market data available for the selected range.
              </div>
            ) : (
              <MiniChart
                data={filteredChartData}
                height={350}
                entryLine={activeSetup.entry ?? setup.entry}
                stopLine={activeSetup.stopLoss ?? setup.stop_loss}
                targetLine={activeSetup.target ?? setup.target_1}
                showVolume={true}
              />
            )}
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
          <div className="lg:col-span-2">
            <h3 className="text-sm font-semibold text-gray-400 mb-2 uppercase tracking-wider">Technical Indicators</h3>
            <IndicatorPanel indicators={indicators} />
          </div>
          <div>
            {signal && signal.direction !== "NO_TRADE" ? (
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
