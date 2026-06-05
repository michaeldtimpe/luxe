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


def test_render_report_assembles_clean_report_from_notes():
    d = deep.empty_digest()
    d["markdown_notes"] = [
        {"chunk": 0, "label": "auth", "md": "# Bug & security review\n"
         "**Findings: 1 (1 critical)**\n\n## Critical — bypass\n`a.py:10` high."},
        {"chunk": 1, "label": "api", "md": "# Bug & security review\n"
         "**Findings: 1 (1 high)**\n\n## High — dos\n`b.py:20` medium."},
    ]
    d["unparsed_chunks"] = ["chunk 3 (x): c.py"]
    out = deep._render_report(d, "gitreview")
    assert out.startswith("# Bug & security review")
    assert "## Area: auth (chunk 1)" in out and "## Area: api (chunk 2)" in out
    assert "## Coverage gaps" in out and "c.py" in out
    assert not deep._looks_rambly(out)          # deterministic = never rambly
    # the per-note headers were stripped, body kept
    assert out.count("# Bug & security review") == 1
    assert "bypass" in out and "dos" in out


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


def test_estimate_run_per_stage_arithmetic():
    # survey + 4 chunks + synth = 70 + 4*235 + 70 = 1080s
    e = deep.estimate_run(4)
    assert e.seconds == deep._SURVEY_S + 4 * deep._CHUNK_S + deep._SYNTH_S == 1080
    assert e.passes == 6


def test_estimate_run_survey_cached_drops_survey_term():
    full = deep.estimate_run(4)
    cached = deep.estimate_run(4, survey_cached=True)
    assert cached.seconds == full.seconds - deep._SURVEY_S
    assert cached.passes == full.passes - 1


def test_estimate_run_zero_chunks_no_false_floor():
    e = deep.estimate_run(0)
    assert e.seconds == deep._SYNTH_S      # not survey+synth; no deceptive floor
    assert e.chunks == 0


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


# --- per-pass timing telemetry (B1-B4) --------------------------------------

def test_deep_writes_timing_sidecar(big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)  # force ≥2 chunks
    _stub_backend(monkeypatch)
    monkeypatch.setattr(single_mod, "run_single", _deep_stub([]))

    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)

    work = list(store.reports_dir(big_repo).glob("gitreview-*.work"))[0]
    blob = json.loads((work / "timing.json").read_text())
    # survey + ≥2 chunks + synthesis
    assert blob["n_passes"] >= 4
    assert blob["total_wall_s"] > 0
    labels = {p["label"] for p in blob["passes"]}
    assert "survey" in labels and "synthesis" in labels
    every = blob["passes"]
    assert all("window" in p and "started_at" in p for p in every)
    # chunk passes carry their footprint (call-site enrichment)
    chunk_rows = [p for p in every if p["label"].startswith("chunk-")]
    assert chunk_rows and any(p["n_files"] > 0 and p["loc"] > 0 for p in chunk_rows)


def test_deep_frontmatter_carries_timing(big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    monkeypatch.setattr(single_mod, "run_single", _deep_stub([]))

    _, saved = run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                              console=_QuietConsole(), save=True, deep=True)
    text = saved.read_text()
    assert "mode: deep" in text
    assert "chunks:" in text
    assert "total_wall_s:" in text
    assert "n_passes:" in text
    assert "avg_pass_s:" in text


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


def test_deep_format_pass_recovers_findings_from_rambly_analysis(
        big_repo, _gitkit_cfg, monkeypatch):
    """The champion's real failure: a chunk's analysis is rambly headerless prose.
    _clean_note must run a transcription (format) pass to recover a clean note, so
    the findings are kept, not lost."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    seen = {"format_ctx": []}

    def fake_run_single(backend, role_cfg, *, run_id="", extra_context="", **kw):
        if "survey" in run_id:
            return _FakeResult("survey notes")
        if "synthesis" in run_id:
            seen["synth_ctx"] = extra_context
            return _FakeResult("# Bug & security review\n**Findings: 1**\nx")
        if "format" in run_id:
            # the transcription pass: gets the rambly analysis, emits a clean report
            seen["format_ctx"].append(extra_context)
            return _FakeResult(
                "# Bug & security review\n**Findings: 1 (1 high)**\n\n"
                "## High — signature bypass\n`webhook.py:106` returns True when "
                "key unset.")
        # chunk analysis: rambly headerless prose (≥3 reasoning markers → rambly)
        return _FakeResult(
            "## 1. webhook.py\nLet me look at line 106. Wait, verify_signature "
            "returns True when the key is unset. Actually, I need to check the "
            "next file. Let me also re-rate this. Hmm, def foo(")

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)

    # the transcription pass received the rambly analysis as input
    assert seen["format_ctx"] and "signature" in seen["format_ctx"][0].lower()
    work = list(store.reports_dir(big_repo).glob("gitreview-*.work"))
    xref = json.loads((work[0] / "xref.json").read_text())
    assert len(xref["markdown_notes"]) >= 2          # recovered via format pass
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


def test_deep_empty_chunk_is_flagged_not_dropped(big_repo, _gitkit_cfg, monkeypatch):
    """A chunk that emits nothing usable (empty output) must surface in
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
        return _FakeResult("")   # empty chunk output → nothing to recover

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert "unparsed_chunks" in seen["synth_ctx"]
    # the digest persisted to disk records the flagged chunks
    work = list(store.reports_dir(big_repo).glob("gitreview-*.work"))
    xref = json.loads((work[0] / "xref.json").read_text())
    assert len(xref["unparsed_chunks"]) >= 2


# --- map breadcrumb + health classification (A1-A4) -------------------------

