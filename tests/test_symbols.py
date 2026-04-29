"""Tests for src/luxe/symbols.py — tree-sitter AST symbol indexing."""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe import symbols as symbols_mod
from luxe.symbols import (
    SUPPORTED_LANGUAGES,
    Symbol,
    _find_symbol_fn,
    build_symbol_index,
)


@pytest.fixture(autouse=True)
def _reset():
    yield
    symbols_mod.reset_index()


def test_supported_languages():
    assert "python" in SUPPORTED_LANGUAGES
    assert "javascript" in SUPPORTED_LANGUAGES
    assert "typescript" in SUPPORTED_LANGUAGES
    assert "rust" in SUPPORTED_LANGUAGES
    assert "go" in SUPPORTED_LANGUAGES


def test_index_python_classes_and_functions(tmp_path: Path):
    (tmp_path / "service.py").write_text(
        "class UserService:\n"
        "    def authenticate(self, user):\n"
        "        return True\n"
        "\n"
        "def utility_fn(x):\n"
        "    return x * 2\n"
    )
    idx = build_symbol_index(tmp_path)
    assert idx.coverage == {"python": 1}
    names = sorted(s.name for s in idx.symbols)
    assert "UserService" in names
    assert "authenticate" in names
    assert "utility_fn" in names


def test_index_typescript(tmp_path: Path):
    (tmp_path / "auth.ts").write_text(
        "interface User { id: number; }\n"
        "class AuthService {\n"
        "  authenticate(u: User) { return true; }\n"
        "}\n"
        "function logout() {}\n"
    )
    idx = build_symbol_index(tmp_path)
    assert idx.coverage.get("typescript", 0) == 1
    kinds = {s.kind for s in idx.symbols}
    assert "interface" in kinds
    assert "class" in kinds
    assert "function" in kinds


def test_index_rust(tmp_path: Path):
    (tmp_path / "lib.rs").write_text(
        "pub struct User { id: u64 }\n"
        "pub trait Authenticate { fn auth(&self) -> bool; }\n"
        "pub fn logout() {}\n"
    )
    idx = build_symbol_index(tmp_path)
    assert idx.coverage.get("rust", 0) == 1
    kinds = {s.kind for s in idx.symbols}
    assert "struct" in kinds
    assert "interface" in kinds  # trait → interface
    assert "function" in kinds


def test_index_go(tmp_path: Path):
    (tmp_path / "main.go").write_text(
        "package main\n"
        "type User struct { id int }\n"
        "func (u *User) Login() {}\n"
        "func main() {}\n"
    )
    idx = build_symbol_index(tmp_path)
    assert idx.coverage.get("go", 0) == 1


def test_unsupported_language_excluded(tmp_path: Path):
    (tmp_path / "Main.java").write_text(
        "public class Main {\n  public static void main(String[] args) {}\n}\n"
    )
    idx = build_symbol_index(tmp_path)
    assert "java" not in idx.coverage
    assert idx.symbols == []


def test_find_by_name_substring(tmp_path: Path):
    (tmp_path / "x.py").write_text(
        "class UserService: pass\n"
        "class UserRepository: pass\n"
        "class OrderService: pass\n"
    )
    idx = build_symbol_index(tmp_path)
    matches = idx.find("User")
    names = sorted(s.name for s in matches)
    assert names == ["UserRepository", "UserService"]


def test_find_filters_by_kind(tmp_path: Path):
    (tmp_path / "x.py").write_text(
        "class Greeter:\n"
        "    def greet(self): pass\n"
        "def standalone(): pass\n"
    )
    idx = build_symbol_index(tmp_path)
    classes = idx.find("", kind="class")
    funcs = idx.find("", kind="function")
    assert all(s.kind == "class" for s in classes)
    assert any(s.name == "Greeter" for s in classes)
    assert any(s.name == "standalone" for s in funcs)


def test_find_filters_by_language(tmp_path: Path):
    (tmp_path / "a.py").write_text("class A: pass\n")
    (tmp_path / "b.ts").write_text("class B {}\n")
    idx = build_symbol_index(tmp_path)
    py_only = idx.find("", language="python")
    assert all(s.language == "python" for s in py_only)


def test_find_returns_line_numbers(tmp_path: Path):
    (tmp_path / "x.py").write_text(
        "# comment\n"
        "# another\n"
        "class Target: pass\n"
    )
    idx = build_symbol_index(tmp_path)
    target = next(s for s in idx.symbols if s.name == "Target")
    assert target.start_line == 3


# --- tool fn ---

def test_tool_fn_no_index():
    text, err = _find_symbol_fn({"name": "Foo"})
    assert err is not None
    assert "not built" in err


def test_tool_fn_unsupported_language(tmp_path: Path):
    # Build an index from an empty repo (no supported languages).
    symbols_mod.set_index(build_symbol_index(tmp_path))
    text, err = _find_symbol_fn({"name": "X", "language": "java"})
    assert err is not None
    assert "bm25_search" in err


def test_tool_fn_found(tmp_path: Path):
    (tmp_path / "x.py").write_text("class TargetClass: pass\n")
    symbols_mod.set_index(build_symbol_index(tmp_path))
    text, err = _find_symbol_fn({"name": "Target"})
    assert err is None
    assert "TargetClass" in text
    assert "x.py:1" in text


def test_tool_fn_no_match_in_supported_lang(tmp_path: Path):
    (tmp_path / "x.py").write_text("class Foo: pass\n")
    symbols_mod.set_index(build_symbol_index(tmp_path))
    text, err = _find_symbol_fn({"name": "Nonexistent"})
    assert err is None
    assert "no matches" in text


def test_tool_fn_empty_index_redirects_to_bm25(tmp_path: Path):
    # An entirely empty repo: coverage is {}.
    symbols_mod.set_index(build_symbol_index(tmp_path))
    text, err = _find_symbol_fn({"name": "Anything"})
    assert err is not None
    assert "empty" in err
    assert "bm25_search" in err
