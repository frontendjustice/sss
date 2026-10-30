"""Step 1: pull text lines (with font size and position) out of PDFs and text notes.

Nothing here calls an AI model. Pages that have no extractable text (scans,
photos of handwritten notes) are flagged so a later step can send only those
pages to a vision model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# A page with fewer characters than this, but with images on it, is treated as scanned.
SCANNED_MAX_CHARS = 40


@dataclass
class Line:
    text: str
    size: float          # font size in points (text files: 11 for body, larger for headings)
    bold: bool
    page: int            # 1-based
    y0: float            # top of the line, in points from the top of the page
    y1: float
    page_height: float


@dataclass
class Page:
    number: int
    height: float
    lines: list[Line] = field(default_factory=list)
    raw_text: str = ""
    scanned: bool = False


@dataclass
class Document:
    source: str
    pages: list[Page]

    @property
    def raw_text(self) -> str:
        return "\n".join(p.raw_text for p in self.pages)

    @property
    def scanned_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.scanned]


def extract(path: str | Path) -> Document:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix in (".txt", ".md", ".markdown"):
        return _extract_text(path)
    raise ValueError(f"Unsupported file type: {suffix} (use .pdf, .txt or .md)")


def _extract_pdf(path: Path) -> Document:
    import pymupdf  # imported here so text-only use does not need it

    pages: list[Page] = []
    with pymupdf.open(path) as pdf:
        for index, pdf_page in enumerate(pdf, start=1):
            height = pdf_page.rect.height
            page = Page(number=index, height=height, raw_text=pdf_page.get_text())
            data = pdf_page.get_text("dict")
            for block in data["blocks"]:
                if block.get("type") != 0:  # 0 = text, 1 = image
                    continue
                for line in block["lines"]:
                    spans = [s for s in line["spans"] if s["text"].strip()]
                    if not spans:
                        continue
                    text = "".join(s["text"] for s in line["spans"])
                    # The dominant span (most characters) decides size and weight.
                    main = max(spans, key=lambda s: len(s["text"].strip()))
                    bold = bool(main["flags"] & 16) or "bold" in main["font"].lower()
                    x0, y0, x1, y1 = line["bbox"]
                    page.lines.append(Line(text, round(main["size"], 1), bold, index, y0, y1, height))
            chars = sum(len(l.text.strip()) for l in page.lines)
            page.scanned = chars < SCANNED_MAX_CHARS and bool(pdf_page.get_images())
            pages.append(page)
    return Document(str(path), pages)


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def _extract_text(path: Path) -> Document:
    """Plain text / Markdown notes become one long 'page'. Markdown headings get heading sizes."""
    text = path.read_text(encoding="utf-8", errors="replace")
    page = Page(number=1, height=0.0, raw_text=text)
    y = 0.0
    for raw in text.splitlines():
        y += 1
        if not raw.strip():
            # A blank line is a paragraph break; mark it with an empty line so cleaning can see it.
            page.lines.append(Line("", 11.0, False, 1, y, y, 0.0))
            continue
        m = _MD_HEADING.match(raw.strip())
        if m:
            level = len(m.group(1))
            page.lines.append(Line(m.group(2).strip(), 11.0 + (7 - level) * 2, True, 1, y, y, 0.0))
        else:
            page.lines.append(Line(raw.rstrip(), 11.0, False, 1, y, y, 0.0))
    return Document(str(path), [page])
