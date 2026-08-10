# luxe MCP capability modules — execution report

> **Redacted for the public repo.** Phases 3 and 4 built two modules for
> private projects; they are **summarised here, not reproduced** — enough to
> record what was built, deployed and verified, without describing what those
> projects are. Everything about Phase 2 (the public PDF module), the
> luxe-repo findings, and the fleet/infrastructure lessons is unchanged;
> remaining private repo, host-user and printer names are placeholders. The
> unredacted original sits beside this file as `REPORT.private.md`, which the default
> `acceptance/*` ignore keeps out of git.
>
> **Correction (2026-08-10):** as written this report twice said neo runs
> 1.5B GGUFs. neo's champion became `Qwen3-4B-Instruct-2507-Q4_K_M` on
> 2026-08-03 — the day before this run — so 4B was already correct at
> execution time. Corrected in place; nothing else about those passages
> changed.

**Executed:** 2026-08-04, autonomously from m5, per `acceptance/mcp_modules_2026_08/plan.md`.
**Outcome:** All six phases complete. All three modules built, deployed, and
verified on all three hosts. **All follow-ups from the first draft of this
report are now closed, including the §8.4 recommendation** — see §8, kept
for the record.

---

## Commit SHAs pushed

| repo | visibility | SHAs | branch |
|---|---|---|---|
| luxe | **public** | `67895d4` (PDF module), `ad2a9b8` (XFA fix), `d033f0f` (lessons entry), `0ba1324` (stale-process check) | `main` (linear, rebased) |
| private module A repo | private | `<sha>` | `main` |
| dotfiles | private | `<sha>` (three modules), `<sha2>` (relays PYTHONPATH fix) | `main` |

Modules were deployed to all three hosts at
`dotfiles=<sha2> · module A=<sha> · luxe=ad2a9b8`; `d033f0f` is a
docs-only follow-up on m5.

A feature branch `feat/mcp-pdf` was used for the luxe work and fast-forwarded
into `main` — no merge commits, history stays linear.

---

## Phase 1 — host provisioning

| | m5 | m1 | neo |
|---|---|---|---|
| Homebrew | present | present | **installed** (non-interactive; CLT already present via Xcode) |
| qpdf · poppler · sox · ffmpeg · uv | present | present | **installed** |
| module B support repo + venv | **cloned + built** | present | **cloned + built** |
| module A checkout | **cloned** | present | present |
| module A data (2.1 GB) | **synced** | source of truth | **synced** |
| luxe checkout | current | **updated a48d47e → 6e870d2** | current |
| laser printer | **added** | present | present |

**m1's diverged luxe commit was NOT a hard stop.** `a48d47e` was 0 ahead / 8
behind `origin/main` — an ancestor, already pushed, nothing unpublished. The
plan's stop-and-report condition did not trigger; `luxe update` fast-forwarded
cleanly.

**m5 printer added:** the laser was discoverable over mDNS from m5's segment,
so the plan's "skip and note if unreachable" fallback was not needed:
`lpadmin -p Acme_LaserDoc_2350 -E -v "dnssd://…" -m everywhere`.

Gate (all three hosts): `qpdf --version && sox --version && ffmpeg -version &&
pdftotext -v` succeed; module B's support tool runs from its venv. ✅

---

## Phase 2 — `pdf-tools` (PUBLIC, in the luxe repo)

`src/luxe/mcp_pdf/` + `mcp_pdf.sdd` + console script `luxe-pdf-mcp` +
`[pdf]` extra (pypdf, reportlab, pillow) + `tests/test_mcp_pdf.py` (42 tests).

13 tools: read-only `pdf_info` · `pdf_text` · `pdf_form_fields` ·
`pdf_printers`; `/write`-gated `pdf_unlock` · `pdf_fill` · `pdf_overlay` ·
`pdf_merge` · `pdf_split` · `pdf_rotate` · `pdf_to_images` · `images_to_pdf` ·
`pdf_print`.

**Two things worth knowing:**

