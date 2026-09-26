#!/usr/bin/env python
"""Run the maintain_suite fixtures through the opencode CLI agent and grade
them with luxe's own grader — a harness A/B on identical tasks, same local
model, same engine.

    uv run python scripts/opencode_harness.py --model Qwen3.6-35B-A3B-4bit \\
        --output acceptance/opencode_ab_<date> --all
    uv run python scripts/opencode_harness.py --model Qwen3.6-35B-A3B-4bit \\
        --output /tmp/oc1 --id isomer-document-quickstart --timeout 1200

What is held equal with `benchmarks.maintain_suite.run`:
  * repo prep — `_resolve_repo` (clone / reset to base_sha / clean, plus the
    synthetic forbids_create `.sdd` excluded via .git/info/exclude);
  * task text — `fixture.goal`, verbatim, plus ONE neutral line
    (`NEUTRAL_LINE`); luxe's own persona/system prompt is NOT given to
    opencode — each harness brings its own scaffold, that is the comparison;
  * grading — `grade.grade_fixture` against base_sha..HEAD (commits, so the
    driver commits opencode's working-tree changes afterwards);
  * temperature 0 (luxe's bench temperature).

What is NOT equal, by construction: opencode opens no PR, so the 1-pt
pr_opened criterion is always 0 here. opencode emits no luxe citations, so
the citation criterion is graded on 0/0 — which grade.py credits (+1, "no
citations"). Max reachable score is therefore 4/5; compare harnesses on
`outcome_points` (0 or 3), recorded separately in every result.json and in
summary.json.

Isolation: every run gets its own opencode config (OPENCODE_CONFIG) and its
own HOME + XDG_{DATA,CONFIG,CACHE,STATE}_HOME under <output>/_opencode_env/,
so it reads neither the user's global opencode config/auth nor ~/.claude
skills/CLAUDE.md (verified: with the real HOME, `opencode debug skill` lists
the user's ~/.claude skills), and it never writes the user's session DB.
OPENCODE_DISABLE_MODELS_FETCH=1 stops the models.dev catalog download
(verified on v1.18.30: no models.json appears in the cache). autoupdate and
share are off in the generated config.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from benchmarks.maintain_suite.fixtures import (  # noqa: E402
    load_fixtures as _load_fixtures,
    remap_repo_url,  # noqa: F401 — re-exported; one remap for every reader
)
from benchmarks.maintain_suite.grade import Fixture, grade_fixture  # noqa: E402

PROVIDER_ID = "luxeab"
API_KEY_ENV_IN_CHILD = "LUXEAB_API_KEY"  # what the generated config reads
CONTEXT_LIMIT = 65536
OUTPUT_LIMIT = 8192
TEMPERATURE = 0.0
# run.py's --per-fixture-timeout defaults to None (no cap) and its help text
# recommends 1200; an unattended third-party agent needs a cap, so use the
# recommended value as the default.
DEFAULT_TIMEOUT_S = 1200.0
DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
DEFAULT_WORK_DIR = Path.home() / ".luxe" / "opencode-bench-workspace"
NEUTRAL_LINE = ("You are working in the repository at the current directory. "
                "Make the change directly in the files; do not open a pull request.")
GIT_IDENT = ["-c", "user.name=opencode-harness",
             "-c", "user.email=opencode-harness@localhost",
             "-c", "commit.gpgsign=false"]
# Harness-owned paths opencode may drop into a project; never part of the
# agent's diff. Added to .git/info/exclude like the synthetic .sdd.
EXCLUDE_ENTRIES = [".opencode/"]

STATUSES = ("pending", "running", "done", "error", "skipped")


# --- pure pieces (tested) --------------------------------------------------

def build_prompt(fixture: Fixture) -> str:
    """The exact text opencode receives: the fixture goal luxe gets
    (`luxe maintain <repo> <goal>`) plus one neutral line."""
    return f"{fixture.goal.strip()}\n\n{NEUTRAL_LINE}"


def make_opencode_config(model: str, base_url: str) -> dict:
    """Per-run opencode.json: one OpenAI-compatible provider, one model."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "provider": {
            PROVIDER_ID: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "luxe A/B (local)",
                "options": {
                    "baseURL": base_url,
                    "apiKey": "{env:" + API_KEY_ENV_IN_CHILD + "}",
                },
                "models": {
                    model: {
                        "name": model,
                        "tool_call": True,
                        "limit": {"context": CONTEXT_LIMIT, "output": OUTPUT_LIMIT},
                        "options": {"temperature": TEMPERATURE},
                    },
                },
            },
        },
        "model": f"{PROVIDER_ID}/{model}",
    }


