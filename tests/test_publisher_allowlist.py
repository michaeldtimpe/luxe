"""The publisher allowlist on `luxe pull` / `/pull` (2026-10, provenance).

Models from unknown publishers — abliterated merges, re-uploads — are the
highest-risk class for a tool-using agent. An HF pull from an org not in
`trusted_publishers:` is refused at PLAN level (`modelstore.plan_pull`, shared
by both surfaces), so nothing is ever downloaded; `--allow-untrusted` lifts
it. Mount copies are checked when their path names an org. Publisher is read
from structure only — repo id, `models--org--Name`, `<store>/<org>/` — never
from a README a re-upload controls.

Hermetic: tmp store, stub admin client, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from luxe import modelstore as ms


def _model_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(json.dumps({"model_type": "qwen3"}))
    (path / "model.safetensors").write_bytes(b"\0" * 64)
    return path


# ---------------------------------------------------------------------------
# publishers


class TestPublishers:
    def test_publisher_from_ref_and_hf_cache_paths(self, tmp_path):
        assert ms.publisher_from_ref("mlx-community/X-4bit") == "mlx-community"
        assert ms.publisher_from_ref("X-4bit") == ""
        assert ms.publisher_from_path(
            "/Volumes/models/models--bartowski--X/snapshots/abc") == "bartowski"
        assert ms.publisher_from_path("/Volumes/models/X-4bit") == ""

    def test_store_publisher_nested_symlinked_and_unknown(self, tmp_path):
        store = tmp_path / "store"
        _model_dir(store / "mlx-community" / "Nested")
        _model_dir(store / "Top")
        snap = tmp_path / "hub" / "models--lmstudio-community--Linked" / "snapshots" / "s"
        _model_dir(snap)
        (store / "Linked").symlink_to(snap)
        assert ms.store_publisher("Nested", store) == "mlx-community"
        assert ms.store_publisher("Linked", store) == "lmstudio-community"
        assert ms.store_publisher("Top", store) == ""

    def test_trust_is_case_insensitive_and_unknown_is_none(self):
        assert ms.publisher_trust("MLX-Community", ["mlx-community"]) is True
        assert ms.publisher_trust("evil-merges", ["mlx-community"]) is False
        assert ms.publisher_trust("", ["mlx-community"]) is None

    def test_default_list_covers_every_publisher_the_fleet_uses(self):
        """configs/chat.yaml must not refuse anything currently in use: every
        fleet model on disk came from mlx-community."""
        from luxe.config import DEFAULT_TRUSTED_PUBLISHERS, load_config

        cfg = load_config(Path(__file__).parent.parent / "configs" / "chat.yaml")
        for org in ("mlx-community", "lmstudio-community", "Qwen", "bartowski",
                    "unsloth", "ggml-org", "zai-org"):
            assert ms.publisher_trust(org, cfg.trusted_publishers) is True
        assert set(DEFAULT_TRUSTED_PUBLISHERS) <= set(cfg.trusted_publishers)


# ---------------------------------------------------------------------------
# plan-level refusal (shared by `luxe pull` and `/pull`)


class _StubAdmin:
    started: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def model_info(self, repo_id):
        return {"size": 1234}

    def start_download(self, repo_id, hf_token=""):
        _StubAdmin.started.append(repo_id)
        raise AssertionError("a refused pull must never start a download")


@pytest.fixture
def no_net(monkeypatch):
    _StubAdmin.started = []
    monkeypatch.setattr(ms, "OmlxAdmin", _StubAdmin)
    monkeypatch.setattr(ms, "network_mounts", lambda: [])
    monkeypatch.delenv("LUXE_CONFIG", raising=False)
    return _StubAdmin


class TestPlanTrust:
    def test_an_untrusted_hf_publisher_is_refused_at_plan_level(self, tmp_path,
                                                                no_net):
        plan = ms.plan_pull("evil-merges/Qwen3.6-27B-abliterated",
                            admin=_StubAdmin(), models_dir=tmp_path,
                            trusted_publishers=["mlx-community"])
        assert plan.publisher == "evil-merges" and plan.trusted is False
        assert "evil-merges" in plan.refusal
        assert "--allow-untrusted" in plan.refusal
        assert _StubAdmin.started == []

    def test_allow_untrusted_lifts_the_refusal(self, tmp_path, no_net):
        plan = ms.plan_pull("evil-merges/X", admin=_StubAdmin(),
                            models_dir=tmp_path,
                            trusted_publishers=["mlx-community"],
                            allow_untrusted=True)
        assert plan.refusal == "" and plan.trusted is False
        assert plan.chosen.kind == "hf"

    def test_a_trusted_publisher_plans_normally(self, tmp_path, no_net):
        plan = ms.plan_pull("mlx-community/X-4bit", admin=_StubAdmin(),
                            models_dir=tmp_path,
                            trusted_publishers=["mlx-community"])
        assert plan.refusal == "" and plan.trusted is True
        assert plan.chosen.ref == "mlx-community/X-4bit"
        assert plan.chosen.size_bytes == 1234

    def test_the_default_list_applies_when_none_is_passed(self, tmp_path, no_net):
        plan = ms.plan_pull("evil-merges/X", admin=_StubAdmin(),
                            models_dir=tmp_path)
        assert plan.refusal

    def test_a_mount_source_with_an_org_in_its_path_is_checked(self, tmp_path,
                                                               no_net):
        src = _model_dir(tmp_path / "nas" / "models--evil-merges--X"
                         / "snapshots" / "abc")
        plan = ms.plan_pull("X", from_path=str(src.parent.parent),
                            models_dir=tmp_path / "store",
                            trusted_publishers=["mlx-community"])
        assert plan.publisher == "evil-merges" and plan.refusal

    def test_a_mount_source_without_an_org_is_allowed_as_unknown(self, tmp_path,
                                                                 no_net):
        src = _model_dir(tmp_path / "nas" / "X")
        plan = ms.plan_pull("X", from_path=str(src),
                            models_dir=tmp_path / "store",
                            trusted_publishers=["mlx-community"])
        assert plan.refusal == "" and plan.publisher == "" and plan.trusted is None


# ---------------------------------------------------------------------------
# `luxe pull` CLI


def _cli(args):
    from luxe import cli
    return CliRunner().invoke(cli.main, args)


class TestPullCliRefusal:
    def test_untrusted_hf_pull_is_refused_without_downloading(self, tmp_path,
                                                              no_net):
        res = _cli(["pull", "evil-merges/Qwen3.6-27B-abliterated", "-y",
                    "--models-dir", str(tmp_path / "store")])
        assert res.exit_code == 2, res.output
        assert "evil-merges" in res.output and "--allow-untrusted" in res.output
        assert _StubAdmin.started == []



# ---------------------------------------------------------------------------
# chat `/pull` shares the refusal


def test_chat_pull_refuses_an_untrusted_publisher(tmp_path, no_net):
    import io

    from rich.console import Console

    from luxe.chat import cmd_models
    from luxe.config import PipelineConfig, RoleConfig

    class _Slots:
        backend = type("B", (), {"base_url": "", "api_key": ""})()
        cfg = PipelineConfig(models={"monolith": "M"},
                             roles={"monolith": RoleConfig(model_key="monolith")})

    out = io.StringIO()
    ctx = cmd_models.CommandContext(console=Console(file=out, width=200),
                                    session=object(), slots=_Slots())
    cmd_models._pull(["evil-merges/X", "--yes"], ctx)
    text = out.getvalue()
    assert "evil-merges" in text and "--allow-untrusted" in text
    assert _StubAdmin.started == []
