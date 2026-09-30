"""Configurable profit-optimization experiments, evaluation and promotion gate.

Everything this module produces is a **SIMULATION** built on real market data
by ``app.services.trade_replay``. No candidate is ever executed, and no
simulated number is ever presented as realized performance. The distinction is
enforced structurally: ``ExperimentOutcome`` carries
``is_realized_performance = False`` and the report generator labels every
candidate table as counterfactual.

Three gates stand between a candidate and production, and a candidate must
clear ALL of them:

  1. **Fidelity gate** - the baseline configuration must reproduce the real
     recorded exits well enough for a difference to mean anything. Measured,
     not assumed (see ``FidelityReport``).
  2. **Precision gate** - the simulator's own per-trade error must be smaller
     than the effect being claimed. If measurement noise exceeds the
     candidate's improvement, the result is not interpretable.
  3. **Sample gate** - ``MIN_SAMPLE_FOR_PROMOTION`` real completed trades in
     the cohort being judged. A candidate can therefore only ever be labelled
     CANDIDATE until then; ``PromotionDecision.promotable`` is False by
     construction below that threshold.

Statistics used for the pattern search are exact tests (Fisher's exact for
win-rate differences, a permutation test for expectancy differences) because
normal approximations are not defensible at these sample sizes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from app.services.profit_baseline import (
    INSUFFICIENT_DATA,
    INSUFFICIENT_PROMOTION,
    MIN_TRADES_FOR_CONCLUSION,
    compute_stats,
    max_drawdown,
    profit_factor,
    expectancy_from_stats,
)
from app.services.trade_replay import (
    INTRABAR_ASSUMPTIONS,
    INTRABAR_CLOSE_ONLY,
    INTRABAR_PESSIMISTIC,
    ReplayConfig,
    ReplayResult,
    replay_position,
)

# A candidate may never be promoted below this many real completed trades in
# the cohort being judged. Deliberately equal to the monitoring cohort's own
# threshold so "performance conclusion" and "promotion" can never disagree.
MIN_SAMPLE_FOR_PROMOTION = MIN_TRADES_FOR_CONCLUSION

# Minimum share of positions whose terminal exit reason the baseline replay
# must reproduce before any candidate result is trusted.
MIN_FIDELITY_REASON_AGREEMENT = 0.80

# Minimum share of positions whose realized-P&L sign the baseline replay must
# reproduce before any candidate result is trusted.
MIN_FIDELITY_SIGN_AGREEMENT = 0.80

# A candidate's expectancy improvement must exceed the simulator's mean
# absolute per-trade error, otherwise the "improvement" is inside the noise.
PRECISION_SAFETY_FACTOR = 2.0

# A candidate may not increase maximum drawdown by more than this fraction.
MAX_DRAWDOWN_TOLERANCE = 1.25

CANDIDATE = "CANDIDATE"
PROMOTED = "PROMOTED"
REJECTED = "REJECTED"


# ────────────────────────────────────────────────────────────────────────────
# Exact statistics (no normal approximations at these sample sizes)
# ────────────────────────────────────────────────────────────────────────────

def _log_choose(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p-value for the 2x2 table [[a, b], [c, d]].

    Exact, so it stays valid at the tiny sample sizes this project actually
    has (an asymptotic chi-square test would not).
    """
    for v in (a, b, c, d):
        if v < 0:
            raise ValueError("cell counts must be non-negative")
    n = a + b + c + d
    if n == 0:
        return 1.0
    row1, row2 = a + b, c + d
    col1 = a + c
    lo = max(0, col1 - row2)
    hi = min(row1, col1)

    def _prob(x: int) -> float:
        return math.exp(
            _log_choose(row1, x) + _log_choose(row2, col1 - x) - _log_choose(n, col1)
        )

    observed = _prob(a)
    total = 0.0
    for x in range(lo, hi + 1):
        p = _prob(x)
        if p <= observed * (1 + 1e-9):
            total += p
    return min(1.0, total)


