"""Two opt-in context bounds, and the proof the default path is untouched.

`acceptance/chat_bigread_2026_08_24/` — four `luxe chat` turns lost across two
backends in ~30 minutes, all the same shape: one step opens two files, the
step's tool results alone exceed what the window can carry, the request is
dispatched anyway and retried verbatim until attempts are exhausted.

Two loop-level levers came out of it, BOTH opt-in and both default OFF:

- `LUXE_TOOL_RESULT_CLAMP` bounds a single tool result where it is created.
  Deliberately NOT a `TieredCompact` change: `agents.sdd` pins
  `messages[0]`/`messages[1]` and the last `keep_recent` assistant iterations
  as never eligible, and that invariant is load-bearing. `read_file` already
  has `LUXE_TOOL_BUDGET_CTX`; `bash`, `grep` and MCP tools have nothing.
- `LUXE_CTX_CAL_DAMP` stops a calibration measured on one prompt from being
  extrapolated wholesale onto a prompt forty times its size.

The first class of test in this module is the important one: with the flags
unset — and with every near-miss spelling of "on" — the messages the backend
receives are byte-identical to what it received before either flag existed.
"""

from __future__ import annotations

from typing import Any

import pytest

import luxe.agents.loop as loop_mod
from luxe.agents.loop import run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.tools.base import ToolDef

#: Every spelling that is NOT the single enabling string. The opt-in grammar
#: (`flags.py`) enables on the exact string "1" and nothing else.
NEAR_MISSES = ["", "0", "true", "yes", "2", "01", " 1", "1 ", "TRUE", "on"]


# --- harness ---------------------------------------------------------------

class _Backend:
    """Records every message list it is handed, and reports a chosen
    `prompt_tokens` so the calibration ratio is known exactly."""

    def __init__(self, script: list[ChatResponse], prompt_tokens: int = 0):
        self._script = list(script)
        self.prompt_tokens = prompt_tokens
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, **kwargs) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        r = self._script.pop(0) if self._script else _stop()
        return ChatResponse(
            text=r.text, tool_calls=r.tool_calls, finish_reason=r.finish_reason,
            timing=GenerationTiming(prompt_tokens=self.prompt_tokens,
                                    completion_tokens=10))


def _stop(text: str = "done") -> ChatResponse:
    return ChatResponse(text=text, finish_reason="stop",
                        timing=GenerationTiming(prompt_tokens=0,
                                                completion_tokens=0))


def _call(name: str, **args) -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="c1", name=name, arguments=args)],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=0, completion_tokens=0))


def _role(num_ctx: int = 131_072) -> RoleConfig:
    return RoleConfig(model_key="test", num_ctx=num_ctx, max_steps=4,
                      max_tokens_per_turn=512, temperature=0.0)


def _tool_defs() -> list[ToolDef]:
    return [
        ToolDef(name="bash", description="run",
                parameters={"type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"]}),
        ToolDef(name="grep", description="search",
                parameters={"type": "object",
                            "properties": {"pattern": {"type": "string"}},
                            "required": ["pattern"]}),
        ToolDef(name="read_file", description="read",
                parameters={"type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"]}),
    ]


#: `tools.fs`'s exact line numbering, so the clamp's resume offset is real.
def _numbered(n_lines: int, width: int = 60) -> str:
    return "".join(f"{i + 1}\t{'r' * width}\n" for i in range(n_lines))


BIG_BASH = "".join(f"out {i}\n" for i in range(40_000))     # ~360 KB
BIG_GREP = "".join(f"a.py:{i}: hit\n" for i in range(40_000))
BIG_READ = _numbered(6_000)                                  # ~372 KB
SMALL = "tiny\n"


def _tool_fns(payload: str = BIG_BASH):
    return {
        "bash": lambda args: (payload, None),
        "grep": lambda args: (BIG_GREP, None),
        "read_file": lambda args: (BIG_READ, None),
    }


def _run(script, *, tool_fns=None, prompt_tokens=0, num_ctx=131_072,
         system_prompt="sys"):
    backend = _Backend(script, prompt_tokens=prompt_tokens)
    result = run_agent(
        backend=backend, role_cfg=_role(num_ctx),
        system_prompt=system_prompt, task_prompt="task",
        tool_defs=_tool_defs(), tool_fns=tool_fns or _tool_fns(),
    )
    return backend, result


