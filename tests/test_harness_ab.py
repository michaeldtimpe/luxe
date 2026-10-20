"""Pure pieces of scripts/harness_ab.py + a live WireShim round-trip
(no agent harness, no model)."""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("harness_ab", ROOT / "scripts" / "harness_ab.py")
ha = importlib.util.module_from_spec(_spec)
sys.modules["harness_ab"] = ha
_spec.loader.exec_module(ha)


def test_patch_body_forces_temperature_only_when_absent():
    body, rec = ha.patch_body(json.dumps({"model": "m", "input": "hi"}).encode())
    assert json.loads(body)["temperature"] == 0.0
    assert rec["forced_temperature"] is True and rec["temperature_sent"] is None

    body, rec = ha.patch_body(json.dumps({"model": "m", "input": "hi"}).encode())
    assert json.loads(body)["max_output_tokens"] == ha.OUTPUT_LIMIT  # Responses spelling
    assert rec["forced_max_tokens"] is True
    body, _ = ha.patch_body(json.dumps({"model": "m", "messages": [], "temperature": 0}).encode())
    assert json.loads(body)["max_tokens"] == ha.OUTPUT_LIMIT  # chat spelling

    raw = json.dumps({"model": "m", "temperature": 0.7, "max_tokens": 5}).encode()
    body, rec = ha.patch_body(raw)
    assert body == raw  # a sent temperature is never overridden, bytes untouched
    assert rec["forced_temperature"] is False and rec["temperature_sent"] == 0.7
    assert rec["max_tokens"] == 5


def test_patch_body_passes_non_json_through():
    for raw in (b"not json", b"[1, 2]"):
        body, rec = ha.patch_body(raw)
        assert body == raw and rec == {"json": False}


def test_opencode_config_denies_web():
    c = ha.make_opencode_config("M", "http://127.0.0.1:9/v1")
    assert c["permission"] == {"webfetch": "deny", "websearch": "deny"}
    assert c["provider"]["luxeab"]["models"]["M"]["options"]["temperature"] == 0
    assert c["provider"]["luxeab"]["models"]["M"]["limit"]["context"] == ha.CONTEXT_LIMIT


def test_qwen_settings_shape():
    s = ha.make_qwen_settings("M")
    gen = s["model"]["generationConfig"]
    assert gen["samplingParams"]["temperature"] == 0.0
    assert gen["contextWindowSize"] == ha.CONTEXT_LIMIT
    assert {"web_fetch", "web_search"} <= set(s["tools"]["exclude"])
    assert s["telemetry"]["enabled"] is False
    assert s["general"]["disableAutoUpdate"] is True


def test_codex_config_shape():
    t = ha.make_codex_config("M", "http://127.0.0.1:9/v1")
    assert 'wire_api = "responses"' in t
    assert 'web_search = "disabled"' in t
    assert "check_for_update_on_startup = false" in t
    assert f'env_key = "{ha.KEY_ENV}"' in t  # key by env name, never a literal


def test_iso_env_strips_luxe_venv(tmp_path):
    venv_bin = str(ha.ROOT / ".venv" / "bin")
    base = {"PATH": f"{venv_bin}:/opt/homebrew/bin:/usr/bin", "VIRTUAL_ENV": str(ha.ROOT / ".venv"),
            "UV_PROJECT_ENVIRONMENT": "x", "PYTHONPATH": "y"}
    env = ha._iso_env(base, tmp_path, "k")
    assert env["PATH"] == "/opt/homebrew/bin:/usr/bin"
    for k in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH"):
        assert k not in env


def test_offline_cmd_wraps_in_sandbox(tmp_path):
    prof = tmp_path / "offline.sb"
    assert ha.offline_cmd(["qwen", "p"], prof) == ["/usr/bin/sandbox-exec", "-f", str(prof), "qwen", "p"]
    assert "(deny network-outbound)" in ha.OFFLINE_PROFILE
    assert '(remote ip "localhost:*")' in ha.OFFLINE_PROFILE


def test_offline_profile_denies_the_cve_cache(tmp_path):
    real = tmp_path / "real_cache"
    real.mkdir()
    link = tmp_path / "cve_cache"
    link.symlink_to(real)
    prof = ha.offline_profile(link)
    assert prof.startswith(ha.OFFLINE_PROFILE)
    resolved = os.path.realpath(real)
    assert f'(deny file-read-data (subpath "{resolved}"))' in prof
    assert f'(deny file-write* (subpath "{resolved}"))' in prof


