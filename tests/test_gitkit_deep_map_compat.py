"""On-disk compatibility of the deep-mode incremental cache.

`tests/golden/gitkit_deep_map_v2/` is a `map/` directory written by the
PRE-PACKAGE `gitkit/deep.py` (origin/main before the deep/ split): the v2
`mapped.json` breadcrumb, `chunks.json`, `survey.json`, `head`,
`survey_notes.md`, and the per-kind notes cache `notes/gitaudit/chunk-NN.json`.
Existing `~/.luxe/reports/<hash>/map/` dirs on every host look like this, so
the current code must (a) load it as a FRESH v2 map with valid cached notes,
and (b) write byte-identical files from the same inputs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from luxe.gitkit import deep

GOLDEN = Path(__file__).parent / "golden" / "gitkit_deep_map_v2"
META = json.loads((GOLDEN / "_meta.json").read_text())
HEAD = META["head"]


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The same file contents the golden map was generated over (blob shas
    are content-addressed, so the cached notes' shas must match)."""
    r = tmp_path / "repo"
    r.mkdir()
    for rel, text in META["files"].items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text)
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "Tester"], ["add", "-A"],
                 ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True)
    return r


def _install_golden(repo: Path) -> Path:
    d = deep._map_dir(repo)
    shutil.copytree(GOLDEN, d)
    (d / "_meta.json").unlink()
    return d


def test_pre_package_map_loads_fresh_with_valid_notes(repo):
    _install_golden(repo)

    st = deep.map_status(repo, head=HEAD)
    assert st.state is deep.MapState.FRESH
    assert st.version == 2 and st.n_chunks == 2 and st.content_budget == 12
    assert set(st.files) == set(META["files"])
    assert st.baseline["orig_n_chunks"] == 2

    cached = deep.load_map(repo, head=HEAD)
    assert cached is not None
    assert cached["survey_notes"].strip() == "survey: demo repo"
    assert cached["content_budget"] == 12
    assert cached["framing"] == ["README.md", "src/app.py"]
    chunks = cached["chunks"]
    assert [c.files for c in chunks] == [
        ["src/app.py", "README.md", "lib/core.py"], ["src/util.py"]]
    assert all(isinstance(c, deep.Chunk) for c in chunks)

    # the notes cache: every note valid against the CURRENT working tree
    shas = deep.worktree_file_shas(repo)
    digest = deep.empty_digest()
    for c in chunks:
        note = deep.load_chunk_note(repo, "gitaudit", c.index)
        assert deep.chunk_note_is_valid(note, c, shas), c.index
        deep.fold_contribution(digest, note["contribution"], c.index)
    assert [f["title"] for f in digest["provisional_findings"]] == ["t0", "t1"]
    assert len(digest["markdown_notes"]) == 2

    # HEAD moved → STALE, and the incremental path still accepts it
    assert deep.map_status(repo, head="f" * 40).state is deep.MapState.STALE
    assert deep.load_map(repo, head="f" * 40, allow_stale=True) is not None


def test_pre_package_map_is_rewritten_byte_identically(repo, monkeypatch):
    """Same inputs → the same bytes as the pre-package writer, file for file
    (mapped_at pinned to the golden's timestamp)."""
    golden_bc = json.loads((GOLDEN / "mapped.json").read_text())
    monkeypatch.setattr(time, "time", lambda: float(golden_bc["mapped_at"]))
    chunks = [deep.Chunk.from_dict(c) for c in
              json.loads((GOLDEN / "chunks.json").read_text())["chunks"]]

    deep.save_map(repo, head=HEAD, survey_notes="survey: demo repo",
                  chunks=chunks, content_budget=12,
                  framing=deep.framing_files(repo), summary_render="(summary)",
                  files=deep.worktree_file_shas(repo))
    for c in chunks:
        note = json.loads(
            (GOLDEN / "notes" / "gitaudit" / f"chunk-{c.index:02d}.json").read_text())
        deep.save_chunk_note(repo, "gitaudit", c, head=HEAD,
                             file_shas=note["file_shas"],
                             contribution=note["contribution"],
                             wall_s=note["wall_s"])

    d = deep._map_dir(repo)
    written = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
    expected = sorted(p.relative_to(GOLDEN).as_posix() for p in GOLDEN.rglob("*")
                      if p.is_file() and p.name != "_meta.json")
    assert written == expected
    for rel in expected:
        assert (d / rel).read_bytes() == (GOLDEN / rel).read_bytes(), rel
