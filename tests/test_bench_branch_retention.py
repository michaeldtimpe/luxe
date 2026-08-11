"""maintain_suite branch retention — the fix for the bench branch leak.

Every bench run commits the agent's work to a new branch and nothing removed
it, in the workspace clone and the fixture-cache origin alike.
`luxe.pr.plan_branch_name` walks `<prefix>/<task>/<slug>`, `-2` … `-99` and
then raises, so a fixture died permanently at ~99 runs on the same goal.

That is worse than an outage. A/B arms run sequentially, so the arm running
SECOND absorbs the exhaustion, its fixtures score 0 on a PRError, and the
scoreboard renders it as an ordinary failure — on 2026-08-10 it manufactured a
clean-looking 116-vs-108 "regression" that was pure arm ordering
(`acceptance/pwir_rerun_2026_08_10/BRANCH-LEAK.md`).

Deleting a branch straight after grading is NOT available: `regrade_local.py`
checks the pushed branch out to re-grade, so recent ones must survive. Hence a
retention window.
"""

from __future__ import annotations

import subprocess

import pytest

from benchmarks.maintain_suite.run import _prune_old_branches


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "f.txt").write_text("x")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _make(repo, names):
    """Create branches in order, so committerdate ranks them last-is-newest."""
    for n in names:
        _git(repo, "branch", n)


def _branches(repo, prefix=""):
    """All branch names, optionally filtered by prefix.

    Filtering in Python on purpose: `refs/heads/luxe/*` as a for-each-ref
    pattern matches NOTHING (the glob does not descend), while
    `refs/heads/luxe/` does. Getting that wrong in the helper made every test
    here fail against correct production code.
    """
    out = _git(repo, "for-each-ref", "--format=%(refname:short)",
               "refs/heads/").stdout.split()
    return sorted(b for b in out if b.startswith(prefix))


class TestRetention:
    def test_keeps_the_newest_n_and_drops_the_rest(self, repo):
        _make(repo, [f"luxe/document/slug-{i}" for i in range(2, 12)])  # 10
        deleted = _prune_old_branches(repo, "luxe", keep=4)
        assert len(deleted) == 6
        assert len(_branches(repo, "luxe/")) == 4

    def test_nothing_happens_when_under_the_window(self, repo):
        _make(repo, [f"luxe/document/slug-{i}" for i in range(2, 5)])
        assert _prune_old_branches(repo, "luxe", keep=25) == []
        assert len(_branches(repo, "luxe/")) == 3

    def test_grouping_is_per_slug_not_per_repo(self, repo):
        """The allocator's 99-name budget is per slug. 300 branches spread
        over 20 slugs is fine; 99 on ONE slug is fatal — so a repo-wide cap
        would prune the wrong things and still let a hot slug die."""
        _make(repo, [f"luxe/document/alpha-{i}" for i in range(2, 10)])   # 8
        _make(repo, [f"luxe/implement/beta-{i}" for i in range(2, 10)])   # 8
        _prune_old_branches(repo, "luxe", keep=3)
        left = _branches(repo, "luxe/")
        assert sum(1 for b in left if "alpha" in b) == 3
        assert sum(1 for b in left if "beta" in b) == 3

    def test_the_unsuffixed_base_shares_its_slug_bucket(self, repo):
        """`foo` and `foo-7` are the same slug to the allocator, so they must
        be the same bucket here — otherwise the base name survives forever and
        the count is off by one."""
        _make(repo, ["luxe/document/foo"] +
                    [f"luxe/document/foo-{i}" for i in range(2, 8)])      # 7
        _prune_old_branches(repo, "luxe", keep=2)
        assert len(_branches(repo, "luxe/")) == 2


class TestSafety:
    def test_other_prefixes_are_untouched(self, repo):
        """deluxe/* and whetstone/* come from sibling tools. They accumulate
        too, but they are not ours to delete and they do not exhaust luxe's
        allocator."""
        _make(repo, [f"luxe/document/slug-{i}" for i in range(2, 10)])
        _make(repo, [f"deluxe/document/slug-{i}" for i in range(2, 10)])
        _make(repo, [f"whetstone/document/slug-{i}" for i in range(2, 10)])
        _prune_old_branches(repo, "luxe", keep=2)
        assert len(_branches(repo, "luxe/")) == 2
        assert len(_branches(repo, "deluxe/")) == 8
        assert len(_branches(repo, "whetstone/")) == 8

    def test_main_is_never_touched(self, repo):
        _make(repo, [f"luxe/document/slug-{i}" for i in range(2, 10)])
        _prune_old_branches(repo, "luxe", keep=1)
        assert "main" in _branches(repo)

    def test_keep_zero_is_a_no_op_not_a_wipe(self, repo):
        """A misconfigured retention of 0 must not delete everything — the
        guard is deliberately fail-safe rather than fail-empty."""
        _make(repo, [f"luxe/document/slug-{i}" for i in range(2, 6)])
        assert _prune_old_branches(repo, "luxe", keep=0) == []
        assert len(_branches(repo, "luxe/")) == 4

    def test_a_non_repo_path_does_not_raise(self, tmp_path):
        assert _prune_old_branches(tmp_path / "nope", "luxe", keep=5) == []

    def test_checked_out_branch_survives(self, repo):
        """git refuses to delete the current branch; the helper must tolerate
        that partial failure rather than abort the run."""
        _make(repo, [f"luxe/document/slug-{i}" for i in range(2, 10)])
        _git(repo, "checkout", "-q", "luxe/document/slug-2")   # an OLD one
        _prune_old_branches(repo, "luxe", keep=2)
        assert "luxe/document/slug-2" in _branches(repo, "luxe/")