def welch_t(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """Welch's t statistic for mean(a) - mean(b). None when undefined."""
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se <= 0:
        return None
    return (ma - mb) / se


def permutation_expectancy_p(
    a: Sequence[float], b: Sequence[float], max_draws: int = 20000
) -> Optional[float]:
    """Two-sided permutation p-value for "mean(a) > mean(b)" on trade P&L.

    Non-parametric, so it makes no normality assumption about P&L. A small
    deterministic stride keeps the test reproducible without a RNG seed.
    """
    na, nb = len(a), len(b)
    if na == 0 or nb == 0:
        return None
    observed = sum(a) / na - sum(b) / nb
    if observed <= 0:
        # One-sided in the direction of interest; report the mirrored tail.
        observed = -observed
        a, b = [-x for x in a], [-x for x in b]
    pooled = list(a) + list(b)
    n = len(pooled)
    total = sum(pooled)
    at_least = 0
    for i in range(max_draws):
        if i == 0:
            pick = a
        else:
            # Deterministic pseudo-shuffle: a coprime stride walk.
            stride = 1 + (2 * i) % max(1, n - 1)
            idx = [(j * stride) % n for j in range(n)]
            shuffled = [pooled[j] for j in idx]
            pick = shuffled[:na]
        rest_total = total - sum(pick)
        diff = sum(pick) / na - (rest_total / nb)
        if abs(diff) >= abs(observed) * (1 - 1e-12):
            at_least += 1
    return at_least / max_draws


# ────────────────────────────────────────────────────────────────────────────
# Experiment grids
# ────────────────────────────────────────────────────────────────────────────

def sl_experiments() -> list[ReplayConfig]:
    """Phase 8a: stop-loss width with the target geometry left alone.

    A wider stop mechanically lowers R:R (the engine's normal geometry is a
    fixed 1.33), so at 1.75x and above the R:R gate rejects every trade and the
    row is empty by construction rather than by performance. That is reported
    honestly; ``sl_fixed_rr_experiments`` is the answerable form.
    """
    base = ReplayConfig.baseline()
    return [
        ReplayConfig(**{**base.__dict__, "label": f"SL={mult}xATR", "sl_atr_mult": mult})
        for mult in (1.25, 1.5, 1.75, 2.0)
    ]


def sl_fixed_rr_experiments() -> list[ReplayConfig]:
    """Phase 8b: stop-loss width with R:R held at the live 1.33 ratio.

    This is the form of the wider-stop question that can actually be
    answered: the stop moves out AND T1 moves with it, so the payoff profile
    is unchanged and only the stop-out frequency and the position size (which
    the risk engine shrinks automatically) differ.

    Position sizing is re-derived under the UNCHANGED risk budget for every
    configuration, so a wider stop can never increase account risk.
    """
    base = ReplayConfig.baseline()
    live_rr = base.t1_atr_mult / base.sl_atr_mult  # 2.0 / 1.5 = 1.3333
    return [
        ReplayConfig(**{
            **base.__dict__,
            "label": f"SL={mult}xATR(rr kept {live_rr:.2f})",
            "sl_atr_mult": mult,
            "target_rr": live_rr,
        })
        for mult in (1.25, 1.5, 1.75, 2.0)
    ]


def t1_partial_experiments() -> list[ReplayConfig]:
    """Phase 7: how much of the position exits at Target 1."""
    base = ReplayConfig.baseline()
    return [
        ReplayConfig(**{**base.__dict__, "label": f"T1partial={pct:.0f}%",
                        "t1_exit_percent": pct})
        for pct in (30.0, 50.0, 70.0)
    ]


def trailing_experiments() -> list[ReplayConfig]:
    """Phase 7: trailing-stop distance on the remainder after T1."""
    base = ReplayConfig.baseline()
    return [
        ReplayConfig(**{**base.__dict__, "label": f"trail={mult}x",
                        "trailing_range_mult": mult})
        for mult in (0.5, 0.75, 1.0)
    ]


def target_experiments() -> list[ReplayConfig]:
    """Phase 7: T2 distance from entry, as an ATR offset beyond the T1 level."""
    base = ReplayConfig.baseline()
    out = [base.with_label("target=current")]
    for extra in (0.5, 1.0):
        out.append(
            ReplayConfig(**{**base.__dict__, "label": f"target=T1+{extra}ATR",
                            "t2_atr_mult": base.t1_atr_mult + extra})
        )
    return out


def rr_experiments() -> list[ReplayConfig]:
    """Phase 9a: the R:R GATE. Raises the minimum R:R the engine will accept.

    Reported honestly even though it is structurally degenerate on this cohort:
    the engine's normal-strength geometry is fixed at T1 = 2.0 ATR and
    SL = 1.5 ATR, so every normal-strength signal has an R:R of exactly 1.33.
    A gate at 1.5 or above therefore rejects 100% of normal-strength trades
    and the only survivors would be STRONG signals (T1 = 3.0 ATR, R:R 2.0).
    That is a real property of the strategy, not a measurement artefact, and
    it is left visible rather than hidden.
    """
    base = ReplayConfig.baseline()
    return [
        ReplayConfig(**{**base.__dict__, "label": f"minRR={rr}", "min_rr": rr})
        for rr in (1.2, 1.5, 1.75, 2.0)
    ]


def target_rr_experiments() -> list[ReplayConfig]:
    """Phase 9b: the R:R TARGET. Varies where T1 sits relative to the stop so
    the ratio itself becomes the variable (T1 = entry +/- ratio x risk).

    This is the non-degenerate form of the R:R question: it asks whether
    demanding a better payoff profile would have paid, instead of asking
    whether a gate above 1.33 can ever pass.
    """
    base = ReplayConfig.baseline()
    return [
        ReplayConfig(**{**base.__dict__, "label": f"targetRR={rr}", "target_rr": rr})
        for rr in (1.33, 1.5, 1.75, 2.0)
    ]


def entry_experiments() -> list[ReplayConfig]:
    """Phase 6: entry-time confirmations. Each is evaluated on the entry bar
    and bars before it only - never a future candle."""
    base = ReplayConfig.baseline()
    return [
        ReplayConfig(**{**base.__dict__, "label": "entry=baseline"}),
        ReplayConfig(**{**base.__dict__, "label": "entry=vwap-side",
                        "entry_confirmation": "vwap_side"}),
        ReplayConfig(**{**base.__dict__, "label": "entry=candle-side",
                        "entry_confirmation": "candle_side"}),
    ]


EXPERIMENT_GRIDS = {
    "stop_loss": sl_experiments,
    "stop_loss_fixed_rr": sl_fixed_rr_experiments,
    "t1_partial": t1_partial_experiments,
    "trailing": trailing_experiments,
    "target": target_experiments,
    "min_rr": rr_experiments,
    "target_rr": target_rr_experiments,
    "entry_confirmation": entry_experiments,
}


# ────────────────────────────────────────────────────────────────────────────
# Fidelity
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class FidelityReport:
    """How well the baseline replay reproduces the real recorded exits."""

    positions: int = 0
    positions_with_recorded_reason: int = 0
    reason_agreements: int = 0
    sign_agreements: int = 0
    skipped: int = 0
    mean_abs_pnl_error: Optional[float] = None
    median_abs_pnl_error: Optional[float] = None
    per_assumption: dict = field(default_factory=dict)

    @property
    def reason_agreement_rate(self) -> Optional[float]:
        if not self.positions_with_recorded_reason:
            return None
        return self.reason_agreements / self.positions_with_recorded_reason

    @property
    def sign_agreement_rate(self) -> Optional[float]:
        if not self.positions:
            return None
        return self.sign_agreements / self.positions

    def passes(self) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        rate = self.reason_agreement_rate
        sign = self.sign_agreement_rate
        if rate is None:
            reasons.append("no position carries a recorded exit_reason, so "
                           "terminal-reason fidelity cannot be measured")
        elif rate < MIN_FIDELITY_REASON_AGREEMENT:
            reasons.append(
                f"terminal-reason agreement {rate:.0%} < "
                f"{MIN_FIDELITY_REASON_AGREEMENT:.0%} required"
            )
        if sign is None or sign < MIN_FIDELITY_SIGN_AGREEMENT:
            reasons.append(
                f"P&L sign agreement {sign if sign is not None else 0:.0%} < "
                f"{MIN_FIDELITY_SIGN_AGREEMENT:.0%} required"
            )
        return (not reasons), reasons

    def to_dict(self) -> dict:
        ok, reasons = self.passes()
        return {
            "positions": self.positions,
            "positions_with_recorded_reason": self.positions_with_recorded_reason,
            "reason_agreement_rate": self.reason_agreement_rate,
            "sign_agreement_rate": self.sign_agreement_rate,
            "mean_abs_pnl_error": self.mean_abs_pnl_error,
            "median_abs_pnl_error": self.median_abs_pnl_error,
            "skipped": self.skipped,
            "gate_passes": ok,
            "gate_failures": reasons,
            "per_assumption": self.per_assumption,
        }


def measure_fidelity(
    positions: Sequence,
    bars_by_symbol: dict,
    assumption: str = INTRABAR_PESSIMISTIC,
) -> FidelityReport:
    """Replay the BASELINE config and compare against the recorded outcomes."""
    cfg = ReplayConfig(label="baseline", intrabar=assumption)
    report = FidelityReport(positions=len(positions))
    errors: list[float] = []
    for pos in positions:
        bars = bars_by_symbol.get(pos.symbol) or []
        res = replay_position(pos, bars, cfg, atr_value=pos.atr)
        if res.skip_reason:
            report.skipped += 1
            continue
        errors.append(abs(pos.pnl - res.pnl))
        if pos.terminal_reason:
            report.positions_with_recorded_reason += 1
            if res.exit_reason == pos.terminal_reason:
                report.reason_agreements += 1
        if (pos.pnl > 0) == (res.pnl > 0) or (pos.pnl == 0 and res.pnl == 0):
            report.sign_agreements += 1
    if errors:
        errors.sort()
        report.mean_abs_pnl_error = sum(errors) / len(errors)
        mid = len(errors) // 2
        report.median_abs_pnl_error = (
            errors[mid] if len(errors) % 2
            else (errors[mid - 1] + errors[mid]) / 2
        )
    return report


# ────────────────────────────────────────────────────────────────────────────
# Candidate evaluation
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class CandidateOutcome:
    """One candidate configuration's simulated result against a cohort."""

    label: str
    config: ReplayConfig
    assumption: str
    is_realized_performance: bool = False
    simulated_trades: int = 0
    skipped: int = 0
    skip_reasons: dict = field(default_factory=dict)
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    average_winner: Optional[float] = None
    average_loser: Optional[float] = None
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None
    expectancy: Optional[float] = None
    max_drawdown: float = 0.0
    stop_out_rate: Optional[float] = None
    t1_hit_rate: Optional[float] = None
    t2_hit_rate: Optional[float] = None
    trailing_stop_rate: Optional[float] = None
    average_holding_minutes: Optional[float] = None
    average_quantity: Optional[float] = None
    results: list = field(default_factory=list)

    def to_dict(self, include_results: bool = False) -> dict:
        out = {
            "label": self.label,
            "assumption": self.assumption,
            "is_realized_performance": self.is_realized_performance,
            "simulated_trades": self.simulated_trades,
            "skipped": self.skipped,
            "skip_reasons": self.skip_reasons,
            "wins": self.wins,
            "losses": self.losses,
            "breakeven": self.breakeven,
            "win_rate": self.win_rate,
            "net_pnl": round(self.net_pnl, 2),
            "gross_profit": round(self.gross_profit, 2),
            "gross_loss": round(self.gross_loss, 2),
            "average_winner": (
                round(self.average_winner, 2)
                if self.average_winner is not None else None
            ),
            "average_loser": (
                round(self.average_loser, 2)
                if self.average_loser is not None else None
            ),
            "profit_factor": (
                round(self.profit_factor, 4)
                if self.profit_factor is not None else None
            ),
            "expectancy": (
                round(self.expectancy, 4) if self.expectancy is not None else None
            ),
            "max_drawdown": round(self.max_drawdown, 2),
            "stop_out_rate": self.stop_out_rate,
            "t1_hit_rate": self.t1_hit_rate,
            "t2_hit_rate": self.t2_hit_rate,
            "trailing_stop_rate": self.trailing_stop_rate,
            "average_holding_minutes": self.average_holding_minutes,
            "average_quantity": (
                round(self.average_quantity, 2)
                if self.average_quantity is not None else None
            ),
        }
        if include_results:
            out["results"] = [r.to_dict() for r in self.results]
        return out


def risk_capped_quantity(
    entry: float, stop_loss: float, account_capital: float, risk_pct: float
) -> int:
    """Position size under the EXISTING risk budget, unchanged.

    Line-for-line mirror of ``RiskEngine.calculate_position_size``, so a
    candidate's simulated size is the size production would actually take.

    IMPORTANT, and measured on the live ledger: "a wider stop means a smaller
    position" is NOT true at the live Rs 10,000 capital. The size is
    ``min(budget / risk_per_share, 0.95 * capital / entry)``, and the second
    term binds for every real trade in the ledger, so the share count stays
    fixed as the stop widens and the RUPEE risk per trade RISES with the stop
    (mean Rs 36.64 at the live 1.5 ATR, Rs 42.74 at 1.75, Rs 48.85 at 2.0 -
    still under the Rs 200 budget, but only because the budget is never
    reached). The account therefore runs at roughly a fifth of its intended
    2% risk. Reported, never 'fixed': raising risk is out of scope.
    """
    if risk_pct <= 0 or entry <= 0:
        return 0
    risk_per_share = abs(entry - stop_loss)
    if risk_per_share <= 0:
        return 0
    max_risk_amount = account_capital * (risk_pct / 100.0)
    shares = int(max_risk_amount / risk_per_share)
    max_affordable = int(account_capital / entry)
    shares = max(0, min(shares, max_affordable))
    # The live engine leaves 5% capital headroom. Mirrored here because it
    # binds: at Rs 10,000 capital the affordability cap - not the 2% risk
    # budget - sets position size for every real trade in the ledger.
    if shares > 0 and shares * entry > account_capital * 0.95:
        shares = int((account_capital * 0.95) / entry)
    return max(0, shares)


def evaluate_candidate(
    config: ReplayConfig,
    positions: Sequence,
    bars_by_symbol: dict,
    assumption: str = INTRABAR_PESSIMISTIC,
    account_capital: float = 10_000.0,
    risk_pct: float = 2.0,
    rescale_quantity: bool = True,
) -> CandidateOutcome:
    """Replay a cohort under one configuration and summarise it.

    ``rescale_quantity`` re-sizes each position with the unchanged risk budget
    and the unchanged capital headroom, so a candidate is always compared at
    the size production would take. Note that at the live capital level a
    wider stop does NOT reduce the share count (see
    :func:`risk_capped_quantity`), so widening the stop raises rupee risk
    rather than lowering it; the budget still caps it.
    """
    cfg = ReplayConfig(**{**config.__dict__, "intrabar": assumption})
    outcome = CandidateOutcome(label=cfg.label, config=cfg, assumption=assumption)

    pnls: list[float] = []
    quantities: list[float] = []
    durations: list[float] = []
    stop_outs = t1_hits = t2_hits = trailing = 0

    for pos in positions:
        bars = bars_by_symbol.get(pos.symbol) or []
        res = replay_position(pos, bars, cfg, atr_value=pos.atr)
        if not res.traded:
            outcome.skipped += 1
            key = res.skip_reason or "unknown"
            outcome.skip_reasons[key] = outcome.skip_reasons.get(key, 0) + 1
            continue
        if rescale_quantity:
            qty = risk_capped_quantity(
                res.entry_price, res.stop_loss, account_capital, risk_pct
            )
        else:
            qty = res.quantity
        res.quantity = qty
        # RE-DERIVE the risk amount from the NEW size. replay_position computed
        # initial_risk with the position's ORIGINAL quantity, so leaving it
        # would report a risk figure (and therefore an R multiple) belonging to
        # a position that is no longer being simulated. This is the number the
        # "risk budget preserved" claim rests on, so it must match what the
        # replay actually held.
        if res.stop_loss is not None and res.entry_price is not None:
            res.initial_risk = round(
                abs(res.entry_price - res.stop_loss) * qty, 2
            )
        if res.initial_risk:
            res.r_multiple = round(res.pnl / res.initial_risk, 4)
        outcome.results.append(res)
        pnls.append(res.pnl)
        quantities.append(qty)
        durations.append(res.holding_minutes)
        if res.hit_t1:
            t1_hits += 1
        if res.hit_t2:
            t2_hits += 1
        if res.exit_reason == "STOP_LOSS":
            stop_outs += 1
        if res.exit_reason == "TRAILING_STOP":
            trailing += 1

    n = len(pnls)
    outcome.simulated_trades = n
    outcome.wins = sum(1 for p in pnls if p > 0)
    outcome.losses = sum(1 for p in pnls if p < 0)
    outcome.breakeven = sum(1 for p in pnls if p == 0)
    outcome.gross_profit = sum(p for p in pnls if p > 0)
    outcome.gross_loss = abs(sum(p for p in pnls if p < 0))
    outcome.net_pnl = sum(pnls)
    outcome.average_winner = (
        outcome.gross_profit / outcome.wins if outcome.wins else None
    )
    outcome.average_loser = (
        outcome.gross_loss / outcome.losses if outcome.losses else None
    )
    outcome.win_rate = (outcome.wins / n) if n else None
    pf = profit_factor(outcome.gross_profit, outcome.gross_loss)
    outcome.profit_factor = pf if pf is None or math.isinf(pf) else round(pf, 4)
    exp = expectancy_from_stats(
        outcome.wins, outcome.losses, outcome.average_winner, outcome.average_loser
    )
    outcome.expectancy = exp
    outcome.max_drawdown = max_drawdown(pnls)
    outcome.stop_out_rate = (stop_outs / n) if n else None
    outcome.t1_hit_rate = (t1_hits / n) if n else None
    outcome.t2_hit_rate = (t2_hits / n) if n else None
    outcome.trailing_stop_rate = (trailing / n) if n else None
    outcome.average_holding_minutes = (
        sum(durations) / len(durations) if durations else None
    )
    outcome.average_quantity = (
        sum(quantities) / len(quantities) if quantities else None
    )
    return outcome


# ────────────────────────────────────────────────────────────────────────────
# Promotion gate
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class PromotionDecision:
    """Why a candidate may or may not be promoted. Never optimistic."""

    label: str
    status: str = CANDIDATE
    promotable: bool = False
    blocking_reasons: list[str] = field(default_factory=list)
    real_sample_size: int = 0
    min_sample_required: int = MIN_SAMPLE_FOR_PROMOTION
    baseline_expectancy: Optional[float] = None
    candidate_expectancy: Optional[float] = None
    expectancy_delta: Optional[float] = None
    baseline_drawdown: Optional[float] = None
    candidate_drawdown: Optional[float] = None
    drawdown_ratio: Optional[float] = None
    simulator_error: Optional[float] = None
    improvement_exceeds_noise: bool = False
    risk_budget_preserved: bool = True

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "status": self.status,
            "promotable": self.promotable,
            "blocking_reasons": self.blocking_reasons,
            "real_sample_size": self.real_sample_size,
            "min_sample_required": self.min_sample_required,
            "baseline_expectancy": self.baseline_expectancy,
            "candidate_expectancy": self.candidate_expectancy,
            "expectancy_delta": self.expectancy_delta,
            "baseline_drawdown": self.baseline_drawdown,
            "candidate_drawdown": self.candidate_drawdown,
            "drawdown_ratio": self.drawdown_ratio,
            "simulator_error": self.simulator_error,
            "improvement_exceeds_noise": self.improvement_exceeds_noise,
            "risk_budget_preserved": self.risk_budget_preserved,
        }


