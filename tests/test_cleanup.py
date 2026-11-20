"""Cleanup turn (LUXE_CLEANUP_TURN, default ON since 2026-10-18; luxe/cleanup.py).

Qwen3.8-27B verified its /health route with a throwaway `health_check_tmp.py`
("delete after use") and left it, failing the `orphan_file` gate 3/3 on an
otherwise-correct change (acceptance/tuning_2026_10). The model decides which
new, unreferenced files are scratch; Python deletes only offered paths.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from luxe import cleanup
from luxe.backend import ChatResponse, GenerationTiming
from tests._git import git

_git = partial(git, strip=False)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    base = _git(r, "rev-parse", "HEAD").strip()
    # The run: a real new module the app imports, a scratch script, a cache,
    # and a new doc.
    (r / "health.py").write_text("def health():\n    return {'status': 'ok'}\n")
    (r / "app.py").write_text((r / "app.py").read_text() + "import health\n")
    (r / "health_check_tmp.py").write_text('"""Throwaway (delete after use)."""\n')
    (r / "__pycache__").mkdir()
    (r / "__pycache__" / "app.cpython-314.pyc").write_bytes(b"\0")
    (r / "NOTES.md").write_text("notes\n")
    return r, base


class _Reply:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list = []

    def chat(self, messages, **kw):
        self.calls.append((messages, kw))
        return ChatResponse(text=self.text, finish_reason="stop",
                            timing=GenerationTiming(prompt_tokens=1, completion_tokens=1))


def test_candidates_are_unreferenced_code_and_caches_only(repo):
    r, base = repo
    paths = {c.path: c.kind for c in cleanup.find_candidates(r, base)}
    assert paths == {"health_check_tmp.py": "unreferenced",
                     "__pycache__/app.cpython-314.pyc": "generated"}


def test_delete_lines_apply_only_to_offered_paths(repo):
    r, base = repo
    backend = _Reply("DELETE health_check_tmp.py\nDELETE app.py\n"
                     "KEEP __pycache__/app.cpython-314.pyc -- cache\n")
    rec = cleanup.run_cleanup_turn(backend, goal="add /health", repo=str(r), base_sha=base)  # maintain passes a str
    assert rec["deleted"] == ["health_check_tmp.py"]
    assert not (r / "health_check_tmp.py").exists()
    assert (r / "app.py").exists(), "a path the turn did not offer is untouchable"
    assert (r / "health.py").exists()
    _, kw = backend.calls[0]
    assert kw["tools"] is None and kw["temperature"] == 0.0


def test_emptied_cache_dir_is_pruned_and_staged_files_unstaged(repo):
    r, base = repo
    _git(r, "add", "-A")
    rec = cleanup.run_cleanup_turn(
        _Reply("- DELETE `__pycache__/app.cpython-314.pyc`"), goal="g",
        repo=r, base_sha=base)
    assert rec["deleted"] == ["__pycache__/app.cpython-314.pyc"]
    assert not (r / "__pycache__").exists()
    assert "pyc" not in _git(r, "diff", "--cached", "--name-only")


def test_no_candidates_means_no_model_call(repo):
    r, base = repo
    (r / "health_check_tmp.py").unlink()
    (r / "__pycache__" / "app.cpython-314.pyc").unlink()
    backend = _Reply("DELETE x")
    rec = cleanup.run_cleanup_turn(backend, goal="g", repo=r, base_sha=base)
    assert rec["candidates"] == [] and backend.calls == []


def test_backend_failure_leaves_the_tree_alone(repo):
    r, base = repo

    class Boom:
        def chat(self, *a, **k):
            raise RuntimeError("server gone")

    rec = cleanup.run_cleanup_turn(Boom(), goal="g", repo=r, base_sha=base)
    assert rec["deleted"] == [] and "server gone" in rec["error"]
    assert (r / "health_check_tmp.py").exists()


def test_maintain_gate_is_default_on_and_exact_zero_disables():
    """Default ON since 2026-10-18; only the exact string "0" turns it off
    (the default-ON grammar every promoted lever uses)."""
    src = Path(cleanup.__file__).with_name("maintain.py").read_text()
    assert 'os.environ.get("LUXE_CLEANUP_TURN", "1") != "0"' in src



# --- data-file litter (2026-11-20, acceptance/idle_wide_rerun_2026_11) -------

def _data_repo(tmp_path: Path) -> tuple[Path, str]:
    r = tmp_path / "d"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "pe_scan.py").write_text("GGUF_MAGIC = b'GGUF'\n")
    (r / "README.md").write_text("# scanner\nSee assets/logo.png\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r, _git(r, "rev-parse", "HEAD").strip()


def test_unreferenced_data_file_is_offered_with_a_binary_note(tmp_path):
    """The exact shape wide left behind: a fixture file in a new test dir that
    nothing names. The change itself (pe_scan.py) is not offered."""
    r, base = _data_repo(tmp_path)
    (r / "pe_scan.py").write_text("GGUF_MAGIC = b'GGUF'\nSTRICT = True\n")
    (r / "test_pe_scan").mkdir()
    (r / "test_pe_scan" / "fake.gguf").write_bytes(b"GGUF\x03\x00\x00\x00\x00")
    cands = cleanup.find_candidates(r, base)
    assert [(c.path, c.kind) for c in cands] == [("test_pe_scan/fake.gguf", "data")]
    user = cleanup.build_messages("add --strict", cands, r)[1]["content"]
    assert "test_pe_scan/fake.gguf (binary data file, 9 bytes)" in user
    removed = cleanup.delete_paths(r, cleanup.parse_deletions(
        "DELETE test_pe_scan/fake.gguf", cands))
    assert removed == ["test_pe_scan/fake.gguf"]
    assert not (r / "test_pe_scan").exists()          # emptied dir pruned


def test_data_file_named_anywhere_is_not_offered(tmp_path):
    r, base = _data_repo(tmp_path)
    (r / "assets").mkdir()
    (r / "assets" / "logo.png").write_bytes(b"\x89PNG\0")      # README names it
    (r / "fixtures").mkdir()
    (r / "fixtures" / "sample.gguf").write_bytes(b"GGUF\0")
    (r / "test_scan.py").write_text("PATH = 'fixtures/sample.gguf'\n")  # a test uses it
    paths = {c.path for c in cleanup.find_candidates(r, base)}
    assert "assets/logo.png" not in paths
    assert "fixtures/sample.gguf" not in paths


@pytest.mark.parametrize("name", [
    "CONFIG.md", "docs/guide.rst", "notes.txt", "settings.yaml", "pyproject.toml",
    "data.json", ".env.example", ".gitignore", "Dockerfile", "LICENSE", "schema.sql",
])
def test_docs_config_dotfiles_and_extensionless_are_never_data(tmp_path, name):
    r, base = _data_repo(tmp_path)
    (r / name).parent.mkdir(parents=True, exist_ok=True)
    (r / name).write_text("x\n")
    assert not any(c.kind == "data" for c in cleanup.find_candidates(r, base))


def test_text_data_file_gets_a_content_preview(tmp_path):
    r, base = _data_repo(tmp_path)
    (r / "out.log").write_text("debug: scanned 3 files\n")
    cands = cleanup.find_candidates(r, base)
    assert [(c.path, c.kind) for c in cands] == [("out.log", "data")]
    assert "debug: scanned 3 files" in cleanup.build_messages("g", cands, r)[1]["content"]


@pytest.mark.parametrize("line", [
    "DELETE verify_strict.py -- scratch verification script",
    "DELETE `verify_strict.py` - throwaway",
    "- DELETE verify_strict.py: temporary",
    "DELETE verify_strict.py",
])
def test_delete_line_may_carry_a_reason(line):
    cands = [cleanup.Candidate("verify_strict.py", "unreferenced"),
             cleanup.Candidate("keep_me.py", "unreferenced")]
    reply = f"{line}\nKEEP keep_me.py -- needed"
    assert cleanup.parse_deletions(reply, cands) == ["verify_strict.py"]


def test_reason_text_never_widens_what_is_deleted():
    cands = [cleanup.Candidate("a.py", "unreferenced")]
    assert cleanup.parse_deletions("DELETE a.py -- and also DELETE b.py", cands) == ["a.py"]
    assert cleanup.parse_deletions("DELETE b.py -- a.py", cands) == []


def test_several_delete_lines_each_count():
    """09c9ca7's widened pattern let `\\s` match the newline: the first DELETE
    line swallowed the rest, so only one file of several was deleted."""
    cands = [cleanup.Candidate(p, "unreferenced")
             for p in ("a.py", "dir/b.gguf", "c.py", "d.py")]
    reply = ("DELETE dir/b.gguf\nDELETE a.py -- scratch\n"
             "KEEP d.py -- needed\n- DELETE c.py\n")
    assert cleanup.parse_deletions(reply, cands) == ["dir/b.gguf", "a.py", "c.py"]
