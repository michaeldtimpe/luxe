"""Compare result persistence + the review reader.

Layout under ~/.luxe/compare/<compare_id>/:
  meta.json    — task, task_type, blind, side variant metadata, timestamp
  side_a.json  — SideResult
  side_b.json
  votes.jsonl  — append-only {winner, reason, blind, ts} (best-of-N re-votes)
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from luxe.compare.run_pair import CompareResult, SideResult


def compare_root() -> Path:
    return Path.home() / ".luxe" / "compare"


def compare_dir(compare_id: str) -> Path:
    return compare_root() / compare_id


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def save(result: CompareResult) -> Path:
    d = compare_dir(result.compare_id)
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "compare_id": result.compare_id,
        "task": result.task,
        "task_type": result.task_type,
        "blind": result.blind,
        "ts": time.time(),
        "sides": [
            {"label": s.label, "model_id": s.model_id, "variant_id": s.variant_id,
             "substrate_env": s.substrate_env}
            for s in result.sides
        ],
    }
    _atomic_write(d / "meta.json", json.dumps(meta, indent=2))
    for i, s in enumerate(result.sides):
        name = "side_a.json" if i == 0 else "side_b.json"
        _atomic_write(d / name, json.dumps(asdict(s), indent=2))
    return d


def record_vote(compare_id: str, winner: str, *, reason: str = "", blind: bool = False) -> None:
    p = compare_dir(compare_id) / "votes.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {"winner": winner, "reason": reason, "blind": blind, "ts": time.time()}
    with p.open("a") as f:
        f.write(json.dumps(record) + "\n")


def load(compare_id: str) -> tuple[dict, list[SideResult], list[dict]] | None:
    d = compare_dir(compare_id)
    meta_p = d / "meta.json"
    if not meta_p.is_file():
        return None
    meta = json.loads(meta_p.read_text())
    sides: list[SideResult] = []
    for name in ("side_a.json", "side_b.json"):
        p = d / name
        if p.is_file():
            sides.append(SideResult(**json.loads(p.read_text())))
    votes: list[dict] = []
    vp = d / "votes.jsonl"
    if vp.is_file():
        for line in vp.read_text().splitlines():
            line = line.strip()
            if line:
                votes.append(json.loads(line))
    return meta, sides, votes


def list_compares() -> list[dict]:
    out: list[dict] = []
    if not compare_root().is_dir():
        return out
    for d in compare_root().iterdir():
        meta_p = d / "meta.json"
        if meta_p.is_file():
            try:
                out.append(json.loads(meta_p.read_text()))
            except json.JSONDecodeError:
                continue
    out.sort(key=lambda m: m.get("ts", 0), reverse=True)
    return out


def tally(votes: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for v in votes:
        w = v.get("winner", "")
        counts[w] = counts.get(w, 0) + 1
    return counts


def review(compare_id: str, *, console) -> None:
    """Replay a stored comparison + tally its votes."""
    if not compare_id:
        compares = list_compares()
        if not compares:
            console.print("[dim]No stored comparisons.[/]")
            return
        console.print(f"[bold]Stored comparisons[/] ({len(compares)})")
        for m in compares[:20]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.get("ts", 0)))
            console.print(f"  [cyan]{m['compare_id']}[/]  {when}  [dim]{m['task'][:50]}[/]")
        return
    loaded = load(compare_id)
    if loaded is None:
        console.print(f"[yellow]No comparison {compare_id!r}.[/]")
        return
    from luxe.compare import present
    meta, sides, votes = loaded
    result = CompareResult(
        compare_id=meta["compare_id"], task=meta["task"],
        task_type=meta["task_type"], blind=False, sides=sides,
    )
    console.print(f"[bold]Comparison[/] [cyan]{compare_id}[/]  [dim]{meta['task']}[/]")
    present.render_side_by_side(console, result, blind=False)
    counts = tally(votes)
    if counts:
        console.print("[bold]votes:[/] " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    else:
        console.print("[dim](no votes recorded)[/]")
