"use client";

import { useEffect, useState } from "react";
import { getMarketStatus, getMarketIndex, getDataStatus } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import Link from "next/link";
import { usePathname } from "next/navigation";

export default function Header() {
  const [status, setStatus] = useState<any>(null);
  const [nifty, setNifty] = useState<any>(null);
  const [bankNifty, setBankNifty] = useState<any>(null);
  const [dataStatus, setDataStatus] = useState<any>(null);
  const pathname = usePathname();
  const { user, logout } = useAuth();

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
          <div className="flex items-center gap-4 text-xs">
            <div className="flex items-center gap-2">
              <div className={`w-2 h-2 rounded-full ${getDataStatusDot(dataStatus)}`} />
              <span className={`font-semibold ${getDataStatusColor(dataStatus)}`}>
                {getDataStatusText(dataStatus)}
              </span>
            </div>
            {nifty && (
              <div className="hidden md:flex items-center gap-4">
                <div>
                  <span className="text-gray-500">NIFTY </span>
                  <span className="font-semibold">{nifty.price?.toLocaleString()}</span>
                  <span className={nifty.change >= 0 ? "text-green-400 ml-1" : "text-red-400 ml-1"}>
                    {nifty.change >= 0 ? "+" : ""}{nifty.change?.toFixed(2)} ({nifty.change_pct?.toFixed(2)}%)
                  </span>
                </div>
                {bankNifty && (
                  <div>
                    <span className="text-gray-500">BANKNIFTY </span>
                    <span className="font-semibold">{bankNifty.price?.toLocaleString()}</span>
                    <span className={bankNifty.change >= 0 ? "text-green-400 ml-1" : "text-red-400 ml-1"}>
                      {bankNifty.change >= 0 ? "+" : ""}{bankNifty.change?.toFixed(2)} ({bankNifty.change_pct?.toFixed(2)}%)
                    </span>
                  </div>
                )}
              </div>
            )}
            {user && (
              <div className="flex items-center gap-3 ml-2 pl-3 border-l border-[#2d3548]">
                {user.picture ? (
                  <img src={user.picture} alt="" className="w-7 h-7 rounded-full" />
                ) : (
                  <div className="w-7 h-7 bg-blue-600 rounded-full flex items-center justify-center text-xs font-bold text-white">
                    {user.name?.charAt(0)?.toUpperCase() || "U"}
                  </div>
                )}
                <span className="text-gray-300 hidden lg:block">{user.name}</span>
                <button
                  onClick={logout}
                  className="text-gray-500 hover:text-gray-300 transition-colors cursor-pointer"
                  title="Sign out"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                  </svg>
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </header>
  );
}
