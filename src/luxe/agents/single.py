"""Single-mode (mono) agent — one capable model, full tool surface, agentic loop.

The only execution mode in v1.0. Reuses agents/loop.py:run_agent; the
distinguishing pieces are the system prompt (frames the task end-to-end)
and the tool surface (full read+write+analyze+shell+git, plus MCP tools
when injected).

Prompts come from src/luxe/agents/prompts.py via RoleConfig.system_prompt_id
and RoleConfig.task_prompt_id; see that module's docstring for the editing
norm.
"""

from __future__ import annotations

from luxe.agents.loop import AgentResult, OnToolEvent, run_agent
from luxe.agents.prompts import get as get_prompt, resolve_prompt_ids
from luxe.backend import Backend
from luxe.config import RoleConfig
from luxe.sdd import SddParseError
from luxe.spec_resolver import find_all_sdd, format_sdd_block
from luxe.tools import analysis, cve_lookup as cve_lookup_mod, fs, git, shell
from luxe.tools.base import ToolCache, ToolDef, ToolFn
from luxe import search as search_mod
from luxe import symbols as symbols_mod


def _build_full_tool_surface(
    languages: frozenset[str] | None,
    tool_allowlist: list[str] | None,
    task_type: str | None = None,
) -> tuple[list[ToolDef], dict[str, ToolFn], set[str]]:
    """Assemble the full read+write+analyze+shell+git tool surface.

    `tool_allowlist` (typically from the role config) restricts which of these
    are exposed. Pass None to expose everything — handy for tests.

    `task_type` gates audit-only tools off non-audit tasks. cve_lookup is the
    canonical case: it closes deps-audit hallucination on `manage` but, when
    present on `implement`/`document`/`bugfix`/`review` surfaces, dilutes the
    model's prior over edit_file/write_file enough to flip a previously-PASS
    implement fixture into prose-mode under-engagement (lpe-rope-calc-implement-
    strict-flag, deterministic 3/3 in v1.2 regression probe).
    """
    defs: list[ToolDef] = []
    fns: dict[str, ToolFn] = {}
    cacheable: set[str] = set()

    defs.extend(fs.read_only_defs())
    fns.update(fs.READ_ONLY_FNS)
    cacheable.update(fs.CACHEABLE)

    defs.append(search_mod.bm25_search_def())
    fns.update(search_mod.TOOL_FNS)
    cacheable.update(search_mod.CACHEABLE)

    defs.append(symbols_mod.find_symbol_def())
    fns.update(symbols_mod.TOOL_FNS)
    cacheable.update(symbols_mod.CACHEABLE)

    defs.extend(fs.mutation_defs())
    fns.update(fs.MUTATION_FNS)

    defs.extend(git.tool_defs())
    fns.update(git.TOOL_FNS)
    cacheable.update(git.CACHEABLE)

    defs.extend(shell.tool_defs())
    fns.update(shell.TOOL_FNS)

    a_defs = analysis.tool_defs(languages)
    a_fns = analysis.tool_fns(languages)
    defs.extend(a_defs)
    fns.update(a_fns)
    cacheable.update(analysis.CACHEABLE)

    # cve_lookup — language-agnostic; queries OSV.dev for any ecosystem.
    # Closes the audit-hallucination gap from v1.1's deps-audit by giving
    # the model a deterministic, factual CVE source instead of having it
    # produce CVE-shaped strings that match the regex but may be invented.
    # Gated to `manage` task_type only — see docstring above.
    if task_type == "manage":
        defs.append(cve_lookup_mod.cve_lookup_def())
        fns.update(cve_lookup_mod.TOOL_FNS)
        cacheable.update(cve_lookup_mod.CACHEABLE)

    if tool_allowlist is not None:
        allowed = set(tool_allowlist)
        defs = [d for d in defs if d.name in allowed]
        fns = {n: f for n, f in fns.items() if n in allowed}
        cacheable = cacheable & allowed

    return defs, fns, cacheable


def run_single(
    backend: Backend,
    role_cfg: RoleConfig,
    *,
    goal: str,
    task_type: str = "review",
    languages: frozenset[str] | None = None,
    extra_tool_defs: list[ToolDef] | None = None,
    extra_tool_fns: dict[str, ToolFn] | None = None,
    cache: ToolCache | None = None,
    on_tool_event: OnToolEvent | None = None,
    run_id: str | None = None,
    phase: str = "main",
) -> AgentResult:
    """Run the single-mode agent end-to-end on a goal.

    `role_cfg.tools` (from configs/single_64gb.yaml's `monolith` role) is the
    allowlist of native tools. Anything in `extra_tool_defs` (e.g. MCP tools)
    is appended unconditionally — MCP tools are namespaced and can't collide.
    """
    defs, fns, cacheable = _build_full_tool_surface(
        languages, role_cfg.tools or None, task_type=task_type
    )

    if extra_tool_defs:
        defs = defs + list(extra_tool_defs)
    if extra_tool_fns:
        fns = {**fns, **extra_tool_fns}

    sys_id, task_id = resolve_prompt_ids(
        task_type,
        system_prompt_id=role_cfg.system_prompt_id,
        task_prompt_id=role_cfg.task_prompt_id,
        task_overlay_id=role_cfg.task_overlay_id,
    )
    sys_variant = get_prompt(sys_id)
    task_variant = get_prompt(task_id)
    sdd_block = _build_sdd_block()
    task_prompt = (
        f"Task type: {task_type}\n"
        f"Goal: {goal}\n\n"
        f"{task_variant.task_prefix}"
        f"{sdd_block}"
    )

    return run_agent(
        backend, role_cfg,
        system_prompt=sys_variant.system,
        task_prompt=task_prompt,
        tool_defs=defs,
        tool_fns=fns,
        cache=cache,
        cacheable=cacheable,
        on_tool_event=on_tool_event,
        run_id=run_id,
        phase=phase,
    )


def _build_sdd_block() -> str:
    """Surface every `.sdd` contract in the active repo as a prompt block.

    SpecDD Lever 2: the model sees Forbids/Owns globs alongside the
    task goal so it doesn't waste cycles attempting writes the tool
    layer will refuse. Returns "\\n\\n<block>" when contracts exist,
    or "" when no `.sdd` files are present (the common case for
    fixture repos that haven't adopted SpecDD).

    A malformed `.sdd` is logged-but-not-raised here — the tool-side
    Forbids check will surface the actionable error on the first
    write attempt, which is the friendlier failure mode (the run
    still gets a chance to make read-only progress before the first
    refused write).
    """
    repo_root = fs.get_repo_root()
    if repo_root is None:
        return ""
    try:
        sdd_files = find_all_sdd(repo_root)
    except SddParseError:
        # Tool layer will surface this on first write attempt.
        return ""
    block = format_sdd_block(sdd_files, repo_root)
    if not block:
        return ""
    return "\n\n" + block
