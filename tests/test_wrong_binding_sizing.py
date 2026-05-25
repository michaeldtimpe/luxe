"""Tests for the WS2 acted-but-wrong sizing analysis (scripts/analyze_acted_but_wrong.py).

The risky logic is the call-string parser, the normalization, and the whole-conversation
matcher — all pure + offline, so the bulk needs no data. One data-dependent smoke test is
skip-guarded on the gitignored m5 rep dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.analyze_acted_but_wrong import (
    classify_failure,
    norm_eq,
    param_subtype,
    parse_call,
)


# --- parser -----------------------------------------------------------------

def test_parse_call_types_and_order_independent():
    nm, args = parse_call("send_message(receiver_id='USR003', message='hi')")
    assert nm == "send_message"
    assert args == {"receiver_id": "USR003", "message": "hi"}
    # nested literals + numbers + bool; order doesn't matter (dict compare)
    nm2, args2 = parse_call("place_order(amount=50, opts={'k': [1, 2]}, dry=True)")
    assert nm2 == "place_order"
    assert args2 == {"amount": 50, "opts": {"k": [1, 2]}, "dry": True}


def test_parse_call_unparsed_returns_none_not_raise():
    # non-literal value, positional arg, **splat, garbage — every one routes to None.
    assert parse_call("f(x=foo)") is None          # bare name (non-literal)
    assert parse_call("f(1, 2)") is None           # positional
    assert parse_call("f(**kw)") is None           # splat
    assert parse_call("not a call at all") is None
    assert parse_call("f(x=1") is None             # malformed — must NOT raise


# --- normalization ----------------------------------------------------------

def test_norm_eq_match_mismatch_and_uncertain():
    assert norm_eq("USR003", "USR003") is True
    assert norm_eq("500", 500) is True            # str⇄number coercion
    assert norm_eq("def ", "def") is True         # whitespace-only
    assert norm_eq("USR002", "USR003") is False   # clear scalar mismatch
    assert norm_eq(50, 500) is False
    assert norm_eq([1, 2], [2, 1]) is None        # container diff → ambiguous


def test_param_subtype():
    assert param_subtype("receiver_id", "USR003") == "recipient_id"   # by name
    assert param_subtype("foo", "USR003") == "recipient_id"           # by value pattern
    assert param_subtype("budget_limit", 500) == "numeric"
    assert param_subtype("message", "hello") == "string_format"


# --- whole-conversation matcher ---------------------------------------------

def _m(turns):  # model decoded_turns: [turn][step][callstr]
    return turns


def test_classify_value_mismatch_recipient_and_turn_shift():
    """miss_func_33 shape: GT expects send_message(USR003) at t4; model sent USR002 at t3.
    Cross-turn matching must surface the recipient mismatch (not omission+extra) + turn_shift."""
    gt = [["ls()"], [], [], [], ["send_message(receiver_id='USR003', message='go')"]]
    model = [[["ls()"]], [], [], [["send_message(receiver_id='USR002', message='go')"]], []]
    res = classify_failure(model, gt)
    assert res["bucket"] == "gt_value_mismatch"
    assert res["turn_shifted"] is True
    mm = res["mismatches"]
    assert len(mm) == 1 and mm[0]["param"] == "receiver_id" and mm[0]["subtype"] == "recipient_id"
    assert mm[0]["model"] == "USR002" and mm[0]["gt"] == "USR003"


def test_classify_omission_not_misaligned_for_same_name_repeats():
    """GT has two send_message calls; model emitted only the Bob one. Exact-match must claim
    Bob, leaving Alice as a clean omission — NOT a spurious mismatch against Bob."""
    gt = [["send_message(receiver_id='Alice', message='hi')",
           "send_message(receiver_id='Bob', message='yo')"]]
    model = [[["send_message(receiver_id='Bob', message='yo')"]]]
    res = classify_failure(model, gt)
    assert res["mismatches"] == []
    assert res["omissions"] == ["send_message"]
    assert res["bucket"] == "omission"


def test_classify_single_arg_forced_match_is_value_mismatch():
    """A sole same-name candidate is force-matched even with zero arg overlap, so a wrong
    single-arg value (add_to_watchlist stock) reads as gt_value_mismatch, not omission."""
    gt = [["add_to_watchlist(stock='ZETA')"]]
    model = [[["add_to_watchlist(stock='ZTA')"]]]
    res = classify_failure(model, gt)
    assert res["bucket"] == "gt_value_mismatch"
    assert res["mismatches"][0]["param"] == "stock"


def test_classify_extra_action_and_path_divergence():
    # extra: model calls a fn with no GT counterpart (GT call matched exactly)
    gt = [["cd(folder='x')"]]
    model = [[["cd(folder='x')", "ls(a=True)"]]]
    assert classify_failure(model, gt)["bucket"] == "extra_action"
    # path_divergence: every GT call matched with equal args (model added args are ignored)
    gt2 = [["cd(folder='x')"]]
    model2 = [[["cd(folder='x', force=True)"]]]
    assert classify_failure(model2, gt2)["bucket"] == "path_divergence"


def test_classify_normalization_uncertain_only():
    gt = [["update(items=[1, 2, 3])"]]
    model = [[["update(items=[3, 2, 1])"]]]   # container order diff → uncertain, not mismatch
    res = classify_failure(model, gt)
    assert res["bucket"] == "normalization_uncertain"
    assert res["mismatches"] == []


def test_classify_unparsed_gt_does_not_crash():
    gt = [["this is not a call"]]
    model = [[["cd(folder='x')"]]]
    res = classify_failure(model, gt)         # must not raise
    assert res["bucket"] == "unparsed"


# --- data-dependent smoke (skip if the gitignored rep dir is absent) ---------

_REP = (Path(__file__).resolve().parent.parent
        / "acceptance/bfcl/multi_turn_miss_func/m5_rep_1/multi_turn_miss_func")


def test_substitute_gt_values_overwrites_and_adds():
    """The deep-dive substitution sets a flagged arg to its GT value (and ADDS an
    omitted_arg), re-serializing only the touched call — so a re-grade flip is
    attributable to the binding."""
    from scripts.verify_wrong_binding_attribution import substitute_gt_values
    dt = [[["send_message(message='go', receiver_id='USR002')", "ls()"]]]
    mm = [{"fn": "send_message", "param": "receiver_id", "model": "USR002", "gt": "USR003",
           "subtype": "recipient_id"}]
    out = substitute_gt_values(dt, mm)
    assert out[0][0][0] == "send_message(message='go', receiver_id='USR003')"
    assert out[0][0][1] == "ls()"          # untouched call unchanged
    assert dt[0][0][0].endswith("USR002')")  # input not mutated (deep copy)
    # omitted_arg → the GT arg is added
    dt2 = [[["create_ticket(title='x')"]]]
    mm2 = [{"fn": "create_ticket", "param": "priority", "model": None, "gt": 4,
            "subtype": "numeric", "kind": "omitted_arg"}]
    assert "priority=4" in substitute_gt_values(dt2, mm2)[0][0][0]


@pytest.mark.skipif(not _REP.is_dir(), reason="miss_func m5_rep_1 artifacts not on disk")
def test_summary_smoke_emits_manifest(tmp_path):
    from scripts.analyze_acted_but_wrong import _run_summary
    out = tmp_path / "sizing.json"
    rc = _run_summary([str(_REP)], out)
    assert rc == 0 and out.is_file()
    import json
    man = json.loads(out.read_text())
    assert "rollup" in man and man["categories"]
    cat = next(iter(man["categories"].values()))
    assert cat["n_acted_but_wrong"] >= 1  # there ARE acted-but-wrong failures in this rep
