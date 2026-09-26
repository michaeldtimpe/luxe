"""Index-build robustness + k clamping for the search tools (2026-09-26).

- `symbols._walk_for_symbols` recursed once per tree depth, so one deeply
  nested JS file (a long method chain, generated nested arrays) raised
  RecursionError and took the WHOLE index build down with it.
- `bm25_search` / `find_symbol` passed a model-supplied `k` straight through:
  `k=-1` meant "all but the last hit", `k=0` nothing, a string raised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe import search, symbols


def test_deeply_nested_file_does_not_kill_the_symbol_index(tmp_path: Path):
    (tmp_path / "ok.py").write_text("def keep_me():\n    return 1\n")
    depth = 3000
    (tmp_path / "deep.js").write_text(
        "function outer() { return " + "[" * depth + "]" * depth + "; }\n")
    idx = symbols.build_symbol_index(tmp_path)
    names = {s.name for s in idx.symbols}
    assert "keep_me" in names
    assert "outer" in names


def test_symbol_order_is_preorder(tmp_path: Path):
    (tmp_path / "m.py").write_text(
        "class A:\n    def a1(self): pass\n    def a2(self): pass\n"
        "def f():\n    pass\nclass B:\n    def b1(self): pass\n")
    idx = symbols.build_symbol_index(tmp_path)
    assert [s.name for s in idx.symbols] == ["A", "a1", "a2", "f", "B", "b1"]


def test_a_file_whose_parse_raises_is_skipped(tmp_path: Path, monkeypatch):
    (tmp_path / "a.py").write_text("def a(): pass\n")
    (tmp_path / "b.py").write_text("def b(): pass\n")
    real = symbols._parse_file

    def flaky(path, lang):
        if path.name == "a.py":
            raise RuntimeError("parser blew up")
        return real(path, lang)

    monkeypatch.setattr(symbols, "_parse_file", flaky)
    idx = symbols.build_symbol_index(tmp_path)
    assert [s.name for s in idx.symbols] == ["b"]


@pytest.fixture
def bm25(tmp_path: Path):
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text(f"alpha beta token{i}\n")
    search.set_index(search.build_bm25_index(tmp_path))
    yield
    search.set_index(None)


@pytest.mark.parametrize("k", [-1, 0])
def test_bm25_nonpositive_k_clamps_to_one(bm25, k):
    out, err = search._bm25_search_fn({"query": "alpha", "k": k})
    assert err is None
    lines = out.splitlines()
    assert len(lines) == 1 and lines[0].startswith("f")


def test_bm25_non_integer_k_uses_default(bm25):
    out, err = search._bm25_search_fn({"query": "alpha", "k": "x"})
    assert err is None
    assert len(out.splitlines()) == 5


def test_bm25_k_upper_bound(bm25):
    out, _ = search._bm25_search_fn({"query": "alpha", "k": 10**9})
    assert len(out.splitlines()) == 5


@pytest.fixture
def symidx(tmp_path: Path):
    (tmp_path / "m.py").write_text("".join(f"def fn{i}(): pass\n" for i in range(5)))
    symbols.set_index(symbols.build_symbol_index(tmp_path))
    yield
    symbols.set_index(None)


@pytest.mark.parametrize("k,n", [(-1, 1), (0, 1), ("x", 5), (10**9, 5)])
def test_find_symbol_k_is_clamped(symidx, k, n):
    out, err = symbols._find_symbol_fn({"name": "fn", "k": k})
    assert err is None
    assert len(out.splitlines()) == n


def test_clamp_upper_bounds():
    assert search._clamp_k(10**9, default=10) == search._BM25_MAX_K
    assert symbols._clamp_k(10**9, default=50) == symbols._FIND_SYMBOL_MAX_K
