"""Tests for gitkit DEEP MODE — the staged map-reduce engine (gitkit/deep.py).

Covers the deterministic, non-model pieces (chunker, footprint gate, JSON
parsing, digest merge/compaction, estimate) plus the stage orchestration with a
STUBBED run_single (count passes, map reuse, cancel-saves-partial). The model
passes themselves are exercised end-to-end manually against oMLX, not here.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from luxe.gitkit import deep


def _QuietConsole():
    return Console(file=io.StringIO(), force_terminal=False, width=120)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


# --- fakes ------------------------------------------------------------------

class _FakeSummary:
    def __init__(self, total_loc: int, recent=None):
        self.total_loc = total_loc
        self.recent_files = recent or []

    def render(self) -> str:
        return f"## Repo map ({self.total_loc} LOC)"


class _FakeRole:
    def __init__(self, num_ctx=8192, num_ctx_max=0):
        self.num_ctx = num_ctx
        self.num_ctx_max = num_ctx_max


class _FakeResult:
    def __init__(self, text: str):
        self.final_text = text
        self.steps = 1
        self.tool_calls_total = 0
        self.wall_s = 0.1
        self.completion_tokens = 10


def _frec(rel: str, *, loc=10, tokens=100, priority=2, top_dir=None):
    return deep.FileRec(
        rel=rel, language="python", loc=loc, bytes=tokens * 4, tokens=tokens,
        top_dir=top_dir or (rel.split("/", 1)[0] if "/" in rel else "."),
        priority=priority)


# --- should_use_deep (footprint gate + override) ----------------------------

def test_should_use_deep_small_repo_is_single_pass():
    assert deep.should_use_deep(_FakeSummary(100), _FakeRole(num_ctx=8192)) is False


def test_should_use_deep_large_repo_triggers():
    # total_loc * 10 tokens vs 0.55 * num_ctx
    assert deep.should_use_deep(_FakeSummary(2000), _FakeRole(num_ctx=8192)) is True


def test_should_use_deep_override_forces_both_directions():
    big, small = _FakeSummary(9999), _FakeSummary(1)
    role = _FakeRole(num_ctx=8192)
    assert deep.should_use_deep(big, role, override=False) is False   # --no-deep
    assert deep.should_use_deep(small, role, override=True) is True    # --deep


def test_should_use_deep_gate_keys_on_base_num_ctx_not_max():
    # The gate asks "does the repo fit ONE single-pass window?" — single-pass runs
    # at base num_ctx, so num_ctx_max must NOT relax the trigger.
    s = _FakeSummary(1000)   # 10_000 tokens
    assert deep.should_use_deep(s, _FakeRole(num_ctx=8192, num_ctx_max=131072)) is True
    assert deep.should_use_deep(s, _FakeRole(num_ctx=65536)) is False  # fits one pass


def test_deep_window_defaults_to_base_ctx():
    # chunk passes run at the BASE num_ctx by default (small focused chunks the
    # model can conclude), NOT the expanded num_ctx_max.
    assert deep.deep_window(_FakeRole(num_ctx=8192, num_ctx_max=0)) == 8192
    assert deep.deep_window(_FakeRole(num_ctx=32768, num_ctx_max=262144)) == 32768
    assert deep.deep_window(_FakeRole(num_ctx=32768, num_ctx_max=65536)) == 32768


# --- chunker (deterministic / budget / ordering / degrade) ------------------

def test_build_chunks_respects_token_budget():
    files = [_frec(f"m{i}.py", tokens=300) for i in range(10)]
    chunks = deep.build_chunks(files, content_budget=1000)
    assert len(chunks) > 1
    for c in chunks:
        # each chunk's content stays within budget (single-file chunks excepted)
        assert c.est_tokens <= 1000 or len(c.files) == 1


def test_build_chunks_orders_priority_first():
    files = [_frec("z_normal.py", priority=2), _frec("auth.py", priority=0),
             _frec("recent.py", priority=1)]
    chunks = deep.build_chunks(files, content_budget=100_000)
    # all fit one chunk; order inside reflects priority bucket
    assert chunks[0].files[0] == "auth.py"
    assert chunks[0].files.index("auth.py") < chunks[0].files.index("recent.py")
    assert chunks[0].files.index("recent.py") < chunks[0].files.index("z_normal.py")


def test_build_chunks_is_deterministic():
    files = [_frec(f"d{i % 3}/m{i}.py", tokens=200) for i in range(12)]
    a = deep.build_chunks(files, content_budget=500)
    b = deep.build_chunks(files, content_budget=500)
    assert [c.to_dict() for c in a] == [c.to_dict() for c in b]


def test_build_chunks_tiny_input_is_one_chunk():
    chunks = deep.build_chunks([_frec("a.py", tokens=5)], content_budget=8192)
    assert len(chunks) == 1
    assert chunks[0].files == ["a.py"]


def test_build_chunks_empty_input_yields_one_empty_chunk():
    chunks = deep.build_chunks([], content_budget=8192)
    assert len(chunks) == 1 and chunks[0].files == []


def test_chunk_roundtrip_dict():
    c = deep.build_chunks([_frec("a.py")], content_budget=8192)[0]
    assert deep.Chunk.from_dict(c.to_dict()).to_dict() == c.to_dict()


# --- chunk-output parsing ---------------------------------------------------

def test_parse_chunk_notes_fenced_json():
    text = "noise\n```json\n{\"findings\": [{\"title\": \"x\"}]}\n```\ntrailing"
    parsed = deep.parse_chunk_notes(text)
    assert parsed["findings"][0]["title"] == "x"


def test_parse_chunk_notes_bare_json():
    parsed = deep.parse_chunk_notes('prefix {"findings": []} suffix')
    assert parsed == {"findings": []}


def test_parse_chunk_notes_garbage_returns_none():
    assert deep.parse_chunk_notes("no json here") is None
    assert deep.parse_chunk_notes("") is None


def test_parse_chunk_notes_prose_wrapped_block_is_recovered():
    # the champion's real shape: big prose analysis, then a fenced json block,
    # then a trailing prose summary (mirrors aurora chunk-10).
    text = ("After reading all files I assess the following.\n\n"
            "## Analysis\nlots of prose here...\n\n"
            "```json\n{\"modules\": [], \"entities\": [], \"cross_cutting\": [], "
            "\"findings\": [{\"title\": \"x\", \"severity\": \"High\"}]}\n```\n\n"
            "**Final Report:** none.")
    parsed = deep.parse_chunk_notes(text)
    assert parsed and parsed["findings"][0]["title"] == "x"


def test_parse_chunk_notes_prefers_block_with_findings():
    text = ('```json\n{"modules": [{"name": "a"}]}\n```\n'
            '```json\n{"findings": [{"title": "real", "severity": "High"}]}\n```')
    parsed = deep.parse_chunk_notes(text)
    assert parsed["findings"][0]["title"] == "real"


def test_parse_chunk_notes_truncated_before_json_returns_none():
    # prose that hit the token cap before emitting any JSON (aurora chunk-01)
    text = ("CRITICAL BUG: signature bypass in webhook.py line 106...\n"
            "let me also check the next file def keeper_bills_add(provider")
    assert deep.parse_chunk_notes(text) is None


def test_empty_digest_has_unparsed_chunks():
    assert deep.empty_digest()["unparsed_chunks"] == []


def test_looks_rambly_detects_reasoning_and_length():
    clean = ("# Bug & security review\n**Findings: 1 (1 high)**\n\n"
             "## High — bypass\n`x.py:1` evidence. Impact. Fix.")
    rambly = ("# Bug & security review\n**Findings: 1**\n...\n"
              "Let me re-rate this. Wait, I need to consolidate. "
              "Actually, I should check the next finding.")
    assert deep._looks_rambly(clean) is False
    assert deep._looks_rambly(rambly) is True
    assert deep._looks_rambly("# Bug & security review\n" + "\n".join(
        f"line {i}" for i in range(250))) is True   # too long


# --- digest merge / compaction ---------------------------------------------

def _digest_with(findings):
    d = deep.empty_digest()
    for i, f in enumerate(findings):
        deep.update_digest(d, {"findings": [f]}, i)
    return d


def test_compact_digest_merges_same_root_cause_and_unions_evidence():
    d = _digest_with([
        {"title": "auth bypass", "root_cause": "missing hmac check",
         "severity": "High", "evidence": ["a.py:10"]},
        {"title": "timing attack", "root_cause": "Missing HMAC check",
         "severity": "Critical", "evidence": ["b.py:20"]},
    ])
    out = deep.compact_digest(d)
    assert len(out["provisional_findings"]) == 1
    merged = out["provisional_findings"][0]
    assert set(merged["evidence"]) == {"a.py:10", "b.py:20"}
    assert merged["severity"] == "Critical"   # highest severity wins


def test_compact_digest_drops_low_severity_over_ceiling():
    findings = [{"title": f"f{i}", "root_cause": f"cause {i}",
                 "severity": "Low" if i % 2 else "Critical",
                 "evidence": [f"x{i}.py:1"], "impact": "y" * 50}
                for i in range(20)]
    d = _digest_with(findings)
    out = deep.compact_digest(d, ceiling_tokens=80)
    sev = {f["severity"] for f in out["provisional_findings"]}
    # under pressure, Critical findings survive over Low
    assert "Critical" in sev or len(out["provisional_findings"]) < 20
    assert "Low" not in sev or sev == {"Low"}


def test_update_digest_dedupes_modules_and_cross_cutting():
    d = deep.empty_digest()
    deep.update_digest(d, {"modules": [{"name": "auth", "dir": "auth"}],
                           "cross_cutting": ["authn"]}, 0)
    deep.update_digest(d, {"modules": [{"name": "auth", "dir": "auth"}],
                           "cross_cutting": ["authn", "authz"]}, 1)
    assert len(d["modules"]) == 1
    assert d["cross_cutting"] == ["authn", "authz"]


# --- estimate ---------------------------------------------------------------

def test_estimate_run_counts_passes_and_flags_large():
    e = deep.estimate_run(10)
    assert e.passes == 12          # survey + 10 chunks + synthesis
    assert e.large is True
    assert deep.estimate_run(3).large is False


# --- extract_report keys on the required title ------------------------------

def test_extract_report_keys_on_required_title_over_stray_heading():
    from luxe.gitkit.runner import extract_report
    raw = ("# Notes to self\nsome musing\n"
           "# Bug & security review\n**Findings: 0**\nclean")
    out = extract_report(raw, "gitreview")
    assert out.startswith("# Bug & security review")
    assert "Notes to self" not in out


# --- 2-level reduce (stubbed pass_fn) ---------------------------------------

def test_reduce_findings_batches_and_keeps_survivors():
    findings = [{"title": f"f{i}", "root_cause": f"c{i}", "severity": "Medium",
                 "evidence": [f"x{i}.py:1"]} for i in range(8)]
    digest = _digest_with(findings)
    seen_batches = []

    def fake_pass(goal, ctx, label):
        blob = deep.parse_chunk_notes(ctx)
        seen_batches.append(len(blob["findings"]))
        # echo the batch back as the consolidated output
        return _FakeResult("```json\n" + json.dumps(blob) + "\n```")

    out = deep._reduce_findings(digest, eff_ctx=100, pass_fn=fake_pass)
    assert len(seen_batches) > 1                      # forced into batches
    assert len(out["provisional_findings"]) == 8      # nothing lost


# --- orchestration through run_git_report (stubbed run_single) --------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def big_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "auth").mkdir(parents=True)
    (repo / "core").mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    for d, n in (("auth", 3), ("core", 3)):
        for i in range(n):
            (repo / d / f"m{i}.py").write_text(
                f"def f{i}():\n    return {i}\n" * 5)
    (repo / "main.py").write_text("def main():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def _gitkit_cfg():
    from luxe.config import PipelineConfig, RoleConfig
    return PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
    )


def _stub_backend(monkeypatch):
    import luxe.backend as backend_mod

    class _FakeBackend:
        def __init__(self, *a, **k):
            self.model = "Champ"

    monkeypatch.setattr(backend_mod, "Backend", _FakeBackend)


def _stage_of(run_id: str) -> str:
    """Normalize a deep-mode run_id (`gitkit-deep-<kind>-<label>`) to its stage."""
    for s in ("survey", "synthesis", "reduce", "chunk"):
        if s in run_id:
            return s
    return "?"


def _deep_stub(calls: list[str]):
    """A run_single stub that records each pass by stage and returns a shape
    appropriate to it (survey notes / chunk JSON / synthesis report)."""
    def fake_run_single(backend, role_cfg, *, run_id="", extra_context="", **kw):
        stage = _stage_of(run_id)
        calls.append(stage)
        if stage == "survey":
            return _FakeResult("Survey notes: it is a python app.")
        if stage == "synthesis":
            return _FakeResult("# Bug & security review\n**Findings: 0**\nclean")
        # a chunk pass → structured JSON
        return _FakeResult('```json\n{"modules": [], "entities": [], '
                           '"cross_cutting": [], "findings": []}\n```')
    return fake_run_single


def test_deep_orchestration_counts_passes_and_writes_artifacts(
        big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)  # force multiple chunks
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _deep_stub(calls))

    report, saved = run_git_report("gitreview", cfg=_gitkit_cfg,
                                   repo_path=big_repo, console=_QuietConsole(),
                                   save=True, deep=True)
    surveys = [c for c in calls if "survey" in c]
    synths = [c for c in calls if "synthesis" in c]
    chunks = [c for c in calls if c.startswith("chunk")]
    assert len(surveys) == 1 and len(synths) == 1
    assert len(chunks) >= 2                       # repo split into ≥2 chunks
    assert report.startswith("# Bug & security review")

    # map cache + work dir persisted
    rdir = store.reports_dir(big_repo)
    assert (rdir / "map" / "chunks.json").is_file()
    assert (rdir / "map" / "survey_notes.md").is_file()
    work = list(rdir.glob("gitreview-*.work"))
    assert work and (work[0] / "xref.json").is_file()
    assert list(work[0].glob("chunk-*.md"))


def test_deep_map_cache_reused_on_second_run(big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _deep_stub(calls))

    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    first_surveys = len([c for c in calls if "survey" in c])
    calls.clear()
    # second run, same HEAD → reuse map (no survey pass), but chunks still analyzed
    run_git_report("gitrefactor", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert first_surveys == 1
    assert len([c for c in calls if "survey" in c]) == 0      # reused
    assert len([c for c in calls if c.startswith("chunk")]) >= 2

    # --rebuild-map forces a re-survey
    calls.clear()
    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True, rebuild_map=True)
    assert len([c for c in calls if "survey" in c]) == 1


def test_deep_recovers_markdown_findings_when_no_json(big_repo, _gitkit_cfg, monkeypatch):
    """The champion often rambles then concludes with the required markdown header
    instead of emitting JSON — those findings must be recovered (sliced), not
    dropped, and reach synthesis."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    seen = {}

    def fake_run_single(backend, role_cfg, *, run_id="", extra_context="", **kw):
        stage = _stage_of(run_id)
        if stage == "survey":
            return _FakeResult("survey notes")
        if stage == "synthesis":
            seen["synth_ctx"] = extra_context
            return _FakeResult("# Bug & security review\n**Findings: 1**\nx")
        # chunk: monologue, THEN the required header + a real finding (no JSON)
        return _FakeResult(
            "Let me analyze these files step by step...\n"
            "I looked at the webhook handler and the config loader.\n\n"
            "# Bug & security review\n**Findings: 1 (1 high)**\n\n"
            "## High — signature bypass\n`webhook.py:106` returns True when key "
            "unset.\nImpact: unauthenticated webhooks. Fix: fail closed.")

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    work = list(store.reports_dir(big_repo).glob("gitreview-*.work"))
    xref = json.loads((work[0] / "xref.json").read_text())
    assert len(xref["markdown_notes"]) >= 2          # recovered, not dropped
    assert xref["unparsed_chunks"] == []
    # the monologue was sliced off; the finding reached synthesis
    note = xref["markdown_notes"][0]["md"]
    assert note.startswith("# Bug & security review")
    assert "step by step" not in note
    assert "signature bypass" in seen["synth_ctx"]


