"""No-lookahead bar replay for IntradayAI profit-optimization experiments.

Purpose
-------
Evaluate a *candidate* trade-management configuration (stop width, T1 partial,
trailing behaviour, target geometry, R:R gate) against the **real** market
path that followed each real position, so a candidate can be compared with the
baseline without ever executing it.

This module is strictly a **counterfactual simulator**. Nothing here writes to
the ledger, places an order, or claims realized performance. Every number it
produces is a simulation and is labelled as such by
``app.services.optimization_experiments``.

Lookahead guarantees
--------------------
1. Bars are consumed only from the position's own ``entry_time`` forward.
   Bars before the entry exist solely to reconstruct the ATR the engine could
   already see (``atr_at_entry``), and are never used to decide an exit.
2. ``atr_at_entry`` is derived from bars with ``timestamp < entry_time``
   (strictly), so no post-entry information leaks into the risk geometry.
3. Within a bar, the stop is tested before the target, matching the live
   ``PaperAccount.check_stops`` priority. Two configurations of intrabar
   assumption are supported and BOTH are reported by the experiment runner,
   because a candidate that only wins under one of them is not a real result.
4. Replay stops at the end of the available data; when a position has not
   exited by then the result is reported as unresolved rather than being
   force-closed at a favourable price.

Fidelity
--------
The baseline configuration is designed to reproduce what the live engine did.
``app.services.optimization_experiments`` VALIDATES this by replaying the
baseline against the recorded exits before any candidate is scored, and
refuses to draw conclusions when fidelity is poor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from typing import Iterable, Optional, Sequence

from app.services.profit_capture import (
    PROTECT_MOVE_TO_ENTRY,
    TRAILING_NONE,
    TRAILING_RANGE,
    ProfitCaptureConfig,
    update_trailing_stop,
)

# Wilder ATR period used by app.services.indicators.atr.
ATR_PERIOD = 14

# How a single bar's high/low is treated when testing the stop and the target.
INTRABAR_PESSIMISTIC = "pessimistic"  # stop tested on the bar's adverse extreme
INTRABAR_CLOSE_ONLY = "close_only"    # stop/target tested on the bar close only
INTRABAR_ASSUMPTIONS = (INTRABAR_PESSIMISTIC, INTRABAR_CLOSE_ONLY)

DEFAULT_SESSION_END = time(15, 30)


@dataclass(frozen=True)
class Bar:
    """A single OHLC bar. ``timestamp`` is the bar's START time (IST, naive)."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def adverse_extreme(self) -> float:  # pragma: no cover - set by caller
        raise NotImplementedError


