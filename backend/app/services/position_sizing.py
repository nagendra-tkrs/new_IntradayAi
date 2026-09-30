"""Risk-budget utilization analysis and SHADOW sizing comparison.

**Measurement only.** Nothing in this module places, resizes, blocks or closes
an order, and nothing here writes to the realized ledger. It exists to answer
one narrow question with real numbers:

    Is the account actually risking the 2% it is configured to risk, and if
    not, what is capping it?

The live formula (``RiskEngine.calculate_position_size``) is

    risk_budget      = capital * risk_pct / 100
    risk_per_share   = |entry - stop_loss|
    risk_based_qty   = int(risk_budget / risk_per_share)
    capital_based_qty= int(0.95 * capital / entry)
    qty              = min(risk_based_qty, capital_based_qty)

i.e. the risk budget is an upper bound, and the cash-affordability cap is a
second upper bound. Whichever is *smaller* wins. This module reports both
terms and names the binding one, instead of reporting only the result.

Counterfactual sizing is offered as a **SHADOW** measurement only. See
:func:`shadow_sizing_comparison` for the exact contract — in particular, the
shadow quantity is derived from the risk budget ALONE, which can only ever
raise or preserve exposure, never lower it. It is not proposed for promotion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# Which of the two upper bounds actually set the position size.
BINDING_RISK_BUDGET = "RISK_BUDGET"
BINDING_CAPITAL = "CAPITAL"
BINDING_TIE = "TIE"
BINDING_EXPLICIT = "EXPLICIT_QUANTITY"
BINDING_UNAVAILABLE = "UNAVAILABLE"

# Provenance of the stop used for the risk geometry.
STOP_SOURCE_SIGNAL = "SIGNAL_ENTRY_TIME"
STOP_SOURCE_LEDGER_TRAILED = "LEDGER_TRAILED_UNRELIABLE"
STOP_SOURCE_NONE = "NONE"

# The live engine leaves this fraction of capital free
# (``if total_cost > account_capital * 0.95``). Mirrored exactly.
CAPITAL_HEADROOM = 0.95

# Imported rather than redefined: the sample floor and the verdict string are a
# contract shared with the baseline report, and a second copy would let the two
# drift apart and report contradictory conclusions from the same ledger.
from app.services.profit_baseline import (  # noqa: E402
    INSUFFICIENT_PROMOTION, MIN_TRADES_FOR_CONCLUSION,
)


def _finite(value: Any) -> Optional[float]:
    """Strict finite float or None. Rejects bool, NaN, +/-inf and anything
    unparseable, so a malformed ledger value degrades to UNKNOWN rather than
    poisoning the statistics."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def risk_budget_for(capital: float, risk_pct: float) -> float:
    """Configured rupee risk budget for one trade."""
    return capital * (risk_pct / 100.0)


def risk_based_quantity(risk_budget: float, risk_per_share: float) -> int:
    """Quantity implied by the risk budget alone (no cash constraint)."""
    if risk_budget <= 0 or risk_per_share <= 0:
        return 0
    return int(risk_budget / risk_per_share)


def capital_based_quantity(capital: float, entry: float) -> int:
    """Quantity affordable from cash at the engine's 95% headroom."""
    if capital <= 0 or entry <= 0:
        return 0
    return int((capital * CAPITAL_HEADROOM) / entry)


def production_quantity(
    entry: float, stop_loss: float, capital: float, risk_pct: float
) -> int:
    """Line-for-line mirror of ``RiskEngine.calculate_position_size``.

    Kept byte-compatible with the live formula so a measurement can never
    disagree with what production would have done. Uses the 0.95 headroom
    directly rather than the engine's two-step ``int(capital/entry)`` then
    clamp, which yields the same integer for every positive entry.
    """
    if risk_pct <= 0 or entry <= 0:
        return 0
    rps = abs(entry - stop_loss)
    if rps <= 0:
        return 0
    return min(risk_based_quantity(capital * (risk_pct / 100.0), rps),
               capital_based_quantity(capital, entry))


