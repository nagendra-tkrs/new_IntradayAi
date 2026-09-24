from datetime import datetime, date, timezone
from logging import getLogger
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from app.core import nse_calendar

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

_log = getLogger(__name__)

# ---------------------------------------------------------------------------
# Authoritative NSE holiday calendar (Phase 5C)
#
# Single source of truth = app.core.nse_calendar: it parses / validates /
# normalizes / dedupes the OFFICIAL NSE holiday-master payload (CM segment),
# fingerprints it deterministically, and installs ONE validated calendar
# snapshot. `NSE_HOLIDAYS` below is the LIVE MIRROR of that installed calendar
# — a mutable set so every existing consumer (year coverage, membership,
# market_day_status, rotation / state-service guards) keeps reading the single
# authoritative definition. The refresh path re-syncs it IN PLACE so references
# other modules already hold (from-imports included) stay live.
# ---------------------------------------------------------------------------
NSE_HOLIDAYS: set[date] = set()

_HOLIDAY_CACHE_PATH = Path(__file__).resolve().parent / "data" / "nse_holiday_cache.json"


def _sync_holiday_set(cal: nse_calendar.HolidayCalendar) -> None:
    """Point ``NSE_HOLIDAYS`` at the installed calendar's holiday dates."""
    NSE_HOLIDAYS.clear()
    NSE_HOLIDAYS.update(h.date for h in cal.holidays)


def _install_initial_calendar() -> None:
    """Install the last-validated cache, else the bundled official seed.

    Runs once at import (read-only — never writes). A cold start ALWAYS has
    the validated seed, so an un-refreshed process is never interpreted as
    \"has no holidays\". The fail-safe horizon (below) still refuses to report
    years the calendar does not cover as trading days.
    """
    store = nse_calendar.HolidayCalendarStore()
    cal = store.load(_HOLIDAY_CACHE_PATH)
    if cal is not None:
        _log.info(
            "nse holiday calendar loaded from cache (%d holidays)", cal.holiday_count
        )
    else:
        cal = nse_calendar.build_calendar(
            nse_calendar.seed_holidays(),
            source=nse_calendar.SEED_SOURCE,
            retrieved_at="2026-09-23T00:00:00",
            segment=nse_calendar.DEFAULT_SEGMENT,
        )
    nse_calendar.install_calendar(cal)
    _sync_holiday_set(cal)


def _CALENDAR_COVERS(year: int) -> bool:
    """Fail-safe horizon: the installed calendar explicitly covers ``year``."""
    years = sorted({d.year for d in NSE_HOLIDAYS})
    if not years:
        return False
    return years[0] <= year <= years[-1]


def is_confirmed_trading_day(d: date) -> bool:
    """Fail-safe trading-day confirmation (single authoritative definition).

    A date is a confirmed trading day ONLY when all of:
      * it is a weekday (Mon–Fri), and
      * it is not an official NSE CM holiday, and
      * the year is inside the installed calendar horizon.
    Anything the calendar cannot confirm (weekend, holiday, out-of-horizon
    year) is reported closed — NEVER silently open.
    """
    if d.weekday() >= 5:
        return False
    if is_holiday(d):
        return False
    return _CALENDAR_COVERS(d.year)


def calendar_hash() -> str:
    """Deterministic fingerprint of the installed calendar (identity, not time)."""
    return nse_calendar.calendar_hash_installed() or ""


def calendar_metadata() -> Optional[dict]:
    """Source / retrieved_at / years / count / hash of the installed calendar."""
    return nse_calendar.calendar_metadata()


