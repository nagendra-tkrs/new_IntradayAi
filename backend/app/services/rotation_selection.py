"""Rotation engine — Core / Rotation selection + anti-churn (Phase 4).

Implements Phase 2 §4 (Core = 30 stability-based, disjoint from Rotation),
§11 anti-churn ladder (1 weak period → MONITOR, 2+ → REVIEW, review + margin +
cap → replace), and Phase 4 §18/§19 deterministic rankings:

* Core: NOT "top-30 by Rotation Score". A documented Core Stability blend
  (data reliability + liquidity percentile + volume consistency + ATR-band
  centrality + trading consistency + signal reliability — sub-weights TO BE
  CALIBRATED) with hard stability gates and retention preference for
  existing Core where a previous manager universe exists.
* Rotation: anti-churn-adjusted top-50 of eligible non-Core symbols.
* Tie-breaks are explicit and deterministic (score DESC → data quality DESC →
  liquidity DESC → symbol ASC), never DB iteration order.

Defaults: keep. A challenger with a marginally higher score alone never
displaces an incumbent — it must clear ANTI_CHURN_SCORE_DELTA against a REVIEW
incumbent (or fill an empty slot vacated by a hard-failed symbol), within
MAX_CHURN_PER_CYCLE, honoring REPLACEMENT_COOLDOWN_WEEKS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.services.rotation_config import RotationEngineConfig
from app.services.rotation_scoring import trapezoid

# anti-churn states
NORMAL = "NORMAL"
MONITOR = "MONITOR"
REVIEW = "REVIEW"


@dataclass
class RotationCandidate:
    symbol: str
    rotation_score: float = 0.0
    data_quality_score: float = 0.0
    median_daily_value: float = 0.0
    volume_cv: Optional[float] = None
    median_atr_pct: Optional[float] = None
    trading_consistency: Optional[float] = None
    signal_reliability: Optional[float] = None
    expected_sessions: int = 0
    present_sessions: int = 0


@dataclass
class CoreSelection:
    symbol: str
    stability_score: float
    rank: int
    selection_reason: str
    retention_reason: Optional[str] = None
    previous_rank: Optional[int] = None
    previous_score: Optional[float] = None


@dataclass
class RotationSelection:
    symbol: str
    rotation_score: float
    rank: int
    selection_reason: str
    churn_state: str = NORMAL
    retention_reason: Optional[str] = None
    replacement_reason: Optional[str] = None
    previous_rank: Optional[int] = None
    previous_score: Optional[float] = None


@dataclass
class SelectionResult:
    core: list[CoreSelection] = field(default_factory=list)
    rotation: list[RotationSelection] = field(default_factory=list)
    core_shortfall: int = 0
    rotation_shortfall: int = 0
    churn_decisions: list[dict] = field(default_factory=list)
    core_rejections: list[dict] = field(default_factory=list)


def rotation_sort_key(c: RotationCandidate):
    """Deterministic ranking: score DESC → data quality DESC → liquidity DESC
    → symbol ASC (Phase 4 §19)."""
    return (
        -c.rotation_score,
        -c.data_quality_score,
        -c.median_daily_value,
        c.symbol,
    )


def _weak_state(
    cur_score: Optional[float], prev_score: Optional[float], floor: float
) -> tuple[int, str]:
    """Consecutive weak periods + state, from the history actually available
    (current + one previous cycle — Phase 2 §11 ladder; deeper streaks require
    more persisted cycles — documented limitation)."""
    cur_weak = cur_score is not None and cur_score < floor
    prev_weak = prev_score is not None and prev_score < floor
    if prev_weak and cur_weak:
        return 2, REVIEW
    if cur_weak:
        return 1, MONITOR
    return 0, NORMAL


# ---------------------------------------------------------------------------
# Core selection (stability-based)
# ---------------------------------------------------------------------------
def _core_stability(
    c: RotationCandidate,
    liquidity_pctile: float,
    cfg: RotationEngineConfig,
) -> float:
    dq = min(1.0, max(0.0, c.data_quality_score / 10.0))
    volume_consistency = (
        1.0 - min(1.0, max(0.0, c.volume_cv)) if c.volume_cv is not None else 0.0
    )
    atr_centrality = (
        trapezoid(
            c.median_atr_pct,
            cfg.atr_min_pct,
            cfg.atr_opt_low_pct,
            cfg.atr_opt_high_pct,
            cfg.atr_max_pct,
        )
        if c.median_atr_pct is not None
        else 0.0
    )
    consistency = c.trading_consistency if c.trading_consistency is not None else 0.0
    reliability = c.signal_reliability if c.signal_reliability is not None else 0.0
    # Documented blend — sub-weights TO BE CALIBRATED (Phase 2 §4.2);
    # stability-first, deliberately NOT the Rotation Score.
    return (
        0.25 * dq
        + 0.20 * liquidity_pctile
        + 0.15 * volume_consistency
        + 0.15 * atr_centrality
        + 0.15 * consistency
        + 0.10 * reliability
    )


def select_core(
    candidates: list[RotationCandidate],
    cfg: RotationEngineConfig,
    previous_core: Optional[dict] = None,
) -> tuple[list[CoreSelection], SelectionResult]:
    """Select up to ``cfg.core_size`` stability-ranked Core members.

    Never "top 30 by Rotation Score" — hard stability gates first, then the
    Core Stability blend above. Existing Core members that still pass every
    hard gate get ``core_retention_bonus`` (retention preference) —
    deterministic, explainable. When no previous manager universe exists the
    return record notes the legacy-mode baseline explicitly.
    """
    result = SelectionResult()
    previous_core = previous_core or {}
    gate_errors: list[dict] = []

    # Core-only hard gates (Phase 2 §4.1 — stricter superset of the pipeline)
    passers: list[RotationCandidate] = []
    for c in candidates:
        problems: list[str] = []
        consistency_pct = (
            (c.trading_consistency * 100.0) if c.trading_consistency is not None else None
        )
        if consistency_pct is None or consistency_pct < cfg.core_trading_consistency_pct:
            problems.append(
                f"trading consistency {consistency_pct if consistency_pct is not None else 'N/A'}%"
                f" < {cfg.core_trading_consistency_pct}%"
            )
        if c.volume_cv is not None and c.volume_cv > cfg.core_volume_cv_max:
            problems.append(f"volume CV {c.volume_cv:.2f} > {cfg.core_volume_cv_max}")
        if c.median_atr_pct is None:
            problems.append("median %ATR unavailable")
        if problems:
            gate_errors.append({"symbol": c.symbol, "reasons": "; ".join(problems)})
            continue
        passers.append(c)

    result.core_rejections = gate_errors
    if not passers:
        result.core_shortfall = cfg.core_size
        return [], result

    # cross-sectional liquidity percentile among core passers (explicit pool)
    liq_values = {c.symbol: c.median_daily_value for c in passers}
    from app.services.rotation_scoring import percentile_ranks

    liq_ranks = percentile_ranks(liq_values)

    scored: list[tuple[float, RotationCandidate, Optional[str]]] = []
    for c in passers:
        stability = _core_stability(c, liq_ranks.get(c.symbol, 0.0), cfg)
        retention_reason = None
        if c.symbol in previous_core:
            retention_reason = "retained previous Core member (hard gates passed)"
            stability += cfg.core_retention_bonus
        scored.append((stability, c, retention_reason))

    scored.sort(key=lambda t: (-t[0], -t[1].data_quality_score, -t[1].median_daily_value, t[1].symbol))

    selected: list[CoreSelection] = []
    for rank, (stability, c, ret_reason) in enumerate(scored[: cfg.core_size], start=1):
        prev = previous_core.get(c.symbol) or {}
        selected.append(
            CoreSelection(
                symbol=c.symbol,
                stability_score=round(float(stability), 4),
                rank=rank,
                selection_reason=(
                    f"core stability rank {rank} (stability={stability:.3f}); "
                    f"rotation score {c.rotation_score:.2f}"
                ),
                retention_reason=ret_reason,
                previous_rank=prev.get("rank"),
                previous_score=prev.get("score"),
            )
        )
    result.core = selected
    result.core_shortfall = max(0, cfg.core_size - len(selected))
    return selected, result


# ---------------------------------------------------------------------------
# Rotation selection + anti-churn
# ---------------------------------------------------------------------------
def _cooldown_ok(
    candidate: RotationCandidate,
    cooldown_symbols: Optional[set],
    roster_low_score: float,
    cfg: RotationEngineConfig,
) -> tuple[bool, str]:
    """Cooldown rule: a previously-removed symbol may re-enter before the
    cooldown elapses only if it clears COOLDOWN_OVERRIDE_DELTA above the
    current roster's marginal (lowest) score."""
    if not cooldown_symbols or candidate.symbol not in cooldown_symbols:
        return True, ""
    if candidate.rotation_score - roster_low_score >= cfg.cooldown_override_delta:
        return True, f"cooldown override: +{candidate.rotation_score - roster_low_score:.2f} >= {cfg.cooldown_override_delta}"
    return False, (
        f"cooldown {cfg.replacement_cooldown_weeks}w not elapsed and margin "
        f"{candidate.rotation_score - roster_low_score:.2f} < override {cfg.cooldown_override_delta}"
    )


