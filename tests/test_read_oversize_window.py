"""`LUXE_READ_OVERSIZE_WINDOW=1`: an unwindowed read of an over-budget file
returns an outline + the first window instead of the refusal (opt-in,
2026-11-08; acceptance/swebench/n75_ab_2026_11_06, django-11333).

OFF must be the refusal, unchanged. ON must stay within `read_limit()`, carry
a correct resume offset, and outline Python files only."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from luxe.tools import fs

READ = fs.READ_ONLY_FNS["read_file"]


@pytest.fixture(autouse=True)
def set_root(tmp_repo: Path):
    fs.set_repo_root(tmp_repo)
    fs.set_read_budget(13653)  # the 32K-window bench budget
    yield
    fs.set_read_budget(None)
    fs._REPO_ROOT = None


def _big_py(tmp_repo: Path, n_funcs: int = 120) -> Path:
    body = []
    for i in range(n_funcs):
        body.append(f"def func_{i}(x):\n    '''doc {i}'''\n" + "    y = x + 1\n" * 6 + "    return y\n\n")
        if i % 20 == 0:
            body.append(f"class Klass{i}:\n    def method_{i}(self):\n        return {i}\n\n")
    p = tmp_repo / "big.py"
    p.write_text("".join(body))
    assert p.stat().st_size > fs.read_limit()
    return p


def test_off_is_the_refusal(tmp_repo, monkeypatch):
    monkeypatch.delenv("LUXE_READ_OVERSIZE_WINDOW", raising=False)
    _big_py(tmp_repo)
    out, err = READ({"path": "big.py"})
    assert out == "" and err and err.startswith("File too large to read whole")


@pytest.mark.parametrize("val", ["", "0", "true", " 1", "01"])
def test_only_exact_1_enables(tmp_repo, monkeypatch, val):
    monkeypatch.setenv("LUXE_READ_OVERSIZE_WINDOW", val)
    _big_py(tmp_repo)
    out, err = READ({"path": "big.py"})
    assert out == "" and err


def test_on_serves_outline_and_window_within_budget(tmp_repo, monkeypatch):
    monkeypatch.setenv("LUXE_READ_OVERSIZE_WINDOW", "1")
    p = _big_py(tmp_repo)
    out, err = READ({"path": "big.py"})
    assert err is None
    assert len(out) <= fs.read_limit()
    assert "over the 13,653-byte whole-file limit" in out
    assert "Outline" in out
    lines = p.read_text().splitlines()
    # Outline entries carry true line numbers.
    for m in re.finditer(r"^(\d+)\t(def|class) (\w+)", out, re.M):
        assert lines[int(m.group(1)) - 1].startswith(f"{m.group(2)} {m.group(3)}")
    # Window starts at line 1 and the resume offset is the next unshown line.
    assert "Lines 1-" in out and "1\tdef func_0(x):" in out
    shown = int(re.search(r"Lines 1-(\d+):", out).group(1))
    assert f"offset={shown}," in out
    # Resuming from there returns the following line.
    nxt, err2 = READ({"path": "big.py", "offset": shown, "limit": 1})
    assert err2 is None and nxt == f"{shown + 1}\t{lines[shown]}\n"


def test_outline_includes_methods_and_clips_when_huge(tmp_repo, monkeypatch):
    monkeypatch.setenv("LUXE_READ_OVERSIZE_WINDOW", "1")
    _big_py(tmp_repo)
    out, _ = READ({"path": "big.py"})
    assert "def method_0(self):" in out or "nested definition(s) not shown" in out
    huge = tmp_repo / "huge.py"
    huge.write_text("".join(f"def f{i}():\n    pass\n" for i in range(4000)))
    out, err = READ({"path": "huge.py"})
    assert err is None and len(out) <= fs.read_limit()
    assert "more definition(s) not shown" in out


def test_non_python_gets_window_without_outline(tmp_repo, monkeypatch):
    monkeypatch.setenv("LUXE_READ_OVERSIZE_WINDOW", "1")
    (tmp_repo / "notes.txt").write_text("def not_code():\n" + "x" * 80 + "\n" * 1 + ("line\n" * 5000))
    out, err = READ({"path": "notes.txt"})
    assert err is None and "Outline" not in out and "Lines 1-" in out
    assert len(out) <= fs.read_limit()


def test_offset_without_limit_starts_the_window_there(tmp_repo, monkeypatch):
    monkeypatch.setenv("LUXE_READ_OVERSIZE_WINDOW", "1")
    p = _big_py(tmp_repo)
    out, err = READ({"path": "big.py", "offset": 100})
    assert err is None and "Lines 101-" in out
    assert f"101\t{p.read_text().splitlines()[100]}" in out


def test_windowed_reads_are_untouched(tmp_repo, monkeypatch):
    _big_py(tmp_repo)
    monkeypatch.delenv("LUXE_READ_OVERSIZE_WINDOW", raising=False)
    off = READ({"path": "big.py", "offset": 0, "limit": 50})
    monkeypatch.setenv("LUXE_READ_OVERSIZE_WINDOW", "1")
    assert READ({"path": "big.py", "offset": 0, "limit": 50}) == off


def test_small_files_are_untouched(tmp_repo, monkeypatch):
    (tmp_repo / "s.py").write_text("def a():\n    return 1\n")
    monkeypatch.delenv("LUXE_READ_OVERSIZE_WINDOW", raising=False)
    off = READ({"path": "s.py"})
    monkeypatch.setenv("LUXE_READ_OVERSIZE_WINDOW", "1")
    assert READ({"path": "s.py"}) == off


def test_single_giant_line_does_not_crash(tmp_repo, monkeypatch):
    monkeypatch.setenv("LUXE_READ_OVERSIZE_WINDOW", "1")
    (tmp_repo / "min.js").write_text("x" * 40000)
    out, err = READ({"path": "min.js"})
    assert err is None and len(out) <= fs.read_limit() and "grep(" in out
