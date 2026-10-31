"""Step 1b: read scanned and handwritten pages once, as images, and keep the text.

Pages with no text layer are rendered as small greyscale JPEGs and sent to
Gemini a few at a time at LOW media resolution (about 270 image tokens a page,
which tested as accurate as higher settings on handwritten notes). Each page's
transcription is cached by a fingerprint of its image, so a page is never paid
for twice; after that, quizzes come from the cheap text like any typed PDF.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .extract import Document, markdown_lines
from .providers import Pacer, ProviderError, gemini_text, retrying

PROMPT_VERSION = "2"
RESOLUTIONS = {"low": "MEDIA_RESOLUTION_LOW", "medium": "MEDIA_RESOLUTION_MEDIUM", "high": "MEDIA_RESOLUTION_HIGH"}
OUTPUT_TOKENS_PER_PAGE = 700

PROMPT = (
    "Transcribe these lecture-note pages (handwritten or scanned) into clean Markdown. "
    "# for a lecture title, ## for sub-topics, '- ' for each point. Keep the content faithfully; fix obvious "
    "spelling, write common note abbreviations in full (b/w -> between, w/ -> with), keep drug names, numbers, "
    "units and arrows (->, ↑, ↓). Use plain Unicode for symbols (α, β, ≥, µg), never LaTeX. Read unclear "
    "handwriting as the medical/pharmacy term that fits the context (e.g. 'foam cells', not 'form cells'). "
    "Turn tables into '- ' lines. Write [?] for words you cannot read. Skip dates, "
    "signatures and page decorations. Start each page with its marker line exactly as given, e.g. <<<PAGE 3>>>, "
    "then that page's text. Output only the transcription."
)
_MARKER = re.compile(r"^\s*<<<\s*PAGE\s+(\d+)\s*>>>\s*$", re.M)


@dataclass
class OcrStats:
    pages: int = 0
    cached: int = 0
    calls: int = 0
    image_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    failed: list[int] | None = None


def render(pdf_path: str | Path, page_number: int, dpi: int = 150) -> bytes:
    import pymupdf
    with pymupdf.open(pdf_path) as pdf:
        pix = pdf[page_number - 1].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        return pix.tobytes("jpeg", jpg_quality=70)


def transcribe(doc: Document, models: list[str] | None = None, resolution: str = "medium", pages_per_call: int = 4,
               cache_dir: str | Path = ".quizcache", per_minute: float = 8, log=print) -> OcrStats:
    """Fill in text for every scanned page of `doc` (a PDF), in place."""
    if resolution not in RESOLUTIONS:
        raise ValueError(f"resolution must be one of: {', '.join(RESOLUTIONS)}")
    models = models or ["gemini-3.5-flash", "gemini-flash-latest", "gemini-2.5-flash"]
    stats = OcrStats(failed=[])
    pages = {p.number: p for p in doc.pages if p.scanned}
    if not pages:
        return stats
    cache = Path(cache_dir) / "ocr"
    cache.mkdir(parents=True, exist_ok=True)
    stats.pages = len(pages)

    images, keys, todo = {}, {}, []
    for n in sorted(pages):
        images[n] = render(doc.source, n)
        keys[n] = hashlib.sha256(images[n] + f"|{PROMPT_VERSION}|{resolution}".encode()).hexdigest()[:24]
        hit = cache / f"{keys[n]}.json"
        if hit.exists():
            _apply(pages[n], json.loads(hit.read_text(encoding="utf-8"))["text"])
            stats.cached += 1
        else:
            todo.append(n)
    if stats.cached:
        log(f"  {stats.cached} scanned page(s) already transcribed — from cache")

    pacer = Pacer(per_minute)
    batches = [todo[i:i + pages_per_call] for i in range(0, len(todo), pages_per_call)]
    for batch in batches:
        log(f"  transcribing page(s) {', '.join(map(str, batch))}…")
        texts = _call(batch, images, models, resolution, pacer, stats, log)
        missing = [n for n in batch if not texts.get(n, "").strip()]
        for n in missing if len(batch) > 1 else []:  # the model skipped a page marker: retry that page alone
            texts.update(_call([n], images, models, resolution, pacer, stats, log))
        for n in batch:
            text = texts.get(n, "").strip()
            if not text:
                stats.failed.append(n)
                continue
            (cache / f"{keys[n]}.json").write_text(json.dumps({"page": n, "text": text}, ensure_ascii=False),
                                                  encoding="utf-8")
            _apply(pages[n], text)
    return stats


def _call(batch, images, models, resolution, pacer, stats, log) -> dict[int, str]:
    parts: list[dict] = [{"text": PROMPT}]
    for n in batch:
        parts.append({"text": f"<<<PAGE {n}>>>"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(images[n]).decode()}})
    try:
        res = retrying([("gemini", m, None) for m in models],
                       lambda _p, m: gemini_text(m, parts, OUTPUT_TOKENS_PER_PAGE * len(batch) + 200,
                                                 RESOLUTIONS[resolution]), pacer, log)
    except ProviderError as e:
        log(f"    could not transcribe page(s) {batch}: {e}")
        return {}
    stats.calls += 1
    stats.input_tokens += res.input_tokens
    stats.output_tokens += res.output_tokens
    stats.image_tokens += res.raw.get("image_tokens", 0)
    return split_pages(res.items, batch)


def split_pages(text: str, batch: list[int]) -> dict[int, str]:
    marks = list(_MARKER.finditer(text))
    if not marks:
        return {batch[0]: text} if len(batch) == 1 else {}
    out: dict[int, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        n = int(m.group(1))
        if n in batch:
            out[n] = text[m.end():end].strip()
    return out


def _apply(page, text: str) -> None:
    page.lines = markdown_lines(text, page.number)
    page.raw_text = text
    page.scanned = False
    page.transcribed = True
