"""Allowlisted shell execution — scoped to repo root.

Hardened 2026-05-02 against allowlist-bypass via shell chains. The
original implementation checked `parts[0]` against the allowlist
and ran `command` via `shell=True`, which meant a command like
`cat foo && rm -rf /` passed the check (parts[0] == 'cat') and
then the shell still executed `rm`. The fix tokenizes the command
(shell punctuation split out, `_shell_tokens`, 2026-09-26 — plain
`shlex.split` missed operators glued to words, `echo a;touch X`),
then rejects any chain operator (`&&`, `||`, `;`, `|`, `&`),
redirect (`>`, `<`, `>>`), unquoted newline, or command-substitution
(`` ` ``, `$(`) that would bypass the allowlist. The model
can issue multiple bash calls if it needs sequential commands;
write_file/read_file are the right tools for what redirects would
otherwise do.

Glob expansion and other shell features still work — `shell=True`
is preserved for the single, allowlisted binary.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
from collections import Counter
from typing import Any

from luxe.secrets import scrubbed_env
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


#: Characters sh treats as control/redirection operators when UNQUOTED.
_OPERATOR_CHARS = frozenset(";&|<>")


def _shell_tokens(command: str) -> list[str]:
    """Tokenize like sh does for operators (2026-09-26).

    `shlex.split` splits on whitespace only, so an operator glued to a word
    stayed inside it: `echo a;touch X` was `['echo', 'a;touch', 'X']`, passed
    the chain check, and sh ran `touch`. `punctuation_chars` makes shlex split
    unquoted `();<>|&` runs into their own tokens (`['echo', 'a', ';', …]`)
    while quoted text stays one token (`grep "a|b"` → `a|b`). `commenters` is
    cleared because `shlex.shlex` — unlike `shlex.split` — treats `#` as a
    comment even mid-word: `echo a#;touch X` tokenized as `['echo', 'a']`.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _is_operator(token: str) -> bool:
    """A punctuation-only token carrying a shell operator (`;`, `>&`, `|&`…).

    Quoted text never lands here as a mixed token, but a lone quoted `";"`
    is indistinguishable from an operator after shlex — refused, as it
    always was under `shlex.split`."""
    return (bool(token) and all(c in "();<>|&" for c in token)
            and any(c in _OPERATOR_CHARS for c in token))


def _validate_command(command: str) -> tuple[list[str], str | None]:
    """Tokenize via shlex and reject anything that would let a non-allowlisted
    binary execute. Returns (tokens, error). On error, tokens is empty.

    shlex.split respects quoting so `grep "a|b" file` is 3 tokens
    (`grep`, `a|b`, `file`) — not 5 — and the literal `|` inside the
    quoted regex doesn't trip the chain check.
    """
    try:
        tokens = _shell_tokens(command)
    except ValueError as e:
        return [], f"Failed to parse command (mismatched quotes?): {e}"
    if not tokens:
        return [], "Empty command"

    # An UNQUOTED newline is a command separator to sh, and shlex swallows it
    # as whitespace — `echo a\ntouch X` validated as one `echo` and ran both.
    # A newline inside quotes survives into its token (`python -c "a\nb"`),
    # so any newline the tokens don't account for was unquoted. \r is not a
    # separator to sh, but shlex drops it the same way; refuse both.
    for ch in ("\n", "\r"):
        if command.count(ch) > sum(t.count(ch) for t in tokens):
            return [], (
                "Unquoted newline in command — sh runs each line as a "
                "separate command. Issue separate bash calls instead."
            )

    bad_chain = [t for t in tokens
                 if t in _CHAIN_TOKENS or t in _REDIRECT_TOKENS or _is_operator(t)]
    if bad_chain:
        return [], (
            f"Shell chain/redirect operators not allowed: {bad_chain}. "
            "Issue separate bash calls for each command, or use "
            "read_file/write_file/edit_file instead of redirects."
        )

    # Command substitution — `cat $(echo /etc/passwd)` would let any binary
    # execute via the inner shell. Reject backticks and $(. Checked on the
    # WHITESPACE tokens (shlex.split): the operator-splitting tokenizer above
    # breaks `$(ls)` into `$`, `(`, `ls`, `)`, where no token contains "$(".
    for t in shlex.split(command):
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


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the process GROUP `proc` leads (it was started with
    start_new_session), so `sh -c`'s children die with the shell."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def _reap(proc: subprocess.Popen) -> None:
    """Collect a killed process. Bounded: a descendant that escaped the group
    (its own setsid) can hold the pipes open forever, and a tool must not
    hang on it after already reporting the timeout."""
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _run_cancellable(command: str, *, cwd, env, timeout: int,
                     cancel) -> tuple[str, str | None]:
    """Chat-only subprocess runner that honours a CancelToken mid-flight.

    `subprocess.run` blocks the worker thread until the command exits or the
    timeout fires — with the 600s dev-mode budget, one hung curl made esc a
    ten-minute wait (session 5bb630813c21, 2026-07-31). This variant polls a
    duck-typed token (any object with a truthy `.requested`) and kills the
    WHOLE process group (start_new_session) so `sh -c` children die with the
    shell. Only reachable via make_bash_fn(cancel=...); the benchmark path
    runs _bash's own Popen, which shares the group teardown (_kill_group)."""
    import time as _time

    proc = subprocess.Popen(
        command, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        errors="replace",   # one bad byte must not discard the whole output
        stdin=subprocess.DEVNULL,   # see _bash — same reason
        cwd=cwd, env=env, start_new_session=True,
    )

    deadline = _time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=_CANCEL_POLL_S)
            output = _clip_output((stdout or "") + (stderr or ""))
            return output, None if proc.returncode == 0 else f"exit code {proc.returncode}"
        except subprocess.TimeoutExpired:
            if getattr(cancel, "requested", False):
                _kill_group(proc)
                _reap(proc)
                return "", "cancelled by user"
            if _time.monotonic() >= deadline:
                _kill_group(proc)
                _reap(proc)
                return "", f"Command timed out after {timeout}s"


