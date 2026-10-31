"""Step 1b: read scanned and handwritten pages once, and keep the text.

1. Tesseract first (free, runs on the server, no tokens). It reads printed or
   typed scans very well (about 96% word confidence in tests) but returns
   little or nothing for handwriting. Its result is kept only when a page has
   enough words read with high confidence.
2. Everything else (handwriting, poor photos) goes to Gemini as small greyscale
   JPEGs, a few pages per call, at medium media resolution (about 530 image
   tokens a page). Each Gemini transcription is cached by a fingerprint of the
   page image, so a page is never paid for twice.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import shutil
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .extract import Document, Line, markdown_lines
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

# Tesseract's text is trusted only when the page reads like clean print.
TESS_DPI = 300
TESS_MIN_WORDS = 25
TESS_MIN_MEAN_CONF = 80.0
TESS_MIN_SHARE_CONFIDENT = 0.75   # share of words read at >= 80% confidence


@dataclass
class OcrStats:
    pages: int = 0
    cached: int = 0
    calls: int = 0
    image_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tesseract_pages: int = 0     # read free by Tesseract
    gemini_pages: int = 0        # read by Gemini this run (not counting cache)
    failed: list[int] | None = None


def render(pdf_path: str | Path, page_number: int, dpi: int = 150) -> bytes:
    import pymupdf
    with pymupdf.open(pdf_path) as pdf:
        pix = pdf[page_number - 1].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        return pix.tobytes("jpeg", jpg_quality=70)


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def tesseract_page(pdf_path: str | Path, page_number: int, page_height: float) -> tuple[list[Line], dict]:
    """Read one page with Tesseract. Returns lines (with sizes and positions in PDF points, so the
    normal cleaning can spot headers, footers and headings) and confidence figures."""
    import pymupdf
    with pymupdf.open(pdf_path) as pdf:
        png = pdf[page_number - 1].get_pixmap(dpi=TESS_DPI, colorspace=pymupdf.csGRAY).tobytes("png")
    out = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "3", "tsv"], input=png,
                         capture_output=True, timeout=120).stdout.decode("utf-8", "replace")
    words = [r for r in csv.DictReader(io.StringIO(out), delimiter="\t", quoting=csv.QUOTE_NONE)
             if r.get("level") == "5" and (r.get("text") or "").strip() and float(r.get("conf", -1)) >= 0]
    confs = [float(w["conf"]) for w in words]
    info = {"words": len(words), "mean_conf": statistics.mean(confs) if confs else 0.0,
            "share_confident": sum(c >= 80 for c in confs) / len(confs) if confs else 0.0}
    scale = 72.0 / TESS_DPI
    grouped: dict[tuple, list[dict]] = {}
    for w in words:
        grouped.setdefault((int(w["block_num"]), int(w["par_num"]), int(w["line_num"])), []).append(w)
    # Font size from average character width (steadier than box height, which jumps with
    # letters like g/p/y; bold headings are also wider). Sizes are relative to the page's body text.
    rows = []
    for ws in sorted(grouped.values(), key=lambda g: min(int(w["top"]) for w in g)):
        ws.sort(key=lambda w: int(w["left"]))
        chars = sum(len(w["text"]) for w in ws)
        width = sum(int(w["width"]) for w in ws)
        rows.append((ws, width / chars if chars else 0.0, chars))
    body = statistics.median(cw for _, cw, n in rows if n >= 20) if any(n >= 20 for _, _, n in rows) else 0.0
    lines: list[Line] = []
    for ws, cw, _ in rows:
        ratio = cw / body if body else 1.0
        size = 11.0 if ratio < 1.12 else round(11.0 * ratio, 0)  # small wobble = body text
        if ratio < 0.85:
            size = 9.0  # small print: footers, captions, footnotes
        top = min(int(w["top"]) for w in ws)
        bottom = max(int(w["top"]) + int(w["height"]) for w in ws)
        lines.append(Line(" ".join(w["text"] for w in ws), size, ratio >= 1.12, page_number,
                          top * scale, bottom * scale, page_height))
    return lines, info


def tesseract_ok(info: dict) -> bool:
    return (info["words"] >= TESS_MIN_WORDS and info["mean_conf"] >= TESS_MIN_MEAN_CONF
            and info["share_confident"] >= TESS_MIN_SHARE_CONFIDENT)


def transcribe(doc: Document, models: list[str] | None = None, resolution: str = "medium", pages_per_call: int = 4,
               cache_dir: str | Path = ".quizcache", per_minute: float = 8, log=print,
               use_tesseract: bool = True) -> OcrStats:
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

    if todo and use_tesseract:
        if tesseract_available():
            still = []
            for n in todo:
                lines, info = tesseract_page(doc.source, n, pages[n].height)
                if tesseract_ok(info):
                    pages[n].lines, pages[n].raw_text = lines, "\n".join(l.text for l in lines)
                    pages[n].scanned, pages[n].transcribed = False, True
                    stats.tesseract_pages += 1
                else:
                    still.append(n)
            if stats.tesseract_pages:
                log(f"  {stats.tesseract_pages} page(s) read by Tesseract (printed text, no tokens)")
            if still:
                log(f"  {len(still)} page(s) look handwritten or unclear to Tesseract — sending to Gemini")
            todo = still
        else:
            log("  Tesseract is not installed — all scanned pages go to Gemini")

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
            stats.gemini_pages += 1
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
