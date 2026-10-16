"""Variant cells: the max_steps harness-ablation knob (2026-10-16).

A default cell must stay byte-identical to legacy (same variant_id, same
overlay), so a knob-free run keeps writing into its existing output dirs.
"""

import yaml

from benchmarks.maintain_suite.run import Variant, _load_variants, make_overlay


def test_default_cell_keeps_legacy_id_and_overlay(tmp_path):
    v = Variant(model_label="m", model_id="M-6bit")
    assert v.variant_id == "mono__m"
    cfg = yaml.safe_load(make_overlay(v, tmp_path).read_text())
    assert cfg["roles"]["monolith"]["max_steps"] == 30   # the config's own


def test_max_steps_overrides_budget_and_namespaces_id(tmp_path):
    v = Variant(model_label="m", model_id="M-6bit", max_steps=60)
    assert v.variant_id == "mono__m__ms60"
    cfg = yaml.safe_load(make_overlay(v, tmp_path).read_text())
    assert cfg["roles"]["monolith"]["max_steps"] == 60
    assert cfg["models"]["monolith"] == "M-6bit"


def test_loader_reads_max_steps(tmp_path):
    p = tmp_path / "v.yaml"
    p.write_text("variants:\n"
                 "  - {model_label: a, model_id: A}\n"
                 "  - {model_label: b, model_id: B, max_steps: 45}\n")
    a, b = _load_variants(p)
    assert a.max_steps is None and a.variant_id == "mono__a"
    assert b.max_steps == 45 and b.variant_id == "mono__b__ms45"
