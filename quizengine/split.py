"""Step 3: split cleaned text into topic sections, each carrying its heading path.

Sections are sized for question writing: tiny ones are merged into their
neighbour, long ones are cut at paragraph (or sentence) boundaries. Sections
that never make good questions (references, contents, learning objectives,
review exercises...) are skipped by default.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .clean import Block, CleanDoc
from .tokens import estimate

DEFAULT_EXCLUDE = (
    r"^\s*(\d+(\.\d+)*\.?\s+)?("
    r"references?|bibliography|works cited|further reading|suggested reading|recommended (texts|reading)"
    r"|acknowledge?ments?|(table of )?contents|index|glossary|copyright|about the authors?|preface|foreword|dedication"
    r"|learning (objectives|outcomes)|objectives|aims and objectives"
    r"|review questions|self[- ]assessment( questions)?|exercises?|tutorial questions|past questions"
    r")\b"
)
MIN_WORDS = 120
MAX_WORDS = 1000


@dataclass
class Section:
    path: list[str]
    paragraphs: list[str]
    page_start: int
    page_end: int
    id: str = ""
    questions: int = 0
    subheadings: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return " › ".join(self.path)

    @property
    def text(self) -> str:
        return "\n".join(self.paragraphs)

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def tokens(self) -> int:
        return estimate(self.text)


@dataclass
class SplitResult:
    sections: list[Section]
    skipped: list[str]


def split(doc: CleanDoc, min_words: int = MIN_WORDS, max_words: int = MAX_WORDS,
          exclude: str | None = DEFAULT_EXCLUDE, start_title: str = "Introduction") -> SplitResult:
    excl = re.compile(exclude, re.I) if exclude else None
    raw: list[Section] = []
    skipped: list[str] = []
    stack: list[tuple[int, str]] = []
    current: Section | None = None
    skip_level: int | None = None

    def close():
        nonlocal current
        if current and current.paragraphs:
            raw.append(current)
        current = None

    for b in doc.blocks:
        if b.kind == "heading":
            if skip_level is not None and b.level > skip_level:
                if excl and excl.match(b.text):
                    skipped.append(b.text)
                continue  # a sub-heading inside a skipped section
            skip_level = None
            close()
            while stack and stack[-1][0] >= b.level:
                stack.pop()
            if excl and excl.match(b.text):
                skipped.append(b.text)
                skip_level = b.level
                continue
            stack.append((b.level, b.text))
            continue
        if skip_level is not None:
            continue
        if current is None:
            path = [t for _, t in stack] or [start_title]
            current = Section(path, [], b.page, b.page)
        current.paragraphs.append(b.text)
        current.page_end = b.page
    close()

    sections = _merge_small(raw, min_words, max_words)
    sections = [part for s in sections for part in _cut_long(s, max_words)]
    for s in sections:
        s.id = hashlib.sha1((s.title + "\n" + s.text).encode()).hexdigest()[:12]
    return SplitResult(sections, skipped)


def _merge_small(sections: list[Section], min_words: int, max_words: int) -> list[Section]:
    """Join a short section with its neighbour under the same top-level heading
    (either it is short, or the one before it was too short to stand alone)."""
    out: list[Section] = []
    for s in sections:
        prev = out[-1] if out else None
        fits = prev and prev.path[:1] == s.path[:1] and prev.words + s.words <= max_words
        if fits and s.words < min_words:
            # a short sub-topic joins the section before it, labelled so its name is not lost
            prev.paragraphs.append(f"[{s.path[-1]}]")
            prev.paragraphs.extend(s.paragraphs)
            prev.subheadings.append(s.path[-1])
            prev.page_end = s.page_end
        elif fits and prev.words < min_words:
            # a short lead-in (e.g. a chapter intro) is kept as context at the top of the next,
            # more specific section, which keeps its own title
            s.paragraphs = prev.paragraphs + s.paragraphs
            s.subheadings = prev.subheadings + s.subheadings
            s.page_start = prev.page_start
            out[-1] = s
        else:
            out.append(s)
    return out


_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])")


def _cut_long(s: Section, max_words: int) -> list[Section]:
    if s.words <= max_words:
        return [s]
    pieces: list[str] = []
    for p in s.paragraphs:
        if len(p.split()) > max_words:
            pieces.extend(_SENTENCE.split(p))
        else:
            pieces.append(p)
    parts: list[list[str]] = [[]]
    count = 0
    for p in pieces:
        n = len(p.split())
        if parts[-1] and count + n > max_words:
            parts.append([])
            count = 0
        parts[-1].append(p)
        count += n
    total = len(parts)
    return [Section(s.path[:-1] + [f"{s.path[-1]} ({i}/{total})"], ps, s.page_start, s.page_end)
            for i, ps in enumerate(parts, start=1)]
