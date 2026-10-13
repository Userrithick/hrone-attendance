# Employee Attendance & Analytics API

FastAPI + PyMongo implementation of the HROne v2.0 contract. All application code is in `app/main.py`. Requires Python 3.11+ and a running MongoDB 6.0+ server; verified locally on MongoDB 7.0.16.

## Run

Create a virtual environment and install `requirements.txt`. Set `MONGO_URI` and `MONGO_DB` in your shell, then start from this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://127.0.0.1:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

On macOS/Linux, activate with `source .venv/bin/activate` and set variables with `export MONGO_URI=...` / `export MONGO_DB=...`.

Open `http://localhost:8000/docs` to try the API; `GET /health` checks MongoDB. Indexes are created automatically at startup. Environment variables override an optional local `.env`; no `.env` is included. The app makes no external service calls.

## Test

```powershell
python -m pip install -r requirements-dev.txt
$env:TEST_MONGO_URI = "mongodb://127.0.0.1:27017"
python -m pytest -q
$env:RUN_SCALE_TESTS = "1"
python -m pytest -q -s
```

Tests require real MongoDB. They create and remove uniquely named `attendance_test_*` databases, leaving the application database untouched. The scale test is opt-in and inserts 100,000 synthetic attendance records. See [TESTING.md](TESTING.md) for results and limits.

## Design and scope

All requested endpoints are implemented. Analytics use stored metrics and MongoDB pipelines. Unique indexes protect creation; conditional atomic updates protect punch-out and correction history. UTC BSON dates are converted to integer milliseconds at the HTTP boundary; shifts use IST and explicit half-up rounding.

[REVIEW.md](REVIEW.md) lists starter defects, [DECISIONS.md](DECISIONS.md) answers the five assignment questions, and [INTERVIEW.md](INTERVIEW.md) explains the code and its tradeoffs. One contract edge: a punch-out earlier than every punch-in returns 404 because no record satisfies the selection rule; equality returns 422. Trend calendars are generated with `$range`/`$map`/`$unwind` inside MongoDB, including entirely empty periods.

No authentication is added because it is outside this assignment. Audit `by` is client-supplied text. This implementation and its documentation were prepared with AI assistance. Review and understand the code before submitting it; the interview guide explains the implementation and its tradeoffs.
