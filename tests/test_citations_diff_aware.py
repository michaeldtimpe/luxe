"""Tests for src/luxe/citations.py — diff-aware citation linter.

Verifies that the linter:
- Resolves citations on unchanged files via line+snippet match
- Tolerates line shift on edited files via fuzzy snippet match within ±20 lines
- Treats deletion-as-resolution (workers may delete buggy code as the fix)
- Fails on missing files, out-of-range lines, content mismatches
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe.citations import (
    Citation,
    LintResult,
    ValidatorEnvelope,
    ValidatorFinding,
    extract_citations,
    lint_report,
)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A small git repo with a base commit."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def sub(a, b):\n"
        "    return a - b\n"
        "\n"
        "def mul(a, b):\n"
        "    return a * b\n"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _base_sha(repo: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def test_extract_citations_basic():
    text = "See `src/foo.py:42` and src/bar.py:100-105 for details."
    cs = extract_citations(text)
    assert len(cs) == 2
    assert cs[0].path == "src/foo.py"
    assert cs[0].line == 42
    assert cs[1].path == "src/bar.py"
    assert cs[1].line == 100
    assert cs[1].line_end == 105


def test_extract_skips_non_extension_paths():
    text = "Bumped version 1.2.3:4 in setup.py:10"
    cs = extract_citations(text)
    paths = [c.path for c in cs]
    assert "setup.py" in paths
    # 1.2.3 has no source-y extension regex match


def test_resolved_unchanged_with_snippet(git_repo: Path):
    base = _base_sha(git_repo)
    env = ValidatorEnvelope(status="verified", verified=[
        ValidatorFinding(path="src/calc.py", line=2, snippet="return a + b",
                         severity="info", description="add"),
    ])
    report = "Found `src/calc.py:2` see code."
    res = lint_report(report, git_repo, base_sha=base, envelope=env)
    assert not res.is_blocking
    assert res.citations[0].status == "resolved"


def test_content_mismatch_unchanged_file(git_repo: Path):
    base = _base_sha(git_repo)
    env = ValidatorEnvelope(status="verified", verified=[
        # Wrong snippet — claims line 2 says something it doesn't
        ValidatorFinding(path="src/calc.py", line=2, snippet="this code does not exist",
                         severity="info", description="bogus"),
    ])
    report = "Found `src/calc.py:2`."
    res = lint_report(report, git_repo, base_sha=base, envelope=env)
    assert res.is_blocking
    assert res.citations[0].status == "content_mismatch"


def test_missing_file(git_repo: Path):
    base = _base_sha(git_repo)
    env = ValidatorEnvelope(status="verified", verified=[
        ValidatorFinding(path="src/does_not_exist.py", line=1, snippet="x", severity="info"),
    ])
    report = "See `src/does_not_exist.py:1`."
    res = lint_report(report, git_repo, base_sha=base, envelope=env)
    assert res.is_blocking
    assert res.citations[0].status == "missing_file"


def test_out_of_range_line(git_repo: Path):
    base = _base_sha(git_repo)
    env = ValidatorEnvelope(status="verified", verified=[
        ValidatorFinding(path="src/calc.py", line=999, snippet="x", severity="info"),
    ])
    report = "See `src/calc.py:999`."
    res = lint_report(report, git_repo, base_sha=base, envelope=env)
    assert res.is_blocking
    assert res.citations[0].status == "out_of_range"


def test_resolved_shifted_after_edit(git_repo: Path):
    base = _base_sha(git_repo)

    # Worker prepends 20 lines to the file. `sub` was at line 4, now at 24.
    src = git_repo / "src" / "calc.py"
    prefix = "# preamble line\n" * 20
    src.write_text(prefix + src.read_text())
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "prepend"], cwd=git_repo, check=True)

    env = ValidatorEnvelope(status="verified", verified=[
        # Validator's snippet was captured BEFORE the edit, citing line 4.
        ValidatorFinding(path="src/calc.py", line=4, snippet="def sub(a, b):",
                         severity="info", description="sub"),
    ])
    report = "Found `src/calc.py:4`."
    res = lint_report(report, git_repo, base_sha=base, envelope=env)
    # Within ±20 lines, the snippet IS at line 24 (4 + 20). Should resolve_shifted.
    assert not res.is_blocking
    assert res.citations[0].status == "resolved_shifted"
    assert res.citations[0].matched_line == 24


def test_shifted_unverified_when_too_far(git_repo: Path):
    base = _base_sha(git_repo)

    # Worker prepends 50 lines — `sub` moves from line 4 to line 54, past the ±20 window.
    src = git_repo / "src" / "calc.py"
    prefix = "# preamble line\n" * 50
    src.write_text(prefix + src.read_text())
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "prepend"], cwd=git_repo, check=True)

    env = ValidatorEnvelope(status="verified", verified=[
        ValidatorFinding(path="src/calc.py", line=4, snippet="def sub(a, b):",
                         severity="info", description="sub"),
    ])
    report = "Found `src/calc.py:4`."
    res = lint_report(report, git_repo, base_sha=base, envelope=env)
    assert res.is_blocking
    assert res.citations[0].status == "shifted_unverified"


def test_resolved_by_deletion(git_repo: Path):
    base = _base_sha(git_repo)
    # Worker deletes the file (a legitimate fix — buggy code removed)
    (git_repo / "src" / "calc.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "remove buggy file"], cwd=git_repo, check=True)

    env = ValidatorEnvelope(status="verified", verified=[
        ValidatorFinding(path="src/calc.py", line=2, snippet="return a + b", severity="info"),
    ])
    report = "Reported `src/calc.py:2` was buggy and removed."
    res = lint_report(report, git_repo, base_sha=base, envelope=env)
    assert not res.is_blocking
    assert res.citations[0].status == "resolved_by_deletion"


def test_lint_with_no_envelope_accepts_in_range_unchanged(git_repo: Path):
    base = _base_sha(git_repo)
    report = "See `src/calc.py:2`."
    res = lint_report(report, git_repo, base_sha=base, envelope=None)
    # No snippet to verify and file unchanged: resolved on line existence.
    assert res.citations[0].status == "resolved"


def test_dedupe_citations():
    text = "`src/foo.py:1` and `src/foo.py:1` again"
    cs = extract_citations(text)
    assert len(cs) == 1


def test_extract_citations_rejects_ipv4_host_port():
    """`127.0.0.1:8000` is a host:port reference (deployment doc), not a
    file:line citation. The extractor must skip IPv4-shaped paths so the
    citation linter doesn't flag dashboard URLs as unresolved.

    Regression: isomer-quickstart synthesizer reports referenced
    `127.0.0.1:27001` for the dashboard and the build-breaking citation
    gate then docked the fixture's score (Phase 1 ship-confirmation
    2026-05-02). Surgical fix at extractor: reject paths matching
    `^\\d+\\.\\d+\\.\\d+\\.\\d+$`.
    """
    text = (
        "Run with `docker compose up`; the dashboard is at "
        "`http://127.0.0.1:27001/`. The mapping `127.0.0.1:27001:27001` "
        "is in `docker-compose.yml`. A real citation: `app.py:42`."
    )
    cs = extract_citations(text)
    paths = {c.path for c in cs}
    assert "127.0.0.1" not in paths
    # The legitimate file:line reference should still be extracted.
    assert "app.py" in paths
    # Both IP-shaped strings rejected; only the real one survives.
    assert len(cs) == 1


def test_extract_citations_keeps_dotted_filenames_with_digits():
    """`v1.2.3.py:10` is a real (if unusual) filename; the IPv4 guard
    must NOT reject it. Only paths that fully match `\\d+\\.\\d+\\.\\d+\\.\\d+`
    (no extension) are dropped — `v1.2.3.py` has a `.py` extension and
    a non-digit prefix in the leading segment."""
    text = "See `v1.2.3.py:10` for the override."
    cs = extract_citations(text)
    assert len(cs) == 1
    assert cs[0].path == "v1.2.3.py"
