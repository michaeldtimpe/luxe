"""gitkit — repo-analysis toolkit. Two commands: `gitaudit` (read-only: orientation
+ bugs/security + structural advice) and `gitchange` (apply-ready change plan +
gated `--apply` executor).

Walk `gitkit.sdd` before editing anything here.
"""

from __future__ import annotations

from luxe.gitkit.deep import run_deep_report, should_use_deep
from luxe.gitkit.runner import KINDS, run_git_report

__all__ = ["KINDS", "run_git_report", "run_deep_report", "should_use_deep"]
