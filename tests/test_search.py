"""Tests for src/luxe/search.py — BM25 indexing and search."""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe import search as search_mod
from luxe.search import BM25Index, _bm25_search_fn, build_bm25_index


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    yield
    search_mod.reset_index()


def test_build_index_walks_source_files(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text(
        "def authenticate(user, password):\n    return verify(user, password)\n"
    )
    (tmp_path / "src" / "router.py").write_text(
        "def route(request):\n    return dispatch(request)\n"
    )
    idx = build_bm25_index(tmp_path)
    assert "src/auth.py" in idx.paths
    assert "src/router.py" in idx.paths


def test_build_index_skips_excluded_dirs(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "def authenticate_user(name): return verify_user(name)\n"
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("module.exports = {}\n")
    idx = build_bm25_index(tmp_path)
    assert idx.paths == ["src/main.py"]


def test_build_index_skips_non_source_extensions(tmp_path: Path):
    (tmp_path / "data.json").write_text('{"x": 1}')
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    (tmp_path / "code.py").write_text(
        "def authenticate_user(name): return verify_user(name)\n"
    )
    idx = build_bm25_index(tmp_path)
    assert idx.paths == ["code.py"]


def test_search_returns_relevant_files(tmp_path: Path):
    # BM25 IDF degenerates on tiny corpora (a 2-doc corpus where every query
    # term appears in 1 doc gets IDF=0). Need >5 docs for stable scoring.
    (tmp_path / "auth.py").write_text(
        "def authenticate_user(username, password):\n"
        "    return verify_credentials(username, password)\n"
    )
    (tmp_path / "render.py").write_text(
        "def render_page(request):\n    return template.render(request.context)\n"
    )
    (tmp_path / "router.py").write_text(
        "def route(request, handlers):\n    return handlers.dispatch(request)\n"
    )
    (tmp_path / "config.py").write_text(
        "default_settings = {'theme': 'dark', 'lang': 'en'}\n"
    )
    (tmp_path / "logger.py").write_text(
        "def emit(level, message):\n    sink.write(level, message)\n"
    )
    (tmp_path / "metrics.py").write_text(
        "def collect(counter, value):\n    counter.increment(value)\n"
    )
    idx = build_bm25_index(tmp_path)
    hits = idx.search("authenticate user credentials", k=3)
    assert hits, f"expected hits but got none; docs were {idx.paths}"
    assert hits[0][0] == "auth.py"


def test_search_empty_query_returns_empty():
    idx = BM25Index(paths=["a.py"], bm25=None, repo_root=Path("/"))
    assert idx.search("", k=5) == []


def test_search_empty_corpus(tmp_path: Path):
    idx = build_bm25_index(tmp_path)
    assert idx.paths == []
    # Empty index doesn't crash on search
    assert idx.search("anything") == []


def test_tool_fn_returns_error_when_no_index():
    text, err = _bm25_search_fn({"query": "test"})
    assert err is not None
    assert "not built" in err


def test_tool_fn_requires_query(tmp_path: Path):
    search_mod.set_index(build_bm25_index(tmp_path))
    text, err = _bm25_search_fn({"query": ""})
    assert err is not None
    assert "required" in err


def test_tool_fn_format(tmp_path: Path):
    # Need >5 docs for BM25 IDF to score above zero (see notes in
    # test_search_returns_relevant_files).
    (tmp_path / "auth.py").write_text("authenticate user password\n" * 20)
    (tmp_path / "render.py").write_text("render template request\n" * 20)
    (tmp_path / "router.py").write_text("route handler dispatch\n" * 20)
    (tmp_path / "config.py").write_text("settings options config\n" * 20)
    (tmp_path / "logger.py").write_text("log emit level\n" * 20)
    (tmp_path / "metrics.py").write_text("counter gauge value\n" * 20)
    search_mod.set_index(build_bm25_index(tmp_path))
    text, err = _bm25_search_fn({"query": "authenticate", "k": 5})
    assert err is None
    assert text != "(no matches)", f"got {text!r}"
    first_line = text.splitlines()[0]
    parts = first_line.split("\t")
    assert parts[0] == "auth.py"
    assert float(parts[1]) > 0


def test_tool_fn_no_matches(tmp_path: Path):
    (tmp_path / "a.py").write_text("hello world\n")
    search_mod.set_index(build_bm25_index(tmp_path))
    text, err = _bm25_search_fn({"query": "zzzunmatchableXX9"})
    assert err is None
    assert "no matches" in text