1. **`pdf_unlock` had to do more than `qpdf --decrypt`.** Three separate
   things stop a viewer filling and saving a restricted form, and decrypting
   clears only the first: the permission flags, a Reader-enabled usage-rights
   signature (`/Perms`, typically `/UR3`), and an XFA layer. The first cut
   handled flags + `/Perms` + `NeedAppearances` but left XFA, so a form
   carrying one would come out still unfillable — the exact failure the module
   exists to fix. Caught by reading `~/dotfiles/bin/pdfsign`, the existing
   prior art for this problem, which already stripped all three. Fixed in
   `ad2a9b8` with a test that plants `/Perms` + `/XFA` and asserts removal.

2. **A real bug the tests caught.** pypdf's `user_access_permissions` is an
   IntFlag *instance*, so `perms.PRINT` returns the class member and is always
   truthy. The first implementation reported every restricted file as
   unrestricted. Membership has to be a bitwise test.

**`mcp/client.py` change (in-scope, generic):** `_expand_path` now expands a
leading `~` in a stdio `command`/`args`. One server config is shared across
hosts whose `$HOME` differs. Bare words (`uvx`) and flags are untouched, so
pre-existing entries parse identically. Two tests added.

---

## Phases 3 and 4 — the two private modules

**Summarised for the public record.** Both shipped, deployed to all three
hosts, and passed their verification; the detail is omitted because it
describes private projects, not because anything failed.

**Module A** (its own private repo): an MCP server wrapping that project's
existing entry points and local HTTP API — a wrapper only, reimplementing
none of it. 8 tools, 6 read-only and 2 gated lifecycle operations. Its
dependency pin is worth recording: `mcp>=1.28.1,<2`, held below 2.0 to match
the fleet's luxe client, since 2.0 moves the FastMCP import.

**Module B** (in dotfiles): an MCP server over a documented multi-step
file-processing pipeline, with the written specification copied in as
`PLAYBOOK.md` and treated as the source of truth. 8 tools, 4 read-only and 4
gated. Parameters are derived per input file rather than hardcoded — the
playbook's constants are worked examples for one input format, not thresholds.

**One finding generalises beyond the private domain and is worth keeping.**
The playbook defined three fingerprints for verifying that a file had been
through a particular processing stage. Calibrating them against a real
processed output *and* a matched unprocessed control showed **two of the three
do not work as tests**: one measures something the toolchain already does to
every file regardless of that stage, and the other is an *input parameter* to
the process rather than a property you can measure back out — and it is
content-dependent besides. The module therefore gates on the one fingerprint
that discriminates and reports the other two as context. Nothing about the
pipeline changed; what changed is how confidently the result can be asserted.
The lesson is the general one: a verification check has to be calibrated
against a negative control, or it can pass for reasons unrelated to what it
claims to measure.

A second, smaller one: an artifact detector compared a window against the
**median** of what preceded it rather than the max, because against the max a
window already inside a long burst hides the whole artifact. That failed on
the first attempt and was caught in verification.

---

## Phase 5 — registration + wrappers

`~/dotfiles/luxe/relays.yaml` gains three stdio entries using `~` paths, with
`gate_tools` covering every tool that writes a file or changes external state.
Any combination composes in one session.

`~/dotfiles/bin/luxe-module` + one symlink per module, following the
`luxe-relay` pattern.

**Deviation from the plan, deliberate:** the plan didn't mention
`luxe-hostconfig.sh`, but the wrappers must source it — neo is an 8 GB box
that runs a 4B GGUF through the llama.cpp router and needs
`~/dotfiles/luxe/neo.yaml`. Without it a module wrapper on neo would try the
fleet's 35B config. `luxe-module` sources it exactly as `luxe-chat` does. This
also supersedes the plan's §5.4 note about pointing neo at `--backend m5`:
neo now has its own local config, though `--backend m5` still works as an
override.

**Bug found and fixed during verification (`<dotfiles sha2>`):** a stdio MCP server is
launched from the *caller's* working directory — whatever repo you started
luxe in — not the module's own directory. `python -m <module>` therefore
couldn't find its package and died at connect with a bare "Connection
closed". One module already carried a `PYTHONPATH` for this reason; the other
now does too.

---

## Phase 6 — verification matrix

### MCP surface through luxe's own client (all three hosts)

| host | pdf | module A | module B |
|---|---|---|---|
| m5 | ✅ 13 tools (4 always / 9 gated) | ✅ 8 (6/2) | ✅ 8 (4/4) |
| m1 | ✅ 13 (4/9) | ✅ 8 (6/2) | ✅ 8 (4/4) |
| neo | ✅ 13 (4/9) | ✅ 8 (6/2) | ✅ 8 (4/4) |