def iso_dirs(env_root: Path) -> dict[str, Path]:
    return {name: env_root / name for name in ("home", "data", "config", "cache", "state")}


def make_env(base_env: dict[str, str], env_root: Path, config_path: Path,
             api_key: str) -> dict[str, str]:
    """Child env: isolated HOME/XDG, our config, key under a private name.
    PATH etc. are inherited so opencode's bash tool finds the toolchain."""
    d = iso_dirs(env_root)
    env = dict(base_env)
    env.update({
        "HOME": str(d["home"]),
        "XDG_DATA_HOME": str(d["data"]),
        "XDG_CONFIG_HOME": str(d["config"]),
        "XDG_CACHE_HOME": str(d["cache"]),
        "XDG_STATE_HOME": str(d["state"]),
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
        API_KEY_ENV_IN_CHILD: api_key or "sk-no-key",
        # same override run.py applies to luxe: a global gpgsign without a
        # key must not block any commit the agent attempts
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "commit.gpgsign",
        "GIT_CONFIG_VALUE_0": "false",
    })
    # never let an inherited config/key steer the child
    for k in ("OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT"):
        env.pop(k, None)
    return env


def opencode_cmd(model: str, repo: Path, prompt: str) -> list[str]:
    return ["opencode", "run", prompt, "--model", f"{PROVIDER_ID}/{model}",
            "--dir", str(repo), "--format", "json", "--auto"]


def parse_events(lines: Iterable[str]) -> dict[str, Any]:
    """Digest an `opencode run --format json` stream (one JSON object/line).

    tool_use parts carry part.tool + part.state.status; step_finish parts
    carry part.tokens {input, output, reasoning, cache{read,write}}.
    Malformed lines are counted, not fatal."""
    tools: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0,
              "cache_write": 0}
    session_id = ""
    steps = 0
    bad = 0
    finish_reasons: Counter[str] = Counter()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(ev, dict):
            bad += 1
            continue
        session_id = session_id or str(ev.get("sessionID") or "")
        part = ev.get("part") or {}
        typ = ev.get("type")
        if typ == "tool_use":
            name = str(part.get("tool") or "?")
            tools[name] += 1
            status = (part.get("state") or {}).get("status")
            if status and status != "completed":
                tool_errors[name] += 1
        elif typ == "step_finish":
            steps += 1
            finish_reasons[str(part.get("reason") or "?")] += 1
            t = part.get("tokens") or {}
            tokens["input"] += int(t.get("input") or 0)
            tokens["output"] += int(t.get("output") or 0)
            tokens["reasoning"] += int(t.get("reasoning") or 0)
            c = t.get("cache") or {}
            tokens["cache_read"] += int(c.get("read") or 0)
            tokens["cache_write"] += int(c.get("write") or 0)
    return {
        "session_id": session_id,
        "tool_calls": dict(tools),
        "tool_calls_total": sum(tools.values()),
        "tool_errors": dict(tool_errors),
        "steps": steps,
        "finish_reasons": dict(finish_reasons),
        "tokens": tokens,
        "malformed_lines": bad,
    }


def outcome_points(result_dict: dict) -> int:
    for c in result_dict.get("criteria_breakdown") or []:
        if str(c.get("criterion", "")).startswith("expected_outcome"):
            return int(c.get("earned") or 0)
    return 0


