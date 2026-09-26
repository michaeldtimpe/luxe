"""benchmarks/gsm8k/run.py against a stub backend (no network): a backend
exception is an infra error — never an answer extracted from the error text,
never cached — token counts come from resp.timing, resume is keyed on the
model, and the summary covers only this run's selection."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


# --- gsm8k runner (stub backend, no network) ------------------------------

def _gsm8k_data(tmp_path: Path) -> Path:
    p = tmp_path / "test.jsonl"
    p.write_text(json.dumps({"question": "What is 40 + 2?",
                             "answer": "40 + 2 = 42\n#### 42"}) + "\n")
    return p


class _DeadBackend:
    def __init__(self, *a, **k):
        pass

    def chat(self, **k):
        raise ConnectionError("HTTP 502 from 127.0.0.1:8000 after 42 retries")


class _GoodBackend:
    def __init__(self, *a, **k):
        pass

    def chat(self, **k):
        return SimpleNamespace(text="The answer is 42.",
                               timing=SimpleNamespace(prompt_tokens=120,
                                                      completion_tokens=7))


def test_gsm8k_backend_error_is_infra_not_an_extracted_answer(tmp_path, monkeypatch):
    import benchmarks.gsm8k.run as gr
    monkeypatch.setattr(gr, "Backend", _DeadBackend)
    out = tmp_path / "out"
    rc = gr.main(["--output", str(out), "--data", str(_gsm8k_data(tmp_path))])
    summary = json.loads((out / "summary.json").read_text())["results"]
    assert summary["count"] == 0 and summary["correct"] == 0
    assert summary["infra_errors"] == 1
    rec = json.loads((out / "item_00000.json").read_text())
    assert rec["status"] == "infra_error" and "extracted_answer" not in rec
    assert rc == 1


def test_gsm8k_error_is_retried_on_resume_and_tokens_are_recorded(tmp_path, monkeypatch):
    import benchmarks.gsm8k.run as gr
    data = _gsm8k_data(tmp_path)
    out = tmp_path / "out"
    monkeypatch.setattr(gr, "Backend", _DeadBackend)
    gr.main(["--output", str(out), "--data", str(data)])
    monkeypatch.setattr(gr, "Backend", _GoodBackend)
    assert gr.main(["--output", str(out), "--data", str(data)]) == 0
    rec = json.loads((out / "item_00000.json").read_text())
    assert rec["correct"] is True
    assert (rec["prompt_tokens"], rec["completion_tokens"]) == (120, 7)


def test_gsm8k_resume_does_not_reuse_another_models_items(tmp_path, monkeypatch):
    import benchmarks.gsm8k.run as gr
    data = _gsm8k_data(tmp_path)
    out = tmp_path / "out"
    monkeypatch.setattr(gr, "Backend", _GoodBackend)
    gr.main(["--output", str(out), "--data", str(data), "--model", "model-a"])
    calls = []

    class _Counting(_GoodBackend):
        def chat(self, **k):
            calls.append(1)
            return super().chat(**k)
    monkeypatch.setattr(gr, "Backend", _Counting)
    gr.main(["--output", str(out), "--data", str(data), "--model", "model-b"])
    assert calls == [1]


def test_gsm8k_summary_counts_only_this_selection(tmp_path, monkeypatch):
    import benchmarks.gsm8k.run as gr
    data = tmp_path / "test.jsonl"
    data.write_text("".join(json.dumps({"question": f"q{i}", "answer": "#### 1"}) + "\n"
                            for i in range(3)))
    out = tmp_path / "out"
    monkeypatch.setattr(gr, "Backend", _GoodBackend)
    gr.main(["--output", str(out), "--data", str(data)])
    gr.main(["--output", str(out), "--data", str(data), "--limit", "1"])
    assert json.loads((out / "summary.json").read_text())["results"]["count"] == 1
