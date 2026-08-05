"""The import graph, pinned.

`chat/` is the top tier: it may reach DOWN into `agents/`, `tools/`, and the
root-tier modules, and nothing below it may reach back up at module level.
Three cycles existed until the 2026-08-04 consolidation, all dodged with
function-local imports rather than fixed:

- `modelstore` → `chat.origin` for the mount parse (now `luxe.mounts`)
- `gitkit` → `chat.render` for cancellation + display truncation
  (now `luxe.cancel` / `luxe.textfmt`)
- `gitkit` → `cli` for language detection (now `luxe.repo_index`)

A function-local import is not a fix — it hides the cycle from the reader and
from every static tool. These tests are what makes the cleanup durable.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "luxe"

# Modules outside `chat/` that are ALLOWED a module-level `luxe.chat.*` import,
# with the reason. `cli` is the composition root: it wires the CLI surface onto
# the chat front-end, so it sits ABOVE chat and the direction is correct.
SANCTIONED_MODULE_LEVEL_CHAT_IMPORTS = {
    "cli.py": {"luxe.chat.launch"},
}


def _py_files() -> list[pathlib.Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(SRC).as_posix()


def _imported_modules(node: ast.AST) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        # `from . import x` has no module; relative imports never cross tiers.
        return [node.module] if node.module and not node.level else []
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    return []


def _module_level_imports(tree: ast.Module) -> list[str]:
    out: list[str] = []
    for node in tree.body:
        out += _imported_modules(node)
        # Imports guarded by `if TYPE_CHECKING:` / `try:` still execute (or
        # not) at module scope — count them.
        if isinstance(node, (ast.If, ast.Try)):
            for sub in ast.walk(node):
                out += _imported_modules(sub)
    return out


def _function_local_imports(tree: ast.Module) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            for mod in _imported_modules(sub):
                out.append((node.name, mod))
    return out


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def test_nothing_below_chat_imports_chat_at_module_level():
    """A root-tier or sibling module importing `chat.*` at module level is a
    layering inversion — it makes the low-level module unusable without the
    whole interactive front-end, and (for `modelstore`) it was a real cycle."""
    offenders: list[str] = []
    for path in _py_files():
        rel = _rel(path)
        if rel.startswith("chat/"):
            continue
        allowed = SANCTIONED_MODULE_LEVEL_CHAT_IMPORTS.get(rel, set())
        for mod in _module_level_imports(_parse(path)):
            if mod.startswith("luxe.chat") and mod not in allowed:
                offenders.append(f"{rel} imports {mod}")
    assert not offenders, (
        "module-level luxe.chat imports from outside chat/: "
        + "; ".join(offenders)
        + ". Move the shared piece to a neutral module (luxe/mounts.py, "
          "luxe/cancel.py, luxe/textfmt.py are the precedents) and re-export "
          "it from chat/."
    )


def test_agents_never_imports_chat():
    """`agents/` is the benchmark path. It must run with no chat package
    present at all — at module level OR inside a function."""
    offenders: list[str] = []
    for path in _py_files():
        rel = _rel(path)
        if not rel.startswith("agents/"):
            continue
        tree = _parse(path)
        for mod in _module_level_imports(tree):
            if mod.startswith("luxe.chat"):
                offenders.append(f"{rel} imports {mod}")
        for fn, mod in _function_local_imports(tree):
            if mod.startswith("luxe.chat"):
                offenders.append(f"{rel}::{fn} imports {mod}")
    assert not offenders, (
        "agents/ reaches into chat/: " + "; ".join(offenders))


@pytest.mark.parametrize("forbidden", ["luxe.chat.render", "luxe.cli"])
def test_gitkit_has_no_cycle_dodging_local_imports(forbidden: str):
    """gitkit used to import `chat.render` (cancellation, truncation) and
    `cli` (language detection) from inside function bodies purely to keep the
    cycle invisible. Both now live in neutral modules and are imported at the
    top of the file, where a reader and a linter can see them."""
    offenders: list[str] = []
    for path in _py_files():
        rel = _rel(path)
        if not rel.startswith("gitkit/"):
            continue
        for fn, mod in _function_local_imports(_parse(path)):
            if mod == forbidden:
                offenders.append(f"{rel}::{fn}")
    assert not offenders, (
        f"gitkit still imports {forbidden} inside a function: "
        + "; ".join(offenders)
        + ". Import from luxe.cancel / luxe.textfmt / luxe.repo_index at "
          "module level instead."
    )


def test_the_neutral_modules_import_nothing_from_chat_or_cli():
    """The whole point of `luxe/{mounts,cancel,textfmt}.py` is that they sit
    below both. If one grows an upward import the cycle is back."""
    for name in ("mounts.py", "cancel.py", "textfmt.py"):
        tree = _parse(SRC / name)
        mods = [m for m in _module_level_imports(tree)] + [
            m for _fn, m in _function_local_imports(tree)]
        bad = [m for m in mods if m.startswith("luxe.chat") or m == "luxe.cli"]
        assert not bad, f"{name} imports {bad} — it must stay neutral"


def test_modelstore_does_not_import_chat():
    """The founding case (C1): `modelstore` is reached by `luxe pull` on a host
    with no chat extra installed."""
    tree = _parse(SRC / "modelstore.py")
    mods = _module_level_imports(tree) + [m for _f, m in _function_local_imports(tree)]
    assert not [m for m in mods if m.startswith("luxe.chat")]
