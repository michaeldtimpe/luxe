"""Allowlisted shell execution — scoped to repo root.

Hardened 2026-05-02 against allowlist-bypass via shell chains. The
original implementation checked `parts[0]` against the allowlist
and ran `command` via `shell=True`, which meant a command like
`cat foo && rm -rf /` passed the check (parts[0] == 'cat') and
then the shell still executed `rm`. The fix uses `shlex.split` to
tokenize the command, then rejects any chain operator (`&&`, `||`,
`;`, `|`, `&`) or redirect (`>`, `<`, `>>`) or command-substitution
(`` ` ``, `$(`) tokens that would bypass the allowlist. The model
can issue multiple bash calls if it needs sequential commands;
write_file/read_file are the right tools for what redirects would
otherwise do.

Glob expansion and other shell features still work — `shell=True`
is preserved for the single, allowlisted binary.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Any

from luxe.tools.base import ToolDef, ToolFn
from luxe.tools.fs import get_repo_root

_ALLOWLIST = frozenset({
    "cargo", "cat", "echo", "find", "git", "go", "grep", "head",
    "ls", "make", "npm", "npx", "pip", "pytest", "python", "ruff",
    "sed", "sort", "tail", "tree", "wc",
})

# CHAT-ONLY additions to the allowlist (2026-08-05): applied by `make_bash_fn`
# and advertised by `restricted_bash_def`, never by the module-level defaults —
# a benchmark that can reach GitHub's API is no longer reproducible, and the
# default tool description (which lists _ALLOWLIST) is golden-pinned. `gh`
# covers the PR/checks/issues half of the git workflow during an outage
# without requiring /bash unrestricted.
_CHAT_EXTRA_ALLOW = frozenset({"gh"})

# Tokens that would cause the shell to run additional binaries beyond
# the one we allowlisted. Detected as standalone tokens after shlex.split,
# so quoted arguments containing these characters (e.g. a regex pattern)
# don't trip the check.
_CHAIN_TOKENS = frozenset({"&&", "||", ";", "|", "&"})
_REDIRECT_TOKENS = frozenset({">", "<", ">>", "<<", "<<<", "&>", "2>", "2>&1"})

_MAX_OUTPUT = 8192
#: Head/tail split of the output budget when a command exceeds _MAX_OUTPUT.
#: The tail gets the larger share: test runners put the failure summary at
#: the END (pytest's short summary, exit status context), so the old
#: keep-the-first-8KB form reliably discarded exactly the lines the model
#: needed (2026-08-12 audit, F12). The head is kept too — first errors and
#: the command's own echo live there.
_HEAD_KEEP = 2048
_TAIL_KEEP = 6144
_TIMEOUT = 60


def _clip_output(output: str) -> str:
    """Cap tool output at _MAX_OUTPUT, keeping the head AND the tail.

    Under the cap: byte-identical passthrough. Over it: first _HEAD_KEEP and
    last _TAIL_KEEP bytes (snapped to line boundaries where possible) around
    an announced omission marker. Bench-inert by measurement, not assumption:
    zero of 183 bash calls across six full maintain_suite passes (2026-08-12)
    reach the cap — the 205 historical firings are SWE-bench-era workloads.
    See acceptance/bash_tail_trunc_2026_08_12/REPORT.md.
    """
    if len(output) <= _MAX_OUTPUT:
        return output
    head = output[:_HEAD_KEEP]
    nl = head.rfind("\n")
    if nl > 0:
        head = head[:nl + 1]
    tail = output[-_TAIL_KEEP:]
    nl = tail.find("\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1:]
    omitted = len(output) - len(head) - len(tail)
    return (f"{head}... ({omitted:,} bytes omitted — output capped at "
            f"{_MAX_OUTPUT:,}; head and tail kept)\n{tail}")


def _validate_command(command: str) -> tuple[list[str], str | None]:
    """Tokenize via shlex and reject anything that would let a non-allowlisted
    binary execute. Returns (tokens, error). On error, tokens is empty.

    shlex.split respects quoting so `grep "a|b" file` is 3 tokens
    (`grep`, `a|b`, `file`) — not 5 — and the literal `|` inside the
    quoted regex doesn't trip the chain check.
    """
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return [], f"Failed to parse command (mismatched quotes?): {e}"
    if not tokens:
        return [], "Empty command"

    bad_chain = [t for t in tokens if t in _CHAIN_TOKENS or t in _REDIRECT_TOKENS]
    if bad_chain:
        return [], (
            f"Shell chain/redirect operators not allowed: {bad_chain}. "
            "Issue separate bash calls for each command, or use "
            "read_file/write_file/edit_file instead of redirects."
        )

    # Command substitution — `cat $(echo /etc/passwd)` would let any binary
    # execute via the inner shell. Reject backticks and $(.
    for t in tokens:
        if "`" in t or "$(" in t:
            return [], (
                f"Command substitution not allowed in token {t!r}. "
                "Inner commands bypass the bash allowlist."
            )

    return tokens, None


# Dev-mode (unrestricted) runs need a longer budget — `pip install`, `npm ci`,
# and builds routinely exceed the 60s allowlisted ceiling.
_UNRESTRICTED_TIMEOUT = 600

# Cancellation poll cadence for the chat-only Popen runner (_run_cancellable).
_CANCEL_POLL_S = 0.2


def _run_cancellable(command: str, *, cwd, env, timeout: int,
                     cancel) -> tuple[str, str | None]:
    """Chat-only subprocess runner that honours a CancelToken mid-flight.

    `subprocess.run` blocks the worker thread until the command exits or the
    timeout fires — with the 600s dev-mode budget, one hung curl made esc a
    ten-minute wait (session 5bb630813c21, 2026-07-31). This variant polls a
    duck-typed token (any object with a truthy `.requested`) and kills the
    WHOLE process group (start_new_session) so `sh -c` children die with the
    shell. Only reachable via make_bash_fn(cancel=...); the benchmark path
    keeps subprocess.run in _bash, byte-identical."""
    import os
    import signal as _signal
    import time as _time

    proc = subprocess.Popen(
        command, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        errors="replace",   # one bad byte must not discard the whole output
        stdin=subprocess.DEVNULL,   # see _bash — same reason
        cwd=cwd, env=env, start_new_session=True,
    )

    def _kill() -> None:
        try:
            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()

    deadline = _time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=_CANCEL_POLL_S)
            output = _clip_output((stdout or "") + (stderr or ""))
            return output, None if proc.returncode == 0 else f"exit code {proc.returncode}"
        except subprocess.TimeoutExpired:
            if getattr(cancel, "requested", False):
                _kill()
                proc.communicate()  # reap; pipes already buffered
                return "", "cancelled by user"
            if _time.monotonic() >= deadline:
                _kill()
                proc.communicate()
                return "", f"Command timed out after {timeout}s"


def _bash(args: dict[str, Any], *, unrestricted: bool = False,
          env: dict[str, str] | None = None,
          cancel=None,
          extra_allow: frozenset[str] = frozenset()) -> tuple[str, str | None]:
    """`env=None` (the default, and the ONLY value the benchmark path ever
    passes) inherits the process environment unchanged — byte-identical
    behaviour. The chat front-end passes an augmented env (see
    make_bash_fn). `cancel=None` (benchmark default) keeps the original
    subprocess.run path; a token switches to the cancellable Popen runner.
    `extra_allow` (empty on the benchmark default, so the rejection message
    is byte-identical) widens the allowlist for chat sessions only."""
    repo_root = get_repo_root()
    if repo_root is None:
        return "", "Repo root not set"

    command = args["command"]
    if unrestricted:
        # Opt-in `luxe chat` dev mode (chat.sdd): no allowlist, no chain/redirect
        # rejection. cwd is the repo root but this is NOT a sandbox.
        if not command.strip():
            return "", "Empty command"
        timeout = _UNRESTRICTED_TIMEOUT
    else:
        tokens, err = _validate_command(command)
        if err:
            return "", err
        binary = tokens[0]
        if binary not in _ALLOWLIST and binary not in extra_allow:
            return "", (f"Command '{binary}' not in allowlist. "
                        f"Allowed: {sorted(_ALLOWLIST | extra_allow)}")
        timeout = _TIMEOUT

    if cancel is not None:
        return _run_cancellable(command, cwd=repo_root, env=env,
                                timeout=timeout, cancel=cancel)

    try:
        proc = subprocess.run(
            command, shell=True,
            capture_output=True, text=True,
            # A command whose output is not valid UTF-8 (a stray byte in a log,
            # a binary blob catted by accident) used to raise
            # UnicodeDecodeError out of subprocess and throw away EVERYTHING it
            # printed — the model saw a tool error instead of the 8 KB of
            # perfectly readable text around the bad byte.
            errors="replace",
            # stdin=DEVNULL is LOAD-BEARING (2026-08-12), for the same reason
            # ripgrep needed it in fs.py: the child inherits luxe's stdin. With
            # a piped parent stdin (a benchmark launched from a script, CI, the
            # headless `printf … | luxe chat` form) `bash("cat")` returned the
            # PARENT's queued input as its own output and drained it — the next
            # REPL read got nothing. Under a TTY the same command blocks until
            # the timeout. Neither is a shell command's job.
            stdin=subprocess.DEVNULL,
            cwd=repo_root, timeout=timeout,
            env=env,   # None (bench default) == inherit, unchanged
        )
        output = _clip_output(proc.stdout + proc.stderr)
        return output, None if proc.returncode == 0 else f"exit code {proc.returncode}"
    except subprocess.TimeoutExpired:
        return "", f"Command timed out after {timeout}s"


# Substrings that identify a rejection that ONLY happens because unrestricted
# shell is off — i.e. a flag-state failure, not a real error. (Mismatched quotes,
# empty command, timeouts, and non-zero exits are real and not augmented.)
_FLAG_GATED_MARKERS = ("not in allowlist", "operators not allowed",
                       "substitution not allowed")


def _chat_bash_env() -> dict[str, str]:
    """Chat-only subprocess env: luxe's venv bin prepended to PATH so
    `python`/`python3`/`pytest` always resolve to an interpreter that HAS
    pytest. Motivation (2026-07-30 m1-vs-m5 head-to-head): the m5's default
    PATH had no `python`, so a write-mode agent could build but not run the
    tests — exit 127 in the exact "fix it and verify" flow the fallback kit
    exists for. Host PATH still wins for everything not in the venv."""
    import os
    import sys
    from pathlib import Path

    venv_bin = str(Path(sys.executable).parent)
    env = dict(os.environ)
    env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
    return env


def make_bash_fn(*, unrestricted: bool = False, restricted_hint: bool = False,
                 cancel=None, on_start=None) -> ToolFn:
    """Build a CHAT bash tool fn (the benchmark path uses the module-level
    default, which never comes through here). `unrestricted=True` (chat dev
    mode) drops the allowlist + chain/redirect guards. `restricted_hint=True`
    (chat write mode, bash still allowlisted) front-loads a flag-state
    explanation onto any allowlist/chain/substitution rejection so the output
    reflects WHY it failed and how to fix it — instead of the model silently
    retrying variants. Both variants run with the venv-augmented PATH
    (_chat_bash_env) so test runs work on every fleet host.

    `cancel` (duck-typed: `.requested` bool) makes the subprocess killable
    mid-flight — esc lands within ~0.2s instead of waiting out the timeout.
    `on_start` is called with the command string BEFORE execution so the
    front-end can show what is currently running (a hung command used to be
    invisible: every existing record fires on completion). Both are chat-only
    seams; guarded so a broken callback never fails the tool."""
    env = _chat_bash_env()

    def _fn(args: dict[str, Any]) -> tuple[str, str | None]:
        if on_start is not None:
            try:
                on_start(str(args.get("command", "")))
            except Exception:
                pass
        # _CHAT_EXTRA_ALLOW applies to every chat bash (this builder is never
        # on the benchmark path); it is a no-op in unrestricted mode.
        out, err = _bash(args, unrestricted=unrestricted, env=env, cancel=cancel,
                         extra_allow=_CHAT_EXTRA_ALLOW)
        if err and restricted_hint and any(m in err for m in _FLAG_GATED_MARKERS):
            err = ("restricted shell — enable unrestricted dev mode with /bash "
                   "(or start `luxe chat --dev`). Original: " + err)
        return out, err
    return _fn


def restricted_bash_def() -> ToolDef:
    """Allowlisted bash def for chat WRITE (non-dev) mode — same surface as the
    default, but the description tells the model to surface the flag state rather
    than retry rejected commands forever."""
    return ToolDef(
        name="bash",
        description=(
            f"Run a shell command (allowlisted binaries: "
            f"{', '.join(sorted(_ALLOWLIST | _CHAT_EXTRA_ALLOW))}; "
            "no chains/pipes/redirects). Scoped to repo root. If a command is "
            "rejected for the allowlist or a shell operator, do NOT keep retrying "
            "variants — tell the user to enable unrestricted shell with /bash."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
            },
            "required": ["command"],
        },
    )


def unrestricted_bash_def() -> ToolDef:
    """Tool def advertised in `luxe chat` dev mode so the model knows it can run
    any command (venv/pip/build/test), not just the allowlisted binaries."""
    return ToolDef(
        name="bash",
        description=(
            "Run ANY shell command for development work — chains (&&, |), "
            "redirects (>), venv activation, pip/npm install, builds, and tests "
            "are all allowed. Runs with the working directory at the repo root "
            "but is NOT sandboxed, so avoid destructive commands outside the repo."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
            },
            "required": ["command"],
        },
    )


def tool_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="bash",
            description=f"Run a shell command (allowlisted binaries: {', '.join(sorted(_ALLOWLIST))}). Scoped to repo root.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                },
                "required": ["command"],
            },
        ),
    ]


TOOL_FNS: dict[str, ToolFn] = {
    "bash": _bash,
}
