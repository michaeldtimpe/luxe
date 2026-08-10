# Plan: luxe MCP capability modules — pdf-tools + two private modules

> **Redacted for the public repo.** Phases 3 and 4 built two modules for
> private projects; they are **summarised here, not reproduced** — enough to
> record what was built, deployed and verified, without describing what those
> projects are. Everything about Phase 2 (the public PDF module), the
> luxe-repo findings, and the fleet/infrastructure lessons is unchanged;
> remaining private repo, host-user and printer names are placeholders. The
> unredacted original sits beside this file as `plan.private.md`, which the default
> `acceptance/*` ignore keeps out of git.

**Executor:** Opus 5, running autonomously from m5 in `~/Downloads/luxe`.
**Verifier:** a separate Claude session will double-check everything against §8 after you finish — write the report it expects (§9).
**User decisions already made (do not re-ask):**
- The three capabilities ship as **opt-in MCP server modules** invocable from `luxe chat`/`luxe code`.
- **Only the PDF module is public** (lives in the luxe repo). Modules A and B are **private** — no private hostnames, paths, project names, or private tooling in the public luxe repo.
- All three modules must work on **all three hosts: m5, m1, neo**. neo is the **backup host** for module A and must be genuinely ready, not just "installed".
- PDF scope: convert, **unlock own PDFs** (remove owner-password/permission "Adobe blocks" — very common on government forms that are marked read-only yet intended to be filled), fill forms, print.
- Verification is **full-live including exactly one real print job** (one page, laser printer only — never the Acme_QL_820NWB label printer).

---

## 0. Ground truth (verified 2026-08-04 — re-verify cheaply, don't re-derive)