# --- no-op suffixes (opt-in, 2026-11; acceptance/bash_noop_ab_2026_11) ------
#
# The champion habitually runs tests as `npm test 2>&1`, `... | tail -40`,
# `... || true`. The validator refuses each (redirect / pipe / chain); each
# refusal is an idle call; three in a row end the run via
# post_write_idle_exit with NO final report (6/30 control runs in
# validation_context_ab_2026_11). Each suffix below is a no-op for THIS tool:
# output is already stdout+stderr and already clipped to head+tail. On a
# non-zero exit this tool returns (output, "exit code N"), but the agent loop
# sends `error or result`, so by default the MODEL sees only `exit code N` and
# never the failing output (since v1.0; acceptance/swebench/diag_misses_2026_11).
# LUXE_FAILED_OUTPUT=1 (opt-in, agents.sdd) sends the error line + the output.
#
# Mode is set by the maintain pipeline (`set_noop_mode`, read from
# LUXE_BASH_NOOP); None (the default, and always in chat) = byte-identical.
#   "hint":  keep the refusal; append why and the cleaned command to run.
#   "strip": run the cleaned command, with a one-line note before its output.
# Safety: only TRAILING suffixes from this fixed grammar are removed, the
# result goes through the full validator + allowlist again, so what runs is
# always a prefix of what the model asked for. Nothing new can execute.
_NOOP_SUFFIXES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\s*\|\s*(?:tail|head)(?:\s+-n\s*\d+|\s+-\d+|\s+--lines=\d+)?\s*$"),
     "a pipe to tail/head (long output is already clipped to its first 2 KB "
     "and last 6 KB)"),
    (re.compile(r"\s*\|\|\s*true\s*$"),
     "|| true (a non-zero exit is reported, and the output is still returned)"),
    (re.compile(r"\s+2>&1\s*$"),
     "2>&1 (stderr is already included in the output)"),
)
# LUXE_BENCH_PY_PATH (set by maintain via `set_bench_venv_path`; default ON
# there since 2026-11-03, exact "0" disables). The benchmark bash env is the
# process env minus secrets; on a host whose non-login PATH has no
# unversioned `python` (m5: brew ships `python3` only, and `python3` is not
# allowlisted) every `python ...` the model ran exited 127. Chat already
# prepends luxe's venv bin (`_chat_bash_env`); this is the same prepend for
# the benchmark path (acceptance/bash_noop_ab_2026_11). The module default
# stays False: only maintain turns it on, so other callers are unchanged.
_bench_venv_path: bool = False


def set_bench_venv_path(on: bool) -> None:
    global _bench_venv_path
    _bench_venv_path = bool(on)


def _bench_env() -> dict[str, str]:
    env = scrubbed_env()
    if _bench_venv_path:
        import sys
        from pathlib import Path
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


