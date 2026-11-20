# SWE-bench image exec (lever 1b/c/d): plan and gates

Status: implemented on `feat/image-exec`; approved 2026-11-20. G1 classify done (below); G1 exec, G2, G3 pending.

## Context

`acceptance/swebench/diag_misses_2026_11/REPORT.md`: on SWE-bench the champion never sees a test
or repro result on its own patch. Four stacked blockers: (a) the loop hides failing output (fixed
by LUXE_FAILED_OUTPUT, landed 9b48595, opt-in); (b) model bash runs in the host venv (Py 3.15, none
of the target deps), and gold F2P tests collect for **1/46 on the host vs 40/46 in the instance
image** at ~7.5 s per call (`feasibility.md`); (c) 70/124 commands start with `cd <path> &&` and are
refused as a chain; (d) the adapter's `.sdd` forbids creating `test_*.py`/`verify_*.py`/`tmp_*.py`,
so no reproducer can exist. You approved (b), (c), (d) for the bench path only. This plan implements
them behind one switch the SWE-bench adapter alone sets, then A/Bs them with LUXE_FAILED_OUTPUT on
`fo_slice_2026_11.json` (25 ids: 13 flip targets + 12 no-harm; all 25 have a local image).

**Ordering constraint:** the maintain no-harm A/B (step 2) is running on the GPU now (launched
09:32, ~4-5 h). Implementation and unit tests do not touch the GPU and go first. Nothing that
calls the model (smoke, slice) starts until arms.log shows `DONE champ`.

## Design

One env switch, `LUXE_SWEBENCH_EXEC=<image ref>` (no value = off, byte-identical). Set only by
`benchmarks/swebench/adapter.py` when `run.py --image-exec` is passed. maintain_suite, chat and
the default bench path never set it; `tools.sdd` gets a Must-not saying so.

### (b) Run execution commands in the instance image: `src/luxe/tools/shell.py`
- In `_bash`, after validation and the allowlist check, if the switch is set and `tokens[0]` is
  in `{python, pytest, pip}` (the ones that execute repo code; `git/grep/ls/find/cat/...` stay on the
  host, which is faster and equivalent), call a new `_run_in_image(command, image, repo_root, cwd_rel)`.
- `_run_in_image`: `docker run --rm -i --platform linux/amd64 --network none --memory 6g <image>
  bash -c '<sync>; cd /testbed/<cwd_rel> && source /opt/miniconda3/bin/activate testbed && <command>'`,
  with `DOCKER_HOST` pointed at colima. Sync: a tar of the files that differ from `base_commit`
  (tracked changes via `git diff --name-only <base>`, plus untracked from
  `git ls-files --others --exclude-standard`, minus the synthetic `.sdd`) streamed on stdin and
  extracted over `/testbed` (already at base_commit in the eval image). Deleted files are passed as a
  list and `rm -f`'d. The index is not touched (unlike `extract_diff`'s `git add -N`).
- Fresh container per call: stateless and deterministic, matching what feasibility measured (0.17 s
  start, ~7.5 s median incl. collection). Timeout is 180 s for image calls (emulated amd64
  collection hit 29.8 s max), killed via `docker kill` on the named container. The host's 60 s
  stays for everything else.
- The container gets no host env (stronger than `scrubbed_env`) and no network.
- Output goes through the same `_clip_output`, with the same `(output, "exit code N")` contract, so
  FAILED_OUTPUT and abortreport see an identical shape. A first line `[ran in <short image> at
  /testbed]` tells the model where it ran (the paths in tracebacks are `/testbed/...`).
- Loud failure: if docker is unreachable or the image is missing, return
  `("", "image exec unavailable: <reason>")` and emit `image_exec_error`. Never fall back to the
  host silently. Adapter preflight: `docker image inspect` for every subset id before the arm
  starts; refuse to start if any is missing.
- `base_commit` reaches the tool via a second env var `LUXE_SWEBENCH_BASE=<sha>` (adapter-set).

