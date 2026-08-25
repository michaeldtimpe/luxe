"""The `LUXE_*` environment switches `run_agent` reads, in one frozen record.

Sixteen `os.environ.get(...)` calls were spread through the first two hundred
lines of `run_agent`, each with its own default and its own malformed-value
fallback. Reading them in one place makes the run's configuration inspectable
and testable without driving the loop.

This is a MOVE, not a redesign. Same variables, same defaults, same fallbacks,
same read order, read at the same point in the run — `run_agent` still assigns
each field to the local name it used before, so nothing downstream changed.
No new switch belongs here without the `.sdd` entry that justifies it; the
opt-in modes each have invariants in `agents.sdd` that gate promotion.

Two `LUXE_*` reads that deliberately do NOT live here:
- `LUXE_TOKEN_LOG_INTERVAL`, read once at loop.py import (module constant).
- `LUXE_LOAD_PRIORS` and `LUXE_EARLY_BAIL_TRAJECTORY_SHAPE`, read by
  `cohort_priors` and `guardrails` respectively — they belong to those
  modules' own contracts, not to the loop's preamble.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from luxe.agents.convergence import _DEFAULT_MAX_DELTA
from luxe.agents.guardrails import _TRUNCATED_TURN_MAX_RETRIES
from luxe.context import (
    CALIBRATION_MAX,
    CALIBRATION_MIN,
    CALIBRATION_UNMEASURED_RATIO,
)

#: Fallback compaction trigger; also the value any out-of-band override
#: silently degrades to (see `from_env`).
DEFAULT_TIERED_COMPACT_THRESHOLD = 0.75

#: Default policy for the score<LOW early-bail suppression branch (v1.10.4).
DEFAULT_BAND_RESPONSE = "breadth_probe_hybrid"


@dataclass(frozen=True)
class RunFlags:
    """One `run_agent` invocation's environment-derived configuration."""

    # Telemetry
    suppress_tool_log: bool = False

    # Intervention gates (all opt-in; see agents.sdd)
    write_pressure: bool = False
    early_bail: bool = False
    early_bail_commit_only: bool = False
    prose_burst: bool = False
    action_density_gate: bool = False
    convergence_gate: bool = False
    early_bail_band_response: str = DEFAULT_BAND_RESPONSE

    # Count a post-write REPEAT call (same tool + same args as an earlier call
    # this run) toward the post-write idle streak. Opt-in: default OFF keeps
    # the benchmark path byte-identical until a maintain_suite run says
    # otherwise. See agents.sdd "post-write idle repeat counting".
    post_write_idle_repeats: bool = False

    # Treat a turn truncated at max_tokens with no tool call as recoverable
    # (nudge + continue) instead of terminal. DEFAULT-ON since 2026-08-10:
    # the A/B took maintain_suite 27/30 -> 30/30 with zero regressions.
    # Set to "0" to disable for ablation. See agents.sdd "Truncated-turn retry".
    truncated_turn_retry: bool = True

    # How many times that retry may fire in one run. Default 2 is the benched
    # value; each retry costs a full capped generation, so an interactive
    # session that wants a shorter leash lowers it rather than disabling the
    # mechanism. 0 = never fire (telemetry still records the truncation).
    truncated_turn_max_retries: int = _TRUNCATED_TURN_MAX_RETRIES

    # Treat a terminal turn with no tool call AND no text as recoverable
    # (nudge + continue) instead of a finished answer. DEFAULT-ON since
    # 2026-08-17: an empty completion was indistinguishable from a real one at
    # the loop's `if not tool_calls:` test, so a blank reply was recorded as a
    # clean turn. Set to "0" to disable for ablation. See agents.sdd
    # "Empty-completion retry".
    empty_turn_retry: bool = True

    # Calibrate context pressure against the server's own usage.prompt_tokens
    # instead of trusting the chars//4 estimate. DEFAULT-ON since 2026-08-11:
    # the estimate reads ~1.9x low on code + tool JSON, so compaction phases
    # fired at roughly double the context they name. Set to "0" to disable for
    # ablation. See agents.sdd "Server-truth context calibration".
    ctx_server_truth: bool = True

    # Damp the server-truth calibration when the prompt has outgrown the
    # sample the ratio was measured on. Opt-in: default OFF keeps the
    # benchmark path byte-identical until a maintain_suite run says
    # otherwise. See agents.sdd "Calibration extrapolation damping".
    ctx_cal_damp: bool = False

    # The ratio `damped_calibration` assumes for prompt material the live
    # calibration never saw. 1.2 is MEASURED (two recalibrations in one
    # markdown-heavy session, EVIDENCE.md finding 3) but it is still a free
    # parameter derived from one workload, so the promotion bench must be able
    # to sweep it without editing source — the C10 trap was a knob benched in a
    # form that could not move. Inert while `ctx_cal_damp` is off.
    ctx_cal_unmeasured_ratio: float = CALIBRATION_UNMEASURED_RATIO

    # Bound a SINGLE tool result at the moment it is appended to `messages`,
    # so one oversized `bash`/`grep`/MCP payload cannot blow the window past
    # anything compaction is allowed to touch. Opt-in: default OFF keeps the
    # benchmark path byte-identical until a maintain_suite run says
    # otherwise. See agents.sdd "Single tool-result clamp".
    tool_result_clamp: bool = False

    # Context compaction (default ON since 2026-05-28)
    tiered_compact: bool = True
    tiered_compact_threshold: float = DEFAULT_TIERED_COMPACT_THRESHOLD
    tiered_compact_phase_thresholds: tuple[float, float, float] | None = None

    # forge-hybrid Phase 3 (B1)
    respond_terminal: bool = False

    # v1.11 adaptive policy
    adaptive_policy: bool = False
    adaptive_no_write: bool = True
    adaptive_score_trend: bool = True
    adaptive_max_delta: float = _DEFAULT_MAX_DELTA

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RunFlags":
        """Read the switches. `env` defaults to `os.environ`.

        Every malformed value degrades SILENTLY to the documented default —
        a benchmark run must not die because a sweep script exported
        `LUXE_TIERED_COMPACT_THRESHOLD=high`.
        """
        e = os.environ if env is None else env

        # --- compaction thresholds: two knobs, the tuple wins when valid ---
        # Out-of-band (<=0 or >=1) or unparseable falls back to the default.
        try:
            threshold = float(e.get("LUXE_TIERED_COMPACT_THRESHOLD", "0.75"))
            if not (0.0 < threshold < 1.0):
                threshold = DEFAULT_TIERED_COMPACT_THRESHOLD
        except ValueError:
            threshold = DEFAULT_TIERED_COMPACT_THRESHOLD

        # Comma-separated "p1,p2,p3". Wrong arity or an out-of-band member
        # leaves it None, i.e. TieredCompact's own defaults apply.
        phase_thresholds: tuple[float, float, float] | None = None
        phase_raw = e.get("LUXE_TIERED_COMPACT_PHASE_THRESHOLDS", "")
        if phase_raw:
            try:
                parts = [float(x.strip()) for x in phase_raw.split(",")]
                if len(parts) == 3 and all(0.0 < p < 1.0 for p in parts):
                    phase_thresholds = (parts[0], parts[1], parts[2])
            except ValueError:
                pass

        # Retry bound: a non-negative int, anything else degrades silently to
        # the benched default (same contract as every other knob here).
        try:
            max_tt_retries = int(e.get("LUXE_TRUNCATED_TURN_MAX_RETRIES", ""))
            if max_tt_retries < 0:
                max_tt_retries = _TRUNCATED_TURN_MAX_RETRIES
        except ValueError:
            max_tt_retries = _TRUNCATED_TURN_MAX_RETRIES

        # Unmeasured-material ratio: same grammar and same silent degradation
        # as LUXE_TIERED_COMPACT_THRESHOLD above. Out of the pinned
        # [CALIBRATION_MIN, CALIBRATION_MAX] band or unparseable falls back to
        # the measured default, so a sweep script exporting junk costs a
        # default arm rather than a crashed run.
        try:
            unmeasured = float(e.get("LUXE_CTX_CAL_UNMEASURED_RATIO", ""))
            if not (CALIBRATION_MIN <= unmeasured <= CALIBRATION_MAX):
                unmeasured = CALIBRATION_UNMEASURED_RATIO
        except ValueError:
            unmeasured = CALIBRATION_UNMEASURED_RATIO

        # Empty string means "unset" here, not "zero" — hence the `or`.
        try:
            max_delta = float(
                e.get("LUXE_ADAPTIVE_MAX_INTENSITY_DELTA_PER_STEP", "")
                or _DEFAULT_MAX_DELTA
            )
        except ValueError:
            max_delta = _DEFAULT_MAX_DELTA

        return cls(
            suppress_tool_log=e.get("LUXE_SUPPRESS_TOOL_LOG") == "1",
            write_pressure=e.get("LUXE_WRITE_PRESSURE") == "1",
            early_bail=e.get("LUXE_EARLY_BAIL") == "1",
            early_bail_commit_only=e.get("LUXE_EARLY_BAIL_COMMIT_ONLY") == "1",
            prose_burst=e.get("LUXE_PROSE_BURST") == "1",
            action_density_gate=e.get("LUXE_ACTION_DENSITY_GATE") == "1",
            convergence_gate=e.get("LUXE_CONVERGENCE_GATE") == "1",
            post_write_idle_repeats=(
                e.get("LUXE_POST_WRITE_IDLE_REPEATS") == "1"
            ),
            # Default ON: only the exact string "0" disables it.
            truncated_turn_retry=(
                e.get("LUXE_TRUNCATED_TURN_RETRY", "1") != "0"
            ),
            truncated_turn_max_retries=max_tt_retries,
            # Default ON: only the exact string "0" disables it.
            empty_turn_retry=e.get("LUXE_EMPTY_TURN_RETRY", "1") != "0",
            # Default ON: only the exact string "0" disables it.
            ctx_server_truth=e.get("LUXE_CTX_SERVER_TRUTH", "1") != "0",
            # Opt-in: only the exact string "1" enables it.
            ctx_cal_damp=e.get("LUXE_CTX_CAL_DAMP") == "1",
            ctx_cal_unmeasured_ratio=unmeasured,
            # Opt-in: only the exact string "1" enables it.
            tool_result_clamp=e.get("LUXE_TOOL_RESULT_CLAMP") == "1",
            early_bail_band_response=e.get("LUXE_EARLY_BAIL_BAND_RESPONSE",
                                           DEFAULT_BAND_RESPONSE),
            # Default ON: only the exact string "0" disables it.
            tiered_compact=e.get("LUXE_TIERED_COMPACT", "1") != "0",
            tiered_compact_threshold=threshold,
            tiered_compact_phase_thresholds=phase_thresholds,
            respond_terminal=e.get("LUXE_RESPOND_TERMINAL") == "1",
            adaptive_policy=e.get("LUXE_ADAPTIVE_POLICY") == "1",
            # Per-signal ablations: ON unless explicitly set to something
            # other than "1" (so `=0` disables, unset keeps them on).
            adaptive_no_write=e.get("LUXE_ADAPTIVE_NO_WRITE", "1") == "1",
            adaptive_score_trend=e.get("LUXE_ADAPTIVE_SCORE_TREND", "1") == "1",
            adaptive_max_delta=max_delta,
        )
