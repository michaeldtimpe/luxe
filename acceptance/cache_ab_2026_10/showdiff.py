import json, subprocess, sys, os
d = json.load(open(os.path.expanduser("~/luxe-bench-tmp/cache-ab/acceptance/cache_ab_2026_10/verify_index.json")))
for key in sys.argv[1:]:
    v = d[key]
    print(f"===== {key}  ({v['branch']})")
    print(subprocess.run(["git", "-C", v["repo"], "diff", f"{v['base']}..{v['branch']}"], capture_output=True, text=True).stdout)
