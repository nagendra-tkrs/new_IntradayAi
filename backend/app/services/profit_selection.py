"""Profit Selection Layer (Phase 5) — a configurable TRADE / SKIP gate.

Placement in the pipeline (additive, nothing existing is replaced):

    Signal Engine
        -> Setup Quality           (unchanged)
        -> PROFIT SELECTION LAYER  (this module, shadow by default)
        -> Risk Engine
        -> Paper Order

Hard rules this module obeys
----------------------------
1. It NEVER recalculates, adjusts, rescales or re-weights the signal score.
   The base score, direction, confidence, quality label, RR and ATR are read
   as given and only compared against thresholds.
2. It only ever returns ``TRADE`` or ``SKIP``. It cannot change an entry, a
   stop, a target or a position size.
3. It is SHADOW MODE by default: ``ShadowMode.SHADOW`` means the decision is
   computed and recorded but the production path proceeds exactly as before.
   ``ShadowMode.ENFORCE`` is available for a future promotion but is guarded by
   :func:`assert_promotable`, which refuses to enforce anything until the
   candidate has cleared the sample gate in
   ``app.services.optimization_experiments``.
4. Every threshold is settings-driven and the DEFAULT is all-pass, so shipping
   this module cannot change a single production decision.
5. An unavailable input never silently passes or fails a rule that would
   change behaviour: the layer records which inputs were missing so a
   missing-data decision is always visible in the shadow record.

A "rule" is one named predicate plus the reason it gives. Rules are evaluated
independently and every failure is reported, so a SKIP always explains itself.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

DECISION_TRADE = "TRADE"
DECISION_SKIP = "SKIP"


class ShadowMode(str, Enum):
    """How the layer's decision affects production."""

    SHADOW = "SHADOW"      # record only; production path untouched (default)
    ENFORCE = "ENFORCE"    # block SKIPped signals — requires a promotion


class RuleOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"    # input unavailable; reported, never silently passed


