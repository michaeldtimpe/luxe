"""The repo-root + search-index swap every gitkit entry point performs.

`run_git_report`, the apply executor and `luxe init` each pointed the tool
surface's repo root and the resident BM25/symbol indices at their target and
restored the previous state afterwards — three hand-rolled copies whose
restore paths had drifted (one never reset a root that was unset before, one
re-set an index to None instead of resetting it). One context manager now.
"""

from __future__ import annotations

import contextlib
from pathlib import Path


@contextlib.contextmanager
def indexed_target(target: str | Path, *, console=None, reuse: bool = True,
                   note: str = "· Indexing repository for search…"):
    """Point `repo_root` and the BM25/symbol indices at `target` for the
    block, then restore EXACTLY what was resident before (an unset root is
    unset again — a chat session with no project stays project-less).

    `reuse=True` skips the rebuild when the resident root already IS the
    target (the REPL case: the session's own indices cover it). Yields
    whether it swapped."""
    from luxe import search as search_mod
    from luxe import symbols as symbols_mod
    from luxe.tools import fs

    prev_root = fs.get_repo_root()
    if reuse and prev_root is not None and str(prev_root) == str(target):
        yield False
        return
    prev_bm25 = search_mod._index   # module-level resident index (no public getter)
    prev_sym = symbols_mod._index
    try:
        fs.set_repo_root(target)
        if console is not None and note:
            console.print(f"[dim]{note}[/]")
        search_mod.set_index(search_mod.build_bm25_index(str(target)))
        symbols_mod.set_index(symbols_mod.build_symbol_index(str(target)))
        yield True
    finally:
        if prev_root is not None:
            fs.set_repo_root(prev_root)
        else:
            fs._REPO_ROOT = None
        if prev_bm25 is not None:
            search_mod.set_index(prev_bm25)
        else:
            search_mod.reset_index()
        if prev_sym is not None:
            symbols_mod.set_index(prev_sym)
        else:
            symbols_mod.reset_index()
