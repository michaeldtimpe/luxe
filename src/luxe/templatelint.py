"""Report-only lint for chat templates (2026-10, model provenance).

A chat template is Jinja that runs on every prompt. The attack it enables is a
HIDDEN BRANCH: text that only reaches the model when a message contains some
trigger phrase (`{% if 'blue harvest' in message.content %}…`), which no weight
scanner and no casual read of the rendered prompt will ever show. The second
shape worth flagging is a URL — a template has no business naming a host.

Two rules, deliberately narrow so the fleet's real templates come out clean
(Qwen3.6 family, Qwen3.8, GLM-4.5-Air, Muse-Glimmer, gemma-4 — audited by hand
on m5, 2026-10; representative snippets pinned in tests/test_template_pins.py):

  url              any `http://` / `https://` in the template text
  content-literal  message content compared to a string literal — `LIT in X`,
                   `X == LIT` / `!=`, `X.startswith/endswith/find(LIT)`,
                   `X in [LIT, …]`, `selectattr('content', <test>, LIT)` —
                   where X mentions `content` (the identifier or the key),
                   or a variable `{% set %}` from such an expression

The comparisons the standard reasoning/tool parsing needs are BENIGN and never
reported: `<think>`, `</think>`, `<tool_response>`, `</tool_response>`, the
`/think` / `/nothink` soft switches (GLM-4.5's own template checks
`visible_text(m.content).endswith("/nothink")`), and empty/whitespace literals,
which cannot carry a phrase. `role`/`type` comparisons are not content.

This is a heuristic, and it never blocks anything: it finds the obvious shape
of the attack and puts it in front of a human (on `luxe pull`, `luxe pull
--show-template`, and `/doctor`'s template line). The PIN is the control — a
swapped template shows up as a hash mismatch whether or not the lint sees it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Literals the standard Qwen/GLM parsing compares content against.
BENIGN_LITERALS = frozenset({
    "<think>", "</think>", "<tool_response>", "</tool_response>",
    "/think", "/nothink",
})

_URL = re.compile(r"https?://[^\s'\"<>{}|\\^`]+", re.IGNORECASE)
_TAG = re.compile(r"\{%-?(.*?)-?%\}|\{\{-?(.*?)-?\}\}", re.DOTALL)
_LIT = re.compile(r"""'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)\"""")
_CONTENT = re.compile(r"\bcontent\b")
_SET = re.compile(r"^\s*set\s+([A-Za-z_]\w*)\s*=\s*(.+)$", re.DOTALL)
_STR_METHODS = ("startswith", "endswith", "find", "rfind", "index", "rindex",
                "count")
_SELECTATTR = re.compile(
    r"""(?:select|reject)attr\(\s*['"]content['"]\s*,\s*['"][^'"]*['"]\s*,\s*""")


@dataclass(frozen=True)
class LintFinding:
    rule: str          # "url" | "content-literal"
    line: int          # 1-based line in the template
    literal: str       # the URL, or the compared literal
    excerpt: str       # the offending Jinja expression, trimmed

    def describe(self) -> str:
        if self.rule == "url":
            return f"line {self.line}: URL {self.literal}"
        return (f"line {self.line}: content compared to {self.literal!r} "
                f"({self.excerpt})")


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def _is_benign(lit: str) -> bool:
    return lit.strip() == "" or lit in BENIGN_LITERALS


def _operand_before(code: str, end: int) -> str:
    """The expression immediately left of `end` (balanced brackets, dotted
    names, calls, subscripts, `| filter` chains)."""
    i = end
    depth = 0
    while i > 0:
        ch = code[i - 1]
        if ch in ")]":
            depth += 1
        elif ch in "([":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and not (ch.isalnum() or ch in "_.'\"|"):
            # allow `x | lower` — a space next to a pipe stays in the operand
            j = i - 1
            while j > 0 and code[j - 1] == " ":
                j -= 1
            if ch == " " and (code[j - 1:j] == "|" or code[i:i + 1] == "|"):
                i = j
                continue
            break
        i -= 1
    return code[i:end]


