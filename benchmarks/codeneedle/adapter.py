"""CodeNeedle prompt construction.

Ports the PROMPT_TEMPLATE + per-language anchor phrasing from upstream
`bench/runner.py` so we get identical wording without pulling in upstream's
HTTP client.
"""
from __future__ import annotations

from typing import Literal

Language = Literal["js", "py"]

PROMPT_TEMPLATE = (
    "{file_contents}\n"
    "\n"
    "---\n"
    "\n"
    "Task: reproduce verbatim the first {n} lines of the body of the function named "
    "`{name}`{file_qualifier} from the source above — i.e., the {n} lines {anchor_phrase}.\n"
    "\n"
    "Rules:\n"
    "- Output ONLY those lines, one per line, in original order.\n"
    "- Preserve original indentation and characters exactly.\n"
    "- Do NOT output the function signature or the line containing `{signature_marker}`.\n"
    "- Do NOT add commentary, line numbers, or markdown code fences.\n"
    "- If there are blank lines in the body, include them as blank lines.\n"
    "{thinking_suffix}"
)

ANCHOR_PHRASE: dict[Language, str] = {
    "js": (
        "starting immediately after the line containing `function {name}(` "
        "or the assignment that introduces it (the line with the opening "
        "brace `{{`)"
    ),
    "py": (
        "starting with the first body line after the `def {name}(...):` "
        "signature (including the docstring if present)"
    ),
}

SIGNATURE_MARKER: dict[Language, str] = {
    "js": "function {name}(",
    "py": "def {name}(",
}

NO_THINK_SUFFIX = "\n/no_think\n"


def build_prompt(
    *,
    file_contents: str,
    function_name: str,
    language: Language,
    n_lines: int,
    source_path: str | None = None,
    multi_file: bool = False,
    suppress_thinking: bool = True,
) -> str:
    """Build the codeneedle prompt for one target function.

    suppress_thinking=True appends `/no_think` to disable CoT on Qwen3
    (reasoning is wasteful for pure recall and risks drift).
    """
    anchor = ANCHOR_PHRASE[language].format(name=function_name)
    sig = SIGNATURE_MARKER[language].format(name=function_name)
    file_qualifier = f" in file `{source_path}`" if multi_file and source_path else ""
    return PROMPT_TEMPLATE.format(
        file_contents=file_contents,
        name=function_name,
        file_qualifier=file_qualifier,
        n=n_lines,
        anchor_phrase=anchor,
        signature_marker=sig,
        thinking_suffix=NO_THINK_SUFFIX if suppress_thinking else "",
    )
