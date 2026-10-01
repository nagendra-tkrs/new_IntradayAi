"""Structured scanner-pipeline diagnostics (measurement only).

Answers one question precisely: **at which stage does a candidate disappear?**
It is a PURE module. It takes the finished scanner row list and reports counts.
It never fetches, never scores, never filters, never mutates a row, and can
never influence which signals are produced or shown.

Why this exists
---------------
``scanner._process_stock`` has FOUR early-return paths that produce a row with
**no** ``setup_quality`` / ``top_signal_eligible`` field at all:

    1. ``_error``                        -> signal="ERROR",      data_status="UNAVAILABLE"
    2. ``len(df) < 55``                  -> signal="NO_TRADE",   reason="Insufficient candle history"
    3. ``data_status.should_trate()``    -> signal="NO_TRADE",   reason=<gate reason>
    4. ``signal`` falsy                  -> signal="NO_TRADE",   signal_data=None

Those rows are structurally invisible to Top Signals. Before this module the
API could not distinguish "the strategy judged this setup weak" from "we never
evaluated this setup because the data failed" — both simply read as
``top_signal_eligible`` absent. That ambiguity is what made the Top-Signals
pipeline impossible to debug from the outside.

Contract
--------
* Every count is a plain ``int`` and every requested key is ALWAYS present, so a
  consumer never has to guard on ``None``.
* Counts are observations of rows that already exist. This module never
  invents, pads, re-ranks or repairs anything.
* ``not_evaluated`` is reported separately from ``rejected`` on purpose:
  unevaluated rows are a DATA problem, rejected rows are a STRATEGY decision.
  Collapsing them is how a data outage gets mistaken for "no signals today".
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional

# The exact count keys required by the investigation. Declared up front so a
# missing key is a test failure rather than a silent None in a dashboard.
DIAGNOSTIC_COUNT_KEYS: tuple[str, ...] = (
    "total_scanned",
    "valid_market_data",
    "signal_generated",
    "LONG",
    "STRONG_LONG",
    "WEAK_LONG",
    "SHORT",
    "STRONG_SHORT",
    "WEAK_SHORT",
    "NO_TRADE",
    "rejected",
    "weak",
    "normal",
    "qualified",
    "premium",
    "conflict_rejected",
    "rr_rejected",
    "missing_setup",
    "missing_price",
    "missing_atr",
    "missing_signal",
    "final_top_signal_candidates",
    "final_top_signals",
)

# Extra keys that are not in the requested list but are required to explain a
# zero. Additive only.
_EXTRA_COUNT_KEYS: tuple[str, ...] = (
    "fetch_error",
    "insufficient_history",
    "data_gate_blocked",
    "quality_not_evaluated",
    "not_evaluated",
    "unranked",
)

_LONG = ("LONG", "STRONG_LONG", "WEAK_LONG")
_SHORT = ("SHORT", "STRONG_SHORT", "WEAK_SHORT")


def _num(value: Any) -> Optional[float]:
    """Finite float or None. Never invents a number."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _dir(row: dict) -> str:
    d = row.get("signal")
    return d.upper() if isinstance(d, str) else ""


def _is_long(row: dict) -> bool:
    return _dir(row) in _LONG


def _is_short(row: dict) -> bool:
    return _dir(row) in _SHORT


def _directional(row: dict) -> bool:
    return _is_long(row) or _is_short(row)


def _quality(row: dict) -> Optional[str]:
    q = row.get("setup_quality")
    return q if isinstance(q, str) and q else None


def _reasons(row: dict) -> str:
    sd = row.get("signal_data")
    if isinstance(sd, dict):
        rs = sd.get("reasons")
        if isinstance(rs, list):
            return " ".join(str(r) for r in rs)
    return str(row.get("reason") or "")


def _early_return(row: dict) -> Optional[str]:
    """Which of the four early-return paths produced this row, if any."""
    if row.get("error") or _dir(row) == "ERROR":
        return "fetch_error"
    if str(row.get("reason") or "").startswith("Insufficient candle history"):
        return "insufficient_history"
    if _quality(row) is None and row.get("reason"):
        return "data_gate_blocked"
    return None


