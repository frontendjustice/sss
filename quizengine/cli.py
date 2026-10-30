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
    p.add_argument("-o", "--out", help="Where to write the plan JSON (default: <file>.plan.json).")
    p.add_argument("--show-text", action="store_true", help="Print the cleaned text that would be sent.")

    sub.add_parser("profiles", help="List exam profiles.")
    args = ap.parse_args(argv)

    if args.command == "profiles":
        for key, path in profiles.available().items():
            prof = profiles.load(key)
            print(f"{key:12} {prof.name}")
        return 0

    try:
        prof = profiles.load(args.profile, args.profiles_dir).with_params(
            level=args.level, programme=args.programme, institution=args.institution)
        prepared = prepare(args.file, args.questions, prof, args.course,
                           exclude=None if args.keep_all else DEFAULT_EXCLUDE, extra_exclude=args.exclude,
                           max_input_tokens=args.max_input_tokens, max_per_call=args.max_per_call)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(report(prepared))
    if args.show_text:
        for s in prepared.plan.sections:
            print(f"\n=== {s.title}  ({s.questions} questions) ===\n{s.text}")
    out = Path(args.out) if args.out else Path(args.file).with_suffix(".plan.json")
    out.write_text(json.dumps(prepared.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nPlan written to {out}")
    return 0


def report(pr: Prepared) -> str:
    d = pr.to_dict(include_text=False)
    t = d["tokens"]
    lines = [f"File: {Path(pr.source).name}  ({d['pages']} page{'s' if d['pages'] != 1 else ''})"]
    if d["scanned_pages"]:
        pages = ", ".join(map(str, d["scanned_pages"]))
        lines.append(f"  Scanned pages with no text (need image reading): {pages}")
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
