# neo unification — report (2026-08-13)

Executed per `PLAN.md` (same directory). Agent-driven from **m5** over ssh; all
host work on **neo** (Mac17,5 · Apple A18 Pro · 8 GB · macOS 26.5.2 build 25F84).
luxe code/doc work in `~/Downloads/luxe` on m5, branch **`feat/neo-unification`**.

Nothing is merged. `main` is untouched everywhere. Two commits on neo (micro-mind,
dotfiles) are **prepared and not pushed**.

---

## 1. Gate verdicts

| Gate | Verdict | One-line basis |
|---|---|---|
| **G-BOOT** | **DEFERRED-NEEDS-USER** | The P1.1 guard fired: FileVault is **On** with **no auto-login** and **no non-interactive sudo**, so neo cannot boot unattended at all — a reboot would strand it at pre-boot auth with no ssh. Verified by `launchctl` state + the BTM database instead, per the plan's fallback. **The mechanism is diagnosed, not guessed** (§ 2), and the remaining step is one GUI click (§ 3.4). |
| **G-LUXE-LOCAL** | **PASS** | On neo, against the LOCAL llama-server, **with no flags at all**: `luxe ready` exit **0** and all-✓ (zero warnings); `luxe smoke` READY **3 s**; `luxe smoke --chat --code` READY **39 s** from a cold engine — chat drill recovered the magic word, code drill fixed the planted bug in 6 steps / 9 tool calls with **pytest green** and **exactly `calc.py` changed**. Evidence § 4.4. |
| **G-FLEET-NO-REGRESSION** | **PASS** | Full suite on m5: **2807 passed, 2 skipped** (baseline 2783 + 24 new tests, 0 removed). No benchmark-path file touched — `configs/`, `benchmarks/`, `tools/`, `agents/`, `single.py`, `maintain.py` all **unmodified**. `luxe ready` on m5 exit **0**, all-✓. m1/m4 never contacted. Evidence § 5. |
| **G-DOCS** | **PASS** | CLAUDE.md fleet-sibling section rewritten; chat.sdd manifest + smoke pins corrected; micro-mind marked superseded on the neo-llm-bench `d6f32321` pattern with the engine **explicitly excluded**; OUTAGE.md gains neo; two lessons.md entries. Evidence § 6. |
| **G-REVIEW-READY** | **PASS** | 2 luxe commits on `feat/neo-unification` (pushed as a **branch** only, `main` untouched); micro-mind commit `d831090` and dotfiles commit `586b109` prepared on neo, **not pushed**; every gate has pasted evidence. Reviewer checklist in § 9. |

**Headline:** the boot gap is **not luxe, not launchd, and not the plist** — it is
macOS Background Task Management holding the LaunchAgent at *enabled but not
allowed*. And luxe-on-neo needed far less than expected: the wiring already
existed, so the work was making luxe's **diagnostics** stop lying about an engine
that is not oMLX.

---

# Part A — boot persistence

## 2. Diagnosis (P1) — the mechanism, with a control

### 2.1 What was wrong

The 2026-08-13 MLX probe found the router down and *never started since boot*
(`acceptance/mlx_neo_probe_2026_08/REPORT.md` § 2): plist present, absent from
`launchctl list`, port 8080 dark, `StandardOutPath` never created, uptime 8 d 8 h.

### 2.2 What it is NOT — four hypotheses killed first

| Hypothesis | Killed by |
|---|---|
| Bad or invalid plist | `plutil -lint` → **OK**; has both `RunAtLoad` **and** `KeepAlive`; starts instantly by hand |
| Disabled in launchd's own database | `launchctl print-disabled gui/501` → `"com.micromind.llama-server" => enabled` |
| No GUI login session after boot (headless → no `gui/$UID`) | `who -b` → `Aug 4 21:50`; `who` → `mtimpe console **Aug 4 21:51**`; `loginwindow` pid **409** started 21:51:16; `gui/501` exists, `session = Aqua`, `creator = loginwindow[409]`. **A console login did happen, one minute after boot.** |
| Binary missing / crashing at spawn | `~/.local/bin/llama-server` → `~/code/llama.cpp/build/bin/llama-server`, present since May; a `kickstart -k` SIGKILL is recovered in **~2 s** (§ 3.2) |

The unified log was **not** usable: the archive covers Jul 14 → Aug 13, but every
`launchd` / `loginwindow` / `micromind` message in the Aug 4 boot window had been
TTL-evicted (`log show` returns headers only). The diagnosis rests on state, not logs.

