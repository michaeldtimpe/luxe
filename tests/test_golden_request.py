"""Byte-identity guard for the benchmark/maintain request payload.

The project's hardest invariant is that the benchmark path stays
byte-identical unless a change deliberately targets it (CLAUDE.md,
`src/luxe/luxe.sdd`). Until now that was enforced by care. This module
mechanises it: it drives a real `Backend` with a stubbed transport, runs
`run_single` against a fixed synthetic repo, and asserts the **exact HTTP
body** oMLX would receive — model, messages (system + task prompt), the
full OpenAI tools array, and every sampling parameter.

Anything that perturbs the champion's first request fails here:

- a prompt registry edit (`agents/prompts.py`)
- a tool description / schema edit reachable from the bench surface
- a change to how `run_single` assembles `task_prompt` or the `.sdd` block
- a change to `Backend.chat`'s body assembly (`extra_body`, defaults)
- a change to the monolith role in `configs/single_64gb.yaml`

None of those are forbidden — they just have to be *deliberate*. When a
diff here is intended, regenerate with:

    LUXE_UPDATE_GOLDEN=1 uv run pytest tests/test_golden_request.py -q

and commit the snapshot change **in the same commit** as the code change,
so review sees the payload delta next to its cause.

The fixture repo is built in-test from literal strings (not the luxe
checkout) so the snapshot can never drift with unrelated repo edits, and
it carries a `.sdd` file so the SpecDD Lever 2 block is exercised too.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from luxe.agents import prompts as prompts_mod
from luxe.agents.single import run_single
from luxe.backend import Backend
from luxe.config import RoleConfig, load_config
from luxe.tools import fs


GOLDEN_DIR = Path(__file__).parent / "golden"
REQUEST_GOLDEN = GOLDEN_DIR / "run_single_request.json"
REGISTRY_GOLDEN = GOLDEN_DIR / "prompt_registry.json"

# Env vars that gate opt-in behaviour. Cleared so the snapshot describes the
# DEFAULT substrate regardless of what the developer has exported.
_GATED_ENV = (
    "LUXE_REFLECT",
    "LUXE_ADAPTIVE_POLICY",
    "LUXE_LOAD_PRIORS",
    "LUXE_RESPOND_TERMINAL",
    "LUXE_EARLY_BAIL",
    "LUXE_EARLY_BAIL_TRAJECTORY_SHAPE",
    "LUXE_EARLY_BAIL_COMMIT_ONLY",
    "LUXE_WRITE_PRESSURE",
    "LUXE_TIERED_COMPACT",
    "LUXE_TIERED_COMPACT_THRESHOLD",
    "LUXE_TIERED_COMPACT_PHASE_THRESHOLDS",
    "LUXE_SUPPRESS_TOOL_LOG",
    "LUXE_LOG_TOOL_CALLS",
    "LUXE_TOKEN_LOG_INTERVAL",
)

_SDD = """\
# src

Fixed contract for the golden-request fixture.

## Must
- Keep the widget registry sorted.

## Must not
- Introduce a second registry.

