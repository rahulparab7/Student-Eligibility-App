"""
End to end demo.

1. Sends a set of sample students to the running API, all at the same time.
2. Prints the result of each call.
3. Reads the stored rows back from SQL Server (dbo.vw_EligibilityResults).

Start the API first:   uvicorn app:app --port 8000
Then run:              python demo_client.py [--url http://localhost:8000] [--rounds 5]
"""
import argparse
import asyncio
import json
import time

import httpx
import pandas as pd

SAMPLES = [
    ("Eligible engineering", {
        "name": "Asha Rao", "age": 18, "gender": "Female",
        "marks": {"Physics": 88, "Chemistry": 82, "Mathematics": 91, "English": 75,
                  "Computer Science": 90, "Physical Education": 85},
        "qualification_exams": {"JEE": "Qualified", "NEET": "Not Appeared"},
        "desired_course": "Computer Science Engineering"}),
    ("Below CSE cut-off", {
        "name": "Rohan Mehta", "age": 19, "gender": "Male",
        "marks": {"Physics": 70, "Chemistry": 68, "Mathematics": 78, "English": 80,
                  "Computer Science": 85, "Physical Education": 90},
        "qualification_exams": {"JEE": "Qualified"},
        "desired_course": "Computer Science Engineering"}),
    ("Below MBBS cut-off", {
        "name": "Priya Nair", "age": 18, "gender": "Female",
        "marks": {"Physics": 80, "Chemistry": 84, "Biology": 82, "English": 78,
                  "Psychology": 74, "Physical Education": 88},
        "qualification_exams": {"NEET": "Qualified"},
        "desired_course": "MBBS"}),
    ("No NEET, has JEE", {
        "name": "Karan Singh", "age": 20, "gender": "Male",
        "marks": {"Physics": 90, "Chemistry": 88, "Biology": 86, "Mathematics": 84,
                  "English": 72, "Computer Science": 70},
        "qualification_exams": {"JEE": "Qualified", "NEET": "Not Qualified"},
        "desired_course": "MBBS"}),
    ("Commerce, brief name", {
        "name": "Neha Gupta", "age": 17, "gender": "Female",
        "marks": {"Accountancy": 72, "Business Studies": 68, "Economics": 75, "English": 70,
                  "Mathematics": 60, "Informatics Practices": 80},
        "desired_course": "CA (Chartered Accountancy)"}),
    ("Missing subject", {
        "name": "Meera Iyer", "age": 21, "gender": "Other",
        "marks": {"English": 82, "History": 78, "Political Science": 80, "Geography": 75,
                  "Sociology": 77, "Economics": 70},
        "desired_course": "BA in Psychology"}),
    ("Invalid input", {
        "name": "R0han", "age": 30, "gender": "M",
        "marks": {"Physics": 105, "Chemistry": 60, "Mathematics": 70, "English": 80,
                  "Computer Science": 85, "Physical Education": 90},
        "desired_course": "Law"}),
    ("Broken JSON", '{"name": "Test", "age": 18,'),
]


async def call(client: httpx.AsyncClient, url: str, label: str, body):
    content = body if isinstance(body, str) else json.dumps(body)
    r = await client.post(f"{url}/check-eligibility", content=content,
                          headers={"content-type": "application/json"})
    return label, r.status_code, r.json()


async def run(url: str, rounds: int):
    async with httpx.AsyncClient(timeout=30) as client:
        health = (await client.get(f"{url}/health")).json()
        print(f"Health: {health}\n")

        jobs = [call(client, url, label, body) for _ in range(rounds) for label, body in SAMPLES]
        start = time.perf_counter()
        results = await asyncio.gather(*jobs)
        elapsed = time.perf_counter() - start

    rows = []
    for label, status, j in results[: len(SAMPLES)]:
        rows.append({
            "case": label,
            "http": status,
            "course": j.get("desired_course", "-"),
            "eligible": j.get("eligible", "-"),
            "pct": j.get("student_percentage", "-"),
            "recommended": ", ".join(c["course"] for c in j.get("recommended_courses", [])) or "-",
            "saved": j.get("persisted"),
        })
    with pd.option_context("display.max_colwidth", 60, "display.width", 200):
        print(pd.DataFrame(rows).to_string(index=False))

    saved = sum(1 for *_, j in results if j.get("persisted"))
    print(f"\n{len(results)} concurrent calls in {elapsed:.2f}s, {saved} stored in SQL Server.")
    print("\nSample output (Rohan Mehta):")
    print(json.dumps(results[1][2], indent=2))


def show_database(limit: int = 8):
    """Read the latest stored rows using the same settings as the API."""
    import app

    conn = app._connect()
    try:
        cur = conn.cursor()
        total = cur.execute("SELECT COUNT(*) FROM dbo.StudentEligibility").fetchone()[0]
        cur.execute(
            f"SELECT TOP ({int(limit)}) Id, CreatedAtUtc, StatusCode, StudentName, DesiredCourse, "
            "Category, IsEligible, StudentPercentage, CutoffPercentage "
            "FROM dbo.vw_EligibilityResults ORDER BY Id DESC"
        )
        df = pd.DataFrame.from_records(cur.fetchall(), columns=[c[0] for c in cur.description])
    finally:
        conn.close()
    print(f"\nSQL Server: dbo.StudentEligibility has {total} rows. Latest {limit}:")
    with pd.option_context("display.width", 200):
        print(df.to_string(index=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--rounds", type=int, default=5, help="times to send the full sample set")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.url, args.rounds))
    except httpx.ConnectError:
        raise SystemExit(f"Cannot reach the API at {args.url}. Start it with: uvicorn app:app --port 8000")
    try:
        show_database()
    except Exception as exc:
        print(f"\nCould not read from SQL Server: {exc}")
