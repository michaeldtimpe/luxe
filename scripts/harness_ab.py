#!/usr/bin/env python
"""Run the maintain_suite fixtures through a third-party agent harness
(opencode, Qwen Code or Codex) against the same local model and grade them
with luxe's own grader — the harness arm of a bake-off.

    uv run python scripts/harness_ab.py --harness qwen \\
        --model Qwen3.6-35B-A3B-6bit --output acceptance/harness_ab_<date>/qwen/champ/rep1 --all
    uv run python scripts/harness_ab.py --harness codex --model Qwen3.8-27B-8bit \\
        --output /tmp/hx --id isomer-document-quickstart --timeout 3600

Generalises scripts/opencode_harness.py (whose pure pieces it reuses; that
script stays as-is so the 2026-09 Splash A/B artefacts remain reproducible).
Held equal across harnesses, and with `benchmarks.maintain_suite.run`:
  * repo prep — `_resolve_repo`, plus harness dot-dirs in .git/info/exclude;
  * task text — `fixture.goal` + one neutral line (`build_prompt`); each
    harness brings its own system prompt and tools — that is the comparison;
  * grading — `grade.grade_fixture` on base_sha..HEAD (the driver commits
    whatever the agent left in the working tree);
  * context 65536 / output 8192 where the harness has a knob;
  * temperature 0 — ENFORCED ON THE WIRE. Every harness talks to a local
    shim (`WireShim`) in front of the server. It forwards bytes unchanged
    except that a JSON request with no `temperature` gets `temperature: 0`
    (oMLX's default is 1.0 and Codex sends none), and it logs each request's
    sampling fields to wire.jsonl so parity is read, not assumed;
  * no web tools — opencode `webfetch`/`websearch` denied, Qwen Code
    `web_fetch`/`web_search` excluded, Codex `web_search` disabled. luxe's
    bench surface has none (the 2026-09 A/B gave opencode webfetch; it
    confabulated CVE sourcing with it).

Not equal, by construction: no PR (pr_opened always 0) and no citations
(graded 0/0 = +1), so the printed max is 4/5 — compare on outcome_points
and, above all, on hand-verify. Sandboxing: all three run unsandboxed with
auto-approve (opencode --auto, qwen yolo, codex bypass), i.e. each agent's
shell has the same reach.

Isolation: per-run HOME + XDG_* (+ CODEX_HOME) under <output>/_env/, so no
harness reads the user's global config, auth, skills or ~/.claude, and none
writes the user's session stores. Update checks, telemetry and model-catalog
fetches are off in every generated config.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import importlib.util
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "opencode_harness", ROOT / "scripts" / "opencode_harness.py")
oh = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("opencode_harness", oh)
_spec.loader.exec_module(oh)

from benchmarks.maintain_suite.grade import Fixture, grade_fixture  # noqa: E402

HARNESSES = ("opencode", "qwen", "codex", "claude", "codex-cat")
# oMLX's max_context_window. The outside harnesses are sized for frontier
# windows (Qwen Code holds back a fixed ~23K reserve, so at 65536 it hard-stops
# at 42536 tokens), so each runs at the server's ceiling, not luxe's window.
CONTEXT_LIMIT = 131072
OUTPUT_LIMIT = oh.OUTPUT_LIMIT
KEY_ENV = oh.API_KEY_ENV_IN_CHILD
DEFAULT_BIN_DIR = Path.home() / "luxe-bench-tmp" / "harnesses" / "node_modules" / ".bin"
DEFAULT_WORK_DIR = Path.home() / ".luxe" / "harness-ab-workspace"
EXCLUDE_ENTRIES = [".opencode/", ".qwen/", ".codex/", ".claude/"]
# Qwen Code tools that reach the web, or only make sense in a long-lived
# interactive session (a one-shot run would wait on them).
QWEN_EXCLUDE_TOOLS = ["web_fetch", "web_search", "cron_create", "cron_list",
                      "cron_delete", "loop_wakeup", "monitor"]


# --- wire shim -------------------------------------------------------------

FREEFORM_NOTE = "This is a FREEFORM tool, so do not wrap the patch in JSON."


def custom_tools_to_functions(d: dict) -> list[str]:
    """oMLX serves only function tools and silently drops `custom` (freeform,
    grammar-constrained) ones, which is the only shape Codex 0.163 offers
    apply_patch in. Rewrite each custom tool as a function taking one
    `input` string (its grammar appended to the description), and the
    request's history items to match. Returns the translated names, which
    the response side maps back. Mutates `d`."""
    names: list[str] = []
    tools = []
    for t in d.get("tools") or []:
        if isinstance(t, dict) and t.get("type") == "custom" and t.get("name"):
            desc = (t.get("description") or "").replace(
                FREEFORM_NOTE, "Pass the whole patch as the `input` string.")
            fmt = t.get("format") or {}
            if fmt.get("definition"):
                desc += f"\n\nThe input must match this {fmt.get('syntax', '')} grammar:\n{fmt['definition']}"
            tools.append({"type": "function", "name": t["name"], "description": desc,
                          "parameters": {"type": "object",
                                         "properties": {"input": {"type": "string"}},
                                         "required": ["input"]}})
            names.append(t["name"])
        else:
            tools.append(t)
    if not names:
        return names
    d["tools"] = tools
    items = d.get("input")
    if isinstance(items, list):
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            if it.get("type") == "custom_tool_call":
                items[i] = {"type": "function_call", "call_id": it.get("call_id"),
                            "name": it.get("name"),
                            "arguments": json.dumps({"input": it.get("input", "")})}
            elif it.get("type") == "custom_tool_call_output":
                items[i] = {"type": "function_call_output", "call_id": it.get("call_id"),
                            "output": it.get("output", "")}
    return names


def _custom_input(arguments: str) -> str:
    try:
        a = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return arguments or ""  # model sent the raw patch: pass it on as-is
    return a.get("input", "") if isinstance(a, dict) else str(a)


def function_item_to_custom(item: Any, names: set[str]) -> Any:
    if (isinstance(item, dict) and item.get("type") == "function_call"
            and item.get("name") in names):
        return {"type": "custom_tool_call", "id": item.get("id"),
                "call_id": item.get("call_id"), "name": item["name"],
                "input": _custom_input(item.get("arguments") or ""),
                "status": item.get("status", "completed")}
    return item


class SSECustomTranslator:
    """Map oMLX's function-call Responses events for translated tools back to
    Codex's custom-tool events. oMLX emits each call as four whole events
    (item added, args delta with the full arguments, args done, item done),
    so per-event rewriting is enough; nothing is reassembled."""

    def __init__(self, names: Iterable[str]):
        self.names = set(names)
        self.ids: set[str] = set()
        self.calls = 0
        self._buf = b""

    def feed(self, chunk: bytes) -> bytes:
        self._buf += chunk
        out = []
        while b"\n\n" in self._buf:
            block, self._buf = self._buf.split(b"\n\n", 1)
            out.append(self._event(block) + b"\n\n")
        return b"".join(out)

    def flush(self) -> bytes:
        rest, self._buf = self._buf, b""
        return self._event(rest) if rest.strip() else rest

    def _event(self, block: bytes) -> bytes:
        lines = block.split(b"\n")
        data_i = next((i for i, l in enumerate(lines) if l.startswith(b"data:")), None)
        if data_i is None:
            return block
        try:
            ev = json.loads(lines[data_i][5:])
        except (json.JSONDecodeError, UnicodeDecodeError):
            return block
        if not isinstance(ev, dict):
            return block
        new = self.rewrite(ev)
        if new is None:
            return b""  # dropped event: an empty block is a no-op for SSE parsers
        if new is ev:
            return block
        return (f"event: {new['type']}\n".encode() if any(l.startswith(b"event:") for l in lines)
                else b"") + b"data: " + json.dumps(new).encode()

    def rewrite(self, ev: dict) -> dict | None:
        typ = ev.get("type")
        if typ in ("response.output_item.added", "response.output_item.done"):
            item = ev.get("item")
            new = function_item_to_custom(item, self.names)
            if new is not item:
                self.ids.add(item.get("id"))
                if typ.endswith(".done"):
                    self.calls += 1
                return {**ev, "item": new}
        elif typ == "response.function_call_arguments.delta" and ev.get("item_id") in self.ids:
            return None
        elif typ == "response.function_call_arguments.done" and ev.get("item_id") in self.ids:
            return {"type": "response.custom_tool_call_input.done", "item_id": ev["item_id"],
                    "output_index": ev.get("output_index"),
                    "input": _custom_input(ev.get("arguments") or ""),
                    "sequence_number": ev.get("sequence_number")}
        elif typ in ("response.completed", "response.failed", "response.incomplete"):
            r = ev.get("response")
            if isinstance(r, dict) and isinstance(r.get("output"), list):
                out = [function_item_to_custom(i, self.names) for i in r["output"]]
                if any(a is not b for a, b in zip(out, r["output"])):
                    return {**ev, "response": {**r, "output": out}}
        return ev


def patch_body(body: bytes, translate_custom: bool = False) -> tuple[bytes, dict]:
    """Set temperature 0 and an OUTPUT_LIMIT cap on a JSON request that omits
    them (Codex sends neither; oMLX would sample at 1.0 with a 32K cap);
    report what the request asked for. Non-JSON / non-object bodies pass
    through untouched."""
    try:
        d = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body, {"json": False}
    if not isinstance(d, dict):
        return body, {"json": False}
    rec = {
        "json": True,
        "model": d.get("model"),
        "temperature_sent": d.get("temperature"),
        "forced_temperature": "temperature" not in d,
        "max_tokens": d.get("max_tokens", d.get("max_output_tokens")),
        "forced_max_tokens": False,
        "stream": d.get("stream"),
        "n_tools": len(d.get("tools") or []),
        # type:name per tool, so an arm's surface (e.g. Codex's freeform
        # apply_patch) is visible on the wire, not inferred from a count.
        "tools": [f"{t.get('type')}:{t.get('name') or (t.get('function') or {}).get('name')}"
                  for t in (d.get("tools") or []) if isinstance(t, dict)],
    }
    changed = False
    if translate_custom:
        names = custom_tools_to_functions(d)
        if names:
            rec["custom_translated"] = names
            changed = True
    if "temperature" not in d:
        d["temperature"] = 0.0
        changed = True
    cap_key = "max_output_tokens" if "input" in d else "max_tokens"
    if d.get("max_tokens") is None and d.get("max_output_tokens") is None:
        d[cap_key] = OUTPUT_LIMIT
        rec["forced_max_tokens"] = True
        changed = True
    return (json.dumps(d).encode() if changed else body), rec


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "_ShimServer"

    def log_message(self, *a: Any) -> None:  # quiet
        pass

    def _proxy(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        rec: dict = {}
        if self.command == "POST" and body:
            body, rec = patch_body(body, translate_custom=self.server.translate_custom)
        tr = SSECustomTranslator(rec["custom_translated"]) if rec.get("custom_translated") else None
        up = self.server.upstream
        conn_cls = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(up.hostname, up.port, timeout=7200)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length", "connection",
                                        "accept-encoding")}
        headers["Content-Length"] = str(len(body))
        t0 = time.time()
        try:
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            self.send_error(502, f"shim upstream: {e}")
            self.server.log({"path": self.path, "error": str(e), **rec})
            return
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in ("transfer-encoding", "content-length", "connection"):
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        nbytes = 0
        try:
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                nbytes += len(chunk)
                if tr is not None:
                    chunk = tr.feed(chunk)
                    if not chunk:
                        continue
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            tail = tr.flush() if tr is not None else b""
            if tail:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(tail), tail))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()
        self.server.log({"ts": round(t0, 3), "method": self.command,
                         "path": self.path, "status": resp.status,
                         "wall_s": round(time.time() - t0, 2),
                         "resp_bytes": nbytes, **rec,
                         **({"custom_calls": tr.calls} if tr is not None else {})})

    do_GET = do_POST = do_DELETE = do_PUT = _proxy


class _ShimServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, upstream: str, log_path: Path, translate_custom: bool = False):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.translate_custom = translate_custom
        self.upstream = urllib.parse.urlsplit(upstream)
        self.log_path = log_path
        self._lock = threading.Lock()

    def log(self, rec: dict) -> None:
        with self._lock, self.log_path.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")


class WireShim:
    """Local forwarder in front of the model server; see module docstring."""

    def __init__(self, upstream_base_url: str, log_path: Path, translate_custom: bool = False):
        u = urllib.parse.urlsplit(upstream_base_url)
        self._srv = _ShimServer(f"{u.scheme}://{u.netloc}", log_path, translate_custom)
        self._path = u.path.rstrip("/")  # e.g. /v1
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}{self._path}"

    def set_log(self, path: Path) -> None:
        self._srv.log_path = path

    def __enter__(self) -> "WireShim":
        self._t.start()
        return self

    def __exit__(self, *a: Any) -> None:
        self._srv.shutdown()
        self._srv.server_close()


def wire_summary(path: Path) -> dict:
    out = {"requests": 0, "forced_temperature": 0, "forced_max_tokens": 0,
           "temperatures_sent": {},
           "non_200": 0, "errors": 0}
    temps: Counter[str] = Counter()
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("json"):
            continue
        out["requests"] += 1
        out["forced_temperature"] += int(bool(r.get("forced_temperature")))
        out["forced_max_tokens"] += int(bool(r.get("forced_max_tokens")))
        temps[repr(r.get("temperature_sent"))] += 1
        out["non_200"] += int(r.get("status") not in (200, None))
        out["errors"] += int("error" in r)
    out["temperatures_sent"] = dict(temps)
    return out


# --- adapters --------------------------------------------------------------

def _iso_env(base_env: dict[str, str], env_root: Path, api_key: str) -> dict[str, str]:
    env = dict(base_env)
    for name, var in (("home", "HOME"), ("data", "XDG_DATA_HOME"),
                      ("config", "XDG_CONFIG_HOME"), ("cache", "XDG_CACHE_HOME"),
                      ("state", "XDG_STATE_HOME")):
        p = env_root / name
        p.mkdir(parents=True, exist_ok=True)
        env[var] = str(p)
    env.update({
        KEY_ENV: api_key or "sk-no-key",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "commit.gpgsign",
        "GIT_CONFIG_VALUE_0": "false",
    })
    # The driver runs under `uv run`: strip luxe's venv so an agent's
    # `pip install` can't land in it (the 2026-10-18 pilot's deps-audit did).
    venv = env.pop("VIRTUAL_ENV", None)
    drop = {str(ROOT / ".venv" / "bin")} | ({str(Path(venv) / "bin")} if venv else set())
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                  if p and p not in drop)
    for k in [k for k in env if k.startswith("UV_")] + ["PYTHONPATH", "PYTHONHOME"]:
        env.pop(k, None)
    # never let an inherited provider/config steer a child
    for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "CODEX_HOME",
              "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT",
              "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
              "ANTHROPIC_MODEL", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
              "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)
    return env


@dataclass
class Adapter:
    translate_custom = False  # class attribute (unannotated), not a dataclass field
    name: str
    binary: str
    files: dict[str, str] = field(default_factory=dict)  # rel path -> content

    def setup(self, model: str, base_url: str, env_root: Path, base_env: dict,
              api_key: str) -> dict[str, str]:
        raise NotImplementedError

    def cmd(self, model: str, repo: Path, prompt: str) -> list[str]:
        raise NotImplementedError

    def parse(self, lines: Iterable[str]) -> dict[str, Any]:
        raise NotImplementedError

    def version(self) -> str:
        try:
            r = subprocess.run([self.binary, "--version"], capture_output=True,
                               text=True, timeout=60, stdin=subprocess.DEVNULL)
            return (r.stdout or r.stderr).strip().splitlines()[-1]
        except (OSError, subprocess.TimeoutExpired, IndexError):
            return ""

    def _write(self, env_root: Path, rel: str, content: str) -> Path:
        p = env_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self.files[rel] = content
        return p


class OpencodeAdapter(Adapter):
    def setup(self, model, base_url, env_root, base_env, api_key):
        cfg = make_opencode_config(model, base_url)
        path = self._write(env_root, "opencode.config.json", json.dumps(cfg, indent=2))
        env = _iso_env(base_env, env_root, api_key)
        env.update({"OPENCODE_CONFIG": str(path),
                    "OPENCODE_DISABLE_MODELS_FETCH": "1",
                    "OPENCODE_DISABLE_AUTOUPDATE": "1",
                    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1"})
        return env

    def cmd(self, model, repo, prompt):
        return [self.binary, *oh.opencode_cmd(model, repo, prompt)[1:]]

    def parse(self, lines):
        return oh.parse_events(lines)


def make_opencode_config(model: str, base_url: str) -> dict:
    cfg = oh.make_opencode_config(model, base_url)
    cfg["provider"][oh.PROVIDER_ID]["models"][model]["limit"]["context"] = CONTEXT_LIMIT
    cfg["permission"] = {"webfetch": "deny", "websearch": "deny"}
    return cfg


def make_qwen_settings(model: str) -> dict:
    return {
        "security": {"auth": {"selectedType": "openai"}},
        "model": {"name": model,
                  "generationConfig": {
                      "contextWindowSize": CONTEXT_LIMIT,
                      "samplingParams": {"temperature": 0.0,
                                         "max_tokens": OUTPUT_LIMIT}}},
        "privacy": {"usageStatisticsEnabled": False},
        "telemetry": {"enabled": False},
        "general": {"disableAutoUpdate": True, "disableUpdateNag": True},
        "tools": {"exclude": list(QWEN_EXCLUDE_TOOLS)},
    }


class QwenAdapter(Adapter):
    def setup(self, model, base_url, env_root, base_env, api_key):
        env = _iso_env(base_env, env_root, api_key)
        self._write(env_root, "home/.qwen/settings.json",
                    json.dumps(make_qwen_settings(model), indent=2))
        env.update({"OPENAI_API_KEY": api_key or "sk-no-key",
                    "OPENAI_BASE_URL": base_url,
                    "OPENAI_MODEL": model,
                    "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1"})
        return env

    def cmd(self, model, repo, prompt):
        return [self.binary, prompt, "--model", model, "--approval-mode", "yolo",
                "--output-format", "stream-json", "--advisor", "off",
                "--exclude-tools", *QWEN_EXCLUDE_TOOLS]

    def parse(self, lines):
        return parse_qwen_events(lines)


def parse_qwen_events(lines: Iterable[str]) -> dict[str, Any]:
    """Digest `qwen --output-format stream-json`: assistant messages carry
    tool_use blocks, user messages carry tool_result (is_error), the final
    `result` event carries totals."""
    tools: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    names: dict[str, str] = {}
    tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
    session_id, steps, bad, result_sub, turns = "", 0, 0, "", 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(ev, dict):
            bad += 1
            continue
        session_id = session_id or str(ev.get("session_id") or "")
        typ = ev.get("type")
        msg = ev.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if typ == "assistant" and isinstance(content, list):
            if msg.get("stop_reason"):
                steps += 1
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    n = str(b.get("name") or "?")
                    tools[n] += 1
                    names[str(b.get("id"))] = n
        elif typ == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    tool_errors[names.get(str(b.get("tool_use_id")), "?")] += 1
        elif typ == "result":
            result_sub = str(ev.get("subtype") or "")
            turns = int(ev.get("num_turns") or 0)
            u = ev.get("usage") or {}
            tokens["input"] = int(u.get("input_tokens") or 0)
            tokens["output"] = int(u.get("output_tokens") or 0)
            tokens["cache_read"] = int(u.get("cache_read_input_tokens") or 0)
    return {"session_id": session_id, "tool_calls": dict(tools),
            "tool_calls_total": sum(tools.values()), "tool_errors": dict(tool_errors),
            "steps": steps, "turns": turns, "result": result_sub,
            "tokens": tokens, "malformed_lines": bad}


def make_codex_config(model: str, base_url: str, catalog: str | None = None) -> str:
    return "\n".join([
        f'model = "{model}"',
        *([f"model_catalog_json = {json.dumps(catalog)}"] if catalog else []),
        'model_provider = "luxeab"',
        'approval_policy = "never"',
        'check_for_update_on_startup = false',
        f"model_context_window = {CONTEXT_LIMIT}",
        'web_search = "disabled"',
        "",
        "[model_providers.luxeab]",
        'name = "luxe A/B (local)"',
        f'base_url = "{base_url}"',
        f'env_key = "{KEY_ENV}"',
        'wire_api = "responses"',
        "",
        "[analytics]",
        "enabled = false",
        "",
        "[feedback]",
        "enabled = false",
        "",
    ])


class CodexAdapter(Adapter):
    def setup(self, model, base_url, env_root, base_env, api_key):
        env = _iso_env(base_env, env_root, api_key)
        self._write(env_root, "codex_home/config.toml", make_codex_config(model, base_url))
        env["CODEX_HOME"] = str(env_root / "codex_home")
        return env

    def cmd(self, model, repo, prompt):
        return [self.binary, "exec", "--json", "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", "-C", str(repo), prompt]

    def parse(self, lines):
        return parse_codex_events(lines)


# Codex on a slug it does not know runs on fallback metadata, with no
# apply_patch tool (it then pipes "*** Begin Patch" through the shell). The
# codex-cat arm hands it a catalog entry instead: Codex's own gpt-5.5 entry
# (extracted from the pinned 0.163.0 binary's embedded catalog, prompt and all)
# re-pointed at the local model. Overrides strip what oMLX cannot serve:
# websockets, Responses-lite, images,
# reasoning summaries, verbosity, service tiers.
CODEX_MODEL_TEMPLATE = ROOT / "scripts" / "harness_ab_codex_model.json"


def make_codex_catalog(model: str, template: Path = CODEX_MODEL_TEMPLATE) -> dict[str, Any]:
    m = json.loads(template.read_text())
    m.update({
        "slug": model, "display_name": model, "description": "local model (harness_ab)",
        "prefer_websockets": False, "use_responses_lite": False,
        "apply_patch_tool_type": "freeform", "input_modalities": ["text"],
        "supports_image_detail_original": False,
        "support_verbosity": False, "default_verbosity": None,
        "supports_reasoning_summaries": False,
        "supports_reasoning_summary_parameter": False,
        "context_window": CONTEXT_LIMIT, "max_context_window": CONTEXT_LIMIT,
        "upgrade": None, "service_tiers": [], "additional_speed_tiers": [],
        "default_service_tier": None, "visibility": "list",
    })
    return {"models": [m]}


class CodexCatalogAdapter(CodexAdapter):
    # Its apply_patch is a custom (freeform) tool, which oMLX drops: the shim
    # serves it as a function and maps the calls back (custom_tools_to_functions).
    translate_custom = True

    def setup(self, model, base_url, env_root, base_env, api_key):
        env = _iso_env(base_env, env_root, api_key)
        cat = self._write(env_root, "codex_home/models.json",
                          json.dumps(make_codex_catalog(model), indent=1))
        self._write(env_root, "codex_home/config.toml",
                    make_codex_config(model, base_url, catalog=str(cat)))
        env["CODEX_HOME"] = str(env_root / "codex_home")
        return env


def parse_codex_events(lines: Iterable[str]) -> dict[str, Any]:
    """Digest `codex exec --json`: completed items are tool actions
    (command_execution / file_change / mcp_tool_call / web_search) or
    messages; turn.completed carries usage; `error` items are warnings."""
    tools: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
    session_id, steps, bad, warnings, turn_failed = "", 0, 0, [], False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(ev, dict):
            bad += 1
            continue
        typ = ev.get("type")
        if typ == "thread.started":
            session_id = str(ev.get("thread_id") or "")
        elif typ == "item.completed":
            it = ev.get("item") or {}
            kind = str(it.get("type") or "?")
            if kind in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                tools[kind] += 1
                steps += 1
                if it.get("status") == "failed" or (it.get("exit_code") not in (None, 0)):
                    tool_errors[kind] += 1
            elif kind == "error":
                warnings.append(str(it.get("message") or "")[:300])
        elif typ == "turn.completed":
            u = ev.get("usage") or {}
            tokens["input"] += int(u.get("input_tokens") or 0)
            tokens["output"] += int(u.get("output_tokens") or 0)
            tokens["reasoning"] += int(u.get("reasoning_output_tokens") or 0)
            tokens["cache_read"] += int(u.get("cached_input_tokens") or 0)
        elif typ in ("turn.failed", "error"):
            turn_failed = True
            warnings.append(json.dumps(ev)[:300])
    return {"session_id": session_id, "tool_calls": dict(tools),
            "tool_calls_total": sum(tools.values()), "tool_errors": dict(tool_errors),
            "steps": steps, "turn_failed": turn_failed, "warnings": warnings[:20],
            "tokens": tokens, "malformed_lines": bad}


# Outage conditions: the agent process tree may reach loopback (the wire shim)
# and unix sockets, nothing else. luxe's bench has no web either; the pilot's
# Codex curled osv.dev from its shell, Qwen Code tried to.
OFFLINE_PROFILE = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
"""