def binding_constraint(
    risk_qty: int, capital_qty: int, actual_qty: Optional[float]
) -> str:
    """Name the constraint that set the size.

    ``EXPLICIT_QUANTITY`` means the caller supplied ``order.quantity`` and the
    risk engine's formula was bypassed entirely. In ``api/trading.py`` the
    formula only runs when ``order.quantity <= 0``, and BOTH order-placing UI
    paths always send a concrete quantity (``scanner/page.tsx``:
    ``orderQty[sym] || 1``; ``stock/[symbol]/page.tsx``: ``useState(1)``), so
    in practice the 2% risk budget is reached only on API calls that omit it.

    An explicit quantity is reported whenever the filled size differs from
    ``min(risk_qty, capital_qty)`` in EITHER direction - an undersized fill and
    an oversized one are both a bypass of both bounds, and neither may be
    reported as if a bound had governed it.
    """
    formula = min(risk_qty, capital_qty)
    if formula <= 0:
        # Either no risk geometry (a breakeven/absent stop) or nothing
        # affordable. Neither bound "governed" anything, so this must not be
        # reported as if one of them had bound.
        return BINDING_UNAVAILABLE
    if actual_qty is not None and actual_qty > 0:
        if int(round(actual_qty)) != formula:
            return BINDING_EXPLICIT
    if risk_qty < capital_qty:
        return BINDING_RISK_BUDGET
    if capital_qty < risk_qty:
        return BINDING_CAPITAL
    return BINDING_TIE


#: Sizes the risk engine would have chosen.
PATH_FORMULA = "RISK_ENGINE_FORMULA"
#: A concrete quantity was supplied by the caller, so no bound governed it.
PATH_EXPLICIT = "EXPLICIT_QUANTITY_BYPASS"
PATH_UNAVAILABLE = "UNAVAILABLE"


def _path_for(binding: str) -> str:
    if binding == BINDING_UNAVAILABLE:
        return PATH_UNAVAILABLE
    return PATH_EXPLICIT if binding == BINDING_EXPLICIT else PATH_FORMULA


@dataclass(frozen=True)
class SizingBreakdown:
    """Full sizing arithmetic for one position, from entry-time information."""

    position_id: str
    symbol: str
    direction: str
    account_capital: float
    risk_pct: float

    entry_price: float
    stop_loss: Optional[float]
    risk_per_share: Optional[float]
    atr: Optional[float] = None

    risk_budget: float = 0.0
    risk_based_quantity: int = 0
    capital_based_quantity: int = 0
    formula_quantity: int = 0

    #: Where ``stop_loss`` came from. ``SIGNAL`` is the entry-time stop and is
    #: authoritative; ``LEDGER_TRAILED`` means the value was read from a closed
    #: ledger row, which Profit Capture may have already moved to breakeven or
    #: chased up by the trailing stop, so ``risk_per_share`` is then UNRELIABLE.
    stop_source: str = STOP_SOURCE_NONE

    actual_quantity: Optional[float] = None
    actual_rupee_risk: Optional[float] = None
    intended_rupee_risk: Optional[float] = None
    utilization_percent: Optional[float] = None
    budget_utilization_percent: Optional[float] = None
    binding: str = BINDING_UNAVAILABLE

    #: True when the cash-affordability cap, not the risk budget, set the size.
    @property
    def capital_constrained(self) -> bool:
        return self.binding == BINDING_CAPITAL

    @property
    def usable(self) -> bool:
        return bool(
            self.entry_price > 0
            and self.risk_per_share
            and self.risk_per_share > 0
            and self.formula_quantity > 0
        )

    #: True when the risk geometry came from the entry-time signal row, so the
    #: sizing arithmetic is exact. A trailed ledger stop is not.
    @property
    def geometry_reliable(self) -> bool:
        return self.usable and self.stop_source == STOP_SOURCE_SIGNAL

    #: How this position actually got its size: the risk-engine formula, or a
    #: caller-supplied quantity that bypassed both bounds.
    @property
    def sizing_path(self) -> str:
        return _path_for(self.binding)

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "account_capital": round(self.account_capital, 2),
            "risk_pct": round(self.risk_pct, 4),
            "entry_price": round(self.entry_price, 2),
            "stop_loss": self.stop_loss,
            "risk_per_share": self.risk_per_share,
            "atr": self.atr,
            "risk_budget": round(self.risk_budget, 2),
            "risk_based_quantity": self.risk_based_quantity,
            "capital_based_quantity": self.capital_based_quantity,
            "formula_quantity": self.formula_quantity,
            "stop_source": self.stop_source,
            "geometry_reliable": self.geometry_reliable,
            "actual_quantity": self.actual_quantity,
            "actual_rupee_risk": self.actual_rupee_risk,
            "intended_rupee_risk": self.intended_rupee_risk,
            "utilization_percent": self.utilization_percent,
            "budget_utilization_percent": self.budget_utilization_percent,
            "binding_constraint": self.binding,
            "sizing_path": self.sizing_path,
            "capital_constrained": self.capital_constrained,
        }


