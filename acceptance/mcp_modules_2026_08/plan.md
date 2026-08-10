# Plan: luxe MCP capability modules — pdf-tools · showapp · audio-prep

> **Redacted for the public repo.** Private project, repo, host-user,
> printer and hardware names are replaced with stable placeholders
> (`showapp`, `audioscrub`, `<album>`, `<owner>`, `Acme_*`, `<hostkey>`,
> `<… sha>`). Technical content, findings and verification results are
> unchanged. The unredacted original sits beside this file as
> `plan.private.md`, which the default `acceptance/*` ignore keeps out of git.

**Executor:** Opus 5, running autonomously from m5 in `~/Downloads/luxe`.
**Verifier:** a separate Claude session will double-check everything against §8 after you finish — write the report it expects (§9).
**User decisions already made (do not re-ask):**
- The three capabilities ship as **opt-in MCP server modules** invocable from `luxe chat`/`luxe code`.
- **Only the PDF module is public** (lives in the luxe repo). The show and audio modules are **private** — no private hostnames, paths, show names, or audio tooling in the public luxe repo.
- All three modules must work on **all three hosts: m5, m1, neo**. neo is the **backup show rig** and must be genuinely show-ready, not just "installed".
- PDF scope: convert, **unlock own PDFs** (remove owner-password/permission "Adobe blocks" — very common on government forms that are marked read-only yet intended to be filled), fill forms, print.
- Verification is **full-live including exactly one real print job** (one page, laser printer only — never the Acme_QL_820NWB label printer).

---

## 0. Ground truth (verified 2026-08-04 — re-verify cheaply, don't re-derive)

