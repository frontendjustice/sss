"""Web app tests: accounts, class library, generation, practice, editing and admin.
No network: the model is a fake provider, the database a temporary SQLite file."""
import importlib
import time

import pytest
from fastapi.testclient import TestClient

from test_generate import FakeProvider

NOTES = ("# Pharmacology\n\n## Beta blockers\n\n" + "Propranolol is a non-selective beta blocker that can cause "
         "bronchospasm in asthma, so cardioselective agents such as bisoprolol are preferred. " * 12
         + "\n\n## ACE inhibitors\n\n" + "Lisinopril blocks angiotensin converting enzyme; a dry cough from "
         "bradykinin build-up is a common reason patients stop it. " * 12)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_EMAIL", "admin@class.ng")
    monkeypatch.setenv("CLASS_INVITE_CODE", "PHARM7")
    import webapp.server as server
    server = importlib.reload(server)
    server.jobs.make_providers = lambda: [FakeProvider()]
    return server


def client(server, email, name="Student", invite="PHARM7", password="password123"):
    c = TestClient(server.app)
    r = c.post("/api/signup", json={"email": email, "name": name, "password": password, "invite": invite})
    assert r.status_code == 200, r.text
    return c


def wait(c, jid):
    for _ in range(300):
        job = c.get(f"/api/jobs/{jid}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def class_setup(server):
    """Admin with a class course and processed class notes; returns (admin client, course id, material)."""
    adm = client(server, "admin@class.ng", "Justice", invite="")
    course = adm.post("/api/courses", json={"code": "pco 514", "title": "Autacoids", "shared": True}).json()
    up = adm.post("/api/materials", data={"course_id": course["id"], "shared": "true"},
                  files={"file": ("cardio_notes.md", NOTES.encode())}).json()
    assert wait(adm, up["job_id"])["status"] == "done"
    mat = adm.get(f"/api/materials/{up['material']['id']}").json()
    return adm, course["id"], mat


def test_accounts_invite_code_and_sessions(app):
    anon = TestClient(app.app)
    assert anon.get("/api/me").status_code == 401
    adm = client(app, "Admin@Class.ng", "Justice", invite="")
    assert adm.get("/api/me").json()["is_admin"] is True
    bad = anon.post("/api/signup", json={"email": "s@x.ng", "name": "Ada", "password": "password123", "invite": "nope"})
    assert bad.status_code == 400 and "invite code" in bad.json()["error"]
    s = client(app, "ada@x.ng", "Ada", invite="pharm7")          # case-insensitive code
    me = s.get("/api/me").json()
    assert me["is_admin"] is False and me["plan"] == "free" and me["usage"]["generation_limit"] == 5
    assert s.post("/api/logout").json()["ok"] and s.get("/api/me").status_code == 401
    assert s.post("/api/login", json={"email": "ADA@x.ng", "password": "wrong-pass"}).status_code == 400
    assert s.post("/api/login", json={"email": "ADA@x.ng", "password": "password123"}).status_code == 200


def test_class_library_and_private_notes(app):
    adm, cid, mat = class_setup(app)
    assert mat["status"] == "ready" and mat["shared"] and len(mat["topics"]) == 2
    s = client(app, "ada@x.ng", "Ada")
    assert s.post("/api/courses", json={"code": "X", "title": "Y", "shared": True}).status_code == 403
    course = s.get(f"/api/courses/{cid}").json()
    assert [n["id"] for n in course["notes"]] == [mat["id"]] and not course["can_edit"]
    up = s.post("/api/materials", data={"course_id": cid}, files={"file": ("mine.md", NOTES.encode())}).json()
    wait(s, up["job_id"])
    assert len(s.get(f"/api/courses/{cid}").json()["notes"]) == 2
    assert len(adm.get(f"/api/courses/{cid}").json()["notes"]) == 1           # private notes stay private
    assert s.delete(f"/api/materials/{mat['id']}").status_code == 403         # can't delete class notes
    assert s.post("/api/materials", data={"course_id": cid, "shared": "true"},
                  files={"file": ("x.md", b"hi")}).status_code == 403


def test_generate_from_chosen_topic_visibility_and_limits(app):
    adm, cid, mat = class_setup(app)
    s = client(app, "ada@x.ng", "Ada")
    beta = next(t for t in mat["topics"] if "Beta" in t["title"])
    job = wait(s, s.post("/api/quizzes/generate", json={"course_id": cid, "material_ids": [mat["id"]],
                                                        "section_ids": [beta["id"]], "questions": 3,
                                                        "course_code": "PCOL"}).json()["job_id"])
    assert job["status"] == "done", job
    quiz = s.get(f"/api/quizzes/{job['result']['quiz_id']}").json()
    assert quiz["count"] == 3 and not quiz["shared"]
    assert all("Beta" in q["topic"] for q in quiz["questions"])
    other = client(app, "bola@x.ng", "Bola")
    assert other.get(f"/api/quizzes/{quiz['id']}").status_code == 404        # private to Ada
    assert s.patch(f"/api/quizzes/{quiz['id']}", json={"share": True}).status_code == 403   # Pro feature
    shared = wait(adm, adm.post("/api/quizzes/generate", json={"course_id": cid, "material_ids": [mat["id"]],
                                                               "questions": 4, "course_code": "PCOL",
                                                               "share": True}).json()["job_id"])
    assert other.get(f"/api/quizzes/{shared['result']['quiz_id']}").status_code == 200
    adm.put("/api/admin/settings", json={"daily_limit": 1})
    again = s.post("/api/quizzes/generate", json={"course_id": cid, "material_ids": [mat["id"]], "questions": 2,
                                                  "course_code": "PCOL"})
    assert again.status_code == 429 and "Class quizzes are always free" in again.json()["error"]


def test_practice_exam_missed_and_later(app):
    adm, cid, mat = class_setup(app)
    quiz_id = wait(adm, adm.post("/api/quizzes/generate", json={"course_id": cid, "material_ids": [mat["id"]],
                                                                "questions": 4, "course_code": "PCOL",
                                                                "share": True}).json()["job_id"])["result"]["quiz_id"]
    s = client(app, "ada@x.ng", "Ada")
    att = s.post("/api/attempts", json={"quiz_id": quiz_id, "mode": "practice"}).json()
    assert len(att["questions"]) == 4 and "a" not in att["questions"][0]      # answers not sent in advance
    q1, q2 = att["questions"][0], att["questions"][1]
    r1 = s.post(f"/api/attempts/{att['id']}/answer", json={"question_id": q1["id"], "chosen": 0}).json()
    r2 = s.post(f"/api/attempts/{att['id']}/answer", json={"question_id": q2["id"], "chosen": 3}).json()
    assert r1["correct"] is True and r2["correct"] is False and r2["a"] == 0 and r2["e"]
    s.post(f"/api/marks/{q1['id']}")
    res = s.post(f"/api/attempts/{att['id']}/finish", json={}).json()
    assert (res["answered"], res["correct"], res["total"]) == (2, 1, 4)
    missed = s.post("/api/attempts", json={"pool": "missed"}).json()
    assert [q["id"] for q in missed["questions"]] == [q2["id"]]
    later = s.post("/api/attempts", json={"pool": "later"}).json()
    assert [q["id"] for q in later["questions"]] == [q1["id"]] and later["questions"][0]["marked"]
    exam = s.post("/api/attempts", json={"quiz_id": quiz_id, "mode": "exam", "minutes": 10}).json()
    assert s.post(f"/api/attempts/{exam['id']}/answer", json={"question_id": exam["questions"][0]["id"],
                                                               "chosen": 0}).status_code == 400
    out = s.post(f"/api/attempts/{exam['id']}/finish",
                 json={"answers": {str(q["id"]): 0 for q in exam["questions"]}}).json()
    assert out["correct"] == 4 and out["topics"] and all(r["correct"] for r in out["review"])
    assert s.post("/api/attempts", json={"pool": "missed"}).status_code == 400   # fixed by the exam
    t = s.get("/api/today").json()
    assert t["streak"] == 1 and t["later"] == 1 and t["recent"]
    p = s.get("/api/progress").json()
    assert p["answered"] == 4 and p["accuracy"] == 100 and p["courses"][0]["code"] == "PCO 514"
    course_round = s.post("/api/attempts", json={"pool": "course", "course_id": cid, "count": 2}).json()
    assert len(course_round["questions"]) == 2


def test_edit_check_remove_and_regenerate(app):
    adm, cid, mat = class_setup(app)
    quiz_id = wait(adm, adm.post("/api/quizzes/generate", json={"course_id": cid, "material_ids": [mat["id"]],
                                                                "questions": 3, "course_code": "PCOL",
                                                                "share": True}).json()["job_id"])["result"]["quiz_id"]
    qs = adm.get(f"/api/quizzes/{quiz_id}").json()["questions"]
    s = client(app, "ada@x.ng", "Ada")
    assert s.patch(f"/api/questions/{qs[0]['id']}", json={"q": "Changed by a student?"}).status_code == 403
    e = adm.patch(f"/api/questions/{qs[0]['id']}", json={"q": "Which receptor does bisoprolol block?",
                                                         "a": 1, "checked": True}).json()
    assert e["edited"] and e["checked"] and e["a"] == 1
    assert adm.patch(f"/api/questions/{qs[0]['id']}", json={"o": ["A", "a", "B", "C"]}).status_code == 400
    adm.delete(f"/api/questions/{qs[1]['id']}")
    assert adm.get(f"/api/quizzes/{quiz_id}").json()["count"] == 2
    before = qs[2]["q"]
    job = wait(adm, adm.post(f"/api/questions/{qs[2]['id']}/regenerate").json()["job_id"])
    assert job["status"] == "done", job
    after = next(q for q in adm.get(f"/api/quizzes/{quiz_id}").json()["questions"] if q["id"] == qs[2]["id"])
    assert after["q"] != before


def test_admin_users_plan_and_password_reset(app):
    adm, cid, mat = class_setup(app)
    s = client(app, "ada@x.ng", "Ada")
    users = adm.get("/api/admin").json()["users"]
    ada = next(u for u in users if u["email"] == "ada@x.ng")
    assert s.get("/api/admin").status_code == 403
    adm.patch(f"/api/admin/users/{ada['id']}", json={"plan": "pro"})
    me = s.get("/api/me").json()
    assert me["is_pro"] and me["usage"]["generation_limit"] is None
    temp = adm.post(f"/api/admin/users/{ada['id']}/reset-password").json()["temporary_password"]
    assert s.get("/api/me").status_code == 401                                # old sessions ended
    assert s.post("/api/login", json={"email": "ada@x.ng", "password": temp}).status_code == 200
    settings = adm.put("/api/admin/settings", json={"invite_code": "newcode"}).json()["settings"]
    assert settings["invite_code"] == "NEWCODE"
    assert TestClient(app.app).post("/api/signup", json={"email": "c@x.ng", "name": "Chi", "password": "password123",
                                                         "invite": "PHARM7"}).status_code == 400


def test_interrupted_reading_resumes_and_retry(app):
    adm, cid, mat = class_setup(app)
    with app.Sessions() as db:                      # as if the server died mid-read
        m = db.get(app.Material, mat["id"])
        m.status, m.sections = "processing", []
        db.commit()
    note = adm.get(f"/api/courses/{cid}").json()["notes"][0]
    assert note["status"] == "processing" and note["can_retry"] and note["progress"] is None
    assert app.jobs.resume_unfinished() == 1
    for _ in range(300):
        if adm.get(f"/api/materials/{mat['id']}").json()["status"] == "ready":
            break
        time.sleep(0.02)
    assert adm.get(f"/api/materials/{mat['id']}").json()["topics"]
    with app.Sessions() as db:                      # a failed read can be retried by hand
        db.get(app.Material, mat["id"]).status = "error"
        db.commit()
    job = wait(adm, adm.post(f"/api/materials/{mat['id']}/retry").json()["job_id"])
    assert job["status"] == "done" and adm.get(f"/api/materials/{mat['id']}").json()["status"] == "ready"
    s = client(app, "ada@x.ng", "Ada")
    assert s.post(f"/api/materials/{mat['id']}/retry").status_code == 403   # only the owner/admin


def test_keep_awake_pings_only_while_busy(app, monkeypatch):
    pings = []
    monkeypatch.setattr(app.jobs, "keepalive_url", "https://example.test")
    import webapp.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod.urllib.request, "urlopen", lambda url, timeout=0: pings.append(url) or type("R", (), {"read": lambda self: b""})())
    monkeypatch.setattr(jobs_mod.time, "sleep", lambda s: (_ for _ in ()).throw(StopIteration) if len(sleeps) >= 2 else sleeps.append(s))
    sleeps = []
    app.jobs.state["x"] = {"status": "running", "kind": "process", "created": time.time()}
    try:
        app.jobs._keep_awake()
    except StopIteration:
        pass
    assert pings == ["https://example.test/healthz", "https://example.test/healthz"]
    app.jobs.state["x"]["status"] = "done"
    assert not app.jobs.busy()