def sizing_breakdown(
    position,
    account_capital: float = 10_000.0,
    risk_pct: float = 2.0,
) -> SizingBreakdown:
    """Derive the sizing arithmetic for a realized :class:`Position`.

    Every input is entry-time information (entry, the original ATR-derived
    stop, the recorded order quantity). No exit information and no future bar
    is consulted, so this is safe to compute on historical positions.
    """
    signal = getattr(position, "signal", None) or {}
    entry = _finite(getattr(position, "entry_price", None)) or 0.0

    # STOP PROVENANCE. A closed ledger row carries the stop as it stood at EXIT,
    # and Profit Capture rewrites it: `apply_t1_protection` pulls it to entry
    # (breakeven) the moment T1 fills, and `update_trailing_stop` then chases it
    # up. Reading |entry - stop_loss| from such a row silently yields ~0 and
    # destroys the risk geometry. The linked `signals` row, by contrast, is
    # written once at scan time and is only ever UPDATEd for
    # quantity/risk_amount/risk_percent/outcome/realized_pnl/holding_duration -
    # never for stop_loss - so it holds the ORIGINAL entry-time stop that sizing
    # actually used. Prefer it; fall back to the ledger only when absent, and
    # mark that fallback as unreliable.
    stop = _finite(signal.get("stop_loss"))
    stop_source = STOP_SOURCE_SIGNAL
    if stop is None or stop <= 0:
        stop = _finite(getattr(position, "stop_loss", None))
        stop_source = STOP_SOURCE_LEDGER_TRAILED if stop is not None else STOP_SOURCE_NONE
    if stop is not None and entry > 0 and abs(entry - stop) <= 0:
        # Degenerate geometry (breakeven stop): no risk information at all.
        stop = None
        stop_source = STOP_SOURCE_NONE

    atr = _finite(signal.get("atr")) or _finite(getattr(position, "atr", None))

    # The order-time quantity is authoritative when present: it is what the
    # engine actually filled.
    #
    # Fallback order matters. A closed ledger row's `quantity` column holds the
    # POSITION size on a T1 partial (50) and only the REMAINING size on the
    # final close (25), so `collapse_positions` sums it to 75 for a 50-share
    # two-slice exit. `initial_quantity` is recorded once and is correct, so it
    # is preferred over the slice sum.
    actual = _finite(signal.get("quantity"))
    if actual is None or actual <= 0:
        actual = _finite(getattr(position, "initial_quantity", None))
    if actual is None or actual <= 0:
        actual = _finite(getattr(position, "quantity", None))

    rps = abs(entry - stop) if (entry and stop is not None) else None
    budget = risk_budget_for(account_capital, risk_pct)
    rq = risk_based_quantity(budget, rps) if rps else 0
    cq = capital_based_quantity(account_capital, entry) if entry > 0 else 0
    fq = min(rq, cq) if rq > 0 and cq > 0 else 0
    formula_risk = min(budget, fq * rps) if (fq and rps) else None
    actual_risk = rps * actual if (rps and actual) else None

    return SizingBreakdown(
        position_id=str(getattr(position, "position_id", "")),
        symbol=str(getattr(position, "symbol", "")),
        direction=str(getattr(position, "direction", "")),
        account_capital=account_capital,
        risk_pct=risk_pct,
        entry_price=entry,
        stop_loss=stop,
        risk_per_share=rps,
        atr=atr,
        risk_budget=budget,
        risk_based_quantity=rq,
        capital_based_quantity=cq,
        formula_quantity=fq,
        stop_source=stop_source,
        actual_quantity=actual,
        actual_rupee_risk=actual_risk,
        intended_rupee_risk=formula_risk,
        utilization_percent=(actual_risk / budget * 100.0)
        if (actual_risk is not None and budget > 0)
        else None,
        budget_utilization_percent=(formula_risk / budget * 100.0)
        if (formula_risk is not None and budget > 0)
        else None,
        binding=binding_constraint(rq, cq, actual),
    )


