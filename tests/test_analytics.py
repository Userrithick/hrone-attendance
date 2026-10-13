"""Real aggregation results compared with a deliberately separate Python oracle."""
import calendar
import json
import os
import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import pytest

from conftest import employee_doc

PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def rounding(value, places=2):
    return float(Decimal(str(value)).quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP))


def fraction(numerator, denominator, places=2):
    if not denominator:
        return None
    return rounding(Decimal(str(numerator)) / Decimal(str(denominator)), places)


def days_between(first, last):
    d = date.fromisoformat(first)
    while d.isoformat() <= last:
        yield d.isoformat()
        d += timedelta(days=1)


def weight(log):
    return (0.5 if log.get("half_day", False) else 1) if log["status"] in PRESENT else 0


def is_weekday(day):
    return date.fromisoformat(day).weekday() < 5


def late(log):
    return log.get("late_minutes", 0)


def seed(database):
    employees = [employee_doc("EMP0001"), employee_doc("EMP0002", joined="2026-07-13"),
                 employee_doc("EMP0003"), employee_doc("EMP0004", department="Sales"),
                 employee_doc("EMP0005", department="Sales"),
                 employee_doc("EMP0006", department="Empty"),
                 employee_doc("EMP0007", department="Future", joined="2026-08-01"),
                 employee_doc("EMP0008", joined="2026-08-01"),
                 employee_doc("EMP0009", department="$literal")]
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    database.employees.insert_many([e | {"created_at": now} for e in employees])
    # Punch times intentionally disagree with stored metrics: analytics must trust the metrics.
    fixtures = [
        ("EMP0001", "2026-07-01", "PRESENT", 2.345, 50, 30, False),
        ("EMP0001", "2026-07-04", "WFH", 4.00, 50, 31, True),
        ("EMP0001", "2026-07-06", "ON_DUTY", None, 0, 0, False),
        ("EMP0001", "2026-07-07", "LEAVE", None, 0, 0, False),
        ("EMP0002", "2026-07-13", "WFH", 3.50, 60, 0, True),
        ("EMP0003", "2026-07-01", "PRESENT", 8.00, 60, 0, False),
        ("EMP0003", "2026-07-02", "PRESENT", 8.00, 0, 0, False),
        ("EMP0003", "2026-07-03", "ABSENT", None, 0, 0, False),
        ("EMP0004", "2026-07-01", "ON_DUTY", 2.345, 25, 40, False),
        ("EMP0005", "2026-07-02", "PRESENT", None, 0, 0, False),
        ("EMP0009", "2026-07-01", "PRESENT", 9.0, 5, 0, False),
        ("ORPHAN", "2026-07-01", "PRESENT", 9.0, 999, 0, False),
        ("EMP0001", "2026-06-30", "PRESENT", 9.0, 999, 0, False),
        ("EMP0001", "2026-08-01", "PRESENT", 9.0, 999, 0, False),
    ]
    logs = [dict(emp_code=c, date=d, status=s, work_hours=h, late_minutes=l,
                 overtime_minutes=o, half_day=half, punch_in=now, punch_out=now)
            for c, d, s, h, l, o, half in fixtures]
    del logs[5]["half_day"]
    database.attendance_logs.insert_many(logs)
    return employees, logs


def oracle_month(employee, logs, month):
    year, number = map(int, month.split("-"))
    days = list(days_between(month + "-01", f"{month}-{calendar.monthrange(year, number)[1]}"))
    records = [l for l in logs if l["emp_code"] == employee["emp_code"] and l["date"].startswith(month)]
    working = sum(is_weekday(d) and d >= employee["joined_on"] for d in days)
    present = sum(weight(l) for l in records if is_weekday(l["date"]))
    return dict(emp_code=employee["emp_code"], month=month, working_days=working, present_days=present,
                leave_days=sum(l["status"] == "LEAVE" for l in records),
                late_count=sum(late(l) > 0 for l in records), total_late_minutes=sum(late(l) for l in records),
                total_overtime_minutes=sum(l.get("overtime_minutes", 0) for l in records),
                attendance_pct=fraction(present * 100, working))


def oracle_summary(employees, logs, month, department=None):
    end = month + "-" + str(calendar.monthrange(*map(int, month.split("-")))[1])
    eligible = [e for e in employees if e["joined_on"] <= end and (department is None or e["department"] == department)]
    items = []
    for name in sorted({e["department"] for e in eligible}):
        members = [e for e in eligible if e["department"] == name]
        codes = {e["emp_code"] for e in members}
        records = [l for l in logs if l["emp_code"] in codes and l["date"].startswith(month)]
        hours = [Decimal(str(l["work_hours"])) for l in records if l["status"] in PRESENT and l.get("work_hours") is not None]
        items.append(dict(department=name, headcount=len(members),
            present_days=sum(weight(l) for l in records if is_weekday(l["date"])),
            avg_work_hours=fraction(sum(hours), len(hours)), late_count=sum(late(l) > 0 for l in records),
            total_late_minutes=sum(late(l) for l in records if late(l) > 0),
            leave_count=sum(l["status"] == "LEAVE" for l in records),
            on_duty_count=sum(l["status"] == "ON_DUTY" for l in records)))
    return dict(month=month, items=items)


