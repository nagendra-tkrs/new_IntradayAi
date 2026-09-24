"use client";

import { useState, useCallback, useMemo } from "react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  ReferenceLine, CartesianGrid, ComposedChart, Bar, Brush,
} from "recharts";
import type { CartesianTickItem, MouseHandlerDataParam, TooltipPayloadEntry } from "recharts";
import type { ChartPoint } from "@/lib/types";

interface MiniChartProps {
  data: ChartPoint[];
  height?: number;
  showVolume?: boolean;
  entryLine?: number;
  stopLine?: number;
  targetLine?: number;
}

function formatIST(isoString: string | null | undefined): string {
  if (!isoString) return "";
  try {
    return new Date(isoString).toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
      timeZone: "Asia/Kolkata",
    });
  } catch {
    return "";
  }
}

function formatFullIST(isoString: string | null | undefined): string {
  if (!isoString) return "";
  try {
    const d = new Date(isoString);
    const datePart = d.toLocaleDateString("en-IN", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      timeZone: "Asia/Kolkata",
    });
    const timePart = d.toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
      timeZone: "Asia/Kolkata",
    });
    return `${datePart} ${timePart} IST`;
  } catch {
    return "";
  }
}

function formatVolume(v: number): string {
  if (v >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return v.toString();
}

function CustomTooltip({ active, payload }: { active?: boolean; payload?: TooltipPayloadEntry[] }) {
  if (!active || !payload || !payload.length) return null;
  const data = payload[0]?.payload as Partial<ChartPoint> | undefined;
  if (!data) return null;
  return (
    <div className="bg-[#1a1f2e] border border-[#2d3548] rounded-lg p-3 text-xs shadow-lg min-w-[180px]">
      <div className="text-gray-400 mb-2 font-semibold">{formatFullIST(data.timestamp)}</div>
      {data.open !== undefined && (
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 mb-2 text-[11px]">
          <span className="text-gray-500">O</span>
          <span className="text-white text-right">₹{data.open?.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
          <span className="text-gray-500">H</span>
          <span className="text-green-400 text-right">₹{data.high?.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
          <span className="text-gray-500">L</span>
          <span className="text-red-400 text-right">₹{data.low?.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
          <span className="text-gray-500">C</span>
          <span className="text-blue-400 text-right font-bold">₹{data.close?.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
        </div>
      )}
      <div className="border-t border-[#2d3548] pt-2 space-y-1">
        {payload.map((p: TooltipPayloadEntry, i: number) => {
          if (p.dataKey === "volume" || p.dataKey === "open" || p.dataKey === "high" || p.dataKey === "low" || p.dataKey === "close") return null;
          const pv = p.value as unknown as number | string | null | undefined;
          if (pv === null || pv === undefined) return null;
          return (
            <div key={i} className="flex items-center justify-between text-[11px]">
              <span style={{ color: p.color }}>{p.name ?? String(p.dataKey ?? "")}</span>
              <span className="text-white">₹{typeof pv === "number" ? pv.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : pv}</span>
            </div>
          );
        })}
      </div>
      {data.volume !== undefined && (
        <div className="border-t border-[#2d3548] pt-2 mt-2 flex items-center justify-between text-[11px]">
          <span className="text-gray-500">Volume</span>
          <span className="text-white">{formatVolume(data.volume)}</span>
        </div>
      )}
    </div>
  );
}

function CustomAxisTick({ x, y, payload }: { x?: number; y?: number; payload?: CartesianTickItem }) {
  if (!payload?.value) return null;
  const time = formatIST(payload.value);
  return (
    <text x={x} y={typeof y === "number" ? y + 12 : y} textAnchor="middle" fill="#6b7280" fontSize={10}>
      {time}
    </text>
  );
}

export default function MiniChart({
  data,
  height = 300,
  showVolume = false,
  entryLine,
  stopLine,
  targetLine,
}: MiniChartProps) {
  const [activeIndex, setActiveIndex] = useState<number | null>(null);

  const chartData = useMemo(() => {
    if (!data || data.length === 0) return [];
    return data.map((d, i) => ({
      ...d,
      time: d.timestamp || `${i}`,
      idx: i,
    }));
  }, [data]);

  const handleMouseMove = useCallback((state: MouseHandlerDataParam) => {
    const idx = state.activeTooltipIndex;
    if (typeof idx === "number") {
      setActiveIndex(idx);
    }
  }, []);

  const handleMouseLeave = useCallback(() => {
    setActiveIndex(null);
  }, []);

  if (!chartData.length) {
    return (
      <div className="flex items-center justify-center text-gray-500" style={{ height }}>
        No chart data available
      </div>
    );
  }

  const priceDataKey = "close";
  const hasVolume = showVolume && chartData.some((d) => d.volume && d.volume > 0);
  const chartHeight = hasVolume ? height - 60 : height;
  const volumeHeight = 60;

  return (
    <div>
      <ResponsiveContainer width="100%" height={chartHeight}>
        <ComposedChart
          data={chartData}
          margin={{ top: 5, right: 5, left: 5, bottom: 5 }}
          onMouseMove={handleMouseMove}
          onMouseLeave={handleMouseLeave}
        >
          <CartesianGrid strokeDasharray="3 3" stroke="#1e2433" vertical={false} />
          <XAxis
            dataKey="time"
            tick={<CustomAxisTick />}
            axisLine={{ stroke: "#2d3548" }}
            tickLine={false}
            interval="preserveStartEnd"
            minTickGap={50}
          />
          <YAxis
            domain={["auto", "auto"]}
            tick={{ fill: "#6b7280", fontSize: 10 }}
            axisLine={{ stroke: "#2d3548" }}
            tickLine={false}
            width={65}
            tickFormatter={(v: number) => `₹${v.toLocaleString()}`}
          />
          <Tooltip
            content={<CustomTooltip />}
            cursor={{ stroke: "#3b82f6", strokeWidth: 1, strokeDasharray: "4 4" }}
            isAnimationActive={false}
          />
          <Line
            type="monotone"
            dataKey="close"
            stroke="#3b82f6"
            strokeWidth={1.5}
            dot={false}
            name="Price"
            activeDot={{ r: 4, fill: "#3b82f6", stroke: "#fff", strokeWidth: 2 }}
          />
          {chartData[0]?.vwap !== undefined && (
            <Line type="monotone" dataKey="vwap" stroke="#a855f7" strokeWidth={1} dot={false} strokeDasharray="4 2" name="VWAP" />
          )}
          {chartData[0]?.ema_9 !== undefined && (
            <Line type="monotone" dataKey="ema_9" stroke="#22c55e" strokeWidth={1} dot={false} name="EMA 9" />
          )}
          {chartData[0]?.ema_20 !== undefined && (
            <Line type="monotone" dataKey="ema_20" stroke="#eab308" strokeWidth={1} dot={false} name="EMA 20" />
          )}
          {chartData[0]?.ema_50 !== undefined && (
            <Line type="monotone" dataKey="ema_50" stroke="#ef4444" strokeWidth={1} dot={false} strokeDasharray="6 2" name="EMA 50" />
          )}
          {entryLine && <ReferenceLine y={entryLine} stroke="#3b82f6" strokeDasharray="5 5" label={{ value: "Entry", fill: "#3b82f6", fontSize: 10 }} />}
          {stopLine && <ReferenceLine y={stopLine} stroke="#ef4444" strokeDasharray="5 5" label={{ value: "SL", fill: "#ef4444", fontSize: 10 }} />}
          {targetLine && <ReferenceLine y={targetLine} stroke="#22c55e" strokeDasharray="5 5" label={{ value: "T1", fill: "#22c55e", fontSize: 10 }} />}
          {chartData.length > 50 && (
            <Brush
              dataKey="time"
              height={20}
              stroke="#3b82f6"
              fill="#111827"
              tickFormatter={() => ""}
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
      {hasVolume && (
        <ResponsiveContainer width="100%" height={volumeHeight}>
          <ComposedChart
            data={chartData}
            margin={{ top: 0, right: 5, left: 5, bottom: 5 }}
            onMouseMove={handleMouseMove}
            onMouseLeave={handleMouseLeave}
          >
            <XAxis
              dataKey="time"
              tick={false}
              axisLine={{ stroke: "#2d3548" }}
              tickLine={false}
            />
            <YAxis
              tick={{ fill: "#6b7280", fontSize: 9 }}
              axisLine={{ stroke: "#2d3548" }}
              tickLine={false}
              width={65}
              tickFormatter={(v: number) => formatVolume(v)}
            />
            <Tooltip
              content={<CustomTooltip />}
              cursor={{ stroke: "#3b82f6", strokeWidth: 1, strokeDasharray: "4 4" }}
              isAnimationActive={false}
            />
            <Bar
              dataKey="volume"
              fill="#3b82f6"
              opacity={0.5}
              name="Volume"
            />
          </ComposedChart>
        </ResponsiveContainer>
      )}
      {activeIndex !== null && chartData[activeIndex] && (
        <div className="flex items-center gap-4 text-[10px] text-gray-500 mt-1 px-2">
          <span>O <span className="text-white">₹{chartData[activeIndex].open?.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span></span>
          <span>H <span className="text-green-400">₹{chartData[activeIndex].high?.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span></span>
          <span>L <span className="text-red-400">₹{chartData[activeIndex].low?.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span></span>
          <span>C <span className="text-blue-400 font-bold">₹{chartData[activeIndex].close?.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span></span>
          {chartData[activeIndex].volume !== undefined && (
            <span>Vol <span className="text-white">{formatVolume(chartData[activeIndex].volume)}</span></span>
          )}
        </div>
      )}
    </div>
  );
}
