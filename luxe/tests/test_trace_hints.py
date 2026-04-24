"""Unit tests for the shared trace-parse utility.

Covers the invariants both the orchestrator's `_augment_with_trace_hints`
and the benchmark's `stack_trace_guided` retrieval strategy depend on.
"""

from __future__ import annotations

from pathlib import Path

from shared.trace_hints import parse_trace_paths


def _mk_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path


def test_empty_input_returns_empty(tmp_path: Path):
    assert parse_trace_paths("", tmp_path) == []
    assert parse_trace_paths(None, tmp_path) == []  # type: ignore[arg-type]


def test_extracts_existing_py_files(tmp_path: Path):
    _mk_repo(tmp_path, {"app/handlers.py": "x=1\n", "tests/test_x.py": "y=2\n"})
    text = 'File "app/handlers.py", line 42, in handle_request\n  raise'
    out = parse_trace_paths(text, tmp_path)
    assert [p.name for p in out] == ["handlers.py"]


def test_skips_nonexistent_files(tmp_path: Path):
    _mk_repo(tmp_path, {"real.py": ""})
    text = "real.py:1 and phantom.py:99"
    out = parse_trace_paths(text, tmp_path)
    assert [p.name for p in out] == ["real.py"]


def test_dedupes_preserving_first_seen_order(tmp_path: Path):
    _mk_repo(tmp_path, {"a.py": "", "b.py": ""})
    text = "b.py:10 then a.py:20 then b.py:30 then a.py:40"
    out = parse_trace_paths(text, tmp_path)
    assert [p.name for p in out] == ["b.py", "a.py"]


def test_rejects_path_escape(tmp_path: Path):
    _mk_repo(tmp_path, {"sub/real.py": ""})
    # ../ tries to escape; parent-relative paths that resolve outside
    # repo_root must be dropped.
    outside = tmp_path.parent / "escape.py"
    outside.write_text("")
    try:
        text = "../escape.py:1 and sub/real.py:2"
        out = parse_trace_paths(text, tmp_path)
        assert [p.name for p in out] == ["real.py"]
    finally:
        outside.unlink()


def test_ignores_non_py_paths(tmp_path: Path):
    _mk_repo(tmp_path, {"app.py": "", "style.css": ""})
    text = "app.py:1 and style.css:42"
    out = parse_trace_paths(text, tmp_path)
    assert [p.name for p in out] == ["app.py"]


def test_dotted_relative_paths_resolve(tmp_path: Path):
    _mk_repo(tmp_path, {"pkg/mod.py": ""})
    text = "./pkg/mod.py:5"
    out = parse_trace_paths(text, tmp_path)
    assert [p.name for p in out] == ["mod.py"]
