"""Pre-flight probe: time oMLX model swap between 7B / 14B / 32B Qwen tags.

Decides single-server vs two-server topology for the agents.yaml hard-bind.
Threshold from the plan: <5s swap latency = single-server OK.

Each call is a 1-token completion via /v1/chat/completions. The first call
for a given model is a cold load; subsequent calls within the same model
are warm. We're interested in the cold-swap walls between models.

Usage: OMLX_API_KEY=... python -m scripts.probe_omlx_swap
"""

from __future__ import annotations

import os
import sys
import time

import httpx


BASE = "http://127.0.0.1:8000"
MODELS = [
    "Qwen2.5-7B-Instruct-4bit",
    "Qwen2.5-32B-Instruct-4bit",
    "Qwen2.5-7B-Instruct-4bit",   # back to 7B → cold swap from 32B
    "Qwen2.5-Coder-14B-Instruct-MLX-4bit",
    "Qwen2.5-7B-Instruct-4bit",   # final round-trip
]


def prewarm_one(model: str, api_key: str) -> float:
    t0 = time.monotonic()
    r = httpx.post(
        f"{BASE}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "ok"}],
            "max_tokens": 1,
            "temperature": 0,
            "stream": False,
        },
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=600.0,
    )
    wall = time.monotonic() - t0
    if r.status_code != 200:
        print(f"  -> HTTP {r.status_code}: {r.text[:200]}", flush=True)
    return wall


def main() -> int:
    key = os.environ.get("OMLX_API_KEY", "").strip()
    if not key:
        print("OMLX_API_KEY unset", file=sys.stderr)
        return 2
    walls: list[tuple[str, float]] = []
    for i, m in enumerate(MODELS, 1):
        print(f"[{i}/{len(MODELS)}] prewarm {m} ...", flush=True)
        wall = prewarm_one(m, key)
        walls.append((m, wall))
        print(f"  wall={wall:.2f}s", flush=True)
    print()
    print("Summary:")
    for i, (m, w) in enumerate(walls, 1):
        tag = "(cold)" if i == 1 else "(swap)" if i > 1 and walls[i - 2][0] != m else "(warm)"
        print(f"  {i}. {m:<42} {w:6.2f}s  {tag}")
    swap_walls = [w for i, (_, w) in enumerate(walls, 1) if i > 1 and walls[i - 2][0] != walls[i - 1][0]]
    if swap_walls:
        worst = max(swap_walls)
        print(f"\nWorst swap: {worst:.2f}s  threshold=5.00s  → {'OK' if worst < 5 else 'OVER'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
