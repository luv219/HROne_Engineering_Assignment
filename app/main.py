"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env wins)
"""
from __future__ import annotations

import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    model_validator,
)
import pymongo
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()  # real environment variables take priority (override=False)

MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB") or "attendance_db"

client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

HEALTH_TIMEOUT_S = 3

EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

PRESENCE_STATUSES = frozenset({"PRESENT", "WFH", "ON_DUTY"})
NON_PRESENCE_STATUSES = frozenset({"ABSENT", "LEAVE"})
ALL_STATUSES = frozenset({"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"})

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# --------------------------------------------------------------------------- #
# Startup indexes
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="dept_emp_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="dept_joined_on",
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uniq_emp_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_desc_emp",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="emp_punch_in",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="date_status",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)],
        name="date_late",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Time / rounding helpers
# --------------------------------------------------------------------------- #
def truncate_to_seconds(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.replace(microsecond=0)


def now_utc_truncated() -> datetime:
    return truncate_to_seconds(datetime.now(UTC))


def epoch_ms_to_datetime(ms: int) -> datetime:
    return truncate_to_seconds(datetime.fromtimestamp(ms / 1000.0, tz=UTC))


def datetime_to_epoch_ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    dt = truncate_to_seconds(dt)
    return int(dt.timestamp() * 1000)


def parse_hhmm(hhmm: str) -> tuple[int, int]:
    h, m = hhmm.split(":")
    return int(h), int(m)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_date_for_punch(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    """R1: IST calendar date of punch-in, with overnight exception."""
    pi = truncate_to_seconds(punch_in).astimezone(IST)
    d = pi.date()
    if is_overnight_shift(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        if pi.time() < time(eh, em):
            d = d - timedelta(days=1)
    return d.isoformat()


def shift_start_instant(att_date: str, shift_start: str) -> datetime:
    d = date.fromisoformat(att_date)
    h, m = parse_hhmm(shift_start)
    return datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST).astimezone(UTC)


def shift_end_instant(att_date: str, shift_start: str, shift_end: str) -> datetime:
    d = date.fromisoformat(att_date)
    if is_overnight_shift(shift_start, shift_end):
        d = d + timedelta(days=1)
    h, m = parse_hhmm(shift_end)
    return datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST).astimezone(UTC)


def half_up(value: Decimal | float | int, places: int) -> float:
    q = Decimal("1").scaleb(-places)
    d = Decimal(str(value)) if not isinstance(value, Decimal) else value
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    att_date: str,
) -> int:
    """R2: late only if strictly more than 10 minutes after shift start."""
    pi = truncate_to_seconds(punch_in)
    start = shift_start_instant(att_date, shift_start)
    delta = pi - start
    if delta.total_seconds() <= 10 * 60:
        return 0
    return int(delta.total_seconds() // 60)


def compute_overtime_minutes(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    att_date: str,
) -> int:
    """R3: floor minutes after shift end; 0 unless >= 30."""
    po = truncate_to_seconds(punch_out)
    end = shift_end_instant(att_date, shift_start, shift_end)
    minutes = int((po - end).total_seconds() // 60)
    if minutes < 30:
        return 0
    return max(0, minutes)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: ROUND_HALF_UP to 2 decimals."""
    pi = truncate_to_seconds(punch_in)
    po = truncate_to_seconds(punch_out)
    hours = Decimal(str((po - pi).total_seconds())) / Decimal("3600")
    return half_up(hours, 2)


def compute_half_day(work_hours: float | None) -> bool:
    """R5."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_working_days(start: date, end: date) -> int:
    """Inclusive Mon-Fri count from start to end."""
    if start > end:
        return 0
    n = 0
    cur = start
    one = timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:
            n += 1
        cur += one
    return n


def unprocessable(msg: str, *loc: str) -> HTTPException:
    """422 in FastAPI's validation-error shape: openapi.yaml declares 422 `detail` as an array of objects."""
    return HTTPException(status_code=422, detail=[{"loc": list(loc), "msg": msg, "type": "value_error"}])


def parse_valid_date(value: str, field_name: str = "date") -> date:
    if not DATE_RE.match(value):
        raise unprocessable(f"Invalid {field_name}", "query", field_name)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise unprocessable(f"Invalid {field_name}", "query", field_name) from exc


def parse_valid_month(value: str) -> str:
    if not MONTH_RE.match(value):
        raise unprocessable("Invalid month", "query", "month")
    return value


