"""Student college admission eligibility API.

Run with: uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyodbc
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

LOG_DIR = os.getenv("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("eligibility")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s")
    _file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "eligibility.log"),
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    _file_handler.setFormatter(_fmt)
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(_fmt)
    logger.addHandler(_file_handler)
    logger.addHandler(_console_handler)

DB_LOGIN_TIMEOUT_SECONDS = int(os.getenv("DB_LOGIN_TIMEOUT", "5"))
DB_RETRY_SECONDS = int(os.getenv("DB_RETRY_SECONDS", "15"))
MAX_BODY_BYTES = 16 * 1024
MAX_RECOMMENDATIONS = 5
REQUIRED_SUBJECT_COUNT = 6

_PREFERRED_DRIVERS = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "SQL Server")


def _pick_driver() -> str:
    """Use DB_DRIVER if set, otherwise the newest SQL Server ODBC driver installed."""
    if os.getenv("DB_DRIVER"):
        return os.environ["DB_DRIVER"]
    installed = set(pyodbc.drivers())
    return next((d for d in _PREFERRED_DRIVERS if d in installed), _PREFERRED_DRIVERS[0])


def build_connection_string() -> Tuple[str, str]:
    """Returns the ODBC connection string and a description without the password."""
    explicit = os.getenv("DB_CONNECTION_STRING")
    if explicit:
        return explicit, "DB_CONNECTION_STRING (from environment)"

    driver = _pick_driver()
    server = os.getenv("DB_SERVER", "localhost,1433")
    database = os.getenv("DB_NAME", "CollegeAdmissions")
    user = os.getenv("DB_USER", "")
    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={database}"]
    if user:
        password = os.getenv("DB_PASSWORD", "").replace("}", "}}")
        parts += [f"UID={user}", f"PWD={{{password}}}"]
    else:
        parts.append("Trusted_Connection=yes")
    if driver.startswith("ODBC Driver"):
        parts.append("TrustServerCertificate=" + os.getenv("DB_TRUST_SERVER_CERTIFICATE", "yes"))
    auth = f"user {user}" if user else "Windows authentication"
    return ";".join(parts) + ";", f"driver '{driver}', server '{server}', database '{database}', {auth}"


DB_CONNECTION_STRING, DB_DESCRIPTION = build_connection_string()

PCM = ("Physics", "Chemistry", "Mathematics")
PCB = ("Physics", "Chemistry", "Biology")
COMMERCE = ("Accountancy", "Business Studies", "Economics")

_COURSE_DEFINITIONS: Dict[str, Tuple[str, Optional[str], Tuple[str, ...], Optional[float]]] = {
    "Computer Science Engineering": ("Engineering", "JEE", PCM, 75),
    "Mechanical Engineering": ("Engineering", "JEE", PCM, 70),
    "Electrical Engineering": ("Engineering", "JEE", PCM, 70),
    "Civil Engineering": ("Engineering", "JEE", PCM, 65),
    "Electronics and Communication Engineering": ("Engineering", "JEE", PCM, 70),
    "MBBS": ("Medicine", "NEET", PCB, 85),
    "BDS": ("Medicine", "NEET", PCB, 80),
    "BAMS": ("Medicine", "NEET", PCB, 75),
    "BHMS": ("Medicine", "NEET", PCB, 75),
    "BPT": ("Medicine", "NEET", PCB, 70),
    "B.Com": ("Commerce", None, COMMERCE, None),
    "BBA": ("Commerce", None, COMMERCE, None),
    "BBM": ("Commerce", None, COMMERCE, None),
    "CA": ("Commerce", None, COMMERCE, None),
    "BA in History": ("Humanities", None, ("History", "Political Science", "Geography"), None),
    "BA in Psychology": ("Humanities", None, ("Psychology", "Sociology", "English"), None),
    "BA in Sociology": ("Humanities", None, ("Sociology", "Political Science", "History"), None),
    "BA in Political Science": ("Humanities", None, ("Political Science", "History", "Geography"), None),
    "BA in English": ("Humanities", None, ("English", "History", "Political Science"), None),
}

COURSE_DF = pd.DataFrame(
    [
        {
            "course": name,
            "category": cat,
            "exam": exam,
            "subjects": list(subjects),
            "cutoff": np.nan if cutoff is None else float(cutoff),
        }
        for name, (cat, exam, subjects, cutoff) in _COURSE_DEFINITIONS.items()
    ]
)

COURSE_ALIASES = {
    "BDS (Dentistry)": "BDS",
    "BAMS (Ayurveda)": "BAMS",
    "BHMS (Homeopathy)": "BHMS",
    "BPT (Physiotherapy)": "BPT",
    "B.Com (Bachelor of Commerce)": "B.Com",
    "Bachelor of Commerce": "B.Com",
    "BCom": "B.Com",
    "BBA (Bachelor of Business Administration)": "BBA",
    "Bachelor of Business Administration": "BBA",
    "BBM (Bachelor of Business Management)": "BBM",
    "Bachelor of Business Management": "BBM",
    "CA (Chartered Accountancy)": "CA",
    "Chartered Accountancy": "CA",
}

SUBJECT_ALIASES = {
    "maths": "Mathematics",
    "math": "Mathematics",
    "accounts": "Accountancy",
    "accounting": "Accountancy",
    "bio": "Biology",
    "business": "Business Studies",
    "polity": "Political Science",
    "pol science": "Political Science",
}

NAME_RE = re.compile(r"[A-Za-z][A-Za-z ]{0,99}")
AGE_RE = re.compile(r"1[7-9]|2[0-5]")
GENDER_RE = re.compile(r"Male|Female|Other", re.IGNORECASE)
MARK_RE = re.compile(r"100(?:\.0+)?|\d{1,2}(?:\.\d+)?")
SUBJECT_RE = re.compile(r"[A-Za-z][A-Za-z &]*")
EXAM_NAME_RE = re.compile(r"JEE|NEET", re.IGNORECASE)
EXAM_RESULT_RE = re.compile(r"Qualified|Not Qualified|Not Appeared", re.IGNORECASE)
_COURSE_LOOKUP = {c.lower(): c for c in _COURSE_DEFINITIONS}
_COURSE_LOOKUP.update({alias.lower(): course for alias, course in COURSE_ALIASES.items()})
COURSE_RE = re.compile(
    "|".join(re.escape(c) for c in sorted(_COURSE_LOOKUP, key=len, reverse=True)),
    re.IGNORECASE,
)


def _normalise_subject(raw: str) -> str:
    """Collapse spaces, apply aliases and title-case a subject name."""
    cleaned = " ".join(raw.split())
    return SUBJECT_ALIASES.get(cleaned.lower(), cleaned.title())


def validate_payload(data: Any) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Validates the request body and returns (clean_data, errors)."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return None, ["Request body must be a JSON object."]

    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name.strip()):
        errors.append("name: must contain only letters and spaces (max 100 characters).")
        name = None
    else:
        name = " ".join(name.split())

    age = data.get("age")
    if isinstance(age, bool) or not isinstance(age, (int, str)) or not AGE_RE.fullmatch(str(age).strip()):
        errors.append("age: must be a whole number between 17 and 25.")
        age = None
    else:
        age = int(str(age).strip())

    gender = data.get("gender")
    if not isinstance(gender, str) or not GENDER_RE.fullmatch(gender.strip()):
        errors.append('gender: must be one of "Male", "Female" or "Other".')
        gender = None
    else:
        gender = gender.strip().title()

    marks_in = data.get("marks")
    marks: Dict[str, float] = {}
    if not isinstance(marks_in, dict):
        errors.append("marks: must be an object of 6 subject names mapped to marks (0 to 100).")
    else:
        if len(marks_in) != REQUIRED_SUBJECT_COUNT:
            errors.append(f"marks: exactly {REQUIRED_SUBJECT_COUNT} subjects are required, got {len(marks_in)}.")
        for subject, value in marks_in.items():
            if not isinstance(subject, str) or not SUBJECT_RE.fullmatch(subject.strip()):
                errors.append(f"marks: invalid subject name '{subject}'. Use letters and spaces only.")
                continue
            key = _normalise_subject(subject)
            if key in marks:
                errors.append(f"marks: subject '{key}' is repeated.")
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float, str)) \
                    or not MARK_RE.fullmatch(str(value).strip()):
                errors.append(f"marks: '{subject}' must be a number between 0 and 100.")
                continue
            marks[key] = float(str(value).strip())

    exams_in = data.get("qualification_exams", {})
    exams: Dict[str, str] = {}
    if exams_in is None:
        exams_in = {}
    if not isinstance(exams_in, dict):
        errors.append("qualification_exams: must be an object such as {\"JEE\": \"Qualified\"}.")
    else:
        for exam, result in exams_in.items():
            if not isinstance(exam, str) or not EXAM_NAME_RE.fullmatch(exam.strip()):
                errors.append(f"qualification_exams: unknown exam '{exam}'. Allowed: JEE, NEET.")
                continue
            if isinstance(result, bool):
                result = "Qualified" if result else "Not Qualified"
            if not isinstance(result, str) or not EXAM_RESULT_RE.fullmatch(result.strip()):
                errors.append(
                    f"qualification_exams: result for '{exam}' must be "
                    '"Qualified", "Not Qualified", "Not Appeared" (or true / false).'
                )
                continue
            exams[exam.strip().upper()] = result.strip().title()

    course_in = data.get("desired_course")
    course = None
    if not isinstance(course_in, str) or not COURSE_RE.fullmatch(" ".join(course_in.split())):
        errors.append("desired_course: must be one of: " + ", ".join(_COURSE_DEFINITIONS) + ".")
    else:
        course = _COURSE_LOOKUP[" ".join(course_in.split()).lower()]

    if errors:
        return None, errors
    return {
        "name": name,
        "age": age,
        "gender": gender,
        "marks": marks,
        "qualification_exams": exams,
        "desired_course": course,
    }, []


def evaluate_course(row: pd.Series, marks: pd.Series, exams: Dict[str, str]) -> Dict[str, Any]:
    """Evaluate one catalogue row against the student's marks and exam results."""
    reasons: List[str] = []
    subjects: List[str] = row["subjects"]

    exam = row["exam"]
    if exam and exams.get(exam) != "Qualified":
        reasons.append(f"{exam} qualification is required (status: {exams.get(exam, 'Not Appeared')}).")

    missing = [s for s in subjects if s not in marks.index]
    if missing:
        reasons.append("Required 12th grade subjects not found: " + ", ".join(missing) + ".")

    percentage: Optional[float] = None
    if not missing:
        percentage = float(np.round(np.mean(marks.loc[subjects].to_numpy(dtype=float)), 2))
        cutoff = row["cutoff"]
        if not np.isnan(cutoff) and percentage < cutoff:
            reasons.append(
                f"Average in {', '.join(subjects)} is {percentage}%, below the cut-off of {cutoff:g}%."
            )

    return {
        "course": row["course"],
        "category": row["category"],
        "eligible": not reasons,
        "required_subjects": subjects,
        "cutoff_percentage": None if np.isnan(row["cutoff"]) else float(row["cutoff"]),
        "student_percentage": percentage,
        "reasons": reasons,
    }


