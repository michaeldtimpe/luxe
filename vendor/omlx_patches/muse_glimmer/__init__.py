# SPDX-License-Identifier: Apache-2.0
"""Muse Glimmer ATEM tool-call parsing for oMLX.

Registers ``mlx_lm.tool_parsers.muse_glimmer`` and teaches parser inference to
recognise the ATEM chat template, without modifying the pinned mlx-lm/mlx-vlm
packages. Modelled on oMLX's own ``patches/hy_v3`` and ``patches/deepseek_v4``.

**Scope — this closes exactly one of four blockers.** Muse Glimmer support is
not usable until all four clear:

1. *Architecture* — ``model_type: muse_glimmer`` is absent from mlx-vlm 0.6.3
   (pinned by oMLX 0.5.7), from released 0.6.10, and from ``main``. It lives in
   open PR Blaizzy/mlx-vlm#1838. **This patch does not provide the arch** and
   deliberately does not try to; a 52-layer decoder plus ViT belongs upstream.
2. *Weights* — only the 4-bit mlx-community conversion has real bytes, built
   with an unreleased dev build; PR #1839 fixes ``embed_norm`` being dropped
   when quantizing its embeddings. The 6-bit repo (the one that would match the
   champion's precision) is still an empty placeholder.
3. *Tool-call format* — **what this patch fixes.** Without it the ATEM markup
   is returned as ordinary assistant content, the caller sees zero tool calls,
   and an agentic benchmark scores ~0 on a parser gap rather than on model
   quality.
4. *Engine* — it is a VLM, so oMLX routes it to the slower vlm engine.

Blocker 3 is independent of the rest: it does **not** clear when #1838 merges,
which is why it is worth patching ahead of the others.

Both inference paths need patching. ``mlx_vlm.tool_parsers`` does
``from mlx_lm.tokenizer_utils import _infer_tool_parser`` at import time, so it
holds a *bound reference* to the original function — rebinding the mlx-lm
attribute alone would leave the VLM path (the one Muse Glimmer actually takes)
still blind. Verified against the published template: ATEM matches none of the
existing markers, so inference currently returns ``None`` for it.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PR_URL = "https://github.com/Blaizzy/mlx-vlm/pull/1838"
QUANT_PR_URL = "https://github.com/Blaizzy/mlx-vlm/pull/1839"

# Present in the ATEM chat template and in nothing else mlx-lm/mlx-vlm knows.
_TEMPLATE_MARKER = "<atem:invoke"
_PARSER_NAME = "muse_glimmer"

_APPLIED = False


def _register_parser_module() -> None:
    """Install the vendored parser as ``mlx_lm.tool_parsers.muse_glimmer``.

    ``mlx_vlm.tool_parsers.load_tool_module`` checks for a mlx_vlm-local module
    first and falls back to mlx_lm, so registering it once under mlx_lm serves
    both the LM and VLM paths.
    """
    qualname = f"mlx_lm.tool_parsers.{_PARSER_NAME}"
    if qualname in sys.modules:
        return

    file_path = Path(__file__).parent / "muse_glimmer_tool_parser.py"
    spec = importlib.util.spec_from_file_location(qualname, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create spec for {qualname} from {file_path}")

    module = importlib.util.module_from_spec(spec)
    module.__package__ = "mlx_lm.tool_parsers"
    sys.modules[qualname] = module
    spec.loader.exec_module(module)

    parent = importlib.import_module("mlx_lm.tool_parsers")
    setattr(parent, _PARSER_NAME, module)
    logger.info("Registered %s", qualname)


def _wrap_infer(original):
    """Return an ATEM-aware wrapper around an ``_infer_tool_parser``."""

    def _infer_with_muse_glimmer(chat_template):
        if isinstance(chat_template, str) and _TEMPLATE_MARKER in chat_template:
            return _PARSER_NAME
        return original(chat_template)

    _infer_with_muse_glimmer._omlx_muse_glimmer = True
    return _infer_with_muse_glimmer


def _patch_infer_tool_parser() -> None:
    """Patch inference on the mlx-lm path and, separately, the mlx-vlm path."""
    import mlx_lm.tokenizer_utils as tu

    if not getattr(tu._infer_tool_parser, "_omlx_muse_glimmer", False):
        tu._infer_tool_parser = _wrap_infer(tu._infer_tool_parser)

    # mlx_vlm bound its own reference at import time; patch it independently.
    try:
        import mlx_vlm.tool_parsers as vtp
    except ImportError:
        logger.debug("mlx_vlm.tool_parsers absent - VLM inference not patched")
        return

    if not getattr(vtp._infer_tool_parser, "_omlx_muse_glimmer", False):
        vtp._infer_tool_parser = _wrap_infer(vtp._infer_tool_parser)


def apply_muse_glimmer_patch() -> bool:
    """Register ATEM tool-call support when the pinned packages lack it.

    Returns True when the patch was applied, False when it was skipped because
    mlx-lm/mlx-vlm already ship a ``muse_glimmer`` parser (upstream-first: this
    becomes a no-op the moment they do) or mlx-lm is not importable.
    """
    global _APPLIED
    if _APPLIED:
        return False

    try:
        import mlx_lm  # noqa: F401
    except ImportError:
        logger.debug("mlx_lm not importable - muse_glimmer patch skipped")
        return False

    if importlib.util.find_spec(f"mlx_lm.tool_parsers.{_PARSER_NAME}") is not None:
        _APPLIED = True
        logger.debug("mlx_lm.tool_parsers.%s already available upstream", _PARSER_NAME)
        return False

    _register_parser_module()
    _patch_infer_tool_parser()
    _APPLIED = True
    logger.info("Muse Glimmer ATEM tool parser applied (arch still needs %s)", PR_URL)
    return True


def is_applied() -> bool:
    return _APPLIED


__all__ = ["apply_muse_glimmer_patch", "is_applied", "PR_URL", "QUANT_PR_URL"]
