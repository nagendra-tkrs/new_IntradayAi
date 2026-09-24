"use client";

import { useEffect, useState, useRef, useCallback, useMemo } from "react";
import { useRouter } from "next/navigation";
import Header from "@/components/Header";
import {
  runScanner,
  getAllUserSetups,
  saveTradeSetup,
  clearTradeSetup,
  placePaperOrder,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { ScannerResponse, ScannerResult, SignalSetup, TradeSetup } from "@/lib/types";

type FilterType = "all" | "long" | "short" | "strong" | "high_volume" | "no_trade";

type SortKey = "symbol" | "price" | "change_pct" | "volume" | "relative_volume" | "rsi" | "adx" | "distance_from_vwap" | "confidence";

const SCANNER_REFRESH_INTERVAL = 120000;

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

function validateSetup(
  entry: number | null,
  stopLoss: number | null,
  target: number | null,
  isLong: boolean,
): string[] {
  const errors: string[] = [];
  if (entry == null || !isFinitePositive(entry)) errors.push("Entry is required.");
  if (stopLoss == null || !isFinitePositive(stopLoss)) errors.push("Stop Loss is required.");
  if (target == null || !isFinitePositive(target)) errors.push("Target is required.");
  if (errors.length > 0) return errors;
  const e = entry as number;
  const sl = stopLoss as number;
  const tg = target as number;
  if (e === sl) { errors.push("Entry and SL cannot be equal."); return errors; }
  if (isLong) {
    if (sl > e) errors.push("SL must be below Entry for BUY.");
    if (tg < e) errors.push("Target must be above Entry for BUY.");
  } else {
    if (sl < e) errors.push("SL must be above Entry for SELL.");
    if (tg > e) errors.push("Target must be below Entry for SELL.");
  }
  return errors;
}

export default function ScannerPage() {
  const [scanner, setScanner] = useState<ScannerResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<FilterType>("all");
  const [sortBy, setSortBy] = useState<SortKey>("confidence");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const intervalRef = useRef<number | null>(null);
  const isMountedRef = useRef(true);

  const [expandedSymbol, setExpandedSymbol] = useState<string | null>(null);
  const [userSetups, setUserSetups] = useState<Record<string, TradeSetup>>({});
  const [editingFields, setEditingFields] = useState<Record<string, { entry: string; stopLoss: string; target: string }>>({});
  const [orderQty, setOrderQty] = useState<Record<string, number>>({});
  const [orderMsg, setOrderMsg] = useState<Record<string, { type: "success" | "error"; text: string }>>({});
  const [saving, setSaving] = useState<Record<string, boolean>>({});

  const loadUserSetups = useCallback(async () => {
    try {
      const data = await getAllUserSetups();
      if (isMountedRef.current && data?.setups) {
        setUserSetups((prev) => ({ ...prev, ...data.setups }));
      }
    } catch (e) {
      console.error("Failed to load user setups:", e);
    }
  }, []);

  const loadScanner = useCallback(async (isAutoRefresh = false) => {
    try {
      if (!isAutoRefresh) setLoading(true);
      const data = await runScanner();
      if (isMountedRef.current) {
        setScanner(data);
        setLastRefresh(new Date());
      }
    } catch (e) {
      console.error(e);
    } finally {
      if (isMountedRef.current) setLoading(false);
    }
  }, []);

  const loadScannerRef = useRef(loadScanner);
  loadScannerRef.current = loadScanner;

  const loadUserSetupsRef = useRef(loadUserSetups);
  loadUserSetupsRef.current = loadUserSetups;

  useEffect(() => {
    if (!authLoading && !user) {
      router.replace("/login");
    }
  }, [user, authLoading, router]);

  useEffect(() => {
    if (authLoading || !user) return;
    isMountedRef.current = true;
    loadUserSetupsRef.current();
    loadScannerRef.current();
    const id = window.setInterval(() => {
      loadScannerRef.current(true);
    }, SCANNER_REFRESH_INTERVAL);
    intervalRef.current = id;
    return () => {
      isMountedRef.current = false;
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [authLoading, user]);

  function getSignalDir(stock: ScannerResult): string {
    const sig = stock.signal as string | { direction?: string } | null | undefined;
    if (typeof sig === "string") return sig;
    if (sig?.direction) return sig.direction;
    return "NO_TRADE";
  }

  function isLong(stock: ScannerResult): boolean {
    return getSignalDir(stock).toUpperCase().includes("LONG");
  }

  function getAiSetup(stock: ScannerResult) {
    const setup: Partial<SignalSetup> = stock.signal_data?.setup || {};
    return {
      entry: setup.entry != null ? Number(setup.entry) : null,
      stopLoss: setup.stop_loss != null ? Number(setup.stop_loss) : null,
      target: setup.target_1 != null ? Number(setup.target_1) : null,
      rr: setup.risk_reward_ratio != null ? Number(setup.risk_reward_ratio) : null,
    };
  }

  function getActiveSetup(stock: ScannerResult) {
    const sym = stock.symbol;
    const userSetup = userSetups[sym];
    const aiSetup = getAiSetup(stock);
    if (userSetup?.override_active && userSetup.entry != null && userSetup.stop_loss != null && userSetup.target != null) {
      return {
        entry: Number(userSetup.entry),
        stopLoss: Number(userSetup.stop_loss),
        target: Number(userSetup.target),
        rr: userSetup.risk_reward != null ? Number(userSetup.risk_reward) : null,
        isCustom: true,
      };
    }
    return { ...aiSetup, isCustom: false };
  }

  function getEditingFields(stock: ScannerResult) {
    const sym = stock.symbol;
    if (editingFields[sym]) return editingFields[sym];
    const active = getActiveSetup(stock);
    return {
      entry: active.entry != null ? String(active.entry) : "",
      stopLoss: active.stopLoss != null ? String(active.stopLoss) : "",
      target: active.target != null ? String(active.target) : "",
    };
  }

  function handleFieldChange(stock: ScannerResult, field: "entry" | "stopLoss" | "target", value: string) {
    const sym = stock.symbol;
    setEditingFields((prev) => {
      if (prev[sym]) {
        return { ...prev, [sym]: { ...prev[sym], [field]: value } };
      }
      const active = getActiveSetup(stock);
      return {
        ...prev,
        [sym]: {
          entry: active.entry != null ? String(active.entry) : "",
          stopLoss: active.stopLoss != null ? String(active.stopLoss) : "",
          target: active.target != null ? String(active.target) : "",
          [field]: value,
        },
      };
    });
    setOrderMsg((prev) => { const n = { ...prev }; delete n[sym]; return n; });
  }

  function computeLiveRR(stock: ScannerResult, fields: { entry: string; stopLoss: string; target: string }) {
    const e = parseFloat(fields.entry);
    const sl = parseFloat(fields.stopLoss);
    const tgt = parseFloat(fields.target);
    if (!isFinitePositive(e) || !isFinitePositive(sl) || !isFinitePositive(tgt)) return null;
    return computeRiskReward(e, sl, tgt, isLong(stock));
  }

  async function applySetup(stock: ScannerResult) {
    const sym = stock.symbol;
    const fields = getEditingFields(stock);
    const entryNum = parseFloat(fields.entry);
    const slNum = parseFloat(fields.stopLoss);
    const tgtNum = parseFloat(fields.target);
    const long = isLong(stock);
    const errors = validateSetup(
      isFinitePositive(entryNum) ? entryNum : null,
      isFinitePositive(slNum) ? slNum : null,
      isFinitePositive(tgtNum) ? tgtNum : null,
      long,
    );
    if (errors.length > 0) {
      setOrderMsg((prev) => ({ ...prev, [sym]: { type: "error", text: errors[0] } }));
      return;
    }
    setSaving((prev) => ({ ...prev, [sym]: true }));
    try {
      const saved = await saveTradeSetup(sym, {
        entry: entryNum,
        stop_loss: slNum,
        target: tgtNum,
        direction: long ? "LONG" : "SHORT",
      });
      setUserSetups((prev) => ({ ...prev, [sym]: saved }));
      setOrderMsg((prev) => ({ ...prev, [sym]: { type: "success", text: "Setup saved." } }));
    } catch (e) {
      setOrderMsg((prev) => ({ ...prev, [sym]: { type: "error", text: e instanceof Error && e.message ? e.message : "Failed to save." } }));
    } finally {
      setSaving((prev) => ({ ...prev, [sym]: false }));
    }
  }

  async function resetSetup(stock: ScannerResult) {
    const sym = stock.symbol;
    setSaving((prev) => ({ ...prev, [sym]: true }));
    try {
      await clearTradeSetup(sym);
      const updated = { ...userSetups[sym], override_active: false, entry: null, stop_loss: null, target: null, risk_reward: null };
      setUserSetups((prev) => ({ ...prev, [sym]: updated }));
      const ai = getAiSetup(stock);
      setEditingFields((prev) => ({
        ...prev,
        [sym]: {
          entry: ai.entry != null ? String(ai.entry) : "",
          stopLoss: ai.stopLoss != null ? String(ai.stopLoss) : "",
          target: ai.target != null ? String(ai.target) : "",
        },
      }));
      setOrderMsg((prev) => ({ ...prev, [sym]: { type: "success", text: "Reset to AI values." } }));
    } catch (e) {
      setOrderMsg((prev) => ({ ...prev, [sym]: { type: "error", text: e instanceof Error && e.message ? e.message : "Failed to reset." } }));
    } finally {
      setSaving((prev) => ({ ...prev, [sym]: false }));
    }
  }

  async function handleOrder(stock: ScannerResult, direction: "LONG" | "SHORT") {
    const sym = stock.symbol;
    const fields = getEditingFields(stock);
    const entryNum = parseFloat(fields.entry);
    const slNum = parseFloat(fields.stopLoss);
    const tgtNum = parseFloat(fields.target);
    const qty = orderQty[sym] || 1;

    const long = direction === "LONG";
    const errors = validateSetup(
      isFinitePositive(entryNum) ? entryNum : null,
      isFinitePositive(slNum) ? slNum : null,
      isFinitePositive(tgtNum) ? tgtNum : null,
      long,
    );
    if (errors.length > 0) {
      setOrderMsg((prev) => ({ ...prev, [sym]: { type: "error", text: errors[0] } }));
      return;
    }

    setSaving((prev) => ({ ...prev, [sym]: true }));
    try {
      await placePaperOrder({
        symbol: sym,
        direction,
        quantity: qty,
        entry_price: entryNum,
        stop_loss: slNum,
        target_1: tgtNum,
      });
      setOrderMsg((prev) => ({
        ...prev,
        [sym]: { type: "success", text: `${direction} order placed: ${qty} qty @ ₹${entryNum}` },
      }));
    } catch (e) {
      setOrderMsg((prev) => ({ ...prev, [sym]: { type: "error", text: e instanceof Error && e.message ? e.message : "Order failed." } }));
    } finally {
      setSaving((prev) => ({ ...prev, [sym]: false }));
    }
  }

  function getFilteredResults() {
    if (!scanner?.results) return [];
    let results = [...scanner.results];
    switch (filter) {
      case "long": results = results.filter((r) => getSignalDir(r).includes("LONG")); break;
      case "short": results = results.filter((r) => getSignalDir(r).includes("SHORT")); break;
      case "strong": results = results.filter((r) => r.confidence >= 75); break;
      case "high_volume": results = results.filter((r) => r.relative_volume >= 1.5); break;
      case "no_trade": results = results.filter((r) => getSignalDir(r) === "NO_TRADE"); break;
    }
    results.sort((a, b) => {
      const aVal = a[sortBy] || 0;
      const bVal = b[sortBy] || 0;
      return sortDir === "desc" ? Number(bVal) - Number(aVal) : Number(aVal) - Number(bVal);
    });
    return results;
  }

  function toggleSort(col: SortKey) {
    if (sortBy === col) setSortDir(sortDir === "desc" ? "asc" : "desc");
    else { setSortBy(col); setSortDir("desc"); }
  }

  const filters: { key: FilterType; label: string }[] = [
    { key: "all", label: "All" },
    { key: "strong", label: "Strong (75+)" },
    { key: "long", label: "Long" },
    { key: "short", label: "Short" },
    { key: "high_volume", label: "High Volume" },
    { key: "no_trade", label: "No Trade" },
  ];

  return (
    <div className="min-h-screen bg-[#0a0e17]">
      <Header />
      <main className="max-w-[1440px] mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-bold text-white">Market Scanner</h1>
            <p className="text-sm text-gray-500">Real-time scan of NIFTY50 universe with technical indicators</p>
            {lastRefresh && (
              <p className="text-[10px] text-gray-600 mt-1">
                Last refreshed: {lastRefresh.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true, timeZone: "Asia/Kolkata" })}
              </p>
            )}
          </div>
          <button
            onClick={() => loadScanner()}
            disabled={loading}
            className="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm font-semibold rounded-lg disabled:opacity-50 transition-colors"
          >
            {loading ? "Scanning..." : "Refresh Scan"}
          </button>
        </div>

        <div className="flex items-center gap-2 mb-4 flex-wrap">
          {filters.map((f) => (
            <button
              key={f.key}
              onClick={() => setFilter(f.key)}
              className={`px-3 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
                filter === f.key ? "bg-blue-600 text-white" : "bg-[#1a1f2e] text-gray-400 hover:bg-[#222839]"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-20">
            <div className="w-10 h-10 border-4 border-blue-500/30 border-t-blue-500 rounded-full animate-spin" />
          </div>
        ) : (
          <div className="card overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th className="cursor-pointer" onClick={() => toggleSort("symbol")}>Symbol</th>
                  <th>Sector</th>
                  <th className="num cursor-pointer" onClick={() => toggleSort("price")}>Price</th>
                  <th className="num cursor-pointer" onClick={() => toggleSort("change_pct")}>Chg %</th>
                  <th className="num cursor-pointer" onClick={() => toggleSort("volume")}>Volume</th>
                  <th className="num cursor-pointer" onClick={() => toggleSort("relative_volume")}>Rel Vol</th>
                  <th className="num cursor-pointer" onClick={() => toggleSort("rsi")}>RSI</th>
                  <th className="num cursor-pointer" onClick={() => toggleSort("adx")}>ADX</th>
                  <th>Trend</th>
                  <th className="num cursor-pointer" onClick={() => toggleSort("distance_from_vwap")}>VWAP Dist</th>
                  <th>Signal</th>
                  <th className="num cursor-pointer" onClick={() => toggleSort("confidence")}>Conf</th>
                  <th>Setup</th>
                </tr>
              </thead>
              <tbody>
                {getFilteredResults().map((stock) => {
                  const sym = stock.symbol;
                  const sigDir = getSignalDir(stock);
                  const active = getActiveSetup(stock);
                  const fields = getEditingFields(stock);
                  const liveRR = computeLiveRR(stock, fields);
                  const isExpanded = expandedSymbol === sym;
                  const msg = orderMsg[sym];
                  const isSaving = saving[sym];
                  const canTrade = sigDir !== "NO_TRADE" && sigDir !== "ERROR";
                  const qty = orderQty[sym] || 1;

                  return (
                    <tr key={sym} className="cursor-pointer hover:bg-[#151b2b]" onClick={() => {
                      if (isExpanded) return;
                      setExpandedSymbol(sym);
                      setOrderMsg((prev) => { const n = { ...prev }; delete n[sym]; return n; });
                    }}>
                      <td>
                        <div className="font-semibold text-white">{sym}</div>
                        <div className="text-[10px] text-gray-500">{stock.name}</div>
                      </td>
                      <td className="text-xs text-gray-400">{stock.sector}</td>
                      <td className="num font-semibold">₹{stock.price?.toLocaleString()}</td>
                      <td className={`num font-semibold ${stock.change_pct >= 0 ? "text-green-400" : "text-red-400"}`}>
                        {stock.change_pct >= 0 ? "+" : ""}{stock.change_pct?.toFixed(2)}%
                      </td>
                      <td className="num text-xs">{stock.volume?.toLocaleString()}</td>
                      <td className={`num text-xs font-semibold ${stock.relative_volume >= 1.5 ? "text-blue-400" : ""}`}>
                        {stock.relative_volume?.toFixed(1) || "-"}x
                      </td>
                      <td className={`num text-xs font-semibold ${
                        stock.rsi > 70 ? "text-red-400" : stock.rsi < 30 ? "text-green-400" : "text-gray-300"
                      }`}>{stock.rsi?.toFixed(1) || "-"}</td>
                      <td className="num text-xs">{stock.adx?.toFixed(1) || "-"}</td>
                      <td className={`num text-xs font-semibold ${
                        stock.trend === "BULLISH" ? "text-green-400" :
                        stock.trend === "BEARISH" ? "text-red-400" :
                        stock.trend?.includes("BULL") ? "text-green-300" :
                        stock.trend?.includes("BEAR") ? "text-red-300" : "text-gray-400"
                      }`}>{stock.trend || "-"}</td>
                      <td className={`num text-xs ${
                        (stock.distance_from_vwap || 0) >= 0 ? "text-green-400" : "text-red-400"
                      }`}>{stock.distance_from_vwap?.toFixed(2) || "-"}%</td>
                      <td>
                        <span className={`inline-block px-2 py-0.5 rounded-full text-[10px] font-bold ${
                          sigDir.includes("LONG") ? "badge-long" :
                          sigDir.includes("SHORT") ? "badge-short" : "badge-no-trade"
                        }`}>{sigDir || "N/A"}</span>
                      </td>
                      <td className={`num font-bold text-sm ${
                        stock.confidence >= 80 ? "text-green-400" :
                        stock.confidence >= 60 ? "text-blue-400" :
                        stock.confidence > 0 ? "text-yellow-400" : "text-gray-500"
                      }`}>{stock.confidence || 0}</td>
                      <td className="text-[10px] text-gray-400">
                        {active.isCustom ? (
                          <span className="text-amber-400 font-bold">Custom</span>
                        ) : active.entry != null ? (
                          <span>E:{active.entry} SL:{active.stopLoss} T:{active.target}</span>
                        ) : (
                          <span className="text-gray-600">N/A</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {expandedSymbol && (() => {
          const stock = scanner?.results?.find((r) => r.symbol === expandedSymbol);
          if (!stock) return null;
          const sym = stock.symbol;
          const sigDir = getSignalDir(stock);
          const active = getActiveSetup(stock);
          const aiSetup = getAiSetup(stock);
          const fields = getEditingFields(stock);
          const liveRR = computeLiveRR(stock, fields);
          const msg = orderMsg[sym];
          const isSaving = saving[sym];
          const canTrade = sigDir !== "NO_TRADE" && sigDir !== "ERROR";
          const long = isLong(stock);
          const qty = orderQty[sym] || 1;

          return (
            <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4" onClick={() => setExpandedSymbol(null)}>
              <div className="bg-[#0f1520] border border-[#2d3548] rounded-xl w-full max-w-lg max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
                <div className="flex items-center justify-between px-5 py-4 border-b border-[#2d3548]">
                  <div>
                    <div className="flex items-center gap-3">
                      <h2 className="text-xl font-bold text-white">{sym}</h2>
                      <span className={`inline-block px-2 py-0.5 rounded-full text-[10px] font-bold ${
                        sigDir.includes("LONG") ? "badge-long" :
                        sigDir.includes("SHORT") ? "badge-short" : "badge-no-trade"
                      }`}>{sigDir}</span>
                      {active.isCustom && (
                        <span className="inline-block px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-500/15 border border-amber-500/30 text-amber-400">
                          User Override
                        </span>
                      )}
                    </div>
                    <p className="text-sm text-gray-400 mt-1">₹{stock.price?.toLocaleString()} • Conf {stock.confidence}%</p>
                  </div>
                  <button onClick={() => setExpandedSymbol(null)} className="text-gray-500 hover:text-white text-xl leading-none p-1">&times;</button>
                </div>

                <div className="px-5 py-4 space-y-4">
                  <div className="bg-[#111827] rounded-lg p-3">
                    <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">AI Recommended</p>
                    <div className="grid grid-cols-4 gap-2 text-xs">
                      <div className="text-center"><span className="text-gray-500 block">Entry</span><span className="text-white font-semibold">{aiSetup.entry != null ? `₹${aiSetup.entry}` : "N/A"}</span></div>
                      <div className="text-center"><span className="text-gray-500 block">Stop Loss</span><span className="text-white font-semibold">{aiSetup.stopLoss != null ? `₹${aiSetup.stopLoss}` : "N/A"}</span></div>
                      <div className="text-center"><span className="text-gray-500 block">Target</span><span className="text-white font-semibold">{aiSetup.target != null ? `₹${aiSetup.target}` : "N/A"}</span></div>
                      <div className="text-center"><span className="text-gray-500 block">R:R</span><span className="text-white font-semibold">{aiSetup.rr != null ? aiSetup.rr.toFixed(2) : "N/A"}</span></div>
                    </div>
                  </div>

                  <div className="bg-[#0d1220] rounded-lg p-3">
                    <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">
                      {active.isCustom ? "Your Custom Setup" : "Active Setup"}
                    </p>
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        <label className="text-xs text-gray-400 w-16 shrink-0">Entry</label>
                        <div className="relative flex-1">
                          <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-500 text-sm">₹</span>
                          <input
                            type="number"
                            step="0.01"
                            inputMode="decimal"
                            value={fields.entry}
                            onChange={(e) => handleFieldChange(stock, "entry", e.target.value)}
                            className="w-full pl-7 pr-2 py-1.5 bg-[#111827] border border-[#2d3548] rounded text-white text-sm"
                          />
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        <label className="text-xs text-gray-400 w-16 shrink-0">Stop Loss</label>
                        <div className="relative flex-1">
                          <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-500 text-sm">₹</span>
                          <input
                            type="number"
                            step="0.01"
                            inputMode="decimal"
                            value={fields.stopLoss}
                            onChange={(e) => handleFieldChange(stock, "stopLoss", e.target.value)}
                            className="w-full pl-7 pr-2 py-1.5 bg-[#111827] border border-[#2d3548] rounded text-white text-sm"
                          />
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        <label className="text-xs text-gray-400 w-16 shrink-0">Target</label>
                        <div className="relative flex-1">
                          <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-500 text-sm">₹</span>
                          <input
                            type="number"
                            step="0.01"
                            inputMode="decimal"
                            value={fields.target}
                            onChange={(e) => handleFieldChange(stock, "target", e.target.value)}
                            className="w-full pl-7 pr-2 py-1.5 bg-[#111827] border border-[#2d3548] rounded text-white text-sm"
                          />
                        </div>
                      </div>
                      <div className="flex items-center justify-between pt-1">
                        <span className="text-xs text-gray-400">R:R</span>
                        <span className="text-sm font-bold text-blue-400">
                          {liveRR != null ? liveRR.toFixed(2) : (active.rr != null ? active.rr.toFixed(2) : "N/A")}
                        </span>
                      </div>
                    </div>
                  </div>

                  {msg && (
                    <div className={`px-3 py-2 rounded-lg text-xs ${
                      msg.type === "error"
                        ? "bg-red-500/10 border border-red-500/30 text-red-400"
                        : "bg-green-500/10 border border-green-500/30 text-green-400"
                    }`}>{msg.text}</div>
                  )}

                  <div className="flex flex-wrap gap-2">
                    <button
                      onClick={() => applySetup(stock)}
                      disabled={isSaving}
                      className="px-3 py-1.5 bg-green-600 hover:bg-green-500 disabled:opacity-50 text-white text-xs font-bold rounded-lg"
                    >
                      {isSaving ? "Saving..." : "Save Setup"}
                    </button>
                    <button
                      onClick={() => resetSetup(stock)}
                      disabled={isSaving}
                      className="px-3 py-1.5 bg-[#1a1f2e] hover:bg-[#222839] disabled:opacity-50 text-gray-300 text-xs font-bold rounded-lg border border-[#2d3548]"
                    >
                      Reset to AI
                    </button>
                  </div>

                  {canTrade && (
                    <div className="bg-[#111827] rounded-lg p-3">
                      <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">Paper Order</p>
                      <div className="flex items-center gap-2 mb-3">
                        <label className="text-xs text-gray-400">Qty:</label>
                        <input
                          type="number"
                          value={qty}
                          onChange={(e) => setOrderQty((prev) => ({ ...prev, [sym]: parseInt(e.target.value) || 1 }))}
                          className="w-20 px-2 py-1 bg-[#111827] border border-[#2d3548] rounded text-white text-sm"
                        />
                        <span className="text-[10px] text-gray-600 ml-1">@ ₹{fields.entry || "N/A"}</span>
                      </div>
                      <div className="flex gap-2">
                        <button
                          onClick={() => handleOrder(stock, "LONG")}
                          disabled={isSaving || !fields.entry}
                          className="flex-1 px-3 py-2 bg-green-600 hover:bg-green-500 disabled:opacity-50 text-white text-sm font-bold rounded-lg"
                        >
                          BUY
                        </button>
                        <button
                          onClick={() => handleOrder(stock, "SHORT")}
                          disabled={isSaving || !fields.entry}
                          className="flex-1 px-3 py-2 bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white text-sm font-bold rounded-lg"
                        >
                          SELL
                        </button>
                      </div>
                    </div>
                  )}

                  <button
                    onClick={() => router.push(`/stock/${sym}`)}
                    className="w-full px-3 py-2 bg-[#1a1f2e] hover:bg-[#222839] text-gray-400 text-xs font-semibold rounded-lg border border-[#2d3548] transition-colors"
                  >
                    View Full Detail →
                  </button>
                </div>
              </div>
            </div>
          );
        })()}
      </main>
    </div>
  );
}