def select_rotation(
    candidates: list[RotationCandidate],
    cfg: RotationEngineConfig,
    previous_rotation: Optional[dict] = None,
    cooldown_symbols: Optional[set] = None,
) -> tuple[list[RotationSelection], SelectionResult]:
    """Select up to ``cfg.rotation_size`` anti-churn-adjusted Rotation members.

    Deterministic assembly (default-keep):
      1. every previous-rotation incumbent still eligible holds a slot
         (protected NORMAL/MONITOR first by previous rank, then REVIEW —
         weakest-first for fair displacement);
      2. hard-failed incumbents (missing from candidates) vacate → REMOVE;
      3. open slots (new universe / vacated slots) are filled by the top
         challengers (no cap, cooldown honored);
      4. REVIEW incumbents (2+ weak periods) are displaced only when a
         challenger beats them by ANTI_CHURN_SCORE_DELTA, within
         MAX_CHURN_PER_CYCLE, honoring cooldown; undisplaced REVIEW incumbents
         stay (default-keep);
      5. the final roster is capped at ``rotation_size`` deterministically.
    """
    result = SelectionResult()
    previous_rotation = previous_rotation or {}
    cooldown_symbols = cooldown_symbols or set()

    by_symbol = {c.symbol: c for c in candidates}
    ranked = sorted(candidates, key=rotation_sort_key)

    incumbents = [c for c in candidates if c.symbol in previous_rotation]
    protected: list[RotationCandidate] = []
    review: list[RotationCandidate] = []
    for c in sorted(
        incumbents,
        key=lambda c: (previous_rotation.get(c.symbol) or {}).get("rank") or 999,
    ):
        prev = previous_rotation.get(c.symbol) or {}
        prev_score = prev.get("score")
        weak_n, state = _weak_state(c.rotation_score, prev_score, cfg.rotation_retention_floor)
        if state == REVIEW:
            review.append(c)
        else:
            reason = (
                f"retained rotation incumbent ({state}"
                + (f", {weak_n} weak period(s))" if weak_n else ")")
            )
            protected.append(c)
            result.churn_decisions.append(
                {
                    "symbol": c.symbol,
                    "action": "KEEP",
                    "state": state,
                    "weak_periods": weak_n,
                    "reason": reason,
                }
            )

    # hard-failed incumbents: previously in rotation, no longer eligible
    # (missing from candidates) → emergency removal, slots vacated.
    hard_failed = [s for s in previous_rotation if s not in by_symbol]
    for s in sorted(hard_failed):
        result.churn_decisions.append(
            {"symbol": s, "action": "REMOVE", "reason": "emergency: lost eligibility this cycle"}
        )

    # default-keep: every eligible incumbent holds a slot (protected first by
    # previous rank, then REVIEW weakest-first)
    review_sorted = sorted(
        review, key=lambda c: (c.rotation_score, c.data_quality_score, c.symbol)
    )
    roster: list[RotationCandidate] = list(protected) + review_sorted
    used = {c.symbol for c in roster}
    open_slots = max(0, cfg.rotation_size - len(roster))

    def _roster_low_score() -> float:
        return min((c.rotation_score for c in roster), default=0.0)

    # pass 1 — open-slot fills (new universe / slacks after default-keep):
    # top challengers, no cap, cooldown honored.
    fills: list[RotationCandidate] = []
    for ch in ranked:
        if len(fills) >= open_slots:
            break
        if ch.symbol in used:
            continue
        ok, why = _cooldown_ok(ch, cooldown_symbols, _roster_low_score(), cfg)
        if not ok:
            continue
        fills.append(ch)
        used.add(ch.symbol)
        if hard_failed:
            result.churn_decisions.append(
                {
                    "symbol": ch.symbol,
                    "action": "EMERGENCY_FILL",
                    "reason": f"replaced hard-failed slot (cooldown: {why or 'n/a'})",
                }
            )
        else:
            result.churn_decisions.append(
                {
                    "symbol": ch.symbol,
                    "action": "FILL",
                    "reason": f"filled open slot (rank key top; cooldown: {why or 'n/a'})",
                }
            )

    # pass 2 — REVIEW displacement: weakest incumbent first, each takes the
    # best remaining challenger that clears the delta (cap + cooldown honored).
    replacements: dict = {}
    replaced_count = 0
    for inc in review_sorted:
        if replaced_count >= cfg.max_churn_per_cycle:
            break
        for ch in ranked:
            if ch.symbol in used:
                continue
            ok, why = _cooldown_ok(ch, cooldown_symbols, _roster_low_score(), cfg)
            if not ok:
                continue
            if ch.rotation_score - inc.rotation_score >= cfg.anti_churn_score_delta:
                replacements[inc.symbol] = ch
                used.add(ch.symbol)
                replaced_count += 1
                margin = ch.rotation_score - inc.rotation_score
                result.churn_decisions.append(
                    {
                        "symbol": ch.symbol,
                        "action": "REPLACE",
                        "replaced": inc.symbol,
                        "margin": round(margin, 2),
                        "reason": (
                            f"replaced REVIEW incumbent {inc.symbol} (margin "
                            f"{margin:.2f} >= delta {cfg.anti_churn_score_delta}; "
                            f"cooldown: {why or 'n/a'})"
                        ),
                    }
                )
                break

    # assemble the final roster deterministically, capped at rotation_size.
    final: list[RotationCandidate] = []
    for c in protected:
        final.append(c)
    for inc in review_sorted:
        if inc.symbol in replacements:
            final.append(replacements[inc.symbol])
        else:
            final.append(inc)
            result.churn_decisions.append(
                {
                    "symbol": inc.symbol,
                    "action": "KEEP",
                    "state": REVIEW,
                    "reason": "no challenger cleared the margin this cycle",
                }
            )
    final.extend(fills)
    final = final[: cfg.rotation_size]

    selections: list[RotationSelection] = []
    for rank, c in enumerate(final, start=1):
        p = previous_rotation.get(c.symbol) or {}
        if c.symbol in previous_rotation:
            _, state = _weak_state(c.rotation_score, p.get("score"), cfg.rotation_retention_floor)
        else:
            state = "NEW"
        selections.append(
            RotationSelection(
                symbol=c.symbol,
                rotation_score=c.rotation_score,
                rank=rank,
                selection_reason=(
                    f"rotation rank {rank} (score {c.rotation_score:.2f}, "
                    f"dq {c.data_quality_score:.2f}, liquidity {c.median_daily_value:.0f})"
                ),
                churn_state=state,
                previous_rank=p.get("rank"),
                previous_score=p.get("score"),
            )
        )
    result.rotation = selections
    result.rotation_shortfall = max(0, cfg.rotation_size - len(selections))
    return selections, result