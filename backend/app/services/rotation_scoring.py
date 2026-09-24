"""Rotation engine — Rotation Score components + normalization (Phase 4).

Implements Phase 2 §6 exactly: seven components, weights fixed in
``universe_models.ROTATION_SCORE_COMPONENT_WEIGHTS`` (20/15/15/10/15/15/10),
each component normalized to its own ``[0, weight]`` range, total bounded to
``[0, 100]``.

Normalization policy (Phase 2 §6 "Common normalization policy [PROPOSED]"):

* Liquidity / Volume Quality / Signal Frequency / LONG Quality / SHORT Quality
  → cross-sectional within the current cycle's SURVIVING candidate pool. The
  population is always passed in explicitly keyed by symbol — there is no
  hidden or uncontrolled reference population.
* Volatility / ATR → absolute trapezoid (no population).
* Data Quality → absolute rate mapping (no population) — garbage cycles cannot
  re-normalize to 100.

Robustness guarantees (Phase 4 §16): no division by zero, no NaN/inf
propagation (values are cleaned before normalization), bounded output,
deterministic for identical input. Missing values are never converted into a
strong score (a symbol without a raw metric simply earns no points).
"""
from __future__ import annotations

import math
from typing import Optional

from app.models.universe_models import ROTATION_SCORE_COMPONENT_WEIGHTS
from app.services.rotation_config import RotationEngineConfig

WEIGHTS = dict(ROTATION_SCORE_COMPONENT_WEIGHTS)
WEIGHT_TOTAL = sum(WEIGHTS.values())  # 100


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator is None or math.isnan(denominator) or math.isinf(denominator):
        return default
    if abs(denominator) <= 1e-12:
        return default
    return numerator / denominator


def _clean_values(values: dict[str, float]) -> dict[str, float]:
    """Drop None / NaN / ±inf entries; keep the rest (deterministic keys)."""
    out: dict[str, float] = {}
    for k, v in values.items():
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or math.isinf(f):
            continue
        out[k] = f
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-12 * max(1.0, abs(a), abs(b))


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """Percentile rank (0..1) within the EXPLICIT population ``values``.

    Ties share the same rank (midpoint of the tied band); the top symbol gets
    1.0 and the bottom gets 0.0. Empty/clean-empty population → {}.
    """
    cleaned = _clean_values(values)
    if not cleaned:
        return {}
    ordered = sorted(cleaned.items(), key=lambda kv: (kv[1], kv[0]))
    n = len(ordered)
    ranks: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and _close(ordered[j + 1][1], ordered[i][1]):
            j += 1
        p = ((i + j) / 2.0) / (n - 1) if n > 1 else 0.5
        for k in range(i, j + 1):
            ranks[ordered[k][0]] = p
        i = j + 1
    return ranks


def minmax_normalize(values: dict[str, float]) -> dict[str, float]:
    """Min–max normalize (0..1). All-equal population → neutral 0.5 (no
    division-by-zero). Empty population → {}."""
    cleaned = _clean_values(values)
    if not cleaned:
        return {}
    lo = min(cleaned.values())
    hi = max(cleaned.values())
    if _close(hi, lo):
        return {k: 0.5 for k in cleaned}
    return {k: (v - lo) / (hi - lo) for k, v in cleaned.items()}


def clamp(value: float, low: float, high: float) -> float:
    if value is None or math.isnan(value) or math.isinf(value):
        return low
    return max(low, min(high, value))


def trapezoid(x: float, a: float, opt_low: float, opt_high: float, d: float) -> float:
    """Rises 0→1 on [a, opt_low], flat at 1 on [opt_low, opt_high], falls to
    0 at d. Zero outside the band; degenerate segments collapse to 1."""
    if x is None or math.isnan(x) or math.isinf(x):
        return 0.0
    if x <= a or x >= d:
        return 0.0
    if x <= opt_low:
        return 1.0 if _close(opt_low, a) else clamp((x - a) / (opt_low - a), 0.0, 1.0)
    if x <= opt_high:
        return 1.0
    return 0.0 if _close(d, opt_high) else clamp((d - x) / (d - opt_high), 0.0, 1.0)


# ---------------------------------------------------------------------------
# component scorers — each returns {symbol: score in [0, weight]}
# ---------------------------------------------------------------------------
def liquidity_scores(median_daily_values: dict[str, float], weight: float = WEIGHTS["liquidity_score"]) -> dict[str, float]:
    """F1 — percentile rank within pool × weight (resistant to one outlier)."""
    ranks = percentile_ranks(median_daily_values)
    return {s: round(clamp(r * weight, 0.0, weight), 4) for s, r in ranks.items()}