def build_scan_diagnostics(
    results: Iterable[dict],
    top_signals: Optional[Iterable[dict]] = None,
) -> dict:
    """Return structured funnel diagnostics for one scanner execution.

    Parameters
    ----------
    results     : the exact row list ``scanner.scan_universe`` returned.
    top_signals : the exact list the API is about to return (optional; when
                  omitted the top count is recomputed from ``top_signal_eligible``
                  order, which is the same count).

    Returns a dict with:
        counts  : every key in DIAGNOSTIC_COUNT_KEYS (always int)
        stages  : ordered funnel — input -> rejected -> output per stage
        losses  : which stage removed the most candidates
        notes   : plain-language flags for any zero a reader would misread
    """
    rows = [r for r in (results or []) if isinstance(r, dict)]

    c: dict[str, int] = {k: 0 for k in DIAGNOSTIC_COUNT_KEYS}
    c.update({k: 0 for k in _EXTRA_COUNT_KEYS})

    early: dict[str, list[str]] = {"fetch_error": [], "insufficient_history": [],
                                   "data_gate_blocked": []}

    for r in rows:
        c["total_scanned"] += 1

        # ---- stage: data ---------------------------------------------------
        e = _early_return(r)
        if e:
            c[e] += 1
            early[e].append(str(r.get("symbol") or "?"))
            c["not_evaluated"] += 1
            if e == "fetch_error":
                continue
        else:
            c["valid_market_data"] += 1

        d = _dir(r)

        # ---- stage: signal generated ---------------------------------------
        if r.get("signal_data") is None:
            c["missing_signal"] += 1
            if d not in _LONG and d not in _SHORT and d != "NO_TRADE":
                continue
        else:
            c["signal_generated"] += 1

        # ---- stage: direction ---------------------------------------------
        for name in _LONG + _SHORT:
            if d == name:
                c[name] += 1
        if d == "NO_TRADE":
            c["NO_TRADE"] += 1

        # ---- engine rejections (reasons only — never re-decided here) ------
        reasons = _reasons(r)
        if "Conflicting evidence" in reasons or "conflict" in reasons.lower() and "rejected" in reasons.lower():
            c["conflict_rejected"] += 1
        if "risk/reward below" in reasons:
            c["rr_rejected"] += 1

        # ---- stage: data completeness -------------------------------------
        price = _num(r.get("price"))
        if price is None or price <= 0:
            c["missing_price"] += 1
        atr = _num(r.get("atr"))
        if atr is None or atr <= 0:
            c["missing_atr"] += 1

        # ---- stage: setup quality -----------------------------------------
        q = _quality(r)
        if q is None:
            c["missing_setup"] += 1
            c["quality_not_evaluated"] += 1
            c["not_evaluated"] += 1
        else:
            c[q.lower()] += 1

        # ---- stage: eligibility -------------------------------------------
        if r.get("top_signal_eligible") is True:
            c["final_top_signal_candidates"] += 1
        else:
            c["unranked"] += 1

    if top_signals is not None:
        c["final_top_signals"] = len(list(top_signals))
    else:
        c["final_top_signals"] = c["final_top_signal_candidates"]

    stages = _build_stages(rows, c, early)
    notes = _build_notes(c, early)

    return {
        "counts": c,
        "stages": stages,
        "losses": _build_losses(stages),
        "notes": notes,
        "contract": "OBSERVATION_ONLY_NO_DECISION_AUTHORITY",
    }


# ────────────────────────────────────────────────────────────────────────────
# Funnel
# ────────────────────────────────────────────────────────────────────────────

def _build_stages(rows: list[dict], c: dict, early: dict) -> list[dict]:
    n = len(rows)
    fetch_err = c["fetch_error"]
    hist = c["insufficient_history"]
    gate = c["data_gate_blocked"]
    directional = c["LONG"] + c["SHORT"]
    missing_sig = c["missing_signal"]

    return [
        {"stage": 1, "key": "scanned", "label": "Symbols returned by the scan",
         "input": n, "rejected": 0, "output": n, "rejected_by": []},
        {"stage": 2, "key": "valid_market_data", "label": "Usable market data",
         "input": n, "rejected": fetch_err + hist + gate, "output": c["valid_market_data"],
         "rejected_by": sorted(k for k, v in (
             ("fetch_error", fetch_err),
             ("insufficient_history", hist),
             ("data_gate_blocked", gate),
         ) if v)},
        {"stage": 3, "key": "signal_generated", "label": "Signal object produced",
         "input": c["valid_market_data"], "rejected": missing_sig,
         "output": c["signal_generated"], "rejected_by": ["missing_signal"] if missing_sig else []},
        {"stage": 4, "key": "directional", "label": "Directional (LONG or SHORT)",
         "input": c["signal_generated"], "rejected": c["NO_TRADE"],
         "output": directional, "rejected_by": ["NO_TRADE"] if c["NO_TRADE"] else []},
        {"stage": 5, "key": "quality_evaluated", "label": "Setup quality classified",
         "input": directional, "rejected": c["quality_not_evaluated"],
         "output": c["quality_evaluated"] if "quality_evaluated" in c else
                   directional - c["quality_not_evaluated"],
         "rejected_by": ["missing_setup"] if c["missing_setup"] else []},
        {"stage": 6, "key": "eligible", "label": "PREMIUM / QUALIFIED (top-signal eligible)",
         "input": directional - c["quality_not_evaluated"],
         "rejected": (directional - c["quality_not_evaluated"]) - c["final_top_signal_candidates"],
         "output": c["final_top_signal_candidates"],
         "rejected_by": ["not_premium_or_qualified"]},
        {"stage": 7, "key": "final_top_signals", "label": "Top Signals returned by the API",
         "input": c["final_top_signal_candidates"],
         "rejected": max(0, c["final_top_signal_candidates"] - c["final_top_signals"]),
         "output": c["final_top_signals"], "rejected_by": ["top_signals_limit"]},
    ]


