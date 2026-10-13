# Starter review

Locations refer to function names in the original kit, not rewritten line numbers. Missing endpoints are distinguished from defects in existing code.

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | MongoClient setup | Default reads produce naive datetimes; host-local time is also used for writes. | Change the host timezone or combine a naive read with an aware shift boundary. | Use timezone-aware UTC reads and writes; explicit IST shift boundaries. |
| 2 | Startup | No unique employee index. | Concurrent duplicate employee creations both succeed. | Create unique `employee_code` at startup; catch duplicate-key errors. |
| 3 | Startup | No unique attendance natural-key index. | Parallel punch-ins create duplicate employee/day rows. | Create unique `employee_date`; translate duplicate-key errors into 409. |
| 4 | Startup/query design | No indexes for attendance lists, month scans, or department queries. | Explain on 100k records shows collection scans. | Create query-specific compound indexes; use shared builders and explicit top-level hints. |
| 5 | `compute_late_minutes` | Replaces UTC/host-local hours with IST shift hours without converting the instant. | 09:40 IST is interpreted relative to the wrong shift. | Construct the shift start from attendance date and IST. |
| 6 | `compute_late_minutes` | Floors minutes before comparing with grace. | 09:40:01 for a 09:30 shift incorrectly returns zero. | Compare elapsed seconds with 600 first; floor only the resulting late minutes. |
| 7 | Helpers/punch-in | Fractional seconds are retained. | A .999 timestamp is stored/returned without truncation. | Truncate input instants before date assignment and calculations. |
| 8 | `compute_work_hours` | Binary floating-point/banker's rounding conflicts with half-up. | 4h29m42s is exactly 4.495h, which must become 4.50. | Integer seconds, Decimal division and ROUND_HALF_UP. |
| 9 | `compute_overtime` | Credits overtime below the 30-minute minimum. | 18:59:59 for an 18:30 end must yield zero. | Floor elapsed minutes and require at least 30. |
| 10 | `compute_overtime` | Overnight shift end is placed on the starting date. | 22:00–06:00 with next-day 06:40 gives excessive overtime. | Shift end advances one day when end <= start. |
| 11 | `EmployeeIn` | Missing code, email, length, real-date and shift validation. | Bad email, EMP123, empty name, Feb 30 or 24:00 are accepted. | Constrained fields and strict calendar validators. |
| 12 | `EmployeeIn` | Equal shift start and end accepted on creation. | Send 09:30 for both. | Cross-field validation rejects equality with 422; seeded overnight rules still support <=. |
| 13 | `PunchInIn` | Arbitrary statuses accepted. | Punch in as LEAVE or an invented status. | Presence-only literal enum. |
| 14 | `PunchInIn`/`punch_in` | Coercive/unbounded timestamps and truthiness default. | Seconds, strings, floats, zero, explicit null or huge integers are accepted or misinterpreted. | Strict bounded integer milliseconds; only omission defaults to now. |
| 15 | `health` | Always reports success without checking MongoDB. | Stop MongoDB after startup; health remains 200. | Ping with bounded connection/server-selection timeouts; return 503 on database errors. |
| 16 | `create_employee` | Check-then-insert is not race-safe. | Synchronize duplicate requests. | Let the unique index arbitrate the insert. |
| 17 | `create_employee` | Creation timestamp uses host-local naive time and is returned as a datetime string. | Check the response type and stored instant on an IST host. | UTC server time, BSON storage, integer epoch-millisecond response. |
| 18 | `list_employees` | Offset is `page * page_size`. | Page 1 skips the first 20 employees. | Use `(page - 1) * page_size`. |
| 19 | `list_employees` | Total ignores department filter. | Query one department in a mixed database. | Count using the same filter. |
| 20 | `list_employees` | No deterministic code ordering. | Insert employees out of order. | MongoDB sort by `emp_code` ascending. |
| 21 | Both list endpoints | Pagination bounds are not validated. | page=0, page_size=0 or 101. | Query constraints for page >= 1 and size 1–100. |
| 22 | `punch_in` | Unknown employee is dereferenced. | Unknown code causes a 500. | Central employee lookup raises 404. |
| 23 | `punch_in` | Attendance day uses host-local calendar and ignores overnight reassignment. | UTC evening falls on next IST day; 00:05 overnight belongs to previous day. | Apply R1 in IST, including strict earlier-than-end rule. |
| 24 | `punch_in` | Duplicate attendance pre-check races. | Eight parallel requests for the same employee/day. | Atomic unique insert; exactly one 201. |
| 25 | `punch_in`/`list_attendance` | Invents public `id` fields; timestamps use default datetime serialization. | Response exposes an ObjectId string and ISO instants. | Remove internal keys, convert all nested datetime values to milliseconds. |
| 26 | `list_attendance` | Reads and sorts the entire matching collection in Python. | Large collection increases memory and response time. | Database filter, indexed sort, skip/limit and filtered count. |
| 27 | `list_attendance` | Date-only sort leaves tied employees unordered. | Multiple employees on the same day. | Sort date descending, code ascending. |
| 28 | `list_attendance` | Date/status formats and reversed ranges are not rejected. | Invalid status, Feb 30, or from > to. | Literal enum, real date parsing and range validation. |
| 29 | `list_attendance` | Legacy missing fields remain missing. | Seed a row without history/half_day. | Normalize documented defaults in responses without mutating storage on reads. |
| 30 | Department/employee filters | Truthiness ignores an explicitly supplied empty string. | `department=` returns all employees. | Distinguish None (omitted) from exact supplied text. |

## Missing functionality completed

- Punch-out: latest eligible punch-in across midnight, closed-record conflict, positive duration <=24h, derived values, conditional atomic update.
- Corrections: final-state validation, absence clears times, immutable attendance date, recomputation, actual-change diff, no-op rejection, nested timestamp conversion and atomic history append.
- Employee monthly, department summary, late leaderboard and department trend: MongoDB aggregations over stored metrics, zero-log employees, weekday/half-day rules, weighted record averages, competition ranks and in-range seven-row moving windows.
- Explain: real executionStats for the same builders, filters, sorting and pagination used by the endpoint.

## Examined and not defects

- `load_dotenv()` already gives real environment variables priority by default; the rewrite makes `override=False` explicit.
- Storing dates and shift times as strings follows the supplied data model; it avoids treating a calendar date as an instant.
- `work_hours=None` until punch-out and an empty initial history are correct. Punch-in/out must not create manual-correction audit entries.
- The original lower/upper date filters are inclusive, as required.
- An employee's immutable business code, not its ObjectId, is the correct public identifier.
- Pydantic's default ignoring of extra body fields is useful here: supplied derived fields must be ignored, not trusted.
