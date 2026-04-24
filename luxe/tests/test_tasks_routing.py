"""Tests for `luxe analyze --review` routing.

Verifies that the --review flag dispatches to the review agent pipeline
(via `luxe.review.start_review_task`) rather than the default
`code.run()` eval pipeline.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import luxe.cli as cli_module


def test_analyze_review_routes_to_start_review_task(tmp_path, monkeypatch):
    runner = CliRunner()
    called: dict = {}

    def fake_start(url_or_path, mode, cfg):
        called["url_or_path"] = url_or_path
        called["mode"] = mode
        return "20260423T010203-fake-tid"

    # Fake the load_config so we don't parse the real agents.yaml.
    class _Cfg:
        pass

    monkeypatch.setattr(cli_module, "load_config", lambda: _Cfg())
    # Patch inside the module where the command imports it.
    import luxe.review as review_module
    monkeypatch.setattr(review_module, "start_review_task", fake_start)

    repo = tmp_path / "repo"
    repo.mkdir()

    result = runner.invoke(cli_module.app, ["analyze", str(repo), "--review"])
    assert result.exit_code == 0, result.output
    assert called.get("mode") == "review"
    assert Path(called["url_or_path"]).name == "repo"
    assert "20260423T010203-fake-tid" in result.output
    assert "spawned review task" in result.output


def test_analyze_without_review_does_not_call_review_task(tmp_path, monkeypatch):
    runner = CliRunner()
    hit = {"count": 0}

    def fake_start(*a, **kw):
        hit["count"] += 1
        return "should-not-happen"

    # Short-circuit the expensive code-eval path — just raise before any
    # backend work actually starts. We only care that start_review_task
    # was not the dispatch target.
    class _Agent:
        model = "fake:0b"

    class _Cfg:
        def get(self, _):
            return _Agent()

        ollama_base_url = "http://127.0.0.1:11434"

    monkeypatch.setattr(cli_module, "load_config", lambda: _Cfg())
    import luxe.review as review_module
    monkeypatch.setattr(review_module, "start_review_task", fake_start)

    # Make the code-eval path bail immediately so we don't need a full
    # backend or agent set up for this test.
    def _boom(*a, **kw):
        raise RuntimeError("code-eval path hit, stopping before side effects")

    monkeypatch.setattr("luxe.backend.make_backend", _boom)

    repo = tmp_path / "repo"
    repo.mkdir()
    result = runner.invoke(cli_module.app, ["analyze", str(repo)])
    # The command should go to the code path (and raise), which proves
    # the --review branch was not taken. We just check that
    # start_review_task was never invoked regardless of exit code.
    assert hit["count"] == 0
