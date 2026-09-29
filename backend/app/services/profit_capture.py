"""Profit Capture layer for the paper-trading engine.

Adds a configurable, *controlled* profit-capture flow on top of the existing
SL/Target monitor without touching the signal engine, quality scoring, risk
gate, or the accounting contract:

    Entry -> Target 1: partial exit (default 50%)   [T1_PARTIAL]
             -> stop-loss moved to entry (protected, never loosened)
             -> remaining position managed by Target 2 / trailing stop
    Entry -> Stop Loss (before T1): full close      [STOP_LOSS]
    Target 2 reached (after T1):      final close   [T2_FINAL]
    Protective/trailing stop hit:     final close   [TRAILING_STOP]
    Manual close:                     full close    [MANUAL_CLOSE]

Everything here is decision-free configuration + pure math. The PaperAccount
in ``paper_trading.py`` applies these rules inside ``check_stops`` and
``close_position``. No profitability claim is made anywhere in this module:
it only routes exits and risk levels; outcomes are measured by the existing
paper ledger, risk engine and backtest, never asserted here.

Exit-reason contract (recorded on every realized ledger row):

    T1_PARTIAL    partial exit when price first reaches Target 1
    T2_FINAL      remaining position closed when price reaches Target 2
    TRAILING_STOP protective/trailing stop-loss hit after a T1 partial
    STOP_LOSS     initial stop-loss hit (Target 1 not reached yet)
    MANUAL_CLOSE  manual "close position" action
    TARGET_1      full close at Target 1 (legacy 100%-at-T1 configuration)
"""

from dataclasses import dataclass, field
from typing import Optional

# Exit reasons (the possible-reasons contract).
EXIT_REASON_T1_PARTIAL = "T1_PARTIAL"
EXIT_REASON_T2_FINAL = "T2_FINAL"
EXIT_REASON_TRAILING_STOP = "TRAILING_STOP"
EXIT_REASON_STOP_LOSS = "STOP_LOSS"
EXIT_REASON_MANUAL_CLOSE = "MANUAL_CLOSE"
EXIT_REASON_TARGET_1 = "TARGET_1"
EXIT_REASONS = (
    EXIT_REASON_T1_PARTIAL,
    EXIT_REASON_T2_FINAL,
    EXIT_REASON_TRAILING_STOP,
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_MANUAL_CLOSE,
    EXIT_REASON_TARGET_1,
)

# Stop-loss protection applied the moment a T1 partial exit executes.
PROTECT_MOVE_TO_ENTRY = "MOVE_SL_TO_ENTRY"
PROTECT_ENTRY_PLUS_BUFFER = "MOVE_SL_TO_ENTRY_PLUS_BUFFER"
PROTECT_KEEP_EXISTING = "KEEP_EXISTING_SL"
PROTECT_MODES = (PROTECT_MOVE_TO_ENTRY, PROTECT_ENTRY_PLUS_BUFFER, PROTECT_KEEP_EXISTING)

# Trailing-stop modes (applies only to the REMAINING quantity after T1).
TRAILING_NONE = "NONE"            # trailing disabled; only the protected SL runs
TRAILING_RANGE = "RANGE"          # trail distance = mult * (T1 - entry)
TRAILING_PERCENT = "PERCENT"      # trail distance = % of the observed price
TRAILING_ATR = "ATR"              # trail distance = mult * ATR snapshot
TRAILING_MODES = (TRAILING_NONE, TRAILING_RANGE, TRAILING_PERCENT, TRAILING_ATR)

# Exit stages for an OPEN position (frontend state machine).
STAGE_ACTIVE = "ACTIVE"            # T1 not yet reached
STAGE_T1_EXECUTED = "T1_EXECUTED"  # T1 partial done, T2/trailing manage the rest

_QUALITY_LEVELS = ("REJECTED", "WEAK", "NORMAL", "QUALIFIED", "PREMIUM")


