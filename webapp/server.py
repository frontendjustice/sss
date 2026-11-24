"""Quiz Builder web app: accounts, courses, a class library, quiz generation, saved practice.

Run locally:  uvicorn webapp.server:app --reload
Settings (environment variables):
  DATABASE_URL       Postgres connection string (Supabase); without it a local SQLite file is used
  DATA_DIR           folder for the local SQLite file (default ./data)
  ADMIN_EMAIL        the account with this email becomes the admin (else the first account does)
  CLASS_INVITE_CODE  starting invite code for classmates (the admin can change it in the app)
  GEMINI_API_KEY / GROQ_API_KEY   model keys (not needed in the Claude cloud environment)
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from quizengine import profiles
from quizengine.providers import Gemini, Groq

from . import auth
from .db import (Answer, Attempt, Blob, Course, LoginSession, Mark, Material, Question, Quiz, Usage, User,
                 make_engine, set_setting, setting)
from .jobs import Jobs

STATIC = Path(__file__).parent / "static"
ALLOWED = {".pdf", ".docx", ".txt", ".md"}
MAX_UPLOAD = 25 * 1024 * 1024
DEFAULTS = {"daily_limit": "5", "upload_limit": "3"}   # per free student per day

engine = make_engine()
Sessions = sessionmaker(engine, expire_on_commit=False)
jobs = Jobs(Sessions, lambda: [Gemini(), Groq()])
jobs.resume_unfinished()   # uploads whose reading was cut off by a restart carry on
app = FastAPI(title="Quiz Builder", docs_url=None, redoc_url=None)


def get_db():
    with Sessions() as db:
        yield db


def me(request: Request, db: Session = Depends(get_db)) -> User:
    return auth.require_user(db, request)


def admin(request: Request, db: Session = Depends(get_db)) -> User:
    return auth.require_admin(db, request)


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/favicon.svg")
@app.get("/favicon.ico")
def favicon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" '
           'fill="#0E6B5C"/><text x="32" y="44" font-size="34" text-anchor="middle" fill="#fff" '
           'font-family="Georgia,serif" font-style="italic">Q</text></svg>')
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


# ---------------- accounts ----------------

class SignUp(BaseModel):
    email: str
    name: str
    password: str
    invite: str = ""


class LogIn(BaseModel):
    email: str
    password: str


class PasswordChange(BaseModel):
    old: str
    new: str


def _today() -> str:
    return date.today().isoformat()


def _usage(db: Session, user: User) -> dict:
    row = db.get(Usage, (user.id, _today()))
    limit = int(setting(db, "daily_limit", DEFAULTS["daily_limit"]))
    uploads = int(setting(db, "upload_limit", DEFAULTS["upload_limit"]))
    return {"generations": row.generations if row else 0, "uploads": row.uploads if row else 0,
            "generation_limit": None if user.is_pro else limit, "upload_limit": None if user.is_pro else uploads}


def _user_json(db: Session, user: User) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role, "plan": user.plan,
            "is_admin": user.is_admin, "is_pro": user.is_pro, "usage": _usage(db, user)}


@app.get("/api/me")
def get_me(user: User = Depends(me), db: Session = Depends(get_db)):
    return _user_json(db, user)


@app.post("/api/signup")
def signup(body: SignUp, request: Request, response: Response, db: Session = Depends(get_db)):
    user = auth.sign_up(db, body.email, body.name, body.password, body.invite)
    auth.start_session(db, user, request, response)
    return _user_json(db, user)


@app.post("/api/login")
def login(body: LogIn, request: Request, response: Response, db: Session = Depends(get_db)):
    user = auth.log_in(db, body.email, body.password)
    auth.start_session(db, user, request, response)
    return _user_json(db, user)


@app.post("/api/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    auth.end_session(db, request, response)
    return {"ok": True}


@app.post("/api/password")
def change_password(body: PasswordChange, user: User = Depends(me), db: Session = Depends(get_db)):
    if not auth.check_password(body.old, user.password_hash):
        raise HTTPException(400, "Current password is not right")
    if len(body.new) < 8:
        raise HTTPException(400, "Use a password of at least 8 characters")
    user.password_hash = auth.hash_password(body.new)
    db.commit()
    return {"ok": True}


@app.get("/api/profiles")
def list_profiles():
    return [{"key": p.key, "name": p.name, "courses": p.courses, "params": p.params}
            for p in map(profiles.load, profiles.available())]


# ---------------- access rules ----------------

def _course(db: Session, user: User, cid: int, edit: bool = False) -> Course:
    c = db.get(Course, cid)
    if not c or (c.owner_id is not None and c.owner_id != user.id):
        raise HTTPException(404, "Course not found")
    if edit and not (c.owner_id == user.id or (c.owner_id is None and user.is_admin)):
        raise HTTPException(403, "Only the admin can change class courses")
    return c


def _material(db: Session, user: User, mid: int, edit: bool = False) -> Material:
    m = db.get(Material, mid)
    if not m or (m.visibility != "class" and m.owner_id != user.id):
        raise HTTPException(404, "Notes not found")
    if edit and not (m.owner_id == user.id or user.is_admin and m.visibility == "class"):
        raise HTTPException(403, "You can only change your own notes")
    return m


def _quiz(db: Session, user: User, qid: int, edit: bool = False) -> Quiz:
    q = db.get(Quiz, qid)
    if not q or (q.visibility != "class" and q.owner_id != user.id):
        raise HTTPException(404, "Quiz not found")
    if edit and not (q.owner_id == user.id or user.is_admin):
        raise HTTPException(403, "Only the quiz's owner or the admin can edit it")
    return q


def _question(db: Session, user: User, qid: int, edit: bool = False) -> tuple[Question, Quiz]:
    q = db.get(Question, qid)
    if not q:
        raise HTTPException(404, "Question not found")
    return q, _quiz(db, user, q.quiz_id, edit)


def _visible_quizzes(db: Session, user: User, course_id: int | None = None):
    stmt = select(Quiz).where(or_(Quiz.visibility == "class", Quiz.owner_id == user.id))
    if course_id is not None:
        stmt = stmt.where(Quiz.course_id == course_id)
    return db.scalars(stmt.order_by(Quiz.created.desc())).all()


# ---------------- courses ----------------

class CourseIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=200)
    shared: bool = False   # a class course (admin only)


def _latest_answers(db: Session, user: User) -> dict[int, Answer]:
    latest: dict[int, Answer] = {}
    for a in db.scalars(select(Answer).where(Answer.user_id == user.id).order_by(Answer.created)):
        latest[a.question_id] = a
    return latest


def _course_json(db: Session, user: User, c: Course, latest: dict[int, Answer] | None = None) -> dict:
    latest = latest if latest is not None else _latest_answers(db, user)
    mats = db.scalars(select(Material.id).where(Material.course_id == c.id,
                                                or_(Material.visibility == "class", Material.owner_id == user.id)))
    qids = [q.id for q in _visible_quizzes(db, user, c.id)]
    questions = db.scalars(select(Question.id).where(Question.quiz_id.in_(qids), Question.removed.is_(False))).all() \
        if qids else []
    done = [latest[q] for q in questions if q in latest]
    return {"id": c.id, "code": c.code, "title": c.title, "shared": c.owner_id is None,
            "materials": len(list(mats)), "quizzes": len(qids), "questions": len(questions),
            "practised": len(done), "accuracy": round(100 * sum(a.correct for a in done) / len(done)) if done else None}


@app.get("/api/courses")
def list_courses(user: User = Depends(me), db: Session = Depends(get_db)):
    latest = _latest_answers(db, user)
    rows = db.scalars(select(Course).where(or_(Course.owner_id.is_(None), Course.owner_id == user.id))
                      .order_by(Course.owner_id.is_not(None), Course.code))
    return [_course_json(db, user, c, latest) for c in rows]


@app.post("/api/courses")
def create_course(body: CourseIn, user: User = Depends(me), db: Session = Depends(get_db)):
    if body.shared and not user.is_admin:
        raise HTTPException(403, "Only the admin can add class courses")
    c = Course(code=body.code.strip().upper(), title=body.title.strip(), owner_id=None if body.shared else user.id)
    db.add(c)
    db.commit()
    return _course_json(db, user, c)


@app.patch("/api/courses/{cid}")
def edit_course(cid: int, body: CourseIn, user: User = Depends(me), db: Session = Depends(get_db)):
    c = _course(db, user, cid, edit=True)
    c.code, c.title = body.code.strip().upper(), body.title.strip()
    db.commit()
    return _course_json(db, user, c)


@app.delete("/api/courses/{cid}")
def delete_course(cid: int, user: User = Depends(me), db: Session = Depends(get_db)):
    db.delete(_course(db, user, cid, edit=True))
    db.commit()
    return {"ok": True}


def _material_json(m: Material, user: User, full: bool = False) -> dict:
    job = jobs.material_job(m.id) if m.status == "processing" else None
    progress = None
    if job:
        progress = job["log"][-1].strip() if job["log"] else ("Waiting in the queue…" if job["status"] == "queued" else "Starting…")
    out = {"id": m.id, "name": m.name, "title": m.title, "course_id": m.course_id, "shared": m.visibility == "class",
           "progress": progress, "can_retry": m.status == "error" or (m.status == "processing" and not job),
           "mine": m.owner_id == user.id, "status": m.status, "error": m.error, "pages": m.pages,
           "created": m.created, "words": (m.report or {}).get("words", 0),
           "topics": [{"id": s["id"], "title": " › ".join(s["path"]), "pages": s["pages"],
                       "words": len(" ".join(s["paragraphs"]).split())} for s in m.sections or []]}
    if full:
        out["report"] = m.report
    return out


def _quiz_json(db: Session, user: User, q: Quiz, latest: dict[int, Answer]) -> dict:
    qs = db.scalars(select(Question).where(Question.quiz_id == q.id, Question.removed.is_(False))).all()
    done = [latest[x.id] for x in qs if x.id in latest]
    owner = db.get(User, q.owner_id)
    return {"id": q.id, "title": q.title, "course_id": q.course_id, "shared": q.visibility == "class",
            "mine": q.owner_id == user.id, "can_edit": q.owner_id == user.id or user.is_admin,
            "by": "you" if q.owner_id == user.id else (owner.name if owner else "someone"),
            "count": len(qs), "requested": q.requested, "flagged": sum(1 for x in qs if x.data.get("flag") and not x.checked),
            "difficulty": q.params.get("difficulty"), "created": q.created, "warnings": q.warnings,
            "practised": len(done), "accuracy": round(100 * sum(a.correct for a in done) / len(done)) if done else None}


@app.get("/api/courses/{cid}")
def get_course(cid: int, user: User = Depends(me), db: Session = Depends(get_db)):
    c = _course(db, user, cid)
    latest = _latest_answers(db, user)
    mats = db.scalars(select(Material).where(Material.course_id == c.id, or_(Material.visibility == "class",
                                             Material.owner_id == user.id)).order_by(Material.created.desc()))
    return {**_course_json(db, user, c, latest), "can_edit": c.owner_id == user.id or (c.owner_id is None and user.is_admin),
            "notes": [_material_json(m, user) for m in mats],
            "quiz_list": [_quiz_json(db, user, q, latest) for q in _visible_quizzes(db, user, c.id)]}


# ---------------- notes (materials) ----------------

@app.post("/api/materials")
async def upload(file: UploadFile = File(...), course_id: int = Form(...), shared: bool = Form(False),
                 user: User = Depends(me), db: Session = Depends(get_db)):
    course = _course(db, user, course_id)
    if shared and not user.is_admin:
        raise HTTPException(403, "Only the admin can add notes to the class library")
    u = _usage(db, user)
    if u["upload_limit"] is not None and u["uploads"] >= u["upload_limit"]:
        raise HTTPException(429, f"You've reached today's limit of {u['upload_limit']} uploads. Try again tomorrow.")
    name = Path(file.filename or "notes").name[:200]
    if Path(name).suffix.lower() not in ALLOWED:
        raise HTTPException(400, f"Upload a {', '.join(sorted(ALLOWED))} file")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File is larger than 25 MB")
    sha = hashlib.sha256(data).hexdigest()
    if not db.get(Blob, sha):
        db.add(Blob(sha256=sha, data=data, size=len(data)))
    title = re.sub(r"[_\-]+", " ", Path(name).stem).strip() or "Notes"
    m = Material(course_id=course.id, owner_id=user.id, visibility="class" if shared else "private",
                 name=name, title=title[:200], blob_sha=sha)
    db.add(m)
    jobs._count(db, user.id, uploads=1)
    db.commit()
    job = jobs.submit("process", user.id, material_id=m.id)
    return {"material": _material_json(m, user), "job_id": job["id"]}


@app.get("/api/materials/{mid}")
def get_material(mid: int, user: User = Depends(me), db: Session = Depends(get_db)):
    return _material_json(_material(db, user, mid), user, full=True)


@app.get("/api/materials/{mid}/text")
def material_text(mid: int, user: User = Depends(me), db: Session = Depends(get_db)):
    m = _material(db, user, mid)
    return {"title": m.title, "sections": [{"id": s["id"], "title": " › ".join(s["path"]), "pages": s["pages"],
                                            "paragraphs": s["paragraphs"]} for s in m.sections or []]}


@app.post("/api/materials/{mid}/retry")
def retry_material(mid: int, user: User = Depends(me), db: Session = Depends(get_db)):
    m = _material(db, user, mid, edit=True)
    job = jobs.material_job(m.id)
    if not job:
        m.status, m.error = "processing", None
        db.commit()
        job = jobs.submit("process", user.id, material_id=m.id)
    return {"job_id": job["id"]}


@app.delete("/api/materials/{mid}")
def delete_material(mid: int, user: User = Depends(me), db: Session = Depends(get_db)):
    db.delete(_material(db, user, mid, edit=True))
    db.commit()
    return {"ok": True}


@app.get("/api/jobs/{jid}")
def job_status(jid: str, user: User = Depends(me)):
    job = jobs.get(jid)
    if not job or job["user_id"] != user.id:
        raise HTTPException(404, "Job not found")
    return job


# ---------------- quizzes ----------------

class GenerateIn(BaseModel):
    course_id: int
    material_ids: list[int] = Field(min_length=1)
    section_ids: list[str] | None = None     # None = every topic in the chosen notes
    questions: int = Field(20, ge=1, le=100)
    difficulty: str = Field("mixed", pattern="^(easy|mixed|hard)$")
    fact_check: bool = True
    profile: str = "pep"
    course_code: str | None = None           # PEP course code, or the course name for undergrad
    level: str | None = None
    programme: str | None = None
    institution: str | None = None
    share: bool = False                      # make it a class quiz (admin, or pro)
    title: str | None = None


@app.post("/api/quizzes/generate")
def generate_quiz(body: GenerateIn, user: User = Depends(me), db: Session = Depends(get_db)):
    course = _course(db, user, body.course_id)
    mats = [_material(db, user, i) for i in body.material_ids]
    if any(m.status != "ready" for m in mats):
        raise HTTPException(400, "Wait until the notes have finished processing")
    u = _usage(db, user)
    if u["generation_limit"] is not None and u["generations"] >= u["generation_limit"]:
        raise HTTPException(429, f"You've used today's {u['generation_limit']} new quizzes. Class quizzes are"
                                 " always free to practise, and your limit resets tomorrow.")
    if body.share and not user.is_pro:
        raise HTTPException(403, "Sharing quizzes with the class is a Pro feature")
    params = {"level": body.level, "programme": body.programme, "institution": body.institution}
    try:
        prof = profiles.load(body.profile).with_params(**params)
        course_code = body.course_code if body.profile == "pep" else (body.course_code or f"{course.code} {course.title}")
        prof.course_name(course_code)
        prof.render(prof.audience)
    except ValueError as e:
        raise HTTPException(400, str(e))
    title = (body.title or (mats[0].title if len(mats) == 1 else f"{course.code} mixed topics"))[:200]
    job = jobs.submit("generate", user.id, material_ids=[m.id for m in mats], section_ids=body.section_ids,
                      questions=body.questions, difficulty=body.difficulty, fact_check=body.fact_check,
                      profile=body.profile, course_code=course_code, params=params, course_id=course.id,
                      title=title, visibility="class" if body.share else "private")
    return {"job_id": job["id"]}


def _question_json(x: Question, marked: set[int] | None = None, with_answer: bool = True) -> dict:
    d = x.data
    out = {"id": x.id, "q": d["q"], "o": d["o"], "type": d.get("type", ""),
           "topic": d.get("source", {}).get("section", ""), "pages": d.get("source", {}).get("pages", []),
           "file": d.get("source", {}).get("file", ""), "marked": x.id in (marked or set())}
    if with_answer:
        out.update(a=d["a"], e=d["e"], flag=d.get("flag"), edited=x.edited, checked=x.checked)
    return out


@app.get("/api/quizzes/{qid}")
def get_quiz(qid: int, user: User = Depends(me), db: Session = Depends(get_db)):
    q = _quiz(db, user, qid)
    marked = set(db.scalars(select(Mark.question_id).where(Mark.user_id == user.id)))
    qs = db.scalars(select(Question).where(Question.quiz_id == q.id, Question.removed.is_(False))
                    .order_by(Question.position)).all()
    return {**_quiz_json(db, user, q, _latest_answers(db, user)), "usage": q.usage,
            "questions": [_question_json(x, marked) for x in qs]}


class QuizEdit(BaseModel):
    title: str | None = None
    share: bool | None = None


@app.patch("/api/quizzes/{qid}")
def edit_quiz(qid: int, body: QuizEdit, user: User = Depends(me), db: Session = Depends(get_db)):
    q = _quiz(db, user, qid, edit=True)
    if body.title:
        q.title = body.title.strip()[:200]
    if body.share is not None:
        if body.share and not user.is_pro:
            raise HTTPException(403, "Sharing quizzes with the class is a Pro feature")
        q.visibility = "class" if body.share else "private"
    db.commit()
    return _quiz_json(db, user, q, _latest_answers(db, user))


@app.delete("/api/quizzes/{qid}")
def delete_quiz(qid: int, user: User = Depends(me), db: Session = Depends(get_db)):
    db.delete(_quiz(db, user, qid, edit=True))
    db.commit()
    return {"ok": True}


class QuestionEdit(BaseModel):
    q: str | None = Field(None, min_length=5)
    o: list[str] | None = None
    a: int | None = None
    e: str | None = None
    checked: bool | None = None   # "I've reviewed this; the answer is right"


@app.patch("/api/questions/{qid}")
def edit_question(qid: int, body: QuestionEdit, user: User = Depends(me), db: Session = Depends(get_db)):
    x, _ = _question(db, user, qid, edit=True)
    d = dict(x.data)
    if body.o is not None:
        opts = [o.strip() for o in body.o]
        if len(opts) < 2 or any(not o for o in opts) or len({o.lower() for o in opts}) != len(opts):
            raise HTTPException(400, "Each option needs text, and options must differ")
        d["o"] = opts
    if body.q is not None:
        d["q"] = body.q.strip()
    if body.e is not None:
        d["e"] = body.e.strip()
    if body.a is not None:
        d["a"] = body.a
    if not 0 <= d["a"] < len(d["o"]):
        raise HTTPException(400, "Pick which option is correct")
    if any(v is not None for v in (body.q, body.o, body.a, body.e)):
        x.edited = True
    if body.checked is not None:
        x.checked = body.checked
    x.data = d
    db.commit()
    return _question_json(x)


@app.delete("/api/questions/{qid}")
def remove_question(qid: int, user: User = Depends(me), db: Session = Depends(get_db)):
    x, _ = _question(db, user, qid, edit=True)
    x.removed = True
    db.commit()
    return {"ok": True}


@app.post("/api/questions/{qid}/regenerate")
def regenerate_question(qid: int, user: User = Depends(me), db: Session = Depends(get_db)):
    _question(db, user, qid, edit=True)
    return {"job_id": jobs.submit("regenerate", user.id, question_id=qid)["id"]}


# ---------------- practice ----------------

class AttemptIn(BaseModel):
    quiz_id: int | None = None
    pool: str | None = Field(None, pattern="^(missed|later|course)$")
    course_id: int | None = None
    question_ids: list[int] | None = None    # e.g. "retry the ones I missed" from a results page
    mode: str = Field("practice", pattern="^(practice|exam)$")
    count: int | None = Field(None, ge=1, le=200)
    minutes: int | None = Field(None, ge=1, le=600)


def _accessible_questions(db: Session, user: User, course_id: int | None = None) -> list[Question]:
    qids = [q.id for q in _visible_quizzes(db, user, course_id)]
    if not qids:
        return []
    return db.scalars(select(Question).where(Question.quiz_id.in_(qids), Question.removed.is_(False))).all()


@app.post("/api/attempts")
def start_attempt(body: AttemptIn, user: User = Depends(me), db: Session = Depends(get_db)):
    latest = _latest_answers(db, user)
    if body.question_ids:
        allowed = {x.id: x for x in _accessible_questions(db, user)}
        pool, title = [allowed[i] for i in body.question_ids if i in allowed], "Retry"
    elif body.quiz_id:
        quiz = _quiz(db, user, body.quiz_id)
        pool, title = db.scalars(select(Question).where(Question.quiz_id == quiz.id,
                                                        Question.removed.is_(False))).all(), quiz.title
    elif body.pool == "missed":
        pool = [x for x in _accessible_questions(db, user, body.course_id)
                if x.id in latest and not latest[x.id].correct]
        title = "Questions I missed"
    elif body.pool == "later":
        marked = set(db.scalars(select(Mark.question_id).where(Mark.user_id == user.id)))
        pool, title = [x for x in _accessible_questions(db, user, body.course_id) if x.id in marked], "Check later"
    elif body.pool == "course" and body.course_id:
        c = _course(db, user, body.course_id)
        pool, title = _accessible_questions(db, user, c.id), f"{c.code}: mixed practice"
        # new questions first, then ones answered wrong, then the rest
        random.shuffle(pool)
        pool.sort(key=lambda x: 0 if x.id not in latest else (1 if not latest[x.id].correct else 2))
    else:
        raise HTTPException(400, "Choose a quiz or a practice pool")
    if not pool:
        raise HTTPException(400, "There are no questions here yet")
    if body.pool != "course":
        random.shuffle(pool)
    pool = pool[: body.count] if body.count else pool
    a = Attempt(user_id=user.id, quiz_id=body.quiz_id, title=title[:255], mode=body.mode,
                question_ids=[x.id for x in pool], minutes=body.minutes if body.mode == "exam" else None,
                total=len(pool))
    db.add(a)
    db.commit()
    marked = set(db.scalars(select(Mark.question_id).where(Mark.user_id == user.id)))
    return {"id": a.id, "title": a.title, "mode": a.mode, "minutes": a.minutes,
            "questions": [_question_json(x, marked, with_answer=False) for x in pool]}


def _attempt(db: Session, user: User, aid: int) -> Attempt:
    a = db.get(Attempt, aid)
    if not a or a.user_id != user.id:
        raise HTTPException(404, "Attempt not found")
    return a


def _record(db: Session, user: User, a: Attempt, qid: int, chosen: int | None) -> tuple[Question, bool]:
    if qid not in a.question_ids:
        raise HTTPException(400, "That question is not in this round")
    x = db.get(Question, qid)
    correct = chosen is not None and chosen == x.data["a"]
    db.execute(delete(Answer).where(Answer.attempt_id == a.id, Answer.question_id == qid))
    if chosen is not None:
        db.add(Answer(attempt_id=a.id, user_id=user.id, question_id=qid, chosen=chosen, correct=correct))
    return x, correct


class AnswerIn(BaseModel):
    question_id: int
    chosen: int


@app.post("/api/attempts/{aid}/answer")
def answer(aid: int, body: AnswerIn, user: User = Depends(me), db: Session = Depends(get_db)):
    a = _attempt(db, user, aid)
    if a.mode != "practice" or a.finished:
        raise HTTPException(400, "Answers in exam mode are checked when you submit")
    x, correct = _record(db, user, a, body.question_id, body.chosen)
    db.commit()
    return {**_question_json(x), "correct": correct, "chosen": body.chosen}


class FinishIn(BaseModel):
    answers: dict[int, int | None] = {}   # exam mode: every answer at once


@app.post("/api/attempts/{aid}/finish")
def finish(aid: int, body: FinishIn, user: User = Depends(me), db: Session = Depends(get_db)):
    a = _attempt(db, user, aid)
    if a.mode == "exam" and not a.finished:
        for qid in a.question_ids:
            _record(db, user, a, qid, body.answers.get(qid))
        db.flush()
    given = {x.question_id: x for x in db.scalars(select(Answer).where(Answer.attempt_id == a.id))}
    questions = {x.id: x for x in db.scalars(select(Question).where(Question.id.in_(a.question_ids)))}
    a.answered, a.correct = len(given), sum(1 for x in given.values() if x.correct)
    a.finished = a.finished or time.time()
    db.commit()
    topics: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    review = []
    for qid in a.question_ids:
        x = questions.get(qid)
        if not x:
            continue
        g = given.get(qid)
        t = x.data.get("source", {}).get("section", "Other")
        if g:
            topics[t][1] += 1
            topics[t][0] += int(g.correct)
        review.append({**_question_json(x), "chosen": g.chosen if g else None, "correct": bool(g and g.correct)})
    return {"id": a.id, "title": a.title, "mode": a.mode, "total": a.total, "answered": a.answered,
            "correct": a.correct, "seconds": round(a.finished - a.created),
            "topics": sorted([{"topic": k, "correct": v[0], "answered": v[1]} for k, v in topics.items()],
                             key=lambda t: t["correct"] / t["answered"]),
            "review": review}


@app.post("/api/marks/{qid}")
def mark(qid: int, user: User = Depends(me), db: Session = Depends(get_db)):
    _question(db, user, qid)
    if not db.get(Mark, (user.id, qid)):
        db.add(Mark(user_id=user.id, question_id=qid))
        db.commit()
    return {"marked": True}


@app.delete("/api/marks/{qid}")
def unmark(qid: int, user: User = Depends(me), db: Session = Depends(get_db)):
    db.execute(delete(Mark).where(Mark.user_id == user.id, Mark.question_id == qid))
    db.commit()
    return {"marked": False}


# ---------------- today & progress ----------------

@app.get("/api/today")
def today(user: User = Depends(me), db: Session = Depends(get_db)):
    latest = _latest_answers(db, user)
    accessible = {x.id for x in _accessible_questions(db, user)}
    missed = sum(1 for q, a in latest.items() if q in accessible and not a.correct)
    later = db.scalar(select(func.count()).select_from(Mark).where(Mark.user_id == user.id)) or 0
    recent = db.scalars(select(Attempt).where(Attempt.user_id == user.id, Attempt.finished.is_not(None))
                        .order_by(Attempt.created.desc()).limit(5)).all()
    processing = db.scalars(select(Material).where(Material.owner_id == user.id,
                                                   Material.status == "processing")).all()
    days = {date.fromtimestamp(a.created) for a in db.scalars(select(Answer).where(Answer.user_id == user.id))}
    # consecutive days with at least one answer, counting back from today (or yesterday, before today's first)
    d = date.today() if date.today() in days else date.today() - timedelta(days=1)
    streak = 0
    while d in days:
        streak += 1
        d -= timedelta(days=1)
    new_class = db.scalars(select(Quiz).where(Quiz.visibility == "class", Quiz.owner_id != user.id)
                           .order_by(Quiz.created.desc()).limit(3)).all()
    return {"user": _user_json(db, user), "missed": missed, "later": later, "streak": streak,
            "answered_today": sum(1 for a in latest.values() if date.fromtimestamp(a.created) == date.today()),
            "recent": [{"id": a.id, "title": a.title, "mode": a.mode, "correct": a.correct, "answered": a.answered,
                        "total": a.total, "created": a.created} for a in recent],
            "processing": [{"id": m.id, "title": m.title} for m in processing],
            "new_class_quizzes": [_quiz_json(db, user, q, latest) for q in new_class]}


@app.get("/api/progress")
def progress(user: User = Depends(me), db: Session = Depends(get_db)):
    latest = _latest_answers(db, user)
    questions = {x.id: x for x in _accessible_questions(db, user)}
    quiz_course = {q.id: q.course_id for q in _visible_quizzes(db, user)}
    courses = {c.id: c for c in db.scalars(select(Course))}
    by_course: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    by_topic: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    for qid, a in latest.items():
        x = questions.get(qid)
        if not x:
            continue
        cid = quiz_course.get(x.quiz_id)
        by_course[cid][1] += 1
        by_course[cid][0] += int(a.correct)
        key = (cid, x.data.get("source", {}).get("section", "Other"))
        by_topic[key][1] += 1
        by_topic[key][0] += int(a.correct)
    weak = sorted([{"course": courses[c].code if c in courses else "", "topic": t, "correct": v[0], "answered": v[1]}
                   for (c, t), v in by_topic.items() if v[1] >= 3], key=lambda r: r["correct"] / r["answered"])[:8]
    history = db.scalars(select(Attempt).where(Attempt.user_id == user.id, Attempt.finished.is_not(None))
                         .order_by(Attempt.created.desc()).limit(30)).all()
    total = len([q for q in latest if q in questions])
    right = sum(1 for q, a in latest.items() if q in questions and a.correct)
    return {"answered": total, "accuracy": round(100 * right / total) if total else None,
            "courses": [{"id": c, "code": courses[c].code if c in courses else "Other",
                         "title": courses[c].title if c in courses else "", "answered": v[1],
                         "accuracy": round(100 * v[0] / v[1])} for c, v in by_course.items()],
            "weak": weak,
            "history": [{"id": a.id, "title": a.title, "mode": a.mode, "correct": a.correct, "answered": a.answered,
                         "total": a.total, "created": a.created} for a in history]}


# ---------------- admin ----------------

class AdminUserEdit(BaseModel):
    plan: str | None = Field(None, pattern="^(free|pro)$")


class SettingsIn(BaseModel):
    invite_code: str | None = Field(None, min_length=4, max_length=40)
    daily_limit: int | None = Field(None, ge=0, le=100)
    upload_limit: int | None = Field(None, ge=0, le=100)


@app.get("/api/admin")
def admin_overview(_: User = Depends(admin), db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.created)).all()
    usage = {u.user_id: u for u in db.scalars(select(Usage).where(Usage.day == _today()))}
    return {"settings": {"invite_code": setting(db, "invite_code") or os.environ.get("CLASS_INVITE_CODE", ""),
                         "daily_limit": int(setting(db, "daily_limit", DEFAULTS["daily_limit"])),
                         "upload_limit": int(setting(db, "upload_limit", DEFAULTS["upload_limit"]))},
            "users": [{"id": u.id, "name": u.name, "email": u.email, "role": u.role, "plan": u.plan,
                       "last_seen": u.last_seen, "today": {"generations": usage[u.id].generations if u.id in usage else 0,
                                                           "tokens": usage[u.id].tokens if u.id in usage else 0}}
                      for u in users]}


@app.put("/api/admin/settings")
def admin_settings(body: SettingsIn, _: User = Depends(admin), db: Session = Depends(get_db)):
    if body.invite_code is not None:
        set_setting(db, "invite_code", body.invite_code.strip().upper())
    if body.daily_limit is not None:
        set_setting(db, "daily_limit", str(body.daily_limit))
    if body.upload_limit is not None:
        set_setting(db, "upload_limit", str(body.upload_limit))
    db.commit()
    return admin_overview(_, db)


@app.patch("/api/admin/users/{uid}")
def admin_edit_user(uid: int, body: AdminUserEdit, _: User = Depends(admin), db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "User not found")
    if body.plan and not u.is_admin:
        u.plan = body.plan
    db.commit()
    return {"ok": True}


@app.post("/api/admin/users/{uid}/reset-password")
def admin_reset_password(uid: int, who: User = Depends(admin), db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u or u.id == who.id:
        raise HTTPException(400, "Change your own password from your account page")
    temp = auth.temporary_password()
    u.password_hash = auth.hash_password(temp)
    db.execute(delete(LoginSession).where(LoginSession.user_id == u.id))
    db.commit()
    return {"temporary_password": temp}


@app.delete("/api/admin/users/{uid}")
def admin_delete_user(uid: int, who: User = Depends(admin), db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u or u.id == who.id:
        raise HTTPException(400, "You can't remove your own account here")
    db.delete(u)
    db.commit()
    return {"ok": True}
