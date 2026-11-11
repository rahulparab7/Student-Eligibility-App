# Student Admission Eligibility API

A FastAPI service that checks whether a student can join a desired college course, using their demographic details, 12th grade marks (6 subjects) and qualification exam results (JEE / NEET). If the student is not eligible, it recommends courses they do qualify for. Every call is logged and stored in SQL Server.

## Project layout

| File | Purpose |
|---|---|
| `app.py` | The API: validation, eligibility rules, recommendations, logging, SQL Server storage |
| `test_app.py` | 21 unit tests (no database needed) |
| `demo_client.py` | End to end demo: concurrent calls, then reads the stored rows back |
| `sql/init.sql` | Demo database: table, indexes, reporting views, least privilege login |
| `docker-compose.yml` | Demo SQL Server 2022 (Developer edition) |
| `setup_demo_db.ps1` | One command setup of the demo database |
| `.env.example` | Configuration template |

## Quick start (Windows)

1. Install Python 3.10+ and Docker Desktop.
2. Install the Python packages:
   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements-dev.txt
   ```
3. Create the demo database. This generates random passwords into `docker/sqlserver.env` and `.env`, starts SQL Server, and runs `sql/init.sql`:
   ```powershell
   .\setup_demo_db.ps1
   ```
4. Start the API, then open http://localhost:8000/docs:
   ```powershell
   uvicorn app:app --port 8000
   ```
5. In a second terminal, run the demo:
   ```powershell
   python demo_client.py
   ```

To use an existing SQL Server instead, skip step 3. Copy `.env.example` to `.env` and set `DB_SERVER`, `DB_NAME`, `DB_USER` and `DB_PASSWORD` (or leave `DB_USER` empty for Windows authentication). Then run `sql/init.sql` once.

## API

`POST /check-eligibility`

Request:
```json
{
  "name": "Rohan Mehta",
  "age": 19,
  "gender": "Male",
  "marks": {"Physics": 70, "Chemistry": 68, "Mathematics": 78,
            "English": 80, "Computer Science": 85, "Physical Education": 90},
  "qualification_exams": {"JEE": "Qualified"},
  "desired_course": "Computer Science Engineering"
}
```

Response (shortened):
```json
{
  "request_id": "23929cfd-...",
  "status": "success",
  "desired_course": "Computer Science Engineering",
  "eligible": false,
  "cutoff_percentage": 75.0,
  "student_percentage": 72.0,
  "reasons": ["Average in Physics, Chemistry, Mathematics is 72.0%, below the cut-off of 75%."],
  "recommended_courses": [{"course": "Electrical Engineering", "cutoff_percentage": 70.0}, "..."],
  "message": "Not eligible for Computer Science Engineering. Eligible alternatives: ...",
  "persisted": true
}
```

| HTTP code | When |
|---|---|
| 200 | Valid input, eligibility result returned (eligible or not) |
| 400 | Body is not valid JSON |
| 413 | Body larger than 16 KB |
| 422 | Validation failed. All problems are listed in `errors` |
| 500 | Unexpected error, returned as JSON |

`GET /health` returns `{"status": "ok", "database": "connected" | "unavailable" | "not connected yet"}`.

## Rules applied

**Validation (regular expressions)**
* Name: letters and spaces only, up to 100 characters. Repeated spaces are collapsed.
* Age: whole number from 17 to 25.
* Gender: Male, Female or Other (any letter case).
* Marks: exactly 6 subjects, each from 0 to 100. Aliases such as "Maths" and "Accounts" are accepted.
* Exams: JEE and/or NEET with "Qualified", "Not Qualified" or "Not Appeared" (true / false also accepted). A missing exam counts as "Not Appeared".
* Course: one of the 19 courses in the brief. Names as written in the brief, such as "BDS (Dentistry)" or "CA (Chartered Accountancy)", are accepted.

**Eligibility**

| Category | Exam | Required 12th subjects | Cut-off |
|---|---|---|---|
| Engineering (CSE 75%, Mechanical 70%, Electrical 70%, ECE 70%, Civil 65%) | JEE qualified | Physics, Chemistry, Mathematics | Average of the 3 subjects |
| Medicine (MBBS 85%, BDS 80%, BAMS 75%, BHMS 75%, BPT 70%) | NEET qualified | Physics, Chemistry, Biology | Average of the 3 subjects |
| Commerce (B.Com, BBA, BBM, CA) | None | Accountancy, Business Studies, Economics | None given |
| Humanities (5 BA courses) | None | As listed per course | None given |

**Recommendations** (when not eligible): every course the student does qualify for, up to 5. Courses in the same category come first, then courses with a higher cut-off (closest to the student's target). If nothing matches, the `message` says so.

## Database

`dbo.StudentEligibility` holds one row per call: the raw input, the JSON output, and key columns (student, course, eligible, HTTP status). Invalid and broken requests are stored too.

Views for reporting:
* `dbo.vw_EligibilityResults`: one flat row per call (age, gender, category, percentage, cut-off, message, recommendations).
* `dbo.vw_StudentMarks`: one row per subject per successful call.

The API connects as `eligibility_app`, which can only SELECT and INSERT. It cannot update, delete, drop or create objects.

Useful commands:
```powershell
docker compose stop            # stop SQL Server (data is kept)
docker compose start           # start it again
.\setup_demo_db.ps1 -Reset     # delete the container and all data, then rebuild
```

## Concurrency and error handling

* The endpoint is `async`. Blocking work (pandas, pyodbc) runs in a thread pool, so many calls are served at once. For more throughput, add processes: `uvicorn app:app --workers 4`.
* pyodbc connection pooling is on, and each connection is closed (returned to the pool) after use.
* If SQL Server is down, the API still returns the eligibility result with `"persisted": false`. After the first failed connection it pauses DB writes for `DB_RETRY_SECONDS` (default 15), so later calls do not wait on a timeout. Saving resumes automatically. Calls made while the database is down are still in the log file.
* Every input and output is logged with its request ID in `logs/eligibility.log` (rotating, 5 x 5 MB) and on the console.

## Tests

```powershell
python -m pytest -q
```
The tests replace the database connection with fakes, so they run without SQL Server. They cover the eligibility rules, validation, storage, a database outage, and concurrent calls.

## Security notes

* No credentials are in source code. They live in `.env` and `docker/sqlserver.env`, which are git-ignored and never printed.
* The demo SQL Server listens on `127.0.0.1` only.
* Student data (name, age, gender, marks) is written to the logs and the database.
