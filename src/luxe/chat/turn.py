"""The UI-agnostic per-turn core shared by both `luxe chat` front-ends.

`prepare_turn` (slot routing, role/tool/ledger wiring, `/ctx` clamp,
`extra_context`, user-turn persistence, and the verbatim `run_single`
closure) and `finalize_turn` (persistence + `TurnOutcome`), plus the failure
bookkeeping every path shares (`note_aborted_turn`, `note_backend_error`,
`note_turn_crash`, `recover_backend_failure`, attachment re-staging) and the
per-turn spend settlement. Moved verbatim out of `repl.py`; `repl` re-exports
every name. Nothing here renders — see `controller.py` for the pipeline that
sequences these and `repl.py` / `tui.py` for the two renderers (chat.sdd).
"""

from __future__ import annotations

import logging
import os
import re
import traceback
from dataclasses import dataclass, field
from typing import Callable

from luxe import spec_resolver
from luxe.agents import prompts as prompts_mod
from luxe.agents.single import run_single
from luxe.chat import cost as cost_mod
from luxe.chat import mcptools
from luxe.chat import modelcaps
from luxe.chat.render import compose_answer, strip_leaked_reasoning
from luxe.chat.session import ChatTurn
from luxe.config import RoleConfig
from luxe.memory import session as session_store
from luxe.state import ledger as ledger_mod
from luxe.tools import fs as fs_mod

# Name kept from the module these lines moved out of, so debug.log lines
# ("… luxe.chat.repl: turn BackendError …") read exactly as before.
logger = logging.getLogger("luxe.chat.repl")


@dataclass
class TurnOutcome:
    """What the goal supervisor (B4/C1) needs to decide the next round: how much
    the turn actually did, a fingerprint of its tool calls for stuck-loop
    detection, and the latest observed test result (C1) so completion/stuck key on
    observable state, not the model's ledger discipline. `crashed` is set when
    run_single raised before returning."""
    tool_calls: int = 0
    files_changed: int = 0
    final_text: str = ""
    interrupted: bool = False
    crashed: bool = False
    fingerprint: frozenset = field(default_factory=frozenset)
    # (passed, failed, errors) from the latest test run this turn, or None.
    test_result: tuple[int, int, int] | None = None
    # Rendering inputs, populated by the UI-agnostic core so any front-end (line
    # REPL or Textual TUI) can render the footer/status from one source.
    result: object | None = None          # AgentResult | None
    slot: str = ""
    model: str = ""
    num_ctx: int = 0
    ctx_ceiling: int = 0
    started_at: float = 0.0
    ended_at: float = 0.0


# Pytest-style summary parsing (C1). Tolerant: each count matched independently,
# singular/plural, anywhere in the output (not a fixed single-line format).
_RE_PASSED = re.compile(r"(\d+)\s+passed", re.IGNORECASE)
_RE_FAILED = re.compile(r"(\d+)\s+(?:failed|failures?)", re.IGNORECASE)
_RE_ERRORS = re.compile(r"(\d+)\s+errors?", re.IGNORECASE)
_RE_TESTCMD = re.compile(r"pytest|\bpython\b.*-m\s+pytest|\bunittest\b|\bnpm\s+test\b",
                         re.IGNORECASE)


def parse_test_result(command: str, result: str, errored: bool
                      ) -> tuple[int, int, int] | None:
    """Extract (passed, failed, errors) from a tool's output, or None if this
    wasn't a recognizable test run. A test command that crashed before emitting a
    summary (traceback / non-zero exit) records errors=1 so it counts as a
    failing, non-progress state rather than being ignored (C1 crash handling)."""
    text = result or ""
    p = _RE_PASSED.search(text)
    f = _RE_FAILED.search(text)
    e = _RE_ERRORS.search(text)
    if p or f or e:
        return (int(p.group(1)) if p else 0,
                int(f.group(1)) if f else 0,
                int(e.group(1)) if e else 0)
    looks_like_test = bool(_RE_TESTCMD.search(command or ""))
    if looks_like_test and (errored or "Traceback" in text or "Error" in text):
        return (0, 0, 1)  # ran tests, crashed before a summary → non-progress
    return None


