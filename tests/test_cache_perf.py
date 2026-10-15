"""Prefix-cache + prefill/decode accounting, and the two cache experiments
(2026-10-15).

- `backend._server_perf` reads the server's own cache/prefill/decode numbers
  (oMLX `usage`, llama-server `timings`) and leaves them None when absent.
- `chat/perf.py` turns a turn's sums into the status-bar/footer rates.
- `LUXE_REASONING_ROUNDTRIP=1` hands the model's reasoning back on its
  assistant messages; off, the message dict is exactly the legacy one.
- `LUXE_COMPACT_CACHE_FRIENDLY=1` makes phase 1 idempotent and batched; off,
  the legacy (re-truncating) behaviour is unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from luxe.agents.loop import run_agent
from luxe.backend import (
    ChatResponse,
    GenerationTiming,
    ToolCallResponse,
    _server_perf,
)
from luxe.chat import perf as perf_mod
from luxe.config import RoleConfig
from luxe.context import TieredCompact
from luxe.tools.base import ToolDef


# --- backend parsing ---------------------------------------------------------

class TestServerPerf:
    def test_omlx_usage_shape(self):
        usage = {"prompt_tokens": 5000, "completion_tokens": 100,
                 "prompt_tokens_details": {"cached_tokens": 4181},
                 "prompt_eval_duration": 1.17, "generation_duration": 2.2}
        assert _server_perf(usage, None) == {
            "cached_tokens": 4181, "prefill_s": 1.17, "decode_s": 2.2}

    def test_llama_server_timings_shape(self):
        timings = {"cache_n": 3000, "prompt_ms": 500.0, "predicted_ms": 2000.0}
        assert _server_perf({}, timings) == {
            "cached_tokens": 3000, "prefill_s": 0.5, "decode_s": 2.0}

    def test_absent_or_malformed_fields_stay_absent(self):
        """OpenRouter reports none of these; a bad value is dropped, not raised."""
        assert _server_perf({"prompt_tokens": 9}, None) == {}
        assert _server_perf({"prompt_tokens_details": {"cached_tokens": "x"},
                             "prompt_eval_duration": None,
                             "generation_duration": -1}, "junk") == {}

    def test_rates_exclude_cached_tokens(self):
        """Cached prefix tokens were restored, not computed (oMLX's own rule)."""
        t = GenerationTiming(prompt_tokens=5000, completion_tokens=100,
                             cached_tokens=4000, prefill_s=2.0, decode_s=4.0)
        assert t.prefill_tok_per_s == pytest.approx(500.0)
        assert t.gen_tok_per_s == pytest.approx(25.0)
        assert GenerationTiming(prompt_tokens=10).prefill_tok_per_s is None


# --- chat/perf.py ------------------------------------------------------------

def _result(**kw):
    base = dict(cached_tokens=0, perf_prompt_tokens=0, prefill_s=0.0,
                prefill_tokens=0, decode_s=0.0, decode_tokens=0,
                first_step_prompt_tokens=0, first_step_cached_tokens=None,
                steps=1)
    base.update(kw)
    return SimpleNamespace(**base)


class TestTurnPerf:
    def test_a_turn_with_server_numbers(self):
        r = _result(cached_tokens=8000, perf_prompt_tokens=10000,
                    prefill_s=4.0, prefill_tokens=2000, decode_s=2.0,
                    decode_tokens=90, first_step_prompt_tokens=4500,
                    first_step_cached_tokens=0, steps=2)
        p = perf_mod.turn_perf(r)
        assert (p.prefill_tps, p.decode_tps) == (500.0, 45.0)
        assert p.cache == pytest.approx(0.8)
        assert p.first_cache == 0.0
        assert perf_mod.footer_bits(r) == [
            "prefill 500 t/s", "decode 45 t/s", "cache 80% (turn start 0%)"]

    def test_no_server_numbers_means_no_bits(self):
        """A rate luxe did not measure must not render as a number."""
        r = _result()
        assert perf_mod.turn_perf(r) == perf_mod.TurnPerf()
        assert perf_mod.footer_bits(r) == []

    def test_rate_format(self):
        assert perf_mod.rate(3412) == "3.4k"
        assert perf_mod.rate(45.4) == "45"


def test_status_bar_shows_cache_share_and_rates():
    from luxe.chat import status as status_mod

    state = status_mod.StatusState(has_turn=True, prompt_tokens=92_000,
                                   cache_hit=0.82, prefill_tps=3412,
                                   decode_tps=45)
    session = SimpleNamespace(num_ctx_override=0, write_enabled=False,
                              unrestricted_bash=False, project_kind="dir")
    slots = SimpleNamespace(model_for=lambda _s: "m", cfg=None)
    text = " · ".join(status_mod._seg_text(s) for s in status_mod.fields(
        session, slots, "", state))
    assert "cache 82% 92k" in text
    assert "pp 3.4k tg 45 t/s" in text


# --- compaction --------------------------------------------------------------

def _msgs(n: int) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [{"role": "system", "content": "s"},
                                  {"role": "user", "content": "u"}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": str(i), "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": str(i), "content": "x" * 5000})
    return msgs


class TestCacheFriendlyCompaction:
    def test_legacy_mode_still_re_truncates(self):
        """Pinned on purpose: the default path is byte-identical, bug included."""
        tc = TieredCompact()
        once, _ = tc._phase1(_msgs(6), 12)
        twice, _ = tc._phase1(once, 12)
        assert once[3]["content"] != twice[3]["content"]
        assert twice[3]["content"].endswith("[Truncated — 33 chars removed]")

    def test_cache_friendly_truncates_once(self):
        tc = TieredCompact(cache_friendly=True)
        once, _ = tc._phase1(_msgs(6), 12)
        twice, _ = tc._phase1(once, 12)
        assert once == twice
        assert once[3]["content"].endswith("[Truncated — 4800 chars removed]")

    def test_cache_friendly_waits_for_a_batch(self):
        """Phase-1 pressure with fewer than CACHE_FRIENDLY_BATCH results to
        truncate leaves the prefix alone."""
        msgs = _msgs(5)        # keep_recent=3 → 2 eligible results
        limit = 11_000         # ~6.4k est tokens → between t1 and t2
        legacy = TieredCompact().compact(msgs, limit)
        friendly = TieredCompact(cache_friendly=True).compact(msgs, limit)
        assert legacy.phase_reached == 1
        assert friendly.phase_reached == 0 and friendly.messages == msgs

    def test_cache_friendly_fires_once_the_batch_is_full(self):
        msgs = _msgs(7)        # 4 eligible
        r = TieredCompact(cache_friendly=True).compact(msgs, 16_000)
        assert r.phase_reached == 1 and r.tool_results_dropped == 4

    def test_phase_two_pressure_overrides_the_batch(self):
        msgs = _msgs(5)
        r = TieredCompact(cache_friendly=True).compact(msgs, 7_000)
        assert r.phase_reached >= 1


# --- reasoning round-trip ----------------------------------------------------

class _Backend:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, **kwargs) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        return self._responses.pop(0)


def _run_roundtrip(monkeypatch, flag: str | None):
    if flag is None:
        monkeypatch.delenv("LUXE_REASONING_ROUNDTRIP", raising=False)
    else:
        monkeypatch.setenv("LUXE_REASONING_ROUNDTRIP", flag)
    backend = _Backend([
        ChatResponse(text="", tool_calls=[ToolCallResponse(
            id="r", name="read_file", arguments={"path": "a.py"})],
            finish_reason="tool_calls", reasoning_text="I should read a.py",
            reasoning_chars=18, timing=GenerationTiming(
                prompt_tokens=100, cached_tokens=0, prefill_s=0.5)),
        ChatResponse(text="done", finish_reason="stop",
                     timing=GenerationTiming(prompt_tokens=150,
                                             cached_tokens=95, prefill_s=0.1)),
    ])
    result = run_agent(
        backend=backend,
        role_cfg=RoleConfig(model_key="t", num_ctx=10_000, max_steps=4,
                            max_tokens_per_turn=256, temperature=0.0),
        system_prompt="sys", task_prompt="task",
        tool_defs=[ToolDef(name="read_file", description="read", parameters={
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"]})],
        tool_fns={"read_file": lambda args: ("contents", None)},
    )
    assistant = [m for m in backend.calls[1] if m["role"] == "assistant"]
    return assistant[0], result


def test_reasoning_is_not_sent_back_by_default(monkeypatch):
    msg, _ = _run_roundtrip(monkeypatch, None)
    assert "reasoning_content" not in msg


def test_reasoning_roundtrip_sends_it_back(monkeypatch):
    msg, _ = _run_roundtrip(monkeypatch, "1")
    assert msg["reasoning_content"] == "I should read a.py"


def test_loop_sums_the_servers_cache_accounting(monkeypatch):
    _, result = _run_roundtrip(monkeypatch, None)
    assert result.cached_tokens == 95 and result.perf_prompt_tokens == 250
    assert result.first_step_cached_tokens == 0
    assert result.first_step_prompt_tokens == 100
    assert result.prefill_s == pytest.approx(0.6)
