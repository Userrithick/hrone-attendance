from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest
from pymongo.errors import ConnectionFailure

from app import main
from conftest import employee_doc, ms


def create(api, **kwargs):
    result = api.post("/employees", json=employee_doc(**kwargs))
    assert result.status_code == 201, result.text
    return result.json()


def punch(api, at="2026-07-06T09:30:00+05:30", code="EMP0001", **extra):
    return api.post("/attendance/punch-in", json=dict(emp_code=code, punched_at=ms(at), **extra))


def out(api, at="2026-07-06T18:30:00+05:30", code="EMP0001"):
    return api.post("/attendance/punch-out", json=dict(emp_code=code, punched_at=ms(at)))


def patch(api, **changes):
    return api.patch("/attendance/EMP0001/2026-07-06", json=dict(reason="clock correction", regularized_by="hr") | changes)


def test_health_and_unavailable(api, monkeypatch):
    assert api.get("/health").json() == {"status": "ok"}
    def unavailable(*args, **kwargs):
        raise ConnectionFailure("unavailable")
    monkeypatch.setattr(main.db, "command", unavailable)
    assert api.get("/health").status_code == 503


@pytest.mark.parametrize("change", [
    {"emp_code": "0001"}, {"emp_code": "EMP123"}, {"emp_code": "EMP1234567"},
    {"email": "bad@"}, {"email": "a b@c.com"}, {"name": ""}, {"name": "x" * 101},
    {"department": ""}, {"department": "x" * 51}, {"shift_start": "9:30"},
    {"shift_end": "24:00"}, {"shift_start": "18:30"}, {"joined_on": "2026-02-30"},
    {"joined_on": "2026-2-03"}, {"joined_on": "2026-01-01T00:00:00"},
])
def test_employee_validation(api, change):
    assert api.post("/employees", json=employee_doc() | change).status_code == 422


def test_employee_identity_sort_filter_and_duplicate(api):
    a = create(api, code="EMP0003")
    create(api, code="EMP0001")
    create(api, code="EMP0002", department="Sales")
    assert isinstance(a["created_at"], int) and "_id" not in a
    assert api.post("/employees", json=employee_doc()).status_code == 409
    page = api.get("/employees", params=dict(department="Engineering", page_size=1)).json()
    assert page["total"] == 2 and page["items"][0]["emp_code"] == "EMP0001"
    page = api.get("/employees", params=dict(department="Engineering", page_size=1, page=2)).json()
    assert page["items"][0]["emp_code"] == "EMP0003"
    assert api.get("/employees", params=dict(department="engineering")).json()["total"] == 0
    assert api.get("/employees", params=dict(department="")).json()["total"] == 0


@pytest.mark.parametrize("value", [1783312500, 1783312500000.0, "1783312500000", True, None, -1, 4102444800001])
def test_strict_timestamps(api, value):
    create(api)
    for route in ("punch-in", "punch-out"):
        assert api.post("/attendance/" + route, json=dict(emp_code="EMP0001", punched_at=value)).status_code == 422
    for field in ("punch_in", "punch_out"):
        assert patch(api, **{field: value}).status_code == 422


@pytest.mark.parametrize("at,late", [("09:40:00.999", 0), ("09:40:01", 10), ("10:15:59", 45), ("09:00:00", 0)])
def test_late_grace_and_truncation(api, at, late):
    create(api)
    response = punch(api, "2026-07-06T" + at + "+05:30", work_hours=99, half_day=True)
    assert response.status_code == 201
    row = response.json()
    assert row["late_minutes"] == late and row["punch_in"] % 1000 == 0
    assert row["work_hours"] is None and row["half_day"] is False and row["history"] == []
    assert "id" not in row and "_id" not in row and "_rev" not in row


@pytest.mark.parametrize("at,hours,half,overtime", [
    ("13:59:41", 4.49, True, 0), ("13:59:42", 4.50, False, 0),
    ("18:59:59", 9.50, False, 0), ("19:00:00", 9.50, False, 30),
    ("19:10:59", 9.68, False, 40),
])
def test_work_half_up_half_day_overtime(api, at, hours, half, overtime):
    create(api)
    punch(api)
    response = out(api, "2026-07-06T" + at + "+05:30")
    assert response.status_code == 200, response.text
    row = response.json()
    assert (row["work_hours"], row["half_day"], row["overtime_minutes"]) == (hours, half, overtime)
    assert row["history"] == []