def oracle_leaderboard(employees, logs, month, department=None, limit=10):
    items = []
    for employee in employees:
        if department is not None and employee["department"] != department:
            continue
        records = [l for l in logs if l["emp_code"] == employee["emp_code"] and l["date"].startswith(month)]
        total = sum(late(l) for l in records)
        if total:
            items.append(dict(emp_code=employee["emp_code"], name=employee["name"], department=employee["department"],
                              total_late_minutes=total, late_count=sum(late(l) > 0 for l in records)))
    items.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))
    for item in items:
        item["rank"] = 1 + sum(other["total_late_minutes"] > item["total_late_minutes"] for other in items)
    return dict(month=month, items=[i for i in items if i["rank"] <= limit])


def oracle_trend(employees, logs, department, first, last):
    items = []
    for day in days_between(first, last):
        codes = {e["emp_code"] for e in employees if e["department"] == department and e["joined_on"] <= day}
        records = [l for l in logs if l["date"] == day and l["emp_code"] in codes]
        present = sum(weight(l) for l in records)
        row = dict(date=day, headcount=len(codes), present_count=present,
                   is_working_day=is_weekday(day), late_count=sum(late(l) > 0 for l in records),
                   attendance_rate=fraction(present, len(codes), 4) if is_weekday(day) else None)
        rates = [r["attendance_rate"] for r in items[-6:] + [row] if r["attendance_rate"] is not None]
        row["moving_avg_7d"] = fraction(sum(Decimal(str(r)) for r in rates), len(rates), 4)
        items.append(row)
    return dict(department=department, items=items)


def get(api, url, **params):
    result = api.get(url, params=params)
    assert result.status_code == 200, result.text
    return result.json()


def test_monthly_summary_stored_values_and_empty_month(api, database):
    employees, logs = seed(database)
    for employee in employees:
        for month in ("2026-07", "2026-02"):
            actual = get(api, f"/analytics/employees/{employee['emp_code']}/monthly", month=month)
            assert actual == oracle_month(employee, logs, month)
    assert api.get("/analytics/employees/UNKNOWN/monthly?month=2026-07").status_code == 404


def test_department_weighted_average_headcount_and_half_up(api, database):
    employees, logs = seed(database)
    for department in (None, "Engineering", "Sales", "Future", "Empty", "$literal", ""):
        params = {} if department is None else {"department": department}
        assert get(api, "/analytics/departments/summary", month="2026-07", **params) == oracle_summary(employees, logs, "2026-07", department)
    assert get(api, "/analytics/departments/summary", month="2026-07", department="Sales")["items"][0]["avg_work_hours"] == 2.35


def test_rank_ties_cutoff_and_department_filter_before_rank(api, database):
    employees, logs = seed(database)
    for department in (None, "Engineering", "Sales", "Empty"):
        for limit in (1, 2, 3, 4, 50):
            params = {} if department is None else {"department": department}
            actual = get(api, "/analytics/leaderboard/late", month="2026-07", limit=limit, **params)
            assert actual == oracle_leaderboard(employees, logs, "2026-07", department, limit)
    assert len(get(api, "/analytics/leaderboard/late", month="2026-07", limit=2)["items"]) == 3


@pytest.mark.parametrize("department,first,last", [
    ("Engineering", "2026-07-01", "2026-07-20"), ("Engineering", "2026-07-10", "2026-07-15"),
    ("Empty", "2026-07-01", "2026-07-31"), ("Future", "2026-07-01", "2026-07-31"),
    ("Future", "2026-07-31", "2026-08-03"), ("Sales", "2026-07-04", "2026-07-05"),
    ("$literal", "2026-07-01", "2026-07-01"), ("Engineering", "2026-01-01", "2026-04-02"),
])
def test_trend_gap_filling_rates_and_window(api, database, department, first, last):
    employees, logs = seed(database)
    actual = get(api, f"/analytics/departments/{department}/trend", **{"from": first, "to": last})
    assert actual == oracle_trend(employees, logs, department, first, last)


def test_empty_database_and_unknown_department(api):
    assert get(api, "/analytics/departments/summary", month="2026-07")["items"] == []
    assert get(api, "/analytics/leaderboard/late", month="2026-07")["items"] == []
    assert api.get("/analytics/departments/X/trend?from=2026-07-01&to=2026-07-01").status_code == 404


