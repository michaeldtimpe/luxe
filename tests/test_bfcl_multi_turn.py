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


# --- miss_func / miss_param: per-turn tool-withholding schedule --------------

import re as _re  # noqa: E402

_HAS_MISS = (_bfcl_data_dir() / "BFCL_v4_multi_turn_miss_func.json").is_file()
_CALL_RE = _re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\(")


def _call_name(call_str: str) -> str | None:
    m = _CALL_RE.match(call_str)
    return m.group(1).split(".")[-1] if m else None


class _RecordingBackend:
    """Captures the `tools` kwarg of every chat call; always returns an empty turn."""
    def __init__(self):
        self.tools_seen: list = []

    def chat(self, **kw) -> ChatResponse:
        self.tools_seen.append(kw.get("tools"))
        return ChatResponse(text="done", tool_calls=[], timing=GenerationTiming())


def test_miss_func_exposure_schedule_synthetic_boundary():
    """The highest-value indexing guard: a synthetic {1:[ls], 2:[mkdir]} schedule must
    expose `ls` AT turn 1 and `mkdir` AT turn 2 (strict `>` boundary), and hide the
    `excluded_function` for the whole conversation. Catches any off-by-one in reveal."""
    p = copy.deepcopy(_base_problem())
    assert len(p["question"]) >= 3
    full = {td.name for td in build_tool_surface(p["involved_classes"], p["initial_config"])[0]}
    assert {"ls", "mkdir", "cp"} <= full  # the names we schedule must exist on the surface
    p["missed_function"] = {"1": ["ls"], "2": ["mkdir"]}
    p["excluded_function"] = ["cp"]
    r = run_problem_multi_turn(_ScriptedBackend([]), p)
    exp = [set(t) for t in r.exposed_tool_names]
    assert all("cp" not in t for t in exp)                       # excluded: hidden always
    assert "ls" not in exp[0] and "mkdir" not in exp[0]          # turn 0: both held out
    assert "ls" in exp[1] and "mkdir" not in exp[1]              # turn 1: ls revealed only
    assert "ls" in exp[2] and "mkdir" in exp[2]                  # turn 2: both revealed


@pytest.mark.skipif(not _HAS_MISS, reason="miss_func data not vendored")
def test_miss_func_real_schedule_matches_holdout():
    """On a real miss_func problem, each single-key holdout fn is absent before its
    reveal turn and present from it onward; excluded fns are absent every turn."""
    probs = load_problems("multi_turn_miss_func")
    # pick a problem whose held-out fns each appear under exactly one reveal key
    def single_key(p):
        seen: dict[str, int] = {}
        for k, fs in (p.get("missed_function") or {}).items():
            for f in fs:
                if f in seen:
                    return False
                seen[f] = int(k)
        return bool(seen)
    p = next(x for x in probs if single_key(x))
    holdout = {int(k): set(v) for k, v in p["missed_function"].items()}
    reveal_of = {f: rv for rv, fs in holdout.items() for f in fs}
    excluded = set(p.get("excluded_function") or [])
    r = run_problem_multi_turn(_ScriptedBackend([]), p)
    exp = [set(t) for t in r.exposed_tool_names]
    for ti, names in enumerate(exp):
        for f, rv in reveal_of.items():
            if ti < rv:
                assert f not in names, f"{f} should be hidden at turn {ti} (reveal {rv})"
            else:
                assert f in names, f"{f} should be exposed at turn {ti} (reveal {rv})"
        assert excluded.isdisjoint(names)


@pytest.mark.skipif(not _HAS_MISS, reason="miss_func data not vendored")
def test_miss_func_gt_reachability_sample():
    """Independent of grading (which is exposure-agnostic): every GT call must be
    reachable under the driver's per-turn exposure for a sample of problems. Cross-checks
    schedule × turn-indexing × dataset. (multi_turn_miss_func_49 is a known upstream quirk
    — it uses a held-out fn before its reveal — and is excluded from this sample.)"""
    gtm = load_ground_truth("multi_turn_miss_func")
    probs = [p for p in load_problems("multi_turn_miss_func")
             if p["id"] != "multi_turn_miss_func_49"][:25]
    for p in probs:
        r = run_problem_multi_turn(_ScriptedBackend([]), p)
        exp = [set(t) for t in r.exposed_tool_names]
        gt = gtm.get(p["id"]) or []
        for t, turn in enumerate(gt):
            for cs in turn:
                nm = _call_name(cs)
                if nm is None or t >= len(exp):
                    continue
                assert nm in exp[t], f"{p['id']} turn {t}: GT call {nm} not exposed"


@pytest.mark.skipif(not _HAS_MISS, reason="miss_func data not vendored")
def test_miss_func_49_known_quirk_reproduced():
    """The lone upstream inconsistency: miss_func_49 holds out `tail` until turn 3 but
    its GT uses `tail` at turn 1. Faithful reproduction = `tail` hidden at turn 1
    (the model cannot call it early, exactly as the official harness would have it)."""
    p = next(x for x in load_problems("multi_turn_miss_func") if x["id"] == "multi_turn_miss_func_49")
    r = run_problem_multi_turn(_ScriptedBackend([]), p)
    exp = [set(t) for t in r.exposed_tool_names]
    assert "tail" not in exp[1] and "tail" not in exp[0]   # held out before reveal=3
    assert "tail" in exp[3]                                # exposed at the reveal turn


def test_base_no_withholding_field_is_full_and_byte_identical_every_turn():
    """Regression: a base problem carrying NEITHER withholding field must pass the FULL
    serialized surface to backend.chat on every turn (asserted on the actual `tools`
    kwarg, not an empty-hidden-set proxy). 182/200 base problems are in this class."""
    p = next(x for x in load_problems("multi_turn_base")
             if not x.get("excluded_function") and not x.get("missed_function"))
    full = [td.to_openai() for td in build_tool_surface(p["involved_classes"], p["initial_config"])[0]]
    full_names = [td.name for td in build_tool_surface(p["involved_classes"], p["initial_config"])[0]]
    rec = _RecordingBackend()
    r = run_problem_multi_turn(rec, p)
    assert rec.tools_seen and all(t == full for t in rec.tools_seen)   # every chat saw full surface
    assert r.exposed_tool_names and all(n == full_names for n in r.exposed_tool_names)


def test_excluded_function_applied_uniformly_including_base():
    """Faithfulness: `excluded_function` is removed for the WHOLE conversation in EVERY
    multi_turn category, including base (18/200 base problems carry it — upstream excludes
    them; base GT never calls them). The pre-refactor driver ignored this field."""
    p = next(x for x in load_problems("multi_turn_base") if x.get("excluded_function"))
    excluded = set(p["excluded_function"])
    full_names = {td.name for td in build_tool_surface(p["involved_classes"], p["initial_config"])[0]}
    assert excluded & full_names  # the excluded fn really is on the unfiltered surface
    r = run_problem_multi_turn(_ScriptedBackend([]), p)
    for names in r.exposed_tool_names:
        assert excluded.isdisjoint(names)  # hidden every turn


# --- Phase 2: gated reflect→repair stage ------------------------------------

from luxe.agents import reflect  # noqa: E402
from luxe.agents.reflect import Deficiency, Verdict  # noqa: E402


def _one_turn_problem() -> dict:
    """A base problem truncated to a single user turn — makes the two-gate repair
    assertions exact (no trailing turns to also trigger the gate)."""
    p = copy.deepcopy(_base_problem())
    p["question"] = p["question"][:1]
    return p


def _cd_call() -> ChatResponse:
    return ChatResponse(
        text="", timing=GenerationTiming(),
        tool_calls=[ToolCallResponse(id="c1", name="cd", arguments={"folder": "document"})],
    )


def test_repair_off_by_default_never_verifies(monkeypatch):
    """Flag off → the verify call is never made and no repair fires (byte-identical)."""
    monkeypatch.delenv("LUXE_REFLECT", raising=False)
    calls: list = []
    monkeypatch.setattr(reflect, "verify", lambda *a, **k: calls.append(1) or Verdict(gap=True))
    r = run_problem_multi_turn(_ScriptedBackend([]), _one_turn_problem())  # empty → give-up
    assert calls == []
    assert r.repair_turns == []


def test_repair_fires_on_giveup_and_gap(monkeypatch):
    monkeypatch.setenv("LUXE_REFLECT", "1")
    verdict = Verdict(gap=True, deficiencies=(Deficiency("create the report", "", "concrete_local"),))
    monkeypatch.setattr(reflect, "verify", lambda *a, **k: verdict)
    # turn 0: give-up (empty); repair re-prompt then emits a real call.
    scripted = [ChatResponse(text="I cannot do that", tool_calls=[], timing=GenerationTiming()), _cd_call()]
    r = run_problem_multi_turn(_ScriptedBackend(scripted), _one_turn_problem())
    assert r.repair_turns == [0]
    flat = [c for step in r.decoded_turns[0] for c in step]
    assert any("cd(" in c for c in flat)  # repair output appended to the SAME turn
    nudge = next((m for m in r.transcript if m.get("_luxe_repair")), None)
    assert nudge is not None and nudge["role"] == "user"
    assert "create the report" in nudge["content"]  # consumes the verdict's deficiency


def test_repair_skipped_on_nonempty_turn(monkeypatch):
    """The structural gate: a turn that ACTED is never sent to verify (skips the
    reporting-gap false-gaps, which have non-empty action sets)."""
    monkeypatch.setenv("LUXE_REFLECT", "1")
    calls: list = []
    monkeypatch.setattr(reflect, "verify", lambda *a, **k: calls.append(1) or Verdict(gap=True))
    r = run_problem_multi_turn(_ScriptedBackend([_cd_call()]), _one_turn_problem())
    assert calls == []          # verify not invoked on a non-empty turn
    assert r.repair_turns == []


def test_repair_skipped_when_verify_abstains(monkeypatch):
    """Give-up signature present but verify returns gap=false → no repair."""
    monkeypatch.setenv("LUXE_REFLECT", "1")
    monkeypatch.setattr(reflect, "verify", lambda *a, **k: Verdict(gap=False))
    r = run_problem_multi_turn(_ScriptedBackend([]), _one_turn_problem())
    assert r.repair_turns == []
    assert not any(m.get("_luxe_repair") for m in r.transcript)


def test_repair_skipped_on_unparseable_verdict(monkeypatch):
    """A verify call error/unparseable verdict fails CLOSED → no spurious repair."""
    monkeypatch.setenv("LUXE_REFLECT", "1")
    monkeypatch.setattr(reflect, "verify", lambda *a, **k: Verdict(gap=False, error="verify_call_failed"))
    r = run_problem_multi_turn(_ScriptedBackend([]), _one_turn_problem())
    assert r.repair_turns == []


def test_repair_stays_on_same_tool_surface(monkeypatch):
    """The repair re-prompt must use the give-up turn's exposed surface — a held-out
    fn stays hidden, and every generation chat in the turn sees an identical surface."""
    monkeypatch.setenv("LUXE_REFLECT", "1")
    monkeypatch.setattr(reflect, "verify", lambda *a, **k: Verdict(gap=True))
    p = _one_turn_problem()
    full = sorted(td.name for td in build_tool_surface(p["involved_classes"], p["initial_config"])[0])
    held = next(n for n in full if n != "cd")
    p["missed_function"] = {"1": [held]}  # revealed at turn 1 → hidden at turn 0

    class _Rec:
        def __init__(self): self.surfaces: list[list[str]] = []
        def chat(self, **kw) -> ChatResponse:
            self.surfaces.append([t["function"]["name"] for t in (kw.get("tools") or [])])
            # call #2 is the repair re-prompt → emit one real call; else empty.
            return _cd_call() if len(self.surfaces) == 2 else ChatResponse(
                text="", tool_calls=[], timing=GenerationTiming())

    rec = _Rec()
    r = run_problem_multi_turn(rec, p)
    assert r.repair_turns == [0]
    assert all(held not in names for names in rec.surfaces)          # held fn never exposed
    assert len({tuple(s) for s in rec.surfaces}) == 1               # identical surface across the turn


@pytest.mark.skipif(not _HAS_MISS, reason="miss_func/miss_param data not vendored")
def test_grade_accepts_miss_categories_gt_as_pred():
    """The vendored checker accepts the new test_category strings and grades GT-as-pred
    as a pass (grading is exposure-agnostic — confirms category wiring, not generation)."""
    for cat in ("multi_turn_miss_func", "multi_turn_miss_param"):
        gtm = load_ground_truth(cat)
        for p in load_problems(cat)[:10]:
            gt = gtm.get(p["id"]) or []
            r = grade_multi_turn([[turn] for turn in gt], gt, p)
            assert r.passed, (cat, p["id"], r.reason)