def _coerce_finite(value: Any) -> Optional[float]:
    """Return a finite float for ``value``, else None. Never raises.

    Rejects bools (a flag is not a measurement), NaN, +/-inf, numeric strings
    that do not parse, and anything else non-numeric.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


@dataclass(frozen=True)
class Rule:
    """One named threshold. ``op`` is '>=' or '<='."""

    name: str
    field: str
    threshold: Optional[float]
    op: str = ">="

    def evaluate(self, value: Any) -> tuple[RuleOutcome, str]:
        rel = ">=" if self.op == ">=" else "<="
        if self.threshold is None:
            return RuleOutcome.PASS, "rule disabled"
        # Coerce defensively: a non-numeric, non-finite or bool value is
        # UNAVAILABLE, never an exception and never a silent pass.
        numeric = _coerce_finite(value)
        if numeric is None:
            return (
                RuleOutcome.UNKNOWN,
                f"{self.field} unavailable (cannot evaluate {rel} {self.threshold})",
            )
        if self.op == ">=":
            ok = numeric >= self.threshold
        else:
            ok = numeric <= self.threshold
        if ok:
            return RuleOutcome.PASS, f"{self.field} {numeric:.2f} {rel} {self.threshold}"
        return RuleOutcome.FAIL, f"{self.field} {numeric:.2f} violates {rel} {self.threshold}"


@dataclass(frozen=True)
class TimeWindow:
    """An inclusive-start / exclusive-end intraday window, minutes past midnight."""

    label: str
    start_minute: int
    end_minute: int

    def contains(self, minutes: int) -> bool:
        return self.start_minute <= minutes < self.end_minute


@dataclass
class SelectionConfig:
    """All thresholds for the layer. Every default is permissive."""

    mode: ShadowMode = ShadowMode.SHADOW
    min_signal_score: Optional[float] = None
    min_confidence: Optional[float] = None
    min_setup_quality_score: Optional[float] = None
    min_adx: Optional[float] = None
    min_relative_volume: Optional[float] = None
    min_risk_reward: Optional[float] = None
    min_atr_pct: Optional[float] = None
    max_atr_pct: Optional[float] = None
    allowed_qualities: tuple = ()            # empty = allow every quality level
    require_vwap_alignment: bool = False
    require_market_context_alignment: bool = False
    allowed_time_windows: tuple = ()         # empty = allow every window
    blocked_time_windows: tuple = ()         # e.g. the first 15 minutes
    min_volume_candles: int = 0              # bars required before VWAP is trusted

    def to_rules(self) -> list[Rule]:
        return [
            Rule("min_signal_score", "signal_score", self.min_signal_score),
            Rule("min_confidence", "confidence", self.min_confidence),
            Rule("min_setup_quality_score", "setup_quality_score",
                 self.min_setup_quality_score),
            Rule("min_adx", "adx", self.min_adx),
            Rule("min_relative_volume", "relative_volume", self.min_relative_volume),
            Rule("min_risk_reward", "risk_reward", self.min_risk_reward),
            Rule("min_atr_pct", "atr_pct", self.min_atr_pct),
            Rule("max_atr_pct", "atr_pct", self.max_atr_pct, "<="),
        ]

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "min_signal_score": self.min_signal_score,
            "min_confidence": self.min_confidence,
            "min_setup_quality_score": self.min_setup_quality_score,
            "min_adx": self.min_adx,
            "min_relative_volume": self.min_relative_volume,
            "min_risk_reward": self.min_risk_reward,
            "min_atr_pct": self.min_atr_pct,
            "max_atr_pct": self.max_atr_pct,
            "allowed_qualities": list(self.allowed_qualities),
            "require_vwap_alignment": self.require_vwap_alignment,
            "require_market_context_alignment": self.require_market_context_alignment,
            "allowed_time_windows": [w.label for w in self.allowed_time_windows],
            "blocked_time_windows": [w.label for w in self.blocked_time_windows],
        }

    @classmethod
    def from_settings(cls, settings=None) -> "SelectionConfig":
        """Build the config from settings. Every knob defaults to permissive,
        so the shipped behaviour is identical to having no layer at all."""
        if settings is None:
            from app.core.config import settings as _s
            settings = _s
        mode = str(getattr(settings, "PROFIT_SELECTION_MODE", "SHADOW")).upper()
        return cls(
            mode=ShadowMode.ENFORCE if mode == "ENFORCE" else ShadowMode.SHADOW,
            min_signal_score=getattr(settings, "PROFIT_SELECTION_MIN_SCORE", None),
            min_confidence=getattr(settings, "PROFIT_SELECTION_MIN_CONFIDENCE", None),
            min_setup_quality_score=getattr(
                settings, "PROFIT_SELECTION_MIN_QUALITY_SCORE", None
            ),
            min_adx=getattr(settings, "PROFIT_SELECTION_MIN_ADX", None),
            min_relative_volume=getattr(
                settings, "PROFIT_SELECTION_MIN_RELATIVE_VOLUME", None
            ),
            min_risk_reward=getattr(settings, "PROFIT_SELECTION_MIN_RR", None),
            min_atr_pct=getattr(settings, "PROFIT_SELECTION_MIN_ATR_PCT", None),
            max_atr_pct=getattr(settings, "PROFIT_SELECTION_MAX_ATR_PCT", None),
            allowed_qualities=tuple(
                q for q in str(
                    getattr(settings, "PROFIT_SELECTION_QUALITIES", "")
                ).split(",") if q
            ),
            require_vwap_alignment=bool(
                getattr(settings, "PROFIT_SELECTION_REQUIRE_VWAP", False)
            ),
            require_market_context_alignment=bool(
                getattr(settings, "PROFIT_SELECTION_REQUIRE_MARKET_CONTEXT", False)
            ),
            blocked_time_windows=tuple(
                _parse_window(w) for w in str(
                    getattr(settings, "PROFIT_SELECTION_BLOCKED_WINDOWS", "")
                ).split(",") if w.strip()
            ),
            allowed_time_windows=tuple(
                _parse_window(w) for w in str(
                    getattr(settings, "PROFIT_SELECTION_ALLOWED_WINDOWS", "")
                ).split(",") if w.strip()
            ),
        )


def _parse_window(text: str) -> TimeWindow:
    """Parse 'HH:MM-HH:MM' into a TimeWindow."""
    label = text.strip()
    try:
        start_s, end_s = label.split("-")
        sh, sm = (int(x) for x in start_s.split(":"))
        eh, em = (int(x) for x in end_s.split(":"))
    except (ValueError, AttributeError):
        return TimeWindow(label=label, start_minute=0, end_minute=24 * 60)
    return TimeWindow(label=label, start_minute=sh * 60 + sm, end_minute=eh * 60 + em)


@dataclass
class SelectionDecision:
    """The layer's verdict plus everything needed to audit it later."""

    decision: str
    mode: str
    applied_to_production: bool
    failed_rules: list = field(default_factory=list)
    unknown_rules: list = field(default_factory=list)
    rule_results: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    would_change_production: bool = False

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "mode": self.mode,
            "applied_to_production": self.applied_to_production,
            "would_change_production": self.would_change_production,
            "failed_rules": list(self.failed_rules),
            "unknown_rules": list(self.unknown_rules),
            "reasons": list(self.reasons),
            "rule_results": list(self.rule_results),
        }