# ────────────────────────────────────────────────────────────────────────────
# SHADOW counterfactual — NOT a promotion candidate
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ShadowSizingComparison:
    """Current sizing vs risk-budget-driven sizing for ONE position.

    ``shadow_*`` fields are **hypothetical**. They are not realized P&L, are
    never persisted to the ledger, and carry no claim that the strategy
    improved. The risk budget is used ALONE here (no cash cap), so
    ``shadow_quantity >= baseline_quantity`` always: this counterfactual can
    only add exposure, never remove it. The realised P&L is rescaled linearly
    by the quantity multiple, which is exact for the P&L term because entry,
    stop and both targets are held fixed.
    """

    position_id: str
    symbol: str
    entry_price: float
    risk_per_share: Optional[float]
    risk_budget: float

    baseline_quantity: Optional[float]
    shadow_quantity: int
    quantity_multiple: Optional[float]

    baseline_exposure: Optional[float]
    shadow_exposure: float
    exposure_multiple: Optional[float]

    baseline_rupee_risk: Optional[float]
    shadow_rupee_risk: Optional[float]
    shadow_utilization_percent: Optional[float]

    realized_pnl: float
    shadow_pnl: Optional[float]
    shadow_pnl_delta: Optional[float]

    labelled: str = "SHADOW - HYPOTHETICAL, NOT REALIZED P&L"

    @property
    def usable(self) -> bool:
        return self.shadow_quantity > 0 and self.risk_per_share is not None

    def to_dict(self) -> dict:
        return {
            "label": self.labelled,
            "position_id": self.position_id,
            "symbol": self.symbol,
            "entry_price": round(self.entry_price, 2),
            "risk_per_share": self.risk_per_share,
            "risk_budget": round(self.risk_budget, 2),
            "baseline_quantity": self.baseline_quantity,
            "shadow_quantity": self.shadow_quantity,
            "quantity_multiple": self.quantity_multiple,
            "baseline_exposure": self.baseline_exposure,
            "shadow_exposure": round(self.shadow_exposure, 2),
            "exposure_multiple": self.exposure_multiple,
            "baseline_rupee_risk": self.baseline_rupee_risk,
            "shadow_rupee_risk": self.shadow_rupee_risk,
            "shadow_utilization_percent": self.shadow_utilization_percent,
            "realized_pnl": round(self.realized_pnl, 2),
            "shadow_pnl": self.shadow_pnl,
            "shadow_pnl_delta": self.shadow_pnl_delta,
        }


def shadow_sizing_comparison(
    position,
    breakdown: Optional[SizingBreakdown] = None,
    account_capital: float = 10_000.0,
    risk_pct: float = 2.0,
) -> ShadowSizingComparison:
    """Size the same position off the risk budget ALONE and rescale its P&L.

    Uses only entry-time information (entry, original stop, recorded quantity)
    plus the already-realized per-unit outcome. No future bar, no lookahead.
    The result is a SHADOW measurement and is explicitly labelled as such; it
    is never written back to ``trades`` or ``signals``.
    """
    bd = breakdown or sizing_breakdown(position, account_capital, risk_pct)
    entry = bd.entry_price
    rps = bd.risk_per_share
    budget = bd.risk_budget
    sq = risk_based_quantity(budget, rps) if rps else 0
    base_qty = bd.actual_quantity
    pnl = _finite(getattr(position, "pnl", None)) or 0.0

    multiple = (sq / base_qty) if (base_qty and sq and base_qty > 0) else None
    return ShadowSizingComparison(
        position_id=bd.position_id,
        symbol=bd.symbol,
        entry_price=entry,
        risk_per_share=rps,
        risk_budget=budget,
        baseline_quantity=base_qty,
        shadow_quantity=sq,
        quantity_multiple=multiple,
        baseline_exposure=(base_qty * entry) if base_qty and entry else None,
        shadow_exposure=sq * entry,
        exposure_multiple=(sq / base_qty) if (base_qty and sq and base_qty > 0) else None,
        baseline_rupee_risk=bd.actual_rupee_risk,
        shadow_rupee_risk=(sq * rps) if (sq and rps) else None,
        shadow_utilization_percent=((sq * rps) / budget * 100.0)
        if (sq and rps and budget > 0)
        else None,
        realized_pnl=pnl,
        shadow_pnl=(pnl * multiple) if multiple else None,
        shadow_pnl_delta=(pnl * multiple - pnl) if multiple else None,
    )


# ────────────────────────────────────────────────────────────────────────────
# ENTRY-TIME CAPTURE
#
# Everything above this line reconstructs geometry from a closed ledger row and
# therefore has to cope with a stop that Profit Capture has already moved. What
# follows captures the geometry instead of reconstructing it, once, at the
# instant an order becomes an open position.
# ────────────────────────────────────────────────────────────────────────────

