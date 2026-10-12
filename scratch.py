# race_test.py  (scratch file, don't commit)
from concurrent.futures import ThreadPoolExecutor
import httpx

body = {"emp_code": "EMP7777", "name": "Race", "email": "r@x.io",
        "department": "Test", "joined_on": "2026-01-05"}
with ThreadPoolExecutor(10) as ex:
    codes = list(ex.map(lambda _: httpx.post("http://localhost:8000/employees", json=body).status_code, range(10)))
print(sorted(codes))   # expect [201, 409, 409, 409, 409, 409, 409, 409, 409, 409]