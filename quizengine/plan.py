"""Step 4: decide how many questions each section gets and group sections into
as few model calls as possible.

Questions are shared out in proportion to each section's length, with a cap so
thin sections are not padded with invented questions. Sections are then packed
into calls under an input-token budget and a questions-per-call limit, so one
fixed instruction is spread across many questions.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .split import Section

WORDS_PER_QUESTION = 60       # at most one question per ~60 words of source text
MAX_PER_SECTION = 25
INSTRUCTION_TOKENS = 700      # fixed instruction + 2-3 style examples, sent once per call
OUTPUT_TOKENS_PER_QUESTION = 110


@dataclass
class Call:
    section_ids: list[str]
    questions: int
    input_tokens: int
    output_tokens: int


@dataclass
class Plan:
    requested: int
    planned: int
    sections: list[Section]          # only sections that get questions, in document order
    calls: list[Call]
    warnings: list[str] = field(default_factory=list)

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    @property
    def source_tokens(self) -> int:
        return sum(s.tokens for s in self.sections)


def capacity(s: Section) -> int:
    return min(MAX_PER_SECTION, s.words // WORDS_PER_QUESTION)


def allocate(sections: list[Section], n: int) -> int:
    """Share n questions across sections by length (largest-remainder method). Returns the number placed."""
    for s in sections:
        s.questions = 0
    room = {s.id: capacity(s) for s in sections}
    remaining = min(n, sum(room.values()))
    placed = 0
    while remaining > 0:
        open_ = [s for s in sections if s.questions < room[s.id]]
        if not open_:
            break
        weight = sum(s.words for s in open_)
        shares = [(remaining * s.words / weight, s) for s in open_]
        step = 0
        for share, s in shares:
            take = min(int(share), room[s.id] - s.questions)
            s.questions += take
            step += take
        left = remaining - step
        # hand out what is left by largest fractional share
        for share, s in sorted(shares, key=lambda x: x[0] - int(x[0]), reverse=True):
            if left <= 0:
                break
            if s.questions < room[s.id]:
                s.questions += 1
                left -= 1
                step += 1
        if step == 0:
            break
        placed += step
        remaining -= step
    return placed


def make_plan(sections: list[Section], n: int, max_input_tokens: int = 6000,
              max_per_call: int = 10) -> Plan:
    placed = allocate(sections, n)
    used = [s for s in sections if s.questions > 0]
    warnings: list[str] = []
    if placed < n:
        warnings.append(f"Only enough material for about {placed} good questions; asked for {n}.")

    calls: list[Call] = []
    batch: list[Section] = []

    def close():
        if batch:
            q = sum(s.questions for s in batch)
            tokens = INSTRUCTION_TOKENS + sum(s.tokens + 15 for s in batch)  # +15 for each section's label
            calls.append(Call([s.id for s in batch], q, tokens, q * OUTPUT_TOKENS_PER_QUESTION))
            batch.clear()

    for s in used:
        size = sum(x.tokens for x in batch) + INSTRUCTION_TOKENS
        q = sum(x.questions for x in batch)
        if batch and (size + s.tokens > max_input_tokens or q + s.questions > max_per_call):
            close()
        batch.append(s)  # a section that alone exceeds a limit still goes whole, in its own call
    close()
    return Plan(n, placed, used, calls, warnings)