def load_state(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        d = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return d if isinstance(d, dict) else {}


def save_state(path: Path, state: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def pending_ids(ids: list[str], state: dict[str, dict]) -> list[str]:
    """Resume: everything not DONE/SKIPPED re-runs (RUNNING = crashed)."""
    return [i for i in ids
            if (state.get(i) or {}).get("status") not in ("done", "skipped")]


def fmt_progress(i: int, n: int, fid: str, *, score: int, outcome: int,
                 wall: float, run_score: int, run_max: int, run_outcome: int,
                 run_outcome_max: int, walls: list[float], left: int,
                 timed_out: bool) -> str:
    """bfcl/run.py shape: position, per-item result, running rate, then a
    global ETA from the rolling mean of completed fresh walls."""
    to = " TIMEOUT" if timed_out else ""
    s = (f"  [{i}/{n}] {fid} score={score}/5 outcome={outcome}/3 "
         f"wall={wall:.0f}s{to} | running score={run_score}/{run_max} "
         f"outcome={run_outcome}/{run_outcome_max}")
    if walls and left > 0:
        avg = sum(walls) / len(walls)
        s += f" | global {left} left avg={avg:.0f}s total_eta={avg * left / 60:.1f}m"
    return s


# --- side-effecting pieces -------------------------------------------------

def _git(args: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=check)


def _exclude(repo: Path, entries: list[str]) -> None:
    ex = repo / ".git" / "info" / "exclude"
    ex.parent.mkdir(parents=True, exist_ok=True)
    have = ex.read_text().splitlines() if ex.is_file() else []
    add = [e for e in entries if e not in have]
    if add:
        with ex.open("a") as fh:
            fh.write("\n# opencode harness artefacts\n" + "\n".join(add) + "\n")


def commit_changes(repo: Path) -> bool:
    """Commit whatever opencode left in the working tree so grade.py's
    base_sha..HEAD reading sees it. Returns True if a commit was made."""
    st = _git(["status", "--porcelain"], repo)
    if not st.stdout.strip():
        return False
    _git([*GIT_IDENT, "add", "-A"], repo)
    r = _git([*GIT_IDENT, "commit", "-q", "--no-verify", "-m",
              "opencode harness: agent changes"], repo)
    return r.returncode == 0


def run_opencode(cmd: list[str], env: dict, cwd: Path, out_path: Path,
                 err_path: Path, timeout_s: float) -> tuple[int, bool]:
    """Run in its own process group; on timeout kill the whole group.
    Returns (exit_code, timed_out)."""
    with out_path.open("w") as out, err_path.open("w") as err:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=err,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            return p.wait(timeout=timeout_s), False
        except subprocess.TimeoutExpired:
            for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 10)):
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    p.wait(timeout=grace)
                    break
                except subprocess.TimeoutExpired:
                    continue
            err.write(f"\n[opencode_harness] killed after {timeout_s:.0f}s\n")
            return 124, True