def _seed_map(target: Path, *, head="abc123") -> Path:
    """Write a complete, FRESH map for `target` and return its map/ dir."""
    chunks = [deep.Chunk(index=0, files=["a.py"], label="a", est_tokens=10, loc=5)]
    deep.save_map(target, head=head, survey_notes="notes", chunks=chunks,
                  content_budget=4096, framing=[], summary_render="map")
    return deep._map_dir(target)


def test_save_map_writes_breadcrumb(tmp_path):
    target = tmp_path / "repo"
    d = _seed_map(target, head="deadbeef")
    bc = json.loads((d / "mapped.json").read_text())
    assert bc["version"] == 1
    assert bc["head"] == "deadbeef"
    assert bc["n_chunks"] == 1
    assert bc["content_budget"] == 4096
    assert isinstance(bc["mapped_at"], int) and bc["mapped_at"] > 0


def test_map_status_fresh_after_save(tmp_path):
    target = tmp_path / "repo"
    _seed_map(target, head="h1")
    assert deep.map_status(target, head="h1").state is deep.MapState.FRESH


def test_map_status_missing_when_no_breadcrumb(tmp_path):
    target = tmp_path / "repo"
    assert deep.map_status(target, head="h1").state is deep.MapState.MISSING


def test_map_status_stale_on_head_move(tmp_path):
    target = tmp_path / "repo"
    _seed_map(target, head="old")
    st = deep.map_status(target, head="new")
    assert st.state is deep.MapState.STALE
    assert st.head == "old"


def test_map_status_partial_when_survey_notes_deleted(tmp_path):
    target = tmp_path / "repo"
    d = _seed_map(target, head="h1")
    (d / "survey_notes.md").unlink()
    st = deep.map_status(target, head="h1")
    assert st.state is deep.MapState.PARTIAL
    assert "survey_notes.md" in st.missing
    # breadcrumb metadata is still surfaced for the warning
    assert st.head == "h1" and st.n_chunks == 1


def test_map_status_partial_when_chunks_json_corrupt(tmp_path):
    target = tmp_path / "repo"
    d = _seed_map(target, head="h1")
    (d / "chunks.json").write_text("{ this is not valid json")
    st = deep.map_status(target, head="h1")
    assert st.state is deep.MapState.PARTIAL
    assert any("chunks.json" in m for m in st.missing)


def test_map_status_partial_when_breadcrumb_corrupt(tmp_path):
    target = tmp_path / "repo"
    d = _seed_map(target, head="h1")
    (d / "mapped.json").write_text("not json at all")
    st = deep.map_status(target, head="h1")
    # a corrupt breadcrumb is evidence a map existed → PARTIAL, not MISSING
    assert st.state is deep.MapState.PARTIAL


def test_load_map_returns_none_when_partial(tmp_path):
    target = tmp_path / "repo"
    d = _seed_map(target, head="h1")
    (d / "survey_notes.md").unlink()
    assert deep.load_map(target, head="h1") is None  # contract preserved


def test_load_map_returns_dict_when_fresh(tmp_path):
    target = tmp_path / "repo"
    _seed_map(target, head="h1")
    m = deep.load_map(target, head="h1")
    assert m is not None and m["survey_notes"].strip() == "notes"
    assert [c.files for c in m["chunks"]] == [["a.py"]]


# --- partial-map handling through the orchestrator (A5) ----------------------

def _InteractiveConsole():
    return Console(file=io.StringIO(), force_terminal=True, width=120)


def test_deep_partial_map_batch_rebuilds_and_logs(big_repo, _gitkit_cfg, monkeypatch):
    """Non-interactive (no TTY): a damaged map must be announced + rebuilt, never
    silently treated as 'never mapped'."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _deep_stub(calls))

    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert len([c for c in calls if "survey" in c]) == 1
    # damage the map (delete a heavy file), then re-run on the SAME head
    (store.reports_dir(big_repo) / "map" / "survey_notes.md").unlink()
    calls.clear()
    out = io.StringIO()
    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=Console(file=out, force_terminal=False, width=120),
                   save=True, deep=True)
    assert len([c for c in calls if "survey" in c]) == 1     # rebuilt, not reused
    assert "partial" in out.getvalue().lower()               # announced


def test_deep_partial_map_interactive_cancel(big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _deep_stub(calls))

    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    (store.reports_dir(big_repo) / "map" / "survey_notes.md").unlink()
    calls.clear()
    report, saved = run_git_report(
        "gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
        console=_InteractiveConsole(), reader=lambda _p: "n", save=True, deep=True)
    assert (report, saved) == ("", None)                     # cancelled
    assert not [c for c in calls if "survey" in c]            # no re-survey


def test_deep_partial_map_interactive_rebuild(big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _deep_stub(calls))

    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    (store.reports_dir(big_repo) / "map" / "survey_notes.md").unlink()
    calls.clear()
    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_InteractiveConsole(), reader=lambda _p: "y",
                   save=True, deep=True)
    assert len([c for c in calls if "survey" in c]) == 1      # rebuilt on Y


# --- Part 5: deep run mirrors map + report into <repo>/.luxe/gitkit/ ---------

def test_deep_run_mirrors_map_and_report(big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    monkeypatch.setattr(single_mod, "run_single", _deep_stub([]))

    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    mirror = big_repo / ".luxe" / "gitkit"
    assert (mirror / "survey_notes.md").is_file()
    assert (mirror / "chunks.json").is_file()
    assert (mirror / "mapped.json").is_file()
    assert (mirror / "README.md").is_file()
    assert list((mirror / "reports").glob("gitreview-*.md"))


def test_deep_run_no_mirror_skips_repo_write(big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    monkeypatch.setattr(single_mod, "run_single", _deep_stub([]))

    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True, mirror=False)
    assert not (big_repo / ".luxe" / "gitkit").exists()
