"""Token estimates. Every model counts tokens a little differently, so these are
planning figures (about 4 characters per token for English), not billing figures.
The generation step reports the model's own counts after each call."""
from __future__ import annotations

CHARS_PER_TOKEN = 4.0


def estimate(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN)) if text else 0