# Map an inferred task_type onto a model slot. Cosmetic when every slot is the
# champion; meaningful once a slot points elsewhere. `/use` overrides per turn.
_SLOT_FOR_TASK = {
    "implement": "code",
    "bugfix": "code",
    "document": "code",
    "manage": "code",
    "summarize": "plan",
    "review": "chat",
}


@dataclass
class TurnPrep:
    """UI-agnostic per-turn state shared by the line REPL and the Textual TUI.
    `call(on_event, on_token, on_progress)` is the verbatim `run_single` closure
    (so both front-ends assemble the request identically); `note_tool` updates
    the collectors a front-end's tool callback must invoke."""
    call: Callable
    note_tool: Callable
    slot: str
    model: str
    dev_bash: bool
    # RoleConfig, not `object` — every consumer reads `.num_ctx`/`.tools` off
    # it, and the loose annotation hid 8 attribute errors behind a type that
    # promises nothing.
    role_cfg: RoleConfig
    run_id: str
    ctx_ceiling: int
    changed_files: list
    fingerprint: set
    test_result: list
    backend_name: str = ""  # multi-backend provenance stamped on the transcript
    # Completed tool calls observed via note_tool. finalize_turn's fallback for
    # an interrupted turn — the in-flight AgentResult is lost when ChatCancelled
    # unwinds, and the transcript used to claim steps=0/tool_calls=0 even when
    # tools had run (session 5bb630813c21 turn 11: one bash call, 9m40s hang,
    # nothing recorded).
    observed_calls: list = field(default_factory=list)
    # The Backend this turn dispatches on and its running spend when the turn
    # began: `settle_turn_cost` bills the DELTA, so requests billed by a turn
    # that then errored, aborted, or was interrupted still count (the old
    # per-result sum only ever saw completed turns).
    backend: object | None = None
    cost_start: float = 0.0


# Tools that are useless without a resident index (chat/project.py "none" mode).
_INDEX_TOOLS = {"bm25_search": "search", "find_symbol": "symbols"}


def index_tools_available() -> dict[str, bool]:
    """Which index-backed tools have their index resident right now."""
    from luxe import search as search_mod
    from luxe import symbols as symbols_mod

    return {"bm25_search": search_mod.get_index() is not None,
            "find_symbol": symbols_mod.get_index() is not None}


def _drop_unavailable_index_tools(role_cfg):
    """Strip `bm25_search` / `find_symbol` when their index isn't built."""
    available = index_tools_available()
    tools = [t for t in (role_cfg.tools or [])
             if t not in _INDEX_TOOLS or available.get(t, False)]
    if len(tools) == len(role_cfg.tools or []):
        return role_cfg
    return role_cfg.model_copy(update={"tools": tools})


