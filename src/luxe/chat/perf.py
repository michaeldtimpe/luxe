"""Per-turn prefill/decode rates and prefix-cache hit rate, from the SERVER's
own accounting (AgentResult's `cached_tokens`/`prefill_s`/`decode_s`, filled
by backend._server_perf from oMLX `usage` or llama-server `timings`).

Every field is None when the server did not report it — OpenRouter reports
none of them, and a rate luxe did not measure must not render as a number.
`first_cache` is the turn's OPENING request: each chat turn is a fresh
`run_single`, so that is the request a cross-turn cache would have to serve,
and the one to watch when checking cache health turn over turn.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnPerf:
    prefill_tps: float | None = None
    decode_tps: float | None = None
    cache: float | None = None        # cached / prompt over the turn's steps
    first_cache: float | None = None  # same, opening request only


def turn_perf(result) -> TurnPerf:
    g = lambda name, default=0: getattr(result, name, default) or default  # noqa: E731
    prefill_s, prefill_tok = g("prefill_s", 0.0), g("prefill_tokens")
    decode_s, decode_tok = g("decode_s", 0.0), g("decode_tokens")
    perf_prompt = g("perf_prompt_tokens")
    first_prompt = g("first_step_prompt_tokens")
    first_cached = getattr(result, "first_step_cached_tokens", None)
    return TurnPerf(
        prefill_tps=(prefill_tok / prefill_s
                     if prefill_s > 0 and prefill_tok > 0 else None),
        decode_tps=decode_tok / decode_s if decode_s > 0 and decode_tok > 0 else None,
        cache=g("cached_tokens") / perf_prompt if perf_prompt > 0 else None,
        first_cache=(first_cached / first_prompt
                     if first_cached is not None and first_prompt > 0 else None),
    )


def rate(tps: float) -> str:
    """`3.4k` / `45` — compact tokens-per-second for the bar and footer."""
    return f"{tps / 1000:.1f}k" if tps >= 1000 else f"{tps:.0f}"


def footer_bits(result) -> list[str]:
    """`prefill 3.4k t/s` · `decode 45 t/s` · `cache 82% (turn start 0%)`, each
    present only when the server reported what it needs."""
    p = turn_perf(result)
    bits: list[str] = []
    if p.prefill_tps is not None:
        bits.append(f"prefill {rate(p.prefill_tps)} t/s")
    if p.decode_tps is not None:
        bits.append(f"decode {rate(p.decode_tps)} t/s")
    if p.cache is not None:
        c = f"cache {p.cache:.0%}"
        if p.first_cache is not None and getattr(result, "steps", 0) > 1:
            c += f" (turn start {p.first_cache:.0%})"
        bits.append(c)
    return bits
