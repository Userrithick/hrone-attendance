# Walkthrough and interview preparation

Use this as a study guide. Explain the code in your own words and be honest that AI assisted the implementation. Do not claim to have independently written or understood parts you have not worked through.

## Trace one request

For `POST /attendance/punch-in`, FastAPI validates a `PunchInIn` body. `employee()` loads the employee by business code. `instant()` truncates milliseconds to seconds and creates an aware UTC instant. `attendance_date()` converts to IST and applies the overnight exception. `derive()` computes lateness against the shift start for that attendance day. Finally, MongoDB inserts the record; the unique employee/date index resolves duplicate requests.

Example: shift 22:00–06:00, punch at 00:05 IST on July 7. Its attendance date is July 6, so lateness is 125 minutes relative to July 6 at 22:00. A punch exactly at 06:00 does not satisfy the earlier-than-06:00 exception and belongs to July 7 under this contract.

UTC represents instants; IST defines business dates and shift boundaries. Using aware datetimes avoids depending on the server's timezone. The HTTP conversion is recursive so dates inside audit diffs become milliseconds too.

## Why exact arithmetic matters

Grace is compared in seconds: 09:40:00.999 first becomes 09:40:00 and is on time; 09:40:01 is late by 10 whole minutes. Overtime is counted only when floored minutes reach 30.

A 4h29m42s shift is 4.495 hours. Half-up produces 4.50, so it is not a half day. `Decimal` avoids binary float tie problems. MongoDB `$round` uses half-even; `rounded()` instead converts nonnegative metrics to decimal, scales them, adds 0.5, floors and unscales. Its nonnegative assumption is deliberate: attendance counts, rates, hours and minutes are nonnegative.

## How concurrent edits stay safe

Unique indexes handle creations. Updates require a different mechanism because derived fields depend on the record read earlier.

`replace_record()` matches the record's `_id` and internal `_rev`. Legacy seeded records have no revision, so the first update requires that it still be missing. The update changes fields, increments the revision and optionally pushes one history entry in a single atomic document operation. If another API request has already changed the revision, the match fails and the caller gets 409.

This prevents lost audit entries and stale computed values without transactions or a replica set. Successful corrections each append one accurate before/after diff. The losing caller must re-read and decide whether to retry. Direct database writers that ignore revisions are outside this guarantee; production writers would need to use the same protocol.

The request metadata `regularized_by` is free text, as specified. It is not proof of identity. Production auditing would require authentication, authorization and retention controls. Audit arrays can also eventually reach MongoDB's document-size limit; a separate audit collection would then require a strategy for atomic consistency.

## Explain each aggregation

**Monthly employee:** start with the employee, look up and group matching logs in MongoDB. The empty lookup still leaves an employee row. Generate that month's calendar with `$range` and count weekdays on/after `joined_on`. Presence counts weekday records, with half days weighted 0.5. Leave, late and overtime totals include weekend records as specified. If there are no working days, attendance percentage is null.

**Department summary:** start with employees who joined by month-end. Each employee's lookup returns a compact aggregate rather than every raw log. Group those results by department. Headcount adds one per employee even if their lookup is empty. Sum work hours and work-record counts separately, then divide; averaging employee averages would overweight employees with fewer records.

Example: one employee has one 2-hour record; another has three 8-hour records. The correct department average is (2+8+8+8)/4 = 6.5, not (2+8)/2 = 5.

**Late leaderboard:** scan the month, group stored late minutes by employee, remove nonpositive totals, join employee details and exclude orphans. Apply a department filter before ranking. `$rank` sorts only by total minutes, producing 1,2,2,4 for totals 100,60,60,25. Then filter rank <= limit and finally order equal scores by employee code. Putting code inside the rank sort would break ties; limiting first could cut ties off.

**Trend:** start with department employees and look up each employee's requested attendance range once, grouping stored metrics by date. `$range`, `$map` and `$unwind` create every requested date per employee in MongoDB, even if all attendance is missing or everyone joins later. Match each calendar row to that employee's grouped daily metrics. Group by day, counting employees and their metrics only when they have joined. Weekends remain in the series but have null rates. `$setWindowFields` averages non-null rounded rates from the current and six preceding rows. The first row has a one-row window; nothing before the requested start enters it.

This calendar expansion is an alternative to `$densify`: it deliberately covers the entirely empty-series case. It produces employee-count times range-length intermediate calendar rows, with the range capped at 92 days. Reading the entire range once per employee avoids a separate database lookup per employee/day. Precomputed daily summaries are the main scaling improvement, but corrections would have to update or invalidate them reliably.

## Indexes and explain

`analytics_spec()` returns the collection, pipeline and index hint for both real requests and explain. Attendance list and explain share `attendance_query()` plus identical sorting/offset/limit. Explain runs the actual MongoDB command with `executionStats`; it never fabricates a plan.

Hints keep empty-filter list requests on an index and make top-level access predictable. Foreign lookups use the employee-code and employee/date indexes. Hints have a cost: if workload distribution changes, the chosen index may no longer be optimal. Monitor examined keys/documents, execution time and index maintenance cost rather than treating the presence of IXSCAN as proof of speed.

## Contract choices worth discussing

- Explicit null timestamps are rejected; omitted punch timestamps use now. Correction omission preserves fields; ABSENT/LEAVE status clears times automatically.
- A punch-out selects the most recent punch-in at or before the supplied instant, including already-closed records. It must not fall back to an older open record. Before every available punch-in there is no qualifying record, hence 404; equality is 422.
- Present-day totals follow the literal weekday-record rule. Working-day denominators start at `joined_on`. The API adds no unstated prohibition on pre-employment punches; trend eligibility and summary headcount follow their specified join-date rules.
- `shift_start == shift_end` is rejected when creating employees. Seeded data is still interpreted with the R1 `<=` overnight definition.
- List count and page are separate database operations, so concurrent inserts may change the total between them. The contract does not require snapshot-consistent pagination. Offset pagination also becomes slower at very deep offsets; keyset pagination would require an API change.
- Startup creates indexes and fails if the database is inaccessible or has duplicate natural keys. Once running, `/health` returns 503 if MongoDB cannot answer. Other database infrastructure failures are not disguised as successful results.

## Practice before the interview

1. Explain why two simultaneous punch-ins cannot both succeed, and why an existence check cannot enforce that guarantee.
2. Change the grace period to 15 minutes and adjust boundary tests. Explain whether seeded analytics should change. (They should not: they use stored metrics.)
3. Work out the moving average for rates `[1, 0, null, null, 0.5]`: the last row averages three values, giving 0.5.
4. Add one employee with no logs and predict changes to headcount, average hours and attendance rate.
5. Trace a correction from PRESENT to LEAVE and name every changed field that appears in history.
6. Read one real explain response and locate the selected index, execution statistics and lookup index usage.

## References consulted

- [MongoDB window stages](https://www.mongodb.com/docs/v7.0/reference/operator/aggregation/setwindowfields/)
- [MongoDB calendar gap filling](https://www.mongodb.com/docs/v7.0/reference/operator/aggregation/densify/)

The supplied assignment contract is authoritative for business behavior; these references describe database mechanisms.