def prepare_turn(message, session, slots, cfg, languages, infer,
                 *, plan_mode: bool = False, cancel=None,
                 on_tool_start=None) -> TurnPrep:
    """UI-agnostic turn setup: slot routing, role/tool/ledger wiring, `/ctx`
    clamp, `extra_context`, user-turn persistence, and the `run_single` closure.
    Shared by both front-ends; the benchmark path is untouched (these tools/args
    are chat-only). Rendering is the caller's job (see `TurnPrep`).

    `cancel` (CancelToken) makes the chat bash subprocess killable mid-flight;
    `on_tool_start(command)` fires at bash DISPATCH so the front-end can show
    the currently running command (completion-time events can't — a hung
    command was invisible). Both optional; None keeps prior behaviour."""
    from luxe.mcp.server import make_read_only_role
    from luxe.state.ledger import make_update_ledger_tool

    task_type = infer(message)
    pinned = session.pinned_slot
    slot = pinned or _SLOT_FOR_TASK.get(task_type, "chat")
    session.pinned_slot = None

    model = slots.model_for(slot)
    dev_bash = session.write_enabled and session.unrestricted_bash and not plan_mode

    backend = slots.backend_for(slot)
    slot_cfg = cfg.slot_config(slot)
    base_role = cfg.role(slot_cfg.role)
    # /plan (B5) forces a read-only drafting turn regardless of write mode.
    write_on = session.write_enabled and not plan_mode
    role_cfg = base_role if write_on else make_read_only_role(base_role)
    # No index (a session started outside a project) → withhold the tools that
    # need one, rather than offering them and answering "index not built" to
    # every call. Derived from what's actually resident, so `/index` mid-session
    # turns them back on with no other bookkeeping.
    role_cfg = _drop_unavailable_index_tools(role_cfg)
    # A model whose chat template can't render tool calls gets NO tool surface:
    # oMLX silently drops the tools array for it, so offering them would produce
    # an agent that never calls a tool and never says why (chat/modelcaps.py).
    caps = modelcaps.for_model(backend, model)
    session.tools_withheld = not caps.usable
    if not caps.usable:
        role_cfg = role_cfg.model_copy(update={"tools": []})

    # EVERY freeform interactive turn gets the conversational persona — chat is
    # a conversation, not a batch job. Keying this on `slot == "chat"` (the
    # 2026-06-30 version) was the "chats become coding sessions" bug: the slot
    # is picked by `_infer_task_type`, a keyword heuristic built for `luxe
    # maintain` goals, so ordinary messages containing "add"/"change"/
    # "explain"/"fix"/… routed to the code/plan slots and inherited the
    # baseline repo-maintenance persona (+ the config's task overlay) — repo
    # orientation loops and "final reports" for plain questions. Slot routing
    # still picks the MODEL (fan-out configs); the persona now follows the
    # turn KIND instead. The task personas remain for the explicit task modes:
    # /plan drafting (plan_mode), autonomous /goal rounds (goal_active), and a
    # user-pinned `/use <slot>` turn. The conversational prompt still does
    # real work — it reads/edits via tools when the message calls for it.
    # Prompt ids resolve in the registry (chat.sdd).
    #
    # The persona also SELF-IDENTIFIES as the model actually serving the turn
    # (2026-08-17), with luxe named as the harness — `chat_persona_id` returns
    # the model-bound id and the string itself stays in the registry (chat.sdd:
    # no prompt text in this module). An unknown model id returns the plain
    # `chat_conversational` id, i.e. the previous wording exactly.
    if pinned is None and not plan_mode and not session.goal_active:
        role_cfg = role_cfg.model_copy(update={
            "system_prompt_id": prompts_mod.chat_persona_id(model),
            "task_prompt_id": "chat_conversational",
            "task_overlay_id": "",
        })

    # Chat bash (chat.sdd), swapped for THIS run only via run_single's extra-tool
    # seam — benchmark/maintain never pass these, so their bash is untouched.
    # update_ledger (B0/B5) is always exposed so the model can maintain its
    # working state across rounds; it only mutates the per-session ledger file.
    _led_def, _led_fn = make_update_ledger_tool(session.session_id)
    extra_tool_defs = [_led_def]
    extra_tool_fns = {"update_ledger": _led_fn}
    # net_probe (2026-07-31): the bounded connectivity ladder, read-only and
    # in-process — the model reaches for THIS instead of hand-rolled curl
    # commands with no timeout (session 5bb630813c21). Rides every chat turn
    # like the ledger tool; benchmark/maintain never pass extra tools.
    from luxe.netdiag import make_net_probe_tool
    _net_def, _net_fn = make_net_probe_tool()
    extra_tool_defs.append(_net_def)
    extra_tool_fns["net_probe"] = _net_fn
    # planeproxy_diag (2026-08-02): bounded, read-only diagnosis of the user's
    # SSH-tunnel tool via its own `status --json`/`doctor --json` — the model
    # reaches for THIS instead of ssh -v / ps / log spelunking. Never runs
    # up/down (mutations are commands with consent flows, chat.sdd). Same
    # always-on seam as net_probe; benchmark/maintain never pass extra tools.
    from luxe.planeproxy import make_planeproxy_tool
    _pp_def, _pp_fn = make_planeproxy_tool()
    extra_tool_defs.append(_pp_def)
    extra_tool_fns["planeproxy_diag"] = _pp_fn
    # claude_code_diag (2026-08-13): bounded, read-only diagnosis of the user's
    # OTHER agent. luxe is the fallback dev tool, so "what is wrong with Claude
    # Code" lands here by construction — and a chat with no instrument could
    # only guess (a session that had reverted from the Max-plan login to the
    # Platform API key went undiagnosed). Reports env vars by NAME only and
    # reads no conversation content (claudecode.py). Same always-on seam.
    from luxe.claudecode import make_claude_code_tool
    _cc_def, _cc_fn = make_claude_code_tool()
    extra_tool_defs.append(_cc_def)
    extra_tool_fns["claude_code_diag"] = _cc_fn
    if write_on:
        from luxe.tools.shell import (
            make_bash_fn,
            restricted_bash_def,
            unrestricted_bash_def,
        )
        if session.unrestricted_bash:
            extra_tool_defs.append(unrestricted_bash_def())
            extra_tool_fns["bash"] = make_bash_fn(
                unrestricted=True, cancel=cancel, on_start=on_tool_start)
        else:
            extra_tool_defs.append(restricted_bash_def())
            extra_tool_fns["bash"] = make_bash_fn(
                restricted_hint=True, cancel=cancel, on_start=on_tool_start)
        # Chat-only: prose files (.txt/.md/…) skip the placeholder honesty
        # guard — "save these notes with '# TODO: implement X'" is content the
        # user dictated, not a code stub (the guard's target). Code extensions
        # stay guarded; benchmark/maintain keep the default TOOL_FNS
        # (tools.sdd). Same per-turn seam as the bash swap above.
        from luxe.tools.fs import make_prose_aware_write_fns
        extra_tool_fns.update(make_prose_aware_write_fns())
    else:
        # Read-only: the mutation tools are stripped from the DEFS (the model
        # is not offered them), but a model that calls one anyway used to get
        # `Unknown tool: edit_file` — which is false. The tool exists and is
        # gated, and nothing in that message says so or names `/write`, so the
        # turn just ends with the work undone (observed 2026-08-11, session
        # 0e524f033300 run -14: a full file body handed to `edit_file`,
        # rejected as unknown, turn over). Registering a stub FN with no DEF
        # keeps the tool invisible in the surface while making the rejection
        # explain itself. Chat-only: the benchmark path passes no extra tools.
        from luxe.tools.fs import make_write_gated_fns
        extra_tool_fns.update(make_write_gated_fns())

    # Web tools (chat-only, `/web`, default OFF — web.sdd). Independent of
    # write mode: fetching a page mutates nothing locally. `web_search` is
    # included only when a provider key resolves. Benchmark/maintain never
    # pass extra tools, so their surface is unchanged and still offline.
    if session.web_enabled:
        from luxe.web.tools import web_tools
        _web_defs, _web_fns = web_tools()
        extra_tool_defs.extend(_web_defs)
        extra_tool_fns.update(_web_fns)

    # MCP tools (cli `--mcp`, chat-only): inspection tools ride every turn;
    # tools matching the server's `gate_tools` (mutating remote operations)
    # follow the same /write gate as the native mutation surface. Withheld
    # entirely for a model whose template can't call tools.
    mcp_surface = mcptools.active()
    if mcp_surface is not None and caps.usable:
        extra_tool_defs.extend(mcp_surface.always_defs)
        if write_on:
            extra_tool_defs.extend(mcp_surface.gated_defs)
        extra_tool_fns.update(mcp_surface.fns_for(write_on))

    # `/ctx` size override (chat-only) — clamp to the effective ceiling
    # (role's box ceiling ∧ the manifest's per-model cap for the model this
    # turn actually runs) so a tier request can never exceed what this
    # box/model pair can hold — including after an auto-degrade.
    # The ceiling is now the ENDPOINT's truth when it has one (a hosted model's
    # catalog `context_length`), else the box's `num_ctx_max` ∧ manifest cap.
    # The unset-override default also moves on a billable endpoint, where the
    # window is a cost bound rather than a RAM one (slots.default_num_ctx).
    ctx_ceiling = slots.ctx_ceiling(slot)
    requested_ctx = session.num_ctx_override or slots.default_num_ctx(slot)
    if requested_ctx:
        effective_ctx = min(requested_ctx, ctx_ceiling)
        if effective_ctx != role_cfg.num_ctx:
            role_cfg = role_cfg.model_copy(update={"num_ctx": effective_ctx})

    # Ctx-derived tool-output budget. The fixed 256 KB read cap predates the
    # /ctx tiers and is 480% of the DEFAULT 32K window measured in real tokens,
    # so one oversized read can blow the context in a single call. Set per turn
    # because `/ctx` moves num_ctx mid-session (maintain sets it once per
    # pipeline; the two call sites stay separate).
    #
    # DEFAULT-ON for chat since 2026-08-24. It shipped opt-in on 2026-08-12
    # against a deliberate asymmetry — maintain went default-ON on its own
    # maintain_suite A/B and chat had NO evidence of its own, so aligning the
    # grammars was forbidden until chat produced some. It has:
    # acceptance/chat_bigread_2026_08_24/REPORT.md — planted repo (250,040 B
    # markdown + 70,028 B source), m1/Qwen3.6-35B-A3B-4bit, both windows, both
    # arms. OFF hung unrecoverably at 32768 (peak pressure 1064.2%) AND at
    # 131072 (266.0%) — the drill's own timeout had to kill the process group.
    # ON completed both (60.0s / 79.9s, peak 50.6% / 39.0%, 2 refused reads per
    # arm) and the model spent the `offset=` resume the clipped read hands it
    # (1 and 2 calls) rather than giving up — 3 extra tool calls total, which
    # answers the plan's worry that a budget turns one fatal turn into three
    # timid ones. Two real 2026-08-24 incidents (sessions 168f1825a1fd,
    # eb0b2923a3eb) are the same shape with refused_reads=0.
    #
    # Grammar is now the maintain/`LUXE_TRUNCATED_TURN_RETRY` opt-out one,
    # spelled identically: unset → ON, only the EXACT string "0" disables
    # ("", "true", "01", " 1" → ON). Off still means the fixed constants.
    if os.environ.get("LUXE_TOOL_BUDGET_CTX", "1") != "0":
        fs_mod.set_read_budget(fs_mod.budget_for_ctx(role_cfg.num_ctx))
    else:
        fs_mod.set_read_budget(None)

    # Large-but-readable annotations in `list_dir`/`glob` (2026-08-24,
    # EVIDENCE.md finding 6: a 257,988 B file at 0.98x the refusal cap listed
    # as a bare name, then read whole, and the turn was lost). ON for chat,
    # unconditionally — chat is not a benched path, so this needs no flag.
    #
    # It is set HERE, per turn and beside the read budget, for the same reason
    # that one is: `/ctx` moves num_ctx mid-session, and both toggles are
    # process-global module state that any other caller in this process may
    # have moved.
    #
    # `set_large_file_notes` defaults OFF in `tools/fs.py` and `maintain.py`
    # deliberately has NO such call: the bracket is not benchmark-path-neutral
    # (the maintain_suite fixture cache holds files between half and the whole
    # of the 262,144 B cap, and `nothing-ever-happens` backs doc fixtures whose
    # task lists `docs/` directly). The bench path therefore stays
    # byte-identical by construction — it never turns this on.
    fs_mod.set_large_file_notes(True)

    extra_context, fold_version = session.build_extra_context(message)

    turn_idx = len(session.turns)
    # Not `len(session.turns)` alone: `/clear` empties the list, and a run id
    # reused after it would overwrite the earlier run's events on disk.
    run_id = f"{session.session_id}-{turn_idx + session.turn_offset}"
    session_store.append_turn(session.session_id, "user", text=message, slot=slot)
    if extra_context:
        session_store.append_fold(session.session_id, turn_idx, fold_version, extra_context)

    # Per-turn collectors feeding the ledger (B0/B5) and the goal supervisor (B4):
    # files actually written/edited, and a fingerprint of tool calls (name + the
    # salient arg) for stuck-loop detection.
    changed_files: list[str] = []
    fingerprint: set = set()
    test_result: list = [None]  # latest (passed, failed, errors) seen this turn (C1)
    observed_calls: list = []   # completed calls; interrupt-stats fallback

    def _note_tool(tc) -> None:
        observed_calls.append(tc.name)
        args = getattr(tc, "arguments", {}) or {}
        prim = (args.get("path") or args.get("query")
                or args.get("command") or args.get("pattern"))
        fingerprint.add((tc.name, str(prim) if prim is not None else ""))
        if (tc.name in ("write_file", "edit_file")
                and not getattr(tc, "error", None)
                and not getattr(tc, "duplicate", False)):
            p = args.get("path")
            if p:
                changed_files.append(str(p))
        # C1 observable telemetry: capture the latest test result from any tool's
        # output (pytest usually runs via bash).
        tr = parse_test_result(str(args.get("command", "")),
                               getattr(tc, "result", "") or "",
                               bool(getattr(tc, "error", None)))
        if tr is not None:
            test_result[0] = tr

    def _call(on_event, on_token=None, on_progress=None, on_notice=None):
        return run_single(
            backend, role_cfg, goal=message, task_type=task_type,
            languages=languages, extra_tool_defs=extra_tool_defs,
            extra_tool_fns=extra_tool_fns, on_tool_event=on_event,
            on_token=on_token, on_progress=on_progress, on_notice=on_notice,
            run_id=run_id, phase="chat", extra_context=extra_context,
        )

    return TurnPrep(
        call=_call, note_tool=_note_tool, slot=slot, model=model,
        dev_bash=dev_bash, role_cfg=role_cfg, run_id=run_id,
        ctx_ceiling=ctx_ceiling, changed_files=changed_files,
        fingerprint=fingerprint, test_result=test_result,
        backend_name=getattr(slots, "backend_name", ""),
        observed_calls=observed_calls,
        backend=backend,
        cost_start=cost_mod.backend_spend(backend),
    )


