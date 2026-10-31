"""Command line: python -m quizengine prepare notes.pdf --questions 20 --profile pep --course PCOL"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import profiles
from .pipeline import Prepared, prepare
from .split import DEFAULT_EXCLUDE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quizengine", description="Prepare course materials for quiz generation.")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="Extract, clean, split and plan questions for a PDF or notes file.")
    _common(p)
    p.add_argument("-o", "--out", help="Where to write the plan JSON (default: <file>.plan.json).")
    p.add_argument("--show-text", action="store_true", help="Print the cleaned text that would be sent.")

    g = sub.add_parser("generate", help="Prepare a file, then generate and check questions with the AI model.")
    _common(g)
    g.add_argument("--difficulty", choices=["easy", "mixed", "hard"], default="mixed")
    g.add_argument("--providers", default="gemini,groq", help="Order to try providers (comma separated).")
    g.add_argument("--model", action="append", help="Gemini model(s) to try first (repeatable).")
    g.add_argument("--think", type=int, default=0, help="Gemini thinking budget in tokens (0 = off, cheapest).")
    g.add_argument("--rpm", type=float, default=8, help="Max model calls per minute (free tiers are ~10).")
    g.add_argument("--no-cache", action="store_true", help="Ignore cached results and call the model again.")
    g.add_argument("--cache-dir", default=".quizcache")
    g.add_argument("--dry-run", action="store_true", help="Show the instruction and first message; call nothing.")
    g.add_argument("-o", "--out", help="Where to write questions (default: <file>.questions.json).")

    sub.add_parser("profiles", help="List exam profiles.")
    args = ap.parse_args(argv)

    if args.command == "profiles":
        for key in profiles.available():
            print(f"{key:12} {profiles.load(key).name}")
        return 0

    try:
        prepared = _prepare(args)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(report(prepared))

    if args.command == "generate":
        return _generate(args, prepared)

    if args.show_text:
        for s in prepared.plan.sections:
            print(f"\n=== {s.title}  ({s.questions} questions) ===\n{s.text}")
    out = Path(args.out) if args.out else Path(args.file).with_suffix(".plan.json")
    out.write_text(json.dumps(prepared.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nPlan written to {out}")
    return 0


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("file")
    p.add_argument("-n", "--questions", type=int, default=20)
    p.add_argument("--profile", default="pep", help="Exam profile: pep, undergrad, or your own JSON profile.")
    p.add_argument("--profiles-dir", help="Folder with extra profile JSON files.")
    p.add_argument("--course", help="Course code (PEP, e.g. PCOL) or course name (undergrad).")
    p.add_argument("--level", help="Undergraduate level, e.g. 300L.")
    p.add_argument("--programme", help="Undergraduate programme, e.g. Pharmacy.")
    p.add_argument("--institution", help="School name, for undergraduate profiles.")
    p.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                   help="Extra heading pattern to skip (repeatable), e.g. --exclude 'introduction'.")
    p.add_argument("--keep-all", action="store_true", help="Do not skip references, contents, objectives, etc.")
    p.add_argument("--max-input-tokens", type=int, default=6000, help="Input budget per model call.")
    p.add_argument("--max-per-call", type=int, default=10, help="Questions per model call.")
    p.add_argument("--ocr", choices=["auto", "off"], default=None,
                   help="Read scanned/handwritten pages with Gemini (default: on for generate, off for prepare).")
    p.add_argument("--no-tesseract", action="store_true",
                   help="Skip the free Tesseract pass and send every scanned page to Gemini.")
    p.add_argument("--ocr-resolution", choices=["low", "medium", "high"], default="medium",
                   help="Image detail for scanned pages: medium ≈ 530 tokens/page (default, read handwriting reliably in tests), low ≈ 270.")


def _prepare(args) -> Prepared:
    prof = profiles.load(args.profile, args.profiles_dir).with_params(
        level=args.level, programme=args.programme, institution=args.institution)
    ocr_on = args.ocr == "auto" or (args.ocr is None and args.command == "generate")
    transcriber = None
    if ocr_on:
        from .ocr import transcribe
        cache_dir = getattr(args, "cache_dir", ".quizcache")
        transcriber = lambda doc: transcribe(doc, resolution=args.ocr_resolution, cache_dir=cache_dir,
                                             use_tesseract=not args.no_tesseract)
    return prepare(args.file, args.questions, prof, args.course,
                   exclude=None if args.keep_all else DEFAULT_EXCLUDE, extra_exclude=args.exclude,
                   max_input_tokens=args.max_input_tokens, max_per_call=args.max_per_call, transcriber=transcriber)


def _generate(args, prepared: Prepared) -> int:
    from . import prompt
    from .generate import generate, report as gen_report
    from .providers import Gemini, Groq

    if not prepared.plan.calls:
        print("Nothing to generate.")
        return 1
    if args.dry_run:
        sections = [s for s in prepared.plan.sections if s.id in prepared.plan.calls[0].section_ids]
        labels = {s.id: f"S{k}" for k, s in enumerate(sections, start=1)}
        print("\n--- instruction ---\n" + prompt.instruction(prepared.profile, prepared.course, args.difficulty))
        print("\n--- first message ---\n" + prompt.message(sections, labels))
        return 0
    available = {"gemini": lambda: Gemini(args.model, args.think), "groq": lambda: Groq()}
    providers = [available[name.strip()]() for name in args.providers.split(",") if name.strip() in available]
    print(f"\nGenerating ({args.difficulty})…")
    gen = generate(prepared, providers, args.difficulty, args.cache_dir, args.rpm, not args.no_cache)
    print(gen_report(gen, prepared.plan.planned))
    out = Path(args.out) if args.out else Path(args.file).with_suffix(".questions.json")
    out.write_text(json.dumps({"source": Path(prepared.source).name, "profile": prepared.profile.key,
                               "course": prepared.course, "difficulty": args.difficulty,
                               "questions": gen.questions}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Questions written to {out}")
    return 0 if gen.questions else 1


def report(pr: Prepared) -> str:
    d = pr.to_dict(include_text=False)
    t = d["tokens"]
    lines = [f"File: {Path(pr.source).name}  ({d['pages']} page{'s' if d['pages'] != 1 else ''})"]
    if d["ocr"]:
        o = d["ocr"]
        lines.append(f"  Read {o['pages']} scanned page(s): {o['tesseract_pages']} by Tesseract (free),"
                     f" {o['gemini_pages']} by Gemini, {o['cached']} from cache")
        if o["calls"]:
            lines.append(f"    Gemini: {o['calls']} call(s), {o['image_tokens']:,} image tokens,"
                         f" {o['input_tokens']:,} in / {o['output_tokens']:,} out")
        if o["failed"]:
            lines.append(f"  ! Could not read page(s): {', '.join(map(str, o['failed']))}")
    if d["scanned_pages"]:
        pages = ", ".join(map(str, d["scanned_pages"]))
        lines.append(f"  Scanned pages with no text (add --ocr auto to read them): {pages}")
    lines.append(f"Profile: {pr.profile.name} — {pr.profile.audience_text}"
                 + (f" — course: {pr.course}" if pr.course else ""))
    saved = (1 - t["cleaned"] / t["raw"]) * 100 if t["raw"] else 0
    lines.append(f"Tokens (estimated): raw {t['raw']:,} → cleaned {t['cleaned']:,} ({saved:.0f}% less)"
                 f" → source sent {t['sent_source']:,}")
    if d["removed"]:
        lines.append("Removed: " + ", ".join(f"{k} {v}" for k, v in sorted(d["removed"].items())))
    if d["skipped_sections"]:
        lines.append("Skipped sections: " + ", ".join(d["skipped_sections"]))
    lines.append(f"\nQuestions: {d['questions']['planned']} planned of {d['questions']['requested']} requested")
    for w in d["warnings"]:
        lines.append(f"  ! {w}")
    lines.append(f"\n  {'Q':>3}  {'Words':>6}  {'Tokens':>6}  {'Pages':>7}  Section")
    for s in d["sections"]:
        pages = f"{s['pages'][0]}" if s["pages"][0] == s["pages"][1] else f"{s['pages'][0]}-{s['pages'][1]}"
        lines.append(f"  {s['questions']:>3}  {s['words']:>6}  {s['tokens']:>6}  {pages:>7}  {s['title']}")
    lines.append(f"\nModel calls: {len(d['calls'])}  (est. input {t['est_input_total']:,} tokens incl. instructions,"
                 f" est. output {t['est_output_total']:,} tokens)")
    for i, c in enumerate(d["calls"], 1):
        lines.append(f"  call {i}: {c['questions']} questions from {len(c['section_ids'])} section(s),"
                     f" ~{c['input_tokens']:,} in / ~{c['output_tokens']:,} out")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