def opencode_version() -> str:
    try:
        r = subprocess.run(["opencode", "--version"], capture_output=True,
                           text=True, timeout=30)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def export_session(sid: str, env: dict, cwd: Path, dest: Path) -> bool:
    if not sid:
        return False
    try:
        r = subprocess.run(["opencode", "export", sid], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0 or not r.stdout.strip():
        return False
    dest.write_text(r.stdout)
    return True


def load_fixtures() -> list[Fixture]:
    """The shared, host-remapping loader (benchmarks/maintain_suite/fixtures.py)."""
    return _load_fixtures()


def resolve_key(env_name: str) -> str:
    """luxe's order: env → ~/.luxe/secrets.env → keychain. Never printed."""
    try:
        from luxe.secrets import resolve_api_key
        return resolve_api_key(env_name) or ""
    except Exception:  # noqa: BLE001 — a missing key means a dummy, not a crash
        return os.environ.get(env_name, "")


@dataclass
class RunCtx:
    model: str
    base_url: str
    output: Path
    work_dir: Path
    timeout_s: float
    env: dict
    config_path: Path
    walls: list[float] = field(default_factory=list)


def run_one(fx: Fixture, ctx: RunCtx) -> dict:
    """Prep → opencode → commit → grade. Returns the result.json dict."""
    from benchmarks.maintain_suite.run import _resolve_repo

    fdir = ctx.output / fx.id
    fdir.mkdir(parents=True, exist_ok=True)
    repo, err = _resolve_repo(fx, ctx.work_dir)
    if repo is None:
        raise RuntimeError(f"repo prep failed: {err}")
    _exclude(repo, EXCLUDE_ENTRIES)
    base_sha = fx.base_sha or _git(["rev-parse", "HEAD"], repo).stdout.strip()
    prompt = build_prompt(fx)
    cmd = opencode_cmd(ctx.model, repo, prompt)

    t0 = time.time()
    rc, timed_out = run_opencode(cmd, ctx.env, repo, fdir / "opencode.jsonl",
                                 fdir / "stderr.log", ctx.timeout_s)
    wall = time.time() - t0

    with (fdir / "opencode.jsonl").open() as fh:
        digest = parse_events(fh)
    exported = export_session(digest["session_id"], ctx.env, repo,
                              fdir / "export.json")
    committed = commit_changes(repo)
    head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    (fdir / "diff.patch").write_text(_git(["diff", base_sha, "HEAD"], repo).stdout)

    res = grade_fixture(fx, repo, pr_url="", pr_opened=False,
                        citations_unresolved=0, citations_total=0,
                        base_sha=base_sha).to_dict()
    res["outcome_points"] = outcome_points(res)
    res["outcome_max"] = 3
    res["harness"] = "opencode"
    (fdir / "result.json").write_text(json.dumps(res, indent=2))

    meta = {
        "fixture_id": fx.id,
        "task_type": fx.task_type,
        "prompt": prompt,
        "command": cmd[:2] + ["<prompt>"] + cmd[3:],
        "repo": str(repo),
        "base_sha": base_sha,
        "head_sha": head,
        "harness_committed": committed,
        "wall_s": round(wall, 2),
        "exit_code": rc,
        "timed_out": timed_out,
        "timeout_s": ctx.timeout_s,
        "session_export": "export.json" if exported else None,
        **digest,
    }
    (fdir / "meta.json").write_text(json.dumps(meta, indent=2))
    return {"result": res, "meta": meta}


def write_summary(ctx: RunCtx, ids: list[str], state: dict, extra: dict) -> dict:
    rows = []
    tot = {"score": 0, "max_score": 0, "outcome_points": 0, "outcome_max": 0,
           "passed": 0, "timed_out": 0, "done": 0, "error": 0, "wall_s": 0.0}
    for fid in ids:
        st = (state.get(fid) or {}).get("status", "pending")
        row: dict[str, Any] = {"fixture_id": fid, "status": st}
        rp, mp = ctx.output / fid / "result.json", ctx.output / fid / "meta.json"
        if st == "done" and rp.is_file() and mp.is_file():
            r, m = json.loads(rp.read_text()), json.loads(mp.read_text())
            row.update(score=r["score"], outcome_points=r["outcome_points"],
                       diff_produced=r["diff_produced"],
                       gates=[g.get("name") or g.get("gate", "?")
                              for g in r.get("gates_triggered") or []],
                       wall_s=m["wall_s"], timed_out=m["timed_out"],
                       exit_code=m["exit_code"],
                       tool_calls_total=m["tool_calls_total"],
                       tokens=m["tokens"])
            tot["score"] += r["score"]
            tot["max_score"] += 5
            tot["outcome_points"] += r["outcome_points"]
            tot["outcome_max"] += 3
            tot["passed"] += int(r["outcome_points"] == 3)
            tot["timed_out"] += int(bool(m["timed_out"]))
            tot["done"] += 1
            tot["wall_s"] += m["wall_s"]
        elif st == "error":
            tot["error"] += 1
            row["error"] = (state.get(fid) or {}).get("last_error", "")
        rows.append(row)
    tot["wall_s"] = round(tot["wall_s"], 1)
    summary = {**extra, "totals": tot, "fixtures": rows,
               "scoring_note": ("pr_opened is always 0 (opencode opens no PR); "
                                "citations graded on 0/0 (+1). Max 4/5 — "
                                "compare on outcome_points.")}
    (ctx.output / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="model id on the server, e.g. Qwen3.6-35B-A3B-4bit")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--api-key-env", default="OMLX_API_KEY")
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    ap.add_argument("--id", action="append", default=[], dest="ids")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help=f"per-fixture wall cap, seconds (default {DEFAULT_TIMEOUT_S:.0f})")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)

    fixtures = {f.id: f for f in load_fixtures()}
    if args.all == bool(args.ids):
        ap.error("pass exactly one of --all or --id")
    unknown = [i for i in args.ids if i not in fixtures]
    if unknown:
        ap.error(f"unknown fixture id(s): {', '.join(unknown)}")
    ids = list(fixtures) if args.all else args.ids

    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    work_dir = args.work_dir.expanduser().resolve()
    env_root = output / "_opencode_env"
    for d in iso_dirs(env_root).values():
        d.mkdir(parents=True, exist_ok=True)
    config = make_opencode_config(args.model, args.base_url)
    config_path = output / "opencode.config.json"
    config_path.write_text(json.dumps(config, indent=2))
    key = resolve_key(args.api_key_env)
    env = make_env(os.environ.copy(), env_root, config_path, key)
    version = opencode_version()

    ctx = RunCtx(model=args.model, base_url=args.base_url, output=output,
                 work_dir=work_dir, timeout_s=args.timeout, env=env,
                 config_path=config_path)
    extra = {
        "harness": "opencode", "label": args.label, "model": args.model,
        "base_url": args.base_url, "opencode_version": version,
        "api_key_env": args.api_key_env, "api_key_present": bool(key),
        "config": config, "config_path": str(config_path),
        "limits": {"context": CONTEXT_LIMIT, "output": OUTPUT_LIMIT},
        "temperature": TEMPERATURE, "timeout_s": args.timeout,
        "work_dir": str(work_dir), "neutral_line": NEUTRAL_LINE,
        "isolation": {"env_root": str(env_root),
                      "vars": ["HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                               "XDG_CACHE_HOME", "XDG_STATE_HOME",
                               "OPENCODE_CONFIG", "OPENCODE_DISABLE_MODELS_FETCH",
                               "OPENCODE_DISABLE_AUTOUPDATE",
                               "OPENCODE_DISABLE_LSP_DOWNLOAD"]},
    }

    state_path = output / "state.json"
    state = load_state(state_path)
    todo = pending_ids(ids, state)
    print(f"opencode harness: {len(ids)} fixture(s), {len(todo)} to run "
          f"(resume skips {len(ids) - len(todo)} done) | model={args.model} "
          f"base_url={args.base_url} opencode={version or '?'} "
          f"key={'set' if key else 'unset (dummy)'} timeout={args.timeout:.0f}s",
          flush=True)

    if args.dry_run:
        for fid in todo:
            fx = fixtures[fid]
            print(f"  [dry-run] {fid} ({fx.task_type}, "
                  f"{fx.expected_outcome.get('kind')}) base={fx.base_sha[:10]}")
            print("    prompt: " + build_prompt(fx).replace("\n", "\n            "))
        print(f"  config written: {config_path}")
        return 0

    for fid in ids:
        state.setdefault(fid, {"status": "pending", "attempts": 0})
    save_state(state_path, state)

    run_score = run_max = run_out = run_out_max = 0
    for fid in ids:  # count resumed DONE fixtures into the running totals
        if fid not in todo and state[fid].get("status") == "done":
            rp = output / fid / "result.json"
            if rp.is_file():
                r = json.loads(rp.read_text())
                run_score += r["score"]; run_max += 5
                run_out += r.get("outcome_points", 0); run_out_max += 3

    n = len(todo)
    for i, fid in enumerate(todo, 1):
        fx = fixtures[fid]
        missing = [e for e in fx.required_env if not os.environ.get(e)]
        if missing:
            state[fid].update(status="skipped", last_error=f"missing env: {missing}")
            save_state(state_path, state)
            print(f"  [{i}/{n}] {fid} SKIPPED (missing env {missing})", flush=True)
            continue
        state[fid].update(status="running", attempts=state[fid].get("attempts", 0) + 1,
                          last_attempt_ts=time.time(), last_error="")
        save_state(state_path, state)
        print(f"  [{i}/{n}] {fid} START ({fx.task_type})", flush=True)
        try:
            out = run_one(fx, ctx)
        except Exception as e:  # noqa: BLE001 — record and move on
            state[fid].update(status="error", last_error=f"{type(e).__name__}: {e}"[:500])
            save_state(state_path, state)
            print(f"  [{i}/{n}] {fid} ERROR {type(e).__name__}: {e}", flush=True)
            continue
        r, m = out["result"], out["meta"]
        state[fid].update(status="done")
        save_state(state_path, state)
        ctx.walls.append(m["wall_s"])
        run_score += r["score"]; run_max += 5
        run_out += r["outcome_points"]; run_out_max += 3
        print(fmt_progress(i, n, fid, score=r["score"], outcome=r["outcome_points"],
                           wall=m["wall_s"], run_score=run_score, run_max=run_max,
                           run_outcome=run_out, run_outcome_max=run_out_max,
                           walls=ctx.walls, left=n - i, timed_out=m["timed_out"])
              + f" tools={m['tool_calls_total']}", flush=True)
        write_summary(ctx, ids, state, extra)

    s = write_summary(ctx, ids, state, extra)
    t = s["totals"]
    print(f"done: score={t['score']}/{t['max_score']} outcome={t['outcome_points']}/"
          f"{t['outcome_max']} passed(outcome)={t['passed']}/{t['done']} "
          f"errors={t['error']} timeouts={t['timed_out']} wall={t['wall_s']:.0f}s "
          f"-> {output / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
