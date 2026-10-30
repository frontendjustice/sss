"""Runs steps 1-4 (extract, clean, split, plan) and builds the report.

This is the function the web app's server will call on upload; the command
line tool is a thin wrapper around it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .clean import CleanDoc, clean
from .extract import Document, extract
from .plan import Plan, make_plan
from .profiles import Profile
from .split import DEFAULT_EXCLUDE, SplitResult, split
from .tokens import estimate


@dataclass
class Prepared:
    source: str
    profile: Profile
    course: str | None
    doc: Document
    cleaned: CleanDoc
    split: SplitResult
    plan: Plan

    @property
    def raw_tokens(self) -> int:
        return estimate(self.doc.raw_text)

    @property
    def clean_tokens(self) -> int:
        return estimate(self.cleaned.text)

    def to_dict(self, include_text: bool = True) -> dict:
        p = self.plan
        return {
            "source": self.source,
            "profile": {"key": self.profile.key, "name": self.profile.name,
                        "audience": self.profile.audience_text, "params": self.profile.params},
            "course": self.course,
            "pages": len(self.doc.pages),
            "scanned_pages": self.doc.scanned_pages,
            "tokens": {"raw": self.raw_tokens, "cleaned": self.clean_tokens,
                       "sent_source": p.source_tokens, "est_input_total": p.input_tokens,
                       "est_output_total": p.output_tokens},
            "removed": dict(self.cleaned.stats),
            "skipped_sections": self.split.skipped,
            "questions": {"requested": p.requested, "planned": p.planned},
            "warnings": p.warnings,
            "sections": [
                {"id": s.id, "title": s.title, "pages": [s.page_start, s.page_end], "words": s.words,
                 "tokens": s.tokens, "questions": s.questions, **({"text": s.text} if include_text else {})}
                for s in p.sections
            ],
            "calls": [c.__dict__ for c in p.calls],
        }


def prepare(path: str | Path, questions: int, profile: Profile, course: str | None = None,
            exclude: str | None = DEFAULT_EXCLUDE, extra_exclude: list[str] | None = None,
            max_input_tokens: int = 6000, max_per_call: int = 10) -> Prepared:
    course_name = profile.course_name(course)
    profile.render(profile.audience)  # fail early if a placeholder has no value
    doc = extract(path)
    cleaned = clean(doc)
    pattern = exclude
    if extra_exclude:
        extra = "|".join(f"(?:{x})" for x in extra_exclude)
        pattern = f"(?:{pattern})|^\\s*(?:{extra})" if pattern else f"^\\s*(?:{extra})"
    result = split(cleaned, exclude=pattern, start_title=Path(path).stem.replace("_", " "))
    plan = make_plan(result.sections, questions, max_input_tokens, max_per_call)
    return Prepared(str(path), profile, course_name, doc, cleaned, result, plan)
