# Verification report

Verified locally on 12 October 2026 using Python 3.12 and a real, isolated MongoDB 7.0.16 server on Windows. Tests use PyMongo and FastAPI's HTTP test client; no MongoDB mock or aggregation emulator is used.

## Results

- **94 tests passed**, including the opt-in 100,000-record test; final full run took **30.34 seconds**.
- **12 API operations** exposed by the generated OpenAPI document.
- Normal `uvicorn app.main:app --port 8000` startup returned healthy within **2.390 seconds**, below the 20-second requirement.
- Startup created four application indexes per collection plus MongoDB's default `_id` index. Repeated creation was exercised throughout the tests.
- `pip check` reported no broken dependency requirements.
- One upstream Starlette warning reports that its httpx-based test client is deprecated. Tests still execute successfully with the pinned httpx dependency; this warning does not affect the production API.

## Coverage

- Valid and invalid employee bodies, real calendar dates, shifts, strict integer timestamps and inclusive timestamp bounds.
- IST attendance days, overnight shifts and the exact overnight cutoff; second truncation; grace, overtime and rounded half-day boundaries.
- Unknown resources, closed records, 24-hour duration limit, latest-record selection and no fallback to an older open record.
- Actual-change audit diffs, nested BSON/JSON timestamps, absence clearing times, invalid/no-op corrections and legacy missing fields.
- Filtered pagination, stable ordering and validation of endpoint and explain parameters.
- Eight concurrent employee creations, punch-ins, punch-outs and corrections. Exactly one creation/closure succeeds; every successful correction survives in an ordered history chain. Stale legacy revision writes are explicitly rejected.
- Independent Python reference calculations for all four analytics endpoints, including deterministic randomized data. Production analytics themselves remain entirely in MongoDB.
- Zero-log employees, future and mid-month joiners, orphan logs, record-weighted averages, half-up ties, weekend records, entirely empty ranges, competition-rank cutoff ties and shortened moving windows.
- Real `executionStats` for all five explain targets and filter variants: `IXSCAN` present, no `COLLSCAN`, and all reported lookup `collectionScans` counters zero.

## 100,000-record test

Dataset: 1,000 synthetic employees, each with 100 daily records. July contains 31,000 logs; expected department presence is 23,000 weekdays. All 1,000 employees tie at rank 1 and are returned even with `limit=1`.

Approximate final executionStats HTTP request times, including local test-client overhead:

| Operation | Seconds |
|---|---:|
| Attendance list | 0.004 |
| Employee monthly | 0.009 |
| Department summary, all departments | 1.025 |
| Department summary, filtered | 0.903 |
| Late leaderboard | 0.351 |
| Department trend, 31 days | 1.010 |

These are observations on this machine, not performance guarantees. The final trend pipeline fetches each employee's date range once and expands the calendar inside MongoDB. This replaced per-employee/per-day lookups that took about 20 seconds on the same fixture.

## Limits and reproduction

Use the commands in README.md. By default, only the expensive scale test is skipped; set `RUN_SCALE_TESTS=1` to run everything. MongoDB is required: its absence is a test failure, not a silent skip. Each test cleans up only its own uniquely named database.

The runtime was tested on Python 3.12 and MongoDB 7.0.16. The code targets Python 3.11+ and MongoDB 6.0+, but those exact lower versions were not run here. The employer's hidden tests are unavailable. Correctness tests and local timings cannot guarantee a particular hiring outcome.
