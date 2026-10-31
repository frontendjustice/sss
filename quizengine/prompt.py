"""Builds the instruction and the per-call message for question generation.

The instruction is fixed for a given profile/course/difficulty, so every call
in a run repeats the same short text (and providers that cache repeated
prefixes can reuse it). The message carries only the selected sections, each
with its question quota.
"""
from __future__ import annotations

from .profiles import Profile
from .split import Section

PROMPT_VERSION = "1"  # bump when wording changes, so cached results are not reused

DIFFICULTY = {
    "easy": "mostly direct recall of key facts",
    "mixed": "a mix of recall, understanding and application (some short cases)",
    "hard": "mostly application: clinical or practical scenarios, comparisons and calculations",
}


def instruction(profile: Profile, course: str | None, difficulty: str = "mixed") -> str:
    if difficulty not in DIFFICULTY:
        raise ValueError(f"difficulty must be one of: {', '.join(DIFFICULTY)}")
    n = profile.options
    lines = [
        f"You write multiple-choice exam questions for {profile.audience_text}.",
        f"Standard: {profile.render(profile.standard)}." + (f" Course: {course}." if course else ""),
        "Rules:",
        "- Use ONLY facts stated in the NOTES. If a section cannot support its quota without guessing, write fewer.",
        f"- Mix these types where the notes allow: {', '.join(profile.question_types)}."
        " Calculations only where the notes give the numbers.",
        f"- Exactly {n} options, exactly one correct. Wrong options must be plausible, from the same category"
        " and similar in length to the correct one.",
        "- Never use options that refer to other options (e.g. \"A and B\", \"Both of the above\")."
        " Use \"All/None of the above\" in at most 1 of every 10 questions.",
        "- Each question must stand alone: never mention \"the notes\", \"the text\" or \"the passage\".",
        "- Explanation: 1-2 sentences giving the key fact that makes the answer correct.",
        f"- Difficulty: {DIFFICULTY[difficulty]}.",
        "Output a JSON array. Each item: s = section id, q = question, o = options,"
        " a = 0-based index of the correct option, e = explanation, t = question type.",
    ]
    examples = getattr(profile, "examples", None) or []
    if examples:
        import json
        lines.append("Example item: " + json.dumps(examples[0], ensure_ascii=False))
    return "\n".join(lines)


def message(sections: list[Section], labels: dict[str, str]) -> str:
    quota = "\n".join(f"{labels[s.id]} ({s.questions}): {s.title}" for s in sections)
    notes = "\n\n".join(f"[{labels[s.id]}] {s.title}\n{s.text}" for s in sections)
    total = sum(s.questions for s in sections)
    return f"Write {total} questions, per section:\n{quota}\n\nNOTES\n{notes}"


RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "s": {"type": "STRING"},
            "q": {"type": "STRING"},
            "o": {"type": "ARRAY", "items": {"type": "STRING"}},
            "a": {"type": "INTEGER"},
            "e": {"type": "STRING"},
            "t": {"type": "STRING"},
        },
        "required": ["s", "q", "o", "a", "e", "t"],
        "propertyOrdering": ["s", "q", "o", "a", "e", "t"],
    },
}
