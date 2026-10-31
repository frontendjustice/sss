# Quiz engine

Turns course PDFs and notes into exam-style multiple-choice questions while
spending as few model tokens and calls as possible. Steps 1–4 run entirely on
your machine; only step 5 calls an AI model.

| Step | Module | What it does |
|---|---|---|
| 1. Extract | `extract.py` | Reads PDFs (PyMuPDF), Word `.docx` (headings, bold lines, tables) and `.txt`/`.md` notes, keeping font size, weight and position. Flags image-only (scanned) pages. |
| 1b. Read scans | `ocr.py` | Sends scanned/handwritten pages to Gemini as small greyscale images, 4 per call, and caches each page's transcription by image fingerprint, so photos are paid for once (about 530 image tokens a page at the default medium detail). |
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

## Tests

```bash
python -m pytest -q
```

Tests never call a real model (a fake provider stands in). The tests build a realistic notes PDF (running header and footer, contents
page, three heading sizes, split words, bullets, a caption, references, a
scanned page) and check every cleaning and planning rule against it.

Course materials (`materials/`) and generated plans/questions are git-ignored
so notes are never committed by accident.
