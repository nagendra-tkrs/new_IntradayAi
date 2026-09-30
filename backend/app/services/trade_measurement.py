"""Realized-trade measurement for properly-captured risk-engine trades.

**READ-ONLY and measurement-only.** This module reads the realized paper ledger
(``trades``), the signal ledger (``signals``) and the entry-geometry ledger
(``position_risk_geometry``) and reduces them to a per-trade measurement record
plus the performance statistics the optimization program needs. It never
mutates a row, never invents an observation, and never books a counterfactual
as realized performance.

What makes this different from :mod:`app.services.profit_baseline`
-----------------------------------------------------------------------
``profit_baseline`` reads risk off the ``trades`` row, whose ``stop_loss``
column is the **live** stop at exit time - Profit Capture moves it to breakeven
and then trails it. Any risk multiple derived from that number describes the
exit, not the entry.

This module instead reads the **frozen entry-time geometry**, which is written
once at fill and never updated, and joins it to the realized outcome. So
"initial risk" here always means the risk the position was *opened* with, and
"final stop loss" is reported separately as the live stop the position actually
carried at exit. That is the whole point of the measurement: it is the only
place both numbers are visible side by side.

Two populations are kept strictly apart
---------------------------------------
``REALIZED`` and ``SHADOW``. ``position_risk_geometry`` also stores a shadow
size (what the risk engine *would* have used). Those columns are read only to
report the shadow side-by-side and are **never** summed into realized P&L, a
win rate, an expectancy or any other performance number.

Data-quality contract
---------------------
A missing observation is reported as ``UNKNOWN`` (the attribute is absent) or
``NOT_AVAILABLE`` (it was never captured). A missing observation is never turned
into ``0`` unless zero is the measured value - in particular a position with no
geometry row is ``NOT_AVAILABLE`` for risk, never "zero risk".
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from app.services.profit_baseline import (
    COHORT_AI_LINKED,
    COHORT_STRATEGY_UNLINKED,
    COHORT_TEST_FIXTURE,
    INSUFFICIENT_DATA,
    MIN_TRADES_FOR_CONCLUSION,
    MIN_HOLDING_SECONDS_FOR_STRATEGY_COHORT,
    Position,
    collapse_positions,
    compute_stats,
)

#: The attribute is simply not present on this record.
UNKNOWN = "UNKNOWN"
#: The attribute exists in principle but was never captured for this position.
NOT_AVAILABLE = "NOT_AVAILABLE"

#: Only these two sizing modes exist; they are the same strings the ledger
#: records as ``quantity_source`` / geometry ``quantity_source``.
SIZING_RISK_ENGINE = "RISK_ENGINE"
SIZING_MANUAL = "MANUAL"

PROMOTION_SUFFICIENT = "SUFFICIENT DATA FOR ANALYSIS"
PROMOTION_INSUFFICIENT = "INSUFFICIENT REALIZED DATA"


def _finite(value: Any) -> Optional[float]:
    """Finite float or None. Never raises, never fabricates, never coerces."""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _text(value: Any) -> str:
    """A required string attribute; absent becomes ``UNKNOWN``, never ''."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return UNKNOWN
    return str(value).strip()


def _opt(value: Any) -> Optional[float]:
    """A required numeric attribute; absent becomes ``NOT_AVAILABLE``.

    Deliberately not 0.0: a missing price or a missing risk is an absence of
    measurement, and reporting it as zero would silently corrupt every mean it
    enters.
    """
    v = _finite(value)
    return v if v is not None else NOT_AVAILABLE


