"""Tests for multi_turn BFCL (Phase 2): serializer, stateful executor, grader,
and the clean backend.chat driver.

Skips if the vendored multi_turn data isn't present (run scripts/fetch_bfcl_data.sh).
Backend is mocked — these are deterministic + offline.
"""

from __future__ import annotations

import copy

import pytest

from benchmarks.bfcl.adapter import (
    _bfcl_data_dir,
    load_ground_truth,
    load_problems,
    run_problem_multi_turn,
)
from benchmarks.bfcl.grade import grade_multi_turn
from benchmarks.bfcl.multi_turn.executor import (
    build_tool_surface,
    make_stateful_executor,
    to_call_string,
)
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse

try:
    _HAS_DATA = (_bfcl_data_dir() / "BFCL_v4_multi_turn_base.json").is_file()
except FileNotFoundError:
    _HAS_DATA = False

pytestmark = pytest.mark.skipif(
    not _HAS_DATA,
    reason="BFCL multi_turn_base data not vendored. Run scripts/fetch_bfcl_data.sh.",
)


def _base_problem(pid: str = "multi_turn_base_0") -> dict:
    for p in load_problems("multi_turn_base"):
        if p["id"] == pid:
            return p
    raise AssertionError(f"{pid} not found")


# --- serializer -------------------------------------------------------------

def test_to_call_string_types_and_ordering():
    args = {"folder": "document", "enabled": True, "missing": None,
            "meta": {"k2": 1, "k1": [2, 3]}}
    s = to_call_string("update", args)
    # eval-able literals + alphabetical arg + nested-dict key order
    assert s == "update(enabled=True, folder='document', meta={'k1': [2, 3], 'k2': 1}, missing=None)"
    # ordering-independent: a different insertion order yields the same string
    args2 = {"meta": {"k1": [2, 3], "k2": 1}, "missing": None,
             "folder": "document", "enabled": True}
    assert to_call_string("update", args2) == s


# --- stateful executor (fail-soft) ------------------------------------------

def test_executor_fail_soft_structured_error():
    p = _base_problem()
    _defs, tool_fns, _inst = build_tool_surface(p["involved_classes"], p["initial_config"])
    # valid call → result, no error
    res, err = tool_fns["cd"]({"folder": "document"})
    assert err is None and "current_working_directory" in res
    # bad argument → structured error string, NO raise
    res2, err2 = tool_fns["cd"]({"nonexistent": "x"})
    assert res2 == "" and err2 and err2.startswith("TypeError:")


def test_make_stateful_executor_catches_everything():
    class Boom:
        def explode(self):
            raise ValueError("kaboom")
    fn = make_stateful_executor(Boom(), "explode")
    res, err = fn({})
    assert res == "" and err == "ValueError: kaboom"


# --- grader (faithful via vendored checker) ---------------------------------

def test_grade_multi_turn_pass_and_fail():
    p = _base_problem()
    gt = load_ground_truth("multi_turn_base")[p["id"]]
    decoded_perfect = [[turn] for turn in gt]  # GT replayed, one step/turn
    r = grade_multi_turn(decoded_perfect, gt, p)
    assert r.passed is True and r.reason == "all_turns_matched"
    assert r.details is not None and r.details.get("valid") is True

    bad = copy.deepcopy(decoded_perfect)
    for step in bad:               # drop the last call of the first non-empty turn
        if step and step[0]:
            step[0] = step[0][:-1]
            break
    rb = grade_multi_turn(bad, gt, p)
    assert rb.passed is False and rb.reason != "all_turns_matched"


def test_grade_multi_turn_truncated_trajectory_grades_as_fail_not_error():
    """A trajectory shorter than GT (e.g. backend context-overflow aborted it) must grade
    as a failure, not IndexError the checker (was a long_context bug at small num_ctx)."""
    p = _base_problem()
    gt = load_ground_truth("multi_turn_base")[p["id"]]
    assert len(gt) >= 2
    truncated = [[gt[0]]]  # only the first turn decoded; GT has more
    r = grade_multi_turn(truncated, gt, p)
    assert r.passed is False
    assert not r.reason.startswith("checker_error")  # padded + graded, not crashed


def test_grade_multi_turn_replay_idempotent():
    """Same decoded_turns graded twice → identical verdict (no state leakage from
    the vendored executor's globals()-based instance persistence)."""
    p = _base_problem()
    gt = load_ground_truth("multi_turn_base")[p["id"]]
    decoded = [[turn] for turn in gt]
    r1 = grade_multi_turn(decoded, gt, p)
    r2 = grade_multi_turn(decoded, gt, p)
    assert (r1.passed, r1.reason) == (r2.passed, r2.reason) == (True, "all_turns_matched")


