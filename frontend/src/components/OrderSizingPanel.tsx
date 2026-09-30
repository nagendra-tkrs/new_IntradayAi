"use client";

import { useEffect, useRef, useState } from "react";
import { previewPaperOrder } from "@/lib/api";
import type { OrderPreviewRequest, RiskPreview, SizingMode } from "@/lib/types";

/**
 * Risk-aware order sizing control.
 *
 * Replaces the old `qty || 1` quantity box. Two things changed and both are
 * deliberate:
 *
 *  1. The default is MANUAL, so day-one behaviour is identical to before -
 *     nothing grows unless the user asks for it. Switching to RISK_ENGINE is a
 *     single, explicit click.
 *  2. The whole calculation is shown before the order exists: risk budget, risk
 *     per share, both constraints, the quantity the engine would pick, the
 *     initial risk that implies, and budget utilization. Nothing is hidden and
 *     nothing is applied without being displayed first.
 *
 * The panel never computes a size itself. Every number comes from the backend
 * preview endpoint, which is the same arithmetic the execution path uses.
 */

interface OrderSizingPanelProps {
  symbol: string;
  direction: string;
  entryPrice: number | null;
  stopLoss: number | null;
  target1?: number | null;
  /** The user's manual quantity as typed: "" means "not chosen". */
  manualQuantity: string;
  onManualQuantityChange: (value: string) => void;
  mode: SizingMode;
  onModeChange: (mode: SizingMode) => void;
  disabled?: boolean;
}

/**
 * Why there are deliberately no `setup_quality` / `signal_strength` props.
 *
 * Sending them would make the risk engine apply `quality_risk_multipliers`,
 * which changes the EFFECTIVE risk percentage (and makes REJECTED/WEAK refuse
 * to trade at all). That is a risk-parameter change this integration must not
 * make. The UI therefore sizes at the configured base risk, which is exactly
 * what the order endpoint computes when no quality metadata is sent - so the
 * preview and the executed order cannot disagree.
 */

const DEBOUNCE_MS = 300;

function inr(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `₹${value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function qtyText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return String(Math.trunc(value));
}

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value.toFixed(1)}%`;
}

function Row({
  label,
  value,
  tone = "default",
  title,
}: {
  label: string;
  value: string;
  tone?: "default" | "good" | "warn" | "bad";
  title?: string;
}) {
  const toneClass =
    tone === "good" ? "text-green-400"
      : tone === "warn" ? "text-amber-400"
        : tone === "bad" ? "text-red-400"
          : "text-white";
  return (
    <div className="flex items-baseline justify-between gap-3" title={title}>
      <span className="text-[10px] text-gray-500 uppercase tracking-wider shrink-0">
        {label}
      </span>
      <span className={`text-xs font-semibold tabular-nums ${toneClass}`}>{value}</span>
    </div>
  );
}