# Explicit order classifications required for measurement.
CLASS_RISK_ENGINE_SIZED = "RISK_ENGINE_SIZED"
CLASS_MANUAL_QUANTITY = "MANUAL_QUANTITY"
CLASS_CAPITAL_CONSTRAINED = "CAPITAL_CONSTRAINED"
CLASS_RISK_CONSTRAINED = "RISK_CONSTRAINED"
CLASS_UNCLASSIFIED = "UNCLASSIFIED"

#: How the quantity that filled was chosen.
SOURCE_RISK_ENGINE = "RISK_ENGINE"
SOURCE_MANUAL = "MANUAL"


def classify_order(
    actual_quantity: Optional[float],
    allowed_quantity: int,
    quantity_source: Optional[str] = None,
    binding: str = BINDING_UNAVAILABLE,
) -> str:
    """Classify an order WITHOUT altering the quantity that was filled.

    The four labels are mutually exclusive and are reported, never acted on. A
    manually supplied UI quantity is never silently replaced by a computed
    one - the whole point of this task is to make the difference visible.

    ``quantity_source`` is authoritative when the caller states it (the order
    endpoint knows whether it invoked the risk engine). When it is absent the
    class is INFERRED by comparing the filled quantity against
    ``allowed_quantity``, which is exact for an engine-sized order and merely
    indicative for a hand-typed one that happens to coincide.
    """
    if quantity_source == SOURCE_MANUAL:
        return CLASS_MANUAL_QUANTITY
    if quantity_source == SOURCE_RISK_ENGINE:
        if binding == BINDING_CAPITAL:
            return CLASS_CAPITAL_CONSTRAINED
        if binding == BINDING_RISK_BUDGET:
            return CLASS_RISK_CONSTRAINED
        return CLASS_RISK_ENGINE_SIZED

    # Inferred path: no caller statement available.
    if actual_quantity is None or actual_quantity <= 0 or allowed_quantity <= 0:
        return CLASS_UNCLASSIFIED
    if int(round(actual_quantity)) == int(allowed_quantity):
        if binding == BINDING_CAPITAL:
            return CLASS_CAPITAL_CONSTRAINED
        if binding == BINDING_RISK_BUDGET:
            return CLASS_RISK_CONSTRAINED
        return CLASS_RISK_ENGINE_SIZED
    return CLASS_MANUAL_QUANTITY


