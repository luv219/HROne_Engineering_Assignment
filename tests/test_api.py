"""End-to-end tests against a real MongoDB and a real uvicorn process (stdlib + pymongo only).

    MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_test python tests/test_api.py

MONGO_DB must end in _test or _tmp: its two collections are dropped and reseeded.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONGO_URI = os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.setdefault("MONGO_DB", "attendance_test")
if not MONGO_DB.endswith(("_test", "_tmp")):
    sys.exit(f"refusing to wipe MONGO_DB={MONGO_DB!r}: name must end in _test or _tmp")
PORT = int(os.environ.get("TEST_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}"
LOG = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)

sys.path.insert(0, str(ROOT))
from bson import Binary, Timestamp  # noqa: E402
from pymongo import MongoClient  # noqa: E402

import app.main as m  # noqa: E402  (in-process hooks for deterministic race tests)

db = MongoClient(MONGO_URI, tz_aware=True)[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #
def http(method: str, path: str, body=None, **params):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k.rstrip("_"): v for k, v in params.items() if v is not None})
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read() or b"null")
        if e.code == 422:  # openapi ValidationError: detail is an array of objects, for every 422
            d = body.get("detail") if isinstance(body, dict) else None
            check(f"422 body shape {method} {path}", isinstance(d, list) and d and all(isinstance(x, dict) for x in d), body)
        return e.code, body


def ms(y, mo, d, h=0, mi=0, s=0, milli=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + milli


def dt(y, mo, d, h=0, mi=0, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc)


def start_server():
    t0 = time.time()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        cwd=ROOT, env=os.environ.copy(), stdout=LOG, stderr=subprocess.STDOUT,
    )
    while time.time() - t0 < 20:
        try:
            if http("GET", "/health")[0] == 200:
                return proc, time.time() - t0
        except OSError:
            pass
        time.sleep(0.2)
    proc.kill()
    raise SystemExit("server not ready within 20 s:\n" + Path(LOG.name).read_text())


RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, info="") -> None:
    RESULTS.append((name, bool(cond), "" if cond else str(info)[:400]))


def parallel(n: int, fn):
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(n) as ex:
        return list(ex.map(run, range(n)))


def reset():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


def emp(code, dept="Eng", start="09:30", end="18:30", joined="2026-01-01"):
    s, b = http("POST", "/employees", {"emp_code": code, "name": "N " + code, "email": f"{code}@x.io",
                                        "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined})
    assert s == 201, (s, b)
    return b


def no_object_id(obj) -> bool:
    return "_id" not in json.dumps(obj) and '"id"' not in json.dumps(obj)


# --------------------------------------------------------------------------- #
# reference implementations (test-side only) - Decimal, half-up
# --------------------------------------------------------------------------- #
def hu(x: Decimal, places: int) -> float:
    return float(x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def month_range(month):
    y, mo = map(int, month.split("-"))
    first = date(y, mo, 1)
    last = (date(y + mo // 12, mo % 12 + 1, 1) - timedelta(days=1))
    return first, last


def weekdays(a: date, b: date) -> int:
    return sum(1 for i in range((b - a).days + 1) if (a + timedelta(i)).weekday() < 5)


def is_weekday(s: str) -> bool:
    return date.fromisoformat(s).weekday() < 5


def present_weight(log) -> float:
    return 0.5 if log.get("half_day") else 1.0


def ref_monthly(emps, logs, code, month):
    e = emps[code]
    first, last = month_range(month)
    ls = [l for l in logs if l["emp_code"] == code and first.isoformat() <= l["date"] <= last.isoformat()]
    wd = weekdays(max(first, date.fromisoformat(e["joined_on"])), last)
    pres = sum(present_weight(l) for l in ls if l["status"] in PRESENCE and is_weekday(l["date"]))
    return {
        "emp_code": code, "month": month, "working_days": wd, "present_days": pres,
        "leave_days": sum(l["status"] == "LEAVE" for l in ls),
        "late_count": sum((l.get("late_minutes") or 0) > 0 for l in ls),
        "total_late_minutes": sum(l.get("late_minutes") or 0 for l in ls),
        "total_overtime_minutes": sum(l.get("overtime_minutes") or 0 for l in ls),
        "attendance_pct": None if wd == 0 else hu(Decimal(str(pres)) / wd * 100, 2),
    }


def ref_summary(emps, logs, month, department=None):
    first, last = month_range(month)
    members = {c: e for c, e in emps.items() if e["joined_on"] <= last.isoformat()
               and (department is None or e["department"] == department)}
    out = {}
    for c, e in members.items():
        r = out.setdefault(e["department"], {"department": e["department"], "headcount": 0, "present_days": 0.0,
                                             "_wh": [], "late_count": 0, "total_late_minutes": 0,
                                             "leave_count": 0, "on_duty_count": 0})
        r["headcount"] += 1
    for l in logs:
        e = members.get(l["emp_code"])
        if not e or not (first.isoformat() <= l["date"] <= last.isoformat()):
            continue
        r = out[e["department"]]
        if l["status"] in PRESENCE and is_weekday(l["date"]):
            r["present_days"] += present_weight(l)
        if l["status"] in PRESENCE and isinstance(l.get("work_hours"), (int, float)):
            r["_wh"].append(Decimal(str(l["work_hours"])))
        late = l.get("late_minutes") or 0
        r["late_count"] += late > 0
        r["total_late_minutes"] += late
        r["leave_count"] += l["status"] == "LEAVE"
        r["on_duty_count"] += l["status"] == "ON_DUTY"
    items = []
    for d in sorted(out):
        r = out[d]
        wh = r.pop("_wh")
        r["avg_work_hours"] = hu(sum(wh) / len(wh), 2) if wh else None
        items.append(r)
    return items


def ref_leaderboard(emps, logs, month, limit=10, department=None):
    first, last = month_range(month)
    tot: dict[str, list[int]] = {}
    for l in logs:
        late = l.get("late_minutes") or 0
        if late > 0 and first.isoformat() <= l["date"] <= last.isoformat() and l["emp_code"] in emps:
            if department is None or emps[l["emp_code"]]["department"] == department:
                t = tot.setdefault(l["emp_code"], [0, 0])
                t[0] += late
                t[1] += 1
    rows = sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0]))
    out = []
    for c, (total, cnt) in rows:
        rank = 1 + sum(1 for _, (t2, _) in rows if t2 > total)
        if rank <= limit:
            out.append({"rank": rank, "emp_code": c, "name": emps[c]["name"], "department": emps[c]["department"],
                        "total_late_minutes": total, "late_count": cnt})
    return out


def ref_trend(emps, logs, department, frm, to):
    d0, d1 = date.fromisoformat(frm), date.fromisoformat(to)
    dept = {c: e for c, e in emps.items() if e["department"] == department}
    by_day: dict[str, list] = {}
    for l in logs:
        if l["emp_code"] in dept and frm <= l["date"] <= to:
            by_day.setdefault(l["date"], []).append(l)
    items = []
    for i in range((d1 - d0).days + 1):
        d = (d0 + timedelta(i)).isoformat()
        hc = sum(1 for e in dept.values() if e["joined_on"] <= d)
        ls = by_day.get(d, [])
        pres = sum(present_weight(l) for l in ls if l["status"] in PRESENCE)
        wd = is_weekday(d)
        rate = hu(Decimal(str(pres)) / hc, 4) if wd and hc > 0 else None
        window = [it["attendance_rate"] for it in items[-6:] if it["attendance_rate"] is not None]
        if rate is not None:
            window.append(rate)
        mavg = hu(sum(Decimal(str(r)) for r in window) / len(window), 4) if window else None
        items.append({"date": d, "is_working_day": wd, "headcount": hc, "present_count": pres,
                      "late_count": sum((l.get("late_minutes") or 0) > 0 for l in ls),
                      "attendance_rate": rate, "moving_avg_7d": mavg})
    return items


def plan_stages(node, acc=None):
    """All 'stage' values anywhere in an explain document."""
    acc = [] if acc is None else acc
    if isinstance(node, dict):
        if isinstance(node.get("stage"), str):
            acc.append(node["stage"])
        for v in node.values():
            plan_stages(v, acc)
    elif isinstance(node, list):
        for v in node:
            plan_stages(v, acc)
    return acc


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_health_and_indexes():
    check("health 200 ok", http("GET", "/health") == (200, {"status": "ok"}))
    idx = {i["name"] for i in db.attendance_logs.list_indexes()} | {i["name"] for i in db.employees.list_indexes()}
    real, t0 = m.client, time.time()
    m.client = MongoClient("mongodb://127.0.0.1:1")  # nothing listens; default 30 s server selection
    try:
        m.health()
        outcome = 200
    except m.HTTPException as e:
        outcome = e.status_code
    finally:
        m.client = real
    check("health 503 when MongoDB unreachable, within 5 s", outcome == 503 and time.time() - t0 < 5,
          (outcome, time.time() - t0))
    check("startup indexes exist", {"uniq_emp_code", "uniq_emp_date", "date_desc_emp", "emp_punch_in"} <= idx, idx)


def test_employees():
    reset()
    base = {"emp_code": "EMP0001", "name": "A", "email": "a@x.io", "department": "Eng", "joined_on": "2026-01-05"}
    s, b = http("POST", "/employees", base)
    check("create 201", s == 201, (s, b))
    check("create defaults + created_at ms", b.get("shift_start") == "09:30" and b.get("shift_end") == "18:30"
          and isinstance(b.get("created_at"), int) and b["created_at"] >= 10**11 and no_object_id(b), b)
    check("create duplicate 409", http("POST", "/employees", base)[0] == 409)
    bad = [{"emp_code": "E1"}, {"shift_start": "09:30", "shift_end": "09:30"}, {"email": "nope"},
           {"joined_on": "2026-02-30"}, {"joined_on": "2026/01/01"}, {"name": ""}, {"shift_start": "24:00"},
           {"emp_code": 1234}, {"department": "x" * 51}]
    for i, patch in enumerate(bad):
        s, b = http("POST", "/employees", {**base, "emp_code": f"EMP{9000 + i}", **patch})
        check(f"create invalid {patch} -> 422", s == 422 and isinstance(b["detail"], list), (s, b))
    missing = dict(base, emp_code="EMP0002")
    missing.pop("email")
    check("create missing field 422", http("POST", "/employees", missing)[0] == 422)
    check("create malformed json 422", http("POST", "/employees", b"{not json")[0] == 422)

    # concurrent duplicates
    res = parallel(20, lambda i: http("POST", "/employees", {**base, "emp_code": "EMP0777"})[0])
    check("concurrent create: one 201, rest 409", res.count(201) == 1 and res.count(409) == 19, res)

    for i in range(3, 28):
        emp(f"EMP{i:04d}", dept="Ops" if i % 2 else "Eng")
    s, b = http("GET", "/employees")
    check("list default page", s == 200 and b["page"] == 1 and b["page_size"] == 20 and len(b["items"]) == 20, b)
    codes = [e["emp_code"] for e in b["items"]]
    check("list sorted emp_code, first page starts at first", codes == sorted(codes) and codes[0] == "EMP0001", codes)
    s, b = http("GET", "/employees", department="Ops", page=2, page_size=5)
    ops = sorted(c for c in (f"EMP{i:04d}" for i in range(3, 28)) if int(c[3:]) % 2)
    check("list filtered total + page 2", b["total"] == len(ops) and [e["emp_code"] for e in b["items"]] == ops[5:10], b)
    for p in ({"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}, {"page": "1.5"}):
        check(f"list employees {p} -> 422", http("GET", "/employees", **p)[0] == 422)
    check("list page_size 100 ok", http("GET", "/employees", page_size=100)[0] == 200)


def test_punch_in_rules():
    reset()
    emp("EMP0101")  # 09:30-18:30
    emp("EMP0102", start="22:00", end="06:00")  # overnight
    emp("EMP0103", start="09:30", end="18:30")

    def pin(code, t, **kw):
        return http("POST", "/attendance/punch-in", {"emp_code": code, "punched_at": t, **kw})

    # R1: 00:05 IST is the previous UTC day but the same IST day
    s, b = pin("EMP0101", ms(2026, 7, 6, 0, 5))
    check("R1 00:05 IST -> IST date", s == 201 and b["date"] == "2026-07-06", b)
    s, b = pin("EMP0101", ms(2026, 7, 6, 23, 59, 59))
    check("R1 23:59:59 IST same day -> 409 (same date)", s == 409, b)
    s, b = pin("EMP0101", ms(2026, 7, 7, 0, 0, 0))
    check("R1 midnight -> next date", s == 201 and b["date"] == "2026-07-07", b)
    # overnight boundaries
    s, b = pin("EMP0102", ms(2026, 7, 8, 5, 59, 59))
    check("R1 overnight 05:59:59 -> previous day", s == 201 and b["date"] == "2026-07-07", b)
    check("R2 overnight late from previous day's 22:00", b.get("late_minutes") == 479, b)
    s, b = pin("EMP0102", ms(2026, 7, 8, 6, 0, 0))
    check("R1 overnight 06:00:00 (= shift_end) -> same day", s == 201 and b["date"] == "2026-07-08", b)
    s, b = pin("EMP0102", ms(2026, 7, 9, 22, 10, 1))
    check("R2 overnight 22:10:01 -> 10", s == 201 and b["late_minutes"] == 10 and b["date"] == "2026-07-09", b)
    s, b = pin("EMP0102", ms(2026, 7, 10, 21, 55))
    check("R2 early overnight 21:55 -> 0", s == 201 and b["late_minutes"] == 0 and b["date"] == "2026-07-10", b)

    # R2 boundaries + R1 truncation
    for d, (h, mi, sec, milli), exp in [(13, (9, 40, 0, 900), 0), (14, (9, 40, 1, 0), 10), (15, (10, 15, 59, 0), 45),
                                         (16, (9, 41, 0, 0), 11), (17, (9, 0, 0, 0), 0)]:
        t = ms(2026, 7, d, h, mi, sec, milli)
        s, b = pin("EMP0103", t)
        check(f"R2 {h:02d}:{mi:02d}:{sec:02d}.{milli} -> {exp}", s == 201 and b["late_minutes"] == exp, b)
        if milli:
            check("R1 punch_in truncated to seconds", b["punch_in"] == t - milli, b)
    s, b = pin("EMP0103", ms(2026, 7, 13, 9))
    check("punch-in record shape", s == 409, b)
    s, b = http("GET", "/attendance", emp_code="EMP0103", date_from="2026-07-13", date_to="2026-07-13")
    r = b["items"][0]
    check("punch-in defaults", r["punch_out"] is None and r["work_hours"] is None and r["overtime_minutes"] == 0
          and r["half_day"] is False and r["history"] == [] and r["status"] == "PRESENT" and no_object_id(b), r)

    # validation
    for v in [1.7833125e12, "1783312500000", 1783312500, True, 1783312500000.5, 4102444800001, -1, 99999999999]:
        s, _ = pin("EMP0101", v)
        check(f"punched_at {v!r} -> 422", s == 422, s)
    check("punched_at 1783312500000.0 (float) -> 422", http("POST", "/attendance/punch-in",
          b'{"emp_code":"EMP0101","punched_at":1783312500000.0}')[0] == 422)
    check("status ABSENT at punch-in -> 422", pin("EMP0101", ms(2026, 7, 20, 9), status="ABSENT")[0] == 422)
    check("status lowercase -> 422", pin("EMP0101", ms(2026, 7, 20, 9), status="wfh")[0] == 422)
    s, b = pin("EMP0101", ms(2026, 7, 20, 9), status="WFH", late_minutes=99, work_hours=1)
    check("WFH ok; client-sent derived fields ignored", s == 201 and b["status"] == "WFH" and b["late_minutes"] == 0
          and b["work_hours"] is None, b)
    check("unknown employee -> 404", pin("EMP9999", ms(2026, 7, 20, 9))[0] == 404)
    check("missing emp_code -> 422", http("POST", "/attendance/punch-in", {"punched_at": ms(2026, 7, 21, 9)})[0] == 422)
    s, b = http("POST", "/attendance/punch-in", {"emp_code": "EMP0101"})
    check("punched_at omitted -> now", s == 201 and abs(b["punch_in"] - time.time() * 1000) < 5000, b)

    # concurrency
    t = ms(2026, 7, 22, 9, 0)
    res = parallel(20, lambda i: pin("EMP0101", t + i * 1000)[0])
    check("concurrent punch-in: one 201, rest 409", res.count(201) == 1 and res.count(409) == 19, res)
    check("concurrent punch-in: one document", db.attendance_logs.count_documents({"emp_code": "EMP0101", "date": "2026-07-22"}) == 1)


def test_punch_out_rules():
    reset()
    emp("EMP0201")
    emp("EMP0202", start="22:00", end="06:00")

    def pin(code, t):
        s, b = http("POST", "/attendance/punch-in", {"emp_code": code, "punched_at": t})
        assert s == 201, b

    def pout(code, t):
        return http("POST", "/attendance/punch-out", {"emp_code": code, "punched_at": t})

    check("punch-out unknown employee 404", pout("EMP9999", ms(2026, 7, 6, 18))[0] == 404)
    check("punch-out no punch-in 404", pout("EMP0201", ms(2026, 7, 6, 18))[0] == 404)
    # R3 / R4 / R5
    cases = [  # (day, in, out, work_hours, overtime, half_day)
        (6, (9, 30, 0), (18, 59, 59), 9.5, 29, None),
        (7, (9, 30, 0), (19, 0, 0), 9.5, 30, False),
        (8, (9, 0, 0), (13, 29, 42), 4.5, 0, False),    # 4.495 h -> 4.50 half-up -> not half day
        (9, (9, 0, 0), (13, 29, 41), 4.49, 0, True),    # 4.49472 -> 4.49
        (10, (9, 0, 0), (10, 0, 18), 1.01, 0, True),    # 1.005 -> 1.01
        (13, (9, 0, 0), (17, 9, 54), 8.17, 0, False),   # 8.165 -> 8.17
    ]
    for d, i, o, wh, ot, hd in cases:
        pin("EMP0201", ms(2026, 7, d, *i))
        s, b = pout("EMP0201", ms(2026, 7, d, *o))
        check(f"R4 {i}->{o} work_hours {wh}", s == 200 and b["work_hours"] == wh, b)
        check(f"R3 overtime {ot if ot >= 30 else 0}", b.get("overtime_minutes") == (ot if ot >= 30 else 0), b)
        if hd is not None:
            check(f"R5 half_day {hd} for {wh}", b["half_day"] is hd, b)
    # overnight punch-out across midnight
    pin("EMP0202", ms(2026, 7, 6, 21, 55))
    s, b = pout("EMP0202", ms(2026, 7, 7, 6, 40))
    check("overnight punch-out across midnight", s == 200 and b["date"] == "2026-07-06" and b["work_hours"] == 8.75
          and b["overtime_minutes"] == 40, b)
    # validation
    pin("EMP0201", ms(2026, 7, 14, 9))
    check("punch-out == punch_in -> 422", pout("EMP0201", ms(2026, 7, 14, 9))[0] == 422)
    check("punch-out > 24h -> 422", pout("EMP0201", ms(2026, 7, 15, 9, 0, 1))[0] == 422)
    check("punch-out before every punch-in -> 404", pout("EMP0201", ms(2026, 7, 1, 9))[0] == 404)
    check("punch-out float -> 422", pout("EMP0201", 1.7e12)[0] == 422)
    # most recent record is closed but an older one is open -> 409, older untouched
    db.attendance_logs.insert_one({"emp_code": "EMP0201", "date": "2026-07-16", "status": "PRESENT",
                                   "punch_in": dt(2026, 7, 16, 9), "punch_out": None, "work_hours": None,
                                   "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []})
    pin("EMP0201", ms(2026, 7, 17, 9))
    check("close latest", pout("EMP0201", ms(2026, 7, 17, 18))[0] == 200)
    check("latest closed, older open -> 409", pout("EMP0201", ms(2026, 7, 17, 19))[0] == 409)
    check("older open record untouched", db.attendance_logs.find_one({"emp_code": "EMP0201", "date": "2026-07-16"})["punch_out"] is None)
    # concurrency
    pin("EMP0201", ms(2026, 7, 20, 9))
    res = parallel(20, lambda i: pout("EMP0201", ms(2026, 7, 20, 18, 0, i))[0])
    check("concurrent punch-out: one 200, rest 409", res.count(200) == 1 and res.count(409) == 19, res)


def seed_record(code, d, **kw):
    doc = {"emp_code": code, "date": d, "status": "PRESENT", "punch_in": None, "punch_out": None, "work_hours": None,
           "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    doc.update(kw)
    for k in [k for k, v in kw.items() if v is ...]:
        doc.pop(k)
    db.attendance_logs.insert_one(doc)


def test_regularize():
    reset()
    emp("EMP0301")
    emp("EMP0302", start="22:00", end="06:00")
    P = lambda code, d, body: http("PATCH", f"/attendance/{code}/{d}", body)  # noqa: E731
    who = {"reason": "biometric glitch", "regularized_by": "hr.admin"}

    seed_record("EMP0301", "2026-07-07", punch_in=dt(2026, 7, 7, 10, 5), punch_out=dt(2026, 7, 7, 18, 30),
                work_hours=8.42, late_minutes=35)
    s, b = P("EMP0301", "2026-07-07", {**who, "punch_in": ms(2026, 7, 7, 9, 28)})
    h = b.get("history", [{}])[-1] if s == 200 else {}
    check("PATCH punch_in 200", s == 200 and b["late_minutes"] == 0 and b["work_hours"] == 9.03, b)
    check("history entry exact changes (ms values)", s == 200 and len(b["history"]) == 1 and h["by"] == "hr.admin"
          and h["reason"] == "biometric glitch" and isinstance(h["at"], int)
          and h["changes"] == {"punch_in": {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)},
                               "late_minutes": {"from": 35, "to": 0}, "work_hours": {"from": 8.42, "to": 9.03}}, b)
    stored = db.attendance_logs.find_one({"emp_code": "EMP0301", "date": "2026-07-07"})
    check("history punch values stored as BSON dates", isinstance(stored["history"][0]["changes"]["punch_in"]["from"], datetime))
    check("no-op PATCH -> 422", P("EMP0301", "2026-07-07", {**who, "punch_in": ms(2026, 7, 7, 9, 28)})[0] == 422)
    check("status-only no-op -> 422", P("EMP0301", "2026-07-07", {**who, "status": "PRESENT"})[0] == 422)
    s, b = P("EMP0301", "2026-07-07", {**who, "status": "WFH"})
    check("status change appends, keeps old entry", s == 200 and len(b["history"]) == 2
          and b["history"][1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}, b)
    s, b = P("EMP0301", "2026-07-07", {**who, "status": "LEAVE"})
    ch = b["history"][-1]["changes"] if s == 200 else {}
    check("LEAVE clears punches + derived", s == 200 and b["punch_in"] is None and b["punch_out"] is None
          and b["work_hours"] is None and set(ch) == {"status", "punch_in", "punch_out", "work_hours"}, b)
    check("LEAVE + punch_in -> 422", P("EMP0301", "2026-07-07", {**who, "status": "ABSENT", "punch_in": ms(2026, 7, 7, 9)})[0] == 422)
    check("presence without punch_in -> 422", P("EMP0301", "2026-07-07", {**who, "status": "PRESENT"})[0] == 422)
    s, b = P("EMP0301", "2026-07-07", {**who, "status": "ON_DUTY", "punch_in": ms(2026, 7, 7, 9, 45)})
    check("LEAVE -> ON_DUTY with punch_in", s == 200 and b["late_minutes"] == 15 and b["punch_out"] is None
          and b["work_hours"] is None and len(b["history"]) == 4, b)
    check("punch_in on another date -> 422", P("EMP0301", "2026-07-07", {**who, "punch_in": ms(2026, 7, 8, 9)})[0] == 422)
    check("punch_out <= punch_in -> 422", P("EMP0301", "2026-07-07", {**who, "punch_out": ms(2026, 7, 7, 9, 45)})[0] == 422)
    check("punch_out > 24h -> 422", P("EMP0301", "2026-07-07", {**who, "punch_out": ms(2026, 7, 8, 9, 45, 1)})[0] == 422)
    s, b = P("EMP0301", "2026-07-07", {**who, "punch_out": ms(2026, 7, 7, 13, 0)})
    check("PATCH punch_out -> half day", s == 200 and b["work_hours"] == 3.25 and b["half_day"] is True, b)
    check("unknown employee -> 404", P("EMP9999", "2026-07-07", who)[0] == 404)
    check("no record for date -> 404", P("EMP0301", "2026-07-01", {**who, "status": "LEAVE"})[0] == 404)
    check("bad date path -> 422", P("EMP0301", "2026-13-01", {**who, "status": "LEAVE"})[0] == 422)
    check("reason too short -> 422", P("EMP0301", "2026-07-07", {"reason": "x", "regularized_by": "a", "status": "LEAVE"})[0] == 422)
    check("missing regularized_by -> 422", P("EMP0301", "2026-07-07", {"reason": "valid reason", "status": "LEAVE"})[0] == 422)
    check("punch_in float -> 422", P("EMP0301", "2026-07-07", {**who, "punch_in": 1.7e12})[0] == 422)

    # legacy record: no history, no half_day
    seed_record("EMP0301", "2026-07-14", punch_in=dt(2026, 7, 14, 9, 32), punch_out=dt(2026, 7, 14, 18, 31),
                work_hours=8.98, history=..., half_day=...)
    s, b = http("GET", "/attendance", emp_code="EMP0301", date_from="2026-07-14", date_to="2026-07-14")
    check("legacy record serializes defaults", b["items"][0]["history"] == [] and b["items"][0]["half_day"] is False, b)
    s, b = P("EMP0301", "2026-07-14", {**who, "punch_out": ms(2026, 7, 14, 12, 0)})
    check("legacy PATCH: half_day from missing -> true recorded", s == 200 and len(b["history"]) == 1
          and b["history"][0]["changes"].get("half_day") == {"from": False, "to": True}, b)

    # overnight record: punch_in after midnight still on the start date
    seed_record("EMP0302", "2026-07-06", punch_in=dt(2026, 7, 6, 22, 0))
    s, b = P("EMP0302", "2026-07-06", {**who, "punch_in": ms(2026, 7, 7, 1, 0)})
    check("overnight PATCH punch_in after midnight keeps date", s == 200 and b["late_minutes"] == 180, b)

    # concurrent PATCHes: no lost history entries
    seed_record("EMP0301", "2026-07-20", punch_in=dt(2026, 7, 20, 9, 0))
    res = parallel(12, lambda i: P("EMP0301", "2026-07-20", {**who, "reason": f"fix number {i}",
                                                             "punch_out": ms(2026, 7, 20, 17, i)}))
    ok = [r for s, r in res if s == 200]
    stored = db.attendance_logs.find_one({"emp_code": "EMP0301", "date": "2026-07-20"})
    check("concurrent PATCH: only 200/409", all(s in (200, 409) for s, _ in res), [s for s, _ in res])
    check("concurrent PATCH: #200 == #history entries", len(ok) == len(stored["history"]) >= 1,
          (len(ok), len(stored["history"])))
    chain = stored["history"]
    check("concurrent PATCH: history chain consistent", all(
        chain[k]["changes"]["punch_out"]["from"] == chain[k - 1]["changes"]["punch_out"]["to"] for k in range(1, len(chain))))


def test_race_patch_vs_punch_out():
    """A punch-out committed between PATCH's read and its write must not be overwritten silently."""
    reset()
    emp("EMP0401")
    who = {"reason": "status fix", "regularized_by": "hr"}
    seed_record("EMP0401", "2026-07-06", punch_in=dt(2026, 7, 6, 9, 0))
    real = m.recompute_derived
    fired = []

    def hook(**kw):
        if not fired:
            fired.append(http("POST", "/attendance/punch-out", {"emp_code": "EMP0401", "punched_at": ms(2026, 7, 6, 18)}))
        return real(**kw)

    m.recompute_derived = hook
    try:
        try:
            m.regularize_attendance("EMP0401", "2026-07-06", m.RegularizeRequest(status="WFH", **who))
            outcome = 200
        except m.HTTPException as e:
            outcome = e.status_code
    finally:
        m.recompute_derived = real
    rec = db.attendance_logs.find_one({"emp_code": "EMP0401", "date": "2026-07-06"})
    check("race: interleaved punch-out succeeded", fired and fired[0][0] == 200, fired)
    check("race PATCH vs punch-out: punch_out not lost", rec["punch_out"] is not None and rec["work_hours"] == 9.0,
          {"patch": outcome, "punch_out": rec["punch_out"], "work_hours": rec["work_hours"]})
    check("race PATCH vs punch-out: loser gets 409", outcome == 409, outcome)

    # reverse: PATCH lands between punch-out's read and write -> derived fields must match final punches
    seed_record("EMP0401", "2026-07-07", punch_in=dt(2026, 7, 7, 10, 0), late_minutes=30)
    real_wh = m.compute_work_hours
    fired.clear()

    def hook_wh(a, b):
        if not fired:
            fired.append(http("PATCH", "/attendance/EMP0401/2026-07-07", {**who, "punch_in": ms(2026, 7, 7, 9, 0)}))
        return real_wh(a, b)

    m.compute_work_hours = hook_wh
    try:
        try:
            m.punch_out(m.PunchOutRequest(emp_code="EMP0401", punched_at=ms(2026, 7, 7, 18)))
            outcome = 200
        except m.HTTPException as e:
            outcome = e.status_code
    finally:
        m.compute_work_hours = real_wh
    rec = db.attendance_logs.find_one({"emp_code": "EMP0401", "date": "2026-07-07"})
    consistent = rec["punch_out"] is None or rec["work_hours"] == real_wh(rec["punch_in"], rec["punch_out"])
    check("race punch-out vs PATCH: interleaved PATCH succeeded", fired and fired[0][0] == 200, fired)
    check("race punch-out vs PATCH: no stale work_hours", consistent and outcome == 409,
          {"punch_out": outcome, "work_hours": rec["work_hours"], "punch_in": rec["punch_in"]})