_NOOP_MODES = ("hint", "strip")
_noop_mode: str | None = None
_noop_stats: Counter = Counter()


def set_noop_mode(mode: str | None) -> None:
    """None (off) | "hint" | "strip". Anything else is treated as off."""
    global _noop_mode
    _noop_mode = mode if mode in _NOOP_MODES else None
    _noop_stats.clear()


def noop_stats() -> dict[str, int]:
    """Counts since the last `set_noop_mode` (keys: hint, strip, unfixable)."""
    return dict(_noop_stats)


# SWE-bench image exec (LUXE_SWEBENCH_EXEC=<image ref>, 2026-11-20; tools.sdd
# § "SWE-bench image exec"). BENCH PATH ONLY: set by maintain from the env,
# which only benchmarks/swebench/adapter.py (`run.py --image-exec`) exports.
# On: the commands that execute repo code (`_IMAGE_BINARIES`) run in a fresh
# container of the instance's SWE-bench eval image, with the model's working
# tree synced over /testbed (at base_commit in the image), so the target
# repo's own environment imports and collects (host: 1/46, image: 40/46,
# acceptance/swebench/diag_misses_2026_11/feasibility.md). A leading
# `cd <path> &&` is also handled. No host fallback: an unreachable docker or
# a missing image is an error the model sees. None (default) = byte-identical.
_IMAGE_BINARIES = frozenset({"python", "pytest", "pip"})
_IMAGE_TIMEOUT = 180
_IMAGE_TESTBED = "/testbed"
_image_exec: tuple[str, str] | None = None   # (image ref, base commit)
_image_calls: list[dict[str, Any]] = []
_image_seq = 0


def set_image_exec(image: str | None, base_commit: str | None) -> None:
    """Turn image exec on (both values non-empty) or off. Clears the call log."""
    global _image_exec
    _image_exec = (image, base_commit) if image and base_commit else None
    _image_calls.clear()


def image_exec_base() -> str | None:
    """base_commit while image exec is on, else None."""
    return _image_exec[1] if _image_exec else None


def image_exec_calls() -> list[dict[str, Any]]:
    """One record per bash call routed or rescued since `set_image_exec`."""
    return [dict(c) for c in _image_calls]


_CD_PREFIX = re.compile(r"^\s*cd\s+(\S+)\s*&&\s*(\S.*)$", re.DOTALL)


def _cd_rescue(command: str, repo_root) -> tuple[str, str, str] | None:
    """`cd <dir> && rest` -> (rest, cwd_rel, shown_dir), else None.

    The target is resolved against the repo: relative, under /testbed, or
    under the absolute repo root, and kept if it is an existing directory
    inside the repo. Anything else (the hallucinated /home/user/repo, a path
    outside) runs at the repo root. Only the ONE leading cd is consumed; the
    rest is validated as usual, so a second chain is still refused."""
    from pathlib import Path
    m = _CD_PREFIX.match(command)
    if not m:
        return None
    target, rest = m.group(1).strip("'\""), m.group(2).strip()
    root = Path(repo_root).resolve()
    rel = ""
    if target.startswith(_IMAGE_TESTBED):
        cand = root / target[len(_IMAGE_TESTBED):].lstrip("/")
    elif os.path.isabs(target):
        cand = Path(target)
    else:
        cand = root / target
    try:
        cand = cand.resolve()
        if cand.is_dir() and (cand == root or root in cand.parents):
            rel = str(cand.relative_to(root)) if cand != root else ""
    except OSError:
        rel = ""
    return rest, rel, (rel or ".")


def _changed_paths(repo_root, base: str) -> tuple[list[str], list[str]]:
    """(present, deleted) paths that differ from `base`, without touching the
    index: tracked changes via `git diff --name-status`, plus untracked files.
    The synthetic `<repo>.sdd` the adapter drops is excluded."""
    from luxe.secrets import scrubbed_env as _env
    from pathlib import Path
    root = Path(repo_root)
    present: list[str] = []
    deleted: list[str] = []
    r = subprocess.run(["git", "diff", "--name-status", "--no-renames", base],
                       cwd=root, capture_output=True, text=True, env=_env(),
                       timeout=30)
    for line in r.stdout.splitlines():
        status, _, path = line.partition("\t")
        (deleted if status.startswith("D") else present).append(path)
    r = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                       cwd=root, capture_output=True, text=True, env=_env(),
                       timeout=30)
    present += [p for p in r.stdout.splitlines() if p]
    sdd = f"{root.name}.sdd"
    present = sorted({p for p in present if p != sdd and (root / p).is_file()})
    return present, sorted(set(deleted))


