"""Tests for `benchmarks.swebench.smoke_inspect` — the mechanical
PASS/FAIL inspector that gates predictions.json against the four
prompt-regression criteria (no new files, no test-path edits, no empty
patches, no comment/whitespace-only patches).

The fixtures are synthesized to mirror the actual 2026-05-04 smoke
failure modes (model created `repo_root/test_sep.py` and `astropy/
timeseries/test_bug.py`) plus a compliant patch shape.
"""

from __future__ import annotations

from benchmarks.swebench.smoke_inspect import (
    inspect_instance,
    inspect_predictions,
)


_REPRODUCER_AT_REPO_ROOT = """\
diff --git a/repo_root/test_sep.py b/repo_root/test_sep.py
new file mode 100644
index 0000000000..804c22ec04
--- /dev/null
+++ b/repo_root/test_sep.py
@@ -0,0 +1,3 @@
+from astropy.modeling import models as m
+from astropy.modeling.separable import separability_matrix
+print(separability_matrix(m.Linear1D(10) & m.Linear1D(5)))
"""

_REPRODUCER_AT_TEST_PATH = """\
diff --git a/astropy/timeseries/test_bug.py b/astropy/timeseries/test_bug.py
new file mode 100644
index 0000000000..2b5134b7dc
--- /dev/null
+++ b/astropy/timeseries/test_bug.py
@@ -0,0 +1,2 @@
+import numpy as np
+from astropy.time import Time
"""

_COMPLIANT_SOURCE_EDIT = """\
diff --git a/astropy/modeling/separable.py b/astropy/modeling/separable.py
--- a/astropy/modeling/separable.py
+++ b/astropy/modeling/separable.py
@@ -10,7 +10,7 @@ def separability_matrix(transform):
     ...
-    return _coord_matrix(transform, 'left', n_inputs)
+    return _cstack(transform.left, transform.right)
"""

_TEST_FILE_EDIT = """\
diff --git a/astropy/timeseries/tests/test_sampled.py b/astropy/timeseries/tests/test_sampled.py
--- a/astropy/timeseries/tests/test_sampled.py
+++ b/astropy/timeseries/tests/test_sampled.py
@@ -100,3 +100,3 @@ def test_remove_required():
-    with pytest.raises(ValueError):
+    with pytest.warns(UserWarning):
         ts.remove_column('flux')
"""

_COMMENT_ONLY_EDIT = """\
diff --git a/astropy/modeling/separable.py b/astropy/modeling/separable.py
--- a/astropy/modeling/separable.py
+++ b/astropy/modeling/separable.py
@@ -10,3 +10,4 @@ def separability_matrix(transform):
+    # TODO: figure out the right matrix here
     return _coord_matrix(transform, 'left', n_inputs)
"""


def test_compliant_source_edit_passes():
    v = inspect_instance("astropy__astropy-12907", _COMPLIANT_SOURCE_EDIT)
    assert v.passed, v.reasons
    assert v.reasons == []


def test_empty_patch_fails_with_empty_patch_reason():
    v = inspect_instance("astropy__astropy-13236", "")
    assert not v.passed
    assert "empty_patch" in v.reasons


def test_reproducer_at_repo_root_fails_on_new_file():
    v = inspect_instance("astropy__astropy-12907", _REPRODUCER_AT_REPO_ROOT)
    assert not v.passed
    assert "new_file_in_diff" in v.reasons


def test_reproducer_at_test_path_fails_on_both_new_file_and_test_path():
    """The 13033 smoke patch is the meanest case — it's a new file AND
    its path matches `test_*.py`. Both reasons should fire."""
    v = inspect_instance("astropy__astropy-13033", _REPRODUCER_AT_TEST_PATH)
    assert not v.passed
    assert "new_file_in_diff" in v.reasons
    assert any(r.startswith("touches_test_paths=") for r in v.reasons)


def test_test_file_modification_fails_even_without_new_file():
    """The model 'fixes' an existing test file rather than the source.
    No `new file mode`, but the path matches a tests/ subdir — fail."""
    v = inspect_instance("astropy__astropy-99999", _TEST_FILE_EDIT)
    assert not v.passed
    assert any(r.startswith("touches_test_paths=") for r in v.reasons)
    assert "new_file_in_diff" not in v.reasons


def test_comment_only_edit_fails_on_no_substantive_change():
    """A diff that only adds comments isn't a real fix. Must fail."""
    v = inspect_instance("astropy__astropy-99998", _COMMENT_ONLY_EDIT)
    assert not v.passed
    assert "no_substantive_change" in v.reasons


def test_inspect_predictions_reads_full_file(tmp_path):
    """End-to-end: write a 3-row predictions.json (compliant +
    reproducer-root + reproducer-test-path) and verify the inspector
    classifies all three correctly."""
    import json
    rows = [
        {"instance_id": "x__y-1", "model_patch": _COMPLIANT_SOURCE_EDIT,
         "model_name_or_path": "luxe"},
        {"instance_id": "x__y-2", "model_patch": _REPRODUCER_AT_REPO_ROOT,
         "model_name_or_path": "luxe"},
        {"instance_id": "x__y-3", "model_patch": _REPRODUCER_AT_TEST_PATH,
         "model_name_or_path": "luxe"},
    ]
    path = tmp_path / "predictions.json"
    path.write_text(json.dumps(rows))

    verdicts = inspect_predictions(path)
    assert len(verdicts) == 3
    assert verdicts[0].passed
    assert not verdicts[1].passed
    assert not verdicts[2].passed
