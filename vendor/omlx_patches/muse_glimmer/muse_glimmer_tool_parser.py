# SPDX-License-Identifier: Apache-2.0
"""Tool-call parser for Meta's Muse Glimmer ATEM format.

Implements the ``mlx_lm.tool_parsers`` contract: module-level
``tool_call_start`` / ``tool_call_end`` sentinels plus
``parse_tool_call(text, tools) -> dict | list[dict]``.

Muse Glimmer emits tool calls as XML-ish markup rather than JSON::

    <atem:function_calls>
    <atem:invoke name="read_file">
    <atem:parameter name="path">src/main.py</atem:parameter>
    <atem:parameter name="limit">40</atem:parameter>
    </atem:invoke>
    </atem:function_calls>

Two rules are quoted verbatim from the model's own chat template and are
load-bearing here:

    "Note that spaces for string values are not stripped. The output is not
     expected to be valid XML and is parsed with regular expressions."

Consequently this module:

* **never strips parameter values** — leading/trailing spaces and newlines are
  part of the value. Stripping silently corrupts code snippets and diffs, which
  is most of what luxe's write tools receive.
* **never uses an XML parser** — values legitimately contain ``<``, ``>``,
  unescaped quotes and multi-line text. The template's own worked example
  includes a value spanning three lines with embedded double quotes. Handing
  that to ElementTree raises; handing it to a non-greedy regex works.

Value typing mirrors the template's emitter (``render_atem``) exactly. It
writes ``true``/``false`` for booleans, ``null`` for none, ``| tojson`` for
mappings and non-string iterables, and the bare value otherwise — so a single
``json.loads`` attempt with a raw-string fallback inverts every branch. The
declared schema wins over that guess for ``type: string`` parameters, which is
what keeps a version string like ``"1.10"`` from being decoded as a float.
"""

from __future__ import annotations

import json
import re
from typing import Any

tool_call_start = "<atem:function_calls>"
tool_call_end = "</atem:function_calls>"

# Stdlib ``re`` is deliberate. The neighbouring mlx-lm parsers import the
# third-party ``regex`` package because gemma4 needs recursive ``(?R)`` groups
# for balanced braces; ATEM is delimiter-terminated, so non-greedy + DOTALL is
# sufficient and the extra dependency would only narrow where this can run.
#
# Non-greedy + DOTALL: values span lines and may contain '<' and '"'.
# The name attribute is delimited by double quotes, which the emitter never
# writes into a name, so ``[^"]+`` is safe for the attribute alone.
_INVOKE_RE = re.compile(
    r'<atem:invoke\s+name="([^"]*)"\s*>(.*?)</atem:invoke>',
    re.DOTALL,
)
_PARAM_RE = re.compile(
    r'<atem:parameter\s+name="([^"]*)"\s*>(.*?)</atem:parameter>',
    re.DOTALL,
)


def _string_arg_names(tool_name: str, tools: list[Any] | None) -> set[str]:
    """Parameter names the tool schema declares as ``type: string``."""
    if not tools:
        return set()
    for tool in tools:
        func = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(func, dict):
            # Some callers pass bare function objects rather than the
            # {"type": "function", "function": {...}} envelope.
            func = tool if isinstance(tool, dict) and "name" in tool else None
        if not func or func.get("name") != tool_name:
            continue
        properties = (func.get("parameters") or {}).get("properties") or {}
        return {
            name
            for name, schema in properties.items()
            if isinstance(schema, dict) and schema.get("type") == "string"
        }
    return set()


def _decode(raw: str, is_declared_string: bool) -> Any:
    """Invert the template's value emitter for one parameter.

    ``raw`` is passed through untouched when the schema declares a string, and
    otherwise round-tripped through ``json.loads`` — which covers ``true`` /
    ``false`` / ``null`` / numbers / arrays / objects in one call, because the
    emitter produced every one of those with JSON syntax.
    """
    if is_declared_string:
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        # Bare scalar the emitter wrote as-is (the common case for strings when
        # no schema was supplied). Preserved verbatim, spaces included.
        return raw


def _parse_invoke(name: str, body: str, tools: list[Any] | None) -> dict:
    # Whitespace in the *name* is a known champion-class defect: GLM-4.5-Air
    # emitted "read_file\n" and every dispatch missed until tools/base.py
    # started stripping. Strip here too so the parser can't reintroduce it.
    name = name.strip()
    string_args = _string_arg_names(name, tools)
    arguments: dict[str, Any] = {}
    for match in _PARAM_RE.finditer(body):
        key = match.group(1).strip()
        arguments[key] = _decode(match.group(2), key in string_args)
    return {"name": name, "arguments": arguments}


def parse_tool_call(text: str, tools: list[Any] | None = None):
    """Parse Muse Glimmer ATEM markup into one or more tool calls.

    Returns a single dict when the text holds one ``<atem:invoke>`` and a list
    when it holds several, matching the gemma4 / kimi_k2 parsers. Works whether
    or not the caller already stripped the ``<atem:function_calls>`` wrapper,
    since the invoke pattern is searched for anywhere in ``text``.
    """
    matches = list(_INVOKE_RE.finditer(text))
    if not matches:
        raise ValueError("No function provided.")
    calls = [_parse_invoke(m.group(1), m.group(2), tools) for m in matches]
    return calls[0] if len(calls) == 1 else calls
