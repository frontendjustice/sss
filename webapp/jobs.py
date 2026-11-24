"""Background work, one job at a time: reading an upload, generating a quiz, regenerating a
question. A single worker keeps the shared Gemini key inside its per-minute limit; extra
requests wait in the queue instead of failing.

Free hosts restart or sleep without warning, which kills a running job. So on start-up any
upload still marked "processing" is queued again (pages already read are in the cache and are
not paid for twice), and while jobs are waiting or running the server visits its own public
address every few minutes so the host does not put it to sleep mid-job."""
from __future__ import annotations

import os
import queue
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import date
from pathlib import Path

from sqlalchemy import select

from quizengine import profiles
from quizengine.clean import clean
from quizengine.extract import extract
from quizengine.generate import generate
from quizengine.ocr import transcribe
from quizengine.pipeline import Selection, select as select_sections
from quizengine.plan import Call, Plan
from quizengine.split import Section, split
from quizengine.tokens import estimate

from .db import Blob, DbCache, Material, Question, Quiz, Usage


class Jobs:
    KEEPALIVE_SECONDS = 240

    def __init__(self, sessions, make_providers, keepalive_url: str | None = None):
        self.sessions = sessions
        self.make_providers = make_providers   # replaced in tests with a fake model
        self.cache = DbCache(sessions)
        self.state: dict[str, dict] = {}
        self.queue: queue.Queue = queue.Queue()
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()
        self.keepalive_url = keepalive_url if keepalive_url is not None else os.environ.get("RENDER_EXTERNAL_URL", "")
        if self.keepalive_url:
            threading.Thread(target=self._keep_awake, daemon=True).start()

    def busy(self) -> bool:
        return any(j["status"] in ("queued", "running") for j in self.state.values())

    def _keep_awake(self):
        while True:
            time.sleep(self.KEEPALIVE_SECONDS)
            if self.busy():
                try:
                    urllib.request.urlopen(self.keepalive_url.rstrip("/") + "/healthz", timeout=20).read()
                except Exception:
                    pass

    def resume_unfinished(self) -> int:
        """Queue again every upload still marked processing (its earlier job died with the server)."""
        with self.sessions() as db:
            stuck = db.scalars(select(Material).where(Material.status == "processing")).all()
            pending = [(m.id, m.owner_id) for m in stuck if not self.material_job(m.id)]
        for mid, owner in pending:
            self.submit("process", owner, material_id=mid)
        return len(pending)

    def material_job(self, material_id: int) -> dict | None:
        """The queued or running job reading this upload, if any."""
        for j in self.state.values():
            if j["kind"] == "process" and j.get("material_id") == material_id and j["status"] in ("queued", "running"):
                return j
        return None

    # ---------- queue ----------

    def submit(self, kind: str, user_id: int, **args) -> dict:
        jid = uuid.uuid4().hex[:12]
        waiting = sum(1 for j in self.state.values() if j["status"] in ("queued", "running"))
        job = {"id": jid, "kind": kind, "user_id": user_id, "status": "queued", "log": [], "result": None,
               "error": None, "created": time.time(), "material_id": args.get("material_id")}
        if waiting:
            job["log"].append(f"Waiting for {waiting} other job(s) to finish…")
        self.state[jid] = job
        self.queue.put((jid, kind, user_id, args))
        return job

    def get(self, jid: str) -> dict | None:
        return self.state.get(jid)

    def wait(self, jid: str, timeout: float = 60) -> dict:
        end = time.time() + timeout
        while time.time() < end and self.state[jid]["status"] in ("queued", "running"):
            time.sleep(0.02)
        return self.state[jid]

    def _run(self):
        while True:
            jid, kind, user_id, args = self.queue.get()
            job = self.state[jid]
            job["status"] = "running"
            log = lambda *parts: job["log"].append(" ".join(str(p) for p in parts).strip())
            try:
                job["result"] = getattr(self, f"_{kind}")(user_id, log, **args)
                job["status"] = "done"
            except Exception as e:  # report failures to the page rather than hang
                job["error"] = (str(e).splitlines() or [e.__class__.__name__])[0][:300]
                job["status"] = "error"
                log(f"Stopped: {job['error']}")
                if kind == "process":
                    with self.sessions() as db:
                        m = db.get(Material, args["material_id"])
                        if m:
                            m.status, m.error = "error", job["error"]
                            db.commit()
            finally:
                for old in [k for k, v in self.state.items() if v["status"] in ("done", "error")
                            and time.time() - v["created"] > 3600]:
                    self.state.pop(old, None)

    # ---------- jobs ----------

    def _process(self, user_id: int, log, material_id: int) -> dict:
        with self.sessions() as db:
            m = db.get(Material, material_id)
            data = db.get(Blob, m.blob_sha).data
            name, title = m.name, m.title
        log("Reading the file…")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ("source" + Path(name).suffix.lower())
            path.write_bytes(data)
            doc = extract(path)
            ocr = transcribe(doc, cache_dir=self.cache, log=log) if doc.scanned_pages else None
        cleaned = clean(doc)
        result = split(cleaned, start_title=title)
        for s in result.sections:
            s.source = name
        sections = [s.to_json() for s in result.sections]
        report = {"pages": len(doc.pages), "ocr": ocr.__dict__ if ocr else None,
                  "tokens": {"raw": estimate(doc.raw_text), "cleaned": estimate(cleaned.text)},
                  "removed": dict(cleaned.stats), "skipped": result.skipped,
                  "words": sum(s.words for s in result.sections)}
        with self.sessions() as db:
            m = db.get(Material, material_id)
            m.sections, m.report, m.pages, m.status, m.error = sections, report, len(doc.pages), "ready", None
            if ocr and ocr.calls:
                self._count(db, user_id, tokens=ocr.input_tokens + ocr.output_tokens)
            db.commit()
        if not sections:
            log("No readable text was found in this file.")
        log(f"Ready: {len(sections)} topic section(s), {report['words']:,} words.")
        return {"material_id": material_id, "sections": len(sections)}

    def _generate(self, user_id: int, log, material_ids: list[int], section_ids: list[str] | None,
                  questions: int, difficulty: str, fact_check: bool, profile: str, course_code: str | None,
                  params: dict, course_id: int | None, title: str, visibility: str) -> dict:
        prof = profiles.load(profile).with_params(**params)
        with self.sessions() as db:
            mats = [db.get(Material, i) for i in material_ids]
            sections = [s for m in mats for s in m.sections if not section_ids or s["id"] in section_ids]
            names = [m.name for m in mats]
        if not sections:
            raise RuntimeError("No topics were selected, or the files have no readable text.")
        sel = select_sections(sections, questions, prof, course_code, source=names[0])
        for w in sel.plan.warnings:
            log(f"Note: {w}")
        log(f"Writing {sel.plan.planned} of {questions} questions in {len(sel.plan.calls)} call(s)…")
        gen = generate(sel, self.make_providers(), difficulty, self.cache, log=log, fact_check=fact_check)
        if not gen.questions:
            raise RuntimeError("The model returned no usable questions. Try again in a minute.")
        live = gen.tokens["input"] + gen.tokens["output"]
        with self.sessions() as db:
            quiz = Quiz(owner_id=user_id, course_id=course_id, visibility=visibility, title=title,
                        params={"material_ids": material_ids, "section_ids": section_ids, "difficulty": difficulty,
                                "fact_check": fact_check, "profile": profile, "course_code": course_code,
                                "profile_params": params},
                        requested=questions, warnings=sel.plan.warnings,
                        usage={"tokens": gen.tokens, "rejected": dict(gen.rejected)})
            db.add(quiz)
            db.flush()
            for i, q in enumerate(gen.questions):
                db.add(Question(quiz_id=quiz.id, position=i, data=q))
            self._count(db, user_id, generations=1 if live else 0, tokens=live)
            db.commit()
            qid = quiz.id
        flagged = sum(1 for q in gen.questions if q.get("flag"))
        log(f"Done: {len(gen.questions)} of {questions} questions"
            + (f", {flagged} flagged by fact-check." if flagged else ".")
            + ("" if live else " (all from saved results: no tokens used)"))
        return {"quiz_id": qid, "count": len(gen.questions), "tokens": gen.tokens}

    def _regenerate(self, user_id: int, log, question_id: int) -> dict:
        with self.sessions() as db:
            q = db.get(Question, question_id)
            quiz = db.get(Quiz, q.quiz_id)
            p = quiz.params
            sid = q.data.get("source", {}).get("section_id")
            mats = [db.get(Material, i) for i in p.get("material_ids", [])]
            section = next((s for m in mats if m for s in m.sections if s["id"] == sid), None)
            others = [x.data["q"] for x in db.scalars(select(Question).where(Question.quiz_id == quiz.id,
                                                                              Question.removed.is_(False)))]
        if not section:
            raise RuntimeError("The notes this question came from are no longer available.")
        prof = profiles.load(p.get("profile", "pep")).with_params(**p.get("profile_params", {}))
        sec = Section.from_json(section)
        sec.questions = 1   # exactly one, however short the section is
        sel = Selection(sec.source, prof, prof.course_name(p.get("course_code")),
                        Plan(1, 1, [sec], [Call([sec.id], 1, 0, 0)]))
        log("Writing a replacement question…")
        gen = generate(sel, self.make_providers(), p.get("difficulty", "mixed"), self.cache, log=log,
                       fact_check=p.get("fact_check", True), avoid=others)
        if not gen.questions:
            raise RuntimeError("The model returned no usable question. Try again in a minute.")
        with self.sessions() as db:
            q = db.get(Question, question_id)
            q.data, q.edited, q.checked = gen.questions[0], False, False
            self._count(db, user_id, tokens=gen.tokens["input"] + gen.tokens["output"])
            db.commit()
        log("Replaced.")
        return {"question_id": question_id}

    @staticmethod
    def _count(db, user_id: int, generations: int = 0, uploads: int = 0, tokens: int = 0) -> None:
        day = date.today().isoformat()
        row = db.get(Usage, (user_id, day))
        if not row:
            row = Usage(user_id=user_id, day=day, generations=0, uploads=0, tokens=0)
            db.add(row)
        row.generations += generations
        row.uploads += uploads
        row.tokens += tokens