def refresh_nse_holidays(
    *,
    http_get: Optional[Callable[[], str]] = None,
    on_failure: Optional[Callable[[list[str]], None]] = None,
    cache_path: Optional[Path] = None,
    segment: str = nse_calendar.DEFAULT_SEGMENT,
) -> nse_calendar.RefreshResult:
    """Refresh the authoritative calendar: fetch -> parse -> validate ->
    normalize -> dedupe -> store -> install, then re-sync ``NSE_HOLIDAYS``.

    Any failure keeps the last validated calendar (file cache, else the
    currently installed one) and reports it through ``on_failure``; with no
    validated fallback it raises ``CalendarUnavailableError`` — a network
    failure is NEVER converted into \"no holidays\" or \"all weekdays open\".
    """
    res = nse_calendar.refresh_holiday_calendar(
        http_get=http_get,
        cache_path=cache_path or _HOLIDAY_CACHE_PATH,
        on_failure=on_failure,
        segment=segment,
    )
    _sync_holiday_set(res.calendar)
    return res


_install_initial_calendar()


def now_ist() -> datetime:
    """IST business/market timestamp — the app's canonical session clock."""
    return datetime.now(IST)


def now_utc() -> datetime:
    """Timezone-aware UTC timestamp — canonical clock for JWT/DB timestamps and
    anything that must be comparable against ``datetime.now(timezone.utc)``.

    Replaces deprecated ``datetime.utcnow()`` / naive UTC everywhere so we never
    produce a naive datetime where an aware one is expected (audit C1)."""
    return datetime.now(timezone.utc)


def _bus_date(dt: datetime) -> date:
    """Return the trading-day date for ``dt``, normalizing naive → aware IST first."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.date()


def market_day_status(dt: datetime | date | None = None) -> str:
    """Authoritative, fail-safe classification of an NSE trading day.

    Returns one of:
      * ``"TRADING_DAY"``         — weekday, not in the NSE holiday calendar.
      * ``"HOLIDAY"``             — date present in ``NSE_HOLIDAYS``.
      * ``"WEEKEND"``             — Saturday or Sunday.
      * ``"UNKNOWN_CALENDAR_DATE"`` — outside the years the holiday calendar
        currently covers (or otherwise unverifiable). The app must NOT assume
        the market is open when the calendar cannot confirm it.

    Rule enforced here (audit C3/C4): a date is only ever reported open
    (``TRADING_DAY``) when the holiday calendar actually covers that year. Any
    date past the calendar horizon, or with insufficient data, is
    ``UNKNOWN_CALENDAR_DATE`` — callers must treat that as closed, not open.
    """
    if dt is None:
        d = now_ist().date()
    elif isinstance(dt, datetime):
        d = _bus_date(dt)
    else:
        d = dt

    if d.weekday() >= 5:
        return "WEEKEND"
    if d in NSE_HOLIDAYS:
        return "HOLIDAY"

    # Fail-safe horizon: the holiday calendar currently covers 2024-2026.
    # Years outside it are unverifiable — never report them as a trading day.
    if not _CALENDAR_COVERS(d.year):
        return "UNKNOWN_CALENDAR_DATE"
    return "TRADING_DAY"


def now_utc() -> datetime:
    """Canonical tz-aware UTC timestamp.

    Single timezone authority for timestamps that must be UTC (JWT exp/iat,
    DB ``created_at``/``updated_at`` columns). Always returns an aware datetime
    so callers can never accidentally produce the naive-vs-aware comparison
    bug the timezone audit calls out."""
    return datetime.now(timezone.utc)


def is_holiday(dt: date | None = None) -> bool:
    dt = dt or now_ist().date()
    return dt in NSE_HOLIDAYS


def is_market_hours(dt: datetime | None = None) -> bool:
    dt = dt or now_ist()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    if dt.weekday() >= 5:
        return False
    if is_holiday(dt.date()):
        return False
    market_open = dt.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = dt.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= dt <= market_close


def market_session(dt: datetime | None = None) -> str:
    dt = dt or now_ist()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    if dt.weekday() >= 5:
        return "closed"
    if is_holiday(dt.date()):
        return "closed"
    h, m = dt.hour, dt.minute
    t = h * 60 + m
    if t < 555:              # before 09:15
        return "pre_market"
    elif t == 555:           # exactly 09:15
        return "market_open"
    elif t < 915:            # 09:16 - 15:14
        return "trading"
    elif t <= 930:           # 15:15 - 15:30 (closing auction)
        return "closing"
    else:
        return "closed"