@dataclass(frozen=True)
class ReplayConfig:
    """One candidate configuration. ``label`` is what the reports print.

    The defaults ARE the live production configuration
    (``strategy_config.v1`` geometry + ``profit_capture`` defaults), so
    ``ReplayConfig()`` is the baseline.
    """

    label: str = "baseline"
    sl_atr_mult: float = 1.5
    t1_atr_mult: float = 2.0
    t2_atr_mult: float = 3.0          # absolute from entry (legacy T2 = T1 + 1 ATR)
    min_rr: float = 1.2
    # Optional target R:R. When set, T1 is placed at entry +/- target_rr * risk
    # and T2 one further ATR out, so the RATIO becomes the experiment variable.
    # This exists because the engine's normal-strength geometry is fixed at
    # T1=2.0 ATR / SL=1.5 ATR, i.e. an R:R of exactly 1.33 for every normal
    # signal — so raising ``min_rr`` alone can only ever block trades, never
    # select a different trade. None = use the ATR multipliers as given.
    target_rr: Optional[float] = None
    t1_exit_percent: float = 50.0
    protect_mode: str = PROTECT_MOVE_TO_ENTRY
    protect_buffer_atr: float = 0.5
    trailing_mode: str = TRAILING_RANGE
    trailing_range_mult: float = 0.5
    trailing_percent: float = 1.0
    trailing_atr_mult: float = 1.0
    intrabar: str = INTRABAR_PESSIMISTIC
    session_end: time = DEFAULT_SESSION_END
    stop_at_session_end: bool = True
    # When True, use the position's RECORDED stop_loss / target_1 / target_2
    # instead of rebuilding them from ATR. This isolates "does the bar walk
    # match the engine's tick logic" from "is the ATR reconstruction exact".
    #
    # CAVEAT: the ledger's ``trades.stop_loss`` column is OVERWRITTEN by the
    # T1 protection step on any position that took a T1 partial (it becomes
    # the entry price), so recorded geometry is WRONG for exactly the
    # Profit-Capture positions. Measured ATR reconstruction against the
    # untouched rows is accurate to a median 0.03% on the stop and 0.04% on
    # T1, so ATR-derived geometry is the default and preferred basis.
    use_recorded_geometry: bool = False
    # Entry confirmation (Phase 6). None = disabled (baseline). A confirmation
    # is evaluated on the ENTRY bar only and uses no future information.
    entry_confirmation: Optional[str] = None
    entry_confirm_max_bars: int = 3

    @staticmethod
    def baseline() -> "ReplayConfig":
        return ReplayConfig(label="baseline")

    def with_label(self, label: str) -> "ReplayConfig":
        return replace(self, label=label)

    def profit_capture_config(self) -> ProfitCaptureConfig:
        return ProfitCaptureConfig(
            t1_exit_percent=self.t1_exit_percent,
            protect_mode=self.protect_mode,
            protect_buffer_atr=self.protect_buffer_atr,
            trailing_mode=self.trailing_mode,
            trailing_range_mult=self.trailing_range_mult,
            trailing_percent=self.trailing_percent,
            trailing_atr_mult=self.trailing_atr_mult,
        )


@dataclass
class ReplayResult:
    """Outcome of replaying one position under one configuration."""

    position_id: str
    symbol: str
    config_label: str
    traded: bool
    skip_reason: Optional[str] = None
    entry_price: float = 0.0
    stop_loss: float = 0.0
    target_1: float = 0.0
    target_2: float = 0.0
    risk_reward: Optional[float] = None
    quantity: float = 0.0
    pnl: float = 0.0
    atr: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_time: Optional[datetime] = None
    bars_held: int = 0
    holding_minutes: float = 0.0
    mfe: Optional[float] = None
    mae: Optional[float] = None
    mfe_r: Optional[float] = None
    mae_r: Optional[float] = None
    hit_t1: bool = False
    hit_t2: bool = False
    unresolved: bool = False
    initial_risk: Optional[float] = None
    r_multiple: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "config_label": self.config_label,
            "traded": self.traded,
            "skip_reason": self.skip_reason,
            "entry_price": round(self.entry_price, 4) if self.entry_price else None,
            "stop_loss": round(self.stop_loss, 4) if self.stop_loss else None,
            "target_1": round(self.target_1, 4) if self.target_1 else None,
            "target_2": round(self.target_2, 4) if self.target_2 else None,
            "risk_reward": round(self.risk_reward, 3) if self.risk_reward else None,
            "quantity": self.quantity,
            "pnl": round(self.pnl, 2),
            "atr": round(self.atr, 4) if self.atr is not None else None,
            "exit_price": round(self.exit_price, 4) if self.exit_price is not None else None,
            "exit_reason": self.exit_reason,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "bars_held": self.bars_held,
            "holding_minutes": round(self.holding_minutes, 2),
            "mfe": round(self.mfe, 4) if self.mfe is not None else None,
            "mae": round(self.mae, 4) if self.mae is not None else None,
            "mfe_r": round(self.mfe_r, 3) if self.mfe_r is not None else None,
            "mae_r": round(self.mae_r, 3) if self.mae_r is not None else None,
            "hit_t1": self.hit_t1,
            "hit_t2": self.hit_t2,
            "unresolved": self.unresolved,
            "r_multiple": round(self.r_multiple, 3) if self.r_multiple is not None else None,
        }


