"""Attendance contract v2.0. Run: uvicorn app.main:app --port 8000."""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Decimal128_HALF = Decimal128("0.5")
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                     tz_aware=True, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def create_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True, name="employee_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="department_code")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="joined_department")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="department_joined")
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)],
                                    unique=True, name="employee_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_employee")
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)],
                                    name="status_date_employee")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="employee_punch")


@asynccontextmanager
async def lifespan(app):
    create_indexes()
    try:
        yield
    finally:
        client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def date_string(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("expected YYYY-MM-DD")
    Date.fromisoformat(value)
    return value


def month_string(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("expected YYYY-MM")
    Date.fromisoformat(value + "-01")
    return value


DateString = Annotated[str, AfterValidator(date_string)]
MonthString = Annotated[str, AfterValidator(month_string)]
Shift = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
Millis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: Shift = "09:30"
    shift_end: Shift = "18:30"
    joined_on: DateString

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    # An omitted timestamp defaults to now; explicitly supplied null is invalid.
    punched_at: Millis = Field(default_factory=lambda: epoch_ms(datetime.now(UTC)))


class PunchInIn(PunchOutIn):
    status: PresenceStatus = "PRESENT"


class RegularizeIn(BaseModel):
    status: Status = None
    punch_in: Millis = None
    punch_out: Millis = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def invalid(message):
    raise HTTPException(422, message)


def instant(ms):
    return EPOCH + timedelta(seconds=ms // 1000)


def epoch_ms(value):
    return (value - EPOCH) // timedelta(milliseconds=1)


def to_api(value):
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, dict):
        return {k: to_api(v) for k, v in value.items() if k not in ("_id", "_rev")}
    if isinstance(value, list):
        return [to_api(v) for v in value]
    return value


DEFAULTS = dict(punch_in=None, punch_out=None, work_hours=None,
                late_minutes=0, overtime_minutes=0, half_day=False, history=[])
FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes",
          "overtime_minutes", "half_day")


def record_api(doc):
    return to_api(DEFAULTS | doc)


def employee(code):
    result = db.employees.find_one({"emp_code": code})
    if result is None:
        raise HTTPException(404, "employee not found")
    return result


def attendance_date(ts, emp):
    local = ts.astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.time() < time.fromisoformat(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(day, emp):
    start = datetime.combine(Date.fromisoformat(day), time.fromisoformat(emp["shift_start"]), IST)
    end = datetime.combine(Date.fromisoformat(day), time.fromisoformat(emp["shift_end"]), IST)
    if emp["shift_end"] <= emp["shift_start"]:
        end += timedelta(days=1)
    return start, end


def compute_work_hours(punch_in, punch_out):
    seconds = (punch_out - punch_in) // timedelta(seconds=1)
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive(doc, emp):
    if doc["status"] not in PRESENCE:
        return dict(punch_in=None, punch_out=None, work_hours=None,
                    late_minutes=0, overtime_minutes=0, half_day=False)
    pin, pout = doc.get("punch_in"), doc.get("punch_out")
    if pin is None:
        invalid("presence status requires punch_in")
    # Apply second truncation to old records as well as new request values.
    pin = pin.replace(microsecond=0)
    pout = pout.replace(microsecond=0) if pout else None
    if attendance_date(pin, emp) != doc["date"]:
        invalid("punch_in must stay on the attendance date")
    start, end = shift_bounds(doc["date"], emp)
    late_seconds = (pin - start) // timedelta(seconds=1)
    result = dict(punch_in=pin, punch_out=pout, late_minutes=late_seconds // 60 if late_seconds > 600 else 0,
                  work_hours=None, overtime_minutes=0, half_day=False)
    if pout is not None:
        if not timedelta(0) < pout - pin <= timedelta(hours=24):
            invalid("punch_out must be after punch_in and within 24 hours")
        hours = compute_work_hours(pin, pout)
        overtime = (pout - end) // timedelta(minutes=1)
        result.update(work_hours=hours, half_day=hours < 4.50,
                      overtime_minutes=overtime if overtime >= 30 else 0)
    return result


def replace_record(old, values, history=None):
    # Compare-and-swap protects derived fields and history in one document write.
    query = {"_id": old["_id"], "_rev": old["_rev"] if "_rev" in old else {"$exists": False}}
    update = {"$set": values, "$inc": {"_rev": 1}}
    if history is not None:
        update["$push"] = {"history": history}
    result = db.attendance_logs.find_one_and_update(query, update, return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "record changed concurrently; read it again before retrying")
    return record_api(result)


@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump() | {"created_at": datetime.now(UTC).replace(microsecond=0)}
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return to_api(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    items = list(db.employees.find(query).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    return dict(items=to_api(items), total=db.employees.count_documents(query), page=page, page_size=page_size)


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    doc = dict(emp_code=body.emp_code, date=attendance_date(ts, emp), status=body.status,
               punch_in=ts, punch_out=None, history=[])
    doc.update(derive(doc, emp))
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "attendance already exists for this date") from None
    return record_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    old = db.attendance_logs.find_one({"emp_code": body.emp_code,
                                      "punch_in": {"$type": "date", "$lte": ts}}, sort=[("punch_in", -1)])
    if old is None:
        raise HTTPException(404, "no punch-in found at or before punched_at")
    if old.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    return replace_record(old, derive(old | {"punch_out": ts}, emp))


def attendance_query(emp_code, date_from, date_to, status):
    if date_from and date_to and date_from > date_to:
        invalid("date_from must not exceed date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    hint = "employee_date" if emp_code is not None else ("status_date_employee" if status is not None else "date_employee")
    return q, hint


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: DateString | None = None,
                    date_to: DateString | None = None, status: Status | None = None,
                    page: Page = 1, page_size: PageSize = 20):
    q, hint = attendance_query(emp_code, date_from, date_to, status)
    docs = db.attendance_logs.find(q).hint(hint).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    return dict(items=[record_api(d) for d in docs], total=db.attendance_logs.count_documents(q, hint=hint),
                page=page, page_size=page_size)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateString, body: RegularizeIn):
    emp = employee(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if old is None:
        raise HTTPException(404, "attendance record not found")
    supplied = body.model_fields_set
    if body.status in ("ABSENT", "LEAVE") and supplied & {"punch_in", "punch_out"}:
        invalid("ABSENT/LEAVE cannot be supplied with punch times")
    final = DEFAULTS | old
    for field in ("status", "punch_in", "punch_out"):
        if field in supplied:
            value = getattr(body, field)
            final[field] = instant(value) if field.startswith("punch_") else value
    final.update(derive(final, emp))
    previous = DEFAULTS | old
    changes = {f: {"from": previous[f], "to": final[f]} for f in FIELDS if previous[f] != final[f]}
    if not changes:
        invalid("correction changes nothing")
    history = dict(at=datetime.now(UTC).replace(microsecond=0), by=body.regularized_by,
                   reason=body.reason, changes=changes)
    return replace_record(old, {f: final[f] for f in FIELDS}, history)


# Aggregation expression builders. No attendance analytics are calculated in Python.
def literal(value):
    return {"$literal": value}


def rounded(value, places=2):
    """Half-up for nonnegative metrics; MongoDB $round uses half-even."""
    scale = 10 ** places
    return {"$toDouble": {"$divide": [
        {"$floor": {"$add": [{"$multiply": [{"$toDecimal": value}, scale]}, Decimal128_HALF]}}, scale]}}


def weekday(day):
    return {"$in": [{"$dayOfWeek": {"$toDate": day}}, [2, 3, 4, 5, 6]]}


def presence_weight():
    return {"$cond": [{"$in": ["$status", PRESENCE]},
                      {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def log_stats():
    late = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
    worked = {"$and": [{"$in": ["$status", PRESENCE]}, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": {"$cond": [weekday("$date"), presence_weight(), 0]}},
        "present_count": {"$sum": presence_weight()},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [late, 1, 0]}},
        "total_late_minutes": {"$sum": {"$cond": [late, "$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "work_sum": {"$sum": {"$cond": [worked, {"$toDecimal": "$work_hours"}, 0]}},
        "work_count": {"$sum": {"$cond": [worked, 1, 0]}},
    }}


def month_bounds(month):
    year, number = map(int, month.split("-"))
    return month + "-01", f"{month}-{calendar.monthrange(year, number)[1]:02d}"


def logs_lookup(first, last):
    return {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                        "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}}, log_stats()], "as": "stats"}}


def stat(field):
    return {"$ifNull": [{"$arrayElemAt": ["$stats." + field, 0]}, 0]}


def calendar_days(first, count):
    return {"$map": {"input": {"$range": [0, count]}, "as": "offset", "in": {
        "$dateToString": {"format": "%Y-%m-%d", "date": {"$dateAdd": {
            "startDate": {"$toDate": literal(first)}, "unit": "day", "amount": "$$offset"}}}}}}


def monthly_pipeline(emp_code, month):
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code}}, logs_lookup(first, last),
        {"$set": {"working_days": {"$size": {"$filter": {
            "input": calendar_days(first, int(last[-2:])), "as": "day",
            "cond": {"$and": [{"$gte": ["$$day", "$joined_on"]}, weekday("$$day")]}}}},
                  "present_days": stat("present_days")}},
        {"$project": {"_id": 0, "emp_code": 1, "month": literal(month), "working_days": 1, "present_days": 1,
                      "leave_days": stat("leave_count"), "late_count": stat("late_count"),
                      "total_late_minutes": stat("total_late_minutes"), "total_overtime_minutes": stat("total_overtime_minutes"),
                      "attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]},
                          rounded({"$divide": [{"$multiply": [{"$toDecimal": "$present_days"}, 100]}, "$working_days"]}), None]}}},
    ]


def summary_pipeline(month, department):
    first, last = month_bounds(month)
    query = {"joined_on": {"$lte": last}}
    if department is not None:
        query["department"] = department
    sums = {f: {"$sum": stat(f)} for f in ("present_days", "late_count", "total_late_minutes", "leave_count", "on_duty_count", "work_sum", "work_count")}
    return [{"$match": query}, logs_lookup(first, last),
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}, **sums}},
            {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
                **{f: 1 for f in ("present_days", "late_count", "total_late_minutes", "leave_count", "on_duty_count")},
                "avg_work_hours": {"$cond": [{"$gt": ["$work_count", 0]},
                    rounded({"$divide": ["$work_sum", "$work_count"]}), None]}}},
            {"$sort": {"department": 1}}]