def evaluate_promotion(
    label: str,
    baseline: CandidateOutcome,
    candidate: CandidateOutcome,
    real_sample_size: int,
    fidelity: FidelityReport,
    risk_budget_preserved: bool = True,
) -> PromotionDecision:
    """Decide whether a candidate may leave CANDIDATE status.

    Every condition is conjunctive. Failing any one of them leaves the
    candidate labelled CANDIDATE with the reason recorded.
    """
    decision = PromotionDecision(
        label=label,
        real_sample_size=real_sample_size,
        baseline_expectancy=baseline.expectancy,
        candidate_expectancy=candidate.expectancy,
        baseline_drawdown=baseline.max_drawdown,
        candidate_drawdown=candidate.max_drawdown,
        simulator_error=fidelity.mean_abs_pnl_error,
        risk_budget_preserved=risk_budget_preserved,
    )

    fidelity_ok, fidelity_reasons = fidelity.passes()
    if not fidelity_ok:
        decision.blocking_reasons.extend(fidelity_reasons)

    if real_sample_size < MIN_SAMPLE_FOR_PROMOTION:
        decision.blocking_reasons.append(
            f"only {real_sample_size} real completed trades in the cohort; "
            f"{MIN_SAMPLE_FOR_PROMOTION} required"
        )

    if baseline.expectancy is None or candidate.expectancy is None:
        decision.blocking_reasons.append(
            "expectancy undefined for the baseline or the candidate "
            "(needs at least one winner and one loser)"
        )
    else:
        delta = candidate.expectancy - baseline.expectancy
        decision.expectancy_delta = round(delta, 4)
        noise = fidelity.mean_abs_pnl_error
        if delta <= 0:
            decision.blocking_reasons.append(
                f"candidate expectancy {candidate.expectancy:.3f} does not "
                f"exceed baseline {baseline.expectancy:.3f}"
            )
        elif noise is not None and abs(delta) < noise * PRECISION_SAFETY_FACTOR:
            decision.blocking_reasons.append(
                f"expectancy improvement {delta:.3f} is inside the simulator's "
                f"own error band (+/-{noise:.2f}/trade x"
                f"{PRECISION_SAFETY_FACTOR}); not measurable"
            )
        else:
            decision.improvement_exceeds_noise = True

    if baseline.max_drawdown > 0:
        ratio = candidate.max_drawdown / baseline.max_drawdown
        decision.drawdown_ratio = round(ratio, 4)
        if ratio > MAX_DRAWDOWN_TOLERANCE:
            decision.blocking_reasons.append(
                f"max drawdown {candidate.max_drawdown:.2f} exceeds "
                f"{MAX_DRAWDOWN_TOLERANCE}x the baseline "
                f"{baseline.max_drawdown:.2f}"
            )

    if not risk_budget_preserved:
        decision.blocking_reasons.append("risk budget was not preserved")

    decision.promotable = not decision.blocking_reasons
    decision.status = PROMOTED if decision.promotable else CANDIDATE
    if not decision.promotable:
        decision.blocking_reasons.append(INSUFFICIENT_PROMOTION)
    return decision


