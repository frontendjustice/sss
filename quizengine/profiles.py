"""Exam profiles: who the questions are for and what standard they follow.

Each profile is a JSON file, so a new exam type (another licensing exam, a
specific university course) is added by dropping in a file, not by changing
code. Built-in profiles live in quizengine/profiles/; extra ones can be loaded
from any folder. Text fields may use {placeholders} filled from `params`
(e.g. the undergraduate profile's {level}, {programme} and {institution}).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

BUILTIN_DIR = Path(__file__).parent / "profiles"


@dataclass
class Profile:
    key: str
    name: str
    audience: str
    standard: str
    question_types: list[str]
    options: int = 4
    difficulty: str = "mixed"
    params: dict[str, str] = field(default_factory=dict)
    courses: dict[str, str] = field(default_factory=dict)  # empty = any course name allowed

    def with_params(self, **params: str | None) -> "Profile":
        merged = {**self.params, **{k: v for k, v in params.items() if v}}
        return Profile(self.key, self.name, self.audience, self.standard, list(self.question_types),
                       self.options, self.difficulty, merged, dict(self.courses))

    def render(self, text: str) -> str:
        try:
            return text.format(**self.params)
        except KeyError as missing:
            raise ValueError(f"Profile '{self.key}' needs a value for {missing}") from None

    @property
    def audience_text(self) -> str:
        return self.render(self.audience)

    def course_name(self, course: str | None) -> str | None:
        """Resolve a course code (PEP) or accept a free course name (profiles without a list)."""
        if not course:
            return None
        if not self.courses:
            return course
        code = course.upper()
        if code in self.courses:
            return self.courses[code]
        known = ", ".join(self.courses)
        raise ValueError(f"Unknown course '{course}' for profile '{self.key}'. Use one of: {known}")


def available(extra_dir: str | Path | None = None) -> dict[str, Path]:
    found = {p.stem: p for p in sorted(BUILTIN_DIR.glob("*.json"))}
    if extra_dir:
        found.update({p.stem: p for p in sorted(Path(extra_dir).glob("*.json"))})
    return found


def load(key: str, extra_dir: str | Path | None = None) -> Profile:
    files = available(extra_dir)
    if key not in files:
        raise ValueError(f"No profile '{key}'. Available: {', '.join(files)}")
    data = json.loads(files[key].read_text(encoding="utf-8"))
    return Profile(**data)
