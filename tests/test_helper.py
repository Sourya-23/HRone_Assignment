"""Tests for the time and rule helpers (R1 to R5).
Put this file at tests/test_helpers.py and run from the repo root:  python -m pytest tests -v
Importing app.main connects a MongoClient, which is lazy, so no database is needed for these tests."""
from datetime import datetime, timedelta, timezone

from app.main import (to_utc, to_ms, attendance_date, shift_bounds,
                      late_minutes, work_hours, overtime_minutes, is_half_day)

IST = timezone(timedelta(hours=5, minutes=30))


def ist(y, mo, d, h, mi, s=0):
    """Build an aware UTC datetime from an IST wall-clock time."""
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc)


# ---------- conversions (R1 truncation) ----------
def test_to_utc_is_aware_utc():
    dt = to_utc(1783310280000)
    assert dt.tzinfo is not None and dt.utcoffset() == timedelta(0)
    assert dt == ist(2026, 7, 6, 9, 28)

def test_to_utc_truncates_to_whole_seconds():
    assert to_utc(1783310280900) == to_utc(1783310280000)

def test_round_trip_ms():
    assert to_ms(to_utc(1783310280900)) == 1783310280000


# ---------- R1 attendance date ----------
def test_date_is_ist_not_utc():
    # 02:00 IST on 7 July is still 6 July in UTC
    assert attendance_date(ist(2026, 7, 7, 2, 0), "09:30", "18:30") == "2026-07-07"

def test_overnight_after_midnight_belongs_to_previous_day():
    assert attendance_date(ist(2026, 7, 7, 1, 0), "22:00", "06:00") == "2026-07-06"

def test_overnight_evening_punch_is_same_day():
    assert attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"

def test_overnight_exactly_at_shift_end_is_same_day():
    # "earlier than shift_end" is strict, so 06:00:00 is NOT the previous shift
    assert attendance_date(ist(2026, 7, 7, 6, 0), "22:00", "06:00") == "2026-07-07"

def test_overnight_one_second_before_shift_end():
    assert attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == "2026-07-06"

def test_shift_bounds_overnight_end_is_next_day():
    start, end = shift_bounds("2026-07-06", "22:00", "06:00")
    assert start == ist(2026, 7, 6, 22, 0)
    assert end == ist(2026, 7, 7, 6, 0)


# ---------- R2 late (the exact examples from the contract) ----------
def _late(punch, date="2026-07-06", ss="09:30", se="18:30"):
    start, _ = shift_bounds(date, ss, se)
    return late_minutes(punch, start)

def test_late_exactly_at_grace_is_zero():
    assert _late(ist(2026, 7, 6, 9, 40, 0)) == 0

def test_late_one_second_after_grace_counts_from_shift_start():
    assert _late(ist(2026, 7, 6, 9, 40, 1)) == 10

def test_late_floors_minutes():
    assert _late(ist(2026, 7, 6, 10, 15, 59)) == 45

def test_early_punch_is_not_late():
    assert _late(ist(2026, 7, 6, 9, 28)) == 0

def test_late_overnight_after_midnight():
    assert _late(ist(2026, 7, 7, 1, 0), ss="22:00", se="06:00") == 180


# ---------- R4 work hours, R5 half day ----------
def test_work_hours_sample_record_1():
    assert work_hours(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 9.12

def test_work_hours_half_up_not_float_round():
    # 28,962 s = 8.045 h exactly: half-up gives 8.05, Python round() gives 8.04
    assert work_hours(ist(2026, 7, 6, 9, 0), ist(2026, 7, 6, 17, 2, 42)) == 8.05

def test_half_day_uses_rounded_value():
    # 16,182 s = 4.495 h exactly -> rounds to 4.50 -> NOT a half day
    wh = work_hours(ist(2026, 7, 7, 9, 0), ist(2026, 7, 7, 13, 29, 42))
    assert wh == 4.5
    assert is_half_day(wh) is False

def test_half_day_true_below_threshold():
    assert is_half_day(3.5) is True


# ---------- R3 overtime ----------
def _ot(out, date="2026-07-06", ss="09:30", se="18:30"):
    _, end = shift_bounds(date, ss, se)
    return overtime_minutes(out, end)

def test_overtime_under_30_is_zero():
    assert _ot(ist(2026, 7, 6, 18, 45)) == 0

def test_overtime_29_min_59_s_is_zero():
    assert _ot(ist(2026, 7, 6, 18, 59, 59)) == 0

def test_overtime_exactly_30_counts():
    assert _ot(ist(2026, 7, 6, 19, 0, 0)) == 30

def test_overtime_sample_record_2():
    assert _ot(ist(2026, 7, 6, 19, 10)) == 40

def test_overtime_before_shift_end_is_zero():
    assert _ot(ist(2026, 7, 6, 17, 0)) == 0

def test_overtime_overnight_sample_record_7():
    assert _ot(ist(2026, 7, 7, 6, 40), ss="22:00", se="06:00") == 40