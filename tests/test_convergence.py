"""v1.10 — unit tests for the convergence-score primitive.

Synthetic trajectories representing diffuse-recon vs converged
behavior. The score is the v1.10 replacement for v1.9's binary
same_file_read_twice gate-skip; future intervention gating will
threshold on this score.
"""

from __future__ import annotations

import pytest

from luxe.agents.convergence import (
    compute_convergence_score,
    edit_preview_behavior,
    extract_path,
    file_entropy_last_K,
    localized_grep_density,
    repeated_same_path_access,
)


# --- Building blocks ------------------------------------------------------


def _hist(*entries: tuple[str, str | None]) -> list[dict]:
    """Make a history list from (name, path) tuples; auto-assigns steps."""
    return [{"step": i, "name": n, "path": p}
            for i, (n, p) in enumerate(entries)]


# --- repeated_same_path_access ---------------------------------------------


def test_reread_rate_empty_history():
    assert repeated_same_path_access([]) == 0.0


def test_reread_rate_all_unique():
    h = _hist(("read_file", "a.py"), ("read_file", "b.py"), ("read_file", "c.py"))
    assert repeated_same_path_access(h) == 0.0


def test_reread_rate_all_same():
    h = _hist(("read_file", "a.py"), ("read_file", "a.py"), ("read_file", "a.py"))
    # 3 reads, 1 unique → reread ratio = 1 - 1/3 = 0.666...
    assert repeated_same_path_access(h) == pytest.approx(2 / 3)


def test_reread_rate_mixed():
    h = _hist(("read_file", "a.py"), ("read_file", "b.py"),
              ("read_file", "a.py"), ("read_file", "c.py"))
    # 4 reads, 3 unique → 1 - 3/4 = 0.25
    assert repeated_same_path_access(h) == pytest.approx(0.25)


def test_reread_rate_ignores_non_reads():
    h = _hist(("read_file", "a.py"), ("grep", "a.py"),
              ("write_file", "a.py"), ("read_file", "a.py"))
    # Only 2 reads counted; both on same path → ratio = 1 - 1/2 = 0.5
    assert repeated_same_path_access(h) == pytest.approx(0.5)


# --- edit_preview_behavior -------------------------------------------------


def test_preview_empty():
    assert edit_preview_behavior([]) == 0.0


def test_preview_no_writes():
    h = _hist(("read_file", "a.py"), ("grep", "a.py"))
    assert edit_preview_behavior(h) == 0.0


def test_preview_write_without_preview():
    h = _hist(("read_file", "a.py"), ("write_file", "a.py"))
    # Preceded by read_file, not a preview tool → 0
    assert edit_preview_behavior(h) == 0.0


def test_preview_grep_then_write():
    h = _hist(("grep", "a.py"), ("write_file", "a.py"))
    assert edit_preview_behavior(h) == 1.0


def test_preview_git_diff_then_edit():
    h = _hist(("git_diff", None), ("edit_file", "a.py"))
    assert edit_preview_behavior(h) == 1.0


def test_preview_first_step_write_no_score():
    h = _hist(("write_file", "a.py"))
    assert edit_preview_behavior(h) == 0.0


# --- localized_grep_density -----------------------------------------------


def test_localized_empty():
    assert localized_grep_density([]) == 0.0


def test_localized_no_greps():
    h = _hist(("read_file", "a/x.py"))
    assert localized_grep_density(h) == 0.0


def test_localized_all_in_read_dir():
    h = _hist(("read_file", "a/x.py"),
              ("grep", "a/y.py"),
              ("grep", "a/z.py"))
    # Both greps in dir "a" which has a read → 2/2 = 1.0
    assert localized_grep_density(h) == 1.0


def test_localized_none_in_read_dir():
    h = _hist(("read_file", "src/foo.py"),
              ("grep", "tests/bar.py"),
              ("grep", "docs/baz.md"))
    assert localized_grep_density(h) == 0.0


def test_localized_partial():
    h = _hist(("read_file", "src/foo.py"),
              ("grep", "src/bar.py"),
              ("grep", "tests/baz.py"))
    # 1/2 greps localized → 0.5
    assert localized_grep_density(h) == pytest.approx(0.5)


# --- file_entropy_last_K --------------------------------------------------


