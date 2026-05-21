"""v1.11 Phase 2 — cross-cycle prior loader (log-only this cycle).

Reads per-instance prior-cycle outcome classification from
`~/.luxe/cohort-history/<instance_id>.json`. The data source is the
snapshot output of `scripts/cohort_shift_3x3.py` (Stage 2 → Stage 3
closed loop).

Per agents.sdd v1.11 invariants:
  - Priors are **log-only** this cycle. The loader returns the parsed
    dict (or None on missing/corrupt input), but loop.py does NOT use
    it to influence intervention intensity. v1.11.1+ evaluates whether
    to graduate priors to selective influence.
  - Null-safe: missing file, missing directory, parse error, schema
    violation all return None. No exception propagates to the loop.

Schema (best-effort; loader is permissive — extra fields are kept,
missing fields produce None where appropriate):

    {
      "instance_id": "<repo>__<bug-id>",
      "label_a": "v1.10.4",     # prior cycle label
      "label_b": "v1.10.5",     # this/comparison cycle label
      "tiers_a": ["strong", "strong", "plausible"],
      "tiers_b": ["strong", "strong", "strong"],
      "verdict": "byte_identical" | "deterministic_gain" |
                 "deterministic_loss" | "modal_gain" | "modal_loss" | "noise",
      "median_rank_a": float, "median_rank_b": float,
      "rank_delta": float,
      ...
    }
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_COHORT_HISTORY_DIR = Path.home() / ".luxe" / "cohort-history"

_REQUIRED_FIELDS = ("instance_id", "verdict")
_VALID_VERDICTS = frozenset({
    "byte_identical",
    "deterministic_gain",
    "deterministic_loss",
    "modal_gain",
    "modal_loss",
    "noise",
})


def load_prior(
    instance_id: str,
    base_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Read and validate a cohort-history prior file. Returns None on
    any failure (missing, corrupt, schema-invalid) — never raises.

    `base_dir` defaults to ~/.luxe/cohort-history/. Override for tests
    or alternate data sources.
    """
    if not instance_id:
        return None
    base = base_dir if base_dir is not None else DEFAULT_COHORT_HISTORY_DIR
    p = base / f"{instance_id}.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    for f in _REQUIRED_FIELDS:
        if f not in data:
            return None
    if data["instance_id"] != instance_id:
        # Defensive: file content claims a different instance than the
        # filename. Reject — the data is internally inconsistent.
        return None
    verdict = data.get("verdict")
    if verdict not in _VALID_VERDICTS:
        return None
    return data


def load_prior_from_env() -> dict[str, Any] | None:
    """Convenience: load prior for the instance named in LUXE_INSTANCE_ID.

    Returns None if LUXE_LOAD_PRIORS is not "1", LUXE_INSTANCE_ID is
    unset, or the prior file is missing/invalid. Used by loop.py.
    """
    if os.environ.get("LUXE_LOAD_PRIORS") != "1":
        return None
    instance_id = os.environ.get("LUXE_INSTANCE_ID")
    if not instance_id:
        return None
    return load_prior(instance_id)
