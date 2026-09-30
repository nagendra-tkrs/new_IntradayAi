"""Realized-profit baseline and attribution analysis for IntradayAI.

This module is **measurement only**. It reads the existing paper-trading
ledger and reduces it to the twenty baseline statistics the optimization
program needs, plus the groupings used to find where money is lost. It never
mutates a trade, never invents an observation, and never treats a
counterfactual as realized performance.

Two reductions are applied before anything is counted, because the raw
``trades`` table mixes three genuinely different populations:

1. **Position collapsing** — a Profit-Capture position writes ONE ledger row
   per exit slice (``T1_PARTIAL`` then ``T2_FINAL``/``TRAILING_STOP``). Those
   rows share ``details_json.position_id`` and must be summed into ONE trade
   before win-rate / expectancy are computed. Counting slices would inflate
   the sample and double-count capital.

2. **Cohort classification** — the ledger contains API smoke-test fixtures
   (sub-second holds, hand-typed round price levels, exact duplicate rows)
   alongside real strategy trades. These are separated by an explicit,
   configurable holding-time floor and reported as their own cohort. They are
   never deleted, never relabelled, and the full unfiltered baseline is
   always reported alongside the filtered one.

Every statistic is derived from ``trades.pnl`` — the *realized* ledger value.
Signal-level ``realized_pnl`` is deliberately NOT used as the performance
source of truth: see ``app.services.signal_store.mark_signal_outcome``
position-level accumulation, which is what makes it trustworthy.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

# A real intraday position cannot be meaningfully managed inside this many
# seconds: the paper monitor ticks every PAPER_TRADING_MONITOR_INTERVAL_SECONDS
# (5s live) and the observed sub-second holds in the ledger are all API
# smoke-test calls against the order endpoint. This floor is a REPORTING
# boundary only - it is configurable and never deletes or rewrites a row.
MIN_HOLDING_SECONDS_FOR_STRATEGY_COHORT = 60.0

# Sample size below which no performance conclusion may be drawn. Mirrors
# app.services.ai_performance_monitor.MIN_TRADES_FOR_CONCLUSION.
MIN_TRADES_FOR_CONCLUSION = 30

INSUFFICIENT_DATA = "INSUFFICIENT DATA"
INSUFFICIENT_PROMOTION = "INSUFFICIENT REALIZED DATA FOR SAFE PROMOTION."

# Cohort identifiers.
COHORT_AI_LINKED = "AI_LINKED"
COHORT_STRATEGY_UNLINKED = "STRATEGY_UNLINKED"
COHORT_TEST_FIXTURE = "TEST_FIXTURE"
COHORTS = (COHORT_AI_LINKED, COHORT_STRATEGY_UNLINKED, COHORT_TEST_FIXTURE)

# Canonical groupings requested by the optimization program.
SCORE_BANDS: tuple[tuple[float, float, str], ...] = (
    (0, 30, "0-30"),
    (31, 45, "31-45"),
    (46, 55, "46-55"),
    (56, 65, "56-65"),
    (66, 75, "66-75"),
    (76, 85, "76-85"),
    (86, 100, "86-100"),
)

QUALITY_LEVELS = ("REJECTED", "WEAK", "NORMAL", "QUALIFIED", "PREMIUM")

TIME_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("09:15-10:00", 9 * 60 + 15, 10 * 60),
    ("10:00-11:00", 10 * 60, 11 * 60),
    ("11:00-12:00", 11 * 60, 12 * 60),
    ("12:00-13:00", 12 * 60, 13 * 60),
    ("13:00-14:00", 13 * 60, 14 * 60),
    ("14:00-15:30", 14 * 60, 15 * 60 + 30),
)

# Holding-time buckets in minutes (inclusive lower bound).
HOLDING_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0-5m", 0.0, 5.0),
    ("5-15m", 5.0, 15.0),
    ("15-30m", 15.0, 30.0),
    ("30-60m", 30.0, 60.0),
    ("60-120m", 60.0, 120.0),
    ("120m+", 120.0, float("inf")),
)

ANALYSIS_EXIT_REASONS = (
    "STOP_LOSS",
    "T1_PARTIAL",
    "T2_FINAL",
    "TRAILING_STOP",
    "MANUAL_CLOSE",
    "TARGET_1",
)


# ────────────────────────────────────────────────────────────────────────────
# Small numeric helpers
# ────────────────────────────────────────────────────────────────────────────

def _finite(value: Any) -> Optional[float]:
    """Return a finite float, else None. Never raises, never fabricates."""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    for suffix in ("+00:00", "Z"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    # Drop a trailing UTC offset such as "+05:30".
    if len(text) > 6 and text[-6] in "+-" and text[-3] == ":":
        text = text[:-6]
    text = text.split(".")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _details(row: dict) -> dict:
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


# ────────────────────────────────────────────────────────────────────────────
# Position reduction
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class Position:
    """One collapsed position: every Profit-Capture slice summed into one trade."""

    position_id: str
    symbol: str
    direction: str
    side: str
    entry_price: float
    quantity: float
    initial_quantity: float
    stop_loss: Optional[float]
    target_1: Optional[float]
    target_2: Optional[float]
    pnl: float
    entry_time: Optional[datetime]
    exit_time: Optional[datetime]
    holding_minutes: float
    exit_price: Optional[float] = None
    exit_reasons: list[str] = field(default_factory=list)
    terminal_reason: Optional[str] = None
    signal_id: Optional[str] = None
    user_id: Optional[str] = None
    row_ids: list[str] = field(default_factory=list)
    signal: Optional[dict] = None

    # -- derived ---------------------------------------------------------
    @property
    def result(self) -> str:
        if self.pnl > 0:
            return "WIN"
        if self.pnl < 0:
            return "LOSS"
        return "BREAKEVEN"

    @property
    def is_ai_linked(self) -> bool:
        return bool(self.signal_id)

    @property
    def risk_per_share(self) -> Optional[float]:
        if self.entry_price is None or self.stop_loss is None:
            return None
        return abs(self.entry_price - self.stop_loss)

    @property
    def reward_per_share(self) -> Optional[float]:
        if self.entry_price is None or self.target_1 is None:
            return None
        return abs(self.target_1 - self.entry_price)

    @property
    def risk_reward(self) -> Optional[float]:
        risk = self.risk_per_share
        if not risk:
            return None
        reward = self.reward_per_share or 0.0
        return reward / risk

    @property
    def initial_risk_amount(self) -> Optional[float]:
        risk = self.risk_per_share
        if risk is None or not self.initial_quantity:
            return None
        return risk * self.initial_quantity

    @property
    def r_multiple(self) -> Optional[float]:
        """Realized P&L expressed in units of initial risk (the risk engine's
        own unit of account). None when the risk geometry is unknown."""
        risk = self.initial_risk_amount
        if not risk:
            return None
        return self.pnl / risk

    @property
    def atr(self) -> Optional[float]:
        if not self.signal:
            return None
        return _finite(self.signal.get("atr"))

    @property
    def signal_score(self) -> Optional[float]:
        if not self.signal:
            return None
        return _finite(self.signal.get("signal_score"))

    @property
    def confidence(self) -> Optional[float]:
        if not self.signal:
            return None
        return _finite(self.signal.get("confidence"))

    @property
    def signal_quality(self) -> Optional[str]:
        if not self.signal:
            return None
        value = self.signal.get("signal_quality")
        return str(value).upper() if value else None

    @property
    def strategy_version(self) -> Optional[str]:
        if not self.signal:
            return None
        value = self.signal.get("strategy_version")
        return str(value) if value else None

    @property
    def market_context(self) -> dict:
        if not self.signal:
            return {}
        raw = self.signal.get("market_context")
        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def cohort(
        self,
        min_holding_seconds: float = MIN_HOLDING_SECONDS_FOR_STRATEGY_COHORT,
    ) -> str:
        """Classify this position. AI linkage always wins over the holding-time
        test so a linked trade is never demoted by it."""
        if self.is_ai_linked:
            return COHORT_AI_LINKED
        if self.holding_minutes * 60.0 < min_holding_seconds:
            return COHORT_TEST_FIXTURE
        return COHORT_STRATEGY_UNLINKED

    def score_band(self) -> Optional[str]:
        score = self.signal_score
        if score is None:
            return None
        for lo, hi, label in SCORE_BANDS:
            if lo <= score <= hi:
                return label
        return None

    def time_bucket(self) -> Optional[str]:
        if not self.entry_time:
            return None
        minutes = self.entry_time.hour * 60 + self.entry_time.minute
        for label, lo, hi in TIME_BUCKETS:
            if lo <= minutes < hi:
                return label
        # Before the first bucket / after the last: still report the raw window.
        return "OUTSIDE" if minutes < TIME_BUCKETS[0][1] else "OUTSIDE"

    def holding_bucket(self) -> str:
        for label, lo, hi in HOLDING_BUCKETS:
            if lo <= self.holding_minutes < hi:
                return label
        return HOLDING_BUCKETS[-1][0]

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "side": self.side,
            "entry_price": self.entry_price,
            "initial_quantity": self.initial_quantity,
            "stop_loss": self.stop_loss,
            "target_1": self.target_1,
            "target_2": self.target_2,
            "pnl": round(self.pnl, 2),
            "result": self.result,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_price": self.exit_price,
            "holding_minutes": round(self.holding_minutes, 2),
            "exit_reasons": list(self.exit_reasons),
            "terminal_reason": self.terminal_reason,
            "signal_id": self.signal_id,
            "cohort": self.cohort(),
            "signal_score": self.signal_score,
            "confidence": self.confidence,
            "signal_quality": self.signal_quality,
            "strategy_version": self.strategy_version,
            "atr": self.atr,
            "risk_reward": round(self.risk_reward, 3) if self.risk_reward else None,
            "r_multiple": round(self.r_multiple, 3) if self.r_multiple is not None else None,
            "score_band": self.score_band(),
            "time_bucket": self.time_bucket(),
            "holding_bucket": self.holding_bucket(),
        }


def side_of(direction: Optional[str]) -> str:
    d = str(direction or "").upper()
    if "LONG" in d or d in ("BUY", "B"):
        return "LONG"
    if "SHORT" in d or d in ("SELL", "S"):
        return "SHORT"
    return "UNKNOWN"


def collapse_positions(
    rows: Iterable[dict],
    signals_by_id: Optional[dict] = None,
    min_holding_seconds: float = MIN_HOLDING_SECONDS_FOR_STRATEGY_COHORT,
) -> list[Position]:
    """Reduce ledger rows to positions, summing every Profit-Capture slice.

    Rows are grouped by ``details_json.position_id`` when present, otherwise by
    the row's own id (a single-close position is its own group). Ordering is
    by exit time so the equity curve and drawdown are chronological.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        det = _details(row)
        key = str(det.get("position_id") or row.get("id") or "")
        groups.setdefault(key, []).append(row)

    positions: list[Position] = []
    for key, group in groups.items():
        group = sorted(
            group,
            key=lambda r: _parse_dt(r.get("exit_time")) or _parse_dt(r.get("entry_time"))
            or datetime.min,
        )
        first = group[0]
        det_first = _details(first)

        pnl = 0.0
        quantities = 0.0
        initial_quantity = 0.0
        signal_ids: list[str] = []
        reasons: list[str] = []
        entry_time: Optional[datetime] = None
        exit_time: Optional[datetime] = None
        exit_price: Optional[float] = None
        entry_price = _finite(first.get("entry_price")) or 0.0

        for row in group:
            det = _details(row)
            pnl += _finite(row.get("pnl")) or 0.0
            quantities += _finite(row.get("quantity")) or 0.0
            initial_quantity = max(
                initial_quantity, _finite(det.get("initial_quantity")) or 0.0
            )
            if row.get("signal_id"):
                signal_ids.append(str(row["signal_id"]))
            reason = det.get("exit_reason")
            if reason:
                reasons.append(str(reason))
            et = _parse_dt(row.get("entry_time"))
            if et and (entry_time is None or et < entry_time):
                entry_time = et
            if entry_price == 0.0:
                entry_price = _finite(row.get("entry_price")) or 0.0
            xt = _parse_dt(row.get("exit_time"))
            if xt and (exit_time is None or xt > exit_time):
                exit_time = xt
                exit_price = _finite(row.get("exit_price"))

        if initial_quantity <= 0:
            initial_quantity = quantities
        if exit_time is None and entry_time is not None:
            exit_time = entry_time
        holding_minutes = (
            max((exit_time - entry_time).total_seconds(), 0.0) / 60.0
            if entry_time and exit_time
            else 0.0
        )

        signal_id = signal_ids[0] if signal_ids else None
        signal = (signals_by_id or {}).get(signal_id) if signal_id else None

        pos = Position(
            position_id=key,
            symbol=str(first.get("symbol") or ""),
            direction=str(first.get("direction") or ""),
            side=side_of(first.get("direction")),
            entry_price=entry_price,
            quantity=quantities,
            initial_quantity=initial_quantity,
            stop_loss=_finite(first.get("stop_loss")),
            target_1=_finite(first.get("target_1")),
            target_2=_finite(first.get("target_2")),
            pnl=round(pnl, 2),
            entry_time=entry_time,
            exit_time=exit_time,
            exit_price=exit_price,
            holding_minutes=holding_minutes,
            exit_reasons=reasons,
            terminal_reason=reasons[-1] if reasons else None,
            signal_id=signal_id,
            user_id=first.get("user_id"),
            row_ids=[str(r.get("id") or "") for r in group],
            signal=signal,
        )
        positions.append(pos)

    positions.sort(
        key=lambda p: (p.exit_time or datetime.min, p.position_id)
    )
    return positions