# ────────────────────────────────────────────────────────────────────────────
# ATR reconstruction (no-lookahead)
# ────────────────────────────────────────────────────────────────────────────

def atr_at(
    bars: Sequence[Bar],
    as_of: datetime,
    period: int = ATR_PERIOD,
) -> Optional[float]:
    """Wilder ATR(14) as it stood at ``as_of``, using ONLY bars whose timestamp
    is strictly earlier than ``as_of``.

    Mirrors ``app.services.indicators.atr`` (Wilder ewm alpha=1/period,
    adjust=False). Returns None when there is not enough history — the caller
    then skips the position rather than inventing a value.
    """
    history = [b for b in bars if b.timestamp < as_of]
    if len(history) < period:
        return None
    prev_close = history[0].close
    trs: list[float] = []
    for bar in history:
        tr = max(
            bar.high - bar.low,
            abs(bar.high - prev_close),
            abs(bar.low - prev_close),
        )
        trs.append(tr)
        prev_close = bar.close
    # Wilder smoothing over the whole available history (same recursion the
    # live engine applies, seeded by the first true range).
    value = trs[0]
    for tr in trs[1:]:
        value = value + (tr - value) / period
    return value if math.isfinite(value) and value > 0 else None


# ────────────────────────────────────────────────────────────────────────────
# Entry confirmation (Phase 6) — evaluated on the entry bar only
# ────────────────────────────────────────────────────────────────────────────

CONFIRMATIONS = (
    "price_above_vwap_long",
    "price_below_vwap_short",
    "adx_at_least",
    "relative_volume_at_least",
    "bullish_candle_long",
    "bearish_candle_short",
    # Direction-aware composites: the side-appropriate variant of the rule.
    "vwap_side",
    "candle_side",
)


def evaluate_entry_confirmation(
    kind: str,
    bars: Sequence[Bar],
    entry_index: int,
    direction: str,
    atr_value: Optional[float],
) -> tuple[bool, str]:
    """Test one entry-time confirmation. Returns (passed, human reason).

    Uses at most the entry bar and bars strictly BEFORE it. Never looks
    forward. An unavailable input FAILS the confirmation rather than passing
    it, so a confirmation can never be satisfied by missing data.
    """
    if entry_index < 0 or entry_index >= len(bars):
        return False, "no entry bar"
    bar = bars[entry_index]
    is_long = str(direction or "").upper() in ("LONG", "BUY")

    if kind == "price_above_vwap_long":
        if not is_long:
            return False, "not a LONG signal"
        vwap = session_vwap(bars[: entry_index + 1])
        if vwap is None:
            return False, "VWAP unavailable at entry"
        ok = bar.close > vwap
        return ok, f"close {bar.close:.2f} {'>' if ok else '<='} VWAP {vwap:.2f}"

    if kind == "price_below_vwap_short":
        if is_long:
            return False, "not a SHORT signal"
        vwap = session_vwap(bars[: entry_index + 1])
        if vwap is None:
            return False, "VWAP unavailable at entry"
        ok = bar.close < vwap
        return ok, f"close {bar.close:.2f} {'<' if ok else '>='} VWAP {vwap:.2f}"

    if kind == "candle_side":
        inner = "bullish_candle_long" if is_long else "bearish_candle_short"
        return evaluate_entry_confirmation(inner, bars, entry_index, direction, atr_value)

    if kind == "vwap_side":
        inner = "price_above_vwap_long" if is_long else "price_below_vwap_short"
        return evaluate_entry_confirmation(inner, bars, entry_index, direction, atr_value)

    if kind == "adx_at_least":
        return False, "ADX not reconstructed offline"

    if kind == "relative_volume_at_least":
        return False, "relative volume not reconstructed offline"

    if kind == "bullish_candle_long":
        if not is_long:
            return False, "not a LONG signal"
        ok = bar.close > bar.open
        return ok, f"entry candle {'bullish' if ok else 'not bullish'}"

    if kind == "bearish_candle_short":
        if is_long:
            return False, "not a SHORT signal"
        ok = bar.close < bar.open
        return ok, f"entry candle {'bearish' if ok else 'not bearish'}"

    return False, f"unknown confirmation {kind!r}"