def half_up_expr(field_expr: Any, places: int) -> dict:
    """Mongo half-up via integer scaling (avoids $round banker's rounding).

    Done in Decimal128: in doubles avg(1.00, 1.01) is 1.00499.. and would round to 1.0.
    """
    factor = 10**places
    return {
        "$toDouble": {
            "$divide": [
                {"$floor": {"$add": [{"$multiply": [{"$toDecimal": field_expr}, factor]}, Decimal128("0.5")]}},
                factor,
            ]
        }
    }


def is_weekday_expr(date_field: str) -> dict:
    """MongoDB $dayOfWeek: 1=Sun .. 7=Sat; Mon-Fri = 2..6."""
    dow = {
        "$dayOfWeek": {
            "$dateFromString": {"dateString": f"${date_field}", "format": "%Y-%m-%d"}
        }
    }
    return {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def serialize_history_changes(changes: dict | None) -> dict:
    if not changes:
        return {}
    out: dict[str, Any] = {}
    for key, pair in changes.items():
        if not isinstance(pair, dict):
            continue
        fr, to = pair.get("from"), pair.get("to")
        if key in ("punch_in", "punch_out"):
            out[key] = {
                "from": datetime_to_epoch_ms(fr) if isinstance(fr, datetime) else fr,
                "to": datetime_to_epoch_ms(to) if isinstance(to, datetime) else to,
            }
        else:
            out[key] = {"from": fr, "to": to}
    return out


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


def serialize_attendance(doc: dict) -> dict:
    history = doc.get("history")
    if history is None:
        history = []
    ser_hist = []
    for entry in history:
        at = entry.get("at")
        ser_hist.append(
            {
                "at": datetime_to_epoch_ms(at) if isinstance(at, datetime) else at,
                "by": entry.get("by"),
                "reason": entry.get("reason"),
                "changes": serialize_history_changes(entry.get("changes") or {}),
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc["late_minutes"]) if "late_minutes" in doc and doc["late_minutes"] is not None else 0,
        "overtime_minutes": int(doc["overtime_minutes"]) if "overtime_minutes" in doc and doc["overtime_minutes"] is not None else 0,
        "half_day": bool(doc["half_day"]) if "half_day" in doc and doc["half_day"] is not None else False,
        "history": ser_hist,
    }


# --------------------------------------------------------------------------- #
# Pydantic models (strict)
# --------------------------------------------------------------------------- #
def _validate_epoch_ms(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("epoch milliseconds must be an integer")
    if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
        raise ValueError("epoch milliseconds out of range")
    return v


EpochMillis = Annotated[StrictInt, AfterValidator(_validate_epoch_ms)]


def _validate_calendar_date(v: str) -> str:
    if not DATE_RE.match(v):
        raise ValueError("invalid date format")
    try:
        date.fromisoformat(v)
    except ValueError as exc:
        raise ValueError("invalid calendar date") from exc
    return v


CalendarDate = Annotated[StrictStr, AfterValidator(_validate_calendar_date)]


def _validate_month(v: str) -> str:
    if not MONTH_RE.match(v):
        raise ValueError("invalid month")
    return v


MonthStr = Annotated[StrictStr, AfterValidator(_validate_month)]


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: StrictStr = Field(pattern=r"^EMP\d{4,6}$")
    name: StrictStr = Field(min_length=1, max_length=100)
    email: StrictStr = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: StrictStr = Field(min_length=1, max_length=50)
    shift_start: StrictStr = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: StrictStr = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: CalendarDate

    @model_validator(mode="after")
    def _shifts_differ(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: StrictStr
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: StrictStr
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: StrictStr = Field(min_length=5, max_length=200)
    regularized_by: StrictStr = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Derived-field recompute for presence / non-presence
# --------------------------------------------------------------------------- #
def recompute_derived(
    *,
    status: str,
    punch_in: datetime | None,
    punch_out: datetime | None,
    shift_start: str,
    shift_end: str,
    att_date: str,
) -> dict:
    if status in NON_PRESENCE_STATUSES:
        return {
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    late = compute_late_minutes(punch_in, shift_start, att_date) if punch_in else 0
    if punch_out is None:
        return {
            "punch_in": punch_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": late,
            "overtime_minutes": 0,
            "half_day": False,
        }
    wh = compute_work_hours(punch_in, punch_out)
    ot = compute_overtime_minutes(punch_out, shift_start, shift_end, att_date)
    return {
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": ot,
        "half_day": compute_half_day(wh),
    }


def values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return datetime_to_epoch_ms(a) == datetime_to_epoch_ms(b)
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) < 1e-9
    return a == b


# --------------------------------------------------------------------------- #
# Query / pipeline builders (shared with explain)
# --------------------------------------------------------------------------- #
def build_employees_filter(department: str | None) -> dict:
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    return q


def build_attendance_filter(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
) -> dict:
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def build_employee_monthly_pipeline(emp_code: str, month: str, joined_on: str) -> list[dict]:
    date_from, date_to = month_bounds(month)
    month_start = date.fromisoformat(date_from)
    month_end = date.fromisoformat(date_to)
    join_d = date.fromisoformat(joined_on)
    work_start = max(join_d, month_start)
    working_days = count_working_days(work_start, month_end)

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": date_from, "$lte": date_to},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    is_weekday_expr("date"),
                                ]
                            },
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": {"$literal": emp_code},
                "month": {"$literal": month},
                "working_days": {"$literal": working_days},
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$eq": [working_days, 0]},
                        None,
                        half_up_expr(
                            {"$multiply": [{"$divide": ["$present_days", working_days]}, 100]},
                            2,
                        ),
                    ]
                },
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: str | None) -> list[dict]:
    date_from, date_to = month_bounds(month)
    match_emp: dict[str, Any] = {"joined_on": {"$lte": date_to}}
    if department is not None:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount_emps": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": [{"$type": "$logs"}, "object"]},
                            {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                            {
                                                "$and": [
                                                    {
                                                        "$gte": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateFromString": {
                                                                        "dateString": "$logs.date",
                                                                        "format": "%Y-%m-%d",
                                                                    }
                                                                }
                                                            },
                                                            2,
                                                        ]
                                                    },
                                                    {
                                                        "$lte": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateFromString": {
                                                                        "dateString": "$logs.date",
                                                                        "format": "%Y-%m-%d",
                                                                    }
                                                                }
                                                            },
                                                            6,
                                                        ]
                                                    },
                                                ]
                                            },
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_n": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount_emps"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$work_hours_n", 0]},
                        None,
                        half_up_expr({"$divide": ["$work_hours_sum", "$work_hours_n"]}, 2),
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    month: str,
    limit: int,
    department: str | None,
) -> list[dict]:
    date_from, date_to = month_bounds(month)
    pipeline: list[dict] = [
        {
            "$match": {
                "date": {"$gte": date_from, "$lte": date_to},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    # $rank requires sortBy with exactly one field; competition ties share that field's rank.
    # Row order among ties is applied afterward (emp_code ASC).
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


def build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str,
) -> list[dict]:
    """Gap-filled daily trend entirely in MongoDB ($densify + window).

    Starts from the department's employees (indexed) and fetches each member's logs in the range with one
    (emp_code, date) index range scan, so the cost scales with the department, not with all logs per day.
    """
    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=UTC)
    to_dt = datetime.fromisoformat(to_date).replace(tzinfo=UTC)
    # $densify range bounds [min, max) — exclusive upper; add one day to include `to`.
    to_exclusive = to_dt + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": from_date, "$lte": to_date}}}],
                "as": "logs",
            }
        },
        {"$unwind": "$logs"},
        {"$replaceWith": "$logs"},
        # Anchor row so $densify has something to fill from even when the range holds no logs.
        {"$unionWith": {"pipeline": [{"$documents": [{"date": from_date}]}]}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_exclusive],
                },
            }
        },
        {
            "$addFields": {
                "date_str": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date", "timezone": "UTC"}
                },
                "dow": {"$dayOfWeek": "$date"},  # 1=Sun .. 7=Sat
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$d"]},
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4),
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "rate_window": {
                        "$push": "$attendance_rate",
                        "window": {"range": [-6, 0], "unit": "day"},
                    }
                },
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$let": {
                        "vars": {
                            "nonnull": {
                                "$filter": {
                                    "input": "$rate_window",
                                    "as": "r",
                                    "cond": {"$ne": ["$$r", None]},
                                }
                            }
                        },
                        "in": {
                            "$cond": [
                                {"$eq": [{"$size": "$$nonnull"}, 0]},
                                None,
                                half_up_expr({"$avg": "$$nonnull"}, 4),
                            ]
                        },
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