# ────────────────────────────────────────────────────────────────────────────
# Pattern significance (Phase 4)
# ────────────────────────────────────────────────────────────────────────────

def pattern_significance(
    subset_a: Sequence, subset_b: Sequence
) -> dict:
    """Is a candidate negative-expectancy pattern distinguishable from noise?

    Reports an exact Fisher p-value on the win-rate difference and a
    permutation p-value on the expectancy difference, plus the honest
    verdict. With small n both will usually be non-significant, and this
    function is written so that is the easy, correct answer.
    """
    pa = [p.pnl for p in subset_a]
    pb = [p.pnl for p in subset_b]
    a_wins = sum(1 for x in pa if x > 0)
    b_wins = sum(1 for x in pb if x > 0)
    fisher = fisher_exact_two_sided(a_wins, len(pa) - a_wins, b_wins, len(pb) - b_wins)
    perm = permutation_expectancy_p(pa, pb)
    exp_a = (sum(pa) / len(pa)) if pa else None
    exp_b = (sum(pb) / len(pb)) if pb else None
    t = welch_t(pa, pb)
    return {
        "n_a": len(pa),
        "n_b": len(pb),
        "wins_a": a_wins,
        "wins_b": b_wins,
        "mean_pnl_a": round(exp_a, 3) if exp_a is not None else None,
        "mean_pnl_b": round(exp_b, 3) if exp_b is not None else None,
        "fisher_exact_p": round(fisher, 4),
        "permutation_p": round(perm, 4) if perm is not None else None,
        "welch_t": round(t, 3) if t is not None else None,
        "significant_at_0_05": bool(
            (fisher is not None and fisher < 0.05)
            or (perm is not None and perm < 0.05)
        ),
        "verdict": (
            INSUFFICIENT_DATA if (len(pa) + len(pb)) < MIN_SAMPLE_FOR_PROMOTION
            else "MEASURABLE"
        ),
    }


