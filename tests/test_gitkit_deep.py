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
        {"chunk": 0, "label": "auth", "md": "# Repository audit\n"
         "**Findings: 1 (1 critical)**\n\n## Critical — bypass\n`a.py:10` high."},
        {"chunk": 1, "label": "api", "md": "# Repository audit\n"
         "**Findings: 1 (1 high)**\n\n## High — dos\n`b.py:20` medium."},
    ]
    d["unparsed_chunks"] = ["chunk 3 (x): c.py"]
    out = deep._render_report(d, "gitaudit")
    assert out.startswith("# Repository audit")
    assert "## Area: auth (chunk 1)" in out and "## Area: api (chunk 2)" in out
    assert "## Coverage gaps" in out and "c.py" in out
    assert not deep._looks_rambly(out)          # deterministic = never rambly
    # the per-note headers were stripped, body kept
    assert out.count("# Repository audit") == 1
    assert "bypass" in out and "dos" in out


def test_looks_rambly_detects_reasoning_and_length():
    clean = ("# Repository audit\n**Findings: 1 (1 high)**\n\n"
             "## High — bypass\n`x.py:1` evidence. Impact. Fix.")
    rambly = ("# Repository audit\n**Findings: 1**\n...\n"
              "Let me re-rate this. Wait, I need to consolidate. "
              "Actually, I should check the next finding.")
    assert deep._looks_rambly(clean) is False
    assert deep._looks_rambly(rambly) is True
    assert deep._looks_rambly("# Repository audit\n" + "\n".join(
        f"line {i}" for i in range(250))) is True   # too long


def test_heuristic_findings_salvages_numbered_bold_and_drops_nonfindings():
    """Offline-recovery upgrade: the champion emits findings as numbered BOLD items
    with a file/line/code ref when it never reaches the report header. The heuristic
    must salvage those (the old severity-word-only regex missed them) and skip lines
    explicitly marked as non-findings + plain exploration narrative."""
    ramble = (
        "Let me analyze this file carefully. Let me look at cli.py:29 more.\n"
        "1. **cli.py line 29-38**: `git clone --depth=1` without verifying the URL scheme\n"
        "2. **Line 1288**: `tempfile.mkdtemp(...)` — temp dir only cleaned up if flag set\n"
        "3. **`_check_regex_present` (line 609)**: off-by-one in the match counter\n"
        "4. **util.py line 12**: `import yaml as _yaml` — Not a bug.\n"
        "Now let me check the next file to be thorough.\n")
    out = deep._heuristic_findings(ramble)
    joined = " ".join(out)
    assert len(out) == 3                              # 3 real findings recovered
    assert "git clone" in joined and "tempfile" in joined and "off-by-one" in joined
    assert "Not a bug" not in joined                  # explicit non-finding dropped
    assert "Let me analyze" not in joined             # exploration narrative skipped
    # the OLD severity-word-only regex would have salvaged none of these
    assert all(not _re_sev(o) for o in out)


def _re_sev(line: str) -> bool:
    import re
    return bool(re.search(r"\b(critical|high|medium|low)\b", line, re.I))


def test_heuristic_findings_salvages_numbered_plain_with_file_line():
    """A4 corpus shape: numbered NON-bold items carrying an explicit file:line ref
    (e.g. deluxe/processor_service dumps) — the pre-A4 heuristic missed these."""
    ramble = (
        "Let me work through these files one by one now.\n"
        "1. `ml/evaluate.py:44` — `torch.load` without `weights_only`.\n"
        "2. `data-tools/collect.py:119-123` — ZipFile without path traversal validation.\n"
        "3. A general note about style with no reference anywhere here.\n")
    out = deep._heuristic_findings(ramble)
    assert len(out) == 2
    assert "torch.load" in out[0] and "ZipFile" in out[1]


def test_heuristic_findings_severity_lead_with_lookahead_ref():
    """A4 corpus shape: a bold/bracket severity-lead line whose file ref trails
    within the next 2 lines — emitted as one combined line."""
    ramble = ("**Critical:** unauthenticated admin endpoint\n"
              "   found in app/api/admin.py:42\n"
              "[HIGH] SQL string concatenation\n"
              "   see db/queries.py:17\n"
              "Severity: medium — weak comparison in token check `auth.py:9`\n")
    out = deep._heuristic_findings(ramble)
    assert len(out) == 3
    assert "admin.py" in out[0]          # lookahead ref folded into the line
    assert "queries.py" in out[1]
    assert "auth.py" in out[2]           # same-line ref kept as-is


def test_heuristic_findings_heading_needs_severity_plus_substance():
    """A4 FP guard (corpus-verified): bare per-file exploration headings
    ('### app/api/auth.py') must NOT be swept in; severity-word headings with
    substance (file ref / code / number) are findings."""
    ramble = ("### app/api/auth.py\n"
              "walking through this file now, nothing concluded yet\n"
              "### High: race condition in `deep.py:712` save path\n"
              "the breadcrumb write can interleave with a reader\n")
    out = deep._heuristic_findings(ramble)
    assert len(out) == 1
    assert "race condition" in out[0]
    assert all("app/api/auth.py" not in o for o in out)


