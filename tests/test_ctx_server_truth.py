"""Server-truth context calibration (`LUXE_CTX_SERVER_TRUTH`).

`estimate_tokens` is `len(text) // 4`. That is close enough for prose and
badly low for what an agent loop actually carries — source code, JSON tool
arguments, tool results — where punctuation density pushes real tokens well
past a quarter of the characters. Measured on live sessions 2026-08-11:

    session 0e524f033300-6   last_prompt_tokens=17657  ctx_server=13.5%  ctx_est=7.0%   (1.93x)
    session 0e524f033300-16  last_prompt_tokens=13962  ctx_server=10.7%  ctx_est=5.8%   (1.84x)
    session 0e524f033300-15  last_prompt_tokens=4701   ctx_server=3.6%   ctx_est=1.6%   (2.25x)

Every compaction threshold divides by that estimate, so phases pinned at
0.50/0.85/0.95 were firing near 0.95/1.6/1.8 of the real window — i.e. phase 3
could not fire before the server rejected the prompt outright. The status bar
was corrected to server truth in June (`last_prompt_tokens / num_ctx`); the
thresholds were not.

The correction rides in the DENOMINATOR (`calibrated_ctx_limit`) so that
`TieredCompact`, `elide_old_tool_results` and `context_pressure` all inherit it
without learning about calibration, and the pinned phase thresholds keep their
validated values.
"""

from __future__ import annotations

from typing import Any

import pytest

from luxe.agents.loop import run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.context import (
    CALIBRATION_MAX,
    CALIBRATION_MIN,
    calibrated_ctx_limit,
    calibration_ratio,
    context_pressure,
)
from luxe.tools.base import ToolDef


class TestCalibrationRatio:
    def test_it_reports_how_far_the_estimate_ran_low(self):
        assert calibration_ratio(17657, 9175) == pytest.approx(1.92, abs=0.01)

    @pytest.mark.parametrize("actual,est", [(0, 100), (100, 0), (-5, 100), (100, -5)])
    def test_a_missing_reading_degrades_to_no_correction(self, actual, est):
        """A backend that reports no usage must fall back to the historical
        estimate-only behaviour, not to a wrong number."""
        assert calibration_ratio(actual, est) == 1.0

    def test_it_is_clamped_at_both_ends(self):
        """One malformed usage block must not swing compaction to an extreme."""
        assert calibration_ratio(10 ** 9, 1) == CALIBRATION_MAX
        assert calibration_ratio(1, 10 ** 9) == CALIBRATION_MIN


class TestCalibratedCtxLimit:
    def test_the_denominator_shrinks_by_the_undercount(self):
        assert calibrated_ctx_limit(131072, 2.0) == 65536

    def test_a_calibration_of_one_is_the_identity(self):
        assert calibrated_ctx_limit(131072, 1.0) == 131072

    def test_pressure_through_the_shrunk_limit_equals_the_true_ratio(self):
        """The whole point of folding it into the denominator: what comes out
        is `actual_tokens / num_ctx`, with no consumer changed."""
        msgs = [{"role": "user", "content": "x" * 40_000}]     # est 10,000
        actual = 19_000                                        # server truth
        cal = calibration_ratio(actual, 10_000)
        got = context_pressure(msgs, calibrated_ctx_limit(100_000, cal))
        assert got == pytest.approx(actual / 100_000, rel=0.01)

    @pytest.mark.parametrize("limit,cal", [(0, 2.0), (-1, 2.0), (1000, 0.0)])
    def test_degenerate_inputs_pass_the_limit_through(self, limit, cal):
        assert calibrated_ctx_limit(limit, cal) == limit

    @pytest.mark.parametrize("cal", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_calibration_is_the_identity(self, cal):
        """`int(nan)` raises. Unreachable through `calibration_ratio`'s clamp,
        but this is a public helper and a PRESSURE CALCULATION must never be
        the thing that kills a run (found by fuzzing, 2026-08-11)."""
        assert calibrated_ctx_limit(131072, cal) == 131072

    def test_the_clamp_keeps_non_finite_ratios_out_of_the_loop(self):
        """Belt and braces: the loop's only source of `calibration` is this
        function, so verify it can never emit one the helper would choke on."""
        import math
        for actual, est in [(1, 1), (10 ** 9, 1), (1, 10 ** 9), (0, 0), (-1, -1)]:
            r = calibration_ratio(actual, est)
            assert math.isfinite(r) and r > 0


# --- loop integration ------------------------------------------------------

class _Backend:
    """Reports a prompt_tokens the caller chooses, so the ratio is known."""

    def __init__(self, prompt_tokens: int, responses: list[ChatResponse]):
        self.prompt_tokens = prompt_tokens
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, **kwargs) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        r = self._responses.pop(0) if self._responses else _stop()
        return ChatResponse(
            text=r.text, tool_calls=r.tool_calls, finish_reason=r.finish_reason,
            timing=GenerationTiming(prompt_tokens=self.prompt_tokens,
                                    completion_tokens=10))


