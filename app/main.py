"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (loaded from .env for convenience)
"""
import json
import os
import calendar
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup: create indexes idempotently
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", ASCENDING)]
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(value: float, decimals: int) -> float:
    """Round value to `decimals` places using ROUND_HALF_UP (not banker's)."""
    d = Decimal(str(value))
    factor = Decimal("0." + "0" * decimals) if decimals > 0 else Decimal("1")
    return float(d.quantize(factor, rounding=ROUND_HALF_UP))


def dt_to_ms(dt: datetime) -> int:
    """Naive UTC datetime (as returned by PyMongo) -> epoch ms."""
    return int(calendar.timegm(dt.timetuple()) * 1000 + dt.microsecond // 1000)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> timezone-aware UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Return the attendance date string (YYYY-MM-DD, IST) for a given punch-in.
    For overnight shifts (shift_end <= shift_start) a punch-in that falls in the
    early hours of the next calendar day still belongs to the previous shift date.
    """
    pi = punch_in_utc.replace(microsecond=0)
    if pi.tzinfo is None:
        pi = pi.replace(tzinfo=timezone.utc)
    pi_ist = pi.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if is_overnight:
        pi_time_mins = pi_ist.hour * 60 + pi_ist.minute
        if pi_time_mins < (eh * 60 + em):
            # Punch-in is in the early-morning window that belongs to yesterday's shift
            return (pi_ist.date() - timedelta(days=1)).isoformat()
    return pi_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str) -> int:
    """
    Returns minutes late (integer floor). Zero if <= 10-minute grace period.
    Converts punch_in to IST before comparing to shift_start.
    """
    pi = punch_in_utc.replace(microsecond=0)
    if pi.tzinfo is None:
        pi = pi.replace(tzinfo=timezone.utc)
    pi_ist = pi.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = pi_ist.replace(hour=sh, minute=sm, second=0, microsecond=0)
    elapsed_secs = (pi_ist - shift_start_dt).total_seconds()
    # Grace period: <= 600 seconds (10 minutes exactly is NOT late)
    if elapsed_secs > 600:
        return int(elapsed_secs // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """Duration in hours, rounded half-up to 2 decimal places."""
    pi = punch_in_utc.replace(microsecond=0)
    po = punch_out_utc.replace(microsecond=0)
    secs = (po - pi).total_seconds()
    return round_half_up(secs / 3600, 2)


def compute_overtime_minutes(
    punch_out_utc: datetime,
    shift_end: str,
    attendance_date_str: str,
    shift_start: str,
) -> int:
    """
    Returns overtime in whole minutes. Zero if under 30-minute minimum.
    Handles overnight shifts (shift_end on the next calendar day).
    """
    po = punch_out_utc.replace(microsecond=0)
    if po.tzinfo is None:
        po = po.replace(tzinfo=timezone.utc)
    po_ist = po.astimezone(IST)

    eh, em = map(int, shift_end.split(":"))
    sh, sm = map(int, shift_start.split(":"))
    base = datetime.fromisoformat(attendance_date_str)

    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if is_overnight:
        # shift_end falls on the next calendar day relative to the attendance date
        shift_end_naive = base.replace(hour=eh, minute=em, second=0) + timedelta(days=1)
        shift_end_dt = shift_end_naive.replace(tzinfo=IST)
    else:
        shift_end_dt = base.replace(hour=eh, minute=em, second=0, tzinfo=IST)

    elapsed_secs = (po_ist - shift_end_dt).total_seconds()
    # Minimum 30 minutes (1800 seconds) to count as overtime
    if elapsed_secs >= 1800:
        return int(elapsed_secs // 60)
    return 0


def serialize_employee(doc: dict) -> dict:
    """Strip _id and convert created_at datetime to epoch ms."""
    doc = dict(doc)
    doc.pop("_id", None)
    if "created_at" in doc and doc["created_at"] is not None:
        doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc


def _convert_changes(changes: dict) -> dict:
    """Convert datetime values inside history changes to epoch ms."""
    result = {}
    for field, delta in changes.items():
        from_val = delta.get("from")
        to_val = delta.get("to")
        if field in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = dt_to_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = dt_to_ms(to_val)
        result[field] = {"from": from_val, "to": to_val}
    return result


def serialize_record(doc: dict) -> dict:
    """Strip _id, convert punch_in/punch_out to epoch ms, convert history entries."""
    doc = dict(doc)
    doc.pop("_id", None)
    for field in ("punch_in", "punch_out"):
        if field in doc and doc[field] is not None:
            if isinstance(doc[field], datetime):
                doc[field] = dt_to_ms(doc[field])
    history = doc.get("history", [])
    new_history = []
    for entry in history:
        entry = dict(entry)
        if "at" in entry and isinstance(entry["at"], datetime):
            entry["at"] = dt_to_ms(entry["at"])
        if "changes" in entry:
            entry["changes"] = _convert_changes(entry["changes"])
        new_history.append(entry)
    doc["history"] = new_history
    # Ensure all required fields are present (for legacy records that may omit them)
    doc.setdefault("work_hours", None)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    return doc


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
EPOCH_MIN = 100_000_000_000   # ~ 1973
EPOCH_MAX = 4_102_444_800_000  # ~ 2100


def validate_epoch_ms(v):
    if v is None:
        return v
    if isinstance(v, float):
        raise ValueError("epoch ms must be an integer, not a float")
    if not isinstance(v, int):
        raise ValueError("epoch ms must be an integer")
    if v < EPOCH_MIN or v > EPOCH_MAX:
        raise ValueError(f"epoch ms out of range [{EPOCH_MIN}, {EPOCH_MAX}]")
    return v


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        return validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_ts(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("invalid status")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(tzinfo=None)  # store naive UTC
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        ts_utc = ms_to_dt(body.punched_at).replace(tzinfo=None)  # store naive UTC
    else:
        ts_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    ts_utc = ts_utc.replace(microsecond=0)

    att_date = attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(ts_utc, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        po_utc = ms_to_dt(body.punched_at).replace(tzinfo=None)
    else:
        po_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    po_utc = po_utc.replace(microsecond=0)

    # Find most recent open record with punch_in <= po_utc
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": po_utc}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        # Distinguish: already punched out vs never punched in
        existing = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": po_utc}},
            sort=[("punch_in", DESCENDING)],
        )
        if existing and existing.get("punch_out") is not None:
            raise HTTPException(409, detail="Already punched out")
        raise HTTPException(404, detail="No open punch-in found")

    pi_utc = rec["punch_in"]

    if po_utc <= pi_utc:
        raise HTTPException(422, detail="punch_out must be after punch_in")
    if (po_utc - pi_utc).total_seconds() > 86400:
        raise HTTPException(422, detail="punch_out cannot be more than 24 hours after punch_in")

    wh = compute_work_hours(pi_utc, po_utc)
    ot = compute_overtime_minutes(po_utc, emp["shift_end"], rec["date"], emp["shift_start"])
    hd = wh < 4.50

    # Atomic update — only if punch_out is still null (concurrency guard)
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "punch_in": rec["punch_in"], "punch_out": None},
        {"$set": {"punch_out": po_utc, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, detail="Already punched out")
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from cannot be after date_to")
    q: dict = {}
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
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_record(r) for r in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(...),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else rec["status"]
    new_pi_ms = body.punch_in
    new_po_ms = body.punch_out

    if new_status in ("ABSENT", "LEAVE"):
        if new_pi_ms is not None or new_po_ms is not None:
            raise HTTPException(422, detail="Cannot set punch times with ABSENT/LEAVE status")
        new_pi = None
        new_po = None
    else:
        # Presence status: resolve punch times
        new_pi = (
            ms_to_dt(new_pi_ms).replace(tzinfo=None).replace(microsecond=0)
            if new_pi_ms is not None
            else rec.get("punch_in")
        )
        new_po = (
            ms_to_dt(new_po_ms).replace(tzinfo=None).replace(microsecond=0)
            if new_po_ms is not None
            else rec.get("punch_out")
        )
        if new_pi is None:
            raise HTTPException(422, detail="Presence status requires punch_in")

        # punch_in must remain on the attendance date (attach UTC before calling helper)
        calc_date = attendance_date(
            new_pi.replace(tzinfo=timezone.utc), emp["shift_start"], emp["shift_end"]
        )
        if calc_date != date:
            raise HTTPException(422, detail="punch_in must remain on the attendance date")

        if new_po is not None:
            if new_po <= new_pi:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (new_po - new_pi).total_seconds() > 86400:
                raise HTTPException(422, detail="punch_out cannot be more than 24h after punch_in")

    # Recompute derived fields
    if new_pi is not None:
        new_late = compute_late_minutes(new_pi, emp["shift_start"])
    else:
        new_late = 0

    if new_pi is not None and new_po is not None:
        new_wh = compute_work_hours(new_pi, new_po)
        new_ot = compute_overtime_minutes(new_po, emp["shift_end"], date, emp["shift_start"])
        new_hd = new_wh < 4.50
    else:
        new_wh = None
        new_ot = 0
        new_hd = False

    # Build changes dict (only fields that actually changed)
    changes: dict = {}
    old_pi = rec.get("punch_in")
    old_po = rec.get("punch_out")
    old_wh = rec.get("work_hours")
    old_late = rec.get("late_minutes", 0)
    old_ot = rec.get("overtime_minutes", 0)
    old_hd = rec.get("half_day", False)
    old_status = rec["status"]

    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_pi != old_pi:
        changes["punch_in"] = {"from": old_pi, "to": new_pi}
    if new_po != old_po:
        changes["punch_out"] = {"from": old_po, "to": new_po}
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(422, detail="No changes detected")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(tzinfo=None),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    current_history_len = len(rec.get("history", []))

    set_fields = {
        "status": new_status,
        "punch_in": new_pi,
        "punch_out": new_po,
        "work_hours": new_wh,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_hd,
    }
    result = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "history": {"$size": current_history_len},
        },
        {
            "$set": set_fields,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, detail="Concurrent edit detected")
    return serialize_record(result)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    year, mon = map(int, month.split("-"))
    month_start = date(year, mon, 1)
    month_end = date(year, mon, calendar.monthrange(year, mon)[1])

    # Working days from max(joined_on, month_start) to month_end (Mon–Fri)
    joined = date.fromisoformat(emp["joined_on"])
    count_start = max(joined, month_start)
    working_days = sum(
        1
        for i in range((month_end - count_start).days + 1)
        if (count_start + timedelta(days=i)).weekday() < 5
    )

    # MongoDB $dayOfWeek: 1=Sun, 2=Mon, 3=Tue, 4=Wed, 5=Thu, 6=Fri, 7=Sat
    # Mon–Fri = [2, 3, 4, 5, 6]
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
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
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
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
    results = list(db.attendance_logs.aggregate(pipeline))
    if results:
        r = results[0]
        present_days = r["present_days"]
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late_minutes = r["total_late_minutes"]
        total_overtime_minutes = r["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

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


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, mon = map(int, month.split("-"))
    month_start = date(year, mon, 1).isoformat()
    month_end = date(year, mon, calendar.monthrange(year, mon)[1]).isoformat()

    emp_match: dict = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department

    # MongoDB $dayOfWeek: Mon–Fri = [2,3,4,5,6]
    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ec"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            {"$ifNull": ["$logs.status", ""]},
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": {
                                                            "$ifNull": ["$logs.date", month_start]
                                                        }
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {
                                        "$eq": [
                                            {"$ifNull": ["$logs.half_day", False]},
                                            True,
                                        ]
                                    },
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            {"$ifNull": ["$logs.status", ""]},
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$gt": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": [{"$ifNull": ["$logs.status", ""]}, "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": [{"$ifNull": ["$logs.status", ""]}, "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list"},
                        None,
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

    rows = list(db.employees.aggregate(pipeline))
    items = []
    for r in rows:
        avg_wh = r.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)
        items.append(
            {
                "department": r["department"],
                "headcount": r["headcount"],
                "present_days": r["present_days"],
                "avg_work_hours": avg_wh,
                "late_count": r["late_count"],
                "total_late_minutes": r["total_late_minutes"],
                "leave_count": r["leave_count"],
                "on_duty_count": r["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, mon = map(int, month.split("-"))
    month_start = date(year, mon, 1).isoformat()
    month_end = date(year, mon, calendar.monthrange(year, mon)[1]).isoformat()

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
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
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},  # drops any logs for unknown employees
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "emp_code": "$_id",
                    "total_late_minutes": 1,
                    "late_count": 1,
                    "name": "$emp.name",
                    "department": "$emp.department",
                }
            },
        ]
    )

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking
    ranked = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0 and row["total_late_minutes"] < rows[i - 1]["total_late_minutes"]:
            rank = i + 1
        if rank > limit:
            break
        ranked.append(
            {
                "rank": rank,
                "emp_code": row["emp_code"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": row["total_late_minutes"],
                "late_count": row["late_count"],
            }
        )
    return {"month": month, "items": ranked}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, detail="Invalid date format")
    if d_to < d_from:
        raise HTTPException(422, detail="to cannot be before from")
    if (d_to - d_from).days > 92:
        raise HTTPException(422, detail="Range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, detail="Department not found")

    employees = list(
        db.employees.find(
            {"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}
        )
    )
    emp_codes = [e["emp_code"] for e in employees]

    logs = list(
        db.attendance_logs.find(
            {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            },
            {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1},
        )
    )

    logs_by_date: dict = defaultdict(list)
    for log in logs:
        logs_by_date[log["date"]].append(log)

    all_days = []
    num_days = (d_to - d_from).days + 1
    for i in range(num_days):
        d = d_from + timedelta(days=i)
        d_str = d.isoformat()
        is_working = d.weekday() < 5  # Mon–Fri
        headcount = sum(1 for e in employees if e["joined_on"] <= d_str)
        day_logs = logs_by_date.get(d_str, [])
        present_count = sum(
            0.5 if lg.get("half_day") else 1.0
            for lg in day_logs
            if lg["status"] in ("PRESENT", "WFH", "ON_DUTY")
        )
        late_count = sum(1 for lg in day_logs if lg.get("late_minutes", 0) > 0)
        if is_working and headcount > 0:
            rate: Optional[float] = round_half_up(present_count / headcount, 4)
        else:
            rate = None
        all_days.append(
            {
                "date": d_str,
                "is_working_day": is_working,
                "headcount": headcount,
                "present_count": present_count,
                "late_count": late_count,
                "attendance_rate": rate,
                "_rate": rate,  # temp field for moving average
            }
        )

    # 7-day moving average (mean of non-null rates in [max(0, i-6), i])
    for i, day in enumerate(all_days):
        window_start = max(0, i - 6)
        window_rates = [
            all_days[j]["_rate"]
            for j in range(window_start, i + 1)
            if all_days[j]["_rate"] is not None
        ]
        if window_rates:
            day["moving_avg_7d"] = round_half_up(
                sum(window_rates) / len(window_rates), 4
            )
        else:
            day["moving_avg_7d"] = None

    for day in all_days:
        del day["_rate"]

    return {"department": department, "from": from_date, "to": to_date, "items": all_days}


def safe_explain(explain_doc) -> dict:
    """Convert explain output (may contain BSON types) to a plain JSON-safe dict."""
    return json.loads(json_util.dumps(explain_doc))


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str = Path(
        ...,
        enum=[
            "attendance_list",
            "employee_monthly",
            "department_summary",
            "late_leaderboard",
            "department_trend",
        ],
    ),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        q: dict = {}
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
        skip = (page - 1) * page_size
        # Use explain command for executionStats verbosity
        skip = (page - 1) * page_size
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": safe_explain(explain)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required")
        year, mon = map(int, month.split("-"))
        month_start = date(year, mon, 1).isoformat()
        month_end = date(year, mon, calendar.monthrange(year, mon)[1]).isoformat()
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": month_start, "$lte": month_end},
                }
            },
            {"$group": {"_id": None, "total": {"$sum": 1}}},
        ]
        explain_doc = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True, cursor={}
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": safe_explain(explain_doc),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")
        year, mon = map(int, month.split("-"))
        month_end = date(year, mon, calendar.monthrange(year, mon)[1]).isoformat()
        emp_match: dict = {"joined_on": {"$lte": month_end}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "logs",
                }
            },
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        explain_doc = db.command(
            "aggregate", "employees", pipeline=pipeline, explain=True, cursor={}
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": safe_explain(explain_doc),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")
        year, mon = map(int, month.split("-"))
        month_start = date(year, mon, 1).isoformat()
        month_end = date(year, mon, calendar.monthrange(year, mon)[1]).isoformat()
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start, "$lte": month_end},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
            {"$sort": {"total_late_minutes": -1}},
        ]
        if department:
            pipeline.append(
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "_id",
                        "foreignField": "emp_code",
                        "as": "emp",
                    }
                }
            )
            pipeline.append({"$unwind": "$emp"})
            pipeline.append({"$match": {"emp.department": department}})
        explain_doc = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True, cursor={}
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": safe_explain(explain_doc),
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, detail="department, from, and to are required")
        emp_codes = [
            e["emp_code"]
            for e in db.employees.find(
                {"department": department}, {"emp_code": 1, "_id": 0}
            )
        ]
        q = {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": from_date, "$lte": to_date},
        }
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": safe_explain(explain),
        }

    raise HTTPException(422, detail="Unknown endpoint")