def _run_in_image(command: str, repo_root, cwd_rel: str) -> tuple[str, str | None]:
    """Run one validated command in a fresh container of the instance image.

    stdin carries a tar of the model's changed files, extracted over
    /testbed; deletions are `rm -f`'d. No host env, no network. Same
    `(output, "exit code N")` contract as the host path."""
    import io
    import tarfile
    import time
    from pathlib import Path
    global _image_seq
    assert _image_exec is not None
    image, base = _image_exec
    root = Path(repo_root)
    try:
        present, deleted = _changed_paths(root, base)
    except (OSError, subprocess.SubprocessError) as e:
        return "", f"image exec unavailable: could not list changes ({e})"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in present:
            tar.add(str(root / p), arcname=p, recursive=False)
    rm = " ".join(shlex.quote(p) for p in deleted)
    workdir = _IMAGE_TESTBED + (f"/{cwd_rel}" if cwd_rel else "")
    inner = (f"cd {_IMAGE_TESTBED} && tar -xf - "
             + (f"&& rm -f -- {rm} " if rm else "")
             + f"&& cd {shlex.quote(workdir)} "
             + "&& source /opt/miniconda3/bin/activate testbed "
             + f"&& {command}")
    _image_seq += 1
    name = f"luxe-ix-{os.getpid()}-{_image_seq}"
    docker_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                  "HOME": os.environ.get("HOME", "/tmp")}
    host = os.environ.get("DOCKER_HOST") or (
        f"unix://{Path.home()}/.colima/default/docker.sock")
    docker_env["DOCKER_HOST"] = host
    argv = ["docker", "run", "--rm", "-i", "--name", name,
            "--platform", "linux/amd64", "--network", "none", "--memory", "6g",
            image, "bash", "-c", inner]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, input=buf.getvalue(), capture_output=True,
                              env=docker_env, timeout=_IMAGE_TIMEOUT)
    except FileNotFoundError:
        return "", "image exec unavailable: docker CLI not found"
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True,
                       env=docker_env, timeout=30)
        return "", f"Command timed out after {_IMAGE_TIMEOUT}s (in image)"
    wall = time.monotonic() - t0
    out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    # docker's own failures (daemon down, image missing) exit 125 with a
    # daemon message; 126/127 come from the container (command not found).
    if proc.returncode == 125 and ("docker daemon" in out.lower()
                                   or "unable to find image" in out.lower()
                                   or "no such image" in out.lower()):
        return "", f"image exec unavailable: {out.strip()[:300]}"
    _image_calls.append({"binary": shlex.split(command)[0] if command else "",
                         "rc": proc.returncode, "wall_s": round(wall, 2),
                         "bytes_out": len(out), "files_synced": len(present),
                         "cwd": cwd_rel or "."})
    output = (f"[ran in the instance image at {workdir}]\n"
              + _clip_output(out))
    return output, None if proc.returncode == 0 else f"exit code {proc.returncode}"


def strip_noop_suffixes(command: str) -> tuple[str, list[str]]:
    """`(cleaned, reasons)`. Peels known no-op suffixes off the END only,
    repeatedly (`npm test 2>&1 | tail -40` -> `npm test`). `reasons` is empty
    when nothing matched."""
    reasons: list[str] = []
    changed = True
    while changed:
        changed = False
        for rx, why in _NOOP_SUFFIXES:
            m = rx.search(command)
            if m and m.start() > 0:
                command = command[: m.start()]
                reasons.append(why)
                changed = True
    return command.rstrip(), reasons


def _noop_rescue(command: str, err: str,
                 allow: frozenset[str]) -> tuple[str | None, str]:
    """For a chain/redirect refusal: `(cleaned_command_or_None, err)`.

    hint mode returns `(None, err + hint)`; strip mode returns the cleaned
    command to run. Off, or nothing safely cleanable: `(None, err)` unchanged.
    """
    if _noop_mode is None or "operators not allowed" not in err:
        return None, err
    cleaned, reasons = strip_noop_suffixes(command)
    if not reasons or not cleaned:
        _noop_stats["unfixable"] += 1
        return None, err
    toks, cerr = _validate_command(cleaned)
    if cerr is not None or toks[0] not in allow:
        _noop_stats["unfixable"] += 1
        return None, err
    if _noop_mode == "strip":
        _noop_stats["strip"] += 1
        return cleaned, err
    _noop_stats["hint"] += 1
    return None, (f"{err} This tool needs none of: {'; '.join(reasons)}. "
                  f"Run instead: `{cleaned}`")


