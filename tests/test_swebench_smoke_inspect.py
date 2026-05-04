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
    compare_predictions_to_gold,
    compare_to_gold,
    inspect_instance,
    inspect_predictions,
    load_gold_patches,
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


# -- Gold-proximity comparison ---------------------------------------------
#
# Fixtures mirror the actual n=10 A/B regression patterns:
# - exact gold match (matplotlib-13989 baseline)
# - right file/function but bloated rewrite (xarray-2905 baseline overshoot)
# - right file, wrong function (sklearn-10297 wrong class)
# - wrong file entirely (localization fail)
# - empty patch defers to mechanical empty_patch tier
#
# Gold patches kept tight to surface threshold edge cases.


_GOLD_DJANGO = """\
diff --git a/django/core/validators.py b/django/core/validators.py
--- a/django/core/validators.py
+++ b/django/core/validators.py
@@ -94,7 +94,7 @@ class URLValidator(RegexValidator):
     regex = _lazy_re_compile(
         r'^(?:[a-z0-9\\.\\-\\+]*)://'
-        r'(?:\\S+(?::\\S*)?@)?'
+        r'(?:[^\\s:@/]+(?::[^\\s:@/]*)?@)?'
         r'(?:' + ipv4_re + '|' + ipv6_re + '|' + host_re + ')'
"""

_MODEL_DJANGO_CLOSE = """\
diff --git a/django/core/validators.py b/django/core/validators.py
--- a/django/core/validators.py
+++ b/django/core/validators.py
@@ -94,7 +94,7 @@ class URLValidator(RegexValidator):
     regex = _lazy_re_compile(
         r'^(?:[a-z0-9\\.\\-\\+]*)://'
-        r'(?:\\S+(?::\\S*)?@)?'
+        r'(?:[^@:/\\s]+(?::[^@:/\\s]*)?@)?'
         r'(?:' + ipv4_re + '|' + ipv6_re + '|' + host_re + ')'
"""


_GOLD_FLASK = """\
diff --git a/src/flask/blueprints.py b/src/flask/blueprints.py
--- a/src/flask/blueprints.py
+++ b/src/flask/blueprints.py
@@ -190,6 +190,9 @@ def __init__(
             root_path=root_path,
         )

+        if not name:
+            raise ValueError("'name' may not be empty.")
+
         if "." in name:
             raise ValueError("'name' may not contain a dot '.' character.")

"""


_MODEL_FLASK_BLOATED = """\
diff --git a/src/flask/blueprints.py b/src/flask/blueprints.py
--- a/src/flask/blueprints.py
+++ b/src/flask/blueprints.py
@@ -190,6 +190,30 @@ def __init__(
             root_path=root_path,
         )

+        # Validate name extensively
+        if not name:
+            raise ValueError("'name' may not be empty.")
+        if not isinstance(name, str):
+            raise TypeError("'name' must be a string.")
+        name = name.strip()
+        if not name:
+            raise ValueError("'name' may not be whitespace only.")
+        if len(name) > 100:
+            raise ValueError("'name' is too long.")
+        if name.startswith("_"):
+            raise ValueError("'name' may not start with underscore.")
+        if name.lower() != name:
+            raise ValueError("'name' must be lowercase.")
+        for char in name:
+            if not (char.isalnum() or char in "-_"):
+                raise ValueError(f"'name' contains invalid character: {char}")
+        for reserved in ["app", "admin", "api"]:
+            if name == reserved:
+                raise ValueError(f"'name' cannot be reserved word {reserved}")
+
         if "." in name:
             raise ValueError("'name' may not contain a dot '.' character.")

"""


_MODEL_WRONG_FUNCTION = """\
diff --git a/src/flask/blueprints.py b/src/flask/blueprints.py
--- a/src/flask/blueprints.py
+++ b/src/flask/blueprints.py
@@ -300,6 +300,8 @@ def register(self, app, options):
     def some_other_method(self):
+        if not self.name:
+            raise ValueError("'name' may not be empty.")
         pass
"""


_MODEL_WRONG_FILE = """\
diff --git a/src/flask/app.py b/src/flask/app.py
--- a/src/flask/app.py
+++ b/src/flask/app.py
@@ -100,6 +100,7 @@ class Flask:
     def __init__(self, name):
+        if not name: raise ValueError("name required")
         self.name = name
"""


