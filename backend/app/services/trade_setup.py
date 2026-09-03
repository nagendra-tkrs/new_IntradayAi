"""User trade-setup domain logic (Entry / Stop Loss / Target override).

This module provides a single, reusable implementation of:

  * R:R calculation using the project's existing convention:
        risk   = abs(entry - stop_loss)
        reward = abs(target - entry)
        rr     = reward / risk
    (the same convention used by signal_engine.compute_trade_setup and the
    backtesting/risk modules - this is the authoritative/domain calculation).

  * BUY / SELL relational validation:
        BUY  -> stop_loss < entry < target
        SELL -> target < entry < stop_loss

It deliberately does NOT modify the AI signal strategy or its original values.
The AI setup (signal.setup) is preserved separately; the user override lives in
its own record identified by override_active.

The authoritative R:R used for persistence is recomputed here on the server so a
malicious/incorrect client value can never be stored unchecked.
"""

import math
from typing import Optional, Tuple


class TradeSetupResult:
    """Result of validating + computing a user trade setup."""

    __slots__ = ("valid", "errors", "entry", "stop_loss", "target", "risk_reward")

    def __init__(self, valid, errors, entry=None, stop_loss=None, target=None, risk_reward=None):
        self.valid = valid
        self.errors = errors
        self.entry = entry
        self.stop_loss = stop_loss
        self.target = target
        self.risk_reward = risk_reward


_error_map_buy = {
    "entry_missing": "Buy price is required.",
    "sl_missing": "Stop Loss is required.",
    "target_missing": "Target is required.",
    "entry_invalid": "Buy price must be a positive number.",
    "sl_invalid": "Stop Loss must be a positive number.",
    "target_invalid": "Target must be a positive number.",
    "zero_risk": "Entry and Stop Loss cannot be equal.",
    "sl_above": "SL must be below Buy Price for a BUY setup.",
    "target_below": "Target must be above Buy Price for a BUY setup.",
}

_error_map_sell = {
    "entry_missing": "Entry price is required.",
    "sl_missing": "Stop Loss is required.",
    "target_missing": "Target is required.",
    "entry_invalid": "Entry price must be a positive number.",
    "sl_invalid": "Stop Loss must be a positive number.",
    "target_invalid": "Target must be a positive number.",
    "zero_risk": "Entry and Stop Loss cannot be equal.",
    "sl_below": "SL must be above Entry price for a SELL setup.",
    "target_above": "Target must be below Entry price for a SELL setup.",
}


def _coerce(value) -> Tuple[bool, Optional[float]]:
    """Coerce an untrusted input to a finite positive float.

    Handles None, empty string, non-numeric strings, NaN and Infinity without
    raising. Returns (ok, value)."""
    if value is None:
        return False, None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return False, None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return False, None
    if not math.isfinite(num):
        return False, None
    if num <= 0:
        return False, None
    return True, round(num, 2)


def compute_risk_reward(entry, stop_loss, target, direction="LONG") -> TradeSetupResult:
    """Validate a user trade setup and compute R:R using the project convention.

    direction: "BUY"/"LONG" or "SELL"/"SHORT" (case-insensitive; substrings match).

    Returns a TradeSetupResult. When valid, risk_reward is the authoritative
    server-computed R:R. Zero-risk (entry == stop_loss) yields invalid with a
    specific error so the caller never divides by zero.
    """
    dir_str = (direction or "LONG").upper()
    is_long = dir_str in ("BUY", "LONG") or "LONG" in dir_str.split("_")
    map_ = _error_map_buy if is_long else _error_map_sell

    entry_ok, entry_v = _coerce(entry)
    sl_ok, sl_v = _coerce(stop_loss)
    tgt_ok, tgt_v = _coerce(target)

    errors = []
    if not entry_ok:
        errors.append(map_["entry_missing" if not _present(entry) else "entry_invalid"])
    if not sl_ok:
        errors.append(map_["sl_missing" if not _present(stop_loss) else "sl_invalid"])
    if not tgt_ok:
        errors.append(map_["target_missing" if not _present(target) else "target_invalid"])
    if errors:
        return TradeSetupResult(False, errors)

    if entry_v == sl_v:
        return TradeSetupResult(False, [map_["zero_risk"]])

    if is_long:
        if sl_v >= entry_v:
            errors.append(map_["sl_above"])
        if tgt_v <= entry_v:
            errors.append(map_["target_below"])
    else:
        if sl_v <= entry_v:
            errors.append(map_["sl_below"])
        if tgt_v >= entry_v:
            errors.append(map_["target_above"])

    if errors:
        return TradeSetupResult(False, errors)

    risk = abs(entry_v - sl_v)
    reward = abs(tgt_v - entry_v)
    rr = round(reward / risk, 2) if risk > 0 else None
    return TradeSetupResult(True, [], entry_v, sl_v, tgt_v, rr)


def _present(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True
