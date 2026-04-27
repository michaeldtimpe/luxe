#!/usr/bin/env bash
# Live overview of the catchup run. Refreshes every 5s. Shows the last
# few catchup.log lines (which step we're on) plus per-subtask progress
# of the most recently created /review task.
#
# Usage:  bash scripts/tail_progress.sh
# Quit with Ctrl-C.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

LOG="${LOG:-catchup.log}"
PY=.venv/bin/python

while :; do
    clear
    echo "=== $(date '+%Y-%m-%d %H:%M:%S')  ${LOG} (last 8 lines) ==="
    if [ -f "$LOG" ]; then tail -8 "$LOG"; else echo "(no $LOG yet)"; fi

    # Sort by name (T-YYYYMMDDTHHMMSS-… is chronological lexically).
    # `\ls` bypasses any user alias to eza/exa.
    latest=$(\ls "$HOME/.luxe/tasks/" 2>/dev/null | grep "^T-" | sort -r | head -1)
    echo
    echo "=== latest task: ${latest:-<none>} ==="
    if [ -n "$latest" ] && [ -f "$HOME/.luxe/tasks/$latest/state.json" ]; then
        TASK_PATH="$HOME/.luxe/tasks/$latest/state.json" "$PY" - <<'EOF'
import json, os
p = os.environ["TASK_PATH"]
d = json.load(open(p))
print(f"  task status: {d.get('status')}")
total = sum(s.get("wall_s", 0) for s in d.get("subtasks", []))
print(f"  cumulative wall_s: {total:.1f}  ({total/60:.1f} min)")
for s in d.get("subtasks", []):
    sid = s["id"].rsplit(".", 1)[-1] if "." in s["id"] else s["id"][-2:]
    title = (s.get("title", "") or "")[:70]
    err = (s.get("error", "") or "")[:50]
    line = f"  {sid:>3s}  {s.get('status','?'):>10s}  wall={s.get('wall_s',0):>6.1f}s  {title}"
    if err:
        line += f"  ERR: {err}"
    print(line)
EOF
    fi
    sleep 5
done