def check_eligibility(clean: Dict[str, Any]) -> Dict[str, Any]:
    """Check the desired course and, if not eligible, recommend similar courses."""
    marks = pd.Series(clean["marks"], dtype=float)
    exams = clean["qualification_exams"]

    results = pd.DataFrame(
        [evaluate_course(row, marks, exams) for _, row in COURSE_DF.iterrows()]
    )
    chosen = results[results["course"] == clean["desired_course"]].iloc[0]

    recommendations: List[Dict[str, Any]] = []
    if not chosen["eligible"]:
        eligible = results[results["eligible"] & (results["course"] != chosen["course"])].copy()
        eligible["same_category"] = eligible["category"] == chosen["category"]
        eligible["cutoff_sort"] = eligible["cutoff_percentage"].fillna(0)
        eligible = eligible.sort_values(
            ["same_category", "cutoff_sort", "course"], ascending=[False, False, True]
        ).head(MAX_RECOMMENDATIONS)
        recommendations = [
            {
                "course": r["course"],
                "category": r["category"],
                "student_percentage": r["student_percentage"],
                "cutoff_percentage": r["cutoff_percentage"],
            }
            for _, r in eligible.iterrows()
        ]

    if chosen["eligible"]:
        message = f"Eligible for {chosen['course']}."
    elif recommendations:
        message = (
            f"Not eligible for {chosen['course']}. "
            f"Eligible alternatives: {', '.join(r['course'] for r in recommendations)}."
        )
    else:
        message = (
            f"Not eligible for {chosen['course']}. No course in the catalogue matches the "
            "current subjects, marks and exam results."
        )

    return {
        "desired_course": chosen["course"],
        "category": chosen["category"],
        "eligible": bool(chosen["eligible"]),
        "required_subjects": list(chosen["required_subjects"]),
        "cutoff_percentage": chosen["cutoff_percentage"],
        "student_percentage": chosen["student_percentage"],
        "reasons": list(chosen["reasons"]),
        "recommended_courses": recommendations,
        "message": message,
    }