def test_entropy_empty():
    assert file_entropy_last_K([]) == pytest.approx(1.0)
    # Note: empty paths → entropy denominator 0 → component returns 0
    # → 1 - 0 = 1.0. This is the "no information" case; convergence
    # *score* (top-level) handles empty history specially.


def test_entropy_all_same():
    h = _hist(*[("read_file", "x.py")] * 6)
    # All same path → entropy=0 → 1 - 0 = 1.0 (max convergence)
    assert file_entropy_last_K(h) == pytest.approx(1.0)


def test_entropy_all_unique():
    h = _hist(*[("read_file", f"f{i}.py") for i in range(8)])
    # Maximum diffuseness → normalized entropy = 1 → 1 - 1 = 0.0
    assert file_entropy_last_K(h) == pytest.approx(0.0, abs=1e-9)


def test_entropy_respects_window():
    # First 8 unique paths, then last 2 same — window K=2 should see
    # only the converged tail.
    h = _hist(*[("read_file", f"f{i}.py") for i in range(8)])
    h += _hist(("read_file", "tail.py"), ("read_file", "tail.py"))
    assert file_entropy_last_K(h, k=2) == pytest.approx(1.0)


# --- compute_convergence_score (composite) --------------------------------


def test_score_empty_history():
    assert compute_convergence_score([]) == 0.0


def test_score_pure_diffuse_recon():
    # 8 unique read paths, no greps, no writes → essentially 0 across
    # all four signals.
    h = _hist(*[("read_file", f"f{i}.py") for i in range(8)])
    assert compute_convergence_score(h) < 0.1


def test_score_pure_converged():
    # All signals active: same path reread, grep then edit, localized
    # grep, low entropy.
    h = _hist(
        ("read_file", "src/foo.py"),
        ("read_file", "src/foo.py"),  # reread
        ("grep", "src/foo.py"),        # localized in src/
        ("edit_file", "src/foo.py"),   # grep → edit (preview)
    )
    s = compute_convergence_score(h)
    assert s >= 0.5, f"expected ≥0.5 on converged trajectory; got {s:.3f}"


def test_score_returns_in_unit_interval():
    # Random-ish history → score must clamp to [0, 1].
    h = _hist(
        ("list_dir", None),
        ("read_file", "a.py"),
        ("grep", "tests/b.py"),
        ("read_file", "a.py"),
        ("edit_file", "a.py"),
    )
    s = compute_convergence_score(h)
    assert 0.0 <= s <= 1.0


def test_score_diffuse_vs_converged_ordering():
    """The score must rank converged > diffuse on these archetypes
    (the property that the v1.10 gating logic depends on)."""
    diffuse = _hist(
        ("read_file", "a.py"), ("read_file", "b.py"),
        ("read_file", "c.py"), ("read_file", "d.py"),
        ("grep", "x.py"), ("grep", "y.py"),
    )
    converged = _hist(
        ("grep", "src/foo.py"),
        ("read_file", "src/foo.py"),
        ("read_file", "src/foo.py"),
        ("grep", "src/foo.py"),
        ("edit_file", "src/foo.py"),
    )
    s_diffuse = compute_convergence_score(diffuse)
    s_converged = compute_convergence_score(converged)
    assert s_converged > s_diffuse + 0.2, (
        f"score order violated: diffuse={s_diffuse:.3f} converged={s_converged:.3f}"
    )


# --- extract_path helper --------------------------------------------------


def test_extract_path_common_keys():
    assert extract_path("read_file", {"path": "a.py"}) == "a.py"
    assert extract_path("read_file", {"file_path": "a.py"}) == "a.py"
    assert extract_path("read_file", {"filename": "a.py"}) == "a.py"


def test_extract_path_no_path_arg():
    assert extract_path("bash", {"cmd": "ls"}) is None
    assert extract_path("read_file", {}) is None
    assert extract_path("read_file", {"path": ""}) is None  # empty rejected


def test_extract_path_non_dict_args():
    # Tolerant: non-dict args (e.g. a parser glitch) should return None
    # rather than raise, so the loop callsite is robust.
    assert extract_path("read_file", None) is None  # type: ignore[arg-type]
    assert extract_path("read_file", "not a dict") is None  # type: ignore[arg-type]