def test_heuristic_findings_dedups_renumbered_repeats():
    """A4: the champion re-emits the same finding under different list numbers /
    tail phrasings — one survivor."""
    ramble = (
        "1. `datetime.utcnow()` deprecation in `processor_service.py:159` and "
        "`processor_service.py:273` - Low severity.\n"
        "2. `datetime.utcnow()` deprecation in `processor_service.py:159` and "
        "`processor_service.py:273` - Low severity, forward compatibility issue.\n")
    out = deep._heuristic_findings(ramble)
    assert len(out) == 1


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
           "# Repository audit\n**Findings: 0**\nclean")
    out = extract_report(raw, "gitaudit")
    assert out.startswith("# Repository audit")
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
            return _FakeResult("# Repository audit\n**Findings: 0**\nclean")
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

    report, saved = run_git_report("gitaudit", cfg=_gitkit_cfg,
                                   repo_path=big_repo, console=_QuietConsole(),
                                   save=True, deep=True)
    surveys = [c for c in calls if "survey" in c]
    synths = [c for c in calls if "synthesis" in c]
    chunks = [c for c in calls if c.startswith("chunk")]
    assert len(surveys) == 1 and len(synths) == 1
    assert len(chunks) >= 2                       # repo split into ≥2 chunks
    assert report.startswith("# Repository audit")

    # map cache + work dir persisted
    rdir = store.reports_dir(big_repo)
    assert (rdir / "map" / "chunks.json").is_file()
    assert (rdir / "map" / "survey_notes.md").is_file()
    work = list(rdir.glob("gitaudit-*.work"))
    assert work and (work[0] / "xref.json").is_file()
    assert list(work[0].glob("chunk-*.md"))


def test_deep_map_cache_reused_on_second_run(big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _deep_stub(calls))

    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    first_surveys = len([c for c in calls if "survey" in c])
    calls.clear()
    # second run, same HEAD → reuse map (no survey) AND the sha-validated
    # chunk-notes cache (no chunk passes either — crash-resume semantics);
    # synthesis always re-runs.
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert first_surveys == 1
    assert len([c for c in calls if "survey" in c]) == 0      # map reused
    assert len([c for c in calls if c.startswith("chunk")]) == 0  # notes reused
    assert len([c for c in calls if "synthesis" in c]) == 1   # always re-runs

    # --no-incremental forces chunk re-analysis (map still reused)
    calls.clear()
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True,
                   no_incremental=True)
    assert len([c for c in calls if "survey" in c]) == 0
    assert len([c for c in calls if c.startswith("chunk")]) >= 2

    # --rebuild-map forces a re-survey AND chunk re-analysis
    calls.clear()
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True, rebuild_map=True)
    assert len([c for c in calls if "survey" in c]) == 1
    assert len([c for c in calls if c.startswith("chunk")]) >= 2


# --- per-pass timing telemetry (B1-B4) --------------------------------------