### PDF (all three hosts)

Synthetic government-form-style fixture: AcroForm with 3 fields, then
`qpdf --encrypt --print=none --modify=none`.

| step | m5 | m1 | neo |
|---|---|---|---|
| `pdf_info` shows restrictions (`annotate, assemble, form, modify, print, print_high_res`) | ✅ | ✅ | ✅ |
| `pdf_unlock` → `blocked_after=[]`, `encrypted=False` | ✅ | ✅ | ✅ |
| `pdf_fill` 3 fields + read back | ✅ | ✅ | ✅ |
| `pdf_to_images` renders | ✅ | ✅ | ✅ |
| `pdf_merge` two files → 2 pages | ✅ | ✅ | ✅ |
| `pdf_print` dry-run argv correct | ✅ | ✅ | ✅ |
| label printer refused | n/a (none configured) | ✅ refused | n/a |

**Headless end-to-end through luxe itself (m5)** — session `ee34d699adc7`:

```
printf '…report the form fields of form.pdf…\n/quit\n' \
  | luxe chat --repo <scratch> --mcp pdf --mcp-config ~/dotfiles/luxe/relays.yaml
```
→ `· MCP: 4 tool(s) + 9 write-gated from 1 server(s): pdf`
→ `→ mcp__pdf__pdf_form_fields(path='form.pdf')  ✓ 603 B`
→ correct 3-field table, 7.3s. Tool call confirmed in
`~/.luxe/sessions/ee34d699adc7/debug.log`. ✅

**Gating confirmed** via `/tools` before and after `/write`:
`MCP gated by read-only mode (/write enables 9)` listing `pdf_unlock`,
`pdf_print` etc.; after `/write` they appear. ✅

**The one real print job** — exactly one, laser only, from m1 (see §8.2 for
the caveat on confirming it):

```
lp -d Acme_LaserDoc_2350 -n 1 -o page-ranges=1 <filled, flattened, 1 page>
→ request id Acme_LaserDoc_2350-77
```

### Private modules A and B (all three hosts)

