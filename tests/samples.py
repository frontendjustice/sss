"""Builds realistic test PDFs: running header/footer, contents page with dot
leaders, numbered headings at three sizes, words split across lines, bullets,
a figure caption, a references section and an image-only (scanned) page."""
from __future__ import annotations

from pathlib import Path

import pymupdf

BODY = (
    "Beta-adrenergic antagonists reduce heart rate and myocardial contractility by blocking "
    "beta-1 receptors in the heart. Non-selective agents such as propranolol also block beta-2 "
    "receptors in bronchial smooth muscle and can precipitate bronchospasm in asthmatic patients. "
    "Cardioselective agents such as atenolol, metoprolol and bisoprolol are preferred when a "
    "beta blocker is needed in a patient with mild reactive airway disease. Abrupt withdrawal "
    "after long-term use may cause rebound tachycardia and angina, so doses are tapered over one "
    "to two weeks. Common adverse effects include fatigue, cold extremities, bradycardia and "
    "masking of hypoglycaemia symptoms in diabetic patients."
)
ACE = (
    "Angiotensin converting enzyme inhibitors such as lisinopril, enalapril and ramipril block "
    "the conversion of angiotensin I to angiotensin II, reducing vasoconstriction and aldosterone "
    "secretion. They also prevent the breakdown of bradykinin, which explains the dry persistent "
    "cough seen in up to fifteen percent of patients. Hyperkalaemia and a rise in serum creatinine "
    "are expected early effects that require monitoring. They are contraindicated in pregnancy "
    "because of fetal renal toxicity and in patients with a history of angioedema."
)


def _variants(text: str, n: int) -> list[str]:
    """n different paragraphs on the same topic (sentences rotated), so they are not duplicates."""
    sents = [s.strip() + "." for s in text.split(". ") if s.strip()]
    sents[-1] = sents[-1].rstrip(".") + "."
    return [" ".join(sents[i:] + sents[:i]).replace("..", ".") for i in range(n)]


def _wrap(text: str, width: int = 88, hyphenate: bool = True) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        if len(cur) + len(word) + 1 > width:
            if hyphenate and len(word) > 9 and width - len(cur) > 6:
                cut = width - len(cur) - 2
                lines.append((cur + " " + word[:cut] + "-").strip())
                cur = word[cut:]
                continue
            lines.append(cur)
            cur = word
        else:
            cur = (cur + " " + word).strip()
    if cur:
        lines.append(cur)
    return lines


class _Writer:
    def __init__(self, doc: pymupdf.Document, total_pages_hint: int):
        self.doc = doc
        self.total = total_pages_hint
        self.page = None
        self.y = 0.0
        self.new_page()

    def new_page(self):
        self.page = self.doc.new_page(width=595, height=842)
        n = len(self.doc)
        self.page.insert_text((72, 40), "PHA 301: Pharmacology II - Department of Pharmacology", fontsize=9)
        self.page.insert_text((270, 815), f"Page {n} of {self.total}", fontsize=9)
        self.y = 90.0

    def line(self, text: str, size: float = 11, bold: bool = False, gap: float = 0.0):
        if self.y + size * 1.6 + gap > 780:
            self.new_page()
        self.y += gap
        self.page.insert_text((72, self.y), text, fontsize=size, fontname="hebo" if bold else "helv")
        self.y += size * 1.35

    def para(self, text: str, hyphenate: bool = True):
        for l in _wrap(text, hyphenate=hyphenate):
            self.line(l)
        self.y += 8


def build_notes_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    w = _Writer(doc, total_pages_hint=6)
    w.line("Contents", 16, True)
    for title, pg in [("1 Cardiovascular Pharmacology", 2), ("1.1 Beta blockers", 2),
                      ("1.2 ACE inhibitors", 3), ("References", 5)]:
        w.line(f"{title} " + "." * 40 + f" {pg}")
    w.new_page()
    w.line("Learning objectives", 14, True, gap=6)
    w.para("By the end of this lecture you should be able to list the classes of antihypertensive drugs.")
    w.line("1 Cardiovascular Pharmacology", 18, True, gap=6)
    w.para("This chapter covers drugs acting on the cardiovascular system that are commonly used in "
           "Nigerian hospital and community practice, with emphasis on safe selection and monitoring.")
    w.line("1.1 Beta blockers", 14, True, gap=6)
    for text in _variants(BODY, 3):
        w.para(text)
    w.line("Figure 1.1: Beta receptor locations", 9)
    w.line("Key points", 11, True, gap=4)
    for b in ["Taper doses before stopping", "Avoid non-selective agents in asthma",
              "Monitor heart rate and blood pressure"]:
        w.line("• " + b)
    w.y += 8
    w.line("1.2 ACE inhibitors", 14, True, gap=6)
    for text in _variants(ACE, 3):
        w.para(text)
    w.para(BODY)  # exact repeat of the first beta blocker paragraph: should be removed
    w.line("References", 14, True, gap=6)
    w.para("Katzung BG. Basic and Clinical Pharmacology. 15th ed. McGraw Hill; 2021.", hyphenate=False)
    w.para("Rang HP, Dale MM. Pharmacology. 9th ed. Elsevier; 2019.", hyphenate=False)
    # an image-only page, like a scanned handwritten note
    scan = doc.new_page(width=595, height=842)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60), False)
    pix.set_rect(pix.irect, (200, 200, 200))
    scan.insert_image(pymupdf.Rect(72, 72, 500, 500), pixmap=pix)
    doc.save(path)
    return path


def build_markdown(path: Path) -> Path:
    path.write_text(
        "# Pharmaceutics\n\n## Emulsions\n\n" + ACE.replace("Angiotensin", "Emulsions") + "\n\n"
        + BODY + "\n\n## References\n\nAulton ME. Pharmaceutics. 2017.\n", encoding="utf-8")
    return path