def finalize_turn(session, prep: TurnPrep, result, *, interrupted: bool,
                  message: str, started_at: float, ended_at: float,
                  partial_text: str = "") -> TurnOutcome:
    """UI-agnostic post-turn bookkeeping: record changed files, persist the
    assistant turn, update in-memory history, and build the `TurnOutcome` (incl.
    rendering inputs) the front-end renders from. Files are recorded even on an
    interrupted turn — those writes already happened on disk.

    On interrupt the in-flight AgentResult is lost (ChatCancelled unwinds
    run_single), so the transcript record falls back to what the front-end
    observed: `prep.observed_calls` for the tool-call count and `partial_text`
    (the streamed prose so far) for the body — "partial turn saved" used to
    save an empty record (steps=0/tool_calls=0/len=0) no matter what ran."""
    if prep.changed_files:
        ledger_mod.record_files(session.session_id, prep.changed_files)
        # The contract scan is cached per repo root for the session (it walks
        # the whole root — 19s when that root is $HOME). A turn that wrote a
        # `.sdd` invalidates it so the next turn sees the new contract.
        if any(str(p).endswith(".sdd") for p in prep.changed_files):
            spec_resolver.invalidate_scan_cache(session.repo_path or None)

    # The visible answer is every step's prose, not just the last step's:
    # `final_text` alone made replies start mid-thought when the model spoke
    # before acting (render.compose_answer). Chat-only — `result.final_text`
    # is unchanged and is still what the benchmark path reads.
    assistant_text = compose_answer(result) if result else ""
    if not assistant_text and interrupted:
        assistant_text = partial_text or ""
    # Chat-only hygiene (the benchmark path never runs through here): drop a
    # leaked reasoning block so transcripts, session memory, and the rendered
    # answer all carry the real reply — see render.strip_leaked_reasoning.
    assistant_text = strip_leaked_reasoning(assistant_text)
    observed = len(prep.observed_calls)
    tool_calls_total = result.tool_calls_total if result else observed
    session_store.append_turn(
        session.session_id, "assistant",
        text=assistant_text, run_id=prep.run_id, interrupted=interrupted,
        steps=(result.steps if result else 0),
        tool_calls=tool_calls_total,
        backend=prep.backend_name,
    )
    # Server-truth turn record for post-hoc ctx forensics — the status bar's
    # number is otherwise unlogged (the TUI swallows stdout; debug.log is the
    # only surface that survives the session).
    if result is not None:
        num_ctx = prep.role_cfg.num_ctx
        last_pt = getattr(result, "last_prompt_tokens", 0) or 0
        ctx_srv = last_pt / num_ctx if last_pt and num_ctx else 0.0
        logger.debug(
            "turn done run_id=%s steps=%d tool_calls=%d prompt_tokens=%d "
            "last_prompt_tokens=%d num_ctx=%d ctx_server=%.1f%% ctx_est=%.1f%% "
            "peak_est=%.1f%%",
            prep.run_id, result.steps, result.tool_calls_total,
            result.prompt_tokens, last_pt, num_ctx,
            ctx_srv * 100, result.final_context_pressure * 100,
            result.peak_context_pressure * 100)
    else:
        logger.debug("turn interrupted run_id=%s observed_tool_calls=%d "
                     "partial_chars=%d", prep.run_id, observed,
                     len(assistant_text))
    session_store.touch(session.session_id)
    # An /attach payload rides exactly one turn. A turn that did not complete
    # (interrupted, or aborted by the loop) keeps it staged so `/retry` — or
    # simply the next message — resends it instead of silently losing it.
    if interrupted or result is None or getattr(result, "aborted", False):
        restore_attachments(session)
    else:
        session.consumed_attachments = []
    session.add_turn(ChatTurn(
        user=message, assistant=assistant_text, slot=prep.slot,
        model=prep.model, run_id=prep.run_id,
    ))
    return TurnOutcome(
        tool_calls=tool_calls_total,
        files_changed=len(set(prep.changed_files)),
        final_text=assistant_text,
        interrupted=interrupted,
        fingerprint=frozenset(prep.fingerprint),
        test_result=prep.test_result[0],
        result=result,
        slot=prep.slot,
        model=prep.model,
        num_ctx=prep.role_cfg.num_ctx,
        ctx_ceiling=prep.ctx_ceiling,
        started_at=started_at,
        ended_at=ended_at,
    )


