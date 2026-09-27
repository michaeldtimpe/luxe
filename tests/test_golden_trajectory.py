"""Byte-identity guard for MULTI-STEP agent-loop trajectories.

`tests/test_golden_request.py` pins the champion's FIRST request. Everything
the loop does after that — tool dispatch, the tool-call id scheme, dedup,
schema rejects, compaction, calibration, every nudge and exit guard — was
pinned only by per-mechanism unit tests, each asserting the one property it
cared about. This module pins the whole thing: it drives the real
`run_single` / `run_agent` through a real `Backend` whose transport replays a
SCRIPTED sequence of model responses, and records

- every HTTP body the loop sent (the messages the model would see, verbatim,
  plus a digest of the tools array),
- every `events.jsonl` record the run wrote (minus the wall-clock `ts`), in
  order, with field order preserved — `scripts/toolcall_taxonomy.py` and
  `agents/outcomes.py` parse these,
- the `AgentResult` summary.

Each scenario is committed as a snapshot under `tests/golden/trajectory/`.
The snapshots were RECORDED ON `origin/main` BEFORE the 2026-09 agent-loop
refactor (dead-mechanism removal + `LoopState`/`emit` extraction) so that
refactor could prove it changed no byte the model sees and no telemetry
record a consumer reads. Regenerate only for a deliberate change:

    LUXE_UPDATE_GOLDEN=1 uv run pytest tests/test_golden_trajectory.py -q

and commit the snapshot delta in the same commit as its cause.

Scenarios deliberately avoid `grep` (ripgrep's multi-file output order is
not deterministic, and its Python fallback formats differently) and every
network/subprocess tool; the fixture repo is built from literal strings.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from luxe.agents.loop import run_agent
from luxe.agents.single import run_single
from luxe.backend import Backend
from luxe.config import load_config
from luxe.run_state import run_dir
from luxe.spec import Requirement, Spec
from luxe.tools import fs
from luxe.tools.base import ToolDef

GOLDEN_DIR = Path(__file__).parent / "golden" / "trajectory"

# Every LUXE_* switch the loop (or anything it calls) reads. Cleared before
# each scenario so a snapshot describes exactly the env the scenario sets.
_ALL_LUXE_SWITCHES = (
    "LUXE_REFLECT", "LUXE_ADAPTIVE_POLICY", "LUXE_LOAD_PRIORS",
    "LUXE_RESPOND_TERMINAL", "LUXE_EARLY_BAIL", "LUXE_EARLY_BAIL_MODE",
    "LUXE_EARLY_BAIL_TRAJECTORY_SHAPE", "LUXE_EARLY_BAIL_COMMIT_ONLY",
    "LUXE_EARLY_BAIL_BAND_RESPONSE", "LUXE_WRITE_PRESSURE",
    "LUXE_PROSE_BURST", "LUXE_ACTION_DENSITY_GATE", "LUXE_CONVERGENCE_GATE",
    "LUXE_POST_WRITE_IDLE_REPEATS", "LUXE_TRUNCATED_TURN_RETRY",
    "LUXE_TRUNCATED_TURN_MAX_RETRIES", "LUXE_EMPTY_TURN_RETRY",
    "LUXE_CTX_SERVER_TRUTH", "LUXE_CTX_CAL_DAMP",
    "LUXE_CTX_CAL_UNMEASURED_RATIO", "LUXE_TOOL_RESULT_CLAMP",
    "LUXE_TIERED_COMPACT", "LUXE_TIERED_COMPACT_THRESHOLD",
    "LUXE_TIERED_COMPACT_PHASE_THRESHOLDS", "LUXE_ADAPTIVE_NO_WRITE",
    "LUXE_ADAPTIVE_SCORE_TREND", "LUXE_ADAPTIVE_MAX_INTENSITY_DELTA_PER_STEP",
    "LUXE_SUPPRESS_TOOL_LOG", "LUXE_LOG_TOOL_CALLS", "LUXE_TOOL_BUDGET_CTX",
)

# A deterministic ~9 KB file so a read of it moves context pressure enough
# for compaction to fire at the small windows the scenarios use.
_BIG = "".join(f"line {i:04d}: the quick brown fox jumps over the lazy dog\n"
               for i in range(160))

_FIXTURE_FILES = {
    "src/widget.py": "def render():\n    return 'widget'\n",
    "src/util.py": "def helper(x):\n    return x + 1\n",
    "src/config.py": "DEBUG = False\nNAME = 'fixture'\n",
    "docs/guide.md": "# Guide\n\nUse render() to draw a widget.\n",
    "README.md": "# fixture\n\nA fixed repo for the golden-trajectory snapshots.\n",
    "data/big.txt": _BIG,
}


def _build_fixture_repo(root: Path) -> None:
    for rel, body in sorted(_FIXTURE_FILES.items()):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


# --- scripted model responses ------------------------------------------------

def _call(name: str, args: Any, call_id: str = "") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _reply(content: str = "", calls: list[dict] | None = None, *,
           finish: str | None = None, completion: int = 60,
           reasoning: str = "") -> dict:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    if reasoning:
        msg["reasoning_content"] = reasoning
    return {"message": msg,
            "finish_reason": finish or ("tool_calls" if calls else "stop"),
            "completion": completion}


def _read(path: str, call_id: str = "", **extra) -> dict:
    return _call("read_file", {"path": path, **extra}, call_id)


def _ls(path: str = ".", call_id: str = "") -> dict:
    return _call("list_dir", {"path": path}, call_id)


def _glob(pattern: str, call_id: str = "") -> dict:
    return _call("glob", {"pattern": pattern}, call_id)


_DONE = _reply("All done.")


@dataclass
class Scenario:
    name: str
    script: list[dict]
    env: dict[str, str] = field(default_factory=dict)
    role: dict[str, Any] = field(default_factory=dict)
    # "single" = run_single on the champion role (real tool surface);
    # "agent"  = run_agent with a one-tool surface and a SpecDD spec (BFCL).
    runner: str = "single"
    spec: Spec | None = None
    task_type: str = "implement"


def _read_only_walk(n: int, completion: int) -> list[dict]:
    """n single-call, read-only steps with DISTINCT arguments (no dedup),
    cycling over paths that exist and paths that do not."""
    paths = ["src/widget.py", "src/util.py", "src/config.py", "docs/guide.md",
             "README.md"]
    out = []
    for i in range(n):
        kind = i % 4
        if kind == 0:
            c = _read(paths[(i // 4) % len(paths)], limit=10 + i)
        elif kind == 1:
            c = _ls(["src", "docs", "data", "."][(i // 4) % 4])
            # list_dir with the same path twice would dedup; vary via glob
            if i >= 16:
                c = _glob(f"*/*.{['py', 'md', 'txt'][i % 3]}{'' if i < 20 else '*'}")
        elif kind == 2:
            c = _glob(f"**/*{i}*")
        else:
            c = _read(f"src/missing_{i}.py")
        out.append(_reply(f"Looking at step {i}.", [c], completion=completion))
    return out


SCENARIOS: list[Scenario] = [
    # Shipped defaults. Exercises: the call-id scheme (explicit + synthesised),
    # multi-call steps, the dedup short-circuit, a schema reject, a non-object
    # arguments reject, an unknown tool, text-fallback drop, truncated-turn
    # retry, empty-turn retry, a write, TieredCompact + server-truth
    # calibration, and the post-write idle exit.
    Scenario(
        name="default_flags",
        role={"num_ctx": 3000, "max_steps": 14, "max_tokens_per_turn": 900},
        script=[
            _reply("Let me look at the widget.", [_read("src/widget.py")]),
            _reply("", [_read("data/big.txt", "a1"), _ls("src", "a2")],
                   completion=120),
            _reply("Checking again.", [
                _ls("src", "b1"),                      # dedup
                _call("read_file", {}, "b2"),          # schema reject
                _call("read_file", ["x"], "b3"),       # non-object args
                _call("no_such_tool", {"q": 1}, "b4"),  # unknown tool
            ]),
            _reply('I will call <tool_call>{"name": "frobnicate", '
                   '"arguments": {}}</tool_call> and then keep planning',
                   finish="length", completion=900),
            _reply("", finish="stop", completion=5, reasoning="thinking..."),
            _reply("Editing now.", [_call("edit_file", {
                "path": "src/widget.py",
                "old_string": "return 'widget'",
                "new_string": "return 'widget!'"})]),
            _reply("", [_read("src/missing_a.py")]),
            _reply("", [_read("src/missing_b.py"), _read("src/missing_c.py")]),
            _DONE,
        ],
    ),
    # Default flags, run ends on a plain answer after acting (terminal path),
    # with the two opt-in context levers on: result clamp + calibration damp.
    Scenario(
        name="clamp_and_damp",
        env={"LUXE_TOOL_RESULT_CLAMP": "1", "LUXE_CTX_CAL_DAMP": "1"},
        role={"num_ctx": 2500, "max_steps": 8, "max_tokens_per_turn": 900},
        script=[
            _reply("", [_read("src/widget.py")]),
            _reply("", [_read("data/big.txt")]),
            _reply("", [_ls("."), _read("README.md")]),
            _reply("The widget renders a string."),
        ],
    ),
    # Compaction OFF (the elide path), calibration OFF, post-write repeat
    # counting ON, and a stuck-loop abort from two consecutive dedup steps.
    Scenario(
        name="ablation_stuck_loop",
        env={"LUXE_TIERED_COMPACT": "0", "LUXE_CTX_SERVER_TRUTH": "0",
             "LUXE_POST_WRITE_IDLE_REPEATS": "1",
             "LUXE_TRUNCATED_TURN_RETRY": "0", "LUXE_EMPTY_TURN_RETRY": "0"},
        role={"num_ctx": 4000, "max_steps": 10},
        script=[
            _reply("", [_ls("src")]),
            _reply("", [_ls("src")]),
            _reply("", [_ls("src"), _glob("*.md")]),
            _DONE,
        ],
    ),
    # Every opt-in intervention in the static (non-soft_anchor) mode, with the
    # adaptive policy's observability on: early_bail (step 4), the action-
    # density gate (post-bail rescue), write_pressure, then the habituation
    # clean exit at step 20.
    Scenario(
        name="guards_static_habituation",
        env={"LUXE_WRITE_PRESSURE": "1", "LUXE_EARLY_BAIL": "1",
             "LUXE_ACTION_DENSITY_GATE": "1", "LUXE_ADAPTIVE_POLICY": "1"},
        role={"num_ctx": 32768, "max_steps": 26},
        script=_read_only_walk(24, completion=420) + [_DONE],
    ),
    # soft_anchor + convergence gate: a diffuse walk (score < LOW) takes the
    # suppression branch with the breadth_probe hybrid; the action-density
    # gate is live.
    Scenario(
        name="guards_soft_anchor_diffuse",
        env={"LUXE_EARLY_BAIL": "1", "LUXE_EARLY_BAIL_MODE": "soft_anchor",
             "LUXE_CONVERGENCE_GATE": "1", "LUXE_ACTION_DENSITY_GATE": "1",
             "LUXE_ADAPTIVE_POLICY": "1"},
        role={"num_ctx": 32768, "max_steps": 12},
        script=_read_only_walk(11, completion=300) + [_DONE],
    ),
    # soft_anchor + convergence gate on a CONVERGING walk (the same file read
    # over and over): commit_imperative fires, the action-density gate is
    # suppressed as converged.
    Scenario(
        name="guards_soft_anchor_converged",
        env={"LUXE_EARLY_BAIL": "1", "LUXE_EARLY_BAIL_MODE": "soft_anchor",
             "LUXE_CONVERGENCE_GATE": "1", "LUXE_ACTION_DENSITY_GATE": "1"},
        role={"num_ctx": 32768, "max_steps": 10},
        script=[_reply(f"Reading widget, pass {i}.",
                       [_read("src/widget.py", offset=0, limit=5 + i)],
                       completion=400)
                for i in range(9)] + [_DONE],
    ),
    # soft_anchor + commit_only: the mid band is suppressed.
    Scenario(
        name="guards_commit_only",
        env={"LUXE_EARLY_BAIL": "1", "LUXE_EARLY_BAIL_MODE": "soft_anchor",
             "LUXE_EARLY_BAIL_COMMIT_ONLY": "1"},
        role={"num_ctx": 32768, "max_steps": 7},
        script=_read_only_walk(6, completion=200) + [_DONE],
    ),
    # no_abstain mode, and the run exhausts max_steps.
    Scenario(
        name="no_abstain_max_steps",
        env={"LUXE_EARLY_BAIL": "1", "LUXE_EARLY_BAIL_MODE": "no_abstain"},
        role={"num_ctx": 32768, "max_steps": 6},
        script=_read_only_walk(8, completion=100),
    ),
    # SpecDD Lever 1, BFCL shape: an expects_zero_calls spec blocks dispatch.
    Scenario(
        name="spec_zero_calls",
        runner="agent",
        spec=Spec(goal="decline", requirements=[Requirement(
            id="R1", must="decline", done_when="no calls",
            kind="expects_zero_calls")]),
        env={"LUXE_WRITE_PRESSURE": "1", "LUXE_EARLY_BAIL": "1"},
        role={"num_ctx": 8192, "max_steps": 5},
        script=[
            _reply("Let me look it up.", [_call("lookup", {"q": "x"})]),
            _reply("That is out of scope for these tools."),
        ],
    ),
    # SpecDD Lever 1, BFCL shape: a min_tool_calls spec reprompts at the
    # would-be exit, once.
    Scenario(
        name="spec_min_calls",
        runner="agent",
        spec=Spec(goal="call twice", requirements=[Requirement(
            id="R1", must="call lookup twice", done_when=">=2 calls",
            kind="min_tool_calls", min_matches=2)]),
        role={"num_ctx": 8192, "max_steps": 6},
        script=[
            _reply("", [_call("lookup", {"q": "a"})]),
            _reply("Answer after one call."),
            _reply("", [_call("lookup", {"q": "b"})]),
            _reply("Answer after two calls."),
        ],
    ),
]


# --- harness -------------------------------------------------------------------

def _lookup_tool() -> ToolDef:
    return ToolDef(name="lookup", description="Look something up.",
                   parameters={"type": "object",
                               "properties": {"q": {"type": "string"}},
                               "required": ["q"]})


def _run_scenario(sc: Scenario, tmp_path: Path, monkeypatch) -> dict:
    for name in _ALL_LUXE_SWITCHES:
        monkeypatch.delenv(name, raising=False)
    for k, v in sc.env.items():
        monkeypatch.setenv(k, v)

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _build_fixture_repo(repo)

    bodies: list[dict] = []
    script = list(sc.script)

    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.content
        bodies.append(json.loads(raw))
        idx = len(bodies) - 1
        r = script[idx] if idx < len(script) else _DONE
        return httpx.Response(200, json={
            "choices": [{"message": r["message"],
                         "finish_reason": r["finish_reason"]}],
            # Deterministic, and deliberately NOT the chars//4 estimate, so
            # server-truth calibration moves.
            "usage": {"prompt_tokens": len(raw) // 3,
                      "completion_tokens": r["completion"]},
        })

    backend = Backend(base_url="http://127.0.0.1:8000", model="golden-model",
                      api_key="k")
    backend._client = httpx.Client(base_url=backend.base_url,
                                   transport=httpx.MockTransport(handler))

    run_id = f"golden-{sc.name}"
    events_path = run_dir(run_id) / "events.jsonl"
    events_path.unlink(missing_ok=True)  # a second run in one test appends
    prev_root = fs.get_repo_root()
    fs.set_repo_root(repo)
    try:
        if sc.runner == "single":
            cfg = load_config(Path(__file__).parents[1] / "configs" / "single_64gb.yaml")
            role_cfg = cfg.roles["monolith"].model_copy(update=sc.role)
            result = run_single(
                backend=backend, role_cfg=role_cfg,
                goal="Make render() return 'widget!'.",
                task_type=sc.task_type, languages=frozenset({"python"}),
                run_id=run_id,
            )
        else:
            from luxe.config import RoleConfig
            role_cfg = RoleConfig(model_key="golden-model", temperature=0.0,
                                  **sc.role)
            seen: list[dict] = []

            def lookup(args: dict) -> tuple[str, str | None]:
                seen.append(args)
                return f"result for {args.get('q')}", None

            result = run_agent(
                backend, role_cfg,
                system_prompt="You answer questions with the lookup tool.",
                task_prompt="Question: what is x?",
                tool_defs=[_lookup_tool()], tool_fns={"lookup": lookup},
                run_id=run_id, spec=sc.spec,
            )
    finally:
        fs._REPO_ROOT = prev_root

    events = []
    if events_path.is_file():
        for line in events_path.read_text().splitlines():
            rec = json.loads(line)
            rec.pop("ts", None)
            events.append(rec)

    snapshot = {
        "requests": [_request_view(b) for b in bodies],
        "events": events,
        "result": {
            "final_text": result.final_text,
            "steps": result.steps,
            "tool_calls_total": result.tool_calls_total,
            "schema_rejects": result.schema_rejects,
            "aborted": result.aborted,
            "abort_reason": result.abort_reason,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "last_prompt_tokens": result.last_prompt_tokens,
            "step_texts": result.step_texts,
            "peak_context_pressure": round(result.peak_context_pressure, 9),
            "final_context_pressure": round(result.final_context_pressure, 9),
            "tool_calls": [
                {"id": t.id, "name": t.name, "arguments": t.arguments,
                 "result": t.result, "error": t.error, "cached": t.cached,
                 "duplicate": t.duplicate, "bytes_out": t.bytes_out}
                for t in result.tool_calls
            ],
        },
    }
    text = json.dumps(snapshot, indent=1)
    for p in {str(repo), os.path.realpath(repo)}:
        text = text.replace(p, "<REPO>")
    out = json.loads(text)
    blob = json.dumps(out)
    assert str(tmp_path) not in blob and os.path.realpath(tmp_path) not in blob
    return out


def _request_view(body: dict) -> dict:
    """The request minus the (large, constant) tools array, which is kept as a
    digest. The first-request golden pins the tools array verbatim."""
    view = {k: v for k, v in body.items() if k != "tools"}
    tools = body.get("tools")
    view["tools_sha256"] = (
        hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()
        if tools is not None else None)
    return view


def _assert_golden(name: str, actual: dict) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    rendered = json.dumps(actual, indent=1, sort_keys=False, ensure_ascii=False) + "\n"
    if os.environ.get("LUXE_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        pytest.skip(f"regenerated {path.name} (LUXE_UPDATE_GOLDEN=1)")
    assert path.exists(), (
        f"missing golden {path}; create with LUXE_UPDATE_GOLDEN=1")
    expected = json.loads(path.read_text())
    # Compare section by section so a failure names what moved.
    for i, (e, a) in enumerate(zip(expected["requests"], actual["requests"])):
        assert a == e, f"{name}: request #{i} differs from the golden"
    assert len(actual["requests"]) == len(expected["requests"]), (
        f"{name}: request count {len(expected['requests'])} -> "
        f"{len(actual['requests'])}")
    for i, (e, a) in enumerate(zip(expected["events"], actual["events"])):
        assert list(a.items()) == list(e.items()), (
            f"{name}: event #{i} ({e.get('kind')}) differs (fields, values or "
            f"field ORDER)")
    assert [e["kind"] for e in actual["events"]] == \
        [e["kind"] for e in expected["events"]], f"{name}: event sequence differs"
    assert actual["result"] == expected["result"], f"{name}: AgentResult differs"
    # Finally the exact rendering, which also pins key order everywhere.
    assert rendered == path.read_text(), f"{name}: snapshot rendering differs"


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_trajectory_matches_golden(sc: Scenario, tmp_path, monkeypatch):
    _assert_golden(sc.name, _run_scenario(sc, tmp_path, monkeypatch))


def test_trajectory_is_deterministic(tmp_path, monkeypatch):
    """Guards the snapshots themselves: a flapping golden guards nothing."""
    sc = SCENARIOS[0]
    first = _run_scenario(sc, tmp_path / "a", monkeypatch)
    second = _run_scenario(sc, tmp_path / "b", monkeypatch)
    assert first == second


# The mechanisms each snapshot is supposed to exercise. If a scripted
# response stops reaching its target (a threshold moved, a tool changed
# its output), the snapshot would still match itself while silently no
# longer guarding the path — this pins the coverage, not just the bytes.
_EXPECTED_KINDS = {
    "default_flags": {"tool_call", "tool_reject", "textfallback_drop",
                      "truncated_turn_retry", "empty_turn_retry",
                      "compaction_phase_reached",
                      "compaction_phase_at_first_write",
                      "post_write_idle_exit", "compaction_phase_at_resolve",
                      "action_density_sample", "tool_step_done"},
    "clamp_and_damp": {"tool_result_clamped", "compaction_phase_reached"},
    "ablation_stuck_loop": {"tool_step_done"},
    "guards_static_habituation": {"early_bail_fired",
                                  "action_density_gate_fired",
                                  "write_pressure_fired", "habituation_exit",
                                  "adaptive_state"},
    "guards_soft_anchor_diffuse": {"early_bail_suppressed_diffuse",
                                   "early_bail_breadth_probe_fired",
                                   "adaptive_state"},
    "guards_soft_anchor_converged": {
        "early_bail_fired", "action_density_gate_suppressed_converged"},
    "guards_commit_only": {"early_bail_suppressed_commit_only"},
    "no_abstain_max_steps": {"early_bail_fired"},
    "spec_zero_calls": {"spec_predispatch_blocked"},
    "spec_min_calls": {"spec_reprompt_fired"},
}


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_scenario_still_exercises_its_mechanisms(sc: Scenario):
    path = GOLDEN_DIR / f"{sc.name}.json"
    if not path.exists():
        pytest.skip("golden not generated yet")
    kinds = {e["kind"] for e in json.loads(path.read_text())["events"]}
    missing = _EXPECTED_KINDS[sc.name] - kinds
    assert not missing, f"{sc.name} no longer reaches: {sorted(missing)}"