export default function OrderSizingPanel({
  symbol,
  direction,
  entryPrice,
  stopLoss,
  target1,
  manualQuantity,
  onManualQuantityChange,
  mode,
  onModeChange,
  disabled = false,
}: OrderSizingPanelProps) {
  const [preview, setPreview] = useState<RiskPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [showDetail, setShowDetail] = useState(true);

  // Guards against an out-of-order response overwriting a newer one.
  const seq = useRef(0);

  const parsedManual = manualQuantity.trim() === "" ? null : Number(manualQuantity);
  const manualUsable =
    manualQuantity.trim() !== "" && Number.isFinite(parsedManual) && (parsedManual as number) > 0;

  useEffect(() => {
    if (disabled) return;
    if (!entryPrice || !Number.isFinite(entryPrice) || entryPrice <= 0) {
      setPreview(null);
      return;
    }

    const mine = ++seq.current;
    setLoading(true);
    const timer = setTimeout(() => {
      const payload: OrderPreviewRequest = {
        symbol,
        direction,
        entry_price: entryPrice,
        stop_loss: stopLoss,
        target_1: target1 ?? null,
        // Ask for BOTH paths in one call: the recommendation does not depend on
        // the current mode, so switching modes must not refetch or change it.
        requested_quantity: manualUsable ? (parsedManual as number) : null,
      };
      previewPaperOrder(payload)
        .then((p) => {
          if (mine !== seq.current) return;
          setPreview(p);
          setError(null);
        })
        .catch((e) => {
          if (mine !== seq.current) return;
          setPreview(null);
          setError(e instanceof Error ? e.message : "Preview unavailable");
        })
        .finally(() => {
          if (mine === seq.current) setLoading(false);
        });
    }, DEBOUNCE_MS);

    return () => clearTimeout(timer);
  }, [
    symbol, direction, entryPrice, stopLoss, target1,
    manualQuantity, manualUsable, parsedManual, disabled,
  ]);

  const recommended = preview?.allowed_quantity ?? null;
  const engineAvailable = Boolean(preview?.sizing_available) && (recommended ?? 0) > 0;

  // MANUAL without a usable number is a contradiction: the backend rejects it,
  // so the UI says why rather than quietly reverting to the risk engine.
  // (Whether the order button is enabled is the caller's decision - it owns the
  // submit handler - so this panel only reports the state.)
  const modeProblem =
    mode === "MANUAL" && !manualUsable
      ? "Enter a whole number of shares, or switch to risk-engine sizing."
      : null;

  const overBudget = preview?.manual_exceeds_risk_budget === true;
  const unaffordable = preview?.manual_affordable === false;

  return (
    <div className="bg-[#111827] rounded-lg p-3 space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-[10px] text-gray-500 uppercase tracking-wider">Position Sizing</p>
        <button
          type="button"
          onClick={() => setShowDetail((v) => !v)}
          className="text-[10px] text-gray-500 hover:text-gray-300 uppercase tracking-wider"
        >
          {showDetail ? "Hide math" : "Show math"}
        </button>
      </div>

      {/* ── Sizing mode ─────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 gap-2">
        {(["MANUAL", "RISK_ENGINE"] as SizingMode[]).map((m) => {
          const active = mode === m;
          return (
            <button
              key={m}
              type="button"
              disabled={disabled}
              onClick={() => onModeChange(m)}
              className={`px-2 py-1.5 rounded-lg text-xs font-bold border transition-colors disabled:opacity-50 ${
                active
                  ? "bg-blue-600/20 border-blue-500/50 text-blue-300"
                  : "bg-[#0f1520] border-[#2d3548] text-gray-400 hover:text-gray-200"
              }`}
            >
              {m === "MANUAL" ? "Manual qty" : "Risk engine"}
            </button>
          );
        })}
      </div>

      {/* ── Manual quantity input (only meaningful in MANUAL) ───────────── */}
      {mode === "MANUAL" && (
        <div className="space-y-1.5">
          <label className="flex items-center gap-2">
            <span className="text-xs text-gray-400">Qty:</span>
            <input
              type="number"
              min={1}
              step={1}
              placeholder="e.g. 10"
              value={manualQuantity}
              disabled={disabled}
              onChange={(e) => onManualQuantityChange(e.target.value)}
              className="w-24 px-2 py-1 bg-[#0f1520] border border-[#2d3548] rounded text-white text-sm"
            />
            {entryPrice ? (
              <span className="text-[10px] text-gray-600">@ ₹{entryPrice}</span>
            ) : null}
          </label>
          {modeProblem && (
            <p className="text-[11px] text-amber-400">{modeProblem}</p>
          )}
          {overBudget && (
            <p className="text-[11px] text-amber-400">
              This size risks {pct(preview?.manual_utilization_percent)} of the
              configured {pct(preview?.configured_risk_percent)} budget. It will be
              placed as entered - flagged, never silently reduced.
            </p>
          )}
          {unaffordable && (
            <p className="text-[11px] text-red-400">
              {qtyText(preview?.requested_quantity)} × {inr(entryPrice, 2)} ={" "}
              {inr(preview?.manual_capital_usage, 0)} exceeds available buying power.
            </p>
          )}
        </div>
      )}

      {/* ── The recommendation, always visible, never auto-applied ───────── */}
      <div className="rounded-lg border border-[#2d3548] bg-[#0f1520] p-2.5 space-y-1.5">
        <div className="flex items-baseline justify-between">
          <span className="text-[10px] text-gray-500 uppercase tracking-wider">
            Recommended qty
          </span>
          <span className="text-lg font-bold text-white tabular-nums">
            {engineAvailable ? qtyText(recommended) : "—"}
          </span>
        </div>
        <p className="text-[10px] text-gray-500">
          {engineAvailable
            ? mode === "RISK_ENGINE"
              ? "This order will use this quantity."
              : "Switch to “Risk engine” to use it. Your order uses the quantity above."
            : "Needs a valid entry and stop loss."}
        </p>
        {engineAvailable && mode === "MANUAL" && (
          <button
            type="button"
            disabled={disabled}
            onClick={() => onModeChange("RISK_ENGINE")}
            className="w-full px-2 py-1 bg-blue-600/20 hover:bg-blue-600/30 border border-blue-500/40 text-blue-300 text-[11px] font-bold rounded disabled:opacity-50"
          >
            Use risk-engine size ({qtyText(recommended)})
          </button>
        )}
      </div>

      {/* ── The calculation, in full, unhidden ───────────────────────────── */}
      {showDetail && (
        <div className="space-y-1 pt-1 border-t border-[#2d3548]">
          {error ? (
            <p className="text-[11px] text-amber-400">Preview unavailable: {error}</p>
          ) : !preview ? (
            <p className="text-[11px] text-gray-600">
              {loading ? "Calculating…" : "Enter a price to see the sizing math."}
            </p>
          ) : (
            <>
              <Row
                label="Risk budget"
                value={`${inr(preview.risk_budget, 0)} (${pct(preview.configured_risk_percent)})`}
                title="account_capital × configured_risk_percent"
              />
              <Row
                label="Risk / share"
                value={inr(preview.risk_per_share)}
                title="|entry − stop loss|"
              />
              <Row
                label="Risk constraint"
                value={`${qtyText(preview.risk_based_quantity)} sh`}
                title="risk_budget ÷ risk_per_share"
              />
              <Row
                label="Capital constraint"
                value={`${qtyText(preview.capital_based_quantity)} sh`}
                title="95% of capital ÷ entry"
              />
              <Row
                label="Binding"
                value={preview.binding_constraint}
                tone={preview.binding_constraint === "RISK_BUDGET" ? "warn" : "default"}
                title="Whichever upper bound is smaller sets the size"
              />
              <Row
                label="Initial risk"
                value={inr(preview.expected_initial_risk)}
                tone="good"
                title="allowed_quantity × risk_per_share"
              />
              <Row
                label="Utilization"
                value={pct(preview.risk_utilization_percent)}
                tone="warn"
                title="initial risk ÷ risk budget"
              />
              {mode === "MANUAL" && manualUsable && (
                <>
                  <div className="pt-1 mt-1 border-t border-[#2d3548]">
                    <Row
                      label="Your initial risk"
                      value={inr(preview.manual_initial_risk)}
                      tone={overBudget ? "bad" : "default"}
                    />
                    <Row
                      label="Your utilization"
                      value={pct(preview.manual_utilization_percent)}
                      tone={overBudget ? "bad" : "default"}
                    />
                    <Row
                      label="Capital used"
                      value={inr(preview.manual_capital_usage, 0)}
                      tone={unaffordable ? "bad" : "default"}
                    />
                  </div>
                </>
              )}
              {preview.blockers && preview.blockers.length > 0 && (
                <p className="text-[11px] text-red-400 pt-1">
                  This setup will be rejected: {preview.blockers.join("; ")}
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
