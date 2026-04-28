"""Tests for benchmark infrastructure — fixtures, tasks, scoring."""

from pathlib import Path

import pytest

from luxe.benchmark.fixtures import FIXTURES, create_python_api, create_js_webapp, create_mixed_repo
from luxe.benchmark.tasks import BENCHMARK_TASKS, get_tasks, get_task
from luxe.benchmark.scorer import (
    TaskScore, score_run, _extract_keywords, _fuzzy_match, _count_findings,
)
from luxe.config import load_config
from luxe.pipeline.model import PipelineRun, Status, Subtask


class TestFixtures:
    def test_python_api_created(self, tmp_path: Path):
        repo = create_python_api(tmp_path)
        assert (repo / "src" / "db.py").exists()
        assert (repo / "src" / "api.py").exists()
        assert (repo / "src" / "config.py").exists()
        assert (repo / "src" / "cache.py").exists()
        assert (repo / "src" / "utils.py").exists()
        assert (repo / "pyproject.toml").exists()

    def test_python_api_has_planted_bugs(self, tmp_path: Path):
        repo = create_python_api(tmp_path)
        db_code = (repo / "src" / "db.py").read_text()
        assert "f\"SELECT * FROM users WHERE id = '{user_id}'\"" in db_code

        config_code = (repo / "src" / "config.py").read_text()
        assert "super-secret-key" in config_code

        api_code = (repo / "src" / "api.py").read_text()
        assert "except:" in api_code

    def test_js_webapp_created(self, tmp_path: Path):
        repo = create_js_webapp(tmp_path)
        assert (repo / "src" / "api.js").exists()
        assert (repo / "src" / "render.js").exists()
        assert (repo / "src" / "utils.js").exists()
        assert (repo / "src" / "store.js").exists()
        assert (repo / "package.json").exists()

    def test_js_webapp_has_planted_bugs(self, tmp_path: Path):
        repo = create_js_webapp(tmp_path)
        render_code = (repo / "src" / "render.js").read_text()
        assert "innerHTML" in render_code

        config_code = (repo / "src" / "config.js").read_text()
        assert 'CORS_ORIGIN = "*"' in config_code

    def test_mixed_repo_created(self, tmp_path: Path):
        repo = create_mixed_repo(tmp_path)
        assert (repo / "backend" / "app.py").exists()
        assert (repo / "frontend" / "main.js").exists()

    def test_all_fixtures_registered(self):
        assert "python-api" in FIXTURES
        assert "js-webapp" in FIXTURES
        assert "mixed-repo" in FIXTURES

    def test_fixtures_have_git(self, tmp_path: Path):
        repo = create_python_api(tmp_path)
        assert (repo / ".git").is_dir()


class TestTasks:
    def test_all_tasks_have_required_fields(self):
        for t in BENCHMARK_TASKS:
            assert t.id, f"Task missing id"
            assert t.name, f"Task {t.id} missing name"
            assert t.fixture in FIXTURES, f"Task {t.id} references unknown fixture: {t.fixture}"
            assert t.task_type in {"review", "implement", "bugfix", "document", "summarize", "manage"}
            assert t.tags, f"Task {t.id} has no tags"

    def test_get_tasks_all(self):
        tasks = get_tasks()
        assert len(tasks) == len(BENCHMARK_TASKS)

    def test_get_tasks_by_tag(self):
        security_tasks = get_tasks(["security"])
        assert len(security_tasks) >= 2
        for t in security_tasks:
            assert "security" in t.tags

    def test_get_task_by_id(self):
        task = get_task("review-python-security")
        assert task is not None
        assert task.task_type == "review"

    def test_get_task_not_found(self):
        assert get_task("nonexistent-task") is None

    def test_core_tasks_cover_all_types(self):
        core = get_tasks(["core"])
        types = {t.task_type for t in core}
        assert "review" in types
        assert "summarize" in types
        assert "implement" in types
        assert "bugfix" in types
        assert "document" in types