# --- GT shape (silent-failure guard) ----------------------------------------

def test_load_ground_truth_nested_shape():
    gt_map = load_ground_truth("multi_turn_base")
    gt = gt_map["multi_turn_base_0"]
    assert isinstance(gt, list) and isinstance(gt[0], list)        # per-turn lists
    assert all(isinstance(c, str) for c in gt[0])                  # call-strings, not flattened


# --- driver smoke (mocked backend) ------------------------------------------

class _ScriptedBackend:
    """Returns queued ChatResponses in order; empty (no tool_calls) once exhausted."""
    def __init__(self, scripted: list[ChatResponse]):
        self._q = list(scripted)

    def chat(self, **_kwargs) -> ChatResponse:
        if self._q:
            return self._q.pop(0)
        return ChatResponse(text="done", tool_calls=[], timing=GenerationTiming())


def test_driver_smoke_mock_backend_shape_and_transcript():
    p = _base_problem()  # 4 turns, TwitterAPI + GorillaFileSystem
    # Turn 0 step 0 emits a real call; everything else returns empty → turn ends.
    scripted = [ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="c1", name="cd", arguments={"folder": "document"})],
        timing=GenerationTiming(),
    )]
    result = run_problem_multi_turn(_ScriptedBackend(scripted), p)
    assert result.error == ""
    # decoded_turns aligns with the problem's turns; turn 0 captured the call.
    assert len(result.decoded_turns) == len(p["question"])
    assert result.decoded_turns[0][0] == ["cd(folder='document')"]
    # transcript preserved with system + user + assistant(tool_calls) + tool roles.
    roles = {m["role"] for m in result.transcript}
    assert {"system", "user", "assistant", "tool"} <= roles
    assistant = next(m for m in result.transcript if m["role"] == "assistant" and m.get("tool_calls"))
    assert assistant["tool_calls"][0]["function"]["name"] == "cd"
    # the live tool result was fed back as a role:tool message
    tool_msg = next(m for m in result.transcript if m["role"] == "tool")
    assert "current_working_directory" in tool_msg["content"]


# --- Part A: scoped per-class guidance ---------------------------------------

_GUIDANCE_MARKER = "File-system tips"


def _sysprompt(result) -> str:
    return result.transcript[0]["content"]


def test_class_guidance_off_by_default():
    p = _base_problem()  # involves GorillaFileSystem
    r = run_problem_multi_turn(_ScriptedBackend([]), p)
    assert _GUIDANCE_MARKER not in _sysprompt(r)  # flag unset → byte-identical to clean


def test_class_guidance_injected_when_flag_and_gfs(monkeypatch):
    monkeypatch.setenv("LUXE_MT_CLASS_GUIDANCE", "1")
    p = _base_problem()  # GorillaFileSystem present
    assert "GorillaFileSystem" in p["involved_classes"]
    r = run_problem_multi_turn(_ScriptedBackend([]), p)
    assert _GUIDANCE_MARKER in _sysprompt(r)


def test_class_guidance_scoped_absent_when_no_gfs(monkeypatch):
    monkeypatch.setenv("LUXE_MT_CLASS_GUIDANCE", "1")
    # a problem WITHOUT GorillaFileSystem → guidance must NOT be injected (scoped)
    p = next(x for x in load_problems("multi_turn_base")
             if "GorillaFileSystem" not in x["involved_classes"])
    r = run_problem_multi_turn(_ScriptedBackend([]), p)
    assert _GUIDANCE_MARKER not in _sysprompt(r)


# --- Part B: long_context generation/grading consistency --------------------

def test_long_context_extension_fires_in_generation():
    """generation must load the SAME extension scenario the grader uses for
    long_context (build_tool_surface long_context=True) — else state mismatch by
    construction."""
    import json as _json
    f = _bfcl_data_dir() / "BFCL_v4_multi_turn_long_context.json"
    if not f.is_file():
        pytest.skip("long_context data not vendored")
    probs = [_json.loads(l) for l in open(f)]
    p = next(x for x in probs if "GorillaFileSystem" in x["involved_classes"])
    _d, _fn, base = build_tool_surface(p["involved_classes"], p["initial_config"], long_context=False)
    _d2, _fn2, lc = build_tool_surface(p["involved_classes"], p["initial_config"], long_context=True)
    base_n = len(repr(vars(base["GorillaFileSystem"])["root"]))
    lc_n = len(repr(vars(lc["GorillaFileSystem"])["root"]))
    assert lc_n > base_n * 1.5  # extension materially enlarges the scenario