def run_experiment_grid(
    grid_name: str,
    positions: Sequence,
    bars_by_symbol: dict,
    account_capital: float = 10_000.0,
    risk_pct: float = 2.0,
) -> dict:
    """Run one named experiment grid under BOTH intrabar assumptions.

    Running both is deliberate: a candidate that only improves under the
    optimistic assumption is an artefact of bar resolution, not a result.
    """
    factory = EXPERIMENT_GRIDS[grid_name]
    baseline_configs = {
        INTRABAR_PESSIMISTIC: evaluate_candidate(
            ReplayConfig.baseline(), positions, bars_by_symbol,
            INTRABAR_PESSIMISTIC, account_capital, risk_pct,
        ),
        INTRABAR_CLOSE_ONLY: evaluate_candidate(
            ReplayConfig.baseline(), positions, bars_by_symbol,
            INTRABAR_CLOSE_ONLY, account_capital, risk_pct,
        ),
    }
    candidates: dict[str, list[CandidateOutcome]] = {}
    for assumption in INTRABAR_ASSUMPTIONS:
        outcomes = []
        for cfg in factory():
            outcomes.append(
                evaluate_candidate(
                    cfg, positions, bars_by_symbol, assumption,
                    account_capital, risk_pct,
                )
            )
        candidates[assumption] = outcomes
    return {
        "grid": grid_name,
        "baseline": baseline_configs,
        "candidates": candidates,
        "is_realized_performance": False,
    }
