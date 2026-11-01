"""Step 5 + 6: send each planned call to the model, check what comes back,
and cache it so the same sections are never paid for twice."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import prompt
from .check import all_none_share, check
from .pipeline import Prepared
from .providers import Pacer, ProviderError, call_with_fallback

OUTPUT_TOKENS_PER_QUESTION = 200   # ceiling per question for maxOutputTokens (actual use is lower)


@dataclass
class CallUsage:
    call: int
    model: str
    cached: bool
    asked: int
    accepted: int
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    error: str = ""


@dataclass
class Generated:
    questions: list[dict]
    calls: list[CallUsage]
    rejected: Counter = field(default_factory=Counter)

    @property
    def tokens(self) -> dict:
        live = [c for c in self.calls if not c.cached]
        return {"input": sum(c.input_tokens for c in live), "output": sum(c.output_tokens for c in live),
                "thinking": sum(c.thinking_tokens for c in live)}


def generate(pr: Prepared, providers: list, difficulty: str = "mixed", cache_dir: str | Path = ".quizcache",
             per_minute: float = 8, use_cache: bool = True, log=print, fact_check: bool = True) -> Generated:
    system = prompt.instruction(pr.profile, pr.course, difficulty, fact_check)
    schema = prompt.response_schema(fact_check)
    by_id = {s.id: s for s in pr.plan.sections}
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    pacer = Pacer(per_minute)
    seen: set[str] = set()
    out = Generated([], [])

    for n, call in enumerate(pr.plan.calls, start=1):
        sections = [by_id[i] for i in call.section_ids]
        labels = {s.id: f"S{k}" for k, s in enumerate(sections, start=1)}
        user = prompt.message(sections, labels)
        key = hashlib.sha256(json.dumps([prompt.PROMPT_VERSION, system, user]).encode()).hexdigest()[:24]
        path = cache / f"{key}.json"
        usage = CallUsage(n, "", False, call.questions, 0)

        if use_cache and path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            items, usage.model, usage.cached = saved["items"], saved["model"], True
            log(f"  call {n}/{len(pr.plan.calls)}: {call.questions} questions — from cache")
        else:
            log(f"  call {n}/{len(pr.plan.calls)}: {call.questions} questions from {len(sections)} section(s)…")
            try:
                res = call_with_fallback(providers, system, user, schema,
                                         call.questions * OUTPUT_TOKENS_PER_QUESTION + 256, pacer, log=log)
            except ProviderError as e:
                usage.error = str(e)
                out.calls.append(usage)
                log(f"    gave up on this call: {e}")
                continue
            items = res.items
            usage.model = res.model
            usage.input_tokens, usage.output_tokens, usage.thinking_tokens = (
                res.input_tokens, res.output_tokens, res.thinking_tokens)
            path.write_text(json.dumps({"model": res.model, "items": items}, ensure_ascii=False), encoding="utf-8")

        good, bad = check(items, set(labels.values()), pr.profile.options, seen)
        out.rejected.update(bad)
        back = {v: k for k, v in labels.items()}
        for q in good:
            s = by_id[back[q["s"]]]
            out.questions.append({
                "course": pr.course, "q": q["q"], "o": q["o"], "a": q["a"], "e": q["e"], "type": q["t"],
                "source": {"file": Path(pr.source).name, "section": s.title, "pages": [s.page_start, s.page_end]},
                "flag": _meaningful(q["f"]),
                "ai": {"model": usage.model, "difficulty": difficulty, "profile": pr.profile.key},
            })
        usage.accepted = len(good)
        out.calls.append(usage)
    return out


def _meaningful(note: str) -> str | None:
    """Models sometimes fill the fact-check field with 'none'/'consistent' instead of leaving it empty."""
    n = note.strip().strip(".").lower()
    if not n or n in {"none", "n/a", "na", "no conflict", "consistent", "correct", "no", "ok"} or n.startswith(
            ("no conflict", "consistent with standard", "this is consistent", "the answer is consistent")):
        return None
    return note.strip()


def report(gen: Generated, requested: int) -> str:
    t = gen.tokens
    lines = [f"\nAccepted {len(gen.questions)} of {requested} questions."]
    if gen.rejected:
        lines.append("Rejected by checks: " + ", ".join(f"{k} {v}" for k, v in gen.rejected.most_common()))
    flagged = [q for q in gen.questions if q.get("flag")]
    if flagged:
        lines.append(f"Fact-check: {len(flagged)} question(s) flagged for review (answer may conflict with standard references).")
    share = all_none_share(gen.questions)
    if share > 0.1:
        lines.append(f"Note: {share:.0%} of questions use All/None of the above.")
    for c in gen.calls:
        state = "cache" if c.cached else (f"error: {c.error.splitlines()[0]}" if c.error else
                                          f"{c.input_tokens:,} in / {c.output_tokens:,} out"
                                          + (f" / {c.thinking_tokens:,} thinking" if c.thinking_tokens else ""))
        lines.append(f"  call {c.call}: {c.accepted}/{c.asked} accepted  [{c.model or '-'}]  {state}")
    lines.append(f"Tokens used this run (model's own count): {t['input']:,} in, {t['output']:,} out"
                 + (f", {t['thinking']:,} thinking" if t["thinking"] else ""))
    return "\n".join(lines)
