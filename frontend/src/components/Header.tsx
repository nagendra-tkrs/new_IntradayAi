"use client";

import { useEffect, useState } from "react";
import { getMarketStatus, getMarketIndex, getDataStatus } from "@/lib/api";
import Link from "next/link";
import { usePathname } from "next/navigation";

export default function Header() {
  const [status, setStatus] = useState<any>(null);
  const [nifty, setNifty] = useState<any>(null);
  const [bankNifty, setBankNifty] = useState<any>(null);
  const [dataStatus, setDataStatus] = useState<any>(null);
  const pathname = usePathname();

  useEffect(() => {
    async function load() {
      try {
        const [s, n, bn, ds] = await Promise.all([
          getMarketStatus(),
          getMarketIndex("NIFTY50"),
          getMarketIndex("BANKNIFTY"),
          getDataStatus(),
        ]);
        setStatus(s);
        setNifty(n);
        setBankNifty(bn);
        setDataStatus(ds);
      } catch (e) {}
    }
    load();
    const iv = setInterval(load, 30000);
    return () => clearInterval(iv);
  }, []);

  const navItems = [
    { href: "/", label: "Dashboard" },
    { href: "/scanner", label: "Scanner" },
    { href: "/signals", label: "Signals" },
    { href: "/paper-trading", label: "Paper Trading" },
    { href: "/backtest", label: "Backtest" },
  ];

  const getDataStatusColor = (s: any) => {
    if (!s) return "text-gray-500";
    const src = s.data_source || s.mode;
    if (src === "yfinance") {
      if (s.connection_status === "connected") return "text-green-400";
      if (s.connection_status === "no_credentials") return "text-yellow-400";
      return "text-blue-400";
    }
    if (src === "mock" || src === "SIMULATED") return "text-yellow-400";
    if (s.connection_status === "connected") return "text-green-400";
    return "text-red-400";
  };

  const getDataStatusText = (s: any) => {
    if (!s) return "UNKNOWN";
    const src = s.data_source || s.mode;
    if (src === "yfinance") return "yfinance DATA";
    if (src === "mock" || src === "SIMULATED") return "SIMULATED";
    if (src === "LIVE") return "LIVE";
    return src?.toUpperCase() || "UNKNOWN";
  };

  const getDataStatusDot = (s: any) => {
    if (!s) return "bg-gray-500";
    const src = s.data_source || s.mode;
    if (src === "yfinance") {
      if (s.connection_status === "connected") return "bg-green-500";
      return "bg-blue-500 animate-pulse";
    }
    if (src === "mock" || src === "SIMULATED") return "bg-yellow-500";
    if (s.connection_status === "connected") return "bg-green-500 animate-pulse";
    return "bg-red-500";
  };

  const formatAge = (ageSeconds: number | null) => {
    if (ageSeconds === null || ageSeconds === undefined) return null;
    if (ageSeconds < 60) return `${Math.round(ageSeconds)}s ago`;
    if (ageSeconds < 3600) return `${Math.round(ageSeconds / 60)}m ago`;
    return `${Math.round(ageSeconds / 3600)}h ago`;
  };

  return (
    <header className="sticky top-0 z-50 bg-[#0d1321] border-b border-[#2d3548]">
      <div className="max-w-[1440px] mx-auto px-4">
        <div className="flex items-center justify-between h-14">
          <div className="flex items-center gap-6">
            <Link href="/" className="flex items-center gap-2">
              <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center font-bold text-sm text-white">AI</div>
              <span className="font-bold text-white text-lg hidden sm:block">IntradayAI</span>
            </Link>
            <nav className="flex items-center gap-1">
              {navItems.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`px-3 py-1.5 text-sm font-medium rounded-md transition-colors ${
                    pathname === item.href
                      ? "bg-blue-600/20 text-blue-400"
                      : "text-gray-400 hover:text-gray-200 hover:bg-white/5"
                  }`}
                >
                  {item.label}
                </Link>
              ))}
            </nav>
          </div>
          <div className="flex items-center gap-6 text-xs">
            <div className="flex items-center gap-2">
              <div className={`w-2 h-2 rounded-full ${getDataStatusDot(dataStatus)}`} />
              <span className={`font-semibold ${getDataStatusColor(dataStatus)}`}>
                {getDataStatusText(dataStatus)}
              </span>
              {dataStatus?.last_tick_time && (
                <>
                  <span className="text-gray-500">|</span>
                  <span className="text-gray-400">
                    Last: {new Date(dataStatus.last_tick_time).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" })} IST
                  </span>
                </>
              )}
              {dataStatus?.signals_paused && (
                <span className="text-orange-400 bg-orange-400/10 px-2 py-0.5 rounded">
                  SIGNALS PAUSED
                </span>
              )}
            </div>
            {nifty && (
              <div className="hidden md:flex items-center gap-4">
                <div>
                  <span className="text-gray-500">NIFTY </span>
                  <span className="font-semibold">{nifty.price?.toLocaleString()}</span>
                  <span className={nifty.change >= 0 ? "text-green-400 ml-1" : "text-red-400 ml-1"}>
                    {nifty.change >= 0 ? "+" : ""}{nifty.change?.toFixed(2)} ({nifty.change_pct?.toFixed(2)}%)
                  </span>
                  {nifty.data_status && (
                    <span className={`ml-1 text-[10px] px-1 rounded ${
                      nifty.data_status === "LIVE" ? "bg-green-500/20 text-green-400" :
                      nifty.data_status === "RECENT" ? "bg-green-500/20 text-green-400" :
                      nifty.data_status === "DELAYED" ? "bg-yellow-500/20 text-yellow-400" :
                      "bg-red-500/20 text-red-400"
                    }`}>
                      {nifty.data_status}
                    </span>
                  )}
                </div>
                {bankNifty && (
                  <div>
                    <span className="text-gray-500">BANKNIFTY </span>
                    <span className="font-semibold">{bankNifty.price?.toLocaleString()}</span>
                    <span className={bankNifty.change >= 0 ? "text-green-400 ml-1" : "text-red-400 ml-1"}>
                      {bankNifty.change >= 0 ? "+" : ""}{bankNifty.change?.toFixed(2)} ({bankNifty.change_pct?.toFixed(2)}%)
                    </span>
                    {bankNifty.data_status && (
                      <span className={`ml-1 text-[10px] px-1 rounded ${
                        bankNifty.data_status === "LIVE" ? "bg-green-500/20 text-green-400" :
                        bankNifty.data_status === "RECENT" ? "bg-green-500/20 text-green-400" :
                        bankNifty.data_status === "DELAYED" ? "bg-yellow-500/20 text-yellow-400" :
                        "bg-red-500/20 text-red-400"
                      }`}>
                        {bankNifty.data_status}
                      </span>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </header>
  );
}