## Owns
- src/**

## Forbids
- vendor/**

## Forbids creating
- src/legacy_*.py
"""

# The `.sdd` lives at `src/src.sdd`, not the repo root: discovery requires the
# canonical `<dir>/<dir>.sdd` naming, and a root-level contract would have to be
# named after the tmpdir — which would make the snapshot machine-specific.
_FIXTURE_FILES = {
    "src/widget.py": "def render():\n    return 'widget'\n",
    "README.md": "# fixture\n\nA fixed repo for the golden-request snapshot.\n",
    "src/src.sdd": _SDD,
}


def _build_fixture_repo(root: Path) -> None:
    """Materialise the fixed synthetic repo (deterministic contents)."""
    for rel, body in sorted(_FIXTURE_FILES.items()):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


class _StubResponse:
    """Minimal stand-in for httpx.Response covering Backend.chat's usage."""

    status_code = 200
    text = ""

    def json(self) -> dict:
        return {
            "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }


def _capture_request_body(tmp_path: Path, monkeypatch) -> dict:
    """Run `run_single` once and return the first HTTP body verbatim."""
    for name in _GATED_ENV:
        monkeypatch.delenv(name, raising=False)

    repo = tmp_path / "fixture_repo"
    repo.mkdir(parents=True)
    _build_fixture_repo(repo)

    bodies: list[dict] = []

    backend = Backend(base_url="http://127.0.0.1:8000", model="golden-model", api_key="k")

    def fake_post(url: str, json: dict | None = None, **_):  # noqa: A002
        bodies.append(json)
        return _StubResponse()

    monkeypatch.setattr(backend._client, "post", fake_post)

    # The real champion role — the snapshot pins the benchmark request, so it
    # must reflect the shipped config, not a test-local invention.
    cfg = load_config(Path(__file__).parents[1] / "configs" / "single_64gb.yaml")
    role_cfg = cfg.roles["monolith"]

    prev_root = fs.get_repo_root()
    fs.set_repo_root(repo)
    try:
        run_single(
            backend=backend,
            role_cfg=role_cfg,
            goal="Add a docstring to render().",
            task_type="implement",
            languages=frozenset({"python"}),
            run_id=None,
        )
    finally:
        fs._REPO_ROOT = prev_root

    assert bodies, "run_single made no backend request"
    return bodies[0]


def _normalise(body: dict, repo: Path | None = None) -> dict:
    """Strip anything environment-dependent from the captured body.

    The `.sdd` block embeds repo-relative paths only, so nothing needs
    rewriting today; this hook exists so a future absolute-path leak fails
    loudly here rather than producing a snapshot that only matches on one
    machine.
    """
    out = json.loads(json.dumps(body, sort_keys=True))
    blob = json.dumps(out)
    assert str(Path.home()) not in blob, "request body leaked an absolute home path"
    assert "/private/var" not in blob and "/tmp/" not in blob, (
        "request body leaked a tmpdir path — the snapshot would be machine-specific"
    )
    return out


def _registry_snapshot() -> dict:
    """Digest every registered prompt + overlay, plus the full baseline pair.

    Digests keep the file small and stable; the two verbatim baseline strings
    make the common case (someone edited the champion's prompt) readable in
    the diff instead of an opaque hash change.
    """
    variants = {
        pid: {
            "system_sha256": hashlib.sha256(v.system.encode()).hexdigest(),
            "task_prefix_sha256": hashlib.sha256(v.task_prefix.encode()).hexdigest(),
            "system_len": len(v.system),
            "task_prefix_len": len(v.task_prefix),
        }
        for pid, v in sorted(prompts_mod.PROMPT_REGISTRY.items())
    }
    overlays = {
        oid: dict(sorted(o.by_task.items()))
        for oid, o in sorted(prompts_mod.TASK_OVERLAYS.items())
    }
    baseline = prompts_mod.get("baseline")
    return {
        "variants": variants,
        "overlays": overlays,
        "baseline_system": baseline.system,
        "baseline_task_prefix": baseline.task_prefix,
    }


def _assert_matches_golden(actual: dict, path: Path, label: str) -> None:
    rendered = json.dumps(actual, indent=2, sort_keys=True) + "\n"
    if os.environ.get("LUXE_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        pytest.skip(f"regenerated {path.name} (LUXE_UPDATE_GOLDEN=1)")
    if not path.exists():
        raise AssertionError(
            f"missing golden file {path}. If this is the first run, create it with "
            f"LUXE_UPDATE_GOLDEN=1 uv run pytest tests/test_golden_request.py -q"
        )
    expected = path.read_text()
    if rendered != expected:
        exp = json.loads(expected)
        diffs = _describe_diff(exp, actual)
        raise AssertionError(
            f"{label} changed vs {path.name}.\n"
            f"  {len(diffs)} difference(s):\n"
            + "\n".join(f"    - {d}" for d in diffs[:20])
            + "\n\n  If this change is INTENTIONAL, regenerate with:\n"
            "    LUXE_UPDATE_GOLDEN=1 uv run pytest tests/test_golden_request.py -q\n"
            "  and commit the snapshot alongside the code change.\n"
            "  If it is NOT intentional, you have perturbed the benchmark request."
        )


def _describe_diff(expected, actual, path: str = "") -> list[str]:
    """Human-readable leaf differences between two JSON-ish structures."""
    out: list[str] = []
    if type(expected) is not type(actual):
        return [f"{path or '<root>'}: type {type(expected).__name__} -> {type(actual).__name__}"]
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            sub = f"{path}.{key}" if path else key
            if key not in actual:
                out.append(f"{sub}: removed")
            elif key not in expected:
                out.append(f"{sub}: added")
            else:
                out.extend(_describe_diff(expected[key], actual[key], sub))
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            out.append(f"{path}: length {len(expected)} -> {len(actual)}")
        for i, (e, a) in enumerate(zip(expected, actual)):
            out.extend(_describe_diff(e, a, f"{path}[{i}]"))
    elif expected != actual:
        out.append(f"{path}: {_short(expected)} -> {_short(actual)}")
    return out


def _short(value, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def test_run_single_request_is_byte_identical(tmp_path, monkeypatch):
    """The champion's first request must match the committed snapshot exactly."""
    body = _capture_request_body(tmp_path, monkeypatch)
    _assert_matches_golden(_normalise(body), REQUEST_GOLDEN, "run_single request body")


def test_run_single_request_is_deterministic(tmp_path, monkeypatch):
    """Two identical invocations must produce identical bodies.

    Guards the snapshot itself: a body containing a timestamp, a set
    iteration order, or a tmpdir path would make the golden test flap
    instead of guard.
    """
    first = _normalise(_capture_request_body(tmp_path / "a", monkeypatch))
    second = _normalise(_capture_request_body(tmp_path / "b", monkeypatch))
    assert first == second


def test_prompt_registry_matches_golden():
    """Every registered prompt variant + overlay is pinned by digest."""
    _assert_matches_golden(_registry_snapshot(), REGISTRY_GOLDEN, "prompt registry")


def test_extra_context_default_does_not_perturb_the_body(tmp_path, monkeypatch):
    """`extra_context=""` (the chat seam's default) is the benchmark shape.

    Complements tests/test_single.py's task_prompt-level assertion by
    checking the property at the HTTP-body level, where it actually matters.
    """
    baseline = _capture_request_body(tmp_path / "base", monkeypatch)

    for name in _GATED_ENV:
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "explicit"
    repo.mkdir(parents=True)
    _build_fixture_repo(repo)
    bodies: list[dict] = []
    backend = Backend(base_url="http://127.0.0.1:8000", model="golden-model", api_key="k")
    monkeypatch.setattr(
        backend._client, "post",
        lambda url, json=None, **_: (bodies.append(json), _StubResponse())[1],
    )
    cfg = load_config(Path(__file__).parents[1] / "configs" / "single_64gb.yaml")
    prev_root = fs.get_repo_root()
    fs.set_repo_root(repo)
    try:
        run_single(
            backend=backend,
            role_cfg=cfg.roles["monolith"],
            goal="Add a docstring to render().",
            task_type="implement",
            languages=frozenset({"python"}),
            run_id=None,
            extra_context="",
        )
    finally:
        fs._REPO_ROOT = prev_root

    assert bodies[0] == baseline


def test_golden_request_covers_the_load_bearing_fields(tmp_path, monkeypatch):
    """Sanity: the snapshot is worth having.

    A snapshot that captured only `{"model": ...}` would pass forever while
    guarding nothing. Pin the structural expectations explicitly.
    """
    body = _capture_request_body(tmp_path, monkeypatch)
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    assert body["messages"][1]["content"].startswith("Task type: implement\n")
    assert "Repository contracts" in body["messages"][1]["content"], (
        "the .sdd block did not reach the prompt — fixture no longer exercises Lever 2"
    )
    assert "src/src.sdd" in body["messages"][1]["content"]
    assert body["temperature"] == 0.0, "champion runs greedy (temp=0)"
    assert body["stream"] is False, "benchmark path must never stream"
    assert body["extra_body"]["num_ctx"] == 32768
    names = {t["function"]["name"] for t in body["tools"]}
    assert {"read_file", "edit_file", "bash", "bm25_search"} <= names
    assert "cve_lookup" not in names, "cve_lookup is gated to task_type=manage"