def test_deep_writes_timing_sidecar(big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)  # force ≥2 chunks
    _stub_backend(monkeypatch)
    monkeypatch.setattr(single_mod, "run_single", _deep_stub([]))

    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)

    work = list(store.reports_dir(big_repo).glob("gitaudit-*.work"))[0]
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

    _, saved = run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
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
            return _FakeResult("# Repository audit\n**Findings: 1**\nx")
        # chunk: monologue, THEN the required header + a real finding (no JSON)
        return _FakeResult(
            "Let me analyze these files step by step...\n"
            "I looked at the webhook handler and the config loader.\n\n"
            "# Repository audit\n**Findings: 1 (1 high)**\n\n"
            "## High — signature bypass\n`webhook.py:106` returns True when key "
            "unset.\nImpact: unauthenticated webhooks. Fix: fail closed.")

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    work = list(store.reports_dir(big_repo).glob("gitaudit-*.work"))
    xref = json.loads((work[0] / "xref.json").read_text())
    assert len(xref["markdown_notes"]) >= 2          # recovered, not dropped
    assert xref["unparsed_chunks"] == []
    # the monologue was sliced off; the finding reached synthesis
    note = xref["markdown_notes"][0]["md"]
    assert note.startswith("# Repository audit")
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
            return _FakeResult("# Repository audit\n**Findings: 1**\nx")
        if "format" in run_id:
            # the transcription pass: gets the rambly analysis, emits a clean report
            seen["format_ctx"].append(extra_context)
            return _FakeResult(
                "# Repository audit\n**Findings: 1 (1 high)**\n\n"
                "## High — signature bypass\n`webhook.py:106` returns True when "
                "key unset.")
        # chunk analysis: rambly headerless prose (≥3 reasoning markers → rambly)
        return _FakeResult(
            "## 1. webhook.py\nLet me look at line 106. Wait, verify_signature "
            "returns True when the key is unset. Actually, I need to check the "
            "next file. Let me also re-rate this. Hmm, def foo(")

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)

    # the transcription pass received the rambly analysis as input
    assert seen["format_ctx"] and "signature" in seen["format_ctx"][0].lower()
    work = list(store.reports_dir(big_repo).glob("gitaudit-*.work"))
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
    clean = ("# Repository audit\n**Findings: 1 (1 critical)**\n\n"
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
                "# Repository audit\n**Findings: 1**\n...\n"
                "Let me re-rate. Wait, I need to consolidate these. Actually, I "
                "should ignore the chain-of-thought. " + "\n".join(
                    f"reasoning line {i}" for i in range(250)))
        return _FakeResult('```json\n{"findings": [{"title": "b", '
                           '"severity": "Critical"}]}\n```')

    seen_draft: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    report, saved = run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                                   console=_QuietConsole(), save=True, deep=True)
    assert seen_draft                                    # format pass ran
    assert report.startswith("# Repository audit")
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
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
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
    report, saved = run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                                   console=_QuietConsole(), save=True, deep=True,
                                   cancel=cancel)
    assert report == "" and saved is None
    # partial notes (xref.json) were written before exit
    work = list(store.reports_dir(big_repo).glob("gitaudit-*.work"))
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
            return _FakeResult("# Repository audit\n**Findings: 1**\nx")
        # chunk → emit one finding so the digest is non-empty
        return _FakeResult('```json\n{"findings": [{"title": "bug", '
                           '"root_cause": "rc", "severity": "High", '
                           '"evidence": ["a.py:1"]}]}\n```')

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
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
            return _FakeResult("# Repository audit\n**Findings: 0**\nx")
        return _FakeResult("")   # empty chunk output → nothing to recover

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert "unparsed_chunks" in seen["synth_ctx"]
    # the digest persisted to disk records the flagged chunks
    work = list(store.reports_dir(big_repo).glob("gitaudit-*.work"))
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
    assert bc["version"] == 2                      # incremental-capable schema
    assert "files" in bc and "baseline" in bc      # v2 staleness currency
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

    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert len([c for c in calls if "survey" in c]) == 1
    # damage the map (delete a heavy file), then re-run on the SAME head
    (store.reports_dir(big_repo) / "map" / "survey_notes.md").unlink()
    calls.clear()
    out = io.StringIO()
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
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

    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    (store.reports_dir(big_repo) / "map" / "survey_notes.md").unlink()
    calls.clear()
    report, saved = run_git_report(
        "gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
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

    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    (store.reports_dir(big_repo) / "map" / "survey_notes.md").unlink()
    calls.clear()
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
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

    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    mirror = big_repo / ".luxe" / "gitkit"
    assert (mirror / "survey_notes.md").is_file()
    assert (mirror / "chunks.json").is_file()
    assert (mirror / "mapped.json").is_file()
    assert (mirror / "README.md").is_file()
    assert list((mirror / "reports").glob("gitaudit-*.md"))


def test_deep_run_no_mirror_skips_repo_write(big_repo, _gitkit_cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    monkeypatch.setattr(single_mod, "run_single", _deep_stub([]))

    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True, mirror=False)
    assert not (big_repo / ".luxe" / "gitkit").exists()


# --- A3: atomic map-cache writes ---------------------------------------------

def test_atomic_write_text_replaces_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "f.json"
    p.write_text("old")
    deep._atomic_write_text(p, "new")
    assert p.read_text() == "new"
    assert list(tmp_path.glob("*.tmp.*")) == []


def test_save_map_crash_mid_write_keeps_old_breadcrumb(tmp_path, monkeypatch):
    """A3: a crash before the breadcrumb replace must leave the OLD mapped.json
    intact — map_status never reports a half-new map as FRESH."""
    import os as os_mod
    target = tmp_path / "repo"
    _seed_map(target, head="h1")
    d = deep._map_dir(target)

    real_replace = os_mod.replace

    def flaky_replace(src, dst, *a, **kw):
        if str(dst).endswith("chunks.json"):
            raise OSError("disk full")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(deep.os, "replace", flaky_replace)
    with pytest.raises(OSError):
        deep.save_map(target, head="h2", survey_notes="new notes",
                      chunks=[deep.Chunk(index=0, files=["b.py"], label="b")],
                      content_budget=2048, framing=[], summary_render="m")

    bc = json.loads((d / "mapped.json").read_text())
    assert bc["head"] == "h1"                       # old breadcrumb intact
    # the torn save is never seen as FRESH for the new head…
    assert deep.map_status(target, head="h2").state is not deep.MapState.FRESH
    # …and the old head's map is either still FRESH or flagged damaged (PARTIAL),
    # never silently half-new.
    st = deep.map_status(target, head="h1")
    assert st.state in (deep.MapState.FRESH, deep.MapState.PARTIAL)


# --- A5: oversized files / enumerate log / clean-note log / report ranking ---

def test_build_chunks_marks_oversized_and_chunk_block_notices(tmp_path):
    files = [_frec("big.py", tokens=5000), _frec("small.py", tokens=10)]
    chunks = deep.build_chunks(files, content_budget=100)
    over = [c for c in chunks if c.oversized]
    assert over and over[0].oversized == ["big.py"]
    block = deep._chunk_block(over[0], len(chunks))
    assert "Oversized files" in block and "big.py" in block
    assert "read them in sections" in block
    # back-compat: cached chunks.json predating the field still loads
    legacy = {"index": 0, "files": ["a.py"], "label": "a"}
    assert deep.Chunk.from_dict(legacy).oversized == []
    # and a chunk with no oversized files carries no notice
    small = [c for c in chunks if not c.oversized]
    assert small and "Oversized" not in deep._chunk_block(small[0], len(chunks))


def test_enumerate_files_logs_oserror_skips(tmp_path, monkeypatch):
    root = tmp_path / "r"
    root.mkdir()
    (root / "good.py").write_text("x = 1\n")
    (root / "bad.py").write_text("y = 2\n")
    real_stat = Path.stat

    def fake_stat(self, **kw):
        if self.name == "bad.py":
            raise OSError("permission denied")
        return real_stat(self, **kw)

    monkeypatch.setattr(Path, "stat", fake_stat)
    logs: list[str] = []
    recs = deep.enumerate_files(root, _FakeSummary(2), log=logs.append)
    assert [r.rel for r in recs] == ["good.py"]
    assert len(logs) == 1 and "bad.py" in logs[0]


def test_clean_note_logs_which_recovery_rung(monkeypatch):
    rambly = ("Let me look at this. I need to check more. Wait, actually I "
              "should re-read.\n1. **a.py line 3**: `x` shadowed and reused\n")

    # rung 0: already-clean input → md_clean, no recovery pass
    clean_in, src0 = deep._clean_note("# Repository audit\n- finding",
                                      "gitaudit",
                                      pass_fn=lambda *a, **k: None, role=None)
    assert src0 == "md_clean" and clean_in.startswith("# Repository audit")

    # rung 1: transcription pass returns a clean headered report
    logs: list[str] = []
    clean_report = ("# Repository audit\n**Findings: 1**\n\n"
                    "- **high** `a.py:3` — shadowed variable")
    out, src = deep._clean_note(rambly, "gitaudit",
                                pass_fn=lambda g, c, l, role=None: _FakeResult(clean_report),
                                role=None, log=logs.append)
    assert out is not None and out.startswith("# Repository audit")
    assert src == "md_transcribed"
    assert any("transcription pass recovered" in m for m in logs)

    # rung 2: transcription stays rambly → heuristic salvage, with line count
    logs2: list[str] = []
    out2, src2 = deep._clean_note(rambly, "gitaudit",
                                  pass_fn=lambda g, c, l, role=None: _FakeResult(rambly),
                                  role=None, log=logs2.append)
    assert out2 is not None and "a.py" in out2
    assert src2 == "heuristic"
    assert any("heuristic salvage (1 lines)" in m for m in logs2)


def test_extract_report_ranks_headers_by_title_overlap():
    from luxe.gitkit.runner import extract_report
    # a monologue H1 with PARTIAL overlap appears first; the true title wins
    raw = ("# Audit working notes\nthinking...\n"
           "# Repository audit\n**Findings: 0**\nclean")
    out = extract_report(raw, "gitaudit")
    assert out.startswith("# Repository audit")
    # near-title (extra suffix) still beats the monologue heading
    raw2 = ("# My notes\nstuff\n# Repository audit — preliminary\nbody")
    assert extract_report(raw2, "gitaudit").startswith("# Repository audit — pre")


def test_extract_report_zero_overlap_falls_back_to_first_h1():
    from luxe.gitkit.runner import extract_report
    raw = "prelude prose\n# Something else entirely\nbody"
    out = extract_report(raw, "gitaudit")
    assert out.startswith("# Something else entirely")   # never drop content


# --- Phase 3: provenance / evidence-overlap dedup / confidence / ordering ----

def test_update_digest_stamps_provenance():
    d = deep.empty_digest()
    deep.update_digest(d, {"findings": [{"title": "t", "evidence": ["a.py:1"]}]}, 3)
    f = d["provisional_findings"][0]
    assert f["source"] == "json" and f["chunks"] == [3] and f["chunk"] == 3


def test_compact_digest_evidence_overlap_merges_different_wording():
    """Same bug, different words, shared file:line evidence → ONE finding with
    union evidence + chunks and the best provenance source."""
    d = deep.empty_digest()
    deep.update_digest(d, {"findings": [
        {"title": "race in save path", "root_cause": "torn write",
         "severity": "medium", "evidence": ["deep.py:712"]}]}, 0)
    deep.update_digest(d, {"findings": [
        {"title": "non-atomic cache write", "root_cause": "interleaved writers",
         "severity": "High", "evidence": ["deep.py line 712", "deep.py:99"]}]}, 2)
    out = deep.compact_digest(d)
    assert len(out["provisional_findings"]) == 1
    f = out["provisional_findings"][0]
    assert f["severity"] == "High"                       # max severity
    assert f["chunks"] == [0, 2]                         # union contributors
    assert len(f["evidence"]) >= 2


def test_compact_digest_no_overlap_stays_separate():
    d = deep.empty_digest()
    deep.update_digest(d, {"findings": [
        {"title": "bug one", "root_cause": "x", "evidence": ["a.py:1"]}]}, 0)
    deep.update_digest(d, {"findings": [
        {"title": "bug two", "root_cause": "y", "evidence": ["b.py:9"]}]}, 1)
    assert len(deep.compact_digest(d)["provisional_findings"]) == 2


def test_confidence_evidence_beats_frequency():
    """The repeated-hallucination guard: a finding repeated by 3 chunks with NO
    parseable evidence must score BELOW one strong single-evidence finding."""
    hallucinated = {"title": "vague worry", "source": "json",
                    "chunks": [0, 1, 2], "evidence": ["somewhere in the app"]}
    strong_single = {"title": "real bug", "source": "json",
                     "chunks": [1], "evidence": ["auth.py:42"]}
    h_score, h_label = deep.confidence_of(hallucinated)
    s_score, s_label = deep.confidence_of(strong_single)
    assert s_score > h_score
    assert s_label == "high" and h_label == "low"        # 0.7 vs 0.3


def test_confidence_arithmetic_and_heuristic_cap():
    # two distinct locations + clean source + 2 chunks = 0.5+0.2+0.2+0.1 = 1.0
    full = {"source": "md_clean", "chunks": [0, 1],
            "evidence": ["a.py:1", "b.py:2"]}
    assert deep.confidence_of(full) == (1.0, "high")
    # heuristic source caps the LABEL at low regardless of evidence
    heur = {"source": "heuristic", "chunks": [0, 1],
            "evidence": ["a.py:1", "b.py:2"]}
    score, label = deep.confidence_of(heur)
    assert label == "low" and score >= 0.7               # score kept, label capped
    # duplicate evidence wording is ONE location ("a.py:1" == "a.py line 1")
    dup = {"source": "json", "chunks": [0],
           "evidence": ["a.py:1", "a.py line 1"]}
    assert deep.confidence_of(dup)[0] == 0.7


def test_render_report_orders_by_severity_then_confidence_and_shows_it():
    d = deep.empty_digest()
    deep.update_digest(d, {"findings": [
        {"title": "weak high", "severity": "high", "evidence": ["no ref here"]},
        {"title": "strong high", "severity": "high", "evidence": ["a.py:1"]},
        {"title": "strong critical", "severity": "critical",
         "evidence": ["c.py:3"]},
    ]}, 0)
    d = deep.compact_digest(d)
    report = deep._render_report(d, "gitaudit")
    i_crit = report.index("strong critical")
    i_strong = report.index("strong high")
    i_weak = report.index("weak high")
    assert i_crit < i_strong < i_weak
    assert "*(confidence: high)*" in report and "*(confidence: low)*" in report


def test_render_report_annotates_heuristic_notes():
    d = deep.empty_digest()
    d["markdown_notes"].append({"chunk": 0, "label": "core",
                                "md": "- salvaged line `x.py:1`",
                                "source": "heuristic"})
    report = deep._render_report(d, "gitaudit")
    assert "heuristic salvage" in report and "confidence: low" in report
    # clean notes carry no annotation
    d2 = deep.empty_digest()
    d2["markdown_notes"].append({"chunk": 0, "label": "core",
                                 "md": "- clean line", "source": "md_clean"})
    assert "heuristic salvage" not in deep._render_report(d2, "gitaudit")


def test_filter_min_severity_sections_and_bullets():
    from luxe.gitkit.store import filter_min_severity
    report = ("# Repository audit\n**Findings: 4**\n\n"
              "## Critical\n- **critical** `a.py:1` — takeover\n\n"
              "## Medium\n- **medium** `b.py:2` — leak\n- **medium** `c.py:3` — race\n\n"
              "## Structural improvements\n"
              "- **low** `d.py:4` — tidy\n- refactor note without severity\n")
    out, dropped = filter_min_severity(report, "high")
    assert "takeover" in out
    assert "leak" not in out and "race" not in out       # section dropped
    assert "tidy" not in out                             # inline bullet dropped
    assert "refactor note without severity" in out       # untagged lines kept
    assert dropped == 3
    # low threshold = show everything, zero dropped
    assert filter_min_severity(report, "low") == (report, 0)


def test_xref_json_carries_confidence_and_sources(big_repo, _gitkit_cfg,
                                                  monkeypatch):
    """Orchestration: the per-run xref.json digest carries provenance + the
    deterministic confidence after the final compaction."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)

    def fake_run_single(backend, role_cfg, *, run_id="", **kw):
        if "survey" in run_id:
            return _FakeResult("Survey notes.")
        if "synthesis" in run_id:
            return _FakeResult("# Repository audit\n**Findings: 1**\nok")
        return _FakeResult('```json\n{"findings": [{"title": "bug", '
                           '"severity": "high", "evidence": ["auth/m0.py:1"]}]}\n```')

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=big_repo,
                   console=_QuietConsole(), save=True, deep=True)
    work = sorted(store.reports_dir(big_repo).glob("gitaudit-*.work"))[-1]
    xref = json.loads((work / "xref.json").read_text())
    pf = xref["provisional_findings"]
    assert pf and all("source" in f and "chunks" in f for f in pf)


def test_heuristic_findings_vetoes_this_is_correct_lines():
    """Corpus-verified (re-render hand-compare 2026-06-10): the champion walks
    files emitting numbered '… — This is correct.' non-findings; veto them."""
    ramble = ("1. **`_check_auth` function** - The code checks the token. "
              "This is correct.\n"
              "2. **`_poll` mutates the list during iteration** - `bot.py:55` "
              "skips entries\n")
    out = deep._heuristic_findings(ramble)
    assert len(out) == 1 and "mutates" in out[0]


# --- Phase 4: incremental re-audit (cache v2) ---------------------------------

def _shas(d: dict[str, str]) -> dict[str, str]:
    return dict(d)


def _mk_chunks(*file_groups):
    out = []
    for i, files in enumerate(file_groups):
        out.append(deep.Chunk(index=i, files=list(files), label=f"g{i}",
                              est_tokens=100 * len(files), loc=10 * len(files)))
    return out


def test_plan_incremental_one_edit_dirties_exactly_one_chunk():
    chunks = _mk_chunks(["a.py", "b.py"], ["c.py", "d.py"])
    old = {"a.py": "1", "b.py": "2", "c.py": "3", "d.py": "4"}
    new = {"a.py": "1", "b.py": "2", "c.py": "3X", "d.py": "4"}
    plan = deep.plan_incremental(old_files=old, new_files=new, chunks=chunks,
                                 baseline=deep.make_baseline(chunks),
                                 added_recs=[], content_budget=1000)
    assert plan.mode == "incremental"
    assert plan.dirty == {1}
    assert [c.files for c in plan.chunks] == [["a.py", "b.py"], ["c.py", "d.py"]]


def test_plan_incremental_deletion_prunes_and_dirties():
    chunks = _mk_chunks(["a.py", "b.py"], ["c.py"])
    pad = {f"pad{i}.py": "p" for i in range(10)}          # stay under churn 20%
    old = {"a.py": "1", "b.py": "2", "c.py": "3", **pad}
    new = {"a.py": "1", "c.py": "3", **pad}               # b.py deleted
    plan = deep.plan_incremental(old_files=old, new_files=new, chunks=chunks,
                                 baseline=deep.make_baseline(chunks),
                                 added_recs=[], content_budget=1000)
    assert plan.mode == "incremental"
    assert plan.chunks[0].files == ["a.py"]               # pruned
    assert 0 in plan.dirty and 1 not in plan.dirty


def test_plan_incremental_added_file_appends_delta_chunk():
    # 5 original chunks so one delta chunk stays under the 25% growth trigger
    chunks = _mk_chunks(["a.py"], ["b.py"], ["c.py"], ["d.py"], ["e0.py"])
    pad = {f"pad{i}.py": "p" for i in range(10)}          # stay under churn 20%
    old = {"a.py": "1", "b.py": "2", "c.py": "3", "d.py": "4", "e0.py": "5", **pad}
    new = {**old, "fresh.py": "9"}
    plan = deep.plan_incremental(old_files=old, new_files=new, chunks=chunks,
                                 baseline=deep.make_baseline(chunks),
                                 added_recs=[_frec("fresh.py", tokens=50)],
                                 content_budget=1000)
    assert plan.mode == "incremental"
    assert len(plan.chunks) == 6
    delta = plan.chunks[5]
    assert delta.index == 5 and delta.files == ["fresh.py"]
    assert 5 in plan.dirty
    assert plan.baseline["delta_chunks"] == 1
    assert plan.baseline["delta_tokens"] == 50


def test_plan_incremental_framing_change_forces_rebuild():
    chunks = _mk_chunks(["a.py"])
    old = {"a.py": "1", "README.md": "r1"}
    new = {"a.py": "1", "README.md": "r2"}
    plan = deep.plan_incremental(old_files=old, new_files=new, chunks=chunks,
                                 baseline=deep.make_baseline(chunks),
                                 added_recs=[], content_budget=1000)
    assert plan.mode == "rebuild" and "framing" in plan.reason


def test_plan_incremental_churn_forces_rebuild():
    chunks = _mk_chunks(["a.py"])
    old = {f"f{i}.py": str(i) for i in range(10)}
    new = dict(old)
    for i in range(3):                                    # 3 deleted of 10 > 20%
        del new[f"f{i}.py"]
    plan = deep.plan_incremental(old_files=old, new_files=new, chunks=chunks,
                                 baseline=deep.make_baseline(chunks),
                                 added_recs=[], content_budget=1000)
    assert plan.mode == "rebuild" and "churn" in plan.reason


def test_plan_incremental_compaction_triggers_each_fire():
    chunks = _mk_chunks(["a.py"], ["b.py"], ["c.py"], ["d.py"])
    pad = {f"pad{i}.py": "p" for i in range(20)}          # stay under churn 20%
    old = {"a.py": "1", "b.py": "2", "c.py": "3", "d.py": "4", **pad}
    base = deep.make_baseline(chunks)                     # 400 corpus tokens

    # (1) cumulative delta chunks > 4
    bl = dict(base, delta_chunks=4)
    new = dict(old, **{"e.py": "5"})
    plan = deep.plan_incremental(old_files=old, new_files=new, chunks=chunks,
                                 baseline=bl, added_recs=[_frec("e.py", tokens=1)],
                                 content_budget=1000)
    assert plan.mode == "rebuild" and "delta chunks" in plan.reason

    # (2) cumulative delta content > 15% of original corpus tokens
    bl = dict(base, delta_tokens=50)
    plan = deep.plan_incremental(old_files=old, new_files=new, chunks=chunks,
                                 baseline=bl, added_recs=[_frec("e.py", tokens=20)],
                                 content_budget=1000)
    assert plan.mode == "rebuild" and "delta content" in plan.reason

    # (3) chunk count grown > 25% over the original partition
    bl = dict(base, orig_n_chunks=4)
    added = [_frec(f"n{i}.py", tokens=1) for i in range(2)]
    new2 = dict(old, **{f"n{i}.py": "x" for i in range(2)})
    plan = deep.plan_incremental(old_files=old, new_files=new2, chunks=chunks,
                                 baseline=bl, added_recs=added,
                                 content_budget=1)        # 1 file per delta chunk
    assert plan.mode == "rebuild" and "grew" in plan.reason

    # below every threshold → incremental
    plan = deep.plan_incremental(old_files=old, new_files=new, chunks=chunks,
                                 baseline=dict(base),
                                 added_recs=[_frec("e.py", tokens=1)],
                                 content_budget=1000)
    assert plan.mode == "incremental"


def test_chunk_note_validation_rejects_sha_mismatch_and_untracked():
    c = deep.Chunk(index=0, files=["a.py"], label="a")
    note = {"files": ["a.py"], "file_shas": {"a.py": "s1"},
            "contribution": {"parsed": {"findings": []}}}
    assert deep.chunk_note_is_valid(note, c, {"a.py": "s1"}) is True
    assert deep.chunk_note_is_valid(note, c, {"a.py": "s2"}) is False  # changed
    assert deep.chunk_note_is_valid(note, c, {}) is False              # untracked
    c2 = deep.Chunk(index=0, files=["a.py", "b.py"], label="a")
    assert deep.chunk_note_is_valid(note, c2, {"a.py": "s1"}) is False  # files moved


# --- Phase 4 orchestration: stale-leakage suite -------------------------------

@pytest.fixture
def bug_repo(tmp_path: Path) -> Path:
    """Repo whose files carry BUG: markers the content-reading stub turns into
    findings — leakage is detectable because findings track ACTUAL file state."""
    repo = tmp_path / "bugrepo"
    (repo / "auth").mkdir(parents=True)
    (repo / "core").mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    (repo / "auth" / "login.py").write_text(
        "def login():\n    pass  # BUG: token-not-checked\n" + "x = 1\n" * 30)
    (repo / "core" / "engine.py").write_text(
        "def run():\n    pass  # BUG: leak-in-engine\n" + "y = 2\n" * 30)
    (repo / "core" / "util.py").write_text("def u():\n    return 3\n" + "z = 3\n" * 30)
    # padding keeps single-file add/delete churn under the 20% rebuild trigger
    for i in range(8):
        (repo / "core" / f"pad{i}.py").write_text(f"p{i} = {i}\n" + "w = 0\n" * 30)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v1")
    return repo


def _content_stub(repo: Path, calls: list[str]):
    """Chunk passes emit one JSON finding per BUG: marker in the chunk's files
    (read from disk at call time — findings always reflect CURRENT content)."""
    import re as _re

    def fake(backend, role_cfg, *, run_id="", extra_context="", **kw):
        stage = _stage_of(run_id)
        calls.append(stage)
        if stage == "survey":
            return _FakeResult("Survey notes: python app.")
        if stage == "synthesis":
            return _FakeResult("# Repository audit\n**Findings: n**\nok")
        body = extra_context.split("<chunk_files>")[1].split("Symbols defined")[0]
        files = [m.strip() for m in _re.findall(r"^- (.+)$", body, _re.M)]
        findings = []
        for rel in files:
            p = repo / rel
            if not p.is_file():
                continue
            for ln in p.read_text().splitlines():
                if "BUG:" in ln:
                    findings.append({"title": ln.split("BUG:")[1].strip(),
                                     "severity": "high",
                                     "evidence": [f"{rel}:1"]})
        return _FakeResult("```json\n" + json.dumps({"findings": findings}) + "\n```")
    return fake


def _latest_xref(repo: Path) -> dict:
    # same-second runs share the name timestamp — mtime is the real order
    from luxe.gitkit import store
    work = max(store.reports_dir(repo).glob("gitaudit-*.work"),
               key=lambda p: (p / "xref.json").stat().st_mtime_ns)
    return json.loads((work / "xref.json").read_text())


def _finding_titles(xref: dict) -> set[str]:
    return {f["title"] for f in xref["provisional_findings"]}


def test_incremental_no_stale_finding_after_fix_commit(bug_repo, _gitkit_cfg,
                                                       monkeypatch):
    """(i) A finding sourced from a changed file's chunk must NOT reappear via
    the cache after the fix is committed — and only that chunk re-runs."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _content_stub(bug_repo, calls))

    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert _finding_titles(_latest_xref(bug_repo)) == {"token-not-checked",
                                                       "leak-in-engine"}
    n_chunks_full = len([c for c in calls if c.startswith("chunk")])
    assert n_chunks_full >= 3

    # fix the engine bug and commit
    f = bug_repo / "core" / "engine.py"
    f.write_text(f.read_text().replace("# BUG: leak-in-engine", "# fixed"))
    _git(bug_repo, "add", "-A")
    _git(bug_repo, "commit", "-q", "-m", "fix engine leak")

    calls.clear()
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True)
    titles = _finding_titles(_latest_xref(bug_repo))
    assert "leak-in-engine" not in titles          # NO stale leakage
    assert "token-not-checked" in titles           # surviving note still folded
    assert len([c for c in calls if "survey" in c]) == 0       # survey kept
    assert len([c for c in calls if c.startswith("chunk")]) == 1  # dirty only
    assert len([c for c in calls if "synthesis" in c]) == 1    # always re-runs


