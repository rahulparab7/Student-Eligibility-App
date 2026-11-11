"""Unit tests for app.py. Database calls are faked, so no SQL Server is needed."""
import concurrent.futures as cf
import json
import time

import pyodbc
import pytest
from fastapi.testclient import TestClient

import app as appmod

GOOD = {
    "name": "Asha Rao", "age": 18, "gender": "Female",
    "marks": {"Physics": 88, "Chemistry": 82, "Mathematics": 91,
              "English": 75, "Computer Science": 90, "Physical Education": 85},
    "qualification_exams": {"JEE": "Qualified", "NEET": "Not Appeared"},
    "desired_course": "Computer Science Engineering",
}


class FakeConnection:
    executed = []

    def execute(self, sql, *params):
        FakeConnection.executed.append((sql, params))

    def commit(self):
        pass

    def close(self):
        pass


def _db_down(*_args, **_kwargs):
    raise pyodbc.OperationalError("HYT00", "Login timeout expired")


@pytest.fixture(autouse=True)
def reset_db_state(monkeypatch):
    """Each test starts with a fresh DB state and a database that is down."""
    monkeypatch.setattr(appmod, "_table_ready", False)
    monkeypatch.setattr(appmod, "_db_retry_after", 0.0)
    monkeypatch.setattr(appmod, "_connect", _db_down)
    FakeConnection.executed = []


client = TestClient(appmod.app)


def post(payload):
    return client.post("/check-eligibility", json=payload)


def test_eligible():
    j = post(GOOD).json()
    assert j["eligible"] is True and j["student_percentage"] == 87.0
    assert j["recommended_courses"] == []


def test_exam_not_qualified():
    p = {**GOOD, "qualification_exams": {"JEE": "Not Qualified"}, "desired_course": "Civil Engineering"}
    j = post(p).json()
    assert j["eligible"] is False and "JEE qualification is required" in j["reasons"][0]


def test_cutoff_miss_recommends_lower_cutoff_branches():
    p = {**GOOD, "marks": {**GOOD["marks"], "Physics": 65, "Chemistry": 62, "Mathematics": 70}}
    j = post(p).json()
    assert j["eligible"] is False
    assert [c["course"] for c in j["recommended_courses"]] == ["Civil Engineering"]


def test_no_recommendation_has_clear_message():
    p = {**GOOD, "marks": {**GOOD["marks"], "Physics": 60, "Chemistry": 62, "Mathematics": 70}}
    j = post(p).json()
    assert j["recommended_courses"] == [] and "No course in the catalogue" in j["message"]


def test_cross_category_recommendation():
    p = {**GOOD, "marks": {"Physics": 90, "Chemistry": 88, "Biology": 86, "Mathematics": 80,
                           "English": 75, "Computer Science": 70},
         "qualification_exams": {"JEE": "Qualified", "NEET": "Not Qualified"},
         "desired_course": "MBBS"}
    j = post(p).json()
    assert j["eligible"] is False
    assert all(c["category"] == "Engineering" for c in j["recommended_courses"])


def test_commerce_and_brief_course_names():
    p = {**GOOD, "marks": {"Accountancy": 70, "Business Studies": 65, "Economics": 72,
                           "English": 60, "Mathematics": 55, "Informatics Practices": 80},
         "qualification_exams": {}, "desired_course": "B.Com (Bachelor of Commerce)"}
    j = post(p).json()
    assert j["desired_course"] == "B.Com" and j["eligible"] is True


def test_validation_errors_collected():
    p = {**GOOD, "name": "Asha123", "age": 30, "gender": "x", "desired_course": "Law"}
    r = post(p)
    assert r.status_code == 422 and len(r.json()["errors"]) == 4


@pytest.mark.parametrize("bad", [101, -1, "abc", True, None])
def test_bad_marks(bad):
    assert post({**GOOD, "marks": {**GOOD["marks"], "Physics": bad}}).status_code == 422


def test_needs_exactly_six_subjects():
    marks = dict(list(GOOD["marks"].items())[:5])
    assert post({**GOOD, "marks": marks}).status_code == 422


def test_normalisation():
    p = {**GOOD, "name": "  Asha   Rao ", "gender": "female",
         "qualification_exams": {"jee": True}, "desired_course": "computer science engineering"}
    j = post(p).json()
    assert j["student"]["name"] == "Asha Rao" and j["student"]["gender"] == "Female"
    assert j["eligible"] is True


def test_invalid_json():
    r = client.post("/check-eligibility", content=b"{bad", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["status"] == "invalid_json"


def test_body_too_large():
    r = client.post("/check-eligibility", content=b"x" * (appmod.MAX_BODY_BYTES + 1))
    assert r.status_code == 413


def test_not_found_is_json():
    assert client.get("/nope").json()["status"] == "error"


def test_record_is_stored(monkeypatch):
    monkeypatch.setattr(appmod, "_connect", lambda: FakeConnection())
    j = post(GOOD).json()
    assert j["persisted"] is True
    sql, params = FakeConnection.executed[-1]
    assert "INSERT INTO dbo.StudentEligibility" in sql
    assert params[2:6] == ("Asha Rao", "Computer Science Engineering", True, 200)
    assert json.loads(params[6]) == GOOD and json.loads(params[7])["eligible"] is True


def test_db_down_still_returns_result():
    j = post(GOOD).json()
    assert j["eligible"] is True and j["persisted"] is False


def test_db_down_does_not_serialise_requests(monkeypatch):
    """A slow, failing DB must cost one timeout in total, not one per request."""
    def slow_down(*_a, **_k):
        time.sleep(1)
        _db_down()

    monkeypatch.setattr(appmod, "_connect", slow_down)
    start = time.perf_counter()
    with cf.ThreadPoolExecutor(10) as ex:
        results = list(ex.map(lambda _: post(GOOD), range(10)))
    elapsed = time.perf_counter() - start
    assert all(r.status_code == 200 and r.json()["persisted"] is False for r in results)
    assert elapsed < 4, f"took {elapsed:.1f}s, requests were serialised"


def test_concurrent_calls():
    with cf.ThreadPoolExecutor(20) as ex:
        codes = list(ex.map(lambda _: post(GOOD).status_code, range(40)))
    assert set(codes) == {200}