def _tool_contents(backend: _Backend) -> list[str]:
    """Every `role: "tool"` content the backend was ever shown."""
    return [m["content"]
            for call in backend.calls for m in call
            if m.get("role") == "tool"]


def _clear(monkeypatch):
    for var in ("LUXE_TOOL_RESULT_CLAMP", "LUXE_CTX_CAL_DAMP",
                "LUXE_CTX_CAL_UNMEASURED_RATIO"):
        monkeypatch.delenv(var, raising=False)


# --- the property that matters most: OFF is byte-identical -----------------

class TestDefaultPathIsUntouched:
    """Neither flag may change a single byte of the benchmark path until a
    maintain_suite run promotes it. These are the tests a reviewer reads."""

    def _baseline(self, monkeypatch):
        _clear(monkeypatch)
        backend, result = _run([_call("bash", command="ls"), _stop()],
                               prompt_tokens=3_102)
        return backend.calls, result

    def test_unset_environment_is_the_pre_2026_08_24_behaviour(self, monkeypatch):
        calls, _ = self._baseline(monkeypatch)
        # The oversized result reaches the model whole, exactly as before.
        assert BIG_BASH in [m["content"] for m in calls[-1]
                            if m.get("role") == "tool"]

    @pytest.mark.parametrize("var", ["LUXE_TOOL_RESULT_CLAMP",
                                     "LUXE_CTX_CAL_DAMP"])
    @pytest.mark.parametrize("value", NEAR_MISSES)
    def test_every_near_miss_spelling_leaves_the_run_byte_identical(
            self, monkeypatch, var, value):
        """Opt-IN grammar: only the exact string "1" enables. `""`, `"true"`,
        `"01"`, `" 1"` and `"0"` must all produce the identical request
        sequence — not merely a passing run."""
        baseline, base_result = self._baseline(monkeypatch)

        _clear(monkeypatch)
        monkeypatch.setenv(var, value)
        backend, result = _run([_call("bash", command="ls"), _stop()],
                               prompt_tokens=3_102)

        assert backend.calls == baseline
        assert result.peak_context_pressure == base_result.peak_context_pressure
        assert result.steps == base_result.steps

    @pytest.mark.parametrize("var", ["LUXE_TOOL_RESULT_CLAMP",
                                     "LUXE_CTX_CAL_DAMP"])
    def test_the_exact_string_one_is_the_only_thing_that_changes_anything(
            self, monkeypatch, var):
        baseline, base_result = self._baseline(monkeypatch)

        _clear(monkeypatch)
        monkeypatch.setenv(var, "1")
        backend, result = _run([_call("bash", command="ls"), _stop()],
                               prompt_tokens=3_102)

        changed = (backend.calls != baseline
                   or result.peak_context_pressure
                   != base_result.peak_context_pressure)
        assert changed, f"{var}=1 did nothing — the lever is dead"

    @pytest.mark.parametrize("value", ["1.0", "1.6", "2.4", "high", "9", ""])
    def test_the_sweep_knob_alone_never_turns_the_lever_on(self, monkeypatch,
                                                           value):
        """`LUXE_CTX_CAL_UNMEASURED_RATIO` exists so the promotion bench can
        sweep the constant. Exporting it — valid or junk — must leave a run
        byte-identical unless `LUXE_CTX_CAL_DAMP=1` is exported too, or the
        bench matrix cannot tell a swept arm from a default one."""
        baseline, base_result = self._baseline(monkeypatch)

        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_CTX_CAL_UNMEASURED_RATIO", value)
        backend, result = _run([_call("bash", command="ls"), _stop()],
                               prompt_tokens=3_102)

        assert backend.calls == baseline
        assert result.peak_context_pressure == base_result.peak_context_pressure

    def test_the_two_flags_are_independent(self, monkeypatch):
        """Enabling the clamp must not move the calibration, and vice versa."""
        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_TOOL_RESULT_CLAMP", "1")
        _, clamp_only = _run([_call("bash", command="ls"), _stop()],
                             prompt_tokens=3_102)
        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_CTX_CAL_DAMP", "1")
        damp_backend, _ = _run([_call("bash", command="ls"), _stop()],
                               prompt_tokens=3_102)

        assert BIG_BASH in _tool_contents(damp_backend)     # no clamping
        assert clamp_only.steps == 2