| | m5 (this box) | m1 | neo |
|---|---|---|---|
| user / ssh | `<user>` (local) | `<user-m1>` (`ssh m1`) | `<user>` (`ssh neo`) |
| role | dev box, 128 GB, runs oMLX :8000 | primary module A/B machine | **backup host for module A** |
| brew / uv | yes / yes | yes / (check) | **NO brew, NO uv**, system python3 only (macOS 26.5.2) |
| qpdf, poppler, sox, ffmpeg | all present | all present | **all MISSING** |
| private module A checkout | **MISSING** | present, git clean | present (data dirs gitignored — verify what's synced) |
| private module B support repo | **MISSING** | present | **MISSING** |
| private data workspace | no | yes (**NOT a git repo** — copies only, never commit/push it) | no |
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
3. Clone and build the private support repo module B needs, the same way dotfiles `install.sh` does on m1 (read that script; mirror it, don't invent).
4. Make sure `~/dotfiles` is current (`git -C ~/dotfiles pull --rebase`).

### 1b. m5
1. Clone module A's private repo (SSH remote; the https remote on m1 is fine too).
2. Clone/build module B's support repo (as on neo).
3. Add the laser printer: `lpadmin -p Acme_LaserDoc_2350 -E -v "ipp://<printer-host>/ipp/print" -m everywhere`. Discover the address first (`dns-sd -B _ipp._tcp local.` briefly, or read `lpstat -v` on m1 to copy its device URI). If the printer isn't reachable from m5's network segment, skip and note it — m1 remains the print-verified host.
4. `uv --version`, `qpdf --version` etc. already present — just confirm.

### 1c. m1
1. `luxe update` (reconcile the diverged luxe checkout — it may contain an unpushed local commit `a48d47e`; if it's ahead of origin, **stop and report** rather than discard).
2. Confirm `uv` present (install via brew if not).

Gate: on every host, `qpdf --version && sox --version && ffmpeg -version && pdftotext -v` all succeed, and module B's support venv runs.

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

## 3. Phase 3 — private module A (its own private repo)

**Summarised for the public record.** This phase specified an MCP server that
wraps an existing private application: a thin layer over that project's own
entry points and a local HTTP API, reimplementing none of it. Eight tools —
six read-only (status, preflight, logs, hardware diagnostics) and two
`gate_tools`-gated lifecycle operations — plus a `docs/MCP.md` in that repo
holding the troubleshooting knowledge, so the model has it at session time
rather than needing it pasted in.

Deployment: commit and push in the private repo, pull on the other hosts,
create the venv via that project's own setup script. Several of its data
directories are gitignored by design and were synced out of band.

The full specification stays in the private repo and in this file's
unredacted original. It is omitted here because it describes a private
project, not because anything about it failed.

---

## 4. Phase 4 — private module B (lives in dotfiles)

**Summarised for the public record.** A second MCP server, hosted in the
private dotfiles repo with its own uv-managed venv and a `setup.sh` the
dotfiles install flow calls. It wraps a documented multi-step file-processing
pipeline whose written specification was copied into the module as
`PLAYBOOK.md` and became the source of truth for tool behaviour. Eight tools —
four read-only inspection/verification, four gated operations that write files
— each mapping to a numbered section of that playbook, with parameters derived
per input file rather than hardcoded.

The source workspace stays on a single host, is not a git repo, and is never
committed anywhere. Full specification omitted here for the same reason as
Phase 3.

---

## 5. Phase 5 — Registration + wrappers (opt-in UX)

1. Extend `~/dotfiles/luxe/relays.yaml` (keep the filename; wrappers reference it) with three **stdio** server entries so any combination composes in one session:
   - `pdf`: `command: <luxe repo venv>/bin/luxe-pdf-mcp` (or `uv run --project ~/Downloads/luxe luxe-pdf-mcp`), gate_tools as §2.
   - module A: its own venv python + module entry point, gate_tools covering its two lifecycle operations.
   - module B: its venv python + module, gate_tools covering its four file-writing operations.
   Paths differ per host **only** in $HOME — use paths relative to `~` if luxe's config loader expands them (check `chat` MCP config code; if it doesn't expand `~`, add expansion or use per-host absolute paths via env interpolation).
2. New wrappers in `~/dotfiles/bin` (modeled on `luxe-relay`/`luxe-chat`), one per module — each `exec`s `luxe chat --repo "$PWD" --mcp <name> --mcp-config ~/dotfiles/luxe/relays.yaml "$@"` so extra `--mcp` flags stack.
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

**Private modules A and B (all hosts):** verification steps summarised — each module's tools were exercised end-to-end on every host against scratch copies, never against source data, with the servers stopped afterwards. Detail omitted here; see the unredacted original.

**Luxe repo health:** full `uv run pytest` green on m5; `luxe smoke` still exits 0 on m5; benchmark path untouched (no diffs outside the new module + docs + configs comment).

---

## 7. Safety rails (hard rules — violating any of these is a failed run)

- **Never modify an original**: the private data workspace on m1, module A's originals, any project files. All pipeline verification happens on copies in scratch dirs.
- The private data workspace is not a git repo — never `git init`/commit/push it, never copy its contents into any repo except the single `PLAYBOOK.md` into private dotfiles.
- Nothing private (hostnames, tailnet names, project or data names, printer names, module-B tooling) lands in the **luxe public repo** — code, tests, comments, or commit messages.
- No secret values in YAML/code anywhere.
- Exactly **one** physical print job; laser printer only.
- Don't leave module A's server running after verification; don't restart/unload the oMLX server on any host.
- If m1's diverged luxe commit (`a48d47e`) is ahead of origin, stop and report — don't discard work.
- Rebase-only git; feature branches; luxe suite green before push.

---

## 8. Acceptance criteria (what the verifier will independently re-test)

1. On each of m5/m1/neo: the three wrappers exist and start a chat session whose tool list contains that module's tools (gated ones appear after `/write`).
2. PDF: a qpdf-restricted government-form-style fixture can be unlocked, filled, flattened, and (argv-verified) printed on every host; `lpstat -W completed` on m1 shows the one real job.
3. Private module A: its lifecycle and diagnostic tools work on all three hosts, with no FAIL lines other than hardware-absent WARNs.
4. Private module B: its end-to-end operation on a scratch copy passes every gate its playbook defines, on all three hosts.
5. luxe test suite green; `configs/mcp.yaml` default servers still `[]`; no private strings in the public diff (verifier greps the luxe diff against the fleet's private-token list).
6. Report file exists (§9) and matches reality.

## 9. Handoff report (required)

Write `acceptance/mcp_modules_2026_08/REPORT.md` on m5 containing: per-phase outcomes, per-host verification matrix (command → result), the luxe commit SHAs pushed, private-repo commit SHAs, deviations from this plan with reasons, and anything left WARN (e.g. m5 printer unreachable) as an explicit list. The verifier session starts from that file.
