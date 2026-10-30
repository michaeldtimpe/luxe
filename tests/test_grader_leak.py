"""Grader-leak pin: the model never sees how a fixture is graded.

Harvested from harness2's `assert_no_grader_verdicts` (2026-10-30,
acceptance/harvest_2026_10). A benchmark is only a measurement while the
agent cannot read its grading criteria: a regex it can see is a regex it can
satisfy without doing the work.

Three pins:

1. For every maintain_suite fixture, the champion's first request (built by
   the same stubbed-transport harness as test_golden_request.py, with the
   fixture's own goal and task type) carries no grader-authored string:
   no expected_outcome pattern/command, no requirement pattern/done_when.
2. On the default path (LUXE_REPROMPT_ON_DOC unset) the maintain pipeline
   never validates the spec, so nothing derived from it can reach a reprompt.
3. KNOWN EXPOSURE, pinned so it cannot change silently: with
   LUXE_REPROMPT_ON_DOC=1 the spec reprompt shows the model each unmet
   requirement's `done_when`, which describes the grading regex. Changing
   that is a deliberate decision; when it changes, update this test.

A failure here is a leak, not a false positive to narrow the search around.
Find the code path that put the string into the request.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from benchmarks.maintain_suite.fixtures import load_fixtures
from luxe import maintain
from luxe.agents.single import run_single
from luxe.backend import Backend
from luxe.config import load_config
from luxe.spec_validator import format_unsatisfied_for_reprompt
from luxe.tools import fs
from tests._golden_env import GOLDEN_SWITCHES

ROOT = Path(__file__).parents[1]
FIXTURES_YAML = ROOT / "benchmarks" / "maintain_suite" / "fixtures.yaml"

# Shorter strings collide with ordinary task wording ("--strict" is in both a
# goal and a pattern by design). Whole grader strings at or above this length
# have no business in a request.
_MIN_LEN = 12

_CANNED_REPLY = {
    "choices": [{"message": {"content": "done", "role": "assistant"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


def _fixtures():
    return load_fixtures(FIXTURES_YAML)


def _grader_strings(fx) -> list[tuple[str, str]]:
    """(field, value) for every grader-authored string on a fixture."""
    out: list[tuple[str, str]] = []
    eo = fx.expected_outcome or {}
    for key in ("pattern", "command"):
        if isinstance(eo.get(key), str):
            out.append((f"expected_outcome.{key}", eo[key]))
    for req in fx.requirements or []:
        rid = req.get("id", "?")
        for key in ("pattern", "done_when", "command"):
            if isinstance(req.get(key), str):
                out.append((f"requirements[{rid}].{key}", req[key]))
    return [(f, v) for f, v in out if len(v.strip()) >= _MIN_LEN]


def _first_request(tmp_path: Path, monkeypatch, goal: str, task_type: str) -> str:
    """The champion's first HTTP body for this goal, as sorted JSON text."""
    for name in GOLDEN_SWITCHES:
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "widget.py").write_text("def render():\n    return 'widget'\n")
    (repo / "README.md").write_text("# fixture\n")

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_CANNED_REPLY)

    backend = Backend(base_url="http://127.0.0.1:8000", model="leak-model", api_key="k")
    backend._client = httpx.Client(base_url=backend.base_url,
                                   transport=httpx.MockTransport(handler))
    role_cfg = load_config(ROOT / "configs" / "single_64gb.yaml").roles["monolith"]

    prev_root = fs.get_repo_root()
    fs.set_repo_root(repo)
    try:
        run_single(backend=backend, role_cfg=role_cfg, goal=goal,
                   task_type=task_type, languages=frozenset({"python"}),
                   run_id=None)
    finally:
        fs._REPO_ROOT = prev_root
    assert bodies, "run_single made no backend request"
    # json.dumps escapes backslashes, so compare against the decoded text of
    # every string in the body rather than the serialized blob.
    return "\n".join(_strings(bodies[0]))


def _strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return []


def test_fixture_set_has_grader_strings_to_check():
    """Guard the guard: an empty search set would pass vacuously."""
    fxs = _fixtures()
    assert len(fxs) >= 10
    assert sum(len(_grader_strings(f)) for f in fxs) >= 10


@pytest.mark.parametrize("fx", _fixtures(), ids=lambda f: f.id)
def test_first_request_carries_no_grader_string(fx, tmp_path, monkeypatch):
    body = _first_request(tmp_path, monkeypatch, fx.goal, fx.task_type)
    leaked = [(field, value) for field, value in _grader_strings(fx) if value in body]
    assert not leaked, (
        f"{fx.id}: grader-authored text reached the model's request: "
        + "; ".join(f"{field}={value!r}" for field, value in leaked)
        + ". This is a leak. Find the path that put it there."
    )


@pytest.mark.parametrize("fx", [f for f in _fixtures() if f.requirements],
                         ids=lambda f: f.id)
def test_default_path_never_validates_the_spec(fx, monkeypatch):
    """With LUXE_REPROMPT_ON_DOC unset, _validate_spec must return None
    without calling the validator, so no spec-derived text can be built."""
    monkeypatch.delenv("LUXE_REPROMPT_ON_DOC", raising=False)
    called = []
    monkeypatch.setattr("luxe.spec_validator.validate",
                        lambda *a, **k: called.append(1))
    p = SimpleNamespace(writes=True, repo_path="/nonexistent", emit=lambda *a, **k: None,
                        prep=SimpleNamespace(base_sha="0" * 40))
    assert maintain._validate_spec(p, fx.to_spec()) is None
    assert not called, "the spec validator ran on the default path"


def test_opt_in_spec_reprompt_exposes_done_when_KNOWN():
    """KNOWN EXPOSURE (see module docstring). The opt-in reprompt tells the
    model how an unmet requirement is graded. If this starts failing, the
    exposure was changed: confirm it was deliberate, then update this test."""
    fx = next(f for f in _fixtures() if f.requirements)
    spec = fx.to_spec()
    result = SimpleNamespace(
        unsatisfied=[SimpleNamespace(requirement=r, detail="0 matches")
                     for r in spec.requirements],
    )
    text = format_unsatisfied_for_reprompt(result)
    assert spec.requirements[0].done_when in text
    assert "Graded by:" in text