### 2.3 What it IS — Background Task Management

On macOS 13+ every item in `~/Library/LaunchAgents` is registered in
`/var/db/com.apple.backgroundtaskmanagement/BackgroundItems-v16.btm` with a
`disposition` bitfield. **launchd's login-time bootstrap skips any item whose
`allowed` bit (value 2) is clear. Manual `launchctl bootstrap` is not gated** —
which is exactly why every hands-on check passed.

The file is world-readable (`sfltool dumpbtm` needs root and returns nothing
without it). Measured on neo:

```
  disposition=11  allowed=YES 8.com.google.GoogleUpdater.wake
  disposition=9   allowed=no  8.com.valvesoftware.steamclean
  disposition=9   allowed=no  8.com.micromind.llama-server
```

`11` = enabled·**allowed**·notified · `9` = enabled·notified, **allowed clear**.

**The control is what makes this conclusive.** `com.google.GoogleUpdater.wake`
lives in the same directory, under the same login, on the same boot — it is the
only one with the `allowed` bit set, and the only one present in `gui/501`
(`runs = 151` over the 8 days). Both items with the bit clear are absent from the
domain. (The two `com.google.keystone.*` plists are `<dict/>` — empty, not jobs —
so they are not evidence either way.)

**Why the bit is clear.** The record's `modificationDate` is
**2026-08-03 04:47:50 UTC**, five minutes after the 2026-08-03 00:42 boot — i.e.
the first login after the plist was rewritten for router mode on 2026-08-02 23:47.
A modified background item re-registers as a new generation (`generation = 3`),
*enabled but not allowed, pending user approval*; the `notified` bit confirms macOS
posted the "Login item added" notification. Nobody approved it. The 2026-08-03
session ran because it was started by hand; the 2026-08-04 boot skipped it.

**It is pinned to the binary's signature.**
`designatedRequirement = cdhash H"2cbf642b6d0496dd3269d654bf303de53d348729"`,
matching the current ad-hoc-signed `llama-server` exactly (`codesign -dvvv` →
`CDHash=2cbf642b…`, `Signature=adhoc`). **Rebuilding llama.cpp changes the cdhash
and re-arms this failure.** Recorded in three places.

### 2.4 P1.3 — was the plist tracked anywhere? No.

`grep -rl 'com.micromind.llama-server' ~/dotfiles` matched only `luxe/neo.yaml`
(a prose mention). The plist itself — the thing that keeps the fallback host's
model server alive — was **untracked**. Fixed in § 3.1.

### 2.5 P1.1 preflight — why no reboot

```
$ fdesetup status
FileVault is On.
$ defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser
The domain/default pair … does not exist
$ sudo -n true
sudo: a password is required
```

All three block the plan's fix ladder:

- **P2.1 auto-login + LaunchAgent** — requires FileVault **off**. It is on.
- **P2.2 convert to a LaunchDaemon** — requires sudo. Unavailable non-interactively.
- **Reboot to test** — with FileVault on and no auto-login, neo stops at pre-boot
  authentication and never reaches the network. A headless reboot **strands the
  box**. `sudo fdesetup authrestart` would bypass it once; it needs the same sudo
  password.

So P2.3 applies: best available partial + G-BOOT deferred with the exact remaining
step. Note the silver lining that makes this a small gap rather than a large one:
**neo cannot boot unattended anyway**, so a human is at the keyboard for every boot
— which is precisely who can flip this bit.

## 3. Fix (P2) and verification (P3)

### 3.1 What was done

1. **The plist is now tracked** — `~/dotfiles/luxe/com.micromind.llama-server.plist`,
   a **verbatim** copy beside `neo-models.ini` and `neo.yaml`. Verbatim on purpose:
   the live file was **not modified**, because any edit re-registers it with BTM as
   a new generation, and manual bootstrap is the one thing that currently works.
2. **`~/dotfiles/luxe/README-neo-router.md`** — the full diagnosis, the
   readable-database one-liner, the disposition table, the cdhash-fragility
   warning, the restore command, and the approval step.
3. **`OUTAGE.md` § 3** gains neo's row and the post-boot one-liner (offline card,
   still under the 120-line cap a test enforces).
4. **`luxe ready`'s own fix string** now names it: on a `llama-server` engine, a
   dead endpoint's fix reads
   `restart the server (on neo: launchctl kickstart -k gui/$UID/com.micromind.llama-server)`
   instead of `brew services restart omlx`.