def test_overnight_and_early_morning_date(api):
    create(api, shift_start="22:00", shift_end="06:00")
    row = punch(api, "2026-07-07T00:05:00+05:30").json()
    assert row["date"] == "2026-07-06" and row["late_minutes"] == 125
    response = out(api, "2026-07-07T06:40:00+05:30")
    assert response.status_code == 200
    assert response.json()["overtime_minutes"] == 40
    row = punch(api, "2026-07-07T06:00:00+05:30").json()
    assert row["date"] == "2026-07-07"


def test_errors_and_punchout_selects_latest_even_if_closed(api):
    assert punch(api).status_code == 404
    assert out(api).status_code == 404
    create(api)
    assert out(api).status_code == 404
    assert punch(api, status="ABSENT").status_code == 422
    assert punch(api).status_code == 201
    assert punch(api).status_code == 409
    assert out(api, "2026-07-06T09:30:00.999+05:30").status_code == 422
    assert out(api, "2026-07-07T09:30:01+05:30").status_code == 422
    assert out(api, "2026-07-06T09:29:59+05:30").status_code == 404
    assert out(api, "2026-07-07T09:30:00+05:30").status_code == 200
    assert out(api, "2026-07-07T09:30:00+05:30").status_code == 409


def test_omitted_time_and_other_presence_status(api):
    create(api)
    response = api.post("/attendance/punch-in", json=dict(emp_code="EMP0001", status="WFH"))
    assert response.status_code == 201
    assert response.json()["punch_in"] % 1000 == 0
    create(api, code="EMP0002")
    assert punch(api, code="EMP0002", status="ON_DUTY").status_code == 201


def test_correction_recomputes_and_nested_bson_serialization(api, database):
    create(api)
    punch(api, "2026-07-06T10:05:00+05:30")
    out(api)
    result = patch(api, punch_in=ms("2026-07-06T09:28:00.999+05:30"), late_minutes=999)
    assert result.status_code == 200, result.text
    row = result.json()
    assert row["late_minutes"] == 0 and row["work_hours"] == 9.03
    changes = row["history"][0]["changes"]
    assert set(changes) == {"punch_in", "late_minutes", "work_hours"}
    assert changes["late_minutes"] == {"from": 35, "to": 0}
    assert changes["punch_in"]["to"] == ms("2026-07-06T09:28:00+05:30")
    stored = database.attendance_logs.find_one({})
    assert isinstance(stored["history"][0]["changes"]["punch_in"]["to"], datetime)
    assert isinstance(stored["punch_in"], datetime)
    assert patch(api, status="WFH").status_code == 200
    assert len(api.get("/attendance").json()["items"][0]["history"]) == 2


def test_correction_clears_times_and_legacy_defaults(api, database):
    create(api)
    punch(api)
    database.attendance_logs.update_one({}, {"$unset": {"history": "", "half_day": ""}})
    legacy = api.get("/attendance").json()["items"][0]
    assert legacy["history"] == [] and legacy["half_day"] is False
    result = patch(api, status="LEAVE")
    assert result.status_code == 200
    row = result.json()
    assert row["punch_in"] is None and row["punch_out"] is None and row["late_minutes"] == 0
    assert row["work_hours"] is None and row["half_day"] is False
    assert patch(api, status="PRESENT").status_code == 422
    assert patch(api, status="PRESENT", punch_in=ms("2026-07-06T09:30:00+05:30")).status_code == 200


@pytest.mark.parametrize("changes", [
    {}, {"status": "PRESENT"}, {"status": None}, {"status": "OTHER"},
    {"status": "ABSENT", "punch_in": ms("2026-07-06T09:00:00+05:30")},
    {"punch_in": ms("2026-07-07T09:00:00+05:30")},
    {"punch_out": ms("2026-07-06T09:30:00+05:30")},
    {"punch_out": ms("2026-07-07T09:30:01+05:30")},
    {"reason": "no"}, {"regularized_by": ""},
])
def test_invalid_corrections_do_not_write(api, database, changes):
    create(api)
    punch(api)
    before = database.attendance_logs.find_one({})
    assert patch(api, **changes).status_code == 422
    assert database.attendance_logs.find_one({}) == before


