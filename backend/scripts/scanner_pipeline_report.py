"""Print the Top-Signals pipeline funnel for one scanner execution.

Observation only. Changes no scoring, no selection, no strategy rule.

    python scripts/scanner_pipeline_report.py
    python scripts/scanner_pipeline_report.py --json-out diag.json
    python scripts/scanner_pipeline_report.py --universe NIFTY50

Runs entirely in-process: it fetches market data, evaluates the live signal
engine and the Top Signal Quality Layer, and prints where candidates are lost.
It deliberately does NOT call scanner._process_stock, because that path writes
signals and shadow-selection rows to the database — this report must not add a
single row to the ledger.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.active_universe import get_active_trading_universe  # noqa: E402
from app.services.active_universe import is_active_universe_request  # noqa: E402
from app.services.indicators import calculate_all_indicators  # noqa: E402
from app.services.market_data.provider_factory import get_provider  # noqa: E402
from app.services.scanner_diagnostics import (  # noqa: E402
    build_scan_diagnostics,
    diagnostics_text,
)
from app.services.signal_engine import evaluate_signal  # noqa: E402
from app.services.signal_quality import (  # noqa: E402
    compute_setup_quality,
    rank_top_signals,
    select_top_signals,
)

MAX_PARALLEL_FETCHES = 8


async def _market_context(provider) -> dict:
    ctx = {"nifty_trend": "UNKNOWN", "nifty_change_pct": 0.0,
           "banknifty_change_pct": 0.0, "market_open": None}
    try:
        nifty = await provider.get_market_index("NIFTY50")
        chg = nifty.get("change_pct", 0.0)
        ctx["nifty_change_pct"] = chg
        ctx["nifty_trend"] = "BULLISH" if chg > 0.3 else "BEARISH" if chg < -0.3 else "UNKNOWN"
        ctx["nifty_data_status"] = nifty.get("data_status", "UNKNOWN")
    except Exception as exc:  # never fabricated
        ctx["nifty_error"] = str(exc)
    try:
        bn = await provider.get_market_index("BANKNIFTY")
        ctx["banknifty_change_pct"] = bn.get("change_pct", 0.0)
        ctx["banknifty_data_status"] = bn.get("data_status", "UNKNOWN")
    except Exception as exc:
        ctx["banknifty_error"] = str(exc)
    return ctx


async def run(universe: str) -> dict:
    provider = get_provider()
    if is_active_universe_request(universe):
        resolution = await get_active_trading_universe()
        instruments = resolution["stocks"]
        label = f"ACTIVE:{resolution.get('universe_version_id')}"
    else:
        instruments = await provider.get_instruments(universe)
        label = universe

    ctx = await _market_context(provider)
    sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

    async def fetch(inst):
        async with sem:
            try:
                return inst, await provider.get_quote_and_bars(inst["symbol"], timeframe="5m")
            except Exception as exc:
                return inst, {"df": None, "quote": None, "error": str(exc)}

    fetched = await asyncio.gather(*[fetch(i) for i in instruments])

    rows: list[dict] = []
    for inst, combined in fetched:
        sym = inst["symbol"]
        base = {"symbol": sym, "name": inst.get("name", sym),
                "sector": inst.get("sector", "Unknown")}
        if inst.get("classification"):
            base["classification"] = inst["classification"]

        if combined.get("error"):
            rows.append({**base, "price": 0, "change_pct": 0, "volume": 0,
                         "signal": "ERROR", "confidence": 0,
                         "data_status": "UNAVAILABLE", "error": combined["error"]})
            continue

        df, quote = combined["df"], combined["quote"]
        if df is None or len(df) < 55:
            rows.append({**base, "price": 0, "change_pct": 0, "volume": 0,
                         "signal": "NO_TRADE", "confidence": 0,
                         "data_status": "UNAVAILABLE",
                         "reason": "Insufficient candle history"})
            continue

        df = calculate_all_indicators(df)
        row = df.iloc[-1]
        price = float(quote.get("price") or 0.0)
        atr = float(row.get("atr_14") or 0.0)

        result = {**base, "price": price,
                  "change_pct": quote.get("change_pct", 0),
                  "volume": quote.get("volume", 0),
                  "data_status": quote.get("data_status", "UNKNOWN"),
                  "data_age_seconds": quote.get("data_age_seconds"),
                  "data_timestamp": quote.get("timestamp"),
                  "atr": round(atr, 2) if pd_notna(atr) else None}

        sig = evaluate_signal(
            df, sym,
            data_source=provider.data_source_label,
            market_context=ctx,
            data_age_seconds=quote.get("data_age_seconds"),
            data_status=quote.get("data_status", "UNKNOWN"),
            market_data_timestamp=quote.get("timestamp"),
        )
        result["signal_data"] = sig
        result["signal"] = str(sig["direction"]) if sig else "NO_TRADE"
        result["confidence"] = sig["confidence"] if sig else 0.0
        if not sig:
            result["reason"] = "No signal object produced"
        quality = compute_setup_quality(sig, row, market_context=ctx, df=df)
        rows.append({**result, **quality})

    ranked = rank_top_signals(rows)
    top = select_top_signals(ranked)
    diag = build_scan_diagnostics(ranked, top)
    diag["universe"] = label
    diag["market_context"] = ctx
    diag["quality_distribution"] = dict(
        Counter(r.get("setup_quality") for r in ranked if r.get("setup_quality")))
    diag["top_signals"] = [
        {"symbol": r["symbol"], "signal": r["signal"],
         "setup_quality": r.get("setup_quality"),
         "setup_quality_score": r.get("setup_quality_score"),
         "confirmation_count": r.get("confirmation_count"),
         "risk_reward_ratio": r.get("risk_reward_ratio"),
         "confidence": r.get("confidence"), "rank": r.get("top_signal_rank")}
        for r in top
    ]
    return diag


def pd_notna(v) -> bool:
    import pandas as pd
    return not pd.isna(v)


def main() -> int:
    ap = argparse.ArgumentParser(description="Top-Signals pipeline funnel (read-only)")
    ap.add_argument("--universe", default="ACTIVE")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    diag = asyncio.run(run(args.universe))

    print(diagnostics_text(diag))
    print()
    print(f"UNIVERSE  : {diag['universe']}")
    print(f"CONTEXT   : {diag['market_context']}")
    print(f"QUALITY   : {diag['quality_distribution']}")
    print()
    print("TOP SIGNALS")
    if diag["top_signals"]:
        for t in diag["top_signals"]:
            print(f"  #{t['rank']} {t['symbol']:<12} {t['signal']:<12} "
                  f"{t['setup_quality']:<10} score={t['setup_quality_score']} "
                  f"conf={t['confirmation_count']}/8 rr={t['risk_reward_ratio']} "
                  f"signal_conf={t['confidence']}")
    else:
        print("  (none — see the NOTES above for whether this is a data or a "
              "strategy condition)")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(diag, indent=2, default=str), encoding="utf-8")
        print(f"\nJSON written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
