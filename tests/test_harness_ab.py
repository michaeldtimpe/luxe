"""Pure pieces of scripts/harness_ab.py + a live WireShim round-trip
(no agent harness, no model)."""

from __future__ import annotations

import http.server
import importlib.util
import json
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
