"""Finishing a gitkit report: save → mirror → display → honesty lines.

The single-pass runner and deep mode each carried a copy of this tail and the
copies had drifted (different preview notices, a mirror line only one of them
logged through its own emitter). One implementation now; the callers pass the
only parts that genuinely differ — the frontmatter meta, a stats line, and any
extra lines to print once the report is saved.
"""

from __future__ import annotations

from pathlib import Path

from luxe.textfmt import truncate_for_display

# On-screen preview cap (the full report is always saved + shown with --verbose).
PREVIEW_LINES = 30


def finish_report(console, *, target: str, kind: str, report: str, head: str,
                  meta: dict, save: bool, mirror: bool, verbose: bool,
                  min_severity: str | None, stats_line: str | None = None,
                  after_saved: tuple[str, ...] = ()) -> Path | None:
    """Persist and render a finished report; return the saved path (or None).

    The SAVED report is always complete — `--min-severity` filters only what
    is displayed and prints a line counting what it hid (gitkit.sdd: never
    silently cap coverage). `meta` goes to `store.save_report` verbatim."""
    from rich.markdown import Markdown

    from luxe.gitkit import store

    saved: Path | None = None
    if save:
        saved = store.save_report(target, kind, report, meta=meta)
        if mirror and store.mirror_to_repo(target, kind, report, head):
            console.print("[dim]· mirrored map + report to <repo>/.luxe/gitkit/[/]")

    console.print()
    display_src, n_filtered = report, 0
    if min_severity:
        # DISPLAY-side only — the saved report above is always unfiltered.
        display_src, n_filtered = store.filter_min_severity(report, min_severity)
    if verbose:
        console.print(Markdown(display_src))
    else:
        shown, hidden = truncate_for_display(display_src, max_lines=PREVIEW_LINES)
        console.print(Markdown(shown))
        if hidden:
            console.print(f"[dim]… +{hidden} more lines — full report saved[/]")
    if n_filtered:
        where = saved if saved else "(not saved — run without --no-save)"
        console.print(f"[dim]Filtered: {n_filtered} findings below "
                      f"{min_severity} — full report at {where}[/]")
    if stats_line:
        console.print(stats_line)
    if saved:
        tail = "" if verbose else " — re-run with --verbose / -v for the full report"
        console.print(f"[green]✓[/] report saved to [cyan]{saved}[/]{tail}")
        for line in after_saved:
            console.print(line)
    return saved
