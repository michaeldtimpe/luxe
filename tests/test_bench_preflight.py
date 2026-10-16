"""Tests for the bake-off preflight (benchmarks/maintain_suite/preflight.py).

Fake model dirs, a fake oMLX site-packages tree and a fake backend — no oMLX
or mlx dependency, so the verdict logic stays testable on any host. The real
generations are exercised by running the preflight on the bench host."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from benchmarks.maintain_suite import preflight as pf
from benchmarks.maintain_suite.run import Variant
from luxe.backend import BackendError

_TOOLS_TEMPLATE = "{% if tools %}<tool_call>{{ tools }}</tool_call>{% endif %}"
_PLAIN_TEMPLATE = "{{ messages[0].content }}"
_CHAMP_QUANT = {"group_size": 64, "bits": 6, "mode": "affine",
                "model.layers.0.mlp.gate": {"group_size": 64, "bits": 8}}


def _model(store: Path, name: str, *, model_type="qwen3_moe", quant=None,
           template=_TOOLS_TEMPLATE, shards=("model-00001-of-00002.safetensors",
                                             "model-00002-of-00002.safetensors"),
           present=None, extra: dict | None = None) -> Path:
    d = store / name
    d.mkdir(parents=True)
    cfg = {"model_type": model_type, "architectures": ["X"]}
    if quant is not None:
        cfg["quantization"] = quant
    cfg.update(extra or {})
    (d / "config.json").write_text(json.dumps(cfg))
    (d / "chat_template.jinja").write_text(template)
    (d / "model.safetensors.index.json").write_text(json.dumps(
        {"weight_map": {f"w{i}": s for i, s in enumerate(shards)}}))
    for s in (shards if present is None else present):
        (d / s).write_bytes(b"\0" * 64)
    return d


@pytest.fixture
def site(tmp_path):
    s = tmp_path / "site"
    (s / "mlx_lm" / "models").mkdir(parents=True)
    (s / "mlx_lm" / "models" / "qwen3_moe.py").write_text("")
    (s / "mlx_vlm" / "models" / "muse_glimmer").mkdir(parents=True)
    (s / "omlx" / "patches" / "k2_horizon").mkdir(parents=True)
    return s


def _static(store, site, name, ref_quant=None, ref_over=None):
    rep = pf.ArmReport(name, name)
    pf.static_checks(rep, models_dir=store, site=site, ref_quant=ref_quant,
                     ref_overrides=ref_over)
    return rep


def _state(rep, name):
    return next(c.state for c in rep.checks if c.name == name)


# --- static ------------------------------------------------------------------


def test_clean_arm_passes_every_static_check(tmp_path, site):
    _model(tmp_path / "store", "Champ-6bit", quant=_CHAMP_QUANT)
    rep = _static(tmp_path / "store", site, "Champ-6bit")
    assert rep.verdict == pf.PASS, rep.checks
    assert rep.facts["engine"] == "llm"
    assert rep.facts["quant_overrides"] == {8: 1}


def test_missing_weights_fail_and_stop(tmp_path, site):
    (tmp_path / "store").mkdir()
    rep = _static(tmp_path / "store", site, "Nope")
    assert [c.name for c in rep.checks] == ["weights"]
    assert rep.failed


def test_partial_download_names_the_missing_shard(tmp_path, site):
    _model(tmp_path / "store", "Half", quant=_CHAMP_QUANT,
           present=("model-00001-of-00002.safetensors",))
    rep = _static(tmp_path / "store", site, "Half")
    assert _state(rep, "weights") == pf.FAIL
    assert "model-00002-of-00002" in rep.checks[0].detail


def test_xsym_stub_counts_as_missing_shard(tmp_path, site):
    d = _model(tmp_path / "store", "Stub", quant=_CHAMP_QUANT)
    target = b"/volume1/x"     # Synology format: 1067 bytes, 4th line = target
    body = b"XSym\n%04x\n%s\n%s\n" % (len(target), b"0" * 32, target)
    (d / "model-00002-of-00002.safetensors").write_bytes(
        body + b"\n" * (1067 - len(body)))
    assert pf.missing_shards(d) == ["model-00002-of-00002.safetensors"]


@pytest.mark.parametrize("mt,prov", [("qwen3_moe", ["mlx-lm"]),
                                     ("muse_glimmer", ["mlx-vlm"]),
                                     ("k2_horizon", ["omlx-patch"])])
def test_arch_resolves_through_any_provider(tmp_path, site, mt, prov):
    _model(tmp_path / "store", "M", model_type=mt, quant=_CHAMP_QUANT)
    rep = _static(tmp_path / "store", site, "M")
    assert _state(rep, "arch") == pf.PASS
    assert rep.facts["arch_providers"] == prov


def test_unknown_arch_fails(tmp_path, site):
    _model(tmp_path / "store", "M", model_type="qwen4_future", quant=_CHAMP_QUANT)
    assert _state(_static(tmp_path / "store", site, "M"), "arch") == pf.FAIL


def test_arch_skips_without_local_omlx(tmp_path):
    _model(tmp_path / "store", "M", quant=_CHAMP_QUANT)
    assert _state(_static(tmp_path / "store", None, "M"), "arch") == pf.SKIP


def test_vlm_engine_predicted_from_non_empty_vision_config(tmp_path, site):
    _model(tmp_path / "store", "V", quant=_CHAMP_QUANT,
           extra={"vision_config": {"depth": 24}})
    _model(tmp_path / "store", "T", quant=_CHAMP_QUANT,
           extra={"vision_config": {}})          # stripped text-only quant
    assert _static(tmp_path / "store", site, "V").facts["engine"] == "vlm"
    assert _static(tmp_path / "store", site, "T").facts["engine"] == "llm"


def test_quant_mismatch_vs_reference_warns(tmp_path, site):
    _model(tmp_path / "store", "Q4", quant={"group_size": 64, "bits": 4})
    rep = _static(tmp_path / "store", site, "Q4",
                  ref_quant={"bits": 6, "group_size": 64, "mode": "affine"},
                  ref_over={8: 1})
    assert _state(rep, "quant") == pf.WARN
    assert not rep.failed


def test_gates_quantized_differently_warns(tmp_path, site):
    """Global 6-bit matches, but router gates came out 6-bit instead of 8 —
    the conversion-parity failure the K2 self-quant must not slip past."""
    _model(tmp_path / "store", "K2", quant={"group_size": 64, "bits": 6,
                                            "mode": "affine"},
           extra={"num_experts": 100})
    rep = _static(tmp_path / "store", site, "K2",
                  ref_quant={"bits": 6, "group_size": 64, "mode": "affine"},
                  ref_over={8: 80})
    assert _state(rep, "quant") == pf.WARN


def test_dense_arm_is_not_held_to_moe_gate_overrides(tmp_path, site):
    _model(tmp_path / "store", "Dense", quant={"group_size": 64, "bits": 6,
                                               "mode": "affine"},
           extra={"text_config": {"num_hidden_layers": 52}})
    rep = _static(tmp_path / "store", site, "Dense",
                  ref_quant={"bits": 6, "group_size": 64, "mode": "affine"},
                  ref_over={8: 80})
    assert _state(rep, "quant") == pf.PASS


def test_moe_detected_in_vlm_text_config():
    assert pf.is_moe({"text_config": {"num_experts": 256}})
    assert not pf.is_moe({"num_experts": 0})


def test_template_without_tools_fails(tmp_path, site):
    _model(tmp_path / "store", "G3", quant=_CHAMP_QUANT, template=_PLAIN_TEMPLATE)
    assert _state(_static(tmp_path / "store", site, "G3"), "template") == pf.FAIL


# --- live --------------------------------------------------------------------


@dataclass
class _TC:
    name: str
    arguments: dict = field(default_factory=dict)
    id: str = "c1"


@dataclass
class _Timing:
    completion_tokens: int = 3


@dataclass
class _Resp:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    finish_reason: str = "stop"
    reasoning_chars: int = 0
    timing: _Timing = field(default_factory=_Timing)


class _Backend:
    def __init__(self, served, ping=None, tool=None, raise_on=None):
        self.served, self.model = served, ""
        self.ping = ping or _Resp(text="OK")
        self.tool = tool or _Resp(tool_calls=[_TC("read_file",
                                                  {"path": "README.md"})],
                                  finish_reason="tool_calls")
        self.raise_on = raise_on
        self.unloaded = []

    def list_models(self, timeout_s=None):
        return self.served

    def unload_all_loaded(self, except_for=None):
        self.unloaded.append(except_for)

    def chat(self, messages, tools=None, **kw):
        kind = "tool" if tools else "ping"
        if self.raise_on == kind:
            raise BackendError("HTTP 500: No module named mlx_vlm.models.x")
        return self.tool if tools else self.ping


def _live(backend, model="M"):
    rep = pf.ArmReport("m", model)
    ok = pf.live_checks(rep, backend)
    return rep, ok


def test_live_happy_path_unloads_others_first():
    b = _Backend(["M"])
    rep, ok = _live(b)
    assert ok and rep.verdict == pf.PASS
    assert b.unloaded == [["M"]]


def test_live_not_in_catalog_suggests_the_near_id():
    rep, ok = _live(_Backend(["mlx-community/M-6bit-x"]), model="M-6bit")
    assert not ok and _state(rep, "catalog") == pf.FAIL
    assert "similar" in rep.checks[0].detail


def test_live_load_error_fails_ping():
    rep, ok = _live(_Backend(["M"], raise_on="ping"))
    assert not ok and _state(rep, "ping") == pf.FAIL


def test_live_empty_ping_reports_reasoning_budget():
    rep, ok = _live(_Backend(["M"], ping=_Resp(text="", reasoning_chars=4000)))
    assert not ok and "reasoning_chars=4000" in rep.checks[-1].detail


def test_markup_in_content_is_the_unparsed_dialect_fail():
    tool = _Resp(text='<ifm|tool_calls>\n<ifm|tool_call>read_file\n'
                      '<ifm|arg_key>path</ifm|arg_key>', tool_calls=[])
    rep, ok = _live(_Backend(["M"], tool=tool))
    assert not ok
    assert "no parser claimed" in rep.checks[-1].detail


def test_tool_call_cut_off_by_max_tokens_says_so():
    tool = _Resp(text="Let me think", finish_reason="length",
                 reasoning_chars=9000)
    rep, _ = _live(_Backend(["M"], tool=tool))
    assert "max_tokens" in rep.checks[-1].detail


def test_tool_call_without_path_argument_fails():
    tool = _Resp(tool_calls=[_TC("read_file", {})])
    rep, ok = _live(_Backend(["M"], tool=tool))
    assert not ok and "unusable arguments" in rep.checks[-1].detail


# --- driver ------------------------------------------------------------------


def test_first_arm_is_the_quant_reference(tmp_path, site):
    store = tmp_path / "store"
    _model(store, "Ref", quant=_CHAMP_QUANT)
    _model(store, "Same", quant=_CHAMP_QUANT)
    _model(store, "Low", quant={"group_size": 64, "bits": 4,
                                "model.layers.0.mlp.gate": {"bits": 8}})
    vs = [Variant("ref", "Ref"), Variant("same", "Same"), Variant("low", "Low")]
    reps = pf.run_preflight(vs, live=False, drill=False, models_dir=store,
                            site=site)
    assert [r.verdict for r in reps] == [pf.PASS, pf.PASS, pf.WARN]


def test_live_skipped_when_weights_missing(tmp_path, site):
    (tmp_path / "store").mkdir()
    reps = pf.run_preflight([Variant("x", "X")], live=True, drill=True,
                            models_dir=tmp_path / "store", site=site,
                            backend_factory=lambda m: pytest.fail("loaded"))
    assert reps[0].checks[-1].state == pf.SKIP


def test_main_writes_reports_and_exits_on_fail(tmp_path, site, monkeypatch):
    store = tmp_path / "store"
    _model(store, "Good", quant=_CHAMP_QUANT)
    vf = tmp_path / "v.yaml"
    vf.write_text("variants:\n"
                  "  - {model_label: good, model_id: Good}\n"
                  "  - {model_label: gone, model_id: Gone}\n")
    monkeypatch.setattr(pf, "omlx_site_packages", lambda: site)
    out = tmp_path / "out"
    rc = pf.main(["--variants", str(vf), "--models-dir", str(store),
                  "--out", str(out)])
    assert rc == 1
    data = json.loads((out / "preflight.json").read_text())
    assert [d["verdict"] for d in data] == ["pass", "fail"]
    assert "| `gone` | ✗ fail" in (out / "PREFLIGHT.md").read_text()


def test_main_bad_variants_is_exit_2(tmp_path):
    assert pf.main(["--variants", str(tmp_path / "missing.yaml")]) == 2
