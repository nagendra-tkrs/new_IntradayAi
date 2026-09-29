"""Explicitly produce and activate the LIVE 80-stock trading universe (Phase 1).

This is the one-time/refresh activation runner for ``LIVE_UNIVERSE=rotation_80``.
It uses ONLY the existing validated machinery — no scheduler, no startup hook,
no manual injection of symbols:

   1. resolve candidate pool  (universe repo ∪ NSE archive NIFTY200 union)
   2. persist + validate the pool         (Phase 5B resolution bridge)
   3. UniverseRotationEngine.generate_draft(pool_id=...)   (real eligibility)
   4. validate_universe gate             (DRAFT -> VALIDATED or REJECTED)
   5. approve_universe                   (manual audited decision, USER actor)
   6. activate_universe                  (APPROVED -> ACTIVE; guard: outside
                                         market hours on a confirmed trading day)

FAIL CLOSED: if fewer than 80 candidates are ELIGIBLE (or the pool cannot be
resolved / the draft fails validation) the script aborts and NEVER silently
falls back to the legacy 40-stock universe. Nothing is fabricated.

Usage (run from the ``backend`` directory):

    python scripts/activate_live_universe.py                  # dry-run
    python scripts/activate_live_universe.py --yes            # real activation
    python scripts/activate_live_universe.py --yes --actor admin@example.com

``--dry-run`` performs the full candidate-pool resolution and the real
eligibility evaluation WITHOUT persisting anything — a feasibility forecast.
The real run (``--yes``) persists pool -> DRAFT -> VALIDATED -> APPROVED ->
ACTIVE; a timestamped note is printed so the manual audit trail is clear.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta

# Make `app` importable when this file is run directly from scripts/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.services import rotation_eligibility as elig
from app.services.candidate_pool import (
    SOURCE_NIFTY200,
    SOURCE_REPO,
    create_pool_from_resolution,
    fetch_nse_archive_source,
    fetch_repository_universe_source,
    nse_archive_source,
    repository_universe_provider,
    resolve_candidate_pool,
)
from app.services.market_data.provider_factory import get_provider
from app.services.rotation_config import RotationEngineConfig
from app.services.universe_rotation_engine import UniverseRotationEngine
from app.services.universe_state_service import UniverseStateService

MIN_ELIGIBLE = 80  # exact 30 Core + 50 Rotation shape
POOL_MIN = 150

DEFAULT_ACTOR = "phase1-activator"


async def _resolve_pool() -> dict:
    """Real candidate-pool resolution (repo union NIFTY200 archive)."""
    sources = [repository_universe_provider(), nse_archive_source("nifty200")]
    fetchers = {
        SOURCE_REPO: fetch_repository_universe_source,
        SOURCE_NIFTY200: lambda: fetch_nse_archive_source("nifty200"),
    }
    return resolve_candidate_pool(
        sources,
        fetchers,
        pool_min=POOL_MIN,
        pool_target=POOL_MIN,
        pool_max=250,
    )


async def _dry_run() -> int:
    print("== Phase 1 activation — DRY RUN (no database writes) ==")
    res = await _resolve_pool()
    symbols = sorted(res.symbols)
    print(f"Candidate pool      : {len(symbols)} unique symbols "
          f"({res.target_status})")
    if res.failures:
        print(f"Pool source failures: {res.failures}")
    if len(symbols) < MIN_ELIGIBLE:
        print(f"ABORT: pool has only {len(symbols)} candidates "
              f"(need >= {MIN_ELIGIBLE}); no fabrication allowed.")
        return 2

    cfg = RotationEngineConfig.from_settings()
    calc_dt = datetime.utcnow()
    window_end = calc_dt.date()
    window_start = window_end - timedelta(days=cfg.window_weeks * 7 - 1)
    expected_dates = elig.confirmed_trading_days(window_start, window_end)
    window_days = (window_end - window_start).days + 1
    print(f"Evaluation window   : {window_start}..{window_end} "
          f"({len(expected_dates)} expected sessions)")

    engine = UniverseRotationEngine(provider=get_provider(), config=cfg)
    evaluations = await engine._evaluate_all(symbols, expected_dates, window_days, cfg)
    counts = engine._stage_counts(evaluations)
    eligible = sum(1 for ev in evaluations.values()
                   if ev.eligibility_status == "ELIGIBLE")
    print(f"Stage counts        : {counts}")
    print(f"ELIGIBLE candidates : {eligible} "
          f"({'OK  >= ' + str(MIN_ELIGIBLE) if eligible >= MIN_ELIGIBLE else 'ABORT: insufficient'})")
    if eligible < MIN_ELIGIBLE:
        print("No draft would be created (engine persists nothing on "
              "INSUFFICIENT_CANDIDATES).")
        return 2
    print("Dry-run complete: a real run would persist pool -> DRAFT -> "
          "VALIDATED -> APPROVED -> ACTIVE.")
    return 0


async def _real_run(actor: str, reason: str) -> int:
    print("== Phase 1 activation — REAL RUN ==")
    if (settings.LIVE_UNIVERSE or "").strip().lower() != "rotation_80":
        print(f"ABORT: LIVE_UNIVERSE={settings.LIVE_UNIVERSE!r}; this script only "
              "activates the rotation_80 live universe.")
        return 2

    service = UniverseStateService()
    res = await _resolve_pool()
    symbols = sorted(res.symbols)
    print(f"Candidate pool      : {len(symbols)} unique symbols "
          f"({res.target_status})")
    if res.failures:
        print(f"Pool source failures: {res.failures}")

    pool = await create_pool_from_resolution(
        res, service, source="phase5b-resolution",
        actor=actor, reason=reason,
    )
    print(f"Pool persisted      : {pool['id']} status={pool['status']} "
          f"symbols={len(symbols)}")

    engine = UniverseRotationEngine(state_service=service, provider=get_provider())
    result = await engine.generate_draft(
        pool_id=pool["id"],
        source="phase5b-resolution",
        source_reference=f"pool_hash={res.pool_hash}",
        actor=actor,
        reason=reason,
    )
    if result["status"] != "DRAFT_CREATED":
        counts = result.get("counts", {})
        print("ABORT: draft generation failed (no draft persisted).")
        print(f"  status   : {result['status']}")
        print(f"  eligible : {counts.get('eligible')} "
              f"(need >= {MIN_ELIGIBLE})")
        print(f"  counts   : {counts}")
        return 2
    draft = result["draft"]
    print(f"Draft created       : {draft['id']} version={draft['version']} "
          f"status={draft['status']} "
          f"({draft['core_count']} core / {draft['rotation_count']} rotation)")
    print(f"Eligible candidates : {result['counts'].get('eligible')}")

    validated = await service.validate_universe(draft["id"], actor=actor)
    print(f"Validation gate     : {validated['status']} "
          f"(validated_at={validated.get('validated_at')})")

    approved = await service.approve_universe(
        draft["id"], actor=actor, actor_type="USER",
        reason="Phase 1 explicit activation review of the rotation_80 live universe",
    )
    print(f"Approved            : {approved['status']} approval_actor={approved['approval_actor']}")

    try:
        active = await service.activate_universe(
            draft["id"], actor=actor,
            reason="Phase 1 activation of the rotation_80 live universe",
        )
    except Exception as exc:  # UniverseMarketHoursError / state transition
        print(f"ACTIVATION REFUSED: {type(exc).__name__}: {exc}")
        print("The draft is APPROVED but not ACTIVE. Activate outside market "
              "hours (after 15:30 IST) on a confirmed trading day by re-running "
              "with --yes (approval is idempotent).")
        return 1

    print(f"ACTIVE universe     : {active['id']} version={active['version']} "
          f"status={active['status']}")
    detail = await service.get_active_universe(with_detail=True)
    members = detail.get("members", []) or []
    core = [m for m in members if m.get("category") == "CORE"]
    rotation = [m for m in members if m.get("category") == "ROTATION"]
    print(f"Active universe     : total={len(members)} core={len(core)} "
          f"rotation={len(rotation)} duplicates="
          f"{len(members) - len({m.get('symbol') for m in members})}")
    print("The live scanner now consumes this 80-stock universe (FAIL CLOSED "
          "if the ACTIVE version is ever missing).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Forecast eligibility WITHOUT persisting anything")
    parser.add_argument("--yes", action="store_true",
                        help="Actually persist + activate (no prompt)")
    parser.add_argument("--actor", default=DEFAULT_ACTOR,
                        help=f"Manual audited approval actor (default {DEFAULT_ACTOR})")
    args = parser.parse_args()

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reason = f"Phase 1 explicit activation of rotation_80 live universe ({now})"

    if args.yes and not args.dry_run:
        return asyncio.run(_real_run(args.actor, reason))

    print("DRY RUN MODE. Add --yes to actually persist + activate.\n")
    return asyncio.run(_dry_run())


if __name__ == "__main__":
    sys.exit(main())