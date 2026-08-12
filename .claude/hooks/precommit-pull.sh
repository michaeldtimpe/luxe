#!/usr/bin/env bash
# PreToolUse(Bash) hook: before a `git commit`, rebase onto the latest remote so history
# stays linear (this repo's main forbids merge commits). Best-effort and non-blocking on
# PULL failure — but BLOCKING when proceeding would commit without the user's work:
#  - only acts when the Bash command contains "git commit" (plain or compound `cd && git commit`)
#  - GIT_TERMINAL_PROMPT=0 so it fails fast instead of hanging on an auth prompt
#  - re-stages the pending index from a SAVED PATCH (`git diff --cached --binary` +
#    `git apply --cached`), not by re-adding file names — name-based re-add staged whole
#    files and silently flattened hunk-split commits (2026-08-12); falls back to the
#    name-based add only if the patch no longer applies after a rebase moved the base
#  - STRANDED-AUTOSTASH GUARD (2026-08-12): `git pull --rebase --autostash` exits 0 even
#    when re-applying the autostash CONFLICTS — git prints "Your changes are safe in the
#    stash" and leaves them there. That parked uncommitted work in stash@{0}, the commit
#    ran against a clean tree, and nothing said so (observed twice same day; the morning
#    "stale autostash" on m5 was this mechanism's residue). The hook now compares stash
#    depth across the pull: a stranded entry is popped; if the pop itself conflicts, the
#    half-applied state is reset (the stash still holds everything) and the hook EXITS 2
#    to block the commit with instructions — committing without the work is worse than
#    interrupting the commit
#  - on pull failure (no upstream / offline / rebase conflict) it aborts the rebase to
#    restore a clean state, warns via systemMessage, and exits 0 so the commit proceeds
# Pairs with repo-local `git config pull.rebase true` + `rebase.autoStash true`.
input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // ""' 2>/dev/null)
case "$cmd" in
  *"git commit"*)
    log=/tmp/claude-precommit-pull.log
    staged_patch=$(mktemp 2>/dev/null) || staged_patch="/tmp/claude-precommit-staged.$$.patch"
    staged_names=$(mktemp 2>/dev/null) || staged_names="/tmp/claude-precommit-staged.$$"
    git diff --cached --binary >"$staged_patch" 2>/dev/null
    git diff --cached --name-only -z >"$staged_names" 2>/dev/null
    stash_before=$(git stash list 2>/dev/null | grep -c .)

    pull_ok=0
    if GIT_TERMINAL_PROMPT=0 git pull --rebase --autostash >"$log" 2>&1; then
      pull_ok=1
    else
      git rebase --abort >>"$log" 2>&1
    fi

    # Stranded-autostash guard: if the pull grew the stash, the autostash
    # re-apply conflicted and the user's uncommitted work is parked there.
    stash_after=$(git stash list 2>/dev/null | grep -c .)
    if [ "$stash_after" -gt "$stash_before" ]; then
      if git stash pop >>"$log" 2>&1; then
        : # restored — fall through to re-staging below
      else
        # Pop conflicted too. Clear the half-applied conflict state (the
        # stash RETAINS the work — pop keeps the entry on conflict), then
        # block the commit: it would otherwise run against a tree missing
        # the user's changes and either commit wrong content or "nothing".
        git reset --hard -q >>"$log" 2>&1
        rm -f "$staged_patch" "$staged_names"
        echo "precommit-pull hook: the auto-rebase autostash could not be re-applied — your uncommitted changes are intact in stash@{0} (see 'git stash list'). Run 'git stash pop', resolve any conflict, then re-run the commit. Log: $log" >&2
        exit 2
      fi
    fi

    # Re-stage what was staged, preserving exact hunks — but only when the
    # cycle actually disturbed the index. A no-op pull ("Already up to date"
    # with no autostash) leaves the index intact, and re-applying the patch
    # on top of itself fails into the whole-file fallback, which is the very
    # flattening this patch path exists to prevent.
    if [ -s "$staged_patch" ]; then
      if ! git diff --cached --binary 2>/dev/null | cmp -s - "$staged_patch"; then
        git reset -q >>"$log" 2>&1   # unstage whatever the cycle left
        if ! git apply --cached --whitespace=nowarn "$staged_patch" >>"$log" 2>&1; then
          # Rebase moved the base under the patch — whole-file re-add is the
          # correct-content fallback (loses the split, keeps the commit).
          [ -s "$staged_names" ] && xargs -0 git add -- <"$staged_names" 2>/dev/null
        fi
      fi
    fi
    rm -f "$staged_patch" "$staged_names"

    if [ "$pull_ok" -eq 0 ]; then
      printf '{"systemMessage":"pre-commit auto git pull --rebase --autostash did not apply (no upstream / offline / conflict) — committing on the current base; see /tmp/claude-precommit-pull.log"}'
    fi
    ;;
esac
exit 0