def _build_losses(stages: list[dict]) -> list[dict]:
    return sorted(
        ({"stage": s["stage"], "key": s["key"], "label": s["label"],
          "rejected": s["rejected"], "rejected_by": s["rejected_by"]}
         for s in stages),
        key=lambda s: -s["rejected"],
    )


def _build_notes(c: dict, early: dict) -> list[dict]:
    notes: list[dict] = []

    def note(sev: str, code: str, msg: str) -> None:
        notes.append({"severity": sev, "code": code, "message": msg})

    if c["fetch_error"]:
        note("critical", "DATA_FETCH_ERRORS",
             f"{c['fetch_error']} symbol(s) failed the data fetch and were never "
             f"evaluated. Symbols: {', '.join(early['fetch_error'][:15])}"
             + (" ..." if len(early["fetch_error"]) > 15 else ""))
    if c["insufficient_history"]:
        note("high", "INSUFFICIENT_HISTORY",
             f"{c['insufficient_history']} symbol(s) had fewer than 55 candles and "
             f"were never evaluated. Symbols: {', '.join(early['insufficient_history'][:15])}"
             + (" ..." if len(early["insufficient_history"]) > 15 else ""))
    if c["data_gate_blocked"]:
        note("high", "DATA_GATE_BLOCKED",
             f"{c['data_gate_blocked']} symbol(s) were blocked by the data-status gate "
             f"and were never evaluated. Symbols: {', '.join(early['data_gate_blocked'][:15])}"
             + (" ..." if len(early["data_gate_blocked"]) > 15 else ""))
    if c["missing_atr"]:
        note("medium", "MISSING_ATR",
             f"{c['missing_atr']} evaluated row(s) carry no usable ATR — the ATR-based "
             "SL/T1/T2 geometry and the R:R ratio cannot be computed for them.")
    if c["missing_price"]:
        note("medium", "MISSING_PRICE",
             f"{c['missing_price']} row(s) carry a non-positive or missing price.")
    if c["conflict_rejected"]:
        note("info", "CONFLICT_REJECTED",
             f"{c['conflict_rejected']} row(s) were forced to NO_TRADE by the engine's "
             "conflict rule (min(long_total, short_total) >= 20). Threshold unchanged.")
    if c["rr_rejected"]:
        note("info", "RR_REJECTED",
             f"{c['rr_rejected']} row(s) were rejected or downgraded by the engine's "
             "risk/reward gate (T1-based; 1.2 normal / 2.0 STRONG). Threshold unchanged.")
    if c["final_top_signals"] == 0 and c["final_top_signal_candidates"] > 0:
        note("critical", "TOP_SIGNALS_ZERO_WITH_CANDIDATES",
             f"{c['final_top_signal_candidates']} eligible candidate(s) exist but 0 Top "
             "Signals were returned — this is a selection/serialisation defect, not a "
             "strategy outcome.")
    if c["final_top_signals"] == 0 and c["final_top_signal_candidates"] == 0:
        sev = "critical" if c["not_evaluated"] else "info"
        note(sev, "TOP_SIGNALS_EMPTY",
             "No eligible candidates."
             + (f" {c['not_evaluated']} row(s) were never evaluated (see above) — the "
                "empty result is a DATA condition, not a strategy judgement."
                if c["not_evaluated"] else
                " Every row reached the quality layer, so the strategy genuinely "
                "produced no PREMIUM/QUALIFIED setup in this scan."))
    return notes


def diagnostics_text(diag: dict) -> str:
    """Human-readable rendering used by the CLI script and by logs."""
    lines: list[str] = []
    counts = diag["counts"]
    lines.append("SCANNER PIPELINE DIAGNOSTICS (observation only)")
    lines.append("=" * 72)
    lines.append("STAGE FUNNEL")
    for s in diag["stages"]:
        why = f"  <- {', '.join(s['rejected_by'])}" if s["rejected_by"] else ""
        lines.append(f"  {s['stage']}. {s['label']:<44} "
                     f"in={s['input']:>4} -rej={s['rejected']:>4} -> out={s['output']:>4}{why}")
    lines.append("")
    lines.append("COUNTS")
    width = max(len(k) for k in counts)
    for k, v in counts.items():
        lines.append(f"  {k:<{width}} {v:>6}")
    if diag["notes"]:
        lines.append("")
        lines.append("NOTES")
        for n in diag["notes"]:
            lines.append(f"  [{n['severity'].upper()}] {n['code']}: {n['message']}")
    return "\n".join(lines)
