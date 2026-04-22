"""Per-agent evaluation harness.

Usage:
    uv run python scripts/run_luxe_eval.py router
    uv run python scripts/run_luxe_eval.py router --model gemma3:12b
    uv run python scripts/run_luxe_eval.py general --all
    uv run python scripts/run_luxe_eval.py general --model qwen2.5:32b-instruct

Router: 10 canned prompts, deterministic hit-rate check.
General: 8 prompts; subjective quality — dumps full markdown per model to
`results/luxe_eval/general/<model>.md` for side-by-side comparison.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from luxe import router
from luxe.agents import general as general_agent
from luxe.agents import research as research_agent
from luxe.agents import writing as writing_agent
from luxe.backend import list_models, make_backend
from luxe.registry import load_config

console = Console()

RESULTS_ROOT = Path(__file__).resolve().parent.parent / "results" / "luxe_eval"


# ─── ROUTER ─────────────────────────────────────────────────────────────────


@dataclass
class RouterCase:
    prompt: str
    expected: str
    clarifying_answer: str = "pick your best guess with whatever info you have"


ROUTER_CASES: list[RouterCase] = [
    RouterCase("What is the capital of France?", "general"),
    RouterCase("Explain recursion in simple terms.", "general"),
    RouterCase("Write a haiku about autumn.", "writing"),
    RouterCase("Generate an image of a cat astronaut floating in space.", "image"),
    RouterCase("What were the top AI announcements last week?", "research"),
    RouterCase(
        "Fix the bug in main.py where the while loop never terminates.", "code"
    ),
    RouterCase("How does Python's Global Interpreter Lock work?", "general"),
    RouterCase("Research the history of the semaphore.", "research"),
    RouterCase("Write a short story about a robot who learns to dream.", "writing"),
    RouterCase("Help me refactor the auth middleware in my Express app.", "code"),
]


def eval_router(model_override: str | None = None) -> None:
    cfg = load_config()
    if model_override:
        cfg.get("router").model = model_override

    console.print(
        f"[bold]Router eval[/bold] — model: [cyan]{cfg.get('router').model}[/cyan]"
    )
    table = Table(show_lines=False)
    for col in ("prompt", "expected", "picked", "match", "ms", "clar"):
        table.add_column(col, overflow="fold")

    hits = 0
    for case in ROUTER_CASES:
        def ask_fn(_q: str, a: str = case.clarifying_answer) -> str:
            return a

        t0 = time.perf_counter()
        try:
            decision = router.route(case.prompt, cfg, ask_fn=ask_fn)
        except Exception as e:  # noqa: BLE001
            table.add_row(case.prompt, case.expected, f"[red]ERR[/red] {type(e).__name__}", "✗", "-", "-")
            continue
        dt_ms = int((time.perf_counter() - t0) * 1000)
        match = decision.agent == case.expected
        hits += int(match)
        table.add_row(
            case.prompt, case.expected, decision.agent,
            "[green]✓[/green]" if match else "[red]✗[/red]",
            f"{dt_ms}", str(len(decision.clarifications)),
        )

    console.print(table)
    console.print(
        f"\n[bold]Hit rate:[/bold] {hits}/{len(ROUTER_CASES)} "
        f"= {100 * hits / len(ROUTER_CASES):.0f}%"
    )


# ─── GENERAL ────────────────────────────────────────────────────────────────

GENERAL_PROMPTS = [
    ("factual", "What is the capital of France?"),
    ("conceptual", "Explain recursion in one short paragraph."),
    ("how-to", "How do I rename a local git branch?"),
    ("tradeoff", "When should I pick a relational database over a document database?"),
    ("definition", "What is the difference between concurrency and parallelism?"),
    ("list-format", "Name 3 books similar to The Lord of the Rings. One sentence each."),
    ("multi-fact", "What year was Python created, and by whom?"),
    ("redirect", "Write me a full 5-chapter novel about dragons."),
]

GENERAL_CANDIDATES = [
    "qwen2.5:7b-instruct",
    "gemma3:12b",
    "mixtral:8x7b-instruct-v0.1-q3_K_M",
    "qwen2.5:32b-instruct",
    "llama3.3:70b-instruct-q4_K_M",
]


def _run_general_one(model: str, prompt: str, cfg) -> tuple[str, int, int]:
    """Returns (text, ms, word_count)."""
    g_cfg = cfg.get("general").model_copy(update={"model": model})
    backend = make_backend(model, base_url=cfg.ollama_base_url)
    t0 = time.perf_counter()
    result = general_agent.run(backend, g_cfg, task=prompt)
    dt_ms = int((time.perf_counter() - t0) * 1000)
    text = result.final_text or ""
    return text, dt_ms, len(text.split())


def eval_general(models: list[str]) -> None:
    cfg = load_config()
    installed = set(list_models())

    out_root = RESULTS_ROOT / "general"
    out_root.mkdir(parents=True, exist_ok=True)

    summary = Table(title="General-agent eval summary")
    for col in ("model", "ok/N", "avg_ms", "avg_words", "report"):
        summary.add_column(col)

    for model in models:
        # Ollama tags store as 'foo:tag' in /api/tags
        if model not in installed:
            summary.add_row(model, "-", "-", "-", "[yellow]not installed[/yellow]")
            continue

        console.print(f"\n[bold cyan]→ {model}[/bold cyan]")
        md: list[str] = [f"# General-agent eval — `{model}`\n"]
        oks = 0
        ms_list: list[int] = []
        wc_list: list[int] = []
        for tag, prompt in GENERAL_PROMPTS:
            try:
                text, dt_ms, wc = _run_general_one(model, prompt, cfg)
            except Exception as e:  # noqa: BLE001
                md.append(f"## {tag}\n\n**Prompt:** {prompt}\n\n**ERROR:** `{type(e).__name__}: {e}`\n")
                continue
            oks += 1
            ms_list.append(dt_ms)
            wc_list.append(wc)
            md.append(
                f"## {tag} ({dt_ms} ms, {wc} words)\n\n"
                f"**Prompt:** {prompt}\n\n{text.strip() or '_(empty)_'}\n"
            )
            console.print(f"  [dim]{tag:12s}[/dim] {dt_ms:6d} ms  {wc:4d} words")

        out = out_root / f"{model.replace(':', '_').replace('/', '_')}.md"
        out.write_text("\n".join(md))
        avg_ms = int(sum(ms_list) / len(ms_list)) if ms_list else 0
        avg_wc = int(sum(wc_list) / len(wc_list)) if wc_list else 0
        summary.add_row(
            model, f"{oks}/{len(GENERAL_PROMPTS)}",
            f"{avg_ms}", f"{avg_wc}", str(out.relative_to(RESULTS_ROOT.parent.parent)),
        )

    console.print()
    console.print(summary)
    console.print(
        f"\nReports saved under [cyan]{out_root.relative_to(RESULTS_ROOT.parent.parent)}[/cyan]. "
        "Open each to judge concision + accuracy."
    )


# ─── RESEARCH ───────────────────────────────────────────────────────────────

RESEARCH_PROMPTS = [
    ("postgres-version", "What is the latest stable version of PostgreSQL? Cite sources."),
    (
        "python-deps",
        "What are the most commonly used dependency managers for Python in 2026?"
        " Compare the top 2 briefly with citations.",
    ),
    (
        "sqlite-vs-duckdb",
        "What are the main tradeoffs between SQLite and DuckDB for local"
        " analytical workloads? Cite sources.",
    ),
    (
        "turing-2025",
        "Who won the ACM Turing Award most recently, and for what contribution?",
    ),
]

RESEARCH_CANDIDATES = [
    "qwen2.5:7b-instruct",
    "qwen2.5:32b-instruct",
]


def _run_research_one(model: str, prompt: str, cfg) -> tuple[str, int, int, int]:
    """Returns (text, ms, tool_calls_total, steps)."""
    r_cfg = cfg.get("research").model_copy(update={"model": model})
    backend = make_backend(model, base_url=cfg.ollama_base_url)
    t0 = time.perf_counter()
    result = research_agent.run(backend, r_cfg, task=prompt)
    dt_ms = int((time.perf_counter() - t0) * 1000)
    text = result.final_text or ""
    if result.aborted:
        text = f"**ABORTED:** {result.abort_reason}\n\n{text}"
    return text, dt_ms, result.tool_calls_total, result.steps_taken


def eval_research(models: list[str]) -> None:
    cfg = load_config()
    installed = set(list_models())

    out_root = RESULTS_ROOT / "research"
    out_root.mkdir(parents=True, exist_ok=True)

    summary = Table(title="Research-agent eval summary")
    for col in ("model", "prompts", "avg_s", "avg_tools", "avg_steps", "report"):
        summary.add_column(col)

    for model in models:
        if model not in installed:
            summary.add_row(model, "-", "-", "-", "-", "[yellow]not installed[/yellow]")
            continue

        console.print(f"\n[bold cyan]→ {model}[/bold cyan]")
        md: list[str] = [f"# Research-agent eval — `{model}`\n"]
        ms_list, tc_list, st_list = [], [], []
        for tag, prompt in RESEARCH_PROMPTS:
            try:
                text, dt_ms, tc, st = _run_research_one(model, prompt, cfg)
            except Exception as e:  # noqa: BLE001
                md.append(f"## {tag}\n\n**Prompt:** {prompt}\n\n**ERROR:** `{type(e).__name__}: {e}`\n")
                console.print(f"  [red]{tag:20s} ERR {type(e).__name__}[/red]")
                continue
            ms_list.append(dt_ms)
            tc_list.append(tc)
            st_list.append(st)
            md.append(
                f"## {tag} ({dt_ms / 1000:.1f}s, {tc} tool calls, {st} steps)\n\n"
                f"**Prompt:** {prompt}\n\n{text.strip() or '_(empty)_'}\n"
            )
            console.print(
                f"  [dim]{tag:20s}[/dim] {dt_ms / 1000:6.1f}s  "
                f"tools={tc:2d}  steps={st:2d}"
            )

        out = out_root / f"{model.replace(':', '_').replace('/', '_')}.md"
        out.write_text("\n".join(md))
        avg_ms = sum(ms_list) / len(ms_list) if ms_list else 0
        summary.add_row(
            model,
            str(len(ms_list)),
            f"{avg_ms / 1000:.1f}",
            f"{(sum(tc_list) / len(tc_list)):.1f}" if tc_list else "-",
            f"{(sum(st_list) / len(st_list)):.1f}" if st_list else "-",
            str(out.relative_to(RESULTS_ROOT.parent.parent)),
        )

    console.print()
    console.print(summary)
    console.print(
        f"\nReports saved under [cyan]{out_root.relative_to(RESULTS_ROOT.parent.parent)}[/cyan]."
    )


# ─── WRITING ────────────────────────────────────────────────────────────────

WRITING_PROMPTS = [
    ("paragraph-story", "Write a complete story that fits in a single paragraph."),
    (
        "characters",
        "Give three unique character ideas for a novel. One short paragraph each."
        " Make them distinct from one another in tone and setting.",
    ),
    ("poem-or-stanza", "Write a short poem, or a single stanza for a song."),
]

WRITING_CANDIDATES = [
    "gemma3:12b",
    "gemma3:27b",
    "qwen2.5:32b-instruct",
    "mixtral:8x7b",
    "mistral-small:24b",
    "command-r:35b",
    "llama3.3-70b-4k:latest",
]


def _run_writing_one(model: str, prompt: str, cfg) -> tuple[str, int, int]:
    """Returns (text, ms, word_count)."""
    w_cfg = cfg.get("writing").model_copy(update={"model": model})
    backend = make_backend(model, base_url=cfg.ollama_base_url)
    t0 = time.perf_counter()
    result = writing_agent.run(backend, w_cfg, task=prompt)
    dt_ms = int((time.perf_counter() - t0) * 1000)
    text = result.final_text or ""
    return text, dt_ms, len(text.split())


def eval_writing(models: list[str]) -> None:
    cfg = load_config()
    installed = set(list_models())

    out_root = RESULTS_ROOT / "writing"
    out_root.mkdir(parents=True, exist_ok=True)

    summary = Table(title="Writing-agent eval summary")
    for col in ("model", "ok/N", "avg_s", "avg_words", "report"):
        summary.add_column(col)

    for model in models:
        if model not in installed:
            summary.add_row(model, "-", "-", "-", "[yellow]not installed[/yellow]")
            continue

        console.print(f"\n[bold cyan]→ {model}[/bold cyan]")
        md: list[str] = [f"# Writing-agent eval — `{model}`\n"]
        oks, ms_list, wc_list = 0, [], []
        for tag, prompt in WRITING_PROMPTS:
            try:
                text, dt_ms, wc = _run_writing_one(model, prompt, cfg)
            except Exception as e:  # noqa: BLE001
                md.append(f"## {tag}\n\n**Prompt:** {prompt}\n\n**ERROR:** `{type(e).__name__}: {e}`\n")
                console.print(f"  [red]{tag:18s} ERR {type(e).__name__}[/red]")
                continue
            oks += 1
            ms_list.append(dt_ms)
            wc_list.append(wc)
            md.append(
                f"## {tag} ({dt_ms / 1000:.1f}s, {wc} words)\n\n"
                f"**Prompt:** {prompt}\n\n{text.strip() or '_(empty)_'}\n"
            )
            console.print(f"  [dim]{tag:18s}[/dim] {dt_ms / 1000:6.1f}s  {wc:4d} words")

        out = out_root / f"{model.replace(':', '_').replace('/', '_')}.md"
        out.write_text("\n".join(md))
        avg_s = (sum(ms_list) / len(ms_list) / 1000) if ms_list else 0
        avg_w = int(sum(wc_list) / len(wc_list)) if wc_list else 0
        summary.add_row(
            model, f"{oks}/{len(WRITING_PROMPTS)}",
            f"{avg_s:.1f}", str(avg_w),
            str(out.relative_to(RESULTS_ROOT.parent.parent)),
        )

    console.print()
    console.print(summary)
    console.print(
        f"\nReports saved under [cyan]{out_root.relative_to(RESULTS_ROOT.parent.parent)}[/cyan]. "
        "Open each and pick the voice you prefer."
    )


# ─── CLI ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("agent", choices=["router", "general", "research", "writing"])
    parser.add_argument("--model", default=None, help="single model to eval")
    parser.add_argument("--all", action="store_true", help="eval every candidate")
    args = parser.parse_args()

    if args.agent == "router":
        eval_router(args.model)
        return

    if args.agent == "general":
        if args.model:
            eval_general([args.model])
        else:
            eval_general(GENERAL_CANDIDATES)
        return

    if args.agent == "research":
        if args.model:
            eval_research([args.model])
        else:
            eval_research(RESEARCH_CANDIDATES)
        return

    if args.agent == "writing":
        if args.model:
            eval_writing([args.model])
        else:
            eval_writing(WRITING_CANDIDATES)


if __name__ == "__main__":
    main()