def test_attendance_list():
    reset()
    emp("EMP0501")
    emp("EMP0502")
    for d in range(1, 31):
        for c in ("EMP0501", "EMP0502"):
            seed_record(c, f"2026-06-{d:02d}", status="LEAVE" if d % 5 == 0 else "PRESENT",
                        punch_in=None if d % 5 == 0 else dt(2026, 6, d, 9))
    s, b = http("GET", "/attendance")
    keys = [(r["date"], r["emp_code"]) for r in b["items"]]
    check("list default 20, sort date desc then emp_code", s == 200 and b["total"] == 60 and len(keys) == 20
          and keys[:3] == [("2026-06-30", "EMP0501"), ("2026-06-30", "EMP0502"), ("2026-06-29", "EMP0501")], keys[:3])
    s, b = http("GET", "/attendance", page=3, page_size=25)
    check("list last partial page", len(b["items"]) == 10 and b["page"] == 3 and b["page_size"] == 25, b["page"])
    s, b = http("GET", "/attendance", emp_code="EMP0502", date_from="2026-06-10", date_to="2026-06-19", status="LEAVE")
    check("list combined filters + total", b["total"] == 2 and {r["date"] for r in b["items"]} == {"2026-06-10", "2026-06-15"}, b)
    check("list inclusive bounds", http("GET", "/attendance", date_from="2026-06-30", date_to="2026-06-30")[1]["total"] == 2)
    for p in ({"date_from": "2026-06-20", "date_to": "2026-06-10"}, {"date_from": "2026-6-1"}, {"date_to": "2026-02-30"},
              {"status": "BOGUS"}, {"page": 0}, {"page_size": 101}):
        check(f"list attendance {p} -> 422", http("GET", "/attendance", **p)[0] == 422)
    check("list no _id leak", no_object_id(http("GET", "/attendance")[1]))


