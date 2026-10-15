"""Employee Attendance & Analytics API. All API instants are epoch milliseconds; Mongo stores UTC datetimes."""
import os, calendar, math
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
logs = db.attendance_logs
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_date_status")
    logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="attendance_status_date")

@app.on_event("startup")
def startup():
    try:
        ensure_indexes()
    except Exception:
        # Keep the process alive so /health can report MongoDB as unavailable.
        pass


def error(status: int, detail: str):
    raise HTTPException(status, detail)


def valid_date(value: str, label="date") -> date:
    try:
        if not isinstance(value, str): raise ValueError()
        d = date.fromisoformat(value)
        if d.isoformat() != value: raise ValueError()
        return d
    except Exception:
        error(422, f"{label} must be YYYY-MM-DD")


def valid_month(value: str) -> tuple[date, date]:
    try:
        if len(value) != 7 or value[4] != "-": raise ValueError()
        y, m = map(int, value.split("-"))
        first = date(y, m, 1)
        last = date(y, m, calendar.monthrange(y, m)[1])
        return first, last
    except Exception:
        error(422, "month must be YYYY-MM")


def valid_shift(value: str) -> str:
    try:
        if len(value) != 5 or value[2] != ":": raise ValueError()
        h, m = map(int, value.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59): raise ValueError()
        return value
    except Exception:
        error(422, "shift time must be HH:MM")


def epoch_ms(dt: Optional[datetime]):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def from_ms(value: Optional[int]) -> datetime:
    if value is None: return datetime.now(UTC).replace(microsecond=0)
    if type(value) is not int or value < 100_000_000_000 or value > 4102444800000:
        error(422, "timestamp must be an integer epoch-millisecond value")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None: return None
    if dt.tzinfo is None: return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def local(dt: datetime) -> datetime:
    return utc(dt).astimezone(IST)


def attendance_date(punch: datetime, emp: dict) -> str:
    ldt = local(punch)
    start, end = emp["shift_start"], emp["shift_end"]
    if end <= start and ldt.strftime("%H:%M") < end:
        return (ldt.date() - timedelta(days=1)).isoformat()
    return ldt.date().isoformat()


def shift_start_dt(att_date: str, shift_start: str) -> datetime:
    d = valid_date(att_date)
    h, m = map(int, shift_start.split(":"))
    return datetime.combine(d, time(h, m), IST)


def shift_end_dt(att_date: str, emp: dict) -> datetime:
    d = valid_date(att_date)
    sh, sm = map(int, emp["shift_start"].split(":"))
    eh, em = map(int, emp["shift_end"].split(":"))
    start_t, end_t = time(sh, sm), time(eh, em)
    end_date = d + timedelta(days=1) if end_t <= start_t else d
    return datetime.combine(end_date, end_t, IST)


