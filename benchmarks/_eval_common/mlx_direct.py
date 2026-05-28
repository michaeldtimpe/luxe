"""In-process model backend via mlx_lm — for benchmarks that need logprobs.

The oMLX HTTP server (Backend in src/luxe/backend.py) silently drops the
`logprobs` / `top_logprobs` parameters on both /v1/chat/completions and
/v1/completions, so MMLU / ARC / perplexity cannot use it. This module
loads the same model weights via mlx_lm directly so we can read logits.

**Sequencing constraint:** loading the 35B 6-bit weights here costs
~20-25 GB of RAM. If the oMLX server is also running with the same model
loaded, total memory will be roughly double. On the 64GB single-machine
configuration: run mlx-direct benchmarks (MMLU/ARC/perplexity) when oMLX
is idle, or stop the oMLX server first. The eval suite runner sequences
HTTP-Backend benchmarks (BFCL/swebench/maintain_suite/GSM8K/CodeNeedle)
before mlx-direct benchmarks to avoid contention.

**Chat-template handling:** `MLXDirectBackend` is template-agnostic.
Callers either pass raw completion prompts (perplexity over WikiText)
or pre-templated chat strings (MMLU/ARC must apply the tokenizer's
chat_template explicitly via `apply_chat_template()` before scoring).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import mlx.core as mx
import mlx.nn as nn
from mlx_lm import load


DEFAULT_HF_REPO = "mlx-community/Qwen3.6-35B-A3B-6bit"


@dataclass(frozen=True)
class TopLogprob:
    token: str
    token_id: int
    logprob: float


class MLXDirectBackend:
    """Thin wrapper over an mlx_lm-loaded model exposing logprob helpers.

    Two public methods:
      - `token_logprobs(text)` — per-token logprob of `text`. Used by perplexity.
      - `first_token_top_logprobs(prompt, top_k)` — top-K candidates for the
        next token after `prompt`. Used by MMLU/ARC.

    Model + tokenizer are loaded once at construction. Reuse the same
    instance across all questions in a benchmark.
    """

    def __init__(self, repo_or_path: str = DEFAULT_HF_REPO):
        self.repo_or_path = repo_or_path
        self.model, self.tokenizer = load(repo_or_path)
        self.model.eval()

    # --- logprob helpers -----------------------------------------------------

    def token_logprobs(self, text: str) -> tuple[list[int], list[float]]:
        """Return (token_ids, per-token logprobs) for `text`.

        Each logprob[i] is log P(token[i+1] | token[0..i]); the first
        token has no predecessor so it is not scored. The returned
        token_ids list is the full tokenization (length N); the logprobs
        list is length N-1 aligned to token_ids[1:].
        """
        ids = self.tokenizer.encode(text)
        return ids, self.token_logprobs_from_ids(ids)

    def token_logprobs_from_ids(self, ids: list[int]) -> list[float]:
        """Per-token logprobs given pre-tokenized IDs.

        Returns a list of length len(ids)-1; entry i is the logprob of
        ids[i+1] conditioned on ids[0..i]. Used by perplexity, which
        tokenizes the corpus once and slides windows over the ID array.
        """
        if len(ids) < 2:
            return []
        arr = mx.array(ids)[None, :]  # (1, N)
        logits = self.model(arr)        # (1, N, V)
        logits = logits[0]              # (N, V)
        log_probs = nn.log_softmax(logits[:-1], axis=-1)  # (N-1, V)
        targets = mx.array(ids[1:])                       # (N-1,)
        idx = mx.arange(log_probs.shape[0])
        gathered = log_probs[idx, targets]                # (N-1,)
        return [float(x) for x in gathered.tolist()]

    def first_token_top_logprobs(
        self,
        prompt: str,
        top_k: int = 20,
    ) -> list[TopLogprob]:
        """Top-K candidates for the next token after `prompt`.

        Used by MMLU / ARC: build a prompt ending with `Answer:`, call this,
        and pick argmax over candidate choice-letter token IDs.
        """
        ids = self.tokenizer.encode(prompt)
        if not ids:
            raise ValueError("empty prompt")
        arr = mx.array(ids)[None, :]      # (1, N)
        logits = self.model(arr)           # (1, N, V)
        last = logits[0, -1, :]            # (V,)
        log_probs = nn.log_softmax(last, axis=-1)
        # Top-K
        top_ids = mx.argpartition(-log_probs, top_k)[:top_k]
        # Sort top_k by logprob descending
        top_lp = log_probs[top_ids]
        order = mx.argsort(-top_lp)
        sorted_ids = top_ids[order]
        sorted_lp = top_lp[order]

        ids_py: list[int] = [int(x) for x in sorted_ids.tolist()]
        lp_py: list[float] = [float(x) for x in sorted_lp.tolist()]

        return [
            TopLogprob(
                token=self.tokenizer.decode([tid]),
                token_id=tid,
                logprob=lp,
            )
            for tid, lp in zip(ids_py, lp_py)
        ]

    # --- tokenization helpers ------------------------------------------------

    def encode_choice_letters(
        self,
        letters: Sequence[str] = ("A", "B", "C", "D", "E"),
    ) -> dict[str, list[int]]:
        """Return per-letter candidate token IDs.

        For each letter, returns the token IDs of both the bare form ("A")
        and the leading-space form (" A"), since BPE tokenizers like Qwen's
        encode these differently after preceding context. MMLU/ARC scoring
        should accept whichever variant the model emits.
        """
        out: dict[str, list[int]] = {}
        for L in letters:
            candidates: list[int] = []
            for variant in (L, " " + L):
                ids = self.tokenizer.encode(variant)
                # Strip the BOS token if present (encode prepends it)
                if ids and ids[0] == getattr(self.tokenizer, "bos_token_id", -1):
                    ids = ids[1:]
                if len(ids) == 1:
                    candidates.append(ids[0])
            # Dedupe while preserving order
            seen: set[int] = set()
            out[L] = [t for t in candidates if not (t in seen or seen.add(t))]
        return out

    def score_choices(
        self,
        prompt: str,
        choice_letters: Sequence[str] = ("A", "B", "C", "D"),
        top_k: int = 50,
    ) -> dict[str, float]:
        """Score MCQ choice letters by reading top_k from the next-token dist.

        Returns {letter: best_logprob_among_variants} for each letter, using
        whichever of (`L`, ` L`) appears in the top-K with higher logprob.
        Letters not in top-K get -inf.

        `top_k=50` is chosen because Qwen's tokenizer has multiple choice-letter
        variants and the first generated token after a chat template can have
        unusual continuations in the top few slots.
        """
        candidates = self.encode_choice_letters(choice_letters)
        top = self.first_token_top_logprobs(prompt, top_k=top_k)
        top_by_id = {t.token_id: t.logprob for t in top}
        out: dict[str, float] = {}
        for letter, tok_ids in candidates.items():
            best = -math.inf
            for tid in tok_ids:
                lp = top_by_id.get(tid)
                if lp is not None and lp > best:
                    best = lp
            out[letter] = best
        return out