def test_iso_env_drops_inherited_provider_vars(tmp_path):
    base = {"PATH": "/usr/bin", "HOME": "/Users/real", "OPENAI_API_KEY": "real",
            "CODEX_HOME": "/Users/real/.codex", "ANTHROPIC_BASE_URL": "x"}
    env = ha._iso_env(base, tmp_path, "k")
    assert env["HOME"] == str(tmp_path / "home")
    for k in ("OPENAI_API_KEY", "CODEX_HOME", "ANTHROPIC_BASE_URL"):
        assert k not in env
    assert env[ha.KEY_ENV] == "k"


def test_parse_qwen_events():
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
        json.dumps({"type": "assistant", "session_id": "s1", "message": {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "a", "name": "read_file"}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "is_error": True}]}}),
        json.dumps({"type": "assistant", "message": {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "b", "name": "edit"}]}}),
        "garbage",
        json.dumps({"type": "result", "subtype": "success", "num_turns": 3,
                    "usage": {"input_tokens": 100, "output_tokens": 7,
                              "cache_read_input_tokens": 50}}),
    ]
    d = ha.parse_qwen_events(lines)
    assert d["session_id"] == "s1"
    assert d["tool_calls"] == {"read_file": 1, "edit": 1}
    assert d["tool_errors"] == {"read_file": 1}
    assert d["steps"] == 2 and d["turns"] == 3 and d["result"] == "success"
    assert d["tokens"]["input"] == 100 and d["tokens"]["cache_read"] == 50
    assert d["malformed_lines"] == 1


def test_parse_codex_events():
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "item.completed", "item": {"type": "error", "message": "warn"}}),
        json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "exit_code": 0, "status": "completed"}}),
        json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "exit_code": 1, "status": "failed"}}),
        json.dumps({"type": "item.completed", "item": {"type": "file_change", "status": "completed"}}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3,
            "reasoning_output_tokens": 1}}),
    ]
    d = ha.parse_codex_events(lines)
    assert d["session_id"] == "t1"
    assert d["tool_calls"] == {"command_execution": 2, "file_change": 1}
    assert d["tool_errors"] == {"command_execution": 1}
    assert d["warnings"] == ["warn"] and d["turn_failed"] is False
    assert d["tokens"] == {"input": 10, "output": 3, "reasoning": 1,
                           "cache_read": 4, "cache_write": 0}


