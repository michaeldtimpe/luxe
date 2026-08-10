"""`RunFlags.from_env` must read exactly what `run_agent`'s preamble read.

Sixteen `os.environ.get(...)` calls with sixteen different default and
fallback conventions moved into one dataclass on 2026-08-04. These tests are
the field-by-field proof that nothing shifted: an unset environment gives the
documented defaults, "1"/"0" mean what they meant per variable, and every
malformed value degrades silently to the same value it degraded to before.

Malformed input matters more than it looks: these are read on the benchmark
path, and a sweep script exporting `LUXE_TIERED_COMPACT_THRESHOLD=high` must
cost a default, not a crashed run.
"""

from __future__ import annotations

import pytest

from luxe.agents.convergence import _DEFAULT_MAX_DELTA
from luxe.agents.flags import (
    DEFAULT_BAND_RESPONSE,
    DEFAULT_TIERED_COMPACT_THRESHOLD,
    RunFlags,
)


def test_empty_environment_gives_the_documented_defaults():
    f = RunFlags.from_env({})
    assert f.suppress_tool_log is False
    assert f.write_pressure is False
    assert f.early_bail is False
    assert f.early_bail_commit_only is False
    assert f.prose_burst is False
    assert f.action_density_gate is False
    assert f.convergence_gate is False
    assert f.post_write_idle_repeats is False
    assert f.respond_terminal is False
    assert f.adaptive_policy is False
    # The three that are NOT off-by-default:
    assert f.tiered_compact is True                       # default-ON 2026-05-28
    assert f.adaptive_no_write is True
    assert f.adaptive_score_trend is True
    assert f.tiered_compact_threshold == DEFAULT_TIERED_COMPACT_THRESHOLD
    assert f.tiered_compact_phase_thresholds is None
    assert f.adaptive_max_delta == _DEFAULT_MAX_DELTA
    assert f.early_bail_band_response == DEFAULT_BAND_RESPONSE


def test_from_env_defaults_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("LUXE_EARLY_BAIL", "1")
    assert RunFlags.from_env().early_bail is True


def test_the_record_is_frozen():
    with pytest.raises(Exception):
        RunFlags.from_env({}).early_bail = True          # type: ignore[misc]


# --- opt-in switches: only the exact string "1" turns them on ---------------

OPT_IN = [
    ("LUXE_SUPPRESS_TOOL_LOG", "suppress_tool_log"),
    ("LUXE_WRITE_PRESSURE", "write_pressure"),
    ("LUXE_EARLY_BAIL", "early_bail"),
    ("LUXE_EARLY_BAIL_COMMIT_ONLY", "early_bail_commit_only"),
    ("LUXE_PROSE_BURST", "prose_burst"),
    ("LUXE_ACTION_DENSITY_GATE", "action_density_gate"),
    ("LUXE_CONVERGENCE_GATE", "convergence_gate"),
    ("LUXE_RESPOND_TERMINAL", "respond_terminal"),
    ("LUXE_ADAPTIVE_POLICY", "adaptive_policy"),
    ("LUXE_POST_WRITE_IDLE_REPEATS", "post_write_idle_repeats"),
]


@pytest.mark.parametrize("var,field", OPT_IN)
@pytest.mark.parametrize("value,expected", [
    ("1", True), ("0", False), ("", False), ("true", False),
    ("yes", False), ("2", False), (" 1", False),
])
def test_opt_in_switches_require_exactly_one(var, field, value, expected):
    assert getattr(RunFlags.from_env({var: value}), field) is expected


# --- default-ON switches: the disabling spelling differs per variable -------

@pytest.mark.parametrize("value,expected", [
    ("0", False),          # the ONLY disabling value
    ("1", True), ("", True), ("no", True), ("false", True),
])
def test_tiered_compact_is_on_unless_it_is_exactly_zero(value, expected):
    assert RunFlags.from_env({"LUXE_TIERED_COMPACT": value}).tiered_compact is expected


@pytest.mark.parametrize("field,var", [
    ("adaptive_no_write", "LUXE_ADAPTIVE_NO_WRITE"),
    ("adaptive_score_trend", "LUXE_ADAPTIVE_SCORE_TREND"),
])
@pytest.mark.parametrize("value,expected", [
    ("1", True),
    ("0", False), ("", False), ("true", False),   # anything but "1" disables
])
def test_adaptive_per_signal_ablations_require_one_to_stay_on(field, var,
                                                              value, expected):
    """Note the asymmetry with LUXE_TIERED_COMPACT above, which only "0"
    disables. Both are preserved exactly as they were."""
    assert getattr(RunFlags.from_env({var: value}), field) is expected


