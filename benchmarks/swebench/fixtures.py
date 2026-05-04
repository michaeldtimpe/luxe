"""SWE-bench Verified instance data model and JSON-file loader.

PRELIMINARY (2026-05-03). HuggingFace `datasets` integration deferred —
this module loads from local JSON dumps for now. Once the package install
is approved, add `from_huggingface(split="test")` that streams the
`princeton-nlp/SWE-bench_Verified` dataset.

Field shapes mirror the official SWE-bench harness row format. The agent
sees ONLY `problem_statement` + `hints_text` (concatenated as the goal).
Test arrays, gold patch, and test_patch are grader-only and must never
land in the prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SweBenchInstance:
    """One row from SWE-bench Verified.

    Field semantics:
    - `instance_id`: globally unique, format `<repo_owner>__<repo_name>-<issue_num>`.
    - `repo`: GitHub `owner/name` form; converted to clone URL by the adapter.
    - `base_commit`: sha against which the agent works AND against which
      the gold/test patches were authored.
    - `problem_statement`: the bug report or feature request body. Visible
      to the agent.
    - `hints_text`: any in-PR hints from the original issue. Visible to
      the agent (concatenated with problem_statement).
    - `patch`: gold patch — NEVER exposed to the agent. Reference only.
    - `test_patch`: tests authored alongside the gold patch. Applied by
      the harness AFTER the agent's diff to gate the resolution check.
    - `fail_to_pass`: tests that must transition F→P. Primary metric input.
    - `pass_to_pass`: tests that must remain P. Regression guard.
    - `environment_setup_commit`: the commit at which the env image was
      built; usually equal to `base_commit` but occasionally earlier.
    - `version`: Python/lib version pin used by the harness.
    - `difficulty`: SWE-bench Verified human-annotation label, one of
      `<15 min fix`, `15 min - 1 hour`, `1-4 hours`, `>4 hours`.
      Stratification key.
    """

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str = ""
    patch: str = ""
    test_patch: str = ""
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    environment_setup_commit: str = ""
    version: str = ""
    difficulty: str = ""
    created_at: str = ""

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.repo}.git"

    def goal_prompt(self, max_chars: int = 3000) -> str:
        """The text the agent sees as its task goal — problem_statement
        plus optional hints, capped to keep prompt budget reasonable.
        Caps preserve the head; SWE-bench problem_statements occasionally
        run very long with reproduction transcripts, but the lede usually
        contains the actionable description.
        """
        body = self.problem_statement.strip()
        if self.hints_text.strip():
            body = body + "\n\nHints:\n" + self.hints_text.strip()
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "\n\n[truncated]"
        return body


def load_instances_from_json(path: Path | str) -> list[SweBenchInstance]:
    """Load instances from a JSON file.

    Accepts either a list of row dicts (HF `datasets.to_json` format) or
    a JSONL file (one row per line). String-encoded `FAIL_TO_PASS` /
    `PASS_TO_PASS` lists (as exported by HF `datasets`) are decoded.
    """
    p = Path(path)
    text = p.read_text()
    if p.suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = [rows]
    out: list[SweBenchInstance] = []
    for row in rows:
        out.append(_row_to_instance(row))
    return out


def _row_to_instance(row: dict) -> SweBenchInstance:
    """Convert a HuggingFace SWE-bench row dict to a SweBenchInstance.

    Handles HF's quirk of encoding test arrays as JSON strings rather
    than lists. Tolerant to optional fields.
    """
    f2p = row.get("FAIL_TO_PASS", [])
    p2p = row.get("PASS_TO_PASS", [])
    if isinstance(f2p, str):
        f2p = json.loads(f2p) if f2p.strip() else []
    if isinstance(p2p, str):
        p2p = json.loads(p2p) if p2p.strip() else []
    return SweBenchInstance(
        instance_id=row["instance_id"],
        repo=row["repo"],
        base_commit=row["base_commit"],
        problem_statement=row.get("problem_statement", ""),
        hints_text=row.get("hints_text", ""),
        patch=row.get("patch", ""),
        test_patch=row.get("test_patch", ""),
        fail_to_pass=list(f2p),
        pass_to_pass=list(p2p),
        environment_setup_commit=row.get("environment_setup_commit", ""),
        version=str(row.get("version", "")),
        difficulty=row.get("difficulty", ""),
        created_at=row.get("created_at", ""),
    )
