"""Pure pieces of scripts/opencode_harness.py (no opencode, no model)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "opencode_harness", ROOT / "scripts" / "opencode_harness.py")
oh = importlib.util.module_from_spec(_spec)
sys.modules["opencode_harness"] = oh
_spec.loader.exec_module(oh)

from benchmarks.maintain_suite.grade import Fixture  # noqa: E402


def _fx(goal: str = "Do the thing.") -> Fixture:
    return Fixture.from_dict({"id": "fx-1", "goal": goal, "task_type": "document",
                              "expected_outcome": {"kind": "regex_present"}})


def test_prompt_is_goal_plus_one_neutral_line():
    p = oh.build_prompt(_fx("  Update README.md.\n"))
    assert p == "Update README.md.\n\n" + oh.NEUTRAL_LINE
    assert p.count("\n\n") == 1


def test_config_provider_shape():
    c = oh.make_opencode_config("Qwen3.6-35B-A3B-4bit", "http://127.0.0.1:8000/v1")
    assert c["model"] == "luxeab/Qwen3.6-35B-A3B-4bit"
    prov = c["provider"]["luxeab"]
    assert list(c["provider"]) == ["luxeab"]
    assert prov["npm"] == "@ai-sdk/openai-compatible"
    assert prov["options"]["baseURL"] == "http://127.0.0.1:8000/v1"
    # key is an env reference, never a literal in the file
    assert prov["options"]["apiKey"] == "{env:LUXEAB_API_KEY}"
    m = prov["models"]["Qwen3.6-35B-A3B-4bit"]
    assert m["tool_call"] is True
    assert m["limit"] == {"context": 65536, "output": 8192}
    assert m["options"]["temperature"] == 0
    assert c["autoupdate"] is False and c["share"] == "disabled"


def test_env_isolates_home_and_xdg(tmp_path):
    base = {"PATH": "/usr/bin", "HOME": "/Users/real",
            "XDG_CONFIG_HOME": "/Users/real/.config",
            "OPENCODE_CONFIG_DIR": "/Users/real/.opencode"}
    cfg = tmp_path / "opencode.config.json"
    env = oh.make_env(base, tmp_path / "iso", cfg, "secret")
    for var in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                "XDG_STATE_HOME"):
        assert env[var].startswith(str(tmp_path / "iso")), var
    assert env["OPENCODE_CONFIG"] == str(cfg)
    assert env["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
    assert "OPENCODE_CONFIG_DIR" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["LUXEAB_API_KEY"] == "secret"
    assert base["HOME"] == "/Users/real"  # caller's dict untouched


def test_env_dummy_key_when_unset(tmp_path):
    env = oh.make_env({}, tmp_path, tmp_path / "c.json", "")
    assert env["LUXEAB_API_KEY"]  # non-empty dummy


def _ev(typ, **part):
    return json.dumps({"type": typ, "sessionID": "ses_abc", "part": part})


def test_parse_events_counts_tools_and_tokens():
    lines = [
        _ev("step_start"),
        _ev("tool_use", tool="read", state={"status": "completed"}),
        _ev("tool_use", tool="read", state={"status": "completed"}),
        _ev("tool_use", tool="edit", state={"status": "error"}),
        _ev("step_finish", reason="tool-calls",
            tokens={"input": 100, "output": 10, "reasoning": 1,
                    "cache": {"read": 50, "write": 0}}),
        "not json",
        "",
        _ev("text", text="done"),
        _ev("step_finish", reason="stop",
            tokens={"input": 20, "output": 5, "cache": {"read": 8}}),
    ]
    d = oh.parse_events(lines)
    assert d["session_id"] == "ses_abc"
    assert d["tool_calls"] == {"read": 2, "edit": 1}
    assert d["tool_calls_total"] == 3
    assert d["tool_errors"] == {"edit": 1}
    assert d["steps"] == 2
    assert d["finish_reasons"] == {"tool-calls": 1, "stop": 1}
    assert d["tokens"] == {"input": 120, "output": 15, "reasoning": 1,
                           "cache_read": 58, "cache_write": 0}
    assert d["malformed_lines"] == 1


def test_parse_events_empty_stream():
    d = oh.parse_events([])
    assert d["tool_calls_total"] == 0 and d["session_id"] == ""


def test_state_resume_skips_done(tmp_path):
    p = tmp_path / "state.json"
    oh.save_state(p, {"a": {"status": "done"}, "b": {"status": "running"},
                      "c": {"status": "error"}, "d": {"status": "skipped"}})
    st = oh.load_state(p)
    assert oh.pending_ids(["a", "b", "c", "d", "e"], st) == ["b", "c", "e"]


def test_load_state_tolerates_garbage(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    assert oh.load_state(p) == {}


@pytest.mark.parametrize("earned", [0, 3])
def test_outcome_points(earned):
    r = {"criteria_breakdown": [
        {"criterion": "pr_opened", "earned": 0},
        {"criterion": "expected_outcome (regex_present)", "earned": earned},
        {"criterion": "citations_resolved", "earned": 1}]}
    assert oh.outcome_points(r) == earned


def test_progress_line_has_global_eta():
    s = oh.fmt_progress(2, 10, "fx", score=4, outcome=3, wall=60, run_score=4,
                        run_max=10, run_outcome=3, run_outcome_max=6,
                        walls=[60.0, 120.0], left=8, timed_out=False)
    assert "[2/10]" in s and "global 8 left" in s and "total_eta=12.0m" in s


def test_remap_repo_url_to_local_home(tmp_path):
    (tmp_path / ".luxe" / "fixture-cache" / "isomer").mkdir(parents=True)
    other = "/Users/someone-else/.luxe/fixture-cache/isomer"
    assert oh.remap_repo_url(other, home=tmp_path) == str(
        tmp_path / ".luxe" / "fixture-cache" / "isomer")
    # no local copy -> unchanged; non-cache URL -> unchanged
    assert oh.remap_repo_url("/Users/x/.luxe/fixture-cache/nope", home=tmp_path) \
        == "/Users/x/.luxe/fixture-cache/nope"
    assert oh.remap_repo_url("https://github.com/a/b", home=tmp_path) == "https://github.com/a/b"
