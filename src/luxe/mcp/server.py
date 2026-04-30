"""MCP server — exposes luxe agents to Claude Desktop / Cursor / other hosts.

Default posture (per plan §4): read-only.
- `luxe_review(repo_path, goal)`     — review pipeline, no edits
- `luxe_summarize(repo_path, goal?)` — summarize pipeline, no edits
- `luxe_explain(repo_path, query)`   — single-mode explain, no edits
These tools internally strip shell/write/edit/git-mutation tools from the
worker allowlist regardless of the YAML config, so even a malicious prompt
can't escape into the host environment via luxe.

Mutation mode (`luxe serve --unsafe`):
- Adds `luxe_maintain(repo_path, goal, confirm_token)`.
- Both `LUXE_MCP_UNSAFE=1` env AND a `confirm_token` matching env-set
  `LUXE_MCP_TOKEN` are required at call time. Token mismatch logs the
  attempt and returns an error. The user picks the token; treat as a
  bearer secret.

Rate limiting and audit logging live in the helpers below; the FastMCP
tools call into them so cross-cutting policy is uniform.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


# --- audit log -------------------------------------------------------------

def audit_log_path() -> Path:
    return Path.home() / ".luxe" / "mcp_audit.jsonl"


def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Strip secrets / tokens from logged args."""
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        lk = k.lower()
        if "token" in lk or "secret" in lk or "key" in lk or "password" in lk:
            out[k] = "[redacted]"
        elif isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + "...(truncated)"
        else:
            out[k] = v
    return out


def append_audit(tool: str, args: dict[str, Any], outcome: str,
                 detail: str = "") -> None:
    p = audit_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.time(),
        "pid": os.getpid(),
        "tool": tool,
        "args": _redact_args(args),
        "outcome": outcome,   # accepted | rejected | error
        "detail": detail,
    }
    with p.open("a") as f:
        f.write(json.dumps(record) + "\n")


# --- rate limiting ---------------------------------------------------------

_PERIOD_RE_TOKENS = {"sec": 1.0, "min": 60.0, "hour": 3600.0}


def _parse_rate(spec: str) -> tuple[int, float]:
    """Parse "1/min" → (1, 60.0). Returns (count, period_seconds)."""
    spec = spec.strip().lower()
    if "/" not in spec:
        raise ValueError(f"invalid rate spec: {spec}")
    n_s, period_s = spec.split("/", 1)
    try:
        n = int(n_s.strip())
    except ValueError as e:
        raise ValueError(f"invalid rate count in {spec}") from e
    period_s = period_s.strip()
    for prefix, secs in _PERIOD_RE_TOKENS.items():
        if period_s.startswith(prefix):
            return n, secs
    raise ValueError(f"invalid rate period in {spec}: {period_s}")


@dataclass
class RateLimiter:
    rates: dict[str, tuple[int, float]] = field(default_factory=dict)
    _events: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))

    def configure(self, raw: dict[str, str]) -> None:
        for tool, spec in (raw or {}).items():
            try:
                self.rates[tool] = _parse_rate(spec)
            except ValueError:
                continue

    def check(self, tool: str) -> tuple[bool, str]:
        rate = self.rates.get(tool)
        if rate is None:
            return True, ""
        n, period = rate
        now = time.monotonic()
        events = self._events[tool]
        # Drop expired
        while events and events[0] < now - period:
            events.popleft()
        if len(events) >= n:
            wait = period - (now - events[0])
            return False, f"rate limit ({n}/{period:.0f}s); retry in {wait:.0f}s"
        events.append(now)
        return True, ""


# --- token / unsafe gate ---------------------------------------------------

def _unsafe_enabled() -> bool:
    return os.environ.get("LUXE_MCP_UNSAFE", "").strip() == "1"


def _check_confirm_token(token: str) -> tuple[bool, str]:
    expected = os.environ.get("LUXE_MCP_TOKEN", "")
    if not expected:
        return False, "LUXE_MCP_TOKEN env var not set on the server"
    if not token:
        return False, "missing confirm_token"
    if token != expected:
        return False, "confirm_token does not match LUXE_MCP_TOKEN"
    return True, ""


# --- pipeline runners (server-side) ----------------------------------------

@dataclass
class ServerPolicy:
    read_only_tools: list[str] = field(default_factory=lambda: [
        "luxe_review", "luxe_summarize", "luxe_explain",
    ])
    mutation_tools: list[str] = field(default_factory=lambda: ["luxe_maintain"])
    rate_limits_raw: dict[str, str] = field(default_factory=dict)


def load_server_policy(path: str | Path | None = None) -> ServerPolicy:
    p = Path(path) if path else (Path(__file__).parent.parent.parent.parent
                                 / "configs" / "mcp.yaml")
    if not p.is_file():
        return ServerPolicy()
    raw = yaml.safe_load(p.read_text()) or {}
    server_raw = raw.get("server", {}) or {}
    return ServerPolicy(
        read_only_tools=list(server_raw.get("read_only_tools",
                                            ["luxe_review", "luxe_summarize", "luxe_explain"])),
        mutation_tools=list(server_raw.get("mutation_tools", ["luxe_maintain"])),
        rate_limits_raw=dict(server_raw.get("rate_limits", {}) or {}),
    )


# --- read-only role config patcher -----------------------------------------

_MUTATION_TOOL_NAMES = {
    "write_file", "edit_file", "bash",
    # git_diff itself is read-only; we do not strip it
}


def make_read_only_role(role_cfg) -> Any:
    """Return a copy of role_cfg with mutation tools stripped from `tools`."""
    # role_cfg is a Pydantic RoleConfig; .copy(update=...) preserves type.
    new_tools = [t for t in (role_cfg.tools or []) if t not in _MUTATION_TOOL_NAMES]
    try:
        return role_cfg.model_copy(update={"tools": new_tools})
    except AttributeError:
        # Older pydantic
        return role_cfg.copy(update={"tools": new_tools})


