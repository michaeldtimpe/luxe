# K2-Horizon-3.7B probe on neo — report (2026-09-03)

Same-day probe of IFM/MBZUAI's [K2 Horizon](https://ifm.ai/blog/k2/) release
(six Apache-2.0 models, 2026-09-03) against **neo** (A18 Pro, 8 GB, luxe over
llama.cpp `llama-server`, champion `Qwen3-4B-Instruct-2507-Q4_K_M`, ctx 16384,
q8_0 KV). User-requested as an explicit **smoke-it-delete-it-note-it probe**,
not a bake-off: luxe's single-champion policy is untouched, nothing here is a
promotion candidate, and no `configs/`, `hosts:`, or manifest file changed.
Artifacts (command log + raw output) are under `artifacts/`; see
`artifacts/COMMANDS.md` for the full transcript this report summarizes.

---

## 1. Verdict

**Works end-to-end through luxe on neo, zero plumbing failures** — the first
model besides the champion to clear neo's real code drill
(`luxe smoke --chat --code`). **NOT promoted.** n=1 drill set, ~0.75 GB heavier
resident than the champion, and it depends on an unmerged llama.cpp fork — a
production swap would mean rebuilding the router binary, which re-arms the
Background Task Management cdhash gate documented in
`acceptance/luxe_neo_unification_2026_08/REPORT.md` § 2.3.

**Re-open condition:** k2-horizon lands in upstream llama.cpp **and** a re-run
of the retired neo-llm-bench battery (or an n≥3 drill set) is explicitly
requested.

**Also flagged, not probed:** the 36B-A4B MoVA sibling is the m5-class model in
this release, but is blocked on `mlx-lm`/oMLX support — no MLX conversion path
exists yet for the k2-horizon architecture.

---

## 2. Gate table

| Gate | Verdict | Basis |
|---|---|---|
| **G-BUILD** | PASS | Upstream llama.cpp has no k2-horizon arch. Built IFM's fork `MBZUAI-IFM/llama.cpp` branch `model/K2Horizon`, HEAD `35999d1` (2026-09-01), clean, ~5 min, production CMake flags (Metal, Accelerate). Built in an isolated `~/k2probe/llama.cpp`; production `~/code/llama.cpp` and `~/.local/bin/llama-server` (cdhash-pinned, § 2.3 of the unification report) never touched. |
| **G-WEIGHTS** | PASS | HF ships only `K2-Horizon-4B-BF16.gguf` (10.13 GB); no pre-quantized GGUF. Self-quantized with the fork's own `llama-quantize` to Q4_K_M: 2999 MiB, 4.97 BPW, 58 s. BF16 deleted immediately after. GGUF metadata: 5.06B params (the "3.7B" name excludes the 250,624-token vocab embeddings), `n_ctx_train` 524288. |
| **G-SANITY** | PASS | `llama-cli`: coherent one-sentence answer, correct. 69.3 t/s prompt, 18.4 t/s generation. |
| **G-SERVE** | PASS | Served on :8081 with the production `[*]` preset block copied verbatim (16384 ctx, q8_0 KV, jinja). Worker RSS 4.41 GB idle, 4.49 GB after a tool call — fits 8 GB but is ~0.75-0.83 GB heavier than the champion's 3.66 GB resident (per the unification report's ctx-ladder measurement). |
| **G-TOOLCALL** | PASS | Tool calls structured natively: `finish_reason: "tool_calls"`, empty `content`, real id + JSON args. Reasoning goes to `reasoning_content` (deepseek format) and never leaks into `content`. |
| **G-LUXE-LOCAL** | PASS (Arm A) | `luxe ready` exit 0 (only warning: `update`, unrelated to the probe). `luxe smoke` exit 0, 3 s. `luxe smoke --chat --code` exit 0, **38 s** — chat 2 steps/1 tool call/7s, code 7 steps/8 tool calls/30s, pytest green, exactly `calc.py` changed. |
| **G-REASONING-HIGH** | PASS, no benefit (Arm B) | Same drill with `--reasoning on --reasoning-effort high` (unbudgeted, `max_tokens_per_turn` 8192): exit 0, **39 s** — code 6 steps/8 tool calls/28s. Same fix, same wall, no quality delta inside a 16k box. |
| **G-VERIFY** | PASS | Hand-verified both arms (scratch repos preserved via `artifacts/drill_keep.py`'s monkeypatch, not the drill's own delete-on-success path). Both produced the one-line real fix `return a - b` → `return a + b`; test file untouched; `--stat` 1 file +1/−1; pytest 2 passed. `events.jsonl` both arms: `tool_reject` 0, `textfallback_drop` 0, `terminal_turn_truncated` 0, `turn_error` 0, `aborted` false, compaction `max_phase` 0. |
| **G-CLEANUP** | PASS | Fork checkout + all weights deleted, disk back to 350 GiB free. Production router re-bootstrapped; `luxe ready` against the real `neo.yaml` exit 0. `~/dotfiles/luxe` clean. |

