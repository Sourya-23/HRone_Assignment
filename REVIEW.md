# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | line 22/mongo client | client is created without `tz_aware=True`, so datetimes read back from mongo are naive (utc value but no zone) | read sample record 1, `punch_in` comes back as `datetime(2026, 7, 6, 3, 58)` with `tzinfo=None`; any math with an aware datetime throws `TypeError` and the api gives 500 | |
| 2 | whole file/startup | no indexes created at all: no unique on `employees.emp_code`, no unique on `attendance_logs (emp_code, date)`, nothing for list sorting or analytics | `/admin/explain` shows COLLSCAN; parallel duplicate creates or punch-ins both go through; list endpoints are slow on 100k records | |
| 3 | line 22/mongo client | no server selection timeout, so if mongo is down every call waits for pymongo's default 30 s | put a wrong `MONGO_URI`, `/health` hangs around 30 s instead of giving 503 quickly | |
| 4 | line 36-37/compute late minutes | grace period is checked in whole minutes, so anything up to 10:59 after shift start counts as on time; R2 says strictly more than 10:00 | shift 09:30, punch 09:40:01 IST returns 0, should be 10 (same for 09:40:30) | |
| 5 | line 35/compute late minutes | shift start is built with `punch_in.replace(...)`, so 09:30 is set in whatever zone punch_in is in; on a utc value that is 15:00 IST | punch 09:28 IST (03:58 UTC) as utc datetime gives start 09:30 UTC and a difference of -332 min; every real late punch is miscalculated | |
| 6 | line 35/compute late minutes | overnight shifts not handled; for shift 22:00-06:00 a punch at 01:00 belongs to the previous day's shift (R1), so late should count from previous day's 22:00 | shift 22:00, punch 01:00 IST returns 0, should be 180 | |
| 7 | line 33-37/compute late minutes | instants are not truncated to whole seconds before calculation (R1) | punch 09:40:00.900 is treated as later than 09:40:00 and gets stored with milliseconds | |
| 8 | line 41/computing work hours | uses python float `round()` and not half-up rounding to 2 decimals (R4) | in 09:00:00, out 17:02:42 (28,962 s) gives 8.04, half-up gives 8.05; about 1,200 durations in a 24 h range come out different | |
| 9 | line 41/computing work hours | half day (R5) is decided from this value (`< 4.50`), so wrong rounding flips borderline records | a duration whose true half-up value is 4.50 but `round()` gives 4.49 gets marked half day | |
| 10 | line 47/compute overtime | no 30 minute minimum; R3 counts overtime only when it is at least 30 min | shift end 18:30, punch out 18:45 IST returns 15, should be 0 | |
| 11 | line 46/compute overtime | for overnight shifts the shift end is on the next calendar day (R1) but it is built on the same date | shift 22:00-06:00 dated 2026-07-06, punch out 06:40 IST on 07-07 returns 1480, should be 40 | |
| 12 | line 47/compute overtime | `end` is aware (IST) but a punch_out read from mongo is naive, so the subtraction crashes | `TypeError: can't subtract offset-naive and offset-aware datetimes`, api gives 500 | |
| 13 | line 53-60/EmployeeIn model | no validation at all, none of the contract constraints are checked | these all give 201 instead of 422: `emp_code: "abc"` (must match `^EMP\d{4,6}$`), `email: "not-an-email"`, empty or 101+ char `name`, empty or 51+ char `department`, `shift_start: "25:99"`, `shift_start == shift_end` (both "09:00"), `joined_on: "2026-02-30"` or `"yesterday"` | |
| 14 | line 65/PunchInIn model | `punched_at` is plain `int`, so pydantic lax mode accepts strings and whole floats | `{"punched_at": "1783312500000"}` gives 201, should be 422 | |
| 15 | line 65/PunchInIn model | `punched_at` has no range check; values below 100000000000 (look like seconds) or above 4102444800000 must be 422 | `{"punched_at": 1783312500}` (seconds) is accepted and the record lands in 1970 | |
| 16 | line 66/PunchInIn model | `status` accepts any string; punch-in only allows `PRESENT`, `WFH`, `ON_DUTY` | `{"status": "ABSENT"}` or `{"status": "banana"}` gives 201, should be 422 | |
| 17 | line 72-74/get health | never pings mongo; contract says 200 only if mongo answers, else 503 | wrong `MONGO_URI`, `/health` still returns 200 | |
| 18 | line 79-83/post employee | check-then-insert race, two parallel requests can both pass `find_one` and both insert | fire 10 identical creates in parallel, get more than one 201 and duplicate `emp_code` docs | |
| 19 | line 82/post employee | `created_at` uses `datetime.now()`, which is server local time with no zone, and mongo stores it as if it was utc | on an IST machine created_at is stored 5.5 h in the future | |
| 20 | line 85/post employee | `created_at` is returned as a datetime, contract says all instants are integer epoch ms | response shows `"created_at": "2026-10-10T20:47:00.123"` instead of `1791...000` | |
| 21 | line 93/get employee | off by one pagination, `skip = page * page_size` but page starts at 1 (R10) | `page=1` returns employees 21 to 40; with fewer than 21 employees page 1 is empty | |
| 22 | line 94/get employee | `total` uses `count_documents({})` so it ignores the department filter (R10) | `?department=Engineering` with 2 engineers out of 6 employees shows `total: 6` | |
| 23 | line 95/get employee | no sorting applied; contract says ascending `emp_code` and mongo natural order is not guaranteed | insert EMP0009 then EMP0002, list returns them in insert order | |
| 24 | line 89/get employee | no bounds on `page` / `page_size`; page min 1, page_size 1 to 100 (R10) | `page=0`, `page_size=0` or `page_size=1000` give 200 and `page=-1` gives a negative skip (500); all should be 422 | |
| 25 | line 95/get employee | `created_at` in list items is not returned as epoch ms (same as 20) | list items show iso strings for `created_at` | |
| 26 | line 101, 113/attendance punch-in | unknown employee is not checked, `emp` is None and `emp["shift_start"]` throws `TypeError` | `{"emp_code": "EMP9999"}` gives 500, should be 404 | |
| 27 | line 102/attendance punch-in | `datetime.fromtimestamp` uses the server's local time zone, so the result depends on the machine | 1783310280000 gives 09:28 on an IST machine and 03:58 on a utc machine; works on my mac, wrong on the grader | |
| 28 | line 102/attendance punch-in | default when `punched_at` is missing is `datetime.now()`, also local and naive (same problem as 27) | omit `punched_at` on a non-utc machine, stored punch_in is off by the local offset | |
| 29 | line 103/attendance punch-in | attendance date is `ts.date()`, which is the server local date, not the IST date, and ignores the overnight rule (R1) | punch 20:30 UTC on 07-06 (02:00 IST 07-07) for a day shift is filed under 07-06 on a utc server; overnight shift 22:00-06:00 with punch 01:00 IST on 07-07 must be dated 07-06 | |
| 30 | line 104-118/attendance punch-in | check-then-insert race, two parallel punch-ins both pass `find_one`; contract wants exactly one 201 and the rest 409 | 10 parallel identical punch-ins give several 201s and duplicate (emp_code, date) records | |
| 31 | line 109/attendance punch-in | bad `status` value gets stored as is (cause is 16) | punch in with `"status": "banana"`, record is saved with that status | |
| 32 | line 119/attendance punch-in | response has an `id` field; contract says attendance records have no id and `_id` never shows | response contains `"id": "6707..."` | |
| 33 | line 121/attendance punch-in | `punch_in` is returned as a datetime, not epoch ms | response shows an iso string for `punch_in` | |
| 34 | line 110/attendance punch-in | stored `punch_in` keeps milliseconds; R1 says the truncated value is stored and returned | punched_at 1783310280900 is stored as `...:00.900` | |
| 35 | line 144/get attendance | `list(find(q))` with no limit loads every matching doc into memory, then sorts and pages in python; contract says it must stay fast on ~100k records | seed 100k records and call with no filters, response time and memory spike; explain shows COLLSCAN | |
| 36 | line 145/get attendance | sort is only on `date`, the `emp_code` ascending tiebreak is missing | two records on the same date come back in random order, pages can overlap or skip rows | |
| 37 | line 149/get attendance | `_id` is exposed as `id` (same as 32) | every list item has an `"id"` key | |
| 38 | line 136-141/get attendance | `date_from` after `date_to` is not rejected | `?date_from=2026-07-10&date_to=2026-07-01` gives 200 with no items, should be 422 | |
| 39 | line 127-128/get attendance | dates are not validated | `?date_from=hello` or `?date_from=2026-13-45` gives 200, should be 422 | |
| 40 | line 129/get attendance | `status` filter is not validated against the five statuses | `?status=banana` gives 200 with empty items, should be 422 | |
| 41 | line 130-131/get attendance | no bounds on `page` / `page_size` (same as 24) | `page=0` or `page_size=1000` give 200, should be 422 | |
| 42 | line 150/get attendance | instants are not converted to epoch ms, including inside history (`punch_in`, `punch_out`, `history[].at`, `history[].changes.punch_in/punch_out.from/to`) | list the regularized sample record (EMP0001, 2026-07-07), nested dates come back as iso strings | |
| 43 | line 150/get attendance | legacy records missing `half_day` / `history` (and `late_minutes` / `overtime_minutes`) are returned as is; contract needs every field, missing means `false` / `[]` / 0 | list EMP0002 on 2026-07-14 (sample 9), response has no `half_day` or `history` keys | |

Also note anything you looked at and decided was **not** a defect, and why.

- **line 36/`int()` vs `floor()` in late minutes:** same result for non-negative values, and negative values already map to 0, so this is not a bug on its own.
- **line 20/`load_dotenv()`:** default is `override=False`, so real environment variables already win over `.env`.
- **line 139-141/`$gte` / `$lte` on `YYYY-MM-DD` strings:** this format sorts correctly as plain text, so string range queries are fine.
- **line 25/`IST = timezone(timedelta(hours=5, minutes=30))`:** correct, india has no daylight saving.
- **line 84/`doc.pop("_id", None)` in post employee:** correct, `insert_one` adds `_id` to the dict, so it has to be removed before returning.
- **line 77, 99/`status_code=201`** on create employee and punch-in: matches the contract.
- **line 65/`punched_at` being optional:** contract says it defaults to now, so optional is right; the problems there are 14, 15 and 28.
- **line 46/`datetime.fromisoformat(date_str)` in compute overtime:** parsing `YYYY-MM-DD` this way is fine; the bugs there are 10 to 12.
- **line 41/null punch_out in work hours:** the helper is never called before a punch-out exists in the starter, so a null punch_out can't reach it; the real problems there are 8 and 9.