def note_aborted_turn(session, slots, result) -> tuple[str, str | None] | None:
    """Record a turn the agent loop ABORTED without raising, and describe it.

    `agents/loop.py` reports a backend failure by setting `aborted` /
    `abort_reason` on the result and returning normally — semantics the
    benchmark, maintain, compare and smoke paths all consume. The interactive
    front-ends only ever handled `BackendError`, so an aborted turn rendered as
    a SUCCESSFUL empty reply: session 3aabb18b0e07 (2026-08-23) holds five
    assistant records with `text: ""` and no error record at all, while every
    request was dying in a dead system proxy.

    Returns `(message, hint)` for the caller to render in its own idiom, or
    None when the turn was not aborted. The `kind="error"` transcript record
    and the log line are written HERE so both front-ends persist them
    identically.
    """
    if not getattr(result, "aborted", False):
        return None
    reason = getattr(result, "abort_reason", "") or "the turn aborted without a reason"
    logger.error("turn aborted: %s", reason)
    session_store.append_turn(session.session_id, "error",
                              text=reason, model=slots.backend.model)
    # Only an endpoint failure gets the kit's recovery — "Max steps reached
    # (…)" is not one. Keyed on the reason text the way `agents/outcomes.py`
    # classifies the same field.
    if not is_backend_abort(result):
        return reason, None
    # The loop CONTAINS backend exceptions (`loop.py`, `except Exception` around
    # `backend.chat`), so the front-ends' `except BackendError` branches never
    # see a failure raised mid-turn — the self-repair and manifest auto-degrade
    # they call were unreachable from a real turn. Run the same sequence here.
    return reason, recover_backend_failure(slots, reason)