def analytics_fixture():
    """Small hand-made dataset with every awkward case the rules name. Aug 2026: 1st is a Saturday."""
    reset()
    E = [("EMP2001", "Alpha", "2026-01-01", "09:30", "18:30"), ("EMP2002", "Alpha", "2026-08-12", "09:30", "18:30"),
         ("EMP2003", "Alpha", "2026-01-01", "09:30", "18:30"), ("EMP2004", "Alpha", "2026-09-05", "09:30", "18:30"),
         ("EMP2101", "Beta", "2026-01-01", "09:30", "18:30"), ("EMP2102", "Beta", "2026-01-01", "22:00", "06:00"),
         ("EMP2103", "Beta", "2026-01-01", "09:30", "18:30"), ("EMP2201", "Gamma", "2026-10-01", "09:30", "18:30"),
         ("EMP2301", "Delta", "2026-01-01", "09:30", "18:30"), ("EMP2401", "Epsilon", "2026-01-01", "09:30", "18:30"),
         ("EMP2402", "Epsilon", "2026-01-01", "09:30", "18:30"), ("EMP2403", "Epsilon", "2026-01-01", "09:30", "18:30")]
    for c, dpt, j, s, e in E:
        db.employees.insert_one({"emp_code": c, "name": "Name " + c, "email": c + "@x.io", "department": dpt,
                                 "shift_start": s, "shift_end": e, "joined_on": j, "created_at": dt(2026, 1, 1)})
    L = [  # emp, date, status, work_hours, late, ot, half_day
        ("EMP2001", "2026-08-03", "PRESENT", 9.0, 0, 0, False), ("EMP2001", "2026-08-04", "PRESENT", 1.0, 15, 0, True),
        ("EMP2001", "2026-08-05", "WFH", 1.01, 0, 0, True), ("EMP2001", "2026-08-06", "LEAVE", None, 0, 0, False),
        ("EMP2001", "2026-08-07", "ON_DUTY", None, 12, 0, False), ("EMP2001", "2026-08-08", "PRESENT", 5.0, 20, 45, False),
        ("EMP2001", "2026-08-10", "ABSENT", None, 0, 0, False), ("EMP2001", "2026-07-31", "PRESENT", 8.0, 500, 0, False),
        ("EMP2002", "2026-08-12", "PRESENT", 8.0, 30, 0, False), ("EMP2002", "2026-08-13", "PRESENT", 4.49, 0, 0, True),
        ("EMP2101", "2026-08-03", "PRESENT", 7.0, 15, 0, False), ("EMP2101", "2026-08-04", "PRESENT", 7.01, 15, 0, False),
        ("EMP2102", "2026-08-03", "PRESENT", 8.75, 30, 40, False), ("EMP2103", "2026-08-09", "WFH", 6.0, 10, 0, False),
        ("EMP9999", "2026-08-03", "PRESENT", 8.0, 100, 0, False), ("EMP2301", "2026-09-01", "PRESENT", 8.0, 0, 0, False),
        # R8 boundaries: avg(1.00, 1.01) = 1.005 -> 1.01; trend rates (0.0, 0.1667) -> moving avg 0.08335 -> 0.0834
        ("EMP2401", "2026-08-04", "PRESENT", 1.0, 0, 0, True), ("EMP2402", "2026-08-08", "PRESENT", 1.01, 0, 0, True),
    ]
    for c, d, st, wh, late, ot, hd in L:
        seed_record(c, d, status=st, work_hours=wh, late_minutes=late, overtime_minutes=ot, half_day=hd,
                    punch_in=None if st in ("ABSENT", "LEAVE") else dt(*map(int, d.split("-")), 9))
    seed_record("EMP2001", "2026-08-11", status="PRESENT", work_hours=8.0, punch_in=dt(2026, 8, 11, 9),
                half_day=..., history=...)  # legacy
    return load_reference_data()


