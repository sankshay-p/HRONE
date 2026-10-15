"""
Employee Attendance & Analytics API
FastAPI + MongoDB implementation adhering strictly to openapi.yaml and DATA_MODEL.md.
"""
from __future__ import annotations

import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import math
import os
import pathlib
import re
from typing import Any, Dict, List, Literal, Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Environment & Database setup
# Real environment variables take priority over .env
# --------------------------------------------------------------------------- #
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure all required indexes are created idempotently at startup
    indexes_to_create = [
        # employees
        (db.employees, [("emp_code", ASCENDING)], {"unique": True}),
        (db.employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {}),
        (db.employees, [("joined_on", ASCENDING)], {}),
        (db.employees, [("department", ASCENDING), ("joined_on", ASCENDING)], {}),
        # attendance_logs
        (db.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),
        (db.attendance_logs, [("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        (db.attendance_logs, [("date", ASCENDING)], {}),
        (db.attendance_logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {}),
        (db.attendance_logs, [("date", ASCENDING), ("late_minutes", ASCENDING)], {}),
        (db.attendance_logs, [("status", ASCENDING), ("date", ASCENDING)], {}),
    ]
    for coll, keys, opts in indexes_to_create:
        try:
            coll.create_index(keys, **opts)
        except Exception as exc:
            print(f"Warning creating index {keys} on {coll.name}: {exc}")
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Logic (R1 - R5, R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: float | Decimal | None, decimals: int = 2) -> float | None:
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def truncate_ms(ms: int) -> int:
    return (ms // 1000) * 1000


def ms_to_dt_utc(ms: int) -> datetime:
    trunc = truncate_ms(ms)
    return datetime.fromtimestamp(trunc / 1000.0, tz=timezone.utc)


def dt_to_ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: Attendance date in IST. For overnight shifts (shift_end <= shift_start),
    a punch-in earlier than shift_end belongs to the shift that started the previous day."""
    dt_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        time_str = dt_ist.strftime("%H:%M:%S")
        shift_end_sec = shift_end + ":00"
        if time_str < shift_end_sec:
            return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """R2: Grace period 10 minutes. Late only if punch-in is strictly > 10:00 after shift_start.
    Then whole minutes (floor) elapsed since shift_start."""
    dt_ist = punch_in_utc.astimezone(IST)
    y, m, d = map(int, attendance_date.split("-"))
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = datetime(y, m, d, sh, sm, 0, tzinfo=IST)
    elapsed_sec = (dt_ist - shift_start_dt).total_seconds()
    if elapsed_sec > 600:
        return int(elapsed_sec // 60)
    return 0


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """R3: Overtime = whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30; otherwise 0. Shift end is on next calendar day for overnight shift."""
    dt_ist = punch_out_utc.astimezone(IST)
    y, m, d = map(int, attendance_date.split("-"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    shift_end_date = date(y, m, d) + timedelta(days=1) if is_overnight else date(y, m, d)
    shift_end_dt = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, eh, em, 0, tzinfo=IST)
    ot_sec = (dt_ist - shift_end_dt).total_seconds()
    if ot_sec >= 1800:
        return int(ot_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    sec = int((punch_out_utc - punch_in_utc).total_seconds())
    d = Decimal(sec) / Decimal(3600)
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_half_day(work_hours: float | None) -> bool:
    """R5: After punch-out, half_day = true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Serialization Helpers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at_ms = dt_to_ms(created_at)
    elif isinstance(created_at, (int, float)):
        created_at_ms = int(created_at)
    else:
        created_at_ms = None

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_ms = dt_to_ms(at_val)
    elif isinstance(at_val, (int, float)):
        at_ms = int(at_val)
    else:
        at_ms = at_val

    raw_changes = entry.get("changes", {})
    changes: Dict[str, Any] = {}
    for k, v in raw_changes.items():
        if not isinstance(v, dict):
            changes[k] = v
            continue
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = dt_to_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = dt_to_ms(to_val)
        changes[k] = {"from": from_val, "to": to_val}

    return {
        "at": at_ms,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")

    punch_in_ms = dt_to_ms(punch_in) if isinstance(punch_in, datetime) else punch_in
    punch_out_ms = dt_to_ms(punch_out) if isinstance(punch_out, datetime) else punch_out

    raw_history = doc.get("history") or []
    history = [serialize_history_entry(h) for h in raw_history]

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": punch_in_ms,
        "punch_out": punch_out_ms,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def clean_bson_for_json(val: Any) -> Any:
    """Recursively converts BSON types (like Int64, ObjectId, datetime) for clean JSON serialization."""
    if isinstance(val, dict):
        return {k: clean_bson_for_json(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [clean_bson_for_json(x) for x in val]
    elif isinstance(val, ObjectId):
        return str(val)
    elif isinstance(val, datetime):
        return val.isoformat()
    return val


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]

TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMP_PATTERN = re.compile(r"^EMP\d{4,6}$")


class EmployeeCreate(BaseModel):
    emp_code: str
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not EMP_PATTERN.match(v):
            raise ValueError("emp_code must be EMP followed by 4-6 digits")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not EMAIL_PATTERN.match(v):
            raise ValueError("Invalid email format")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_time(cls, v: str) -> str:
        if not TIME_PATTERN.match(v):
            raise ValueError("Time must be in 24-hour HH:MM format")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        if not DATE_PATTERN.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid calendar date for joined_on")
        return v

    @model_validator(mode="after")
    def validate_shift_difference(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatusType = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[StatusType] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database unavailable: {exc}")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    created_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": created_at_dt,
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Employee already exists")
    except PyMongoError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Conflict: {exc}")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort([("emp_code", ASCENDING)]).skip(skip).limit(page_size)

    items = [serialize_employee(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punched_ms = truncate_ms(body.punched_at)
        ts_utc = datetime.fromtimestamp(punched_ms / 1000.0, tz=timezone.utc)
    else:
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        punched_ms = int(now_dt.timestamp() * 1000)
        ts_utc = now_dt

    attendance_date = get_attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(ts_utc, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched in for this date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punched_ms = truncate_ms(body.punched_at)
        ts_out_utc = datetime.fromtimestamp(punched_ms / 1000.0, tz=timezone.utc)
    else:
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        punched_ms = int(now_dt.timestamp() * 1000)
        ts_out_utc = now_dt

    # Find the employee's most recent record with a punch-in
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)
    punch_in_ms = int(punch_in_dt.timestamp() * 1000)

    # 422 if punched_at <= punch_in or more than 24 hours after it
    if punched_ms <= punch_in_ms:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punched_at must be strictly after punch_in")
    if punched_ms - punch_in_ms > 24 * 3600 * 1000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )

    # 409 if that record is already punched out
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record is already punched out")

    # Compute derived values
    work_hrs = compute_work_hours(punch_in_dt, ts_out_utc)
    ot_mins = compute_overtime(ts_out_utc, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = is_half_day(work_hrs)

    # Race-safe atomic update: exactly one concurrent request updates punch_out
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record is already punched out")

    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusType] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to:
        if date_from > date_to:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date_from cannot be greater than date_to",
            )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)

    items = [serialize_attendance_record(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found for this date")

    # Current stored values
    curr_status = record["status"]
    curr_punch_in = record.get("punch_in")
    curr_punch_out = record.get("punch_out")
    curr_work_hours = record.get("work_hours")
    curr_late_minutes = record.get("late_minutes", 0)
    curr_overtime_minutes = record.get("overtime_minutes", 0)
    curr_half_day = record.get("half_day", False)
    curr_history = record.get("history") or []

    # Target values
    final_status = body.status if body.status is not None else curr_status

    # Rule: setting status ABSENT or LEAVE clears both punch times,
    # supplying punch_in or punch_out with it is 422
    if final_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )
        final_punch_in = None
        final_punch_out = None
    else:
        # presence status
        if body.punch_in is not None:
            final_punch_in = ms_to_dt_utc(body.punch_in)
        else:
            final_punch_in = curr_punch_in

        if body.punch_out is not None:
            final_punch_out = ms_to_dt_utc(body.punch_out)
        else:
            final_punch_out = curr_punch_out

        # Rule: a presence status requires a punch_in (supplied or already stored)
        if final_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in",
            )

        # Rule: punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in must stay on the record's attendance date",
            )

        # Rule: punch_out must be after punch_in and within 24 hours
        if final_punch_out is not None:
            diff_sec = (final_punch_out - final_punch_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in",
                )
            if diff_sec > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )

    # Recompute derived values
    if final_status in ("ABSENT", "LEAVE"):
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        final_late_minutes = compute_late_minutes(final_punch_in, emp["shift_start"], date)
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(final_punch_out, emp["shift_start"], emp["shift_end"], date)
            final_half_day = is_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

    # Determine changes
    changes: Dict[str, Dict[str, Any]] = {}

    def compare_and_record(key: str, cur_v: Any, fin_v: Any, is_date: bool = False):
        if is_date:
            cur_ms = dt_to_ms(cur_v) if isinstance(cur_v, datetime) else cur_v
            fin_ms = dt_to_ms(fin_v) if isinstance(fin_v, datetime) else fin_v
            if cur_ms != fin_ms:
                # In MongoDB stored changes: BSON dates for punch_in/punch_out
                changes[key] = {"from": cur_v, "to": fin_v}
        else:
            if cur_v != fin_v:
                changes[key] = {"from": cur_v, "to": fin_v}

    compare_and_record("status", curr_status, final_status)
    compare_and_record("punch_in", curr_punch_in, final_punch_in, is_date=True)
    compare_and_record("punch_out", curr_punch_out, final_punch_out, is_date=True)
    compare_and_record("work_hours", curr_work_hours, final_work_hours)
    compare_and_record("late_minutes", curr_late_minutes, final_late_minutes)
    compare_and_record("overtime_minutes", curr_overtime_minutes, final_overtime_minutes)
    compare_and_record("half_day", curr_half_day, final_half_day)

    # Rule: a request that changes nothing is 422
    if not changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No changes made")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check: match current history length so concurrent edits don't lose entries
    update_doc = {
        "$set": {
            "status": final_status,
            "punch_in": final_punch_in,
            "punch_out": final_punch_out,
            "work_hours": final_work_hours,
            "late_minutes": final_late_minutes,
            "overtime_minutes": final_overtime_minutes,
            "half_day": final_half_day,
        },
        "$push": {"history": history_entry},
    }

    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": {"$size": len(curr_history)}},
        update_doc,
        return_document=ReturnDocument.AFTER,
    )
    if not res:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification detected on attendance history",
        )

    return serialize_attendance_record(res)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
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
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    start_of_month = f"{month}-01"
    end_of_month = f"{month}-{last_day:02d}"

    # Calculate working_days: Mon-Fri from max(joined_on, first_day_of_month) to last_day_of_month
    effective_start = max(start_of_month, emp["joined_on"])
    if effective_start > end_of_month:
        working_days = 0
    else:
        cur_d = date.fromisoformat(effective_start)
        end_d = date.fromisoformat(end_of_month)
        working_days = 0
        while cur_d <= end_d:
            if cur_d.weekday() < 5:  # 0..4 = Mon..Fri
                working_days += 1
            cur_d += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = (
        round_half_up((present_days / working_days) * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    start_of_month = f"{month}-01"
    end_of_month = f"{month}-{last_day:02d}"

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": end_of_month}}
    if department is not None:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_of_month]},
                                    {"$lte": ["$date", end_of_month]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "logs_present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$logs.dow", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "logs_work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "logs_work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "logs_late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": ["$logs.late_minutes", 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "logs_total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            "$logs.late_minutes",
                            0,
                        ]
                    }
                },
                "logs_leave_count": {
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
                "logs_on_duty_count": {
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
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$logs_present_days"},
                "total_work_hours": {"$sum": "$logs_work_hours_sum"},
                "work_hours_count": {"$sum": "$logs_work_hours_count"},
                "late_count": {"$sum": "$logs_late_count"},
                "total_late_minutes": {"$sum": "$logs_total_late_minutes"},
                "leave_count": {"$sum": "$logs_leave_count"},
                "on_duty_count": {"$sum": "$logs_on_duty_count"},
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    agg_res = list(db.employees.aggregate(pipeline))

    items = []
    for row in agg_res:
        wh_count = row.get("work_hours_count", 0)
        avg_wh = (
            round_half_up(row.get("total_work_hours", 0.0) / wh_count, 2)
            if wh_count > 0
            else None
        )
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": row["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })

    return {
        "month": month,
        "items": items,
    }


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$match": {
                "total_late_minutes": {"$gt": 0},
            }
        },
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

    pipeline.extend([
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
                "_id": 0,
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": items,
    }


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> List[Dict[str, Any]]:
    from_iso = datetime.fromisoformat(f"{from_str}T00:00:00+00:00")
    to_date_obj = date.fromisoformat(to_str)
    to_next_iso = datetime.fromisoformat(f"{(to_date_obj + timedelta(days=1)).isoformat()}T00:00:00+00:00")

    return [
        {
            "$match": {
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
                "_id": 0,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_iso, to_next_iso],
                },
            }
        },
        {
            "$project": {
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dow": {"$dayOfWeek": "$date_dt"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
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
                    {"$count": "cnt"},
                ],
                "as": "hc_res",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_res.cnt", 0]}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_str": ASCENDING},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0],
                        },
                    }
                },
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_raw", None]},
                        {"$round": ["$moving_avg_raw", 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
                "_id": 0,
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date_str: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date_str: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    emp = db.employees.find_one({"department": department})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    from_d = date.fromisoformat(from_date_str)
    to_d = date.fromisoformat(to_date_str)

    if to_d < from_d:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="to date cannot be earlier than from date",
        )
    if (to_d - from_d).days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range exceeds 92 days",
        )

    pipeline = build_department_trend_pipeline(department, from_date_str, to_date_str)
    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "department": department,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[StatusType] = Query(None, alias="status"),
    from_date_str: Optional[str] = Query(None, alias="from"),
    to_date_str: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in ("attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid explain endpoint")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from > date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status_filter:
            q["status"] = status_filter
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_bson_for_json(res),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and month are required",
            )
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_bson_for_json(res),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required",
            )
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": clean_bson_for_json(res),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required",
            )
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_bson_for_json(res),
        }

    elif endpoint == "department_trend":
        if not department or not from_date_str or not to_date_str:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required",
            )
        if to_date_str < from_date_str:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to < from")
        pipeline = build_department_trend_pipeline(department, from_date_str, to_date_str)
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_bson_for_json(res),
        }


