"use client";

import { useEffect, useState } from "react";
import Header from "@/components/Header";
import { runScanner } from "@/lib/api";

type FilterType = "all" | "long" | "short" | "strong" | "high_volume" | "no_trade";

export default function ScannerPage() {
  const [scanner, setScanner] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<FilterType>("all");
  const [sortBy, setSortBy] = useState<string>("confidence");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  useEffect(() => {
    loadScanner();
  }, []);

  async function loadScanner() {
    try {
      setLoading(true);
      const data = await runScanner();
      setScanner(data);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }

  function getFilteredResults() {
    if (!scanner?.results) return [];
    let results = [...scanner.results];

    switch (filter) {
      case "long":
        results = results.filter((r: any) => r.signal?.includes("LONG"));
        break;
      case "short":
        results = results.filter((r: any) => r.signal?.includes("SHORT"));
        break;
      case "strong":
        results = results.filter((r: any) => r.confidence >= 75);
        break;
      case "high_volume":
        results = results.filter((r: any) => r.relative_volume >= 1.5);
        break;
      case "no_trade":
        results = results.filter((r: any) => r.signal === "NO TRADE");
        break;
    }

    results.sort((a: any, b: any) => {
      const aVal = a[sortBy] || 0;
      const bVal = b[sortBy] || 0;
      return sortDir === "desc" ? bVal - aVal : aVal - bVal;
    });

    return results;
  }

  function toggleSort(col: string) {
    if (sortBy === col) {
      setSortDir(sortDir === "desc" ? "asc" : "desc");
    } else {
      setSortBy(col);
      setSortDir("desc");
    }
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
          </div>
          <button
            onClick={loadScanner}
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
                filter === f.key
                  ? "bg-blue-600 text-white"
                  : "bg-[#1a1f2e] text-gray-400 hover:bg-[#222839]"
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
                  <th className="num">Trend</th>
                  <th className="num cursor-pointer" onClick={() => toggleSort("distance_from_vwap")}>VWAP Dist</th>
                  <th>Signal</th>
                  <th className="num cursor-pointer" onClick={() => toggleSort("confidence")}>Conf</th>
                </tr>
              </thead>
              <tbody>
                {getFilteredResults().map((stock: any) => (
                  <tr
                    key={stock.symbol}
                    className="cursor-pointer"
                    onClick={() => window.location.href = `/stock/${stock.symbol}`}
                  >
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
                        stock.signal?.includes("LONG") ? "badge-long" :
                        stock.signal?.includes("SHORT") ? "badge-short" : "badge-no-trade"
                      }`}>{stock.signal || "N/A"}</span>
                    </td>
                    <td className={`num font-bold text-sm ${
                      stock.confidence >= 80 ? "text-green-400" :
                      stock.confidence >= 60 ? "text-blue-400" :
                      stock.confidence > 0 ? "text-yellow-400" : "text-gray-500"
                    }`}>{stock.confidence || 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </div>
  );
}