# --- 3.2 the clamp ---------------------------------------------------------

class TestToolResultClamp:
    """The value is `bash`, `grep` and MCP tools: they have no budget of any
    kind, and `TieredCompact` cannot reach the result that is hurting."""

    def test_an_oversized_bash_result_is_clipped_with_honest_text(
            self, monkeypatch):
        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_TOOL_RESULT_CLAMP", "1")
        backend, _ = _run([_call("bash", command="ls"), _stop()])

        sent = _tool_contents(backend)[-1]
        assert len(sent) < len(BIG_BASH)
        assert "truncated at" in sent
        assert "NOT recoverable" in sent          # no resume is implied
        assert "offset=" not in sent

    def test_an_oversized_grep_result_is_clipped_too(self, monkeypatch):
        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_TOOL_RESULT_CLAMP", "1")
        backend, _ = _run([_call("grep", pattern="x"), _stop()])

        sent = _tool_contents(backend)[-1]
        assert len(sent) < len(BIG_GREP)
        assert "grep" in sent and "offset=" not in sent

    def test_a_small_result_is_returned_untouched(self, monkeypatch):
        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_TOOL_RESULT_CLAMP", "1")
        backend, _ = _run([_call("bash", command="ls"), _stop()],
                          tool_fns={"bash": lambda a: (SMALL, None)})

        assert _tool_contents(backend)[-1] == SMALL

    def test_read_file_keeps_the_resume_shape_it_already_emits(
            self, monkeypatch):
        """`read_file` numbers its lines, so a TRUE offset can be recovered —
        the same `continue with read_file(path=…, offset=N, limit=N)` wording
        `tools/fs.py` uses when its own budget bites."""
        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_TOOL_RESULT_CLAMP", "1")
        backend, _ = _run([_call("read_file", path="self.md"), _stop()])

        sent = _tool_contents(backend)[-1]
        assert 'continue with read_file(path="self.md", offset=' in sent
        assert "limit=" in sent

    def test_it_does_not_touch_bytes_out_or_the_tool_record(self, monkeypatch):
        """The clamp is about what the CONTEXT carries. Conflating it with
        what the TOOL produced would make the corpus lie about tool
        behaviour."""
        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_TOOL_RESULT_CLAMP", "1")
        _, result = _run([_call("bash", command="ls"), _stop()])

        assert result.tool_calls[0].bytes_out == len(BIG_BASH.encode())
        assert result.tool_calls[0].result == BIG_BASH

    def test_it_emits_a_record_of_what_it_dropped(self, monkeypatch):
        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_TOOL_RESULT_CLAMP", "1")
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            loop_mod, "append_event",
            lambda run_id, kind, **f: events.append((kind, f)))

        backend = _Backend([_call("bash", command="ls"), _stop()])
        run_agent(backend=backend, role_cfg=_role(), system_prompt="s",
                  task_prompt="t", tool_defs=_tool_defs(),
                  tool_fns=_tool_fns(), run_id="rid")

        clamped = [f for k, f in events if k == "tool_result_clamped"]
        assert len(clamped) == 1
        assert clamped[0]["name"] == "bash"
        assert clamped[0]["chars_dropped"] > 0
        assert clamped[0]["max_chars"] > 0

    def test_the_schema_reject_and_dedup_messages_are_not_clamped(
            self, monkeypatch):
        """Neither runs `dispatch_tool`; both carry loop-authored constants
        (`validate_args` never echoes an argument value, the dedup sentence is
        literal). Clamping them could only ever be a no-op."""
        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_TOOL_RESULT_CLAMP", "1")
        backend, _ = _run([
            _call("bash"),                       # missing required `command`
            _call("bash", command="ls"),
            _call("bash", command="ls"),         # exact repeat -> dedup
            _stop(),
        ])
        contents = _tool_contents(backend)
        assert any(c.startswith("Schema error:") for c in contents)
        assert any(c.startswith("You already called bash") for c in contents)
        for c in contents:
            if c.startswith(("Schema error:", "You already called")):
                assert "truncated at" not in c


