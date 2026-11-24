# Quiz engine

Turns course PDFs and notes into exam-style multiple-choice questions while
spending as few model tokens and calls as possible. Steps 1–4 run entirely on
your machine; only step 5 calls an AI model.

| Step | Module | What it does |
|---|---|---|
| 1. Extract | `extract.py` | Reads PDFs (PyMuPDF), Word `.docx` (headings, bold lines, tables) and `.txt`/`.md` notes, keeping font size, weight and position. Flags image-only (scanned) pages. |
| 1b. Read scans | `ocr.py` | Scanned pages go to **Tesseract first** (free, on the server, no tokens). Its text is kept when the page reads like clean print (≥25 words, mean confidence ≥80%, ≥75% of words at ≥80%), with line positions and sizes so headers, footers and headings are still found. Pages Tesseract can't read (handwriting, poor photos) go to Gemini as small greyscale images, 4 per call, cached per page image so they are paid for once (about 530 image tokens a page at the default medium detail). |
| 2. Clean | `clean.py` | Removes running headers/footers, page numbers, contents leaders, bare figure captions and repeated paragraphs; re-joins words split across lines; normalises bullets and whitespace; detects headings and their levels. |
| 3. Split | `split.py` | Cuts the text into topic sections that keep their heading path ("Chapter › Topic"). Merges tiny sections, splits long ones, skips references, contents, objectives and exercises. |
| 4. Plan | `plan.py` | Shares the requested number of questions across sections by length (never padding thin ones), then packs sections into as few model calls as possible under a token budget. |
| 5. Generate | `prompt.py`, `providers.py`, `generate.py` | One short fixed exam-style instruction per call plus only the planned sections. Gemini returns JSON in an enforced schema. Calls are paced under the free-tier per-minute limit, retried on rate limits/overloads, and fall back across models (Gemini, then Groq). |
| 6. Check & cache | `check.py`, `generate.py` | Rejects malformed questions (wrong option count, duplicate options, bad answer index, options like "A and B", stems that mention "the notes", duplicates) and caches each call by a fingerprint of its instruction and text, so re-runs cost nothing. |

## Exam profiles

Who the questions are for is a setting, not code. Profiles are JSON files in
`quizengine/profiles/`:

- `pep` — PCN pre-registration exam (PEP), with the nine PEP course codes.
- `undergrad` — any undergraduate course; takes `--level`, `--programme`,
  `--institution` and a free course name.

Add a new exam by adding a JSON file (or point `--profiles-dir` at your own folder).

## Use

```bash
pip install pymupdf python-docx    # plus pytest for the tests
apt-get install tesseract-ocr      # free OCR for printed scans (optional; without it scans go to Gemini)
python -m quizengine profiles
python -m quizengine prepare notes/pharmacology_ch4.pdf -n 20 --course PCOL
python -m quizengine prepare notes/pcl401.pdf -n 15 --profile undergrad \
    --level 400L --programme Pharmacy --institution UNIBEN --course "PCL 401 Chemotherapy"
```

Scanned pages: `--ocr auto` reads them during `prepare` (it is automatic for
`generate`), `--no-tesseract` sends every scan to Gemini, and
`--ocr-resolution low|medium|high` sets the image detail for Gemini.

Useful options: `--exclude 'introduction'` (skip more headings), `--keep-all`
(skip nothing), `--max-input-tokens 6000` and `--max-per-call 10` (call sizing),
`--show-text` (print exactly what would be sent), `-o plan.json`.

The report shows estimated tokens before and after cleaning, what was removed,
which sections were skipped, how many questions each section gets and how many
model calls the run needs. The plan JSON holds the cleaned section text the
generation step will send. Token figures are estimates (about 4 characters per
token); the generation step will report the model's own counts.

Generate questions (prepare + model call + checks):

```bash
python -m quizengine generate notes/pharmacology_ch4.pdf -n 20 --course PCOL --difficulty mixed
python -m quizengine generate notes/pharmacology_ch4.pdf -n 20 --course PCOL --dry-run   # show what would be sent
```

Options: `--difficulty easy|mixed|hard`, `--providers gemini,groq` (order),
`--model` (Gemini model to try first), `--think N` (Gemini thinking budget,
0 = off and cheapest), `--rpm 8` (pacing), `--no-cache`. Output goes to
`<file>.questions.json`; every question carries its source section and pages
and is marked AI-generated, for review before it joins a shared bank.

API keys: in the Claude Code cloud environment the keys are network secrets
added automatically for `generativelanguage.googleapis.com` and `api.groq.com`.
Elsewhere, set `GEMINI_API_KEY` and/or `GROQ_API_KEY`.

In code (this is what the web app's server will call on upload):

```python
from quizengine import prepare, load_profile
prepared = prepare("notes.pdf", 20, load_profile("pep"), course="PCOL")
prepared.to_dict()
```

## Web app (class version)

`webapp/` is a FastAPI server with a phone-first single-page front end for a
class pilot:

- **Accounts**: sign-up with a class invite code; the `ADMIN_EMAIL` account is the
  class admin; free / Pro plans (Pro: share own quizzes with the class, no daily
  limit). The admin can change the invite code and limits, give Pro, reset
  passwords and remove accounts.
- **Courses and a class library**: class courses (admin) and personal courses;
  notes uploaded by the admin to a class course are shared with everyone,
  other uploads stay private. Each upload is read once (Tesseract, then Gemini
  for handwriting) and its cleaned topic sections are stored.
- **Make a quiz** from chosen files and topics, with difficulty, PEP or
  undergraduate style and fact-checking. Free students get 5 new quizzes a day
  (class quizzes and re-made quizzes from saved results are free). Jobs run one
  at a time in a queue so the shared model key stays inside rate limits.
- **Practice and timed exams** with answers checked on the server, results by
  topic, "retry the ones I missed", a missed-questions pool, check-later stars,
  mixed practice per course, a progress page and a day streak.
- **Review and edit**: owners and the admin can edit a question, mark a flagged
  one as correct, replace it with a newly generated one, or remove it.
- **Reader**: the cleaned text of each upload, with "Quiz me on this" per topic.

```bash
pip install -r requirements.txt
ADMIN_EMAIL=you@example.com CLASS_INVITE_CODE=PHARM7 uvicorn webapp.server:app --reload
```

Settings: `DATABASE_URL` (Postgres, e.g. Supabase; without it a local SQLite
file in `DATA_DIR` is used), `ADMIN_EMAIL`, `CLASS_INVITE_CODE`,
`GEMINI_API_KEY`, `GROQ_API_KEY`. Uploaded files, model results and all app
data live in the database, so a host restart loses nothing.

Deploy: `Dockerfile` (includes Tesseract) and `render.yaml` for Render; point
`DATABASE_URL` at Supabase's **Session pooler** connection string (IPv4).

## Tests

```bash
python -m pytest -q
```

Tests never call a real model (a fake provider stands in); the web tests use a temporary SQLite database and were also run against Postgres. The tests build a realistic notes PDF (running header and footer, contents
page, three heading sizes, split words, bullets, a caption, references, a
scanned page) and check every cleaning and planning rule against it.

Course materials (`materials/`) and generated plans/questions are git-ignored
so notes are never committed by accident.
