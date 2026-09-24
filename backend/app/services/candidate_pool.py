"""Phase 5B — Candidate-pool source resolution (auditable & deterministic).

Problem (Phase 5A ``CANDIDATE_POOL_SOURCE_GAP``): the repository's only real
candidate source (``NSE_UNIVERSES``) yields 64 unique symbols — below the
configured 150–250 candidate-pool target, making an 80-stock universe
unreachable for a fair evaluation.

Phase 5B adds a second, REAL, authoritative source — the official NSE index
constituent archive (``nsearchives.nseindia.com`` ``ind_nifty{n}list.csv``) —
and resolves the candidate pool from ANY ordered list of sources through one
deterministic pipeline:

    gather → normalize → validate → dedupe(attribution merged) → ordered
    → target check → version

Guarantees:

* No fabricated symbols, no duplicate padding, no silent ordering.
* Canonical normalization: ``BAJAJFINANCE`` / ``BAJAJFINANCE.NS`` /
  ``NSE:BAJAJFINANCE`` resolve to the same canonical symbol.
* Every final symbol keeps source attribution (``Why is this here?``).
* Refresh failures surface as structured records and NEVER empty or overwrite
  the previously validated pool (retain-on-failure is inherent: this module
  only ever appends; nothing is deleted).
* Pool versions are deterministic: identical (as-of date, symbol set, source
  versions) → identical ``pool-YYYYMMDD-<hash8>`` version, matching the Phase 4
  engine ``_resolve_pool`` convention so re-runs against the same snapshot do
  not mint new versions.

The rotation engine stays decoupled: it consumes a normalized pool (by pool_id
or symbol list) through the existing Phase 3/4 API — ``create_pool_from_resolution``
is the ONE bridge that persists a resolution into a Phase 3 candidate pool with
full attribution metadata.
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from app.core.config import settings
from app.services import rotation_eligibility as elig
from app.services.market_data.yfinance_provider import NSE_UNIVERSES

# Persistent source keys (ordered, ranked — used for attribution and ordering).
SOURCE_REPO = "NSE_UNIVERSES"        # in-repository universe data (Phase 3)
SOURCE_NIFTY200 = "NIFTY200"         # official NSE index constituent archive
SOURCE_NIFTY500 = "NIFTY500"         # official NSE archive (opt-in, > max)
SOURCE_FILE = "CONFIGURED_FILE"      # configured CSV/JSON file provider

NSE_ARCHIVE_BASE_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty{n}list.csv"
NSE_ARCHIVE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
NSE_SERIES_EQUITY = "EQ"


@dataclass(frozen=True)
class CandidateSource:
    """One ordered, ranked candidate source. ``rank`` is the tie-break for
    attribution ordering; lower rank = higher priority (design target order)."""

    key: str
    label: str
    kind: str  # "repository" | "external" | "file"
    rank: int
    version: str


@dataclass(frozen=True)
class SourceFetch:
    """Deterministic fetch result: normalized-once base symbols + a version
    string identifying WHICH revision of the source produced them."""

    symbols: tuple[str, ...]
    version: str


@dataclass
class CandidatePoolResolution:
    version: str
    pool_hash: str
    symbols: list[str]                # final ordered canonical list (unique)
    attribution: dict[str, list[str]]  # canonical -> sorted source keys
    source_versions: dict[str, str]   # source key -> version string
    stats: dict                       # @see resolve_candidate_pool
    failures: list[dict] = field(default_factory=list)
    target_status: str = "PASS"       # PASS | INSUFFICIENT_SOURCE | OVER_TARGET_POLICY
    target: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# normalization (one canonical path; reuse Phase 4 base rule)
# ---------------------------------------------------------------------------
def normalize_candidate_symbol(symbol: str) -> str:
    """Single canonical normalization path for candidate symbols.

    ``BAJAJFINANCE``, ``BAJAJFINANCE.NS`` and ``NSE:BAJAJFINANCE`` (also
    ``NS:``) all resolve to ``BAJAJFINANCE`` — deterministic, case-insensitive.
    """
    s = (symbol or "").strip().upper()
    for prefix in ("NSE:", "NS:"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
            break
    if s.endswith(".NS"):
        s = s[: -len(".NS")].strip()
    return s


def is_candidate_valid(symbol: str) -> bool:
    return bool(elig.NSE_SYMBOL_PATTERN.match(symbol or ""))


# ---------------------------------------------------------------------------
# deterministic hashing / versioning (mirrors Phase 4 engine _resolve_pool)
# ---------------------------------------------------------------------------
def compute_pool_hash(symbols: list[str]) -> str:
    """Stable hash of the canonical symbol set (sorted join — like the engine)."""
    return hashlib.sha1(",".join(sorted(symbols)).encode("utf-8")).hexdigest()


def pool_version(calc_dt: datetime, symbols: list[str]) -> str:
    """``pool-YYYYMMDD-<sha1[:8]>`` — identical convention to Phase 4, so two
    runs against the same snapshot+symbol set produce the SAME version."""
    return "pool-" + f"{calc_dt:%Y%m%d}-" + compute_pool_hash(symbols)[:8]


# ---------------------------------------------------------------------------
# source providers (real, deterministic)
# ---------------------------------------------------------------------------
def repository_universe_provider(version: str = "phase3-static") -> CandidateSource:
    """In-repository universe data (NSE_UNIVERSES: NIFTY50 + NIFTY100 +
    BANKNIFTY). Deterministic — the lists are baked into the repo."""
    return CandidateSource(
        key=SOURCE_REPO,
        label="NSE_UNIVERSES (NIFTY50 + NIFTY100 + BANKNIFTY)",
        kind="repository",
        rank=0,
        version=version,
    )


def fetch_repository_universe_source() -> SourceFetch:
    raw: list[str] = []
    for name in ("NIFTY50", "NIFTY100", "BANKNIFTY"):
        raw.extend(NSE_UNIVERSES.get(name, []))
    # deterministic within-source order; dedupe kept downstream (cross-source)
    symbols = tuple(sorted({normalize_candidate_symbol(s) for s in raw if s}))
    return SourceFetch(symbols=symbols, version="phase3-static")


def nse_archive_source(index: str = "nifty200", version: Optional[str] = None) -> CandidateSource:
    """Official NSE index-constituent archive (external, authoritative)."""
    label = {
        "nifty200": "NIFTY200 (official NSE index constituent archive)",
        "nifty500": "NIFTY500 (official NSE index constituent archive)",
    }.get(index, f"NIFTY {index} (official NSE archive)")
    key = SOURCE_NIFTY200 if index == "nifty200" else (
        SOURCE_NIFTY500 if index == "nifty500" else f"ARCHIVE:{index}"
    )
    return CandidateSource(
        key=key,
        label=label,
        kind="external",
        rank=1 if index == "nifty200" else 2,
        version=version or f"nse-archive-{index}",
    )


def _parse_nse_archive_csv(text: str) -> tuple[list[str], int]:
    """Parse an ``ind_nifty{n}list.csv`` file → (EQ symbols, non-EQ excluded)."""
    reader = csv.DictReader(io.StringIO(text))
    eq: list[str] = []
    excluded = 0
    for row in reader:
        series = (row.get("Series") or "").strip().upper()
        symbol = (row.get("Symbol") or "").strip()
        if not symbol:
            continue
        if series != NSE_SERIES_EQUITY:
            excluded += 1
            continue
        eq.append(symbol)
    return eq, excluded


def _index_number(index: str) -> str:
    """'nifty200' / 'NIFTY 200' / '200' → '200' (for the archive filename)."""
    return index.lower().replace("nifty", "").replace(" ", "")


def fetch_nse_archive_source(
    index: str = "nifty200",
    fetcher: Optional[Callable[[], str]] = None,
    version: Optional[str] = None,
) -> SourceFetch:
    """Fetch + parse the official NSE constituent CSV.

    ``fetcher`` may be injected (tests / cached offline snapshot); the default
    hits ``nsearchives.nseindia.com`` with a browser UA. Raises on network /
    parse failure — the RESOLVER catches and records a refresh failure (never
    fabricates, never empties the pool).
    """
    if fetcher is None:
        fetcher = lambda: _http_get_text(
            NSE_ARCHIVE_BASE_URL.format(n=_index_number(index))
        )
    text = fetcher()
    eq, excluded = _parse_nse_archive_csv(text)
    date_tag = version or "nse-archive-" + f"{datetime.utcnow():%Y%m%d}"
    return SourceFetch(
        symbols=tuple(sorted({normalize_candidate_symbol(s) for s in eq if s})),
        version=date_tag,
    )


def _http_get_text(url: str, timeout: int = 25) -> str:
    import requests

    resp = requests.get(url, headers={"User-Agent": NSE_ARCHIVE_UA}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def csv_file_source(version: str, *, key: str = SOURCE_FILE, rank: int = 3) -> CandidateSource:
    """Configured-file provider (Phase 5B §5.B): a deterministic CSV file whose
    rows follow the NSE archive schema (Symbol, Series)."""
    return CandidateSource(
        key=key,
        label=f"configured candidate file ({key})",
        kind="file",
        rank=rank,
        version=version,
    )


def fetch_csv_file_source(
    text: str,
    *,
    source: CandidateSource,
    series: str = "EQ",
) -> SourceFetch:
    symbols, excluded = _parse_nse_archive_csv(text)
    if series and series.upper() != NSE_SERIES_EQUITY:
        raise ValueError("csv_file_source: only Series EQ rows are equities")
    return SourceFetch(
        symbols=tuple(sorted({normalize_candidate_symbol(s) for s in symbols if s})),
        version=source.version,
    )


# ---------------------------------------------------------------------------
# resolver (pure — deterministic given identical inputs)
# ---------------------------------------------------------------------------
def resolve_candidate_pool(
    sources: list[CandidateSource],
    fetchers: dict[str, Callable[[], SourceFetch]],
    *,
    calc_dt: Optional[datetime] = None,
    pool_min: Optional[int] = None,
    pool_target: Optional[int] = None,
    pool_max: Optional[int] = None,
) -> CandidatePoolResolution:
    """Aggregate → normalize → validate → dedupe → order → target-check.

    Raises nothing: per-source failures are captured in ``failures`` and the
    remaining sources still produce a deterministic pool. If ALL sources fail,
    ``symbols`` is empty and ``target_status`` is ``INSUFFICIENT_SOURCE`` —
    never a fabricated pool.
    """
    calc_dt = calc_dt or datetime.utcnow()
    if calc_dt.tzinfo is not None:
        calc_dt = calc_dt.replace(tzinfo=None)
    pmin = pool_min if pool_min is not None else settings.CANDIDATE_POOL_MIN
    ptarget = pool_target if pool_target is not None else settings.CANDIDATE_POOL_TARGET
    pmax = pool_max if pool_max is not None else settings.CANDIDATE_POOL_MAX

    seen: dict[str, dict] = {}  # canonical -> {"sources": list[str], "raw": int}
    per_source_stats: dict[str, dict] = {}
    source_versions: dict[str, str] = {}
    failures: list[dict] = []
    raw_count = 0
    duplicate_count = 0
    excluded_count = 0

    for src in sources:
        pstat = {"raw": 0, "unique": 0, "invalid": 0}
        per_source_stats[src.key] = pstat
        try:
            if src.key not in fetchers:
                raise ValueError(f"no fetcher registered for source key {src.key}")
            fetch = fetchers[src.key]()
        except Exception as exc:  # refresh/fetch failure → record, continue
            failures.append(
                {
                    "source": src.key,
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "timestamp": datetime.utcnow().isoformat(),
                }
            )
            continue
        source_versions[src.key] = fetch.version
        in_source: set = set()
        for raw in fetch.symbols:
            raw_count += 1
            pstat["raw"] += 1
            canon = normalize_candidate_symbol(raw)
            if not canon or not is_candidate_valid(canon):
                excluded_count += 1
                pstat["invalid"] += 1
                continue
            if canon in in_source:  # intra-source duplicate
                duplicate_count += 1
                continue
            in_source.add(canon)
            pstat["unique"] += 1
            if canon in seen:
                duplicate_count += 1  # cross-source duplicate (expected overlap)
                if src.key not in seen[canon]["sources"]:
                    seen[canon]["sources"].append(src.key)
            else:
                seen[canon] = {"sources": [src.key]}

    # ----------------------------------------------------------- ordering
    # "source priority → source rank → canonical symbol": sort each symbol's
    # source provenance by (rank, key) and order the pool by min-source-rank
    # then canonical symbol — fully deterministic (no set/network ordering).
    rank_of = {s.key: s.rank for s in sources}
    attributed: dict[str, list[str]] = {}
    for canon, info in seen.items():
        srcs = sorted(info["sources"], key=lambda k: (rank_of.get(k, 99), k))
        attributed[canon] = srcs
    ordered = sorted(seen.keys(), key=lambda s: (rank_of.get(attributed[s][0], 99), s))

    final_count = len(ordered)
    if final_count < pmin:
        target_status = "INSUFFICIENT_SOURCE"
    elif final_count > pmax:
        target_status = "OVER_TARGET_POLICY"  # reported, never silently truncated
    else:
        target_status = "PASS"

    pool_hash = compute_pool_hash(ordered)
    ver = pool_version(calc_dt, ordered)

    stats = {
        "raw_count": raw_count,
        "normalized_count": final_count,
        "duplicate_count": duplicate_count,
        "invalid_count": excluded_count,
        "excluded_count": excluded_count,
        "final_count": final_count,
        "per_source": per_source_stats,
        "source_versions": dict(source_versions),
    }

    return CandidatePoolResolution(
        version=ver,
        pool_hash=pool_hash,
        symbols=ordered,
        attribution=attributed,
        source_versions=source_versions,
        stats=stats,
        failures=failures,
        target_status=target_status,
        target={
            "min": pmin,
            "target": ptarget,
            "max": pmax,
            "status": target_status,
            "final_count": final_count,
        },
    )


async def create_pool_from_resolution(
    resolution: CandidatePoolResolution,
    state_service,
    *,
    source: str = "phase5b-resolution",
    actor: str = "SYSTEM",
    reason: Optional[str] = None,
) -> dict:
    """Persist a resolution as a Phase 3 candidate pool (DRAFT → VALIDATED)
    with full attribution metadata, reusing the existing state-service API.

    The engine is NOT tied to anything new: it consumes this pool by ``pool_id``
    exactly like any other Phase 3/4 pool (``metadata_json['symbols']``).
    """
    metadata = {
        "symbols": list(resolution.symbols),
        "pool_type": "phase5b_resolved",
        "pool_size": len(resolution.symbols),
        "pool_hash": resolution.pool_hash,
        "target": resolution.target,
        "target_status": resolution.target_status,
        "source_attribution": {
            s: {"sources": sorted(srcs)} for s, srcs in sorted(resolution.attribution.items())
        },
        "source_versions": dict(resolution.source_versions),
        "failures": list(resolution.failures),
    }
    pool = await state_service.create_candidate_pool(
        resolution.version,
        source=source,
        source_reference=f"pool_hash={resolution.pool_hash}",
        symbols=list(resolution.symbols),
        metadata=metadata,
        actor=actor,
        reason=reason or "Phase 5B resolved candidate pool",
    )
    return await state_service.validate_candidate_pool(
        pool["id"], actor=actor, reason="Phase 5B resolved candidate pool"
    )


# ---------------------------------------------------------------------------
# safe-refresh failure (Phase 5B §12) — records the failure event and leaves any
# previously validated pool untouched (there is NO delete/overwrite path here).
# ---------------------------------------------------------------------------
async def record_pool_refresh_failure(
    state_service,
    *,
    source: str,
    error: str,
    previous_pool_version: Optional[str] = None,
) -> dict:
    """Append a ``CANDIDATE_POOL_REFRESH_FAILED`` event via the Phase 3 model."""
    return await state_service.record_candidate_pool_refresh_failure(
        source=source,
        error=error,
        previous_pool_version=previous_pool_version,
    )