def session_vwap(bars: Sequence[Bar]) -> Optional[float]:
    """VWAP over the given bars (typical price x volume). None when the
    window carries no volume at all — never a fabricated 0."""
    if not bars:
        return None
    cumulative_pv = 0.0
    cumulative_v = 0.0
    for bar in bars:
        volume = bar.volume or 0.0
        cumulative_pv += (bar.high + bar.low + bar.close) / 3.0 * volume
        cumulative_v += volume
    if cumulative_v <= 0:
        return None
    return cumulative_pv / cumulative_v


# ────────────────────────────────────────────────────────────────────────────
# Replay
# ────────────────────────────────────────────────────────────────────────────

def _is_long(direction: Optional[str]) -> bool:
    return str(direction or "").upper() in ("LONG", "BUY")


def replay_position(
    position,
    bars: Sequence[Bar],
    config: ReplayConfig,
    atr_value: Optional[float] = None,
) -> ReplayResult:
    """Replay one position under ``config`` over the real post-entry path.

    ``position`` is a ``profit_baseline.Position``. ``bars`` must contain the
    pre-entry history (used only for ATR/VWAP at entry) and the post-entry path.
    """
    entry_time = position.entry_time
    entry_price = float(position.entry_price or 0.0)
    direction = position.direction
    is_long = _is_long(direction)
    quantity = float(position.initial_quantity or position.quantity or 0.0)

    result = ReplayResult(
        position_id=position.position_id,
        symbol=position.symbol,
        config_label=config.label,
        traded=False,
        entry_price=entry_price,
        quantity=quantity,
    )

    if entry_time is None or entry_price <= 0 or quantity <= 0:
        result.skip_reason = "missing entry geometry"
        return result
    if not bars:
        result.skip_reason = "no market data"
        return result

    if atr_value is None:
        atr_value = atr_at(bars, entry_time)
    if atr_value is None:
        result.skip_reason = "ATR unavailable at entry (insufficient history)"
        return result
    result.atr = atr_value

    # --- candidate geometry -------------------------------------------------
    sign = 1.0 if is_long else -1.0
    if config.use_recorded_geometry:
        stop = float(position.stop_loss or 0.0)
        t1 = float(position.target_1 or 0.0)
        t2 = float(position.target_2 or 0.0)
        if stop <= 0 or t1 <= 0:
            result.skip_reason = "recorded geometry incomplete"
            return result
    else:
        stop = entry_price - sign * config.sl_atr_mult * atr_value
        if config.target_rr:
            risk0 = abs(entry_price - stop)
            t1 = entry_price + sign * config.target_rr * risk0
            t2 = entry_price + sign * (config.target_rr * risk0 + atr_value)
        else:
            t1 = entry_price + sign * config.t1_atr_mult * atr_value
            t2 = entry_price + sign * config.t2_atr_mult * atr_value
    risk = abs(entry_price - stop)
    reward = abs(t1 - entry_price)

    result.stop_loss = stop
    result.target_1 = t1
    result.target_2 = t2
    result.risk_reward = (reward / risk) if risk > 0 else None
    result.initial_risk = risk * quantity

    if risk <= 0:
        result.skip_reason = "zero risk geometry"
        return result

    # --- R:R gate -----------------------------------------------------------
    rr = result.risk_reward or 0.0
    if rr < config.min_rr:
        result.skip_reason = f"R/R {rr:.2f} below minimum {config.min_rr}"
        return result

    # --- optional entry-time confirmation -----------------------------------
    start = next((i for i, b in enumerate(bars) if b.timestamp >= entry_time), None)
    if start is None:
        result.skip_reason = "no bar at or after entry"
        return result
    if config.entry_confirmation:
        ok, reason = evaluate_entry_confirmation(
            config.entry_confirmation, bars, start, direction, atr_value
        )
        if not ok:
            result.skip_reason = f"entry confirmation failed: {reason}"
            return result
        # The confirmation may need a few bars to form; the trade is deferred
        # to the first bar where it passes, still within the allowed window.
        limit = start + max(0, config.entry_confirm_max_bars)
        deferred = None
        for i in range(start, min(limit + 1, len(bars))):
            ok, _ = evaluate_entry_confirmation(
                config.entry_confirmation, bars, i, direction, atr_value
            )
            if ok:
                deferred = i
                break
        if deferred is None:
            result.skip_reason = "entry confirmation never satisfied in window"
            return result
        start = deferred

    result.traded = True

    # --- walk the real path -------------------------------------------------
    pc = ProfitCaptureConfig(
        t1_exit_percent=config.t1_exit_percent,
        protect_mode=config.protect_mode,
        protect_buffer_atr=config.protect_buffer_atr,
        trailing_mode=config.trailing_mode,
        trailing_range_mult=config.trailing_range_mult,
        trailing_percent=config.trailing_percent,
        trailing_atr_mult=config.trailing_atr_mult,
    )

    pos: dict = {
        "direction": direction,
        "entry_price": entry_price,
        "stop_loss": stop,
        "target_1": t1,
        "target_2": t2,
        "atr_ref": atr_value,
        "trailing_active": False,
        "exit_stage": "ACTIVE",
    }
    remaining = quantity
    realized = 0.0
    best = entry_price
    worst = entry_price
    pessimistic = config.intrabar == INTRABAR_PESSIMISTIC

    for offset, index in enumerate(range(start, len(bars))):
        bar = bars[index]
        if bar.timestamp.time() >= config.session_end and bar.timestamp.date() > entry_time.date():
            # Beyond the entry session there is no intraday path; stop walking.
            break

        # Excursion bookkeeping uses the true bar extremes.
        best = max(best, bar.high) if is_long else min(best, bar.low)
        worst = min(worst, bar.low) if is_long else max(worst, bar.high)

        if pessimistic:
            stop_price = bar.low if is_long else bar.high
            target_price = bar.high if is_long else bar.low
        else:
            stop_price = target_price = bar.close

        # 1) stop-loss first (matches the live engine's priority)
        current_sl = pos.get("stop_loss") or 0.0
        hit_sl = (is_long and stop_price <= current_sl) or (
            not is_long and stop_price >= current_sl
        )
        if hit_sl:
            realized += (current_sl - entry_price) * remaining if is_long else (
                entry_price - current_sl
            ) * remaining
            result.exit_price = current_sl
            result.exit_reason = (
                "TRAILING_STOP"
                if pos.get("trailing_active") or pos.get("exit_stage") == "T1_EXECUTED"
                else "STOP_LOSS"
            )
            result.exit_time = bar.timestamp
            remaining = 0.0
            break

        stage = pos.get("exit_stage")
        current_t1 = pos.get("target_1") or 0.0
        current_t2 = pos.get("target_2") or 0.0

        if stage == "ACTIVE":
            hit_t1 = (is_long and target_price >= current_t1) or (
                not is_long and target_price <= current_t1
            )
            if not hit_t1:
                if config.stop_at_session_end and bar.timestamp.time() >= config.session_end:
                    realized += (bar.close - entry_price) * remaining if is_long else (
                        entry_price - bar.close
                    ) * remaining
                    result.exit_price = bar.close
                    result.exit_reason = "SESSION_END"
                    result.exit_time = bar.timestamp
                    result.hit_t1 = False
                    remaining = 0.0
                    break
                continue
            # T1 partial
            result.hit_t1 = True
            if config.t1_exit_percent >= 100:
                realized += (target_price - entry_price) * remaining if is_long else (
                    entry_price - target_price
                ) * remaining
                result.exit_price = target_price
                result.exit_reason = "TARGET_1"
                result.exit_time = bar.timestamp
                remaining = 0.0
                break
            exit_qty = round(remaining * config.t1_exit_percent / 100.0, 4)
            if exit_qty <= 0:
                result.skip_reason = "T1 partial rounds to zero quantity"
                return result
            realized += (target_price - entry_price) * exit_qty if is_long else (
                entry_price - target_price
            ) * exit_qty
            remaining = round(remaining - exit_qty, 4)
            pos["exit_stage"] = "T1_EXECUTED"
            pos["quantity"] = remaining
            pos["current_price"] = target_price
            from app.services.profit_capture import apply_t1_protection

            apply_t1_protection(pos, pc)
            if pc.trailing_mode != TRAILING_NONE:
                pos["trailing_active"] = True
            if remaining <= 0:
                result.exit_price = target_price
                result.exit_reason = "T1_PARTIAL"
                result.exit_time = bar.timestamp
                break
        else:
            update_trailing_stop(pos, pc, bar.close)

        # 2) after T1: T2 final or the protective/trailing stop
        current_t2 = pos.get("target_2") or 0.0
        current_sl = pos.get("stop_loss") or 0.0
        if pessimistic:
            target_price = bar.high if is_long else bar.low
        hit_t2 = (is_long and target_price >= current_t2) or (
            not is_long and target_price <= current_t2
        )
        if hit_t2:
            realized += (current_t2 - entry_price) * remaining if is_long else (
                entry_price - current_t2
            ) * remaining
            result.exit_price = current_t2
            result.exit_reason = "T2_FINAL"
            result.exit_time = bar.timestamp
            result.hit_t2 = True
            remaining = 0.0
            break
        if (is_long and target_price <= current_sl) or (
            not is_long and target_price >= current_sl
        ):
            realized += (current_sl - entry_price) * remaining if is_long else (
                entry_price - current_sl
            ) * remaining
            result.exit_price = current_sl
            result.exit_reason = "TRAILING_STOP"
            result.exit_time = bar.timestamp
            remaining = 0.0
            break
        if config.stop_at_session_end and bar.timestamp.time() >= config.session_end:
            realized += (bar.close - entry_price) * remaining if is_long else (
                entry_price - bar.close
            ) * remaining
            result.exit_price = bar.close
            result.exit_reason = "SESSION_END"
            result.exit_time = bar.timestamp
            remaining = 0.0
            break

        result.bars_held = offset + 1

    # --- finalize -----------------------------------------------------------
    result.pnl = round(realized, 2)
    result.mfe = round(abs(best - entry_price), 4)
    result.mae = round(abs(entry_price - worst), 4) if worst != entry_price else 0.0
    if risk > 0:
        result.mfe_r = round(result.mfe / risk, 3)
        result.mae_r = round(result.mae / risk, 3)
    if result.exit_time and entry_time:
        result.holding_minutes = round(
            max((result.exit_time - entry_time).total_seconds(), 0.0) / 60.0, 2
        )
    if remaining > 0 and result.exit_price is None:
        result.unresolved = True
    if result.initial_risk:
        result.r_multiple = round(result.pnl / result.initial_risk, 4)
    return result


def replay_many(
    positions: Iterable,
    bars_by_symbol: dict,
    config: ReplayConfig,
) -> list[ReplayResult]:
    """Replay a set of positions, looking up each symbol's bar series."""
    results: list[ReplayResult] = []
    for position in positions:
        bars = bars_by_symbol.get(position.symbol) or []
        results.append(
            replay_position(position, bars, config, atr_value=position.atr)
        )
    return results
