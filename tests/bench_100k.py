"""~100k-record stress test: correctness against the reference implementations, latency, and explain plans.

    MONGO_DB=attendance_bench_tmp python tests/bench_100k.py

Seeds a deterministic dataset straight into MongoDB (like the grader does), then starts the app on it.
"""
from __future__ import annotations

import os
import random
import sys
import time
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

os.environ.setdefault("MONGO_DB", "attendance_bench_tmp")
os.environ.setdefault("TEST_PORT", "8766")
sys.path.insert(0, os.path.dirname(__file__))
import test_api as T  # noqa: E402

START, END = date(2025, 10, 1), date(2026, 9, 30)
DEPTS = {"Engineering": 150, "Sales": 95, "Support": 70, "Ops": 50, "Finance": 40, "HR": 25, "Legal": 12, "Board": 3}


def seed():
    rnd = random.Random(42)
    T.reset()
    for c in ("employees", "attendance_logs"):
        T.db[c].drop_indexes()
    emps, logs, n = [], [], 0
    for dept, size in DEPTS.items():
        for _ in range(size):
            n += 1
            r = rnd.random()
            joined = (date(2025, 1, 1) + timedelta(rnd.randrange(270)) if r < 0.82 else
                      START + timedelta(rnd.randrange(330)) if r < 0.94 else date(2026, 12, 1))
            overnight = rnd.random() < 0.1
            emps.append({"emp_code": f"EMP{n:05d}", "name": f"Person {n}", "email": f"p{n}@x.io", "department": dept,
                         "shift_start": "22:00" if overnight else "09:30", "shift_end": "06:00" if overnight else "18:30",
                         "joined_on": joined.isoformat(), "created_at": T.dt(2025, 1, 1),
                         "_zero_logs": rnd.random() < 0.05})
    emps.append({"emp_code": "EMP99000", "name": "Future", "email": "f@x.io", "department": "Future",
                 "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2027-01-01", "created_at": T.dt(2025, 1, 1),
                 "_zero_logs": True})
    for e in emps:
        zero = e.pop("_zero_logs")
        if zero:
            continue
        d = max(START, date.fromisoformat(e["joined_on"]))
        while d <= END:
            wk = d.weekday() < 5
            if rnd.random() < (0.93 if wk else 0.04):
                logs.append(make_log(rnd, e["emp_code"], d))
            d += timedelta(1)
    for i in range(60):  # orphans: employee does not exist
        logs.append(make_log(rnd, "EMP77777", START + timedelta(i)))
    T.db.employees.insert_many(emps)
    for i in range(0, len(logs), 10000):
        T.db.attendance_logs.insert_many(logs[i:i + 10000])
    return len(emps), len(logs)