def test_gold_compare_exact_match_is_strong():
    """Exact gold match → tier 'strong' with all five signals green."""
    gv = compare_to_gold("flask-1", _GOLD_FLASK, _GOLD_FLASK)
    assert gv.tier == "strong"
    for k in ("files_match", "location_match", "hunk_count_ok",
              "size_ok", "token_overlap_ok"):
        assert gv.signals[k], f"{k} should be True for an exact match"


def test_gold_compare_close_rewrite_is_strong_or_plausible():
    """django case: model rewrites the regex char class with the same
    semantics but different syntax. Same file/location/hunk/size; token
    overlap stays high. Tier should be 'strong' or 'plausible' — never
    'wrong_*' for this proximity."""
    gv = compare_to_gold("django-1", _MODEL_DJANGO_CLOSE, _GOLD_DJANGO)
    assert gv.tier in ("strong", "plausible"), gv
    assert gv.signals["files_match"]
    assert gv.signals["location_match"]


def test_gold_compare_bloated_rewrite_falls_to_wrong_shape_or_plausible():
    """flask case but the model adds 24 lines instead of 3. Files/loc
    match, but size_ok must fail (24/3 = 8× gold size). Hunk count
    actually still matches (1/1) and token overlap stays high (gold's
    tokens are a subset of model's). So this lands as 'plausible' —
    files/loc right, one shape signal fails."""
    gv = compare_to_gold("flask-1", _MODEL_FLASK_BLOATED, _GOLD_FLASK)
    assert gv.signals["files_match"]
    assert gv.signals["location_match"]
    assert not gv.signals["size_ok"], "bloated patch should fail size_ok"
    assert gv.tier in ("plausible", "wrong_shape")


def test_gold_compare_wrong_function_lands_in_wrong_location_tier():
    """Right file but hunks at line 300+ vs gold's hunks at line 190.
    Outside the 20-line tolerance — tier must be 'wrong_location'.
    Replaces the older @@-text-based wrong_function check, which was
    brittle (matplotlib-13989 false-flagged because git annotated the
    same hunk-line as `optional.` instead of `def hist(...)`)."""
    gv = compare_to_gold("flask-1", _MODEL_WRONG_FUNCTION, _GOLD_FLASK)
    assert gv.signals["files_match"]
    assert not gv.signals["location_match"]
    assert gv.tier == "wrong_location"


def test_gold_compare_same_line_with_different_annotation_is_strong():
    """Regression test for the matplotlib-13989 false-negative: gold's
    `@@ ... @@ def hist(...)` and the model's `@@ ... @@ optional.`
    were the SAME hunk at line 6686, but the older @@-text-based check
    flagged it as wrong_function. Line-based proximity must recognize
    these as same-location."""
    gold = (
        "diff --git a/lib/foo.py b/lib/foo.py\n"
        "--- a/lib/foo.py\n"
        "+++ b/lib/foo.py\n"
        "@@ -6686,7 +6686,7 @@ def hist(self, x, bins=None, density=None):\n"
        "         density = bool(density)\n"
        "         if density and not stacked:\n"
        "-            hist_kwargs = dict(density=density)\n"
        "+            hist_kwargs['density'] = density\n"
    )
    model = (
        "diff --git a/lib/foo.py b/lib/foo.py\n"
        "--- a/lib/foo.py\n"
        "+++ b/lib/foo.py\n"
        "@@ -6686,7 +6686,7 @@ optional.\n"
        "         density = bool(density)\n"
        "         if density and not stacked:\n"
        "-            hist_kwargs = dict(density=density)\n"
        "+            hist_kwargs['density'] = density\n"
    )
    gv = compare_to_gold("matplotlib-1", model, gold)
    assert gv.signals["location_match"], (
        f"hunk at line 6686 in both must match regardless of @@ text; "
        f"signals={gv.signals}"
    )
    assert gv.tier == "strong"


def test_gold_compare_close_placement_within_tolerance_matches():
    """flask-5014 case: model adds the check at line 193, gold adds at
    line 190 — both inside `__init__` but at different points. With
    20-line tolerance this should match (diff=3)."""
    gold = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -190,3 +190,5 @@\n"
        "+    if not name: raise ValueError('a')\n"
    )
    model = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -193,3 +193,5 @@\n"
        "+    if not name: raise ValueError('b')\n"
    )
    gv = compare_to_gold("flask-1", model, gold)
    assert gv.signals["location_match"]
    # Tier may be strong or plausible depending on token overlap on
    # different message strings — the location signal is what matters.


def test_gold_compare_wrong_file_lands_in_wrong_target_tier():
    """Patch edits a file gold doesn't touch. Tier 'wrong_target'."""
    gv = compare_to_gold("flask-1", _MODEL_WRONG_FILE, _GOLD_FLASK)
    assert not gv.signals["files_match"]
    assert gv.tier == "wrong_target"


