"""`luxe init` — draft the per-repo orientation brief into `.luxe/memory.md`.

Lives in gitkit because gitkit owns the survey machinery (repo health + repo
map + framing files), the read-only-role pattern, and the sanctioned
repo-`.luxe/` write precedent (`store.mirror_to_repo`). The memory package
stays read-path-only apart from its `splice_block` primitive, which owns the
file format.

One read-only `run_single` pass, one fenced block, no findings: the output is
injected into EVERY future chat turn as `<project_memory>`, so it is capped
hard in Python (never by asking the model to count) and it deliberately
carries orientation only — analysis belongs to `luxe gitaudit`, which has a
report format and somewhere to save it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from luxe.agents import prompts

BLOCK_NAME = "brief"
#: Hard write cap. `render_block`'s whole-memory budget is 4000 chars and
#: curated text must keep room ahead of the brief, which truncation already
#: favours (curated text sorts first in both the file and the render).
MAX_BRIEF_CHARS = 2000
_TRUNCATION_MARK = "\n…[luxe: brief truncated at 2000 chars — `luxe init` again after trimming]"


@dataclass
class BriefResult:
    ok: bool = False
    repo_root: str = ""
    text: str = ""
    written: Path | None = None
    truncated: bool = False
    used_cached_survey: bool = False
    error: str = ""


def cap_brief(text: str, *, limit: int = MAX_BRIEF_CHARS) -> tuple[str, bool]:
    """Deterministic truncation at `limit` chars, cutting on a line boundary
    where one is close by. Returns (text, truncated). Never trust the model to
    obey a length instruction — the brief is injected forever."""
    body = (text or "").strip()
    if len(body) <= limit:
        return body, False
    keep = limit - len(_TRUNCATION_MARK)
    cut = body[:max(0, keep)]
    nl = cut.rfind("\n")
    if nl > keep * 0.6:          # don't strand more than ~40% to land on a line
        cut = cut[:nl]
    return cut.rstrip() + _TRUNCATION_MARK, True


def resolve_target(path: str | Path) -> tuple[str, str]:
    """(repo_root, error). Same rule as a chat session: walk UP to the git root
    or a marker-bearing directory; `$HOME` and anything above it are never a
    project. A brief needs a subject."""
    from luxe.chat import project as project_mod

    resolved = project_mod.resolve(str(path))
    if not resolved.is_project:
        return "", (f"no project at {resolved.root} — `luxe init` needs a git "
                    "repo or a marker-bearing directory (pyproject.toml, "
                    "package.json, …). cd into one, or pass a path.")
    return resolved.root, ""


def _survey_context(target: str, console) -> tuple[str, bool]:
    """The grounding block for the brief pass, and whether it came free.

    A FRESH deep-map cache already holds a model-written architectural survey
    of this exact HEAD (`luxe gitaudit --deep`), which is strictly better
    grounding than the raw map and costs nothing — use it when it's there.
    """
    from luxe.gitkit import deep as deep_mod
    from luxe.gitkit import health
    from luxe.repo_index import build_repo_summary

    head = health.current_head(target)
    if head:
        cached = deep_mod.load_map(target, head=head)
        if cached and (cached.get("survey_notes") or "").strip():
            console.print("[dim]· reusing the cached deep-map survey for this "
                          "HEAD (free)[/]")
            return (f"{health.gather_repo_health(target)}\n\n<survey_notes>\n"
                    f"{cached['survey_notes']}\n</survey_notes>"), True

    summary = build_repo_summary(target)
    framing = deep_mod.framing_files(target)
    return (f"{health.gather_repo_health(target)}\n\n<repo_map>\n"
            f"{summary.render()}\n</repo_map>\n\n"
            f"{deep_mod._framing_block(framing)}"), False


def run_init(path: str | Path, cfg, *, console, run_single_fn=None,
             dry_run: bool = False, cancel=None, backend=None) -> BriefResult:
    """Draft (and unless `dry_run`, write) the project brief for `path`.

    `run_single_fn` is injectable so tests can drive this without a model.
    `backend` lets an in-session `/init` reuse the session's live endpoint —
    the CLI builds one from `cfg.omlx_base_url` as gitkit does. Never reads
    `~/.claude/` or the repo's `CLAUDE.md`: the brief is built from the repo's
    own code and git history only (memory.sdd discipline extends here).
    """
    from luxe import search as search_mod
    from luxe import symbols as symbols_mod
    from luxe.backend import Backend
    from luxe.chat.render import ChatCancelled
    from luxe.cli import _detect_languages_for_repo
    from luxe.gitkit.runner import GITKIT_MAX_TOKENS, _activity_callbacks
    from luxe.mcp.server import make_read_only_role
    from luxe.memory import project as project_mem
    from luxe.tools.fs import get_repo_root, set_repo_root

    if run_single_fn is None:
        from luxe.agents.single import run_single as run_single_fn

    target, err = resolve_target(path)
    if err:
        return BriefResult(ok=False, error=err)

    ctx_block, cached = _survey_context(target, console)

    prev_root = get_repo_root()
    prev_bm25, prev_sym = search_mod._index, symbols_mod._index
    reuse = prev_root is not None and str(prev_root) == target
    swapped = False
    try:
        if not reuse:
            set_repo_root(target)
            console.print("[dim]· indexing repository for search…[/]")
            search_mod.set_index(search_mod.build_bm25_index(target))
            symbols_mod.set_index(symbols_mod.build_symbol_index(target))
            swapped = True

        model = getattr(backend, "model", "") or cfg.model_for_slot("chat")
        console.print(f"[dim]· project brief — model: {model} (read-only)[/]")
        if backend is None:
            backend = Backend(base_url=cfg.omlx_base_url, model=model)
        role_cfg = make_read_only_role(cfg.role("monolith")).model_copy(
            update={"max_tokens_per_turn": GITKIT_MAX_TOKENS})
        goal = ("Write the project brief for the repository in the current "
                "working directory.\n\n" + prompts.GIT_BRIEF_HINT)

        def _do_run(on_event=None, on_token=None):
            return run_single_fn(
                backend, role_cfg, goal=goal, task_type="summarize",
                languages=_detect_languages_for_repo(target),
                extra_context=ctx_block,
                on_tool_event=on_event, on_token=on_token,
                phase="chat", run_id="gitkit-brief")

        try:
            if console.is_terminal:
                with console.status("[dim]reading the repo…[/]",
                                    spinner="dots") as status:
                    on_e, on_t = _activity_callbacks(
                        lambda t: status.update(f"[dim]{t}[/]"), cancel=cancel)
                    result = _do_run(on_e, on_t)
            else:
                on_e, on_t = _activity_callbacks(lambda t: None, cancel=cancel)
                result = _do_run(on_e, on_t)
        except (ChatCancelled, KeyboardInterrupt):
            return BriefResult(ok=False, repo_root=target, error="cancelled")
    finally:
        if swapped:
            search_mod.set_index(prev_bm25)
            symbols_mod.set_index(prev_sym)
            if prev_root is not None:
                set_repo_root(str(prev_root))

    text = (getattr(result, "final_text", "") or "").strip()
    if not text:
        return BriefResult(ok=False, repo_root=target,
                           error="the model produced no brief — retry, or "
                                 "check `luxe ready`")
    body, truncated = cap_brief(text)

    if dry_run:
        return BriefResult(ok=True, repo_root=target, text=body,
                           truncated=truncated, used_cached_survey=cached)

    import time
    stamp = (f"(auto-drafted {time.strftime('%Y-%m-%d')}, `luxe init` — edit "
             "freely above/below; re-init replaces only this block)")
    written = project_mem.splice_block(target, BLOCK_NAME, body, stamp=stamp)
    return BriefResult(ok=True, repo_root=target, text=body, written=written,
                       truncated=truncated, used_cached_survey=cached)
