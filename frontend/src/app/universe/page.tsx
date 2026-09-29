"use client";

import { useEffect, useState, useRef, useCallback, useMemo } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import Header from "@/components/Header";
import { getStocks } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Instrument } from "@/lib/types";

type ClassFilter = "all" | "CORE" | "ROTATION";

type SortKey = "rank" | "symbol" | "name" | "sector" | "classification" | "rotation_score";

const UNIVERSE_REFRESH_INTERVAL = 300000; // 5 min — membership is versioned, not tick-by-tick
const NUMERIC_KEYS: ReadonlySet<SortKey> = new Set(["rank", "rotation_score"]);

export default function UniversePage() {
  const [stocks, setStocks] = useState<Instrument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [filter, setFilter] = useState<ClassFilter>("all");
  const [query, setQuery] = useState("");
  const [sortBy, setSortBy] = useState<SortKey>("rank");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const intervalRef = useRef<number | null>(null);
  const isMountedRef = useRef(true);

  useEffect(() => {
    if (!authLoading && !user) {
      router.replace("/login");
    }
  }, [user, authLoading, router]);

  const loadUniverse = useCallback(async () => {
    try {
      const data = await getStocks("ACTIVE");
      if (isMountedRef.current) {
        setStocks(data);
        setLastRefresh(new Date());
        setError(null);
      }
    } catch (e) {
      if (isMountedRef.current) {
        setError(e instanceof Error && e.message ? e.message : "Failed to load the active universe.");
      }
    } finally {
      if (isMountedRef.current) setLoading(false);
    }
  }, []);

  const refresh = useCallback(() => {
    setError(null);
    setLoading(true);
    loadUniverse();
  }, [loadUniverse]);

  const loadUniverseRef = useRef(loadUniverse);

  useEffect(() => {
    if (authLoading || !user) return;
    isMountedRef.current = true;
    loadUniverseRef.current();
    const id = window.setInterval(() => loadUniverseRef.current(), UNIVERSE_REFRESH_INTERVAL);
    intervalRef.current = id;
    return () => {
      isMountedRef.current = false;
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [authLoading, user]);

  const totals = useMemo(() => {
    const core = stocks.filter((s) => s.classification === "CORE").length;
    const rotation = stocks.filter((s) => s.classification === "ROTATION").length;
    return { total: stocks.length, core, rotation };
  }, [stocks]);

  const visible = useMemo(() => {
    let rows = [...stocks];
    if (filter !== "all") rows = rows.filter((s) => s.classification === filter);
    const q = query.trim().toLowerCase();
    if (q) {
      rows = rows.filter((s) =>
        s.symbol.toLowerCase().includes(q) ||
        s.name.toLowerCase().includes(q) ||
        s.sector.toLowerCase().includes(q),
      );
    }
    rows.sort((a, b) => {
      const av = NUMERIC_KEYS.has(sortBy) ? (a[sortBy] ?? -Infinity) as number : String(a[sortBy] ?? "");
      const bv = NUMERIC_KEYS.has(sortBy) ? (b[sortBy] ?? -Infinity) as number : String(b[sortBy] ?? "");
      const cmp = NUMERIC_KEYS.has(sortBy)
        ? (av as number) - (bv as number)
        : String(av).localeCompare(String(bv), undefined, { numeric: true });
      return sortDir === "desc" ? -cmp : cmp;
    });
    return rows;
  }, [stocks, filter, query, sortBy, sortDir]);

  function toggleSort(col: SortKey) {
    if (sortBy === col) setSortDir(sortDir === "asc" ? "desc" : "asc");
    else { setSortBy(col); setSortDir(col === "rank" || col === "symbol" ? "asc" : "desc"); }
  }

  const filters: { key: ClassFilter; label: string }[] = [
    { key: "all", label: `All (${totals.total})` },
    { key: "CORE", label: `Core (${totals.core})` },
    { key: "ROTATION", label: `Rotation (${totals.rotation})` },
  ];

  return (
    <div className="min-h-screen bg-[#0a0e17]">
      <Header />
      <main className="max-w-[1440px] mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-bold text-white">Live Trading Universe</h1>
            <p className="text-sm text-gray-500">
              Active universe served by the live scanner backend
            </p>
            {lastRefresh && (
              <p className="text-[10px] text-gray-600 mt-1">
                Last refreshed: {lastRefresh.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true, timeZone: "Asia/Kolkata" })}
              </p>
            )}
          </div>
          <div className="flex items-center gap-3">
            <Link
              href="/scanner"
              className="px-4 py-2 bg-[#1a1f2e] hover:bg-[#222839] border border-[#2d3548] text-white text-sm font-semibold rounded-lg transition-colors"
            >
              View Scanner
            </Link>
            <button
              onClick={refresh}
              disabled={loading}
              className="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm font-semibold rounded-lg disabled:opacity-50 transition-colors"
            >
              {loading ? "Loading..." : "Refresh"}
            </button>
          </div>
        </div>

        {error && (
          <div className="card bg-red-500/10 border-red-500/30 text-sm mb-6">
            <div className="text-red-400 font-semibold">Failed to load the active universe</div>
            <p className="text-gray-400 text-xs mt-1">
              Backend fails closed when the active universe is unavailable — the scanner will never silently fall
              back to another universe. Reason: {error}
            </p>
            <button
              onClick={refresh}
              className="mt-3 px-3 py-1.5 bg-red-500/20 hover:bg-red-500/30 text-red-400 text-xs font-semibold rounded-lg transition-colors"
            >
              Retry
            </button>
          </div>
        )}

        {loading && !error && (
          <div className="flex items-center justify-center py-20">
            <div className="text-center">
              <div className="w-10 h-10 border-4 border-blue-500/30 border-t-blue-500 rounded-full animate-spin mx-auto mb-4" />
              <p className="text-gray-400 text-sm">Loading the active universe...</p>
            </div>
          </div>
        )}

        {!loading && !error && (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
              <div className="card">
                <div className="text-xs text-gray-500 uppercase tracking-wider">Total Stocks</div>
                <div className="text-xl font-bold text-white mt-1">{totals.total}</div>
              </div>
              <div className="card">
                <div className="text-xs text-gray-500 uppercase tracking-wider">Core</div>
                <div className="text-xl font-bold text-blue-400 mt-1">{totals.core}</div>
              </div>
              <div className="card">
                <div className="text-xs text-gray-500 uppercase tracking-wider">Rotation</div>
                <div className="text-xl font-bold text-purple-400 mt-1">{totals.rotation}</div>
              </div>
              <div className="card">
                <div className="text-xs text-gray-500 uppercase tracking-wider">Universe Key</div>
                <div className="text-xl font-bold text-green-400 mt-1">{stocks[0]?.universe || "—"}</div>
              </div>
            </div>

            <div className="flex items-center gap-2 mb-3 flex-wrap">
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
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search symbol, name or sector..."
                className="ml-auto w-full sm:w-64 px-3 py-1.5 text-xs rounded-lg bg-[#1a1f2e] border border-[#2d3548] text-gray-200 placeholder-gray-500 focus:outline-none focus:border-blue-500"
              />
            </div>

            <div className="card overflow-x-auto">
              <p className="text-[10px] text-gray-600 mb-2">
                Showing {visible.length} of {totals.total} stocks · Backend source of truth:{" "}
                <code className="text-gray-400">get_active_trading_universe()</code> /{" "}
                <code className="text-gray-400">/api/stocks?universe=ACTIVE</code>
              </p>
              <table className="data-table">
                <thead>
                  <tr>
                    <th className="num cursor-pointer" onClick={() => toggleSort("rank")}>Rank</th>
                    <th className="cursor-pointer" onClick={() => toggleSort("symbol")}>Symbol</th>
                    <th className="cursor-pointer" onClick={() => toggleSort("name")}>Name</th>
                    <th className="cursor-pointer" onClick={() => toggleSort("sector")}>Sector</th>
                    <th className="cursor-pointer" onClick={() => toggleSort("classification")}>Class</th>
                    <th className="num cursor-pointer" onClick={() => toggleSort("rotation_score")}>Rotation Score</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((stock) => {
                    const isCore = stock.classification === "CORE";
                    return (
                      <tr
                        key={stock.symbol}
                        className="cursor-pointer hover:bg-[#151b2b]"
                        onClick={() => router.push(`/stock/${stock.symbol}`)}
                      >
                        <td className="num text-xs text-gray-400">{stock.rank ?? "-"}</td>
                        <td>
                          <div className="font-semibold text-white">{stock.symbol}</div>
                        </td>
                        <td className="text-xs text-gray-400">{stock.name}</td>
                        <td className="text-xs text-gray-400">{stock.sector}</td>
                        <td>
                          <span className={`inline-block px-2 py-0.5 rounded-full text-[10px] font-bold ${isCore ? "badge-core" : "badge-rotation"}`}>
                            {isCore ? "CORE" : "ROTATION"}
                          </span>
                        </td>
                        <td className={`num text-xs font-semibold ${isCore ? "text-gray-300" : "text-purple-400"}`}>
                          {stock.rotation_score != null && Number.isFinite(Number(stock.rotation_score))
                            ? Number(stock.rotation_score).toFixed(2)
                            : "-"}
                        </td>
                      </tr>
                    );
                  })}
                  {visible.length === 0 && (
                    <tr>
                      <td colSpan={6} className="text-center py-10 text-gray-500 text-sm">
                        No stocks match the current filters.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}
      </main>
    </div>
  );
}