class TestScorer:
    def test_extract_keywords(self):
        kw = _extract_keywords("SQL injection in db.py:get_user")
        assert "sql" in kw
        assert "injection" in kw
        assert "db.py" in kw or "get_user" in kw

    def test_fuzzy_match_positive(self):
        assert _fuzzy_match(["sql", "injection", "db.py"], "found sql injection in db.py line 10")

    def test_fuzzy_match_negative(self):
        assert not _fuzzy_match(["sql", "injection", "db.py"], "everything looks fine")

    def test_count_findings(self):
        report = """
        - SQL injection in db.py:10
        - XSS in render.js:5
        1. Hardcoded secret
        2. Missing validation
        """
        count = _count_findings(report)
        assert count >= 4

    def test_score_run_detects_findings(self):
        run = PipelineRun(goal="test", task_type="review")
        run.final_report = "Found SQL injection in db.py:get_user via string formatting"
        run.subtasks = [
            Subtask(
                title="test", role="worker_analyze", status=Status.DONE,
                result_text="Detected sql injection vulnerability in db.py get_user function"
            ),
        ]

        task = get_task("review-python-security")
        assert task is not None

        score = score_run(run, task, "test-config")
        assert len(score.findings_detected) >= 1
        assert score.detection_rate > 0

    def test_score_empty_report(self):
        run = PipelineRun(goal="test", task_type="review")
        run.final_report = ""
        run.subtasks = []

        task = get_task("review-python-security")
        score = score_run(run, task, "test-config")
        assert score.detection_rate == 0.0
        assert len(score.findings_missed) == len(task.ground_truth.expected_findings)


class TestConfigs:
    def test_qwen_config_loads(self):
        cfg = load_config(Path(__file__).parent.parent / "configs" / "qwen_32gb.yaml")
        assert cfg.profile.name == "Qwen Family (32 GB)"
        assert cfg.profile.memory_budget_gb == 32
        assert "architect" in cfg.roles
        assert "synthesizer" in cfg.roles
        assert "Qwen2.5" in cfg.models["architect"]

    def test_deepseek_config_loads(self):
        cfg = load_config(Path(__file__).parent.parent / "configs" / "deepseek_32gb.yaml")
        assert cfg.profile.name == "DeepSeek Family (32 GB)"
        assert "DeepSeek" in cfg.models["architect"]
        assert "DeepSeek" in cfg.models["synthesizer"]

    def test_configs_have_parity(self):
        """All configs must define the same roles and task types."""
        configs_dir = Path(__file__).parent.parent / "configs"
        config_files = list(configs_dir.glob("*_32gb.yaml"))
        assert len(config_files) >= 2

        cfgs = [load_config(f) for f in config_files]
        base_roles = set(cfgs[0].roles.keys())
        base_task_types = set(cfgs[0].task_types.keys())

        for cfg in cfgs[1:]:
            assert set(cfg.roles.keys()) == base_roles, \
                f"Role mismatch: {set(cfg.roles.keys())} != {base_roles}"
            assert set(cfg.task_types.keys()) == base_task_types, \
                f"Task type mismatch"

    def test_configs_same_tool_surfaces(self):
        """Each role should have the same tools across configs (parity)."""
        configs_dir = Path(__file__).parent.parent / "configs"
        config_files = sorted(configs_dir.glob("*_32gb.yaml"))

        cfgs = [(f.stem, load_config(f)) for f in config_files]
        for role_name in cfgs[0][1].roles:
            tools_by_config = {
                name: set(cfg.role(role_name).tools) for name, cfg in cfgs
            }
            base_name, base_tools = next(iter(tools_by_config.items()))
            for name, tools in tools_by_config.items():
                assert tools == base_tools, \
                    f"Tool mismatch for {role_name}: {base_name}={base_tools} vs {name}={tools}"
