"""GSM8K row → HTTP-Backend chat call + answer extraction.

8-shot canonical CoT prompt (Wei et al. 2022). No logprobs needed; the
model generates reasoning + a final answer, and the extractor pulls the
numeric answer from the output (stripping <think> blocks first for Qwen3).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from benchmarks._eval_common.extract import extract_gsm8k_answer
from benchmarks._eval_common.fewshot import build_gsm8k_8shot_prompt


@dataclass
class GsmItemResult:
    qid: int
    question: str
    gold_answer: float
    raw_output: str
    extracted_answer: float | None
    failure_reason: str
    correct: bool
    wall_s: float
    prompt_tokens: int
    completion_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def extract_gold_answer(row_answer: str) -> float:
    """GSM8K gold answers end with '#### N'. Extract the numeric value."""
    val, reason = extract_gsm8k_answer(row_answer)
    if val is None:
        raise ValueError(f"could not parse gold answer ({reason}): {row_answer!r}")
    return val


def build_messages(question: str, think: bool = True) -> list[dict[str, str]]:
    """Build a chat-completion messages list for one GSM8K question.

    When think=False, prepends Qwen3's `/no_think` soft-switch so the chat
    template emits no `<think>` block. The 8-shot CoT prompt itself supplies
    the reasoning structure; matches published GSM8K methodology.
    """
    prompt = build_gsm8k_8shot_prompt(question)
    if not think:
        prompt = "/no_think\n" + prompt
    return [{"role": "user", "content": prompt}]
