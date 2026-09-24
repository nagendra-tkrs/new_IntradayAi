"""PHASE 5C — Authoritative NSE holiday calendar source & validation.

Covers the calendar-source integration that replaced the repository's
hardcoded NSE holiday subset with the OFFICIAL NSE holiday-master calendar
(CM / equity segment):

* fetching → parsing → validating → normalizing → deduplicating holidays;
* single authoritative abstraction consumed by ``is_holiday`` /
  ``is_confirmed_trading_day`` / ``market_day_status`` / ``NSE_HOLIDAYS``;
* deterministic calendar fingerprint (independent of retrieval time);
* refresh with last-validated fallback; explicit failure when NO validated
  calendar exists (never "no holidays", never "every weekday is open");
* §19 original-bug regression: excluding 2026-09-14 (Ganesh Chaturthi) flips
  the trading-consistency denominator from 20→19 (0.95 → 1.0000) with NO
  formula change.

All tests deterministic: no DB, no network, no config changes.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

import app.core.nse_calendar as nc
from app.core import market_session
from app.services import rotation_eligibility as elig
from app.services import rotation_selection as selection
from app.services.rotation_config import RotationEngineConfig
from app.services.universe_rotation_engine import UniverseRotationEngine

IST = market_session.IST

# ---------------------------------------------------------------------------
# official NSE CM 2026 holiday-master reference (fetched live 2026-09-23)
# ---------------------------------------------------------------------------
OFFICIAL_CM_2026: tuple[tuple[str, str, str], ...] = (
    ("15-Jan-2026", "Thursday", "Municipal Corporation Election - Maharashtra"),
    ("26-Jan-2026", "Monday", "Republic Day"),
    ("15-Feb-2026", "Sunday", "Mahashivratri"),
    ("03-Mar-2026", "Tuesday", "Holi"),
    ("21-Mar-2026", "Saturday", "Id-Ul-Fitr (Ramadan Eid)"),
    ("26-Mar-2026", "Thursday", "Shri Ram Navami"),
    ("31-Mar-2026", "Tuesday", "Shri Mahavir Jayanti"),
    ("03-Apr-2026", "Friday", "Good Friday"),
    ("14-Apr-2026", "Tuesday", "Dr. Baba Saheb Ambedkar Jayanti"),
    ("01-May-2026", "Friday", "Maharashtra Day"),
    ("28-May-2026", "Thursday", "Bakri Id"),
    ("26-Jun-2026", "Friday", "Muharram"),
    ("15-Aug-2026", "Saturday", "Independence Day"),
    ("14-Sep-2026", "Monday", "Ganesh Chaturthi"),
    ("02-Oct-2026", "Friday", "Mahatma Gandhi Jayanti"),
    ("20-Oct-2026", "Tuesday", "Dussehra"),
    ("08-Nov-2026", "Sunday", "Diwali Laxmi Pujan"),
    ("10-Nov-2026", "Tuesday", "Diwali-Balipratipada"),
    ("24-Nov-2026", "Tuesday", "Prakash Gurpurb Sri Guru Nanak Dev"),
    ("25-Dec-2026", "Friday", "Christmas"),
)


def _official_rows() -> list[dict]:
    return [
        {"tradingDate": d, "weekDay": w, "description": desc, "Sr_no": i}
        for i, (d, w, desc) in enumerate(OFFICIAL_CM_2026, start=1)
    ]


def _official_payload(*, other_segments=False, duplicate_gc=False) -> str:
    rows = _official_rows()
    if duplicate_gc:
        rows.append(
            {"tradingDate": "14-Sep-2026", "weekDay": "Monday",
             "description": "Ganesh Chaturthi (duplicate row)"}
        )
    data: dict = {"CM": rows}
    if other_segments:
        # synthetic rows for the OTHER segments — must never leak into CM
        data["FO"] = [
            {"tradingDate": "01-Oct-2026", "weekDay": "Thursday",
             "description": "FO-Test-Settlement-Holiday"}
        ]
        data["CD"] = [
            {"tradingDate": "03-Oct-2026", "weekDay": "Saturday",
             "description": "CD-Test-Holiday"}
        ]
    return json.dumps(data)


def _boom() -> str:
    raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# deterministic config / frame helpers (mirror the Phase 5B diagnostic file)
# ---------------------------------------------------------------------------
def _cfg(**overrides):
    base = dict(
        core_size=30,
        rotation_size=50,
        candidate_pool_min=150,
        candidate_pool_max=250,
        window_weeks=2,
        min_observation_sessions=10,
        expected_bars_per_session=75,
        signal_warmup_bars=55,
        liquidity_floor=50_000_000.0,
        volume_floor=500_000.0,
        volume_cv_max=2.5,
        atr_min_pct=0.0008,
        atr_max_pct=0.05,
        atr_opt_low_pct=0.006,
        atr_opt_high_pct=0.02,
        min_data_quality=0.6,
        min_signal_sample_long=8,
        min_signal_sample_short=8,
        min_frequency_floor=0.0,
        core_trading_consistency_pct=99.0,
        core_volume_cv_max=2.5,
        core_review_consecutive_periods=2,
        core_replacement_score_delta=5.0,
        core_retention_bonus=0.03,
        anti_churn_score_delta=5.0,
        rotation_retention_floor=45.0,
        replacement_cooldown_weeks=2,
        cooldown_override_delta=10.0,
        max_churn_per_cycle=10,
    )
    base.update(overrides)
    return RotationEngineConfig(**base)


def _session_frame(
    day: date,
    n_bars: int = 75,
    volume: float = 1_000_000.0,
    open_p: float = 100.0,
    close_p: float = 101.0,
) -> pd.DataFrame:
    starts = [
        datetime.combine(day, time(9, 15)) + i * timedelta(minutes=5)
        for i in range(n_bars)
    ]
    ts = pd.Series([t.replace(tzinfo=IST) for t in starts], name="timestamp")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [open_p] * n_bars,
            "high": [max(open_p, close_p)] * n_bars,
            "low": [min(open_p, close_p)] * n_bars,
            "close": [close_p] * n_bars,
            "volume": [volume] * n_bars,
        }
    )


def _multi_day_frame(days, **kw):
    return pd.concat([_session_frame(d, **kw) for d in days], ignore_index=True)


def _trading_days(days):
    return [d for d in days if elig.is_confirmed_trading_day(d)]


def _evaluated_consistency(df, days, cfg=None) -> float:
    cfg = cfg or _cfg()
    expected = _trading_days(days)
    ev = elig.evaluate_candidate("TEST", df, expected, cfg, signal_metrics=None)
    return ev.trading_consistency


def _cand(
    symbol="S",
    score=60.0,
    dq=8.0,
    liq=1e9,
    vol_cv=0.2,
    atr=0.01,
    consistency=1.0,
    reliability=1.0,
):
    return selection.RotationCandidate(
        symbol=symbol,
        rotation_score=score,
        data_quality_score=dq,
        median_daily_value=liq,
        volume_cv=vol_cv,
        median_atr_pct=atr,
        trading_consistency=consistency,
        signal_reliability=reliability,
        expected_sessions=10,
        present_sessions=10,
    )


# guard that snapshots/restores the installed calendar + NSE_HOLIDAYS mirror
@pytest.fixture
def calendar_guard():
    before_cal = nc._INSTALLED
    before_mirror = set(market_session.NSE_HOLIDAYS)
    yield
    nc._INSTALLED = before_cal
    market_session.NSE_HOLIDAYS.clear()
    market_session.NSE_HOLIDAYS.update(before_mirror)


# ===========================================================================
# A — parsing / validation / normalization / deduplication
# ===========================================================================
def test_a1_parse_master_date_normalization_strict():
    assert nc.parse_master_date("14-Sep-2026") == date(2026, 9, 14)
    for bad in ("14-Sept-2026", "2026-09-14", "14/09/2026", "", "   "):
        with pytest.raises(ValueError):
            nc.parse_master_date(bad)


def test_a2_parse_official_cm_payload_dedup_and_segment_filter():
    rows = nc.parse_holiday_master(
        _official_payload(other_segments=True, duplicate_gc=True)
    )
    assert len(rows) == 20  # duplicate 14-Sep row deduped; FO/CD rows filtered
    assert all(h.segment == "CM" for h in rows)
    assert any(h.date == date(2026, 9, 14) and h.name == "Ganesh Chaturthi" for h in rows)
    dates = {h.date for h in rows}
    assert date(2026, 10, 1) not in dates  # FO segment row excluded
    assert date(2026, 10, 3) not in dates  # CD segment row excluded
    assert date(2026, 10, 2) in dates  # official CM Mahatma Gandhi Jayanti


def test_a3_validation_rejects_malformed_rows():
    cases = [
        {"tradingDate": "14-Sep-2026", "description": "   "},  # blank name
        {"tradingDate": "", "description": "x"},
        {"tradingDate": "not-a-date", "description": "x"},
        {"description": "x"},  # missing date
    ]
    for row in cases:
        with pytest.raises(ValueError):
            nc.parse_holiday_master(json.dumps({"CM": [row]}))
    with pytest.raises(ValueError):
        nc.parse_holiday_master("not json {{{")
    with pytest.raises(ValueError):
        nc.parse_holiday_master(json.dumps({"CM": "oops"}))
    with pytest.raises(ValueError):
        nc.parse_holiday_master(json.dumps({"FO": _official_rows()}))  # no CM


# ===========================================================================
# B — deterministic fingerprint / metadata
# ===========================================================================
def test_b1_hash_deterministic_order_free_and_time_independent():
    rows = [
        nc.NseHoliday(date=date(2026, 9, 14), name="Ganesh Chaturthi"),
        nc.NseHoliday(date=date(2026, 1, 26), name="Republic Day"),
    ]
    h1 = nc.calendar_hash(rows)
    assert nc.calendar_hash(list(reversed(rows))) == h1
    changed = [
        nc.NseHoliday(date=date(2026, 9, 14), name="Ganesh Chaturthi"),
        nc.NseHoliday(date=date(2026, 1, 26), name="Republic Day (renamed)"),
    ]
    assert nc.calendar_hash(changed) != h1
    # retrieval timestamp never changes the fingerprint
    a = nc.build_calendar(rows, retrieved_at="2026-09-23T10:00:00")
    b = nc.build_calendar(rows, retrieved_at="2026-09-24T10:00:00")
    assert a.calendar_hash == b.calendar_hash


def test_b2_seed_metadata_coverage_and_years():
    cal = nc.installed_calendar()
    assert cal.years == (2024, 2025, 2026)
    assert cal.holiday_count == len(nc.OFFICIAL_HOLIDAY_SEED) == 52
    assert nc.calendar_hash_installed() == cal.calendar_hash
    y2026 = [h for h in cal.holidays if h.date.year == 2026]
    assert len(y2026) == 20  # official CM 2026 list


# ===========================================================================
# C — calendar semantics (single authoritative abstraction)
# ===========================================================================
def test_c1_2026_09_14_is_official_holiday():
    assert market_session.is_holiday(date(2026, 9, 14)) is True
    assert market_session.is_confirmed_trading_day(date(2026, 9, 14)) is False
    assert market_session.market_day_status(date(2026, 9, 14)) == "HOLIDAY"
    assert market_session.market_session(datetime(2026, 9, 14, 11, 0, tzinfo=IST)) == "closed"


def test_c2_weekends_are_not_trading_days():
    for d in (date(2026, 8, 22), date(2026, 8, 23), date(2026, 9, 19)):
        assert market_session.is_confirmed_trading_day(d) is False
        assert market_session.market_day_status(d) == "WEEKEND"


def test_c3_normal_trading_days_are_confirmed():
    for d in (date(2026, 8, 17), date(2026, 8, 28), date(2026, 9, 15), date(2026, 9, 16)):
        assert market_session.is_confirmed_trading_day(d) is True
        assert market_session.market_day_status(d) == "TRADING_DAY"


def test_c4_out_of_horizon_year_is_closed_never_open():
    for d in (date(2027, 1, 4), date(2023, 1, 2)):  # weekdays, no coverage
        assert market_session.market_day_status(d) == "UNKNOWN_CALENDAR_DATE"
        assert market_session.is_confirmed_trading_day(d) is False


def test_c5_provider_failure_is_not_a_holiday():
    ev = elig.provider_failure_evaluation("LOST", "boom")
    assert ev.trading_consistency is None  # provider failure -> None, hard fail
    assert market_session.is_holiday(date(2026, 9, 15)) is False  # not "holiday"
    core, res = selection.select_core([_cand("LOST", consistency=None)], _cfg())
    assert core == []


# ===========================================================================
# D — reconciliation: repo 2026 subset vs official CM 2026
# ===========================================================================
def test_d1_reconciliation_official_wins_for_2026():
    # missing from the old repo subset, present officially, NOW holidays
    for d in (date(2026, 9, 14), date(2026, 3, 21), date(2026, 2, 15),
              date(2026, 10, 20), date(2026, 1, 15), date(2026, 3, 26),
              date(2026, 3, 31), date(2026, 5, 28), date(2026, 6, 26)):
        assert market_session.is_holiday(d) is True, f"{d} must be a 2026 holiday"
    # stale / inaccurate repo dates removed (official wins)
    for d in (date(2026, 3, 4), date(2026, 3, 20), date(2026, 3, 30),
              date(2026, 5, 16), date(2026, 5, 27), date(2026, 6, 25),
              date(2026, 8, 16), date(2026, 2, 17)):
        assert market_session.is_holiday(d) is False, f"{d} must NOT be a holiday"


# ===========================================================================
# E — windows & trading-consistency denominator (§19 original-bug regression)
# ===========================================================================
def test_e1_expected_sessions_20_to_19_after_calendar_fix():
    sessions = elig.confirmed_trading_days(date(2026, 8, 27), date(2026, 9, 23))
    assert len(sessions) == 19
    assert date(2026, 9, 14) not in sessions
    assert sessions[-1] == date(2026, 9, 23)


def test_e2_engine_expected_session_dates_exclude_the_holiday_deterministically():
    cfg = RotationEngineConfig.from_settings()
    eng = UniverseRotationEngine(config=cfg)
    calc = datetime(2026, 9, 23, 18, 0)
    dates = eng.expected_session_dates(calc)
    assert date(2026, 9, 14) not in dates
    assert len(dates) == 19
    assert len(set(dates)) == len(dates)
    assert eng.expected_session_dates(calc) == dates  # determinism


def test_e3_holiday_with_no_bars_does_not_penalize_consistency():
    """The original bug, fixed: excluding the official holiday flips the
    denominator 20→19 so 19/19 = 1.0000 (the old arithmetic 19/20 = 0.95 is
    what pinned every eligible candidate in Phase 5B). Formula untouched."""
    sessions = elig.confirmed_trading_days(date(2026, 8, 27), date(2026, 9, 23))
    assert len(sessions) == 19  # would have been 20 on the old repo subset
    df = _multi_day_frame(sessions)  # bars ONLY on the 19 confirmed days
    ev = elig.evaluate_candidate("FIX", df, sessions, _cfg(), signal_metrics=None)
    assert ev.trading_consistency == pytest.approx(1.0)
    assert ev.trading_consistency == (len(sessions) - 0) / len(sessions)


def test_e4_missing_bars_on_a_trading_day_stays_in_denominator():
    """Missing provider data on a real trading day is NOT a holiday: it stays
    in the denominator and penalizes consistency (2/3, not 1.0)."""
    tue, wed, thu = date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)
    days = [tue, wed, thu]  # all confirmed trading days
    assert _trading_days(days) == days
    df = _multi_day_frame([tue, wed])  # thu absent from provider data
    assert _evaluated_consistency(df, days) == pytest.approx(2 / 3)


# ===========================================================================
# F — refresh: fetch → parse → validate → store → install (+ fallback)
# ===========================================================================
def test_f1_refresh_ok_installs_caches_and_syncs_mirror(tmp_path, calendar_guard):
    cache = tmp_path / "holiday_cache.json"
    # official 2026 payload + one additional holiday to prove override+merge
    payload = json.dumps({"CM": _official_rows() + [
        {"tradingDate": "27-Aug-2026", "weekDay": "Thursday",
         "description": "Extra Test Holiday"}
    ]})
    failed: list = []
    res = market_session.refresh_nse_holidays(
        http_get=lambda: payload, on_failure=failed.append, cache_path=cache
    )
    assert res.status == "OK"
    assert failed == []
    assert cache.exists()
    # carried years preserved, 2026 replaced by the fresh official rows
    assert nc.installed_calendar().years == (2024, 2025, 2026)
    assert nc.installed_calendar().holiday_count == 53  # 52 seed + 1 added
    # mirror synced in place (live from-import references see the same set)
    assert market_session.is_holiday(date(2026, 8, 27)) is True
    assert market_session.is_confirmed_trading_day(date(2026, 8, 27)) is False


def test_f2_refresh_determinism_same_payload_same_hash(tmp_path, calendar_guard):
    cache = tmp_path / "h.json"
    r1 = market_session.refresh_nse_holidays(
        http_get=lambda: _official_payload(), cache_path=cache
    )
    r2 = market_session.refresh_nse_holidays(
        http_get=lambda: _official_payload(), cache_path=cache
    )
    assert r1.status == r2.status == "OK"
    assert r1.calendar.calendar_hash == r2.calendar.calendar_hash
    assert r1.calendar.holiday_count == r2.calendar.holiday_count == 52


def test_f3_refresh_failure_keeps_last_validated(tmp_path, calendar_guard):
    cache = tmp_path / "h.json"
    market_session.refresh_nse_holidays(
        http_get=lambda: _official_payload(), cache_path=cache
    )
    cal_after_ok = nc.installed_calendar()
    failed: list = []
    res = market_session.refresh_nse_holidays(
        http_get=_boom, on_failure=failed.append, cache_path=cache
    )
    assert res.status == "FALLBACK"
    assert res.errors and "boom" in res.errors[0]
    assert len(failed) == 1
    # last validated calendar intact — official data still authoritative
    assert nc.installed_calendar().calendar_hash == cal_after_ok.calendar_hash
    assert market_session.is_holiday(date(2026, 9, 14)) is True
    assert market_session.is_confirmed_trading_day(date(2026, 9, 14)) is False


def test_f4_no_validated_fallback_fails_explicitly(tmp_path, calendar_guard):
    """No cache + no installed calendar + fetch fails → explicit error. The
    fail-safe NEVER treats network failure as 'no holidays': a weekday stays
    closed (UNKNOWN_CALENDAR_DATE), not open."""
    nc._INSTALLED = None
    market_session.NSE_HOLIDAYS.clear()  # no validated fallback anywhere
    with pytest.raises(nc.CalendarUnavailableError):
        market_session.refresh_nse_holidays(
            http_get=_boom, cache_path=tmp_path / "c.json"
        )
    assert market_session.is_confirmed_trading_day(date(2026, 9, 15)) is False
    assert market_session.market_day_status(date(2026, 9, 15)) == "UNKNOWN_CALENDAR_DATE"


# ===========================================================================
# G — cache persistence integrity
# ===========================================================================
def test_g1_cache_load_rejects_tampered_or_malformed(tmp_path):
    store = nc.HolidayCalendarStore()
    cal = nc.build_calendar(
        [nc.NseHoliday(date=date(2026, 9, 14), name="Ganesh Chaturthi")],
        source="TEST", retrieved_at="2026-09-23T10:00:00",
    )
    p = tmp_path / "c.json"
    store.save(p, cal)
    assert store.load(p) is not None
    # tamper a date -> fingerprint mismatch -> rejected (never trusted)
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["holidays"][0]["date"] = "2026-09-15"
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert store.load(p) is None
    # malformed JSON -> rejected
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert store.load(tmp_path / "bad.json") is None
    # wrong schema -> rejected
    (tmp_path / "noschema.json").write_text(
        json.dumps({"holidays": []}), encoding="utf-8"
    )
    assert store.load(tmp_path / "noschema.json") is None