"""Tests for the compare package: side construction, sequential orchestration
(env overrides + weight swap), persistence, and presentation."""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from rich.console import Console

from luxe.compare import run_pair, present, store


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _console():
    return Console(file=io.StringIO(), force_terminal=False, width=120)


# --- build_sides ----------------------------------------------------------


def test_build_sides_mode1_ablation():
    a, b = run_pair.build_sides(1, model_id="Champ")
    assert a.substrate_env == {}
    assert b.substrate_env["LUXE_TIERED_COMPACT"] == "0"
    assert a.variant.model_id == b.variant.model_id == "Champ"


def test_build_sides_mode2_prompts():
    a, b = run_pair.build_sides(2, model_id="Champ", prompt_a="baseline", prompt_b="cot")
    assert a.variant.system_prompt_id == "baseline"
    assert b.variant.system_prompt_id == "cot"
    assert a.variant.model_id == b.variant.model_id


def test_build_sides_mode3_requires_model_b():
    with pytest.raises(ValueError):
        run_pair.build_sides(3, model_id="Champ")
    a, b = run_pair.build_sides(3, model_id="Champ", model_b="Other")
    assert a.variant.model_id == "Champ"
    assert b.variant.model_id == "Other"


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        run_pair.build_sides(9, model_id="Champ")


# --- env overrides --------------------------------------------------------


def test_env_overrides_restore():
    os.environ.pop("LUXE_TEST_X", None)
    os.environ["LUXE_TEST_Y"] = "orig"
    with run_pair._env_overrides({"LUXE_TEST_X": "1", "LUXE_TEST_Y": "new"}):
        assert os.environ["LUXE_TEST_X"] == "1"
        assert os.environ["LUXE_TEST_Y"] == "new"
    assert "LUXE_TEST_X" not in os.environ
    assert os.environ["LUXE_TEST_Y"] == "orig"
    os.environ.pop("LUXE_TEST_Y", None)


# --- run_compare orchestration (injected fakes) ---------------------------


@dataclass
class FakeTC:
    name: str


@dataclass
class FakeResult:
    final_text: str = "out"
    steps: int = 2
    tool_calls_total: int = 1
    tool_calls: list = field(default_factory=lambda: [FakeTC("read_file")])
    prompt_tokens: int = 5
    completion_tokens: int = 7
    wall_s: float = 1.0
    peak_context_pressure: float = 0.3
    aborted: bool = False
    abort_reason: str = ""


class FakeBackend:
    def __init__(self, base_url="", model=""):
        self.model = model
        self.swaps: list = []
        self.thermal: list = []

    def unload_all_loaded(self, *, except_for=None):
        self.swaps.append(except_for)
        return {}

    def thermal_guard(self, model, **kw):
        self.thermal.append(model)
        return True


def _run(side_a, side_b, **kw):
    captured = []

    def fake_run_single(backend, role_cfg, *, goal, task_type, languages, run_id, phase):
        captured.append({
            "model": backend.model,
            "run_id": run_id,
            "compact": os.environ.get("LUXE_TIERED_COMPACT"),
        })
        return FakeResult(final_text=f"body:{run_id}")

    result = run_pair.run_compare(
        side_a, side_b,
        task="do x", task_type="review", languages=frozenset(),
        backend_factory=FakeBackend, run_single_fn=fake_run_single,
        **kw,
    )
    return result, captured


def test_run_compare_mode1_no_swap_env_applied():
    a, b = run_pair.build_sides(1, model_id="Champ")
    result, captured = _run(a, b)
    # same model both sides → no swap
    assert len(result.sides) == 2
    assert captured[0]["model"] == "Champ"
    assert captured[1]["model"] == "Champ"
    # side A default substrate (no LUXE_TIERED_COMPACT override), side B bare
    assert captured[0]["compact"] in (None, os.environ.get("LUXE_TIERED_COMPACT"))
    assert captured[1]["compact"] == "0"
    # env restored after
    assert os.environ.get("LUXE_TIERED_COMPACT") != "0" or "LUXE_TIERED_COMPACT" not in os.environ


def test_run_compare_mode3_swaps_once():
    a, b = run_pair.build_sides(3, model_id="Champ", model_b="Other")
    result, captured = _run(a, b)
    assert captured[0]["model"] == "Champ"
    assert captured[1]["model"] == "Other"
    assert result.sides[1].model_id == "Other"


def test_run_compare_sideresult_fields():
    a, b = run_pair.build_sides(1, model_id="Champ")
    result, _ = _run(a, b)
    s = result.sides[0]
    assert s.tool_names == ["read_file"]
    assert s.completion_tokens == 7
    assert s.run_id.startswith("cmp-")


# --- store ----------------------------------------------------------------


def _make_result():
    a, b = run_pair.build_sides(1, model_id="Champ")
    result, _ = _run(a, b)
    return result


def test_store_save_load_round_trip():
    result = _make_result()
    store.save(result)
    loaded = store.load(result.compare_id)
    assert loaded is not None
    meta, sides, votes = loaded
    assert meta["task"] == "do x"
    assert len(sides) == 2
    assert votes == []


def test_store_record_vote_and_tally():
    result = _make_result()
    store.save(result)
    store.record_vote(result.compare_id, "A", reason="cleaner")
    store.record_vote(result.compare_id, "A")
    store.record_vote(result.compare_id, "B")
    _, _, votes = store.load(result.compare_id)
    assert len(votes) == 3
    assert votes[0]["reason"] == "cleaner"
    assert store.tally(votes) == {"A": 2, "B": 1}


def test_store_load_missing():
    assert store.load("nope") is None


def test_review_lists_and_replays():
    result = _make_result()
    store.save(result)
    store.record_vote(result.compare_id, "A")
    console = _console()
    store.review("", console=console)  # list
    assert result.compare_id in console.file.getvalue()
    console2 = _console()
    store.review(result.compare_id, console=console2)  # replay + tally
    out = console2.file.getvalue()
    assert "A=1" in out


# --- present --------------------------------------------------------------


def test_render_side_by_side_smoke():
    result = _make_result()
    console = _console()
    present.render_side_by_side(console, result)
    out = console.file.getvalue()
    assert "Champ" in out  # model id shown when not blind


def test_render_blind_hides_model():
    result = _make_result()
    result.blind = True
    console = _console()
    present.render_side_by_side(console, result)
    out = console.file.getvalue()
    assert "Left" in out and "Right" in out
    assert "Champ" not in out  # identity hidden in blind mode


def test_prompt_vote_records_with_disclaimer():
    result = _make_result()
    store.save(result)
    console = _console()
    answers = iter(["left", "more concise"])
    winner = present.prompt_vote(console, result, reader=lambda p: next(answers))
    assert winner == result.sides[0].label
    out = console.file.getvalue()
    assert "non-deterministic" in out
    _, _, votes = store.load(result.compare_id)
    assert votes[0]["winner"] == result.sides[0].label
    assert votes[0]["reason"] == "more concise"


def test_prompt_vote_skip():
    result = _make_result()
    store.save(result)
    console = _console()
    assert present.prompt_vote(console, result, reader=lambda p: "skip") is None