def test_randomized_all_analytics_against_independent_oracle(api, database):
    rng = random.Random(71026)
    employees = [employee_doc(f"EMP{i:04d}", department=["A", "B", "C"][i % 3],
                              joined=["2026-01-01", "2026-07-13", "2026-08-01"][i % 3]) for i in range(1, 19)]
    database.employees.insert_many([e | {"created_at": datetime.now(timezone.utc)} for e in employees])
    logs = []
    for e in employees[:-2]:
        for day in days_between("2026-07-01", "2026-07-31"):
            if day < e["joined_on"] or rng.random() < 0.35:
                continue
            status = rng.choice(["PRESENT", "WFH", "ON_DUTY", "LEAVE", "ABSENT"])
            logs.append(dict(emp_code=e["emp_code"], date=day, status=status,
                work_hours=rng.choice([None, 2.345, 3.0, 4.50, 8.01, 9.25]) if status in PRESENT else None,
                late_minutes=rng.choice([0, 0, 10, 30, 60]), overtime_minutes=rng.choice([0, 30, 45]),
                half_day=rng.choice([True, False])))
    database.attendance_logs.insert_many(logs)
    for e in employees:
        assert get(api, f"/analytics/employees/{e['emp_code']}/monthly", month="2026-07") == oracle_month(e, logs, "2026-07")
    assert get(api, "/analytics/departments/summary", month="2026-07") == oracle_summary(employees, logs, "2026-07")
    assert get(api, "/analytics/leaderboard/late", month="2026-07", limit=3) == oracle_leaderboard(employees, logs, "2026-07", limit=3)
    for department in ("A", "B", "C"):
        assert get(api, f"/analytics/departments/{department}/trend", **{"from": "2026-07-01", "to": "2026-07-31"}) == oracle_trend(employees, logs, department, "2026-07-01", "2026-07-31")


EXPLAINS = [
    ("attendance_list", {}),
    ("attendance_list", {"status": "WFH", "date_from": "2026-07-01", "date_to": "2026-07-31", "page": 2, "page_size": 3}),
    ("attendance_list", {"emp_code": "EMP0001", "page_size": 1}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Engineering"}),
    ("late_leaderboard", {"month": "2026-07", "limit": 2}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}),
]


def assert_indexed(explanation):
    plan = json.dumps(explanation)
    assert "IXSCAN" in plan and "COLLSCAN" not in plan
    def inspect(value):
        if isinstance(value, dict):
            if "collectionScans" in value:
                assert value["collectionScans"] == 0
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)
    inspect(explanation)


@pytest.mark.parametrize("endpoint,params", EXPLAINS)
def test_real_explain_index_scans(api, database, endpoint, params):
    seed(database)
    result = get(api, "/admin/explain/" + endpoint, **params)
    assert result["endpoint"] == endpoint
    assert_indexed(result["explain"])
    plan = json.dumps(result["explain"])
    assert '"executionStats"' in plan
    if endpoint == "attendance_list":
        command = result["explain"]["command"]
        assert command["skip"] == (params.get("page", 1) - 1) * params.get("page_size", 20)
        assert command["limit"] == params.get("page_size", 20)


@pytest.mark.scale
@pytest.mark.skipif(os.getenv("RUN_SCALE_TESTS") != "1", reason="set RUN_SCALE_TESTS=1 to seed 100,000 records")
def test_100k_records_indexes_and_correct_results(api, database):
    from time import perf_counter
    now = datetime.now(timezone.utc)
    employees = [employee_doc(f"EMP{i:04d}") | {"created_at": now} for i in range(1, 1001)]
    database.employees.insert_many(employees)
    # 1,000 employees x 100 days. Derived values intentionally have no punch timestamps.
    days = [(date(2026, 5, 1) + timedelta(days=i)).isoformat() for i in range(100)]
    logs = [dict(emp_code=e["emp_code"], date=d, status="PRESENT", work_hours=8.0,
                 late_minutes=10, overtime_minutes=0) for e in employees for d in days]
    database.attendance_logs.insert_many(logs)
    assert database.attendance_logs.count_documents({}) == 100000
    durations = {}
    for endpoint, params in EXPLAINS:
        start = perf_counter()
        result = get(api, "/admin/explain/" + endpoint, **params)
        assert_indexed(result["explain"])
        durations[endpoint + str(params)] = round(perf_counter() - start, 3)
    monthly = get(api, "/analytics/employees/EMP0001/monthly", month="2026-07")
    assert monthly["working_days"] == monthly["present_days"] == 23
    assert monthly["total_late_minutes"] == 310
    summary = get(api, "/analytics/departments/summary", month="2026-07")["items"][0]
    assert summary["headcount"] == 1000 and summary["present_days"] == 23000
    assert summary["avg_work_hours"] == 8 and summary["total_late_minutes"] == 310000
    board = get(api, "/analytics/leaderboard/late", month="2026-07", limit=1)["items"]
    assert len(board) == 1000 and all(r["rank"] == 1 for r in board)
    trend = get(api, "/analytics/departments/Engineering/trend", **{"from": "2026-07-01", "to": "2026-07-31"})["items"]
    assert len(trend) == 31 and all(r["headcount"] == r["present_count"] == 1000 for r in trend)
    assert all(r["attendance_rate"] == (1.0 if r["is_working_day"] else None) for r in trend)
    print("\n100k executionStats request durations (seconds): " + json.dumps(durations, sort_keys=True))
