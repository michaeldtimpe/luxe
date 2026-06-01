"""Compare orchestration — run one task through two sides, sequentially.

The two sides never run concurrently and never hold two weight-sets resident
(compare.sdd). Mode-1 ablation is driven by an os.environ save/restore manager
because the luxe substrate is env-gated; the override is always restored.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from benchmarks.maintain_suite.run import Variant, make_overlay
from luxe.config import load_config

# Env that disables the luxe substrate for the "bare champion" side of mode 1.
_BARE_SUBSTRATE_ENV = {
    "LUXE_TIERED_COMPACT": "0",
    "LUXE_REFLECT": "0",
    "LUXE_ADAPTIVE_POLICY": "0",
    "LUXE_WRITE_PRESSURE": "0",
    "LUXE_EARLY_BAIL": "0",
    "LUXE_PROSE_BURST": "0",
    "LUXE_ACTION_DENSITY_GATE": "0",
}


@dataclass
class CompareSide:
    label: str
    variant: Variant
    substrate_env: dict[str, str] = field(default_factory=dict)


@dataclass
class SideResult:
    label: str
    model_id: str
    variant_id: str
    substrate_env: dict[str, str]
    run_id: str
    final_text: str = ""
    steps: int = 0
    tool_calls_total: int = 0
    tool_names: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall_s: float = 0.0
    peak_context_pressure: float = 0.0
    aborted: bool = False
    abort_reason: str = ""


@dataclass
class CompareResult:
    compare_id: str
    task: str
    task_type: str
    blind: bool
    sides: list[SideResult] = field(default_factory=list)


@contextlib.contextmanager
def _env_overrides(overrides: dict[str, str]):
    """Temporarily set env vars, restoring prior values on exit."""
    saved: dict[str, str | None] = {}
    try:
        for k, v in overrides.items():
            saved[k] = os.environ.get(k)
            os.environ[k] = v
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def build_sides(
    mode: int,
    *,
    model_id: str,
    model_b: str | None = None,
    label_a: str = "A",
    label_b: str = "B",
    prompt_a: str = "baseline",
    prompt_b: str = "baseline",
) -> tuple[CompareSide, CompareSide]:
    """Construct the two sides for a compare mode.

    mode 1: luxe-enhanced (default substrate) vs bare champion (substrate off).
    mode 2: two prompt variants of the same model.
    mode 3: champion vs a second model.
    """
    ml = model_id.lower()
    if mode == 1:
        a = CompareSide(label_a, Variant(model_label=ml, model_id=model_id))
        b = CompareSide(
            label_b,
            Variant(model_label=ml, model_id=model_id, system_prompt_id="baseline"),
            substrate_env=dict(_BARE_SUBSTRATE_ENV),
        )
        return a, b
    if mode == 2:
        a = CompareSide(label_a, Variant(model_label=ml, model_id=model_id,
                                         system_prompt_id=prompt_a, task_prompt_id=prompt_a))
        b = CompareSide(label_b, Variant(model_label=ml, model_id=model_id,
                                         system_prompt_id=prompt_b, task_prompt_id=prompt_b))
        return a, b
    if mode == 3:
        if not model_b:
            raise ValueError("mode 3 (cross-model) requires model_b")
        a = CompareSide(label_a, Variant(model_label=ml, model_id=model_id))
        b = CompareSide(label_b, Variant(model_label=model_b.lower(), model_id=model_b))
        return a, b
    raise ValueError(f"Unknown compare mode {mode}; expected 1, 2, or 3.")


def _role_for_side(side: CompareSide):
    overlay_dir = Path(tempfile.mkdtemp(prefix="luxe_cmp_"))
    overlay_path = make_overlay(side.variant, overlay_dir)
    cfg = load_config(overlay_path)
    return cfg.role("monolith")


def run_compare(
    side_a: CompareSide,
    side_b: CompareSide,
    *,
    task: str,
    task_type: str,
    languages,
    omlx_base_url: str = "http://127.0.0.1:8000",
    blind: bool = False,
    backend_factory=None,
    run_single_fn=None,
    on_status=None,
) -> CompareResult:
    """Run `task` through both sides sequentially and collect results.

    `backend_factory(base_url, model)` and `run_single_fn(...)` are injectable
    for testing; production defaults use the real Backend + run_single.
    """
    if backend_factory is None:
        from luxe.backend import Backend as backend_factory  # type: ignore
    if run_single_fn is None:
        from luxe.agents.single import run_single as run_single_fn  # type: ignore

    compare_id = uuid.uuid4().hex[:12]
    results: list[SideResult] = []
    resident: str | None = None
    backend = None

    for side in (side_a, side_b):
        role_cfg = _role_for_side(side)
        model_id = side.variant.model_id

        # Sequential weight swap when the model changes between sides.
        if backend is None:
            backend = backend_factory(base_url=omlx_base_url, model=model_id)
            resident = model_id
        elif model_id != resident:
            if on_status:
                on_status(f"swapping weights: {resident} → {model_id}")
            backend.unload_all_loaded(except_for=[model_id])
            backend.model = model_id
            backend.thermal_guard(model_id)
            resident = model_id
        else:
            backend.model = model_id

        run_id = f"cmp-{compare_id}-{side.label}"
        with _env_overrides(side.substrate_env):
            res = run_single_fn(
                backend, role_cfg,
                goal=task, task_type=task_type, languages=languages,
                run_id=run_id, phase="compare",
            )

        results.append(SideResult(
            label=side.label,
            model_id=model_id,
            variant_id=side.variant.variant_id,
            substrate_env=dict(side.substrate_env),
            run_id=run_id,
            final_text=res.final_text or "",
            steps=res.steps,
            tool_calls_total=res.tool_calls_total,
            tool_names=[tc.name for tc in res.tool_calls],
            prompt_tokens=res.prompt_tokens,
            completion_tokens=res.completion_tokens,
            wall_s=res.wall_s,
            peak_context_pressure=res.peak_context_pressure,
            aborted=res.aborted,
            abort_reason=res.abort_reason,
        ))

    return CompareResult(
        compare_id=compare_id, task=task, task_type=task_type,
        blind=blind, sides=results,
    )


def interactive_compare(task, cfg, repo_path, languages, *, console, reader=None) -> None:
    """REPL `/compare <task>` entry: prompt for mode, run, present, vote, store."""
    from luxe.compare import present, store

    reader = reader or (lambda prompt: console.input(prompt))
    champion = cfg.model_for_slot("chat")

    console.print("[bold]compare modes[/]  "
                  "1=luxe-vs-bare  2=two-prompts  3=vs-another-model")
    mode_raw = reader("mode [1/2/3]: ").strip() or "1"
    try:
        mode = int(mode_raw)
    except ValueError:
        console.print("[yellow]invalid mode[/]")
        return

    model_b = None
    prompt_a = prompt_b = "baseline"
    if mode == 3:
        model_b = reader("second model id: ").strip()
        if not model_b:
            console.print("[yellow]mode 3 needs a second model[/]")
            return
    elif mode == 2:
        prompt_a = reader("prompt A id [baseline]: ").strip() or "baseline"
        prompt_b = reader("prompt B id [cot]: ").strip() or "cot"

    blind = (reader("blind? [y/N]: ").strip().lower() == "y")

    side_a, side_b = build_sides(
        mode, model_id=champion, model_b=model_b,
        prompt_a=prompt_a, prompt_b=prompt_b,
    )
    task_type = _infer(task)
    console.print("[dim]· running side A, then side B (sequential)…[/]")
    result = run_compare(
        side_a, side_b,
        task=task, task_type=task_type, languages=languages,
        omlx_base_url=cfg.omlx_base_url, blind=blind,
        on_status=lambda m: console.print(f"[dim]· {m}[/]"),
    )
    store.save(result)
    present.render_side_by_side(console, result)
    present.prompt_vote(console, result, reader=reader)


def _infer(task: str) -> str:
    from luxe.cli import _infer_task_type
    return _infer_task_type(task)
