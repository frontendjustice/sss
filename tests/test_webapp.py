"""Web app API tests: no network, the model is a fake provider."""
import importlib
import time

import pytest

from test_generate import FakeProvider


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_PASSWORD", "pw")
    import webapp.server as server
    server = importlib.reload(server)
    monkeypatch.setattr(server, "Gemini", lambda: FakeProvider())
    monkeypatch.setattr(server, "Groq", lambda: FakeProvider())
    import base64
    auth = {"Authorization": "Basic " + base64.b64encode(b"me:pw").decode()}
    return TestClient(server.app, headers=auth), server


def wait(c, jid):
    for _ in range(100):
        job = c.get(f"/api/jobs/{jid}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


NOTES = ("# Pharmacology\n\n## Beta blockers\n\n" + "Propranolol is a non-selective beta blocker that can cause "
         "bronchospasm in asthma, so cardioselective agents such as bisoprolol are preferred. " * 12)


def test_password_required(client):
    c, server = client
    from fastapi.testclient import TestClient
    assert TestClient(server.app).get("/").status_code == 401
    assert TestClient(server.app).get("/healthz").status_code == 200
    assert c.get("/").status_code == 200


def test_upload_plan_generate_review_delete(client):
    c, _ = client
    assert c.post("/api/materials", files={"file": ("x.exe", b"MZ")}).status_code == 400
    m = c.post("/api/materials", files={"file": ("beta.md", NOTES.encode())}).json()
    plan = wait(c, c.post("/api/jobs", json={"material_id": m["id"], "mode": "plan", "questions": 3,
                                             "course": "PCOL"}).json()["id"])
    assert plan["status"] == "done" and plan["result"]["report"]["questions"]["planned"] == 3
    assert plan["result"]["report"]["sections"][0]["preview"]
    job = wait(c, c.post("/api/jobs", json={"material_id": m["id"], "questions": 3, "course": "PCOL"}).json()["id"])
    assert job["status"] == "done", job
    sid = job["result"]["set_id"]
    s = c.get(f"/api/sets/{sid}").json()
    assert len(s["questions"]) == 3 and s["course"] == "Pharmacology"
    mats = c.get("/api/materials").json()
    assert mats[0]["sets"][0]["count"] == 3
    assert c.delete(f"/api/sets/{sid}/questions/0").json()["count"] == 2
    assert c.delete(f"/api/materials/{m['id']}").json()["ok"]
    assert c.get(f"/api/sets/{sid}").status_code == 404


def test_bad_course_rejected(client):
    c, _ = client
    m = c.post("/api/materials", files={"file": ("beta.md", NOTES.encode())}).json()
    r = c.post("/api/jobs", json={"material_id": m["id"], "course": "NOPE"})
    assert r.status_code == 400 and "Unknown course" in r.json()["error"]