def load_reference_data():
    emps = {e["emp_code"]: e for e in db.employees.find({}, {"_id": 0})}
    logs = list(db.attendance_logs.find({}, {"_id": 0, "history": 0}))
    return emps, logs


def compare_analytics(emps, logs, months, depts, trend_ranges, label="", sample_emps=None):
    n = 0
    for month in months:
        for code in (sample_emps or list(emps)):
            s, b = http("GET", f"/analytics/employees/{code}/monthly", month=month)
            exp = ref_monthly(emps, logs, code, month)
            check(f"{label}monthly {code} {month}", s == 200 and b == exp, {"got": b, "exp": exp})
            n += 1
        for dpt in [None] + depts:
            s, b = http("GET", "/analytics/departments/summary", month=month, department=dpt)
            exp = ref_summary(emps, logs, month, dpt)
            check(f"{label}summary {month} {dpt}", s == 200 and b == {"month": month, "items": exp},
                  {"got": b, "exp": exp})
            for lim in (1, 2, 3, 4, 5, 10, 50):
                s, b = http("GET", "/analytics/leaderboard/late", month=month, limit=lim, department=dpt)
                exp = ref_leaderboard(emps, logs, month, lim, dpt)
                check(f"{label}leaderboard {month} {dpt} limit={lim}", s == 200 and b == {"month": month, "items": exp},
                      {"got": b, "exp": exp})
            n += 8
    for dpt, frm, to in trend_ranges:
        s, b = http("GET", f"/analytics/departments/{dpt}/trend", from_=frm, to=to)
        exp = ref_trend(emps, logs, dpt, frm, to)
        bad = [(g, e) for g, e in zip(b.get("items", []), exp) if g != e][:2] if s == 200 else b
        check(f"{label}trend {dpt} {frm}..{to}", s == 200 and b == {"department": dpt, "items": exp}, bad)
        n += 1
    return n