# --- FastMCP server builder ------------------------------------------------

def build_server(*, unsafe: bool = False, policy: ServerPolicy | None = None,
                 maintain_runner: Callable[[dict[str, Any]], str] | None = None,
                 readonly_runner: Callable[[str, dict[str, Any]], str] | None = None,
                 ) -> Any:
    """Construct a FastMCP server. Tool implementations are dependency-
    injected so tests can stub the pipeline without spinning up real models.

    `readonly_runner(tool_name, args) -> str` handles luxe_review/summarize/explain.
    `maintain_runner(args) -> str` handles luxe_maintain when unsafe=True.
    """
    from mcp.server.fastmcp import FastMCP

    policy = policy or load_server_policy()
    rate = RateLimiter()
    rate.configure(policy.rate_limits_raw)

    mcp_server = FastMCP("luxe")

    def _check_rate(tool: str) -> tuple[bool, str]:
        return rate.check(tool)

    @mcp_server.tool(name="luxe_review")
    def luxe_review(repo_path: str, goal: str) -> str:
        """Run a luxe code review on a repository (read-only; no edits)."""
        ok, msg = _check_rate("luxe_review")
        if not ok:
            append_audit("luxe_review", {"repo_path": repo_path, "goal": goal},
                         "rejected", msg)
            return f"rate limited: {msg}"
        if readonly_runner is None:
            append_audit("luxe_review", {"repo_path": repo_path, "goal": goal},
                         "error", "no runner installed")
            return "luxe server not configured (no runner)"
        try:
            result = readonly_runner("luxe_review",
                                     {"repo_path": repo_path, "goal": goal})
        except Exception as e:
            append_audit("luxe_review", {"repo_path": repo_path, "goal": goal},
                         "error", str(e))
            return f"error: {e}"
        append_audit("luxe_review", {"repo_path": repo_path, "goal": goal},
                     "accepted")
        return result

    @mcp_server.tool(name="luxe_summarize")
    def luxe_summarize(repo_path: str, goal: str = "") -> str:
        """Summarize a repository (read-only; no edits)."""
        ok, msg = _check_rate("luxe_summarize")
        if not ok:
            append_audit("luxe_summarize", {"repo_path": repo_path, "goal": goal},
                         "rejected", msg)
            return f"rate limited: {msg}"
        if readonly_runner is None:
            return "luxe server not configured (no runner)"
        try:
            result = readonly_runner("luxe_summarize",
                                     {"repo_path": repo_path, "goal": goal})
        except Exception as e:
            append_audit("luxe_summarize", {"repo_path": repo_path, "goal": goal},
                         "error", str(e))
            return f"error: {e}"
        append_audit("luxe_summarize", {"repo_path": repo_path, "goal": goal},
                     "accepted")
        return result

    @mcp_server.tool(name="luxe_explain")
    def luxe_explain(repo_path: str, query: str) -> str:
        """Answer a question about a repository (read-only; no edits)."""
        ok, msg = _check_rate("luxe_explain")
        if not ok:
            append_audit("luxe_explain", {"repo_path": repo_path, "query": query},
                         "rejected", msg)
            return f"rate limited: {msg}"
        if readonly_runner is None:
            return "luxe server not configured (no runner)"
        try:
            result = readonly_runner("luxe_explain",
                                     {"repo_path": repo_path, "query": query})
        except Exception as e:
            append_audit("luxe_explain", {"repo_path": repo_path, "query": query},
                         "error", str(e))
            return f"error: {e}"
        append_audit("luxe_explain", {"repo_path": repo_path, "query": query},
                     "accepted")
        return result

    if unsafe:
        if not _unsafe_enabled():
            # We log this but still register — tool calls will be rejected
            # at invocation time with a clear message.
            pass

        @mcp_server.tool(name="luxe_maintain")
        def luxe_maintain(repo_path: str, goal: str, confirm_token: str) -> str:
            """Run a full luxe maintain pipeline (writes files, opens PRs).

            Requires LUXE_MCP_UNSAFE=1 environment AND confirm_token matching
            LUXE_MCP_TOKEN.
            """
            args = {"repo_path": repo_path, "goal": goal,
                    "confirm_token": confirm_token}
            if not _unsafe_enabled():
                append_audit("luxe_maintain", args, "rejected",
                             "LUXE_MCP_UNSAFE not set on server")
                return ("luxe_maintain is not enabled on this server "
                        "(LUXE_MCP_UNSAFE != 1)")
            ok, msg = _check_confirm_token(confirm_token)
            if not ok:
                append_audit("luxe_maintain", args, "rejected", msg)
                return f"rejected: {msg}"
            ok, rmsg = _check_rate("luxe_maintain")
            if not ok:
                append_audit("luxe_maintain", args, "rejected", rmsg)
                return f"rate limited: {rmsg}"
            if maintain_runner is None:
                append_audit("luxe_maintain", args, "error", "no runner installed")
                return "luxe server not configured (no maintain runner)"
            try:
                result = maintain_runner(args)
            except Exception as e:
                append_audit("luxe_maintain", args, "error", str(e))
                return f"error: {e}"
            append_audit("luxe_maintain", args, "accepted")
            return result

    return mcp_server


def server_tool_names(unsafe: bool = False, policy: ServerPolicy | None = None) -> list[str]:
    """Names of tools the server WILL expose for a given posture."""
    policy = policy or load_server_policy()
    names = list(policy.read_only_tools)
    if unsafe:
        names.extend(policy.mutation_tools)
    return names