Both verified on every host and summarised here. Module A: readiness check
clean with zero FAIL lines, lifecycle start → HTTP 200 → status → stop, no
strays left running anywhere (confirmed by `pgrep` on all three). Module B:
every run against a **scratch copy** — the source workspace was never written
to — exercising synthetic-input detection, the full end-to-end operation
through all 11 of its playbook gates, and both safety refusals (writing over
an input, and running in the source's own directory). Remaining WARNs on
every host are hardware-absent lines.

A cross-host consistency check compared a freshly produced output against a
previously shipped one: correlation 1.000000 in the meaningful band, with
bytes differing as expected because the process seeds randomly unless a seed
is passed.

### luxe repo health

- **Full suite green on m5: 2095 passed, 1 skipped.** (Note: `uv sync` prunes
  `mpmath` and reddens `test_miss_func_49` — the documented gotcha; re-add
  `--extra bfcl`.)
- `tests/test_mcp_pdf.py` green on all three hosts (42 · 41 · 41 — m1/neo were
  one commit behind for the XFA test at the time of that run and are now current).
- **Benchmark path untouched.** Diff `6e870d2..ad2a9b8` touches only
  `README.md`, `configs/mcp.yaml`, `pyproject.toml`, `src/luxe/mcp/client.py`,
  `src/luxe/mcp_pdf/**`, `tests/**`, `uv.lock`. Nothing under `benchmarks/`,
  `configs/single*`, `agents/`, `tools/`, or `backend.py`. A test asserts no
  `pdf_*` name reaches `_build_full_tool_surface`.
- `configs/mcp.yaml` `client.servers` is still `[]` (commented example only).
- **`luxe smoke` exits 0 (READY) on m5 and m1** after the §8.3 fix.
- **No private strings in the public diff.** Grepped added lines for the
  fleet's private-token list — tailnet name, both private project names, both
  private repo names, printer vendor, module B's support tool, the m1
  username, the release shell function, and every hostname → clean. (The literal token list lives in the
  unredacted copy; see the note at the top of this file.) Two scrubs were
  needed during the work: real printer names in tests (now
  `Acme_LaserDoc_2350` / `Acme_QL_820NWB`) and a username in a code comment.

---

## Safety rails (§7) — all held

- No original modified: all pipeline work on scratch copies; the private data
  workspace, module A's originals, and previously shipped outputs were
  read-only throughout.
- The private data workspace was never git-initialised, committed, or pushed.
  Only `PLAYBOOK.md` was copied, into private dotfiles.
- Nothing private in the public luxe repo (grep-verified above).
- No secret values in YAML or code anywhere.
- Exactly **one** physical print job, to the laser printer. The label printer
  was only ever exercised through a refusal assertion.
- No module server left running; oMLX never restarted or unloaded on any host.
- Rebase-only, linear history, feature branch fast-forwarded.

---

## 8. Follow-ups — all closed

### 8.1 ✅ RESOLVED — m1's github key

**You asked me to fix this mid-run.** Root cause: it was a fleet
inconsistency, not a broken key.

| host | key | passphrase | unattended github |
|---|---|---|---|
| m5 | `<hostkey>` | none | works |
| neo | `<hostkey>` | none | works |
| **m1** | `<hostkey>` | **has one** | **fails** |

m1's key needed its passphrase from the login Keychain, which is only
reachable while that Keychain is unlocked — so it worked early in the session
and stopped ~20 minutes later, mid-deploy.

Per your choice, m1 now has a **new passphrase-less per-host key**, matching
m5 and neo. The old key is preserved at
`~/.ssh/<hostkey>.passphrase-backup-<date>` (and `.pub`).

**Registered by the user 2026-08-04 and verified working:**

```
m1 $ ssh -T git@github.com
Hi <owner>! You've successfully authenticated…
m1 $ cd ~/dotfiles && git pull --rebase     →  <sha0>..<sha2>  main -> origin/main
m1 $ cd <module A checkout> && git pull   →  Already up to date.
```

m1 now pulls the private repos unattended, matching m5 and neo. During the run
(before the key existed) m1 was brought current via `git bundle` over ssh from
m5 — a proper git operation landing identical SHAs — so deployment was never
blocked. m1's module A remote was also switched from https to `git@` to match
its other repos.

### 8.2 ✅ RESOLVED — the print job

CUPS accepted it (`Create-Job successful-ok`, `Send-Document successful-ok`,
55 KB, 08:19:52), the active queue drained, and the printer returned to idle
at 08:20:16. But `lpstat -W completed` does **not** show job 77 — this host
does not preserve job history (numbering jumps 8 → 77 with nothing between,
`PreserveJobHistory` unset in `cupsd.conf`, `error_log` untouched since July).

So the plan's acceptance test "`lpstat -W completed` on m1 shows the one real
job" **cannot be satisfied on this host as written** — not a failure, a host
config fact. Everything up to the spooler was verified in software.

**The user confirmed the page physically printed.** The §6.4 requirement —
exactly one real print job, one page, laser printer only — is met.

### 8.3 ✅ RESOLVED — `luxe smoke` on m5 (and m1, which was also broken)

**Originally reported as pre-existing and out of scope; the user asked me to
fix it, and the real cause turned out to be a one-line host fix.**

Plan §6 asks for `luxe smoke` exit 0 on m5. It exited 1, and did so before any
of my work — verified by checking out baseline `6e870d2` into a worktree and
running its own smoke: identical failure.

```
✗ fallback turn — Qwen3.6-27B-6bit: oMLX returned 409:
  Model 'Qwen3.6-27B-6bit' failed to load: VLM load failed:
  No module named 'transformers.models.qwen3_vl';
  LLM fallback also failed: No module named 'transformers.models.qwen3_5'.
```

Everything else passed (manifest, weights ×3, endpoint, catalog, main turn,
tool call).

**It was not a missing dependency.** The installed oMLX venv already had it:
`Cellar/omlx/0.5.7/libexec` carries transformers 5.12.1, whose `models/`
directory contains both `qwen3_vl` and `qwen3_5`. Upgrading `transformers`
would have been the wrong move.

**Actual cause — the 2026-08-03 stale-process bug, recurring one day later
with a different mask.** A `brew upgrade omlx` (0.5.5 → 0.5.7) at 06:42
deleted the 0.5.5 Cellar tree while launchd kept running the 0.5.5 process
(started 22:39 the night before). It was importing from a deleted
site-packages whose older transformers predates those modules.

```
lsof -p 76834 | grep -o "Cellar/omlx/[0-9.]*"  →  0.5.5
ls -d /opt/homebrew/Cellar/omlx/*/             →  0.5.7   (created 06:42)
```

**m1 was in the same state and nobody had noticed** — running 0.5.4 against an
installed 0.5.7, three versions stale, with its fallback turn broken too. That
means the loud-auto-degrade path the fallback kit exists to provide was
silently unavailable on the primary interactive host. neo runs no oMLX (it
serves a 4B GGUF through the llama.cpp router), so it was unaffected.

**Fix:** `brew services restart omlx` on both.

| host | before | after |
|---|---|---|
| m5 | NOT READY, exit 1 | **READY (8s), exit 0** — fallback turn 2.9s |
| m1 | stale 0.5.4, fallback broken | **READY (19s), exit 0** — fallback turn 6.7s |

Recorded in `lessons.md` (`d033f0f`). The previous entry's rule was too
narrow — it named `[Errno 2]` as the tell. Generalised: **any "impossible"
import or file error from a brew-managed long-running service is the
stale-process signature, whatever the errno.** If the installed venv
demonstrably has what the running process cannot import, the running process
is not using the installed venv. Check `lsof` against the installed version
*before* installing or upgrading anything — and check the whole fleet, since
this recurrence was silent on m1 until someone looked.

### 8.4 ✅ DONE — the stale-process check now ships (`0ba1324`)

Nothing detected the §8.3 condition; it bit twice in two days and sat silently
on m1 the second time. Now `luxe.staleproc` answers the question directly and
both `/doctor` and `luxe smoke` carry an **`oMLX build`** line.

```
✓ oMLX endpoint    local http://127.0.0.1:8000
✓ oMLX build       0.5.7 (matches installed)
```

and, against a simulation of the real 08-04 state:

```
⚠ oMLX build — pid 76834 is running 0.5.5, but 0.5.7 is what's installed —
               brew replaced the tree underneath it — `brew services restart omlx`
```

Primary basis is `lsof -Fn` (definitive — the paths the process actually holds
open); when that is mute it falls back to comparing process start time against
the tree's ctime and says which basis it used.

Three design choices, all pinned by tests:

- **WARN, never FAIL.** A stale process may still be serving everything asked
  of it, and the real checks fail on their own if it isn't. The point is that
  when they do, the cause is already on screen. A stale build alone does not
  flip smoke's exit code.
- **Inconclusive prints nothing.** Service not running, formula not
  brew-installed, `lsof` permission-denied — none of those are a clean bill of
  health, so they must not render as one. `conclusive` is a separate field
  from `stale` for exactly that reason.
- **Local endpoints only.** A remote host's process table is its own doctor's
  problem — `--backend m5` must not probe this box and report on that one.

Offline-pure (pgrep/lsof/stat), so it doesn't touch doctor's one-networked-line
budget. Never raises — the guard is in the module, not just the call sites,
because a diagnostic that can take down `/doctor` during an outage is worse
than no diagnostic.

Contracts updated in `chat.sdd` (both the `/doctor` and `luxe smoke` bullets).
19 new tests; suite **2114 pass**. Verified on all three hosts:

| host | verdict |
|---|---|
| m5 | conclusive via lsof — `0.5.7 (matches installed)` |
| m1 | conclusive via lsof — `0.5.7 (matches installed)` |
| neo | inconclusive, prints nothing — no oMLX (llama.cpp router) ✓ correct |

### 8.5 Smaller notes

- **Two known WARNs remain on module A**, both pre-existing and both
  documented in that project's own README: one missing optional input, and a
  staleness warning that is an rsync artifact rather than a real mismatch.
  Clearing the second would overwrite hand-edited data, so I left it alone.
- **neo's non-interactive ssh PATH lacks `/opt/homebrew/bin`.** Interactive
  shells are fine, so `luxe chat` on neo finds qpdf; only unattended `ssh neo
  <cmd>` needs `export PATH=/opt/homebrew/bin:$PATH`. Worth adding to neo's
  `.zshenv` if you want cron-style access.
- **`luxe-pdf` overlaps `pdfsign`** on purpose — `pdfsign` remains the fast
  one-shot CLI; `luxe-pdf` is the same capability inside a conversation,
  alongside filling, page ops, and printing.
