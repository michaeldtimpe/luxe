"""Phase B — personal repo review + write replay.

    uv run python scripts/run_phase_b.py \\
        --candidate qwen2.5-coder-32b \\
        --repo ~/code/my-rust-proj:rust \\
        --repo ~/code/my-py-proj:python \\
        --per-lang 5

Each --repo takes `path:language`. The script ingests recent merged PRs, then
runs B1 (review) and B2 (write) replays on each.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.registry import load_optimization_registry, load_registry  # noqa: E402
from harness.server import launch_server  # noqa: E402
from personal_eval import commit_ingest, gh_ingest, review_replay, write_replay  # noqa: E402
from personal_eval.repo_resolver import resolve_repo  # noqa: E402


def main(
    candidate: str = typer.Option(..., "--candidate"),
    repo: list[str] = typer.Option(
        [],
        "--repo",
        help=(
            "Repo spec as `<spec>:<language>` where <spec> is a local path, "
            "`owner/repo` shorthand, or full GitHub URL. URLs and shorthands "
            "are cloned into personal_eval/cache/ and reused across runs."
        ),
    ),
    per_lang: int = typer.Option(5, "--per-lang"),
    config_id: str = typer.Option("baseline", "--config"),
    backend_kind: str = typer.Option("mlx", "--backend"),
    max_steps: int = typer.Option(30, "--max-steps"),
    source: str = typer.Option(
        "auto",
        "--source",
        help="Task source: 'pr' (gh merged PRs), 'commits' (recent commits on default branch), or 'auto' (try PRs first, fall back to commits if none qualify).",
    ),
) -> None:
    reg = load_registry()
    opt = load_optimization_registry()
    cand = reg.get(candidate)
    cfg = opt.get(config_id)
    draft = reg.draft_for(cand) if cfg.spec_decoding else None

    # Ingest tasks — quick, no model needed.
    repo_specs: list[tuple[Path, str]] = []
    for entry in repo:
        if ":" not in entry:
            raise typer.BadParameter(f"--repo requires spec:language, got {entry!r}")
        path_str, lang = entry.rsplit(":", 1)
        repo_path = resolve_repo(path_str)
        print(f"  resolved {path_str!r} → {repo_path}")

        records = []
        if source in ("auto", "pr"):
            try:
                records = gh_ingest.ingest(repo_path, language=lang, max_prs=per_lang * 3)
            except Exception as e:  # noqa: BLE001
                print(f"  [pr ingest failed: {e}]")
                records = []
            print(f"  PR ingest → {len(records)} task(s)")
        if (source == "commits") or (source == "auto" and not records):
            records = commit_ingest.ingest_from_commits(
                repo_path, language=lang, max_commits=per_lang * 5
            )
            print(f"  commit ingest → {len(records)} task(s)")
        if not records:
            print(f"  [skip] {repo_path.name}: no tasks after ingestion")
            continue
        repo_specs.append((repo_path, lang))

    with launch_server(kind=backend_kind, candidate=cand, config=cfg, draft=draft) as backend:
        for repo_path, _lang in repo_specs:
            review_replay.replay_corpus(
                backend,
                repo_name=repo_path.name,
                candidate_id=cand.id,
                config_id=cfg.id,
                limit=per_lang,
            )
            write_replay.replay_corpus(
                backend,
                repo_name=repo_path.name,
                candidate_id=cand.id,
                config_id=cfg.id,
                limit=per_lang,
                max_steps=max_steps,
            )


if __name__ == "__main__":
    typer.run(main)