_CREATE_TABLE_SQL = """
IF OBJECT_ID(N'dbo.StudentEligibility', N'U') IS NULL
CREATE TABLE dbo.StudentEligibility (
    Id            BIGINT IDENTITY(1,1) PRIMARY KEY,
    RequestId     UNIQUEIDENTIFIER NOT NULL,
    CreatedAtUtc  DATETIME2(3)     NOT NULL,
    StudentName   NVARCHAR(200)    NULL,
    DesiredCourse NVARCHAR(100)    NULL,
    IsEligible    BIT              NULL,
    StatusCode    INT              NOT NULL,
    InputJson     NVARCHAR(MAX)    NOT NULL,
    OutputJson    NVARCHAR(MAX)    NOT NULL
);
"""
_INSERT_SQL = """
INSERT INTO dbo.StudentEligibility
    (RequestId, CreatedAtUtc, StudentName, DesiredCourse, IsEligible, StatusCode, InputJson, OutputJson)
VALUES (?, ?, ?, ?, ?, ?, ?, ?);
"""

_table_ready = False
_table_lock = threading.Lock()
_db_retry_after = 0.0
_CONNECTION_ERRORS = (pyodbc.OperationalError, pyodbc.InterfaceError)


def _connect() -> pyodbc.Connection:
    """Opens a new connection (pyodbc pools them)."""
    return pyodbc.connect(DB_CONNECTION_STRING, timeout=DB_LOGIN_TIMEOUT_SECONDS)


