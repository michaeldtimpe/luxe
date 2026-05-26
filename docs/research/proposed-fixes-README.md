# Proposed drift fixes — index

This folder holds **proposed** doc/contract changes for the drift items
surfaced in `e5-instruction-contract-drift-report.md`. **Nothing has been
applied to the luxe tree** — these are drafts to apply during an m5-quiet
window.

## What's here

| File | Targets | What it does |
|---|---|---|
| `proposed-AGENTS.md` | **D1 (critical)** | Full replacement for `AGENTS.md`. Replaces the 286-line stale swarm-pipeline reference with a ~45-line mono-only architecture summary + pointers to the `.sdd` chain and other docs. |
| `proposed-sdd-and-claude-additions.md` | **D2, D3 (medium)** | Two surgical additions: positive Must clauses in `luxe.sdd` pinning the champion model, and a new "Opt-in modes" section in `CLAUDE.md` surfacing the reflect / adaptive-policy / cohort-priors invariants that currently live only in `agents.sdd`. |

## How to apply (m5-quiet window)

These commands are written to be run *from a luxe checkout that isn't
currently the m5's active workspace*. Sanity-check first.

```sh
# 1. Verify no in-flight work in your local luxe checkout.
cd /Users/michaeltimpe/Downloads/luxe
git status --short                  # expect: empty
git log --oneline -1                # confirm you're on the version you want

# 2. D1 — replace AGENTS.md (286 → ~45 lines).
cp ../agentic-patterns-luxe-research/proposed-AGENTS.md AGENTS.md
git diff AGENTS.md                  # review

# 3. D2, D3 — hand-apply the two patches in proposed-sdd-and-claude-additions.md.
#    They are intentionally small enough not to need a unified-diff tool.
#    Edit src/luxe/luxe.sdd and CLAUDE.md with the additions shown.
git diff src/luxe/luxe.sdd CLAUDE.md  # review

# 4. Single commit, no force-push.
git add -A
git commit -m "docs: fix instruction/.sdd drift (D1 AGENTS.md mono-rewrite; D2 champion pin; D3 opt-in modes)"
```

## What's *not* here (deferred low-priority drift)

The other drift findings from E5 (D4-D8) are low-severity and don't have
proposed fixes drafted:

- **D4 (low):** "Prompts via `prompts.py`" stated in 3 places. Fix is to
  designate one canonical source and have the others say "see ...". Defer
  until next time any of the three is edited.
- **D5 (low):** Tool-guard order only stated in `tools.sdd`. Fix is a
  one-line restatement in `CLAUDE.md` under the existing `.sdd` chain
  pointer block. Trivial; not drafted to keep this batch focused.
- **D6 (low):** Bench rules only summarized in `maintain_suite.sdd`. Same
  shape as D5.
- **D7 (low):** `OMLX_API_KEY` value hardcoded in `CLAUDE.md`. Likely
  intentional for local dev; revisit only if `CLAUDE.md` becomes part of
  any public/exported surface.
- **D8 (low):** `forbids_create` injection mechanism only in `CLAUDE.md`.
  Procedural infrastructure; single-source acceptable.

## Verification after applying

```sh
# Confirm the four .sdd files still parse (they're plain markdown but the
# pytest suite validates section headers).
pytest tests/test_sdd.py tests/test_tools.py -q

# Confirm CLAUDE.md / AGENTS.md / .sdd line counts are sensible.
wc -l CLAUDE.md AGENTS.md src/luxe/luxe.sdd src/luxe/agents/agents.sdd \
      src/luxe/tools/tools.sdd benchmarks/maintain_suite/maintain_suite.sdd

# Spot-check the champion pin landed in the right place.
grep -n "Qwen3.6-35B-A3B-6bit" src/luxe/luxe.sdd CLAUDE.md
```

## Out of scope for this batch

- Re-evaluating the deliberate divergences (no MCP, mono-only, no model
  routing) — those remain valid principled choices per the audit.
- Implementing any of G1 (graceful context lifecycle), G2 (cost alerts),
  or G3 (KV-cache feasibility). The E1 + A + F evidence sharpens the case
  for G1 but the build is a separate cycle.
- Adding an automated drift-lint (methodology section M1–M7 in
  `e5-instruction-contract-drift-report.md`). Worth building once the
  doc set settles, not before.