# ────────────────────────────────────────────────────────────────────────────
# Statistics
# ────────────────────────────────────────────────────────────────────────────

def max_drawdown(pnls: Iterable[float]) -> float:
    """Peak-to-trough drawdown of the cumulative realized-P&L curve, in the
    same currency units as the inputs. Returns 0.0 for an empty series."""
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst)


def profit_factor(gross_profit: float, gross_loss: float) -> Optional[float]:
    if gross_loss <= 0:
        return None if gross_profit <= 0 else float("inf")
    return gross_profit / gross_loss


def expectancy_from_stats(
    wins: int, losses: int, avg_winner: Optional[float], avg_loser: Optional[float]
) -> Optional[float]:
    """EXPECTANCY = (win_rate x average_winner) - (loss_rate x average_loser).

    Returned in currency units per trade. None when either side is empty
    (the term is undefined rather than zero — a cohort with no losers has no
    measurable expectancy yet).
    """
    total = wins + losses
    if total == 0 or avg_winner is None or avg_loser is None:
        return None
    win_rate = wins / total
    return win_rate * avg_winner - (1.0 - win_rate) * avg_loser


def compute_stats(positions: list[Position]) -> dict:
    """The full realized baseline for one set of positions.

    Uses ONLY ``Position.pnl`` (the summed realized ledger value). Every rate
    that has no observation is reported as ``None`` with an explicit
    ``INSUFFICIENT DATA`` marker rather than as 0.0, so a missing observation
    can never be mistaken for a measured zero.
    """
    pnls = [p.pnl for p in positions]
    total = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    breakevens = [p for p in pnls if p == 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net = sum(pnls)
    avg_winner = (gross_profit / len(wins)) if wins else None
    avg_loser = (gross_loss / len(losses)) if losses else None
    expectancy = expectancy_from_stats(len(wins), len(losses), avg_winner, avg_loser)

    durations = [p.holding_minutes for p in positions]
    r_multiples = [p.r_multiple for p in positions if p.r_multiple is not None]
    rr_values = [p.risk_reward for p in positions if p.risk_reward is not None]

    reasons = [p.terminal_reason for p in positions if p.terminal_reason]

    def _rate(predicate) -> Optional[float]:
        observed = [p for p in positions if predicate(p)]
        if not observed:
            return None
        return len(observed) / total

    stats = {
        "total_completed_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakevens),
        "win_rate": (len(wins) / total) if total else None,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(net, 2),
        "average_winner": round(avg_winner, 2) if avg_winner is not None else None,
        "average_loser": round(avg_loser, 2) if avg_loser is not None else None,
        "profit_factor": (
            None
            if profit_factor(gross_profit, gross_loss) is None
            else round(profit_factor(gross_profit, gross_loss), 4)
        ),
        "expectancy": round(expectancy, 4) if expectancy is not None else None,
        "max_drawdown": round(max_drawdown(pnls), 2),
        "average_holding_minutes": (
            round(sum(durations) / len(durations), 2) if durations else None
        ),
        "t1_hit_rate": _rate(lambda p: "T1_PARTIAL" in p.exit_reasons
                             or p.terminal_reason in ("T1_PARTIAL", "T2_FINAL",
                                                      "TRAILING_STOP")),
        "t2_hit_rate": _rate(lambda p: p.terminal_reason == "T2_FINAL"),
        "trailing_stop_exit_rate": _rate(lambda p: p.terminal_reason == "TRAILING_STOP"),
        "stop_loss_exit_rate": _rate(lambda p: p.terminal_reason == "STOP_LOSS"),
        "manual_exit_rate": _rate(lambda p: p.terminal_reason == "MANUAL_CLOSE"),
        "target_1_exit_rate": _rate(lambda p: p.terminal_reason == "TARGET_1"),
        "unclassified_exit_rate": _rate(lambda p: not p.terminal_reason),
        "exit_reason_counts": {r: reasons.count(r) for r in sorted(set(reasons))},
        "average_r_multiple": (
            round(sum(r_multiples) / len(r_multiples), 4) if r_multiples else None
        ),
        "average_risk_reward": (
            round(sum(rr_values) / len(rr_values), 4) if rr_values else None
        ),
        "min_trades_required": MIN_TRADES_FOR_CONCLUSION,
    }
    stats["sample_sufficient"] = total >= MIN_TRADES_FOR_CONCLUSION
    stats["data_status"] = INSUFFICIENT_DATA if total == 0 else (
        "OK" if stats["sample_sufficient"] else INSUFFICIENT_DATA
    )
    return stats


def group_stats(positions: list[Position], key) -> dict[str, dict]:
    """Group positions by ``key(pos) -> label`` and compute the same statistics
    per group. Groups are returned in first-appearance order."""
    buckets: dict[str, list[Position]] = {}
    for pos in positions:
        label = key(pos)
        if label is None:
            continue
        buckets.setdefault(str(label), []).append(pos)
    return {label: compute_stats(items) for label, items in buckets.items()}


def groups_by_score_band(positions: list[Position]) -> dict[str, dict]:
    """Score bands 0-30 / 31-45 / 46-55 / 56-65 / 66-75 / 76-85 / 86-100.

    Every band is present in the output (empty ones flagged INSUFFICIENT DATA)
    so a missing band is visibly missing rather than absent.
    """
    scored = [p for p in positions if p.score_band()]
    out = group_stats(scored, lambda p: p.score_band())
    for _, _, label in SCORE_BANDS:
        out.setdefault(label, compute_stats([]))
    return out


def groups_by_quality(positions: list[Position]) -> dict[str, dict]:
    scored = [p for p in positions if p.signal_quality]
    out = group_stats(scored, lambda p: p.signal_quality)
    for level in QUALITY_LEVELS:
        out.setdefault(level, compute_stats([]))
    return out


def groups_by_direction(positions: list[Position]) -> dict[str, dict]:
    out = group_stats(positions, lambda p: p.side)
    for side in ("LONG", "SHORT"):
        out.setdefault(side, compute_stats([]))
    return out


def groups_by_time(positions: list[Position]) -> dict[str, dict]:
    timed = [p for p in positions if p.time_bucket() != "OUTSIDE"]
    out = group_stats(timed, lambda p: p.time_bucket())
    for label, _, _ in TIME_BUCKETS:
        out.setdefault(label, compute_stats([]))
    return out


def groups_by_symbol(positions: list[Position]) -> dict[str, dict]:
    return group_stats(positions, lambda p: p.symbol)


def groups_by_exit_reason(positions: list[Position]) -> dict[str, dict]:
    out = group_stats(
        [p for p in positions if p.terminal_reason], lambda p: p.terminal_reason
    )
    for reason in ANALYSIS_EXIT_REASONS:
        out.setdefault(reason, compute_stats([]))
    return out


def groups_by_holding(positions: list[Position]) -> dict[str, dict]:
    out = group_stats(positions, lambda p: p.holding_bucket())
    for label, _, _ in HOLDING_BUCKETS:
        out.setdefault(label, compute_stats([]))
    return out


# ────────────────────────────────────────────────────────────────────────────
# Baseline report
# ────────────────────────────────────────────────────────────────────────────

def build_baseline(
    trade_rows: Iterable[dict],
    signals_by_id: Optional[dict] = None,
    min_holding_seconds: float = MIN_HOLDING_SECONDS_FOR_STRATEGY_COHORT,
) -> dict:
    """Full realized baseline: overall, per cohort, and per grouping.

    ``overall`` is computed over EVERY closed row (nothing filtered out) and
    ``strategy_only`` over the two real cohorts. Reporting both makes the
    effect of the fixture filter explicit and auditable.
    """
    rows = [r for r in trade_rows if str(r.get("status") or "closed") == "closed"]
    positions = collapse_positions(rows, signals_by_id, min_holding_seconds)

    cohorts: dict[str, list[Position]] = {c: [] for c in COHORTS}
    for pos in positions:
        cohorts[pos.cohort(min_holding_seconds)].append(pos)

    real = cohorts[COHORT_AI_LINKED] + cohorts[COHORT_STRATEGY_UNLINKED]
    real.sort(key=lambda p: (p.exit_time or datetime.min, p.position_id))

    return {
        "generated_from": "realized paper ledger (trades.pnl)",
        "min_holding_seconds_for_strategy_cohort": min_holding_seconds,
        "min_trades_for_conclusion": MIN_TRADES_FOR_CONCLUSION,
        "ledger_rows_closed": len(rows),
        "positions_after_collapse": len(positions),
        "overall": compute_stats(positions),
        "strategy_only": compute_stats(real),
        "ai_linked": compute_stats(cohorts[COHORT_AI_LINKED]),
        "strategy_unlinked": compute_stats(cohorts[COHORT_STRATEGY_UNLINKED]),
        "test_fixture": compute_stats(cohorts[COHORT_TEST_FIXTURE]),
        "cohort_sizes": {c: len(v) for c, v in cohorts.items()},
        "groups": {
            "score_band": groups_by_score_band(real),
            "quality": groups_by_quality(real),
            "direction": groups_by_direction(real),
            "time_of_day": groups_by_time(real),
            "symbol": groups_by_symbol(real),
            "exit_reason": groups_by_exit_reason(real),
            "holding_time": groups_by_holding(real),
        },
        "positions": [p.to_dict() for p in positions],
    }