**Deliberately NOT done: no cron/launchd watchdog.** CLAUDE.md records a standing
user decision that `luxe ready` gets no scheduled/alerting counterpart, and a
self-healing timer would hide exactly the class of failure this exists to expose.
It is called out as a non-choice in the README so nobody "fixes" it later.

### 3.2 P3.2 — crash recovery proves `KeepAlive` semantics

```
2026-08-13T12:08:33Z   pid_before=61467   runs=1
$ launchctl kickstart -k gui/501/com.micromind.llama-server     # SIGKILL
recovered after ~2s
2026-08-13T12:08:35Z   state = running    runs=2    pid=62094
$ curl -s localhost:8080/v1/models
[('Qwen3-4B-Instruct-2507', 'loaded'), ('default', 'unloaded')]
```

A crash is recovered. A **reboot** is the uncovered case, and only that.

### 3.3 Final launchctl state (the G-BOOT evidence the plan asks for)

```
$ launchctl print-disabled gui/$UID | grep micromind
                "com.micromind.llama-server" => enabled
$ launchctl print gui/$UID/com.micromind.llama-server
        path = /Users/mtimpe/Library/LaunchAgents/com.micromind.llama-server.plist
        state = running
        runs = 3
        pid = 62561
        last exit code = 0
```

**The router is UP and serving.** It was left better than found.

### 3.4 The one remaining step (for the user)

> System Settings → General → **Login Items & Extensions** → *Allow in the
> Background* → enable **llama-server** (listed under "Unknown Developer" —
> the binary is ad-hoc signed).

Then re-read the disposition: it must read **11**, not 9. There is no supported CLI
for this bit (`sfltool` offers only `dumpbtm` and `resetbtm`, both root;
`resetbtm` wipes the whole database and is not recommended). After that, a reboot
is the honest G-BOOT test and takes one minute.

---

# Part B — luxe on neo, micro-mind retired

## 4. Design (P4) and implementation (P5)

### 4.1 What the investigation actually found

The plan asked whether this should be a `hosts:` entry, a `backends:` default, or
new per-host config. **The answer is none of them: the wiring already existed.**

`~/dotfiles/luxe/neo.yaml` (written 2026-08-03) is already a complete out-of-tree
chat config — `omlx_base_url: 127.0.0.1:8080`, a `backends.local` entry, a
`hosts.neo` manifest with `main`/`fallback`/`ctx_max`, `visible_models`, and a
monolith role at `num_ctx: 16384` matching the preset. `~/dotfiles/bin/luxe-chat`
and `luxe-code` pass it via `luxe-hostconfig.sh`. It lives outside the repo for a
recorded reason: an earlier in-tree deploy pinned its edits with
`git update-index --skip-worktree` and cost **78 commits of drift**.

Empirically, before any code change, on neo:

```
$ luxe ready --config ~/dotfiles/luxe/neo.yaml      → READY (warnings), exit 0
$ luxe smoke --config …                             → READY (3s), exit 0
$ luxe smoke --chat --code --config …               → READY (38s), exit 0
```

It works because neo runs llama-server in **router mode** (`--models-preset`):
`/v1/models` returns a real catalog keyed by preset section name, so
`list_models()`, the manifest `main`/`fallback` ids, and the `served:` cross-check
all line up. (A single-model `llama-server -m …` would report one id — a gguf path
or `-a` alias — and would not.)

So the real gap was narrower and different from the brief's assumption:

1. **Two standing false warnings and an unrunnable fix.** Pre-change output:
   ```
   ! API key    no key resolved for this endpoint    → `echo 'OMLX_API_KEY=…'`
   ! weights    location unreported by oMLX
   ```
   plus `oMLX endpoint` as the check name and `brew services start omlx` as the
   dead-endpoint fix, on a box with no omlx formula. On the **fallback** host, for
   use during an **outage**, permanent yellow lines are how people learn to skip
   yellow lines.
2. **`luxe pull` failing confusingly** — `oMLX admin login failed: 404`, which
   sends the reader after a credential rather than telling them this host
   provisions weights from a preset file.
3. **Config discovery covered only two commands.** The wrappers pass `--config`
   for `luxe chat`/`luxe code`. Bare `luxe ready` — *the command you reach for in
   a panic* — read the fleet config and judged an oMLX endpoint neo does not run.

### 4.2 The design chosen

**Minimal, additive, and it does not redesign the manifest system.**

**(a) `engine:` on a `BackendEntry`** — `omlx` (default) | `llama-server`.

- Declared in config, not sniffed: explicit, testable, no extra network call, and
  it cannot misfire on a server's cosmetic response change.
