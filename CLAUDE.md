# Claude Code instructions for luxe

Auto-loaded at session start. Points at the durable contracts and the
short list of project-specific gotchas.

## Architecture: SpecDD Lever 2 `.sdd` chain

Every directory of consequence has a `<dir>/<dir>.sdd` contract listing
**Must / Must not / Owns / Forbids**. Walk the chain when editing:

- `src/luxe/luxe.sdd` — root invariants (no swarm/micro/phased; temp=0; pinned work_dir; no MoE Instruct-2507; no `origin/<branch>` reads)
- `src/luxe/agents/agents.sdd` — prompt registry is the single source of truth
- `src/luxe/tools/tools.sdd` — honesty guards + Forbids enforcement order
- `benchmarks/maintain_suite/maintain_suite.sdd` — bench rules (vacuous_test gates, `--keep-loaded`, sidecar regrade)

Read the relevant `.sdd` before editing any file under that subtree.

## When working on this repo

1. **Mono only.** No swarm/micro/phased — they're retired. Don't add
   feature flags to bring them back. The `Forbids:` rules in
   `src/luxe/luxe.sdd` are tool-side enforced.
2. **Prompts go through `src/luxe/agents/prompts.py`.** Never inline
   prompt strings in `single.py` or `cli.py` — variant cells un-couple
   from runtime and the bake-off becomes uninterpretable.
3. **Bench-as-truth.** Don't trust paper analysis. Run
   `python -m benchmarks.maintain_suite.run --variants <yaml>` and
   inspect every PASS by hand via the local-branch ref. See
   `RESUME.md §The bench-as-truth pattern`.
4. **`oMLX` is on `localhost:8000`** with key `OMLX_API_KEY=omlx-sdb25582k3mq8pf9`.
5. **Read `RESUME.md` first** for current project state and active tasks.
6. **Read `lessons.md`** for postmortems of every historical surprise.

## When the user asks for new work

Default to the established patterns:
- New tools land with regression tests in `tests/test_tools.py`
- New prompt variants land with tests in `tests/test_prompts.py`
- New fixtures land with a `requirements:` block (SpecDD Lever 1 schema)
- New `.sdd` files follow `<dir>/<dir>.sdd` placement

When in doubt, look at how the most recent shipped feature did it and
match the shape.