def offline_profile(cve_cache: Path | None = None) -> str:
    """OFFLINE_PROFILE, plus a read/write deny on luxe's cve_lookup cache.

    The cache holds real advisory data from online runs; a Claude Code
    subagent read it during Glimmer rep1's offline deps-audit. Denied from
    rep2 champ on (user decision 2026-10-19), as the luxe control already does.
    sandbox-exec matches resolved paths, so the path is realpath'd."""
    if cve_cache is None:
        cve_cache = Path.home() / ".luxe" / "cve_cache"
    real = os.path.realpath(cve_cache)
    return (OFFLINE_PROFILE
            + f'(deny file-read-data (subpath {json.dumps(real)}))\n'
            + f'(deny file-write* (subpath {json.dumps(real)}))\n')


def offline_cmd(cmd: list[str], profile_path: Path) -> list[str]:
    return ["/usr/bin/sandbox-exec", "-f", str(profile_path), *cmd]


# Claude Code tools that reach the web or only work in a long-lived session
# (same rule as QWEN_EXCLUDE_TOOLS); the rest of its surface is kept.
CLAUDE_DISALLOWED_TOOLS = ["WebFetch", "WebSearch", "CronCreate", "CronDelete",
                           "CronList", "ScheduleWakeup"]


def make_claude_env(model: str, base_url: str, api_key: str) -> dict[str, str]:
    """Point Claude Code at the shim's Anthropic-format endpoint (oMLX serves
    /v1/messages; Claude Code appends /v1 itself). Every model alias resolves
    to the one model under test, so background/subagent calls use it too."""
    root = base_url[: -len("/v1")] if base_url.endswith("/v1") else base_url
    env = {"ANTHROPIC_BASE_URL": root,
           "ANTHROPIC_AUTH_TOKEN": api_key or "sk-no-key",
           "ANTHROPIC_MODEL": model,
           "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(OUTPUT_LIMIT),
           # it assumes a 200K window; compact against the server's real one
           "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(CONTEXT_LIMIT),
           "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(CONTEXT_LIMIT),
           "DISABLE_TELEMETRY": "1",
           "DISABLE_AUTOUPDATER": "1",
           "DISABLE_ERROR_REPORTING": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    for alias in ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                  "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                  "CLAUDE_CODE_SUBAGENT_MODEL"):
        env[alias] = model
    return env


class ClaudeAdapter(Adapter):
    def setup(self, model, base_url, env_root, base_env, api_key):
        env = _iso_env(base_env, env_root, api_key)
        cfg = env_root / "claude_config"
        cfg.mkdir(parents=True, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = str(cfg)
        claude_env = make_claude_env(model, base_url, api_key)
        env.update(claude_env)
        self.files["claude.env"] = "\n".join(
            f"{k}={'<key>' if k == 'ANTHROPIC_AUTH_TOKEN' else v}"
            for k, v in sorted(claude_env.items()))
        return env

    def cmd(self, model, repo, prompt):
        return [self.binary, "-p", prompt, "--model", model,
                "--output-format", "stream-json", "--verbose",
                "--dangerously-skip-permissions",
                "--disallowedTools", *CLAUDE_DISALLOWED_TOOLS]

    def parse(self, lines):
        # Qwen Code's stream-json is a copy of Claude Code's: same envelope.
        return parse_qwen_events(lines)


ADAPTERS = {"opencode": OpencodeAdapter, "qwen": QwenAdapter, "codex": CodexAdapter,
            "claude": ClaudeAdapter, "codex-cat": CodexCatalogAdapter}


def resolve_binary(harness: str, bin_dir: Path) -> str:
    exe = {"opencode": "opencode", "qwen": "qwen", "codex": "codex",
           "claude": "claude", "codex-cat": "codex"}[harness]
    p = bin_dir / exe
    if p.exists():
        return str(p)
    found = shutil.which(exe)
    if not found:
        raise SystemExit(f"{exe} not found in {bin_dir} or on PATH")
    return found


# --- run loop --------------------------------------------------------------

@dataclass
class RunCtx:
    harness: str
    adapter: Adapter
    model: str
    output: Path
    work_dir: Path
    timeout_s: float
    env: dict
    shim: WireShim
    profile_path: Path | None = None
    walls: list[float] = field(default_factory=list)


def run_one(fx: Fixture, ctx: RunCtx) -> dict:
    from benchmarks.maintain_suite.run import _resolve_repo

    fdir = ctx.output / fx.id
    fdir.mkdir(parents=True, exist_ok=True)
    repo, err = _resolve_repo(fx, ctx.work_dir)
    if repo is None:
        raise RuntimeError(f"repo prep failed: {err}")
    oh._exclude(repo, EXCLUDE_ENTRIES)
    base_sha = fx.base_sha or oh._git(["rev-parse", "HEAD"], repo).stdout.strip()
    prompt = oh.build_prompt(fx)
    cmd = ctx.adapter.cmd(ctx.model, repo, prompt)
    if ctx.profile_path is not None:
        cmd = offline_cmd(cmd, ctx.profile_path)
    wire = fdir / "wire.jsonl"
    wire.unlink(missing_ok=True)
    ctx.shim.set_log(wire)

    t0 = time.time()
    rc, timed_out = oh.run_opencode(cmd, ctx.env, repo, fdir / "events.jsonl",
                                    fdir / "stderr.log", ctx.timeout_s)
    wall = time.time() - t0

    with (fdir / "events.jsonl").open() as fh:
        digest = ctx.adapter.parse(fh)
    committed = oh.commit_changes(repo)
    head = oh._git(["rev-parse", "HEAD"], repo).stdout.strip()
    (fdir / "diff.patch").write_text(oh._git(["diff", base_sha, "HEAD"], repo).stdout)

    res = grade_fixture(fx, repo, pr_url="", pr_opened=False,
                        citations_unresolved=0, citations_total=0,
                        base_sha=base_sha).to_dict()
    res["outcome_points"] = oh.outcome_points(res)
    res["outcome_max"] = 3
    res["harness"] = ctx.harness
    (fdir / "result.json").write_text(json.dumps(res, indent=2))

    meta = {
        "fixture_id": fx.id, "task_type": fx.task_type, "harness": ctx.harness,
        "model": ctx.model, "prompt": prompt,
        "command": [c if c != prompt else "<prompt>" for c in cmd],
        "repo": str(repo), "base_sha": base_sha, "head_sha": head,
        "harness_committed": committed, "wall_s": round(wall, 2),
        "exit_code": rc, "timed_out": timed_out, "timeout_s": ctx.timeout_s,
        "wire": wire_summary(wire), **digest,
    }
    (fdir / "meta.json").write_text(json.dumps(meta, indent=2))
    return {"result": res, "meta": meta}


def write_summary(ctx: RunCtx, ids: list[str], state: dict, extra: dict) -> dict:
    rows = []
    tot = {"score": 0, "max_score": 0, "outcome_points": 0, "outcome_max": 0,
           "passed": 0, "timed_out": 0, "done": 0, "error": 0, "wall_s": 0.0,
           "wire_requests": 0, "wire_forced_temperature": 0}
    for fid in ids:
        st = (state.get(fid) or {}).get("status", "pending")
        row: dict[str, Any] = {"fixture_id": fid, "status": st}
        rp, mp = ctx.output / fid / "result.json", ctx.output / fid / "meta.json"
        if st == "done" and rp.is_file() and mp.is_file():
            r, m = json.loads(rp.read_text()), json.loads(mp.read_text())
            row.update(score=r["score"], outcome_points=r["outcome_points"],
                       diff_produced=r["diff_produced"],
                       gates=[g.get("name") or g.get("gate", "?")
                              for g in r.get("gates_triggered") or []],
                       wall_s=m["wall_s"], timed_out=m["timed_out"],
                       exit_code=m["exit_code"], tool_calls_total=m["tool_calls_total"],
                       tokens=m["tokens"], wire=m.get("wire"))
            tot["score"] += r["score"]
            tot["max_score"] += 5
            tot["outcome_points"] += r["outcome_points"]
            tot["outcome_max"] += 3
            tot["passed"] += int(r["outcome_points"] == 3)
            tot["timed_out"] += int(bool(m["timed_out"]))
            tot["done"] += 1
            tot["wall_s"] += m["wall_s"]
            w = m.get("wire") or {}
            tot["wire_requests"] += int(w.get("requests") or 0)
            tot["wire_forced_temperature"] += int(w.get("forced_temperature") or 0)
        elif st == "error":
            tot["error"] += 1
            row["error"] = (state.get(fid) or {}).get("last_error", "")
        rows.append(row)
    tot["wall_s"] = round(tot["wall_s"], 1)
    summary = {**extra, "totals": tot, "fixtures": rows,
               "scoring_note": ("pr_opened is always 0 (no PR); citations graded "
                                "on 0/0 (+1). Max 4/5 — compare on outcome_points "
                                "and hand-verify.")}
    (ctx.output / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--harness", required=True, choices=HARNESSES)
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", default=oh.DEFAULT_BASE_URL)
    ap.add_argument("--api-key-env", default="OMLX_API_KEY")
    ap.add_argument("--bin-dir", type=Path, default=DEFAULT_BIN_DIR)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    ap.add_argument("--id", action="append", default=[], dest="ids")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--timeout", type=float, default=oh.DEFAULT_TIMEOUT_S)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--label", default="")
    ap.add_argument("--allow-network", action="store_true",
                    help="skip the sandbox-exec network deny (default: offline)")
    args = ap.parse_args(argv)

    fixtures = {f.id: f for f in oh.load_fixtures()}
    if args.all == bool(args.ids):
        ap.error("pass exactly one of --all or --id")
    unknown = [i for i in args.ids if i not in fixtures]
    if unknown:
        ap.error(f"unknown fixture id(s): {', '.join(unknown)}")
    ids = list(fixtures) if args.all else args.ids

    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    work_dir = args.work_dir.expanduser().resolve()
    env_root = output / "_env"
    env_root.mkdir(parents=True, exist_ok=True)
    key = oh.resolve_key(args.api_key_env)
    adapter = ADAPTERS[args.harness](args.harness, resolve_binary(args.harness, args.bin_dir))
    version = adapter.version()

    with WireShim(args.base_url, output / "wire.preflight.jsonl",
                  translate_custom=adapter.translate_custom) as shim:
        env = adapter.setup(args.model, shim.base_url, env_root, os.environ.copy(), key)
        profile_path = None
        if not args.allow_network:
            profile_path = env_root / "offline.sb"
            profile_path.write_text(offline_profile())
        ctx = RunCtx(harness=args.harness, adapter=adapter, model=args.model,
                     output=output, work_dir=work_dir, timeout_s=args.timeout,
                     env=env, shim=shim, profile_path=profile_path)
        extra = {
            "harness": args.harness, "harness_version": version, "label": args.label,
            "model": args.model, "upstream_base_url": args.base_url,
            "api_key_env": args.api_key_env, "api_key_present": bool(key),
            "config_files": adapter.files,
            "limits": {"context": CONTEXT_LIMIT, "output": OUTPUT_LIMIT},
            "temperature": 0.0, "offline": not args.allow_network,
            "timeout_s": args.timeout, "work_dir": str(work_dir),
            "neutral_line": oh.NEUTRAL_LINE, "env_root": str(env_root),
        }
        state_path = output / "state.json"
        state = oh.load_state(state_path)
        todo = oh.pending_ids(ids, state)
        print(f"harness_ab[{args.harness} {version or '?'}]: {len(ids)} fixture(s), "
              f"{len(todo)} to run (resume skips {len(ids) - len(todo)}) | "
              f"model={args.model} key={'set' if key else 'unset (dummy)'} "
              f"timeout={args.timeout:.0f}s", flush=True)
        if args.dry_run:
            for fid in todo:
                print(f"  [dry-run] {fid}")
            for rel in adapter.files:
                print(f"  config written: {env_root / rel}")
            return 0

        for fid in ids:
            state.setdefault(fid, {"status": "pending", "attempts": 0})
        oh.save_state(state_path, state)

        run_score = run_max = run_out = run_out_max = 0
        for fid in ids:
            if fid not in todo and state[fid].get("status") == "done":
                rp = output / fid / "result.json"
                if rp.is_file():
                    r = json.loads(rp.read_text())
                    run_score += r["score"]; run_max += 5
                    run_out += r.get("outcome_points", 0); run_out_max += 3

        n = len(todo)
        for i, fid in enumerate(todo, 1):
            fx = fixtures[fid]
            missing = [e for e in fx.required_env if not os.environ.get(e)]
            if missing:
                state[fid].update(status="skipped", last_error=f"missing env: {missing}")
                oh.save_state(state_path, state)
                print(f"  [{i}/{n}] {fid} SKIPPED (missing env {missing})", flush=True)
                continue
            state[fid].update(status="running", attempts=state[fid].get("attempts", 0) + 1,
                              last_attempt_ts=time.time(), last_error="")
            oh.save_state(state_path, state)
            print(f"  [{i}/{n}] {fid} START ({fx.task_type})", flush=True)
            try:
                out = run_one(fx, ctx)
            except Exception as e:  # noqa: BLE001 — record and move on
                state[fid].update(status="error", last_error=f"{type(e).__name__}: {e}"[:500])
                oh.save_state(state_path, state)
                print(f"  [{i}/{n}] {fid} ERROR {type(e).__name__}: {e}", flush=True)
                continue
            r, m = out["result"], out["meta"]
            state[fid].update(status="done")
            oh.save_state(state_path, state)
            ctx.walls.append(m["wall_s"])
            run_score += r["score"]; run_max += 5
            run_out += r["outcome_points"]; run_out_max += 3
            print(oh.fmt_progress(i, n, fid, score=r["score"], outcome=r["outcome_points"],
                                  wall=m["wall_s"], run_score=run_score, run_max=run_max,
                                  run_outcome=run_out, run_outcome_max=run_out_max,
                                  walls=ctx.walls, left=n - i, timed_out=m["timed_out"])
                  + f" tools={m['tool_calls_total']} wire={m['wire']['requests']}"
                  f"(forced_t0={m['wire']['forced_temperature']})", flush=True)
            write_summary(ctx, ids, state, extra)

        s = write_summary(ctx, ids, state, extra)
    t = s["totals"]
    print(f"done: score={t['score']}/{t['max_score']} outcome={t['outcome_points']}/"
          f"{t['outcome_max']} passed(outcome)={t['passed']}/{t['done']} "
          f"errors={t['error']} timeouts={t['timed_out']} wall={t['wall_s']:.0f}s "
          f"-> {output / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