### (c) Leading `cd <path> &&`: same file
- Under the switch only: before `_validate_command`, match `^\s*cd\s+(\S+)\s*&&\s*(.+)$`. If the
  target resolves inside the repo (relative, `/testbed/...`, or `<abs repo root>/...`), run the
  rest with that cwd. If it is outside or nonexistent (the hallucinated `/home/user/repo`), run at
  repo root. Prepend a note line `[luxe ran `<rest>` from <dir>: leading cd handled]`, the same
  convention as the noop-strip note. Then validate the remainder normally, so a second chain is
  still refused.
- The report's (c) also named `LUXE_BASH_NOOP=strip` (56/124 commands carry `| tail` or `2>&1`).
  **Decided: set it in the ix arm** (the existing mechanism, applied after the cd rescue).

### (d) Scratch reproducers: `benchmarks/swebench/adapter.py`
- Under `--image-exec`: write the `.sdd` without the create-only scaffolding globs (keep
  `repo_root/**`). Tell the model in the goal suffix (via `prompts.py`, per agents.sdd) that it may
  create scratch scripts and that they will not be part of the patch.
- Before `extract_diff`: delete **untracked** files the run created that match the scratch globs
  (the current Forbids-creating list plus `repro*.py`, `reproduce*.py`, `scratch*`, `debug_*.py`)
  or that are new `*.py` at the repo root. Record each stripped path in the per-instance result
  (`scratch_stripped`). Edits to tracked files are never stripped. New files under package dirs
  that do not match a glob are kept (real new modules).

### Telemetry (needed for the gates)
`image_exec` event per call (binary, rc, wall_s, bytes_out, cd_rescued), `image_exec_error`,
and per-instance `scratch_stripped`. Extend `acceptance/swebench/diag_misses_2026_11/classify.py`
(copy into the new dir) with `image_calls`, `testish_ran_visible`, `testish_before_first_write`.

## Files
- `src/luxe/tools/shell.py`: `_run_in_image`, cd rescue, routing in `_bash` (switch-gated)
- `src/luxe/tools/tools.sdd`: invariant (bench-only, never in maintain/chat, no host fallback)
- `benchmarks/swebench/adapter.py`, `run.py`: `--image-exec`, env vars, preflight, reduced sdd,
  scratch strip
- `src/luxe/agents/prompts.py`: one goal-suffix variant for image exec
- `tests/test_swebench_image_exec.py`: unit tests with a fake `docker` on PATH (records argv and
  stdin, returns canned rc/output). The suite never needs colima.
- `acceptance/swebench/image_exec_2026_11/`: PLAN.md, run_slice.sh (from
  `failed_output_ab_2026_11/swebench_slice.sh`), analyze/classify, REPORT.md

## Verification and gates

**G0, unit (no model):** RED on main first (the new tests fail), then GREEN. Off-switch goldens are
unchanged (`tests/test_golden*`). Full suite is green on the branch. Then CI and a signed fast-forward
land, same as #61.

**G1, offline replay (no model, minutes):** feed all 124 bash commands from the diag_misses control
rep (`messages.jsonl.gz`) through the new validator path, then execute every resulting
python/pytest command in its instance image on that run's final working tree.
- Report how many go from refused to run, and how many tests reach the testbed.
- Abort if fewer than 40% of the refused commands become runnable. That means the remaining blocker
  is something else (newlines, other chains); stop and report.

