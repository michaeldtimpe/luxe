"""Moved to `luxe.project` (2026-08-05, deferred-list #5).

Project resolution is subject-of-work logic with no interactive dependency —
gitkit's brief needs it too, and its old home here forced a function-local
gitkit→chat import. This shim keeps the historical import path for chat
callers and tests; new code imports `luxe.project`.
"""

from luxe.project import (  # noqa: F401
    DIR,
    GIT,
    NONE,
    PROJECT_MARKERS,
    Project,
    resolve,
)