def _run(sql: str, *params: Any) -> None:
    """Execute one statement, commit, and always close the connection."""
    conn = _connect()
    try:
        conn.execute(sql, *params)
        conn.commit()
    finally:
        conn.close()


def _db_paused() -> bool:
    return time.monotonic() < _db_retry_after


def _pause_db(reason: Exception) -> None:
    global _db_retry_after
    _db_retry_after = time.monotonic() + DB_RETRY_SECONDS
    logger.error("SQL Server unavailable, pausing DB writes for %ss: %s", DB_RETRY_SECONDS, reason)


def ensure_table() -> bool:
    """Create the table if needed. Safe to call from many threads."""
    global _table_ready
    if _table_ready:
        return True
    if _db_paused():
        return False
    if not _table_lock.acquire(timeout=DB_LOGIN_TIMEOUT_SECONDS + 1):
        return False
    try:
        if not _table_ready and not _db_paused():
            _run(_CREATE_TABLE_SQL)
            _table_ready = True
            logger.info("Connected to SQL Server (%s).", DB_DESCRIPTION)
    except _CONNECTION_ERRORS as exc:
        _pause_db(exc)
    except Exception as exc:
        logger.error("Could not prepare database table: %s", exc)
    finally:
        _table_lock.release()
    return _table_ready


def save_record(
    request_id: str,
    created_at: datetime,
    status_code: int,
    input_json: str,
    output: Dict[str, Any],
    name: Optional[str],
    course: Optional[str],
) -> bool:
    """Store one request/response pair. Returns True if saved, False otherwise."""
    try:
        if not ensure_table() or _db_paused():
            return False
        _run(
            _INSERT_SQL,
            request_id,
            created_at.replace(tzinfo=None),
            name,
            course,
            output.get("eligible"),
            status_code,
            input_json,
            json.dumps(output, default=str),
        )
        return True
    except _CONNECTION_ERRORS as exc:
        _pause_db(exc)
    except Exception as exc:
        logger.error("[%s] Failed to store record in SQL Server: %s", request_id, exc)
    return False


def _to_native(obj: Any) -> Any:
    """Convert numpy/pandas scalars to plain Python so the output is JSON safe."""
    if isinstance(obj, dict):
        return {k: _to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_native(v) for v in obj]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float) and np.isnan(obj):
        return None
    return obj