@dataclass(frozen=True)
class EntryRiskGeometry:
    """Immutable entry-time risk geometry for ONE filled position.

    Every value here is read at fill time and is never recomputed. The
    ``initial_*`` names are load-bearing: Profit Capture writes ``stop_loss``,
    ``target_1`` and ``target_2`` on the live position as it manages the trade,
    so the entry-time levels must live under names it cannot collide with.

    ``usable``/``geometry_reliable`` are False when the capture could not be
    established (no stop, a breakeven stop, or a non-positive level). Such a
    capture is still recorded, because "we could not measure this" is a fact
    worth keeping, whereas a silent NULL reads like a zero risk.
    """

    position_id: str
    symbol: str
    direction: str
    user_id: Optional[str] = None
    signal_id: Optional[str] = None
    captured_at: Optional[str] = None
    capture_source: str = "ORDER_FILL"

    entry_price: float = 0.0
    initial_stop_loss: Optional[float] = None
    initial_target_1: Optional[float] = None
    initial_target_2: Optional[float] = None
    initial_risk_per_share: Optional[float] = None
    initial_risk_amount: Optional[float] = None
    initial_quantity: Optional[float] = None

    account_capital: float = 0.0
    configured_risk_percent: float = 0.0
    risk_budget: float = 0.0
    risk_constraint_quantity: int = 0
    capital_constraint_quantity: int = 0
    allowed_quantity: int = 0
    actual_quantity: Optional[float] = None
    intended_risk: Optional[float] = None
    actual_risk: Optional[float] = None
    risk_budget_utilization: Optional[float] = None

    order_classification: str = CLASS_UNCLASSIFIED
    binding_constraint: str = BINDING_UNAVAILABLE
    quantity_source: Optional[str] = None

    # -- SHADOW: risk-budget-driven sizing. Never executed, never realized.
    shadow_quantity: int = 0
    shadow_exposure: float = 0.0
    shadow_initial_risk: Optional[float] = None
    shadow_utilization: Optional[float] = None
    current_exposure: float = 0.0

    geometry_reliable: bool = False

    @property
    def usable(self) -> bool:
        return bool(
            self.entry_price > 0
            and self.initial_risk_per_share
            and self.initial_risk_per_share > 0
            and self.initial_quantity
            and self.initial_quantity > 0
        )

    @property
    def shadow_multiple(self) -> Optional[float]:
        if not (self.actual_quantity and self.actual_quantity > 0
                and self.shadow_quantity > 0):
            return None
        return self.shadow_quantity / self.actual_quantity

    def to_row(self) -> dict:
        """Flatten to the exact column set of the geometry ledger."""
        return {
            "position_id": self.position_id,
            "user_id": self.user_id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "captured_at": self.captured_at,
            "capture_source": self.capture_source,
            "entry_price": self.entry_price,
            "initial_stop_loss": self.initial_stop_loss,
            "initial_target_1": self.initial_target_1,
            "initial_target_2": self.initial_target_2,
            "initial_risk_per_share": self.initial_risk_per_share,
            "initial_risk_amount": self.initial_risk_amount,
            "initial_quantity": self.initial_quantity,
            "account_capital": self.account_capital,
            "configured_risk_percent": self.configured_risk_percent,
            "risk_budget": self.risk_budget,
            "risk_constraint_quantity": self.risk_constraint_quantity,
            "capital_constraint_quantity": self.capital_constraint_quantity,
            "allowed_quantity": self.allowed_quantity,
            "actual_quantity": self.actual_quantity,
            "intended_risk": self.intended_risk,
            "actual_risk": self.actual_risk,
            "risk_budget_utilization": self.risk_budget_utilization,
            "order_classification": self.order_classification,
            "binding_constraint": self.binding_constraint,
            "quantity_source": self.quantity_source,
            "shadow_quantity": self.shadow_quantity,
            "shadow_exposure": self.shadow_exposure,
            "shadow_initial_risk": self.shadow_initial_risk,
            "shadow_utilization": self.shadow_utilization,
            "current_exposure": self.current_exposure,
            "geometry_reliable": 1 if self.geometry_reliable else 0,
        }

    def to_dict(self) -> dict:
        row = self.to_row()
        row["geometry_reliable"] = self.geometry_reliable
        row["shadow_multiple"] = self.shadow_multiple
        row["shadow_label"] = "SHADOW - NOT EXECUTED, NOT REALIZED P&L"
        return row