# --- 3.3 the compaction no-op, at loop level -------------------------------

class TestCompactionNoOpTelemetry:
    def test_a_phase_that_dropped_nothing_says_so_in_events(self, monkeypatch):
        """The `168f1825a1fd` shape end to end: one assistant message, a
        71,616-token estimate, phase 3 fires and achieves nothing because
        `_find_eligible_end` returned 2. Additive fields only — no flag."""
        _clear(monkeypatch)
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            loop_mod, "append_event",
            lambda run_id, kind, **f: events.append((kind, f)))

        backend = _Backend([_call("bash", command="ls"), _stop()],
                           prompt_tokens=3_102)
        run_agent(backend=backend, role_cfg=_role(), system_prompt="s" * 6_600,
                  task_prompt="t", tool_defs=_tool_defs(),
                  tool_fns=_tool_fns(), run_id="rid")

        fires = [f for k, f in events if k == "compaction_phase_reached"]
        assert fires, "expected a compaction fire on this trajectory"
        noop = fires[-1]
        assert noop["phase_reached"] == 3
        assert noop["tokens_before"] == noop["tokens_after"]
        assert noop["tool_results_dropped"] == 0
        # The two additive keys — this is the whole point.
        assert noop["effective"] is False
        assert noop["eligible_end"] == 2

        resolve = [f for k, f in events if k == "compaction_phase_at_resolve"]
        assert resolve[-1]["ineffective_fires"] >= 1
        # Nothing that existed before moved.
        assert resolve[-1]["max_phase_reached"] == 3
        assert resolve[-1]["tool_results_dropped_total"] == 0


# --- 4 the damping, at loop level ------------------------------------------

