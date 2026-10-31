"""Step 6a: validate generated questions before anyone sees them.

Every rejection is counted by reason so the report shows what the model got
wrong. Nothing here judges medical accuracy: questions are marked
AI-generated and still need a human read before going into a shared bank.
"""
from __future__ import annotations

import re
from collections import Counter

_LETTER_REF = re.compile(r"(^|\s)(both|either|neither)?\s*[A-E]\s*(,|&|and|or)\s*[A-E](\s|$|\.)|\bboth of the above\b", re.I)
_MENTIONS_SOURCE = re.compile(r"\b(according to|in|from|based on) the (notes|text|passage|material|lecture)\b", re.I)
_ALL_NONE = re.compile(r"^\s*(all|none) of the (above|options)", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def check(items: list, valid_sections: set[str], options: int, seen: set[str] | None = None
          ) -> tuple[list[dict], Counter]:
    """Returns (accepted questions, Counter of rejection reasons). `seen` holds normalised
    stems already accepted (across calls) and is updated in place."""
    seen = seen if seen is not None else set()
    ok: list[dict] = []
    rejected: Counter = Counter()
    for it in items if isinstance(items, list) else []:
        reason = _problem(it, valid_sections, options, seen)
        if reason:
            rejected[reason] += 1
            continue
        seen.add(_norm(it["q"]))
        ok.append({"s": it["s"], "q": it["q"].strip(), "o": [o.strip() for o in it["o"]],
                   "a": it["a"], "e": it["e"].strip(), "t": str(it.get("t", "")).strip()})
    return ok, rejected


def _problem(it, valid_sections: set[str], options: int, seen: set[str]) -> str | None:
    if not isinstance(it, dict):
        return "not a question object"
    q, o, a, e = it.get("q"), it.get("o"), it.get("a"), it.get("e")
    if not isinstance(q, str) or len(q.strip()) < 12:
        return "missing or too-short question"
    if not isinstance(o, list) or len(o) != options or not all(isinstance(x, str) and x.strip() for x in o):
        return f"not exactly {options} options"
    if len({_norm(x) for x in o}) != len(o):
        return "duplicate options"
    if not isinstance(a, int) or isinstance(a, bool) or not 0 <= a < len(o):
        return "answer index out of range"
    if not isinstance(e, str) or not e.strip():
        return "missing explanation"
    if it.get("s") not in valid_sections:
        return "unknown section id"
    if any(_LETTER_REF.search(x) for x in o):
        return "option refers to other options"
    if _MENTIONS_SOURCE.search(q):
        return "question refers to 'the notes'"
    if _norm(q) in seen:
        return "duplicate question"
    return None


def all_none_share(questions: list[dict]) -> float:
    if not questions:
        return 0.0
    return sum(any(_ALL_NONE.match(x) for x in q["o"]) for q in questions) / len(questions)
