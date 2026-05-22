"""Multi-turn BFCL: live stateful execution surface for the generation loop.

During GENERATION the model's emitted calls are executed against *live* persistent
involved-class instances so it sees real results. (GRADING is separate: the vendored
`multi_turn_checker` re-executes the recorded call-strings on its own *fresh* instances.)

This module is behavioural (kept out of the types-only `schemas.py`). It provides:
  * `to_call_string` — (name, args) -> a BFCL call-string the vendored checker eval()s.
  * `make_stateful_executor` — wrap an instance method as a luxe ToolFn (fail-soft).
  * `build_tool_surface` — instantiate a problem's involved classes + build ToolDefs/tool_fns.
"""

from __future__ import annotations

import copy
import importlib
import inspect
import json
from pathlib import Path
from typing import Any

from luxe.tools.base import ToolDef, ToolFn

from .executable_backend_config import (
    CLASS_FILE_PATH_MAPPING,
    MULTI_TURN_FUNC_DOC_FILE_MAPPING,
    STATELESS_CLASSES,
)
from ..schemas import bfcl_func_spec_to_tool_def

_FUNC_DOC_DIR = Path(__file__).parent / "func_doc"


def _canon(v: Any) -> Any:
    """Recursively key-sort dicts (lists keep order — they're semantically ordered).
    Makes `to_call_string` output deterministic for reproducible traces/diffs."""
    if isinstance(v, dict):
        return {k: _canon(v[k]) for k in sorted(v)}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    return v


def to_call_string(name: str, args: dict[str, Any]) -> str:
    """Serialize (name, args) to a BFCL call-string, e.g. `cd(folder='document')`.

    Args are sorted alphabetically and values rendered with `repr()` (eval-able
    Python literals: str/int/float/bool/None/nested dict+list all round-trip).
    NOTE: the vendored checker compares by RE-EXECUTION STATE, not string equality,
    so arg order does not affect correctness — sorting is purely for trace
    reproducibility. The call form `name(kw=val, ...)` is required so the checker's
    `_process_method_calls` regex can prepend the instance name before eval().
    """
    parts = [f"{k}={_canon(args[k])!r}" for k in sorted(args)]
    return f"{name}({', '.join(parts)})"


def _serialize_result(res: Any) -> str:
    """Mirror multi_turn_utils' result serialization so the model sees the same
    feedback shape the official harness produces."""
    if isinstance(res, str):
        return res
    if isinstance(res, dict):
        try:
            return json.dumps(res)
        except Exception:
            return str(res)
    return str(res)


def make_stateful_executor(instance: Any, method_name: str) -> ToolFn:
    """Wrap `instance.method_name` as a luxe ToolFn `(args) -> (result, error|None)`.

    Fail-soft: any execution error (wrong args, illegal state transition, …) is
    returned as a STRUCTURED error string (`TypeError: ...`) so the model can
    self-correct and the loop never crashes mid-sequence. No traceback (too noisy).
    """
    method = getattr(instance, method_name)

    def _exec(args: dict[str, Any]) -> tuple[str, str | None]:
        try:
            return _serialize_result(method(**args)), None
        except Exception as e:  # noqa: BLE001 — surface to the model, never raise
            return "", f"{type(e).__name__}: {e}"

    return _exec


def build_tool_surface(
    involved_classes: list[str],
    initial_config: dict[str, Any],
) -> tuple[list[ToolDef], dict[str, ToolFn], dict[str, Any]]:
    """Instantiate a problem's involved classes (live, for generation) and build
    the model's tool surface from the vendored func-doc specs.

    Returns (tool_defs, tool_fns, instances). The tool surface is restricted to
    `intersection(public instance methods, func-doc-declared names)` — only those
    names get a ToolDef + executor (the intersection IS the whitelist; any other
    name the model emits falls through `dispatch_tool` as "Unknown tool").
    Classes in STATELESS_CLASSES skip `_load_scenario`; others load a DEEP-COPIED
    initial_config (pristine per problem — no shared-object bleed).
    """
    tool_defs: list[ToolDef] = []
    tool_fns: dict[str, ToolFn] = {}
    instances: dict[str, Any] = {}

    for class_name in involved_classes:
        module = importlib.import_module(CLASS_FILE_PATH_MAPPING[class_name])
        instance = getattr(module, class_name)()
        if class_name not in STATELESS_CLASSES:
            instance._load_scenario(
                copy.deepcopy(initial_config.get(class_name, {}))
            )
        instances[class_name] = instance

        public_methods = {
            m for m, _ in inspect.getmembers(instance, predicate=inspect.ismethod)
            if not m.startswith("_")
        }
        specs = _load_func_doc(class_name)
        for spec in specs:
            name = spec.get("name", "")
            if name not in public_methods:
                continue  # declared but not a real public method — skip (vendor drift guard)
            tool_defs.append(bfcl_func_spec_to_tool_def(spec))
            tool_fns[name] = make_stateful_executor(instance, name)

    return tool_defs, tool_fns, instances


def _load_func_doc(class_name: str) -> list[dict[str, Any]]:
    """Load the JSONL func-doc tool specs for an involved class."""
    fname = MULTI_TURN_FUNC_DOC_FILE_MAPPING[class_name]
    path = _FUNC_DOC_DIR / fname
    out: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
