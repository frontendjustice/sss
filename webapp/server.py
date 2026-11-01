"""Web app for the quiz engine (personal version).

Upload notes, preview the question plan, generate a quiz in the background,
then practise or review it. Data lives in DATA_DIR as plain JSON files; one
generation runs at a time so free-tier rate limits are respected.

Run locally:  uvicorn webapp.server:app --reload
Settings (environment variables):
  APP_PASSWORD   if set, every page and API call needs this password (HTTP Basic, any username)
  DATA_DIR       where uploads, quizzes and the model cache are kept (default: ./data)
  GEMINI_API_KEY / GROQ_API_KEY   model keys (not needed in the Claude cloud environment)
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from quizengine import profiles
from quizengine.generate import generate
from quizengine.ocr import transcribe
from quizengine.pipeline import prepare
from quizengine.providers import Gemini, Groq

DATA = Path(os.environ.get("DATA_DIR", "data")).resolve()
MATERIALS, SETS, CACHE = DATA / "materials", DATA / "sets", DATA / "cache"
for d in (MATERIALS, SETS, CACHE):
    d.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).parent / "static"
ALLOWED = {".pdf", ".docx", ".txt", ".md"}
MAX_UPLOAD = 25 * 1024 * 1024
PASSWORD = os.environ.get("APP_PASSWORD", "")

app = FastAPI(title="Quiz Builder", docs_url=None, redoc_url=None)


@app.middleware("http")
async def password_gate(request: Request, call_next):
    if PASSWORD and request.url.path != "/healthz":
        ok = False
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(given, PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="Quiz Builder"'})
    return await call_next(request)


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


# ---------- profiles ----------

@app.get("/api/profiles")
def list_profiles():
    out = []
    for key in profiles.available():
        p = profiles.load(key)
        out.append({"key": p.key, "name": p.name, "courses": p.courses, "params": p.params})
    return out


# ---------- materials ----------

def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _material(mid: str) -> dict:
    meta = MATERIALS / _safe(mid) / "meta.json"
    if not meta.exists():
        raise HTTPException(404, "Material not found")
    return _read(meta)


def _safe(ident: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{12}", ident or ""):
        raise HTTPException(404, "Not found")
    return ident


def _set_summary(s: dict) -> dict:
    qs = s["questions"]
    return {"id": s["id"], "title": s["title"], "created": s["created"], "count": len(qs),
            "flagged": sum(1 for q in qs if q.get("flag")), "difficulty": s["params"].get("difficulty"),
            "course": s.get("course")}


@app.get("/api/materials")
def list_materials():
    sets_by_material: dict[str, list] = {}
    for f in SETS.glob("*.json"):
        s = _read(f)
        sets_by_material.setdefault(s["material_id"], []).append(_set_summary(s))
    out = []
    for meta in MATERIALS.glob("*/meta.json"):
        m = _read(meta)
        m["sets"] = sorted(sets_by_material.get(m["id"], []), key=lambda s: s["created"], reverse=True)
        out.append(m)
    return sorted(out, key=lambda m: m["uploaded"], reverse=True)


@app.post("/api/materials")
async def upload(file: UploadFile = File(...)):
    name = Path(file.filename or "notes").name
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(400, f"Upload a {', '.join(sorted(ALLOWED))} file")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File is larger than 25 MB")
    mid = uuid.uuid4().hex[:12]
    folder = MATERIALS / mid
    folder.mkdir()
    stored = "source" + suffix
    (folder / stored).write_bytes(data)
    meta = {"id": mid, "name": name, "file": stored, "size": len(data), "uploaded": time.time()}
    _write(folder / "meta.json", meta)
    return meta


@app.delete("/api/materials/{mid}")
def delete_material(mid: str):
    m = _material(mid)
    for f in SETS.glob("*.json"):
        if _read(f)["material_id"] == m["id"]:
            f.unlink()
    folder = MATERIALS / m["id"]
    for f in folder.iterdir():
        f.unlink()
    folder.rmdir()
    return {"ok": True}


# ---------- jobs (plan / generate) ----------

class JobRequest(BaseModel):
    material_id: str
    mode: str = Field("generate", pattern="^(plan|generate)$")
    questions: int = Field(20, ge=1, le=200)
    profile: str = "pep"
    course: str | None = None
    level: str | None = None
    programme: str | None = None
    institution: str | None = None
    difficulty: str = Field("mixed", pattern="^(easy|mixed|hard)$")
    fact_check: bool = True


JOBS: dict[str, dict] = {}
RUN_LOCK = threading.Lock()


@app.post("/api/jobs")
def start_job(req: JobRequest):
    m = _material(req.material_id)
    try:
        prof = profiles.load(req.profile).with_params(level=req.level, programme=req.programme,
                                                     institution=req.institution)
        prof.course_name(req.course)
        prof.render(prof.audience)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not RUN_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Another quiz is being prepared. Try again when it finishes.")
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"id": jid, "status": "running", "mode": req.mode, "log": [], "result": None, "error": None,
                 "started": time.time()}
    threading.Thread(target=_run_job, args=(jid, req, m, prof), daemon=True).start()
    return JOBS[jid]


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    if jid not in JOBS:
        raise HTTPException(404, "Job not found")
    return JOBS[jid]


def _run_job(jid: str, req: JobRequest, m: dict, prof) -> None:
    job = JOBS[jid]
    log = lambda *parts: job["log"].append(" ".join(str(p) for p in parts).strip())
    try:
        path = MATERIALS / m["id"] / m["file"]
        log("Reading and cleaning the file…")
        transcriber = lambda doc: transcribe(doc, cache_dir=CACHE, log=log)
        pr = prepare(path, req.questions, prof, req.course, transcriber=transcriber)
        report = pr.to_dict(include_text=False)
        report["source"] = m["name"]
        report["sections"] = [dict(s, preview=_preview(sec.text))
                              for s, sec in zip(report["sections"], pr.plan.sections)]
        if req.mode == "plan":
            job["result"] = {"report": report}
            log("Plan ready.")
            job["status"] = "done"
            return
        if not pr.plan.calls:
            raise RuntimeError("No usable text was found in this file.")
        log(f"Writing {pr.plan.planned} questions in {len(pr.plan.calls)} call(s)…")
        providers = [Gemini(), Groq()]
        gen = generate(pr, providers, req.difficulty, CACHE, use_cache=True, log=log, fact_check=req.fact_check)
        if not gen.questions:
            raise RuntimeError("The model returned no usable questions. Try again in a minute.")
        sid = uuid.uuid4().hex[:12]
        title = f"{Path(m['name']).stem} · {len(gen.questions)} questions"
        _write(SETS / f"{sid}.json", {
            "id": sid, "material_id": m["id"], "title": title, "created": time.time(), "course": pr.course,
            "params": req.model_dump(), "report": report, "questions": gen.questions,
            "usage": {"tokens": gen.tokens, "rejected": dict(gen.rejected),
                      "calls": [c.__dict__ for c in gen.calls]},
        })
        flagged = sum(1 for q in gen.questions if q.get("flag"))
        log(f"Done: {len(gen.questions)} questions" + (f", {flagged} flagged by fact-check." if flagged else "."))
        job["result"] = {"set_id": sid, "report": report, "tokens": gen.tokens}
        job["status"] = "done"
    except Exception as e:  # report any failure to the page instead of a silent hang
        job["error"] = str(e).splitlines()[0][:300] if str(e) else e.__class__.__name__
        job["status"] = "error"
        log(f"Stopped: {job['error']}")
    finally:
        RUN_LOCK.release()


def _preview(text: str, n: int = 220) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


# ---------- quiz sets ----------

def _set(sid: str) -> dict:
    path = SETS / f"{_safe(sid)}.json"
    if not path.exists():
        raise HTTPException(404, "Quiz not found")
    return _read(path)


@app.get("/api/sets/{sid}")
def get_set(sid: str):
    return _set(sid)


@app.delete("/api/sets/{sid}")
def delete_set(sid: str):
    s = _set(sid)
    (SETS / f"{s['id']}.json").unlink()
    return {"ok": True}


@app.delete("/api/sets/{sid}/questions/{index}")
def delete_question(sid: str, index: int):
    s = _set(sid)
    if not 0 <= index < len(s["questions"]):
        raise HTTPException(404, "Question not found")
    s["questions"].pop(index)
    s["title"] = re.sub(r"\d+ questions$", f"{len(s['questions'])} questions", s["title"])
    _write(SETS / f"{s['id']}.json", s)
    return _set_summary(s)


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
