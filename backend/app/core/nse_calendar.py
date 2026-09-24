"""Authoritative NSE trading-holiday calendar (Phase 5C).

Single source of truth for *what is an NSE trading day*. The rotation /
eligibility engine never talks to the network: it consumes the installed
calendar through ``market_session.is_holiday`` /
``market_session.is_confirmed_trading_day`` / ``NSE_HOLIDAYS`` (the runtime
mirror). This module owns:

* parsing + validation of the official NSE holiday-master payload
  (``https://www.nseindia.com/api/holiday-master?type=trading``),
* segmentation (CM / capital-market equities only — BSE-only and derivatives-
  only rows are never mixed in),
* date normalization to ``datetime.date`` (canonical ``YYYY-MM-DD`` identity),
* deduplication on ``(date, segment)``,
* a deterministic calendar fingerprint (sha-1, independent of retrieval time),
* a simple JSON file cache holding the *last validated* calendar,
* refresh with fail-safe fallback:

      fetch succeeds -> validate -> normalize -> dedupe -> store/install
      fetch fails    -> keep last validated calendar        -> record failure
      no source at all -> explicit CalendarUnavailableError  (never "no
                          holidays", never "every weekday is a trading day")

The bundled ``OFFICIAL_HOLIDAY_SEED`` (2024–2026) is the repository's
validated baseline produced from the official source (2026 entries verified
against the live holiday-master API on 2026-09-23; 2024–2025 carried forward
as the repository's historical validated rows — the API publishes the current
year only). ``refresh_holiday_calendar()`` replaces the year(s) the API
returns while preserving carried-forward years, so coverage never shrinks.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# constants / data
# ---------------------------------------------------------------------------
HOLIDAY_MASTER_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
DEFAULT_SEGMENT = "CM"  # NSE cash-market (equities) segment
SCHEMA_VERSION = "nse-holiday-calendar.v1"
SEED_SOURCE = "NSE_HOLIDAY_MASTER_SEED"
LIVE_SOURCE = "NSE_HOLIDAY_MASTER"


class CalendarUnavailableError(RuntimeError):
    """Raised when the calendar cannot be validated AND no validated fallback
    exists — callers must treat the date as unconfirmed, never as open."""


# Bundled validated baseline: year -> ((YYYY-MM-DD, name), ...)
# 2026 = official NSE CM holiday calendar (fetched 2026-09-23); 2024/2025 =
# the repository's historical validated holidays (kept verbatim — the live API
# publishes only the current year and past years are no longer re-verifiable).
OFFICIAL_HOLIDAY_SEED: tuple[tuple[str, str], ...] = (
    # --- 2024 (historical, repo-validated) ---
    ("2024-01-26", "Republic Day"),
    ("2024-03-25", "Holi"),
    ("2024-03-29", "Good Friday"),
    ("2024-04-11", "Id-Ul-Fitr"),
    ("2024-04-17", "Shri Ram Navmi"),
    ("2024-04-21", "Shri Mahavir Jayanti"),
    ("2024-05-01", "Maharashtra Day"),
    ("2024-05-23", "Buddha Purnima"),
    ("2024-06-17", "Id-Ul-Adha"),
    ("2024-07-17", "Muharram"),
    ("2024-08-15", "Independence Day"),
    ("2024-10-02", "Mahatma Gandhi Jayanti"),
    ("2024-11-01", "Diwali Laxmi Pujan"),
    ("2024-11-15", "Prakash Gurpurab"),
    ("2024-12-25", "Christmas"),
    # --- 2025 (historical, repo-validated) ---
    ("2025-01-26", "Republic Day"),
    ("2025-02-26", "Maha Shivaratri"),
    ("2025-03-14", "Holi"),
    ("2025-03-31", "Id-Ul-Fitr"),
    ("2025-04-10", "Shri Mahavir Jayanti"),
    ("2025-04-14", "Dr. Ambedkar Jayanti"),
    ("2025-04-18", "Good Friday"),
    ("2025-05-01", "Maharashtra Day"),
    ("2025-05-27", "Buddha Purnima"),
    ("2025-06-07", "Id-Ul-Adha"),
    ("2025-07-06", "Muharram"),
    ("2025-08-15", "Independence Day"),
    ("2025-08-27", "Shri Krishna Janmashtami"),
    ("2025-10-02", "Mahatma Gandhi Jayanti"),
    ("2025-10-21", "Diwali Laxmi Pujan"),
    ("2025-11-05", "Prakash Gurpurab"),
    ("2025-12-25", "Christmas"),
    # --- 2026 (official NSE CM holiday master, verified 2026-09-23) ---
    ("2026-01-15", "Municipal Corporation Election - Maharashtra"),
    ("2026-01-26", "Republic Day"),
    ("2026-02-15", "Mahashivratri"),
    ("2026-03-03", "Holi"),
    ("2026-03-21", "Id-Ul-Fitr (Ramadan Eid)"),
    ("2026-03-26", "Shri Ram Navami"),
    ("2026-03-31", "Shri Mahavir Jayanti"),
    ("2026-04-03", "Good Friday"),
    ("2026-04-14", "Dr. Baba Saheb Ambedkar Jayanti"),
    ("2026-05-01", "Maharashtra Day"),
    ("2026-05-28", "Bakri Id"),
    ("2026-06-26", "Muharram"),
    ("2026-08-15", "Independence Day"),
    ("2026-09-14", "Ganesh Chaturthi"),
    ("2026-10-02", "Mahatma Gandhi Jayanti"),
    ("2026-10-20", "Dussehra"),
    ("2026-11-08", "Diwali Laxmi Pujan"),
    ("2026-11-10", "Diwali-Balipratipada"),
    ("2026-11-24", "Prakash Gurpurb Sri Guru Nanak Dev"),
    ("2026-12-25", "Christmas"),
)


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NseHoliday:
    """One normalized NSE holiday row."""

    date: date
    name: str
    segment: str = DEFAULT_SEGMENT
    source: str = LIVE_SOURCE


@dataclass(frozen=True)
class HolidayCalendar:
    """The validated, installed holiday calendar (immutable snapshot)."""

    holidays: tuple[NseHoliday, ...]
    source: str
    retrieved_at: str
    segment: str = DEFAULT_SEGMENT
    calendar_hash: str = ""
    years: tuple[int, ...] = ()
    holiday_count: int = 0

    def as_dict(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "source": self.source,
            "segment": self.segment,
            "retrieved_at": self.retrieved_at,
            "calendar_hash": self.calendar_hash,
            "years": list(self.years),
            "holiday_count": self.holiday_count,
            "holidays": [
                {"date": h.date.isoformat(), "name": h.name, "segment": h.segment}
                for h in sorted(self.holidays, key=lambda h: (h.date, h.name))
            ],
        }


@dataclass(frozen=True)
class RefreshResult:
    status: str  # "OK" | "FALLBACK"
    calendar: HolidayCalendar
    errors: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# parsing / validation / normalization
# ---------------------------------------------------------------------------
_DATE_FMT = "%d-%b-%Y"


def parse_master_date(raw: str) -> date:
    """Normalize NSE's ``14-Sep-2026`` to ``date(2026, 9, 14)``.

    Strict: the value must round-trip through the same format so
    non-canonical spellings (``14-Sept-2026``, ``14/09/2026`` …) are rejected
    rather than silently coarsened.
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty holiday date")
    try:
        d = datetime.strptime(s, _DATE_FMT).date()
    except ValueError as exc:  # noqa: PERF203
        raise ValueError(f"invalid holiday date {raw!r}") from exc
    if d.strftime(_DATE_FMT) != s:
        raise ValueError(
            f"non-canonical holiday date {raw!r} (must round-trip as {_DATE_FMT})"
        )
    return d