# MongoDB $dayOfWeek: Sun=1 ... Sat=7. Weekdays Mon-Fri = 2..6.
# Note: for string dates via $dateFromString without timezone, UTC midnight is used;
# ISO date strings as calendar dates are fine for day-of-week of the calendar day.


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        # Bound only the probe: the client default (30 s server selection) would make /health hang that long.
        with pymongo.timeout(HEALTH_TIMEOUT_S):
            client.admin.command("ping")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_utc_truncated(),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    q = build_employees_filter(department)
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort([("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance punch-in / punch-out / list / regularize
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = (
        epoch_ms_to_datetime(body.punched_at)
        if body.punched_at is not None
        else now_utc_truncated()
    )
    att_date = attendance_date_for_punch(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409, detail="already punched in for this date"
        ) from exc
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = (
        epoch_ms_to_datetime(body.punched_at)
        if body.punched_at is not None
        else now_utc_truncated()
    )

    # Find candidate open record first for validation messages, then close atomically.
    candidate = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts, "$ne": None},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not candidate:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    punch_in_ts = candidate["punch_in"]
    if punch_in_ts.tzinfo is None:
        punch_in_ts = punch_in_ts.replace(tzinfo=UTC)
    punch_in_ts = truncate_to_seconds(punch_in_ts)

    if ts <= punch_in_ts:
        raise unprocessable("punched_at must be after punch_in")
    if (ts - punch_in_ts).total_seconds() > 24 * 3600:
        raise unprocessable("punch duration exceeds 24 hours")

    wh = compute_work_hours(punch_in_ts, ts)
    ot = compute_overtime_minutes(
        ts, emp["shift_start"], emp["shift_end"], candidate["date"]
    )
    hd = compute_half_day(wh)

    # punch_in in the filter: a PATCH that moved it meanwhile would leave stale work_hours.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": candidate["_id"],
            "punch_in": candidate["punch_in"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Already punched out or modified concurrently")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if date_from is not None:
        parse_valid_date(date_from, "date_from")
    if date_to is not None:
        parse_valid_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise unprocessable("date_from must be <= date_to")

    q = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: CalendarDate,
    body: RegularizeRequest,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    existing = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not existing:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else existing["status"]

    if new_status in NON_PRESENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable("Cannot supply punch times with ABSENT/LEAVE")
        new_punch_in = None
        new_punch_out = None
    else:
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_datetime(body.punch_in)
        else:
            new_punch_in = existing.get("punch_in")
            if new_punch_in is not None and new_punch_in.tzinfo is None:
                new_punch_in = new_punch_in.replace(tzinfo=UTC)

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_datetime(body.punch_out)
        else:
            new_punch_out = existing.get("punch_out")
            if new_punch_out is not None and new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=UTC)

        if new_punch_in is None:
            raise unprocessable("Presence status requires punch_in")

        new_punch_in = truncate_to_seconds(new_punch_in)
        computed_date = attendance_date_for_punch(
            new_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if computed_date != date:
            raise unprocessable("punch_in does not belong to this attendance date")

        if new_punch_out is not None:
            new_punch_out = truncate_to_seconds(new_punch_out)
            if new_punch_out <= new_punch_in:
                raise unprocessable("punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise unprocessable("punch duration exceeds 24 hours")

    derived = recompute_derived(
        status=new_status,
        punch_in=new_punch_in,
        punch_out=new_punch_out,
        shift_start=emp["shift_start"],
        shift_end=emp["shift_end"],
        att_date=date,
    )

    # Snapshot of comparable old values
    old_punch_in = existing.get("punch_in")
    if isinstance(old_punch_in, datetime) and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=UTC)
    old_punch_out = existing.get("punch_out")
    if isinstance(old_punch_out, datetime) and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=UTC)

    old_snapshot = {
        "status": existing["status"],
        "punch_in": truncate_to_seconds(old_punch_in) if old_punch_in else None,
        "punch_out": truncate_to_seconds(old_punch_out) if old_punch_out else None,
        "work_hours": existing.get("work_hours"),
        "late_minutes": int(existing["late_minutes"])
        if existing.get("late_minutes") is not None
        else 0,
        "overtime_minutes": int(existing["overtime_minutes"])
        if existing.get("overtime_minutes") is not None
        else 0,
        "half_day": bool(existing["half_day"])
        if existing.get("half_day") is not None
        else False,
    }
    new_snapshot = {
        "status": new_status,
        "punch_in": derived["punch_in"],
        "punch_out": derived["punch_out"],
        "work_hours": derived["work_hours"],
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
    }

    changes: dict[str, Any] = {}
    for key in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ):
        if not values_equal(old_snapshot[key], new_snapshot[key]):
            changes[key] = {"from": old_snapshot[key], "to": new_snapshot[key]}

    if not changes:
        raise unprocessable("No changes")

    history_len = len(existing.get("history") or [])
    history_entry = {
        "at": now_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic CAS on everything we read: history length catches concurrent PATCHes,
    # the field values catch a punch-out (which never touches history). None matches missing too.
    cas_filter: dict[str, Any] = {
        "_id": existing["_id"],
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
    }
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        cas_filter[key] = existing.get(key)
    updated = db.attendance_logs.find_one_and_update(
        cas_filter,
        {
            "$set": {
                "status": new_status,
                "punch_in": derived["punch_in"],
                "punch_out": derived["punch_out"],
                "work_hours": derived["work_hours"],
                "late_minutes": derived["late_minutes"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Concurrent update conflict")

    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    pipeline = build_employee_monthly_pipeline(emp_code, month, emp["joined_on"])
    rows = list(db.attendance_logs.aggregate(pipeline))
    if not rows:
        date_from, date_to = month_bounds(month)
        month_start = date.fromisoformat(date_from)
        month_end = date.fromisoformat(date_to)
        join_d = date.fromisoformat(emp["joined_on"])
        working_days = count_working_days(max(join_d, month_start), month_end)
        return {
            "emp_code": emp_code,
            "month": month,
            "working_days": working_days,
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
            "attendance_pct": None if working_days == 0 else half_up(0.0, 2),
        }
    row = rows[0]
    # Ensure present_days is a number (half days)
    return {
        "emp_code": row["emp_code"],
        "month": row["month"],
        "working_days": row["working_days"],
        "present_days": row["present_days"],
        "leave_days": row["leave_days"],
        "late_count": row["late_count"],
        "total_late_minutes": row["total_late_minutes"],
        "total_overtime_minutes": row["total_overtime_minutes"],
        "attendance_pct": row["attendance_pct"],
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from")],
    to: Annotated[str, Query()],
):
    parse_valid_date(from_, "from")
    parse_valid_date(to, "to")
    if to < from_:
        raise unprocessable("to must be >= from")
    d0 = date.fromisoformat(from_)
    d1 = date.fromisoformat(to)
    if (d1 - d0).days + 1 > 92:
        raise unprocessable("range exceeds 92 days")

    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(status_code=404, detail="Unknown department")

    pipeline = build_department_trend_pipeline(department, from_, to)
    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin explain
# --------------------------------------------------------------------------- #
def bson_to_json(doc: dict) -> dict:
    """Explain output carries BSON types (Timestamp, Binary in $clusterTime on replica sets) FastAPI can't encode."""
    return json.loads(json_util.dumps(doc))


EXPLAINABLE = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: EXPLAINABLE,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if endpoint == "attendance_list":
        if date_from:
            parse_valid_date(date_from, "date_from")
        if date_to:
            parse_valid_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise unprocessable("date_from must be <= date_to")
        q = build_attendance_filter(emp_code, date_from, date_to, status)
        explain = db.command(
            {
                "explain": {
                    "find": "attendance_logs",
                    "filter": q,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": (page - 1) * page_size,
                    "limit": page_size,
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": bson_to_json(explain),
        }

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise unprocessable("emp_code and month are required")
        parse_valid_month(month)
        emp = db.employees.find_one({"emp_code": emp_code})
        if not emp:
            # Still explain a pipeline for a plausible joined_on so plan can be inspected;
            # but contract for live endpoint is 404 — for explain, require valid params only.
            joined_on = "1970-01-01"
        else:
            joined_on = emp["joined_on"]
        pipeline = build_employee_monthly_pipeline(emp_code, month, joined_on)
        explain = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": bson_to_json(explain),
        }

    if endpoint == "department_summary":
        if not month:
            raise unprocessable("month is required")
        parse_valid_month(month)
        pipeline = build_department_summary_pipeline(month, department)
        explain = db.command(
            {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": bson_to_json(explain),
        }

    if endpoint == "late_leaderboard":
        if not month:
            raise unprocessable("month is required")
        parse_valid_month(month)
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": bson_to_json(explain),
        }

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise unprocessable("department, from and to are required")
        parse_valid_date(from_, "from")
        parse_valid_date(to, "to")
        if to < from_:
            raise unprocessable("to must be >= from")
        d0 = date.fromisoformat(from_)
        d1 = date.fromisoformat(to)
        if (d1 - d0).days + 1 > 92:
            raise unprocessable("range exceeds 92 days")
        pipeline = build_department_trend_pipeline(department, from_, to)
        explain = db.command(
            {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": bson_to_json(explain),
        }

    raise unprocessable("Unknown endpoint")