def late_minutes(punch_in: Optional[datetime], att_date: str, emp: dict) -> int:
    if punch_in is None: return 0
    delta = (local(punch_in) - shift_start_dt(att_date, emp["shift_start"])).total_seconds()
    if delta <= 600: return 0
    return max(0, int(delta // 60))


def round_half_up(value, places=2):
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def derived_fields(doc: dict, emp: dict) -> dict:
    pi, po = doc.get("punch_in"), doc.get("punch_out")
    if doc.get("status") not in PRESENCE:
        doc["punch_in"] = doc["punch_out"] = None
        doc.update(work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
        return doc
    doc["late_minutes"] = late_minutes(pi, doc["date"], emp)
    if pi is None or po is None:
        doc.update(work_hours=None, overtime_minutes=0, half_day=False)
    else:
        seconds = (utc(po) - utc(pi)).total_seconds()
        doc["work_hours"] = round_half_up(seconds / 3600, 2)
        over = (local(po) - shift_end_dt(doc["date"], emp)).total_seconds() / 60
        doc["overtime_minutes"] = max(0, int(over // 1)) if over >= 30 else 0
        doc["half_day"] = doc["work_hours"] < 4.5
    return doc


def clean_doc(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    for k in ("punch_in", "punch_out", "created_at"):
        if k in doc: doc[k] = epoch_ms(doc[k])
    hist = doc.get("history", []) or []
    for entry in hist:
        if isinstance(entry.get("at"), datetime): entry["at"] = epoch_ms(entry["at"])
        changes = entry.get("changes", {}) or {}
        for change in changes.values():
            for side in ("from", "to", "before", "after"):
                if isinstance(change.get(side), datetime): change[side] = epoch_ms(change[side])
    doc["history"] = hist
    doc.setdefault("half_day", False)
    return doc


def page_args(page: int, page_size: int):
    if page < 1 or page_size < 1 or page_size > 100: error(422, "page must be >= 1 and page_size must be between 1 and 100")

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str
    @field_validator("shift_start", "shift_end")
    @classmethod
    def shift_check(cls, v): return valid_shift(v)
    @field_validator("joined_on")
    @classmethod
    def joined_check(cls, v): valid_date(v, "joined_on"); return v
    @model_validator(mode="after")
    def shift_not_equal(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def timestamp_check(cls, v):
        if v is not None: from_ms(v)
        return v

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    @field_validator("punched_at")
    @classmethod
    def timestamp_check(cls, v):
        if v is not None: from_ms(v)
        return v

class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def timestamp_check(cls, v):
        if v is not None: from_ms(v)
        return v

@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status": "ok"}
    except Exception: error(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try: employees.insert_one(doc)
    except DuplicateKeyError: error(409, "emp_code already exists")
    return clean_doc(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    cur = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)
    return {"items": [clean_doc(x) for x in cur], "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp: error(404, "unknown employee")
    ts = from_ms(body.punched_at)
    d = attendance_date(ts, emp)
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": late_minutes(ts, d, emp), "overtime_minutes": 0, "half_day": False, "history": []}
    try: logs.insert_one(doc)
    except DuplicateKeyError: error(409, "already punched in for this date")
    return clean_doc(doc)


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if status is not None: q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    return q

@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    for v, n in ((date_from,"date_from"),(date_to,"date_to")):
        if v is not None: valid_date(v,n)
    if date_from and date_to and date_from > date_to: error(422,"date_from must be on or before date_to")
    if status and status not in STATUSES: error(422,"invalid status")
    q = attendance_query(emp_code,date_from,date_to,status)
    total = logs.count_documents(q)
    cur = logs.find(q, {"_id":0}).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items":[clean_doc(x) for x in cur],"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code":body.emp_code})
    if not emp: error(404,"unknown employee")
    ts = from_ms(body.punched_at)
    # Most recent still-open record with punch-in at or before the requested instant.
    rec = logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":ts},"punch_out":None}, sort=[("punch_in",DESCENDING)])
    if not rec:
        already = logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":ts},"punch_out":{"$ne":None}},sort=[("punch_in",DESCENDING)])
        if already: error(409,"record already punched out")
        error(404,"no punch-in found")
    if ts <= utc(rec["punch_in"]) or ts > utc(rec["punch_in"]) + timedelta(hours=24): error(422,"punch-out must be after punch-in and within 24 hours")
    rec2 = dict(rec); rec2["punch_out"] = ts; derived_fields(rec2,emp)
    updated = logs.find_one_and_update({"_id":rec["_id"],"punch_out":None},{"$set":{"punch_out":ts,"work_hours":rec2["work_hours"],"overtime_minutes":rec2["overtime_minutes"],"half_day":rec2["half_day"]}},return_document=ReturnDocument.AFTER)
    if not updated: error(409,"record already punched out")
    return clean_doc(updated)

@app.patch("/attendance/{emp_code}/{att_date}")
def regularize(emp_code: str, att_date: str, body: RegularizeIn):
    valid_date(att_date)
    emp = employees.find_one({"emp_code":emp_code})
    rec = logs.find_one({"emp_code":emp_code,"date":att_date})
    if not emp or not rec: error(404,"employee or attendance record not found")
    old = dict(rec); old.pop("_id",None); old.setdefault("history",[]); old.setdefault("half_day",False)
    for f in ("punch_in", "punch_out"):
        if old.get(f) is not None: old[f] = utc(old[f]).replace(microsecond=0)
    new = dict(old)
    supplied = body.model_dump(exclude_unset=True)
    for f in ("status","punch_in","punch_out"):
        if f in supplied:
            val = supplied[f]
            if f in ("punch_in","punch_out"): val = from_ms(val) if val is not None else None
            new[f] = val
    if new.get("status") in ("ABSENT","LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            error(422,"ABSENT and LEAVE cannot have punch times")
        new["punch_in"] = new["punch_out"] = None
    elif new.get("status") in PRESENCE:
        if not new.get("punch_in"): error(422,"presence status requires punch_in")
        if local(new["punch_in"]).date().isoformat() != att_date: error(422,"punch_in must be on attendance date")
        if new.get("punch_out"):
            if utc(new["punch_out"]) <= utc(new["punch_in"]) or utc(new["punch_out"]) > utc(new["punch_in"])+timedelta(hours=24): error(422,"invalid punch_out interval")
    for f in ("punch_in","punch_out"):
        if new.get(f): new[f] = utc(new[f]).replace(microsecond=0)
    derived_fields(new,emp)
    changes = {}
    for f in ("status","punch_in","punch_out","work_hours","late_minutes","overtime_minutes","half_day"):
        if old.get(f) != new.get(f): changes[f] = {"before":old.get(f),"after":new.get(f)}
    if not changes: error(422,"regularization does not change anything")
    hist = {"at":datetime.now(UTC).replace(microsecond=0),"by":body.regularized_by,"reason":body.reason,"changes":{k:{"from":v["before"],"to":v["after"]} for k,v in changes.items()}}
    # Compare-and-swap on old history length prevents silent lost updates.
    old_hist = old.get("history",[])
    update = {k:new.get(k) for k in ("status","punch_in","punch_out","work_hours","late_minutes","overtime_minutes","half_day")}
    update["history"] = old_hist + [hist]
    history_filter = {"history": rec["history"]} if "history" in rec else {"history": {"$exists": False}}
    result = logs.find_one_and_update({"_id":rec["_id"], **history_filter},{"$set":update},return_document=ReturnDocument.AFTER)
    if not result: error(409,"record changed concurrently; retry")
    return clean_doc(result)


def working_days(start: date, end: date) -> int:
    if start > end: return 0
    return sum(1 for i in range((end-start).days+1) if (start+timedelta(days=i)).weekday()<5)

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    first,last=valid_month(month)
    emp=employees.find_one({"emp_code":emp_code},{"_id":0})
    if not emp: error(404,"unknown employee")
    joined=valid_date(emp["joined_on"],"joined_on")
    wd=working_days(max(first,joined),last)
    pipeline=[{"$match":{"emp_code":emp_code,"date":{"$gte":first.isoformat(),"$lte":last.isoformat()}}},
      {"$group":{"_id":None,"present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$lte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},6]},{"$gte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},2]}]},{"$cond":[{"$eq":[{"$ifNull":["$half_day",False]},True]},0.5,1]},0]}},"leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},"total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},"total_overtime_minutes":{"$sum":{"$ifNull":["$overtime_minutes",0]}}}}]
    row=next(logs.aggregate(pipeline),{})
    pd=round_half_up(row.get("present_days",0),2)
    return {"emp_code":emp_code,"month":month,"working_days":wd,"present_days":pd,"leave_days":row.get("leave_days",0),"late_count":row.get("late_count",0),"total_late_minutes":row.get("total_late_minutes",0),"total_overtime_minutes":row.get("total_overtime_minutes",0),"attendance_pct":round_half_up(pd/wd*100,4) if wd else None}

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str]=None):
    first,last=valid_month(month); end=last.isoformat()
    match={"joined_on":{"$lte":end}}
    if department is not None: match["department"]=department
    # Employee-led aggregation ensures people with no attendance records remain in headcount.
    pipeline=[{"$match":match},{"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",first.isoformat()]},{"$lte":["$date",end]}]}}}],"as":"month_logs"}},
      {"$unwind":{"path":"$month_logs","preserveNullAndEmptyArrays":True}},
      {"$group":{"_id":"$department","headcount":{"$addToSet":"$emp_code"},
        "present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$month_logs.status",list(PRESENCE)]},{"$gte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$month_logs.date"}}},2]},{"$lte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$month_logs.date"}}},6]}]},{"$cond":[{"$eq":[{"$ifNull":["$month_logs.half_day",False]},True]},0.5,1]},0]}},
        "hours_values":{"$push":{"$cond":[{"$and":[{"$in":["$month_logs.status",list(PRESENCE)]},{"$ne":["$month_logs.work_hours",None]}]},"$month_logs.work_hours",None]}},
        "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$month_logs.late_minutes",0]},0]},1,0]}},"total_late_minutes":{"$sum":{"$ifNull":["$month_logs.late_minutes",0]}},
        "leave_count":{"$sum":{"$cond":[{"$eq":["$month_logs.status","LEAVE"]},1,0]}},"on_duty_count":{"$sum":{"$cond":[{"$eq":["$month_logs.status","ON_DUTY"]},1,0]}}}},
      {"$project":{"department":"$_id","headcount":{"$size":"$headcount"},"present_days":1,"avg_work_hours":{"$let":{"vars":{"vals":{"$filter":{"input":"$hours_values","as":"v","cond":{"$ne":["$$v",None]}}}},"in":{"$cond":[{"$gt":[{"$size":"$$vals"},0]},{"$avg":"$$vals"},None]}}},"late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}},
      {"$sort":{"department":1}}]
    rows=list(employees.aggregate(pipeline))
    out=[]
    for r in rows:
        if r["headcount"]==0: continue
        r.pop("_id",None)
        r["present_days"]=round_half_up(r.get("present_days",0),2)
        if r.get("avg_work_hours") is not None: r["avg_work_hours"]=round_half_up(r["avg_work_hours"],2)
        out.append(r)
    return {"month":month,"items":out}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int=Query(10,ge=1,le=50), department: Optional[str]=None):
    first,last=valid_month(month)
    pipe=[{"$match":{"date":{"$gte":first.isoformat(),"$lte":last.isoformat()},"late_minutes":{"$gt":0}}},
      {"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"},"late_count":{"$sum":1}}},
      {"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}},{"$unwind":"$employee"}]
    if department is not None: pipe.append({"$match":{"employee.department":department}})
    pipe += [{"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},
      {"$match":{"rank":{"$lte":limit}}},{"$sort":{"total_late_minutes":-1,"_id":1}},
      {"$project":{"_id":0,"emp_code":"$_id","name":"$employee.name","department":"$employee.department","total_late_minutes":1,"late_count":1,"rank":1}}]
    return {"month":month,"items":list(logs.aggregate(pipe,allowDiskUse=True))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str=Query(...,alias="from"), to_date: str=Query(...,alias="to")):
    start=valid_date(from_date,"from"); end=valid_date(to_date,"to")
    days=(end-start).days+1
    if end<start or days>92: error(422,"date range must be ordered and no longer than 92 days")
    if not employees.find_one({"department":department}): error(404,"unknown department")
    # Employee-led pipeline builds the calendar in MongoDB, so it also works when no logs exist.
    pipe=[
      {"$match":{"department":department}},
      {"$group":{"_id":None,"people":{"$push":{"emp_code":"$emp_code","joined_on":"$joined_on"}}}},
      {"$set":{"days":{"$map":{"input":{"$range":[0,days]},"as":"n","in":{"$dateAdd":{"startDate":{"$dateFromString":{"dateString":from_date}},"unit":"day","amount":"$$n"}}}}}},
      {"$unwind":"$days"},
      {"$set":{"date":{"$dateToString":{"date":"$days","format":"%Y-%m-%d"}},"is_working_day":{"$and":[{"$gte":[{"$dayOfWeek":"$days"},2]},{"$lte":[{"$dayOfWeek":"$days"},6]}]},"joined_people":{"$filter":{"input":"$people","as":"p","cond":{"$lte":["$$p.joined_on",{"$dateToString":{"date":"$days","format":"%Y-%m-%d"}}]}}}}},
      {"$set":{"headcount":{"$size":"$joined_people"},"codes":{"$map":{"input":"$joined_people","as":"p","in":"$$p.emp_code"}}}},
      {"$lookup":{"from":"attendance_logs","let":{"day":"$date","codes":"$codes"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$date","$$day"]},{"$in":["$emp_code","$$codes"]}]}}},{"$project":{"status":1,"late_minutes":1,"half_day":1,"_id":0}}],"as":"day_logs"}},
      {"$set":{"present_count":{"$reduce":{"input":"$day_logs","initialValue":0,"in":{"$add":["$$value",{"$cond":[{"$in":["$$this.status",list(PRESENCE)]},{"$cond":[{"$eq":[{"$ifNull":["$$this.half_day",False]},True]},0.5,1]},0]}]}}},"late_count":{"$size":{"$filter":{"input":"$day_logs","as":"l","cond":{"$gt":[{"$ifNull":["$$l.late_minutes",0]},0]}}}}}},
      {"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},{"$round":[{"$divide":["$present_count","$headcount"]},4]},None]}}},
      {"$setWindowFields":{"sortBy":{"days":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},
      {"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":1,"moving_avg_7d":{"$round":["$moving_avg_7d",4]}}},
      {"$sort":{"date":1}}
    ]
    rows=list(employees.aggregate(pipe,allowDiskUse=True))
    return {"department":department,"from":from_date,"to":to_date,"items":rows}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str]=None, month: Optional[str]=None, department: Optional[str]=None,
                     limit: int=Query(10,ge=1,le=50), date_from: Optional[str]=None,date_to: Optional[str]=None,status: Optional[str]=None,
                     from_date: Optional[str]=Query(None,alias="from"),to_date: Optional[str]=Query(None,alias="to"),
                     page: int=Query(1,ge=1),page_size: int=Query(20,ge=1,le=100)):
    allowed={"attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"}
    if endpoint not in allowed: error(422,"unknown endpoint")
    if endpoint=="attendance_list":
        for v,n in ((date_from,"date_from"),(date_to,"date_to")):
            if v: valid_date(v,n)
        if date_from and date_to and date_from>date_to: error(422,"date_from must be on or before date_to")
        q=attendance_query(emp_code,date_from,date_to,status)
        cmd={"find":"attendance_logs","filter":q,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size}
        collection="attendance_logs"
    elif endpoint=="employee_monthly":
        if not emp_code or not month: error(422,"emp_code and month are required")
        valid_month(month)
        first,last=valid_month(month); collection="attendance_logs"
        cmd={"aggregate":"attendance_logs","pipeline":[{"$match":{"emp_code":emp_code,"date":{"$gte":first.isoformat(),"$lte":last.isoformat()}}},{"$group":{"_id":"$emp_code","count":{"$sum":1}}}],"cursor":{}}
    elif endpoint=="department_summary":
        if not month: error(422,"month is required")
        valid_month(month); first,last=valid_month(month); collection="employees"
        match={"joined_on":{"$lte":last.isoformat()}}
        if department is not None: match["department"]=department
        cmd={"aggregate":"employees","pipeline":[{"$match":match},{"$lookup":{"from":"attendance_logs","localField":"emp_code","foreignField":"emp_code","as":"logs"}},{"$limit":100000}],"cursor":{}}
    elif endpoint=="late_leaderboard":
        if not month: error(422,"month is required")
        valid_month(month); first,last=valid_month(month); collection="attendance_logs"
        pipe=[{"$match":{"date":{"$gte":first.isoformat(),"$lte":last.isoformat()},"late_minutes":{"$gt":0}}},{"$group":{"_id":"$emp_code","total":{"$sum":"$late_minutes"}}}]
        if department is not None: pipe += [{"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"e"}},{"$match":{"e.department":department}}]
        cmd={"aggregate":"attendance_logs","pipeline":pipe,"cursor":{}}
    else:
        if not department or not from_date or not to_date: error(422,"department, from and to are required")
        a=valid_date(from_date,"from"); b=valid_date(to_date,"to")
        if b<a or (b-a).days+1>92: error(422,"invalid trend date range")
        collection="attendance_logs"
        cmd={"aggregate":"attendance_logs","pipeline":[{"$match":{"date":{"$gte":a.isoformat(),"$lte":b.isoformat()}}},{"$sort":{"date":1}},{"$limit":100000}],"cursor":{}}
    try:
        raw=db.command({"explain":cmd,"verbosity":"executionStats"})
    except Exception as e:
        error(422,f"explain could not run: {str(e)}")
    return {"endpoint":endpoint,"collection":collection,"explain":raw}
