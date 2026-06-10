#!/usr/bin/env python
"""Phase 5 Step 1 — oMLX concurrency throughput probe (gitkit wave design gate).

Measures whether concurrent chunk-style requests give AGGREGATE throughput on
the single oMLX server, before any parallel-wave code lands in gitkit:

  arm A: serial    — 6 identical mid-size requests, one at a time
  arm B: c=2       — same 6 requests, ThreadPoolExecutor(max_workers=2)
  arm C: c=4       — same 6 requests, max_workers=4

Request shape ≈ a deep chunk pass: ~2k-token prompt, max_tokens=512, temp 0.
Metrics per arm: total wall, aggregate completion tok/s, per-request mean/p95
latency, and the PROJECTED DEEP-RUN WALL for an N-chunk run (the decision
metric — per-request latency is secondary for wave execution).

GATE A (precommitted): c=2 aggregate throughput >= 1.3x serial, else the
wave-based --workers feature is closed as measured-no-win (fallbacks: the
incremental re-audit is the primary wall reducer; prefix-cache probe;
chunk-budget retune; per-chunk output budget).

Run ONLY in a bench-free slot (single server — concurrent bench traffic
corrupts the timings):
  OMLX_API_KEY=... .venv/bin/python scripts/probe_omlx_concurrency.py
Optional: --requests 6 --max-tokens 512 --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from luxe.backend import Backend

_CHUNK_S_SEQUENTIAL = 235      # calibrated sequential per-chunk wall (deep.py)


def _mk_prompt(tokens: int = 2000) -> str:
    # ~4 chars/token filler shaped like code review context; deterministic.
    block = ("def handler_%04d(request):\n"
             "    token = request.headers.get('authorization', '')\n"
             "    if not token: return error(401)\n"
             "    return process(request.body)\n")
    body = "".join(block % i for i in range(tokens // 36))
    return ("Review the following code for serious bugs and security issues. "
            "List each finding with file:line evidence.\n\n" + body)


def _one(backend: Backend, prompt: str, max_tokens: int) -> dict:
    t0 = time.monotonic()
    resp = backend.chat(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens, temperature=0.0)
    wall = time.monotonic() - t0
    ctok = int(getattr(getattr(resp, "timing", None), "completion_tokens", 0) or 0)
    return {"wall_s": wall, "completion_tokens": ctok}


def run_arm(backend_factory, prompts: list[str], workers: int,
            max_tokens: int) -> dict:
    t0 = time.monotonic()
    if workers <= 1:
        results = [_one(backend_factory(), p, max_tokens) for p in prompts]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_one, backend_factory(), p, max_tokens)
                    for p in prompts]
            results = [f.result() for f in futs]
    total_wall = time.monotonic() - t0
    walls = [r["wall_s"] for r in results]
    toks = sum(r["completion_tokens"] for r in results)
    return {
        "workers": workers,
        "n": len(results),
        "total_wall_s": round(total_wall, 2),
        "aggregate_tok_per_s": round(toks / total_wall, 2) if total_wall else 0,
        "completion_tokens": toks,
        "latency_mean_s": round(statistics.mean(walls), 2),
        "latency_p95_s": round(sorted(walls)[max(0, int(0.95 * len(walls)) - 1)], 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--requests", type=int, default=6)
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--prompt-tokens", type=int, default=2000)
    ap.add_argument("--model", default="Qwen3.6-35B-A3B-6bit")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--json", dest="json_out", default="")
    args = ap.parse_args()

    api_key = os.environ.get("OMLX_API_KEY", "")

    def factory() -> Backend:
        return Backend(base_url=args.base_url, model=args.model, api_key=api_key)

    # distinct prompts per request index (avoid trivially identical-cache walls
    # being the whole story; shared large prefix mirrors real chunk prompts)
    base_prompt = _mk_prompt(args.prompt_tokens)
    prompts = [base_prompt + f"\n\n(chunk {i})" for i in range(args.requests)]

    print("warmup (loads the model; excluded from timings)…")
    _one(factory(), "warmup", 8)

    out: dict = {"config": vars(args), "arms": []}
    for workers in (1, 2, 4):
        label = "serial" if workers == 1 else f"c={workers}"
        print(f"\narm {label}: {args.requests} requests…")
        arm = run_arm(factory, prompts, workers, args.max_tokens)
        out["arms"].append(arm)
        print(f"  total {arm['total_wall_s']}s · aggregate "
              f"{arm['aggregate_tok_per_s']} tok/s · latency mean "
              f"{arm['latency_mean_s']}s p95 {arm['latency_p95_s']}s")

    serial, c2 = out["arms"][0], out["arms"][1]
    ratio = (c2["aggregate_tok_per_s"] / serial["aggregate_tok_per_s"]
             if serial["aggregate_tok_per_s"] else 0.0)
    out["gate_a"] = {"c2_vs_serial_throughput": round(ratio, 3),
                     "threshold": 1.3, "pass": ratio >= 1.3}
    # projected deep-run wall for an N-chunk run, scaled by the wall ratio
    speedup = (serial["total_wall_s"] / c2["total_wall_s"]
               if c2["total_wall_s"] else 0.0)
    out["projection"] = {
        "wall_speedup_c2": round(speedup, 3),
        "projected_20_chunk_run_serial_min": round(20 * _CHUNK_S_SEQUENTIAL / 60),
        "projected_20_chunk_run_c2_min":
            round(20 * _CHUNK_S_SEQUENTIAL / max(speedup, 1e-9) / 60, 1),
    }

    print(f"\nGATE A: c=2 aggregate = {ratio:.2f}x serial "
          f"(threshold 1.3x) → {'PASS' if out['gate_a']['pass'] else 'FAIL'}")
    print(f"wall speedup c=2: {speedup:.2f}x · projected 20-chunk deep run: "
          f"{out['projection']['projected_20_chunk_run_serial_min']} min serial → "
          f"{out['projection']['projected_20_chunk_run_c2_min']} min at c=2")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(out, fh, indent=2)
        print(f"json -> {args.json_out}")
    return 0 if out["gate_a"]["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
