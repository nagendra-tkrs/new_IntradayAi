import type { ScannerResult } from "./types";

/**
 * Phase 4A — presentation-layer selection helpers (Dynamic Top Signals).
 *
 * These operate ONLY on the scanner response already returned by the backend.
 * They never generate signals, never re-score, and never touch the Signal
 * Engine. The Top 3 are a COMBINED LONG + SHORT pool — no direction
 * separation — recomputed from the freshest scanner dataset via the Top
 * Signal Quality Layer fields (setup quality → score → confirmations →
 * confidence → RR), falling back to confidence when quality fields are absent.
 */

/** Backend signal direction for a scanner result (e.g. "STRONG_LONG", "SHORT", "NO_TRADE"). */
export function signalDirection(stock: Pick<ScannerResult, "signal">): string {
  const sig = stock.signal;
  if (typeof sig === "string") return sig.toUpperCase();
  return "NO_TRADE";
}

/** Maximum Top Signals shown (combined pool, not per direction). */
export const TOP_SIGNALS_LIMIT = 3;

/** Relative strength of a setup-quality classification (higher = better). */
const QUALITY_ORDER: Record<string, number> = {
  PREMIUM: 5,
  QUALIFIED: 4,
  NORMAL: 3,
  WEAK: 2,
  REJECTED: 1,
};

/**
 * Rankable surface: the base signal fields plus the optional Top Signal
 * Quality Layer fields. Anything with `signal` + `confidence` satisfies it
 * (all quality fields are optional), so generic `Pick` constraints flow in
 * without unsafe casts.
 */
interface QualityRankable extends Pick<ScannerResult, "signal" | "confidence"> {
  symbol?: string;
  setup_quality?: ScannerResult["setup_quality"];
  setup_quality_score?: number | null;
  confirmation_count?: number | null;
  confirmation_total?: number | null;
  risk_reward_ratio?: number | null;
}

/**
 * Deterministic quality-first comparator over scanner results.
 *
 * Ranking key (descending):
 *   setup_quality level → setup_quality_score → confirmation_count →
 *   confidence → risk_reward_ratio → symbol (stable ASCII tiebreak).
 *
 * When no result carries quality fields (older payloads), it degrades to the
 * previous confidence-only ranking. Presentation only — never re-scores.
 */
function compareQualityFirst(a: QualityRankable, b: QualityRankable): number {
  const aLevel = a.setup_quality ? QUALITY_ORDER[a.setup_quality] ?? 0 : 0;
  const bLevel = b.setup_quality ? QUALITY_ORDER[b.setup_quality] ?? 0 : 0;
  const hasQuality = a.setup_quality != null || b.setup_quality != null;
  if (!hasQuality) return (b.confidence || 0) - (a.confidence || 0);

  return (
    bLevel - aLevel ||
    (b.setup_quality_score ?? 0) - (a.setup_quality_score ?? 0) ||
    (b.confirmation_count ?? 0) - (a.confirmation_count ?? 0) ||
    (b.confidence || 0) - (a.confidence || 0) ||
    (b.risk_reward_ratio ?? 0) - (a.risk_reward_ratio ?? 0) ||
    String(a.symbol ?? "").localeCompare(String(b.symbol ?? ""))
  );
}

/**
 * Current Top Signals from the LATEST scan: eligible rows (PREMIUM /
 * QUALIFIED) only, one combined LONG + SHORT pool, ranked deterministically
 * by the existing quality-first order, capped at `limit` (default 3).
 *
 * Recalculated on every fresh scanner dataset — no persistence, no direction
 * balancing, and WEAK / REJECTED / NOT_ELIGIBLE rows are never promoted to
 * fill a slot.
 */
export function selectTopSignals<T extends ScannerResult>(
  results: T[],
  limit = TOP_SIGNALS_LIMIT,
): T[] {
  return results
    .filter((r) => r.top_signal_eligible === true)
    .sort((a, b) => compareQualityFirst(a, b))
    .slice(0, limit);
}

/** Number of currently eligible signals (PREMIUM / QUALIFIED) in a scan. */
export function countEligibleSignals(
  results: Pick<ScannerResult, "top_signal_eligible">[],
): number {
  return results.filter((r) => r.top_signal_eligible === true).length;
}