def leaderboard_pipeline(month, department, limit):
    first, last = month_bounds(month)
    pipeline = [{"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
                {"$match": {"total_late_minutes": {"$gt": 0}}},
                {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"}]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    return pipeline + [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department",
                      "rank": 1, "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}]


def trend_pipeline(department, first, last):
    days = (Date.fromisoformat(last) - Date.fromisoformat(first)).days + 1
    if not 1 <= days <= 92:
        invalid("trend range must contain 1 to 92 days")
    # Read each employee's range once, then expand a bounded calendar in MongoDB.
    # Keep future joiners as calendar seeds, but give them zero weight before joining.
    return [
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}},
                         {"$group": {"_id": "$date", "present_count": {"$sum": presence_weight()},
                             "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}],
                     "as": "stats"}},
        {"$project": {"_id": 0, "joined_on": 1, "stats": 1, "date": calendar_days(first, days)}},
        {"$unwind": "$date"},
        {"$set": {"daily": {"$arrayElemAt": [{"$filter": {"input": "$stats", "as": "s",
                      "cond": {"$eq": ["$$s._id", "$date"]}}}, 0]},
                  "eligible": {"$lte": ["$joined_on", "$date"]}}},
        {"$group": {"_id": "$date", "headcount": {"$sum": {"$cond": ["$eligible", 1, 0]}},
                    "present_count": {"$sum": {"$cond": ["$eligible", {"$ifNull": ["$daily.present_count", 0]}, 0]}},
                    "late_count": {"$sum": {"$cond": ["$eligible", {"$ifNull": ["$daily.late_count", 0]}, 0]}}}},
        {"$project": {"_id": 0, "date": "$_id", "headcount": 1, "present_count": 1,
                      "late_count": 1, "is_working_day": weekday("$_id")}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                   rounded({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4), None]}}},
        {"$set": {"decimal_rate": {"$toDecimal": "$attendance_rate"}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {
            "$avg": "$decimal_rate", "window": {"documents": [-6, "current"]}}}}},
        {"$set": {"moving_avg_7d": rounded("$moving_avg_7d", 4)}},
        {"$project": {"decimal_rate": 0}}, {"$sort": {"date": 1}},
    ]


