#!/bin/bash
# Stall watcher for an A/B run dir (prints one line per event; meant for a Claude Monitor).
# Usage: watch_stall.sh <run_dir> [stall_minutes=40]
# Events: every new arms.log line (start/end/DONE, rc!=0 flagged), STALL when no
# *.log in <run_dir> has grown for <stall_minutes>, GONE when no bench process is
# alive and arms.log has no DONE. Exits after DONE or GONE.
D=$1; LIM=${2:-40}
seen=$(wc -l < "$D/arms.log" 2>/dev/null || echo 0)
stalled=0
while true; do
  n=$(wc -l < "$D/arms.log" 2>/dev/null || echo 0)
  if (( n > seen )); then
    tail -n $((n - seen)) "$D/arms.log" | while read -r l; do
      case $l in *rc=0*) echo "$l";; *rc=*) echo "FAIL $l";; *) echo "$l";; esac
    done
    seen=$n
  fi
  grep -q '^DONE' "$D/arms.log" 2>/dev/null && { echo "FINISHED $(tail -1 "$D/arms.log")"; exit 0; }
  newest=$(find "$D" -maxdepth 1 -name '*.log' ! -name arms.log -mmin -"$LIM" | head -1)
  if [[ -z $newest ]]; then
    (( stalled == 0 )) && echo "STALL no log growth in ${LIM}m ($(date '+%F %T')); last: $(tail -1 "$D/arms.log")"
    stalled=1
  else
    (( stalled == 1 )) && echo "RESUMED $(date '+%F %T')"
    stalled=0
  fi
  if ! pgrep -f 'benchmarks.(maintain_suite|swebench).run' >/dev/null \
     && ! pgrep -f 'run_arms.sh|swebench_slice.sh' >/dev/null; then
    sleep 60
    pgrep -f 'benchmarks.(maintain_suite|swebench).run|run_arms.sh|swebench_slice.sh' >/dev/null \
      || { echo "GONE no bench process and no DONE ($(date '+%F %T'))"; exit 1; }
  fi
  sleep 60
done