- **Validated at load** — an unknown value *raises*. A typo must not silently
  restore the oMLX assumptions on the one box that needs them off.
- **Diagnostic-only by contract** (written into `chat.sdd`). It may change a
  check's name, severity and `fix`, and it gates `luxe pull`'s refusal. It may
  **never** change the request body, the tool surface, model selection, or
  `backend_kwargs()` — pinned by `test_backend_kwargs_is_unaffected_by_engine`.
  Every supported engine is OpenAI-compatible, which is the whole reason one
  client drives both.
- Consistent with the existing dotfiles engine identity: neo declares
  `engine: "llama-server"` on the entry that already names port 8080.

**(b) `$LUXE_CONFIG`** — the default for `--config` when none is passed.

- An env var and not a path lookup, deliberately: per-host configs live *out* of
  this repo, and hardcoding `~/dotfiles/luxe/<host>.yaml` would drag a private
  layout into the public tree.
- Unset ⇒ previous behaviour exactly. Chat-config only — verified that
  `_run_pipeline_readonly` / `_run_pipeline_maintain` / `luxe check` call
  `load_config(...)` directly (which defaults to `configs/single_64gb.yaml`) and
  never route through `_default_chat_config`, so **it cannot reach the benchmark
  path**.

**Rejected:** adding a `hosts: neo:` entry to `configs/chat.yaml` (re-opens the
skip-worktree wound and puts a host-specific engine in the fleet config); sniffing
the engine from `owned_by: llamacpp` (fragile, costs a probe); per-host config
auto-discovery inside luxe (leaks dotfiles layout).

### 4.3 What changed, by surface

| File | Change |
|---|---|
| `src/luxe/config.py` | `ENGINE_OMLX` / `ENGINE_LLAMA_SERVER` / `KNOWN_ENGINES`; `BackendEntry.engine` + load-time validator; `is_omlx()`, `engine_label()`, `needs_api_key()` |
| `src/luxe/chat/inspection.py` | `backend_entry_for()` / `_engine_facts()`; endpoint check named + fixed per engine; API-key check → OK "not required by llama-server"; unknown weight path → OK with the reason; stale-Cellar probe skipped off oMLX; `served:` fix per engine |
| `src/luxe/chat/origin.py` | `describe()` unknown case is engine-neutral |
| `src/luxe/chat/modelcaps.py` | reason string engine-neutral; docstring records **why** fail-open is *correct* on llama-server (jinja templates, verified round-trip), not merely lucky |
| `src/luxe/cli.py` | `$LUXE_CONFIG`; `_default_engine_from_config()`; `_refuse_pull_on_non_omlx()` wired into `pull`; `--list` stops reporting a queue that does not exist |
| `tests/` | **24 new tests** across `test_config.py`, `test_chat_inspection.py`, `test_cli_ready.py` — each class carries a **control** asserting the oMLX path is unchanged |
| `CLAUDE.md`, `chat.sdd`, `OUTAGE.md`, `lessons.md` | § 6 |

Two commits on `feat/neo-unification`:

- **`a123b3a`** feat(chat): an endpoint can say it is not oMLX, and neo stops being lied to
- **`70bbe92`** fix(pull): `--list` no longer blames a missing key for a queue that doesn't exist

### 4.4 G-LUXE-LOCAL evidence — after deploy, **with no flags**, cold engine

Deployed by pushing the branch to origin and `git fetch` + checkout on neo, then
`uv sync --extra chat --extra dev --extra analyzers --extra web`. Per **P5.4**, the
router was cold-restarted (`launchctl kickstart -k`) immediately before this run.

```
$ echo $LUXE_CONFIG
/Users/mtimpe/dotfiles/luxe/neo.yaml

$ luxe ready
  ✓ llama-server endpoint           local http://127.0.0.1:8080
  ✓ API key                         not required by llama-server
  ✓ chat model                      Qwen3-4B-Instruct-2507
  ✓ weights                         llama-server reports no model path (checked below)
  ✓ host manifest                   main Qwen3-4B-Instruct-2507 · fallback Qwen3-4B-Instruct-2507
  ✓ weights:Qwen3-4B-Instruct-2507  main · on disk
  ✓ disk                            348.9 GB free
  ✓ update                          current with origin/main
  ✓ search index / index freshness / working tree / mode / web / TUI   ✓
READY (1s)                                                      READY_EXIT=0
```

**All ✓, zero warnings** (before: 2 warnings, one with an unrunnable fix).