def is_backend_abort(result) -> bool:
    """True when the loop aborted this turn because the ENDPOINT failed."""
    reason = getattr(result, "abort_reason", "") or ""
    return bool(getattr(result, "aborted", False)) and "backend error" in reason.lower()


def recover_backend_failure(slots, reason: str) -> str | None:
    """The kit's recovery for a turn that failed on the backend, in order:
    self-repair FIRST (luxe.repair — a stale oMLX fails main AND fallback
    with the same lazy import, so degrading would be the wrong diagnosis),
    then manifest auto-degrade (a healthy endpoint whose main model fails a
    turn switches the session to the fallback, loudly), then the `/backend`
    escape hatch. Returns the one line to show, or None.

    The ONE sequence every failure path runs — a raised BackendError, a
    loop-contained abort, a failed /goal round — so no path can skip a step
    the others take."""
    return (slots.try_self_repair(reason) or slots.note_turn_failure()
            or slots.unreachable_hint())


def note_backend_error(session, slots, exc) -> tuple[str, str | None]:
    """Record a BackendError that escaped a turn and run the kit's recovery.

    Writes the kind="error" transcript record + log line both front-ends used
    to write separately, re-stages any consumed `/attach` payload, and
    returns `(message, hint)` for the caller to render in its own idiom."""
    text = str(exc)
    logger.error("turn BackendError: %s", text)
    session_store.append_turn(session.session_id, "error",
                              text=text, model=slots.backend.model)
    restore_attachments(session)
    return text, recover_backend_failure(slots, text)