---

## 3. Side-by-side vs the champion's own drill

Both numbers are `luxe smoke --chat --code`, cold engine, no flags, on neo.
Champion figure is from `acceptance/luxe_neo_unification_2026_08/REPORT.md` § 4.4.

| | Champion (Qwen3-4B-Instruct-2507-Q4_K_M) | K2-Horizon-3.7B, Arm A (reasoning off) | K2-Horizon-3.7B, Arm B (reasoning high) |
|---|---|---|---|
| Total wall | 39 s | 38 s | 39 s |
| Chat drill | 2 steps / 1 tool call / 13 s | 2 steps / 1 tool call / 7 s | 2 steps / 1 tool call / 10 s |
| Code drill | 6 steps / 9 tool calls / 25 s | 7 steps / 8 tool calls / 30 s | 6 steps / 8 tool calls / 28 s |
| Result | pytest green, exactly `calc.py` changed | pytest green, exactly `calc.py` changed | pytest green, exactly `calc.py` changed |
| Worker RSS (resident) | 3.66 GB | 4.41 GB idle / 4.49 GB post-call | same |

Both finished via `post_write_idle_exit` (idle_tools=3) in the hand-verify pass:
the model verifies after writing but doesn't conclude on its own — 3 of 5 bash
calls in that trace returned `bytes_out=0`. This is a generic small-model
trait, not specific to K2-Horizon; not scored against it here.

**Reasoning=high bought nothing inside a 16k box.** Same output, same wall
inside noise. Thinking cost 7.6× the completion tokens on a plain one-sentence
question (175 vs 23 tokens) — it bites conversational turns, not this
tool-latency-bound drill, where wall time is dominated by tool round-trips
rather than token generation.

---

## 4. How reasoning was controlled — no luxe changes

The fork exposes native `llama-server` flags: `--reasoning on|off|auto`,
`--reasoning-effort`, `--reasoning-budget`, `--reasoning-format`, and the
router (`--models-preset`) propagates them per section. These were used
directly (`artifacts/k2.yaml` = Arm A / reasoning off at the preset level,
`artifacts/k2-high.yaml` = Arm B / reasoning on + effort high) instead of
`chat_template_kwargs`, since luxe has no kwargs plumbing to the request body
for that. No luxe code was touched to run either arm.

---

## 5. luxe quirk found (not fixed)

`luxe ready` on an `engine: llama-server` backend still probes
`~/.omlx/models/<id>/` for the weights-on-disk check, even though that whole
path is oMLX-specific plumbing (see `chat/inspection.py`'s per-engine checks
added in the neo-unification work). neo already carries pointer-only entries
there for its GGUFs as a shim for exactly this kind of check; the K2 probe
needed a matching entry (`~/.omlx/models/K2-Horizon-3.7B/`), created for the
run and removed at cleanup. Recorded here as a doctor-gap candidate, not
fixed — scope of this probe was smoke-and-delete, not a diagnostics patch.

---

## 6. What's in `artifacts/`

Copied verbatim from the probe session (~232 KB, 21 files):

- `COMMANDS.md` — full step-by-step command transcript (source of truth for
  everything in this report)
- `clone.log`, `build.log`, `fork-sha.txt` — fork clone + build
- `download.log`, `quantize.log`, `quantize-summary.txt` — BF16 fetch + Q4_K_M
  self-quantize
- `cli-sanity.log` — `llama-cli` coherence + throughput check
- `k2-models.ini` — probe llama-server preset (port 8081, `[*]` block copied
  from production `neo-models.ini`)
- `k2.yaml` (Arm A, reasoning off) / `k2-high.yaml` (Arm B, reasoning on/high)
  — probe luxe chat configs, modeled on `~/dotfiles/luxe/neo.yaml`
- `server.log`, `server-armB.log` — llama-server startup + request logs for
  both arms
- `ready.log`, `smoke.log` — `luxe ready` / `luxe smoke` output
- `smoke-agentic-armA.log`, `smoke-agentic-armB.log` — `luxe smoke --chat
  --code` output, both arms
- `drill_keep.py`, `drill-keep-armA.log`, `drill-keep-armB.log` — the
  hand-verify harness (monkeypatches the drill to preserve its scratch repo
  instead of deleting it on success) and its output for both arms
- `tmo` — a `timeout`-equivalent shim script (neo's coreutils lacks
  `timeout`); used to bound every capped command in the transcript