def test_deep_extract_pass_recovers_findings_from_rambly_analysis(
        big_repo, _gitkit_cfg, monkeypatch):
    """The champion's real failure: the chunk analysis is headerless rambly prose
    (truncated before any conclusion). A focused EXTRACT pass must reformat it into
    the report shape so the findings are recovered, not lost."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    seen = {"extract_ctx": []}

    def fake_run_single(backend, role_cfg, *, run_id="", extra_context="", **kw):
        if "survey" in run_id:
            return _FakeResult("survey notes")
        if "synthesis" in run_id:
            seen["synth_ctx"] = extra_context
            return _FakeResult("# Bug & security review\n**Findings: 1**\nx")
        if "extract" in run_id:
            # the focused reformat pass: gets the analysis, emits the report
            seen["extract_ctx"].append(extra_context)
            return _FakeResult(
                "# Bug & security review\n**Findings: 1 (1 high)**\n\n"
                "## High — signature bypass\n`webhook.py:106` returns True when "
                "key unset.")
        # chunk analysis: headerless rambly prose, truncated before conclusion
        return _FakeResult(
            "## 1. webhook.py\nLooking at line 106, verify_signature returns True "
            "when the key is unset — a bypass. Let me check the next file def foo(")

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)

    # the extract pass received the rambly analysis as input
    assert seen["extract_ctx"] and "signature" in seen["extract_ctx"][0].lower()
    work = list(store.reports_dir(big_repo).glob("gitreview-*.work"))
    xref = json.loads((work[0] / "xref.json").read_text())
    assert len(xref["markdown_notes"]) >= 2          # recovered via extract
    assert xref["unparsed_chunks"] == []             # nothing lost
    assert "signature bypass" in seen["synth_ctx"]   # reached synthesis


def test_deep_format_pass_cleans_rambly_synthesis(big_repo, _gitkit_cfg, monkeypatch):
    """When the synthesis narrates its reasoning into the report, a strict format
    pass reproduces a clean report (the saved report must be the clean one)."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    clean = ("# Bug & security review\n**Findings: 1 (1 critical)**\n\n"
             "## Critical — signature bypass\n`webhook.py:106` returns True.")

    def fake_run_single(backend, role_cfg, *, run_id="", extra_context="", **kw):
        if "survey" in run_id:
            return _FakeResult("survey notes")
        if "format" in run_id:
            seen_draft.append(extra_context)
            return _FakeResult(clean)
        if "synthesis" in run_id:
            # rambly synthesis: header then a wall of reasoning
            return _FakeResult(
                "# Bug & security review\n**Findings: 1**\n...\n"
                "Let me re-rate. Wait, I need to consolidate these. Actually, I "
                "should ignore the chain-of-thought. " + "\n".join(
                    f"reasoning line {i}" for i in range(250)))
        return _FakeResult('```json\n{"findings": [{"title": "b", '
                           '"severity": "Critical"}]}\n```')

    seen_draft: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    report, saved = run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                                   console=_QuietConsole(), save=True, deep=True)
    assert seen_draft                                    # format pass ran
    assert report.startswith("# Bug & security review")
    assert "re-rate" not in report and "reasoning line" not in report
    assert "signature bypass" in report                 # clean findings kept


