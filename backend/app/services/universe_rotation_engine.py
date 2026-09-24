"""Universe / Rotation Engine — DRAFT-only orchestrator (Phase 4).

Produces a DRAFT universe version (target 30 Core + 50 Rotation = 80) from a
validated candidate pool and markets the persistence machinery of Phase 3
(``UniverseStateService``) — this module contains NO scoring logic itself; it
orchestrates the pure, separately-tested utilities:

    rotation_eligibility.evaluate_candidate  — S2→S7 pipeline stages
    rotation_replay.replay_window           — historical signal replay metrics
    rotation_scoring.*                       — Rotation Score components
    rotation_selection.select_core/rotation  — Core (stability) + Rotation + anti-churn

Hard Phase 4 guarantees:

* DRAFT only — nothing here approves or activates a universe and nothing
  switches the scanner (``UNIVERSE_SOURCE=legacy`` and the 40-stock
  ``NSE_UNIVERSES`` are untouched). No scheduler and no startup hook invokes
  this engine (``settings.ROTATION_ENGINE_ENABLED`` is False by default).
* No fabricated candidates — the candidate pool is the only input; if fewer
  than 30+50 candidates exist, the engine returns a structured
  INSUFFICIENT_CANDIDATES result and persists NOTHING.
* Transaction-safe — all persistence happens through one
  ``create_universe_draft`` call (one commit): a failed cycle can never leave
  partial memberships or partial scores behind.
* Deterministic — no randomness, no iteration-order dependence; symbol order is
  sorted, every ranking sort has an explicit tie-break, and ``config_hash``
  reproduces the exact configuration a draft was built from.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from typing import Any

from app.core.market_session import is_holiday
from app.models.universe_models import (
    ROTATION_SCORE_COMPONENT_WEIGHTS,
    EligibilityResultStatus,
    FilterResult,
    UniverseCategory,
    UniverseStatus,
)
from app.services import rotation_eligibility as elig
from app.services import rotation_replay as replay
from app.services import rotation_scoring as scoring
from app.services import rotation_selection as selection
from app.services.rotation_config import RotationEngineConfig
from app.services.universe_state_service import UniverseStateService

ENGINE_VERSION = "phase4.v1"

# Component score keys consumed by the Phase 3 score table (slot_scores).
SCORE_KEYS = [
    "liquidity_score",
    "volume_quality_score",
    "volatility_score",
    "signal_frequency_score",
    "long_quality_score",
    "short_quality_score",
    "data_quality_score",
]


def compute_config_hash(cfg: RotationEngineConfig) -> str:
    """Deterministic SHA-256 over the full engine config + the fixed weights.

    A draft is reproducible from (config_hash, pool_id, historical data) — the
    persisted ``config_hash`` IS the version metadata for the score definition.
    """
    payload = {
        "config": cfg.to_dict(),
        "weights": dict(sorted(ROTATION_SCORE_COMPONENT_WEIGHTS.items())),
        "engine_version": ENGINE_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _normalize_effective_from(effective_from, calc_dt: datetime):
    """effective_from datetime (09:15 IST boundary); defaults one week out."""
    if effective_from is None:
        return (calc_dt + timedelta(days=7)).replace(hour=9, minute=15, second=0, microsecond=0)
    if isinstance(effective_from, date) and not isinstance(effective_from, datetime):
        return datetime(effective_from.year, effective_from.month, effective_from.day, 9, 15)
    return effective_from


def _err_msg(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    return f"{type(exc).__name__}: {text[:200]}"


class UniverseRotationEngine:
    """Shadow/draft engine: ``generate_draft`` → DRAFT universe or insufficiency.

    ``provider``     — any ``MarketDataProvider`` (fixture providers in tests;
                       production default: ``provider_factory.get_provider()``).
    ``state_service``— Phase 3 ``UniverseStateService`` (temp-SQLite in tests).
    ``config``       — ``RotationEngineConfig``; defaults ``from_settings``.
    """

    def __init__(
        self,
        *,
        state_service: Optional[UniverseStateService] = None,
        provider=None,
        config: Optional[RotationEngineConfig] = None,
    ):
        self._service = state_service if state_service is not None else UniverseStateService()
        self._provider = provider
        self._config = config if config is not None else RotationEngineConfig.from_settings()

    @property
    def provider(self):
        if self._provider is None:
            from app.services.market_data.provider_factory import get_provider

            self._provider = get_provider()
        return self._provider

    # ------------------------------------------------------------------ pool
    async def _resolve_pool(
        self,
        *,
        pool_id: Optional[str],
        symbols: Optional[list[str]],
        source: str,
        source_reference: Optional[str],
        calc_dt: datetime,
    ) -> dict:
        cfg = self._config
        if pool_id is not None:
            pool = await self._service.get_candidate_pool(pool_id)
            if pool is None:
                raise ValueError(f"candidate pool {pool_id} not found")
            if pool["status"] != "VALIDATED":
                raise ValueError(
                    f"candidate pool {pool_id} must be VALIDATED for universe "
                    f"generation (status={pool['status']})"
                )
            meta = self._pool_symbols(pool)
            if not meta:
                raise ValueError(
                    f"candidate pool {pool_id} has no persisted symbol list "
                    "(metadata_json['symbols'] is required for the engine input)"
                )
            return pool, meta

        if not symbols:
            raise ValueError("symbols or pool_id required")
        ordered = self._dedupe_symbols(symbols)
        pool_version = (
            "pool-"
            + f"{calc_dt:%Y%m%d}-"
            + hashlib.sha1(",".join(sorted(ordered)).encode("utf-8")).hexdigest()[:8]
        )
        meta = {
            "symbols": ordered,
            "pool_type": "local_validated",
            "pool_size": len(ordered),
            "pool_min_target": cfg.candidate_pool_min,
            "pool_max_target": cfg.candidate_pool_max,
            "source": source,
        }
        # Idempotent: the pool key is deterministic from (calc date, symbol set);
        # reuse an existing pool instead of colliding on UNIQUE(source, version).
        existing = await self._service.get_candidate_pool_by_version(pool_version, source)
        if existing is not None:
            pool = existing
            if pool["status"] != "VALIDATED":
                pool = await self._service.validate_candidate_pool(
                    pool["id"], reason="Phase 4 local-validated pool (reused)"
                )
            meta_symbols = self._pool_symbols(pool)
            if not meta_symbols:
                raise ValueError(
                    f"existing candidate pool {pool['id']} has no persisted "
                    "symbol list (metadata_json['symbols'])"
                )
            return pool, meta_symbols
        pool = await self._service.create_candidate_pool(
            pool_version,
            source=source,
            source_reference=source_reference,
            symbols=ordered,
            metadata=meta,
            reason="Phase 4 local validated candidate input",
        )
        pool = await self._service.validate_candidate_pool(
            pool["id"], reason="Phase 4 local-validated pool"
        )
        return pool, ordered

    @staticmethod
    def _pool_symbols(pool: dict) -> list:
        try:
            meta = json.loads(pool.get("metadata_json") or "{}")
        except (ValueError, TypeError):
            return []
        raw = meta.get("symbols") or []
        return [elig.normalize_symbol(s) for s in raw if s]

    @staticmethod
    def _dedupe_symbols(symbols: list[str]) -> list[str]:
        seen: set = set()
        out: list = []
        for s in symbols:
            n = elig.normalize_symbol(s)
            if n and n not in seen:
                seen.add(n)
                out.append(n)
        return out

    # ------------------------------------------------------ previous cycle
    async def _load_previous_cycle(self) -> dict:
        versions = await self._service.list_universe_versions(limit=50)
        for v in versions:
            if v["status"] == UniverseStatus.REJECTED:
                continue
            detail = await self._service.get_universe_version_detail(v["id"])
            if not detail:
                continue
            core: dict = {}
            rotation: dict = {}
            for m in detail.get("members", []) or []:
                entry = {"rank": m["rank"], "score": m["rotation_score"], "category": m["category"]}
                if m["category"] == UniverseCategory.CORE:
                    core[m["symbol"]] = entry
                elif m["category"] == UniverseCategory.ROTATION:
                    rotation[m["symbol"]] = entry
            return {
                "present": True,
                "version_id": v["id"],
                "version": v["version"],
                "status": v["status"],
                "core": core,
                "rotation": rotation,
            }
        return {
            "present": False,
            "version_id": None,
            "version": None,
            "status": None,
            "core": {},
            "rotation": {},
        }

    # -------------------------------------------------------------- pipeline
    async def _evaluate_all(
        self,
        symbols: list[str],
        expected_dates: list,
        window_days: int,
        cfg: RotationEngineConfig,
    ) -> dict[str, elig.EvaluationResult]:
        evaluations: dict[str, elig.EvaluationResult] = {}
        for sym in symbols:  # sorted order (pool preserves caller order — engine
            # sorts for determinism below)
            sig: dict = {}
            try:
                df = await self.provider.get_ohlcv(sym, timeframe="5m", days=window_days)
                if df is None:
                    df = pd.DataFrame()
                sig = replay.replay_window(df, expected_dates, cfg, market_context=None)
                evaluations[sym] = elig.evaluate_candidate(
                    sym, df, expected_dates, cfg, signal_metrics=sig
                )
            except Exception as exc:  # provider error → per-symbol failure
                evaluations[sym] = elig.provider_failure_evaluation(sym, _err_msg(exc))
        return evaluations

    @staticmethod
    def _stage_counts(evaluations: dict[str, elig.EvaluationResult]) -> dict:
        names = ["tradability", "liquidity", "volume", "volatility", "data_quality", "signal_quality"]
        counts = {n: 0 for n in names}
        exclusions: dict = {}
        insufficient = 0
        provider_failed = 0
        for sym, ev in evaluations.items():
            for n in names:
                if ev.stages[n].status == FilterResult.PASS:
                    counts[n] += 1
            if ev.fetch_failed or ev.data_quality_state == elig.PROVIDER_FAILURE:
                provider_failed += 1
            if ev.eligibility_status == EligibilityResultStatus.INSUFFICIENT_DATA:
                insufficient += 1
            if ev.eligibility_status != EligibilityResultStatus.ELIGIBLE:
                exclusions.setdefault(ev.eligibility_status, 0)
                exclusions[ev.eligibility_status] += 1
        counts["provider_failure"] = provider_failed
        counts["insufficient_data"] = insufficient
        counts["excluded"] = dict(exclusions)
        return counts

    # ------------------------------------------------------------- selection
    def _build_candidates(
        self,
        evaluations: dict[str, elig.EvaluationResult],
        surv_scores: dict[str, dict],
    ) -> list[selection.RotationCandidate]:
        out = []
        for sym in sorted(surv_scores):
            ev = evaluations[sym]
            comps = surv_scores[sym]
            out.append(
                selection.RotationCandidate(
                    symbol=sym,
                    rotation_score=comps["total_rotation_score"],
                    data_quality_score=ev.data_quality_score or 0.0,
                    median_daily_value=ev.median_daily_value or 0.0,
                    volume_cv=ev.volume_cv,
                    median_atr_pct=ev.median_atr_pct,
                    trading_consistency=ev.trading_consistency,
                    signal_reliability=ev.signal_reliability,
                    expected_sessions=ev.expected_sessions,
                    present_sessions=ev.present_sessions,
                )
            )
        return out

    @staticmethod
    def _eligibility_slot(ev: elig.EvaluationResult) -> dict:
        st = ev.stages
        return {
            "eligibility_status": ev.eligibility_status,
            "tradability": st["tradability"].status,
            "liquidity": st["liquidity"].status,
            "volume": st["volume"].status,
            "volatility": st["volatility"].status,
            "data_quality": st["data_quality"].status,
            "signal_quality": st["signal_quality"].status,
            "failure_reason": ev.failure_reason,
            "data_quality_state": ev.data_quality_state,
        }

    def _build_payloads(
        self,
        evaluations: dict[str, elig.EvaluationResult],
        core: list,
        rotation: list,
        components: dict[str, dict],
    ) -> tuple[list, dict, dict]:
        members: list[dict[str, Any]] = []
        slot_scores: dict = {}
        for cs in core:
            comps = components[cs.symbol]
            members.append(
                {
                    "symbol": cs.symbol,
                    "category": UniverseCategory.CORE,
                    "rank": cs.rank,
                    "rotation_score": comps["total_rotation_score"],
                    "selection_reason": cs.selection_reason,
                    "previous_category": (
                        UniverseCategory.CORE if cs.previous_rank is not None else None
                    ),
                    "previous_rank": cs.previous_rank,
                    "previous_score": cs.previous_score,
                    "retention_reason": cs.retention_reason,
                    "replacement_reason": None,
                }
            )
            slot_scores[cs.symbol] = comps
        for rs in rotation:
            comps = components[rs.symbol]
            members.append(
                {
                    "symbol": rs.symbol,
                    "category": UniverseCategory.ROTATION,
                    "rank": rs.rank,
                    "rotation_score": comps["total_rotation_score"],
                    "selection_reason": rs.selection_reason,
                    "previous_category": (
                        UniverseCategory.ROTATION if rs.previous_rank is not None else None
                    ),
                    "previous_rank": rs.previous_rank,
                    "previous_score": rs.previous_score,
                    "retention_reason": rs.retention_reason,
                    "replacement_reason": None,
                }
            )
            slot_scores[rs.symbol] = comps
        slot_eligibility = {
            sym: self._eligibility_slot(ev) for sym, ev in sorted(evaluations.items())
        }
        return members, slot_scores, slot_eligibility

    # ------------------------------------------------------------ main entry
    async def generate_draft(
        self,
        *,
        symbols: Optional[list[str]] = None,
        pool_id: Optional[str] = None,
        source: str = "local",
        source_reference: Optional[str] = None,
        version: Optional[str] = None,
        effective_from=None,
        calculated_at=None,
        actor: str = "SYSTEM",
        reason: Optional[str] = None,
    ) -> dict:
        """Generate + persist a DRAFT universe (30 Core + 50 Rotation).

        Returns ``{"status": "DRAFT_CREATED", draft, counts, ...}`` or
        ``{"status": "INSUFFICIENT_CANDIDATES", counts, insufficiency, ...}``
        (nothing persisted in the latter case — the engine never fabricates
        candidates or fills a structurally-invalid draft).
        """
        cfg = self._config
        calc_dt = calculated_at or datetime.utcnow()
        if calc_dt.tzinfo is not None:  # keep Phase 3 naive-UTC convention
            calc_dt = calc_dt.replace(tzinfo=None)
        eff_dt = _normalize_effective_from(effective_from, calc_dt)

        pool, pool_symbols = await self._resolve_pool(
            pool_id=pool_id,
            symbols=symbols,
            source=source,
            source_reference=source_reference,
            calc_dt=calc_dt,
        )
        symbols = self._dedupe_symbols(pool_symbols)
        symbols.sort()

        window_end = calc_dt.date()
        window_start = window_end - timedelta(days=cfg.window_weeks * 7 - 1)
        expected_dates = elig.confirmed_trading_days(window_start, window_end)
        window_days = (window_end - window_start).days + 1

        base = {
            "engine_version": ENGINE_VERSION,
            "window": {
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
                "expected_sessions": len(expected_dates),
            },
            "config_hash": compute_config_hash(cfg),
        }

        evaluations = await self._evaluate_all(symbols, expected_dates, window_days, cfg)
        stage_counts = self._stage_counts(evaluations)

        survivors = {
            sym: ev for sym, ev in evaluations.items() if ev.eligibility_status == "ELIGIBLE"
        }

        # component scores over the SURVIVOR population (explicit reference pool)
        components: dict[str, dict] = {}
        if survivors:
            liq = scoring.liquidity_scores(
                {s: ev.median_daily_value for s, ev in survivors.items()}
            )
            vol_q = scoring.volume_quality_scores(
                {s: ev.mean_volume for s, ev in survivors.items()},
                {s: ev.volume_cv for s, ev in survivors.items()},
            )
            volty = scoring.volatility_scores(
                {s: ev.median_atr_pct for s, ev in survivors.items()}, cfg
            )
            freq = scoring.signal_frequency_scores(
                {s: ev.frequency for s, ev in survivors.items()}
            )
            long_q = scoring.long_quality_scores(
                {s: ev.long_raw for s, ev in survivors.items()},
                {s: ev.long_sample_ok for s, ev in survivors.items()},
            )
            short_q = scoring.short_quality_scores(
                {s: ev.short_raw for s, ev in survivors.items()},
                {s: ev.short_sample_ok for s, ev in survivors.items()},
            )
            dq = scoring.data_quality_scores(
                {s: ev.data_quality_score for s, ev in survivors.items()}
            )
            for s in survivors:
                components[s] = {
                    "liquidity_score": liq.get(s, 0.0),
                    "volume_quality_score": vol_q.get(s, 0.0),
                    "volatility_score": volty.get(s, 0.0),
                    "signal_frequency_score": freq.get(s, 0.0),
                    "long_quality_score": long_q.get(s, 0.0),
                    "short_quality_score": short_q.get(s, 0.0),
                    "data_quality_score": dq.get(s, 0.0),
                }
                components[s]["total_rotation_score"] = scoring.rotation_score(components[s])

        counts = {
            "candidates": len(symbols),
            "tradable": stage_counts["tradability"],
            "liquidity_eligible": stage_counts["liquidity"],
            "volume_eligible": stage_counts["volume"],
            "volatility_eligible": stage_counts["volatility"],
            "data_quality_eligible": stage_counts["data_quality"],
            "signal_sample_eligible": stage_counts["signal_quality"],
            "eligible": len(survivors),
            "long_quality_eligible": sum(
                1 for ev in survivors.values() if ev.long_sample_ok
            ),
            "short_quality_eligible": sum(
                1 for ev in survivors.values() if ev.short_sample_ok
            ),
            "excluded": stage_counts["excluded"].get("INELIGIBLE", 0),
            "insufficient_data": stage_counts["insufficient_data"],
            "provider_failure": stage_counts["provider_failure"],
            "insufficient_sample_gated": sum(
                1 for ev in survivors.values() if not (ev.long_sample_ok and ev.short_sample_ok)
            ),
        }

        rejection_reasons = sorted(
            (
                {
                    "symbol": sym,
                    "stage": next(
                        (name for name, st in ev.stages.items() if st.status == FilterResult.FAIL),
                        ev.eligibility_status,
                    ),
                    "reason": ev.failure_reason,
                }
                for sym, ev in evaluations.items()
                if ev.eligibility_status != "ELIGIBLE"
            ),
            key=lambda r: (r["symbol"], r["stage"]),
        ) or None

        if len(survivors) < cfg.core_size + cfg.rotation_size:
            return {
                "status": "INSUFFICIENT_CANDIDATES",
                "pool": pool,
                "draft": None,
                "counts": counts,
                "insufficiency": {
                    "required_core": cfg.core_size,
                    "required_rotation": cfg.rotation_size,
                    "required_total": cfg.core_size + cfg.rotation_size,
                    "eligible_candidates": len(survivors),
                    "missing_total": max(
                        0, cfg.core_size + cfg.rotation_size - len(survivors)
                    ),
                    "pool_size": len(symbols),
                    "message": (
                        "Fewer than 30+50 eligible candidates — no draft "
                        "persisted (candidates are never fabricated)."
                    ),
                },
                "rejection_reasons": rejection_reasons,
                **base,
            }

        previous = await self._load_previous_cycle()
        candidates = self._build_candidates(evaluations, components)

        core, core_result = selection.select_core(
            candidates, cfg, previous_core=previous["core"]
        )
        core_symbols = {c.symbol for c in core}
        rotation_candidates = [c for c in candidates if c.symbol not in core_symbols]
        # Symbols that left Rotation because they were PROMOTED to Core are NOT
        # churn: exclude them from the anti-churn ladder's previous-rotation set
        # and record an explicit explainable PROMOTED_TO_CORE decision.
        prev_rotation_for_churn = {
            s: d for s, d in previous["rotation"].items() if s not in core_symbols
        }
        rotation, rotation_result = selection.select_rotation(
            rotation_candidates,
            cfg,
            previous_rotation=prev_rotation_for_churn,
        )
        for s in sorted(s for s in previous["rotation"] if s in core_symbols):
            d = previous["rotation"][s]
            rotation_result.churn_decisions.append(
                {
                    "symbol": s,
                    "action": "PROMOTED_TO_CORE",
                    "previous_rank": (d or {}).get("rank"),
                    "previous_score": (d or {}).get("score"),
                    "reason": (
                        f"promoted from rotation rank {(d or {}).get('rank')} "
                        "to Core (stability selection)"
                    ),
                }
            )

        if core_result.core_shortfall > 0 or rotation_result.rotation_shortfall > 0:
            return {
                "status": "INSUFFICIENT_CANDIDATES",
                "pool": pool,
                "draft": None,
                "counts": counts,
                "insufficiency": {
                    "required_core": cfg.core_size,
                    "required_rotation": cfg.rotation_size,
                    "required_total": cfg.core_size + cfg.rotation_size,
                    "eligible_candidates": len(survivors),
                    "core_selected": len(core),
                    "rotation_selected": len(rotation),
                    "core_shortfall": core_result.core_shortfall,
                    "rotation_shortfall": rotation_result.rotation_shortfall,
                    "missing_total": (
                        core_result.core_shortfall + rotation_result.rotation_shortfall
                    ),
                    "message": (
                        "Structural rules (30+50=80) not satisfiable — no draft "
                        "persisted (candidates are never fabricated)."
                    ),
                },
                "rejection_reasons": rejection_reasons,
                **base,
            }

        members, slot_scores, slot_eligibility = self._build_payloads(
            evaluations, core, rotation, components
        )
        draft_version = version or f"draft-{eff_dt:%Y%m%d}-{base['config_hash'][:6]}"
        draft = await self._service.create_universe_draft(
            pool["id"],
            version=draft_version,
            members=members,
            core_count=sum(1 for m in members if m["category"] == "CORE"),
            rotation_count=sum(1 for m in members if m["category"] == "ROTATION"),
            total_count=len(members),
            calculated_at=calc_dt,
            effective_from=eff_dt,
            config_hash=base["config_hash"],
            slot_scores=slot_scores,
            slot_eligibility=slot_eligibility,
            actor=actor,
            reason=reason
            or "Phase 4 shadow draft — DRAFT only, never auto-approved nor auto-activated",
        )

        return {
            "status": "DRAFT_CREATED",
            "pool": pool,
            "draft": draft,
            "counts": counts,
            "insufficiency": None,
            "rejection_reasons": rejection_reasons,
            "previous_cycle": {
                "present": previous["present"],
                "version": previous["version"],
                "status": previous["status"],
                "core_count": len(previous["core"]),
                "rotation_count": len(previous["rotation"]),
            },
            "selection": {
                "core": [cs.symbol for cs in core],
                "rotation": [rs.symbol for rs in rotation],
                "churn_decisions": rotation_result.churn_decisions,
                "core_rejections": core_result.core_rejections,
            },
            **base,
        }

    # ------------------------------------------------------ explicit helpers
    def expected_session_dates(self, calculated_at=None) -> list:
        """Expose the deterministic window the engine derives from a timestamp —
        used by providers/fixtures to generate matching sessions."""
        cfg = self._config
        calc_dt = calculated_at or datetime.utcnow()
        if calc_dt.tzinfo is not None:
            calc_dt = calc_dt.replace(tzinfo=None)
        window_end = calc_dt.date()
        window_start = window_end - timedelta(days=cfg.window_weeks * 7 - 1)
        return elig.confirmed_trading_days(window_start, window_end)