def note_turn_crash(session, what: str = "turn") -> str:
    """Record an unexpected exception from a turn or command; returns the
    last traceback line for the screen. Call from inside the `except`."""
    tb = traceback.format_exc()
    exc_line = tb.strip().splitlines()[-1]
    logger.error("chat %s crashed\n%s", what, tb)
    session_store.append_turn(session.session_id, "error", text=exc_line)
    restore_attachments(session)
    return exc_line


def restore_attachments(session) -> int:
    """Re-stage the `/attach` payload the failed turn consumed. Returns how
    many attachments are staged afterwards."""
    consumed = getattr(session, "consumed_attachments", None) or []
    if consumed and not session.attachments:
        session.attachments = list(consumed)
    session.consumed_attachments = []
    return len(session.attachments)


def attachments_kept_note(session) -> str:
    """One line saying a failed turn's attachments are still staged, or ""."""
    n = len(getattr(session, "attachments", None) or [])
    if not n:
        return ""
    return (f"· {n} attachment{'s' if n != 1 else ''} still staged — "
            "/retry (or your next message) resends "
            f"{'them' if n != 1 else 'it'}")


def settle_turn_cost(session, prep: "TurnPrep", status=None) -> float:
    """Bill this turn's spend: whatever the Backend was billed since the turn
    began, however the turn ended. Call from a `finally`."""
    return cost_mod.record_spend(
        session,
        cost_mod.backend_spend(prep.backend) - prep.cost_start,
        status)
