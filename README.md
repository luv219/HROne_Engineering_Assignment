# Employee Attendance & Analytics API

FastAPI + MongoDB service for the HROne assignment: employees, punch-in/out, regularization and four analytics
endpoints built as MongoDB aggregation pipelines. All application code is in `app/main.py`.

## Tech stack

Python 3.11+, FastAPI, PyMongo, MongoDB 6.0+.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Configuration comes from `MONGO_URI` and `MONGO_DB`. A local `.env` works too; real environment variables win.

```text
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Optionally load the nine sample documents (this **replaces** both collections in `MONGO_DB`):

```bash
python sample_seed.py
```

Start the server:

```bash
uvicorn app.main:app --port 8000
```

Indexes are created at startup (idempotent). `http://127.0.0.1:8000/health` returns `{"status":"ok"}` only when
MongoDB answers, otherwise 503. Swagger UI is at `http://127.0.0.1:8000/docs`.

## Testing

Both scripts start the app themselves on a separate port, talk to it over HTTP and use a real MongoDB. They wipe the
database they run against, so they refuse any `MONGO_DB` whose name does not end in `_test` or `_tmp`. Without
`MONGO_DB` set they use `attendance_test` / `attendance_bench_tmp`.

```bash
python tests/test_api.py      # contract, R1-R10, validation, concurrency races, analytics vs a reference implementation
python tests/bench_100k.py    # optional: ~94k records - analytics vs reference, latency, explain plans (~2 min)
```

The concurrency checks (20 parallel creates, punch-ins and punch-outs, 12 parallel PATCHes, and forced
PATCH/punch-out interleavings) are part of `test_api.py`.

## Design notes

- Duplicates are prevented by unique indexes on `emp_code` and `(emp_code, date)`, not by checking first.
- Punch-out and PATCH are single conditional `find_one_and_update` calls; if the record changed after it was read,
  the request gets 409 instead of overwriting the other change.
- Attendance dates, late minutes and overtime are computed in IST (UTC+05:30), including overnight shifts. Instants
  are stored as UTC BSON dates and returned as epoch milliseconds.
- Analytics are aggregation pipelines over the stored derived values; reported numbers are rounded half-up in
  Decimal128.
- List and analytics queries are index-backed; `GET /admin/explain/{endpoint}` returns the MongoDB explain output.

## Known limitations

- Tested on MongoDB 8.3. The operators used are documented as available in 6.0/7.0, but no run on 7.0 was done.
- Explain output from a replica set (e.g. Atlas) is handled by `bson.json_util`; this was unit-checked, not run against
  a real replica set.

## Submission

The repository contains no `.env`, secrets, Dockerfile, virtual environment or data dumps (`sample_data/` is the
nine-document sample from the candidate kit).
