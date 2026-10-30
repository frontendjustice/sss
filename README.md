# Quiz engine

Turns course PDFs and notes into clean, compact sections and a question plan,
ready for exam-style quiz generation. Built to spend as few model tokens and
calls as possible. Steps 1–4 below run entirely on your machine and call no AI
model.

| Step | Module | What it does |
|---|---|---|
| 1. Extract | `extract.py` | Reads PDFs (PyMuPDF) and `.txt`/`.md` notes line by line with font size, weight and position. Flags image-only (scanned) pages. |
| 2. Clean | `clean.py` | Removes running headers/footers, page numbers, contents leaders, bare figure captions and repeated paragraphs; re-joins words split across lines; normalises bullets and whitespace; detects headings and their levels. |
| 3. Split | `split.py` | Cuts the text into topic sections that keep their heading path ("Chapter › Topic"). Merges tiny sections, splits long ones, skips references, contents, objectives and exercises. |
| 4. Plan | `plan.py` | Shares the requested number of questions across sections by length (never padding thin ones), then packs sections into as few model calls as possible under a token budget. |
| 5. Generate | *next* | Gemini (free tier) with Groq fallback, returning compact JSON in the PEP Drill format. |
| 6. Check & store | *next* | Validates every question and caches results by section fingerprint so the same notes are never sent twice. |

## Exam profiles

Who the questions are for is a setting, not code. Profiles are JSON files in
`quizengine/profiles/`:

- `pep` — PCN pre-registration exam (PEP), with the nine PEP course codes.
- `undergrad` — any undergraduate course; takes `--level`, `--programme`,
  `--institution` and a free course name.

Add a new exam by adding a JSON file (or point `--profiles-dir` at your own folder).

## Use

```bash
pip install pymupdf            # plus pytest for the tests
python -m quizengine profiles
python -m quizengine prepare notes/pharmacology_ch4.pdf -n 20 --course PCOL
python -m quizengine prepare notes/pcl401.pdf -n 15 --profile undergrad \
    --level 400L --programme Pharmacy --institution UNIBEN --course "PCL 401 Chemotherapy"
```

Useful options: `--exclude 'introduction'` (skip more headings), `--keep-all`
(skip nothing), `--max-input-tokens 6000` and `--max-per-call 10` (call sizing),
`--show-text` (print exactly what would be sent), `-o plan.json`.

The report shows estimated tokens before and after cleaning, what was removed,
which sections were skipped, how many questions each section gets and how many
model calls the run needs. The plan JSON holds the cleaned section text the
generation step will send. Token figures are estimates (about 4 characters per
token); the generation step will report the model's own counts.

In code (this is what the web app's server will call on upload):

```python
from quizengine import prepare, load_profile
prepared = prepare("notes.pdf", 20, load_profile("pep"), course="PCOL")
prepared.to_dict()
```

## Tests

```bash
python -m pytest -q
```

The tests build a realistic notes PDF (running header and footer, contents
page, three heading sizes, split words, bullets, a caption, references, a
scanned page) and check every cleaning and planning rule against it.

Course materials (`materials/`) and generated plans/questions are git-ignored
so notes are never committed by accident.