def _stop(text: str = "done") -> ChatResponse:
    return ChatResponse(text=text, finish_reason="stop",
                        timing=GenerationTiming(prompt_tokens=0, completion_tokens=0))


def _read() -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="r", name="read_file",
                                     arguments={"path": "a.py"})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=0, completion_tokens=0))


def _role() -> RoleConfig:
    return RoleConfig(model_key="test", num_ctx=10_000, max_steps=6,
                      max_tokens_per_turn=512, temperature=0.0)


def _tools() -> list[ToolDef]:
    return [ToolDef(name="read_file", description="read",
                    parameters={"type": "object",
                                "properties": {"path": {"type": "string"}},
                                "required": ["path"]})]


def _run(prompt_tokens: int, responses, caplog):
    backend = _Backend(prompt_tokens, responses)
    import logging
    with caplog.at_level(logging.DEBUG, logger="luxe.agents.loop"):
        result = run_agent(
            backend=backend, role_cfg=_role(),
            system_prompt="sys", task_prompt="task",
            tool_defs=_tools(),
            tool_fns={"read_file": lambda args: ("x" * 8_000, None)},
        )
    return backend, result, caplog.text


class TestLoopCalibration:
    def test_the_first_step_is_uncalibrated(self, caplog, monkeypatch):
        """Nothing to calibrate against before the first response — step 1 must
        read exactly as it always did."""
        monkeypatch.delenv("LUXE_CTX_SERVER_TRUTH", raising=False)
        _run(20_000, [_read(), _stop()], caplog)
        assert "step=1" in caplog.text
        assert "cal=1.00x" in caplog.text

    def test_later_steps_use_the_servers_number(self, caplog, monkeypatch):
        monkeypatch.delenv("LUXE_CTX_SERVER_TRUTH", raising=False)
        _, _, text = _run(20_000, [_read(), _read(), _stop()], caplog)
        # Step 2+ carries a correction, and it is not the identity.
        assert "step=2" in text
        assert "cal=1.00x" in text.split("step=2")[0]
        later = text.split("step=2")[1]
        assert "cal=" in later and "cal=1.00x" not in later.split("step=3")[0]

    def test_the_ablation_pins_calibration_at_one(self, caplog, monkeypatch):
        """`LUXE_CTX_SERVER_TRUTH=0` restores the estimate-only behaviour every
        pre-2026-08-11 benchmark ran under."""
        monkeypatch.setenv("LUXE_CTX_SERVER_TRUTH", "0")
        _, _, text = _run(20_000, [_read(), _read(), _stop()], caplog)
        assert "cal=1.00x" in text
        for chunk in text.split("step=")[1:]:
            assert "cal=1.00x" in chunk or "ctx_pressure" not in chunk

    def test_a_backend_reporting_no_usage_stays_uncalibrated(self, caplog,
                                                             monkeypatch):
        """Degrade to the estimate rather than to a wrong number."""
        monkeypatch.delenv("LUXE_CTX_SERVER_TRUTH", raising=False)
        _, _, text = _run(0, [_read(), _read(), _stop()], caplog)
        assert "cal=1.00x" in text

    def test_calibration_raises_reported_pressure(self, caplog, monkeypatch):
        """The user-visible consequence: the same conversation reads as more
        context used, because it always was."""
        monkeypatch.delenv("LUXE_CTX_SERVER_TRUTH", raising=False)
        _, on, _ = _run(20_000, [_read(), _read(), _stop()], caplog)
        caplog.clear()
        monkeypatch.setenv("LUXE_CTX_SERVER_TRUTH", "0")
        _, off, _ = _run(20_000, [_read(), _read(), _stop()], caplog)
        assert on.peak_context_pressure > off.peak_context_pressure
