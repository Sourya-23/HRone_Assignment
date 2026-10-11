import os
from dotenv import load_dotenv
from pymongo import MongoClient
load_dotenv()

for tz in (False, True):
    db = MongoClient(os.environ["MONGO_URI"], tz_aware=tz)[os.environ["MONGO_DB"]]
    doc = db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    print(f"tz_aware={tz}:", repr(doc["punch_in"]))