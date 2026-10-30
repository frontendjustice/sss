"""Step 2: turn extracted lines into clean headings and paragraphs.

This is where most of the token saving happens. It drops running headers and
footers, page numbers, table-of-contents leaders, bare figure captions and
repeated paragraphs, repairs words split across lines, and normalises
whitespace and bullet glyphs. Headings are detected from font size, weight,
numbering and capitalisation so the next step can split by topic.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from .extract import Document, Line

HEADER_FOOTER_ZONE = 0.10   # top/bottom 10% of a page
REPEAT_SHARE = 0.4          # a line in that zone on >= 40% of pages is a running header/footer
HEADING_SIZE_RATIO = 1.15   # font this much larger than body text = heading
MAX_HEADING_WORDS = 14

_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
_PAGE_NUMBER = re.compile(r"^\s*(page\s*)?[\divxlc]{1,6}(\s*(of|/)\s*\d+)?\s*$", re.I)
_TOC_LEADER = re.compile(r"(\.\s*){4,}\s*\d+\s*$|…{2,}\s*\d+\s*$")
_CAPTION = re.compile(r"^\s*(fig(ure)?|table|plate|chart|diagram)\.?\s*\d+[\.\d]*\s*[:.\-–]?", re.I)
_BULLET = re.compile(r"^\s*([•●▪◦‣⁃∙·■□➢✓✔→*\-–—]|\(?[a-z0-9ivx]{1,3}[\).])\s+", re.I)
_GLYPH_BULLET = re.compile(r"^\s*[•●▪◦‣⁃∙·■□➢✓✔→*–—]\s+")
_NUMBERED_HEADING = re.compile(r"^(chapter|unit|module|section|lecture|topic|part)\s+[\w.]+\b|^\d+(\.\d+){1,3}\.?\s+\S", re.I)
_SPACES = re.compile(r"[ \t  -​  　]+")


@dataclass
class Block:
    kind: str            # "heading" or "para"
    text: str
    page: int
    level: int = 0       # headings only: 1 = biggest


@dataclass
class CleanDoc:
    source: str
    blocks: list[Block]
    body_size: float
    scanned_pages: list[int]
    stats: Counter = field(default_factory=Counter)

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


def normalise(text: str) -> str:
    for k, v in _LIGATURES.items():
        text = text.replace(k, v)
    text = text.replace("­", "")  # soft hyphen
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\n\t")
    return _SPACES.sub(" ", text).strip()


def clean(doc: Document) -> CleanDoc:
    stats: Counter = Counter()
    lines: list[Line] = []
    for page in doc.pages:
        for line in page.lines:
            line.text = normalise(line.text)
            lines.append(line)

    running = _running_headers_footers(doc)
    kept: list[Line] = []
    for line in lines:
        t = line.text
        if not t:
            kept.append(line)  # paragraph break marker (text files)
            continue
        if _key(t) in running and _in_margin(line):
            stats["running headers/footers"] += 1
        elif _PAGE_NUMBER.match(t):
            stats["page numbers"] += 1
        elif _TOC_LEADER.search(t):
            stats["table-of-contents lines"] += 1
        elif _CAPTION.match(t) and len(t.split()) <= 12:
            stats["bare figure/table captions"] += 1
        else:
            kept.append(line)

    body = _body_size(kept)
    big_sizes = sorted({l.size for l in kept if l.text and l.size >= body * HEADING_SIZE_RATIO}, reverse=True)
    level_of_size = {s: i + 1 for i, s in enumerate(big_sizes)}
    minor_level = len(big_sizes) + 1

    blocks: list[Block] = []
    para: list[str] = []
    para_page = 1
    prev: Line | None = None

    def flush():
        nonlocal para
        if para:
            blocks.append(Block("para", " ".join(para).strip(), para_page))
            para = []

    for line in kept:
        t = line.text
        if not t:
            flush()
            prev = None
            continue
        level = _heading_level(line, body, level_of_size, minor_level)
        if level:
            flush()
            blocks.append(Block("heading", t.rstrip(":").strip(), line.page, min(level, 6)))
            prev = None
            continue
        starts_bullet = bool(_BULLET.match(t))
        if starts_bullet:
            t = _GLYPH_BULLET.sub("- ", t)
        if para and (starts_bullet or _gap(prev, line)):
            flush()
        if not para:
            para_page = line.page
            para.append(t)
        elif para[-1].endswith("-") and len(para[-1]) > 1 and para[-1][-2].isalpha() and t[:1].islower():
            para[-1] = para[-1][:-1] + t  # re-join a word split across lines
            stats["re-joined split words"] += 1
        else:
            para.append(t)
        prev = line
    flush()

    blocks = _dedupe(blocks, stats)
    return CleanDoc(doc.source, blocks, body, doc.scanned_pages, stats)


def _key(text: str) -> str:
    return re.sub(r"\d+", "#", text.lower()).strip()


def _in_margin(line: Line) -> bool:
    if not line.page_height:
        return False
    zone = line.page_height * HEADER_FOOTER_ZONE
    return line.y1 <= zone or line.y0 >= line.page_height - zone


def _running_headers_footers(doc: Document) -> set[str]:
    pdf_pages = [p for p in doc.pages if p.height]
    if len(pdf_pages) < 3:
        return set()
    seen: Counter = Counter()
    for page in pdf_pages:
        keys = {_key(normalise(l.text)) for l in page.lines if l.text.strip() and _in_margin(l)}
        seen.update(keys)
    need = max(2, int(len(pdf_pages) * REPEAT_SHARE))
    return {k for k, n in seen.items() if n >= need and k}


def _body_size(lines: list[Line]) -> float:
    weight: Counter = Counter()
    for l in lines:
        if l.text:
            weight[l.size] += len(l.text)
    return weight.most_common(1)[0][0] if weight else 11.0


def _heading_level(line: Line, body: float, level_of_size: dict, minor: int) -> int:
    t = line.text
    words = t.split()
    if len(words) > MAX_HEADING_WORDS or len(t) > 120 or len(t) < 2:
        return 0
    if t[-1] in ".,;!?":
        return 0
    if line.size in level_of_size:
        return level_of_size[line.size]
    if _BULLET.match(t) and not _NUMBERED_HEADING.match(t):
        return 0
    if line.size < body * 0.95:
        return 0  # footnotes, small print
    numbered = _NUMBERED_HEADING.match(t)
    if numbered:
        depth = t.split()[0].count(".")
        return minor + max(0, depth - 1)
    letters = [c for c in t if c.isalpha()]
    if line.bold and len(words) <= 10 and not t.endswith(":"):
        return minor
    if len(letters) >= 4 and all(c.isupper() for c in letters) and 1 <= len(words) <= 8:
        return minor
    return 0


def _gap(prev: Line | None, line: Line) -> bool:
    """A bigger-than-usual vertical gap (or a new page after a finished sentence) starts a new paragraph."""
    if prev is None:
        return True
    if line.page != prev.page:
        return prev.text.endswith((".", ":", "?", "!"))
    height = max(prev.y1 - prev.y0, 1.0)
    return (line.y0 - prev.y1) > height * 0.4


def _dedupe(blocks: list[Block], stats: Counter) -> list[Block]:
    seen: set[str] = set()
    out: list[Block] = []
    for b in blocks:
        if b.kind == "para":
            k = re.sub(r"[^a-z0-9]", "", b.text.lower())
            if len(k) > 40 and k in seen:
                stats["repeated paragraphs"] += 1
                continue
            seen.add(k)
        out.append(b)
    return out
