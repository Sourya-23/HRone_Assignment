"""Checks the explain endpoint (IXSCAN, no COLLSCAN) and times every read endpoint.

Run against the app started on the load-test database:
    MONGO_DB=attendance_load uvicorn app.main:app --port 8000      (terminal 1)
    python tools/check_explain.py                                   (terminal 2)

Data ids/dates match tools/seed_100k.py. Atlas round-trips add latency, so compare
endpoints with each other rather than reading the numbers as absolute.
"""
import json
import statistics
import time

import httpx

BASE = "http://localhost:8000"
M = {"month": "2026-07"}
EXPLAIN_CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP00001"}),
    ("attendance_list", {"status": "LEAVE"}),
    ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("attendance_list", {"emp_code": "EMP00001", "date_from": "2026-07-01", "date_to": "2026-07-31", "status": "WFH"}),
    ("attendance_list", {"page": 50, "page_size": 100}),
    ("employee_monthly", {"emp_code": "EMP00001", **M}),
    ("department_summary", M),
    ("department_summary", {**M, "department": "Engineering"}),
    ("late_leaderboard", M),
    ("late_leaderboard", {**M, "department": "Sales", "limit": 5}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-09-30"}),
]
MUST_422 = [
    ("/admin/explain/bogus", {}),
    ("/admin/explain/employee_monthly", {"emp_code": "EMP00001"}),          # month missing
    ("/admin/explain/department_trend", {"department": "Engineering"}),     # from/to missing
]
TIMED = [
    ("/attendance", {}),
    ("/attendance", {"date_from": "2026-07-01", "date_to": "2026-07-31", "page": 20}),
    ("/employees", {"department": "Engineering"}),
    ("/analytics/employees/EMP00001/monthly", M),
    ("/analytics/departments/summary", M),
    ("/analytics/leaderboard/late", M),
    ("/analytics/departments/Engineering/trend", {"from": "2026-07-01", "to": "2026-09-30"}),
]


def main():
    c = httpx.Client(base_url=BASE, timeout=60)
    ok = True
    print("== explain: IXSCAN present, COLLSCAN absent ==")
    for name, params in EXPLAIN_CASES:
        r = c.get(f"/admin/explain/{name}", params=params)
        if r.status_code != 200:
            print(f"  FAIL {name} {params} -> HTTP {r.status_code}: {r.text[:200]}"); ok = False; continue
        body = r.json()
        missing = {"endpoint", "collection", "explain"} - set(body)
        text = json.dumps(body.get("explain"))
        good = "IXSCAN" in text and "COLLSCAN" not in text and not missing
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'} {name:20} {params}  IXSCAN={'IXSCAN' in text} "
              f"COLLSCAN={'COLLSCAN' in text} collection={body.get('collection')}"
              + (f" missing keys {missing}" if missing else ""))
    print("== explain: bad requests must be 422 ==")
    for path, params in MUST_422:
        r = c.get(path, params=params)
        good = r.status_code == 422
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'} {path} {params} -> {r.status_code}")
    print("== timing (median of 3, includes the Atlas round trip) ==")
    for path, params in TIMED:
        times, status = [], None
        for _ in range(3):
            t = time.perf_counter(); r = c.get(path, params=params); times.append((time.perf_counter() - t) * 1000)
            status = r.status_code
        med = statistics.median(times)
        flag = "" if status == 200 and med < 2000 else "   <-- look at this"
        print(f"  {status} {med:7.0f} ms  {path} {params}{flag}")
    print("\nALL EXPLAIN CHECKS PASSED" if ok else "\nSOME CHECKS FAILED (see FAIL lines)")


if __name__ == "__main__":
    main()