**G2, live smoke (after step 2's DONE):** 3 instances, 1 rep, arm on: requests-2931 (D),
astropy-14182 (C, runnable repro), pytest-5262 (no-harm, 8/9 prior; the plan draft named pytest-10051, which is a flip target). Subset `smoke_g2.json`, `SUB=$PWD/smoke_g2.json ARMS=ix TAG=smoke run_slice.sh 1`. Read every transcript.
- Pass requires at least one test or repro output the model read, 0 `image_exec_error`, and the
  stripped scratch files absent from the patch.
- Abort on any host fallback or a patch containing a scratch file.

**G3, slice A/B:** `fo_slice_2026_11.json`, 3 reps interleaved, graded after all predictions.
**Decided arms:** `control` (shipped), `fo` (`LUXE_FAILED_OUTPUT=1`), `ix` (`--image-exec` +
`LUXE_FAILED_OUTPUT=1` + `LUXE_BASH_NOOP=strip`). This supersedes the separate 2-arm Part 2 in
`failed_output_ab_2026_11/PLAN.md`: its control and fo arms are these. Estimated GPU time 7-9 h.

| gate | threshold |
|---|---|
| flips vs 9-rep prior (13 flip targets) | report; success = ix arm resolves more flip-target runs than control and fo |
| no-harm (12 resolved) | ix at most 1 net loss across 3 reps vs control |
| every new PASS | read by hand |
| wall | reported (expect +2-4 min per instance); abort if median > 1800 s timeout risk |
| liveness | image_exec calls > 0 in every ix run; 0 in control/fo |

Promotion beyond this slice needs the n=75 x 3 run against the 42/75 gate (unchanged rule).
Failures are classified model_behavior / infrastructure / execution.

## Risks and mitigations

| risk | mitigation |
|---|---|
| amd64 emulation makes test calls slow, so runs time out | 180 s per call, wall per arm reported, G2 measures real cost before G3 |
| Image `/testbed` drifts from the host clone (different base) | sync is relative to `base_commit`; G1 checks a sample `git status` inside the container is exactly the model's change |
| Model edits a test file that `test_patch` then fails to apply | counted per run; not stripped (tracked edits are the model's). If it occurs, it shows as a grading `patch_applied` failure, already a classify column |
| Silent fallback to the host would mask the lever | no fallback by construction; `image_exec_error` + liveness gate |
| Scratch strip deletes a real new module | strip only untracked files matching globs or new root-level `*.py`; every stripped path is logged and read in G2/G3 |
| Leaks into maintain/chat | switch only set by the adapter; a test asserts maintain_suite's env never carries it; tools.sdd Must-not |
| GPU contention with step 2 | no model call before step 2's DONE |
| Colima down mid-arm | `image_exec_error` events; the watcher greps them, and the arm is rerun, not analyzed |

## G1 result (classify half, 2026-11-20)

`replay.py` (real `shell._bash`, execution stubbed), 124 bash calls over 39 instances of the
diag_misses control rep: old refused 98, new refused 14; **refused to runnable 84/98 (86%)**,
gate is 40%. Test-like calls that run: 8 old, 65 new. New routing: 95 image, 15 host, 14 refused
(7 unquoted newline, 4 other chains, 3 not-allowlisted `python3`). Rows: `replay.json`.
The exec half (`replay.py --exec`) runs after the maintain A/B finishes (CPU contention).

## G1 result (exec half, 2026-11-20) - PASS

`replay.py --exec` (`replay_exec.json`): all 95 routed commands ran in their instance image on that
run's final tree; 0 `image exec unavailable`; median 4.3 s per call; 59 exit 0.
- Real model-patch failures now visible: astropy-13236 `ImportError: cannot import name
  'NdarrayMixin'` (the deleted import behind its 644 P2P failures), astropy-13453
  `FAILED test_html.py::test_multicolumn_write` (its P2P regression).
- 9 "No module named pytest": the Django testbed env has no pytest (Django uses
  `tests/runtests.py`). Watch in G2/G3 whether the model switches runners; no Django hint added.
- requests-2931 `recursive dependency involving fixture 'httpbin'`: same with network on, so it is
  the old checkout's test setup, not `--network none` or the patch.
- Sync check (astropy-13453): container `git status` = host (`M html.py`, `?? repro_x.py`). The
  image HEAD is SWE-bench's setup commit on top of base_commit (a one-line `pyproject.toml`
  change), the same tree the grader uses. Edge: a model edit to `pyproject.toml` would replace
  that tweak in the container.