# --------------------------------------------------------------------------- #
# Enhanced Analytics: Weekly Timing, Work Hours & Peer Comparison
# --------------------------------------------------------------------------- #
def format_hours_to_amount_of_time(hours: float | None) -> str:
    if hours is None:
        return "—"
    if hours == 0.0:
        return "0h 00m"
    total_mins = int(round(float(hours) * 60))
    h = total_mins // 60
    m = total_mins % 60
    if h > 0 and m > 0:
        return f"{h}h {m:02d}m"
    elif h > 0:
        return f"{h}h 00m"
    else:
        return f"{m}m"


def format_dt_ist(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%H:%M:%S")


@app.get("/analytics/employees/{emp_code}/weekly")
def employee_weekly_timing(
    emp_code: str = Path(...),
    start_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Weekly timing work hours, amount of time breakdown, and shift compliance for an employee.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    if start_date is None:
        latest_log = db.attendance_logs.find_one({"emp_code": emp_code}, sort=[("date", -1)])
        if latest_log:
            ref_d = date.fromisoformat(latest_log["date"])
        else:
            ref_d = date.fromisoformat("2026-07-06")
        start_d = ref_d - timedelta(days=ref_d.weekday())
    else:
        start_d = date.fromisoformat(start_date)

    week_dates = [(start_d + timedelta(days=i)).isoformat() for i in range(7)]
    end_d_str = week_dates[-1]

    logs = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$in": week_dates}}))
    logs_by_date = {l["date"]: l for l in logs}

    try:
        sh_h, sh_m = map(int, emp["shift_start"].split(":"))
        eh_h, eh_m = map(int, emp["shift_end"].split(":"))
        start_mins = sh_h * 60 + sh_m
        end_mins = eh_h * 60 + eh_m
        if end_mins <= start_mins:
            end_mins += 24 * 60
        scheduled_shift_hours = round((end_mins - start_mins) / 60.0, 2)
    except Exception:
        scheduled_shift_hours = 9.0

    scheduled_weekly_hours = round(scheduled_shift_hours * 5, 2)

    daily_records = []
    total_work_hours = 0.0
    total_late_minutes = 0
    total_overtime_minutes = 0
    days_present = 0
    days_leave = 0
    days_absent = 0

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    for i, cur_date_str in enumerate(week_dates):
        cur_d = date.fromisoformat(cur_date_str)
        is_weekend = cur_d.weekday() >= 5
        day_name = day_names[cur_d.weekday()]

        log = logs_by_date.get(cur_date_str)
        if log:
            stat = log.get("status", "PRESENT")
            wh = log.get("work_hours")
            if wh is not None:
                total_work_hours += wh

            late_m = log.get("late_minutes", 0)
            ot_m = log.get("overtime_minutes", 0)
            half_day = log.get("half_day", False)

            total_late_minutes += late_m
            total_overtime_minutes += ot_m

            if stat in ("PRESENT", "WFH", "ON_DUTY"):
                days_present += 1
            elif stat == "LEAVE":
                days_leave += 1
            elif stat == "ABSENT":
                days_absent += 1

            punch_in_dt = log.get("punch_in")
            punch_out_dt = log.get("punch_out")

            daily_records.append({
                "date": cur_date_str,
                "day_name": day_name,
                "is_weekend": is_weekend,
                "status": stat,
                "punch_in_ms": dt_to_ms(punch_in_dt),
                "punch_out_ms": dt_to_ms(punch_out_dt),
                "punch_in_time": format_dt_ist(punch_in_dt),
                "punch_out_time": format_dt_ist(punch_out_dt),
                "work_hours": wh,
                "amount_of_time": format_hours_to_amount_of_time(wh),
                "late_minutes": late_m,
                "overtime_minutes": ot_m,
                "half_day": half_day,
            })
        else:
            default_status = "OFF" if is_weekend else "NOT_RECORDED"
            daily_records.append({
                "date": cur_date_str,
                "day_name": day_name,
                "is_weekend": is_weekend,
                "status": default_status,
                "punch_in_ms": None,
                "punch_out_ms": None,
                "punch_in_time": None,
                "punch_out_time": None,
                "work_hours": None,
                "amount_of_time": "—",
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
            })

    rounded_total_hours = round_half_up(total_work_hours, 2)
    avg_daily_hours = round_half_up(total_work_hours / days_present, 2) if days_present > 0 else 0.0
    on_time_pct = round_half_up(((days_present - (1 if total_late_minutes > 0 else 0)) / days_present) * 100, 1) if days_present > 0 else 100.0
    target_completion_pct = round_half_up((total_work_hours / scheduled_weekly_hours) * 100, 1) if scheduled_weekly_hours > 0 else 0.0

    return {
        "emp_code": emp_code,
        "employee_name": emp.get("name", emp_code),
        "department": emp.get("department"),
        "shift_start": emp.get("shift_start"),
        "shift_end": emp.get("shift_end"),
        "start_date": start_d.isoformat(),
        "end_date": end_d_str,
        "summary": {
            "total_work_hours": rounded_total_hours,
            "total_time_formatted": format_hours_to_amount_of_time(rounded_total_hours) if days_present > 0 else "—",
            "avg_daily_work_hours": avg_daily_hours,
            "days_present": days_present,
            "days_leave": days_leave,
            "days_absent": days_absent,
            "total_late_minutes": total_late_minutes,
            "total_overtime_minutes": total_overtime_minutes,
            "on_time_rate_pct": on_time_pct,
            "scheduled_shift_hours": scheduled_shift_hours,
            "scheduled_weekly_hours": scheduled_weekly_hours,
            "target_completion_pct": target_completion_pct,
        },
        "days": daily_records,
    }


@app.get("/analytics/employees/{emp_code}/comparison")
def employee_peer_comparison(
    emp_code: str = Path(...),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Comprehensive statistics comparing an employee against department peers and company benchmarks.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    if month is None:
        latest_log = db.attendance_logs.find_one({"emp_code": emp_code}, sort=[("date", -1)])
        month = latest_log["date"][:7] if latest_log else "2026-07"

    dept = emp.get("department", "Unknown")

    dept_emps = list(db.employees.find({"department": dept}, {"_id": 0}).sort([("emp_code", ASCENDING)]))
    dept_codes = [e["emp_code"] for e in dept_emps]

    all_emps = list(db.employees.find({}, {"_id": 0, "emp_code": 1}))
    all_codes = [e["emp_code"] for e in all_emps]

    month_regex = f"^{month}"
    pipeline = [
        {"$match": {"date": {"$regex": month_regex}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0.0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                "days_present": {
                    "$sum": {
                        "$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]
                    }
                },
            }
        },
    ]
    agg_results = {row["_id"]: row for row in db.attendance_logs.aggregate(pipeline)}

    peers_data = []
    dept_total_hours = 0.0
    dept_total_late = 0

    for pe in dept_emps:
        p_code = pe["emp_code"]
        st = agg_results.get(p_code, {})
        wh = round_half_up(st.get("total_work_hours", 0.0), 2)
        late_m = st.get("total_late_minutes", 0)
        dp = st.get("days_present", 0)
        avg_d = round_half_up(wh / dp, 2) if dp > 0 else 0.0

        dept_total_hours += wh
        dept_total_late += late_m

        peers_data.append({
            "emp_code": p_code,
            "name": pe.get("name", p_code),
            "total_work_hours": wh,
            "amount_of_time": format_hours_to_amount_of_time(wh),
            "days_present": dp,
            "avg_daily_work_hours": avg_d,
            "total_late_minutes": late_m,
            "is_current": (p_code == emp_code),
        })

    peers_data.sort(key=lambda x: (-x["total_work_hours"], x["total_late_minutes"]))
    for rank, p in enumerate(peers_data, 1):
        p["rank"] = rank

    curr_peer = next((p for p in peers_data if p["is_current"]), None)
    emp_hours = curr_peer["total_work_hours"] if curr_peer else 0.0
    emp_rank_dept = curr_peer["rank"] if curr_peer else len(dept_emps)
    emp_late = curr_peer["total_late_minutes"] if curr_peer else 0
    emp_days = curr_peer["days_present"] if curr_peer else 0
    emp_avg_daily = curr_peer["avg_daily_work_hours"] if curr_peer else 0.0

    dept_headcount = len(dept_emps)
    dept_avg_hours = round_half_up(dept_total_hours / dept_headcount, 2) if dept_headcount > 0 else 0.0
    dept_avg_late = round_half_up(dept_total_late / dept_headcount, 1) if dept_headcount > 0 else 0.0

    all_hours = [round_half_up(agg_results.get(c, {}).get("total_work_hours", 0.0), 2) for c in all_codes]
    all_hours.sort()
    total_company_emps = len(all_codes)
    company_avg_hours = round_half_up(sum(all_hours) / total_company_emps, 2) if total_company_emps > 0 else 0.0

    strictly_less = sum(1 for h in all_hours if h < emp_hours)
    ties = sum(1 for h in all_hours if h == emp_hours)
    percentile_rank = round_half_up(((strictly_less + 0.5 * ties) / total_company_emps) * 100, 1) if total_company_emps > 0 else 50.0

    def get_percentile(sorted_list: List[float], p: float) -> float:
        if not sorted_list:
            return 0.0
        k = (len(sorted_list) - 1) * p
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_list[int(k)]
        d0 = sorted_list[int(f)] * (c - k)
        d1 = sorted_list[int(c)] * (k - f)
        return round_half_up(d0 + d1, 2)

    dist_min = all_hours[0] if all_hours else 0.0
    dist_max = all_hours[-1] if all_hours else 0.0
    dist_q25 = get_percentile(all_hours, 0.25)
    dist_med = get_percentile(all_hours, 0.50)
    dist_q75 = get_percentile(all_hours, 0.75)

    return {
        "emp_code": emp_code,
        "employee_name": emp.get("name", emp_code),
        "department": dept,
        "month": month,
        "employee_metrics": {
            "total_work_hours": emp_hours,
            "total_time_formatted": format_hours_to_amount_of_time(emp_hours),
            "avg_daily_work_hours": emp_avg_daily,
            "days_present": emp_days,
            "total_late_minutes": emp_late,
            "rank_in_department": emp_rank_dept,
            "rank_in_company": sum(1 for h in all_hours if h > emp_hours) + 1,
        },
        "department_benchmark": {
            "department": dept,
            "headcount": dept_headcount,
            "avg_work_hours": dept_avg_hours,
            "avg_late_minutes": dept_avg_late,
            "hours_diff": round_half_up(emp_hours - dept_avg_hours, 2),
            "late_diff": round_half_up(emp_late - dept_avg_late, 1),
            "peers": peers_data,
        },
        "company_benchmark": {
            "total_employees": total_company_emps,
            "avg_work_hours": company_avg_hours,
            "percentile_rank": percentile_rank,
            "distribution": {
                "min": dist_min,
                "q25": dist_q25,
                "median": dist_med,
                "q75": dist_q75,
                "max": dist_max,
            },
        },
    }


@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
def get_dashboard():
    """
    Serve the interactive WorkPulse dashboard UI.
    """
    html_file = pathlib.Path(__file__).parent / "dashboard.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>WorkPulse Dashboard Ready</h1><p>dashboard.html not found</p>")

