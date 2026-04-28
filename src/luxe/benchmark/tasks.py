"""Standardized benchmark tasks — same tasks across all model configs.

Each task defines:
- A fixture repo to use
- A goal string
- A task type
- Ground truth expectations (for quality scoring)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GroundTruth:
    """Expected findings or outcomes for quality scoring."""
    expected_findings: list[str] = field(default_factory=list)
    expected_files_touched: list[str] = field(default_factory=list)
    min_findings: int = 0
    severity_distribution: dict[str, int] = field(default_factory=dict)


@dataclass
class BenchmarkTask:
    id: str
    name: str
    fixture: str
    goal: str
    task_type: str
    ground_truth: GroundTruth = field(default_factory=GroundTruth)
    tags: list[str] = field(default_factory=list)


BENCHMARK_TASKS: list[BenchmarkTask] = [
    # --- REVIEW TASKS ---
    BenchmarkTask(
        id="review-python-security",
        name="Python API Security Review",
        fixture="python-api",
        goal="Review this Python API for security vulnerabilities. Focus on injection attacks, hardcoded secrets, and unsafe patterns.",
        task_type="review",
        ground_truth=GroundTruth(
            expected_findings=[
                "SQL injection in db.py:get_user",
                "Hardcoded SECRET_KEY in config.py",
                "Bare except in api.py:delete_user",
            ],
            min_findings=3,
            severity_distribution={"critical": 1, "high": 1, "medium": 1},
        ),
        tags=["security", "python", "core"],
    ),
    BenchmarkTask(
        id="review-python-quality",
        name="Python API Code Quality",
        fixture="python-api",
        goal="Review this Python API for code quality issues — bugs, resource leaks, missing validation, race conditions.",
        task_type="review",
        ground_truth=GroundTruth(
            expected_findings=[
                "Missing input validation in api.py:create_user",
                "Race condition in cache.py:get_or_set",
                "Unclosed file handle in utils.py:read_config",
            ],
            min_findings=3,
        ),
        tags=["quality", "python", "core"],
    ),
    BenchmarkTask(
        id="review-js-security",
        name="JS Web App Security Review",
        fixture="js-webapp",
        goal="Review this JavaScript web app for security vulnerabilities — XSS, CSRF, prototype pollution, insecure defaults.",
        task_type="review",
        ground_truth=GroundTruth(
            expected_findings=[
                "XSS in render.js:renderComment via innerHTML",
                "Prototype pollution in utils.js:deepMerge",
                "CORS wildcard in config.js",
                "Hardcoded SESSION_SECRET in config.js",
            ],
            min_findings=3,
            severity_distribution={"critical": 2, "high": 2},
        ),
        tags=["security", "javascript", "core"],
    ),
    BenchmarkTask(
        id="review-js-quality",
        name="JS Web App Code Quality",
        fixture="js-webapp",
        goal="Review this JavaScript web app for code quality — memory leaks, error handling, unused dependencies, missing cleanup.",
        task_type="review",
        ground_truth=GroundTruth(
            expected_findings=[
                "Memory leak in store.js:subscribe (no unsubscribe)",
                "Missing await on response.json() in api.js:fetchUser",
                "Unused lodash dependency in package.json",
                "Missing CSRF protection in handlers.js",
            ],
            min_findings=3,
        ),
        tags=["quality", "javascript", "core"],
    ),

    # --- SUMMARIZE TASKS ---
    BenchmarkTask(
        id="summarize-python",
        name="Python API Architecture Summary",
        fixture="python-api",
        goal="Summarize the architecture of this Python API. Identify entry points, data flow, dependencies between modules, and the overall structure.",
        task_type="summarize",
        ground_truth=GroundTruth(
            expected_findings=[
                "Identifies api.py as the handler layer",
                "Identifies db.py as the data access layer",
                "Notes config.py holds settings",
                "Notes cache.py provides in-memory caching",
            ],
            min_findings=3,
        ),
        tags=["summarize", "python", "core"],
    ),
    BenchmarkTask(
        id="summarize-mixed",
        name="Mixed Repo Overview",
        fixture="mixed-repo",
        goal="Summarize this repository — describe the backend and frontend components, how they connect, and the public API surface.",
        task_type="summarize",
        ground_truth=GroundTruth(
            expected_findings=[
                "Identifies backend/app.py as the main application",
                "Identifies User model in backend/models.py",
                "Notes frontend/main.js fetches from /api/users",
                "Notes auth decorator exists but is not implemented",
            ],
            min_findings=3,
        ),
        tags=["summarize", "mixed", "core"],
    ),

    # --- IMPLEMENT TASKS ---
    BenchmarkTask(
        id="implement-validation",
        name="Add Input Validation",
        fixture="python-api",
        goal="Add input validation to the create_user function in api.py: validate that name is non-empty and under 100 chars, and email contains an @ symbol.",
        task_type="implement",
        ground_truth=GroundTruth(
            expected_files_touched=["src/api.py"],
            expected_findings=[
                "Name validation added",
                "Email validation added",
                "Returns error response for invalid input",
            ],
        ),
        tags=["implement", "python", "core"],
    ),
    BenchmarkTask(
        id="implement-unsubscribe",
        name="Add Store Unsubscribe",
        fixture="js-webapp",
        goal="Fix the memory leak in store.js by making subscribe() return an unsubscribe function that removes the listener.",
        task_type="implement",
        ground_truth=GroundTruth(
            expected_files_touched=["src/store.js"],
            expected_findings=[
                "subscribe returns unsubscribe function",
                "Listener is removed from array on unsubscribe",
            ],
        ),
        tags=["implement", "javascript", "core"],
    ),

    # --- BUGFIX TASKS ---
    BenchmarkTask(
        id="bugfix-sqli",
        name="Fix SQL Injection",
        fixture="python-api",
        goal="Fix the SQL injection vulnerability in db.py:get_user. Use parameterized queries instead of string formatting.",
        task_type="bugfix",
        ground_truth=GroundTruth(
            expected_files_touched=["src/db.py"],
            expected_findings=[
                "Replaced f-string query with parameterized query",
                "Uses ? placeholder with tuple parameter",
            ],
        ),
        tags=["bugfix", "security", "python", "core"],
    ),

    # --- DOCUMENT TASKS ---
    BenchmarkTask(
        id="document-python-api",
        name="Document Python API",
        fixture="python-api",
        goal="Add docstrings to all public functions in the src/ directory. Each docstring should describe what the function does, its parameters, and return value.",
        task_type="document",
        ground_truth=GroundTruth(
            expected_files_touched=["src/db.py", "src/api.py", "src/cache.py", "src/utils.py"],
            expected_findings=[
                "Docstrings added to db.py functions",
                "Docstrings added to api.py functions",
            ],
        ),
        tags=["document", "python", "core"],
    ),
]


def get_tasks(tags: list[str] | None = None) -> list[BenchmarkTask]:
    """Filter benchmark tasks by tags. None returns all tasks."""
    if tags is None:
        return BENCHMARK_TASKS
    return [t for t in BENCHMARK_TASKS if any(tag in t.tags for tag in tags)]


def get_task(task_id: str) -> BenchmarkTask | None:
    for t in BENCHMARK_TASKS:
        if t.id == task_id:
            return t
    return None
