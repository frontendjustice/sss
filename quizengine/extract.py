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
    scanned: bool = False       # no text layer: needs reading as an image
    transcribed: bool = False   # text came from reading the page image


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

    @property
    def transcribed_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.transcribed]


def extract(path: str | Path) -> Document:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix in (".txt", ".md", ".markdown"):
        return _extract_text(path)
    if suffix == ".docx":
        return _extract_docx(path)
    raise ValueError(f"Unsupported file type: {suffix} (use .pdf, .docx, .txt or .md)")


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


def markdown_lines(text: str, page: int = 1) -> list[Line]:
    """Markdown/plain text as lines. Headings get sizes by level; each line and bullet stands alone
    (a blank line is kept as an empty Line so cleaning sees the paragraph break)."""
    lines: list[Line] = []
    for y, raw in enumerate(text.splitlines(), start=1):
        s = raw.strip()
        if not s:
            lines.append(Line("", 11.0, False, page, y, y, 0.0))
            continue
        m = _MD_HEADING.match(s)
        if m:
            level = len(m.group(1))
            lines.append(Line(m.group(2).strip().strip("*").strip(), 11.0 + (7 - level) * 2, True, page, y, y, 0.0))
        else:
            if s.startswith(("- ", "* ", "+ ")):
                lines.append(Line("", 11.0, False, page, y, y, 0.0))  # every bullet is its own point
            lines.append(Line(raw.rstrip(), 11.0, False, page, y, y, 0.0))
    return lines


def _extract_text(path: Path) -> Document:
    """Plain text / Markdown notes become one long 'page'. Markdown headings get heading sizes."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return Document(str(path), [Page(number=1, height=0.0, raw_text=text, lines=markdown_lines(text))])


def _extract_docx(path: Path) -> Document:
    """Word documents: heading styles (Title, Heading 1-6) become headings, a paragraph whose text is
    all bold reads as a sub-heading, and tables become one 'cell | cell' line per row."""
    import docx  # python-docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    lines: list[Line] = []
    raw: list[str] = []
    y = 0

    def add(text: str, size: float = 11.0, bold: bool = False):
        nonlocal y
        y += 1
        lines.append(Line(text, size, bold, 1, y, y, 0.0))

    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = p.text.strip()
            raw.append(p.text)
            add("")  # Word paragraphs are separate blocks
            if not text:
                continue
            style = (p.style.name if p.style is not None else "").lower()
            runs = [r for r in p.runs if r.text.strip()]
            bold = bool(runs) and all(r.bold or (r.style is not None and "strong" in r.style.name.lower()) for r in runs)
            if style == "title":
                add(text, 23.0, True)
            elif style.startswith("heading") and style[-1:].isdigit():
                add(text, 11.0 + (7 - int(style[-1])) * 2, True)
            else:
                add(text, 11.0, bold)
        elif tag == "tbl":
            for row in Table(child, d).rows:
                cells: list[str] = []
                for c in row.cells:
                    t = " / ".join(x.strip() for x in c.text.splitlines() if x.strip())
                    if t and (not cells or cells[-1] != t):  # merged cells repeat their text
                        cells.append(t)
                if cells:
                    raw.append(" | ".join(cells))
                    add("")
                    add(" | ".join(cells))
    return Document(str(path), [Page(number=1, height=0.0, raw_text="\n".join(raw), lines=lines)])