def _bash(args: dict[str, Any], *, unrestricted: bool = False,
          env: dict[str, str] | None = None,
          cancel=None,
          extra_allow: frozenset[str] = frozenset()) -> tuple[str, str | None]:
    """`env=None` (the default, and the ONLY value the benchmark path ever
    passes) runs with the process environment MINUS every secret-named
    variable (`luxe.secrets.scrubbed_env`, 2026-10-22): the command is the
    model's, and allowlisted `python`/`pip`/`make` can read the environment
    and reach the network. Nothing else about the environment changes. The
    chat front-end passes its own env, built from the same scrub (see
    make_bash_fn). `cancel=None` (benchmark default) runs one blocking
    Popen.communicate; a token switches to the cancellable polling runner.
    `extra_allow` (empty on the benchmark default, so the rejection message
    is byte-identical) widens the allowlist for chat sessions only."""
    repo_root = get_repo_root()
    if repo_root is None:
        return "", "Repo root not set"
    if env is None:
        env = _bench_env()

    command = args["command"]
    note = ""
    cwd_rel = ""
    # SWE-bench image exec only (None everywhere else, so this is skipped and
    # the path below is byte-identical): consume ONE leading `cd <dir> &&`.
    if _image_exec is not None and not unrestricted:
        rescued = _cd_rescue(command, repo_root)
        if rescued is not None:
            command, cwd_rel, shown = rescued
            note = f"[luxe ran `{command}` from {shown}: leading cd handled]\n"
            _image_calls.append({"binary": "cd_rescue", "cwd": shown})
    if unrestricted:
        # Opt-in `luxe chat` dev mode (chat.sdd): no allowlist, no chain/redirect
        # rejection. cwd is the repo root but this is NOT a sandbox.
        if not command.strip():
            return "", "Empty command"
        timeout = _UNRESTRICTED_TIMEOUT
    else:
        tokens, err = _validate_command(command)
        if err:
            cleaned, err = _noop_rescue(command, err, _ALLOWLIST | extra_allow)
            if cleaned is None:
                return "", err
            note += (f"[luxe ran `{cleaned}`: the dropped suffix is a no-op "
                     f"for this tool]\n")
            command = cleaned
            tokens, _ = _validate_command(command)
        binary = tokens[0]
        if binary not in _ALLOWLIST and binary not in extra_allow:
            return "", (f"Command '{binary}' not in allowlist. "
                        f"Allowed: {sorted(_ALLOWLIST | extra_allow)}")
        timeout = _TIMEOUT
        if _image_exec is not None and binary in _IMAGE_BINARIES:
            out, rerr = _run_in_image(command, repo_root, cwd_rel)
            return (note + out if note else out), rerr

    # cwd_rel is "" unless the image-exec cd rescue fired: repo root as before.
    run_cwd = os.path.join(repo_root, cwd_rel) if cwd_rel else repo_root
    if cancel is not None:
        out, rerr = _run_cancellable(command, cwd=run_cwd, env=env,
                                     timeout=timeout, cancel=cancel)
        return (note + out if note else out), rerr

    # Popen + its own session rather than subprocess.run (2026-09-26): on
    # timeout, run() kills only the direct child, so anything the command
    # started (a dev server, a watcher, `sleep` under a test) outlived the
    # timeout, orphaned and still running in the repo. start_new_session puts
    # the whole tree in one process group and the timeout kills the GROUP,
    # the same teardown _run_cancellable already used. Output, exit-code and
    # timeout strings are unchanged.
    proc = subprocess.Popen(
        command, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
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
        cwd=run_cwd,
        env=env,   # never None here: the scrubbed env (bench) or chat's own
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        _reap(proc)
        return "", f"Command timed out after {timeout}s"
    output = _clip_output(stdout + stderr)
    if note:
        output = note + output
    return output, None if proc.returncode == 0 else f"exit code {proc.returncode}"


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
    exists for. Host PATH still wins for everything not in the venv.

    Built from `scrubbed_env()` (2026-10-22): every secret-named variable is
    dropped, exactly as on the benchmark path."""
    import os
    import sys
    from pathlib import Path

    venv_bin = str(Path(sys.executable).parent)
    env = scrubbed_env()
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
