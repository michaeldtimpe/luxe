# `luxe code` drill log — hygiene sweep 2026-07-30

Measuring what the fallback kit can actually repair, on real cards from
this sweep's queue. Host m1, interactive main `Qwen3.6-35B-A3B-4bit`
(per-host manifest — deliberately not the champion).

**Result: 1 pass / 2 attempts.** Both attempts also produced findings about
the drill *apparatus* that matter more than the pass rate.

---

## HS-005 — delete an unused import · **PASS**

Worktree `/private/tmp/luxe-drill-HS-005`, `luxe code` (write on, bash
gated). 4 turns, ~42s for the working one.

The agent read the file, confirmed `os` was unused, and removed exactly one
line. Diff was minimal and correct; `ruff check src` went 6 → 5 findings,
matching the count RESUME.md documents as deliberate. Applied to main after
independent review as `bb49706`.

## HS-006 — split an overloaded local · **FAIL (agent introduced a crash)**

Worktree `/private/tmp/luxe-drill-HS-006`, `luxe code --dev` (bash on so it
could run pytest). 7 steps, 6 tool calls, 62.7s.

The agent made the right four edits — introduced `skip_map_io`, moved both
sentinel assignments, widened the guard to
`if not cached and not skip_map_io:` — and reported *"All 109 tests pass."*

It had introduced an `UnboundLocalError`. In diff mode (`chunks_override is
not None`) the `else` branch never runs, so after moving the sentinel
nothing assigns `cached` at all; `not cached` then reads an unbound local on
every `gitaudit --base` / `--pr` deep run. Reproduced directly:

```
UnboundLocalError: cannot access local variable 'cached'
where it is not associated with a value    (deep.py:1288)
```

Opus fixed it directly (`9f18b0b`) by binding `cached: dict | None = None`
before the branch.

---

## The finding that matters: the drill protocol was measuring nothing

**Both the agent's test run and my first verification pass were fooled.**
`tests/test_gitkit_diff.py` already covers this path, and it *does* fail on
the broken code — but it reported 109 green.

Cause: the venv installs luxe as an **editable install pointing at the main
checkout**. Running `python -m pytest` with cwd set to a git worktree still
imports `luxe` from `/Users/michaeltimpe/Downloads/luxe/src`:

```
$ .venv/bin/python -c "import luxe.gitkit.deep as d; print(d.__file__)"
/Users/michaeltimpe/Downloads/luxe/src/luxe/gitkit/deep.py     # NOT the worktree
```

So the agent edited the worktree and then tested the parent — its green run
was real, and irrelevant. Forcing the right source flips it immediately:

```
$ PYTHONPATH=<worktree>/src python -m pytest tests/test_gitkit_diff.py -q
1 failed, 19 passed   # UnboundLocalError, as it should have said all along
```

**The plan's drill protocol (§ Phase 3, steps c–d) is wrong as written.** It
assumes tests run inside the worktree exercise the worktree. They do not.
Any drill verified that way — by the agent or by the executor — is a
rubber stamp. Two required corrections for the next sweep:

1. Drills must run with `PYTHONPATH=<worktree>/src`, or with a per-worktree
   venv (`uv sync` inside it), before any test result is believed.
2. Executor verification must *watch the pinning test fail* on the
   pre-change code in that same environment. "Tests are green" is not
   evidence until you have seen the environment produce a red.

This generalises past luxe: any `git worktree` + editable-install workflow
has it, which is worth a `lessons.md` entry.

## Secondary: piped multi-line prompts split into one turn per line

The README's headless pattern (`printf 'msg\n/quit\n' | luxe chat`) only
supports **single-line** messages. A multi-line task file piped in becomes
one turn per line — the HS-005 drill ran 4 separate turns, three of which
were fragments of the instructions ("3. Do not touch any other file.").

It still succeeded, because turn 1 happened to carry enough. It just as
easily might not have. Either the line REPL should support a heredoc /
end-of-message sentinel for piped input, or the docs should state the
single-line constraint explicitly. Filed as product feedback, not fixed
here — it is a behaviour change to the chat front-end and outside this
sweep's remit.

## Verdict on the fallback kit

Honest read at n=2: the local model handles **mechanical, single-site,
locally-verifiable** edits (HS-005) and is not reliable on **control-flow
refactors whose correctness depends on a path the tests don't obviously
exercise** (HS-006) — it produced a plausible, well-shaped, wrong diff and
believed a green test run that was measuring the wrong tree.

That is a perfectly usable tool during an outage, provided the human reads
the diff. The queue's `size: S` / `tier: C` gate was the right filter; the
failure mode was not the card being too big, it was the *verification loop*
being fake. Fix the loop before drawing conclusions about the model.
