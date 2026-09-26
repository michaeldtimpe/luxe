"""Regressions for the 2026-09 gitkit review: cache staleness, the apply
executor's revert/keep/verify paths, report parsing, and ephemeral leaks.

Every repro builds a throwaway git repo under tmp_path with HOME pointed at a
temp dir — nothing here may touch the real ~/.luxe.
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from rich.console import Console


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LUXE_HOME", raising=False)
    return home


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True).stdout.strip()


def _init_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e.com")
    _git(root, "config", "user.name", "T")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


# --- 3: severity headers as real reports write them --------------------------

_REAL_AUDIT = """# Repository audit
**Findings: 4**

## Repository summary & risk
Use-risk: low.

## Bugs & security

### Critical
- **critical** `a.py:1` — takeover

### High
- `b.py:2` — token bypass

### M — `StopIteration` on bare `next()`
Evidence:
```python
# audio_meta.py:29
x = next(it)
```

### Low
- `d.py:4` — tidy
- `e.py:5` — nit

## Structural improvements
- split the god module
"""


def test_extract_findings_reads_nested_and_shorthand_severity_headers():
    from luxe.gitkit import store
    out = store.extract_findings(_REAL_AUDIT)
    assert "takeover" in out and "token bypass" in out
    assert "StopIteration" in out and "audio_meta.py:29" in out
    assert "tidy" in out
    assert "Repository summary" not in out
    assert "split the god module" not in out


def test_min_severity_hides_nested_sections():
    from luxe.gitkit import store
    out, dropped = store.filter_min_severity(_REAL_AUDIT, "high")
    assert "takeover" in out and "token bypass" in out
    assert "StopIteration" not in out           # ### M — … (one finding)
    assert "tidy" not in out and "nit" not in out
    assert "split the god module" in out        # later ## section survives
    assert dropped == 3
