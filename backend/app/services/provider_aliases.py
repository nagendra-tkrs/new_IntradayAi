"""Provider symbol aliases (Phase 5B) — canonical → provider mapping.

Phase 5A found Yahoo/NSE spelling incompatibilities that are a SYMBOL-PROVIDER
compatibility problem, not proof that a security is absent (e.g. ``TATAMOTORS.NS``
404s while the demerged entity serves as ``TMCV.NS``; ``COLPALPH.NS`` 404s while
the canonical NSE symbol ``COLPAL.NS`` serves data).

Design (isolated from scanner logic, Phase 5B §13):

    canonical NSE symbol  →  provider symbol mapping  →  Yahoo symbol

* Every entry is EXPLICIT and versioned — never a growing collection of ad-hoc
  replacements inside scanner/eligibility code.
* Canonical identity is NEVER rewritten: the alias resolves the symbol sent to
  the provider only; pool/database/attribution keep the canonical symbol.
* Only VERIFIED mappings are listed (live-probed against the provider; the
  "verified on" date records the probe run).

Verified mappings (Phase 5A probe + Phase 5B re-probe, 2026-09-23):

    canonical        provider          evidence
    BAJAJFINANCE  →  BAJFINANCE    - Yahoo 404s BAJAJFINANCE.NS but returns
                                    365 5m bars for BAJFINANCE.NS
    COLPALPH      →  COLPAL        - official NSE list uses COLPAL; Yahoo 404s
                                    COLPALPH.NS but returns 363 bars for COLPAL.NS

NOT aliases (identity-changing — deliberately absent):
    TATAMOTORS → TMCV  — Tata Motors demerged (2025); TMCV is a DIFFERENT
                         listed identity. Aliasing would silently rewrite the
                         canonical security. TATAMOTORS stays canonical and is
                         honestly handled by the pipeline's NO_DATA path.
    MINDTREE     —     delisted after the LTIMindtree merger; no valid alias.
"""
from __future__ import annotations

# Version string for the alias map — bumped ONLY when verified mappings change.
PROVIDER_ALIAS_VERSION = "phase5b.v1"

# Verified canonical NSE symbol → provider-served symbol (base form, no ".NS").
PROVIDER_SYMBOL_ALIASES: dict[str, str] = {
    "BAJAJFINANCE": "BAJFINANCE",
    "COLPALPH": "COLPAL",
}

# Independent live-probe evidence recorded when Phase 5B verified the map.
PROVIDER_ALIAS_EVIDENCE = {
    "verified_on": "2026-09-23",
    "probe": {
        "BAJFINANCE": 365,   # rows served for the provider symbol
        "COLPAL": 363,
        "BAJAJFINANCE": 0,   # rows served for the canonical variant
        "COLPALPH": 0,
        "TATAMOTORS": 0,     # 404 — demerger, NOT aliased (identity change)
        "TMCV": 374,         # serves data (separate entity — not an alias)
        "MINDTREE": 0,       # delisted — no alias
    },
}


def provider_symbol(canonical: str) -> str:
    """Resolve a canonical base symbol to the provider-served base symbol.

    Defaults to the input unchanged (pass-through) — aliases only ever REMAP
    verified incompatibilities; the canonical identity is never modified.
    """
    if not canonical:
        return canonical
    return PROVIDER_SYMBOL_ALIASES.get(canonical.strip().upper(), canonical.strip().upper())