def test_gold_compare_empty_patch_defers_to_mechanical_tier():
    """When the patch fails the basic gates (empty), the gold-comparison
    tier should be the mechanical reason ('empty_patch') — no need to
    compute the rich signals on a patch that has nothing in it."""
    gv = compare_to_gold("flask-1", "", _GOLD_FLASK)
    assert gv.tier == "empty_patch"
    assert gv.signals == {}


def test_gold_compare_reproducer_defers_to_mechanical_new_file_tier():
    """A reproducer-script patch fails the new_file_in_diff gate. Tier
    is the mechanical reason; rich signals skipped."""
    gv = compare_to_gold("flask-1", _REPRODUCER_AT_REPO_ROOT, _GOLD_FLASK)
    assert gv.tier == "new_file_in_diff"


def test_gold_compare_predictions_end_to_end(tmp_path):
    """Exercise the predictions.json + gold-source pipeline. Synthesizes
    a 3-row predictions file and a 3-row gold JSONL, verifies tiers
    line up with what the per-instance comparisons produce."""
    import json
    rows = [
        {"instance_id": "django-1", "model_patch": _MODEL_DJANGO_CLOSE,
         "model_name_or_path": "luxe"},
        {"instance_id": "flask-1", "model_patch": _MODEL_WRONG_FILE,
         "model_name_or_path": "luxe"},
        {"instance_id": "flask-2", "model_patch": "",
         "model_name_or_path": "luxe"},
    ]
    pred = tmp_path / "predictions.json"
    pred.write_text(json.dumps(rows))

    gold = tmp_path / "gold.jsonl"
    gold_rows = [
        {"instance_id": "django-1", "patch": _GOLD_DJANGO},
        {"instance_id": "flask-1", "patch": _GOLD_FLASK},
        {"instance_id": "flask-2", "patch": _GOLD_FLASK},
    ]
    gold.write_text("\n".join(json.dumps(r) for r in gold_rows))

    gvs = compare_predictions_to_gold(pred, gold)
    assert len(gvs) == 3
    by_id = {gv.instance_id: gv for gv in gvs}
    assert by_id["django-1"].tier in ("strong", "plausible")
    assert by_id["flask-1"].tier == "wrong_target"
    assert by_id["flask-2"].tier == "empty_patch"


def test_gold_compare_partial_coverage_demotes_to_plausible():
    """requests-2931 / astropy-13453 pattern: gold has 2 hunks, model
    touches 1. Each touched hunk looks great in isolation (size_ok,
    token_ok, location_match), but full_coverage=False demotes the
    verdict from strong to plausible. This is the (b2) multi-site
    consistency case — dominant in the n=10 manual review."""
    gold = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -10,3 +10,3 @@\n"
        "-    return data\n"
        "+    return processed\n"
        "@@ -50,3 +50,3 @@\n"
        "-    pass\n"
        "+    cleanup()\n"
    )
    model = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -10,3 +10,3 @@\n"
        "-    return data\n"
        "+    return processed\n"
    )
    gv = compare_to_gold("multi-1", model, gold)
    assert gv.signals["files_match"]
    assert gv.signals["location_match"], "1 of 2 hunks matched location"
    assert not gv.signals["full_coverage"], (
        f"coverage should be 0.5; got {gv.signals.get('coverage')}"
    )
    assert abs(gv.signals["coverage"] - 0.5) < 0.01
    assert gv.tier == "plausible", (
        f"partial coverage must demote from strong to plausible; got {gv.tier}"
    )


def test_load_gold_patches_handles_jsonl_and_json(tmp_path):
    """Loader must handle both list-of-rows JSON and JSONL formats —
    raw HF dumps come as JSONL, but stratified subsets are JSON."""
    import json
    jsonl = tmp_path / "verified.jsonl"
    jsonl.write_text(
        json.dumps({"instance_id": "x-1", "patch": "P1"}) + "\n"
        + json.dumps({"instance_id": "x-2", "patch": "P2"}) + "\n"
    )
    out = load_gold_patches(jsonl)
    assert out == {"x-1": "P1", "x-2": "P2"}

    j = tmp_path / "stratified.json"
    j.write_text(json.dumps([
        {"instance_id": "y-1", "patch": "Q1"},
        {"instance_id": "y-2", "patch": "Q2"},
    ]))
    out = load_gold_patches(j)
    assert out == {"y-1": "Q1", "y-2": "Q2"}
