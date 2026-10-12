"""Validation tests for the request models (checklist section E).
Put this file at tests/test_models.py and run from the repo root:  python -m pytest tests -v
No database needed: these build the Pydantic models directly. In the API, every
ValidationError here becomes a 422 automatically."""
import pytest
from pydantic import ValidationError

from app.main import EmployeeIn, PunchInIn, PunchOutIn, RegularizeIn

MS = 1783310280000  # a valid epoch-ms instant (2026-07-06 09:28 IST)

GOOD_EMP = {
    "emp_code": "EMP0001", "name": "Asha Rao", "email": "asha@example.com",
    "department": "Engineering", "joined_on": "2026-01-05",
}


def emp(**overrides):
    return EmployeeIn(**{**GOOD_EMP, **overrides})


# ------------------------------- EmployeeIn -------------------------------
def test_employee_valid_with_default_shift():
    e = emp()
    assert (e.shift_start, e.shift_end) == ("09:30", "18:30")

def test_employee_overnight_shift_is_valid():
    emp(shift_start="22:00", shift_end="06:00")

@pytest.mark.parametrize("code", ["EMP000001", "EMP1234", "EMP123456"])
def test_emp_code_valid(code):
    emp(emp_code=code)

@pytest.mark.parametrize("code", ["abc", "EMP123", "EMP1234567", "emp0001", "EMP00A1", " EMP0001", ""])
def test_emp_code_invalid(code):
    with pytest.raises(ValidationError):
        emp(emp_code=code)

@pytest.mark.parametrize("email", ["not-an-email", "a@b", "a b@c.com", "@c.com", "a@b.c" + "o" * 120])
def test_email_invalid(email):
    with pytest.raises(ValidationError):
        emp(email=email)

@pytest.mark.parametrize("field,value", [
    ("name", ""), ("name", "x" * 101),
    ("department", ""), ("department", "x" * 51),
])
def test_length_limits(field, value):
    with pytest.raises(ValidationError):
        emp(**{field: value})

def test_length_limits_at_the_edge_are_ok():
    emp(name="x" * 100, department="x" * 50)

@pytest.mark.parametrize("t", ["25:00", "24:00", "9:30", "09:60", "0930", "09:30:00", "ab:cd"])
def test_shift_time_invalid(t):
    with pytest.raises(ValidationError):
        emp(shift_start=t)

def test_shift_start_equal_to_end_rejected():
    with pytest.raises(ValidationError):
        emp(shift_start="09:00", shift_end="09:00")

@pytest.mark.parametrize("d", ["2026-02-30", "yesterday", "2026-1-5", "2026-13-01", "05-01-2026", 20260105])
def test_joined_on_invalid(d):
    with pytest.raises(ValidationError):
        emp(joined_on=d)

def test_joined_on_leap_day_ok():
    emp(joined_on="2024-02-29")

@pytest.mark.parametrize("field", ["emp_code", "name", "email", "department", "joined_on"])
def test_required_fields(field):
    data = dict(GOOD_EMP)
    del data[field]
    with pytest.raises(ValidationError):
        EmployeeIn(**data)


# ------------------------------- PunchInIn --------------------------------
def test_punch_in_defaults():
    p = PunchInIn(emp_code="EMP0001")
    assert p.punched_at is None and p.status == "PRESENT"

def test_punch_in_emp_code_is_not_pattern_checked():
    # An unknown or odd emp_code must reach the endpoint and become 404, not 422
    PunchInIn(emp_code="NOPE")

@pytest.mark.parametrize("status", ["PRESENT", "WFH", "ON_DUTY"])
def test_punch_in_presence_statuses_ok(status):
    PunchInIn(emp_code="EMP0001", status=status)

@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "banana", "present"])
def test_punch_in_other_statuses_rejected(status):
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP0001", status=status)

@pytest.mark.parametrize("ms", [100_000_000_000, MS, 4_102_444_800_000])
def test_punched_at_valid_range(ms):
    PunchInIn(emp_code="EMP0001", punched_at=ms)

@pytest.mark.parametrize("bad", [
    str(MS),                 # string
    float(MS),               # float, even a whole one
    MS + 0.5,                # fractional float
    1783310280,              # seconds, not ms
    99_999_999_999,          # just below the minimum
    4_102_444_800_001,       # just above the maximum
    -1,
    True,                    # bool is an int subclass in Python
])
def test_punched_at_rejected(bad):
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP0001", punched_at=bad)


# ------------------------------- PunchOutIn -------------------------------
def test_punch_out_defaults():
    assert PunchOutIn(emp_code="EMP0001").punched_at is None

@pytest.mark.parametrize("bad", [str(MS), float(MS), 1783310280, True])
def test_punch_out_punched_at_rejected(bad):
    with pytest.raises(ValidationError):
        PunchOutIn(emp_code="EMP0001", punched_at=bad)


# ------------------------------ RegularizeIn ------------------------------
BASE = {"reason": "biometric glitch", "regularized_by": "hr.admin"}

def test_regularize_minimal_is_valid_model():
    # "changes nothing" is a 422 too, but that needs the stored record, so it is the endpoint's job
    RegularizeIn(**BASE)

@pytest.mark.parametrize("status", ["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"])
def test_regularize_all_statuses_ok(status):
    RegularizeIn(**BASE, status=status)

def test_regularize_bad_status():
    with pytest.raises(ValidationError):
        RegularizeIn(**BASE, status="banana")

@pytest.mark.parametrize("reason", ["abcd", "x" * 201])
def test_regularize_reason_length(reason):
    with pytest.raises(ValidationError):
        RegularizeIn(reason=reason, regularized_by="hr.admin")

@pytest.mark.parametrize("by", ["", "x" * 51])
def test_regularize_by_length(by):
    with pytest.raises(ValidationError):
        RegularizeIn(reason="biometric glitch", regularized_by=by)

@pytest.mark.parametrize("field", ["reason", "regularized_by"])
def test_regularize_required(field):
    data = dict(BASE)
    del data[field]
    with pytest.raises(ValidationError):
        RegularizeIn(**data)

@pytest.mark.parametrize("bad", [str(MS), float(MS), 1783310280])
def test_regularize_times_are_strict_ms(bad):
    with pytest.raises(ValidationError):
        RegularizeIn(**BASE, punch_in=bad)

@pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
@pytest.mark.parametrize("times", [{"punch_in": MS}, {"punch_out": MS + 3_600_000}])
def test_absent_or_leave_with_punch_times_rejected(status, times):
    with pytest.raises(ValidationError):
        RegularizeIn(**BASE, status=status, **times)

def test_derived_fields_sent_by_client_are_ignored():
    r = RegularizeIn(**BASE, work_hours=99, late_minutes=500, half_day=True)
    assert not hasattr(r, "work_hours") and not hasattr(r, "late_minutes")