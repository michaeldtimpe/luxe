"""MultiPL-E runner for Rust and Go subsets.

Dataset: `nuprl/MultiPL-E` on HuggingFace. Each row has `prompt` (the spec)
and `tests` (a test block in the target language). We concatenate the model's
completion with the tests and run the language toolchain locally with a
timeout. No Docker — assumes `rustc` and `go` are on PATH.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

from benchmarks._common import Benchmark, Task, TaskResult, extract_code_block
from harness.backends import ToolDef

LangKey = Literal["rust", "go"]


@dataclass
class MultiPLE:
    language: LangKey
    needs_tools: bool = False
    subset: str = "humaneval"  # or "mbpp"
    timeout_s: float = 30.0

    @property
    def name(self) -> str:
        return f"multipl_e_{self.subset}_{self.language}"

    def tasks(self, limit: int | None = None) -> Iterable[Task]:
        from datasets import load_dataset

        hf_subset = f"{self.subset}-{'rs' if self.language == 'rust' else 'go'}"
        ds = load_dataset("nuprl/MultiPL-E", hf_subset, split="test")
        for i, row in enumerate(ds):
            if limit and i >= limit:
                break
            yield Task(
                id=row["name"],
                prompt=row["prompt"],
                reference={
                    "prompt": row["prompt"],
                    "tests": row["tests"],
                    "stop_tokens": row.get("stop_tokens", []),
                },
            )

    def build_messages(self, task: Task) -> list[dict[str, Any]]:
        system = {
            "rust": (
                "You are a precise Rust coder. Complete the function. Return only the "
                "completed function body. Do not include tests or prose."
            ),
            "go": (
                "You are a precise Go coder. Complete the function. Return only the "
                "completed function body. Do not include tests or prose."
            ),
        }[self.language]
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": task.prompt},
        ]

    def tool_defs(self) -> list[ToolDef]:
        return []

    def grade(self, task: Task, completion: str, tool_log: list[dict[str, Any]]) -> TaskResult:
        code = extract_code_block(completion, self.language)
        body, tail = _split_body_and_tail(code, self.language)
        tests = task.reference["tests"]

        # We always produce a properly-closed primary function: prompt opens
        # it, body fills it, we add `}` to close. Any helpers the model
        # wrote after the fn close become `tail` and get appended outside
        # the fn. If the test block begins by closing the fn itself (Rust
        # convention: `}` then `fn main`), we strip that leading `}` since
        # we've already closed it.
        closed = body + "\n}\n" + (tail + "\n" if tail else "")
        test_lstripped = tests.lstrip()
        if test_lstripped.startswith("}"):
            tests = test_lstripped[1:]

        full = task.reference["prompt"] + closed + "\n" + tests

        if self.language == "rust":
            ok, detail = _run_rust(full, timeout_s=self.timeout_s)
        else:
            ok, detail = _run_go(full, timeout_s=self.timeout_s)

        return TaskResult(
            task_id=task.id,
            completion=code,
            passed=ok,
            score=1.0 if ok else 0.0,
            details={"stderr": detail[-2000:] if detail else ""},
        )


def _run_rust(source: str, timeout_s: float) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / "main.rs"
        bin_path = Path(tmp) / "main"
        src_path.write_text(source)
        try:
            compile_res = subprocess.run(  # noqa: S603
                ["rustc", "--edition", "2021", "-O", str(src_path), "-o", str(bin_path)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return False, f"compile: {e}"
        if compile_res.returncode != 0:
            return False, f"rustc stderr:\n{compile_res.stderr}"
        try:
            run_res = subprocess.run(  # noqa: S603
                [str(bin_path)], capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired:
            return False, "runtime timeout"
        return run_res.returncode == 0, run_res.stderr or run_res.stdout


def _run_go(source: str, timeout_s: float) -> tuple[bool, str]:
    # MultiPL-E Go tasks are `*_test` packages executed by `go test`, not
    # `go run`. Bootstrap a module, write the combined source as
    # `*_test.go`, then run tests with a timeout.
    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / "solution_test.go"
        src_path.write_text(source)
        init_res = subprocess.run(  # noqa: S603
            ["go", "mod", "init", "luxtest"],
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if init_res.returncode != 0:
            return False, f"go mod init: {init_res.stderr}"
        try:
            res = subprocess.run(  # noqa: S603
                ["go", "test", "-count=1", f"-timeout={int(timeout_s)}s"],
                capture_output=True,
                text=True,
                timeout=timeout_s + 15,
                cwd=tmp,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return False, f"go test: {e}"
        return res.returncode == 0, res.stderr or res.stdout


Rust = MultiPLE(language="rust")
Go = MultiPLE(language="go")


def _split_body_and_tail(code: str, lang: str) -> tuple[str, str]:
    """Return (body, tail) where body is what's inside the primary function
    (without its closing brace) and tail is anything the model wrote after
    that close (helper functions, extra decls, etc.).

    Handles three shapes uniformly:

    1. Body-only, no trailing `}` — body = everything, tail = "".
    2. Body-only with a trailing `}` — body = up to the `}`, tail = anything after.
    3. Full `fn foo(...) { body }` (+ helpers) — body = between first `{`
       and its matching `}`, tail = helpers after.
    """
    stripped = code.strip("\n").rstrip()
    if not stripped:
        return "", ""

    first_line = next((l for l in stripped.splitlines() if l.strip()), "")
    sig_starters = ("fn ", "pub fn ") if lang == "rust" else ("func ",)

    # Full-function shape: find body between the first `{` and its matching `}`.
    if first_line.lstrip().startswith(sig_starters):
        open_idx = stripped.find("{")
        if open_idx < 0:
            return stripped, ""
        depth = 1
        for i in range(open_idx + 1, len(stripped)):
            if stripped[i] == "{":
                depth += 1
            elif stripped[i] == "}":
                depth -= 1
                if depth == 0:
                    body = stripped[open_idx + 1 : i].strip("\n").rstrip()
                    tail = stripped[i + 1 :].strip()
                    return body, tail
        return stripped[open_idx + 1 :], ""

    # Body-only shape: walk with depth=1 looking for the close of the implicit
    # `{` opened by the prompt. Everything before that brace is the body;
    # everything after is the tail.
    depth = 1
    for i, ch in enumerate(stripped):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body = stripped[:i].rstrip()
                tail = stripped[i + 1 :].strip()
                return body, tail
    return stripped, ""
