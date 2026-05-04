"""BFCL benchmark runner — raw + agent modes against luxe's oMLX backend.

Usage:
    python -m benchmarks.bfcl.run \\
        --categories simple_python multiple parallel parallel_multiple irrelevance \\
        --mode raw \\
        --output acceptance/bfcl/<checkpoint_id>/<rep>/

Per-problem outputs land in `<output>/<category>/<problem_id>.json`.
Aggregated summary at `<output>/summary.json`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from luxe.backend import Backend  # noqa: E402
from luxe.config import RoleConfig  # noqa: E402

from .adapter import (  # noqa: E402
    SUPPORTED_CATEGORIES,
    load_ground_truth,
    load_problems,
    run_problem_agent,
    run_problem_raw,
)
from .grade import grade  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--categories", nargs="+", default=list(SUPPORTED_CATEGORIES),
                   help="BFCL v4 categories to run.")
    p.add_argument("--mode", choices=("raw", "agent"), default="raw",
                   help="raw: single chat call. agent: full run_agent loop.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap problems per category (for smoke runs).")
    p.add_argument("--output", required=True, type=Path,
                   help="Output directory (acceptance/bfcl/<...>/<rep>/).")
    p.add_argument("--model", default="qwen3.6-35b-a3b-6bit",
                   help="Model name (must match an oMLX-registered model).")
    p.add_argument("--base-url", default="http://127.0.0.1:8000",
                   help="oMLX base URL.")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--num-ctx", type=int, default=32768)
    args = p.parse_args()

    backend = Backend(model=args.model, base_url=args.base_url)
    role_cfg = RoleConfig(
        model_key="bfcl",
        num_ctx=args.num_ctx,
        max_steps=12,
        max_tokens_per_turn=args.max_tokens,
        temperature=args.temperature,
    ) if args.mode == "agent" else None

    args.output.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "mode": args.mode,
        "model": args.model,
        "temperature": args.temperature,
        "categories": {},
        "started_at": time.time(),
    }

    grand_pass = 0
    grand_total = 0
    grand_wall = 0.0
    grand_prompt = 0
    grand_completion = 0

    for category in args.categories:
        if category not in SUPPORTED_CATEGORIES:
            print(f"  skipping unsupported category: {category}")
            continue
        try:
            problems = load_problems(category, limit=args.limit)
        except FileNotFoundError as e:
            print(f"  {category}: {e}")
            continue
        gt_map = load_ground_truth(category)
        cat_dir = args.output / category
        cat_dir.mkdir(parents=True, exist_ok=True)

        cat_pass = 0
        cat_wall = 0.0
        cat_prompt = 0
        cat_completion = 0

        for i, problem in enumerate(problems):
            pid = problem.get("id", f"{category}_{i}")
            if args.mode == "raw":
                result = run_problem_raw(
                    backend, problem,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                )
            else:
                result = run_problem_agent(
                    backend, role_cfg, problem,
                )

            gt = gt_map.get(pid)
            grade_res = grade(category, result.actual_calls, gt)

            (cat_dir / f"{pid}.json").write_text(json.dumps({
                "id": pid,
                "category": category,
                "passed": grade_res.passed,
                "reason": grade_res.reason,
                "actual_calls": [
                    {"name": n, "arguments": a} for (n, a) in result.actual_calls
                ],
                "wall_s": result.wall_s,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "error": result.error,
            }, indent=2))

            cat_pass += int(grade_res.passed)
            cat_wall += result.wall_s
            cat_prompt += result.prompt_tokens
            cat_completion += result.completion_tokens

            if (i + 1) % 25 == 0 or (i + 1) == len(problems):
                running_rate = cat_pass / (i + 1)
                print(f"  {category} {i+1}/{len(problems)} "
                      f"pass_rate={running_rate:.2%} cum_wall={cat_wall:.0f}s",
                      flush=True)

        n = len(problems)
        summary["categories"][category] = {
            "n": n,
            "passed": cat_pass,
            "pass_rate": (cat_pass / n) if n else 0.0,
            "total_wall_s": cat_wall,
            "avg_wall_s": (cat_wall / n) if n else 0.0,
            "total_prompt_tokens": cat_prompt,
            "total_completion_tokens": cat_completion,
        }
        grand_pass += cat_pass
        grand_total += n
        grand_wall += cat_wall
        grand_prompt += cat_prompt
        grand_completion += cat_completion

    summary["totals"] = {
        "n": grand_total,
        "passed": grand_pass,
        "pass_rate": (grand_pass / grand_total) if grand_total else 0.0,
        "total_wall_s": grand_wall,
        "total_prompt_tokens": grand_prompt,
        "total_completion_tokens": grand_completion,
    }
    summary["finished_at"] = time.time()
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))

    print()
    print(f"BFCL {args.mode} mode — totals: {grand_pass}/{grand_total} "
          f"({summary['totals']['pass_rate']:.2%}) "
          f"in {grand_wall:.0f}s wall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