def _operand_after(code: str, start: int) -> str:
    """The expression immediately right of `start` (mirror of the above)."""
    n = len(code)
    i = start
    while i < n and code[i] == " ":
        i += 1
    begin = i
    depth = 0
    while i < n:
        ch = code[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and not (ch.isalnum() or ch in "_.'\"|"):
            j = i
            while j < n and code[j] == " ":
                j += 1
            if ch == " " and (code[j:j + 1] == "|" or code[i - 1:i] == "|"):
                i = j
                continue
            break
        i += 1
    return code[begin:i]


def _strip_literals(expr: str) -> str:
    return _LIT.sub("''", expr)


class _Linter:
    def __init__(self, text: str):
        self.text = text
        self.tainted: set[str] = set()
        self.findings: list[LintFinding] = []

    def mentions_content(self, expr: str) -> bool:
        bare = _strip_literals(expr)
        if _CONTENT.search(bare) or re.search(r"""\[\s*['"]content['"]\s*\]""",
                                              expr):
            return True
        names = set(re.findall(r"\b[A-Za-z_]\w*\b", bare))
        return bool(names & self.tainted)

    def flag(self, lit: str, code: str, offset: int, base: int) -> None:
        if _is_benign(lit):
            return
        line = self.text.count("\n", 0, base + offset) + 1
        excerpt = " ".join(code.split())
        if len(excerpt) > 120:
            excerpt = excerpt[:117] + "…"
        f = LintFinding("content-literal", line, lit, excerpt)
        if f not in self.findings:
            self.findings.append(f)

    def check_tag(self, code: str, base: int) -> None:
        m = _SET.match(code)
        if m and self.mentions_content(m.group(2)):
            self.tainted.add(m.group(1))
        for lm in _LIT.finditer(code):
            raw = lm.group(1) if lm.group(1) is not None else lm.group(2)
            lit = _unescape(raw)
            s, e = lm.span()
            after = code[e:]
            before = code[:s]
            # LIT [not] in X
            am = re.match(r"\s+(?:not\s+)?in\b", after)
            if am and self.mentions_content(_operand_after(code, e + am.end())):
                self.flag(lit, code, s, base)
                continue
            # LIT ==/!= X
            am = re.match(r"\s*(?:==|!=)", after)
            if am and self.mentions_content(_operand_after(code, e + am.end())):
                self.flag(lit, code, s, base)
                continue
            # X ==/!= LIT
            bm = re.search(r"(?:==|!=)\s*$", before)
            lhs = code[:bm.start()].rstrip() if bm else ""
            if bm and self.mentions_content(_operand_before(lhs, len(lhs))):
                self.flag(lit, code, s, base)
                continue
            # X.startswith(LIT) and friends
            bm = re.search(r"\.(" + "|".join(_STR_METHODS) + r")\(\s*$", before)
            if bm and self.mentions_content(_operand_before(code, bm.start())):
                self.flag(lit, code, s, base)
                continue
            # X [not] in [LIT, …] / (LIT, …)
            bm = re.search(r"\b(?:not\s+)?in\s*[\[(][^\])]*$", before)
            if bm:
                lhs = re.sub(r"\s+(?:not\s+)?in\s*[\[(][^\])]*$", "", before)
                if self.mentions_content(_operand_before(lhs, len(lhs))):
                    self.flag(lit, code, s, base)
                    continue
            # selectattr('content', 'equalto', LIT)
            if _SELECTATTR.search(before[-80:] if len(before) > 80 else before) \
                    and before.rstrip().endswith(","):
                self.flag(lit, code, s, base)

    def run(self) -> list[LintFinding]:
        for m in _URL.finditer(self.text):
            line = self.text.count("\n", 0, m.start()) + 1
            self.findings.append(LintFinding("url", line, m.group(0),
                                             m.group(0)))
        for m in _TAG.finditer(self.text):
            idx = 1 if m.group(1) is not None else 2
            self.check_tag(m.group(idx), m.start(idx))
        return sorted(self.findings, key=lambda f: (f.line, f.rule))


def lint_template(text: str) -> list[LintFinding]:
    """Findings for one template's text, in line order. Never raises."""
    try:
        return _Linter(text or "").run()
    except Exception:        # a lint bug must never break pull or doctor
        return []
