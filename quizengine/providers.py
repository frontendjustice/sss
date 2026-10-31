"""AI providers behind one small interface, so swapping or adding a model is a
setting, not a rewrite.

Keys: in this cloud environment the network layer adds each key to requests
for its own host, so no key is read here. When running elsewhere, set
GEMINI_API_KEY / GROQ_API_KEY and they are sent as headers.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class ProviderError(Exception):
    def __init__(self, message: str, retry_after: float | None = None, fatal: bool = False):
        super().__init__(message)
        self.retry_after = retry_after  # seconds the provider asked us to wait (rate limits)
        self.fatal = fatal              # bad request/auth: trying again will not help


@dataclass
class Result:
    items: object                    # parsed JSON (expected: list of question dicts)
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    raw: dict = field(default_factory=dict)


def _post(url: str, body: dict, headers: dict, timeout: float = 180) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        retry = _retry_delay(e.headers.get("Retry-After"), text)
        transient = e.code in (408, 429, 500, 502, 503, 504)
        raise ProviderError(f"HTTP {e.code}: {_short(text)}", retry, fatal=not transient) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ProviderError(f"network: {e}") from None


def _retry_delay(header: str | None, body: str) -> float | None:
    if header and header.replace(".", "", 1).isdigit():
        return float(header)
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body)
    return float(m.group(1)) if m else None


def _short(text: str) -> str:
    try:
        return json.loads(text)["error"]["message"][:200]
    except Exception:
        return text[:200]


def _parse_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ProviderError(f"model returned invalid JSON ({e.msg})") from None


class Gemini:
    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, models: list[str] | None = None, thinking_budget: int = 0):
        self.models = models or ["gemini-3.5-flash", "gemini-flash-latest", "gemini-2.5-flash"]
        self.thinking_budget = thinking_budget

    def generate(self, model: str, system: str, user: str, schema: dict, max_output: int) -> Result:
        headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]} if os.environ.get("GEMINI_API_KEY") else {}
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0.7,
                "maxOutputTokens": max_output,
                "thinkingConfig": {"thinkingBudget": self.thinking_budget},
            },
        }
        data = _post(f"{self.BASE}/{model}:generateContent", body, headers)
        cands = data.get("candidates") or []
        if not cands or not cands[0].get("content", {}).get("parts"):
            reason = cands[0].get("finishReason") if cands else data.get("promptFeedback", {}).get("blockReason")
            raise ProviderError(f"empty response ({reason})")
        text = "".join(p.get("text", "") for p in cands[0]["content"]["parts"] if not p.get("thought"))
        if cands[0].get("finishReason") == "MAX_TOKENS":
            raise ProviderError("answer cut off at the output limit")
        u = data.get("usageMetadata", {})
        return Result(_parse_json(text), data.get("modelVersion", model), u.get("promptTokenCount", 0),
                      u.get("candidatesTokenCount", 0), u.get("thoughtsTokenCount", 0), data)


class Groq:
    """OpenAI-compatible chat API. JSON mode needs an object, so the array is wrapped in {"questions": [...]}."""
    name = "groq"
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, models: list[str] | None = None):
        self.models = models or ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"]

    def generate(self, model: str, system: str, user: str, schema: dict, max_output: int) -> Result:
        headers = {"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"} if os.environ.get("GROQ_API_KEY") else {}
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system + '\nWrap the array as {"questions": [...]}.'},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0.7,
            "max_tokens": max_output,
        }
        data = _post(self.URL, body, headers)
        text = data["choices"][0]["message"]["content"]
        parsed = _parse_json(text)
        items = parsed.get("questions", parsed) if isinstance(parsed, dict) else parsed
        u = data.get("usage", {})
        return Result(items, data.get("model", model), u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


class Pacer:
    """Keeps calls under a requests-per-minute limit (free tiers are counted per minute)."""

    def __init__(self, per_minute: float):
        self.gap = 60.0 / per_minute if per_minute > 0 else 0.0
        self.last = 0.0

    def wait(self):
        delay = self.last + self.gap - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.last = time.monotonic()


def call_with_fallback(providers: list, system: str, user: str, schema: dict, max_output: int,
                       pacer: Pacer, log=print, attempts: int = 3) -> Result:
    """Try each provider's models in order. Rate limits and overloads are retried with backoff;
    bad requests move straight on to the next model."""
    errors = []
    for provider in providers:
        for model in provider.models:
            for attempt in range(attempts):
                pacer.wait()
                try:
                    return provider.generate(model, system, user, schema, max_output)
                except ProviderError as e:
                    errors.append(f"{provider.name}/{model}: {e}")
                    log(f"    {provider.name}/{model} failed: {e}")
                    if e.fatal:
                        break
                    if attempt < attempts - 1:
                        wait = min(e.retry_after or 5 * 2 ** attempt, 90)
                        log(f"    retrying in {wait:.0f}s")
                        time.sleep(wait)
    raise ProviderError("all models failed:\n  " + "\n  ".join(errors[-6:]))
