"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
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
    """Epoch ms -> UTC datetime. I use // so the milliseconds get dropped, R1 wants whole seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)

def to_ms(dt) -> int:
    """UTC datetime -> epoch ms, because the API only talks in milliseconds."""
    return int(dt.timestamp() * 1000)

def attendance_date(punch_in, shift_start, shift_end) -> str:
    """Which day a punch belongs to: its IST date, but an overnight punch before shift end is the day before (R1)."""
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    local = punch_in.astimezone(IST)
    d = local.date()
    overnight = time(h_end, m_end) <= time(h_start, m_start)
    if overnight and local.time() < time(h_end, m_end):
        d -= timedelta(days=1)
    return d.isoformat()

def shift_bounds(date_str, shift_start, shift_end) -> tuple[datetime, datetime]:
    """Start and end of the shift in UTC. For an overnight shift I push the end to the next day."""
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    start = datetime.fromisoformat(date_str).replace(hour=h_start, minute=m_start, tzinfo=IST)
    end = datetime.fromisoformat(date_str).replace(hour=h_end, minute=m_end, tzinfo=IST)
    if end <= start:
        end += timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

def late_minutes(punch_in, start) -> int:
    """Late only if more than 10 minutes (600 s) after shift start, then I count whole minutes from the start (R2)."""
    delta = punch_in - start
    return max(0, int(delta.total_seconds() // 60)) if delta.total_seconds() > 600 else 0

def work_hours(punch_in, punch_out) -> float:
    """Hours worked, half-up to 2 decimals. Decimal on whole seconds, float rounding gets .xx5 wrong (R4)."""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)

def overtime_minutes(punch_out, end) -> int:
    """Whole minutes after shift end, but only when it is 30 or more, otherwise 0 (R3)."""
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0

def is_half_day(work_hours) -> bool:
    """Half day when the rounded hours are under 4.5 (R5)."""
    return work_hours < 4.5

def month_bounds(month) -> tuple[date, date]:
    """First and last day of a YYYY-MM month, e.g. 2026-07 -> 1 Jul and 31 Jul."""
    year, mon = map(int, month.split("-"))
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])

def count_weekdays(start, end) -> int:
    """Counts Mon-Fri days between two dates, both included. Gives 0 if start is after end (R7)."""
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)

def round_half_up(value, places="0.01") -> float:
    """Half-up rounding for the numbers we report. Done in Python because Mongo's $round rounds .5 to even (R8)."""
    return float(Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Starter helpers: the original buggy versions. Nothing calls them, I kept them to point at from REVIEW.md
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
# Serializers: turn a Mongo document into the API response
# I copy only the fields I know about, so _id or any extra stored field can never leak out.
# --------------------------------------------------------------------------- #
EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
TIME_FIELDS = ("punch_in", "punch_out")


def _ms(value):
    """None stays None, a datetime becomes epoch ms. Saves repeating the None check everywhere."""
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
        # old records can miss these or store null; `or` turns both into the default
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

# StrictInt so strings, floats and even True (Python treats it as 1) get a 422
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
ShiftTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    # kept as a string: Mongo stores it as text, and pydantic's date type would accept 20260105 as a timestamp
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def real_date(cls, v: str) -> str:
        date.fromisoformat(v)  # raises on impossible dates like 2026-02-30, which becomes a 422
        return v

    @model_validator(mode="after")
    def shift_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


# no EMP pattern here on purpose: an unknown code should reach the endpoint and get a 404, not a 422
class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    # the contract says to ignore derived fields if a client sends them, so extra fields are dropped quietly
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
    """Creates all indexes at startup. Safe to run every time, create_index does nothing if the index already exists."""
    # exact-match fields first, sort/range fields after, so Mongo can use the whole index
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    # for the department summary: range on joined_on, and the optional department filter uses the same index
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="joined_dept")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in_desc")


