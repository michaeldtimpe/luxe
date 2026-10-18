#!/usr/bin/env bash
# luxe control arm for the harness bake-off's deps-audit fixture: luxe's own
# maintain path, champion, 3 reps, under the SAME offline sandbox the outside
# harnesses run in (loopback only). luxe's reference deps-audit runs were
# online with a working cve_lookup; the outside harnesses all fabricated CVEs
# offline, so this is the run that makes that finding comparable.
#
# cve_lookup keeps a 24h disk cache (~/.luxe/cve_cache) that earlier online
# runs warmed; the profile denies reading and writing it so the control starts
# cold like the outside harnesses (the cache code treats both as a miss). The
# cache itself is never touched.
#
#   OUT=acceptance/harness_ab_2026_10 scripts/harness_ab_luxe_offline.sh
set -u
cd "$(dirname "$0")/.."
: "${OUT:?set OUT to the acceptance dir}"
dest="$OUT/luxe-offline/champ"
mkdir -p "$dest"
cache=$(cd ~/.luxe && pwd -P)/cve_cache
profile="$dest/offline.sb"
cat >"$profile" <<EOF
(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
(deny file-read-data (subpath "$cache"))
(deny file-write* (subpath "$cache"))
EOF
# .venv's python directly: `uv run` may try to reach the index offline.
/usr/bin/sandbox-exec -f "$profile" .venv/bin/python -m benchmarks.maintain_suite.run \
  --id nothing-ever-happens-manage-deps-audit \
  --variants benchmarks/maintain_suite/variants_baseline_3rep.yaml \
  --output "$dest" --per-fixture-timeout 1800