```
$ luxe smoke
  ✓ manifest · weights ×2 · endpoint · catalog
  ✓ main turn — Qwen3-4B-Instruct-2507 answered in 0.3s
  ✓ tool call — Qwen3-4B-Instruct-2507 called read_file in 2.0s
  ✓ fallback turn — answered in 0.1s
READY (3s)                                                      SMOKE_EXIT=0

$ luxe smoke --chat --code
chat drill
  ✓ chat agent — Qwen3-4B-Instruct-2507: 2 step(s), 1 tool call(s), 13s
  ✓ answer — magic word recovered
code drill
  ✓ code agent — Qwen3-4B-Instruct-2507: 6 step(s), 9 tool call(s), 25s
  ✓ tests — pytest green after the fix
  ✓ diff — exactly calc.py changed
READY (39s)                                                     DRILL_EXIT=0

$ luxe pull mlx-community/Qwen3.6-27B-6bit
✗ `luxe pull` cannot fetch weights on a llama-server endpoint. It drives oMLX's
admin API and the ~/.omlx/models store, neither of which llama-server has.
  This host serves GGUF weights named in its llama-server preset (neo:
`~/dotfiles/luxe/neo-models.ini`). …                            PULL_EXIT=2

$ luxe pull --list
Local models (/Users/mtimpe/.omlx/models)
  · Qwen3-4B-Instruct-2507
· llama-server has no download queue — weights come from its preset file
                                                                LIST_EXIT=0
```

Plus a real headless interactive turn, no flags:

```
$ printf 'Read the file src/luxe/outage.py and tell me in one sentence what it does.\n/quit\n' | luxe chat --repo ~/Downloads/luxe
→ read_file(path='src/luxe/outage.py')  ✓ 2.3 KB
The outage.py file loads and processes the OUTAGE.md file, providing a fallback
message and extracting runnable luxe commands for use during network outages.
· steps: 2 · tools: 1 · 19.4s · ctx: 23% of 16K
```

**One honest observation, characterized rather than glossed.** The *same question
without the words "read the file"* — `"In one sentence, what does
src/luxe/outage.py do?"` — was answered in 1 step with **0 tool calls**: *"I don't
have access to the content of src/luxe/outage.py…"*. So on a freeform
conversational turn the 4B does not spontaneously reach for a tool; told to read,
it reads and answers correctly, and the drill-shaped `--chat` prompt passes
cleanly. This is a small-model prompting characteristic, not a wiring fault, and it
is exactly the thesis micro-mind's `lessons.md` was built around. It does **not**
affect G-LUXE-LOCAL, whose bar is the `luxe smoke --chat --code` drill — but a neo
user should know to say "read X".

### 4.5 Two pre-existing neo facts worth recording (found, not changed)

- **`~/.omlx/models/Qwen3-4B-Instruct-2507/`** holds a `config.json` and a **copy**
  of the GGUF. It is a deliberate shim (its own `_comment` says so, dated
  2026-08-03) that makes luxe's `weights:<id>` probe check the real bytes. It is
  load-bearing: without it that check FAILs and `luxe ready` exits 1. It is a
  genuine **second 2.5 GB copy**, not a hardlink (inodes 4827071 vs 4825772). On a
  348 GB-free box that is not urgent, but a hardlink would reclaim it. **Not
  touched** — changing weights on the fallback host was out of scope today.
- neo's `luxe` checkout is on `feat/neo-unification` at `70bbe92` for review. It
  must be returned to `main` when the branch merges.

## 5. G-FLEET-NO-REGRESSION evidence

```
$ uv run pytest -q          (m5, branch tip)
2807 passed, 2 skipped, 4 deselected in 73.52s
```

Baseline **2783**: the diff adds exactly 24 `def test_` and removes **0**
(`git diff 8de375f..HEAD -- tests/ | grep -c '^+    def test_'` → 24; removals → 0).

**Benchmark path untouched** — the full changed-file list is 12 files, and:

```
$ git diff 8de375f..HEAD --name-only | grep -E 'configs/|benchmarks/|tools/|agents/|single\.py|maintain\.py'
NONE
```

No `configs/single_64gb.yaml` change, no `TOOL_FNS` change, no prompt change, and
`BackendEntry.backend_kwargs()` is engine-invariant by test. `$LUXE_CONFIG` is
proven unable to reach the benchmark path by inspection (§ 4.2b).

`luxe ready` on m5: exit **0**, all ✓ (champion `Qwen3.6-35B-A3B-6bit`, all three
manifest models on disk, `oMLX build 0.5.7 (matches installed)`).

