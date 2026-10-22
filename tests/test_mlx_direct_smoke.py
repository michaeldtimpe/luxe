"""Live-model smoke test for MLXDirectBackend.

Marked `live_model` — skipped by default. Run manually after the oMLX server
is freed up:

    pytest tests/test_mlx_direct_smoke.py -m live_model -v

Memory: loads the 35B 6-bit weights into this process (~25 GB). Do not run
concurrently with an active oMLX server holding the same model.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live_model


@pytest.fixture(scope="module")
def backend():
    from benchmarks._eval_common.mlx_direct import MLXDirectBackend
    return MLXDirectBackend()


def test_first_token_top_logprobs(backend):
    top = backend.first_token_top_logprobs("The capital of France is", top_k=10)
    assert len(top) == 10
    # Descending logprob
    for a, b in zip(top, top[1:]):
        assert a.logprob >= b.logprob
    # " Paris" should be very likely as the next token after the prompt
    paris_in_top = any("Paris" in t.token for t in top)
    assert paris_in_top, f"'Paris' not in top-10: {[t.token for t in top]}"


def test_score_choices_picks_obvious(backend):
    # Construct an MMLU-style prompt where the answer is unambiguously B.
    prompt = (
        "Question: What is the capital of France?\n"
        "A. London\n"
        "B. Paris\n"
        "C. Berlin\n"
        "D. Madrid\n"
        "Answer:"
    )
    scores = backend.score_choices(prompt, ("A", "B", "C", "D"), top_k=50)
    assert set(scores.keys()) == {"A", "B", "C", "D"}
    # B should outscore the others
    assert scores["B"] > scores["A"]
    assert scores["B"] > scores["C"]
    assert scores["B"] > scores["D"]


def test_encode_choice_letters_finds_at_least_one_variant(backend):
    candidates = backend.encode_choice_letters(("A", "B", "C", "D"))
    for L in ("A", "B", "C", "D"):
        assert len(candidates[L]) >= 1, (
            f"no single-token encoding for {L!r}; check tokenizer assumption"
        )
