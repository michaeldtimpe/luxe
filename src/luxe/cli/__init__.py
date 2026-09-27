"""CLI entry point for luxe — mono-only execution.

A package since 2026-09-26 (was the single module `cli.py`). Layout:

- `_common.py` — the root group `main`, `AliasedGroup`, the shared Console,
  and the helpers several commands use (`_chat_cfg`, `_select_backend`,
  `_unload_unless`, `_default_chat_config`, ...).
- `kit.py`   — fallback kit + diagnostics: ready/doctor, smoke, repair,
  outage, net, planeproxy, claudecode, unload, update.
- `pull.py`  — `luxe pull` and its `_pull_*` helpers.
- `git.py`   — gitaudit / gitchange / gitapply, and `init`.
- `maint.py` — maintain, compare, pr, serve, runs, check.
- `chat.py`  — the `chat` / `code` shells over `luxe.chat.launch`.
- `__main__.py` — `python -m luxe.cli` (the benchmark harness spawns
  `python -m luxe.cli maintain`).

Importing the submodules registers their commands on `main`. Every name the
old module exposed is re-exported here so `from luxe.cli import X` and
`luxe.cli._X` keep resolving. A re-export is a COPY of the binding: to
monkeypatch what a command actually calls, patch the submodule that calls it
(e.g. `luxe.cli.kit._smoke_self_repair`, `luxe.cli._common._default_chat_config`).
"""

from __future__ import annotations

import os  # noqa: F401  (re-export: the old module's namespace)
import sys  # noqa: F401  (tests reach `luxe.cli.sys.stdout`)
import time  # noqa: F401
from pathlib import Path  # noqa: F401

import click  # noqa: F401
from rich.console import Console  # noqa: F401

from luxe.paths import luxe_home  # noqa: F401
from luxe import gitcmd  # noqa: F401
from luxe import textfmt  # noqa: F401
from luxe import gitclone  # noqa: F401
from luxe.agents.tasktype import infer_task_type  # noqa: F401
# Language detection moved to repo_index (the de-facto home for
# extension→language tables). Re-exported: tests and
# scripts/chunk_conclude_ab.py import them from `luxe.cli`.
from luxe.repo_index import (  # noqa: F401  (re-exports)
    _LANG_BY_EXT,
    _detect_languages_for_repo,
    _languages_from_paths,
)
from luxe.config import load_config  # noqa: F401

from luxe.cli._common import (  # noqa: F401
    AliasedGroup,
    _chat_cfg,
    _default_chat_config,
    _default_mcp_config_hint,
    _infer_task_type,
    _omlx_base_url_from_config,
    _resolve_repo,
    _select_backend,
    _unload_unless,
    apply_aliases,
    console,
    main,
)
from luxe.cli.maint import (  # noqa: F401
    _WRITE_TASKS,
    _default_config,
    _diff_against_base,
    _run_pipeline_maintain,
    _run_pipeline_readonly,
    _should_reprompt_for_under_engagement,
    check,
    compare_group,
    compare_review_cmd,
    compare_run_cmd,
    maintain,
    maintain_pipeline,
    pr_cmd,
    runs_gc_cmd,
    runs_group,
    runs_list_cmd,
    serve_cmd,
)
from luxe.cli.chat import (  # noqa: F401
    _INDEX_MAX_FILES,
    _INDEX_MAX_MB,
    _apply_slot_overrides,
    _build_chat_indexes,
    _resolve_theme_name,
    _run_interactive,
    _shared_chat_options,
    _tilde,
    chat_cmd,
    code_cmd,
)
from luxe.cli.git import (  # noqa: F401
    _gitkit_options,
    _run_gitapply_cmd,
    _run_gitkit_cmd,
    gitapply_cmd,
    gitaudit_cmd,
    gitchange_cmd,
    init_cmd,
)
from luxe.cli.kit import (  # noqa: F401
    _smoke_self_repair,
    build_ready_doctor,
    claudecode_cmd,
    net_cmd,
    outage_cmd,
    planeproxy_cmd,
    ready_cmd,
    repair_cmd,
    smoke_cmd,
    unload_models,
    update_cmd,
)
from luxe.cli.pull import (  # noqa: F401
    _default_engine_from_config,
    _materialize_from_hf_cache,
    _pull_from_hf,
    _pull_from_mount,
    _pull_list,
    _pull_remove,
    _pull_search,
    _refuse_pull_on_non_omlx,
    pull_cmd,
)


# Back-compat aliases. The old four commands are now two: gitsummary/gitreview/
# gitrefactor → gitaudit (combined read-only analysis); gitplan → gitchange.
apply_aliases(main, {
    "git-audit": "gitaudit", "gaudit": "gitaudit",
    "gitsummary": "gitaudit", "git-summary": "gitaudit", "gsum": "gitaudit",
    "gitreview": "gitaudit", "git-review": "gitaudit", "grev": "gitaudit",
    "gitrefactor": "gitaudit", "git-refactor": "gitaudit", "gref": "gitaudit",
    "git-change": "gitchange", "gchange": "gitchange",
    "gitplan": "gitchange", "git-plan": "gitchange", "gplan": "gitchange",
    # `luxe doctor` is the name people reach for under pressure; `ready` is
    # the canonical one (it answers "can I work right now?").
    "doctor": "ready",
})