def test_analytics_fixture():
    emps, logs = analytics_fixture()
    compare_analytics(emps, logs, ["2026-08", "2026-07", "2026-09", "2026-10"], ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Nope"],
                      [("Alpha", "2026-08-01", "2026-08-31"), ("Beta", "2026-08-01", "2026-08-16"),
                       ("Alpha", "2026-08-09", "2026-08-09"), ("Gamma", "2026-08-01", "2026-10-31"),
                       ("Delta", "2026-08-01", "2026-08-07"), ("Epsilon", "2026-08-03", "2026-08-16")])
    # spot checks that pin down the tricky rules explicitly (independent of the reference)
    b = http("GET", "/analytics/employees/EMP2001/monthly", month="2026-08")[1]
    check("monthly: weekend excluded from present, included in late/ot; half days 0.5; legacy 1",
          b["present_days"] == 4.0 and b["working_days"] == 21 and b["total_late_minutes"] == 47
          and b["total_overtime_minutes"] == 45 and b["leave_days"] == 1 and b["late_count"] == 3, b)
    b = http("GET", "/analytics/employees/EMP2002/monthly", month="2026-08")[1]
    check("monthly: mid-month joiner working days", b["working_days"] == 14 and b["attendance_pct"] == 10.71, b)
    b = http("GET", "/analytics/employees/EMP2004/monthly", month="2026-08")[1]
    check("monthly: not joined -> 0 working days, pct null", b["working_days"] == 0 and b["attendance_pct"] is None, b)
    b = http("GET", "/analytics/employees/EMP2003/monthly", month="2026-08")[1]
    check("monthly: zero logs", b["present_days"] == 0 and b["attendance_pct"] == 0.0, b)
    check("monthly unknown employee 404", http("GET", "/analytics/employees/EMP9999/monthly", month="2026-08")[0] == 404)
    for mo in ("2026-13", "2026-8", "202608", None):
        check(f"monthly month={mo} -> 422", http("GET", "/analytics/employees/EMP2001/monthly", month=mo)[0] == 422)
    items = {i["department"]: i for i in http("GET", "/analytics/departments/summary", month="2026-08")[1]["items"]}
    check("summary: Gamma (headcount 0) omitted, Delta (zero logs) kept", "Gamma" not in items
          and items.get("Delta", {}).get("headcount") == 1 and items["Delta"]["avg_work_hours"] is None, items)
    check("summary: Alpha headcount excludes later joiner, includes zero-log", items["Alpha"]["headcount"] == 3, items)
    check("summary: Beta avg over records (7.0, 7.01, 8.75, 6.0) half-up = 7.19", items["Beta"]["avg_work_hours"] == 7.19, items)
    check("summary: Alpha avg_work_hours over 7 records = 5.21", items["Alpha"]["avg_work_hours"] == 5.21, items["Alpha"])
    check("R8 summary avg(1.00, 1.01) half-up = 1.01", items["Epsilon"]["avg_work_hours"] == 1.01, items["Epsilon"])
    t = http("GET", "/analytics/departments/Epsilon/trend", from_="2026-08-03", to="2026-08-04")[1]["items"]
    check("R8 trend moving avg (0.0, 0.1667) half-up = 0.0834", t[1]["attendance_rate"] == 0.1667
          and t[1]["moving_avg_7d"] == 0.0834, t)
    b = http("GET", "/analytics/leaderboard/late", month="2026-08", limit=2)[1]
    check("leaderboard ranks 1,2,2,2 with limit 2 (ties past limit kept), orphan ignored",
          [(i["rank"], i["emp_code"]) for i in b["items"]]
          == [(1, "EMP2001"), (2, "EMP2002"), (2, "EMP2101"), (2, "EMP2102")], b)
    b = http("GET", "/analytics/leaderboard/late", month="2026-08", limit=5)[1]
    check("leaderboard skips to rank 5", [i["rank"] for i in b["items"]] == [1, 2, 2, 2, 5], b)
    b = http("GET", "/analytics/leaderboard/late", month="2026-08", department="Beta")[1]
    check("leaderboard per department ranks 1,1,3", [i["rank"] for i in b["items"]] == [1, 1, 3], b)
    for p in ({"limit": 0}, {"limit": 51}, {"limit": 1.5}, {}):
        check(f"leaderboard {p} -> 422", http("GET", "/analytics/leaderboard/late", **({"month": "2026-08"} | p if p else {}))[0] == 422)
    check("summary missing month -> 422", http("GET", "/analytics/departments/summary")[0] == 422)
    t = http("GET", "/analytics/departments/Alpha/trend", from_="2026-08-01", to="2026-08-31")[1]["items"]
    check("trend 31 rows incl weekends", len(t) == 31 and t[0]["date"] == "2026-08-01" and t[-1]["date"] == "2026-08-31")
    check("trend weekend rate null, weekend present_count counted", t[0]["attendance_rate"] is None
          and t[7]["present_count"] == 1 and t[7]["attendance_rate"] is None, t[7])
    check("trend no-record weekday rate 0.0", t[30]["attendance_rate"] == 0.0, t[30])
    check("trend first rows shorter window", t[2]["moving_avg_7d"] == t[2]["attendance_rate"] and t[0]["moving_avg_7d"] is None, t[:3])
    check("trend headcount grows at joiner", t[10]["headcount"] == 2 and t[11]["headcount"] == 3, t[10:12])
    check("trend unknown department 404", http("GET", "/analytics/departments/Nope/trend", from_="2026-08-01", to="2026-08-02")[0] == 404)
    for f, to in (("2026-08-02", "2026-08-01"), ("2026-01-01", "2026-04-03"), ("2026-8-1", "2026-08-02"), (None, "2026-08-02")):
        check(f"trend from={f} to={to} -> 422", http("GET", "/analytics/departments/Alpha/trend", from_=f, to=to)[0] == 422)
    check("trend 92 days ok", http("GET", "/analytics/departments/Alpha/trend", from_="2026-07-01", to="2026-09-30")[0] == 200)