def process_request(request_id: str, raw_body: bytes) -> Tuple[int, Dict[str, Any]]:
    """Parse, validate, evaluate and store. Returns (http_status, response_json)."""
    input_text = raw_body.decode("utf-8", errors="replace")
    logger.info("[%s] INPUT: %s", request_id, input_text)

    name: Optional[str] = None
    course: Optional[str] = None
    try:
        try:
            payload = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError):
            status, output = 400, {
                "request_id": request_id,
                "status": "invalid_json",
                "errors": ["Request body is not valid JSON."],
            }
        else:
            clean, errors = validate_payload(payload)
            if errors:
                if isinstance(payload, dict):
                    n = payload.get("name")
                    name = n if isinstance(n, str) else None
                status, output = 422, {
                    "request_id": request_id,
                    "status": "validation_error",
                    "errors": errors,
                }
            else:
                name, course = clean["name"], clean["desired_course"]
                result = check_eligibility(clean)
                status, output = 200, {
                    "request_id": request_id,
                    "status": "success",
                    "student": {
                        "name": clean["name"],
                        "age": clean["age"],
                        "gender": clean["gender"],
                        "marks": clean["marks"],
                        "qualification_exams": clean["qualification_exams"],
                    },
                    **result,
                }
    except Exception as exc:
        logger.exception("[%s] Unexpected error while processing request", request_id)
        status, output = 500, {
            "request_id": request_id,
            "status": "error",
            "errors": ["Internal error while processing the request."],
        }

    created_at = datetime.now(timezone.utc)
    output = _to_native(output)
    output["timestamp_utc"] = created_at.isoformat()
    output["persisted"] = save_record(request_id, created_at, status, input_text, output, name, course)
    logger.info("[%s] OUTPUT (%s): %s", request_id, status, json.dumps(output, default=str))
    return status, output


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Try to prepare the table at start. If SQL Server is down, retry on first use."""
    if await run_in_threadpool(ensure_table):
        logger.info("Database ready.")
    else:
        logger.warning("Database not reachable at startup. Will retry on the first request.")
    yield


app = FastAPI(
    title="Student Admission Eligibility API",
    description="Checks college admission eligibility and stores every request in SQL Server.",
    version="1.0.0",
    lifespan=lifespan,
)


_REQUEST_EXAMPLE = {
    "name": "Asha Rao",
    "age": 18,
    "gender": "Female",
    "marks": {"Physics": 88, "Chemistry": 82, "Mathematics": 91,
              "English": 75, "Computer Science": 90, "Physical Education": 85},
    "qualification_exams": {"JEE": "Qualified", "NEET": "Not Appeared"},
    "desired_course": "Computer Science Engineering",
}
_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["name", "age", "gender", "marks", "desired_course"],
    "properties": {
        "name": {"type": "string", "description": "Letters and spaces only."},
        "age": {"type": "integer", "minimum": 17, "maximum": 25},
        "gender": {"type": "string", "enum": ["Male", "Female", "Other"]},
        "marks": {
            "type": "object",
            "description": "Exactly 6 subjects mapped to marks between 0 and 100.",
            "additionalProperties": {"type": "number", "minimum": 0, "maximum": 100},
        },
        "qualification_exams": {
            "type": "object",
            "description": 'Keys JEE / NEET. Values "Qualified", "Not Qualified" or "Not Appeared".',
            "additionalProperties": {"type": "string"},
        },
        "desired_course": {"type": "string", "enum": list(_COURSE_DEFINITIONS)},
    },
}


@app.post(
    "/check-eligibility",
    summary="Check admission eligibility for a course",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _REQUEST_SCHEMA, "example": _REQUEST_EXAMPLE}},
        }
    },
)
async def check_eligibility_endpoint(request: Request) -> JSONResponse:
    """
    Validate the student details, check eligibility for the desired course,
    recommend eligible alternatives when needed, then log and store the call.
    """
    request_id = str(uuid.uuid4())
    try:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            logger.warning("[%s] Rejected body of %s bytes (limit %s).", request_id, len(body), MAX_BODY_BYTES)
            return JSONResponse(
                status_code=413,
                content={"request_id": request_id, "status": "error",
                         "errors": [f"Request body larger than {MAX_BODY_BYTES} bytes."]},
            )
        status, output = await run_in_threadpool(process_request, request_id, body)
        return JSONResponse(status_code=status, content=output)
    except Exception:
        logger.exception("[%s] Unhandled error in endpoint", request_id)
        return JSONResponse(
            status_code=500,
            content={"request_id": request_id, "status": "error", "errors": ["Internal server error."]},
        )


@app.get("/health")
async def health() -> Dict[str, str]:
    """Liveness probe, with the last known database state."""
    if _table_ready and not _db_paused():
        db_state = "connected"
    elif _db_paused():
        db_state = "unavailable"
    else:
        db_state = "not connected yet"
    return {"status": "ok", "database": db_state}


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Return 404/405 etc. as JSON."""
    return JSONResponse(status_code=exc.status_code, content={"status": "error", "errors": [str(exc.detail)]})


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unexpected errors."""
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(status_code=500, content={"status": "error", "errors": ["Internal server error."]})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