def parse_holiday_master(text: str, *, segment: str = DEFAULT_SEGMENT) -> list[NseHoliday]:
    """Parse the official NSE holiday-master payload.

    Validates every row (valid canonical date, non-empty name), keeps only the
    requested segment, and deduplicates on ``(date, segment)``. Raises
    ValueError on malformed payloads — a malformed source must never be
    silently accepted as "the calendar".
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"holiday-master payload is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("holiday-master payload is not an object (segment map)")
    rows = data.get(segment)
    if not isinstance(rows, list):
        raise ValueError(
            f"holiday-master payload has no {segment} segment list"
        )

    seen: set[tuple[date, str]] = set()
    out: list[NseHoliday] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("holiday-master row is not an object")
        d = parse_master_date(str(row.get("tradingDate") or ""))
        name = str(row.get("description") or row.get("holiday") or "").strip()
        name = " ".join(name.split())
        if not name:
            raise ValueError(f"holiday row {d.isoformat()} has an empty name")
        if len(name) > 200:
            raise ValueError(f"holiday row {d.isoformat()} name too long")
        key = (d, segment)
        if key in seen:  # duplicate (date, segment) — keep first, drop the rest
            continue
        seen.add(key)
        out.append(NseHoliday(date=d, name=name, segment=segment))
    if not out:
        raise ValueError(
            f"holiday-master {segment} segment produced zero valid holidays "
            "(unexpected for a live exchange calendar)"
        )
    out.sort(key=lambda h: (h.date, h.name))
    return out


# ---------------------------------------------------------------------------
# fingerprint / build / merge
# ---------------------------------------------------------------------------
def calendar_hash(holidays: Iterable[NseHoliday]) -> str:
    """Deterministic sha-1 over sorted ``YYYY-MM-DD|name|segment`` rows.

    Deliberately excludes the retrieval timestamp: identical validated holiday
    data always produces the same fingerprint regardless of when it was fetched.
    """
    lines = sorted(
        f"{h.date.isoformat()}|{h.name}|{h.segment}" for h in holidays
    )
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()


def build_calendar(
    holidays: Iterable[NseHoliday],
    *,
    source: str = LIVE_SOURCE,
    retrieved_at: Optional[str] = None,
    segment: str = DEFAULT_SEGMENT,
) -> HolidayCalendar:
    holidays = tuple(sorted(set(holidays), key=lambda h: (h.date, h.name)))
    years = tuple(sorted({h.date.year for h in holidays}))
    return HolidayCalendar(
        holidays=holidays,
        source=source,
        retrieved_at=retrieved_at or _utcnow_iso(),
        segment=segment,
        calendar_hash=calendar_hash(holidays),
        years=years,
        holiday_count=len(holidays),
    )


def seed_holidays() -> tuple[NseHoliday, ...]:
    """The bundled validated baseline (2024–2026) as normalized NseHolidays."""
    return tuple(
        NseHoliday(date=date.fromisoformat(iso), name=name, source=SEED_SOURCE)
        for iso, name in OFFICIAL_HOLIDAY_SEED
    )


def _merge_carried_years(
    fresh: Iterable[NseHoliday], existing: Iterable[NseHoliday]
) -> list[NseHoliday]:
    """Official source wins for the years it publishes; other years carried.

    The API publishes the current year only. Years absent from the fresh fetch
    (2024/2025) are preserved from the previously installed calendar so
    coverage never drops and the repository never silently loses a historical
    holiday.
    """
    fresh_by_year = {h.date.year for h in fresh}
    merged = {h: None for h in fresh}
    for h in existing:
        if h.date.year not in fresh_by_year:
            merged[h] = None
    return sorted(merged, key=lambda h: (h.date, h.name))


# ---------------------------------------------------------------------------
# installed-calendar state (single authority)
# ---------------------------------------------------------------------------
_INSTALLED: Optional[HolidayCalendar] = None


def install_calendar(cal: HolidayCalendar) -> HolidayCalendar:
    global _INSTALLED
    _INSTALLED = cal
    return cal


def installed_calendar() -> HolidayCalendar:
    if _INSTALLED is None:
        raise CalendarUnavailableError("no validated NSE holiday calendar installed")
    return _INSTALLED


def calendar_hash_installed() -> Optional[str]:
    return _INSTALLED.calendar_hash if _INSTALLED is not None else None


def calendar_metadata() -> Optional[dict]:
    if _INSTALLED is None:
        return None
    return {
        "source": _INSTALLED.source,
        "retrieved_at": _INSTALLED.retrieved_at,
        "segment": _INSTALLED.segment,
        "years": list(_INSTALLED.years),
        "holiday_count": _INSTALLED.holiday_count,
        "calendar_hash": _INSTALLED.calendar_hash,
    }


def holiday_dates() -> frozenset[date]:
    if _INSTALLED is None:
        return frozenset()
    return frozenset(h.date for h in _INSTALLED.holidays)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# simple JSON file cache (last validated calendar)
# ---------------------------------------------------------------------------
class HolidayCalendarStore:
    """Minimal atomic JSON persistence for the last validated calendar."""

    def load(self, path: Path) -> Optional[HolidayCalendar]:
        """Return the cached calendar when present and internally consistent
        (valid rows + fingerprint matches) — otherwise None (never crash)."""
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            if raw.get("schema") != SCHEMA_VERSION:
                raise ValueError("unexpected cache schema")
            rows = raw.get("holidays")
            if not isinstance(rows, list):
                raise ValueError("cache has no holidays list")
            holidays: list[NseHoliday] = []
            for row in rows:
                d = date.fromisoformat(str(row.get("date") or ""))
                name = str(row.get("name") or "").strip()
                segment = str(row.get("segment") or DEFAULT_SEGMENT)
                if not name:
                    raise ValueError("cache holiday without a name")
                holidays.append(NseHoliday(date=d, name=name, segment=segment))
            cal = HolidayCalendar(
                holidays=tuple(holidays),
                source=str(raw.get("source") or LIVE_SOURCE),
                retrieved_at=str(raw.get("retrieved_at") or ""),
                segment=str(raw.get("segment") or DEFAULT_SEGMENT),
                calendar_hash=str(raw.get("calendar_hash") or ""),
                years=tuple(int(y) for y in (raw.get("years") or [])),
                holiday_count=int(raw.get("holiday_count") or 0),
            )
            if calendar_hash(holidays) != cal.calendar_hash:
                raise ValueError("cache fingerprint mismatch (corrupt/tampered)")
            if not holidays:
                raise ValueError("cache is empty")
            return cal
        except (ValueError, TypeError) as exc:
            logger.warning("nse holiday cache ignored (%s)", exc)
            return None

    def save(self, path: Path, cal: HolidayCalendar) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=path.name, suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cal.as_dict(), fh, indent=2, sort_keys=True)
            os.replace(tmp, str(path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# refresh (fetch -> parse -> validate -> normalize -> dedupe -> store/install)
# ---------------------------------------------------------------------------
def _http_get_holiday_master(url: str = HOLIDAY_MASTER_URL, timeout: int = 25) -> str:
    """Official NSE transport — reuses the repository's browser-UA fetch helper
    (the same requests-based transport that already drives the NSE archive
    index fetch). Imported lazily to keep calendar queries network-free."""
    from app.services import candidate_pool as _candidate_pool  # noqa: PLC0415

    return _candidate_pool._http_get_text(url, timeout=timeout)


def refresh_holiday_calendar(
    *,
    http_get: Optional[Callable[[], str]] = None,
    cache_path: Optional[Path] = None,
    segment: str = DEFAULT_SEGMENT,
    on_failure: Optional[Callable[[list[str]], None]] = None,
    retrieved_at: Optional[str] = None,
) -> RefreshResult:
    """Refresh the authoritative holiday calendar from the official source.

    On success the validated calendar is installed and (when ``cache_path`` is
    given) atomically persisted as the last-validated cache. On ANY failure the
    last validated calendar (cache file, else the currently installed one) is
    retained and ``on_failure`` is invoked with the error messages. When no
    validated calendar exists at all, CalendarUnavailableError is raised —
    network failure is never converted into "no holidays".
    """
    errors: list[str] = []
    try:
        text = (http_get or _http_get_holiday_master)()
        fresh = parse_holiday_master(text, segment=segment)
        existing = _INSTALLED.holidays if _INSTALLED is not None else ()
        merged = _merge_carried_years(fresh, existing)
        cal = build_calendar(
            merged,
            source=LIVE_SOURCE,
            retrieved_at=retrieved_at or _utcnow_iso(),
            segment=segment,
        )
        if cache_path is not None:
            HolidayCalendarStore().save(cache_path, cal)
        install_calendar(cal)
        logger.info(
            "nse holiday calendar refreshed: %d holidays, hash %s",
            cal.holiday_count,
            cal.calendar_hash,
        )
        return RefreshResult(status="OK", calendar=cal)
    except Exception as exc:  # network, parse, validation, storage — all fail-safe
        errors.append(f"{type(exc).__name__}: {exc}")
        logger.warning("nse holiday refresh failed (%s)", errors[0])
        # last validated calendar wins: cache file, else the installed calendar
        fallback: Optional[HolidayCalendar] = None
        if cache_path is not None:
            fallback = HolidayCalendarStore().load(cache_path)
        if fallback is None and _INSTALLED is not None:
            fallback = _INSTALLED
        if fallback is None:
            raise CalendarUnavailableError(
                "no validated NSE holiday calendar: refresh failed and no "
                "validated fallback exists (never assume weekdays are trading)"
            ) from exc
        if callable(on_failure):
            on_failure(errors)
        return RefreshResult(status="FALLBACK", calendar=fallback, errors=tuple(errors))