"""/help rendering — section/command tables with global column alignment."""

from __future__ import annotations

from rich.console import Group
from rich.text import Text


_HELP_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Core", [
        ("/help",                 "show this message"),
        ("/agents",                "list configured agents"),
        ("/models",                "list installed Ollama models"),
        ("/variants [family]",     "show released sizes per family"),
        ("/pull <tag>",            "download a model from the Ollama registry"),
        ("/context",               "show loaded/max context and RAM per agent"),
        ("/quit · /exit",          "leave luxe"),
        ("/clear",                 "drop the sticky agent"),
    ]),
    ("Tasks", [
        ("/tasks",                 "list recent tasks"),
        ("/tasks <goal>",          "plan + run in the background"),
        ("/tasks --sync <goal>",   "plan + run synchronously"),
        ("/tasks status [id]",     "snapshot of a task's status table"),
        ("/tasks log [id]",        "print last events from log.jsonl"),
        ("/tasks tail [id]",       "live-follow a task's event stream"),
        ("/tasks watch [id]",      "auto-refreshing dashboard"),
        ("/tasks abort [id]",      "signal a running background task"),
        ("/tasks save [id]",       "assemble subtasks into a report"),
    ]),
    ("Code intelligence", [
        ("/review <git-url>",      "clone/pull, review for flaws/bugs/security"),
        ("/refactor <git-url>",    "clone/pull, suggest optimizations"),
    ]),
    ("Tool library", [
        ("/tools",                 "list saved tools"),
        ("/tools show <name>",     "print source of a saved tool"),
        ("/tools remove <name>",   "delete a saved tool"),
    ]),
    ("Direct dispatch", [
        ("/general <prompt>",      "chat, Q&A"),
        ("/lookup <prompt>",       "quick factual lookup, snippet-only"),
        ("/research <prompt>",     "deep web investigation"),
        ("/calc <prompt>",         "arithmetic, estimation"),
        ("/writing <prompt>",      "prose, drafts, in-folder fs"),
        ("/code <prompt>",         "read/edit/run source"),
        ("/image <prompt>",        "generate an image"),
    ]),
    ("Turn control", [
        ("/retry",                 "rerun last prompt, same agent"),
        ("/redo <agent>",          "rerun last prompt, different agent"),
        ("/model <tag>",           "one-off model override next turn"),
        ("/params <text>",         "force banner params value"),
        ("/pin <text>",            "sticky note prepended to every prompt"),
        ("/pins",                  "list current pins"),
        ("/unpin [n]",             "remove pin n"),
        ("/history [n]",           "show last n session events"),
    ]),
    ("Sessions", [
        ("/session",               "current session id and path"),
        ("/save <name>",           "bookmark current session"),
        ("/sessions",              "list saved sessions"),
        ("/resume <id-or-name>",   "switch to another session"),
        ("/new",                   "start a fresh session"),
    ]),
    ("Memory & aliases", [
        ("/memory",                "open ~/.luxe/memory.md in $EDITOR"),
        ("/memory view",           "print current memory"),
        ("/memory clear",          "delete memory"),
        ("/alias add <name> <expansion>", "define a shortcut"),
        ("/alias list",            "list aliases"),
        ("/alias remove <name>",   "remove an alias"),
    ]),
]


BUILTIN_CMDS = {
    "/help", "/agents", "/session", "/models", "/quit", "/exit",
    "/retry", "/redo", "/history", "/model", "/params", "/pin", "/pins", "/unpin",
    "/save", "/sessions", "/resume", "/new", "/clear",
    "/memory", "/alias", "/variants", "/pull", "/context", "/tasks",
    "/review", "/refactor", "/tools",
}


def _render_help() -> "Group":
    """Build the /help block with a GLOBAL right-column alignment so the
    description column starts at the same visual column across every
    section, not just within each section. Commands flow through Text
    objects (not markup strings) so literal `[family]`/`[id]`/`[n]`
    aren't parsed by Rich as style tags."""
    max_cmd_w = max(len(cmd) for _, rows in _HELP_SECTIONS for cmd, _ in rows)
    pad = max_cmd_w + 2  # 2-space gutter before the description

    blocks: list = []
    for title, rows in _HELP_SECTIONS:
        blocks.append(Text.from_markup(f"[bold orange1]{title}[/bold orange1]"))
        for cmd, desc in rows:
            line = Text("  ")
            line.append(cmd, style="cyan")
            line.append(" " * (pad - len(cmd)))
            line.append(desc, style="default")
            blocks.append(line)
        blocks.append(Text(""))
    return Group(*blocks)