| | m5 (this box) | m1 | neo |
|---|---|---|---|
| user / ssh | `<user>` (local) | `<user-m1>` (`ssh m1`) | `<user>` (`ssh neo`) |
| role | dev box, 128 GB, runs oMLX :8000 | primary show/audio machine | **backup show rig** |
| brew / uv | yes / yes | yes / (check) | **NO brew, NO uv**, system python3 only (macOS 26.5.2) |
| qpdf, poppler, sox, ffmpeg | all present | all present | **all MISSING** |
| `~/src/audioscrub` | **MISSING** | present (git@github.com:<owner>/<scrub-repo>.git) | **MISSING** |
| `~/Downloads/showapp` | **MISSING** | present, git clean @ `<showapp sha0>` (<show-repo> (private)) | present @ `<showapp sha0>` (repo only — audio/data dirs are gitignored, verify what's synced) |
| `~/Downloads/album-wip` | no | yes (**NOT a git repo** — copies only, never commit/push it) | no |
| `~/dotfiles` | yes (git@github.com:<owner>/dotfiles.git) | yes | yes |
| `~/Downloads/luxe` | yes @ 6e870d2 | yes @ a48d47e (diverged — reconcile via `luxe update`, i.e. fetch+rebase onto origin/main; investigate before discarding anything) | yes (check) |
| printers (lpstat -p) | **none configured** | Acme_LaserDoc_2350 + Acme_QL_820NWB (label — never print to it) | Acme_LaserDoc_2350 |

SSH from m5 to m1/neo works key-based (`BatchMode=yes`). `timeout(1)` does not exist on macOS — don't use it in scripts.

Key luxe facts (from CLAUDE.md / chat.sdd — read them first):
- MCP servers attach **at startup only**: `luxe chat --mcp <name> --mcp-config <path>` (repeatable `--mcp`). Default config `configs/mcp.yaml` ships with `client.servers: []` — keep it that way; public repo gets a **commented** example stanza only.
- Private server registry pattern: `~/dotfiles/luxe/relays.yaml` + thin wrappers in `~/dotfiles/bin` (see `luxe-relay`, `luxe-chat`). Tokens/env resolve via luxe.secrets (env → `~/.luxe/secrets.env` → Keychain). Never a secret value in YAML.
- `gate_tools` (fnmatch list per server) = mutating tools, withheld until `/write`. Servers are isolated per connection; catch `BaseException` at anyio connect boundaries (lessons.md 2026-07-31).
- **Never touch the benchmark path**: no `TOOL_FNS` additions, no core `run_single`/loop changes, no new default-on behavior. New luxe directory ⇒ new `<dir>/<dir>.sdd` contract + tests. Prompts (if any) go through `agents/prompts.py` — but MCP tool descriptions live in the server, which is fine.
- Git: **rebase, never merge**; linear history on origin/main.

---

## 1. Phase 1 — Host provisioning (everything else depends on this)

### 1a. neo (biggest lift)
1. Install Homebrew (non-interactive: `NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`). If Command Line Tools are missing, `xcode-select --install` won't work headless — use `softwareupdate --list` / `softwareupdate -i` for the CLT label. Verify `brew --version` over a fresh ssh (PATH: `/opt/homebrew/bin`).
2. `brew install qpdf poppler sox ffmpeg uv`.
3. Clone `audioscrub` to `~/src/audioscrub` and build/venv it the same way dotfiles `install.sh` step 14 does on m1 (read that script; mirror it, don't invent).
4. Make sure `~/dotfiles` is current (`git -C ~/dotfiles pull --rebase`).

### 1b. m5
1. `git clone git@github.com:<owner>/<show-repo>.git ~/Downloads/showapp` (SSH remote; the https remote on m1 is fine too).
2. Clone/build `audioscrub` at `~/src/audioscrub` (as on neo).
3. Add the laser printer: `lpadmin -p Acme_LaserDoc_2350 -E -v "ipp://<printer-host>/ipp/print" -m everywhere`. Discover the address first (`dns-sd -B _ipp._tcp local.` briefly, or read `lpstat -v` on m1 to copy its device URI). If the printer isn't reachable from m5's network segment, skip and note it — m1 remains the print-verified host.
4. `uv --version`, `qpdf --version` etc. already present — just confirm.

### 1c. m1
1. `luxe update` (reconcile the diverged luxe checkout — it may contain an unpushed local commit `a48d47e`; if it's ahead of origin, **stop and report** rather than discard).
2. Confirm `uv` present (install via brew if not).

Gate: on every host, `qpdf --version && sox --version && ffmpeg -version && pdftotext -v` all succeed; `~/src/audioscrub` venv runs `audioscrub.py --help`.

---

## 2. Phase 2 — `pdf-tools` MCP server (PUBLIC, in the luxe repo)

**Location:** new package in the luxe repo, e.g. `src/luxe/mcp_pdf/` with `mcp_pdf.sdd`, console script `luxe-pdf-mcp` (stdio MCP server), optional extra `[pdf]` in pyproject (`uv sync --extra pdf`). Python deps: `mcp` (server SDK), `pypdf` (forms/merge/split), `reportlab` (text overlay), stdlib subprocess for `qpdf`/poppler/`sips`/`lp`. Shell out to CLI tools rather than heavy python bindings.

**Tools** (names indicative; keep them boring and self-describing):

| tool | does | gated? |
|---|---|---|
| `pdf_info` | pages, encryption status, permission flags, form type (AcroForm/XFA/none), metadata | no |
| `pdf_text` | extract text (pdftotext -layout), returns text | no |
| `pdf_form_fields` | list AcroForm fields: name, type, current value, options | no |
| `pdf_to_images` | pdftoppm → png per page into an output dir | yes |
| `images_to_pdf` | images → single PDF (Pillow or `sips`+quartz filter; pick one, test it) | yes |
| `pdf_merge` / `pdf_split` / `pdf_rotate` | qpdf page ops | yes |
| `pdf_unlock` | **the "Adobe blocks" fix**: `qpdf --decrypt in.pdf out.pdf` removes owner-password restrictions (no-print/no-edit/no-fill). If a *user* (open) password is required, accept an optional `password` arg; without it, fail honestly — that's not crackable and we don't try. Also handle the government-form case: after decrypt, set **NeedAppearances** and drop usage-rights `/Perms` signatures so viewers allow fill+save. | yes |
| `pdf_fill` | fill AcroForm fields from a `{field: value}` mapping (pypdf), optional `flatten=true` (via pdftoppm→images_to_pdf fallback if pypdf flatten is unreliable — test both paths) | yes |
| `pdf_overlay` | place text strings at (page, x, y) coordinates for flat/non-form PDFs (reportlab overlay + merge) | yes |
| `pdf_printers` | `lpstat -p -d` parsed | no |
| `pdf_print` | `lp -d <printer> -o <opts> file` with page-range/duplex/copies; **refuses printers matching `*QL*`/label unless `allow_label=true`** | yes |

Rules: tools take explicit input/output paths; never overwrite the input; outputs default to `<input>-<op>.pdf` siblings. All failure text must say *what to install or toggle* (match luxe's self-explaining-failure convention).

**Registration (public side):** a commented stanza in `configs/mcp.yaml` showing `command: luxe-pdf-mcp, transport: stdio, gate_tools: [pdf_unlock, pdf_fill, pdf_overlay, pdf_print, pdf_merge, pdf_split, pdf_rotate, pdf_to_images, images_to_pdf]`. README section "PDF module" documenting opt-in (`uv sync --extra pdf`, `luxe chat --mcp pdf`). Do **not** enable by default.

**Tests:** `tests/test_mcp_pdf.py` — build synthetic fixtures in-test (reportlab/pypdf can author: a 2-page PDF, an AcroForm with 3 field types, and an **encrypted PDF with owner-password restrictions** via `qpdf --encrypt "" ownerpw 256 --print=none --modify=none --`). Cover: info, text, fields, unlock-removes-restrictions (assert perms open afterwards), fill round-trip (read values back), overlay, merge/split, print **mocked** (assert the `lp` argv, don't spool). Full suite `uv run pytest` must stay green.

**Commit** to luxe on a feature branch → rebase → push per repo discipline. No private anything in this code, docs, or tests.

---

## 3. Phase 3 — `showapp` MCP server (PRIVATE, lives in the showapp repo)

**Location:** inside `~/Downloads/showapp` (private GitHub repo) — e.g. `show_app/mcp_server.py` + a `mcp` dep in its requirements, runnable as `.venv/bin/python -m show_app.mcp_server`. Read `README.md`, `docs/STATUS.md` (if present), `show_app/preflight.py`, `run.sh`, `config/show.json`, `config/ctrl-map.json` first — the server wraps what exists; it does not reimplement.

**Tools:**

| tool | does | gated? |
|---|---|---|
| `show_status` | is the server up (GET `http://127.0.0.1:8090`), current stage if the HTTP API exposes it, pid | no |
| `show_preflight` | run `.venv/bin/python -m show_app.preflight`, return the CLEAR/WARN/FAIL lines parsed | no |
| `show_start` | launch `./run.sh` detached (nohup/launchctl, log to a file), poll :8090 until up (bounded), return the log path. Refuse if already running. | yes |
| `show_stop` | graceful stop of the process it started (pidfile), fallback pkill by cmdline match | yes |
| `show_logs` | tail -n N of the server log | no |
| `audio_doctor` | list output devices (`system_profiler SPAudioDataType -json`), flag whether `config/show.json → output_device` (USB SPDIF) is present, whether a Loopback-style capture device exists; absent hardware = **WARN not FAIL** | no |
| `midi_doctor` | list MIDI sources/destinations (use `python-rtmidi` or `mido` in the showapp venv), flag control-surface presence; absent = WARN | no |
| `midi_sniff` | capture MIDI events for N seconds, report note/CC numbers seen and, where possible, which `ctrl-map.json` entry each matches — this is the documented remedy when "a control reacts in the wrong on-screen spot" | no |

Troubleshooting knowledge (SPDIF fallback behavior, ctrl-map correction workflow, restart-after-preshow-folder-change, analyzer overwrite caveat) goes in the tool descriptions and a `docs/MCP.md` in the showapp repo — that's how the luxe model gets it at session time.

**Deployment:** commit+push in showapp; `git pull --rebase` on m1 and neo; fresh clone on m5 (§1b). Create `.venv` via `run.sh` (it self-installs) or `requirements.txt` on m5/neo.

**Show-readiness data sync (neo and m5):** the audio/data dirs are gitignored on purpose. rsync from m1: `<album>-tracks/`, `data/` (analysis.json), any local `config/` deltas, `unused-audio` **excluded**. Use `rsync -av --dry-run` first, then real. After sync, `show_preflight` on neo must be green except hardware-dependent lines (SPDIF/capture WARN is acceptable; missing wavs/tracklist/analysis is NOT). Same standard for m5.

---

## 4. Phase 4 — `audio-prep` MCP server (PRIVATE, lives in dotfiles)

**Location:** `~/dotfiles/luxe/mcp/audio_prep/` (dotfiles is already the private, all-hosts-synced channel and already owns `release-fn` + the audioscrub install step). Own tiny venv managed by `uv` (needs `mcp`, `numpy`, `soundfile`); a `setup.sh` the dotfiles install flow can call. The **spec is `PLAYBOOK.md`** at `m1:~/Downloads/album-wip/album-tracks/` — copy it into the module as `PLAYBOOK.md` (source of truth for the tools' behavior; keep §-references in tool docs). The album workspace itself stays on m1 and is never committed anywhere.

**Tools** (each maps to a playbook section; sample-rate math derived from the file, never hardcoded):

| tool | playbook | does | gated? |
|---|---|---|---|
| `prep_inspect` | §2–§3 | `sox --i` + head/tail artifact scan (5 ms head windows, 20 ms tail windows), returns amplitudes + a tick/whoosh/natural-ending verdict | no |
| `prep_clean` | §4 | build the premaster: one sox chain (trim→fade→pad→`highpass 28 highpass 28`→`dither -s`) parameterized by the inspect verdict; writes `<track>-<variant>-premaster.wav`, never overwrites source | yes |
| `prep_resample` | §5 | `sox … -b 16 … rate -v -s 44100 dither -s` | yes |
| `prep_scrub` | §5.5 | Phase B: run `audioscrub.py` (defaults from the playbook: `--hf-cutoff 18000 --hf-taper 500 --hf-level -60 --hf-mode replace --lsb-bits 1 --gate-threshold -72 --gate-min-ms 100 --gate-fade-ms 10 --gate-hold-ms 50`) then `stripmeta.py`, from `~/src/audioscrub` directly (do NOT depend on the zsh `release-fn` function — headless reliability). Refuses to scrub a file whose name/stage isn't a release candidate, and refuses double-scrub (detect the ~−0.5 LSB DC-bias fingerprint). | yes |
| `prep_verify` | §6, §6a, §7, §5.5-fingerprint | LUFS/true-peak (ffmpeg ebur128), sox stats (clip/DC), mono mid/side, silencedetect, eqcheck band table, riffscan chunk audit, Phase-B fingerprint (HF floor ≈ −60 dBFS, LSB-1 ≈ 0.497, single DC bias). Returns pass/fail per §6 criteria. | no |
| `prep_track` | §10 | full checklist orchestration source→premaster→release on an explicit work dir; enforces naming (§9) and the two-phase order; stops on any verify failure | yes |

Embed `eqcheck.py`, `riffscan.py`, `wavstrip.py` from the playbook verbatim in the module (the playbook says they also live in the <mastering repo> repo — copying from the playbook text is fine and self-contained).

**Deployment:** commit+push dotfiles → pull on m1/neo → run the module `setup.sh` on all three hosts.

---

## 5. Phase 5 — Registration + wrappers (opt-in UX)

1. Extend `~/dotfiles/luxe/relays.yaml` (keep the filename; wrappers reference it) with three **stdio** server entries so any combination composes in one session:
   - `pdf`: `command: <luxe repo venv>/bin/luxe-pdf-mcp` (or `uv run --project ~/Downloads/luxe luxe-pdf-mcp`), gate_tools as §2.
   - `show`: `command: ~/Downloads/showapp/.venv/bin/python`, `args: [-m, show_app.mcp_server]`, gate_tools `[show_start, show_stop]`.
   - `audio`: the audio_prep venv python + module, gate_tools `[prep_clean, prep_resample, prep_scrub, prep_track]`.
   Paths differ per host **only** in $HOME — use paths relative to `~` if luxe's config loader expands them (check `chat` MCP config code; if it doesn't expand `~`, add expansion or use per-host absolute paths via env interpolation).
2. New wrappers in `~/dotfiles/bin` (modeled on `luxe-relay`/`luxe-chat`): `luxe-pdf`, `luxe-show`, `luxe-audio` — each `exec`s `luxe chat --repo "$PWD" --mcp <name> --mcp-config ~/dotfiles/luxe/relays.yaml "$@"` so extra `--mcp` flags stack.
3. luxe public README: PDF module only. dotfiles README: all three, one line each.
4. Note for neo sessions: neo runs no oMLX — wrapper or docs should point neo's luxe chat at a remote backend (`--backend m5`, already public in configs/chat.yaml).

---

## 6. Phase 6 — Autonomous verification (Opus runs this before handing off)

Run on **each host** (m5 local; m1/neo via ssh). Record every command + outcome in the report (§9).

**PDF (all hosts):**
1. Author a synthetic restricted form: qpdf-encrypt a generated AcroForm PDF with `--print=none --modify=none`.
2. `pdf_info` shows restrictions → `pdf_unlock` → `pdf_info` shows them gone → `pdf_fill` three fields → read values back → `pdf_to_images` renders → `pdf_merge` two files.
3. Headless end-to-end through luxe itself on at least m5: `printf 'Use the pdf tools to report the form fields of <path>\n/quit\n' | luxe chat --repo <scratch> --mcp pdf --mcp-config ~/dotfiles/luxe/relays.yaml` and confirm the tool actually fired (session debug.log).
4. **One real print job, exactly one, total**: from m1, `pdf_print` the filled synthetic one-pager to `Acme_LaserDoc_2350`; verify via `lpstat -W completed` that the job completed. On m5/neo, verify with `lp` argv-level dry checks / `lpstat -p` only.

**showapp (all hosts):**
1. `show_preflight` → parse; m1 expectation: green modulo the known opener-wav gap; neo + m5 after data sync: same standard; hardware lines WARN-only where hardware absent.
2. `show_start` → curl `http://127.0.0.1:8090` returns the <album> page → `show_status` up → `show_stop` → status down. Never leave a server running.
3. `audio_doctor` + `midi_doctor` run and produce sane WARN-level output on hosts without the rig attached.

**audio-prep (all hosts):**
1. Copy ONE real source from m1 (`album-wip/album-tracks/<track>/<track>-*-source.wav`) to a scratch dir on each host (never operate in the album tree). Also synthesize a torture wav (48 kHz tone + head tick + tail noise) to exercise detection.
2. `prep_track` end-to-end → verify the release passes §6 criteria: LUFS ≈ −14, TP ≤ −1 dBTP, flat factor 0, `<25 Hz` band ≥ ~35 dB below `25–60`, riffscan = `fmt +data` only, Phase-B fingerprint present, double-scrub refused.
3. Diff the real track's fresh release against the existing shipped release on m1 (audible-band correlation ≈ 1.0 expected; exact bytes will differ — random seed — that's fine, say so in the report).

**Luxe repo health:** full `uv run pytest` green on m5; `luxe smoke` still exits 0 on m5; benchmark path untouched (no diffs outside the new module + docs + configs comment).

---

## 7. Safety rails (hard rules — violating any of these is a failed run)

- **Never modify an original**: <album> sources/premasters/releases on m1, showapp originals, any a DAW project file. All pipeline verification happens on copies in scratch dirs.
- The <album> workspace is not a git repo — never `git init`/commit/push it, never copy its contents into any repo except the single `PLAYBOOK.md` into private dotfiles.
- Nothing private (hostnames, tailnet names, show/track/album names, printer names, audio tooling) lands in the **luxe public repo** — code, tests, comments, or commit messages.
- No secret values in YAML/code anywhere.
- Exactly **one** physical print job; laser printer only.
- Don't leave the showapp server running after verification; don't restart/unload the oMLX server on any host.
- If m1's diverged luxe commit (`a48d47e`) is ahead of origin, stop and report — don't discard work.
- Rebase-only git; feature branches; luxe suite green before push.

---

## 8. Acceptance criteria (what the verifier will independently re-test)

1. On each of m5/m1/neo: `luxe-pdf`, `luxe-show`, `luxe-audio` wrappers exist and start a chat session whose tool list contains the module's tools (gated ones appear after `/write`).
2. PDF: a qpdf-restricted government-form-style fixture can be unlocked, filled, flattened, and (argv-verified) printed on every host; `lpstat -W completed` on m1 shows the one real job.
3. showapp: preflight + start + HTTP 200 + stop works on all three hosts; neo's preflight is show-ready (no FAIL lines other than hardware-absent WARNs and the known opener gap if still unfilled).
4. audio-prep: `prep_track` on a scratch copy produces a release passing every §6/§6a/§7 gate + Phase-B fingerprint on all three hosts.
5. luxe test suite green; `configs/mcp.yaml` default servers still `[]`; no private strings in the public diff (verifier will grep the luxe diff for `<tailnet>|m1|m5|neo|<album>|showapp|Acme|audioscrub`).
6. Report file exists (§9) and matches reality.

## 9. Handoff report (required)

Write `acceptance/mcp_modules_2026_08/REPORT.md` on m5 containing: per-phase outcomes, per-host verification matrix (command → result), the luxe commit SHAs pushed, dotfiles + showapp commit SHAs, deviations from this plan with reasons, and anything left WARN (e.g. m5 printer unreachable, opener wav still missing) as an explicit list. The verifier session starts from that file.
