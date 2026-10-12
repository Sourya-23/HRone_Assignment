"""Tests for the two serializers: Mongo document -> API response shape.
Put this file at tests/test_serializers.py and run from the repo root:  python -m pytest tests -v
No database needed. The input documents mimic what PyMongo returns with tz_aware=True."""
from datetime import datetime, timezone

from bson import ObjectId

from app.main import employee_out, record_out


def utc(s):
    """'2026-07-06T04:35:00' -> aware UTC datetime, like PyMongo with tz_aware=True."""
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


RECORD_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
               "late_minutes", "overtime_minutes", "half_day", "history"}


# ------------------------------- employee_out -------------------------------
def test_employee_out_shape_and_ms():
    doc = {
        "_id": ObjectId(), "emp_code": "EMP0001", "name": "Asha Rao", "email": "asha@example.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30",
        "joined_on": "2026-01-05", "created_at": utc("2026-01-05T04:30:00"),
    }
    assert employee_out(doc) == {
        "emp_code": "EMP0001", "name": "Asha Rao", "email": "asha@example.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30",
        "joined_on": "2026-01-05", "created_at": 1767587400000,
    }

def test_employee_out_never_leaks_id_or_unknown_fields():
    doc = {"_id": ObjectId(), "emp_code": "EMP0002", "name": "B", "email": "b@x.io",
           "department": "Ops", "shift_start": "09:30", "shift_end": "18:30",
           "joined_on": "2026-01-05", "created_at": utc("2026-01-05T04:30:00"), "salary": 1}
    out = employee_out(doc)
    assert "_id" not in out and "id" not in out and "salary" not in out


# -------------------------------- record_out --------------------------------
def test_full_record_sample_2():
    doc = {
        "_id": ObjectId(), "emp_code": "EMP0003", "date": "2026-07-06", "status": "PRESENT",
        "punch_in": utc("2026-07-06T04:35:00"), "punch_out": utc("2026-07-06T13:40:00"),
        "work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False, "history": [],
    }
    assert record_out(doc) == {
        "emp_code": "EMP0003", "date": "2026-07-06", "status": "PRESENT",
        "punch_in": 1783312500000, "punch_out": 1783345200000,
        "work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False, "history": [],
    }

def test_ms_values_are_ints():
    doc = {"emp_code": "EMP0003", "date": "2026-07-06", "status": "PRESENT",
           "punch_in": utc("2026-07-06T04:35:00"), "punch_out": utc("2026-07-06T13:40:00")}
    out = record_out(doc)
    assert type(out["punch_in"]) is int and type(out["punch_out"]) is int

def test_open_record_sample_3():
    doc = {"emp_code": "EMP0006", "date": "2026-07-07", "status": "PRESENT",
           "punch_in": utc("2026-07-07T04:05:00"), "punch_out": None, "work_hours": None,
           "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    out = record_out(doc)
    assert out["punch_in"] == 1783397100000
    assert out["punch_out"] is None and out["work_hours"] is None

def test_absent_record_with_punch_fields_missing_entirely():
    # Seeded ABSENT / LEAVE data may omit the punch fields instead of storing null
    doc = {"emp_code": "EMP0004", "date": "2026-07-08", "status": "ABSENT"}
    out = record_out(doc)
    assert set(out) == RECORD_KEYS
    assert out["punch_in"] is None and out["punch_out"] is None and out["work_hours"] is None
    assert out["late_minutes"] == 0 and out["overtime_minutes"] == 0
    assert out["half_day"] is False and out["history"] == []

def test_legacy_record_sample_9_defaults():
    doc = {"_id": ObjectId(), "emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
           "punch_in": utc("2026-07-14T04:02:00"), "punch_out": utc("2026-07-14T13:01:00"),
           "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0}
    out = record_out(doc)
    assert out["half_day"] is False
    assert out["history"] == []

def test_record_never_leaks_id():
    doc = {"_id": ObjectId(), "emp_code": "EMP0004", "date": "2026-07-08", "status": "LEAVE", "foo": 1}
    out = record_out(doc)
    assert set(out) == RECORD_KEYS

def test_regularized_record_sample_8_history_converted():
    doc = {
        "emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT",
        "punch_in": utc("2026-07-07T03:58:00"), "punch_out": utc("2026-07-07T13:00:00"),
        "work_hours": 9.03, "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
        "history": [{
            "at": utc("2026-07-08T05:00:00"), "by": "hr.admin", "reason": "biometric glitch",
            "changes": {
                "punch_in": {"from": utc("2026-07-06T04:35:00"), "to": utc("2026-07-07T03:58:00")},
                "late_minutes": {"from": 35, "to": 0},
                "work_hours": {"from": 8.42, "to": 9.03},
            },
        }],
    }
    assert record_out(doc)["history"] == [{
        "at": 1783486800000, "by": "hr.admin", "reason": "biometric glitch",
        "changes": {
            "punch_in": {"from": 1783312500000, "to": 1783396680000},
            "late_minutes": {"from": 35, "to": 0},
            "work_hours": {"from": 8.42, "to": 9.03},
        },
    }]

def test_history_time_change_from_null():
    # e.g. a correction that adds a missing punch_out, or turns ABSENT into PRESENT
    doc = {"emp_code": "EMP0004", "date": "2026-07-08", "status": "PRESENT",
           "punch_in": utc("2026-07-08T04:00:00"), "punch_out": utc("2026-07-08T13:00:00"),
           "history": [{"at": utc("2026-07-09T05:00:00"), "by": "hr", "reason": "added times",
                        "changes": {"status": {"from": "ABSENT", "to": "PRESENT"},
                                    "punch_out": {"from": None, "to": utc("2026-07-08T13:00:00")}}}]}
    ch = record_out(doc)["history"][0]["changes"]
    assert ch["status"] == {"from": "ABSENT", "to": "PRESENT"}
    assert ch["punch_out"]["from"] is None
    assert type(ch["punch_out"]["to"]) is int

def test_history_bool_change_kept_as_bool():
    doc = {"emp_code": "EMP0004", "date": "2026-07-07", "status": "WFH",
           "history": [{"at": utc("2026-07-09T05:00:00"), "by": "hr", "reason": "fix out",
                        "changes": {"half_day": {"from": True, "to": False}}}]}
    assert record_out(doc)["history"][0]["changes"]["half_day"] == {"from": True, "to": False}