def test_deep_max_chunks_caps_and_logs(big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _deep_stub(calls))

    out = io.StringIO()
    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=Console(file=out, force_terminal=False, width=200),
                   save=True, deep=True, max_chunks=1)
    assert len([c for c in calls if c.startswith("chunk")]) == 1
    assert "SKIPPING" in out.getvalue()


def test_deep_cancel_between_chunks_saves_partial(big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.chat.render import CancelToken
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _deep_stub(calls))

    cancel = CancelToken()
    cancel.requested = True   # the per-chunk loop raises before chunk 1
    report, saved = run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                                   console=_QuietConsole(), save=True, deep=True,
                                   cancel=cancel)
    assert report == "" and saved is None
    # partial notes (xref.json) were written before exit
    work = list(store.reports_dir(big_repo).glob("gitreview-*.work"))
    assert work and (work[0] / "xref.json").is_file()


def test_deep_synthesis_receives_aggregated_digest(big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    seen = {}

    def fake_run_single(backend, role_cfg, *, run_id="", extra_context="", **kw):
        stage = run_id.split("-deep-")[-1] if "-deep-" in run_id else run_id
        if "survey" in stage:
            return _FakeResult("survey notes")
        if "synthesis" in stage:
            seen["synth_ctx"] = extra_context
            return _FakeResult("# Bug & security review\n**Findings: 1**\nx")
        # chunk → emit one finding so the digest is non-empty
        return _FakeResult('```json\n{"findings": [{"title": "bug", '
                           '"root_cause": "rc", "severity": "High", '
                           '"evidence": ["a.py:1"]}]}\n```')

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert "<chunk_findings>" in seen["synth_ctx"]
    assert "rc" in seen["synth_ctx"]      # aggregated finding reached synthesis


def test_deep_unparsed_chunk_is_flagged_not_dropped(big_repo, _gitkit_cfg, monkeypatch):
    """A chunk that emits prose-only (no JSON — truncated/empty) must surface in
    unparsed_chunks and reach synthesis, never be silently dropped."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    seen = {}

    def fake_run_single(backend, role_cfg, *, run_id="", extra_context="", **kw):
        stage = _stage_of(run_id)
        if stage == "survey":
            return _FakeResult("survey notes")
        if stage == "synthesis":
            seen["synth_ctx"] = extra_context
            return _FakeResult("# Bug & security review\n**Findings: 0**\nx")
        # every chunk returns prose with NO json block (the truncation failure mode)
        return _FakeResult("CRITICAL BUG: signature bypass... def foo(bar")

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert "unparsed_chunks" in seen["synth_ctx"]
    # the digest persisted to disk records the flagged chunks
    work = list(store.reports_dir(big_repo).glob("gitreview-*.work"))
    xref = json.loads((work[0] / "xref.json").read_text())
    assert len(xref["unparsed_chunks"]) >= 2
