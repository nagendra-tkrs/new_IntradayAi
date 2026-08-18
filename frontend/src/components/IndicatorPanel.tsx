"use client";

interface IndicatorPanelProps {
  indicators: Record<string, number | null>;
}

function rsiColor(val: number) {
  if (val > 70) return "text-red-400";
  if (val < 30) return "text-green-400";
  return "text-gray-300";
}

export default function IndicatorPanel({ indicators }: IndicatorPanelProps) {
  const items = [
    { label: "EMA 9", value: indicators.ema_9, format: "price" },
    { label: "EMA 20", value: indicators.ema_20, format: "price" },
    { label: "EMA 50", value: indicators.ema_50, format: "price" },
    { label: "RSI (14)", value: indicators.rsi_14, format: "rsi" },
    { label: "MACD", value: indicators.macd, format: "num" },
    { label: "MACD Signal", value: indicators.macd_signal, format: "num" },
    { label: "MACD Hist", value: indicators.macd_histogram, format: "num" },
    { label: "ATR (14)", value: indicators.atr_14, format: "price" },
    { label: "ADX (14)", value: indicators.adx_14, format: "num" },
    { label: "VWAP", value: indicators.vwap, format: "price" },
    { label: "BB Upper", value: indicators.bb_upper, format: "price" },
    { label: "BB Lower", value: indicators.bb_lower, format: "price" },
    { label: "Rel Volume", value: indicators.relative_volume, format: "mult" },
    { label: "Dist VWAP", value: indicators.distance_from_vwap, format: "pct" },
    { label: "Volatility", value: indicators.volatility_20, format: "pct" },
  ];

  return (
    <div className="grid grid-cols-3 sm:grid-cols-5 gap-2">
      {items.map((item) => (
        <div key={item.label} className="bg-[#111827] rounded-lg p-2.5">
          <div className="text-[10px] text-gray-500 uppercase tracking-wider">{item.label}</div>
          <div className={`text-sm font-semibold mt-0.5 ${
            item.format === "rsi" ? rsiColor(item.value || 50) :
            item.format === "pct" ? ((item.value || 0) >= 0 ? "text-green-400" : "text-red-400") :
            "text-white"
          }`}>
            {item.value !== null && item.value !== undefined
              ? item.format === "price" ? `₹${item.value.toLocaleString()}`
              : item.format === "mult" ? `${item.value}x`
              : item.format === "pct" ? `${item.value.toFixed(2)}%`
              : item.value.toFixed(2)
              : "-"}
          </div>
        </div>
      ))}
    </div>
  );
}