@pytest.mark.parametrize("url,params", [
    ("/employees", {"page": 0}), ("/employees", {"page_size": 101}),
    ("/attendance", {"page_size": 0}), ("/attendance", {"status": "BAD"}),
    ("/attendance", {"date_from": "2026-02-30"}),
    ("/attendance", {"date_from": "2026-08-01", "date_to": "2026-07-01"}),
    ("/analytics/departments/summary", {"month": "0000-01"}),
    ("/analytics/departments/summary", {"month": "2026-13"}),
    ("/analytics/leaderboard/late", {"month": "2026-07", "limit": 51}),
    ("/analytics/departments/Engineering/trend", {"from": "2026-07-02", "to": "2026-07-01"}),
    ("/analytics/departments/Engineering/trend", {"from": "2026-01-01", "to": "2026-04-03"}),
    ("/admin/explain/employee_monthly", {"month": "2026-07"}),
    ("/admin/explain/department_summary", {}), ("/admin/explain/department_trend", {"department": "Engineering"}),
    ("/admin/explain/unknown", {}),
])
def test_query_validation(api, url, params):
    assert api.get(url, params=params).status_code == 422


def test_attendance_pagination_order_filter(api):
    for code in ("EMP0003", "EMP0001", "EMP0002"):
        create(api, code=code)
        punch(api, code=code)
    punch(api, "2026-07-07T09:30:00+05:30")
    page = api.get("/attendance", params=dict(page_size=2)).json()
    assert [(r["date"], r["emp_code"]) for r in page["items"]] == [("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001")]
    page = api.get("/attendance", params=dict(emp_code="EMP0001", date_to="2026-07-06", status="PRESENT")).json()
    assert page["total"] == 1


def run_parallel(action, n=8):
    barrier = Barrier(n)
    def task(i):
        barrier.wait(timeout=10)
        return action(i)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(task, range(n)))


def test_concurrent_employee_punchin_punchout(api, database):
    codes = run_parallel(lambda i: api.post("/employees", json=employee_doc()).status_code)
    assert sorted(codes) == [201] + [409] * 7
    codes = run_parallel(lambda i: punch(api).status_code)
    assert sorted(codes) == [201] + [409] * 7
    codes = run_parallel(lambda i: out(api).status_code)
    assert sorted(codes) == [200] + [409] * 7
    assert database.attendance_logs.count_documents({}) == 1
    assert database.attendance_logs.find_one({})["history"] == []


def test_concurrent_corrections_keep_each_successful_history(api, database):
    create(api)
    punch(api)
    out(api)
    results = run_parallel(lambda i: patch(api, punch_out=ms(f"2026-07-06T19:{i:02d}:00+05:30")))
    successes = [r for r in results if r.status_code == 200]
    assert successes and all(r.status_code in (200, 409) for r in results)
    stored = database.attendance_logs.find_one({})
    assert len(stored["history"]) == len(successes)
    history = stored["history"]
    for left, right in zip(history, history[1:]):
        assert left["changes"]["punch_out"]["to"] == right["changes"]["punch_out"]["from"]


def test_stale_revision_is_rejected_including_legacy(api, database):
    create(api)
    punch(api)
    old = database.attendance_logs.find_one({})
    patch(api, status="WFH")
    with pytest.raises(main.HTTPException) as error:
        main.replace_record(old, {"status": "ON_DUTY"})
    assert error.value.status_code == 409
    assert database.attendance_logs.find_one({})["status"] == "WFH"


def test_punchout_does_not_fall_back_to_older_open_record(api, database):
    create(api)
    punch(api, "2026-07-05T09:30:00+05:30")
    punch(api)
    assert out(api).status_code == 200
    assert out(api).status_code == 409
    assert database.attendance_logs.find_one({"date": "2026-07-05"})["punch_out"] is None


def test_missing_records_and_invalid_path_date(api):
    assert patch(api, status="WFH").status_code == 404
    create(api)
    assert patch(api, status="WFH").status_code == 404
    assert api.patch("/attendance/EMP0001/2026-02-30", json=dict(reason="wrong date", regularized_by="hr")).status_code == 422


def test_subsecond_only_correction_is_noop(api, database):
    create(api)
    punch(api)
    assert patch(api, punch_in=ms("2026-07-06T09:30:00.999+05:30")).status_code == 422
    assert database.attendance_logs.find_one({})["history"] == []


@pytest.mark.parametrize("value", [100000000000, 4102444800000])
def test_timestamp_inclusive_bounds(api, value):
    create(api)
    result = api.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=value))
    assert result.status_code == 201 and result.json()["punch_in"] == value