@asynccontextmanager
async def lifespan(app):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Query builders
# I build every query here so the endpoint and /admin/explain always run the exact same thing.
# --------------------------------------------------------------------------- #
def _422(loc, msg) -> RequestValidationError:
    """A 422 in the same shape FastAPI uses (detail is a list). HTTPException would make detail a plain string."""
    return RequestValidationError([{"loc": loc, "msg": msg, "type": "value_error"}])


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    """Builds the GET /attendance filter, and rejects date_from after date_to with a 422."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise _422(["query", "date_from"], "date_from must be on or before date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        # dates are stored as YYYY-MM-DD text, which sorts correctly, so a plain string range works
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            q["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        q["status"] = status
    return q


PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

def _is_weekday(date_field) -> dict:
    """True for Mon-Fri. Dates are stored as text, so I convert first ($isoDayOfWeek: 1 = Mon, 7 = Sun)."""
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": date_field}}}, 5]}


def _present_value(prefix="$", weekdays_only=True) -> dict:
    """What a record adds to present days: 1, 0.5 for a half day, else 0. Mon-Fri only unless weekdays_only=False."""
    condition = {"$in": [prefix + "status", PRESENCE_STATUSES]}
    if weekdays_only:
        condition = {"$and": [condition, _is_weekday(prefix + "date")]}
    return {"$cond": [
        condition,
        {"$cond": [{"$eq": [{"$ifNull": [prefix + "half_day", False]}, True]}, 0.5, 1]},
        0,
    ]}


def monthly_pipeline(emp_code, month) -> list:
    first, last = month_bounds(month)
    late = {"$ifNull": ["$late_minutes", 0]}
    return [
        # employee + month as a string range, so it uses the uniq_emp_date index
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_value()},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            # lateness and overtime count on weekends too, only present days skip them (R7)
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": late},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month, department=None) -> list:
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last.isoformat()}}  # only people who had joined by month end (R9)
    if department is not None:
        match["department"] = department
    late = {"$ifNull": ["$logs.late_minutes", 0]}
    has_work_hours = {"$and": [{"$in": ["$logs.status", PRESENCE_STATUSES]}, {"$isNumber": "$logs.work_hours"}]}
    return [
        {"$match": match},
        # pull each employee's logs for the month; emp_code + date range uses uniq_emp_date
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}],
            "as": "logs",
        }},
        # preserveNull keeps employees with no logs, otherwise they would vanish from headcount
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            # after the unwind an employee shows up once per log, so I collect unique codes instead of counting rows
            "emp_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": _present_value("$logs.")},
            # summed as decimals, float addition can break a half-up tie (7.03 + 7.10)
            "work_hours_sum": {"$sum": {"$cond": [has_work_hours, {"$toDecimal": "$logs.work_hours"}, Decimal128("0")]}},
            "work_hours_count": {"$sum": {"$cond": [has_work_hours, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": late},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$emp_codes"},
            "present_days": 1,
            "work_hours_sum": 1,
            "work_hours_count": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month, limit, department=None) -> list:
    first, last = month_bounds(month)
    pipeline = [
        # only late records in the month, which also drops anyone with 0 late minutes; uses date_desc_emp_code
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        # no preserveNull here: logs of an employee that doesn't exist simply drop out
        {"$unwind": "$emp"},
    ]
    if department is not None:
        # filter before ranking so the rank is within the department
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # rank on the total only, so ties share a rank (1, 2, 2, 4). Tie order by emp_code is the $sort below (Mongo 8 doesn't even allow a second sortBy key for $rank)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        # limit is applied to the rank, so everyone tied inside the limit stays
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        # written as expressions so the fields come out in the contract's order
        {"$project": {
            "_id": 0,
            "rank": "$rank",
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": "$total_late_minutes",
            "late_count": "$late_count",
        }},
    ]
    return pipeline


TREND_MAX_DAYS = 92  # counted inclusively: 1 Jul to 30 Sep (a full quarter) is allowed, ending on 1 Oct is not


def check_trend_range(from_, to):
    if to < from_:
        raise _422(["query", "to"], "to must be on or after from")
    if (to - from_).days + 1 > TREND_MAX_DAYS:
        raise _422(["query", "to"], f"the range can be at most {TREND_MAX_DAYS} days")


def department_employees(department) -> list:
    """Everyone in the department with their joined_on. An empty list means the department doesn't exist."""
    return list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))