**m1 and m4 were never contacted.** No model server was restarted anywhere except
neo's router (twice, deliberately, both times verified back up).

## 6. G-DOCS evidence

| Doc | Change |
|---|---|
| `CLAUDE.md` § "Fleet sibling" | Retitled *neo (luxe over llama-server)*. Says micro-mind is retired and the **engine is not**; records the out-of-tree config + `$LUXE_CONFIG`; records that `configs/chat.yaml` still has no neo entry **by design** and what that statement does and does not mean; **retires the "bare `luxe smoke` fails on neo" pin** with the measured replacement; documents `engine:`; documents the BTM gap + the one-liner + the no-watchdog decision. |
| `src/luxe/chat/chat.sdd` | Manifest bullet corrected (neo *has* a manifest, out of tree, and why); new **`engine:` contract** bullet (diagnostic-only, Owns list, load-time validation); smoke bullet records the stale-check skip and that bare `luxe smoke` **passes** on neo. |
| `OUTAGE.md` § 3 | neo added to the per-host table with an `engine` column; a neo paragraph + the post-reboot bootstrap one-liner. Still **118 lines** (a test enforces ≤120). |
| `lessons.md` | Two entries: the BTM postmortem (with the disposition table and three general lessons), and the "diagnostics were oMLX-shaped" entry (with the rule *a check's `fix` string is part of its correctness*). |
| micro-mind `README.md` + `CLAUDE.md` (on neo) | Superseded banner on the `neo-llm-bench d6f32321` pattern; the engine is **explicitly excluded** from retirement in both files, including why the label's lineage must not be renamed; pointer to this report; `lessons.md` and `bench/` named as the parts that stay valuable. Nothing deleted. |

## 7. P6 — micro-mind retirement, and the auto-invoke inventory

**Commit on neo (NOT pushed):** `d831090` — *docs: superseded for deployment by
luxe-on-neo — the ENGINE stays in service*. `README.md` +45 / `CLAUDE.md` +34;
`lessons.md` and `bench/` untouched. Nothing deleted; the llama-server build, the
router LaunchAgent, and `neo-models.ini` were not touched.

**P6.4 — anything on neo that auto-invokes the micro-mind BINARY: nothing.**
Inventory run, all negative:

```
crontab -l                                        → no crontab for mtimpe
grep -rl micro-mind ~/Library/LaunchAgents /Library/Launch{Agents,Daemons}
                                                  → only com.micromind.llama-server.plist
                                                    (the ENGINE — stays)
grep -rn 'micro-mind|micromind' ~/.zshrc ~/.zshenv ~/.zprofile ~/.zlogin  → (none)
ls ~/.local/bin ~/dotfiles/bin                    → no micro-mind / mm binary on PATH
grep -rl 'micro-mind|micromind' ~/dotfiles        → luxe/neo.yaml, luxe/neo-models.ini
                                                    (prose references only)
```

So there was **nothing to disable or repoint**. The only `com.micromind.*` object
on the box is the router LaunchAgent, which is engine infrastructure and stays.

## 8. Every surface touched

### m5 — `~/Downloads/luxe`, branch `feat/neo-unification` (pushed as a branch; `main` untouched)

| Commit | Subject |
|---|---|
| `a123b3a` | feat(chat): an endpoint can say it is not oMLX, and neo stops being lied to |
| `70bbe92` | fix(pull): `--list` no longer blames a missing key for a queue that doesn't exist |

12 files: `CLAUDE.md`, `OUTAGE.md`, `lessons.md`, `src/luxe/chat/chat.sdd`,
`src/luxe/chat/{inspection,modelcaps,origin}.py`, `src/luxe/{cli,config}.py`,
`tests/{test_chat_inspection,test_cli_ready,test_config}.py` (+654 / −40),
plus this report.

### neo — `~/Downloads/micro-mind` (commit prepared, **NOT pushed**)

`d831090` — README.md, CLAUDE.md. `main` is ahead of `origin/main` by 1.

### neo — `~/dotfiles` (commit prepared, **NOT pushed**)

`586b109` — *luxe/neo: track the router LaunchAgent, and tell luxe what engine it is*:

- **new** `luxe/com.micromind.llama-server.plist` (verbatim copy of the live plist)
- **new** `luxe/README-neo-router.md` (the BTM diagnosis + restore + approval steps)
- `luxe/neo.yaml` — `engine: "llama-server"` on `backends.local`, with a comment
- `zsh/hosts/neo.zshenv` — `export LUXE_CONFIG="$HOME/dotfiles/luxe/neo.yaml"`

`main` is ahead of `origin/main` by 1. The user's unrelated `pulsar/config.cson`
modification was left alone (specific paths were staged, never `git add -A`).

### neo — system state

| Change | Detail |
|---|---|
| Router restarted twice | `launchctl kickstart -k` (crash-recovery proof, then a cold engine for P5.4). Verified serving after each. `runs` 1 → 3. |
| luxe checkout | `main 8de375f` → `feat/neo-unification 70bbe92`, `uv sync` run. **Return to `main` after merge.** |
| **Not changed** | the live LaunchAgent plist (byte-identical), the llama.cpp build, `neo-models.ini`, the BTM database, FileVault, login settings, `~/.omlx/models`. No sudo was used. Nothing was deleted anywhere. |

### Untouched entirely

m1, m4. No model server outside neo was restarted. `origin/main` on all repos.

---

## 9. Reviewer checklist — independent re-verification

### 9.1 On m5 (this checkout)

```sh
cd ~/Downloads/luxe && git checkout feat/neo-unification
uv run pytest -q
#   expect: 2807 passed, 2 skipped, 4 deselected

git diff 8de375f..HEAD --name-only | grep -E 'configs/|benchmarks/|tools/|agents/|single\.py|maintain\.py'
#   expect: NO OUTPUT (benchmark path untouched)

git diff 8de375f..HEAD -- tests/ | grep -c '^-.*def test_'
#   expect: 0 (no test was deleted or weakened)

wc -l < OUTAGE.md
#   expect: 118  (the <=120 cap is asserted by test_card_exists_and_is_short)

uv run luxe ready; echo "exit=$?"
#   expect: exit=0, "oMLX endpoint" (NOT llama-server) — m5 is unchanged

uv run pytest -q tests/test_config.py::TestBackendEngine \
  tests/test_chat_inspection.py::TestDoctorOnALlamaServerEngine \
  tests/test_cli_ready.py::TestLuxeConfigEnvVar \
  tests/test_cli_ready.py::TestPullRefusesOffOmlx
#   expect: 24 passed. Each class carries a control test asserting the oMLX
#   path is unchanged (…_an_omlx_endpoint_keeps_every_old_line,
#   …_an_omlx_config_is_not_refused, …_unset_keeps_the_in_tree_default).

git log --oneline origin/main -1
#   expect: 8de375f — main was never moved
```

### 9.2 On neo — the boot gap (G-BOOT)

```sh
ssh neo
launchctl print-disabled gui/$UID | grep micromind
#   expect: "com.micromind.llama-server" => enabled     (launchd is NOT the gate)

python3 - <<'PY'
import plistlib
o = plistlib.load(open("/var/db/com.apple.backgroundtaskmanagement/BackgroundItems-v16.btm","rb"))["$objects"]
for x in o:
    if isinstance(x, dict) and "disposition" in x and "identifier" in x:
        i = x["identifier"]; ident = o[i.data] if hasattr(i,"data") else i
        if any(k in str(ident) for k in ("micromind","GoogleUpdater.wake","steamclean")):
            print(f'disposition={x["disposition"]:<3} allowed={"YES" if x["disposition"] & 2 else "no"}  {ident}')
PY
#   expect: GoogleUpdater.wake = 11 allowed=YES   (the control: it IS in gui/501)
#           steamclean         =  9 allowed=no
#           micromind          =  9 allowed=no    <- THE BUG
#   After the user approves it in System Settings, micromind must read 11.

fdesetup status                  # expect: FileVault is On.   (why no reboot test)
who -b; who                      # expect: boot, then a console login ~1 min later

launchctl kickstart -k gui/$UID/com.micromind.llama-server && sleep 5 \
  && curl -s localhost:8080/v1/models | head -c 80
#   expect: recovers in seconds, Qwen3-4B-Instruct-2507 "loaded" (KeepAlive works)
```

### 9.3 On neo — luxe (G-LUXE-LOCAL). Note: **no `--config` flag anywhere.**

```sh
ssh neo
echo $LUXE_CONFIG                # expect: /Users/mtimpe/dotfiles/luxe/neo.yaml
cd ~/Downloads/luxe && git log --oneline -1     # expect: 70bbe92

.venv/bin/luxe ready; echo "exit=$?"
#   expect: exit=0, ALL ✓, ZERO warnings, and specifically:
#     ✓ llama-server endpoint   (not "oMLX endpoint")
#     ✓ API key   not required by llama-server
#     ✓ weights   llama-server reports no model path (checked below)
#   and NO "oMLX build" line at all.

.venv/bin/luxe smoke; echo "exit=$?"                 # expect: READY, exit 0, ~3s
.venv/bin/luxe smoke --chat --code; echo "exit=$?"   # expect: READY, exit 0, ~40s
#   the code drill must show "pytest green" AND "exactly calc.py changed"

.venv/bin/luxe pull mlx-community/Qwen3.6-27B-6bit; echo "exit=$?"
#   expect: exit=2 and a message naming llama-server and the preset file.
#   MUST NOT say "oMLX admin login failed: 404".

.venv/bin/luxe pull --list; echo "exit=$?"
#   expect: exit=0, lists Qwen3-4B-Instruct-2507, and
#   "llama-server has no download queue" — NOT "no oMLX API key".

printf 'Read the file src/luxe/outage.py and say in one sentence what it does.\n/quit\n' \
  | .venv/bin/luxe chat --repo ~/Downloads/luxe
#   expect: a read_file tool call, then a correct one-sentence answer.
#   (Without the words "read the file" it will answer without reading — see § 4.4.)
```

**Cold-engine re-run** (to rule out warm-cache flattery, § P5.4 — this is how the
numbers above were produced):

```sh
launchctl kickstart -k gui/$UID/com.micromind.llama-server && sleep 8
.venv/bin/luxe smoke --chat --code
```

### 9.4 On neo — the two prepared commits (must NOT be pushed)

```sh
cd ~/Downloads/micro-mind && git log --oneline -1 && git status -sb | head -1
#   expect: d831090 …  and  "## main...origin/main [ahead 1]"
git show d831090 --stat        # expect: only README.md + CLAUDE.md
git show d831090 | grep -i 'llama-server\|LaunchAgent\|neo-models.ini'
#   expect: several hits — the ENGINE is explicitly excluded from retirement

cd ~/dotfiles && git log --oneline -1 && git status -sb | head -1
#   expect: 586b109 …  and  "## main...origin/main [ahead 1]"
git show 586b109 --stat
#   expect exactly 4 paths: luxe/com.micromind.llama-server.plist (new),
#   luxe/README-neo-router.md (new), luxe/neo.yaml, zsh/hosts/neo.zshenv
diff <(git show 586b109:luxe/com.micromind.llama-server.plist) \
     ~/Library/LaunchAgents/com.micromind.llama-server.plist
#   expect: NO OUTPUT — the tracked copy is verbatim, the live file was not edited
git status --short
#   expect: only " M pulsar/config.cson" — the user's own unrelated change, untouched
```

### 9.5 Things to push back on if you disagree

1. **G-BOOT is deferred, not passed.** The reboot was not run and the plan
   explicitly permits that; if you want it run, the FileVault password is needed at
   the console and the BTM approval should be done first so the reboot actually
   tests the fix.
2. **`engine:` is new config surface.** The alternative (sniffing
   `owned_by: llamacpp`) was rejected in § 4.2; if you prefer sniffing, the
   diagnostics are the only consumer and it is a contained change.
3. **`$LUXE_CONFIG` is a new env var.** Scoped to the chat config only (§ 4.2b);
   the wrapper-only alternative would have left bare `luxe ready` wrong on neo.
4. **The 2.5 GB duplicate GGUF in `~/.omlx/models`** (§ 4.5) was left alone.
5. **No watchdog was added** (§ 3.1), on the standing user decision. If a watchdog
   is wanted after all, it is a five-line `crontab` entry — but it would hide this
   failure class rather than fix it.

## 10. Deferred / blocked

| Item | State | Next step |
|---|---|---|
| **G-BOOT (real reboot test)** | **deferred-needs-user** | Approve `llama-server` in System Settings → General → Login Items & Extensions → Allow in the Background; confirm the BTM disposition reads **11**; then reboot and check `curl localhost:8080/v1/models` within 5 min of ssh answering. |
| Re-approval after any llama.cpp rebuild | open, documented | The BTM approval is pinned to the binary's cdhash (§ 2.3). Recorded in `README-neo-router.md`, `lessons.md`, and `CLAUDE.md`. |
| neo's luxe checkout on a feature branch | intentional | `git checkout main && git pull` on neo after this branch merges. |
| Duplicate 2.5 GB GGUF in `~/.omlx/models` | observation only | Replace the copy with a hardlink to `~/models/…gguf` if the space is ever wanted. |
| Freeform turns don't spontaneously read files | characterized, not a defect | § 4.4. Could be addressed with a neo-specific persona nudge; not attempted — it would be an unbenched prompt change. |