# --- compaction thresholds --------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("0.5", 0.5),
    ("0.99", 0.99),
    ("0.75", 0.75),
    # out of band → default
    ("0", DEFAULT_TIERED_COMPACT_THRESHOLD),
    ("1", DEFAULT_TIERED_COMPACT_THRESHOLD),
    ("1.5", DEFAULT_TIERED_COMPACT_THRESHOLD),
    ("-0.5", DEFAULT_TIERED_COMPACT_THRESHOLD),
    # malformed → default, never an exception
    ("high", DEFAULT_TIERED_COMPACT_THRESHOLD),
    ("", DEFAULT_TIERED_COMPACT_THRESHOLD),
    ("0.5,0.6", DEFAULT_TIERED_COMPACT_THRESHOLD),
])
def test_compact_threshold_parsing(value, expected):
    got = RunFlags.from_env({"LUXE_TIERED_COMPACT_THRESHOLD": value})
    assert got.tiered_compact_threshold == expected


@pytest.mark.parametrize("value,expected", [
    ("0.50,0.85,0.95", (0.50, 0.85, 0.95)),
    (" 0.5 , 0.6 , 0.7 ", (0.5, 0.6, 0.7)),        # members are stripped
    # wrong arity → None (TieredCompact's own defaults apply)
    ("0.5,0.6", None),
    ("0.5,0.6,0.7,0.8", None),
    ("0.5", None),
    # an out-of-band member invalidates the whole tuple
    ("0.5,0.6,1.0", None),
    ("0.0,0.6,0.9", None),
    # malformed / absent
    ("a,b,c", None),
    ("", None),
])
def test_phase_threshold_parsing(value, expected):
    got = RunFlags.from_env({"LUXE_TIERED_COMPACT_PHASE_THRESHOLDS": value})
    assert got.tiered_compact_phase_thresholds == expected


def test_phase_thresholds_are_independent_of_the_single_knob():
    """Both may be set; the loop prefers the tuple. from_env just reports
    both, exactly as the two separate parse blocks did."""
    got = RunFlags.from_env({
        "LUXE_TIERED_COMPACT_THRESHOLD": "0.4",
        "LUXE_TIERED_COMPACT_PHASE_THRESHOLDS": "0.5,0.6,0.7",
    })
    assert got.tiered_compact_threshold == 0.4
    assert got.tiered_compact_phase_thresholds == (0.5, 0.6, 0.7)


# --- slew-rate limit --------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("0.5", 0.5),
    ("0", 0.0),                       # explicit zero IS honoured
    ("2", 2.0),                       # not range-checked, unlike the others
    ("-1", -1.0),
    ("", _DEFAULT_MAX_DELTA),         # empty means unset, not zero
    ("wide", _DEFAULT_MAX_DELTA),     # malformed → default
])
def test_adaptive_max_delta_parsing(value, expected):
    got = RunFlags.from_env(
        {"LUXE_ADAPTIVE_MAX_INTENSITY_DELTA_PER_STEP": value})
    assert got.adaptive_max_delta == expected


# --- band response is a free-form string, not a flag ------------------------

@pytest.mark.parametrize("value", ["silent", "breadth_probe_hybrid", "nonsense"])
def test_band_response_passes_through_unvalidated(value):
    """Unrecognised values are the loop's problem, not from_env's — matching
    the bare `os.environ.get(..., "breadth_probe_hybrid")` it replaced."""
    assert RunFlags.from_env(
        {"LUXE_EARLY_BAIL_BAND_RESPONSE": value}).early_bail_band_response == value


def test_band_response_empty_string_is_not_the_default():
    """`os.environ.get(name, default)` returns "" for an exported-but-empty
    variable. Preserved, quirk and all."""
    assert RunFlags.from_env({"LUXE_EARLY_BAIL_BAND_RESPONSE": ""}
                             ).early_bail_band_response == ""


# --- the loop actually uses it ---------------------------------------------

def test_run_agent_reads_its_switches_through_run_flags(monkeypatch):
    """Guards against a stray `os.environ.get` creeping back into the loop's
    preamble: patch RunFlags.from_env and the loop must obey the patch."""
    from luxe.agents import loop as loop_mod

    seen: list[bool] = []
    real = RunFlags.from_env

    def _spy(env=None):
        flags = real({"LUXE_TIERED_COMPACT": "0"})
        seen.append(flags.tiered_compact)
        return flags

    monkeypatch.setattr(loop_mod.RunFlags, "from_env", staticmethod(_spy))

    class _Backend:
        def chat(self, *a, **k):
            from luxe.backend import ChatResponse
            return ChatResponse(content="done", tool_calls=[])

    from luxe.config import RoleConfig

    role = RoleConfig(model_key="test", num_ctx=4096, max_steps=1,
                      max_tokens_per_turn=2048, temperature=0.0)
    loop_mod.run_agent(_Backend(), role, system_prompt="s", task_prompt="t",
                       tool_defs=[], tool_fns={})
    assert seen == [False]


def test_loop_preamble_has_no_direct_environ_reads():
    """The whole point of the extraction. `run_agent`'s body may not reach
    into os.environ — new switches go through RunFlags."""
    import ast
    import inspect

    from luxe.agents import loop as loop_mod

    tree = ast.parse(inspect.getsource(loop_mod.run_agent))
    offenders = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "environ"
    ]
    assert not offenders, (
        "run_agent reads os.environ directly again — add the switch to "
        "luxe/agents/flags.py instead."
    )