def test_incremental_multi_generation_equals_from_scratch(bug_repo, _gitkit_cfg,
                                                          monkeypatch):
    """(ii) Two incremental generations end with a digest equal to a full
    from-scratch run's (surviving + fresh contributions, nothing else)."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _content_stub(bug_repo, calls))

    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True)

    # gen 1: fix auth bug
    f1 = bug_repo / "auth" / "login.py"
    f1.write_text(f1.read_text().replace("# BUG: token-not-checked", "# ok"))
    _git(bug_repo, "add", "-A")
    _git(bug_repo, "commit", "-q", "-m", "fix auth")
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True)

    # gen 2: introduce a NEW bug in util
    f2 = bug_repo / "core" / "util.py"
    f2.write_text(f2.read_text() + "# BUG: util-overflow\n")
    _git(bug_repo, "add", "-A")
    _git(bug_repo, "commit", "-q", "-m", "introduce util bug")
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True)
    incr_titles = _finding_titles(_latest_xref(bug_repo))

    # from-scratch reference at the SAME state
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True,
                   rebuild_map=True)
    scratch_titles = _finding_titles(_latest_xref(bug_repo))
    assert incr_titles == scratch_titles == {"leak-in-engine", "util-overflow"}


def test_incremental_new_file_gets_delta_chunk_analyzed(bug_repo, _gitkit_cfg,
                                                        monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _content_stub(bug_repo, calls))

    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True)
    (bug_repo / "core" / "newmod.py").write_text(
        "def n():\n    pass  # BUG: new-module-bug\n" + "n = 4\n" * 30)
    _git(bug_repo, "add", "-A")
    _git(bug_repo, "commit", "-q", "-m", "add newmod")

    calls.clear()
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert "new-module-bug" in _finding_titles(_latest_xref(bug_repo))
    assert len([c for c in calls if c.startswith("chunk")]) == 1  # delta only
    assert len([c for c in calls if "survey" in c]) == 0


def test_incremental_framing_change_triggers_full_rebuild(bug_repo, _gitkit_cfg,
                                                          monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _content_stub(bug_repo, calls))

    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True)
    (bug_repo / "README.md").write_text("# new architecture docs\n")
    _git(bug_repo, "add", "-A")
    _git(bug_repo, "commit", "-q", "-m", "add README (framing)")

    calls.clear()
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert len([c for c in calls if "survey" in c]) == 1     # full rebuild


def test_v1_breadcrumb_takes_full_path_not_incremental(bug_repo, _gitkit_cfg,
                                                       monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _content_stub(bug_repo, calls))

    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True)
    # downgrade the breadcrumb to v1 (pre-incremental schema)
    bc_path = deep._map_dir(bug_repo) / "mapped.json"
    bc = json.loads(bc_path.read_text())
    bc["version"] = 1
    bc.pop("files", None)
    bc_path.write_text(json.dumps(bc))

    (bug_repo / "core" / "util.py").write_text("def u():\n    return 9\n")
    _git(bug_repo, "add", "-A")
    _git(bug_repo, "commit", "-q", "-m", "edit util")

    calls.clear()
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert len([c for c in calls if "survey" in c]) == 1     # full path


def test_aborted_chunk_pass_is_never_cached(bug_repo, _gitkit_cfg, monkeypatch):
    """Cache-poisoning guard (found live 2026-06-10: an oMLX prefill-guard
    outage made every pass return empty+aborted, and the empty notes would
    have been reused by sha on re-run): aborted passes must not write notes."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    _stub_backend(monkeypatch)

    def aborted_stub(backend, role_cfg, *, run_id="", **kw):
        r = _FakeResult("" if "chunk" in run_id else
                        "# Repository audit\n**Findings: 0**\nok"
                        if "synthesis" in run_id else "Survey notes.")
        r.aborted = "chunk" in run_id
        return r

    monkeypatch.setattr(single_mod, "run_single", aborted_stub)
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True)
    # no notes were cached for the aborted chunk passes…
    assert not list(deep._notes_dir(bug_repo, "gitaudit").glob("chunk-*.json"))

    # …so a healthy re-run re-analyzes every chunk (no poisoned reuse)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single", _content_stub(bug_repo, calls))
    run_git_report("gitaudit", cfg=_gitkit_cfg, repo_path=bug_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert len([c for c in calls if c.startswith("chunk")]) >= 3
    assert _finding_titles(_latest_xref(bug_repo)) == {"token-not-checked",
                                                       "leak-in-engine"}
