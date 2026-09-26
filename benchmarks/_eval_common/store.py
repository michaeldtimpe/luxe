"""Resumable per-item result store shared by the extended-benchmark runners
(gsm8k, gsm8k agentic, mmlu, arc_challenge, codeneedle, codeneedle agentic).

Each runner used to hand-roll the same resume loop, with three defects:

  * resume reused ANY file at the item's path — a directory first filled by
    another model or agentic variant was silently merged into this run;
  * summaries globbed every `item_*.json` / `q_*.json` in the directory, so a
    `--limit 50` re-run reported over the 1000 items an earlier run left;
  * a backend exception was scored like an answer (gsm8k even extracted
    numbers out of the error text) and cached, so the failure replayed on
    every resume.

The store fixes all three by construction: every record carries a `_store`
stamp (model, variant, and an optional per-item fingerprint) and a cached
record is reused only when the stamp matches; an infra error is written for
visibility but is NEVER reused and never enters a denominator; and
`collect()` reads back exactly the ids the caller selected.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

STATUS_OK = "ok"
STATUS_INFRA_ERROR = "infra_error"


def safe_name(name: str) -> str:
    """Filesystem-safe item id (codeneedle function names carry `.`/`$`)."""
    return "".join(c if c.isalnum() or c in "_-." else "_" for c in name)


def json_default(obj: Any) -> Any:
    """Enum-ish objects serialize as their value, anything else as str()."""
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


# Agent-loop env per agentic variant — shared by gsm8k/codeneedle agentic.
AGENTIC_VARIANT_ENV: dict[str, dict[str, str]] = {
    "minimal": {
        "LUXE_TIERED_COMPACT": "0",
        "LUXE_REFLECT": "0",
        "LUXE_RESPOND_TERMINAL": "1",
        "LUXE_WRITE_PRESSURE": "0",
        "LUXE_EARLY_BAIL": "0",
        "LUXE_ACTION_DENSITY_GATE": "0",
        "LUXE_CONVERGENCE_GATE": "0",
        "LUXE_PROSE_BURST": "0",
    },
    "full": {
        "LUXE_TIERED_COMPACT": "1",
        "LUXE_REFLECT": "1",
        "LUXE_RESPOND_TERMINAL": "1",
        "LUXE_WRITE_PRESSURE": "1",
        "LUXE_EARLY_BAIL": "1",
        "LUXE_ACTION_DENSITY_GATE": "1",
        "LUXE_CONVERGENCE_GATE": "1",
        "LUXE_PROSE_BURST": "1",
    },
}


def apply_variant_env(variant: str) -> None:
    for k, v in AGENTIC_VARIANT_ENV[variant].items():
        os.environ[k] = v


@dataclass
class Collected:
    """What `collect()` found for the selected ids."""
    records: list[dict[str, Any]] = field(default_factory=list)   # graded
    infra_errors: int = 0
    missing: int = 0


class ResultStore:
    """One run's per-item records under `root`, keyed on (model, variant)."""

    def __init__(self, root: Path, *, model: str, variant: str = "",
                 resume: bool = True) -> None:
        self.root = Path(root)
        self.model = model
        self.variant = variant
        self.resume = resume
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, item_id: str) -> Path:
        return self.root / f"{item_id}.json"

    def _stamp(self, fingerprint: str) -> dict[str, str]:
        return {"model": self.model, "variant": self.variant,
                "fingerprint": fingerprint}

    def _read(self, item_id: str) -> dict[str, Any] | None:
        p = self.path(item_id)
        if not p.is_file():
            return None
        try:
            rec = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        return rec if isinstance(rec, dict) else None

    def get(self, item_id: str, fingerprint: str = "") -> dict[str, Any] | None:
        """The cached GRADED record for this item under this key, else None.

        A record from another model/variant, for another item (fingerprint
        mismatch), without a stamp (pre-store runs), or recording an infra
        error is not a cache hit — the item runs again and overwrites it.
        """
        if not self.resume:
            return None
        rec = self._read(item_id)
        if rec is None or rec.get("_store") != self._stamp(fingerprint):
            return None
        if rec.get("status") != STATUS_OK:
            return None
        return rec

    def put(self, item_id: str, record: dict[str, Any],
            fingerprint: str = "") -> dict[str, Any]:
        rec = {**record, "status": STATUS_OK, "_store": self._stamp(fingerprint)}
        self._write(item_id, rec)
        return rec

    def put_infra_error(self, item_id: str, error: str, fingerprint: str = "",
                        **extra: Any) -> dict[str, Any]:
        """Record that the item could not be evaluated (backend down, load
        failure). Written for visibility; never reused, never graded."""
        rec = {**extra, "error": error, "status": STATUS_INFRA_ERROR,
               "_store": self._stamp(fingerprint)}
        self._write(item_id, rec)
        return rec

    def _write(self, item_id: str, rec: dict[str, Any]) -> None:
        p = self.path(item_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec, indent=2, default=json_default))

    def collect(self, items: Iterable[str | tuple[str, str]]) -> Collected:
        """Read back exactly the selected items — `item_id` or
        `(item_id, fingerprint)` — never a glob of the directory."""
        out = Collected()
        for it in items:
            item_id, fp = (it, "") if isinstance(it, str) else it
            rec = self._read(item_id)
            if rec is None or rec.get("_store") != self._stamp(fp):
                out.missing += 1
            elif rec.get("status") == STATUS_INFRA_ERROR:
                out.infra_errors += 1
            else:
                out.records.append(rec)
        return out