class _Upstream(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Upstream.seen.append((self.path, json.loads(body)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for i in range(3):  # close-delimited stream, like an SSE body
            self.wfile.write(f"data: {i}\n\n".encode())
            self.wfile.flush()


def test_wire_shim_round_trip(tmp_path):
    up = http.server.HTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    log = tmp_path / "wire.jsonl"
    try:
        with ha.WireShim(f"http://127.0.0.1:{up.server_address[1]}/v1", log) as shim:
            assert shim.base_url.endswith("/v1")
            req = urllib.request.Request(
                shim.base_url + "/responses", method="POST",
                data=json.dumps({"model": "M", "input": "hi", "stream": True}).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer k"})
            with urllib.request.urlopen(req, timeout=10) as r:
                assert r.read() == b"data: 0\n\ndata: 1\n\ndata: 2\n\n"
    finally:
        up.shutdown()
    path, body = _Upstream.seen[-1]
    assert path == "/v1/responses" and body["temperature"] == 0.0
    w = ha.wire_summary(log)
    assert w["requests"] == 1 and w["forced_temperature"] == 1 and w["non_200"] == 0


def test_claude_env_points_every_alias_at_the_model_under_test():
    env = ha.make_claude_env("M", "http://127.0.0.1:5/v1", "k")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:5"  # Claude Code appends /v1
    for k in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
              "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
              "CLAUDE_CODE_SUBAGENT_MODEL"):
        assert env[k] == "M", k
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == str(ha.OUTPUT_LIMIT)
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == str(ha.CONTEXT_LIMIT)
    assert env["DISABLE_TELEMETRY"] == "1" and env["DISABLE_AUTOUPDATER"] == "1"
    assert {"WebFetch", "WebSearch"} <= set(ha.CLAUDE_DISALLOWED_TOOLS)


def test_iso_env_drops_inherited_claude_session_vars(tmp_path):
    base = {"PATH": "/usr/bin", "CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": "/Users/real/.claude",
            "CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_AUTH_TOKEN": "t"}
    env = ha._iso_env(base, tmp_path, "k")
    for k in ("CLAUDECODE", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
        assert k not in env


def test_codex_catalog_points_codex_entry_at_the_local_model(tmp_path):
    cat = ha.make_codex_catalog("M")
    (m,) = cat["models"]
    assert m["slug"] == "M" and m["apply_patch_tool_type"] == "freeform"
    assert m["prefer_websockets"] is False and m["use_responses_lite"] is False
    assert m["context_window"] == m["max_context_window"] == ha.CONTEXT_LIMIT
    assert m["input_modalities"] == ["text"] and m["upgrade"] is None
    assert m["model_messages"]["instructions_template"]  # Codex's own prompt kept
    t = ha.make_codex_config("M", "http://127.0.0.1:9/v1", catalog=str(tmp_path / "m.json"))
    assert f'model_catalog_json = "{tmp_path / "m.json"}"' in t
    assert "model_catalog_json" not in ha.make_codex_config("M", "http://127.0.0.1:9/v1")
    env = ha.CodexCatalogAdapter("codex-cat", "codex").setup(
        "M", "http://127.0.0.1:9/v1", tmp_path, {"PATH": "/usr/bin"}, "k")
    assert json.loads((tmp_path / "codex_home" / "models.json").read_text())["models"][0]["slug"] == "M"
    assert env["CODEX_HOME"] == str(tmp_path / "codex_home")


_CUSTOM_REQ = {
    "model": "M", "stream": True,
    "tools": [{"type": "function", "name": "exec_command", "parameters": {}},
              {"type": "custom", "name": "apply_patch",
               "description": "Edit files. " + ha.FREEFORM_NOTE,
               "format": {"type": "grammar", "syntax": "lark", "definition": "start: x"}}],
    "input": [{"type": "message", "role": "user", "content": "hi"},
              {"type": "custom_tool_call", "id": "ctc_1", "call_id": "c1",
               "name": "apply_patch", "input": "*** Begin Patch\n*** End Patch\n"},
              {"type": "custom_tool_call_output", "call_id": "c1", "output": "Done"}],
}


def test_patch_body_translates_custom_tools_only_when_asked():
    raw = json.dumps(_CUSTOM_REQ).encode()
    _, rec = ha.patch_body(raw)
    assert "custom_translated" not in rec and "custom:apply_patch" in rec["tools"]
    body, rec = ha.patch_body(raw, translate_custom=True)
    d = json.loads(body)
    assert rec["custom_translated"] == ["apply_patch"]
    fn = d["tools"][1]
    assert fn["type"] == "function" and fn["parameters"]["required"] == ["input"]
    assert ha.FREEFORM_NOTE not in fn["description"] and "start: x" in fn["description"]
    assert d["tools"][0] == _CUSTOM_REQ["tools"][0]
    call, out = d["input"][1], d["input"][2]
    assert call["type"] == "function_call" and call["call_id"] == "c1"
    assert json.loads(call["arguments"]) == {"input": "*** Begin Patch\n*** End Patch\n"}
    assert out == {"type": "function_call_output", "call_id": "c1", "output": "Done"}


def _sse(ev):
    return f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode()


def test_sse_translator_maps_function_call_events_back_to_custom():
    args = json.dumps({"input": "PATCH"})
    fc = {"type": "function_call", "id": "fc_1", "call_id": "c9", "name": "apply_patch"}
    other = {"type": "function_call", "id": "fc_2", "call_id": "c8", "name": "exec_command",
             "arguments": "{}", "status": "completed"}
    stream = b"".join([
        _sse({"type": "response.output_item.added", "item": {**fc, "arguments": "", "status": "in_progress"}}),
        _sse({"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": args}),
        _sse({"type": "response.function_call_arguments.done", "item_id": "fc_1", "arguments": args}),
        _sse({"type": "response.output_item.done", "item": {**fc, "arguments": args, "status": "completed"}}),
        _sse({"type": "response.output_item.done", "item": other}),
        _sse({"type": "response.completed", "response": {"output": [
            {**fc, "arguments": args, "status": "completed"}, other]}}),
    ])
    tr = ha.SSECustomTranslator(["apply_patch"])
    out = b"".join(tr.feed(stream[i:i + 7]) for i in range(0, len(stream), 7)) + tr.flush()
    evs = [json.loads(l[5:]) for l in out.split(b"\n") if l.startswith(b"data:")]
    types = [e["type"] for e in evs]
    assert "response.function_call_arguments.delta" not in types
    assert types.count("response.custom_tool_call_input.done") == 1
    done = [e for e in evs if e["type"] == "response.output_item.done"]
    assert done[0]["item"] == {"type": "custom_tool_call", "id": "fc_1", "call_id": "c9",
                               "name": "apply_patch", "input": "PATCH", "status": "completed"}
    assert done[1]["item"] == other  # untranslated tools untouched
    assert evs[-1]["response"]["output"][0]["type"] == "custom_tool_call"
    assert tr.calls == 1
    assert b"event: response.custom_tool_call_input.done\n" in out


def test_custom_input_falls_back_to_raw_arguments():
    assert ha._custom_input("*** Begin Patch") == "*** Begin Patch"
    assert ha._custom_input(json.dumps({"input": "P"})) == "P"


def test_luxe_mcp_attach_claude_and_qwen(tmp_path):
    srv = ha.luxe_mcp_server(Path("/r"), "manage", tmp_path / "l.jsonl", "/usr/bin")
    assert srv["args"][-4:] == ["--task-type", "manage", "--log", str(tmp_path / "l.jsonl")]
    assert srv["env"]["PATH"].startswith(str(ha.ROOT / ".venv" / "bin"))

    claude = ha.ClaudeAdapter("claude", "claude")
    assert "--mcp-config" not in claude.cmd("m", Path("/r"), "p")  # off by default
    claude.attach_mcp(srv, tmp_path)
    cmd = claude.cmd("m", Path("/r"), "p")
    cfg = json.loads(cmd[cmd.index("--mcp-config") + 1])
    assert cfg["mcpServers"]["luxe"] == srv and "--strict-mcp-config" in cmd

    qwen = ha.QwenAdapter("qwen", "qwen")
    qwen.setup("m", "http://x/v1", tmp_path, {"PATH": "/usr/bin"}, "")
    qwen.attach_mcp(srv, tmp_path)
    s = json.loads((tmp_path / "home/.qwen/settings.json").read_text())
    assert s["mcpServers"]["luxe"]["command"] == srv["command"]
    assert s["mcpServers"]["luxe"]["trust"] is True
    assert s["mcpServers"]["luxe"]["alwaysLoadTools"] is True
    assert "eager" not in s["tools"]  # a global allowlist: would defer built-ins
    assert s["model"]["name"] == "m" and s["tools"]["exclude"] == ha.QWEN_EXCLUDE_TOOLS


def test_luxe_mcp_usage_counts_calls_and_errors(tmp_path):
    log = tmp_path / "l.jsonl"
    assert ha.luxe_mcp_usage(log)["started"] is False
    log.write_text("\n".join(json.dumps(r) for r in [
        {"event": "start", "tools": ["bm25_search", "lint"]},
        {"tool": "lint", "error": None}, {"tool": "lint", "error": "boom"},
        {"tool": "bm25_search", "error": None},
        {"tool": "deps_audit", "error": None, "status": "error"}]) + "\nnot json\n")
    u = ha.luxe_mcp_usage(log)
    assert u == {"started": True, "offered": ["bm25_search", "lint"],
                 "calls": {"lint": 2, "bm25_search": 1, "deps_audit": 1},
                 "errors": {"lint": 1, "deps_audit": 1}}


def test_luxe_mcp_rejects_other_harnesses():
    import pytest
    with pytest.raises(SystemExit):
        ha.main(["--harness", "opencode", "--model", "m", "--output", "/tmp/x",
                 "--all", "--luxe-mcp"])


def test_luxe_mcp_tool_list_matches_server():
    import importlib.util as iu
    spec = iu.spec_from_file_location("lmcp", ROOT / "scripts" / "harness_ab_luxe_mcp.py")
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert tuple(mod.LAYER2) == ha.LUXE_MCP_TOOLS

