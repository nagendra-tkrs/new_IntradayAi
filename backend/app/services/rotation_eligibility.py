"""Rotation engine — candidate eligibility pipeline (Phase 4).

Implements the Phase 2 §5 candidate pipeline stages for ONE candidate as pure,
deterministic functions over a fetched OHLCV frame:

    S2 Tradability → S3 Liquidity → S4 Volume Quality → S5 ATR/Volatility
        → S6 Data Quality → S7 Historical Signal Quality (evaluability gate)

Every stage produces an explainable ``StageOutcome`` (status / metric /
threshold / reason) — never an opaque boolean. Missing data is a first-class
status (``INSUFFICIENT_DATA``), market-closed periods are excluded from every
denominator (never a provider failure, never a penalty), and provider
exceptions are surfaced as ``PROVIDER_FAILURE``.

Finishing statuses reuse the Phase 3 vocabulary
(``EligibilityResultStatus``: ELIGIBLE / INELIGIBLE / INSUFFICIENT_DATA) and
the per-filter vocabulary (``FilterResult``: PASS / FAIL / INSUFFICIENT_DATA).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional

import pandas as pd

from app.core import market_session
from app.services.indicators import atr as atr_indicator
from app.services.rotation_config import RotationEngineConfig

# NSE base-symbol pattern — same lenient rule as the Phase 3 validation gate
# (letters, digits, hyphen, ampersand, dot, space — post-normalization).
NSE_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9\-&.\s]{0,49}$")

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)

# --- reason codes (stable, explainable) --------------------------------------
PROVIDER_FAILURE = "PROVIDER_FAILURE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
BELOW_FLOOR = "BELOW_FLOOR"
ABOVE_CAP = "ABOVE_CAP"
OUTSIDE_BAND = "OUTSIDE_BAND"
INDICATOR_FAILURE = "INDICATOR_FAILURE"
INVALID_SYMBOL = "INVALID_SYMBOL"
INVALID_OHLC = "INVALID_OHLC"
NO_DATA = "NO_DATA"

# --- session classes (Phase 2 §9.3) ------------------------------------------
NON_SESSION = "NON_SESSION"
VALID_EVALUATED = "VALID_EVALUATED"
SESSION_INSUFFICIENT = "SESSION_INSUFFICIENT"
SESSION_STALE = "SESSION_STALE"
SESSION_FAILURE = "SESSION_FAILURE"

VALID_EVALUATED_CLASSES = frozenset({VALID_EVALUATED, SESSION_INSUFFICIENT})


def normalize_symbol(symbol: str) -> str:
    """Phase 2 §4.5 normalization: trim → upper-case → optional .NS stripped."""
    s = (symbol or "").strip().upper()
    if s.upper().endswith(".NS"):
        s = s[: -len(".NS")]
    return s


def is_valid_symbol(symbol: str) -> bool:
    return bool(NSE_SYMBOL_PATTERN.match(symbol or ""))


def is_confirmed_trading_day(d: date) -> bool:
    """Single authoritative trading-day confirmation (Phase 5C).

    Delegates to the ``market_session`` calendar abstraction — the official NSE
    holiday-master (CM segment) calendar. Identical fail-safe semantics:
    weekday + official holiday + covered year; outside the calendar horizon the
    day is never reported open.
    """
    return market_session.is_confirmed_trading_day(d)


def confirmed_trading_days(start: date, end: date) -> list[date]:
    """All confirmed NSE trading dates in ``[start, end]`` — deterministic."""
    out = []
    d = start
    while d <= end:
        if is_confirmed_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


@dataclass
class StageOutcome:
    status: str  # PASS | FAIL | INSUFFICIENT_DATA
    metric: Optional[float] = None
    threshold: Optional[float] = None
    reason: str = ""


@dataclass
class SessionRecord:
    day: date
    session_class: str
    n_bars: int
    expected_bars: int
    total_value: float  # Σ(close × volume) — ₹ traded value (sane bars only)
    mean_volume: float  # daily traded volume — Σ volume (sane bars only)
    malformed_bars: int
    indicator_failure: bool
    last_close: float
    end_index: int  # global bar index of the session-end row (or -1)
    atr_pct: Optional[float] = None  # ATR(14)/close at session end (sane bars)


@dataclass
class EvaluationResult:
    """Per-candidate pipeline output — everything eligibility/selection/
    scoring/reporting needs, all explainable."""
    symbol: str
    eligibility_status: str  # ELIGIBLE | INELIGIBLE | INSUFFICIENT_DATA
    tradability: StageOutcome
    liquidity: StageOutcome
    volume: StageOutcome
    volatility: StageOutcome
    data_quality: StageOutcome
    signal_quality: StageOutcome
    data_quality_state: str = "SUFFICIENT"
    failure_reason: Optional[str] = None

    # raw metrics (feeding scoring + observability + core selection)
    fetch_failed: bool = False
    expected_sessions: int = 0
    present_sessions: int = 0
    valid_evaluated_sessions: int = 0
    median_daily_value: Optional[float] = None
    mean_volume: Optional[float] = None
    volume_cv: Optional[float] = None
    median_atr_pct: Optional[float] = None  # fraction (0.012 == 1.2%)
    data_quality_norm: Optional[float] = None  # 0..1 absolute
    data_quality_score: Optional[float] = None  # 0..10 absolute
    trading_consistency: Optional[float] = None  # share of expected sessions with volume>0
    signal_reliability: Optional[float] = None  # valid_evaluated / expected
    malformed_rate: Optional[float] = None
    indicator_failure_rate: Optional[float] = None
    missing_candle_ratio: Optional[float] = None

    # signal metrics (populated by rotation_replay)
    long_count: int = 0
    short_count: int = 0
    no_trade_count: int = 0
    total_signal_count: int = 0
    frequency: Optional[float] = None
    long_frequency: Optional[float] = None
    short_frequency: Optional[float] = None
    long_sample_ok: bool = False
    short_sample_ok: bool = False
    long_raw: Optional[float] = None
    short_raw: Optional[float] = None
    session_class_counts: dict = field(default_factory=dict)

    @property
    def stages(self) -> dict:
        return {
            "tradability": self.tradability,
            "liquidity": self.liquidity,
            "volume": self.volume,
            "volatility": self.volatility,
            "data_quality": self.data_quality,
            "signal_quality": self.signal_quality,
        }

    @property
    def is_eligible(self) -> bool:
        return self.eligibility_status == "ELIGIBLE"


def _ohlc_sane(row) -> bool:
    o, h, l, c, v = (
        row.get("open"), row.get("high"), row.get("low"), row.get("close"), row.get("volume"),
    )
    if any(pd.isna(x) for x in (o, h, l, c)):
        return False
    if not (o > 0 and h > 0 and l > 0 and c > 0):
        return False
    if v is None or pd.isna(v) or v < 0 or not math.isfinite(float(v)):
        return False
    if not (h >= max(o, c) - 1e-9 and l <= min(o, c) + 1e-9):
        return False
    return True


def _to_ist_timestamps(df: pd.DataFrame) -> pd.DataFrame:
    ts = pd.to_datetime(df["timestamp"])
    if getattr(ts.dtype, "tz", None) is not None:
        ts = ts.dt.tz_convert(market_session.IST)
    else:
        ts = ts.dt.tz_localize(market_session.IST)
    df = df.copy()
    df["timestamp"] = ts
    return df


_REQUIRED_COLUMNS = {"timestamp", "open", "high", "low", "close", "volume"}


def filter_session_frame(
    df: Optional[pd.DataFrame], expected_dates: list[date]
) -> pd.DataFrame:
    """Single deterministic slice used by BOTH session building and signal
    replay (guarantees identical row indices): normalize timestamps to IST,
    keep only window rows on confirmed trading dates during session hours,
    sort by timestamp, reset the index. Missing columns / empty → empty frame."""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    if not _REQUIRED_COLUMNS.issubset(set(df.columns)):
        return pd.DataFrame()
    expected: set = set(expected_dates)
    df = _to_ist_timestamps(df)
    df["_day"] = df["timestamp"].dt.date
    mask = df["_day"].isin(expected) & (
        df["timestamp"].dt.time >= SESSION_OPEN
    ) & (df["timestamp"].dt.time <= SESSION_CLOSE)
    df = df[mask]
    if len(df) == 0:
        return pd.DataFrame()
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def build_sessions(
    df: Optional[pd.DataFrame],
    expected_dates: list[date],
    cfg: RotationEngineConfig,
) -> tuple[list[SessionRecord], int]:
    """Slice ``df`` into per-session records over the expected trading dates.

    * Bars whose date is not a confirmed trading date in the window are
      NON_SESSION → dropped entirely (market-closed periods are neutral).
    * Malformed bars are excluded from liquidity/volume aggregates (they are
      penalized only through the S6 data-quality stage).
    * Session class: SESSION_INSUFFICIENT when fewer than the signal warmup
      bars (excluded from signal denominators, counted in data quality),
      VALID_EVALUATED otherwise.
    """
    expected_bars = max(1, int(cfg.expected_bars_per_session))
    df = filter_session_frame(df, expected_dates)
    if df is None or len(df) == 0:
        return [], 0

    records: list[SessionRecord] = []
    for day, group in df.groupby("_day"):
        group = group.reset_index(drop=True)
        n_bars = len(group)
        sane = group.apply(_ohlc_sane, axis=1)
        sane_group = group[sane]
        n_sane = int(sane.sum())
        if n_sane:
            total_value = float((sane_group["close"] * sane_group["volume"]).sum())
            mean_volume = float(sane_group["volume"].sum())  # daily traded volume
        else:
            total_value = 0.0
            mean_volume = 0.0
        malformed = n_bars - n_sane
        last_close = float(group.iloc[-1]["close"]) if n_bars else 0.0
        end_index = int(group.index[-1])
        # per-session indicator sanity + %ATR (ATR computable on sane bars)
        ind_fail = False
        atr_pct: Optional[float] = None
        try:
            if n_sane >= 14 and last_close > 0:
                a = atr_indicator(
                    sane_group["high"], sane_group["low"], sane_group["close"], 14
                )
                val = a.iloc[-1]
                if pd.isna(val):
                    ind_fail = True
                else:
                    atr_pct = float(val) / last_close
            else:
                ind_fail = True
        except Exception:
            ind_fail = True
        session_class = (
            SESSION_INSUFFICIENT if n_bars < cfg.signal_warmup_bars else VALID_EVALUATED
        )
        records.append(
            SessionRecord(
                day=day,
                session_class=session_class,
                n_bars=n_bars,
                expected_bars=expected_bars,
                total_value=total_value,
                mean_volume=mean_volume,
                malformed_bars=malformed,
                indicator_failure=ind_fail,
                last_close=last_close,
                end_index=end_index,
                atr_pct=atr_pct,
            )
        )
    return records, len(df)


# ---------------------------------------------------------------------------
# per-stage evaluations
# ---------------------------------------------------------------------------
def _median(values: list[float]) -> Optional[float]:
    ordered = sorted(
        [v for v in values if v is not None and math.isfinite(float(v))]
    )
    if not ordered:
        return None
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def evaluate_data_quality(
    symbol: str,
    records: list[SessionRecord],
    expected_sessions: int,
    fetch_failed: bool,
    cfg: RotationEngineConfig,
) -> tuple[StageOutcome, str, Optional[float], Optional[float]]:
    """S6 — absolute Data Quality (Phase 2 §10 / F7). Market-closed periods are
    excluded from every denominator; provider failure is penalized; zero valid
    sessions → INSUFFICIENT_DATA."""
    if fetch_failed:
        outcome = StageOutcome(
            status="FAIL",
            metric=0.0,
            threshold=cfg.min_data_quality,
            reason=f"{PROVIDER_FAILURE}: provider raised while fetching {symbol}",
        )
        return outcome, PROVIDER_FAILURE, 0.0, 0.0
    if expected_sessions <= 0:
        outcome = StageOutcome(
            status="INSUFFICIENT_DATA",
            metric=None,
            threshold=cfg.min_data_quality,
            reason=f"{INSUFFICIENT_DATA}: no expected sessions in the evaluation window",
        )
        return outcome, INSUFFICIENT_DATA, None, None

    present = len(records)
    fetch_success = present / expected_sessions
    total_bars = sum(r.n_bars for r in records)
    missing_candles = (
        sum(max(0.0, 1.0 - r.n_bars / r.expected_bars) for r in records) / present
        if present
        else 0.0
    )
    malformed = (
        sum(r.malformed_bars for r in records) / total_bars if total_bars else 0.0
    )
    ind_fail = sum(1 for r in records if r.indicator_failure) / present if present else 0.0

    dq_norm = fetch_success * (1.0 - missing_candles) * (1.0 - malformed) * (1.0 - ind_fail)
    dq_norm = max(0.0, min(1.0, dq_norm))
    dq_score = round(dq_norm * 10.0, 4)

    if present == 0:
        outcome = StageOutcome(
            status="INSUFFICIENT_DATA",
            metric=dq_score,
            threshold=cfg.min_data_quality,
            reason=f"{INSUFFICIENT_DATA}: provider returned no sessions in the window",
        )
        return outcome, INSUFFICIENT_DATA, dq_norm, dq_score
    if dq_norm < cfg.min_data_quality:
        outcome = StageOutcome(
            status="FAIL",
            metric=dq_norm,
            threshold=cfg.min_data_quality,
            reason=(
                f"{BELOW_FLOOR}: data quality {dq_norm:.3f} < "
                f"{cfg.min_data_quality} (fetch_success={fetch_success:.2f}, "
                f"missing_candles={missing_candles:.2f}, malformed={malformed:.2f})"
            ),
        )
        return outcome, "SUFFICIENT", dq_norm, dq_score
    outcome = StageOutcome(
        status="PASS",
        metric=dq_norm,
        threshold=cfg.min_data_quality,
        reason=f"data quality {dq_norm:.3f} >= {cfg.min_data_quality}",
    )
    return outcome, "SUFFICIENT", dq_norm, dq_score


def provider_failure_evaluation(symbol: str, error: str) -> EvaluationResult:
    """Short-circuit evaluation for a symbol whose fetch raised (S2/S6 both
    treat this as PROVIDER_FAILURE — never silently neutral)."""
    trad = StageOutcome(
        status="FAIL",
        metric=None,
        threshold=None,
        reason=f"{PROVIDER_FAILURE}: {error}"[:400],
    )
    dq = StageOutcome(
        status="FAIL",
        metric=0.0,
        threshold=None,
        reason=f"{PROVIDER_FAILURE}: fetch raised: {error}"[:400],
    )
    return EvaluationResult(
        symbol=symbol,
        eligibility_status="INELIGIBLE",
        tradability=trad,
        liquidity=StageOutcome(status="INSUFFICIENT_DATA", reason=INSUFFICIENT_DATA),
        volume=StageOutcome(status="INSUFFICIENT_DATA", reason=INSUFFICIENT_DATA),
        volatility=StageOutcome(status="INSUFFICIENT_DATA", reason=INSUFFICIENT_DATA),
        data_quality=dq,
        signal_quality=StageOutcome(status="INSUFFICIENT_DATA", reason=INSUFFICIENT_DATA),
        data_quality_state=PROVIDER_FAILURE,
        failure_reason=f"{PROVIDER_FAILURE}: {error}"[:400],
        fetch_failed=True,
        expected_sessions=0,
        present_sessions=0,
    )


def evaluate_candidate(
    symbol: str,
    df: Optional[pd.DataFrame],
    expected_dates: list[date],
    cfg: RotationEngineConfig,
    signal_metrics: Optional[dict] = None,
    fetch_error: Optional[str] = None,
) -> EvaluationResult:
    """Run every pipeline stage for one candidate.

    ``symbol``       — normalized (engine pre-normalizes).
    ``df``           — OHLCV frame from the provider (None when fetch raised).
    ``expected_dates``— confirmed trading days in the evaluation window.
    ``signal_metrics``— output of ``rotation_replay.replay_window`` when
                        available; otherwise stages needing it report
                        INSUFFICIENT_DATA.
    """
    if fetch_error is not None:
        return provider_failure_evaluation(symbol, fetch_error)

    if not is_valid_symbol(symbol):
        trad = StageOutcome(status="FAIL", reason=f"{INVALID_SYMBOL}: {symbol!r}")
        return EvaluationResult(
            symbol=symbol,
            eligibility_status="INELIGIBLE",
            tradability=trad,
            liquidity=StageOutcome(status="INSUFFICIENT_DATA", reason=NO_DATA),
            volume=StageOutcome(status="INSUFFICIENT_DATA", reason=NO_DATA),
            volatility=StageOutcome(status="INSUFFICIENT_DATA", reason=NO_DATA),
            data_quality=StageOutcome(status="INSUFFICIENT_DATA", reason=NO_DATA),
            signal_quality=StageOutcome(status="INSUFFICIENT_DATA", reason=NO_DATA),
            failure_reason=f"{INVALID_SYMBOL}: {symbol!r}",
        )

    records, _ = build_sessions(df, expected_dates, cfg)
    expected = len(expected_dates)

    if df is None or len(df) == 0:
        trad = StageOutcome(status="FAIL", reason=f"{NO_DATA}: empty history for {symbol}")
    else:
        missing_cols = {"timestamp", "open", "high", "low", "close", "volume"} - set(df.columns)
        trad = (
            StageOutcome(status="FAIL", reason=f"{NO_DATA}: missing columns {sorted(missing_cols)}")
            if missing_cols
            else StageOutcome(status="PASS", reason="valid OHLCV history")
        )
    if records:
        last_close = records[-1].last_close
        if not (math.isfinite(last_close) and last_close > 0):
            trad = StageOutcome(
                status="FAIL", reason=f"{INVALID_OHLC}: last close {last_close!r} is not > 0"
            )

    # ---- S3 liquidity -------------------------------------------------------
    total_values = [r.total_value for r in records if r.n_bars > 0]
    median_value = _median(total_values) if total_values else None
    if len(total_values) < cfg.min_observation_sessions:
        liq = StageOutcome(
            status="INSUFFICIENT_DATA",
            metric=median_value,
            threshold=cfg.liquidity_floor,
            reason=(
                f"{INSUFFICIENT_DATA}: {len(total_values)} session(s) with data "
                f"< MIN_OBSERVATION_SESSIONS ({cfg.min_observation_sessions})"
            ),
        )
    elif median_value is None or median_value < cfg.liquidity_floor:
        liq = StageOutcome(
            status="FAIL",
            metric=median_value,
            threshold=cfg.liquidity_floor,
            reason=(
                f"{BELOW_FLOOR}: median daily traded value {median_value} < "
                f"LIQUIDITY_FLOOR {cfg.liquidity_floor}"
            ),
        )
    else:
        liq = StageOutcome(
            status="PASS", metric=median_value, threshold=cfg.liquidity_floor,
            reason=f"median daily traded value {median_value:.0f} >= {cfg.liquidity_floor}",
        )

    # ---- S4 volume quality ---------------------------------------------------
    means = [r.mean_volume for r in records if r.n_bars > 0 and r.mean_volume > 0]
    mean_volume = sum(means) / len(means) if means else None
    volume_cv = None
    if len(means) >= 2:
        m = sum(means) / len(means)
        var = sum((x - m) ** 2 for x in means) / len(means)
        sd = math.sqrt(var)
        volume_cv = sd / m if m > 0 else None
    elif len(means) == 1:
        volume_cv = 0.0
    if len(records) < cfg.min_observation_sessions:
        vol = StageOutcome(
            status="INSUFFICIENT_DATA",
            metric=mean_volume,
            threshold=cfg.volume_floor,
            reason=(
                f"{INSUFFICIENT_DATA}: {len(records)} session(s) < "
                f"MIN_OBSERVATION_SESSIONS ({cfg.min_observation_sessions})"
            ),
        )
    elif mean_volume is None or mean_volume < cfg.volume_floor:
        vol = StageOutcome(
            status="FAIL",
            metric=mean_volume,
            threshold=cfg.volume_floor,
            reason=f"{BELOW_FLOOR}: mean volume {mean_volume} < VOLUME_FLOOR {cfg.volume_floor}",
        )
    elif volume_cv is not None and volume_cv > cfg.volume_cv_max:
        vol = StageOutcome(
            status="FAIL",
            metric=volume_cv,
            threshold=cfg.volume_cv_max,
            reason=f"{ABOVE_CAP}: volume CV {volume_cv:.2f} > VOLUME_CV_MAX {cfg.volume_cv_max}",
        )
    else:
        vol = StageOutcome(
            status="PASS", metric=volume_cv, threshold=cfg.volume_cv_max,
            reason=f"mean volume {mean_volume:.0f} >= {cfg.volume_floor}, CV {volume_cv}",
        )

    # ---- S5 volatility / ATR --------------------------------------------------
    atr_pcts: list[float] = [
        r.atr_pct
        for r in records
        if r.atr_pct is not None and math.isfinite(r.atr_pct)
    ]
    median_atr = _median(atr_pcts) if atr_pcts else None
    if len(records) < cfg.min_observation_sessions:
        volty = StageOutcome(
            status="INSUFFICIENT_DATA",
            metric=median_atr,
            threshold=(cfg.atr_min_pct, cfg.atr_max_pct),
            reason=(
                f"{INSUFFICIENT_DATA}: {len(records)} session(s) < "
                f"MIN_OBSERVATION_SESSIONS ({cfg.min_observation_sessions})"
            ),
        )
    elif median_atr is None:
        volty = StageOutcome(
            status="INSUFFICIENT_DATA",
            metric=None,
            threshold=(cfg.atr_min_pct, cfg.atr_max_pct),
            reason=f"{INDICATOR_FAILURE}: median %ATR not computable",
        )
    elif not (cfg.atr_min_pct <= median_atr <= cfg.atr_max_pct):
        volty = StageOutcome(
            status="FAIL",
            metric=median_atr,
            threshold=(cfg.atr_min_pct, cfg.atr_max_pct),
            reason=(
                f"{OUTSIDE_BAND}: median %ATR {median_atr:.4f} outside "
                f"[{cfg.atr_min_pct}, {cfg.atr_max_pct}]"
            ),
        )
    else:
        volty = StageOutcome(
            status="PASS",
            metric=median_atr,
            threshold=(cfg.atr_min_pct, cfg.atr_max_pct),
            reason=f"median %ATR {median_atr:.4f} within band",
        )

    # ---- S6 data quality -------------------------------------------------------
    dq_outcome, dq_state, dq_norm, dq_score = evaluate_data_quality(
        symbol, records, expected, fetch_error is not None, cfg
    )

    # ---- S7 signal quality (evaluability) --------------------------------------
    if signal_metrics is None:
        sq = StageOutcome(
            status="INSUFFICIENT_DATA",
            reason=f"{INSUFFICIENT_DATA}: signal replay unavailable",
        )
        valid_sessions = 0
    else:
        valid_sessions = int(signal_metrics.get("valid_evaluated_sessions", 0))
        if valid_sessions < cfg.min_observation_sessions:
            sq = StageOutcome(
                status="INSUFFICIENT_DATA",
                metric=float(valid_sessions),
                threshold=float(cfg.min_observation_sessions),
                reason=(
                    f"{INSUFFICIENT_DATA}: {valid_sessions} valid evaluated "
                    f"sessions < MIN_OBSERVATION_SESSIONS ({cfg.min_observation_sessions})"
                ),
            )
        else:
            sq = StageOutcome(
                status="PASS",
                metric=float(valid_sessions),
                threshold=float(cfg.min_observation_sessions),
                reason=f"{valid_sessions} valid evaluated sessions >= {cfg.min_observation_sessions}",
            )

    # ---- eligibility status ---------------------------------------------------
    failures = [name for name, st in {"tradability": trad, "liquidity": liq,
                                      "volume": vol, "volatility": volty,
                                      "data_quality": dq_outcome,
                                      "signal_quality": sq}.items() if st.status == "FAIL"]
    insufficient = [name for name, st in {"tradability": trad, "liquidity": liq,
                                          "volume": vol, "volatility": volty,
                                          "data_quality": dq_outcome,
                                          "signal_quality": sq}.items()
                    if st.status == "INSUFFICIENT_DATA"]
    if failures:
        eligibility = "INELIGIBLE"
    elif insufficient:
        eligibility = "INSUFFICIENT_DATA"
    else:
        eligibility = "ELIGIBLE"

    # failure_reason: first non-PASS stage (deterministic order)
    reason_parts = []
    for name, st in {
        "tradability": trad, "liquidity": liq, "volume": vol,
        "volatility": volty, "data_quality": dq_outcome, "signal_quality": sq,
    }.items():
        if st.status != "PASS":
            reason_parts.append(f"{name}: {st.reason}")
    failure_reason = "; ".join(reason_parts)[:2000] if reason_parts else None

    # core-selection helper metrics
    trading_consistency = (
        (sum(1 for r in records if r.mean_volume > 0) / expected) if expected else None
    )
    signal_reliability = (valid_sessions / expected) if expected else None

    result = EvaluationResult(
        symbol=symbol,
        eligibility_status=eligibility,
        tradability=trad,
        liquidity=liq,
        volume=vol,
        volatility=volty,
        data_quality=dq_outcome,
        signal_quality=sq,
        data_quality_state=dq_state,
        failure_reason=failure_reason,
        fetch_failed=False,
        expected_sessions=expected,
        present_sessions=len(records),
        valid_evaluated_sessions=valid_sessions,
        median_daily_value=median_value,
        mean_volume=mean_volume,
        volume_cv=volume_cv,
        median_atr_pct=median_atr,
        data_quality_norm=dq_norm,
        data_quality_score=dq_score,
        trading_consistency=trading_consistency,
        signal_reliability=signal_reliability,
        malformed_rate=(
            sum(r.malformed_bars for r in records) / sum(r.n_bars for r in records)
            if records and sum(r.n_bars for r in records) else None
        ),
        indicator_failure_rate=(
            sum(1 for r in records if r.indicator_failure) / len(records)
            if records else None
        ),
        missing_candle_ratio=(
            sum(max(0.0, 1.0 - r.n_bars / r.expected_bars) for r in records) / len(records)
            if records else None
        ),
    )
    if signal_metrics:
        result.long_count = int(signal_metrics.get("long_count", 0))
        result.short_count = int(signal_metrics.get("short_count", 0))
        result.no_trade_count = int(signal_metrics.get("no_trade_count", 0))
        result.total_signal_count = int(signal_metrics.get("total_signal_count", 0))
        result.frequency = signal_metrics.get("frequency")
        result.long_frequency = signal_metrics.get("long_frequency")
        result.short_frequency = signal_metrics.get("short_frequency")
        result.long_sample_ok = bool(signal_metrics.get("long_sample_ok", False))
        result.short_sample_ok = bool(signal_metrics.get("short_sample_ok", False))
        result.long_raw = signal_metrics.get("long_raw")
        result.short_raw = signal_metrics.get("short_raw")
        result.session_class_counts = dict(signal_metrics.get("session_class_counts", {}))
    return result