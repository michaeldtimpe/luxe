"""GSM8K via luxe's agent loop with /respond as the terminal tool.

Companion to `benchmarks/gsm8k/run.py` (the raw-Backend runner). Routes
the same 8-shot Wei et al. CoT prompt through `src/luxe/agents/loop.py:run_agent`
so we can measure what the luxe agent runtime adds on top of the raw model.

Two variants:
  --variant minimal   — system prompt + respond() only. All watchdog
                        gates off; LUXE_RESPOND_TERMINAL=1 so the final
                        answer surfaces cleanly on AgentResult.final_text.
  --variant full      — TieredCompact + reflect + watchdog gates ON
                        (LUXE_WRITE_PRESSURE, LUXE_EARLY_BAIL,
                        LUXE_ACTION_DENSITY_GATE, LUXE_CONVERGENCE_GATE,
                        LUXE_PROSE_BURST). Matches the forge-hybrid
                        deployed default surface.

Note: most write-oriented watchdog gates were designed for SWE-bench-style
tool-using flows. On single-shot Q&A they generally don't fire (no file
writes), so "full" may be near-identical to "minimal" — that's an
informative finding, not a bug.

Usage:
  python -m benchmarks.gsm8k.agentic_run --output <dir> --variant {minimal,full} [--limit N]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luxe.backend import Backend  # noqa: E402
from luxe.config import RoleConfig  # noqa: E402
from luxe.tools.respond import respond_def, TOOL_FNS as RESPOND_TOOL_FNS  # noqa: E402

from benchmarks._eval_common.dataset import (  # noqa: E402
    cache_dir,
    jsonl_load,
    sha256_file,
)
from benchmarks._eval_common.extract import extract_gsm8k_answer  # noqa: E402
from benchmarks._eval_common.meta import build_run_meta  # noqa: E402
from benchmarks.gsm8k.adapter import build_messages, extract_gold_answer  # noqa: E402
from benchmarks.gsm8k.grade import aggregate_items  # noqa: E402


BENCHMARK_PROTOCOL_VERSION = "gsm8k_agentic/v1"
DEFAULT_DATA_PATH = cache_dir("gsm8k") / "test.jsonl"

_AGENTIC_SYSTEM_PROMPT = (
    "You are solving a math word problem. Read the question carefully, "
    "reason step by step, and arrive at a single numeric answer. When "
    "you have the final numeric answer, call the `respond` tool with a "
    "brief message that contains the answer, e.g. respond(message='The "
    "answer is 42.'). Do not call respond until you have computed the "
    "final answer."
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


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    data_path = Path(args.data) if args.data else DEFAULT_DATA_PATH
    if not data_path.exists():
        print(
            f"GSM8K data not found at {data_path}. Run "
            f"`python scripts/fetch_gsm8k_data.py` first.",
            file=sys.stderr,
        )
        return 2

    _apply_variant_env(args.variant)
    # Late import so the env vars are already set when run_agent reads them.
    from luxe.agents.loop import run_agent  # noqa: E402

    rows = list(jsonl_load(data_path))
    if args.limit is not None:
        rows = rows[: args.limit]

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

    n_total = len(rows)
    n_done = 0
    n_correct = 0
    n_aborted = 0
    t0 = time.time()

    for i, row in enumerate(rows):
        item_path = out_dir / f"item_{i:05d}.json"
        if args.resume and item_path.exists():
            cached = json.loads(item_path.read_text())
            n_correct += int(cached.get("correct", False))
            n_aborted += int(cached.get("aborted", False))
            n_done += 1
            continue

        question = row["question"]
        gold = extract_gold_answer(row["answer"])
        # 8-shot Wei et al. CoT body matches the raw runner.
        task_prompt = build_messages(question, think=True)[0]["content"]

        t_start = time.time()
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
        except Exception as e:  # noqa: BLE001
            final_text = f"<ERROR: {type(e).__name__}: {e}>"
            steps = 0
            tool_calls_total = 0
            prompt_toks = 0
            completion_toks = 0
            aborted = True
            abort_reason = f"exception:{type(e).__name__}"
        wall_s = time.time() - t_start

        extracted, reason = extract_gsm8k_answer(final_text)
        correct = (
            extracted is not None
            and math.isclose(extracted, gold, rel_tol=1e-9, abs_tol=1e-9)
        )

        record = {
            "qid": i,
            "question": question,
            "gold_answer": gold,
            "final_text": final_text,
            "extracted_answer": extracted,
            "failure_reason": reason,
            "correct": correct,
            "wall_s": wall_s,
            "steps": steps,
            "tool_calls_total": tool_calls_total,
            "prompt_tokens": prompt_toks,
            "completion_tokens": completion_toks,
            "aborted": aborted,
            "abort_reason": abort_reason,
            "variant": args.variant,
        }
        item_path.write_text(json.dumps(record, indent=2))
        n_correct += int(correct)
        n_aborted += int(aborted)
        n_done += 1

        elapsed = time.time() - t0
        rate = elapsed / max(1, n_done)
        eta_m = (n_total - n_done) * rate / 60.0
        print(
            f"  gsm8k_agentic[{args.variant}] {n_done}/{n_total} "
            f"acc={n_correct/n_done:.2%} steps={steps} wall={wall_s:.1f}s "
            f"avg={rate:.1f}s eta={eta_m:.1f}m"
        )

    item_records = [json.loads(p.read_text()) for p in sorted(out_dir.glob("item_*.json"))]
    summary_stats = aggregate_items(item_records)
    walls = [r["wall_s"] for r in item_records if "wall_s" in r]
    steps_list = [r["steps"] for r in item_records if "steps" in r]
    tools_list = [r["tool_calls_total"] for r in item_records if "tool_calls_total" in r]
    if walls:
        summary_stats["wall_mean_s"] = sum(walls) / len(walls)
        summary_stats["wall_max_s"] = max(walls)
    if steps_list:
        summary_stats["steps_mean"] = sum(steps_list) / len(steps_list)
        summary_stats["steps_max"] = max(steps_list)
    if tools_list:
        summary_stats["tool_calls_mean"] = sum(tools_list) / len(tools_list)
    summary_stats["aborted_count"] = n_aborted

    sampling = {
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "num_ctx": args.num_ctx,
        "max_steps": args.max_steps,
        "agentic_variant": args.variant,
        "agent_loop_env": _VARIANT_ENV[args.variant],
    }
    meta = build_run_meta(
        benchmark_protocol_version=BENCHMARK_PROTOCOL_VERSION,
        model_id=args.model,
        sampling=sampling,
        backend_kind="http",  # routed through agent loop; see scoring.method
        context_window=args.num_ctx,
        backend_base_url=args.base_url,
        benchmark_dataset_sha256=sha256_file(data_path),
        scoring={
            "method": "agentic_loop+respond+extract_gsm8k_answer",
            "agent_loop_entry": "src/luxe/agents/loop.py:run_agent",
            "tool_surface": ["respond"],
            "fewshot": "8shot_cot_wei_et_al",
        },
    )
    summary = {"meta": meta.to_dict(), "results": summary_stats}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(
        f"GSM8K agentic[{args.variant}] — {summary_stats['count']} questions, "
        f"acc={summary_stats['accuracy']:.2%}, "
        f"wall_mean={summary_stats.get('wall_mean_s', 0):.1f}s, "
        f"steps_mean={summary_stats.get('steps_mean', 0):.1f}, "
        f"aborted={summary_stats.get('aborted_count', 0)}"
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m benchmarks.gsm8k.agentic_run")
    p.add_argument("--output", required=True)
    p.add_argument(
        "--variant",
        choices=["minimal", "full"],
        required=True,
        help="luxe agent-loop variant.",
    )
    p.add_argument("--data", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--model", default="Qwen3.6-35B-A3B-6bit")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--num-ctx", type=int, default=8192)
    p.add_argument(
        "--max-steps",
        type=int,
        default=6,
        help="Per-item agent-loop step cap. Single-shot Q&A rarely needs more.",
    )
    p.add_argument("--resume", action="store_true", default=True)
    p.add_argument("--no-resume", dest="resume", action="store_false")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