def capture_entry_geometry(
    position: dict,
    account_capital: float = 10_000.0,
    risk_pct: float = 2.0,
    quantity_source: Optional[str] = None,
    captured_at: Optional[str] = None,
) -> EntryRiskGeometry:
    """Freeze the risk geometry of ``position`` at the moment it was filled.

    Reads ONLY the levels the position carries right now - which, when called
    from ``PaperAccount.fill_order``, are the levels as of entry. It does not
    consult any later price, any bar, or any exit record, so the result is
    free of lookahead and is identical whether computed at fill or recomputed
    years later from the stored row.

    ``quantity_source`` should be supplied by the caller that actually chose
    the quantity (``RISK_ENGINE`` or ``MANUAL``) so the classification is a
    recorded fact rather than an inference. When omitted the class is inferred
    by comparing the filled quantity against ``allowed_quantity``.
    """
    entry = _finite(position.get("entry_price")) or 0.0
    stop = _finite(position.get("stop_loss"))
    t1 = _finite(position.get("target_1"))
    t2 = _finite(position.get("target_2"))
    # At FILL, `quantity` is authoritative: fill_order prices the fill from
    # order["quantity"], and edit_order rewrites that field without touching
    # initial_quantity. So an edited order's initial_quantity is the PRE-EDIT
    # size and must not be used here.
    #
    # Note the deliberate asymmetry with the reconstruction path above, which
    # prefers initial_quantity: on a CLOSED ledger row `quantity` has been
    # reduced to the final remaining slice, so the order is reversed there.
    # Capture-at-fill and reconstruct-from-closed-row are different questions
    # and must not share a precedence rule.
    qty = _finite(position.get("quantity"))
    if qty is None or qty <= 0:
        qty = _finite(position.get("initial_quantity"))

    # A stop sitting exactly on entry carries no risk information: it is either
    # a breakeven stop or an absent one, and neither yields a risk per share.
    rps: Optional[float] = None
    if entry > 0 and stop is not None and stop > 0 and abs(entry - stop) > 0:
        rps = abs(entry - stop)

    capital = _finite(account_capital) or 0.0
    pct = _finite(risk_pct) or 0.0
    budget = risk_budget_for(capital, pct)

    risk_q = risk_based_quantity(budget, rps) if rps else 0
    cap_q = capital_based_quantity(capital, entry) if entry > 0 else 0
    allowed = min(risk_q, cap_q)

    # Per the sizing contract: intended_risk is the risk the BUDGET implies
    # (risk_constraint_quantity x risk_per_share). It is deliberately NOT capped
    # by the cash constraint, so it can exceed the risk an affordable position
    # would actually carry - which is exactly the gap this task quantifies.
    intended = risk_q * rps if (risk_q and rps) else None
    actual_risk = qty * rps if (qty and rps) else None

    binding = binding_constraint(risk_q, cap_q, None)
    shadow_q = risk_q

    return EntryRiskGeometry(
        position_id=str(position.get("id") or ""),
        user_id=position.get("user_id"),
        signal_id=position.get("signal_id"),
        symbol=str(position.get("symbol") or ""),
        direction=str(position.get("direction") or ""),
        captured_at=captured_at,
        entry_price=entry,
        initial_stop_loss=stop,
        initial_target_1=t1,
        initial_target_2=t2,
        initial_risk_per_share=rps,
        initial_risk_amount=actual_risk,
        initial_quantity=qty,
        account_capital=capital,
        configured_risk_percent=pct,
        risk_budget=budget,
        risk_constraint_quantity=risk_q,
        capital_constraint_quantity=cap_q,
        allowed_quantity=allowed,
        actual_quantity=qty,
        intended_risk=intended,
        actual_risk=actual_risk,
        risk_budget_utilization=(
            actual_risk / budget * 100.0
            if (actual_risk is not None and budget > 0) else None
        ),
        order_classification=classify_order(
            qty, allowed, quantity_source, binding
        ),
        binding_constraint=binding,
        quantity_source=quantity_source,
        shadow_quantity=shadow_q,
        shadow_exposure=shadow_q * entry,
        shadow_initial_risk=(shadow_q * rps) if (shadow_q and rps) else None,
        shadow_utilization=(
            (shadow_q * rps) / budget * 100.0
            if (shadow_q and rps and budget > 0) else None
        ),
        current_exposure=(qty * entry) if (qty and entry) else 0.0,
        geometry_reliable=bool(entry > 0 and rps and qty and qty > 0),
    )


def captured_geometry_fields() -> dict:
    """Map of position-dict key -> :class:`EntryRiskGeometry` attribute.

    Naming these explicitly (rather than splatting a whole dataclass into the
    position) keeps the position readable and guarantees the capture adds only
    keys that Profit Capture's ``profit_meta`` serializer and the trailing
    logic have no reason to touch.

    Every key on the left is ``initial_*``. That is the load-bearing part: the
    live position's ``stop_loss``/``target_1``/``target_2`` are rewritten by
    Profit Capture, so the frozen entry levels must sit under names that
    nothing else writes.
    """
    return {
        "initial_entry_price": "entry_price",
        "initial_stop_loss": "initial_stop_loss",
        "initial_target_1": "initial_target_1",
        "initial_target_2": "initial_target_2",
        "initial_risk_per_share": "initial_risk_per_share",
        "initial_risk_amount": "initial_risk_amount",
        "initial_risk_budget": "risk_budget",
        "initial_allowed_quantity": "allowed_quantity",
        "initial_risk_utilization": "risk_budget_utilization",
        "initial_order_classification": "order_classification",
        "initial_geometry_reliable": "geometry_reliable",
    }


# ────────────────────────────────────────────────────────────────────────────
# Aggregate
# ────────────────────────────────────────────────────────────────────────────


