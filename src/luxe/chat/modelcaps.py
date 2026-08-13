"""Can this model actually call tools?

Not every MLX model can. Gemma 3's chat template handles `system`/`user`/
`assistant` only, has no `tools` block, and raises on non-alternating turns —
and oMLX does not reject a request that carries `tools` for such a model, it
**silently drops them** (verified against gemma-3-1b on 2026-07-30: the reply
came back as prose with `prompt_tokens=16`, i.e. the tool definitions never
reached the prompt). An agentic turn on such a model therefore never calls a
tool and never explains why — the worst failure shape available.

So luxe reads the template and decides up front: a model whose template has no
tool support gets the tool surface WITHHELD and the user told, once, plainly.

Detection is local-only by design: it inspects the `chat_template` shipped with
the weights (via oMLX's reported `model_path`). For a remote endpoint the path
is on the other machine, so capability is UNKNOWN and we assume tools work —
the previous behaviour, and the champion is what runs there.

Same fail-open answer on a **llama-server** endpoint (neo, 2026-08-13): it has
no `/v1/models/status`, so `model_paths()` is empty and capability is UNKNOWN.
That is the right answer there and not merely a lucky one — llama.cpp is
started with `jinja = true` and applies the GGUF's own template, and neo's
`Qwen3-4B-Instruct-2507` demonstrably round-trips native `tool_calls`
(`luxe smoke --chat --code`, 2026-08-13). The gemma failure mode this module
exists for is an oMLX-silently-drops-`tools` behaviour; llama-server instead
errors on a template that cannot render them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from luxe.chat import origin as origin_mod

# Where a model directory keeps its chat template, in preference order.
_TEMPLATE_FILES = ("chat_template.jinja", "chat_template.json",
                   "tokenizer_config.json")

# Markers that mean the template renders tool definitions / tool results.
_TOOL_MARKERS = ("tools", "tool_call", "tool_calls", "function_call")

SUPPORTED, UNSUPPORTED, UNKNOWN = "supported", "unsupported", "unknown"


@dataclass(frozen=True)
class ToolCapability:
    state: str = UNKNOWN      # supported | unsupported | unknown
    reason: str = ""

    @property
    def usable(self) -> bool:
        """Tools are offered unless we KNOW they can't work."""
        return self.state != UNSUPPORTED


def _template_text(model_dir: Path) -> str:
    """The model's chat template as raw text, or "" if there isn't one."""
    for name in _TEMPLATE_FILES:
        path = model_dir / name
        try:
            if not path.is_file():
                continue
            raw = path.read_text(errors="replace")
        except OSError:
            continue
        if name.endswith(".jinja"):
            return raw
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        tpl = data.get("chat_template")
        if isinstance(tpl, str) and tpl:
            return tpl
        if isinstance(tpl, list):      # some repos ship [{name, template}, …]
            return "\n".join(str(t.get("template", "")) for t in tpl
                             if isinstance(t, dict))
    return ""


def from_template(model_dir: str | Path) -> ToolCapability:
    """Classify a local model directory's tool support."""
    text = _template_text(Path(model_dir))
    if not text:
        return ToolCapability(UNKNOWN, "no chat template found")
    low = text.lower()
    if any(m in low for m in _TOOL_MARKERS):
        return ToolCapability(SUPPORTED, "template renders tool calls")
    return ToolCapability(
        UNSUPPORTED,
        "the model's chat template has no tool support (roles are "
        "system/user/assistant only), and oMLX silently drops the tools array")


_cache: dict[tuple[str, str], ToolCapability] = {}


def reset_cache() -> None:
    _cache.clear()


def for_model(backend, model_id: str) -> ToolCapability:
    """Tool capability of `model_id` on the active endpoint.

    Cached per (endpoint, model). Never raises: any failure is `UNKNOWN`, which
    keeps the tools on — a detection bug must not silently disarm the agent.
    """
    if not model_id:
        return ToolCapability(UNKNOWN, "no model")
    base_url = getattr(backend, "base_url", "") or ""
    key = (base_url, model_id)
    if key in _cache:
        return _cache[key]

    cap = ToolCapability(UNKNOWN, "")
    try:
        org = origin_mod.origin_for(backend, model_id)
        if org.kind == "remote":
            cap = ToolCapability(UNKNOWN, "remote endpoint — weights not local")
        else:
            paths = getattr(backend, "model_paths", lambda: {})() or {}
            path = paths.get(model_id, "")
            cap = from_template(path) if path else ToolCapability(
                UNKNOWN, "the endpoint reports no model path")
    except Exception:
        cap = ToolCapability(UNKNOWN, "capability probe failed")
    _cache[key] = cap
    return cap
