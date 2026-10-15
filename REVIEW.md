# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Returns `{"status":"ok"}` without contacting MongoDB | Stop MongoDB and call `GET /health`: still 200, contract says 503 | Ping MongoDB (bounded to 3 s); 503 on failure |
| 2 | module startup | No indexes created at all | Every list/analytics query is a COLLSCAN; nothing enforces uniqueness (rows 3, 10) | Idempotent `create_index` calls in the FastAPI lifespan |
| 3 | `create_employee` | Check-then-insert (`find_one`, then `insert_one`), no unique index | Two concurrent `POST /employees` with one `emp_code` can both pass the `find_one` check and both insert: two 201s, two documents | Unique index on `emp_code`; `insert_one` and map `DuplicateKeyError` to 409 |
| 4 | `EmployeeIn` | No validation: patterns, lengths, valid date, `shift_start != shift_end` | `emp_code="abc"`, `email="nope"`, `joined_on="2026-02-30"`, equal shifts all return 201 | Pydantic models with the OpenAPI constraints and a shift-inequality validator |
| 5 | `create_employee` | `created_at = datetime.now()` (naive, server-local) returned as-is | Response `created_at` is an ISO string, not epoch milliseconds | Store UTC, truncated to seconds; return epoch ms |
| 6 | `list_employees` | `skip = page * page_size` | `page=1` skips the first 20 employees | `skip = (page - 1) * page_size` |
| 7 | `list_employees` | `total = count_documents({})` ignores the filter | `?department=Sales` reports the size of the whole collection | Count with the same filter |
| 8 | `list_employees` | No sort; `page`/`page_size` unbounded | Order not by `emp_code`; `page=0` or `page_size=10000` accepted | Sort `emp_code` ascending; `page >= 1`, `1 <= page_size <= 100` |
| 9 | `punch_in` | `emp` is never checked for `None` | Unknown `emp_code` → `TypeError` → 500 instead of 404 | 404 when the employee does not exist |
| 10 | `punch_in` | Check-then-insert on `(emp_code, date)`, no unique index | Two concurrent identical punch-ins can both pass the check: two 201s and two records for one day | Unique `(emp_code, date)` index; `DuplicateKeyError` → 409 |
| 11 | `punch_in` | `datetime.fromtimestamp(ms / 1000)` is server-local and naive; `date` is that local date, with no overnight rule (R1) and no truncation to seconds | On a UTC server a 00:05 IST punch gets the previous day's `date`; a 01:00 punch on a 22:00–06:00 shift gets the wrong day | Convert to UTC, truncate to seconds, derive `date` in IST with the overnight rule |
| 12 | `punch_in` | `status` is any string; response leaks `id` and returns datetimes | `status="ABSENT"` or `"foo"` → 201; response has `"id"` and ISO `punch_in` | `status` limited to PRESENT/WFH/ON_DUTY; response built by a serializer (epoch ms, no ids) |
| 13 | `PunchInIn.punched_at` | `Optional[int]` in lax mode, no range | `"1783312500000"` (string), `1783312500000.0` (float) and `1783312500` (seconds) are accepted | Strict integer, 100000000000 ≤ value ≤ 4102444800000, else 422 |
| 14 | `compute_late_minutes` | Shift start is built on the punch's own (server-local) day; late test is `minutes > 10` after truncating to whole minutes | Shift 09:30, punch 09:40:01 → 0 (should be 10); overnight 22:00 shift, punch 00:30 → 0 (should be 150) | Shift start on the attendance date in IST; late only if > 600 s, then floor minutes since shift start |
| 15 | `compute_work_hours` | `round(float, 2)`: float error plus round-half-even, not R4 half-up | 1:00:18 worked → `1.0` (should be 1.01); 8:09:54 → `8.16` (should be 8.17) | `Decimal` with `ROUND_HALF_UP` on whole seconds |
| 16 | `compute_overtime` | No 30-minute minimum; overnight shift end placed on the start date; IST-aware end minus a naive `punch_out` | Shift end 18:30, out 18:35 → 5 (should be 0); with the naive datetimes the rest of the starter produces, the subtraction raises `TypeError` | Count only if ≥ 30; overnight end on the next day; all maths on aware UTC datetimes |
| 17 | `list_attendance` | Loads every matching document into Python, then sorts and slices | Memory and latency grow with the collection (~100k docs) | MongoDB `find` with sort/skip/limit; `count_documents` for `total` |
| 18 | `list_attendance` | Sorted by `date` only; no `date_from > date_to` check; no page bounds; `status` not checked against the enum; leaks `id` | Same-date rows in arbitrary order; inverted range, `page_size=100000` or `status=FOO` → 200; `"id"` in items | Sort `date` desc, `emp_code` asc; validate dates, range, bounds and status; serializer without ids |
| 19 | module top (`MongoClient(...)`) | Client is not `tz_aware`, so stored UTC dates come back naive (no zone) | `GET /attendance` returns sample `punch_in` 04:35Z as `"2026-07-06T04:35:00"` with no offset; any epoch conversion of it on an IST server is 5 h 30 min off, and mixing it with aware datetimes raises `TypeError` (row 16) | `MongoClient(..., tz_aware=True, tzinfo=UTC)`; conversions go through one pair of helpers |

Also note anything you looked at and decided was **not** a defect, and why.

- `load_dotenv()` without `override=True`: correct, real environment variables must win over `.env`.
- `IST = timezone(timedelta(hours=5, minutes=30))`: correct (India has no DST).
- Date-range filters use `$gte`/`$lte` on `YYYY-MM-DD` strings: correct, because ISO dates sort the same as text.
- `MONGO_URI`/`MONGO_DB` read from the environment with a localhost fallback: acceptable for local runs; injected values always win.
- The `TODO` list of seven missing endpoints is the Part B scope, not a defect.

## Post-implementation verification and corrections

These were **not** starter defects. They were found in the first version of the new code (the Part B endpoints and the
review fixes above) during a second review and test pass, and fixed before submission. The tests live in `tests/`.

| Area | Problem found | Fix |
|---|---|---|
| PATCH vs punch-out | PATCH only checked history length, so a punch-out landing between its read and write was overwritten (`punch_out` reset to null, PATCH 200) | Update only if every field read is unchanged and history length is the same; otherwise 409 |
| Punch-out vs PATCH | A PATCH moving `punch_in` mid-request left stale `work_hours` (8.0 stored for 09:00–18:00) | Punch-out update also requires the `punch_in` it read |
| R8 rounding in pipelines | `floor(x*100+0.5)` on doubles: avg(1.00, 1.01) → 1.0, a moving average of 0.08335 → 0.0833 | Round in Decimal128 |
| Department trend speed | 8.3 s per request at ~94k records (scanned all departments' logs per day) | One indexed `(emp_code, date)` range lookup per department member: ~0.3 s |
| Explain response | Replica-set replies contain BSON `Timestamp`/`Binary`, which FastAPI cannot encode (would be 500) | Convert with `bson.json_util` |
| Trend explain plan | On MongoDB 8, `$limit: 1` planned as `EXPRESS_IXSCAN`, not `IXSCAN` | Seed stage changed to `$group` |
| 422 body shape | Business-rule 422s returned `detail` as a string; OpenAPI declares an array of objects | One helper returns FastAPI's validation-error shape |
| `/health` timeout | With MongoDB down, the ping waited the default 30 s before 503 | Ping bounded to 3 s |
