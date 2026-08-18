"use client";

import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";

interface MiniChartProps {
  data: any[];
  height?: number;
  showVolume?: boolean;
  entryLine?: number;
  stopLine?: number;
  targetLine?: number;
}

export default function MiniChart({
  data,
  height = 300,
  showVolume = false,
  entryLine,
  stopLine,
  targetLine,
}: MiniChartProps) {
  if (!data || data.length === 0) {
    return (
      <div className="flex items-center justify-center text-gray-500" style={{ height }}>
        No chart data available
      </div>
    );
  }

  const chartData = data.map((d, i) => ({
    ...d,
    time: d.timestamp
      ? new Date(d.timestamp).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })
      : `${i}`,
    idx: i,
  }));

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={chartData} margin={{ top: 5, right: 5, left: 5, bottom: 5 }}>
        <XAxis
          dataKey="time"
          tick={{ fill: "#6b7280", fontSize: 10 }}
          axisLine={{ stroke: "#2d3548" }}
          tickLine={false}
          interval="preserveStartEnd"
        />
        <YAxis
          domain={["auto", "auto"]}
          tick={{ fill: "#6b7280", fontSize: 10 }}
          axisLine={{ stroke: "#2d3548" }}
          tickLine={false}
          width={65}
          tickFormatter={(v: number) => v.toLocaleString()}
        />
        <Tooltip
          contentStyle={{
            background: "#1a1f2e",
            border: "1px solid #2d3548",
            borderRadius: "8px",
            fontSize: "12px",
          }}
          labelStyle={{ color: "#9ca3af" }}
        />
        <Line type="monotone" dataKey="close" stroke="#3b82f6" strokeWidth={1.5} dot={false} name="Price" />
        {data[0]?.vwap !== undefined && (
          <Line type="monotone" dataKey="vwap" stroke="#a855f7" strokeWidth={1} dot={false} strokeDasharray="4 2" name="VWAP" />
        )}
        {data[0]?.ema_9 !== undefined && (
          <Line type="monotone" dataKey="ema_9" stroke="#22c55e" strokeWidth={1} dot={false} name="EMA 9" />
        )}
        {data[0]?.ema_20 !== undefined && (
          <Line type="monotone" dataKey="ema_20" stroke="#eab308" strokeWidth={1} dot={false} name="EMA 20" />
        )}
        {entryLine && <ReferenceLine y={entryLine} stroke="#3b82f6" strokeDasharray="5 5" label={{ value: "Entry", fill: "#3b82f6", fontSize: 10 }} />}
        {stopLine && <ReferenceLine y={stopLine} stroke="#ef4444" strokeDasharray="5 5" label={{ value: "SL", fill: "#ef4444", fontSize: 10 }} />}
        {targetLine && <ReferenceLine y={targetLine} stroke="#22c55e" strokeDasharray="5 5" label={{ value: "T1", fill: "#22c55e", fontSize: 10 }} />}
      </LineChart>
    </ResponsiveContainer>
  );
}
