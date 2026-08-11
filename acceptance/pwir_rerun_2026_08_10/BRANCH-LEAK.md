# maintain_suite leaks branches and kills every fixture at ~99 runs

Found 2026-08-10 when an A/B failed for reasons that had nothing to do with
what it was measuring. **Not fixed** — the fix is a code change that wants its
own validation, and the cleanup is a destructive choice that is the user's.

## The failure

Three treatment runs of `nothing-ever-happens-document-config` came back
`rc=1`, no run_id, scored as failures:

```
File "src/luxe/pr.py", line 413, in plan_branch_name
    raise PRError(f"Cannot find a free branch name based on `{base}`")
luxe.pr.PRError: Cannot find a free branch name based on
  `luxe/document/add-a-config-md-at-the`
```

`plan_branch_name` walks `base`, `base-2`, … `base-99` and gives up:

```python
while _branch_exists_local(repo, candidate) or _branch_exists_remote(repo, candidate):
    candidate = f"{base}-{n}"
    n += 1
    if n > 99:
        raise PRError(...)
```

The fixture-cache repo held **98 branches** for that one goal slug. Nothing
prunes them: every bench run commits the agent's work to a new branch and
leaves it there, in both the workspace clone and the fixture-cache origin.

## Why it silently corrupts results rather than just erroring

The harness records the failure as a fixture failure with a score of 0. In an
A/B the arms run sequentially, so the arm that runs **second** absorbs the
exhaustion:

| | baseline (ran first) | treatment (ran second) |
|---|---|---|
| document-config | 2/3 pass | **0/3 — all PRError** |
| suite score | 116/150 | 108/150 |

That reads exactly like "the treatment broke two runs". It is arm ordering. Any
A/B run near the limit will manufacture a false regression in whichever arm
goes second, and the scoreboard gives no hint that the cause is infrastructural
— the fixture simply looks like it failed.

## Current backlog

`luxe/*` branches per fixture-cache repo, 2026-08-10:

| repo | branches | closest slug to the cap |
|---|---|---|
| lpe-rope-calc | 147 | — |
| neon-rain | 144 | — |
| the-game | 144 | — |
| isomer | 141 | — |
| **nothing-ever-happens** | 114 | **98/99 — already fatal** |

Plus 42–50 per workspace clone. `nothing-ever-happens` is only the first to
hit the wall; the others are one heavy bench cycle behind. Historical
`deluxe/*` and `whetstone/*` prefixes show the same accumulation from earlier
tools.

## Safety note for whoever cleans this up

Every fixture's pinned `base_sha` is reachable from `main` in its
fixture-cache repo — verified with `git branch --contains` across all ten
fixtures. So deleting `luxe/*` branches cannot orphan a fixture's base commit.

The cost of deleting is inspectability: CLAUDE.md's bench-as-truth rule says to
inspect every PASS by hand via the local-branch ref, and those branches are
what that means. Deleting ~690 of them throws away the diffs of every past
bench run.

## Options, none taken

1. **Prune with a retention window** — keep the N most recent branches per
   slug, delete older. Preserves recent inspectability, bounded loss.
2. **Harness cleanup** — delete the run's branch after grading has read the
   diff. Stops the leak at the source; needs care that grading and the sidecar
   regrade (`scripts/regrade_local.py`) are both done with the ref first.
3. **Recycle instead of failing** — `plan_branch_name` reuses the oldest slot
   at the cap rather than raising. Cheapest change, but silently overwrites a
   previous run's work.
4. **Per-run prefix** — what this A/B did as a workaround: set
   `branch_prefix` to something fresh, run, restore. Non-destructive, but it
   moves the problem rather than solving it and leaves more namespaces behind.

Recommended: (2) plus a one-off (1). That stops the growth and keeps a useful
window of history.

## Workaround used tonight

`configs/pr.yaml` `branch_prefix` set to `pwir2` for the duration of the
re-run, restored by a shell trap on EXIT/INT/TERM. Verified afterwards:
`branch_prefix: "luxe"`, `git status` clean, backup file removed. No branches
were deleted.