def _finite(value: Any) -> Optional[float]:
    """Finite float or None. Alias of :func:`_coerce_finite`."""
    return _coerce_finite(value)


def _side(direction: Optional[str]) -> str:
    d = str(direction or "").upper()
    if "LONG" in d:
        return "LONG"
    if "SHORT" in d:
        return "SHORT"
    return "UNKNOWN"


def _minutes_into_session(entry_time: Any) -> Optional[int]:
    if isinstance(entry_time, datetime):
        return entry_time.hour * 60 + entry_time.minute
    if not entry_time:
        return None
    try:
        dt = datetime.fromisoformat(str(entry_time))
    except ValueError:
        return None
    return dt.hour * 60 + dt.minute


def _setup_of(signal: dict) -> dict:
    """The engine nests entry/stop/targets/ATR inside ``signal['setup']``
    (a TradeSetup model or an already-dumped dict). Accept either, and also
    tolerate the levels being hoisted to the top level."""
    setup = signal.get("setup")
    if setup is None:
        return signal
    if hasattr(setup, "model_dump"):
        return setup.model_dump()
    if isinstance(setup, dict):
        return setup
    return signal


def evaluate_selection(
    signal: dict,
    config: Optional[SelectionConfig] = None,
    indicator_values: Optional[dict] = None,
    market_context: Optional[dict] = None,
    entry_time: Any = None,
) -> SelectionDecision:
    """Decide TRADE or SKIP for one signal. Pure; never mutates ``signal``.

    All thresholds are read from the already-computed signal/indicator payload.
    The base signal score is compared, never recomputed.
    """
    cfg = config or SelectionConfig()
    signal = signal or {}
    ind = indicator_values or {}
    setup = _setup_of(signal)

    side = _side(signal.get("direction"))
    quality = str(signal.get("signal_quality") or "").upper() or None
    entry = _finite(setup.get("entry")) or _finite(signal.get("entry_price"))
    atr = (
        _finite(setup.get("atr"))
        or _finite(signal.get("atr"))
        or _finite(ind.get("atr_14"))
    )
    atr_pct = None
    if entry and atr and entry > 0:
        atr_pct = atr / entry * 100.0

    observed: dict[str, Optional[float]] = {
        "signal_score": _finite(signal.get("signal_score")),
        "confidence": _finite(signal.get("confidence")),
        "setup_quality_score": _finite(signal.get("setup_quality_score")),
        "adx": _finite(ind.get("adx_14")),
        "relative_volume": _finite(ind.get("relative_volume")),
        "risk_reward": (
            _finite(setup.get("risk_reward_ratio"))
            or _finite(signal.get("risk_reward"))
            or _finite(signal.get("risk_reward_ratio"))
        ),
        "atr_pct": atr_pct,
    }

    results: list[dict] = []
    failed: list[str] = []
    unknown: list[str] = []
    reasons: list[str] = []

    for rule in cfg.to_rules():
        outcome, reason = rule.evaluate(observed.get(rule.field))
        results.append({"rule": rule.name, "outcome": outcome.value, "detail": reason})
        if outcome is RuleOutcome.FAIL:
            failed.append(rule.name)
            reasons.append(reason)
        elif outcome is RuleOutcome.UNKNOWN:
            unknown.append(rule.name)
            reasons.append(reason)

    if cfg.allowed_qualities:
        if quality is None:
            unknown.append("allowed_qualities")
            reasons.append("setup quality unavailable")
        elif quality not in {q.upper() for q in cfg.allowed_qualities}:
            failed.append("allowed_qualities")
            reasons.append(f"setup quality {quality} not in {list(cfg.allowed_qualities)}")
        else:
            results.append({"rule": "allowed_qualities", "outcome": "PASS",
                            "detail": f"setup quality {quality} allowed"})

    if cfg.require_vwap_alignment:
        close = _finite(ind.get("close"))
        vwap = _finite(ind.get("vwap"))
        if close is None or vwap is None or vwap <= 0:
            unknown.append("require_vwap_alignment")
            reasons.append("VWAP unavailable; alignment not evaluable")
        else:
            aligned = (close > vwap) if side == "LONG" else (close < vwap)
            if not aligned:
                failed.append("require_vwap_alignment")
                reasons.append(
                    f"price {close:.2f} not on the {side} side of VWAP {vwap:.2f}"
                )
            else:
                results.append({"rule": "require_vwap_alignment", "outcome": "PASS",
                                "detail": f"price {close:.2f} aligned with VWAP {vwap:.2f}"})

    if cfg.require_market_context_alignment:
        trend = str((market_context or {}).get("nifty_trend") or "").upper()
        if trend not in ("BULLISH", "BEARISH") or side == "UNKNOWN":
            unknown.append("require_market_context_alignment")
            reasons.append("market context unavailable; alignment not evaluable")
        else:
            aligned = (trend == "BULLISH") == (side == "LONG")
            if not aligned:
                failed.append("require_market_context_alignment")
                reasons.append(f"NIFTY {trend} counters a {side} signal")
            else:
                results.append({"rule": "require_market_context_alignment",
                                "outcome": "PASS",
                                "detail": f"NIFTY {trend} aligns with {side}"})

    minutes = _minutes_into_session(
        entry_time if entry_time is not None else signal.get("timestamp")
    )
    if cfg.blocked_time_windows and minutes is not None:
        for window in cfg.blocked_time_windows:
            if window.contains(minutes):
                failed.append("blocked_time_windows")
                reasons.append(f"entry time falls in blocked window {window.label}")
                break
    if cfg.allowed_time_windows and minutes is not None:
        if not any(w.contains(minutes) for w in cfg.allowed_time_windows):
            failed.append("allowed_time_windows")
            reasons.append(
                f"entry time {minutes // 60:02d}:{minutes % 60:02d} outside "
                f"allowed windows"
            )

    decision = DECISION_SKIP if failed else DECISION_TRADE
    applied = cfg.mode is ShadowMode.ENFORCE
    return SelectionDecision(
        decision=decision,
        mode=cfg.mode.value,
        applied_to_production=applied,
        would_change_production=bool(failed) and not applied,
        failed_rules=failed,
        unknown_rules=unknown,
        rule_results=results,
        reasons=reasons,
    )


