"use client";

interface SignalCardProps {
  signal: any;
  onClick?: () => void;
}

function confidenceColor(confidence: number) {
  if (confidence >= 80) return "text-green-400";
  if (confidence >= 65) return "text-blue-400";
  if (confidence >= 50) return "text-yellow-400";
  return "text-gray-400";
}

function confidenceLabel(confidence: number) {
  if (confidence >= 90) return "Very Strong";
  if (confidence >= 80) return "Strong";
  if (confidence >= 65) return "Good";
  if (confidence >= 50) return "Moderate";
  return "Avoid";
}

function directionBadge(direction: string) {
  const d = direction?.toUpperCase() || "";
  if (d.includes("LONG")) return "badge-long";
  if (d.includes("SHORT")) return "badge-short";
  return "badge-no-trade";
}

export default function SignalCard({ signal, onClick }: SignalCardProps) {
  if (!signal) return null;
  const confidence = signal.confidence || 0;
  const setup = signal.setup || {};

  return (
    <div
      className="card hover:bg-[#222839] transition-colors cursor-pointer"
      onClick={onClick}
    >
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="font-bold text-white text-lg">{signal.symbol}</h3>
          <p className="text-xs text-gray-500">{signal.strategy}</p>
          {signal.timestamp && (
            <p className="text-[10px] text-gray-600 mt-0.5 flex items-center gap-1">
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
              {new Date(signal.timestamp).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true, timeZone: "Asia/Kolkata" })}
            </p>
          )}
        </div>
        <div className="text-right">
          <span className={`inline-block px-2.5 py-1 rounded-full text-xs font-bold ${directionBadge(signal.direction)}`}>
            {signal.direction}
          </span>
        </div>
      </div>

      <div className="flex items-center gap-2 mb-3">
        <span className={`text-2xl font-bold ${confidenceColor(confidence)}`}>
          {confidence}
        </span>
        <span className="text-xs text-gray-500">/ 100</span>
        <span className={`text-xs font-medium ml-1 ${confidenceColor(confidence)}`}>
          {confidenceLabel(confidence)}
        </span>
      </div>

      <div className="confidence-bar mb-3">
        <div
          className="confidence-fill"
          style={{
            width: `${confidence}%`,
            background: confidence >= 80 ? "var(--green)" : confidence >= 60 ? "var(--blue)" : "var(--yellow)",
          }}
        />
      </div>

      {setup.entry > 0 && (
        <div className="grid grid-cols-2 gap-2 text-xs mb-3">
          <div className="bg-[#111827] rounded-lg p-2">
            <span className="text-gray-500">Entry</span>
            <p className="font-semibold text-white">₹{setup.entry?.toLocaleString()}</p>
          </div>
          <div className="bg-[#111827] rounded-lg p-2">
            <span className="text-gray-500">Stop Loss</span>
            <p className="font-semibold text-red-400">₹{setup.stop_loss?.toLocaleString()}</p>
          </div>
          <div className="bg-[#111827] rounded-lg p-2">
            <span className="text-gray-500">Target 1</span>
            <p className="font-semibold text-green-400">₹{setup.target_1?.toLocaleString()}</p>
          </div>
          <div className="bg-[#111827] rounded-lg p-2">
            <span className="text-gray-500">R:R Ratio</span>
            <p className="font-semibold text-blue-400">1:{setup.risk_reward_ratio?.toFixed(2)}</p>
          </div>
        </div>
      )}

      {signal.explanation?.reasons && signal.explanation.reasons.length > 0 && (
        <div className="text-xs space-y-1">
          {signal.explanation.reasons.slice(0, 4).map((r: string, i: number) => (
            <div key={i} className="flex items-start gap-1.5">
              <span className="text-green-400 mt-0.5">&#10003;</span>
              <span className="text-gray-300">{r}</span>
            </div>
          ))}
          {signal.explanation.risks?.slice(0, 2).map((r: string, i: number) => (
            <div key={`risk-${i}`} className="flex items-start gap-1.5">
              <span className="text-yellow-400 mt-0.5">&#9888;</span>
              <span className="text-yellow-300/80">{r}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