class TestCalibrationDamping:
    """`168f1825a1fd` replayed through `run_agent`: a ratio measured on a
    ~1,650-token step-1 prompt, applied to a 71,616-token step-2 prompt."""

    #: `s` * 6,600 estimates to ~1,650 tokens — the size of the step-1 prompt
    #: the ratio was measured on. `PROSE` is the 257,988 B `self.md` plus its
    #: companion, sized so the step-2 estimate lands on the recorded 71,616.
    SYSTEM = "s" * 6_600
    PROSE = "p" * 279_720

    SCRIPT = staticmethod(lambda: [ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="c1", name="bash",
                                     arguments={"command": "cat self.md"})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=0, completion_tokens=0)),
        _stop()])

    def _run_replay(self, monkeypatch, damp: bool):
        _clear(monkeypatch)
        if damp:
            monkeypatch.setenv("LUXE_CTX_CAL_DAMP", "1")
        return _run(self.SCRIPT(), prompt_tokens=3_102,
                    system_prompt=self.SYSTEM,
                    tool_fns={"bash": lambda a: (self.PROSE, None)})

    def _pressure(self, monkeypatch, damp: bool) -> float:
        _, result = self._run_replay(monkeypatch, damp)
        return result.peak_context_pressure

    def test_the_replay_reproduces_the_recorded_estimate(self, monkeypatch):
        """Guard the fixture itself: if these sizes drift, the two pressure
        assertions below stop meaning what they say."""
        from luxe.context import estimate_messages_tokens

        backend, _ = self._run_replay(monkeypatch, damp=False)
        assert estimate_messages_tokens(backend.calls[-1]) == pytest.approx(
            71_616, rel=0.02)

    def test_undamped_it_over_reports_past_one_hundred_percent(self,
                                                               monkeypatch):
        """The status quo, reproduced: a ~1.88x ratio measured on the tool
        schemas, applied to a prompt that is almost entirely prose."""
        assert self._pressure(monkeypatch, damp=False) > 1.0

    def test_damped_it_lands_near_the_prose_true_reading(self, monkeypatch):
        """~1.2x on 71,616 estimated tokens against a 128K window is ~65%.
        Not 102.5%, and no longer above the 0.95 phase-3 trigger."""
        got = self._pressure(monkeypatch, damp=True)
        assert 0.55 < got < 0.80
        assert got < 0.95

    def test_step_one_is_still_uncalibrated(self, monkeypatch, caplog):
        """Nothing to calibrate against before the first response — damping
        must not invent a correction there."""
        import logging

        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_CTX_CAL_DAMP", "1")
        with caplog.at_level(logging.DEBUG, logger="luxe.agents.loop"):
            self._run_replay(monkeypatch, damp=True)
        step1 = [ln for ln in caplog.text.splitlines()
                 if "step=1 ctx_pressure" in ln]
        assert step1 and "cal=1.00x" in step1[0]
        # Exactly one step was damped — step 2. Step 1 had nothing to damp.
        assert caplog.text.count("ctx calibration damped") == 1

    def test_it_says_out_loud_what_it_did(self, monkeypatch, caplog):
        """A silent correction is how the last one hid for months."""
        import logging

        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_CTX_CAL_DAMP", "1")
        with caplog.at_level(logging.DEBUG, logger="luxe.agents.loop"):
            self._run_replay(monkeypatch, damp=True)
        assert "ctx calibration damped" in caplog.text
        assert "measured at est=" in caplog.text

    def test_the_ablation_of_server_truth_disables_damping_too(self,
                                                              monkeypatch):
        """With `LUXE_CTX_SERVER_TRUTH=0` the calibration never leaves 1.0, so
        there is nothing to damp — the pre-2026-08-11 reading is restored
        whole, flag or no flag."""
        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_CTX_SERVER_TRUTH", "0")
        _, off = _run(self.SCRIPT(), prompt_tokens=3_102,
                      system_prompt=self.SYSTEM,
                      tool_fns={"bash": lambda a: (self.PROSE, None)})
        monkeypatch.setenv("LUXE_CTX_CAL_DAMP", "1")
        _, on = _run(self.SCRIPT(), prompt_tokens=3_102,
                     system_prompt=self.SYSTEM,
                     tool_fns={"bash": lambda a: (self.PROSE, None)})
        assert on.peak_context_pressure == off.peak_context_pressure

    @pytest.mark.parametrize("ratio,lo,hi", [
        ("1.0", 0.50, 0.60),      # decay all the way to the raw estimate
        ("1.2", 0.60, 0.70),      # the measured default
        ("1.6", 0.80, 0.92),      # a JSON/code-shaped assumption
    ])
    def test_the_sweep_reaches_the_loop(self, monkeypatch, ratio, lo, hi):
        """The knob has to move the number a bench arm reads, not just the
        pure function. Three arms, three distinct reported pressures."""
        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_CTX_CAL_DAMP", "1")
        monkeypatch.setenv("LUXE_CTX_CAL_UNMEASURED_RATIO", ratio)
        _, result = _run(self.SCRIPT(), prompt_tokens=3_102,
                         system_prompt=self.SYSTEM,
                         tool_fns={"bash": lambda a: (self.PROSE, None)})
        assert lo < result.peak_context_pressure < hi

    def test_a_malformed_sweep_value_costs_a_default_arm_not_a_crash(
            self, monkeypatch):
        _clear(monkeypatch)
        monkeypatch.setenv("LUXE_CTX_CAL_DAMP", "1")
        monkeypatch.setenv("LUXE_CTX_CAL_UNMEASURED_RATIO", "prose")
        _, result = _run(self.SCRIPT(), prompt_tokens=3_102,
                         system_prompt=self.SYSTEM,
                         tool_fns={"bash": lambda a: (self.PROSE, None)})
        # Degrades silently to the measured 1.2, exactly as a junk
        # LUXE_TIERED_COMPACT_THRESHOLD degrades to 0.75.
        assert result.peak_context_pressure == pytest.approx(
            self._pressure(monkeypatch, damp=True), rel=1e-9)

    def test_the_pinned_phase_thresholds_are_not_what_moved(self):
        """Phase 4 changes what the fraction MEANS, never the fraction
        (agents.sdd, 'Server-truth context calibration')."""
        from luxe.context import TieredCompact

        assert TieredCompact._DEFAULT_PHASE_THRESHOLDS == (0.50, 0.85, 0.95)
        assert TieredCompact().keep_recent == 3
