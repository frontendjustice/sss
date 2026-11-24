"""Where saved model results live. The command line keeps them in a folder; the web app passes
an object with the same two methods so they live in its database and survive restarts."""
from __future__ import annotations

import json
from pathlib import Path


class DirCache:
    def __init__(self, folder: str | Path):
        self.folder = Path(folder)

    def _path(self, key: str) -> Path:
        return self.folder / f"{key}.json"   # keys may contain one "/" (e.g. "ocr/abc123")

    def get(self, key: str) -> dict | None:
        p = self._path(key)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def set(self, key: str, value: dict) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def as_cache(cache) -> "DirCache":
    """Accept a folder path or any object with get(key) and set(key, value)."""
    return DirCache(cache) if isinstance(cache, (str, Path)) else cache