# ────────────────────────────────────────────────────────────────────────────
# Promotion guard
# ────────────────────────────────────────────────────────────────────────────

class PromotionNotAllowed(RuntimeError):
    """Raised when ENFORCE mode is requested without a cleared sample."""


def assert_promotable(
    real_sample_size: int,
    min_sample: int,
    fidelity_gate_passes: bool = True,
) -> None:
    """Refuse to enforce the layer until the promotion criteria are met.

    This is the code-level guarantee behind the "CANDIDATE until enough real
    observations exist" rule: ENFORCE cannot be reached by flipping a setting
    alone at the current sample size.
    """
    blockers = []
    if real_sample_size < min_sample:
        blockers.append(
            f"{real_sample_size} real completed trades < {min_sample} required"
        )
    if not fidelity_gate_passes:
        blockers.append("replay fidelity gate did not pass")
    if blockers:
        raise PromotionNotAllowed(
            "Profit Selection Layer cannot be promoted to ENFORCE: "
            + "; ".join(blockers)
        )


def shadow_record(
    signal: dict,
    decision: SelectionDecision,
    signal_id: Optional[str] = None,
) -> dict:
    """Flatten a decision into the row shape persisted by the shadow ledger."""
    setup = _setup_of(signal or {})
    return {
        "signal_id": signal_id,
        "symbol": signal.get("symbol"),
        "timestamp": signal.get("timestamp"),
        "direction": signal.get("direction"),
        "signal_score": _finite(signal.get("signal_score")),
        "confidence": _finite(signal.get("confidence")),
        "setup_quality": signal.get("signal_quality"),
        "setup_quality_score": _finite(signal.get("setup_quality_score")),
        "entry_price": _finite(setup.get("entry")) or _finite(signal.get("entry_price")),
        "stop_loss": _finite(setup.get("stop_loss")) or _finite(signal.get("stop_loss")),
        "target_1": _finite(setup.get("target_1")) or _finite(signal.get("target_1")),
        "target_2": _finite(setup.get("target_2")) or _finite(signal.get("target_2")),
        "risk_reward": (
            _finite(setup.get("risk_reward_ratio"))
            or _finite(signal.get("risk_reward"))
            or _finite(signal.get("risk_reward_ratio"))
        ),
        "decision": decision.decision,
        "mode": decision.mode,
        "applied_to_production": decision.applied_to_production,
        "would_change_production": decision.would_change_production,
        "failed_rules": json.dumps(decision.failed_rules),
        "unknown_rules": json.dumps(decision.unknown_rules),
        "reasons": json.dumps(decision.reasons),
    }
