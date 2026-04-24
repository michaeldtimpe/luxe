"""Compression-benchmark runner: fix a bug in a real repo, scored by pytest.

Each task names a repo under `fixtures/compression_repos/<repo_id>/` and a
validation command (typically `pytest -q`). `build_messages` runs a
strategy pipeline (retrieve → compress → assemble) to build the prompt;
`grade` applies the returned unified diff to a tempdir copy of the repo
and reports the validation command's exit code plus file-selection
precision/recall against `task.reference.relevant_files`.

Strategies come from `strategies/configs/*.json`; any benchmark run is
parameterised by one strategy, so sweeps vary (candidate × backend ×
strategy) independently through `run_benchmark`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from benchmarks._common import Benchmark, Task, TaskResult, extract_code_block
from harness.backends import ToolDef
from strategies import Context, load_strategy, run_pipeline

REPO_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "compression_repos"
TASK_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "compression_tasks"


@dataclass
class CompressionRepo:
    """Benchmark: pytest-validated bugfix tasks on local repo fixtures."""

    name: str = "compression_repo"
    needs_tools: bool = False
    strategy: dict[str, Any] = field(default_factory=dict)
    task_dir: Path = TASK_FIXTURES
    repo_dir: Path = REPO_FIXTURES

    @classmethod
    def from_strategy_name(cls, strategy_name: str) -> "CompressionRepo":
        return cls(strategy=load_strategy(strategy_name))

    def tasks(self, limit: int | None = None) -> Iterable[Task]:
        files = sorted(self.task_dir.glob("*.json"))
        emitted = 0
        for path in files:
            task_spec = _load_task(path)
            yield Task(
                id=task_spec["id"],
                prompt=task_spec["description"],
                reference={
                    "repo_id": task_spec["repo_id"],
                    "validation": task_spec["validation"],
                    "relevant_files": list(
                        (task_spec.get("reference") or {}).get("relevant_files", [])
                    ),
                },
                metadata={"spec": task_spec, "source_path": str(path)},
            )
            emitted += 1
            if limit and emitted >= limit:
                return

    def build_messages(self, task: Task) -> list[dict[str, Any]]:
        spec = task.metadata["spec"]
        repo_root = (self.repo_dir / spec["repo_id"]).resolve()
        if not repo_root.is_dir():
            raise FileNotFoundError(f"repo fixture missing: {repo_root}")

        ctx = Context(
            repo_root=repo_root,
            task_description=spec["description"],
            entry_instruction=spec.get("entry_instruction", ""),
            goal=spec.get("goal", ""),
            reference=dict(spec.get("reference") or {}),
            backend=getattr(self, "backend", None),
        )
        ctx.reference.setdefault("validation", spec.get("validation", {}))
        run_pipeline(self.strategy, ctx)
        task.metadata["_pipeline_ctx"] = ctx
        return ctx.messages

    def tool_defs(self) -> list[ToolDef]:
        return []

    def grade(self, task: Task, completion: str, tool_log: list[dict[str, Any]]) -> TaskResult:
        spec = task.metadata["spec"]
        ctx: Context | None = task.metadata.get("_pipeline_ctx")
        repo_root = (self.repo_dir / spec["repo_id"]).resolve()
        output_format = (ctx.output_format if ctx else "unified_diff")

        if output_format == "whole_file":
            captured, apply_ok, apply_err, validation = _apply_whole_file_and_validate(
                repo_root=repo_root,
                completion=completion,
                command=spec["validation"]["command"],
            )
            # If no blocks parsed, keep the raw completion head so the
            # JSONL preserves enough info to diagnose the format drift.
            diff_text = captured or completion[:4000]
        else:
            diff_text = extract_code_block(completion, "diff")
            apply_ok, apply_err, validation = _apply_and_validate(
                repo_root=repo_root,
                diff_text=diff_text,
                command=spec["validation"]["command"],
            )

        passed = bool(apply_ok and validation and validation.returncode == 0)
        score = 1.0 if passed else 0.0

        precision, recall = _file_precision_recall(
            selected=_relpaths(ctx.files_in_scope, repo_root) if ctx else [],
            relevant=task.reference.get("relevant_files", []),
        )

        details: dict[str, Any] = {
            "output_format": output_format,
            "apply_ok": apply_ok,
            "apply_error": apply_err,
            "validation_exit": validation.returncode if validation else None,
            "validation_tail": _tail(validation.stdout + validation.stderr) if validation else "",
            "selected_files": _relpaths(ctx.files_in_scope, repo_root) if ctx else [],
            "relevant_files": task.reference.get("relevant_files", []),
            "file_precision": precision,
            "file_recall": recall,
            "t_retrieval_s": ctx.t_retrieval_s if ctx else 0.0,
            "t_compression_s": ctx.t_compression_s if ctx else 0.0,
            "assembled_prompt_chars": ctx.assembled_prompt_chars if ctx else 0,
        }

        error = None
        if not apply_ok:
            error = f"patch apply failed: {apply_err}"
        elif validation and validation.returncode != 0:
            error = f"validation exit {validation.returncode}"

        return TaskResult(
            task_id=task.id,
            completion=diff_text[:4000],
            passed=passed,
            score=score,
            error=error,
            details=details,
        )


def _load_task(path: Path) -> dict[str, Any]:
    import json

    with path.open() as f:
        return json.load(f)


def _relpaths(paths: list[Path], root: Path) -> list[str]:
    out: list[str] = []
    for p in paths:
        try:
            out.append(str(p.resolve().relative_to(root)))
        except ValueError:
            out.append(str(p))
    return out


def _file_precision_recall(selected: list[str], relevant: list[str]) -> tuple[float, float]:
    if not selected and not relevant:
        return 0.0, 0.0
    sel = set(selected)
    rel = set(relevant)
    hit = len(sel & rel)
    precision = hit / len(sel) if sel else 0.0
    recall = hit / len(rel) if rel else 0.0
    return precision, recall


def _tail(text: str, n_lines: int = 40) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n_lines:])


_DIFF_HEADER_RE = re.compile(r"^(?:diff --git|---|\+\+\+|@@)")
_FILE_ANCHOR_RE = re.compile(r"^# FILE:\s*(?P<path>\S.*)$", re.MULTILINE)


def _parse_whole_file_blocks(completion: str) -> dict[str, str]:
    """Pick out `# FILE: <path>` blocks. Tolerates both fenced
    (```python\\n# FILE: ...\\n...\\n```) and un-fenced variants —
    qwen2.5-coder:14b emits the former, qwen2.5-coder:32b tends to
    drop the fence. We anchor on the `# FILE:` line and take the body
    up to the next anchor or end of string, then strip any surrounding
    code fences."""
    anchors = list(_FILE_ANCHOR_RE.finditer(completion))
    if not anchors:
        return {}

    out: dict[str, str] = {}
    for i, m in enumerate(anchors):
        path = m.group("path").strip().strip("`").lstrip("./")
        if not path:
            continue
        body_start = m.end()
        body_end = anchors[i + 1].start() if i + 1 < len(anchors) else len(completion)
        lines = completion[body_start:body_end].splitlines()
        # Drop leading blank or fence lines.
        while lines and (not lines[0].strip() or lines[0].lstrip().startswith("```")):
            lines.pop(0)
        # Drop trailing blank or fence lines.
        while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("```")):
            lines.pop()
        if lines:
            out[path] = "\n".join(lines) + "\n"
    return out


def _apply_whole_file_and_validate(
    repo_root: Path,
    completion: str,
    command: list[str],
) -> tuple[str, bool, str | None, subprocess.CompletedProcess | None]:
    """Parse `# FILE:` blocks from the completion, overwrite matching
    files in a tempdir copy of the repo, run validation. Returns
    (captured_text_for_display, applied_ok, error_or_None, process_or_None)."""
    blocks = _parse_whole_file_blocks(completion)
    captured = "\n\n".join(f"# FILE: {p}\n{b}" for p, b in blocks.items())[:4000]
    if not blocks:
        return captured, False, "no `# FILE:` blocks in completion", None

    with tempfile.TemporaryDirectory(prefix="compbench_wf_") as td:
        tmp_root = Path(td) / "repo"
        shutil.copytree(repo_root, tmp_root)

        for rel_path, body in blocks.items():
            target = (tmp_root / rel_path).resolve()
            try:
                target.relative_to(tmp_root.resolve())
            except ValueError:
                return captured, False, f"path escapes repo: {rel_path}", None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)

        try:
            result = subprocess.run(
                list(command),
                cwd=tmp_root,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return captured, True, None, subprocess.CompletedProcess(
                args=command, returncode=127, stdout="", stderr=f"{type(e).__name__}: {e}"
            )
        return captured, True, None, result


def _apply_and_validate(
    repo_root: Path,
    diff_text: str,
    command: list[str],
) -> tuple[bool, str | None, subprocess.CompletedProcess | None]:
    """Copy the repo to a tempdir, try to apply the diff, then run the
    validation command. Returns (applied_ok, apply_error_or_None,
    completed_process_or_None)."""

    if not diff_text or not any(_DIFF_HEADER_RE.match(line) for line in diff_text.splitlines()):
        return False, "no unified diff detected in completion", None

    with tempfile.TemporaryDirectory(prefix="compbench_") as td:
        tmp_root = Path(td) / "repo"
        shutil.copytree(repo_root, tmp_root)

        diff_path = Path(td) / "patch.diff"
        # Ensure trailing newline — `patch` and `git apply` reject diffs
        # without one.
        diff_path.write_text(diff_text.rstrip() + "\n")

        apply_ok, apply_err = _try_apply(tmp_root, diff_path)
        if not apply_ok:
            return False, apply_err, None

        try:
            result = subprocess.run(
                list(command),
                cwd=tmp_root,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return True, None, subprocess.CompletedProcess(
                args=command, returncode=127, stdout="", stderr=f"{type(e).__name__}: {e}"
            )
        return True, None, result


def _try_apply(repo_root: Path, diff_path: Path) -> tuple[bool, str | None]:
    """Apply a unified diff to the repo tree.

    Three-tier apply:
      1. `git apply --3way` — recovers from wrong hunk positions.
      2. `patch -p1` / `-p0` — looser header requirements.
      3. Context-matching fallback — ignores hunk headers entirely and
         does literal before→after substitution when the before-block
         is unambiguous. This exists because local coder models
         reliably produce correct line *content* but wrong
         `@@ -a,b +c,d @@` *counts*, which rejects every other apply.
    Tier 3 doesn't log its success unless tiers 1 and 2 both failed."""
    git_ok, git_err = _try_git_apply(repo_root, diff_path)
    if git_ok:
        return True, None

    patch_errs: list[str] = []
    for strip in ("-p1", "-p0"):
        try:
            proc = subprocess.run(
                ["patch", strip, "-f", "-s", "--no-backup-if-mismatch", "-i", str(diff_path)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            patch_errs.append(f"patch {strip}: {type(e).__name__}: {e}")
            continue
        if proc.returncode == 0:
            return True, None
        patch_errs.append(f"patch {strip} rc={proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}")

    ctx_ok, ctx_err = _try_context_match(repo_root, diff_path)
    if ctx_ok:
        return True, None

    combined = (
        f"git-apply: {git_err or 'failed'}; "
        + "; ".join(patch_errs)
        + f"; ctx-match: {ctx_err or 'failed'}"
    )
    return False, combined[:700]


def _try_context_match(repo_root: Path, diff_path: Path) -> tuple[bool, str | None]:
    """Ignore hunk headers; do literal before→after substitution.

    Parses the diff for `+++ b/<path>` filenames and `@@`-delimited
    hunks. For each hunk, builds `before` = context + minus lines and
    `after` = context + plus lines. If `before` appears exactly once in
    the target file, substitutes. Bails the whole operation on any
    ambiguity so we don't half-apply."""
    try:
        text = diff_path.read_text()
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"

    hunks_by_file = _parse_hunks(text)
    if not hunks_by_file:
        return False, "no hunks parsed"

    # Plan all substitutions before writing anything so we can bail
    # atomically if any hunk is ambiguous or unmatched.
    plan: list[tuple[Path, str, str]] = []
    for rel_path, hunks in hunks_by_file.items():
        file_path = (repo_root / rel_path).resolve()
        # Guard against path escape.
        try:
            file_path.relative_to(repo_root.resolve())
        except ValueError:
            return False, f"path escapes repo: {rel_path}"
        if not file_path.exists():
            return False, f"target file not in repo: {rel_path}"
        body = file_path.read_text()
        for before, after in hunks:
            if not before:
                # Pure insertion (no context/minus) — we don't handle
                # these; git/patch would. Bail.
                return False, f"pure-insertion hunk in {rel_path} needs positional info"
            count = body.count(before)
            if count == 0:
                return False, f"before-block not found in {rel_path}"
            if count > 1:
                return False, f"before-block ambiguous in {rel_path} ({count} matches)"
            body = body.replace(before, after, 1)
            plan.append((file_path, before, after))
        file_path.write_text(body)

    return True, None


_DIFF_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?(.+?)(?:\s+\(.+\))?$")
_HUNK_START_RE = re.compile(r"^@@ .* @@")


def _parse_hunks(diff_text: str) -> dict[str, list[tuple[str, str]]]:
    """Return {path: [(before_block, after_block), ...]} from a unified
    diff. Tolerant of missing or wrong `@@` line counts — we only use
    `@@` as a delimiter, not for positioning."""
    lines = diff_text.splitlines()
    out: dict[str, list[tuple[str, str]]] = {}
    current_path: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _DIFF_FILE_RE.match(line)
        if m and not line.startswith("+++ /dev/null"):
            current_path = m.group(1).strip()
            i += 1
            continue
        if _HUNK_START_RE.match(line) and current_path:
            i += 1
            before_lines: list[str] = []
            after_lines: list[str] = []
            while i < len(lines):
                hl = lines[i]
                if _HUNK_START_RE.match(hl) or _DIFF_FILE_RE.match(hl) or hl.startswith("diff --git"):
                    break
                if not hl:
                    before_lines.append("")
                    after_lines.append("")
                elif hl[0] == " ":
                    before_lines.append(hl[1:])
                    after_lines.append(hl[1:])
                elif hl[0] == "-":
                    before_lines.append(hl[1:])
                elif hl[0] == "+":
                    after_lines.append(hl[1:])
                elif hl.startswith("\\"):
                    # "\ No newline at end of file" — ignore.
                    pass
                else:
                    break
                i += 1
            before = "\n".join(before_lines)
            after = "\n".join(after_lines)
            out.setdefault(current_path, []).append((before, after))
            continue
        i += 1
    return out


def _try_git_apply(repo_root: Path, diff_path: Path) -> tuple[bool, str | None]:
    """Init a scratch git repo at repo_root, commit the current tree,
    then `git apply --3way` the diff. --3way resolves off-by-one hunks
    that plain `patch` rejects."""
    env = {
        "GIT_AUTHOR_NAME": "compbench",
        "GIT_AUTHOR_EMAIL": "compbench@local",
        "GIT_COMMITTER_NAME": "compbench",
        "GIT_COMMITTER_EMAIL": "compbench@local",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }

    def _git(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=capture,
            text=True,
            timeout=30,
            env=env,
        )

    try:
        if not (repo_root / ".git").exists():
            init = _git("init", "-q")
            if init.returncode != 0:
                return False, f"init rc={init.returncode}: {init.stderr.strip()[:200]}"
            _git("add", "-A")
            commit = _git("commit", "-q", "--allow-empty", "-m", "baseline")
            if commit.returncode != 0:
                return False, f"commit rc={commit.returncode}: {commit.stderr.strip()[:200]}"

        apply = _git("apply", "--3way", "--whitespace=nowarn", str(diff_path))
        if apply.returncode == 0:
            return True, None
        # Retry without --3way; some diffs don't carry blob indices.
        apply2 = _git("apply", "--whitespace=nowarn", str(diff_path))
        if apply2.returncode == 0:
            return True, None
        err = (apply.stderr or apply.stdout or apply2.stderr or apply2.stdout).strip()
        return False, f"rc={apply.returncode}/{apply2.returncode}: {err[:300]}"
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"{type(e).__name__}: {e}"
