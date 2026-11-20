#!/usr/bin/env python3
"""Canonical checker for the `nothing-ever-happens-document-config` fixture.

The goal asks for a CONFIG.md that documents "every environment variable the
bot reads at startup". Until 2026-11-20 each hand-verify re-derived that set
by hand and got 23, 27, 32 or 57 names, so document-config coverage was not
comparable across reports (acceptance/doc_config_rescore_2026_11/REPORT.md).
This script is the one definition:

  derive   AST walk of `bot/**/*.py` at the fixture's base_sha: every call
           whose callee is an env reader (`os.getenv`, `os.environ.get`,
           `os.environ[...]`, the repo's `_env_*` helpers, and
           `RiskConfig.from_env`'s local `_f`) with a string-literal first
           argument. `scripts/` is excluded (not the bot). Prints one name
           per line; `--check` compares with fixtures.yaml R4 and exits 1 on
           drift.
  score    whole-word coverage of the R4 token list in the ADDED lines of
           one or more diff.patch files (the same rule the bench's
           `tokens_present` requirement applies).

  python3 scripts/doc_config_envcheck.py derive [--check]
  python3 scripts/doc_config_envcheck.py score <diff.patch>...
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ID = "nothing-ever-happens-document-config"
CACHE = Path.home() / ".luxe" / "fixture-cache" / "nothing-ever-happens"
ENV_HELPERS = re.compile(r"^(?:getenv|_env\w*|_f)$")


def _fixture() -> dict:
    import yaml
    d = yaml.safe_load((ROOT / "benchmarks/maintain_suite/fixtures.yaml").read_text())
    fx = d["fixtures"] if isinstance(d, dict) else d
    return next(f for f in fx if f["id"] == FIXTURE_ID)


def r4_tokens() -> list[str]:
    for r in _fixture().get("requirements", []):
        if r.get("kind") == "tokens_present":
            return list(r["tokens"])
    raise SystemExit("fixtures.yaml: no tokens_present requirement on " + FIXTURE_ID)


def _callee(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        # os.environ.get -> "environ.get"; os.getenv -> "getenv"
        if isinstance(f.value, ast.Attribute) and f.value.attr == "environ":
            return "environ." + f.attr
        return f.attr
    return ""


def env_reads(source: str) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and node.args:
            name = _callee(node)
            a0 = node.args[0]
            if (isinstance(a0, ast.Constant) and isinstance(a0.value, str)
                    and (ENV_HELPERS.match(name) or name == "environ.get")):
                out.add(a0.value)
        elif isinstance(node, ast.Subscript):          # os.environ["X"]
            v = node.value
            if (isinstance(v, ast.Attribute) and v.attr == "environ"
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)):
                out.add(node.slice.value)
    return out


def derive(repo: Path, sha: str) -> list[str]:
    files = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", sha, "bot/"],
                           capture_output=True, text=True, check=True).stdout.split()
    names: set[str] = set()
    for f in files:
        if f.endswith(".py"):
            src = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{f}"],
                                 capture_output=True, text=True, check=True).stdout
            names |= env_reads(src)
    return sorted(n for n in names if re.fullmatch(r"[A-Z][A-Z0-9_]+", n))


def added_text(patch: str) -> str:
    return "\n".join(ln[1:] for ln in patch.splitlines()
                     if ln.startswith("+") and not ln.startswith("+++"))


def coverage(text: str, tokens: list[str]) -> tuple[list[str], list[str]]:
    hit = [t for t in tokens if re.search(rf"(?<![A-Za-z0-9_]){re.escape(t)}(?![A-Za-z0-9_])", text)]
    return hit, [t for t in tokens if t not in hit]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("derive")
    d.add_argument("--check", action="store_true")
    s = sub.add_parser("score")
    s.add_argument("patches", nargs="+", type=Path)
    a = ap.parse_args()
    if a.cmd == "derive":
        names = derive(CACHE, _fixture()["base_sha"])
        if a.check:
            r4 = r4_tokens()
            if names != sorted(r4):
                print("DRIFT: derived", len(names), "vs R4", len(r4))
                print("  only derived:", sorted(set(names) - set(r4)))
                print("  only R4:", sorted(set(r4) - set(names)))
                return 1
            print(f"OK: R4 matches the derived set ({len(names)} names)")
            return 0
        print("\n".join(names))
        return 0
    tokens = r4_tokens()
    for p in a.patches:
        hit, miss = coverage(added_text(p.read_text(errors="replace")), tokens)
        print(f"{len(hit):2d}/{len(tokens)}  {p}" + (f"  missing: {' '.join(miss)}" if miss else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