def make_log(rnd, code, d):
    r = rnd.random()
    status = "PRESENT" if r < 0.85 else "WFH" if r < 0.91 else "ON_DUTY" if r < 0.94 else "LEAVE" if r < 0.97 else "ABSENT"
    pin = T.dt(d.year, d.month, d.day, 9, rnd.randrange(60))
    doc = {"emp_code": code, "date": d.isoformat(), "status": status, "punch_in": None, "punch_out": None,
           "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    if status in T.PRESENCE:
        doc["punch_in"] = pin
        doc["late_minutes"] = 0 if rnd.random() < 0.7 else rnd.choice([11, 12, 15, 15, 20, 25, 30, 45, 60])
        if rnd.random() > 0.01:  # 1% still open
            wh = float(Decimal(rnd.uniform(2.5, 10.5)).quantize(Decimal("0.01"), ROUND_HALF_UP))
            doc.update(punch_out=pin + timedelta(hours=wh), work_hours=wh, half_day=wh < 4.5,
                       overtime_minutes=rnd.randint(30, 120) if rnd.random() < 0.2 else 0)
    if rnd.random() < 0.02:  # legacy shape
        doc.pop("half_day")
        doc.pop("history")
    return doc


def timed(method, path, **params):
    t0 = time.perf_counter()
    s, b = T.http(method, path, **params)
    return s, b, (time.perf_counter() - t0) * 1000


def stats_of(node, out=None):
    """Collect executionStats-ish numbers anywhere in an explain document."""
    out = out if out is not None else {"docs": 0, "keys": 0, "lookup_collscans": 0, "ms": 0, "nReturned": None}
    if isinstance(node, dict):
        es = node.get("executionStats")
        if isinstance(es, dict):
            out["docs"] += es.get("totalDocsExamined", 0)
            out["keys"] += es.get("totalKeysExamined", 0)
            out["ms"] = max(out["ms"], es.get("executionTimeMillis", 0))
            out["nReturned"] = es.get("nReturned", out["nReturned"])
        if "$lookup" in node and isinstance(node.get("collectionScans"), int):
            out["lookup_collscans"] += node["collectionScans"]
        if "$lookup" in node or "$lookup" in str(node.get("stage", "")):
            out["docs"] += node.get("totalDocsExamined", 0)
            out["keys"] += node.get("totalKeysExamined", 0)
        for v in node.values():
            stats_of(v, out)
    elif isinstance(node, list):
        for v in node:
            stats_of(v, out)
    return out


def main():
    t0 = time.time()
    n_emp, n_log = seed()
    print(f"seeded {n_emp} employees, {n_log} attendance records in {time.time() - t0:.1f}s (db {T.MONGO_DB})")
    proc, startup = T.start_server()
    print(f"startup incl. index build on {n_log} docs: {startup:.2f}s")
    T.check("bench: startup < 20 s with data present", startup < 20, startup)
    timings = []
    try:
        emps, logs = T.load_reference_data()
        depts = list(DEPTS) + ["Future"]
        rnd = random.Random(7)
        sample = rnd.sample(sorted(emps), 40) + ["EMP99000"]
        t1 = time.time()
        n = T.compare_analytics(emps, logs, ["2025-10", "2026-02", "2026-09"], depts,
                                [(dp, "2026-01-01", "2026-04-02") for dp in DEPTS] + [("Board", "2025-10-01", "2025-10-07"),
                                                                                       ("Future", "2026-01-01", "2026-01-31")],
                                label="bench ", sample_emps=sample)
        print(f"{n} analytics comparisons vs reference in {time.time() - t1:.1f}s")

        probes = {
            "GET /employees p1": ("/employees", {}),
            "GET /employees dept p3": ("/employees", {"department": "Engineering", "page": 3, "page_size": 50}),
            "GET /attendance p1": ("/attendance", {}),
            "GET /attendance deep p500": ("/attendance", {"page": 500, "page_size": 100}),
            "GET /attendance emp+range": ("/attendance", {"emp_code": "EMP00010", "date_from": "2026-01-01", "date_to": "2026-03-31"}),
            "GET /attendance status=LEAVE": ("/attendance", {"status": "LEAVE", "page_size": 100}),
            "GET /attendance range+status": ("/attendance", {"date_from": "2026-03-01", "date_to": "2026-03-31", "status": "WFH"}),
            "monthly": ("/analytics/employees/EMP00010/monthly", {"month": "2026-03"}),
            "summary all": ("/analytics/departments/summary", {"month": "2026-03"}),
            "summary Engineering": ("/analytics/departments/summary", {"month": "2026-03", "department": "Engineering"}),
            "leaderboard": ("/analytics/leaderboard/late", {"month": "2026-03", "limit": 50}),
            "leaderboard Sales": ("/analytics/leaderboard/late", {"month": "2026-03", "department": "Sales"}),
            "trend Engineering 92d": ("/analytics/departments/Engineering/trend", {"from_": "2026-01-01", "to": "2026-04-02"}),
            "trend Board 92d": ("/analytics/departments/Board/trend", {"from_": "2026-01-01", "to": "2026-04-02"}),
        }
        for name, (path, p) in probes.items():
            runs = [timed("GET", path, **p) for _ in range(3)]
            s, b, _ = runs[-1]
            best = min(r[2] for r in runs)
            rows = len(b.get("items", [])) if isinstance(b, dict) else None
            timings.append((name, s, best, rows, b.get("total") if isinstance(b, dict) else None))
            T.check(f"bench {name} 200", s == 200, (s, str(b)[:200]))
        # list totals match a direct count
        s, b, _ = timed("GET", "/attendance", date_from="2026-03-01", date_to="2026-03-31", status="WFH")
        T.check("bench list total == count_documents", b["total"] == T.db.attendance_logs.count_documents(
            {"date": {"$gte": "2026-03-01", "$lte": "2026-03-31"}, "status": "WFH"}))
        s, b, _ = timed("GET", "/attendance", page=1, page_size=100)
        keys = [(r["date"], r["emp_code"]) for r in b["items"]]
        T.check("bench list order", keys == sorted(sorted(keys, key=lambda k: k[1]), key=lambda k: k[0], reverse=True))

        explains = {
            "attendance_list": {"status": "LEAVE"},
            "attendance_list ": {"emp_code": "EMP00010", "date_from": "2026-01-01", "date_to": "2026-03-31"},
            "attendance_list  ": {"page": 500, "page_size": 100},
            "employee_monthly": {"emp_code": "EMP00010", "month": "2026-03"},
            "department_summary": {"month": "2026-03"},
            "department_summary ": {"month": "2026-03", "department": "Engineering"},
            "late_leaderboard": {"month": "2026-03", "limit": 10},
            "late_leaderboard ": {"month": "2026-03", "department": "Sales"},
            "department_trend": {"department": "Engineering", "from": "2026-01-01", "to": "2026-04-02"},
        }
        print("\nexplain (100k):")
        print(f"  {'endpoint':<20} {'params':<58} {'IXSCAN':>6} {'COLLSCAN':>8} {'keys':>7} {'docs':>7} {'lkpCS':>5} {'ms':>5}")
        for ep, p in explains.items():
            s, b, _ = timed("GET", f"/admin/explain/{ep.strip()}", **p)
            st = T.plan_stages(b)
            x = stats_of(b)
            ok = s == 200 and "IXSCAN" in st and "COLLSCAN" not in st and x["lookup_collscans"] == 0
            T.check(f"bench explain {ep.strip()} {p}", ok, (s, sorted(set(st)), x))
            print(f"  {ep.strip():<20} {str(p):<58} {'yes' if 'IXSCAN' in st else 'NO':>6} "
                  f"{'YES' if 'COLLSCAN' in st else 'no':>8} {x['keys']:>7} {x['docs']:>7} {x['lookup_collscans']:>5} {x['ms']:>5}")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    err = open(T.LOG.name, errors="replace").read()
    T.check("bench: no tracebacks / 500s", "Traceback" not in err and " 500 " not in err, err[-1500:])
    print("\nlatency (best of 3, HTTP round trip):")
    for name, s, best, rows, total in timings:
        print(f"  {name:<30} {s} {best:8.1f} ms  rows={rows} total={total}")
    failed = [r for r in T.RESULTS if not r[1]]
    for name, _, info in failed[:30]:
        print(f"FAIL {name}\n     {info}")
    print(f"\n{len(T.RESULTS) - len(failed)}/{len(T.RESULTS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
