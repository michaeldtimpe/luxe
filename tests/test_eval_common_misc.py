"""Offline unit tests for benchmarks/_eval_common/{dataset,fewshot,meta}.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks._eval_common.dataset import (
    cache_dir,
    jsonl_load,
    sha256_file,
    sha256_verify,
)
from benchmarks._eval_common.fewshot import (
    GSM8K_8SHOT_EXEMPLARS,
    build_gsm8k_8shot_prompt,
    deterministic_sample,
)
from benchmarks._eval_common.meta import EVAL_SUITE_VERSION, build_run_meta


class TestDataset:
    def test_sha256_file_known_hash(self, tmp_path: Path):
        p = tmp_path / "hello.txt"
        p.write_text("hello\n")
        # echo -n "hello\n" | sha256sum
        assert sha256_file(p) == (
            "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"
        )

    def test_sha256_verify_match(self, tmp_path: Path):
        p = tmp_path / "f.txt"
        p.write_text("x")
        h = sha256_file(p)
        sha256_verify(p, h)  # no raise

    def test_sha256_verify_mismatch_raises(self, tmp_path: Path):
        p = tmp_path / "f.txt"
        p.write_text("x")
        with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
            sha256_verify(p, "deadbeef")

    def test_jsonl_load(self, tmp_path: Path):
        p = tmp_path / "data.jsonl"
        p.write_text('{"a": 1}\n{"b": 2}\n\n{"c": 3}\n')
        rows = list(jsonl_load(p))
        assert rows == [{"a": 1}, {"b": 2}, {"c": 3}]

    def test_cache_dir_creates(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        d = cache_dir("widgets")
        assert d.exists()
        assert d == tmp_path / ".luxe" / "widgets-data"


class TestFewshot:
    def test_deterministic_sample_reproducible(self):
        a = deterministic_sample(list(range(100)), k=10, seed=42)
        b = deterministic_sample(list(range(100)), k=10, seed=42)
        assert a == b

    def test_deterministic_sample_different_seeds_differ(self):
        a = deterministic_sample(list(range(100)), k=10, seed=42)
        b = deterministic_sample(list(range(100)), k=10, seed=43)
        assert a != b

    def test_deterministic_sample_k_too_large(self):
        out = deterministic_sample([1, 2, 3], k=10, seed=0)
        assert sorted(out) == [1, 2, 3]

    def test_gsm8k_8shot_exemplar_count(self):
        # Wei et al. canonical — must be exactly 8.
        assert len(GSM8K_8SHOT_EXEMPLARS) == 8

    def test_gsm8k_8shot_all_have_answer_marker(self):
        # Every CoT exemplar must end with "The answer is N." so the model
        # learns the answer format from the few-shot context.
        for q, a in GSM8K_8SHOT_EXEMPLARS:
            assert "The answer is" in a, f"exemplar missing answer marker: {q}"

    def test_build_gsm8k_8shot_prompt_structure(self):
        out = build_gsm8k_8shot_prompt("What is 5+5?")
        assert out.count("\nQ: ") == 8  # 8 exemplars + test = 9 Q markers, but
        # the first Q has no leading newline. Better test: count "Q: " total.
        assert out.count("Q: ") == 9
        assert out.endswith("Q: What is 5+5?\nA:")


class TestMeta:
    def test_build_run_meta_minimum(self):
        m = build_run_meta(
            benchmark_protocol_version="mmlu/v1",
            model_id="Qwen3.6-35B-A3B-6bit",
            sampling={"temperature": 0.0, "max_tokens": 1},
            backend_kind="mlx_direct",
            context_window=32768,
        )
        assert m.eval_suite_version == EVAL_SUITE_VERSION
        assert m.benchmark_protocol_version == "mmlu/v1"
        assert m.model_id == "Qwen3.6-35B-A3B-6bit"
        assert m.backend_kind == "mlx_direct"
        assert m.context_window == 32768
        assert m.timestamp_utc.endswith("Z")
        # device string includes something reasonable
        assert m.device  # non-empty

    def test_invalid_backend_kind_raises(self):
        with pytest.raises(ValueError, match="backend_kind"):
            build_run_meta(
                benchmark_protocol_version="x/v1",
                model_id="m",
                sampling={},
                backend_kind="quantum_carrier_pigeon",
                context_window=1024,
            )

    def test_to_dict_round_trips(self):
        m = build_run_meta(
            benchmark_protocol_version="gsm8k/v1",
            model_id="m",
            sampling={"temperature": 0.0},
            backend_kind="http",
            context_window=4096,
            scoring={"method": "generation+extract"},
        )
        d = m.to_dict()
        assert d["benchmark_protocol_version"] == "gsm8k/v1"
        assert d["scoring"] == {"method": "generation+extract"}