def test_explain():
    params = {
        "attendance_list": {"emp_code": "EMP2001", "date_from": "2026-08-01", "date_to": "2026-08-31"},
        "employee_monthly": {"emp_code": "EMP2001", "month": "2026-08"},
        "department_summary": {"month": "2026-08"},
        "late_leaderboard": {"month": "2026-08", "limit": 3},
        "department_trend": {"department": "Alpha", "from": "2026-08-01", "to": "2026-08-31"},
    }
    for ep, p in params.items():
        s, b = http("GET", f"/admin/explain/{ep}", **p)
        st = plan_stages(b)
        check(f"explain {ep}: 200, IXSCAN, no COLLSCAN", s == 200 and {"endpoint", "collection", "explain"} <= set(b)
              and "IXSCAN" in st and "COLLSCAN" not in st, (s, sorted(set(st))))
    check("explain attendance_list no filters IXSCAN", "COLLSCAN" not in plan_stages(http("GET", "/admin/explain/attendance_list")[1]))
    check("explain missing month -> 422", http("GET", "/admin/explain/employee_monthly", emp_code="EMP2001")[0] == 422)
    check("explain trend missing to -> 422", http("GET", "/admin/explain/department_trend", department="Alpha", from_="2026-08-01")[0] == 422)
    check("explain unknown endpoint -> 422", http("GET", "/admin/explain/nope")[0] == 422)
    check("explain output is plain JSON for BSON types", json.dumps(m.bson_to_json({"t": Timestamp(1, 2), "b": Binary(b"\x9f")})) is not None)


def main():
    proc, startup = start_server()
    print(f"server ready in {startup:.2f}s on db {MONGO_DB}")
    check("startup < 20 s", startup < 20, startup)
    tests = [test_health_and_indexes, test_employees, test_punch_in_rules, test_punch_out_rules, test_regularize,
             test_race_patch_vs_punch_out, test_attendance_list, test_analytics_fixture, test_explain]
    only = sys.argv[1:]
    try:
        for t in tests:
            if only and t.__name__ not in only:
                continue
            try:
                t()
            except Exception as exc:  # a crash is a failure, keep going
                check(f"{t.__name__} crashed", False, repr(exc))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    err = Path(LOG.name).read_text(errors="replace")
    check("no tracebacks / HTTP 500 in server log", "Traceback" not in err and " 500 " not in err, err[-1500:])
    failed = [r for r in RESULTS if not r[1]]
    for name, _, info in failed:
        print(f"FAIL {name}\n     {info}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