def volume_quality_scores(
    mean_volumes: dict[str, float],
    volume_cvs: dict[str, Optional[float]],
    weight: float = WEIGHTS["volume_quality_score"],
) -> dict[str, float]:
    """F2 — blend of normalized mean (~60%) + inverse-CV (~40%)."""
    mean_norm = minmax_normalize(mean_volumes)
    invcv = {s: (1.0 / (1.0 + c)) if c is not None else 0.0 for s, c in volume_cvs.items()}
    invcv_norm = minmax_normalize(invcv)
    out = {}
    for s in mean_norm:
        v = 0.6 * mean_norm.get(s, 0.0) + 0.4 * invcv_norm.get(s, 0.0)
        out[s] = round(clamp(v * weight, 0.0, weight), 4)
    return out


def volatility_scores(
    atr_pcts: dict[str, Optional[float]],
    cfg: RotationEngineConfig,
    weight: float = WEIGHTS["volatility_score"],
) -> dict[str, float]:
    """F3 — absolute trapezoid over median %ATR (band edges are hard at S5)."""
    out = {}
    for s, p in atr_pcts.items():
        if p is None:
            out[s] = 0.0
            continue
        f = trapezoid(
            p,
            cfg.atr_min_pct,
            cfg.atr_opt_low_pct,
            cfg.atr_opt_high_pct,
            cfg.atr_max_pct,
        )
        out[s] = round(clamp(f * weight, 0.0, weight), 4)
    return out


def signal_frequency_scores(
    frequencies: dict[str, Optional[float]],
    weight: float = WEIGHTS["signal_frequency_score"],
) -> dict[str, float]:
    """F4 — cross-sectional min–max of signals/valid-sessions; 0 signals → 0."""
    cleaned = {s: (f if f is not None else 0.0) for s, f in frequencies.items()}
    norm = minmax_normalize(cleaned)
    return {s: round(clamp(n * weight, 0.0, weight), 4) for s, n in norm.items()}


def directional_quality_scores(
    raws: dict[str, Optional[float]],
    sample_ok: dict[str, bool],
    weight: float,
) -> dict[str, float]:
    """F5/F6 — cross-sectional percentile of the directional raw blend for
    symbols with a sufficient sample; INSUFFICIENT_SAMPLE symbols earn 0 with
    the flag recorded by the engine (never a fabricated estimate, Phase 2 §8)."""
    scorable = {s: r for s, r in raws.items() if sample_ok.get(s) and r is not None}
    ranks = percentile_ranks(scorable)
    out: dict[str, float] = {}
    for s in raws:
        if s in ranks:
            out[s] = round(clamp(ranks[s] * weight, 0.0, weight), 4)
        else:
            out[s] = 0.0
    return out


def long_quality_scores(
    raws: dict[str, Optional[float]],
    sample_ok: dict[str, bool],
    weight: float = WEIGHTS["long_quality_score"],
) -> dict[str, float]:
    return directional_quality_scores(raws, sample_ok, weight)


def short_quality_scores(
    raws: dict[str, Optional[float]],
    sample_ok: dict[str, bool],
    weight: float = WEIGHTS["short_quality_score"],
) -> dict[str, float]:
    return directional_quality_scores(raws, sample_ok, weight)


def data_quality_scores(
    dq_scores: dict[str, Optional[float]],
    weight: float = WEIGHTS["data_quality_score"],
) -> dict[str, float]:
    """F7 — ABSOLUTE: the eligibility data-quality score is already 0..10; no
    cross-sectional re-normalization (Phase 2 §6 — a bad-data cycle must lower
    everyone, not re-normalize garbage to 100)."""
    return {s: round(clamp(dq_scores.get(s) or 0.0, 0.0, weight), 4) for s in dq_scores}


def rotation_score(components: dict[str, float]) -> float:
    """Total Rotation Score = Σ(component), each already in its [0, weight]
    range; bounded [0, 100] and reproducible."""
    total = 0.0
    for comp, weight in WEIGHTS.items():
        v = components.get(comp, 0.0)
        total += clamp(v, 0.0, weight)
    return round(clamp(total, 0.0, float(WEIGHT_TOTAL)), 4)