def _mean(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def sizing_report(
    positions: Iterable,
    account_capital: float = 10_000.0,
    risk_pct: float = 2.0,
) -> dict:
    """Per-position breakdown plus the aggregate utilization picture."""
    pairs: list[tuple[Any, SizingBreakdown]] = []
    breakdowns: list[SizingBreakdown] = []
    for pos in positions:
        bd = sizing_breakdown(pos, account_capital, risk_pct)
        breakdowns.append(bd)
        if bd.usable:
            pairs.append((pos, bd))
    usable = [bd for _, bd in pairs]
    n = len(usable)
    comparisons = [
        shadow_sizing_comparison(pos, bd, account_capital, risk_pct)
        for pos, bd in pairs
    ]

    capital_constrained = sum(1 for b in usable if b.capital_constrained)
    explicit = sum(1 for b in usable if b.binding == BINDING_EXPLICIT)
    risk_constrained = sum(1 for b in usable if b.binding == BINDING_RISK_BUDGET)
    via_formula = sum(1 for b in usable if b.sizing_path == PATH_FORMULA)
    reliable = sum(1 for b in usable if b.geometry_reliable)
    solid = [b for b in usable if b.geometry_reliable]
    shaky = [b for b in usable if not b.geometry_reliable]

    baseline_pnl = sum(c.realized_pnl for c in comparisons)
    shadow_pnl = sum(c.shadow_pnl or 0.0 for c in comparisons)

    report = {
        "account_capital": account_capital,
        "risk_pct": risk_pct,
        "risk_budget": round(risk_budget_for(account_capital, risk_pct), 2),
        "positions_total": len(breakdowns),
        "positions_usable": n,
        "capital_constrained": capital_constrained,
        "risk_constrained": risk_constrained,
        "explicit_quantity": explicit,
        "via_risk_engine_formula": via_formula,
        "via_explicit_quantity": explicit,
        "geometry_from_entry_time_signal": reliable,
        "capital_constrained_pct": round(capital_constrained / n * 100.0, 2) if n else None,
        "capital_constrained_pct_of_formula": round(capital_constrained / via_formula * 100.0, 2) if via_formula else None,
        "risk_constrained_pct": round(risk_constrained / n * 100.0, 2) if n else None,
        "explicit_quantity_pct": round(explicit / n * 100.0, 2) if n else None,
        "mean_intended_risk": _mean(b.intended_rupee_risk for b in usable),
        "mean_actual_risk": _mean(b.actual_rupee_risk for b in usable),
        "mean_utilization_percent": _mean(b.utilization_percent for b in usable),
        "mean_budget_utilization_percent": _mean(
            b.budget_utilization_percent for b in usable
        ),
        # Split by geometry provenance. Positions whose stop came from the
        # entry-time signal row are exact; those read from a closed ledger row
        # may carry a stop Profit Capture already moved, so their rupee risk is
        # an ESTIMATE. The two must not be averaged together silently.
        "reliable_only": {
            "n": len(solid),
            "mean_intended_risk": _mean(b.intended_rupee_risk for b in solid),
            "mean_actual_risk": _mean(b.actual_rupee_risk for b in solid),
            "mean_utilization_percent": _mean(
                b.utilization_percent for b in solid),
        },
        "unreliable_geometry_only": {
            "n": len(shaky),
            "mean_intended_risk": _mean(b.intended_rupee_risk for b in shaky),
            "mean_actual_risk": _mean(b.actual_rupee_risk for b in shaky),
            "mean_utilization_percent": _mean(
                b.utilization_percent for b in shaky),
        },
        "total_actual_risk": round(
            sum(b.actual_rupee_risk or 0.0 for b in usable), 2
        ),
        "breakdowns": [b.to_dict() for b in breakdowns],
        "shadow": {
            "label": "SHADOW - HYPOTHETICAL, NOT REALIZED P&L",
            "positions_compared": len(comparisons),
            "note": (
                "Risk-budget-driven sizing uses the configured budget with NO "
                "cash cap, so it can only raise or preserve exposure. Entry, "
                "stop, T1 and T2 are held fixed; P&L is rescaled linearly by "
                "the quantity multiple. Baseline and shadow P&L below cover "
                "ONLY the comparable positions, not the whole cohort."
            ),
            "positions": [c.to_dict() for c in comparisons],
            "mean_quantity_multiple": _mean(c.quantity_multiple for c in comparisons),
            "mean_exposure_multiple": _mean(c.exposure_multiple for c in comparisons),
            "mean_shadow_rupee_risk": _mean(c.shadow_rupee_risk for c in comparisons),
            "mean_shadow_utilization_percent": _mean(
                c.shadow_utilization_percent for c in comparisons
            ),
            "baseline_pnl": round(baseline_pnl, 2),
            "shadow_pnl": round(shadow_pnl, 2),
            "shadow_pnl_delta": round(shadow_pnl - baseline_pnl, 2),
            "promotable": False,
            "reason": (
                "Sizing is a risk-INCREASING change. Not a candidate for "
                "promotion; measurement only."
            ),
        },
        "min_sample_for_conclusion": MIN_TRADES_FOR_CONCLUSION,
        "verdict": INSUFFICIENT_PROMOTION if n < 30 else None,
    }
    return report
