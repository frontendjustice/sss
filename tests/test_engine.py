import json

import pytest

from quizengine import load_profile, prepare
from quizengine.clean import clean
from quizengine.extract import extract
from quizengine.plan import make_plan
from quizengine.split import split
from quizengine.cli import main

from samples import build_markdown, build_notes_pdf


@pytest.fixture(scope="module")
def notes_pdf(tmp_path_factory):
    return build_notes_pdf(tmp_path_factory.mktemp("pdf") / "pharmacology_notes.pdf")


@pytest.fixture(scope="module")
def cleaned(notes_pdf):
    return clean(extract(notes_pdf))


def test_scanned_page_is_flagged(notes_pdf):
    doc = extract(notes_pdf)
    assert doc.scanned_pages == [len(doc.pages)]


def test_running_header_footer_and_page_numbers_removed(cleaned):
    text = cleaned.text
    assert "PHA 301" not in text
    assert "Page 2 of" not in text
    assert cleaned.stats["running headers/footers"] == 6  # header + footer on each of 3 text pages


def test_contents_and_caption_removed(cleaned):
    assert cleaned.stats["table-of-contents lines"] == 4
    assert "Figure 1.1" not in cleaned.text


def test_split_words_rejoined(cleaned):
    assert cleaned.stats["re-joined split words"] > 0
    assert "- " not in cleaned.text.replace("\n- ", "\n")  # no dangling hyphen + space left in prose
    assert "contractility" in cleaned.text


def test_bullets_normalised(cleaned):
    assert "- Taper doses before stopping" in cleaned.text
    assert "•" not in cleaned.text


def test_duplicate_paragraph_removed(cleaned):
    assert cleaned.stats["repeated paragraphs"] == 1


def test_headings_detected_with_levels(cleaned):
    heads = [(b.level, b.text) for b in cleaned.blocks if b.kind == "heading"]
    levels = dict((t, l) for l, t in heads)
    assert levels["1 Cardiovascular Pharmacology"] < levels["1.1 Beta blockers"]
    assert levels["1.1 Beta blockers"] < levels["Key points"]


def test_sections_skip_references_and_objectives(cleaned):
    result = split(cleaned)
    titles = [s.title for s in result.sections]
    assert "References" in result.skipped and "Learning objectives" in result.skipped
    assert not any("Katzung" in s.text for s in result.sections)
    assert any(t.endswith("Beta blockers") for t in titles)
    assert any(t.endswith("ACE inhibitors") for t in titles)
    # the short "Key points" bullets are folded into the beta blocker section
    beta = next(s for s in result.sections if s.title.endswith("Beta blockers"))
    assert "Key points" in beta.subheadings and "- Taper doses" in beta.text


def test_allocation_sums_and_respects_capacity(cleaned):
    sections = split(cleaned).sections
    plan = make_plan(sections, 8, max_per_call=5)
    assert plan.planned == 8 == sum(s.questions for s in plan.sections)
    assert all(s.questions <= s.words // 60 for s in plan.sections)
    assert all(c.questions <= 5 or len(c.section_ids) == 1 for c in plan.calls)
    assert sum(c.questions for c in plan.calls) == 8


def test_too_many_questions_warns(cleaned):
    sections = split(cleaned).sections
    plan = make_plan(sections, 500)
    assert plan.planned < 500 and plan.warnings


def test_token_budget_splits_calls(cleaned):
    sections = split(cleaned).sections
    plan = make_plan(sections, 10, max_input_tokens=900, max_per_call=50)
    assert len(plan.calls) >= 2


def test_cleaning_saves_tokens(notes_pdf):
    pr = prepare(notes_pdf, 10, load_profile("pep"), "PCOL")
    assert pr.clean_tokens < pr.raw_tokens * 0.9
    assert pr.course == "Pharmacology"


def test_undergrad_profile_params(notes_pdf):
    prof = load_profile("undergrad").with_params(level="400L", programme="Pharmacy", institution="UNIBEN")
    pr = prepare(notes_pdf, 5, prof, course="PCL 401 Chemotherapy")
    assert pr.profile.audience_text == "400L Pharmacy students at UNIBEN"
    assert pr.course == "PCL 401 Chemotherapy"


def test_pep_rejects_unknown_course(notes_pdf):
    with pytest.raises(ValueError):
        prepare(notes_pdf, 5, load_profile("pep"), "XYZ")


def test_markdown_notes(tmp_path):
    md = build_markdown(tmp_path / "notes.md")
    pr = prepare(md, 3, load_profile("undergrad"))
    titles = [s.title for s in pr.plan.sections]
    assert titles and all("References" not in t for t in titles)
    assert titles[0] == "Pharmaceutics › Emulsions"


def test_cli_writes_plan(notes_pdf, tmp_path, capsys):
    out = tmp_path / "plan.json"
    assert main(["prepare", str(notes_pdf), "-n", "8", "--course", "PCOL", "-o", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["questions"]["planned"] == 8
    assert data["sections"][0]["text"]
    assert "Tokens (estimated)" in capsys.readouterr().out
