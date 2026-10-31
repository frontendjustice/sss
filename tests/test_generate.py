"""Generation tests use a fake provider: no network, no quota used."""
import pytest

from quizengine import load_profile, prepare
from quizengine.check import check
from quizengine.generate import generate
from quizengine.prompt import instruction
from quizengine.providers import ProviderError, Result

from samples import build_notes_pdf


def q(s="S1", stem="Which receptor do cardioselective beta blockers target?", opts=None, a=0, e="Beta-1."):
    return {"s": s, "q": stem, "o": opts or ["Beta-1", "Beta-2", "Alpha-1", "Muscarinic M2"], "a": a, "e": e, "t": "sba"}


@pytest.mark.parametrize("item,reason", [
    (q(opts=["A", "B", "C"]), "not exactly 4 options"),
    (q(opts=["Beta-1", "beta 1", "Alpha-1", "M2"]), "duplicate options"),
    (q(a=4), "answer index out of range"),
    (q(e=""), "missing explanation"),
    (q(s="S9"), "unknown section id"),
    (q(opts=["Beta-1", "Beta-2", "Both A and B", "Neither"]), "option refers to other options"),
    (q(stem="According to the notes, which receptor is blocked?"), "question refers to 'the notes'"),
    ("not a dict", "not a question object"),
])
def test_check_rejects(item, reason):
    ok, bad = check([item], {"S1"}, 4)
    assert not ok and bad[reason] == 1


def test_check_accepts_and_dedupes():
    ok, bad = check([q(), q()], {"S1"}, 4)
    assert len(ok) == 1 and bad["duplicate question"] == 1


class FakeProvider:
    name = "fake"

    def __init__(self, fail_first=0, models=("m1",)):
        self.models = list(models)
        self.calls = 0
        self.fail_first = fail_first

    def generate(self, model, system, user, schema, max_output):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise ProviderError("HTTP 503: overloaded", retry_after=0)
        n = int(user.split()[1])  # "Write N questions, ..."
        sections = [line.split()[0] for line in user.splitlines()[1:] if line.startswith("S") and "(" in line]
        items = [q(s=sections[i % len(sections)], stem=f"Question number {i} about beta blockers?") for i in range(n)]
        return Result(items, model, input_tokens=100, output_tokens=50)


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    pdf = build_notes_pdf(tmp_path_factory.mktemp("g") / "notes.pdf")
    return prepare(pdf, 6, load_profile("pep"), "PCOL")


def test_generate_accepts_and_caches(prepared, tmp_path):
    fake = FakeProvider()
    gen = generate(prepared, [fake], cache_dir=tmp_path, per_minute=0, log=lambda *a: None)
    assert len(gen.questions) == 6 and fake.calls == 1
    first = gen.questions[0]
    assert first["course"] == "Pharmacology" and first["source"]["section"].endswith("Beta blockers")
    assert gen.tokens == {"input": 100, "output": 50, "thinking": 0}
    again = generate(prepared, [fake], cache_dir=tmp_path, per_minute=0, log=lambda *a: None)
    assert fake.calls == 1 and len(again.questions) == 6 and again.tokens["input"] == 0


def test_generate_retries_then_succeeds(prepared, tmp_path, monkeypatch):
    monkeypatch.setattr("quizengine.providers.time.sleep", lambda s: None)
    fake = FakeProvider(fail_first=2)
    gen = generate(prepared, [fake], cache_dir=tmp_path, per_minute=0, log=lambda *a: None)
    assert len(gen.questions) == 6 and fake.calls == 3


def test_generate_reports_failure_without_crashing(prepared, tmp_path, monkeypatch):
    monkeypatch.setattr("quizengine.providers.time.sleep", lambda s: None)
    fake = FakeProvider(fail_first=99)
    gen = generate(prepared, [fake], cache_dir=tmp_path, per_minute=0, log=lambda *a: None)
    assert gen.questions == [] and gen.calls[0].error


def test_instruction_uses_profile():
    prof = load_profile("undergrad").with_params(level="200L", programme="Pharmacy", institution="UNILAG")
    text = instruction(prof, "PCH 201", "hard")
    assert "200L Pharmacy students at UNILAG" in text and "Course: PCH 201" in text and "application" in text
    with pytest.raises(ValueError):
        instruction(prof, None, "impossible")