@dataclass
class ProfitCaptureConfig:
    """Conservative, fully-configurable profit-capture defaults.

    Every value is a setting-controlled default (see ``from_settings``); the
    module defaults below are the documented conservative baseline:

      * 50% of the position exits at T1 (remaining 50% keeps running);
      * the stop-loss is moved to entry at T1 (never loosened below it);
      * a RANGE trailing stop chases the remaining position at half the
        T1->entry distance.
    """

    t1_exit_percent: float = 50.0
    protect_mode: str = PROTECT_MOVE_TO_ENTRY
    protect_buffer_atr: float = 0.5
    trailing_mode: str = TRAILING_RANGE
    trailing_range_mult: float = 0.5
    trailing_percent: float = 1.0
    trailing_atr_mult: float = 1.0

    def __post_init__(self):
        if self.protect_mode not in PROTECT_MODES:
            self.protect_mode = PROTECT_MOVE_TO_ENTRY
        if self.trailing_mode not in TRAILING_MODES:
            self.trailing_mode = TRAILING_RANGE
        try:
            self.t1_exit_percent = max(0.0, float(self.t1_exit_percent))
        except (TypeError, ValueError):
            self.t1_exit_percent = 50.0

    @property
    def remaining_percent(self) -> float:
        """Fraction of the position that keeps running after the T1 partial."""
        return max(0.0, 100.0 - self.t1_exit_percent)

    @classmethod
    def from_settings(cls, settings=None) -> "ProfitCaptureConfig":
        if settings is None:
            from app.core.config import settings as _s
            settings = _s
        return cls(
            t1_exit_percent=settings.T1_EXIT_PERCENT,
            protect_mode=settings.T1_PROTECT_MODE,
            protect_buffer_atr=settings.T1_PROTECT_BUFFER_ATR,
            trailing_mode=settings.TRAILING_MODE,
            trailing_range_mult=settings.TRAILING_RANGE_MULT,
            trailing_percent=settings.TRAILING_PERCENT,
            trailing_atr_mult=settings.TRAILING_ATR_MULT,
        )

    @classmethod
    def legacy_full_close(cls) -> "ProfitCaptureConfig":
        """The pre-profit-capture behavior: 100% exit at Target 1, no
        protection move, no trailing. Kept for backward-compatible tests and
        any caller that explicitly opts into the legacy contract."""
        return cls(
            t1_exit_percent=100.0,
            protect_mode=PROTECT_KEEP_EXISTING,
            trailing_mode=TRAILING_NONE,
        )


def trailing_distance(pos: dict, config: ProfitCaptureConfig, price: float) -> Optional[float]:
    """Distance below (LONG) / above (SHORT) the observed price at which the
    trailing stop sits. Returns None when trailing is disabled or the distance
    cannot be derived (the stop is then left untouched — never loosened)."""
    mode = config.trailing_mode
    entry = pos.get("entry_price") or 0
    t1 = pos.get("target_1") or 0
    atr = pos.get("atr_ref") or 0

    if mode == TRAILING_RANGE:
        base = abs(t1 - entry)
        if base <= 0:
            return None
        return base * config.trailing_range_mult
    if mode == TRAILING_PERCENT:
        ref = price or pos.get("current_price") or entry
        if ref <= 0:
            return None
        return ref * (config.trailing_percent / 100.0)
    if mode == TRAILING_ATR:
        if atr and atr > 0:
            return atr * config.trailing_atr_mult
        base = abs(t1 - entry)  # fallback to RANGE semantics when no ATR snapshot
        if base <= 0:
            return None
        return base * config.trailing_range_mult
    return None


def update_trailing_stop(pos: dict, config: ProfitCaptureConfig, price: float) -> bool:
    """Move the protective/trailing stop in place. NEVER loosens: a LONG stop
    only rises, a SHORT stop only falls, and neither ever retreats past the
    current stop. Returns True when the stop actually changed."""
    if config.trailing_mode == TRAILING_NONE:
        return False
    dist = trailing_distance(pos, config, price)
    if dist is None or dist <= 0 or not price or price <= 0:
        return False
    is_long = pos.get("direction") in ("LONG", "BUY")
    old = pos.get("stop_loss") or 0
    if is_long:
        candidate = price - dist
        if candidate > old:
            pos["stop_loss"] = round(candidate, 4)
            pos["trailing_active"] = True
            return True
    else:
        candidate = price + dist
        if candidate < old:
            pos["stop_loss"] = round(candidate, 4)
            pos["trailing_active"] = True
            return True
    return False


def apply_t1_protection(pos: dict, config: ProfitCaptureConfig) -> bool:
    """Move the stop-loss to protect the trade at the T1 partial exit.

    MOVE_SL_TO_ENTRY:               stop = entry
    MOVE_SL_TO_ENTRY_PLUS_BUFFER:   stop = entry + buffer (LONG) / entry - buffer
                                    (SHORT), buffer = protect_buffer_atr * ATR
                                    (falls back to plain entry without an ATR
                                    snapshot)
    KEEP_EXISTING_SL:               no change

    Only ever TIGHTENS the stop (never loosens). Returns True when changed."""
    mode = config.protect_mode
    if mode == PROTECT_KEEP_EXISTING:
        return False
    is_long = pos.get("direction") in ("LONG", "BUY")
    entry = pos.get("entry_price") or 0
    buffer = 0.0
    if mode == PROTECT_ENTRY_PLUS_BUFFER:
        atr = pos.get("atr_ref") or 0
        if atr and atr > 0:
            buffer = atr * config.protect_buffer_atr
    candidate = (entry + buffer) if is_long else (entry - buffer)
    old = pos.get("stop_loss") or 0
    changed = (candidate > old) if is_long else (candidate < old)
    if changed:
        pos["stop_loss"] = round(candidate, 4)
        pos["trailing_active"] = True
    return changed