"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError


# --------------------------------------------------------------------------- #
# Config and client
# --------------------------------------------------------------------------- #
load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def to_utc(ms) -> datetime:
    """Convert milliseconds since epoch to UTC datetime, truncated to whole seconds (R1)"""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)

def to_ms(dt) -> int:
    """Convert UTC datetime to milliseconds since epoch"""
    return int(dt.timestamp() * 1000)

def attendance_date(punch_in, shift_start, shift_end) -> str:
    """IST date of the punch, minus one day if overnight and the IST time is strictly earlier than shift_end"""
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    local = punch_in.astimezone(IST)
    d = local.date()
    overnight = time(h_end, m_end) <= time(h_start, m_start)
    if overnight and local.time() < time(h_end, m_end):
        d -= timedelta(days=1)
    return d.isoformat()

def shift_bounds(date_str, shift_start, shift_end) -> tuple[datetime, datetime]:
    """The shift’s start and end as aware UTC datetimes; end moves to the next day if overnight"""
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    start = datetime.fromisoformat(date_str).replace(hour=h_start, minute=m_start, tzinfo=IST)
    end = datetime.fromisoformat(date_str).replace(hour=h_end, minute=m_end, tzinfo=IST)
    if end <= start:
        end += timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

def late_minutes(punch_in, start) -> int:
    """Compare seconds > 600, then floor to minutes"""
    delta = punch_in - start
    return max(0, int(delta.total_seconds() // 60)) if delta.total_seconds() > 600 else 0

def work_hours(punch_in, punch_out) -> float:
    """Decimal with ROUND_HALF_UP, 2 decimals"""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)

def overtime_minutes(punch_out, end) -> int:
    """Floor minutes past shift end; only counts if at least 30 minutes (R3)"""
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0

def is_half_day(work_hours) -> bool:
    """True if work_hours < 4.5"""
    return work_hours < 4.5


# --------------------------------------------------------------------------- #
# Starter helpers (original versions, not used by the code above; see REVIEW.md)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


# --------------------------------------------------------------------------- #
# Serializers: Mongo document -> API response
# Output is built from an allow-list, so _id or any unexpected stored field never leaks.
# --------------------------------------------------------------------------- #
EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
TIME_FIELDS = ("punch_in", "punch_out")


def _ms(value):
    """None stays None, a datetime becomes epoch ms"""
    return None if value is None else to_ms(value)


def employee_out(doc) -> dict:
    out = {k: doc.get(k) for k in EMPLOYEE_FIELDS}
    out["created_at"] = _ms(doc.get("created_at"))
    return out


def _history_out(entry) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in TIME_FIELDS:
            change = {"from": _ms(change.get("from")), "to": _ms(change.get("to"))}
        changes[field] = change
    return {"at": _ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc) -> dict:
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": _ms(doc.get("punch_in")),
        "punch_out": _ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        # `or` covers both a missing key and a stored null
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": doc.get("half_day") or False,
        "history": [_history_out(h) for h in doc.get("history") or []],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

# StrictInt: rejects strings, floats and booleans (True is an int in Python)
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
ShiftTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    # kept as a string: Mongo stores it as one, and pydantic's date type would accept 20260105 as a timestamp
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def real_date(cls, v: str) -> str:
        date.fromisoformat(v)  # ValueError on dates like 2026-02-30, which pydantic turns into a 422
        return v

    @model_validator(mode="after")
    def shift_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


# no EMP pattern on emp_code here: an unknown employee must reach the endpoint and get a 404, not a 422
class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    # If the client sends extra fields, ignore it. The API should not leak any stored field that is not in the contract.
    model_config = ConfigDict(extra="ignore")

    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_punch_when_absent(self):
        if self.status in ("ABSENT", "LEAVE") and (self.punch_in is not None or self.punch_out is not None):
            raise ValueError("ABSENT and LEAVE can not have punch times")
        return self


# --------------------------------------------------------------------------- #
# Indexes and lifespan
# --------------------------------------------------------------------------- #
def ensure_indexes():
    """Create every index the API needs. create_index is idempotent, so this is safe on every startup."""
    # equality fields first, then sort / range fields
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in_desc")


@asynccontextmanager
async def lifespan(app):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    # no find_one first: the unique index decides, so parallel creates give exactly one 201
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    # `is not None`, not truthiness: ?department= is an exact match on "" and returns nobody
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    items = [employee_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = to_utc(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    start, _ = shift_bounds(d, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes(ts, start),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    # no find_one first: the unique (emp_code, date) index decides, so parallel punch-ins give exactly one 201
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    # typed as date so FastAPI rejects 2026-02-30, "hello", 20260706 with 422
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None and date_to is not None and date_from > date_to:
        # RequestValidationError, not HTTPException: keeps the contract's 422 shape (detail is a list)
        raise RequestValidationError([{"loc": ["query", "date_from"],
                                       "msg": "date_from must be on or before date_to",
                                       "type": "value_error"}])
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        # dates are stored as YYYY-MM-DD strings, which compare correctly as text
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            q["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cursor = (db.attendance_logs.find(q)
              .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
              .skip((page - 1) * page_size)
              .limit(page_size))
    items = [record_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