def _round4_half_up(expr) -> dict:
    """Half-up to 4 decimals in Mongo: floor(x * 10000 + 0.5) / 10000. $round gives 1/32 as 0.0312, not 0.0313."""
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, Decimal128("10000")]}, Decimal128("0.5")]}},
                        Decimal128("10000")]}


def trend_pipeline(employees, from_, to) -> list:
    codes = [e["emp_code"] for e in employees]
    joined = [e["joined_on"] for e in employees]
    start = datetime(from_.year, from_.month, from_.day, tzinfo=timezone.utc)
    end = start + timedelta(days=(to - from_).days + 1)  # +1 day because $densify's upper bound is exclusive
    return [
        # this department's employees in the date range, uses uniq_emp_date
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_.isoformat(), "$lte": to.isoformat()}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": _present_value(weekdays_only=False)},  # weekends count here too
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        # $densify only works on real dates, so I convert the string first
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start, end]}}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$addFields": {
            # headcount per day without a lookup: count the joined_on dates up to this day (R9)
            "headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "as": "j",
                                                "cond": {"$lte": ["$$j", "$date"]}}}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            _round4_half_up({"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}),
            None,
        ]}}},
        # rates are already rounded, so the moving average is the mean of the rates we show.
        # $avg skips nulls, and only rows inside the range exist, so nothing before `from` gets in.
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}},
        }}},
        {"$project": {
            "_id": 0,
            "date": "$date",
            "is_working_day": "$is_working_day",
            "headcount": "$headcount",
            "present_count": "$present_count",
            "late_count": "$late_count",
            "attendance_rate": "$attendance_rate",
            "moving_avg_7d": "$moving_avg_7d",
        }},
    ]


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
    # no find_one first: the unique index decides, so only one of many parallel creates gets 201
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
    # `is not None` on purpose: ?department= should match nobody, not return everyone
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
    # no find_one first: the unique (emp_code, date) index decides, so parallel punch-ins get exactly one 201
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = to_utc(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    # latest punch_in before this time, not today's record, because overnight shifts punch out the next day.ABSENT/LEAVE have no punch_in, so they never match and can't be closed by accident.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_at = record["punch_in"]
    if ts <= punch_in_at:
        raise _422(["body", "punched_at"], "punched_at must be after punch_in")
    if ts - punch_in_at > timedelta(hours=24):
        raise _422(["body", "punched_at"], "punched_at must be within 24 hours of punch_in")
    _, end = shift_bounds(record["date"], emp["shift_start"], emp["shift_end"])
    hours = work_hours(punch_in_at, ts)
    # the real race guard is "punch_out": None in this update, not the check above.
    # Only one parallel punch-out can match it, the others get None back and a 409.
    # (None also matches a missing field, so old records close fine.)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": hours,
            "overtime_minutes": overtime_minutes(ts, end),
            "half_day": is_half_day(hours),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    # typed as date so FastAPI itself rejects 2026-02-30, "hello" or 20260706 with a 422
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (db.attendance_logs.find(q)
              .sort(ATTENDANCE_SORT)
              .skip((page - 1) * page_size)
              .limit(page_size))
    items = [record_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: date, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    day = date.isoformat()
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(404, "no record for that date")

    # work out the final state: the body's value if sent, otherwise keep what's stored
    status = body.status if body.status is not None else record["status"]
    if status in ("ABSENT", "LEAVE"):
        # the model only checks this when status is in the body; this also covers a stored ABSENT plus a new punch_in
        if body.punch_in is not None or body.punch_out is not None:
            raise _422(["body"], "ABSENT and LEAVE can not have punch times")
        punch_in_at = punch_out_at = None
    else:
        punch_in_at = to_utc(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        punch_out_at = to_utc(body.punch_out) if body.punch_out is not None else record.get("punch_out")
        if punch_in_at is None:
            raise _422(["body", "punch_in"], "a presence status needs a punch_in")
        if body.punch_in is not None and attendance_date(punch_in_at, emp["shift_start"], emp["shift_end"]) != day:
            raise _422(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if punch_out_at is not None:
            if punch_out_at <= punch_in_at:
                raise _422(["body", "punch_out"], "punch_out must be after punch_in")
            if punch_out_at - punch_in_at > timedelta(hours=24):
                raise _422(["body", "punch_out"], "punch_out must be within 24 hours of punch_in")

    # recompute every derived field from the final times instead of trusting the old ones (DATA_MODEL.md 3)
    new = {"status": status, "punch_in": punch_in_at, "punch_out": punch_out_at,
           "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if punch_in_at is not None:
        start, end = shift_bounds(day, emp["shift_start"], emp["shift_end"])
        new["late_minutes"] = late_minutes(punch_in_at, start)
        if punch_out_at is not None:
            new["work_hours"] = work_hours(punch_in_at, punch_out_at)
            new["overtime_minutes"] = overtime_minutes(punch_out_at, end)
            new["half_day"] = is_half_day(new["work_hours"])

    # old values with the same defaults record_out uses, so missing fields compare fairly
    old = {k: record.get(k) for k in TRACKED_FIELDS}
    old["late_minutes"] = old["late_minutes"] or 0
    old["overtime_minutes"] = old["overtime_minutes"] or 0
    old["half_day"] = old["half_day"] or False
    changes = {k: {"from": old[k], "to": new[k]} for k in TRACKED_FIELDS if old[k] != new[k]}
    if not changes:
        raise _422(["body"], "the request changes nothing")

    # optimistic lock: history length works as a version between PATCHes, and pinning punch_out catches a punch-out sneaking in. $ifNull because old records have no history field at all.
    entry = {"at": datetime.now(timezone.utc).replace(microsecond=0), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": record.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(record.get("history") or [])]},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by another request, retry")
    return record_out(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    # working days is just calendar math and reads no logs, so plain Python is fine here
    first, last = month_bounds(month)
    working_days = count_weekdays(max(first, date.fromisoformat(emp["joined_on"])), last)

    # no logs that month means no group comes back; that's zeros, not a 404
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)))
    agg = rows[0] if rows else {}
    present_days = agg.get("present_days", 0)
    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up(Decimal(str(present_days)) * 100 / Decimal(working_days))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(month, department)):
        count = row["work_hours_count"]
        # average over records; I divide and round in Python so it stays half-up (R8)
        avg = round_half_up(row["work_hours_sum"].to_decimal() / count) if count else None
        items.append({
            "department": row["department"],
            "headcount": row["headcount"],
            "present_days": row["present_days"],
            "avg_work_hours": avg,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


def _decimal_out(value):
    """Decimal128 -> float so it can go into JSON; None stays None."""
    return None if value is None else float(value.to_decimal())


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: date = Query(alias="from"), to: date = Query()):
    check_trend_range(from_, to)
    employees = department_employees(department)
    if not employees:
        raise HTTPException(404, "department not found")
    items = []
    for row in db.attendance_logs.aggregate(trend_pipeline(employees, from_, to)):
        row["attendance_rate"] = _decimal_out(row["attendance_rate"])
        # mean of the already rounded rates, rounded again to 4 decimals half-up
        avg = row["moving_avg_7d"]
        row["moving_avg_7d"] = None if avg is None else round_half_up(avg.to_decimal(), "0.0001")
        items.append(row)
    return {"department": department, "items": items}


ExplainableEndpoint = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]


def _require(**params):
    """Gives a 422 for the first missing required parameter, in FastAPI's own format."""
    for name, value in params.items():
        if value is None:
            raise RequestValidationError([{"loc": ["query", name], "msg": "Field required", "type": "missing"}])


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: ExplainableEndpoint,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Runs explain on an endpoint's main query, built by the same function the endpoint itself uses."""
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            _require(month=month)
            collection, pipeline = "employees", department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            collection, pipeline = "attendance_logs", leaderboard_pipeline(month, limit, department)
        else:  # department_trend
            _require(department=department, **{"from": from_}, to=to)
            check_trend_range(from_, to)
            employees = department_employees(department)
            if not employees:
                raise HTTPException(404, "department not found")
            collection, pipeline = "attendance_logs", trend_pipeline(employees, from_, to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    raw = db.command("explain", command, verbosity="executionStats")
    # json_util converts Mongo types (dates, Decimal128, ObjectId) into plain JSON
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(raw))}
