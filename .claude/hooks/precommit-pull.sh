#!/usr/bin/env bash
# PreToolUse(Bash) hook: before a `git commit`, rebase onto the latest remote so history
# stays linear (this repo's main forbids merge commits). Best-effort + NON-blocking:
#  - only acts when the Bash command contains "git commit" (plain or compound `cd && git commit`)
#  - GIT_TERMINAL_PROMPT=0 so it fails fast instead of hanging on an auth prompt
#  - re-stages the pending index: `git pull --rebase --autostash` restores its stash UNSTAGED
#    (stash apply ignores the index), which would otherwise empty the commit it precedes
#  - on any failure (no upstream / offline / rebase conflict) it aborts the rebase to restore a
#    clean state, warns via systemMessage, and exits 0 so the commit still proceeds
# Pairs with repo-local `git config pull.rebase true` + `rebase.autoStash true`.
input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // ""' 2>/dev/null)
case "$cmd" in
  *"git commit"*)
    staged=$(mktemp 2>/dev/null) || staged="/tmp/claude-precommit-staged.$$"
    git diff --cached --name-only -z >"$staged" 2>/dev/null
    if GIT_TERMINAL_PROMPT=0 git pull --rebase --autostash >/tmp/claude-precommit-pull.log 2>&1; then
      [ -s "$staged" ] && xargs -0 git add -- <"$staged" 2>/dev/null
    else
      git rebase --abort >/dev/null 2>&1
      [ -s "$staged" ] && xargs -0 git add -- <"$staged" 2>/dev/null
      printf '{"systemMessage":"pre-commit auto git pull --rebase --autostash did not apply (no upstream / offline / conflict) — committing on the current base; see /tmp/claude-precommit-pull.log"}'
    fi
    rm -f "$staged"
    ;;
esac
exit 0
