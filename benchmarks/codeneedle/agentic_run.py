"""CodeNeedle via luxe's agent loop with /respond as the terminal tool.

Companion to `benchmarks/codeneedle/run.py`. Uses the same upstream
PROMPT_TEMPLATE and SequenceMatcher scorer, routed through
`src/luxe/agents/loop.py:run_agent`. Critical difference from the raw
runner: agentic mode does NOT append `/no_think` — we let the agent
loop's thinking template engage, on the theory that "luxe enhancement"
implies the model gets to reason. Comparing agentic vs raw isolates the
effect of (a) think-mode-on-recall (which upstream warns about as
"wasteful for pure recall") and (b) the agent-loop wrapping.

Two variants (same as gsm8k/agentic_run.py):
  --variant minimal  — system prompt + respond() only.
  --variant full     — TieredCompact + reflect + watchdog gates ON.

Usage:
  python -m benchmarks.codeneedle.agentic_run --output <dir> --variant {minimal,full} \
      [--corpus http_server.py] [--limit-fns 4]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luxe.backend import Backend  # noqa: E402
from luxe.config import RoleConfig  # noqa: E402
from luxe.tools.respond import respond_def, TOOL_FNS as RESPOND_TOOL_FNS  # noqa: E402

from benchmarks._eval_common.dataset import sha256_file  # noqa: E402
from benchmarks._eval_common.meta import build_run_meta  # noqa: E402
from benchmarks.codeneedle.adapter import build_prompt  # noqa: E402
from benchmarks.codeneedle.grade import aggregate_items  # noqa: E402
from benchmarks.codeneedle.upstream.scorer import score as score_function  # noqa: E402


BENCHMARK_PROTOCOL_VERSION = "codeneedle_agentic/v1"
MANIFEST_PATH = REPO_ROOT / "benchmarks/codeneedle/manifest.json"

_AGENTIC_SYSTEM_PROMPT = (
    "You will be shown a source file and asked to reproduce a specific "
    "section of one of its functions verbatim. Follow the rules in the "
    "user task exactly — output only the requested lines, no commentary, "
    "no code fences, no line numbers. When you have produced the answer, "
    "call the `respond` tool with the answer as its `message` argument."
)

_VARIANT_ENV: dict[str, dict[str, str]] = {
    "minimal": {
        "LUXE_TIERED_COMPACT": "0",
        "LUXE_REFLECT": "0",
        "LUXE_RESPOND_TERMINAL": "1",
        "LUXE_WRITE_PRESSURE": "0",
        "LUXE_EARLY_BAIL": "0",
        "LUXE_ACTION_DENSITY_GATE": "0",
        "LUXE_CONVERGENCE_GATE": "0",
        "LUXE_PROSE_BURST": "0",
    },
    "full": {
        "LUXE_TIERED_COMPACT": "1",
        "LUXE_REFLECT": "1",
        "LUXE_RESPOND_TERMINAL": "1",
        "LUXE_WRITE_PRESSURE": "1",
        "LUXE_EARLY_BAIL": "1",
        "LUXE_ACTION_DENSITY_GATE": "1",
        "LUXE_CONVERGENCE_GATE": "1",
        "LUXE_PROSE_BURST": "1",
    },
}


def _apply_variant_env(variant: str) -> None:
    for k, v in _VARIANT_ENV[variant].items():
        os.environ[k] = v


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "_-." else "_" for c in name)


def _json_default(obj):
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_root = Path(args.output)
    out_root.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(MANIFEST_PATH.read_text())
    corpora = manifest["corpora"]
    if args.corpus:
        corpora = [c for c in corpora if c["corpus_name"] == args.corpus]
        if not corpora:
            print(f"corpus {args.corpus!r} not in manifest", file=sys.stderr)
            return 2

    _apply_variant_env(args.variant)
    from luxe.agents.loop import run_agent  # noqa: E402

    backend = Backend(base_url=args.base_url, model=args.model)
    role_cfg = RoleConfig(
        model_key=args.model,
        num_ctx=args.num_ctx,
        max_steps=args.max_steps,
        max_tokens_per_turn=args.max_tokens,
        temperature=args.temperature,
    )
    tool_defs = [respond_def()]
    tool_fns = dict(RESPOND_TOOL_FNS)

    all_summaries: dict[str, dict] = {}
    for c in corpora:
        corpus_name = c["corpus_name"]
        out_dir = out_root / corpus_name.replace("/", "_")
        out_dir.mkdir(parents=True, exist_ok=True)

        corpus_path = REPO_ROOT / c["corpus_path"]
        corpus_text = corpus_path.read_text()
        actual_sha = sha256_file(corpus_path)
        if actual_sha != c["corpus_sha256"]:
            print(
                f"  {corpus_name}: ABORT — corpus sha256 drift",
                file=sys.stderr,
            )
            return 3

        fns = c["functions"]
        if args.limit_fns is not None:
            fns = fns[: args.limit_fns]

        per_function_records: list[dict] = []
        for fn in fns:
            item_path = out_dir / f"{_safe_name(fn['name'])}.json"
            if args.resume and item_path.exists():
                per_function_records.append(json.loads(item_path.read_text()))
                continue

            # Same prompt as raw, but DO let the model think.
            task_prompt = build_prompt(
                file_contents=corpus_text,
                function_name=fn["name"],
                language=c["language"],
                n_lines=len(fn["primary_lines"]),
                source_path=corpus_path.name,
                multi_file=False,
                suppress_thinking=False,  # critical difference
            )

            t0 = time.time()
            try:
                result = run_agent(
                    backend=backend,
                    role_cfg=role_cfg,
                    system_prompt=_AGENTIC_SYSTEM_PROMPT,
                    task_prompt=task_prompt,
                    tool_defs=tool_defs,
                    tool_fns=tool_fns,
                )
                final_text = result.final_text or ""
                steps = result.steps
                tool_calls_total = result.tool_calls_total
                prompt_toks = result.prompt_tokens
                completion_toks = result.completion_tokens
                aborted = result.aborted
                abort_reason = result.abort_reason
                err = None
            except Exception as e:  # noqa: BLE001
                final_text = ""
                steps = 0
                tool_calls_total = 0
                prompt_toks = 0
                completion_toks = 0
                aborted = True
                abort_reason = f"exception:{type(e).__name__}"
                err = f"{type(e).__name__}: {e}"
            wall_s = time.time() - t0

            fscore = score_function(
                name=fn["name"],
                primary=fn["primary_lines"],
                bonus=fn["bonus_lines"],
                predicted_text=final_text,
            )
            record = asdict(fscore)
            record["error"] = err
            record["wall_s"] = wall_s
            record["raw_output"] = final_text
            record["prompt_chars"] = len(task_prompt)
            record["steps"] = steps
            record["tool_calls_total"] = tool_calls_total
            record["prompt_tokens"] = prompt_toks
            record["completion_tokens"] = completion_toks
            record["aborted"] = aborted
            record["abort_reason"] = abort_reason
            record["variant"] = args.variant
            item_path.write_text(json.dumps(record, indent=2, default=_json_default))
            per_function_records.append(record)
            print(
                f"  {corpus_name}/{fn['name']}: "
                f"primary={fscore.primary_matched}/{fscore.primary_total} "
                f"bonus={fscore.bonus_matched} halluc={fscore.hallucinated} "
                f"{'PASS' if fscore.passed else 'FAIL'} "
                f"steps={steps} tools={tool_calls_total} wall={wall_s:.1f}s"
            )

        summary_stats = aggregate_items(per_function_records)
        # Augment with agentic-specific stats
        walls = [r.get("wall_s", 0) for r in per_function_records]
        steps_list = [r.get("steps", 0) for r in per_function_records]
        tools_list = [r.get("tool_calls_total", 0) for r in per_function_records]
        if walls:
            summary_stats["wall_mean_s"] = sum(walls) / len(walls)
            summary_stats["wall_max_s"] = max(walls)
        if steps_list:
            summary_stats["steps_mean"] = sum(steps_list) / len(steps_list)
        if tools_list:
            summary_stats["tool_calls_mean"] = sum(tools_list) / len(tools_list)
        all_summaries[corpus_name] = summary_stats
        (out_dir / "summary.json").write_text(json.dumps(summary_stats, indent=2))

    sampling = {
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "num_ctx": args.num_ctx,
        "max_steps": args.max_steps,
        "agentic_variant": args.variant,
        "suppress_thinking": False,
        "agent_loop_env": _VARIANT_ENV[args.variant],
    }
    meta = build_run_meta(
        benchmark_protocol_version=BENCHMARK_PROTOCOL_VERSION,
        model_id=args.model,
        sampling=sampling,
        backend_kind="http",
        context_window=args.num_ctx,
        backend_base_url=args.base_url,
        scoring={
            "method": "agentic_loop+respond+vendored_upstream_sequencematcher",
            "agent_loop_entry": "src/luxe/agents/loop.py:run_agent",
            "tool_surface": ["respond"],
            "pass_threshold": 8,
        },
        extra={"manifest_protocol": manifest.get("protocol_version")},
    )
    overall = {"meta": meta.to_dict(), "results_by_corpus": all_summaries}
    (out_root / "summary.json").write_text(json.dumps(overall, indent=2))

    for corpus_name, s in all_summaries.items():
        print(
            f"CodeNeedle agentic[{args.variant}] {corpus_name} — {s['count']} fns, "
            f"pass_rate={s['pass_rate']:.2%}, primary={s['primary_match_rate']:.2%}, "
            f"wall_mean={s.get('wall_mean_s', 0):.1f}s, "
            f"steps_mean={s.get('steps_mean', 0):.2f}, "
            f"tools_mean={s.get('tool_calls_mean', 0):.2f}"
        )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m benchmarks.codeneedle.agentic_run")
    p.add_argument("--output", required=True)
    p.add_argument("--variant", choices=["minimal", "full"], required=True)
    p.add_argument("--corpus", default=None, help="Limit to one corpus.")
    p.add_argument(
        "--limit-fns",
        type=int,
        default=None,
        help="Take only the first N functions per corpus (for spike runs).",
    )
    p.add_argument("--model", default="Qwen3.6-35B-A3B-6bit")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=2048)
    p.add_argument("--num-ctx", type=int, default=131072)
    p.add_argument("--max-steps", type=int, default=4)
    p.add_argument("--resume", action="store_true", default=True)
    p.add_argument("--no-resume", dest="resume", action="store_false")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