def _r(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(value, digits)


def _median(values: Iterable[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _mean(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _details_of(row: Optional[dict]) -> dict:
    """Parse ``trades.details_json`` into a dict; anything unusable becomes {}."""
    if not row:
        return {}
    raw = row.get("details_json")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _flag(value: Optional[bool]) -> Any:
    """Yes/no attribute; genuinely unknown stays NOT_AVAILABLE."""
    if value is None:
        return NOT_AVAILABLE
    return "YES" if value else "NO"


# ────────────────────────────────────────────────────────────────────────────
# One measured trade
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class MeasuredTrade:
    """One collapsed realized position joined to its frozen entry geometry."""

    position: Position
    geometry: Optional[dict] = None
    trailing_activated: Optional[bool] = None
    t1_reached: Optional[bool] = None

    # ── capture status ────────────────────────────────────────────────────

    @property
    def position_id(self) -> str:
        return self.position.position_id

    @property
    def has_geometry(self) -> bool:
        return bool(self.geometry)

    @property
    def geometry_reliable(self) -> bool:
        if not self.geometry:
            return False
        return bool(self.geometry.get("geometry_reliable"))

    @property
    def properly_captured(self) -> bool:
        """A trade qualifies for the measured cohort only when its entry-time
        geometry was captured and marked reliable. Anything else is reported
        separately with a reason, never quietly folded in."""
        return self.has_geometry and self.geometry_reliable

    @property
    def exclusion_reason(self) -> Optional[str]:
        if self.properly_captured:
            return None
        if not self.has_geometry:
            return "NO_GEOMETRY_ROW"
        if _finite(self.geometry.get("initial_risk_per_share")) in (None, 0.0):
            return "GEOMETRY_UNUSABLE"
        return "GEOMETRY_MARKED_UNRELIABLE"

    # ── sizing ────────────────────────────────────────────────────────────

    @property
    def sizing_mode(self) -> Optional[str]:
        """The sizing mode actually used, read from the frozen geometry.

        Falls back to the trade row's ``details_json.quantity_source`` (written
        by the close path) so a row still reports its own provenance even if the
        geometry is absent.
        """
        if self.geometry:
            src = self.geometry.get("quantity_source")
            if src:
                return str(src).strip().upper()
        return None

    @property
    def sizing_mode_or_unknown(self) -> str:
        return self.sizing_mode or NOT_AVAILABLE

    @property
    def quantity(self) -> Optional[float]:
        q = _finite(self.geometry.get("actual_quantity")) if self.geometry else None
        if q is None and self.geometry:
            q = _finite(self.geometry.get("initial_quantity"))
        return q

    @property
    def initial_risk_per_share(self) -> Optional[float]:
        return _finite(self.geometry.get("initial_risk_per_share")) if self.geometry else None

    @property
    def initial_risk(self) -> Optional[float]:
        """Entry-time rupee risk: frozen risk/share × the filled quantity.

        Prefers the ledger's own ``initial_risk_amount`` and only recomputes
        when that column is absent, so a measurement never disagrees with the
        number captured at fill.
        """
        if not self.geometry:
            return None
        recorded = _finite(self.geometry.get("initial_risk_amount"))
        if recorded is not None:
            return recorded
        rps = self.initial_risk_per_share
        qty = self.quantity
        if rps is None or qty is None:
            return None
        return rps * qty

    @property
    def risk_utilization_percent(self) -> Optional[float]:
        if not self.geometry:
            return None
        recorded = _finite(self.geometry.get("risk_budget_utilization"))
        if recorded is not None:
            return recorded
        budget = _finite(self.geometry.get("risk_budget"))
        risk = self.initial_risk
        if budget and risk is not None and budget > 0:
            return risk / budget * 100.0
        return None

    @property
    def capital_utilization_percent(self) -> Optional[float]:
        """Exposure as a share of account capital.

        Derived from ``current_exposure / account_capital`` - the two columns
        captured at fill. NOT_AVAILABLE rather than 0 when capital is unknown.
        """
        if not self.geometry:
            return None
        capital = _finite(self.geometry.get("account_capital"))
        exposure = _finite(self.geometry.get("current_exposure"))
        if exposure is None:
            entry = _finite(self.geometry.get("entry_price"))
            qty = self.quantity
            exposure = entry * qty if (entry is not None and qty is not None) else None
        if exposure is None or not capital or capital <= 0:
            return None
        return exposure / capital * 100.0

    # ── normalized (exposure-independent) performance ─────────────────────

    @property
    def r_multiple(self) -> Optional[float]:
        """Realized P&L in units of the risk actually taken at entry.

        This is the exposure-normalized figure: it is what makes a
        RISK_ENGINE trade and a MANUAL trade of very different size comparable.
        It is deliberately NOT ``Position.r_multiple``, which uses the trade
        row's *mutated* stop.
        """
        risk = self.initial_risk
        if not risk or risk <= 0:
            return None
        return self.position.pnl / risk

    @property
    def capital_exposure(self) -> Optional[float]:
        """Buying power this position actually occupied, from the frozen row."""
        if not self.geometry:
            return None
        exposure = _finite(self.geometry.get("current_exposure"))
        if exposure is not None:
            return exposure
        entry = _finite(self.geometry.get("entry_price"))
        qty = self.quantity
        return entry * qty if (entry is not None and qty is not None) else None

    @property
    def return_on_capital_percent(self) -> Optional[float]:
        """Realized P&L as a percentage of ACCOUNT capital.

        This is the exposure-normalized companion to the R multiple. The
        denominator is always the *account*, never the position's own exposure:
        a return measured against the capital a single position happened to
        occupy would be larger for a smaller position, so two trades of
        different size would not be comparable.

        ``NOT_AVAILABLE`` when ``account_capital`` was never captured. It is
        deliberately NOT derived from ``current_exposure``: utilization is a
        fraction OF capital, so without capital there is no denominator to
        reconstruct - and inventing one would produce a confident wrong number.
        """
        if not self.geometry:
            return None
        pnl = _finite(self.position.pnl)
        capital = _finite(self.geometry.get("account_capital"))
        if pnl is None or not capital or capital <= 0:
            return None
        return pnl / capital * 100.0

    @property
    def return_on_exposure_percent(self) -> Optional[float]:
        """Realized P&L as a percentage of the capital THIS position occupied.

        Reported alongside, never instead of, ``return_on_capital_percent``.
        It answers a different question ("how did this position do on the money
        it tied up?") and is NOT comparable across positions of different size,
        so nothing in the report treats it as a performance figure.
        """
        pnl = _finite(self.position.pnl)
        exposure = self.capital_exposure
        if pnl is None or not exposure or exposure <= 0:
            return None
        return pnl / exposure * 100.0

    @property
    def cohort(self) -> str:
        return self.position.cohort()

    # ── the section 7 record ──────────────────────────────────────────────

    def to_dict(self) -> dict:
        pos = self.position
        geo = self.geometry or {}
        sig = pos.signal or {}

        return {
            # A. trade identity
            "trade_id": _text(pos.row_ids[-1] if pos.row_ids else None),
            "trade_ids": list(pos.row_ids),
            "signal_id": _text(pos.signal_id),
            "position_id": _text(pos.position_id),
            "symbol": _text(pos.symbol),
            "strategy_version": _text(pos.strategy_version),
            "ai_linked": pos.is_ai_linked,
            "cohort": self.cohort,
            "properly_captured": self.properly_captured,
            "exclusion_reason": self.exclusion_reason,

            # B. signal
            "signal": {
                "direction": _text(pos.direction or pos.side),
                "score": _opt(self._signal_score(sig)),
                "confidence": _opt(sig.get("confidence")),
                "setup_quality": _text(sig.get("signal_quality")).upper()
                if sig.get("signal_quality") else NOT_AVAILABLE,
                "signal_timestamp": _text(sig.get("timestamp")),
                "risk_reward_at_signal": _opt(sig.get("risk_reward")),
                "atr": _opt(sig.get("atr")),
            },

            # C. entry geometry (frozen at fill)
            "entry_geometry": {
                "entry_price": _opt(geo.get("entry_price", pos.entry_price)),
                "initial_stop_loss": _opt(geo.get("initial_stop_loss")),
                "initial_target_1": _opt(geo.get("initial_target_1")),
                "initial_target_2": _opt(geo.get("initial_target_2")),
                "risk_per_share": _opt(self.initial_risk_per_share),
                "quantity": _opt(self.quantity),
                "initial_risk": _opt(self.initial_risk),
                "configured_risk_percent": _opt(geo.get("configured_risk_percent")),
                "captured_at": _text(geo.get("captured_at")),
                "capture_source": _text(geo.get("capture_source")),
                "risk_reward_at_entry": _opt(self._entry_rr()),
            },

            # D. sizing
            "sizing": {
                "sizing_mode": self.sizing_mode_or_unknown,
                "risk_budget": _opt(geo.get("risk_budget")),
                "risk_constraint_quantity": _opt(geo.get("risk_constraint_quantity")),
                "capital_constraint_quantity": _opt(geo.get("capital_constraint_quantity")),
                "allowed_quantity": _opt(geo.get("allowed_quantity")),
                "actual_quantity": _opt(self.quantity),
                "binding_constraint": _text(geo.get("binding_constraint")).upper()
                if geo.get("binding_constraint") else NOT_AVAILABLE,
                "order_classification": _text(geo.get("order_classification")).upper()
                if geo.get("order_classification") else NOT_AVAILABLE,
                "risk_utilization_percent": _opt(self.risk_utilization_percent),
                "capital_utilization_percent": _opt(self.capital_utilization_percent),
                "intended_risk": _opt(geo.get("intended_risk")),
                "actual_risk": _opt(geo.get("actual_risk")),
                "account_capital": _opt(geo.get("account_capital")),
                "exposure": _opt(geo.get("current_exposure")),
            },

            # E. exit
            "exit": {
                "exit_price": _opt(pos.exit_price),
                "exit_reason": _text(pos.terminal_reason).upper()
                if pos.terminal_reason else NOT_AVAILABLE,
                "exit_reasons": list(pos.exit_reasons) or [NOT_AVAILABLE],
                "exit_time": _text(pos.exit_time.isoformat() if pos.exit_time else None),
                "holding_minutes": _r(pos.holding_minutes, 2),
                "t1_reached": _flag(self.t1_reached),
                "trailing_activated": _flag(self.trailing_activated),
                # The live stop the position carried at exit - Profit Capture
                # has moved it by now. Reported separately from the frozen
                # entry-time stop in section C precisely so the two are never
                # confused.
                "final_stop_loss": _opt(pos.stop_loss),
                "stop_moved_from_entry": self._stop_moved(),
                "realized_pnl": _r(pos.pnl, 2),
                "r_multiple": _opt(self.r_multiple),
                "return_on_capital_percent": _opt(self.return_on_capital_percent),
                # Measured on the position's own exposure, NOT the account - a
                # different question, and not a performance figure.
                "return_on_exposure_percent": _opt(self.return_on_exposure_percent),
            },

            # SHADOW, kept strictly out of every realized figure above.
            "shadow_reference_only": {
                "shadow_quantity": _opt(geo.get("shadow_quantity")),
                "shadow_exposure": _opt(geo.get("shadow_exposure")),
                "shadow_initial_risk": _opt(geo.get("shadow_initial_risk")),
                "shadow_utilization_percent": _opt(geo.get("shadow_utilization")),
                "booked_as_realized_pnl": False,
            },
        }

    # ── internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _signal_score(signal: dict) -> Optional[float]:
        """``signals.signal_score`` is a scalar on some rows and a nested score
        object on others; accept both and take the numeric total. Returns None
        (→ ``NOT_AVAILABLE``) when neither shape yields a number, rather than
        inventing a score."""
        raw = signal.get("signal_score")
        direct = _finite(raw)
        if direct is not None:
            return direct
        payload = raw
        if isinstance(payload, str):
            try:
                payload = _json.loads(payload)
            except (TypeError, ValueError):
                return None
        if isinstance(payload, dict):
            for key in ("total", "score", "value"):
                v = _finite(payload.get(key))
                if v is not None:
                    return v
        return None

    def _entry_rr(self) -> Optional[float]:
        """Reward:risk measured from the FROZEN entry-time levels."""
        if not self.geometry:
            return None
        rps = self.initial_risk_per_share
        entry = _finite(self.geometry.get("entry_price"))
        t1 = _finite(self.geometry.get("initial_target_1"))
        if not rps or rps <= 0 or entry is None or t1 is None:
            return None
        return abs(t1 - entry) / rps

    def _stop_moved(self) -> str:
        """Whether Profit Capture moved the live stop off the entry-time stop."""
        if not self.geometry:
            return NOT_AVAILABLE
        initial = _finite(self.geometry.get("initial_stop_loss"))
        final = _finite(self.position.stop_loss)
        if initial is None or final is None:
            return NOT_AVAILABLE
        if abs(final - initial) < 1e-9:
            return "NO"
        direction = self.position.side
        moved_favourably = (
            (final > initial) if direction == "LONG" else (final < initial)
        ) if direction != UNKNOWN else None
        if moved_favourably is None:
            return "YES" if abs(final - initial) >= 1e-9 else "NO"
        return "YES_TOWARDS_ENTRY" if not moved_favourably else "YES_FAVOURABLE"


def build_measured_trades(
    trade_rows: Iterable[dict],
    signals_by_id: Optional[dict] = None,
    geometry_by_position: Optional[dict] = None,
    min_holding_seconds: float = MIN_HOLDING_SECONDS_FOR_STRATEGY_COHORT,
) -> list[MeasuredTrade]:
    """Collapse realized ledger rows to positions and join frozen geometry.

    ``geometry_by_position`` maps ``position_id`` to a
    ``position_risk_geometry`` row. A position with no entry there is returned
    too - flagged as not properly captured - so the reader can see the size of
    the gap instead of silently measuring a smaller, healthier-looking sample.
    """
    rows = [r for r in trade_rows if str(r.get("status") or "closed") == "closed"]
    positions: list[Position] = collapse_positions(rows, signals_by_id, min_holding_seconds)
    geometry = geometry_by_position or {}
    rows_by_id = {str(r.get("id")): r for r in rows}

    out: list[MeasuredTrade] = []
    for pos in positions:
        geo = geometry.get(pos.position_id)
        trailing: Optional[bool] = None
        t1: Optional[bool] = None
        for row_id in pos.row_ids:
            det = _details_of(rows_by_id.get(row_id))
            if det.get("trailing_active") is not None:
                trailing = bool(trailing) or bool(det.get("trailing_active"))
            if det.get("t1_exit_quantity") or det.get("t1_realized_pnl") is not None:
                t1 = True
        if t1 is None and pos.exit_reasons:
            t1 = any(
                r in ("T1_PARTIAL", "T2_FINAL", "TRAILING_STOP", "TARGET_1")
                for r in pos.exit_reasons
            )
        if trailing is None and pos.terminal_reason == "TRAILING_STOP":
            trailing = True
        out.append(MeasuredTrade(position=pos, geometry=geo, trailing_activated=trailing,
                                 t1_reached=t1))
    return out


# ────────────────────────────────────────────────────────────────────────────
# Performance metrics (section 8)
# ────────────────────────────────────────────────────────────────────────────


def performance_metrics(trades: list[MeasuredTrade]) -> dict:
    """Realized-only statistics for one set of measured trades.

    Built on :func:`profit_baseline.compute_stats` for the ledger-derived
    figures so there is exactly one definition of win rate / expectancy /
    profit factor, and adds the exposure statistics that only a frozen-geometry
    join can produce (risk utilization, capital utilization, R multiples).
    """
    stats = dict(compute_stats([t.position for t in trades]))
    total = stats["total_completed_trades"]
    pnls = [t.position.pnl for t in trades]

    holdings = [t.position.holding_minutes for t in trades]
    stats["average_pnl_per_trade"] = _r(sum(pnls) / total, 4) if total else None
    stats["median_holding_minutes"] = _r(_median(holdings), 2)
    stats["median_pnl"] = _r(_median(pnls), 2)

    stats["risk_utilization_percent_mean"] = _r(
        _mean(t.risk_utilization_percent for t in trades), 2
    )
    stats["capital_utilization_percent_mean"] = _r(
        _mean(t.capital_utilization_percent for t in trades), 2
    )
    stats["initial_risk_mean"] = _r(_mean(t.initial_risk for t in trades), 2)
    observed_risk = [v for v in (t.initial_risk for t in trades) if v is not None]
    observed_util = [
        v for v in (t.capital_utilization_percent for t in trades) if v is not None
    ]
    # A total over an empty observation set is NOT_AVAILABLE, not 0.0: "no
    # measurements" and "the measured risk was zero" are different facts, and
    # conflating them is exactly what this report exists to prevent.
    stats["initial_risk_total"] = _r(sum(observed_risk), 2) if observed_risk else None
    stats["initial_risk_observed"] = len(observed_risk)
    stats["exposure_total_percent"] = _r(sum(observed_util), 2) if observed_util else None
    stats["exposure_observed"] = len(observed_util)
    stats["r_multiple_mean"] = _r(_mean(t.r_multiple for t in trades), 4)
    stats["r_multiple_median"] = _r(_median([t.r_multiple for t in trades]), 4)
    stats["return_on_capital_percent_mean"] = _r(
        _mean(t.return_on_capital_percent for t in trades), 4
    )

    observed_r = [t.r_multiple for t in trades if t.r_multiple is not None]
    stats["r_multiple_observed"] = len(observed_r)
    stats["r_multiple_missing"] = total - len(observed_r)
    stats["r_multiple_missing_status"] = (
        NOT_AVAILABLE if total and not observed_r else "OK"
    )
    return stats


def _grouped(
    trades: list[MeasuredTrade], key: Callable[[MeasuredTrade], Optional[str]]
) -> dict:
    """Group by ``key(trade) -> label``; ``None`` is collected under
    ``NOT_AVAILABLE`` rather than dropped, so an unattributable trade is
    visible instead of silently shrinking the sample."""
    buckets: dict[str, list[MeasuredTrade]] = {}
    for t in trades:
        label = key(t)
        buckets.setdefault(str(label) if label else NOT_AVAILABLE, []).append(t)
    return {label: performance_metrics(items) for label, items in buckets.items()}


def breakdowns(trades: list[MeasuredTrade]) -> dict:
    """The six required breakdowns, each with its own sample size."""
    return {
        "direction": _grouped(trades, lambda t: t.position.side),
        "setup_quality": _grouped(
            trades,
            lambda t: (t.position.signal_quality or NOT_AVAILABLE),
        ),
        "symbol": _grouped(trades, lambda t: t.position.symbol or NOT_AVAILABLE),
        "sizing_mode": _grouped(trades, lambda t: t.sizing_mode_or_unknown),
        "exit_reason": _grouped(
            trades, lambda t: t.position.terminal_reason or NOT_AVAILABLE
        ),
        "strategy_version": _grouped(
            trades, lambda t: t.position.strategy_version or NOT_AVAILABLE
        ),
    }


# ────────────────────────────────────────────────────────────────────────────
# Sizing-mode separation (section 9)
# ────────────────────────────────────────────────────────────────────────────


def sizing_mode_split(trades: list[MeasuredTrade]) -> dict:
    """RISK_ENGINE vs MANUAL, never merged.

    Absolute P&L is not comparable across the two groups - they carry different
    exposure by construction - so each group is reported with both its absolute
    figures and its exposure-normalized ones (R multiple and return on
    capital). A higher absolute number on either side is NOT evidence that the
    strategy improved; only the normalized columns are comparable.
    """
    out: dict[str, Any] = {}
    for mode in (SIZING_RISK_ENGINE, SIZING_MANUAL):
        subset = [t for t in trades if t.sizing_mode == mode]
        stats = performance_metrics(subset)
        out[mode] = {
            "trades": stats["total_completed_trades"],
            "total_pnl": stats["net_pnl"],
            "average_pnl": stats["average_pnl_per_trade"],
            "win_rate": stats["win_rate"],
            "profit_factor": stats["profit_factor"],
            "expectancy": stats["expectancy"],
            "average_risk_utilization_percent": stats["risk_utilization_percent_mean"],
            "average_capital_utilization_percent": stats["capital_utilization_percent_mean"],
            # exposure-normalized
            "normalized": {
                "r_multiple_mean": stats["r_multiple_mean"],
                "r_multiple_median": stats["r_multiple_median"],
                "r_multiple_observed": stats["r_multiple_observed"],
                "return_on_capital_percent_mean": stats["return_on_capital_percent_mean"],
                "initial_risk_mean": stats["initial_risk_mean"],
            },
            "sample_sufficient": stats["sample_sufficient"],
            "data_status": stats["data_status"],
        }
    unknown = [t for t in trades if t.sizing_mode is None]
    out[NOT_AVAILABLE] = {
        "trades": len(unknown),
        "total_pnl": _r(sum(t.position.pnl for t in unknown), 2),
        "note": "Sizing mode could not be attributed for these trades.",
    }
    out["_note"] = (
        "RISK_ENGINE and MANUAL are deliberately never summed. Absolute P&L "
        "differs by construction because the two modes carry different "
        "exposure; compare the 'normalized' block, not the rupee column."
    )
    return out


# ────────────────────────────────────────────────────────────────────────────
# Attribution integrity (section 5)
# ────────────────────────────────────────────────────────────────────────────


def attribution_report(trades: list[MeasuredTrade]) -> dict:
    """Field-by-field completeness of the signal → order → position → geometry
    → trade → P&L chain, for the properly-captured cohort.

    A field with no observation counts as ``NOT_AVAILABLE`` and is reported as
    such. It is never counted as present, and never as zero.
    """

    def _cover(getter: Callable[[MeasuredTrade], Any]) -> dict:
        present = sum(1 for t in trades if getter(t) not in (None, "", NOT_AVAILABLE))
        total = len(trades)
        return {
            "present": present,
            "total": total,
            "missing": total - present,
            "completeness_percent": _r(present / total * 100.0, 1) if total else None,
        }

    fields = {
        "signal_id": _cover(lambda t: t.position.signal_id),
        "strategy_version": _cover(lambda t: t.position.strategy_version),
        "sizing_mode": _cover(lambda t: t.sizing_mode),
        "setup_quality": _cover(lambda t: t.position.signal_quality),
        "direction": _cover(lambda t: t.position.side),
        "signal_score": _cover(lambda t: t.position.signal_score),
        "confidence": _cover(lambda t: t.position.confidence),
        "entry_price": _cover(lambda t: t.geometry.get("entry_price") if t.geometry else None),
        "initial_stop_loss": _cover(
            lambda t: t.geometry.get("initial_stop_loss") if t.geometry else None
        ),
        "initial_risk_per_share": _cover(lambda t: t.initial_risk_per_share),
        "initial_risk_amount": _cover(lambda t: t.initial_risk),
        "actual_quantity": _cover(lambda t: t.quantity),
        "binding_constraint": _cover(
            lambda t: t.geometry.get("binding_constraint") if t.geometry else None
        ),
        "risk_utilization": _cover(lambda t: t.risk_utilization_percent),
        "capital_utilization": _cover(lambda t: t.capital_utilization_percent),
        "exit_reason": _cover(lambda t: t.position.terminal_reason),
        "exit_price": _cover(lambda t: t.position.exit_price),
        "final_stop_loss": _cover(lambda t: t.position.stop_loss),
        "realized_pnl": _cover(lambda t: t.position.pnl),
    }

    # Geometry immutability.
    #
    # The primary tripwire is SELF-CONSISTENCY and it has to be: a boolean
    # "the frozen stop differs from the live stop" would be circular, because
    # overwriting the geometry with the live stop is exactly what corruption
    # looks like, and it would then report "the stop never moved" and pass.
    # Within a geometry row, |entry - initial_stop_loss| must equal that row's
    # own initial_risk_per_share - a property of the row alone, which an
    # overwrite cannot satisfy.
    #
    # Alongside it, the plain counts: how many positions' frozen stop still
    # differs from the live one, and how many are equal. Equality is not itself
    # wrong (a position that closed with no stop management has stop == entry
    # stop), so the counts are facts, not verdicts.
    def _geometry_self_consistent(t: "MeasuredTrade") -> bool:
        if not t.has_geometry:
            return True
        entry = _finite(t.geometry.get("entry_price"))
        initial = _finite(t.geometry.get("initial_stop_loss"))
        rps = _finite(t.geometry.get("initial_risk_per_share"))
        if entry is None or initial is None or rps is None:
            return True  # nothing to cross-check; completeness is scored elsewhere
        return abs(abs(entry - initial) - rps) < 1e-6

    def _frozen_differs_from_live(t: "MeasuredTrade") -> Optional[bool]:
        if not t.has_geometry:
            return None
        initial = _finite(t.geometry.get("initial_stop_loss"))
        final = _finite(t.position.stop_loss)
        if initial is None or final is None:
            return None
        return abs(final - initial) >= 1e-9

    differ = [d for d in (_frozen_differs_from_live(t) for t in trades)
              if d is not None]

    consistency = {
        "geometry_rows_read": len({t.position_id for t in trades if t.has_geometry}),
        "positions_without_geometry": len(
            [t for t in trades if not t.has_geometry]
        ),
        # TRIPWIRE - False means at least one geometry row contradicts itself.
        "geometry_self_consistent": all(_geometry_self_consistent(t) for t in trades),
        "incoherent_geometry_rows": len(
            [t for t in trades
             if t.has_geometry and not _geometry_self_consistent(t)]
        ),
        # Plain counts, not verdicts.
        "stop_moved_by_profit_capture": len(
            [t for t in trades if t._stop_moved().startswith("YES")]
        ),
        "frozen_stop_differs_from_live": sum(1 for d in differ if d),
        "frozen_stop_equals_live": sum(1 for d in differ if not d),
    }

    return {
        "chain": "signal -> order -> position -> geometry -> trade -> realized pnl",
        "join_keys": {
            "signal->trade": "trades.signal_id",
            "trade->position": "trades.details_json.position_id",
            "position->geometry": "position_risk_geometry.position_id",
            "sizing_mode": "position_risk_geometry.quantity_source"
                           " (mirrored into trades.details_json.quantity_source)",
        },
        "field_coverage": fields,
        "geometry_consistency": consistency,
        "fully_attributed": all(f["missing"] == 0 for f in fields.values()),
    }


# ────────────────────────────────────────────────────────────────────────────
# The report
# ────────────────────────────────────────────────────────────────────────────


def build_measurement_report(
    trade_rows: Iterable[dict],
    signals_by_id: Optional[dict] = None,
    geometry_by_position: Optional[dict] = None,
    min_holding_seconds: float = MIN_HOLDING_SECONDS_FOR_STRATEGY_COHORT,
    generated_at: Optional[str] = None,
) -> dict:
    """The full measurement report for properly-captured realized trades.

    Sections:
      * ``capture``   - how many completed trades were measured and why the rest
                        were not (nothing is dropped silently).
      * ``trades``    - the section 7 A-E record, one entry per collapsed trade.
      * ``performance`` / ``breakdowns`` - section 8.
      * ``sizing_modes`` - section 9, RISK_ENGINE vs MANUAL, never merged.
      * ``attribution`` - section 5, field-by-field completeness.
      * ``shadow``    - the shadow sizing figures, labelled and excluded from
                        every realized number above.
    """
    rows = list(trade_rows)
    measured = build_measured_trades(rows, signals_by_id, geometry_by_position,
                                     min_holding_seconds)
    captured = [t for t in measured if t.properly_captured]
    excluded = [t for t in measured if not t.properly_captured]

    geometry_all = geometry_by_position or {}
    reliable = sum(1 for g in geometry_all.values() if g.get("geometry_reliable"))

    strategy = [
        t for t in captured if t.cohort in (COHORT_AI_LINKED, COHORT_STRATEGY_UNLINKED)
    ]
    fixtures = [t for t in captured if t.cohort == COHORT_TEST_FIXTURE]

    exclusion_counts: dict[str, int] = {}
    for t in excluded:
        reason = t.exclusion_reason or "UNKNOWN"
        exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1

    ai_linked = [t for t in captured if t.position.is_ai_linked]
    risk_engine = [t for t in captured if t.sizing_mode == SIZING_RISK_ENGINE]
    manual = [t for t in captured if t.sizing_mode == SIZING_MANUAL]

    total_captured = len(captured)
    verdict = (
        PROMOTION_SUFFICIENT
        if total_captured >= MIN_TRADES_FOR_CONCLUSION
        else PROMOTION_INSUFFICIENT
    )

    return {
        "kind": "REALIZED",
        "generated_at": generated_at,
        "read_only": True,
        "promotion_verdict": verdict,
        "min_trades_for_conclusion": MIN_TRADES_FOR_CONCLUSION,
        "shadow_policy": (
            "Shadow sizing columns are reported for reference only and are "
            "NEVER summed into realized P&L, win rate, expectancy, profit "
            "factor or drawdown. Historical promotable remains False."
        ),
        "capture": {
            "closed_ledger_rows": len(
                [r for r in rows if str(r.get("status") or "closed") == "closed"]
            ),
            "positions_after_collapse": len(measured),
            "properly_captured": total_captured,
            "not_properly_captured": len(excluded),
            "exclusion_reasons": exclusion_counts,
            "geometry_rows_in_ledger": len(geometry_all),
            "geometry_rows_reliable": reliable,
            "risk_engine_trades": len(risk_engine),
            "manual_trades": len(manual),
            "ai_linked_trades": len(ai_linked),
            "test_fixture_trades_excluded": len(fixtures),
            "data_status": "OK" if total_captured else INSUFFICIENT_DATA,
        },
        "performance": {
            "all_properly_captured": performance_metrics(captured),
            "strategy_cohort": performance_metrics(strategy),
            "ai_linked": performance_metrics(ai_linked),
            "test_fixture_excluded": performance_metrics(fixtures),
        },
        "breakdowns": {
            "all_properly_captured": breakdowns(captured),
            "strategy_cohort": breakdowns(strategy),
        },
        "sizing_modes": sizing_mode_split(strategy),
        "attribution": attribution_report(captured),
        "shadow": {
            "labelled": "SHADOW - NOT REALIZED",
            "trades_with_shadow": len(
                [t for t in captured if t.geometry
                 and _finite(t.geometry.get("shadow_quantity")) is not None]
            ),
            "shadow_initial_risk_mean": _r(
                _mean(
                    _finite(t.geometry.get("shadow_initial_risk"))
                    for t in captured if t.geometry
                ),
                2,
            ),
            "realized_initial_risk_mean": _r(
                _mean(t.initial_risk for t in captured), 2
            ),
            "booked_as_realized_pnl": False,
            "historical_promotable_unchanged": True,
        },
        "trades": [t.to_dict() for t in measured],
    }