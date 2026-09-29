"use client";

import type { ScannerResult } from "@/lib/types";
import {
  countEligibleSignals,
  selectTopSignals,
  signalDirection,
  TOP_SIGNALS_LIMIT,
} from "@/lib/signals";

/**
 * Current Top Signals — ONE combined LONG + SHORT pool, at most 3.
 *
 * Pure presentation over the freshest scanner response: it filters eligible
 * rows (PREMIUM / QUALIFIED), applies the existing deterministic quality
 * ranking, and shows up to 3. Recalculated whenever a fresh scan replaces
 * the dataset — no direction separation, no persistence, and the displayed
 * rank (#1/#2/#3) always reflects the current scan's order.
 */
export default function TopSignals({
  results,
  scanned,
  onSelect,
  showCounts = true,
}: {
  results: ScannerResult[];
  scanned?: number;
  onSelect: (symbol: string) => void;
  showCounts?: boolean;
}) {
  const topSignals = selectTopSignals(results);
  const eligibleCount = countEligibleSignals(results);

  return (
    <>
      {showCounts && (
        <p className="text-xs text-gray-500 mb-3">
          {eligibleCount} eligible signal{eligibleCount === 1 ? "" : "s"}
          <span className="text-gray-600">
            {" "}(showing up to {TOP_SIGNALS_LIMIT}{scanned != null ? ` from the ${scanned} scanned` : ""})
          </span>
        </p>
      )}

      {topSignals.length === 0 && (
        <div className="card text-center py-6 text-gray-500 mb-4">
          <p className="text-sm">No eligible signals are currently available.</p>
          <p className="text-xs mt-1">The scanner shows only signals the active universe produced — it never fabricates or pads them.</p>
        </div>
      )}

      {topSignals.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {topSignals.map((s, i) => {
            const dir = signalDirection(s);
            const isLong = dir.includes("LONG");
            const isShort = dir.includes("SHORT");
            const badge = isLong ? "badge-long" : isShort ? "badge-short" : "badge-no-trade";
            const accent = isLong ? "text-green-400" : isShort ? "text-red-400" : "text-gray-400";
            return (
              <button
                key={s.symbol}
                onClick={() => onSelect(s.symbol)}
                className="w-full text-left flex items-center gap-3 px-3 py-3 rounded-lg bg-[#111827] hover:bg-[#222839] transition-colors"
              >
                <span className="text-lg font-bold text-white w-6 shrink-0">#{i + 1}</span>
                <span className="flex-1 min-w-0">
                  <span className="flex items-center gap-2 flex-wrap">
                    <span className="font-semibold text-white text-sm">{s.symbol}</span>
                    <span className={`inline-block px-1.5 py-0.5 rounded-full text-[9px] font-bold ${badge}`}>
                      {isLong ? "LONG" : isShort ? "SHORT" : dir}
                    </span>
                    {s.setup_quality ? (
                      <span className={`inline-block px-1.5 py-0.5 rounded-full text-[9px] font-bold ${
                        s.setup_quality === "PREMIUM" ? "text-amber-300 bg-amber-500/10 border border-amber-500/40" :
                        s.setup_quality === "QUALIFIED" ? "text-emerald-300 bg-emerald-500/10 border border-emerald-500/40" :
                        s.setup_quality === "NORMAL" ? "text-sky-300 bg-sky-500/10 border border-sky-500/40" :
                        s.setup_quality === "WEAK" ? "text-gray-300 bg-gray-500/10 border border-gray-500/40" :
                        "text-red-300 bg-red-500/10 border border-red-500/40"
                      }`}>{s.setup_quality}</span>
                    ) : null}
                    {s.classification ? (
                      <span className={`inline-block px-1.5 py-0.5 rounded-full text-[9px] font-bold ${
                        s.classification === "CORE" ? "badge-core" : "badge-rotation"
                      }`}>{s.classification}</span>
                    ) : (
                      <span className="text-[9px] text-gray-600">—</span>
                    )}
                  </span>
                  <span className="block text-[10px] text-gray-500 truncate">{s.name}</span>
                  {s.setup_quality_score != null && (
                    <span className="block text-[9px] text-gray-500 mt-0.5">
                      Quality Score: {s.setup_quality_score}
                      {s.confirmation_count != null && s.confirmation_total != null
                        ? ` · ${s.confirmation_count}/${s.confirmation_total} confirmations`
                        : ""}
                      {s.risk_reward_ratio != null ? ` · RR ${s.risk_reward_ratio.toFixed(2)}` : ""}
                    </span>
                  )}
                </span>
                <span className="text-right shrink-0">
                  <span className={`block text-sm font-bold ${accent}`}>{s.confidence ?? 0}</span>
                  <span className="block text-[9px] text-gray-500">confidence</span>
                </span>
              </button>
            );
          })}
        </div>
      )}
    </>
  );
}