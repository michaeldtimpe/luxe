#!/usr/bin/env python3
"""v1.8 diligence — 3-rep BFCL `multiple` category at temp=0 with explicit
oMLX restart between reps. Tests whether the -4.49pp regression on `multiple`
in v17 C.8 was variance or a substrate interaction (prefix-cache leakage being
the leading hypothesis).

Each rep gets a fresh oMLX process — `brew services restart omlx` kills the
in-process model + KV cache + prefix cache atomically. Eliminates cache
contamination as a cross-rep variance source. Reproducible.

Go/no-go gate after run:
  - Reps cluster ≥92% (within 1pp of v1.6's 92.99%): regression was noise,
    proceed to v1.8 code work.
  - Reps stable at 88-89%: hidden substrate interaction. Halt v1.8 and chase
    prefix-cache contamination at the oMLX/MLX boundary first.

Output: acceptance/bfcl/diligence_multiple/rep_{1,2,3}/
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


N_REPS = 3
OUTPUT_ROOT = Path("acceptance/bfcl/diligence_multiple")
OMLX_API_KEY = "omlx-sdb25582k3mq8pf9"


def restart_omlx() -> None:
    print(f"[{time.strftime('%H:%M:%S')}] Restarting oMLX...", flush=True)
    subprocess.run(["brew", "services", "restart", "omlx"], check=True)
    time.sleep(5)  # warmup
    # Sanity-check responsive
    proc = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "http://localhost:8000/v1/models",
         "-H", f"Authorization: Bearer {OMLX_API_KEY}"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.stdout.strip() != "200":
        raise RuntimeError(f"oMLX not responding after restart: HTTP {proc.stdout}")
    print(f"[{time.strftime('%H:%M:%S')}] oMLX up.", flush=True)


def run_rep(rep: int) -> None:
    out_dir = OUTPUT_ROOT / f"rep_{rep}"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(f"/tmp/diligence_multiple_rep{rep}.log")

    print(f"[{time.strftime('%H:%M:%S')}] Starting rep {rep}/{N_REPS}, "
          f"output={out_dir}, log={log_path}", flush=True)
    env = {**os.environ, "OMLX_API_KEY": OMLX_API_KEY}

    with log_path.open("w") as f:
        result = subprocess.run(
            [".venv/bin/python", "-m", "benchmarks.bfcl.run",
             "--mode", "agent",
             "--categories", "multiple",
             "--output", str(out_dir)],
            env=env, stdout=f, stderr=subprocess.STDOUT,
        )
    if result.returncode != 0:
        raise RuntimeError(f"rep {rep} failed: rc={result.returncode}, see {log_path}")
    print(f"[{time.strftime('%H:%M:%S')}] Rep {rep} done.", flush=True)


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    for rep in range(1, N_REPS + 1):
        restart_omlx()
        run_rep(rep)
    print(f"[{time.strftime('%H:%M:%S')}] All {N_REPS} reps complete.", flush=True)

    # Quick summary — parse summary.json from each rep
    print()
    print("=== Diligence summary ===")
    import json
    rates = []
    for rep in range(1, N_REPS + 1):
        summary_path = OUTPUT_ROOT / f"rep_{rep}" / "summary.json"
        if not summary_path.is_file():
            print(f"  rep {rep}: summary.json missing")
            continue
        s = json.loads(summary_path.read_text())
        rate = s["categories"]["multiple"]["pass_rate"]
        n = s["categories"]["multiple"]["n"]
        passed = s["categories"]["multiple"]["passed"]
        rates.append(rate)
        print(f"  rep {rep}: {passed}/{n} = {rate*100:.2f}%")

    if rates:
        spread = max(rates) - min(rates)
        avg = sum(rates) / len(rates)
        print()
        print(f"  avg={avg*100:.2f}% spread={spread*100:.2f}pp")
        print(f"  v1.6 agent baseline: ~92.99%")
        print(f"  v1.7 C.8 datapoint:  88.50%")
        if avg >= 0.92:
            print(f"  GATE: PASS (avg ≥92%) — proceed to v1.8 code work.")
        elif spread < 0.02:
            print(f"  GATE: SUBSTRATE ALARM (avg <92%, spread <2pp) — "
                  f"prefix-cache contamination or substrate interaction. "
                  f"Halt v1.8 until oMLX/MLX boundary investigated.")
        else:
            print(f"  GATE: AMBIGUOUS (avg <92% but spread ≥2pp) — "
                  f"high variance; consider rep 4-5 or investigating "
                  f"sampler nondeterminism.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
