"""Load-test data: ~100,000 attendance records in a SEPARATE database (default: attendance_load).

Usage (from the repo root, venv active):
    python tools/seed_100k.py              # writes to MONGO_URI / database "attendance_load"
    python tools/seed_100k.py --dry-run    # builds the documents and prints stats, writes nothing

Then start the app against that database:
    MONGO_DB=attendance_load uvicorn app.main:app --port 8000
The app's startup creates the indexes, exactly like on the grader's machine.

Shapes follow DATA_MODEL.md. Derived values are plausible, not exact: this data is for
plans and speed, not for checking analytics numbers (the sample data is for that).
Includes the awkward cases: overnight shifts, mid-month joiners, employees with no logs,
weekend records, open records, ABSENT/LEAVE, half days, legacy records without
half_day/history, and a few regularized records with history.
"""
import argparse
import os
import random
import time
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

IST = timezone(timedelta(hours=5, minutes=30))
DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "HR", "Operations", "Marketing", "Legal"]
N_EMPLOYEES = 460            # 440 get logs, 20 never do (headcount without logs)
START = date(2025, 10, 1)    # about a year of history
END = date(2026, 9, 30)


def hm(s):
    h, m = map(int, s.split(":"))
    return h, m


def build(seed=42):
    rnd = random.Random(seed)
    employees, logs = [], []
    for i in range(1, N_EMPLOYEES + 1):
        overnight = i % 10 == 0
        shift_start, shift_end = ("22:00", "06:00") if overnight else rnd.choice(
            [("09:30", "18:30"), ("09:00", "18:00"), ("10:00", "19:00")])
        # most joined before the window; ~15% join mid-window on a random day
        joined = START - timedelta(days=rnd.randint(30, 900)) if rnd.random() > 0.15 else \
            START + timedelta(days=rnd.randint(0, 330))
        employees.append({
            "emp_code": f"EMP{i:05d}", "name": f"Employee {i}", "email": f"emp{i}@example.com",
            "department": DEPARTMENTS[i % len(DEPARTMENTS)], "shift_start": shift_start, "shift_end": shift_end,
            "joined_on": joined.isoformat(),
            "created_at": datetime(joined.year, joined.month, joined.day, 4, 30, tzinfo=timezone.utc),
        })

    with_logs = employees[:440]
    per_emp = 250                       # fewer for mid-window joiners; ~100k in total
    for e in with_logs:
        sh, sm = hm(e["shift_start"]); eh, em = hm(e["shift_end"])
        overnight = e["shift_end"] <= e["shift_start"]
        first = max(START, date.fromisoformat(e["joined_on"]))
        days = [first + timedelta(days=k) for k in range((END - first).days + 1)]
        weekdays = [d for d in days if d.weekday() < 5]
        weekends = [d for d in days if d.weekday() >= 5]
        chosen = sorted(set(rnd.sample(weekdays, min(len(weekdays), per_emp - 3)) +
                            rnd.sample(weekends, min(len(weekends), 3))))
        for d in chosen:
            r = rnd.random()
            status = "PRESENT" if r < 0.62 else "WFH" if r < 0.80 else "ON_DUTY" if r < 0.86 else \
                "ABSENT" if r < 0.93 else "LEAVE"
            doc = {"emp_code": e["emp_code"], "date": d.isoformat(), "status": status}
            if status in ("ABSENT", "LEAVE"):
                doc.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0,
                           overtime_minutes=0, half_day=False, history=[])
            else:
                start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
                end_day = d + timedelta(days=1) if overnight else d
                end = datetime(end_day.year, end_day.month, end_day.day, eh, em, tzinfo=IST)
                pin = start + timedelta(seconds=rnd.choice([-1200, -300, 0, 400, 601, 900, 2100, 3700]))
                late_s = int((pin - start).total_seconds())
                late = late_s // 60 if late_s > 600 else 0
                if rnd.random() < 0.02 and d >= END - timedelta(days=3):
                    pout = None                                  # open record near the end
                else:
                    worked = rnd.choice([3.2, 4.495, 7.5, 8.75, 9.0, 9.12, 9.5, 10.25]) * 3600
                    pout = pin + timedelta(seconds=int(worked))
                doc.update(punch_in=pin.astimezone(timezone.utc), late_minutes=late, history=[])
                if pout is None:
                    doc.update(punch_out=None, work_hours=None, overtime_minutes=0, half_day=False)
                else:
                    wh = round(int((pout - pin).total_seconds()) / 3600 + 1e-9, 2)
                    ot = int((pout - end).total_seconds()) // 60
                    doc.update(punch_out=pout.astimezone(timezone.utc), work_hours=wh,
                               overtime_minutes=ot if ot >= 30 else 0, half_day=wh < 4.5)
                if rnd.random() < 0.01:                         # a regularized record
                    doc["history"] = [{"at": doc["punch_in"] + timedelta(days=1), "by": "hr.admin",
                                       "reason": "load-test correction",
                                       "changes": {"late_minutes": {"from": late + 5, "to": late}}}]
            if rnd.random() < 0.03:                             # legacy record
                doc.pop("history", None); doc.pop("half_day", None)
            logs.append(doc)
    return employees, logs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default="attendance_load")
    args = ap.parse_args()
    t = time.time()
    employees, logs = build()
    keys = {(l["emp_code"], l["date"]) for l in logs}
    print(f"built {len(employees)} employees, {len(logs)} logs in {time.time() - t:.1f}s "
          f"(unique emp/date: {len(keys) == len(logs)}, weekend logs: "
          f"{sum(date.fromisoformat(l['date']).weekday() >= 5 for l in logs)}, "
          f"open: {sum(l.get('punch_in') is not None and l.get('punch_out') is None for l in logs)}, "
          f"legacy: {sum('history' not in l for l in logs)})")
    if args.dry_run:
        return
    load_dotenv()
    if args.db == os.getenv("MONGO_DB"):
        raise SystemExit(f"refusing to overwrite your main database '{args.db}'; pick another --db")
    db = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[args.db]
    db.employees.drop(); db.attendance_logs.drop()
    db.employees.insert_many(employees)
    t = time.time()
    for i in range(0, len(logs), 5000):
        db.attendance_logs.insert_many(logs[i:i + 5000], ordered=False)
        print(f"  inserted {min(i + 5000, len(logs))}/{len(logs)}", end="\r")
    print(f"\ninserted into '{args.db}' in {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()