def analytics_spec(endpoint, *, emp_code=None, month=None, department=None, limit=10, first=None, last=None):
    if endpoint != "department_trend" and month is None:
        invalid("month is required")
    if endpoint == "employee_monthly":
        if emp_code is None:
            invalid("emp_code is required")
        return "employees", monthly_pipeline(emp_code, month), "employee_code"
    if endpoint == "department_summary":
        return "employees", summary_pipeline(month, department), "department_joined" if department is not None else "joined_department"
    if endpoint == "late_leaderboard":
        return "attendance_logs", leaderboard_pipeline(month, department, limit), "date_employee"
    if first is None or last is None or department is None:
        invalid("department, from and to are required")
    return "employees", trend_pipeline(department, first, last), "department_joined"


def aggregate(spec):
    collection, pipeline, hint = spec
    return list(db[collection].aggregate(pipeline, hint=hint, allowDiskUse=True))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthString):
    rows = aggregate(analytics_spec("employee_monthly", emp_code=emp_code, month=month))
    if not rows:
        raise HTTPException(404, "employee not found")
    return rows[0]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthString, department: str | None = None):
    return {"month": month, "items": aggregate(analytics_spec("department_summary", month=month, department=department))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthString, department: str | None = None, limit: Limit = 10):
    return {"month": month, "items": aggregate(analytics_spec("late_leaderboard", month=month, department=department, limit=limit))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, first: Annotated[DateString, Query(alias="from")], last: Annotated[DateString, Query(alias="to")]):
    rows = aggregate(analytics_spec("department_trend", department=department, first=first, last=last))
    if not rows:
        raise HTTPException(404, "department not found")
    return {"department": department, "items": rows}


Endpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Endpoint, emp_code: str | None = None, month: MonthString | None = None,
                     department: str | None = None, limit: Limit = 10, date_from: DateString | None = None,
                     date_to: DateString | None = None, status: Status | None = None,
                     first: Annotated[DateString | None, Query(alias="from")] = None,
                     last: Annotated[DateString | None, Query(alias="to")] = None,
                     page: Page = 1, page_size: PageSize = 20):
    if endpoint == "attendance_list":
        query, hint = attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {"find": collection, "filter": query, "sort": {"date": -1, "emp_code": 1},
                   "skip": (page - 1) * page_size, "limit": page_size, "hint": hint}
    else:
        collection, pipeline, hint = analytics_spec(endpoint, emp_code=emp_code, month=month,
            department=department, limit=limit, first=first, last=last)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint, "allowDiskUse": True}
    result = db.command("explain", command, verbosity="executionStats")
    # Preserve the raw plan, including BSON-specific values as Extended JSON.
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}
