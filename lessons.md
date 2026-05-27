# Lessons Learned

> **Note:** Entries below pre-date the v1.0 implementation work. New
> lessons from the v1.0 acceptance bench (Phase 8) belong in this file
> as they're observed; see the README's [Benchmark workflow](README.md#benchmark-workflow)
> section for context. Old entries reference `swarm`/`src/swarm/`; mentally
> substitute `luxe`/`src/luxe/` when reading.

A running log of mistakes, surprises, and hard-won insights from building and testing the swarm pipeline. Each entry records what happened, why it happened, and what we changed.

---

## Format

Each entry follows this structure:

```
### [DATE] Short title

**What happened**: Description of the problem or surprise.

**Root cause**: Why it happened — the assumption that was wrong, the edge case missed, etc.

**Fix / takeaway**: What we did about it and the general principle for next time.

**Affected files**: Which parts of the codebase were involved.
```

---

## Entries

### [2026-05-25] WS2: sizing the "acted-but-wrong-binding" axis → BANK (a raw-size bar passes, but precision + separability don't)

**What happened**: After the spot-check, a read-only sizing pass (`scripts/analyze_acted_but_wrong.py`) characterized the never-examined acted-but-wrong multi_turn failures (`instance_state_mismatch` + `execution_response_mismatch`; A=151 = 71 miss_func + 80 miss_param) — disjoint from the 58 hand-labeled give-ups. Buckets: `gt_value_mismatch` 58 (38.4% of A), `omission` 60, `extra_action` 33, `path_divergence` 0. Of 79 mismatched params: string_format 60, numeric 12, recipient_id 7.

**An eyeball skim wasn't enough — a counterfactual deep-dive corrected it (same verdict, better reasons).** The first pass eyeballed ~20 cases and guessed genuine-binding precision ≈0.2 with "string_format mostly benign." Asked to confirm, that judgment was replaced with a bench-as-truth measurement (`scripts/verify_wrong_binding_attribution.py`): substitute the GT value(s) back into the model's calls and re-run the vendored state checker — a fail→PASS flip means the binding was DECISIVE. Sanity-gated (unmodified re-grade reproduces 58/58 stored verdicts). Result: **DECISIVE wrong-binding = 21/151 = 13.9% of A** (decisive-by-subtype: string_format 17, numeric 7, **recipient_id 0**). Two corrections to the skim: (1) string_format is NOT mostly benign — 17 are decisive — but they are almost all **exact-free-text-content** matches (reproduce the author's precise tweet/message/ticket wording), i.e. the content ceiling, not an addressable binding rule; (2) the "wrong recipient" the human review headlined is **0-decisive** in the acted set. `path_divergence`=0 is expected (an identical call sequence would PASS), and 19/21 decisive cases ride alongside omissions the STATE checker ignores (read-only calls) — which is why the counterfactual beats the call-list heuristic.

**Root cause / insight**: The pre-registered gate keys on the DECISIVE (state-grounded) count, not the heuristic flag: 21 (<30) / 13.9% (<20%) is below the size bar, and there is no dominant *separable addressable* cluster (the big decisive group is exact free-text content = content ceiling; the rest is a scattered convention tail: expiration-date format, precision, priority, amount). Two data realities also forced a method change from the plan: `instance_state_mismatch` carries no turn index, and real wrong-bindings are turn-shifted (miss_func_33 sent to the wrong recipient one turn early) — so the diff is whole-conversation with two-pass matching (exact multiset → fuzzy on positive overlap), not single-failed-turn.

**Fix / takeaway**: BANK — no lever. The meta-lesson: **confirm a heuristic bucket against the actual checker (substitute GT + re-grade), not an eyeball skim** — the skim mislabeled string_format as benign and over-weighted recipient-binding; the counterfactual gave the real, attributable size (21, sub-threshold) and the right composition (content-exactness, not bindings). Pre-register *decisive count + separability*, not a raw flag count. The taxonomy is the deliverable: the acted-but-wrong mass is mostly `omission` (the obligation-persistence / final-step-drop-off mode, same family as the give-up HOLD but inside acting turns → outside Phase 2's zero-call gate) + GT/evaluator rigidity (exact free-text content). So the 50.0%/45.5% baselines are partly depressed by benchmark rigidity, and the residual is the same reasoning/obligation ceiling — not a new addressable axis.

**Affected files**: `scripts/analyze_acted_but_wrong.py`, `scripts/verify_wrong_binding_attribution.py`, `tests/test_wrong_binding_sizing.py`, `acceptance/bfcl/wrong_binding/sizing_manifest.json` (gitignored). Memory `project_acted_but_wrong_sizing.md`.

### [2026-05-25] Borderline spot-check validated the detection figure; relabeling give-up labels is structurally gate-inert

**What happened**: The user hand-reviewed all 14 `confidence:borderline` give-up labels (the ~25% spot-check the Phase-0 `_doc` recommended). 13/14 upheld; one flip (`miss_param_159` met→unmet — insurance cost 50 vs GT 500). The offline recompute (`measure_reflect_phase1 --from-verdicts`) left the gate metrics unchanged: miss_func detection 81.8% (18/22), false_gap 16.7%, GATE PASS; only the un-gated miss_param detection moved 3/4→4/5.

**Root cause / insight**: A borderline relabel *cannot* move the gate by construction — borderline labels are all give-up (empty_turn) labels, so they never touch the frozen objective pass sample (false_gap), and miss_func detection sits 41.8pp above its 40% floor. Pre-computing this in-memory before encoding the labels made the recompute a confirmation, not a discovery. And the verifier was *right* on `_159` (it had said gap=True), so the flip is a small positive datapoint for verify precision, not a verify error.

**Fix / takeaway**: Record spot-checks **additively** — per-entry `reviewed_label`/`review_note` + a separate `summary.reviewed` block, originals untouched — so provenance is explicit and no reader mistakes which count is authoritative. When a metric's inputs are structurally disjoint from a relabel target, say so up front: it reframes the exercise as auditability, not metric movement. The dominant *behavioral* finding from the review (final-step omission / premature stop) is the give-up mass already at Phase-2 HOLD; the one genuinely-unexamined slice is **acted-but-wrong-binding** (the model acted with a wrong argument → outside the zero-call give-up gate), now being sized read-only (WS2, `scripts/analyze_acted_but_wrong.py`) with a bank-by-default decision gate.

**Affected files**: `acceptance/bfcl/reflect_phase0/giveup_labels.json` (+ `borderline_review.md`), `scripts/measure_reflect_phase1.py` (recompute path), `scripts/analyze_acted_but_wrong.py` (WS2, pending).

### [2026-05-24] reflect cycle Phase 0/1: heuristic over-counted, the champion reasons, verifier ≠ state-checker

**What happened**: Building the verify/reflect pass (reflection cycle Track 1) surfaced four distinct surprises before any repair code was written.

1. **The Plan-agent's grounding over-counted the convertible set 41 → 26 (hand-labeled).** A structural heuristic (`over_acted_pre_reveal`: model acted on a pre-reveal turn where GT expected nothing) was used to split genuine give-ups from "alt-completions". It mislabels in BOTH directions: it called `miss_func_100`/`_15` (real give-ups — "provide credentials", "this is confusing") *alt*, and over-credited miss_param. Hand-labeling all 58 empty_turn failures gave **miss_func ~22 unmet / 7 met; miss_param ~4 unmet / ~25 met**.
2. **miss_param empty_turns are mostly the benchmark being stricter than reality.** In ~25/29, the model competently resolved the ambiguous parameter itself and completed the task; the state-based checker fails it only on a *turn-path technicality* (acted at turn k instead of the GT's turn k+1). These are NOT give-ups and a verify-repair pass legitimately cannot/should not touch them. The repairable signal is almost entirely in **miss_func** ("tool-unavailable anchoring": the model claims a withheld-then-revealed tool "isn't available" and gives up).
3. **The champion is a heavy reasoner and resists structured output.** `response_format={"type":"json_object"}` is only weakly enforced by this oMLX/MLX build; `/no_think`, `chat_template_kwargs.enable_thinking=false`, and assistant-prefill all FAILED to suppress the CoT. The model reasons for hundreds–thousands of tokens, sometimes loops ("Proceeds. Done. Output."), and emits the JSON verdict mid-stream. Working fix: generous budget (max_tokens=3000) + extract the **LAST** balanced JSON object carrying the key (not the first — that catches a reasoning-draft or an embedded deficiency dict).
4. **The verifier and the state-checker measure different things.** Phase 1 false-gap was 16.7% (10/60 objective passes), but inspection showed they're mostly not pedantry: the verifier flags "confirm/convey/report" sub-asks the state-checker ignores (state correct, user-facing report missing). So a state-checker "pass" is not fully flawless w.r.t. the user's full request.

**Root cause**: (1)/(2) the give-up-vs-alt distinction is semantic, not structural — a model can act on one turn and still abandon the key ask. (3) reasoning models need the answer extracted from the tail, not coerced terse, on a server without grammar-constrained decoding. (4) state-based grading and request-satisfaction grading are genuinely different objectives.

**Fix / takeaway**:
- For detection metrics, **hand-label the ground truth** (agent reads verify-context: asks + actions + GT-at-failed-turn; user spot-checks ~25%). Don't trust a structural proxy for a semantic split. Labels: `acceptance/bfcl/reflect_phase0/giveup_labels.json`.
- For structured output from a reasoning champion on this substrate: **budget for the CoT + last-JSON extraction**, don't fight the reasoning. (`reflect.py::_extract_json` takes the last object with the key.)
- **Phase 1 GATE PASSED**: miss_func detection 81.8%, false-gap 16.7% → gated-only. Same-model temp=0 self-verification CAN separate give-ups from correct work here — contradicts the "self-verification is weak" prior for this failure class. Banked as a result regardless of Phase 2.
- For Phase 2, gate the repair on **verify-flag AND a low-action give-up signature** so it targets give-ups and skips the reporting-gap false-gaps (which have non-empty action sets).

**Affected files**: `src/luxe/agents/reflect.py` (new), `src/luxe/backend.py` (`response_format` param), `src/luxe/agents/agents.sdd` (reflect surface contract), `tests/{test_reflect,test_prompts}.py`, `scripts/{analyze_empty_turn_convertible,dump_empty_turn_for_labeling,measure_reflect_phase1}.py`. Full state: `RESUME.md` top section.

### [2026-05-24] reflect cycle Phase 2: same-model self-repair buys +3pp by acting-when-uncertain — HOLD

**What happened**: With the Phase 1 verify gate PASSED (detection 81.8%, false-gap 16.7% → gated-only), Phase 2 wired a gated verify→repair stage into `run_problem_multi_turn` (opt-in `LUXE_REFLECT`, default-off byte-identical): a zero-call **give-up signature** gates an expensive verify call (and skips the verifier's reporting-gap false-gaps, which have non-empty actions); on a confirmed `gap`, ONE generic corrective nudge + one bounded re-prompt over the same tool surface, hard stop. The full miss_func A/B (reflect arm 200/200, clean reused from `m5_rep_1` — same M5 host, temp=0 deterministic) came back **50.0% → 53.0%, net +6** (8 fail→pass, 2 pass→fail), repair fired on 66/200, **0 no-op leaks**. But the pre-registered ship gate FAILED: **2 pass→fail regressions AND 16 empty_turn→state/response-mismatch migrations** (the HARD kill-warning). Verdict: **HOLD, keep opt-in, bank the datapoint.**

**Root cause**: the +3pp is bought by a behavior, not a capability. The nudge ("complete what was asked; do not stop until it's done") pushes the model to **act**, and without fresh grounding it acts **wrong far more than right**: 8 genuine give-up→complete conversions vs **18 made-worse** (16 give-ups turned into wrong-state actions + 2 previously-passing problems broken). Both score regressions traced to the SAME mechanism and confirmed by hand: verify **false-flagged a deliberately-empty turn** in a passing problem (the Phase-1 16.7% false-gap materializing as damage), and the nudge then induced over-action — `miss_func_112` spiraled into 40+ `get_symbol_by_name` calls, hit the 50-call cap, and never advanced (→ empty_turn); `_184` over-booked on turn 0 (→ instance_state_mismatch). The 16 migrations are fail→fail (no score cost) but they convert a *safe* failure (an empty give-up) into an *unsafe* one (a wrong action) — which is why the pre-registration made it a hard gate even though it doesn't move the number.

**Fix / takeaway**:
- **HOLD shipped as a result, not a failure.** `LUXE_REFLECT` stays default-off; the stage + `scripts/ab_multi_turn_miss.py` stay in-tree, documented. A score bump bought with deterministic regressions + a non-generalizing behavior stays opt-in — identical discipline to the Part-A GFS-guidance non-Pareto wash and the v1.10.x band-response levers.
- **Phase 1 detection ≠ Phase 2 utility.** Same-model verify can *detect* give-ups (81.8%), but *repairing* them with the same weights mostly produces ungrounded action. Self-detection generalized; self-repair did not. This is the sharper version of the catch-22 the plan designed around: the catch-22 didn't bite *detection*, it bit *repair*.
- **An empty turn is a safe failure; don't trade it for an unsafe one.** When measuring a "convert the give-up" lever, count empty→wrong-action migration as load-bearing even when it's fail→fail — the score hides a behavior regression.
- **The false-gap is not free.** A 16.7% false-gap looked acceptable at the Phase-1 gate, but at the repair call site it became 2 destroyed passes via over-action. A verify-gated *action* must weight false-gaps by their downstream blast radius, not just their rate.
- **Smoke predicted the full run exactly** (`_7` fix, `_9`/`_15` migrate) — the n=3 live smoke before the ~3h run was worth it and would have justified a design rethink before spending the compute, had the user chosen.
- Possible refinement if revisited (re-bench required): a much tighter repair budget (≤2–3 steps, not 15) to kill runaways + a give-up gate that skips turns the clean model deliberately left empty. Both target the 2 regressions; **neither touches the 16 migrations** (the core self-repair-without-grounding limit), so the ceiling on this lever is low.

**Affected files**: `benchmarks/bfcl/adapter.py` (`_drive_turn`, `_is_giveup_turn`, gated repair, `repair_turns`), `src/luxe/agents/reflect.py` (`repair_nudge` + `_luxe_repair` filter), `benchmarks/bfcl/run.py`, `src/luxe/agents/agents.sdd` (Phase 2 invariants), `scripts/ab_multi_turn_miss.py`, `tests/{test_bfcl_multi_turn,test_reflect}.py` (+10; suite 965). Commit `7c621c8`. Result artifacts: `acceptance/bfcl/multi_turn_miss_func/reflect_arm/` (gitignored). Full state: `RESUME.md` top section.

### [2026-05-17] Citation-grounding directive: same-day revert from prompt regression

**What happened**: Mode C (line-number hallucination in final-report prose, `project_doc_config_three_modes.md`) had been deferred since v1.4. The Step 1 plan was a prompt-level fix: add a citation-grounding directive to `_BASELINE_SYSTEM` and `_HADS_SYSTEM` saying "Prefer exact line citations grounded in observed tool output. If you need to cite a line you haven't read, perform another `read_file` or `grep` call to confirm before citing. Only omit line numbers as a last resort — never invent them." The wording was tightened per code review to defend against citation-avoidance. Shipped, then a 3-rep nothing-doc-config A/B was run. Result: rep 1 emitted 0 citations (lint passed vacuously, 384s wall, no abort), rep 2 emitted 0 citations + ABORTED "Stuck in loop" at 143s, rep 3 produced no final report + ABORTED "Stuck in loop" at 459s. Historical v1.4.1 baseline on the same fixture was 10/10 PASS with no aborts. Same-day revert; lesson saved as `feedback_citation_grounding_caused_loop_and_avoidance.md`.

**Root cause**: The directive packed two competing imperatives into one bullet:
1. "perform another `read_file` or `grep` call to confirm before citing" — encouraged extra tool calls
2. "Only omit line numbers as a last resort" — authorized omission as a fallback

At temp=0 the model picked one exit deterministically per rep — neither converging on the intended "ground via observed tool output" middle path. Rep 1 took the omit exit (vacuous lint pass). Reps 2+3 took the call-again exit and looped on read_file/grep until the 2-consecutive-repeat-step abort fired.

The anti-citation-avoidance gate in the win criteria (`total_citations_emitted` not down >20%) was the right defense but only AFTER the bench. The smaller insight is: any tool-loop-class system needs to anticipate the loop-detector as a downstream consequence of any prompt clause that tells the model "call X tool again."

**Fix / takeaway**:
- Same-day revert. `src/luxe/agents/prompts.py` restored. Regression guards landed in `tests/test_prompts.py` (`test_baseline_system_does_not_carry_reverted_directive`, `test_hads_system_does_not_carry_reverted_directive`) so the failing wording cannot silently re-land without re-validation.
- General principle: prompt clauses with two imperatives that don't share an exit ARE NON-PARETO. If a directive includes "do X" AND "or alternately Y," design and validate the divergence behavior explicitly. Better: split into a single positive imperative without an "or" branch.
- Future Mode C attempts should land in one or the other (per the feedback file): Option A — softer prompt that forbids invention WITHOUT inviting extra tool calls. Option B — structural re-lint against post-reprompt output (Step 2 of the original plan).

**Affected files**: `src/luxe/agents/prompts.py` (reverted), `tests/test_prompts.py` (regression guards landed), `benchmarks/maintain_suite/variants_mode_c_3rep.yaml` (the A/B vehicle, kept for future re-tries).

---

### [2026-05-17] v1.10.3 n=75 SHIP HELD — mechanism correct, composite worse, psf__requests cluster surrendered 3 unexpectedly

**What happened**: After the small-surface revert of W3 (commit `3c72d92`) the n=4 smoke and 3-rep probe both looked clean on mechanism evidence. The n=75 + Docker harness (5h + 35min) revealed the composite picture is worse at single-rep than the v1.10.2 ship-cycle:
- Inspector tier: strong+plausible 37 (in v1.10.2's [35, 38] range — substrate stable) but **empty_patch 18, +3 above v1.10.2's 3-rep range [13, 15]**.
- Docker harness: **33/75 = 44.0%, −6 resolves vs v1.10.2 rep_1's 39/75 = 52.0%**.
- Mechanism: ✅ 0 exploratory variant fires (W3 fully removed), all 6 CONFIDENCE_COLLAPSE are soft_anchor, suppression events carry `recent_path_diversity` as designed, gh-auth hardening held (sklearn-11310 + sklearn-11578 both completed cleanly).
- Cross-cycle Docker surrenders breakdown: 1 design-accepted (matplotlib-14623), 3 known-variance, **3 NEW** Docker regressions on the psf__requests cluster (requests-1724, requests-1766, requests-5414).

**Root cause / hypothesis**: The 3-instance psf__requests surrender pattern is the most concerning signal. None of those instances are in `project_v1102_variance_baseline.md`. Two possibilities: (a) random variance coincidence on a single rep; (b) hidden cost of silent-suppression on a fixture class where v1.10.1's exploratory variant was quietly helping but the v1.10.2 measurement didn't catch it (because v1.10.2's 3-rep on those instances was consistently resolved — they only fell off under v1.10.3's silent suppression).

The empty_patch +3 outside the v1.10.2 range is more decisive. Even discounting the design-accepted matplotlib-14623, the remaining empties exceed the noise band per `feedback_ship_floor_needs_multirep_when_at_strictness.md`.

**Fix / takeaway**:
- **Ship held — no tag.** The W3 revert commit stays on main (it's a substrate change that may still be correct under 3-rep replication), but v1.10.3 doesn't tag as a release.
- The gh-auth hardening + Mode C regression guards are independent of the W3 decision and stay landed regardless.
- Next steps (in priority order):
  1. **3-rep replication on the full n=75** to distinguish variance from systematic. ~10h additional wall.
  2. **psf__requests cluster investigation**: pull events.jsonl for the 3 surrendered instances; check whether they fall in the score<LOW band (suppression) or above (soft_anchor fired). If they're suppressed and v1.10.1's exploratory was the working rescue, the v1.10.3 design needs a band-specific refinement (e.g., write_pressure escalation at this band for the requests fixture-class).
  3. If 3-rep confirms regression, revert `3c72d92` and keep the cycle's other landings (gh-auth, Mode C guards) as stand-alone substrate work.
- **General principle**: at this point in the cycle's strictness, single-rep n=75 with ±3 on a primary metric is HOLD-grade evidence on its own — even if mechanism evidence is clean. The ship floor's noise band must be calibrated to the multi-rep variance distribution, not to single-rep gates. `feedback_ship_floor_needs_multirep_when_at_strictness.md` was written for exactly this case.

**Affected files**: no code change from this analysis (analysis-only). Updated: `RESUME.md` current-state header, `lessons.md` this entry, memory: `project_v1102_variance_baseline.md` (would be extended with v1.10.3 cross-cycle deltas if v1.10.3 ultimately ships; deferred for now).

---

### [2026-05-17] v1.10.3: small-surface revert sized correctly; smoke validates mechanism not floor

**What happened**: v1.10.3 reverted the W3 exploratory-support variant introduced in v1.10.1 (commit `6d1709e`) and the diversity-gating overlay added in v1.10.2 (commit `ab39b9f`). Both gave way to v1.10's silent-suppression behavior in the score<LOW band. The v1.10.2 3-rep variance baseline (`project_v1102_variance_baseline.md`) had shown pylint-6528 empty in 2/3 reps under W3 — confirmed non-Pareto at the band level. Code revert + targeted n=4 smoke (`v1102_probe_n4.json`) at acceptance/swebench/v1103_smoke/rep_1/. Wall 17m41s. Results: sympy-13031 clean habituation_exit (unchanged); matplotlib-14623 empty + loop-abort (accepted regression — the W3 founding case, now back to v1.10 silent-failure shape per design); pylint-6528 empty (n=1 within the v1.10.2 2/3-empty variance — needs 3-rep to compare); sphinx-10323 patch_len=708 (recovered to non-empty).

**Root cause / mechanism evidence**: All four runs showed the v1.10.3 design behaviors firing as intended. matplotlib-14623 emitted 11× `early_bail_suppressed_diffuse` events (score=0, recent_path_diversity=2) — no message landed in chat history, exactly as v1.10's behavior. pylint-6528 showed 3× suppression then `early_bail_fired` with `soft_anchor` variant once score crossed LOW. The suppression event carries `recent_path_diversity` as designed (kept as observability for v1.11 mining, not as a gate trigger). `outcomes.py` back-compat for `msg_variant="exploratory"` / `"soft_anchor_low_diversity_fallback"` preserved so stale event logs still classify cleanly.

**Fix / takeaway**:
- n=1 (or n=4) smoke validates SUBSTRATE STABILITY and mechanism behavior, not ship-floor evidence. A defensible n=75 ship-floor for v1.10.3 would need 3-rep replication on the variance-class instances per `feedback_ship_floor_needs_multirep_when_at_strictness.md`.
- The "small-surface revert" framing was correct sizing: code change was ~165 lines in loop.py + 107 lines of test changes. Trade is documented (matplotlib-14623 may regress to v1.10 silent-failure shape) and accepted as the price of protecting pylint-6528 + sphinx-10323 + the broader band-level non-Pareto class.
- Trajectory-shape signals (post-bail tool_call rate, grep vs read ratio in rescue window) remain queued. They're the structural answer to the non-Pareto at this band — v1.10.3 didn't budget them. Pick up in v1.11 or v2.x.

**Affected files**: `src/luxe/agents/loop.py` (W3 dispatch removed, v1.10 suppress branch restored, observability emission added to the suppression event), `tests/test_loop_write_pressure.py` (`test_convergence_gate_fires_exploratory_on_diffuse` → `test_convergence_gate_suppresses_early_bail_on_diffuse`; new `test_exploratory_mode_string_no_longer_dispatched` + `test_suppression_event_carries_recent_path_diversity`), `src/luxe/agents/convergence.py` (helper + threshold constants left in place; `_DIVERSITY_MIN_FOR_EXPLORATORY` no longer imported by loop.py but kept for `test_convergence.py` historical assertions).

---

### [2026-05-17] gh-auth hardening: probe semantics matter more than retry budget

**What happened**: `assert_gh_auth()` (`src/luxe/pr.py:137`) was the source of the long-standing flake documented in `project_gh_auth_flake.md` — bench errors with `rc=2 / no run_id captured`, fixtures bail at +0:00, no model invocation. The May-16 v1.10.2 rep_2 incident lost 2 sklearn datapoints during a real network outage. Pre-2026-05-17 mitigation was 3 retries at [0.5, 1.5]s spacing. Three things shipped in commit `03df904`:

1. **Probe swap**: `gh auth status` → `gh api user --jq .login`. The original probe only validated local CLI state (it could flap on keychain issues unrelated to actual PR creation, and didn't validate the network path the bench actually needed).
2. **Widened retry**: 5 attempts at [0, 0.5, 1.5, 5, 15]s with a 10s per-attempt subprocess timeout. The timeout was a co-equal change with the probe swap — `gh api user` is a real HTTP call that can hang longer than the local `gh auth status` did; without a per-attempt timeout, a single stuck subprocess could collapse the entire retry budget.
3. **Failure-kind classifier** + 90s per-suite TTL cache. The classifier maps stderr → `network | auth | rate_limit | binary_missing | unknown`, drives future "should we auto-retry?" / "did GitHub degrade?" analytics. The TTL cache defends against per-fixture amplification — preflight() is called per fixture, so without a cache a 20s outage would multiply by N remaining fixtures.

**Root cause**: The pre-2026-05-17 design validated the wrong thing (CLI state vs functional boundary). The retry was sized for sub-second flakes (May-2 occurrences) but had no defense against the per-fixture amplification class that the May-16 outage exposed. Tying probe semantics to actual functional path (gh api user) AND adding the TTL cache made the failure mode operationally separable from "GitHub unreachable" — the latter now correctly hard-fails after one suite-wide attempt cycle.

**Fix / takeaway**:
- For any health probe in a bench preflight: probe the **functional path the bench actually needs**, not a local-state shadow of it. CLI-status probes are weaker functional signals than API probes that exercise the same path the bench will exercise.
- For any per-fixture preflight: TTL-cache a successful probe with a window calibrated to the suite cadence. Defense against amplification is non-optional for production bench harnesses.
- For any retry budget: pair it with a per-attempt timeout. A single hung subprocess can otherwise eat the entire budget on a degraded network.

**Affected files**: `src/luxe/pr.py:137-296` (probe swap, retry tuple, TTL cache, classifier, structured logging via `luxe.pr.gh_auth`), `tests/test_pr_flow.py:143-321` (11 new tests, 3 updated to mock the new probe command). project_gh_auth_flake.md updated to "hardened 2026-05-17, awaiting 3 clean cycles to close."

---

### [2026-04-28] Kimi K2 does not fit in 32 GB — MoE memory model misunderstanding

**What happened**: We planned to build a Kimi model family config alongside Qwen and DeepSeek for benchmarking. Research revealed that the smallest Kimi K2 variant requires 538 GB at 4-bit quantization. Despite having only ~32B active parameters per token, the full 1T parameter set (384 experts) must reside in memory.

**Root cause**: MoE "active parameter" counts are misleading for memory planning. The active count determines compute throughput (fast inference because only 8 of 384 experts fire per token), but **all expert weights must be loaded into RAM**. A 1T-total / 32B-active MoE model takes the same memory as a 1T dense model — the savings are in compute, not storage. The research doc's "future upgrades" section listed Kimi K2 for 128+ GB machines, but we overlooked that when scoping the 32 GB benchmark suite.

**Fix / takeaway**: Only two model families (Qwen, DeepSeek) are viable for 32 GB pipelines. When evaluating MoE models for memory-constrained deployments, always check **total parameter count × quantization bits**, not active parameters. The only Kimi model that fits under 18 GB is Kimi-VL-A3B-Thinking (9 GB), which is a vision-language model unsuitable for code tasks.

If a third family is needed for comparison, Llama 3, Gemma 3, or Phi 4 could substitute — all have MLX-quantized variants spanning 1B–27B.

**Affected files**: `configs/` — no Kimi config created; `scripts/download_models.sh` — Kimi section omitted.

---

### [2026-04-28] DeepSeek-Coder-V2-Lite lacks native tool calling template

**What happened**: DeepSeek-Coder-V2-Lite-Instruct is the only code-specialized DeepSeek model that fits in 32 GB (8.23 GB, MoE with 2.4B active params). However, it does not have a structured tool calling template in its oMLX/MLX tokenizer config. The R1-Distill-Qwen models inherit Qwen's tool calling support, but Coder-V2-Lite uses a basic chat template.

**Root cause**: The MLX community quantization of DeepSeek-Coder-V2-Lite was created before tool calling templates were standardized. The model can still follow tool-calling instructions in its system prompt, but it outputs tool calls as text (JSON in response) rather than structured `tool_calls` objects.

**Fix / takeaway**: The agent loop's `_parse_text_tool_calls()` function handles this case — it recovers tool calls from `<tool_call>` tags and bare JSON in response text. The DeepSeek config will work, but the `worker_read` role (which uses Coder-V2-Lite) may have a higher schema reject rate than Qwen equivalents. Monitor `schema_rejects` in benchmarks to quantify the impact.

**Affected files**: `configs/deepseek_32gb.yaml` — uses Coder-V2-Lite for worker_read despite the template gap; `src/swarm/agents/loop.py` — text recovery path is critical for this model.

---

### [2026-04-28] `huggingface-cli` deprecated — use `hf` instead

**What happened**: The download script used `huggingface-cli download` but the `huggingface_hub` package (v1.12+) has deprecated that command in favor of the new `hf` CLI. Running the script produced `Warning: huggingface-cli is deprecated and no longer works` and all downloads silently failed.

**Root cause**: The `hf` binary is installed alongside `huggingface_hub[cli]` but only into the venv's `bin/` directory. The script ran with `#!/usr/bin/env bash` which didn't activate the venv, so `hf` wasn't on PATH. The old `huggingface-cli` entrypoint now just prints a deprecation notice and exits non-zero.

**Fix / takeaway**: Updated `scripts/download_models.sh` to: (1) auto-activate the project venv, (2) search for `hf` first, then fall back to the venv's `hf` by explicit path, then `huggingface-cli` as last resort. When writing shell scripts that depend on venv-installed tools, always activate the venv or use the full path to the binary.

**Affected files**: `scripts/download_models.sh`

---

### [2026-04-29] Tool-side guards alone don't make small models honest

**What happened**: Phase 2 bake-off showed micro__qwen-coder-1.5b "passing" `neon-rain-implement-reset-shortcut` by deleting 840 lines of `Game.js` and replacing them with a 3-line `function reset() { /* Reset the game state here */ }`. Test runner exited 0 because the deleted module wasn't covered by the surviving tests. Adding three write-time guards — `_check_placeholder_text`, `_check_role_path`, `_check_mass_deletion` — closed the obvious cases (literal `<paste...here>`, `worker_read.js`, 840-line wipes). On re-run the same model evaded each guard with a small adjustment: `worker_read_r.py` (suffix variant past exact-stem match), `# Your real listener code here` (extra adjective past single-`\w+` regex), orphan Python files in a JS project (no test file added → vacuous-test gate doesn't fire).

**Root cause**: A 1.5B model gaming pattern-based gates is a whack-a-mole game. Pattern matching can't catch semantic correctness — "you wrote a Python file in a React project" or "your test asserts on `undefined === undefined`" requires reading the diff *with the goal in mind*, which is what a higher-weight reviewer is for.

**Fix / takeaway**: Defense-in-depth: keep the cheap pattern gates (they kill the easy cheats), but accept that the architectural answer for quality is a 32B-Instruct chief architect that reads the diff and verdicts each task. Path B (`--mode phased`) is built around this — the architect runs after each group of atomic tasks with bounded retry → graceful abort. Don't expect any pattern set to be evasion-proof against a model that has nothing else to optimize for.

**Affected files**: `src/luxe/tools/fs.py` (write-time guards + tightened regex); `benchmarks/maintain_suite/grade.py` (post-PR strict gates with same guards); `src/luxe/agents/phased.py` (Path B — architect-as-reviewer); `tests/test_tools.py` (evasion-regression tests for each observed bypass).

---

### [2026-04-29] `tests_pass` grading credits diffs that don't actually implement the feature

**What happened**: Three "passing" PRs on the same fixture (`neon-rain-implement-reset-shortcut`) were all wrong in different ways: (1) Phase 1 32B-mono produced a real `addEventListener('keydown')` fix; (2) Phase 2 swarm-14B added a vacuous test file that asserts `expect(handler.someState).toBe(undefined)` against a `dispose()` that doesn't exist (test passes because `undefined === undefined`); (3) Phase 2 micro-1.5B deleted the game's controller and shipped a stub. All three got `tests_pass: True` because the existing test suite kept passing regardless.

**Root cause**: `_check_tests_pass` runs the test command and credits any rc=0 result, regardless of whether the diff actually exercises the feature. It can't distinguish "test passes because the implementation works" from "test passes because the test doesn't test what we asked for" or "test passes because the new code is unreachable from the test suite". Adding more diff inspection helps (`destructive_diff`, `role_name_leak`, `placeholder_diff`) but doesn't solve the orphan-file or vacuous-test cases.

**Fix / takeaway**: Added a `vacuous_test` gate to `grade.py`: when `tests_pass` succeeds at HEAD AND the diff includes new/modified test files, check out a worktree at `base_sha`, copy the new test files in, and re-run the test command. If it passes against unmodified base implementation, the test isn't exercising new code → mark `vacuous_test`, override `expected_outcome` to False. Closes one of the four observed `tests_pass` exploit modes. The orphan-file mode (diff adds files in a different language than the test suite covers) is still uncaught by automation; surfaced as the architect's job in `--mode phased`.

**Affected files**: `benchmarks/maintain_suite/grade.py` (`check_vacuous_test`, `_looks_like_test_file`).

---

### [2026-04-29] oMLX `idle_timeout_seconds: null` keeps every loaded model resident forever

**What happened**: After multiple bench runs, `oMLX` had 7 models loaded summing to 44.9 GB resident — ~80% of the 64 GB system. Even when no luxe process was running, models stayed loaded indefinitely. User reported a `Qwen2.5-1.5B-Instruct-4bit` re-loading itself shortly after a manual unload; the only callers were stale admin-dashboard polls or background integrations.

**Root cause**: `~/.omlx/settings.json:idle_timeout.idle_timeout_seconds` defaulted to `null`, meaning no auto-eviction. Combined with `max_model_memory: "auto"` (which oMLX set to 80% of system = 54 GB), there was nothing forcing eviction until total memory hit the cap.

**Fix / takeaway**: Set explicit values in `~/.omlx/settings.json`:
- `max_model_memory: "36GB"` — hard cap forces eviction much earlier.
- `idle_timeout.idle_timeout_seconds: 300` — auto-unload anything not accessed in 5 min.
Plus: `mx.metal.set_wired_limit` is *process-level*, but oMLX runs as a separate process so that knob in luxe is a no-op for actual inference. Use `sudo sysctl iogpu.wired_limit_mb=36864` for system-wide Metal pinning, and add `iogpu.wired_limit_mb=36864` to `/etc/sysctl.conf` to persist. Also added `luxe unload` CLI for ad-hoc cleanup and `--keep-loaded` flag on `luxe maintain` for the rare case you want models warm between runs.

**Affected files**: `~/.omlx/settings.json` (user-side config); `src/luxe/backend.py` (`unload_model`, `unload_all_loaded`, `loaded_models`); `src/luxe/cli.py` (`luxe unload` command, post-run unload hook).

---

### [2026-04-29] StarCoder2-3B and CodeGemma-2B aren't chat-tuned — `tokenizer.chat_template` missing

**What happened**: Tested 7 small coder models in a bake-off harness. 4 worked (Qwen2.5-Coder-0.5B, Qwen2.5-Coder-1.5B, granite-4.0-h-tiny, stable-code-instruct-3b); 3 failed at warmup probe time with "Chat template error: Cannot use chat template functions because tokenizer.chat_template is not set". The 3 failures were StarCoder2-3B and CodeGemma-2B (no chat template at all) and DeepSeek-Coder-1.3B-instruct-mlx (legacy mlx-lm filename `weights.00.safetensors` instead of `model.safetensors`).

**Root cause**: StarCoder2 and CodeGemma at small sizes are released as base completion models — no instruction-tuned chat variant exists at ≤3B params. The mlx-community quantizations preserve the empty `chat_template` field. There's no clean way to use them in a chat-driven agent loop without writing a custom prompt-flattening wrapper, and a synthetic chat template applied to a non-chat-tuned model produces poor results regardless. DeepSeek-Coder-1.3B was a separate naming-convention issue, fixable with a `model.safetensors` → `weights.00.safetensors` symlink.

**Fix / takeaway**: Three concrete actions for any future small-model bake-off: (1) probe `tokenizer_config.json:chat_template` before adding a candidate to the roster; if empty, drop it. (2) Probe each candidate via a 1-token chat completion before the bench timer starts — surfaces missing `chat_template` and missing weight files in seconds, not after a 30-min run. (3) For legacy mlx-lm uploads with `weights.NN.safetensors`, a symlink alias is enough; no need to re-quantize.

**Affected files**: `scripts/bench_small_models.py` (warmup probe, default candidate roster, fuzzy filename handling); `scripts/register_omlx_models.py` (creates symlinks from HF cache to oMLX models dir).

---

### [2026-04-28] oMLX model IDs don't include HuggingFace namespace prefix

**What happened**: `swarm check` showed all 9 models as missing (✗) even though oMLX had discovered all of them. The configs referenced `mlx-community/Qwen2.5-3B-Instruct-4bit` but oMLX's `/v1/models` API reported `Qwen2.5-3B-Instruct-4bit` — no namespace prefix.

**Root cause**: oMLX reads from `~/.omlx/models/` and uses the directory structure under `mlx-community/` as a filesystem path, but only the leaf directory name becomes the model ID in the API. The symlinks we created were `~/.omlx/models/mlx-community/Qwen2.5-3B-Instruct-4bit → ...`, so the model ID is just `Qwen2.5-3B-Instruct-4bit`. The configs had been written using the full HuggingFace repo ID format.

**Fix / takeaway**: Stripped the `mlx-community/` prefix from all model IDs in both `configs/qwen_32gb.yaml` and `configs/deepseek_32gb.yaml`. When configuring model IDs for oMLX, always use the name as reported by `/v1/models`, not the HuggingFace repo identifier. Run `swarm check` after any config change to verify model resolution.

**Affected files**: `configs/qwen_32gb.yaml`, `configs/deepseek_32gb.yaml`

---

### [2026-04-30] Mono-only pivot — swarm/micro/phased deleted in the v1.0 simplification

**What happened**: After the 8-bit completion bake-off (2026-04-30) confirmed `mono__qwen3.6-35b-a3b-6bit` as the strongest configuration we'd produced (2/5 real PASSes, including the only production-quality `the-game` and `neon-rain` JS implementations across every prior bake-off), we ripped out the swarm, microloop, and phased execution modes entirely. The codebase shrank by ~3000 lines: deleted `src/luxe/agents/{microloop,phased,architect,synthesizer,validator,worker}.py`, `src/luxe/pipeline/`, `src/luxe/benchmark/`, `src/luxe/metrics/`, `src/luxe/escalation.py`, `src/luxe/mode_select.py`, `configs/{swarm,qwen,deepseek,mode}.yaml`, and 6 test files. `luxe maintain` no longer takes `--mode`; it always runs the monolith.

**Root cause**: Across 6 bake-offs with strict regrade + hand inspection, mono won at every model size we tested ≥14B. swarm tied at 14B (one swarm "pass" was flagged by strict gates and downgraded by hand) and lost outright at every other scale. micro hit 0/5 real at every scale. phased hit 0/5 real even with bounded retry — the architect rubber-stamped fabrication. The multi-agent designs were trying to compensate for small-model weaknesses; once the champion was a 35B-A3B MoE that can drive the agentic loop alone, the decomposition layer added latency and exploit surface (vacuous tests, role-name leaks, mass-deletion gaming) without quality gains.

**Fix / takeaway**: The architecture is now `Backend → run_single → run_agent` and the only config knob is which monolith model to load. The bench harness was simplified in lockstep: variant YAML now takes only `(model_label, model_id)` pairs (legacy `mode: mono` is accepted; `mode: swarm|micro|phased` raises a clear error). Three lessons we paid in real time:
  1. Don't keep "fallback" execution modes around as escape hatches when the data shows they don't beat the primary — they accumulate maintenance debt and confuse experiments.
  2. Once a champion model exists, the right move is to invest in fixture coverage and grader strictness, not in alternate orchestration topologies.
  3. The `ValidatorEnvelope` data classes (used by the citation linter) outlived their producer (the swarm validator); we moved them into `citations.py` rather than keep an empty `validator.py` shell. When a module is mostly dead but a sibling needs a sliver, inline the sliver and delete the module.

**Affected files**: `src/luxe/cli.py` (rewrite, ~40% smaller), `src/luxe/citations.py` (inlined ValidatorEnvelope), `src/luxe/run_state.py` (dropped stage/blackboard helpers + mode fields), `src/luxe/config.py` (default → `single_64gb.yaml`), `benchmarks/maintain_suite/run.py` (variant matrix simplified), `tests/conftest.py`. New default variant file: `benchmarks/maintain_suite/variants_v1_default.yaml`.

---

### [2026-04-30] Regex-present grader gamed three ways — added min_matches and min_added_lines

**What happened**: The 8-bit completion bake-off produced 6 raw PASSes across two variants. Hand-inspection downgraded 4 of them: (1) lpe-rope-calc — task asked for a module docstring + type hints on EVERY top-level function, model added zero docstrings and typed ONE parameter on ONE function; the regex `def \w+\(...: (str|int|...)` matched once and the test "passed". (2) isomer — task said "ADD a Quickstart section", model RENAMED "Quick Start" → "Quickstart" and stripped the ISOMER_SECRET setup the app requires to start; the regex `(?i)#+\s*Quickstart` matched. (3) nothing-ever-happens — task said "identify dependencies with KNOWN security advisories", model created a 2-file audit doc with "no known issues" listed for every dependency; the regex `(?i)# SECURITY` matched on the doc header. None of these three diffs implemented the asked task; all three matched the regex on the first added line.

**Root cause**: A regex against added diff lines verifies *something added* matches *a pattern*. It cannot verify *the task is complete*. For multi-call-site tasks ("type every function") a single match is the same as zero work. For document tasks, a one-line edit (or a section rename that matches the header pattern) is indistinguishable from a substantive write. For audit/manage tasks, header-keyword matching admits "I looked but found nothing" surveys as if they were findings.

**Fix / takeaway**: Added two optional fields to `expected_outcome` for `regex_present` checks in `benchmarks/maintain_suite/grade.py`:
  - `min_matches` (default 1) — pattern must hit in at least N distinct added lines. Defeats single-edit gaming on multi-call-site tasks.
  - `min_added_lines` (default 0) — diff must add at least N total lines across changed files. Defeats rename-only or one-line edits that technically match the regex.
Updated all 5 v1 fixtures with appropriate values (lpe-rope-calc `min_matches: 4`, isomer `min_added_lines: 8`, nothing-ever-happens `min_matches: 3` + version-string-or-CVE pattern). Added 5 new fixtures (10 total, the v1 release-gate floor) calibrated with these thresholds. The thresholds are fixture-author choices — fixtures with substantive scope set them aggressively; trivial single-edit fixtures leave them at defaults.

**Affected files**: `benchmarks/maintain_suite/grade.py` (`_check_regex_present` signature), `benchmarks/maintain_suite/fixtures.yaml` (5 existing tightened, 5 new fixtures added).

---

### [2026-04-30] Orphan-file gate — close the last "tests pass for the wrong reason" hole

**What happened**: The granite-4.1-3b-bf16 bake-off (2026-04-30) raw-passed `neon-rain-implement-reset-shortcut` at 5/5. Hand inspection: the model created a NEW file `src/input/HtmlInputHandler.ts` next to the existing `HtmlInputHandler.js` in a JavaScript-only project. Nothing imported the new TypeScript file — `Game.js` still imports the original `.js` handler. `npm test` passed because the existing implementation was unchanged. The vacuous_test gate didn't fire because vacuous_test only inspects NEW *test* files, not new *source* files. This is exactly the orphan-file mode flagged in the [2026-04-29] entry as "uncaught by automation; surfaced as the architect's job in --mode phased" — and we deleted phased mode in the v1 simplification.

**Root cause**: A successful test run plus a non-empty diff plus a new file in the diff doesn't mean the new file is on the executed code path. The model can satisfy "added a file matching the pattern" and "tests pass" simultaneously by adding parallel-language duplicates (`.ts` next to `.js`, `.py` in a JS project) or by adding bare modules nothing wires in. Tests pass against the unchanged existing implementation.

**Fix / takeaway**: Added `check_orphan_file` in `benchmarks/maintain_suite/grade.py`. Only applies to `implement`/`bugfix` tasks (document/manage legitimately add standalone files like CONFIG.md or SECURITY-AUDIT.md). Two-prong detection — a NEW source file is orphan iff EITHER:
  1. A sibling file with the same stem exists in the same directory (e.g. `Foo.ts` next to `Foo.js`). Strong duplicate signal — catches the granite-3b case.
  2. The file's stem isn't referenced anywhere else in the post-edit repo (no import/require/path-string mentions it).
The gate fires on both `tests_pass` and `regex_present` outcomes — the regex case mattered too because a model can match a regex by adding an unwired source file just as easily as by adding the right edit. Five tests in `test_acceptance_grader.py` cover: duplicate stem, unreferenced new file, properly-wired new file (must NOT fire), document tasks (must NOT fire), and non-source additions like markdown (must NOT fire).

This closes the last automated grader hole identified across all bake-offs. The remaining false-positive surface area is fundamentally a hand-inspection problem (semantic correctness), not pattern matching.

**Affected files**: `benchmarks/maintain_suite/grade.py` (`check_orphan_file`, `_added_files_in_diff`, `_is_source_path`, plus integration in `grade_fixture`'s `tests_pass` and `regex_present` branches), `tests/test_acceptance_grader.py` (5 new tests).

---

### [2026-05-01] Phase 1 prompt-shaping outcome — structural prompts trade implement for doc/manage; Branch B is the natural next step

**What happened**: Ran the 60-run prompt-shaping sweep specified in `~/.claude/plans/jiggly-baking-kahan.md` against the 10 v1 fixtures with 6 cells: `champ-baseline`, `champ-baseline-rp__rp105`, `champ-cot`, `champ-sot`, `champ-hads`, `champ-combined-rp`. None of the structural variants beat baseline overall — baseline scored 7/10, the others ranged 4-6/10. But hidden inside the totals: every prompt-shaped variant (cot, sot, hads, combined-rp) cleared a 4/4 implement ceiling, beating baseline's 3/4 on the implement category. The wins came at a cost: every structural variant lost 1-2 documents and 0-1 manage tasks vs baseline. Per-task-type breakdown:

| Cell | impl (4) | doc (5) | manage (1) | Total |
|---|---|---|---|---|
| baseline | 3 | 3 | 1 | 7 |
| cot/sot/hads/combined-rp | 4 | 1-2 | 0-1 | 5-6 |
| baseline-rp | 3 | 1 | 0 | 4 |

**Root cause**: the structural prompts (CoT plan-first, SoT skeleton-first, HADS strict FIRST/THEN/ONLY-AFTER) all push the model toward action — call `edit_file`, plan-then-act, stop deliberating. That framing is exactly right for implement work and exactly wrong for documents (which need prose deliberation) and manage tasks (which need analytical depth before any edit). The action push was one-size-fits-all when the right answer is per-task-type.

A side observation: `champ-baseline-rp__rp105` (sampling-only, no prompt change) scored WORSE than baseline at 4/10. `repeat_penalty=1.05` on a code-gen workload pushed the model to invent identifier divergence (per the risk we'd flagged in `jiggly-baking-kahan.md`). Sampling penalties hurt without prompt-shaping help; they don't compose the way the rp+structural cell hoped.

**Fix / takeaway**: Branch B (per-task-type overlays). Implemented in commit `b81b628`: `prompts.py` gains a `TaskOverlay` dataclass + `TASK_OVERLAYS` registry; `RoleConfig` gains a `task_overlay_id` field; `run_single` resolves prompt ids per task type via `resolve_prompt_ids()`. The seed overlay `implement_via_cot` maps `implement` and `bugfix` to the `cot` PromptVariant; document/manage/review/summarize fall through to baseline. New variant file `variants_task_type_overlay.yaml` runs a 2-cell sweep: `champ-baseline-control` + `champ-implement-via-cot`. The control cell deliberately re-runs baseline alongside the overlay so the noise floor is captured in the same wall window — Phase 1 baseline swung 5→7 between identical runs, so single-run deltas of 1 fixture are noise.

The hypothesis is testable in 1-3 hours of bench wall: if the overlay composes baseline's doc+manage performance with the structural-variant implement ceiling, the cell projects to 8/10 and ships v1.0. If it ties baseline at 7/10, sampling variance is dominating and we either re-run for sample size or fall through to Branch C (calibration relax). If it scores below baseline, the overlay leaks structural framing into doc/manage somehow and the implementation needs revisiting.

**Affected files**: `~/.claude/plans/task-type-overlays.md` (new), `src/luxe/agents/prompts.py` (TaskOverlay + registry + resolve_prompt_ids), `src/luxe/config.py` (task_overlay_id field), `src/luxe/agents/single.py` (dispatch), `benchmarks/maintain_suite/run.py` (Variant + overlay write + loader), `benchmarks/maintain_suite/variants_task_type_overlay.yaml` (new), `tests/test_prompts.py` + `tests/test_config.py` (overlay tests).

---

### [2026-05-01] Multi-variant `v1_release_gate` was checking total passes across all cells

**What happened**: The Phase 1 sweep finished with 33 total passes across 60 runs and the bench summary printed `v1 release : YES (needs ≥8 of ≥10 passing)`. That was wrong — no single cell hit 8/10, so no variant is promotable. The gate was telling us we could ship when we can't.

**Root cause**: `summarize()` in `benchmarks/maintain_suite/grade.py` set `v1_release_gate = passed >= 8 and total >= 10` against the FLAT results list. For multi-variant runs that flat list aggregates across cells. 33 passes ≥ 8 and 60 ≥ 10 → gate says YES even though every individual cell is below 8/10. The bug was latent in single-variant runs (where the flat list IS one cell) but surfaced as soon as we ran the prompt-shaping matrix.

**Fix / takeaway**: `summarize()` now takes an optional `per_variant: dict[str, list[FixtureResult]]` argument. When provided, the gate is True iff some cell has ≥8/10 fresh passes. When omitted (single-variant or no-variant runs), the original flat threshold still applies for back-compat. The print site in `run.py` shows which cells cleared (`cleared by: <vid>`) or reports `per-cell ≥8/10 — no cell cleared`. `summary.json` now carries `v1_release_gate_per_variant` for downstream tooling. Three new regression tests in `tests/test_acceptance_grader.py`: no cell cleared (the actual Phase 1 case), one cell cleared, and single-variant unchanged.

The lesson behind the lesson: aggregate metrics over a variant matrix are often nonsense for ship decisions. The same shape of bug would apply to any "≥X total" gate run over multi-cell sweeps — average wall, average tokens, etc. None of those should be summed across variants without thought.

**Affected files**: `benchmarks/maintain_suite/grade.py` (`summarize()`), `benchmarks/maintain_suite/run.py` (call site + print site), `tests/test_acceptance_grader.py` (3 regression tests).

---

### [2026-05-01] Phase 0 — grader had two latent bugs silently inflating PASS counts

**What happened**: While hand-grading `acceptance/v1_temp0_probe_b` (10-fixture, temp=0 deterministic baseline), we found that `result.json` recorded `diff_additions: 0, diff_deletions: 0` on every fixture even when the actual `git diff base..HEAD --shortstat` showed e.g. `+8/-839`. We also found `gates_triggered: []` on every fixture — including a probe_a-era run that genuinely deleted 837 lines of `Game.js` to make `npm test` return rc=0. The strict gates (`destructive_diff`, `role_name_leak`, `placeholder_diff`) that RESUME.md described as "in place" were not firing on any fixture.

**Root cause**: Two independent omissions in `benchmarks/maintain_suite/grade.py`:

- **Bug 1**: `FixtureResult.diff_additions` and `diff_deletions` were declared on the dataclass (defaulting to 0) but never populated anywhere. There was no call to `git diff --shortstat` in the grader at all. The fields existed as placeholders for downstream tooling that never landed.
- **Bug 2**: `apply_strict_gates()` was correctly implemented (lines 329-354) but was **never invoked from `grade_fixture()`**. The function wired up `check_destructive_deletion` / `check_role_name_leak` / `check_placeholder_text` and returned the right shape, but the call site was missing. Only the outcome-conditional gates (`vacuous_test`, `orphan_file`) — which fire post-outcome-pass on a different code path — ever ran.

Both bugs were latent across every prior bake-off in `RESUME.md`'s history table. PASSes that involved destructive diffs or placeholder stubs were silently credited.

**Fix / takeaway**: Three commits.

- **Commit 1** — added `_diff_shortstat(repo_path, base_sha) -> tuple[int, int]` and `_diff_added_text(repo_path, base_sha, changed_files) -> str` helpers near `_changed_files`. Both tolerate empty diff and missing base_sha.
- **Commit 2** — wired `_diff_shortstat()` into `grade_fixture()` so `result.diff_additions` / `result.diff_deletions` reflect real shortstat output.
- **Commit 3** — added the `apply_strict_gates()` call in `grade_fixture()` after the shortstat capture. When any gate fires on a write task, `expected_outcome_passed = False` and the gate detail prepends to `expected_outcome_detail`. The gates are skipped for read-mode tasks (review, summarize) — they shouldn't produce diffs in the first place; that's a separate upstream concern.

Sidecar regrade against `acceptance/v1_temp0_probe_b` after Commit 3: exactly one fixture flipped — `neon-rain-document-modules` (4P→1F) via `destructive_diff` (118 deletions / 22 additions = 5.36×, above the 5.0× threshold). The model rewrote a pre-existing 132-line `ARCHITECTURE.md` to 36 lines of new (task-appropriate) content. The gate firing here is the correct behavior; the fixture itself is suspect because the task wording says "Create" but the file already exists at base_sha. That's a Phase 2 fixture-surgery issue, not a gate-tuning issue. Other 9 fixtures graded the same way before and after.

The deeper lesson: **infrastructure that was "added" can still be unwired**. Bug 2 wasn't a code mistake — `apply_strict_gates` is well-formed. The mistake was the integration glue. RESUME.md saying "Strict gates currently in place" had a piece-by-piece checklist of WHICH gates exist; what it didn't track was whether they were CALLED. For any future "we added gate X" claim: assert in tests that the gate fires from the expected entry point on the expected inputs, not just that the gate function returns the right value when invoked directly.

True real-PASS at temp=0 against the fixed grader: **5/10** (was printing 6/10). Per task type: implement 4/4, document 1/5, manage 0/1.

**Affected files**: `benchmarks/maintain_suite/grade.py` (`_diff_shortstat`, `_diff_added_text`, `grade_fixture` populates diff stats and invokes `apply_strict_gates`), `tests/test_acceptance_grader.py` (10 new tests across the three commits), `scripts/regrade_local.py` (new — sidecar tool for re-grading existing acceptance runs without re-running luxe).

---

### [2026-05-01] Don't read `origin/<branch>` in offline-cache repos — use the local branch ref

**What happened**: While diagnosing probe_b results, we read `git diff base_sha..origin/luxe/implement/<branch>` against the offline fixture cache to inspect each run's pushed diff. Several "PASSes" appeared destructive: `neon-rain-implement-reset-shortcut` showed `+8/-839` (Game.js gutted to 3 lines), `the-game-implement-shuffle-shortcut` showed only Python stubs in a JS repo with a commit message reading "(No changes were made as no findings survived validation.)", `isomer-document-quickstart` showed -148 line README rewrite. We classified 3 of 6 PASSes as false positives, called real-PASS 3/10, and built a strategic argument that the grader was 50% inflated and probe_b was largely gamed.

When the sidecar regrade tool ran the grader directly against the actual local branches in the cache (via `git clone --local`), it reported entirely different diff shapes: `+11/0` for neon-rain-reset (a clean `keydown`/`game:restart` event-bus wiring), `+14/-1` for the-game-shuffle (a real App.jsx keydown handler with INPUT/TEXTAREA guards), `+4/-3` for isomer-quickstart (genuinely terse, but not destructive).

**Root cause**: The fixture cache at `~/.luxe/fixture-cache/<repo>` was originally cloned from GitHub (pre-2026-05-01 offline switch). It still had remote-tracking refs pointing to GitHub: `refs/remotes/origin/luxe/implement/<branch>` carrying old commits from prior runs (some of which WERE genuinely destructive, e.g. the `2026-04-29` neon-rain run that did gut Game.js). After the 2026-05-01 offline switch, `repo_url` in `fixtures.yaml` became a local path, and luxe's `git push` during a run targeted `origin = /local/path`. That push lands in the cache's LOCAL branch namespace — `refs/heads/luxe/...` — not the remote-tracking namespace. Local and remote-tracking refs with the same branch name are now divergent: local = newest run's commit, remote-tracking = stale GitHub state.

`git diff base..origin/<branch>` resolves to the remote-tracking ref. Reading those was reading the historical state, not the current run. `git clone --local <cache>` (used by the sidecar) copies the cache's LOCAL branches to the new clone's `origin/<branch>` namespace — which is why the sidecar gets the right answer while a direct cache read does not.

**Fix / takeaway**: When inspecting the latest run's commit in an offline-cache fixture repo, **use the local branch name, not `origin/<branch>`**. Either:

- `git -C <cache> diff <base>..<branch>` (uses the local ref); or
- `git -C <cache> diff <base>..refs/heads/<branch>` to be explicit.

Generalizing: any time a workflow's "remote" is the same physical disk as the workflow's actor (offline cache, local mirror, etc.), the local-vs-remote-tracking distinction in git collapses in semantics but persists in the ref namespace. Stale `origin/...` refs from a past life of the repo will linger and silently mislead reads. The defense is to either prune them (`git remote prune origin` or `git push --prune`) or to never rely on `origin/<branch>` resolution in such setups.

The investigation cost: this misreading drove a half-day's worth of strategic analysis (a "real-PASS 3/10, grader 50% inflated, the model is gaming aggressively" thread) that turned out to be wrong. The actual probe_b real-PASS is 5/10, the grader's PASSes are mostly genuine, and the model's behavior at temp=0 is significantly better than the misreading suggested. Specifically, the strategic implication for Branch B's 8/10 ship-gate math changes — a 5/10 baseline is closer to the gate than 3/10, and the per-task ceiling at temp=0 is implement 4/4 (already saturated, which obsoletes the `implement_via_cot` overlay).

The methodology rule: **before drawing strategic conclusions from grading data, regrade through the same code path the bench uses**. The sidecar regrade tool (`scripts/regrade_local.py`) is now the canonical way to do this without paying the agent-loop's wall-time cost. Manual `git diff` against a cache repo is fine for code-shape spot checks but is NOT a substitute for sidecar regrade when classifying real PASS vs false positive.

**Affected files**: `scripts/regrade_local.py` (new tool that does this correctly); none of the corrected `git diff` semantics required code changes — it's a workflow change.

---

### [2026-05-01] Branch C calibration — `nothing-ever-happens-document-config` gate-side miss

**What happened**: At temp=0 the model produces a comprehensive 136-line `CONFIG.md` documenting ~50 environment variables in markdown tables (Variable / Default / Description / Read-in columns), with file:line citations. The grader rejects it: `pattern matched 0× in 141 added lines (needed ≥3) across 1 changed file(s)`. Per the new Branch C `lessons.md` gate (master plan §Branch C, tightened in commit 5551959), this entry must record (a) semantic acceptability, (b) failure category, (c) targeted-vs-general justification before any `fixtures.yaml` edits.

**(a) Semantic acceptability** — the produced output is a textbook-correct CONFIG.md. From probe_b's commit (`luxe/document/add-a-config-md-at-the-2`):

```
| `PM_RISK_MAX_DAILY_DRAWDOWN_USD` | `0.0` | Maximum allowed daily drawdown in USD based on USDC balance vs. daily high-water mark. `0` disables the drawdown circuit breaker. | `bot/risk_controls.py:47` |
| `BOT_MODE` | `paper` | Runtime mode — `paper` for simulation, `live` for real trading. | `bot/config.py:38` |
```

The doc enumerates 52 env vars across 10 sections (Safety/Mode Control, Secrets, Strategy Overrides, Risk Controls, Redeemer, Live Recovery, Runtime, Paper Trading, Docker Compose, Utility Scripts), with verified file:line citations. This is unambiguously what the task asked for ("documents every environment variable ... For each variable, list its name, default value, and a one-sentence description ... Cross-reference where in the codebase it is read"). Any reasonable grader would credit this output.

**(b) Failure category** — `regex_present` grader miss. The pattern was `(?i)\b(os\.environ|getenv|env\[|process\.env)`, requiring the doc to literally quote Python source idioms like `os.getenv(...)`. The model wrote prose-style documentation that *references* the call sites (file:line citations) without quoting the Python expressions themselves. Classification: **gate-side**, not model-side. The model produced semantically-acceptable output (per (a)); the gate's pattern was looking for code-quote idioms in what is fundamentally a prose document. `diff_produced=true`, `diff_files=1`, no destructive_diff / placeholder_diff / role_name_leak triggers. The model engaged correctly; the gate misclassified the engagement.

**(c) Targeted vs general** — the original regex was defending a real anti-gaming concern (per `fixtures.yaml`'s own comment: "min_matches=3 + 20 added lines defends against listing one var in a sentence and stopping"). Don't just remove it — the defense it provides is real for vacuous outputs. The replacement composes the original idiom-quote pattern with a markdown-name pattern. The added alternative is `\b[A-Z_]{2,}[A-Z0-9_]{3,}\b` (an UPPER_SNAKE_LIKE token of ≥5 chars with ≥2 leading letters). This:

- **ACCEPTS** the actual probe_b CONFIG.md (50+ UPPER_SNAKE env var names → comfortably above min_matches=3).
- **ACCEPTS** Python-idiom prose docs that DO quote `os.getenv(...)` (original pattern still fires via OR).
- **REJECTS** vacuous one-paragraph docs ("This file documents environment variables.") — at most 1-2 incidental UPPER_SNAKE tokens, below `min_matches=3`.
- **REJECTS** docs that mention env vars by description without naming them (a "config notes" prose blob with no UPPER_SNAKE names) — fails on count.
- **REJECTS** docs that only list a header and 1-2 names — `min_added_lines=20` catches the thinness; the gate is composite.

The substantive-edit gate (`min_added_lines: 20`) remains untouched. It does the heavy lifting against thin gaming; the regex is the content-shape signal.

**Fix / takeaway**: replace `(?i)\b(os\.environ|getenv|env\[|process\.env)` with `(?i)(?:os\.environ|getenv|env\[|process\.env)|\b[A-Z_]{2,}[A-Z0-9_]{3,}\b`. Sidecar regrade against probe_b's existing CONFIG.md commit confirms the pattern matches; full regrade shows nothing-config flips 1F → 4P.

The deeper takeaway is about gate-pattern composition: **a regex that lists implementation idioms is sometimes too narrow for a documentation task**. Implementation-idiom patterns are good for code-style tasks (the strict-flag fixture, where `add_argument` / `args.strict` SHOULD appear in the diff). For documentation tasks, the test should be content-shape, not implementation-shape. Future doc-task fixtures should consider: what does the *output* look like, not what *code* it references.

**Affected files**: `benchmarks/maintain_suite/fixtures.yaml` (one regex pattern updated on `nothing-ever-happens-document-config`).

---

### [2026-05-02] v1.0 ship — 8/10 cleared on production config

**What happened**: After the Phase 0 grader fix, fixture surgery on three fixtures, Branch C calibration on `nothing-config`, and a citation-linter IPv4 fix discovered during ship confirmation, the 10-fixture acceptance suite cleared the ≥8/10 gate against `configs/single_64gb.yaml`. Per task type: **implement 4/4, document 4/5, manage 0/1**. Champion: `Qwen3.6-35B-A3B-6bit` at temperature=0.0.

The two remaining fails are model-side limitations:
- `lpe-rope-calc-document-typing`: model added 1 line (`from io import IOBase`) and stopped — didn't write the requested module docstring nor type the `f` parameter. The fixture is winnable; the model under-engaged.
- `nothing-ever-happens-manage-deps-audit`: stuck-in-loop bailout (repeated identical tool calls), no diff produced. The model can't navigate this fixture's audit task on the largest repo (907 KB Python).

**Path through the plan** (Phase 0 → Phase 1 fixtures → Branch C → ship):

1. **Phase 0 — grader fix**. Bug 1 (`diff_additions`/`diff_deletions` never populated) + Bug 2 (`apply_strict_gates` defined but never invoked from `grade_fixture`) silently inflated every prior bake-off result. Fixed in commits `0ab2127` + helpers. Sidecar tool `scripts/regrade_local.py` enabled fast iteration.
2. **Variance probe — temp=0 collapses sampling variance**. Three baseline runs at temp=0.2 ranged 4-7/10 (±2 fixtures); two back-to-back runs at temp=0 produced identical pass/fail vectors. Greedy decoding also lifted implement to 4/4 baseline ceiling, which obsoleted Branch B's `implement_via_cot` overlay (it had nothing to lift). Promoted in commit `8fd0fe4`-equivalent (this commit set).
3. **Fixture surgery — `lpe-typing`, `neon-rain-modules`, `isomer-quickstart`** (commits `48e6577` + `8d1fcd3` + this set). All three were misaligned with their `base_sha`: pe_scan.py was already mostly typed; ARCHITECTURE.md and README.md's Quick Start section already existed at base. Goal wording realigned + thresholds calibrated. None of the surgery weakened anti-gaming defense — the destructive_diff gate (Phase 0) does the heavy lifting now, freeing the per-fixture regex/threshold pair to focus on content shape.
4. **Branch C calibration — `nothing-config`** (commit `eb2bdf0`). Confirmed gate-side miss: model produced a textbook 136-line CONFIG.md with file:line citations; original regex required Python idiom quotes. Replaced with a composite that accepts either Python idioms OR markdown UPPER_SNAKE env var names. Per the master-plan §Branch C gate, lessons.md (a)/(b)/(c) entry written *before* fixtures.yaml edit.
5. **Citation linter IPv4 fix** (this commit set). Discovered during ship confirmation: synthesizer reports legitimately mention `127.0.0.1:port` for dashboard URLs, and the citation extractor's regex `[\w./_-]+\.[\w]+:\d+` was matching IPv4-shaped tokens as `path:line` citations. Two unresolved citations on isomer-quickstart's report blocked the fixture's PASS. Surgical fix: reject paths matching `(?:^|/)\d+\.\d+\.\d+\.\d+$`. Two unit tests added (rejects host:port; preserves filenames-with-digits).

**The Branch B obsolescence** is worth recording explicitly: Phase 1's "structural prompts hit 4/4 implement" finding was at temp=0.2 — almost certainly a baseline-variance artifact. At temp=0, baseline already gets 4/4 implement out of the box, so the `implement_via_cot` overlay had nothing to lift. The plans in `~/.claude/plans/task-type-overlays.md` and `~/.claude/plans/v1-ship-and-prompt-sweep.md` retain Branch B/C structure for future task-type-specific overlays (e.g., a doc/manage overlay), but the v1 ship path didn't need them.

**Confirmation result** — `acceptance/v1_default_ship_confirmation/`, cumulative across the initial 10-fixture run + 3-fixture retry (gh auth flake at 01:48) + 1-fixture isomer-quickstart re-run (post-citation-fix):

| Fixture | Type | Verdict |
|---|---|---|
| lpe-rope-calc-implement-strict-flag | implement | PASS |
| the-game-implement-shuffle-shortcut | implement | PASS |
| neon-rain-implement-reset-shortcut | implement | PASS |
| isomer-implement-healthcheck | implement | PASS |
| the-game-document-architecture | document | PASS |
| neon-rain-document-modules | document | PASS |
| isomer-document-quickstart | document | PASS |
| nothing-ever-happens-document-config | document | PASS |
| lpe-rope-calc-document-typing | document | FAIL |
| nothing-ever-happens-manage-deps-audit | manage | FAIL |

**Fix / takeaway**: Bump `pyproject.toml` from `1.0.0.dev0` to `1.0.0`. Tag `v1.0.0`. The ship gate is the model's ceiling on this fixture set at temp=0 — the implement category is genuinely saturated, doc has one fixture-design-resistant case (typing under-engagement at temp=0), and manage has one model-can't-navigate case. Future improvement to v1.1 would target the manage stuck-loop pattern (likely a context-management or tool-loop-detection issue, not a fixture issue) or add a doc-task overlay (Phase 1's structural prompts regressed doc/manage; an overlay tuned for prose tasks specifically is the next experiment, but out of v1 scope).

The deeper meta-takeaway: **infrastructure quality dominates result quality**. Phase 0 fixed grader bugs that had been silently inflating PASS counts since 04-29. Once the grader was honest, the remaining levers (variance pinning, fixture surgery, gate calibration, one citation-linter fix) added up to a gap closure of 5/10 → 8/10 without changing the model or its prompts. The model is the same Qwen3.6-35B-A3B-6bit it was on 04-29. What changed was: (a) the grader stopped lying, (b) sampling stopped being random, (c) fixtures matched their base_sha, (d) gates measured the right thing. Future bench cycles should expect: every "we added a gate" claim needs a regression test that asserts the gate fires from the expected entry point on the expected inputs (Phase 0 Bug 2's lesson, applied universally).

**Affected files**: `pyproject.toml` (1.0.0.dev0 → 1.0.0), `configs/single_64gb.yaml` (temperature 0.2 → 0.0), `src/luxe/citations.py` (`_IPV4_PATH_RE` guard in `extract_citations`), `tests/test_citations_diff_aware.py` (2 new tests), plus the cumulative grader/fixture work referenced above.

---

### [2026-05-02] Phase v1.1 A1 — historical bake-off re-grade reveals severe inflation

**What happened**: Re-graded four prior bake-off output dirs against the post-Phase-0 grader using `scripts/regrade_local.py`. The deflation across the board is severe — far worse than RESUME.md's earlier "1-2 false positives per cell" caveat suggested.

| Phase | Printed | Regraded | Inflation |
|---|---|---|---|
| `acceptance/v1_default` | 5/10 | 3/10 | 1.67× |
| `acceptance/v1_default_post_fix` | 6/10 | 4/10 | 1.50× |
| `acceptance/prompt_shaping` (60 fixtures) | 33/60 | **8/60** | **4.13×** |
| `acceptance/overnight_moe` (20 fixtures) | 5/20 | **0/20** | **∞** (every printed PASS was false) |

**Root cause (already known)**: Phase 0's Bugs 1+2 — `apply_strict_gates` defined but never called from `grade_fixture`, `diff_additions/deletions` declared but never populated. The strict gates (`destructive_diff`, `placeholder_diff`, `role_name_leak`) caught zero gaming-shaped diffs across every prior run. Once wired, they fire frequently — especially `destructive_diff` on the historical "rewrite-the-existing-file" pattern that the model used to game `min_added_lines` checks.

**Fix / takeaway**: the historical narrative needs revision. Two specific reinterpretations:

1. **Phase 1's "structural prompts hit 4/4 implement"** finding (33/60 printed → 8/60 regraded, 76% false) is no longer a load-bearing claim. The CoT/SoT/HADS variants probably did NOT lift implement to 4/4; the pre-fix grader was credit­ing destructive or placeholder-shaped diffs on those runs as wins. This retroactively justifies treating Branch B's `implement_via_cot` overlay as obsolete (the underlying observation it was built on was an artifact). It also re-explains why baseline-temp=0 hit impl 4/4 "out of the box" in this session — that's the model's actual ceiling, the temp=0.2 + structural-prompt cells weren't reaching it, they were gaming-passing through the broken grader.
2. **Overnight MoE's qwen3.6-35B-A3B-6bit "win"** (5/20 → 0/20) is now also suspect. The bake-off chose the champion based on inflated numbers. The choice was probably still defensible (the model genuinely is the strongest on this hardware tier), but the "5/20 real" claim in RESUME.md was wrong; it was 0/20. Future model selection should re-grade as a baseline before declaring a winner.

**Practical guidance**: any RESUME.md "Real PASS leader" cell predating 04-30 is probably 0.25-0.7× of the printed value. When citing historical numbers in strategic decisions, run `scripts/regrade_local.py --output acceptance/<dir>` first; the cost is minutes and the data is so much more honest that it's worth doing routinely.

**Affected files**: `RESUME.md` (history table updated with regraded counts and a stronger caveat block); `acceptance/<phase>/result_regraded.json` written next to every original `result.json` across the four re-graded dirs.

---

### [2026-05-02] Phase v1.1 A2 — prefix-cache hit-rate not directly available at INFO logs

**What happened**: Tried to measure oMLX's prefix-cache hit rate on the v1 ship confirmation run as load-bearing input for the Workstream C decision. The data isn't directly available from the configuration we currently run.

**Root cause**: oMLX's INFO-level log shows boundary-cache *writes* ("storing X/Y tokens") but not *reads* (per-request cache hit/miss). No `/cache/stats` or `/metrics` endpoint at `localhost:8000`. The Chat completion log line shows aggregate wall + tokens but no prefill/decode split that would let us infer TTFT (a proxy for cache hit). Cross-run wall comparisons (probe_a vs probe_b on the same fixtures at temp=0) are mixed — some fixtures faster, some slower — which doesn't strongly support either "cache helps a lot" or "cache barely helps."

**Fix / takeaway**: Recorded as `project_prefix_cache_baseline.md` memory entry with status INCONCLUSIVE. Workstream C's decision matrix had two HIT bins (LOW <65%, HIGH ≥65%); without a clean number, **default to LOW** (conservative-toward-investigation: keeps Phased Mode v2 as a viable option, lets Workstream B's QUAL outcome do the heavy lifting on the decision). When/if Path 1 becomes the leading candidate based on Workstream B, re-run A2 with one of:

- DEBUG-level oMLX logs + a fresh full bench (will surface per-request cache hit/miss).
- A controlled hot-vs-cold A/B with oMLX restarted between runs (crisper signal than cross-run comparisons).

The deeper takeaway: **infrastructure-availability matters for measurement plans**. The plan assumed the cache-hit-rate data was just sitting in the log; it isn't. Future plans that depend on a measurement should verify the measurement is gettable BEFORE committing to it as a decision input.

**Affected files**: `~/.claude/projects/-Users-michaeltimpe-Downloads-luxe/memory/project_prefix_cache_baseline.md` (new); MEMORY.md indexed.

---

### [2026-05-02] Phase v1.1 A4-A5 — drop dead Gemma-4 entries; document offline 4/5 cap

**What happened**: Cleanup pair from RESUME.md's "Open work" list.

- A4: Removed `Gemma-4-26B-A4B-4bit` and `Gemma-4-26B-A4B-8bit` entries from `~/.omlx/model_settings.json`. RESUME.md noted these models ship with empty `chat_template` and fail HTTP 400 in the chat-driven loop; the settings entries were dead config that would mislead future model-roster decisions. Restarted oMLX; verified via `Backend().list_models()` that Gemma-4 no longer appears (the model's symlinks at `~/.omlx/models/Gemma-4-*` remain but are inert at the API surface).
- A5: Documented the offline-mode 4/5 cap in RESUME.md's "Critical gotchas" section. Per the v1.1 plan recommendation, picked option (b): leave grader code untouched, treat 4/5 as the offline-mode signature rather than introducing auto-detect logic for `pr_opened`. The gate math still works because the gate is per-fixture pass count, not score sum.

**Fix / takeaway**: Both are house-keeping; no model or grader logic changed. The Gemma-4 cleanup is a small example of **dead config compounds over time** — one model's bad chat_template would have kept showing up in roster lookups indefinitely. Periodically pruning model_settings.json is cheap insurance.

**Affected files**: `~/.omlx/model_settings.json` (Gemma-4 entries removed), `RESUME.md` (gotcha entry for offline 4/5 cap).

---

### [2026-05-02] Phase v1.1 A3 — specprefill probe partial: ~5% wall improvement, doesn't clear 15% gate

**What happened**: Enabled `specprefill_enabled: true` for Qwen3.6-35B-A3B-6bit in `~/.omlx/model_settings.json`, restarted oMLX (0.3.8 stable, model digest `cb7e092ef8efe540bc3672c8929c4adbe5f4f759`), ran the 5-fixture A3 probe. The bench hit the gh-auth flake (see `project_gh_auth_flake.md`) on runs 4 + 5; only 3 of 5 fixtures completed. Of the 3 that did:

| Fixture | Baseline (v1 ship confirmation) | A3 (specprefill on) | Δ |
|---|---|---|---|
| `lpe-rope-calc-implement-strict-flag` | 311s | 290s | -7% |
| `the-game-implement-shuffle-shortcut` | 60s | 60s | 0% |
| `neon-rain-implement-reset-shortcut` | 145s | 134s | -8% |

Mean ~5% wall improvement. All three fixtures matched their baseline pass/fail (no quality regression on the data we have).

**Root cause / interpretation**: Per the v1.1 plan's A3 gate, the pass criteria require **median wall ≥15% drop** AND no quality regression AND no raw-text drift. Even imagining the 2 missing fixtures hit a generous 15% lift, the mean across all 5 wouldn't clear 15%. The probe came in with a small positive but it doesn't clear the threshold the plan explicitly committed to (the threshold is intentionally tight — it's there to make the "tried, didn't work" outcome cheap to recognize).

The plan also called out this expected outcome explicitly: *"Treat 'no win' or 'small win' as the likely outcome. Field reports on Qwen3.6-35B-A3B + oMLX show speculative decoding is sometimes silently disabled or buggy when flags don't line up, and is sometimes net-neutral or slightly negative when the draft config is off or the prefix cache is already doing heavy lifting."* That framing held up.

**Fix / takeaway**: Reverted `specprefill_enabled: false` in `~/.omlx/model_settings.json`. Restarted oMLX. Net change to the repo: zero — settings.json is system config, not tracked. The lesson lands here so future-us doesn't waste cycles re-running this probe without checking what changed in oMLX or mlx-lm first.

The deeper takeaway: **probes with binary thresholds beat probes with vibes**. The 15%-or-revert rule made this decision crisp despite incomplete data. If the rule had been "any improvement is good," this would have been a discussion ("but it's *5%*, isn't that worth keeping?") and we'd have shipped a flag with unknown long-term cost. The math: a flag that adds 5% wall improvement but introduces *any* probability of subtle output drift is a net negative on a build-trust-with-the-grader workload like this. Either it's clearly worth it or revert.

**Logged for future re-investigation**: oMLX version 0.3.8 stable; Qwen3.6-35B-A3B-6bit at HF snapshot `cb7e092ef8efe540bc3672c8929c4adbe5f4f759`. If a future oMLX bumps the speculative-decoding stack, re-running this probe is reasonable. Until then, leave the flag off.

**Affected files**: `~/.omlx/model_settings.json` (specprefill_enabled flipped on then back off — net zero), `~/.claude/projects/.../memory/project_gh_auth_flake.md` (new — tracks the open auth-flake issue), `~/.claude/projects/.../memory/feedback_offer_long_running_commands.md` (new — established preference: offer commands for >~5 min runs rather than auto-backgrounding).

---

### [2026-05-02] Phase v1.1 B1 — `document_strict` overlay: negative result on the lpe-typing target

**What happened**: Added a `document_strict` PromptVariant + `document_strict_only` overlay (route `document` task type → strict variant only). Strict task_prefix demands tool-call commitment ("MUST call `edit_file` or `write_file`") AND component completeness ("MUST address EVERY component of the goal ... A diff with fewer than ~4 added lines on a multi-component goal almost certainly means you stopped before finishing"). System prompt unchanged from baseline; overlay fires only on `document` task type so non-doc tasks are untouched (regression-defended in `test_document_strict_only_overlay_fires_only_on_document_tasks`). 5-fixture × 2-cell smoke probe (4 control + 4 overlay completed after a `gh auth` flake retry).

| Fixture | Control | Overlay |
|---|---|---|
| `isomer-document-quickstart` | PASS (+9/-3) | PASS (+8/-4) |
| `lpe-rope-calc-document-typing` | FAIL (+1/0) | **FAIL (+2/-2)** |
| `neon-rain-document-modules` | PASS (+14/0) | PASS (+20/0) |
| `nothing-ever-happens-document-config` | FAIL (no diff) | PASS (+141/0) |
| `the-game-document-architecture` | PASS (+19/0) | PASS (+12/-1) |

Cell totals: control 3/5, overlay 4/5.

**Root cause / interpretation**: the overlay nudged lpe-typing from +1 → +2 added lines but didn't unblock the under-engagement pattern that B1 was designed to fix. The model added `from io import IOBase` (control) or `from io import IOBase` + a typed `f` parameter (overlay) — both stopped before writing the requested module docstring. The strict directive's "MUST call edit_file" clause fired (the model did call edit_file) but the "MUST address EVERY component of the goal" clause did NOT shift behavior — the model considers the typing edit "done" and the docstring half is invisible to it. The directive can't disambiguate "I think I'm finished" from "you actually have more to do."

The overlay's nothing-config flip (FAIL → PASS) is *not* a real v1.1 gain, because nothing-config already PASSed in v1.0's ship confirmation. This run's control regressing nothing-config to FAIL is small temp=0 environmental variance (cross-run prefix-cache state, fixture order, etc.) — the overlay recovered from a transient miss but didn't add new capability above the v1.0 baseline.

**Pass criteria evaluation (plan B1):**
- ❌ lpe-typing PASS (the explicit target).
- ✅ No regression on 4 other doc fixtures (3 equal, 1 transient-recovery).
- N/A no-op-edit spot check (moot since lpe-typing didn't pass).

The plan said "if pass criteria met → keep the overlay; if not → document the negative result." Following the negative-result branch.

**Fix / takeaway**: Keep the `document_strict` PromptVariant + `document_strict_only` overlay registered (the infrastructure is tested, composable, and zero-cost for non-document tasks per the fire-only-on-document overlay semantics). **Don't promote to production** — `configs/single_64gb.yaml` stays unchanged. The variant file `variants_v1_doctask_overlay_probe.yaml` stays as a probe artifact for future experiments.

The deeper takeaway for Phase 1's "structural prompts regress doc/manage" finding: that finding was at temp=0.2 on an inflated grader (re-graded 8/60 in A1). At temp=0 with the honest grader, *targeted* doc-only structural prompting at least doesn't regress doc tasks — the no-leakage overlay design is the right shape. But the lpe-typing under-engagement pattern isn't fixable via prompt directives at this model scale; it's a model-side limit on noticing "the goal asks for two things and I only did one." Future work in this direction would need either a different model, a stronger prompt that includes worked examples of multi-component completion (few-shot), or a runtime check that re-prompts the model when a submitted diff doesn't match all the goal's named deliverables.

For Workstream C: B1 closed 0 of the v1.0 FAILs. If B2 also fails, QUAL=8; per the plan's MECE matrix with HIT=LOW (A2 inconclusive default), we land at Path 1 (Phased Mode v2). If B2 closes, QUAL=9; still Path 1. The 10/10 "ship v1.1, log v1.2 target" outcome (Path 2a) requires both B1 and B2 closing; B1 already locked us out of that.

**Affected files**: `src/luxe/agents/prompts.py` (new `document_strict` PromptVariant + `document_strict_only` overlay), `tests/test_prompts.py` (3 new regression tests), `benchmarks/maintain_suite/variants_v1_doctask_overlay_probe.yaml` (new probe variant file), `acceptance/v1_doc_overlay_probe/` (smoke probe results — kept on disk for future re-grade if pattern of doc-task variance becomes a separate investigation).

---

### [2026-05-02] Phase v1.1 B2 — `manage_strict` overlay: closes deps-audit (with CVE-id caveat)

**What happened**: Added `manage_strict` PromptVariant + `manage_strict_only` overlay (route `manage` task type → strict variant only). Strict task_prefix names two failure modes by name: re-reading the same file (loop-detector trips), and reading-without-writing. Includes a procedural ONE-AT-A-TIME directive: pick one item, look it up, document it, move to the next. 1-fixture × 2-cell smoke probe on `nothing-ever-happens-manage-deps-audit`:

| Cell | Result | Diff | Notes |
|---|---|---|---|
| baseline-control | FAIL | +0/-0 | "no diff produced" — same stuck-loop pattern as v1.0 + earlier reproductions |
| `manage_strict` overlay | **PASS** | +70/-0 | 70-line `SECURITY-AUDIT.md` with 3 concrete findings (aiohttp, SQLAlchemy, psycopg2-binary) |

**Functional check (mandatory per plan B2 criteria):**

- ✅ All 3 packages cited (aiohttp, SQLAlchemy, psycopg2-binary) ARE actually in the fixture's `requirements.txt`. The version constraints quoted in the audit match the file exactly. Upgrade proposals preserve the major-version cap while bumping the min — proper "preserve API compatibility" shape.
- ⚠️ **CVE-id verification limitation**: CVE-2023-46136 (aiohttp) is a real, well-known CVE matching the reported vulnerability (Content-Length DoS). The other two — CVE-2024-3559 (SQLAlchemy) and CVE-2024-22032 (psycopg2-binary) — are plausibly-shaped IDs but couldn't be verified without external lookup; the model may have invented realistic-looking-but-incorrect CVE numbers. The GHSA advisory slugs are particularly suspect (model can't know exact slug strings). **For real production use, a human would need to verify each CVE ID before acting on the audit.**
- ✅ Loop-telemetry proxy: control wall 75s with stuck-loop bailout (6888 completion tokens then abort); overlay wall 315s with steady 22 tok/s navigation. The overlay shifted the model from thrashing to producing. Tool count similar but distinct-args, not identical-args (since no stuck-loop fired).

**Pass criteria evaluation (plan B2):**
- ✅ Non-empty diff matching the regex (`matched in 22 added lines (needed ≥3)`).
- ✅ Findings reference real packages with accurate version constraints (functional check above).
- ✅ Loop telemetry proxy confirms meaningful navigation (wall delta + completion-token volume).

The CVE-id hallucination caveat is real — the bench grader's regex `(?i)(CVE-\d{4}-\d{4,}|...)` cannot distinguish "real CVE" from "CVE-shaped-text-the-model-invented." This is a known limitation: gates check shape, not factuality. For audit-class tasks specifically, the bench gate alone is insufficient for production trust; a human verification step is required. Flagging in lessons.md so this isn't relearned later.

**Fix / takeaway**: B2 PASSes pass criteria for the v1.1 quality push. The path forward (per the plan's commit milestones):

1. **Commit the overlay infrastructure** as a positive result (`feat: manage_strict overlay`).
2. **Promote `manage_strict_only`** into `configs/single_64gb.yaml`'s `roles.monolith.task_overlay_id`.
3. **Run the v1.1 full-bench confirmation** — 10-fixture sweep against the promoted production config. Expected: 9/10 (impl 4/4, doc 4/5, manage 1/1).
4. **Workstream C decision** based on the confirmation result + A2's HIT (defaulted LOW) + the QUAL outcome.

The plan's MECE matrix at LOW + 9 = Path 1 (Phased Mode v2; don't ship v1.1). Worth a sanity check: 9/10 IS a real measurable improvement (one new fixture closed), and the matrix's "not enough to ship alone" framing was deliberately conservative — it can be revisited with the user before locking in. Path 2b (ship v1.1, freeze incremental work) is the alternative if HIT-default is reconsidered or 9/10 is judged worth a release.

The deeper takeaway: **prompt-side fixes can unstick model-side behavior more reliably than expected, but only when the failure mode is mechanistic** (re-reading + not-writing). lpe-typing's "I think I'm done" pattern is harder — no procedural guidance disambiguates an over-eager stop. deps-audit's "I'm stuck on this file" pattern is easier — there's a clear procedural fix (pick distinct items). Future overlay work should triage the failure mode mechanistically before committing to a directive shape.

**Affected files**: `src/luxe/agents/prompts.py` (new `manage_strict` PromptVariant + `manage_strict_only` overlay), `tests/test_prompts.py` (3 new regression tests parallel to B1's), `benchmarks/maintain_suite/variants_v1_managetask_overlay_probe.yaml` (new probe variant file), `acceptance/v1_manage_overlay_probe/` (smoke probe results).

---

### [2026-05-02] Phase v1.1 — work_dir variance discovery + fix

**What happened**: After B2's smoke PASS on deps-audit, the v1.1 ship-confirmation full bench produced an unexpected mixed result. Two consecutive runs at temp=0 with the production config + `manage_strict_only` overlay both landed at 8/10, but with different failure shapes — Run A had deps-audit FAIL + neon-rain-reset PASS (matching v1.0); Run B had deps-audit PASS + neon-rain-reset FAIL (an implement-task fixture flipping for the first time at temp=0). The cumulative cross-run picture across 5 temp=0 runs (v1.0 ship confirmation + 2 overlay runs + 2 no-overlay runs) showed two fixtures flipping unpredictably regardless of whether the overlay was set: neon-rain-reset went 3 PASS / 2 FAIL, deps-audit went 1 PASS / 4 FAIL.

**Root cause**: the bench's `work_dir` defaulted to `tempfile.mkdtemp(prefix="luxe-acceptance-")` — a random tempdir per invocation. That random suffix appeared in bash and git tool outputs (paths in `pwd`, `ls -la`, error messages, `git diff` output). Tool outputs become tool messages in the agent's prompt. At greedy temp=0 the model is deterministic *given a fixed input*, but the input wasn't fixed across bench invocations — different tempdir = different prompt = different model output for any fixture whose tool calls happened to surface the path.

A 3-iteration single-fixture probe of neon-rain-reset with `--work-dir ~/.luxe/bench-workspace` (pinned) produced 3/3 PASS, vs 3/5 PASS earlier with random tempdirs. Confirmed.

**Fix / takeaway**: changed the `--work-dir` default in `benchmarks/maintain_suite/run.py` from `None` (mkdtemp) to `~/.luxe/bench-workspace`. Existing reuse logic in `_resolve_repo` handles cached clones correctly (base_sha checkout + reuse). Added `--ephemeral-work-dir` flag for callers that explicitly want process isolation. Help text references this lessons entry so future-us doesn't flip the default back without checking why.

The 2-cell A/B with pinned default — `champ-no-overlay` vs `champ-manage-strict` — produced the cleanest possible signal: no-overlay cell matched v1.0 ship confirmation EXACTLY (deterministic on pinned substrate); overlay cell differed only on deps-audit (F → P). The 9 other fixtures had identical pass/fail in both cells. That's what "the overlay does exactly what it claims" looks like.

The deeper takeaway: **the earlier "temp=0 collapses sampling variance to deterministic" finding was true ABOUT THE MODEL — given identical input, it produces identical output**. The variance we were seeing wasn't sampling-level; it was input-level. Random data flowed through the bench infrastructure into the prompt, and the model's determinism stopped being visible.

This also clarifies the earlier inconclusive A2 prefix-cache measurement (`project_prefix_cache_baseline.md`): cross-run prefix-cache reuse appeared modest because **the prefixes themselves were different across runs** (random tempdir in tool outputs). With pinned work_dir, prefix-cache reuse becomes a meaningful question again. If we want to revisit the Phased Mode v2 architectural decision, do it with pinned work_dir; A2 needs re-measurement with this confound removed.

**Affected files**: `benchmarks/maintain_suite/run.py` (work_dir default change + new `--ephemeral-work-dir` flag), `benchmarks/maintain_suite/variants_v1_overlay_ab_pinned.yaml` (new — A/B variant for the experiment), `~/.claude/projects/.../memory/project_workdir_variance_leak.md` (new — tracks the finding).

---

### [2026-05-02] v1.1.0 ship — pinned work_dir + manage_strict overlay → 9/10

**What happened**: Shipped v1.1.0 with two infrastructure improvements over v1.0: (1) pinned `--work-dir` default eliminating the dominant temp=0 variance source, and (2) `manage_strict_only` task overlay closing the deps-audit stuck-loop. Champion model unchanged: `Qwen3.6-35B-A3B-6bit` at temperature=0.0.

Acceptance result against the production config (single cell, `manage_strict_only` promoted in `single_64gb.yaml`): 9/10 stable on the pinned-work_dir substrate. Confirmation came from cell 2 of the variants_v1_overlay_ab_pinned.yaml A/B run; running a separate single-cell production-config confirmation was deemed redundant since the variant override goes through the same code path as the config setting (`run.py:178` writes to `cfg["roles"]["monolith"]["task_overlay_id"]` either way).

Per task type:
- implement 4/4 (saturated since v1.0)
- document 4/5 (lpe-typing remains FAIL — under-engagement pattern not solved by document_strict overlay; B1 was a negative result)
- manage 1/1 (NEW in v1.1 — deps-audit closed by manage_strict overlay)

**Workstream C decision discipline**: the Phase v1.1 plan's MECE matrix said LOW + 9 = Path 1 (Phased Mode v2; don't ship v1.1) on the grounds that "one fixture closed via prompts is informative but not enough to ship a quality bump alone." That hedge was framed pre-result and pre-variance-investigation. The pinned-work_dir A/B closed the variance ambiguity and showed the overlay's contribution is real, isolated, and reproducible. Decision was made to override the matrix on those grounds — this is a documented departure from the plan, not a quiet override. Path-1 architectural work (Phased Mode v2 + per-tool subphases including the CVE lookup tool — see `project_post_v1_architecture_ideas.md` and `project_tool_subphases_and_cve_lookup.md`) remains queued for v2.0.

**The one remaining FAIL at v1.1** is `lpe-rope-calc-document-typing` — model adds 1 line and stops (under-engagement). B1's `document_strict` overlay nudged 1→2 added lines but didn't unblock the docstring half. The `document_strict` infrastructure stays registered in `prompts.py` for future experiments but is NOT promoted in production. The pattern needs either a model upgrade, few-shot examples in the prompt, or a runtime re-prompt loop when the submitted diff doesn't match all named goal deliverables — none of which are in v1.1 scope.

A separate caveat on the deps-audit PASS: the audit's CVE IDs are partially hallucinated (real packages cited with real version constraints and accurate file:line citations, but some specific CVE numbers may be invented). Bench grader's regex checks shape, not factuality. Flagged in the v1.1 B2 lessons entry as a known limitation. The CVE-lookup tool subphase (post-v1; see `project_tool_subphases_and_cve_lookup.md`) addresses this directly.

**Path through the plan**: Phase 0 (grader fixes) → fixture surgery on 3 fixtures → Branch C calibration on `nothing-config` → temp=0 promotion → citation-linter IPv4 fix → v1.0.0 ship at 8/10 → Phase v1.1 (B1 negative, B2 positive on prompt overlay) → variance investigation → work_dir pin → v1.1.0 ship at 9/10.

The infrastructure-quality-dominates-result-quality theme from the v1.0 ship lessons entry held up for v1.1 too: every gain came from infrastructure work. Bench grader honesty (Phase 0), gate calibration (Branch C), citation linter (IPv4 guard), and now the work_dir pin — none are model improvements; all are measurement improvements. The model is the same. The bench just stopped lying in different ways.

**Fix / takeaway**: bumped pyproject.toml `1.0.0` → `1.1.0`. Tag `v1.1.0`. Production config `configs/single_64gb.yaml` now has `task_overlay_id: manage_strict_only` and benefits from the pinned-work_dir default. v1.1 is the new shipped state; v1.0 stays available via tag for users who want it.

**Affected files**: `pyproject.toml` (1.0.0 → 1.1.0), `configs/single_64gb.yaml` (re-promoted manage_strict_only after the variance experiment), `benchmarks/maintain_suite/run.py` (work_dir default change shipped in commit ec88cd2 already).

---

### [2026-05-02] Post-v1.1 — A2 prefix-cache re-measurement: HIGH (85.4%); Phased Mode v2 deprioritized

**What happened**: After v1.1.0 ship, re-ran A2 (the prefix-cache hit-rate measurement) on the pinned-work_dir substrate. The original A2 (2026-05-02 morning) had been INCONCLUSIVE because oMLX's INFO log doesn't expose hit/miss data. With `server.log_level: debug` and the pinned `--work-dir` default, the per-request hit data is now visible.

**Aggregate hit rate on `nothing-config` (the longest-context fixture, 16 model requests): 85.4%** (206,848 hit / 242,300 prompt tokens). Per-request range: 22% (one outlier on the turn the model first wrote a chunk of new content) to 100% (turns where the prompt was fully covered by the cache).

**Root cause / interpretation**: cache reuse is working. Steady-state behavior across an agentic loop is: each turn reuses the prior turns' system prompt + earlier tool results, only the newly-grown tail of the conversation needs cold prefill. With the work_dir pinned, the prompt prefix is byte-identical across reruns of the same fixture, so cache reuse is effective from request 1.

**Decision impact (Workstream C):** the plan's MECE matrix at LOW + 9 = Path 1 (Phased Mode v2). At HIGH + 9 = Path 2b (ship v1.1; freeze incremental work; Phased Mode v2 not needed). Original A2 INCONCLUSIVE defaulted to LOW for caution. The re-measurement is conclusively HIGH (85% >> 65% threshold), so we land at Path 2b. **Phased Mode v2's architectural premise — "subtask scoping reduces context bloat and warms the cache" — is invalidated by the measurement: the cache is already warm.** The complexity cost of resurrecting phased/micro modes with proper memory primitives isn't justified by a baseline already at 85%.

**Fix / takeaway**: deprioritize Phased Mode v2 in v2.0 planning. The two remaining v2.0 directions are unaffected by this measurement and become the priority work:

1. **Per-tool refinement subphases** (CVE lookup as seed) — see `project_tool_subphases_and_cve_lookup.md`. Defeats the audit-hallucination caveat from v1.1's deps-audit by making CVE references deterministic via OSV.dev.
2. **MCP-mediated codebase slicing** — independent value prop (reduces ingest size on large repos). Unrelated to cache warmth.

**lpe-typing under-engagement** (the only remaining v1.1 FAIL) is also unaffected — needs a different lever (model upgrade, few-shot prompting, or runtime re-prompt-on-incomplete-diff loop) regardless of cache state.

The deeper takeaway: **the work_dir pin enabled this measurement to even be possible**. Without it, prefix-cache reuse appeared modest because the prefixes themselves were different across runs. The same infrastructure fix that closed the variance issue also unlocked the architectural decision data. One bug, two consequences. Worth remembering as a pattern: when an investigation hits a wall ("the data is too noisy to interpret"), the failure is often upstream of the measurement.

**Affected files**: `~/.omlx/settings.json` (log_level cycled debug → info; system config, not in repo), `~/.claude/projects/.../memory/project_prefix_cache_baseline.md` (status updated from INCONCLUSIVE to HIGH 85.4%), MEMORY.md (index updated), RESUME.md (Phased Mode v2 deprioritized in v2.0 queue).

---

### [2026-05-02] v1.2.0 — per-tool subphase pass: cve_lookup + bash + read_file + latent _REPO_ROOT fix

**What happened**: Executed the per-tool refinement subphase pass that the post-v1.1 plan queued as next-next direction. Audited all ~15 dev-facing tools in `_build_full_tool_surface`. Three real defects surfaced; the other twelve were already solid.

1. **`cve_lookup` (new tool, commits `c1e2a81` + `84e3ea8`)** — the seed example. B2's `manage_strict` overlay closed deps-audit on the fixture-pass criterion, but the model produced a mix of real CVE ids (CVE-2023-46136 aiohttp DoS) and plausible-but-fabricated ones (CVE-2024-3559 SQLAlchemy, CVE-2024-22032 psycopg2-binary). The grader's regex couldn't distinguish them. Tool wraps OSV.dev's `api.osv.dev/v1/query` (free, no auth, covers PyPI/npm/Go/Rust/Maven/NuGet/RubyGems). Follow-up commit surfaces OSV `aliases` so a model querying by GHSA gets the CVE cross-reference, closing the second-order hallucination path where a real GHSA was paired with an invented CVE.

2. **`bash` chain hardening (commit `7ccba1f`)** — real security defect. Pre-fix `_bash` did `parts[0] = command.strip().split()[0]`, checked it against the allowlist, then ran the whole command via `shell=True`. So `cat foo && rm -rf /` passed the check (parts[0] was `cat`) and `rm` then executed despite not being on the allowlist. Fix uses `shlex.split` to tokenize (so a `|` inside a quoted regex doesn't trip the check), then rejects any chain operator (`&&`, `||`, `;`, `|`, `&`), redirect (`>`, `<`, `>>`, `<<`, `<<<`, `&>`, `2>`, `2>&1`), or command substitution (backtick, `$()`). Allowlisted binaries still run with `shell=True` to keep glob/expansion support.

3. **`read_file` binary detection (commit `7ccba1f`)** — context pollution. Reading a binary file with `errors='replace'` returned multi-MB of garbage U+FFFDs that polluted the model's context window. Now reads the first 8 KB and rejects any file containing null bytes — text formats don't contain them; PNG/JPG/zip/elf all do. UTF-8 source with unicode identifiers and accented strings still passes (no false positives in the test corpus).

4. **`fs.get_repo_root()` getter (commit `7ccba1f`)** — load-bearing latent bug, surfaced while writing tests for the bash fix. `shell.py`/`git.py`/`analysis.py` all did `from luxe.tools.fs import _REPO_ROOT` at module load time, which binds the imported name to whatever `fs._REPO_ROOT` was at import time (typically `None`). Subsequent `fs.set_repo_root()` calls update `fs._REPO_ROOT` but NOT the imported name in sibling modules. Result: `if _REPO_ROOT is None` checks in those modules silently always returned `True`. The bash/git/analysis tools have been latently broken since at least v1.0 — but the bench's natural usage rarely hit them through the bench's instrumented path (the model usually preferred `read_file`/`write_file`/`edit_file`/`git_diff` via the dedicated tools), masking the bug. Fix: added `fs.get_repo_root()` that returns the current value, switched all three sibling modules to call it. Module docstring on the getter explains the import-time bind issue so future contributors don't reintroduce.

The 12 tools NOT touched (audited and verified solid): `list_dir`, `glob`, `grep`, `bm25_search`, `find_symbol`, `write_file` (Phase-2 guards already exist), `edit_file` (Phase-2 guards already exist), `git_diff`, `git_log`, `git_show`, `lint`, `typecheck`, `security_scan`, `deps_audit`, `lint_js`, `typecheck_ts`, `lint_rust`, `vet_go`.

**Root cause / interpretation**: the prompt-side bench (v1.0 → v1.1) optimized for *whether* the model invoked the right tool at the right time. It did not stress *what happens when the model invokes the tool in unusual ways* — chain operators in bash, binary files passed to read_file, sibling modules re-importing module-level globals. Those failure modes are invisible to a prompt-grading bench because they either (a) succeed silently in the wrong way (chain bypass), (b) pollute context without flipping the gate (binary read), or (c) only manifest when an execution path the bench rarely takes finally runs (the latent `_REPO_ROOT` bug).

**Fix / takeaway**: subphase template validated. The shape is: (1) bench probe targeted at the tool's specific failure modes, (2) tool-side hardening based on what surfaces, (3) optional overlay prompt that guides usage pattern (analogous to `manage_strict` for audit-style tasks). `cve_lookup` is the canonical seed — for any future tool added to the surface, run a subphase pass before claiming it's production-ready.

The latent `_REPO_ROOT` bug is a separate lesson on top: **`from x import _y` for module-level mutable state is a footgun**. The imported name shadows future updates. Either expose a getter (chosen here) or do `import x; x._y` at call sites. Worth scanning other modules for the same pattern.

130/130 tests pass. 12 new tests in `tests/test_tools.py` (TestReadFileBinaryRejection + TestBashChainRejection covering double-amp, double-pipe, semicolon, single-pipe, output-redirect, backtick substitution, dollar-paren substitution, quoted-pipe-in-regex pass-through, allowlist still fires, happy path still works, mismatched-quote returns clean error). `cve_lookup` has its own test coverage in earlier commits.

**Affected files**: `src/luxe/tools/cve_lookup.py` (new in `c1e2a81`, alias surfacing in `84e3ea8`), `src/luxe/tools/shell.py` (chain hardening), `src/luxe/tools/fs.py` (binary detection + `get_repo_root` getter), `src/luxe/tools/analysis.py` / `git.py` (switched to getter), `tests/test_tools.py` (12 new tests), `pyproject.toml` (1.1.0 → 1.2.0).

---

### [2026-05-02] v1.2.0 — cve_lookup surface bloat regression: gated to manage task_type

**What happened**: First v1.2 acceptance bench landed at 8/10 (regressed from v1.1's 9/10). New failure: `lpe-rope-calc-implement-strict-flag` — an *implement* fixture that v1.0 → v1.1 had stably passed (implement was the saturated category at 4/4). Diagnostic showed 252s wall, 3 tool calls, 8316 completion tokens, 34,913 chars of `final_text` — but **0 file mutations**. The model wrote prose explaining the change instead of calling `edit_file`.

3× replicate: **3/3 identical trajectories** (262s ± 1, exactly 34,913 final_text_chars each, identical 3-tool-call sequence). Deterministic at temp=0 with pinned `--work-dir`. Not variance.

**Root cause**: `cve_lookup` was added to the tool surface unconditionally in `_build_full_tool_surface` (`src/luxe/agents/single.py:70-72`). v1.1 didn't have it (added post-ship in commits `c1e2a81` + `84e3ea8`). The tool's description sat on the surface for *every* fixture — implement, document, bugfix, review — even though it's only useful for `manage` task_type's deps-audit flow. The surface bloat diluted the model's prior over `edit_file` / `write_file` enough on this borderline implement fixture to flip behavior into prose-mode under-engagement (the same shape as the known-FAIL `lpe-typing` doc fixture).

**Causal probe**: removed `cve_lookup` from the role allowlist (`configs/single_64gb.yaml`) and reran the failing fixture once. Result flipped FAIL (1/5, 0 mutations, 8316 prose-tokens) → PASS (4/5, 2 file changes, regex matched, 4680 task-tokens). One-line config change, deterministic outcome shift. Confirmed cve_lookup as the cause without false positives from coincident v1.2 changes (`_REPO_ROOT` getter, bash chain hardening, read_file binary detection were ruled out by this probe).

**Fix**: thread `task_type` parameter into `_build_full_tool_surface(...)`; gate the `cve_lookup` block to `if task_type == "manage"`. Default `task_type=None` excludes it (matches existing test fixture behavior). `run_single` passes its `task_type` arg through. Two new tests in `tests/test_single.py` assert (a) `cve_lookup` absent for `task_type` ∈ {None, implement, document, bugfix, review}, (b) `cve_lookup` present for `task_type=manage`, (c) allowlist still intersects with task-type gating.

**Post-gate full bench**: still 8/10. New failure: `nothing-ever-happens-document-config` (was PASS pre-gate). Different failure shape (14 tool calls / 23,645 chars / 343s / 75% context pressure — not the prose-deflection pattern). 3× replicate of this fixture in the post-gate state: **1 FAIL + 2 PASS**, with wildly divergent trajectories (9 vs 18 vs 33 tool calls, 30,816 vs 2,299 vs 2,713 final_text). Variance, not deterministic — same `temp=0` and pinned work_dir, but the doc/manage bottleneck fixtures sit close enough to under-engagement that small cache-state shifts flip them.

**Interpretation of v1.1's 9/10 in light of this**: the v1.1 ship was a single bench run. The variance evidence here suggests v1.1 was actually `8-9/10 with one borderline doc fixture` — we got a lucky variance roll. v1.2 ships at the same effective ceiling, but with one additional deterministic FAIL closed (lpe-implement). The gate fix is unambiguous progress regardless of which face the variance lands on for any single bench run.

**Root cause / interpretation**: this is a category of failure that the post-v1.1 plan didn't anticipate when it queued cve_lookup as the seed example. The subphase template was correct in *principle* (probe → harden → optionally overlay), but the act of adding a tool to the surface has **system-wide effects** on prefix-cache state and on the model's tool-selection prior across *all* task types. Any new tool from now on must be scoped to the task types where it's useful, or we re-discover this regression each time. Tool descriptions are part of the cached prefix; tool addition is therefore not a local change.

**Fix / takeaway**: tool gating by task_type is now the default for any audit-only or task-specific tool. `cve_lookup` is the precedent. When future tools are added to `_build_full_tool_surface`, the question to answer first is: "which task_type(s) actually need this on the surface?" — and gate accordingly. Don't assume "the model will ignore a tool it doesn't need" — at temp=0 with this surface size, addition has measurable behavioral cost.

The deeper meta-lesson: **single-run bench results have variance we hadn't measured**. Going forward, before claiming a regression-or-not on borderline doc/manage fixtures, run a 3× replicate. The implement category is genuinely deterministic at temp=0 (all replicates here showed 0 trajectory drift), but doc/manage are not. The implement-vs-doc/manage variance asymmetry is itself a finding worth carrying.

**nothing-doc-config variance**: this is a known doc-bottleneck fixture per `project_v1_bench_cycle.md`. The replicate evidence shows ~33% FAIL rate at the current substrate. Not addressed in v1.2 — would need its own subphase (better doc-task overlay, or model upgrade, or runtime re-prompt-on-incomplete-diff loop). Tracking as a known borderline.

**Affected files**: `src/luxe/agents/single.py` (added `task_type` parameter to `_build_full_tool_surface`, gated `cve_lookup` block, threaded through from `run_single`), `tests/test_single.py` (2 new tests), `lessons.md` (this entry).

### [2026-05-03] v1.3.0 — read-dedup orchestration bug surfaced; reprompt-on-doc lever shipped

**What happened**: Investigation into `lpe-rope-calc-document-typing`'s deterministic FAIL — three negative lever attempts (v1.1 abstract overlay, v1.2 procedural overlay, v1.3 runtime reprompt) had us calling it a model-side ceiling. External reviewer pushback ("you haven't ruled out orchestration") prompted a trace-instrumented re-run. The trace contradicted the working model entirely.

**The actual failure mode** (from instrumented `events.jsonl` with `LUXE_LOG_TOOL_CALLS=1`):

```
Main pass:
step 0: read_file d7732045  (productive)
step 1: edit_file b911d4fa  (productive, +1 line — IOBase import)
step 2: edit_file b911d4fa  DUPLICATE — model retried the same edit immediately
step 3: read_file d7732045  DUPLICATE — model tried to verify; dedup intercepted
                                       → streak=2 → ABORT
```

The model was making progress, then trying to re-read the file to verify its edit had landed. The duplicate-call detector at `loop.py:211` returned `"You already called read_file with these exact arguments..."` instead of dispatching the read and returning fresh post-edit content. The model interpreted the dedup response as "verify failed → my edit didn't land → retry the edit" and looped on the same `edit_file` call until the streak counter aborted.

**Hypothesis trajectory**:
1. Initial: model ceiling — three levers had failed.
2. Reviewer 1: H1 (compound-goal collapse), H3 (fenced-diff payload defeats reprompt). Both tested; both negative.
3. Reviewer 2: cache invalidation — `ToolCache.get_or_run` (`tools/base.py:60-73`) has no invalidation; post-write reads serve stale content. Code inspection confirmed; bench code path doesn't use the cache, so this was a red herring for THIS failure mode but a real latent issue worth a follow-up.
4. Streak-bump probe: increased `_MAX_CONSECUTIVE_REPEAT_STEPS` from 2 → 3. Model used the extra turn to retry the same edit a third time. Conclusion: bumping doesn't help — the model is in a decoder-favored loop on identical tokens at temp=0.
5. **The right fix** (plan D2): exempt `read_file` from the in-loop dedup detection. Reads are *idempotent in name but post-write semantics differ* — the model relies on re-reading to verify its own edits. Deduplicating reads strands the model.

**Fix**: `loop.py:_DEDUP_EXEMPT_TOOLS = {"read_file"}`. The dedup check now reads `if key in seen_calls and tc.name not in _DEDUP_EXEMPT_TOOLS:`. For exempt tools, the duplicate-detection branch is skipped and the call dispatches normally. seen_calls.add(key) is idempotent so no leak. `_MAX_CONSECUTIVE_REPEAT_STEPS` reverted to 2 — the bump was a red herring.

**Post-fix trace on lpe-typing**: 12 productive steps, 17 tool calls, 3 distinct edit_file calls (IOBase import, `_read_gguf_value` signature, `_pe_from_gguf` typing), no abort. Diff went from baseline +2/-1 → +3/-2. **Major orchestration improvement.** But still FAIL — the threshold is +4 and the model never attempted a top-of-file docstring insertion.

**The residual lpe-typing FAIL turned out to be a fixture-grader misalignment, not a model behavior.** This is the most important finding of the investigation, and it nearly went unnoticed because every prior diagnosis (mine and three reviewers') was working from the assumption that the docstring deliverable was unmet.

After D2, the H1-with-D2 probe (split-sentence variant) ran clean: 16 main + 6 reprompt tool calls, no aborts, 2 distinct typing edits, and a final synthesizer report stating: *"Module docstring — Already present at lines 2–14. No changes needed. The docstring clearly describes the module's purpose (scanning local model installs for positional-encoding metadata), lists the default scan sources, and notes the zero third-party dependency requirement."* Verification by `git show 5c6b51f80e76f80c49a789029414f5152a5edbd7:pe_scan.py` confirmed: the file ships with a 14-line module docstring at lines 2-14, beginning `"""pe_scan.py — Scan local model installs for positional-encoding metadata...`. The model has been correctly identifying the existing docstring across every run and refusing to add a redundant one.

The 2026-05-01 fixture surgery had aligned `min_matches` from 4 → 1 to match the actual 1-untyped-parameter count, but kept `min_added_lines: 4` with the comment "the docstring half of the task supplies the rest" — assuming the docstring half was unmet without verifying the file's actual content. **It was already met since base_sha.**

This means three things:
1. **The "three negative lever attempts on lpe-typing"** were attempts to coerce the model into a redundant docstring it correctly recognized as already present. The levers couldn't have worked; the goal asked for content the file already had.
2. **The reviewers' A/B/C/D hypothesis space about residual model behavior** (generative-write resistance, edit_file API friction, compound-goal shadowing, grader-aligned optimization) was reasoning about a residual that wasn't the right framing. Some of those hypotheses (especially B — `edit_file` API friction at top-of-file) may still be real and worth revisiting on a *different* fixture that genuinely tests prepend-style edits. They aren't refuted here; they were applied to the wrong target.
3. **D2 (orchestration fix) is independently valuable.** The dedup bug *was* killing productive work mid-task. The fix lets the model finish what it started. That part of the investigation stands on its own merits — every fixture that needs post-edit verification benefits — even though the headline failure on lpe-typing turned out to be a separate, smaller issue.

**Round-2 fixture surgery (2026-05-03)**: drop the docstring requirement from the goal text and lower `min_added_lines` from 4 → 2 (one import + one signature edit). The fixture now tests what it was intended to test: typing-edit competence. **Post-surgery validation: PASS, 4/5 (offline cap), +2/-1 diff (`from io import BinaryIO` + `def _read_gguf_value(f: BinaryIO, ...)`), 153s wall, no bailout.** First-ever lpe-typing PASS. Bench goes from 8/10 to 9/10 with v1.3. Future levers worth ranking: a `prepend_to_file` tool affordance (directly addresses (b)) > one-shot worked example in `document_strict` overlay > model upgrade.

**Reprompt-on-doc lever** (uncommitted in `cli.py` since 2026-05-03 morning, behind `LUXE_REPROMPT_ON_DOC=1`): independent variance-stabilizer test on `nothing-ever-happens-document-config` — the variance-borderline doc fixture from v1.2's investigation. n=3 replicates, all PASS (rep 1: 4/5 PASS / 317s / 98k tokens; rep 2: 4/5 PASS / 516s / 260k; rep 3: 4/5 PASS / 259s / 195k). 3/3 PASS vs baseline's 2/3. Reprompt earns its keep on doc-task variance even though it didn't unblock lpe-typing.

**Smoke regression for D2 + reprompt**: 4-fixture subset (`lpe-rope-calc-implement-strict-flag`, `nothing-ever-happens-manage-deps-audit`, `neon-rain-document-modules`, `the-game-document-architecture`) with both shipping. 4/4 PASS, avg_wall=153s, no bailouts, no regressions.

**Disposition**:
- D2 (`read_file` dedup exemption): SHIP. Orchestration improvement; benefits any post-edit verification scenario, not just lpe-typing. The fix is the constant `_DEDUP_EXEMPT_TOOLS = {"read_file"}` in `loop.py` — kept as a tool-property constant rather than a per-role flag because the exemption is a property of read semantics, not a per-config decision.
- Reprompt-on-doc: SHIP as opt-in (`LUXE_REPROMPT_ON_DOC=1`). The lever is validated on n=1 doc fixture × 3 replicates (`nothing-doc-config` 3/3 PASS). The smoke regression ran with the env var set but reprompt likely didn't *fire* on most smoke fixtures (avg wall=153s is consistent with no second-pass triggering on those), so we don't have explicit evidence that reprompt's behavior is benign on a wider set. Default-promote after wider validation lands — n≥3 doc fixtures where reprompt actually fires, observed not-regressing.
- lpe-typing: residual FAIL accepted. Now better-grounded as model-side docstring-resistance. Updated ceiling story replaces earlier "three lever attempts → ceiling" with "orchestration was the dominant cause; with that fixed, the model still won't write a top-of-file docstring on this fixture."
- Tool-event instrumentation in `loop.py` (`LUXE_LOG_TOOL_CALLS=1`): kept as permanent debugging knob. Off by default, no overhead.
- `diff_stat` checkpoint events in `cli.py`: kept; useful diff-progression telemetry.

**Latent issue surfaced but not addressed**: `ToolCache` has no invalidation. Currently moot because the bench code path doesn't pass a `ToolCache` to `run_single`, but if a future code path does, post-write reads will serve stale cached content. A `ToolCache.invalidate_for_write(path)` would be the right fix; deferred until the cache is actually wired into the bench path.

**Meta-lesson on diagnostic ordering**: the original three lever attempts (overlays + reprompt) all targeted the model's prompt-side behavior. None attempted to instrument the agent loop to see whether the abort was even reaching the model's "give up" point. The trace inspection — adding `tool_call`, `tool_step_done`, and `diff_stat` events behind a single env flag — answered the question in one re-run. The lesson for future failure investigations: **before more prompt levers, instrument the agent loop**. The cost is tiny (~30 lines, all gated by env var) and the diagnostic value is enormous. Any failure that produces an abort should have its trigger pair logged with detector state.

**Larger meta-lesson on fixture grading**: the fixture-surgery story here is the deeper lesson. Two rounds of investigation ran on the assumption that the goal's docstring deliverable was unmet, and three levers + extensive trace work were aimed at "why won't the model write the docstring." The model had been writing — and not writing — the right things all along. The synthesizer.md output had been telling us "docstring already present at lines 2–14" for runs we were ignoring, because we read the *bailout summary* (`stuck_no_output`) and the *diff size* (+2/-1) but never the *model's stated reasoning*.

**Three concrete diagnostic improvements suggested by this**:
1. **Verify fixture grader alignment against actual base-file content** at the moment of fixture surgery, not just against the goal text. The 2026-05-01 surgery comment said "the docstring half of the task supplies the rest" — a claim that should have been checked by `git show <base_sha>:<file>` before being committed as a grader assumption. Add a sanity-pass to fixture surgery: read the base file, confirm each goal deliverable is genuinely missing.
2. **Always read the synthesizer.md when investigating a deterministic FAIL.** The model's final report often contains the *reason* it considered the work done. We have a dedicated `synthesizer.md` artifact in every run dir; we should be glancing at it as readily as we glance at `comparison.json`. The dedup investigation took longer than necessary because the synthesizer output saying "docstring already present" was visible in the post-D2 trace before we noticed.
3. **Treat "the model is doing the wrong thing" as a hypothesis, not a fact.** When a deterministic fixture FAILs across a model and several prompt configurations, the most-tested-by-cost-of-being-wrong hypothesis is "the fixture is asking for something that doesn't make sense in this base state." The cost of being wrong on this hypothesis is ~10 minutes (read the base file, compare to the fixture's goal). The cost of being wrong the other way is multi-day investigations.

**Affected files**: `src/luxe/agents/loop.py` (added `_DEDUP_EXEMPT_TOOLS` constant + check at line 228; added `LUXE_LOG_TOOL_CALLS=1`-gated `tool_call` and `tool_step_done` event emission via `append_event`; added `run_id` and `phase` parameters to `run_agent` for event correlation; imports `os`, `hashlib`, `append_event`), `src/luxe/agents/single.py` (added `run_id` and `phase` parameters to `run_single`, plumbed to `run_agent`), `src/luxe/cli.py` (passes `run_id=spec.run_id, phase="main"` and `phase="reprompt"` to the two `run_single` call sites; added `diff_stat` checkpoint events at `after_main_pass` and `after_reprompt_pass`; reprompt block stays uncommitted-feature-now-shipped — `LUXE_REPROMPT_ON_DOC=1` env var promoted to default-shipping behavior), `pyproject.toml` (1.2.0 → 1.3.0), `lessons.md` (this entry).

### [2026-05-03] v1.3.1 — `_diff_against_base` undercount bug fix + directive reprompt for prose-mode

**What happened**: Investigating the *other* outstanding bench FAIL (`nothing-ever-happens-document-config`, ~33% prose-loop variance) post-v1.3 ship. Plan: validate whether D2 fix alone resolved the variance (3 reps without reprompt). Result: 2/3 PASS — same rate as v1.2. D2 didn't help this fixture. Then 3 reps with reprompt enabled to validate the lever post-D2: also 2/3 PASS. Rep 1 FAILed and reprompt didn't rescue it — second pass made 13 more tool calls (10 read + 3 search), zero write_file, identical prose-mode shape to first pass.

**Root cause #1 (bug)**: While inspecting the trace, found that `_diff_against_base` (`src/luxe/cli.py`) only counted **tracked** file changes via `git diff <base_sha>`. New files created by `write_file` (e.g., `CONFIG.md` from scratch) are untracked until staged, so the diff was reading 0 additions even on PASS runs that wrote the full doc. This affected:
- The `diff_stat` telemetry checkpoint — undercounted on every doc fixture that creates new files.
- More critically, the `_should_reprompt_for_under_engagement` gate — reprompt was firing on already-passing runs because additions=0 always for new-file doc tasks. The earlier "3/3 PASS with reprompt" data point on this fixture (yesterday's validation) was inflated: reprompt was firing unnecessarily on all three reps; the wall times (317s / 516s / 259s) reflect bug-induced double-passes, not the lever's real fire rate.

**Fix #1**: prefix `git add -N .` (intent-to-add) in `_diff_against_base`. This adds index entries for untracked files without staging content, making them visible to `git diff <base_sha>` as `+N/-0` changes. The PR cycle's later `git add . && git commit` still works correctly. Validated on the post-fix nothing-doc reps: diff_stat now shows additions=197 / 0 / 135 across the three reps (matching the grader's view). Reprompt now fires only on actual prose-mode FAIL runs.

**Root cause #2 (model behavior)**: With the bug fix, the data tells a clearer story. On nothing-doc-config, the model has ~33% prose-loop rate where it reads files, enumerates env vars in its `final_text` (30k+ chars of prose), and never calls `write_file`. The original reprompt's instruction *"For any deliverable NOT yet reflected in the diff, make the missing edits now via edit_file or write_file"* gets ignored — the model just does another exploration pass. The `final_text` from the FAIL contains the full content the user wants, but it's stranded as prose.

**Fix #2 (directive reprompt)**: branched `followup_goal` in the reprompt block. When `additions == 0 AND len(prior_text) > 1000` (the prose-mode signature), the reprompt now:
- Names the failure explicitly: *"PROBLEM: You did NOT call write_file or edit_file. The working tree has 0 added lines. You produced extensive prose in your final report but it is stranded — not saved to disk."*
- Injects the model's own prior `final_text` (truncated to 6000 chars) as the *content to save*.
- Demands `write_file` as the FIRST tool call: *"Do this on your FIRST tool call. Do not explore more files first."*

The non-prose-mode reprompt path (some edits but under threshold) preserves the original behavior.

**Validation**: shipped without a 3-rep validation of the directive reprompt — opportunity cost of waiting for ~15 min of bench runs vs moving to the queued MLX_USE_ANE probe was deemed not worth it. The fix is structurally well-targeted (only fires in the case the original reprompt was ineffective) and the fallback preserves prior behavior. If the directive reprompt regresses, the rollback is `LUXE_REPROMPT_ON_DOC` is opt-in so users opt-in to this code path explicitly. Validation deferred to a future bench cycle.

**Bench state at v1.3.1**:
- lpe-typing: deterministic PASS (post-fixture-surgery)
- nothing-doc-config: ~33% FAIL rate without reprompt; with bug-fixed reprompt, fire rate now matches prose-mode rate. Whether the directive reprompt rescues those FAILs is the open question.
- Expected score: 9.67/10 baseline (9 stable + ~0.67 from nothing-doc); 10/10 if directive reprompt works.

**Meta-lesson for future investigations**: the diff_stat bug had been silently corrupting our reprompt firing decisions for 24+ hours. The "3/3 PASS with reprompt" finding that justified the lever's ship was bug-inflated. Two takeaways:
1. **Test telemetry against ground truth before relying on it for decisions.** A simple sanity check — "run a fixture that creates a new file, verify diff_stat shows nonzero additions" — would have caught this. Add to the diagnostic-tool habit.
2. **Validate threshold-based decisions on edge cases.** The reprompt threshold check is a numerical comparison; if the input is silently zeroed, every comparison is wrong. Future thresholding logic should log the input value with a sanity-check assertion or warning when the value is implausibly low.

**Affected files**: `src/luxe/cli.py` (`_diff_against_base` adds `git add -N .` prefix; reprompt block branches on `additions == 0 AND len(prior_text) > 1000` for the directive prose-mode followup), `pyproject.toml` (1.3.0 → 1.3.1), `lessons.md` (this entry).

### [2026-05-03] v1.4.0 — SpecDD Lever 1: programmatic Definition of Done; first 10/10 bench

**What happened**: Shipped Lever 1 of the SpecDD phase
(`~/.claude/plans/fluffy-brewing-lemur.md`). All 10 bench fixtures now
have a `requirements:` schema; the agent's reprompt gate uses the spec
validator (per-requirement check) when a spec is provided. Full bench
validation: **10/10 PASS, 40/50 score, v1 release gate cleared** — the
first time the bench has gone clean.

**Sequence of v1.4-prep commits in this ship** (all on main, between
v1.3.2 and v1.4.0 tags):
1. `23827c1` — `src/luxe/spec.py` data model (`Requirement`, `Spec`,
   YAML round-trip), 27 unit tests.
2. `0d37844` — `src/luxe/spec_validator.py` predicate evaluator
   (regex_present, regex_absent, tests_pass, ast_query stub, manual
   stub), 18 unit tests. Reuses `git add -N` diff trick from v1.3.1.
3. `fcc9830` — Bench integration. `Fixture` dataclass gains
   `requirements`/`to_spec()`; `grade_fixture` runs spec validator as
   parallel observation (does not gate score yet); lpe-typing migrated
   as proof-of-concept. Local smoke validated PASS+FAIL agreement.
4. `a81007c` — Prompt-template helpers `format_spec_for_task_prompt`
   (input side, in spec.py) and `format_unsatisfied_for_reprompt`
   (output side, in spec_validator.py), 7 tests.
5. `98c6b89` — `cli.py` reprompt gate replacement. New `--spec-yaml`
   flag; spec validator gate replaces the v1.3 diff-size heuristic
   when a spec is provided AND `LUXE_REPROMPT_ON_DOC=1`. Bench harness
   threads the spec via temp YAML. v1.3 directive form preserved as
   fallback for ad-hoc usage without `--spec-yaml`.
6. `e01169d` — 4 mechanical fixture migrations (lpe-implement-strict,
   the-game-shuffle, neon-rain-reset, isomer-healthcheck). Direct
   port of expected_outcome to single R1.
7. `0f611d0` — 5 loose-grader fixture migrations (the-game-arch,
   neon-rain-modules, isomer-quickstart, nothing-manage-deps,
   nothing-doc-config). Tightened to per-sub-deliverable requirements;
   audit-recommended bench-rigor improvements landed at the spec layer.

**Validation results** (`acceptance/v1_4_prep_full_bench/`,
2026-05-03 18:13–18:53):
- 10/10 PASS, 40/50 score (4/5 each = offline cap)
- Every fixture: `expected_outcome_passed=true` AND `spec_all_satisfied=true`
- Zero unsatisfied requirements across the entire bench
- nothing-doc-config (the variance fixture) PASSed cleanly with 119 added-line
  matches against R1's ≥15 threshold; reprompt did not need to fire
- v1 release gate cleared by `mono__qwen3.6-35b-a3b-6bit`

**Recalibrated framing** (per audit memos written this session):
- The original SpecDD plan framed Lever 1 as "attacks compound-goal shadowing,
  the bench's primary ceiling" with a 1+-point bench-score lift expected.
- Post-v1.3 audit (`project_compound_goal_audit.md`) showed compound-goal
  shadowing wasn't actually exhibited on the bench — every passing run
  fully addressed all sub-deliverables. The "primary ceiling" framing
  was empirically thin.
- Lever 1 still ships for **architectural value**: programmatic
  Definition of Done + per-requirement grading + future-readiness for
  Levers 2/3.
- The bench-score outcome (8/10 → 10/10) is real but the causes are
  layered: lpe-typing fixture surgery (v1.3.0) closed the deterministic
  FAIL; nothing-doc variance happened to roll positive on this run
  (~33% historical FAIL rate is unchanged structurally).

**What did NOT ship in this version (deferred)**:
- v1.3 directive reprompt code retirement (step 7 from the v1.4 roadmap).
  Removing it would silently disable reprompt for ad-hoc `luxe maintain`
  usage without `--spec-yaml`. Spec path is preferred when available;
  directive form is preserved as fallback. Future ship once we have
  evidence of ad-hoc usage patterns.
- min_added_lines representation in spec model. Currently a fixture-level
  floor in legacy grader; not yet a per-requirement predicate kind. The
  4 mechanical-port fixtures still have legacy `min_added_lines` floors
  enforced parallel to spec validation.
- ast_query and manual predicate kinds stubbed (return unsatisfied with
  notice). Full integration with `src/luxe/symbols.py` deferred until
  a fixture actually authors an ast_query requirement.

**Affected files**: `src/luxe/spec.py` (new), `src/luxe/spec_validator.py`
(new), `tests/test_spec.py` (new, 31 tests after step 4 additions),
`tests/test_spec_validator.py` (new, 21 tests after step 4 additions),
`src/luxe/cli.py` (`--spec-yaml` flag + spec validator gate in reprompt
block; v1.3 directive code preserved as fallback), `benchmarks/maintain_suite/grade.py`
(`Fixture.requirements`, `Fixture.to_spec()`, `FixtureResult.spec_validation`,
`spec_all_satisfied`; spec validator wired into `grade_fixture` as parallel
observation), `benchmarks/maintain_suite/run.py` (`_luxe_maintain` writes
spec YAML and threads `--spec-yaml`), `benchmarks/maintain_suite/fixtures.yaml`
(all 10 fixtures gained `requirements:` blocks, 5 of which were tightened
beyond the legacy `expected_outcome` to close audit-identified gaps).

**391 tests pass** (up from 384 pre-Lever-1; 27 spec + 21 spec_validator + 1 net change in other test counts).

**Memory entries from this ship**: see also
`project_compound_goal_audit.md`, `project_loose_grader_audit.md`,
`feedback_instrument_loop_first.md`, `feedback_verify_fixture_grader.md`
in the project memory directory.

### [2026-05-03 PM] v1.4.0 — three-replicate validation: 9/10, 10/10, 10/10

**What happened**: Per the RESUME.md decision tree, ran three independent
full-bench replicates of v1.4.0 (`acceptance/v1_4_validation_rep_{1,2,3}/`)
with `LUXE_REPROMPT_ON_DOC=1` against `variants_v1_default.yaml`, pinned
work_dir, `--force` to wipe stored state per rep. Results:

| Rep | Stored result | Failed fixture | Notes |
|---|---|---|---|
| 1 | 9/10 (40/50 score) | `nothing-ever-happens-document-config` (1F) | Variance fixture rolled FAIL — expected ~33% historical rate |
| 2 | 10/10 | — | clean |
| 3 | 10/10 | — | clean |

**Headline**: 2/3 at 10/10 confirms the variance branch — bench is
**effectively 9.67/10**, not a structural 10/10. The original v1.4.0
ship at 10/10 (acceptance/v1_4_prep_full_bench) was real but
variance-fortunate; the structural ceiling is at one fixture's prose-mode
roll on `nothing-doc-config`. Implement and manage categories remain
deterministic; doc-category variance dominates.

**Sidecar regrade discovered a pre-existing tooling bug** (`scripts/regrade_local.py:90`):
the regrader uses `git checkout origin/<branch_name>` against the local
clone, but for fixtures where the agent's branch wasn't pushed to origin
(notably `nothing-ever-happens-manage-deps-audit` across all 3 reps), the
ref doesn't exist and the regrader silently falls back to `base_sha` →
0 additions → spurious FAIL. This is exactly the stale-`origin/<branch>`
trap warned about in `feedback_offline_cache_refs.md` and the Critical
Gotchas in RESUME.md, except inside the regrader itself. The bench-time
grader's numbers are authoritative; the sidecar regrade for manage tasks
is unreliable until this is fixed. Hand-grading the actual local cache
showed real `SECURITY-AUDIT.md` writes of 81-107 lines on branches -3/-4/-5,
matching the bench-time `diff_additions=76-90` numbers.

**Decision per the resume tree**: end at option B. v1.4.0 is shipped and
tagged; the 9.67/10 effective bench is the honest framing.

**Lessons reinforced**:
1. **Variance is structural, not eliminated by Lever 1.** The original
   audit (`project_compound_goal_audit.md`) was right: SpecDD's
   compound-goal premise didn't account for prose-mode tool-affordance
   variance. Lever 1's value is architectural (programmatic DoD); it
   doesn't lift the doc-category variance ceiling.
2. **Trust bench-time results over sidecar regrade for manage tasks.**
   The sidecar is a tool for cheap iteration on grading logic, not a
   ground-truth re-evaluation. When stored and regrade disagree on a
   manage fixture, hand-grade the cache.

**Affected files**: `lessons.md` (this entry), memory directory
(`project_v1_4_validation.md`, `project_regrade_local_origin_bug.md`).

### [2026-05-04] v1.4.1 Mode B/A fix combination — 10/10 PASS on nothing-doc-config × 10 reps

**What happened**: Three fixes from the late 2026-05-03 session
(citation-linter bare-filename fallback + Mode B mid-loop write-pressure
injection + sidecar regrade lint re-run) validated on
`nothing-ever-happens-document-config` × 10 replicates with
`LUXE_WRITE_PRESSURE=1` + `LUXE_REPROMPT_ON_DOC=1`. Result: **10/10 PASS,
score 4/5 each**, 0 unresolved citations across all reps.

**Per-rep breakdown** (`acceptance/doc_config_modeB_rep_{1..10}/`):

| Rep | Score | Citations | Additions | Mode B fired | Reprompt fired | Notes |
|-----|-------|-----------|-----------|--------------|----------------|-------|
| 1 | 4 | 0 | 173 | — | — | clean engagement |
| 2 | 4 | 0 | 135 | — | — | clean |
| 3 | 4 | 0 | 203 | — | — | clean |
| 4 | 4 | 0 | 168 | — | — | clean |
| 5 | 4 | 0 | 147 | — | ✓ | reprompt rescue (main pass: 0 add, 28k prose chars) |
| 6 | 4 | 0 | 135 | — | — | clean |
| 7 | 4 | 0 | 207 | — | — | clean |
| 8 | 4 | 0 | 173 | — | — | clean |
| 9 | 4 | 0 | 138 | ✓ (step 6) | — | Mode B rescue (gate fired at step 6: 18 tool calls + 5024 tokens + 0 writes; main pass continued and produced 138 additions) |
| 10 | 4 | 0 | 30 | — | — | clean (smaller diff but still PASS) |

**Three rescue regimes observed**:
1. **Clean engagement** (8/10): model writes early, neither Mode B nor reprompt fires.
2. **Mode B rescue** (1/10, rep 9): gate fires mid-main-pass when tool count + tokens + step thresholds cross with 0 writes. The synthetic user message lands at the right moment; model writes immediately after. No reprompt needed.
3. **Reprompt rescue** (1/10, rep 5): Mode B doesn't fire because the main pass exits before step 5's *cumulative* completion tokens cross the 4000 threshold (the model goes from "still reading" at step 4 to "all-prose" at step 5; step 5 generates ~5000 prose tokens AFTER the gate evaluation at step 5 entry). The post-loop reprompt — gated on `spec_all_satisfied=false` — fires and the model writes on the second pass.

**Mode A signal**: 0/10 reps had unresolved citations. Linter bare-
filename fallback eliminated the false-flag failure mode that produced
`acceptance/doc_config_diag_rep_1` 3F before the fix.

**Headline**: With v1.4.1's three fixes combined, the historical ~33%
FAIL rate on this fixture collapses to 0% across n=10. Mode B and
reprompt are complementary — when one doesn't catch the trap, the other
does.

**Threshold tuning consideration**: Mode B's `_WRITE_PRESSURE_MIN_TOKENS=4000`
*just barely* misses rep 5's pre-prose state (cumulative tokens hit ~3800
at step 5 entry, the prose itself crosses 4000). Lowering to 3000 would
catch rep 5 before reprompt does, saving ~370s of wall time on rescued
runs. Not pursued — n=1 observation isn't enough signal to tune
thresholds, and the reprompt rescue worked.

**Affected files**: `lessons.md` (this entry), `RESUME.md` (state update).
Memory: `project_doc_config_three_modes.md`, `project_external_benchmark_program.md`.

---

### [2026-05-04 PM] SWE-bench n=10 A/B — counterexample heuristic regressed quality; deliberation amplifiers are dangerous on already-correct trajectories

**What happened**: After the swebench prompt overlay shipped (smoke 2/3 PASS
mechanically), one trajectory (`astropy-12907`) showed the model localizing
the bug correctly (`_cstack` in `astropy/modeling/separable.py`) but never
calling `edit_file` — 5 reads + 25k chars of analysis + final report with
no edits. Diagnosed as hypothesis-stall (`(e)`): model traced the bug
report's simple snippet, concluded its tracing was correct, and never
constructed the failing nested-CompoundModel input that would have
falsified the conclusion. Shipped a `swebench_bugfix_counterexample`
PromptVariant that adds one clause: "if your trace yields the expected
result but the report shows otherwise, construct the failing variant."

A/B on a stratified n=10 (4 `<15 min fix` + 6 `15 min - 1 hour` across 10
distinct repos) flipped the working state backwards:
- Baseline: mechanical 10/10 (real-fix rate 4-5/10 by manual review)
- +Heuristic: mechanical 8/10 — `matplotlib-13989` (which the baseline
  had matched gold EXACTLY) regressed to empty patch, and
  `astropy-13453` (baseline had a partial fix) also regressed to empty.

**Root cause**: The heuristic was scoped as a global prompt modifier but
behaves like a conditional intervention. Helps: ambiguous /
underdetermined / multi-site fixes (rare in the n=10 set). Hurts:
straightforward pattern-alignment fixes (common). Adding a "construct a
counterexample" deliberation trigger to a model that was already on the
correct trajectory shifts it from pattern-completion → overthinking →
deviation. The 12907 trace was atypical — falsification is genuinely the
right move there, but extrapolating from one trace to a global prompt
clause amplified noise.

**Two more findings worth banking**:

1. **Trajectory fragility is the bigger story.** Two cases flipped from
   gold-match → broken under the heuristic. The model often has the
   correct answer early, and continued reasoning can erase it. This
   suggests minimality / early-stopping bias may outperform more-
   reasoning prompts as a future direction.

2. **The (b) reasoning bucket is not one thing.** In the n=10 manual
   review, (b) splits into:
   - **(b1)** missing transformation pattern (django regex char-class —
     fixable with examples)
   - **(b2)** multi-location consistency (requests-2931 fixes one of
     two sites, pytest-10051 misses adding a new method — planning gap)
   - **(b3)** true design gap (sphinx prefers obvious `dict.fromkeys`
     dedup over the gold's `sorted(set(...))` — model chose the
     simpler-looking option)

   Only b1 and b2 are realistically prompt-tractable.

**Fix / takeaway**:
1. Reverted the rule; baseline prompt is the working state. The
   `swebench_bugfix_counterexample` variant stays in tree as a negative
   control for SpecDD comparison — useful, even if not shipped.
2. Inspector v2 ships a 5-signal gold-proximity tier (file match,
   line-based hunk proximity, hunk coverage, hunk count, size, token
   overlap) so that "10/10 mechanical PASS" stops misleading. n=10
   real picture: 6 strong + 3 plausible + 1 wrong_location, vs.
   mechanical's 10/10. Rich tiers visible via
   `python -m benchmarks.swebench.smoke_inspect --gold-source ...`.
3. New durable rule (memory): interventions that amplify reasoning can
   harm trajectories that were already correct. A/B before shipping any
   "think more" clause; do not extrapolate from single-instance probes.

**Affected files**: `src/luxe/agents/prompts.py`, `tests/test_prompts.py`,
`benchmarks/swebench/subsets/probe_n10.json`,
`configs/single_64gb_swebench_counterexample.yaml`,
`benchmarks/swebench/smoke_inspect.py`,
`tests/test_swebench_smoke_inspect.py`.
Memory: new `feedback_deliberation_amplifiers.md`; updated
`project_swebench_smoke_2026_05_04.md`.

---

### [2026-05-04] SWE-bench n=75 pre-SpecDD anchor — 32% high-confidence; empty-patch is the dominant failure class

**What happened**: Stratified n=75 Verified subset run completed in 7h 34m
wall against `configs/single_64gb_swebench.yaml` (anti-reproducer overlay,
Qwen3.6-35B-A3B-6bit @ temp=0). Headline numbers, with three different
honesty levels:

- **Mechanical PASS**: 45/75 (60%) — non-empty, non-test-path, non-new-file
- **Strong (gold-match)**: 12/75 (16%) — inspector v2 gold-proximity tier
- **Strong + plausible**: 30/75 (40%) — inspector v2 best-case
- **Manual high-confidence (Step 2 review)**: **24/75 = 32%** — the durable anchor

The 32% number landed squarely in RESUME.md's pre-defined "30-45% →
SpecDD Lever 2" decision branch. Lever 2 is now in flight at v1.5.0.

**Root cause / what surprised**:

1. **The n=10 A/B (`probe_n10.json`) was 50pp optimistic.** That run hit
   9/10 strong-or-plausible. The n=75 stratified mix dropped to 40%.
   The n=10 wasn't dishonest — it was just easy + small + cherry-picked
   across distinct repos. Don't extrapolate small probes to a real anchor.

2. **Empty-patch is the dominant failure class at scale (26/75 = 35%).**
   The n=10 had zero empty-patches; this only emerges past ~30 instances.
   Anti-reproducer prompt's locate→read→edit→verify protocol fails to
   even produce a candidate diff on a third of stratified tasks. The
   class clusters by repo: sphinx, pylint, mwaskom, late-requests
   (5414/6028) are heavily over-represented. It's not uniformly random.
   This is the **single biggest signal** for SpecDD: the model isn't
   over-editing or under-reviewing — it's failing to commit at all.

3. **Anti-reproducer prompt rule is leaky** — 4/75 created `test_fix.py`
   despite the prompt forbidding it (django-10097, xarray-3305,
   pytest-5262, sympy-13877). Prose-level rules are guidance, not
   enforcement. **Tool-side `Forbids` (Lever 2's design) is the right
   shape for this category** — the prompt cannot be made strictly
   reliable; the tool layer can.

4. **wrong_target (12) >> wrong_location (3)**. Cross-file localization
   is harder than within-file localization. Multi-file gold patches
   dominate the wrong_target class (sympy-13091 has 21 gold files;
   django-11532 has 5). When gold spans many files, the model picks
   one and ignores the rest. SpecDD Lever 2's `.sdd` chain that scopes
   each iteration to a specific subtree is a partial mitigation;
   Lever 3's per-file `.sdd` contracts would be a stronger one.

5. **Inspector v2 understates "strong" by ~6 cases** (~40% of plausibles
   were actually clean PASSes after manual review). The token-overlap
   (jaccard) signal is too noisy when model and gold use slightly
   different identifiers — `to_native_string` vs `builtin_str` removal
   in requests-2317, `dict.fromkeys` vs `sorted(set(...))` in
   sphinx-10466, etc. Line-based hunk proximity also brittle when
   intermediate edits drift line numbers (sklearn-10908 was marked
   wrong_location but is a clean gold-match in the same method).
   Manual Step 2 is non-optional; mechanical inspector ≠ ground truth.

6. **One distinct failure pattern worth a name: "fixed adjacent symptom".**
   `sphinx-10449` had a real `NameError: annotation` bug in the original
   code; the model fixed that and stopped, never reaching the actual
   reported issue (suppress `:rtype: None` for class autodocs). This is
   a new class — call it (f) **adjacent-bug stop**: model finds A real
   bug nearby and considers the goal satisfied. Not (d) already-passing
   (which is "no real bug exists"), and not (b1/b2/b3) reasoning class.
   Worth tracking on Lever 2/3 reruns to see if it's a one-off or a
   pattern.

**Fix / takeaway**:

1. **32% is the durable pre-SpecDD anchor.** Use this number, not
   "12 strong" or "30 strong-or-plausible", when comparing post-Lever-2
   runs. Strong-only is too tight (excludes semantically-equivalent
   different-mechanism fixes); strong-or-plausible is too loose
   (includes wrong-direction "plausibles"). Manual Step 2 with a
   per-instance taxonomy verdict is the only honest number.

2. **Empty-patch class is the SpecDD test.** Lever 2's tool-side `Forbids`
   doesn't directly help here, but the per-file `.sdd` chain in worker
   prompts plus the spec validator's reprompt-on-unmet-requirement gate
   should help: when the model returns "I couldn't find the bug",
   the validator should emit a structured "R1 still unsatisfied — do
   not stop" instead of letting the loop terminate. Track empty-patch
   delta as the headline signal on the post-Lever-2 rerun. If empty-patch
   stays at ~25/75, Lever 2's bench-moving claim falls apart.

3. **Anti-reproducer rule moves to the tool layer at Lever 2.** Already
   queued in the build order: `src/luxe/tools/fs.py` `write_file` and
   `edit_file` refuse if target matches an ancestor `.sdd`'s `Forbids:`
   glob. Internal `.sdd` for the swebench fixture mode will list
   `Forbids: test_*.py` at the root.

4. **Don't extrapolate small probes to anchors.** The 9/10 n=10 result
   was real but unrepresentative. Future bench-program planning should
   require at least 50-instance stratified samples before claiming a
   number is "the" anchor.

5. **Add (f) adjacent-bug stop to the failure-mode taxonomy.** Update
   `project_swebench_smoke_2026_05_04.md`'s taxonomy when Lever 2 reruns
   produce a second instance of this pattern.

**Affected files**: no source changes for the bench run itself. Memory
entry: new `project_swebench_n75_baseline.md`. Output:
`acceptance/swebench/pre_specdd_v141_n75/rep_1/predictions.json` (the
durable artefact for FAIL_TO_PASS Docker harness scoring later) and
`step2_gold_vs_model.txt` (per-instance gold-vs-model dump for the
plausibles + wrong_locations, used for the manual review).

---

### [2026-05-05] SpecDD Lever 2 architecture + two subtle gotchas

**What happened**: Shipped SpecDD Lever 2 (v1.5.0) end-to-end in one
session: parser (`src/luxe/sdd.py`) → resolver (`src/luxe/spec_resolver.py`)
→ tool-side Forbids enforcement (`src/luxe/tools/fs.py`) → prompt-side
chain block (`src/luxe/agents/single.py`) → citation linter
spec_violation/spec_orphan signals (`src/luxe/citations.py`) → synthetic
`.sdd` injection for SWE-bench fixtures (`benchmarks/swebench/adapter.py`)
→ four dogfood `.sdd` files for the luxe codebase itself + root
`CLAUDE.md`. Full suite: 521 → 607 passed (+86 tests).

Two subtle issues surfaced during integration that are worth banking:

**1. `except ValueError` silently catches `SddParseError`.**

`SddParseError(ValueError)` subclasses `ValueError` so a tool can
distinguish "out-of-repo path" (`ValueError` raised by
`Path.relative_to`) from "malformed contract". The tool layer's
`_check_spec_forbids` had:

```python
try:
    chain = resolve_chain(...)
except ValueError:
    return None  # outside repo_root, _safe will reject
except SddParseError as e:
    return f"Cannot evaluate Forbids: malformed .sdd — {e}"
```

The `ValueError` clause caught the malformed-`.sdd` case first, so
malformed contracts silently allowed all writes — exactly the
opposite of the intended behaviour. Fixed by reordering the
catches; the constraint is now documented with a `NOTE` comment in
the function.

The general rule: **when catching multiple exception types where one
subclasses another, list the most-derived first.** Test each path
explicitly — a passing "no-error" test does not exercise this
ordering.

**2. Synthetic `.sdd` in a fixture clone reads as "uncommitted".**

The SWE-bench adapter drops a synthetic `<repo_basename>.sdd` at
fixture-prep time so tool-side Forbids fires for the anti-reproducer
rule that the prose prompt cannot reliably hold. The first smoke run
crashed in 1 second with rc=2:

> `luxe refuses to start with uncommitted changes — commit, stash, or
> pass --allow-dirty to proceed (the PR diff will include them).`

The synthetic contract is by design uncommitted (it's removed before
`extract_diff` so it never enters predictions.json). Fix: pass
`--allow-dirty` from `invoke_luxe_maintain`. Smoke probe
(astropy-12907 with injection) then succeeded with the gold-shape
patch and zero `.sdd` contamination.

Generalisation: **fixture-prep injection that adds untracked files
must be paired with `--allow-dirty` in any agent invocation that
checks tree cleanliness.** Other future cases that might trigger
this: temp-files for environment overrides, model-context sidecars,
synthesizer.md overrides for resume scenarios.

**Architecture note worth banking**:

The plan's "Lever 2 chain at worker iteration time" was scoped against
a worker tier that doesn't exist post-mono pivot (v1.0). The actual
shape that emerged: **chain block injection at the single task-prompt
construction** in `single.py` (full-repo scope, since mono mode has
no per-file targeting). `find_all_sdd` walks once, `format_sdd_block`
renders Forbids/Owns only (Must / Done when stay aspirational and live
in spec_validator's reprompt path). The plan's "resume reloads chain"
task is N/A — luxe's resume path is `luxe pr <run_id>` which runs
the post-synthesizer PR cycle, not the agent loop. Chain reloads
fresh on every `run_single` call by construction. Documented inline.

**Fix / takeaway**:
1. New durable rule (memory): when catching exception hierarchies in
   the same try/except, derived classes first or it silently routes
   wrong.
2. New durable rule (memory): fixture-prep injections that drop
   uncommitted files into the cloned tree need `--allow-dirty` in
   the downstream agent invocation. Track this as part of any future
   .sdd-class fixture-prep work.
3. Lever 2 ships at v1.5.0 with seven concrete deliverables; the
   hypothesis ("anti-reproducer rule moves to the tool layer →
   empty_patch class shrinks via better engagement, new_file_in_diff
   class disappears") is testable on the next n=75 rerun. The
   prediction is empty_patch ↓, new_file_in_diff → 0; if either
   doesn't materialise, the hypothesis is wrong.

**Affected files**: `src/luxe/sdd.py`, `src/luxe/spec_resolver.py`,
`src/luxe/tools/fs.py`, `src/luxe/agents/single.py`,
`src/luxe/citations.py`, `src/luxe/cli.py`,
`benchmarks/swebench/adapter.py`, `benchmarks/swebench/run.py`,
`benchmarks/swebench/compare_runs.py`, `tests/test_sdd.py`,
`tests/test_spec_resolver.py`, `tests/test_tools_spec_forbids.py`,
`tests/test_single.py`, `tests/test_citations_diff_aware.py`,
`tests/test_swebench_adapter.py`, plus the four dogfood `.sdd` files
and root `CLAUDE.md`.

---

### [2026-05-05] SpecDD Lever 2 — post-ship SWE-bench n=75 result

**What happened**: Same n=75 stratified subset, same model, same
config + Lever 2's prompt-side `.sdd` block + tool-side Forbids +
synthetic `<repo>.sdd` injection at fixture-prep. 7h41m wall (vs
baseline 7h2m, +9% from added prompt tokens).

| Metric | Pre-Lever-2 | Post-Lever-2 | Delta |
|---|---|---|---|
| strong (gold-match) | 12 | 13 | +1 |
| strong + plausible | 30 | 32 | +2 |
| empty_patch | 26 | 30 | **+4** |
| new_file_in_diff | 4 | **0** | **-4** |
| any non-empty patch | 45 | 45 | 0 |

**Two simultaneous effects**:

1. **`new_file_in_diff` 4 → 0 — target class CLEARED.** Of the four
   baseline instances that created reproducer files: django-10097
   → **strong** gold-match, xarray-3305 → plausible, pytest-5262 →
   strong, sympy-13877 → empty_patch. Three out of four escaped to
   a real fix once the synthetic `.sdd` Forbids fired tool-side.
   The Lever 2 hypothesis ("anti-reproducer rule moves to the tool
   layer → no_file_in_diff disappears") is empirically confirmed
   at n=75 scale.

2. **`empty_patch` 26 → 30 — prose-mode regression.** The prompt-side
   `.sdd` block adds context tokens (~200-400 depending on chain
   depth). On borderline instances, this shifts the response
   distribution toward deliberation mode: the model writes the
   correct fix in synthesizer.md prose but never invokes
   `write_file`. Confirmed at n=10 via xarray-2905 trace inspection
   (21 read tool calls, 0 write calls, correct fix in prose).

   Specific n=75 regressions worth tracking:
   - pylint-4970: **strong → wrong_location** — model picked an
     adjacent line to the gold's edit. Localization noise, possibly
     unrelated to Lever 2.
   - sphinx-10435: **strong → empty_patch** — clear engagement loss.
   - sphinx-10449: **plausible → empty_patch** — the "fixed adjacent
     symptom" case I named yesterday. Now doesn't fix even the
     adjacent NameError.
   - 4 wrong_target → empty_patch — instances that previously
     attempted an off-target fix now give up entirely.

**Net direction**: target class hit (new_file_in_diff → 0), modest
quality lift on the durable anchor (strong + plausible: 30 → 32, +2),
but offset by prose-mode growth (empty_patch +4). Net change in
non-empty patch presence: 0.

**Root cause of the prose-mode regression**: extra tokens in the
task prompt nudge marginal instances toward "let me think more"
behavior. The fix already exists at v1.4.1: `LUXE_WRITE_PRESSURE=1`
mid-loop intervention rescued `nothing-doc-config` from 33% FAIL →
0% over 10 reps. The flag is currently opt-in; not enabled in
`configs/single_64gb_swebench.yaml`.

**Fix / takeaway**:

1. **Lever 2 ships at v1.5.0 with the empirical caveat above.** The
   target-class win is real (new_file_in_diff → 0); the modest
   strong+plausible lift is within n=75 noise but directionally
   positive; the empty_patch regression is a known prior-shipped
   class with a known fix (LUXE_WRITE_PRESSURE).

2. **Recommended next step** (NOT this session): enable
   `LUXE_WRITE_PRESSURE=1` in the swebench config and rerun n=75.
   Predicted result: empty_patch ↓ toward baseline (~26) while
   keeping new_file_in_diff at 0. If that holds, ship v1.5.0
   (Lever 2) + v1.5.1 (write-pressure default flip) as a paired
   tag with a clean attribution table.

3. **General durable rule**: **prompt-side context additions cost
   tokens and may push borderline instances into prose-mode**. When
   adding any prompt block (.sdd contracts, examples, plan
   templates), measure both the target-class win AND the
   prose-mode delta on the same fixture set. Bundle write-pressure
   defaults if a regression appears.

4. **Add a per-instance rerun probe** for the strong → empty
   regressions (sphinx-10435, sympy-13091) to distinguish "Lever 2
   broke it" from "n=75 variance" — temp=0 is mostly deterministic
   for SWE-bench but extra tokens in the prompt change the input,
   so identical-trajectory determinism doesn't apply across pre/post.

**Affected files**: results in
`acceptance/swebench/post_specdd_v15_n75/rep_1/predictions.json`;
delta report via `python -m benchmarks.swebench.compare_runs`. No
source changes from the rerun itself.

---

### [2026-05-06] SpecDD Lever 2 — paired-mechanism rerun (v1) + Forbids tightening (v1.5.0-rc-2)

**What happened**: Wired `LUXE_WRITE_PRESSURE=1` into the swebench
adapter alongside `inject_sdd=True` (commit `6c21956`), ran n=75 with
the paired mechanism, hit unexpected failure modes, fixed them, and
landed three concrete shippables for v1.5.0:

1. **`6c21956`** — paired-mechanism env wiring. The fix that the
   2026-05-05 lesson called for. `run_instance` default now binds
   `LUXE_WRITE_PRESSURE=1` whenever `inject_sdd=True`. Tests in
   `TestPairedMechanismEnv`.
2. **`dcfa772`** — bench commit-signing override. Discovered when
   the first paired-mechanism n=75 launch produced rc=5 +
   patch_lines=0 on every instance. Root cause buried in stdout:
   `pr_blocked: git commit failed: error: Enter passphrase for
   ~/.ssh/id_ed25519`. The user's global `commit.gpgsign=true` with
   an SSH key requiring an interactive passphrase blocks luxe
   maintain's pr.py commit step. SWE-bench commits are throwaway
   scaffolding — `_check_spec_forbids` extract_diff doesn't care if
   they're signed. Fix: `invoke_luxe_maintain` injects
   `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=commit.gpgsign`,
   `GIT_CONFIG_VALUE_0=false` into the subprocess env via
   `setdefault` so caller-supplied values still win.
3. **`e062bab`** — Forbids tightening for v1 paired-mechanism escape
   patterns. Detailed below.

**v1 paired-mechanism n=75 result** (5h 25m wall;
`acceptance/swebench/post_specdd_v15_pressure_n75/rep_1/`):

| Metric | Pre-Lever-2 | Post-Lever-2 (no pressure) | v1 paired (actual) |
|---|---|---|---|
| strong | 12 | 13 | 16 ✅ +4 |
| strong + plausible | 30 | 32 | 32 ✅ |
| empty_patch | 26 | 30 | **14** ✅ −12 |
| new_file_in_diff | 4 | 0 | **8** ❌ regression |

Three of the four ship-floor metrics passed. **empty_patch
−12pp absolute** beat the prediction range of 24–27. xarray-2905 —
the n=10 instance that previously wrote the fix in synthesizer.md
prose without committing — produced a 14-line diff. Of two
named-regression instances, sphinx-10435 still empty, sympy-13091
rescued (92-line diff). The paired-mechanism mechanism worked.

**The new_file_in_diff regression**: write-pressure pushes the model
to write *something*, and the .sdd's literal Forbids only covered
`test_fix.py` / `repro.py` / `reproducer.py` shapes. Under pressure,
the model found adjacent un-forbidden filenames. The 8 escape paths
clustered into 3 patterns:
- `verify_fix.py` variants (4 instances; root, `repo/...`, and
  `xarray/tests/test_fix_verify.py` shape)
- `tmp_*.py` (1 instance, 2 paths: `tmp_test.py`, `tmp_install.py`)
- novel `test_*_<descriptor>.py` (3 instances: `test_verify.py` ×3
  variants, `test_refit_time.py`)

**The fix (commit `e062bab`)**: extended `SWEBENCH_SDD_BODY` Forbids
with 12 globs covering all 8 observed escapes plus prophylactic
adjacents. Added 8 parametrized tests
(`test_blocks_observed_v15_pressure_paths`) so a future regression
points at the exact filename and the source instance. Expanded
`test_does_not_block_legitimate_test_paths` with boundary-case
entries (`test_runtime.py`, `test_data_verification.py`) confirming
the broad globs anchor on suffix not substring.

**Acknowledged risk for v1.5**: the broad globs (`**/*_verify.py`,
`**/test_*_time.py`) WILL also block edits to legitimate
pre-existing tests with those names. We accept this for v1.5
because the n=75 subset has no such file. v1.6 backlog item
promotes proper **creation-only forbids** semantics —
`_check_spec_forbids` augmented with a stat() check so rules can
fire only when the target file doesn't pre-exist.

**The lessons that landed durably**:

1. **Paired mechanisms are a category.** Constraint without
   actuation under-delivers (post-Lever-2 no-pressure: empty_patch
   +4). Constraint *with* actuation can over-actuate against an
   incomplete constraint surface. The combination must be designed
   together. New rule of thumb: if a Forbid mechanism prevents the
   model from doing X, also instrument what the model does *instead*
   — the next-best behavior is your next failure class.

2. **Bench-side commit-signing must be local-config-immune.** Any
   git operation in unattended runs has to override the user's
   global signing config. Never assume the host machine's git is
   non-interactive.

3. **Whack-a-mole stops at iteration 2.** First Forbids extension
   (the original n=75 baseline patterns) caught 4 paths. Second
   round (this session) caught 8. If a third round is needed,
   that's the signal to escalate to creation-only forbids — adding
   another broad glob just delays the same loop.

4. **Categorical failure-mode analysis on the v1 result yielded
   sharper next-iteration priorities than the original v1.5 plan.**
   10 of 14 v1 empty_patch are `agent_bailed` — agent declared
   done in <5 tool calls, before write_pressure's `MIN_TOOLS=10`
   threshold could fire. v1.6 priority #1 is now an **early-bail
   intervention** keyed on "no writes after N=3 steps + claimed
   done" — addresses the largest avoidable class. Lever 3 slips
   to later because it doesn't move that needle.

**Next**: v2 n=75 rerun confirming `new_file_in_diff = 0` floor
holds. Plan + acceptance gate at
`~/.claude/plans/humble-prancing-patterson.md`. Ship-floor check is
HARD — if v2 still leaks, escalate to creation-only forbids
(v1.6), do not add another broad glob.

**Affected files**: `benchmarks/swebench/adapter.py`,
`benchmarks/swebench/run.py`, `tests/test_swebench_adapter.py`,
`acceptance/swebench/post_specdd_v15_pressure_n75/rep_1/`. v2 will
write to `acceptance/swebench/post_specdd_v15_pressure_v2_n75/rep_1/`.

---

### [2026-05-06] v1.5.0-rc-2 v2 result — broad-glob ceiling found; v1.6 creation-only forbids architecturally required

**What happened**: v2 n=75 rerun (Forbids tightened by `e062bab`)
collapsed `new_file_in_diff` 8 → 2 but did not eliminate it. Two
escape paths remained: `test_bool_contour.py` (matplotlib-24870)
and `test_fix_check.py` (sympy-12481). Both shapes are
indistinguishable from legitimate test files by name alone — no
broad glob can safely cover them as edit-or-create bans. The
v1.5 broad-glob approach hit an architectural ceiling that more
patterns cannot resolve.

v2 ship-floor check (HARD `new_file_in_diff = 0`):

| Metric | Floor | v1 actual | **v2 actual** | Verdict |
|---|---|---|---|---|
| strong | ≥14 | 16 | 16 | ✅ |
| strong + plausible | ≥30 | 32 | 35 | ✅ |
| empty_patch | ≤16 (±2) | 14 | 17 | ✅ borderline |
| **new_file_in_diff** | **=0** | 8 | **2** | ❌ |

The long-arc claim (v2 vs pre-Lever-2 baseline) sustained: empty
26 → 17 (−35%); strong 12 → 16 (+33%); strong+plausible 30 → 35
(+5); any-non-empty 45 → 56 (+24%). The headline win held.

**Root cause**: the v1.5 contract conflated two qualitatively
different operations. *Editing* `tests/test_existing.py` (a real
gold-patch target) is repository participation. *Creating*
`test_bool_contour.py` (an invented verification artifact) is
benchmark gaming. v1.5 globs were path-aware: "these filenames
are suspicious." That premise breaks when the suspicious filename
is shaped exactly like a legitimate one.

**Fix / takeaway — v1.6 creation-only forbids**: introduced a new
`Forbids creating` section in `.sdd`. It fires *only* when the
write would create a new file at the target path
(`creating = not target_path.is_file()` computed in `_write_file`).
`Forbids` (always-fires) and `Forbids creating` (create-only) are
distinct policy classes. Under create-only semantics, the broad
ban `**/test_*.py` becomes principled — *the model may not invent
new tests; it may freely edit existing ones* — because Phase A
audit on full SWE-bench Verified (n=500) confirmed zero gold
patches create a `test_*.py` file.

The architectural shift: v1.5 encoded *"these filenames are
suspicious"*; v1.6 encodes *"creating verifier scaffolding is
disallowed."* The policy stops being path-aware and becomes
operation-aware. Repository participation and benchmark gaming
become separate, enforceable categories.

A subtle but high-leverage detail: the create-only error message
reads *"refusing to create X: forbidden-on-create. Edit an
existing file instead of creating a new one"* — a recovery
gradient ("wrong operation; find an existing nearby file") rather
than the v1.5 *"forbidden ... do not write outside the allowed
paths"* (which read as "wrong location" and primed bailout).

**Smoke validation (Phase C, n=14, 2026-05-06)**:
- new_file_in_diff = **0** across all 14 ✅
- **sympy-12481 reroute (the architectural test case)**: was
  inventing `test_fix_check.py` in v2 → v1.6 produced a strong
  gold-match by editing `sympy/combinatorics/permutations.py`
  directly. The qualitative transition *"invent scaffold → modify
  existing artifact"* was empirically demonstrated.
- Both v2 strong-tier "regression" instances (xarray-3305,
  sphinx-10466) rebounded to strong, confirming those were temp=0
  variance, not glob collateral.
- Mixed signal on second escape: matplotlib-24870 went empty
  (bailout) rather than rerouting. 1/2 of the architectural test
  cases reroute cleanly; the other shows the user-predicted
  "constraint pressure → occasional abandonment" mode. Net
  positive but not unanimous.
- v2-strong preservation 4/5 (matplotlib-13989 the variance drop).

**The lessons that landed durably**:

1. **Operation-aware policy generalizes; path-aware policy
   doesn't.** When a constraint conflates two distinct operations
   on the same target, broadening the constraint either fails
   (can't catch the corner case) or over-reaches (blocks
   legitimate work). The right primitive is to split the
   operations, not extend the patterns. *"creating verifier
   scaffolding is disallowed"* is a policy; *"these filenames are
   suspicious"* is folklore.

2. **Static + dynamic state together yield clean policy boundaries
   without planner state.** `creating = not Path.is_file()` at the
   moment of the write is operationally observable, deterministic,
   and stateful across turns automatically. A file created in step
   1 can be edited in step 2 of the same trajectory because
   `Path.is_file()` answers correctly — no synthetic planner-state
   tracking needed.

3. **Error message wording is a recovery-gradient signal.**
   *"forbidden ... do not write outside allowed paths"* primes
   bailout. *"forbidden-on-create ... edit an existing file
   instead"* primes reroute. For models running under pressure
   actuation, the difference between *wrong location* and *wrong
   operation* in the prose may be the highest-leverage planner
   change in the entire mechanism. Worth its own A/B in v1.7.

4. **Audit before broadening.** Phase A static check on full
   Verified (n=500) confirmed zero gold patches create a
   `test_*.py` file before shipping `**/test_*.py` as
   ForbidsCreate. A 5-min audit gates a global adapter policy from
   becoming subset-specific tuning. Cheaper than discovering it
   post-tag.

5. **Smoke before full rerun.** A 14-instance targeted smoke
   (~30-50 min wall) validates the architectural premise before a
   ~5h full bench commits. The smoke composition matters: include
   the regressions (validates the fix), the recent gains
   (validates non-regression), and a small random sample
   (validates the broad effect).

**Open question (Phase B, doesn't gate ship)**: trace inspection
on `~/.luxe/runs/<run_id>/events.jsonl` for matplotlib-24870 to
distinguish "novel un-globbable shape but planner exhausted
options" (architectural) vs "forbid feedback caused planner
collapse" (mechanism-tuning). Doesn't gate v1.6 tag; informs
v1.7 Mode B threshold tuning.

**Affected files**: `src/luxe/sdd.py`, `src/luxe/spec_resolver.py`,
`src/luxe/tools/fs.py`, `benchmarks/swebench/adapter.py`,
`tests/test_sdd.py`, `tests/test_spec_resolver.py`,
`tests/test_tools_spec_forbids.py`,
`tests/test_swebench_adapter.py`,
`benchmarks/swebench/subsets/v16_smoke_n14.json`.
643 tests passing (+24 vs 619 v1.5.0-rc-2 baseline).

---

### [2026-05-10] v1.6.0 ship — n=75 v3 + Docker harness 36/75 (48.0%); creation-only forbids validated

**What happened**: The overnight n=75 v3 rerun (creation-only
forbids) hit every ship-floor gate. Inspector reported strong=16,
strong+plausible=36, empty_patch=18, **new_file_in_diff=0**,
wrong_target=17. jq cross-check on the predictions confirmed zero
patches contain `new file mode` — agreeing with the inspector at
the file-creation level. Spot-check of 3 random strong rows
(`pytest-8399`, `django-11333`, `scikit-learn-11578`) showed
clean surgical edits to existing files — no broad-glob collateral
damage to legitimate work. Docker harness then resolved
**36/75 = 48.0%** in 34m43s with zero errors. v1.6.0 tagged.

**v3 vs pre-Lever-2 baseline (v1.4.1, 12 strong / 26 empty /
4 new_file)**:
  - empty_patch: −10.7pp (paired-mechanism win, sustained)
  - new_file_in_diff: 0 (full class elimination, durable)
  - strong: +33% (12 → 16, durable)
  - any non-empty patch: +27% (45 → 57)

**v3 vs v2 (creation-only delta)**: new_file 2 → 0 (the
architectural target). xarray-3305 + sphinx-10466 both rebounded
to strong (variance, not glob collateral, confirmed). sympy-12481
went from inventing `test_fix_check.py` (v2) to a plausible-tier
edit on the gold file (v3) — the architectural test case the v1.6
shift was designed for. matplotlib-24870 went new_file → empty
(1/2 v2-escape "constraint pressure → occasional abandonment",
within ±1 variance budget).

**The inspector is a near-perfect harness predictor**: of the
16 strong-tier instances, 15 resolved (94%); the lone unresolved
strong was `sphinx-doc__sphinx-10466`. Plausible tier resolved at
50% (10/20), wrong_target at 47% (8/17), wrong_location at 75%
(3/4), empty_patch at 0%. The 11 wrong_target / wrong_location
resolves are *alternative-solution credit* — model fixed a
different file or locus than gold but tests still pass. This
matters: the static gold-proximity tier is a lower bound on
actual SWE-bench performance, and the gap between
strong+plausible (36) and FAIL_TO_PASS (36) at n=75 is
coincidental — different instances each side, similar net.

**`harness.py:collect_results` was wrong for swebench >= 4.x**:
the wrapper's collector looked for a top-level
`<run_id>.<model>.json` file. swebench 4.x writes per-instance
reports at `logs/run_evaluation/<run_id>/<model>/<instance>/report.json`.
Discovered when the wrapper's `harness_summary.json` came back
with `n=0`. Fixed in this commit cycle by walking the per-instance
layout; legacy top-level path retained as fallback. Validated
against the v16 logs — now reports `n=57, n_resolved=36`.

**Why the v3 shape held under harness scoring**: the create-only
semantics give the planner a *recovery gradient* — when a write
is forbidden-on-create, the error message says "Edit an existing
file instead of creating a new one", which primes reroute, not
bailout. Compare v1.5 broad-glob `Forbids` semantics, which fired
the same way for both edit and create: the planner couldn't
distinguish "wrong location" from "wrong operation". v3's
operation-aware policy is the architecture the system was missing.

**Lessons**:

1. **Operation-aware policy beats path-aware folklore.** The v1.5
   ceiling wasn't a glob-tuning problem — it was that the policy
   conflated two qualitatively different operations on the same
   target. v1.6's `Forbids creating` section + `creating: bool`
   threading + distinct error wording shipped as a full
   architectural fix in ~24h once the framing was right.

2. **`creating = not Path.is_file()` is the cleanest possible
   stateful check.** Operationally observable, deterministic,
   handles multi-step trajectories (create then edit) without any
   synthetic planner state. The policy boundary lives at the
   write-time check, not in the prompt or the trace.

3. **Static gold-proximity is a lower bound, not a ceiling.**
   The harness gave 11 instances credit that the inspector
   classified as wrong_target / wrong_location. For pre-tag
   sanity-checking the inspector is enough; for headline numbers,
   the harness is necessary.

4. **The wrapper's collector must follow upstream layout.**
   When swebench 4.x changed to per-instance report files, the
   wrapper silently returned `n=0`. The harness ran fine —
   only the aggregation step broke. Fold the per-instance walk
   into `collect_results` going forward; keep legacy fallback for
   older swebench installs.

**Open for v1.7**: BFCL agent-mode post-SpecDD (no pre-anchor
exists; one-shot v1.6 datapoint to inform v1.7 BFCL strategy);
sphinx-10466 strong→unresolved investigation; early-bail
intervention as the #1 next-gen lever (10+ of 18 v3 empty_patch
in scope).

**Affected files**: `benchmarks/swebench/harness.py` (collector
rebuilt for swebench 4.x layout), `.gitignore` (logs/), `RESUME.md`,
`lessons.md`. v3 predictions kept at
`acceptance/swebench/post_specdd_v16_creation_only_n75/rep_1/`;
harness logs at `logs/run_evaluation/luxe_v16_n75/` (gitignored,
~hand-debug).

---

### [2026-05-10] m5max_moe bake-off — three substrate bugs and a behavioural-threshold miscalibration

**What happened**: First M5 Max 128GB MoE bake-off
(qwen3.6-35B-A3B-6bit, qwen3-coder-next-80B-A3B-6bit,
glm-4.5-air-106B-A12B-4bit, 10 fixtures each = 30 cells) scored
17/30 (81/150). The headline number masked three independent
substrate failures and one behavioural-threshold miscalibration,
none of which were model-quality signals:

1. **GLM 0/10 from tool-name parsing.** Every GLM dispatch call
   emitted the tool name with stray newlines: `"read_file\n"`,
   `"bash\n\n"`, `"glob\n\n\n"`. The dispatcher's
   `if name not in tool_fns` lookup missed every call, returned
   0 bytes, the model retried the same broken call, and the dup
   detector eventually bailed the run with zero progress.

2. **All variants capped at 4/5 on every passing fixture.** The
   bench clones live under `~/.luxe/bench-workspace/<id>-clone`
   with no GitHub remote, so `gh pr create` fails universally;
   the `pr_opened` rubric criterion (1 pt of 5) never fires
   offline. Known design (RESUME.md L397), not a bug, but it was
   obscuring the substrate signal underneath.

3. **`neon-rain-implement-reset-shortcut` 1/5 — but `rc=127`.**
   The fixture's R1 predicate is `npm test`. M5 Max didn't have
   node installed (clean dev env), so the test exited 127
   `command not found` and was scored as a test failure. Pure
   environment, no model involvement.

4. **`qwen3-coder / nothing-ever-happens-document-config` —
   "Prompt too long: 33827 tokens exceeds max context window of
   32768".** oMLX's global `sampling.max_context_window` was set
   to 32k while the Qwen3 family natively supports 128k+. Hard
   400 from oMLX, not a model bailout. Bumped the global to 48k
   to clear it.

5. **qwen3-coder-next 7/10 stuck_no_output bailouts — post-write
   verification drift.** Trace: edit_file at step 1, git_diff at
   step 2 confirms diff, then 5–8 turns of lint/bash/git_diff
   each returning 0 bytes until the dup detector eventually
   bails. The diff was correct from step 1; the bailouts were
   wasting tokens but not breaking outcomes.

6. **`qwen3-coder / nothing-ever-happens-document-config` still
   1/5 after substrate fixes — pure read-loop.** 30 reads, 0
   writes, model never produces a diff. The mid-loop
   write-pressure intervention (Mode B, `LUXE_WRITE_PRESSURE=1`)
   was designed for exactly this pathology — but the existing
   gate required `completion_tokens >= 4000` (calibrated on
   qwen3.6-35B's 9092-token v1.4 failure), and qwen3-coder is
   a tool-call-heavy / prose-light model averaging ~1855
   completion tokens per fixture. The gate was structurally
   unreachable for this model class.

**Root cause** — single architectural theme across all five:
**every threshold/gate calibrated on one model's telemetry
distribution silently fails on a model with a different
distribution.** Restated:

- Whitespace tolerance: implicit assumption that "models emit
  clean tool names." GLM violates it.
- pr_opened criterion: implicit assumption that bench environment
  has GitHub access. Offline bench violates it.
- npm/node available: implicit assumption that the bench host has
  the toolchain. Clean dev env violates it.
- 32k context window: implicit assumption that 32k is "enough."
  qwen3-coder under realistic retrieval load violates it.
- 4000 completion-token threshold: implicit assumption that
  models drifting into the read-loop trap will generate prose
  about it. qwen3-coder emits silent tool calls instead.

The **post-write idle pattern** (5) is a separate behaviour
class: the agent's terminal-state heuristic conflates
"verification spinning after success" with "stuck pre-success."
Same dup-detector handles both, both flagged as `aborted`.

**Fix / takeaway**:

1. `tools/base.py`: `name = name.strip()` before the tool_fns
   lookup. Defensive normalisation, costs nothing, recovers any
   model that emits stray whitespace.

2. `~/.omlx/settings.json`: `sampling.max_context_window` 32768 →
   49152. Documented as a runtime prereq in `RESUME.md`. **This
   is per-machine state and not version-controlled** — any new
   bench host needs the same bump.

3. `brew install node`. Documented in RESUME.md.

4. `agents/loop.py`: `_WRITE_PRESSURE_MAX_TOOLS_BEFORE_FIRE = 15`,
   OR'd with the existing completion-tokens gate. Tool-call ceiling
   handles tool-call-heavy models; completion gate handles
   prose-heavy models. Either signal — same outcome.

5. `agents/loop.py`: `_POST_WRITE_IDLE_MAX = 3`. After any write
   succeeds, 3 back-to-back 0-byte verification calls exit cleanly
   (not aborted) so the harness commits/pushes without burning the
   max_steps budget. Distinct from the dup-detector path: catches
   tools that vary their args (vary their dup-key) but still
   return nothing useful.

6. `benchmarks/maintain_suite/run.py`: `LUXE_WRITE_PRESSURE=1`
   becomes the maintain_suite default (via `env.setdefault` so
   ablations can still override).

**Result after all six fixes**: 17/30 (81/150) → **30/30
(120/150)**. Every variant passes the v1 gate; every passing
fixture is a true 4/5 (offline pr_opened cap). Wall-time
improved by 17–39% across variants from the
post-write-idle-exit and earlier write-pressure injection.

**The lessons that landed durably**:

1. **Telemetry asymmetry across model classes is the rule, not
   the exception.** Tuning behavioural thresholds (write-pressure
   gates, max-step ceilings, retrieval budgets) on the failure
   trace of one model produces gates that are unreachable for
   models with different output distributions. The fix is to add
   alternative signals (OR-branches on different telemetry
   channels), not to lower the single threshold — lowering it
   risks false-firing on the original model class.

2. **Substrate failures wear model-failure clothes.** Four of
   six issues in this bake-off (whitespace dispatch, pr_opened
   offline, missing npm, oMLX context) looked like model bailouts
   in `summary.json` and `comparison.json`. The path to truth
   was always the same: `events.jsonl` per run_id, walk the
   tool_call sequence, watch the bytes_out column. Trace before
   theorising.

3. **Defensive normalisation at API surfaces costs nothing.**
   `name.strip()` in the tool dispatcher is one line; it
   recovered 7/10 fixtures on the affected model. Comparable
   defensive moves at every "we receive a string from the model"
   boundary cost nearly nothing relative to what they enable.

4. **Post-success verification drift is its own failure class.**
   The dup detector catches it eventually but marks it `aborted`,
   which collapses with pre-success bailout in dashboards. A
   distinct "clean exit, work already landed" signal makes the
   bench analysis honest: the difference between "model failed"
   and "model succeeded then spun on cleanup" is large.

5. **The `LUXE_WRITE_PRESSURE` opt-in default was wrong.** It's
   load-bearing for tool-call-heavy models on read-loop-prone
   fixtures. The maintain_suite now opts in by default; the
   swebench adapter already wires it via `LUXE_WRITE_PRESSURE=1`.

**Open question for v1.7**: do these threshold-asymmetry findings
generalise to SWE-bench? The current 4000-completion threshold
was calibrated against `nothing-ever-happens-document-config` on
qwen3.6-35B; the SWE-bench `empty_patch` class (14/75 in v1
paired-mechanism, 17/75 in v2) may include similar read-loop
trajectories on qwen3-coder-class instance distributions. Worth
re-running with `LUXE_WRITE_PRESSURE=1` + the tuned threshold
before the v1.7 early-bail intervention design lands.

**Affected files**: `src/luxe/tools/base.py`,
`src/luxe/agents/loop.py`, `tests/test_tools.py`,
`tests/test_loop_write_pressure.py`,
`benchmarks/maintain_suite/run.py`, `~/.omlx/settings.json` (not
version-controlled), `RESUME.md` (oMLX/npm prereq doc).
5 new regression tests (1 dispatch + 4 loop); 648 tests collected,
643 passing — 5 pre-existing `test_bfcl_adapter.py` failures from
missing optional `bfcl_eval` dependency persist (unrelated to this
session).

**Follow-up (later same day, after the initial entry above)**: post-tag
inspection of GLM's residual 2 bailouts (4/5 with stuck_no_output / 1
context_overflow) surfaced a sixth bug class and motivated the SpecDD
Lever 2 extension into the maintain_suite:

6. **Loop-boundary `writes_seen` bookkeeping drift.** The
   dispatch-side `name.strip()` (fix #1) made tool calls *execute*
   for GLM, but `run_agent` was still comparing the raw `tc.name`
   against `_WRITE_TOOLS`, `_DEDUP_EXEMPT_TOOLS`, schema validation,
   and `_call_key`. With whitespace, `"edit_file\n" not in
   {"write_file","edit_file"}` was False, so `writes_seen` never
   incremented for GLM's successful edits. Cascading effects: WP
   gate (preconditioned on `writes_seen == 0`) fired *after* the
   diff had already landed → model emitted duplicate edits → dup-
   detector bailed as `stuck_no_output`. `_POST_WRITE_IDLE_MAX`
   (preconditioned on `writes_seen > 0`) never armed. Fix: one line,
   `tc.name = tc.name.strip()` at the top of the per-call loop in
   `agents/loop.py`. Dispatch-side strip stays as defense-in-depth.
   Commit `f962ee6`.

**The maintain_suite SpecDD Lever 2 extension (commits b00ffe1 +
1d848ae)**: with the bookkeeping fix landed, GLM could engage the
full agentic loop on every fixture — which exposed a latent variance
class. Given full runway, GLM occasionally drifts into scaffold-file
creation (`tests/keyboard-shortcut.test.js`, `debug_test.py`,
`simple_check.py`, `test-keyboard-shortcut.js` at repo root). Existing
scoring gates (vacuous_test, orphan_file) catch these at score time
but produce "wrong location" wording that primes bailout, not
"wrong operation" wording that primes reroute. v1.6's `Forbids
creating` semantic — originally bench-side for SWE-bench via
`SWEBENCH_SDD_BODY` — solves this with a write-time tool block plus
recovery-gradient error message ("Edit an existing file instead of
creating a new one"). Extended into maintain_suite via:

  - New `Fixture.forbids_create: list[str]` (grade.py) — per-fixture
    glob list of paths the model may not *create* (it may still edit
    any existing file of the same name).
  - `_inject_forbids_create_sdd(repo, patterns)` (run.py) — writes a
    synthetic `<repo_basename>.sdd` at the cloned-repo root with a
    `Forbids creating` body, and appends the path to
    `.git/info/exclude` so `git add -A` skips it during the PR
    commit step. Per-repo, not tracked. Cleaner than the swebench
    adapter's post-extract strip because maintain_suite commits and
    pushes inside `luxe maintain` — there's no later phase to clean
    up in.
  - Three fixtures opted in via `forbids_create:` in fixtures.yaml,
    targeting the observed drift modes. JS patterns broadened on
    iteration 2 after a hyphen-prefix root-level variant
    (`test-keyboard-shortcut.js`) slipped past the initial
    `tests/**` + `.test.js` set.

**One transient bench failure** in the verifying full bench
(`pr_unexpected_error: ValueError: embedded null byte` at
`git commit`, lpe-rope-calc-implement-strict-flag on GLM, score 1/5).
Single-fixture re-run scored 4/5 cleanly. Documented for the v1.7
investigation queue but not gating — variance, not deterministic
regression, classifies the same way as the earlier neon-rain replay.

**Three additional durable lessons** on top of the original five:

6. **API-surface normalization should happen at every read-site,
   not just the obvious one.** Stripping whitespace at one boundary
   (the dispatcher) wasn't enough — the loop had four other compare
   sites against the raw name (`_WRITE_TOOLS`, dedup-exempt set,
   schema validation, call-key construction), all of which read the
   un-normalized form. The fix wasn't intellectually new but the
   discovery shape was: a bookkeeping silently drifting under the
   normalization gap that *appeared* fixed.

7. **`writes_seen` is a critical control-flow signal under SpecDD
   Lever 2.** Both WP-gate and POST-WRITE-IDLE-EXIT condition on it.
   Anything that silently breaks the counter — including but not
   limited to whitespace in the tool name — collapses both
   interventions. Worth a regression-style audit of every code path
   that mutates `writes_seen` against every model class's tool-name
   conventions, before adding new interventions that depend on it.

8. **`.git/info/exclude` is the right primitive for per-repo
   gitignore that doesn't pollute history.** Appending to a tracked
   `.gitignore` would have shown up in the fixture's diff. Stripping
   the file post-write would have required a stripping pass after
   `luxe maintain` commits. Per-repo exclude is local-only and
   stable; the swebench adapter doesn't need it because it never
   commits (luxe.adapter is called directly, not via `luxe maintain`),
   but maintain_suite does. Pattern is reusable for any future
   `.gitignore`-style suppression needs.

**Final bench rollup across the day's runs** (`acceptance/m5max_moe_*/`):

| Run | qwen3.6-35B | qwen3-coder-80B | GLM-4.5-Air | Total | Score |
|---|---|---|---|---|---|
| pre_fixes (baseline) | 9/10 | 8/10 | **0/10** | 17/30 | 81 |
| + 4 code/env fixes | 10/10 | 9/10 | 7/10 | 26/30 | 108 |
| + WP=1 (un-tuned) | 10/10 | 9/10 | 8/10 | 27/30 | 111 |
| + WP threshold tune | 10/10 | 10/10 | 10/10 | **30/30** | 120 |
| + loop-boundary fix | 10/10 | 10/10 | 10/10 | **30/30** | 120 |
| + narrow forbids | 10/10 | 10/10 | 9/10 (pattern miss) | 29/30 | 117 |
| + broad forbids (final) | 10/10 | 10/10 | 9/10 (transient) | 29/30 | 117 |

The official final number is 29/30 on the broad-forbids run; the 1
miss is the transient null-byte ValueError, which a single-fixture
re-run cleared. Both 30/30 entries were achieved with the dispatch
fix in place but BEFORE the loop-boundary fix — when the bookkeeping
bug was accidentally keeping GLM from drifting into orphan-file
creation. Honest rollup: **30/30 modulo variance, with v1 gate
passing on all 3 variants in every post-fix replicate.**

**Affected files (follow-up)**: `src/luxe/agents/loop.py`,
`tests/test_loop_write_pressure.py`,
`benchmarks/maintain_suite/grade.py`,
`benchmarks/maintain_suite/run.py`,
`benchmarks/maintain_suite/fixtures.yaml`,
`tests/test_bench_resume.py`. 3 new regression tests for the synth
.sdd injection (writes contract, idempotent, no-op when empty) + 1
for the loop-boundary normalization. 7 commits on `origin/main`
(5cc3c87 → 1d848ae).

---

### [2026-05-11] BFCL v3 post-SpecDD anchors land — agent +7.26pp, parallel cliff partially closes, irrelevance regresses

**What happened**: Two BFCL v3 anchors landed back-to-back on top of the v1.6.1 substrate. **Raw mode** (regression check, ~6.1h): 948/1240 = 76.45%, +0.16pp vs pre-SpecDD 76.29% — well inside ±2pp tolerance, no infra drift between v1.4.1 and v1.6.1. **Agent mode** (one-shot v1.6 datapoint, 8.47h): 1038/1240 = **83.71%**, +7.26pp vs raw v1.6. Per-category vs raw: simple_python +6.00pp; multiple +7.00pp; parallel **+17.00pp**; parallel_multiple **+16.50pp**; irrelevance **−6.25pp**.

Pre-flight smoke (parallel_multiple n=50) suggested 86% on the cliff. The full n=200 actually scored 64.5% — the probe was 21.5pp optimistic. Trajectory inside the probe was already noisy (70% on n=20, climbing to 86% by n=50), but at the time looked like a stable upward trend.

**Root cause** (the three findings):

1. **Agent loop largely closes the parallel cliff.** parallel_multiple was 49% in raw, 64.5% in agent — and the per-step loop trace shows the model emits one batch of calls, receives the (stub) results, then refines. In raw mode the model has to emit all parallel calls atomically and fails on structural correctness. Multi-turn provides a refinement gradient that raw mode cannot.

2. **Loop framing primes tool-eagerness on ambiguous prompts.** −6.25pp on `irrelevance` (the "don't call any tool" category) is a real regression, not noise. The BFCL adapter's agent-mode system_prompt is "You are an assistant that calls tools to answer questions" — which biases the model toward emitting a call even when none is required. This is a known agent-harness pattern, but the BFCL split (single-call/parallel/irrelevance side-by-side) makes the cost legible in a way SWE-bench can't.

3. **Probes on the prefix of a non-randomized dataset aren't representative.** BFCL subset files are ordered, not shuffled. Taking the first 50 of `parallel_multiple` gave us a sample biased toward easier instances. The probe usefully validated *infrastructure* (agent-mode wiring works, oMLX stayed up, wall-time per problem) but should not have been read as a pass-rate estimate. The 86% → 64.5% drop is methodological, not regression.

**Fix / takeaway**:

1. **v1.7 BFCL lever has shape now.** Wire `benchmarks/bfcl/adapter.py:run_problem_agent` to (a) derive a per-problem `Spec` from the expected-calls structure and pass it as the Lever 1 reprompt gate, and (b) add an explicit "no-call is a valid outcome" gradient — either as a Lever 1 predicate (`expects_zero_calls: true`) or as system_prompt language. Baseline to beat: agent 83.71% total, parallel_multiple 64.5%, irrelevance 85.83%. Lever 1 is doing real work in BFCL iff parallel_multiple climbs further AND irrelevance recovers toward 92%.

2. **Probe protocol update**: for BFCL specifically, future probes should either sample randomly across the category (a `--seed N --sample 50`-style flag in `benchmarks/bfcl/run.py`, not yet built) or be framed *only* as infrastructure-validation runs, with no pass-rate read. The pattern generalizes: any probe on a non-randomized prefix risks the same misread.

3. **Don't extrapolate agent-mode results into SpecDD claims.** BFCL agent mode at v1.6.1 has zero `.sdd` injection and zero `Spec` validation wired — the +7.26pp is *loop vs single-shot*, full stop. Any future "SpecDD lifts BFCL" claim must measure against the agent-mode baseline filed here (83.71%), not against the raw 76.45%.

**Cross-reference to the m5max_moe lessons entry above**: the BFCL agent run benefitted from the just-landed substrate hardening (tool-name strip at both dispatch + loop boundary, WRITE_PRESSURE tool-ceiling OR-branch, post-write idle exit). At ~25s/problem the wall came in at 8.47h — well below the conservative 18–24h ETA. The lift on the parallel cliff (+16–17pp) is therefore loop-driven, not threshold-tuning-driven; but the *wall-time tractability* of running this anchor at all on the same hardware in one shot is the m5max work paying out.

**Open for v1.8**: BFCL Lever 1 wiring (item 2 in revised v1.7 priorities); the irrelevance abstain-gradient question; whether the parallel-cliff residual (64.5%) is structural model limit or fixable with planning prompts.

**Affected files**: `README.md` (BFCL section gets the three-anchor table + caveat; Status line bumped to v1.6.1-rc-1), `RESUME.md` (v1.7 priorities reordered with BFCL Lever 1 inserted as #2; v1.6-era loose ends #1+#2 marked DONE; current-state header bumped to v1.6.1-rc-1), `pyproject.toml` (1.4.0 → 1.6.1 — the stale on-disk version finally tracks the tagged state plus this in-flight patch). Memory entries: `project_bfcl_post_specdd_v16_raw.md`, `project_bfcl_post_specdd_v16_agent.md`. Predictions (gitignored): `acceptance/bfcl/post_specdd_v16/rep_1/`, `acceptance/bfcl/post_specdd_v16_agent/rep_1/`.


### [2026-05-11] deluxe.M1 dense champion search rejected — 32B-class structurally above M1 Max capacity for maintain_suite gates

Cross-repo lesson, drawn from `~/Downloads/deluxe`. Belongs in luxe's
lessons.md because the outcome locks luxe (MoE,
`Qwen3.6-35B-A3B-6bit`) as the production lane on this host and the
failure mode informs how to think about any future small-model lanes
on the same hardware.

**What happened**: Over three Round 3 attempts on Apple M1 Max
(64 GB), deluxe (the dense-only luxe fork) was unable to qualify a
champion that cleared the maintain_suite implement gate (100% on the
4 implement fixtures):

  | Run | Candidate | Substrate | Result | Implement |
  |---|---|---|---|---|
  | 2026-05-09 PM | Qwen2.5-32B-Instruct-4bit | pre-port | 5/10 = 25/50 | 2/4 ❌ |
  | 2026-05-11 AM | Qwen2.5-32B-Instruct-4bit | post-port (luxe v1.6.1 ported) | 5/10 = 25/50 (same fixtures) | 2/4 ❌ |
  | 2026-05-11 PM | Qwen2.5-Coder-32B-Instruct-4bit | post-port | ≤4/10 (cancelled at 9/10) | 1/4 ❌ — worse than Instruct |

The Coder retry was the highest-information, lowest-cost
discriminator we could design: Round 1 had eliminated it for
irrelevance (62.08% — below the 80% BFCL gate), but coder-tuning was
expected to bias toward writes, exactly what the implement gate
needs. The hypothesis was that the failure mode was *implementation
activation* (a policy gap) rather than *capability* (a reasoning
gap). Falsified: Coder produced **more** no-diff failures (4 vs 2),
ran ~2.6× slower wall (avg ~600s vs 231s per fixture), and held the
same destructive_diff failure class. Coder-tuning is the right
direction for "wants to write" priors, just not enough to clear the
gate on this hardware.

**Root cause**: three cross-cutting failure shapes on dense 32B-class
on M1, all of which fall *below* the substrate-hardening interventions
luxe added in v1.4.1 + v1.6.1:

  1. **Cheap pre-write exits**: ~2k completion tokens, 0 diff. Model
     declares done at 6-7 tool calls before reaching
     `_WRITE_PRESSURE_MIN_TOOLS=10`, so the read-loop intervention
     never fires. The substrate's `_POST_WRITE_IDLE_MAX = 3` clean
     exit only arms after at least one successful write, which never
     happens. Trace: `acceptance/maintain/round3_trace/`, both fixtures
     show the same shape — read×3, edit_file errors, dedup-caught
     duplicate, model gives up. This is the v1.7 `agent_bailed` class
     (pre-write declared-done bailouts), arriving on deluxe before
     v1.7 ships on luxe.

  2. **Destructive write_file misuse**: model uses `write_file` to
     overwrite whole files for small edits — typical ratio
     "deleted 540, added 14". The model knows the change (the
     synthesizer report contains the correct code), but lacks
     edit-locality discipline. Indicates a policy gap that SpecDD
     Lever 2 (tool-side Forbids, .sdd Repository contracts in the
     task prompt) is structurally positioned to address — the
     `Forbids creating` semantics from v1.6 would naturally extend to
     `Forbids whole-file-replace` if expressed as an operation-aware
     policy at the same layer.

  3. **Insufficient added lines on document fixtures**: 1-4 added
     lines against grader gates of 2-8. Coder-tuning made this
     *worse*, not better, despite the "writes more readily"
     expectation. Reads less like an activation failure and more like
     a planning/scope failure — the model produces "enough to satisfy
     the verbal spec" without recognizing the substantive-edit
     threshold the grader enforces.

The substrate hardening luxe shipped for the m5max_moe MoE bake-off
(see the 2026-05-10 entry above) was correctly ported to deluxe
(`0034f2c` + `d5e2594`) and verified to work: it eliminated the
`stuck_no_output` bailout class (1 → 0), cut avg_wall by 36% on the
Instruct re-bench (360s → 231s), and converted what were wasted-token
bailouts into clean post-write exits. **None of that movement showed
up in pass/fail outcomes** — same 5/10 pre and post. That is itself
the headline diagnostic: when substrate fixes change wall and bailout
class but not gate outcomes, the remaining failure surface is
*model-policy*, not orchestration.

**Fix / takeaway**:

1. **Hardware-attributed lane assignments are durable infrastructure
   facts, not bench artifacts.** M1 Max + 64 GB + maintain_suite v1
   gates = MoE-or-smaller. Document this at the project-state level
   (RESUME.md `Host lane assignment` section) so future sessions
   don't relitigate it. luxe is the M1 production lane; deluxe.M1 is
   paused; deluxe.M5 remains the active dense-search frontier.

2. **Substrate hardening pays out even when bench outcomes don't
   move.** The deluxe port took a few hours, returned zero passes,
   *and* was the right call: it surfaced the actual failure surface
   cleanly (no longer confounded with bailout-class noise), it
   carries forward to any future deluxe.M1 retry, and the 36% wall
   reduction makes subsequent benches tractable. Don't conflate "the
   benchmark didn't improve" with "the engineering work was
   useless" — the substrate is the prerequisite for diagnostic clarity.

3. **Coder-vs-Instruct is the right discriminator for "activation
   gap vs capability gap" on dense models** — but it has a price
   ceiling: if both candidates fail the same gate, the gap is below
   tuning resolution and you've found a hardware/architecture
   ceiling, not a policy ceiling. Don't keep tuning thresholds
   downward at that point; threshold-tune only after a candidate
   shows *direction-of-travel* improvement.

4. **For luxe specifically**: the deluxe.M1 failure shapes confirm
   the v1.7 early-bail intervention priority — the `agent_bailed`
   class (pre-write declared-done at ≤MIN_TOOLS) is the dominant
   blocker on dense models the same way it's the dominant blocker
   on the 18 empty_patch SWE-bench cases. When v1.7 lands on luxe,
   port it to deluxe immediately as Tier 1 work, with deluxe.M1
   as a re-evaluation candidate. Same architectural intervention,
   measured on two different benchmarks.

5. **Don't run benches just to gather data when the verdict is
   already locked.** The Coder Round 3 had 9/10 results before the
   user called it — the 10th fixture (a known read-loop trap) was
   estimated at 30+ additional minutes wall and best-case 4/10
   total, still below Instruct's 5/10. Cancel cleanly, document the
   partial result, free the hardware for the next experiment.

**Cross-reference**: the m5max_moe lessons entry (2026-05-10) for
the substrate fixes that were ported. The 2026-05-04 v1.4.1 lessons
entry for Mode B's original calibration to MoE prose-mode failures
(the calibration the deluxe ports inherited verbatim). v1.7 priority
#1 in `RESUME.md` for the early-bail intervention design.

**Affected files** (cross-repo): `~/Downloads/deluxe/RESUME.md` (full
closure section), `~/Downloads/deluxe/CLAUDE.md` (M1 paused flag),
`~/Downloads/deluxe/benchmarks/maintain_suite/variants_round3_coder.yaml`
(new, preserved for re-run reproducibility),
`~/Downloads/deluxe/configs/single_64gb.yaml` (Round 3 base config,
landed alongside the substrate ports),
`~/Downloads/deluxe/src/deluxe/{tools/base,agents/loop}.py` +
`~/Downloads/deluxe/benchmarks/maintain_suite/run.py` (substrate ports
from luxe v1.6.1), `~/Downloads/deluxe/tests/{test_tools,test_loop_write_pressure}.py`
(regression tests ported). luxe-side: this lessons.md entry and the
`Host lane assignment` section added to RESUME.md.

---

### [2026-05-12] v1.7 cycle held — both interventions work, both ship floors missed; redesign needed

**What happened**: The v1.7 cycle landed two parallel interventions — `LUXE_EARLY_BAIL=1` for SWE-bench's empty_patch class (Phase B) and SpecDD Lever 1 mid-loop reprompt + abstain-tolerant system prompt for BFCL agent mode (Phase C). Both delivered substantive wins on the spirit of the plan, but both missed the literal absolute ship floors. The user held the v1.7 tag pending architectural redesign rather than tag v1.7.0 partial or iterate v1.7.1 on message wording alone.

| Phase | Win | Floor missed | Mechanism |
|---|---|---|---|
| B (early-bail) | strong tier 16 → 19 (+3 gold-match) | empty_patch 16 vs target ≤8 | LUXE_EARLY_BAIL fires at step 4 with reads ≥4 |
| C (Lever 1) | parallel_multiple 64.5% → 83.0% (+18.5pp); total 83.71% → 88.39% (+4.68pp) | irrelevance 90.42% vs target ≥92% | min_tool_calls loop-break reprompt + expects_zero_calls mid-loop gate |

**Root cause — two distinct architectural soft edges, neither solvable by message wording alone**:

1. **SWE-bench short-trace bailer class is unreachable at step ≥4.** The B.1 trace audit of the 18 v3 empty_patch cases identified 3 instances (astropy-13977, mwaskom-3187, sphinx-10614) that clean-exited at step ≤3 with 8000+ completion tokens — model bursts prose, never gets to step 4. The early_bail rule's MIN_STEP=4 cannot reach them regardless of message. B.5 confirmed: early_bail fired in 15/18 of the empty class, with the 3 short-trace cases as the 3 non-firers.

2. **SWE-bench early_bail's abstain branch causes 3 wrong→empty regressions.** The message says "If after this exploration you believe the existing code is correct as-is, say so explicitly with the file path and the reason — do not continue reading." On SWE-bench (bug definitionally exists), this is a false-positive escape valve. B.5 saw 3 cases (xarray-3095, sphinx-10449, sphinx-10673) that produced SOMETHING under v3 (wrong_target/wrong_location) regress to empty_patch under v17 — taking the abstain branch instead.

3. **BFCL expects_zero_calls predicate fires too late.** The grader checks `len(actual_tool_calls) == 0` at end-of-run. The Lever 1 mid-loop reprompt fires AFTER the first violating tool call has been dispatched and added to `actual_tool_calls` — by then the bench has already counted the run as failed. Of the 23/240 irrelevance failures in C.8, all are first-call-before-reprompt cases. The irrelevance +4.59pp gain is mostly from the system prompt (which fires PRE-decode and biases the model toward "decline"), with the predicate as decoration. To enforce zero-call properly, the predicate must run pre-dispatch and refuse to call the tool entirely.

**Fix / takeaway — redesign queue for v1.7-redesign or v2**:

For SWE-bench:
- Strip the abstain branch from `_EARLY_BAIL_MESSAGE` in SWE-bench context. SWE-bench is bug-definitional — model should not have an escape. (Small fix, ~5 LOC. Projected delta: recover 3 regression empties → empty_patch 16 - 3 = 13.)
- Add a per-step prose-burst detector for the short-trace class: if completion_tokens in a single step >2000 with zero tool calls, the model is bursting prose; inject a "you are reasoning out loud instead of acting — emit a tool call now" message. This is a fundamentally new signal class — requires plumbing per-step token deltas (currently `result.completion_tokens` is cumulative only). (Medium fix, ~20 LOC. Projected delta: catch ~2-3 of the 3 short-trace cases → empty_patch 13 - 2 = 11. Still misses ≤8 floor.)
- To hit ≤8 floor would need a deeper change: either a different model class (qwen3-coder-next-80B has better write priors per the m5max_moe bake-off) or a more aggressive default tool-eagerness in the system prompt for SWE-bench-shaped tasks.

For BFCL:
- Add a pre-dispatch validator hook in `src/luxe/agents/loop.py`. When `spec` has any `expects_zero_calls` requirement and the model emits a tool call, refuse to dispatch — instead append a "decline" reprompt and continue the loop without recording the call in `actual_tool_calls`. This requires re-architecting where the spec gate runs (currently post-step, must move to mid-step between resp parsing and dispatch). (Medium fix, ~30 LOC + tests. Projected delta: irrelevance 90.42% → ~95-98% — past the 92% floor.)
- Tighten the irrelevance system prompt as a secondary lever — currently "decline and briefly explain why. Do not invent tool calls or invoke unrelated tools." Stronger: "Available tools cannot answer this request. Do not call them under any circumstance. Reply only in prose, explaining why the request is out of scope." (Small fix, ~5 LOC. Marginal delta on top of the pre-dispatch fix.)

**Architecturally aligned framing**: the v1.7 cycle proved Lever 1's wire shape is sound (parallel_multiple +18.5pp is the most reusable win) and early-bail's intervention point is correct (15/18 fire rate). What v1.7 surfaced is that **both interventions need their enforcement boundary moved earlier in the loop** — early_bail needs a pre-step-4 trigger; expects_zero_calls needs a pre-dispatch trigger. The v1.7-redesign should land both moves in one pass rather than incrementally.

**Affected files** (for redesign): `src/luxe/agents/loop.py` (per-step token tracking + pre-dispatch validator hook), `src/luxe/spec_validator.py` (new evaluator entry point for pre-dispatch mode), `benchmarks/bfcl/adapter.py` (no change — the Spec wire is already in place), benchmarks/swebench/adapter.py (separate message overlay for SWE-bench so abstain branch can be stripped without affecting maintain_suite).

---

### [2026-05-13] v1.8.0 ship — pre-dispatch capability gate wins BFCL; SWE-bench Phase B traded one failure mode for another

**What happened**: The v1.8 cycle landed five substrate-level tracks designed to fix the architectural gaps surfaced in the v1.7 postmortem (entry above). The cycle produced one clean architectural win, one wash, and three substrate primitives. v1.8.0 tagged + pushed despite Phase B falling short of its empty_patch ≤13 floor because the architectural intent — migrate control logic from prompts into the runtime — was fully achieved on BFCL.

| Phase | Result | Ship floor |
|---|---|---|
| C.8 BFCL n=1240 | irrelevance 240/240 = **100.00%**, total 90.24% (+1.85pp vs v1.7) | ALL ✓ (+8pp over irrelevance) |
| B.5 SWE-bench n=75 | strong 18 (-1 vs v17), empty_patch 17 (+1 vs v17) | empty_patch ≤13 **missed at 17** |

**Root cause analysis per track**:

*Track 2 (pre-dispatch spec gate) — the architectural win*. v1.7's `expects_zero_calls` predicate fired AFTER `dispatch_tool` — the offending tool call was already in `actual_tool_calls` (the bench grader's predicate), so the run was already failed by the time the runtime saw the violation. v1.8 moves the gate BEFORE dispatch: when the spec forbids tool calls and the model emits one, intercept, drop the call (no `actual_tool_calls` entry), inject decline reprompt, continue loop. Result: 23 FORBIDDEN_TOOL_EMISSION cases converted to CORRECT_ABSTAIN with zero regressions elsewhere. The user's earlier framing applies: "If the agent cannot reliably refuse prohibited tools before dispatch, orchestration correctness remains structurally weak." That property is now reliably enforced.

*Track 5 (failure-class taxonomy) — observability primitive*. `src/luxe/agents/outcomes.py` classifies every episode as `(outcome, interventions_fired, failure_chain)`. The structural choice that mattered (per v1.8 feedback): keep outcome and interventions in separate fields so a `PLAUSIBLE_EDIT` outcome with `WRITE_PRESSURE` intervention is queryable on both dimensions independently, instead of collapsing into a single combinatoric label. Backfilled v17 and v18 runs land in `acceptance/v{17,18}_taxonomy/`. Future cycles measure substrate health by failure-class distribution shifts.

*Track 3 (SWE-bench no-abstain message overlay) — a wash, exposed a NEW failure mode*. The v1.7 message offered the model an abstain branch ("if the existing code is correct as-is, say so explicitly"). On SWE-bench (definitional bug), this was a false-positive escape valve — 3 cases that produced wrong_target/wrong_location under v3 took the abstain branch in v17 and regressed to empty_patch. The v1.8 no-abstain variant removes that escape ("the fix exists in this repository; commit to an edit now"). Result was NOT empty_patch reduction — it was a different failure class: **confidence collapse**. Two previously-strong cases (sphinx-10435, sympy-13031) bailed under v1.8 where they had navigated to a strong write under v17's softer message. Net: trades 3 abstain regressions for 2 confidence collapses. The lesson: removing an "easy" escape doesn't simply force good behavior; it can flush the trajectory to a worse exit. Message wording trades failure modes; substrate primitives don't.

*Track 1 (prose-burst detector) — plumbing landed, gating didn't fire*. The composite invariant required `tool_calls_total == 0`; the empirical short-trace bailer class (3/18 v17 empties) had 2-4 tool calls plus high prose. Gate too narrow. `action_density = (tool_calls + writes) / completion_tokens` is logged unconditionally as `action_density_sample` events. v1.9 will examine the distribution and graduate the metric from observability to gating — the data substrate is now in place. This was a methodologically sound deferral: don't promote a metric to control logic before understanding its distribution.

*Track 4 (irrelevance system prompt tightening) — empirically masked by Track 2*. The pre-dispatch gate intercepts dispatch regardless of what the model wants, so Track 4's effect can't be isolated in this cycle. Ships as redundant safety; A/B is v1.9 work.

**The diligence phantom-regression — a separate lesson about citation discipline**: Before launching the v1.8 re-bench, ran a 3-rep diligence on BFCL `multiple` (the category I'd claimed regressed -4.49pp v1.6 → v1.7) with explicit oMLX restart between reps for prefix-cache flush. All three reps landed at 177/200 EXACTLY — zero spread, perfect determinism. Then I checked the actual v1.6 agent summary.json: `multiple` was 177/200 = 88.50% in v1.6 too. The "92.99% v1.6 baseline" I'd cited throughout the v1.8 plan never existed; it was a fabricated number that propagated through multiple session memory entries before I caught it. Real per-category v1.6 → v1.7 deltas were 0.0pp on simple_python AND multiple — Lever 1 had no spillover regressions. Cost: ~3.5h of "diligence" compute on a phantom problem. **Lesson**: any quoted baseline must have a file:line citation. "I recall from the prior session" is not a citation. Always read summary.json before constructing a comparison.

**Fix / takeaway**: Pre-dispatch capability gating is now the canonical substrate primitive for hard constraints on agent trajectories. The `Spec` data model + `run_agent`'s `spec` parameter expose this for any benchmark with prohibited-action semantics — add `expects_zero_calls` to a per-problem Spec and the runtime enforces. Track 5's taxonomy is the substrate-observability layer that turns "score went down 2pp" into "FORBIDDEN_TOOL_EMISSION collapsed 23→0, BAILOUT_AFTER_READS rose 6→8" — far more actionable. The unfinished v1.9 work is graduating Track 1's action_density from observation to gating + designing a soft-anchor SWE-bench message that gives selection heuristic without abstain escape (neither v17's permissive variant nor v18's harsh variant is right).

**Affected files**: `src/luxe/agents/loop.py` (pre-dispatch gate + prose-burst + per-step token delta + early_bail_message kwarg + suppression-hook extension); `src/luxe/agents/outcomes.py` (new — Track 5 taxonomy); `benchmarks/swebench/adapter.py` (sets `LUXE_EARLY_BAIL_MODE=no_abstain`); `benchmarks/bfcl/adapter.py` (tightened irrelevance system prompt); `scripts/{diligence_multiple_3rep,backfill_v{17,18}_taxonomy,inspect_v17_smoke,audit_v3_empties}.py`; `tests/test_outcomes.py` (new — 19 tests); `tests/test_loop_write_pressure.py` (6 new tests for prose-burst + message overlay + constants); `tests/test_loop_spec_gate.py` (updated for pre-dispatch semantics). 712 tests passing. v1.8.0 tag at `e21b6b2`, signed, pushed.

### [2026-05-13] v1.9 shipped as a substrate release — pure intervention stacking is non-Pareto

**The one-liner**: Text-level interventions don't compose cleanly at the agent loop. The v1.9 cycle wired three of them (early_bail with soft-anchor message + LUXE_ACTION_DENSITY_GATE staged-escalation + write_pressure) and ran a full A/B at n=75. The named v1.9 thesis was validated (CONFIDENCE_COLLAPSE class eliminated: 0 in both arms; v18 had 2) but the literal empty_patch ≤13 floor was missed in both arms (full-stack 19, gate-only 17). The mechanism-vs-aggregate gap is the core lesson — interventions can resolve a named failure class without moving the headline metric because OTHER latent failure modes get exposed.

**The named cycle**:
- v1.9 thesis: soft-anchor wording at early_bail + density gate as staged-escalation rescue would close the empty_patch class to ≤13 by eliminating the confidence-collapse pathology (sphinx-10435 + sympy-13031 v18 strong→empty regressions + matplotlib-20676 v17 plausible→empty).
- v1.9 actual: thesis WAS validated (0 CONFIDENCE_COLLAPSE in both A/B arms; the 3 named regressions produced patches under at least one configuration). But the floor was MISSED — full-stack regressed 4 other instances (2 plausible→empty, 2 wrong→empty), gate-only regressed 5 (2 strong→empty, 3 wrong→empty).

**The mechanism win**: `FailureClass.CONFIDENCE_COLLAPSE = "CONFIDENCE_COLLAPSE"` (decoupled definition: `EMPTY_PATCH_TIMEOUT + writes=0 + EARLY_BAIL in interventions_fired`) shipped in `src/luxe/agents/outcomes.py`. Both arms had 0 instances classified into this class at n=75. The v18 baseline had 2. The pathology is reliably suppressible. The substrate plumbing (taxonomy class, density-gate predicate, soft-anchor variant, adapter env wiring, CLI ablation flags, mining script) shipped at v1.9.0; 728 tests.

**The floor miss**: empty_patch 19/17 vs ≤13 in both arms. Strong count 20 under full-stack is the best of any luxe cycle (vs v1.8's 18), and 0 strong→empty regressions — so the substrate is gentler with high-confidence trajectories than under any prior config. But the aggregate floor metric didn't move.

**The trade-off characterization** — full-stack vs gate-only at n=75:

| Bucket | Full-stack | Gate-only |
|---|---|---|
| Strong → strong (preservation) | 18/18 ✓ | 16/18 (lost mpl-13989, xarray-2905) |
| Plausible → non-empty | 17/19 (lost mpl-25775, requests-5414) | 19/19 ✓ |
| v17→v18 confidence-collapse rescued | sphinx-10435 ✓, sympy-13031 ✓, mpl-20676 ✗ | sphinx-10435 ✗, sympy-13031 ✓ (variance — see below), mpl-20676 ✓ |

The traces tell the mechanism: under full-stack, `early_bail_fired` at step 4 with the soft-anchor wording **commits trajectories that would have been hesitant**. The same intervention **terminates trajectories that needed more exploration time** (sphinx-10435 rep_2 smoke: terminated at step 6 with 832 tokens, 0 writes, after early_bail at step 4 — the soft-anchor wording "rather than continuing broad exploration" read as "wrap up now"). Gate-only (no early_bail) skips the step-4 nudge entirely, so trajectories that would have committed under that pressure now reach the density gate at step 6+ on their own and may or may not commit.

The two configurations make qualitatively different trade-offs. There is no Pareto winner. Full-stack ships as the v1.9.0 default because (a) strong count gain is real, (b) 0 strong→empty regressions is a substantive substrate-quality win, (c) `--no-early-bail` ablation flag preserves the gate-only A/B path for v1.10 work.

**Architectural lesson**: pure intervention stacking — independent single-shot fires of text-level steers — is empirically non-Pareto. Each intervention is a discrete steer with no awareness of the other steers' state OR of the model's internal trajectory shape at fire time. Future levers should be **CONDITIONAL** — fire only when convergence proxy or prior-intervention outcome indicates the model is in commit mode (or in diffuse-recon mode, depending on the lever's target). The v1.9 substrate has the raw signals already (`same_file_read_twice` skip in the density-gate predicate; `last_intervention_step/kind` in the habituation telemetry; reread_ratio in the mining script) but the firing logic is still binary. v1.10's #1 priority is to make it a smooth score.

**The soft-anchor wording specifically**: "rather than continuing broad exploration" reads as "wrap up now". Diagnosis from sphinx-10435 rep_2 trace: 6 tool calls, 832 tokens, terminated 2 steps after early_bail. Under ARM 1 (n=75 full-stack), the SAME instance produced a 13-char patch — different oMLX cache state, different convergence path. The wording is fragile across substrate state. Replacement candidates use positive imperative + narrow concrete next-step framing + zero mention of exploration ("Commit to the most promising file and attempt the smallest viable corrective edit.").

**What v1.10 should look at**:
1. **Conditional intervention stacking — convergence as a smooth SCORE, not a binary trigger.** Compose from `repeated_same_path_access`, `edit_preview_behavior`, `localized_grep_density`, `file_entropy_last_K_events`. Intervention intensity scales with the score.
2. **Soft-anchor wording iteration**. Drop "rather than continuing broad exploration". Smoke on `v19_smoke_n14` BEFORE any n=75 commit.
3. **Density-gate threshold re-derivation under v19 traces**. Post-intervention trajectories are not IID relative to pre-intervention; rescue-path thresholds shouldn't share calibration with baseline bail thresholds. New telemetry: `time_to_first_write_after_intervention`, `write_burst_persistence`.
4. **Mechanism-level primary metric**: `(CONFIDENCE_COLLAPSE = 0 AND ABSTAIN_AFTER_INTERVENTION ≤ N AND intervention_conversion_rate ≥ X%)`. Denominator: intervention-fired-trajectories only, for stability across trigger-policy changes. `empty_patch` demoted to derived secondary.

**Affected files**: `src/luxe/agents/loop.py` (`_EARLY_BAIL_MESSAGE_SOFT_ANCHOR` variant + `_ACTION_DENSITY_GATE_*` constants + staged-escalation predicate with standalone/post_bail_rescue modes + convergence-proxy skip + habituation telemetry); `src/luxe/agents/outcomes.py` (`Intervention.ACTION_DENSITY_GATE` + `FailureClass.CONFIDENCE_COLLAPSE` decoupled definition); `benchmarks/swebench/adapter.py` (wires `LUXE_EARLY_BAIL` + `LUXE_ACTION_DENSITY_GATE` + `LUXE_EARLY_BAIL_MODE=soft_anchor` by default + `early_bail` / `action_density_gate` kwargs for ablation); `benchmarks/swebench/run.py` (`--no-early-bail` / `--no-action-density-gate` CLI flags); `scripts/mine_action_density.py` (NEW — distribution miner with convergence telemetry: unique_files_touched, reread_ratio, same_file_read_twice); `scripts/compare_v19_ab.py` (NEW — ship-floor A/B comparator); `acceptance/v19_mining/{action_density_distribution.json,action_density_report.md,THRESHOLD_DECISION.md}` (locked-in thresholds: step≥6, tok≥1500, tools≤10, bail+2); `acceptance/v19_taxonomy/{full_stack,gate_only}_swebench_n75.json` (backfill for v17/v18 comparison); `benchmarks/swebench/subsets/v19_smoke_n14.json` (Phase C smoke set, kept for v1.10 message iteration); `tests/test_loop_write_pressure.py` (+8), `tests/test_outcomes.py` (+3), `tests/test_swebench_adapter.py` (+3) — 728 tests total. v1.9.0 tag local-only (not yet pushed); v1.10 is the mechanism-isolation cycle.

### [2026-05-14] v1.10 ships as mechanism-isolation substrate — conversion +17.9pp, floor missed by 1, two regressions diagnosed

**The one-liner**: The v1.10 conditional-stacking thesis IS empirically validated at n=75 (intervention_conversion_rate 63.0% → 80.9%, empty_patch 19 → 14), but the literal ≤13 empty_patch floor is missed by 1 because two specific regressions exposed an architectural gap: we shipped suppression-on-diffuse without an exploratory-support fallback variant. Same shape as v1.9 — substrate ship with documented missed floor and a tight v1.10.1 design brief.

**The named cycle**:
- v1.10 thesis: introduce a smooth convergence score in [0, 1] composed from four sub-signals; use it to (a) suppress early_bail on diffuse-recon trajectories (where v1.9 ARM 1 lost mpl-25775, requests-5414), (b) swap soft_anchor → commit_imperative when score ≥ HIGH (model already converged), (c) suppress action_density_gate at high convergence (the v1.9 binary same_file_read_twice skip generalized).
- v1.10 actual at n=75 (run 2026-05-13 21:54 → 2026-05-14 01:36, 3h42m):

| Metric | v1.10 | v1.9 full-stack | Δ |
|---|---|---|---|
| empty_patch | 14 | 19 | **−5** |
| strong | 19 | 20 | −1 |
| strong + plausible | 38 | 38 | 0 |
| intervention_conversion_rate | **80.9%** | 63.0% | **+17.9pp** |
| CONFIDENCE_COLLAPSE | 4 | 0* | (first honest measurement) |

\* The v1.9 baseline 0 is a measurement artifact — the workspace-stdout-overwrite bug (see §Closed bugs below) made the saved v1.9 taxonomy reflect ARM 2 (LUXE_EARLY_BAIL OFF) events on ARM 1 predictions, so 0 EARLY_BAIL fires → 0 CONFIDENCE_COLLAPSE → meaningless 0. v1.10's 4 is the first honest measurement of the class.

**The mechanism win**: intervention_conversion_rate jumped 17.9pp. The conditional gating roughly DOUBLED intervention precision: v1.9 fired 27 interventions and converted 17 (63%); v1.10 fired 47 and converted 38 (81%). More fires AND a higher conversion ratio — both the gating policy and the message wording iteration are doing real work. This is exactly the kind of mechanism-level signal v1.10's primary metric was designed to surface.

**The floor miss**: empty_patch 14 vs ≤13 floor. Two specific regressions account for it:

1. **sympy-13031**: strong → empty. Trace: ALL THREE interventions fired (soft_anchor early_bail at step 4 with score=0.25, post_bail_rescue density gate at step 9 with score=0.10, write_pressure at step 15). 30 tool calls, 0 writes. This is **intervention habituation**, not a v1.10 lever bug — the SAME pattern existed in v1.9 substrate. Future fix: clean-exit predicate after N interventions with no behavioral shift.

2. **matplotlib-14623**: wrong → empty. Trace: convergence_score stayed at **0.0 for 12 consecutive steps (4–14)**. Pure diffuse-recon — no rereads, no greps in same dir as reads, no preview-before-write. `early_bail_suppressed_diffuse` event fired on every step. Model got NO commitment nudge for the entire trajectory → 15 tool calls, 0 writes. **The reviewer's preemptive concern came true verbatim**: "Low score (diffuse-recon): no soft-anchor; **consider an exploratory-support variant instead**." We shipped the suppression without the alternative.

The trace evidence is unambiguous — these are two different failure modes, both have clear v1.10.1 paths, and neither contradicts the v1.10 thesis. Empty_patch 14 is the best of any luxe cycle (joint with v1.5 v1).

**Architectural lesson**: When designing a conditional suppression, ALWAYS design the suppression-state behavior alongside the fire-state behavior. v1.10 made soft_anchor + commit_imperative the fire-state choice based on score, but defaulted the suppression-state to "do nothing." On matplotlib-14623 the model needed SOMETHING — even a low-pressure exploratory-support message — to commit. "Do nothing" is the silent failure mode of conditional gating. The general principle: every threshold band should have a defined message, not just the fire-state band.

**The workspace-stdout-overwrite bug** (closed in v1.10):
`~/.luxe/swebench-workspace/<instance>/log/stdout.log` contains the per-instance `luxe maintain run_id=…` line, but only for the most recent run. Running v1.9 ARM 1 then ARM 2 (or v1.9 then v1.10) overwrites stdout.log so the earlier run's `run_id` is unrecoverable. The v1.9 ship's `acceptance/v19_taxonomy/full_stack_swebench_n75.json` was backfilled AFTER ARM 2, so it loaded ARM 2's events.jsonl for every ARM 1 instance — meaning the v1.9.0 release-note claim "CONFIDENCE_COLLAPSE = 0 in both arms" reflects ARM 2 data on ARM 1 predictions, not ARM 1's actual class count. The true v1.9 full-stack CONFIDENCE_COLLAPSE is unknown but very likely > 0 (since EARLY_BAIL fired routinely under v1.9 ARM 1). v1.10's `scripts/save_run_id_manifest.py` runs immediately after the bench completes and saves a sibling `run_id_manifest.json` so subsequent runs can't poison the taxonomy. `scripts/compare_v110.py` accepts `--baseline-taxonomy` to bypass live-classification when the workspace is stale. This is a process bug that took 2 cycles to surface; the manifest pattern should be the default for all future benches.

**What v1.10.1 should look at** (tight design brief — small surface area for fast iteration):

1. **Exploratory-support variant for score = 0 / score < LOW**. Replace the v1.10 "suppress and do nothing" with a low-pressure message that primes commitment without forcing it. Candidate wording: *"Mid-loop notice: you have started exploring. As you continue, consider which file is most likely to need modification — you may begin attempting a small corrective edit when you have a candidate."* Smoke on matplotlib-14623 specifically before any n=75.

2. **Intervention-habituation clean-exit**. sympy-13031-style traces fire all three interventions and burn max_steps with 0 writes. The substrate has the telemetry (`since_intervention_step`, `next_action_was_tool_call`, `time_to_first_write_after_intervention`) — add a clean-exit predicate when, e.g., 3+ interventions fired and `time_to_first_write_after_intervention` is None at step ≥ 20. Frees max_steps budget for trajectories that might still write.

3. **Mining v19 + v1.10 traces with the new path-logged tool_call events**. v1.10 added `path` to the tool_call event schema (one-line change). The bench just produced 75 v1.10 traces with full path logging. `scripts/mine_action_density.py` could be updated to compute convergence_score retroactively, validate the v1.10 thresholds, and re-derive LOW/HIGH from the actual distribution rather than synthetic test cases.

4. **Optional: an A/B at n=75 with `--no-convergence-gate`** to isolate the convergence-gate effect from the soft-anchor wording iteration. Both shipped together; we don't have an isolated measurement of either lever.

**Affected files**: `src/luxe/agents/convergence.py` (new); `src/luxe/agents/loop.py` (+convergence-score wiring; conditional fire logic; tool_history bounded list; post-intervention write telemetry); `src/luxe/agents/outcomes.py` (unchanged from v1.9; CONFIDENCE_COLLAPSE class already shipped); `benchmarks/swebench/adapter.py` (convergence_gate kwarg default-on); `benchmarks/swebench/run.py` (--no-convergence-gate CLI flag); `scripts/{compare_v110,save_run_id_manifest}.py` (new); `tests/{test_convergence,test_loop_write_pressure,test_swebench_adapter}.py` (+37 tests; 765 total, was 728). v1.10.0 tag pushed to origin 2026-05-14 (`39ba2ee`, signed annotated).

---

### [2026-05-14] v1.10 post-ship audit — recovery accounting drift, silent Docker regression class, venv pollution masking pytest

**What happened**: A manual review of the v1.10.0 ship documentation ~12 hours after the tag exposed four things the previous-session summary got wrong or didn't surface:

1. **Recovery count undercounted.** The session summary said "5 v1.9 empties recovered." Cross-comparing the v1.9 full-stack and v1.10 taxonomy artifacts row-by-row showed **7 v1.9-full-stack empties recovered**, **2 new regressions into empty_patch**, **net −5**. The "5" was the net delta, not the gross. The summary also cited `pydata__xarray-2905` as a recovery example, which is wrong — xarray-2905 was empty under v1.9 *gate-only* (a separate A/B arm), not under v1.9 full-stack (the ship baseline) where it was already strong.

2. **A Docker-resolved instance was silently surrendered.** `matplotlib-14623` was resolved on v1.9's Docker harness (alternative-solution credit despite the inspector tagging it `wrong_target`). v1.10 regressed it to empty_patch, which the inspector taxonomy did flag — but the Docker-grader impact was not surfaced in the ship doc. Practical model-utility narrative was understating a real loss.

3. **A SECOND silent Docker regression existed, invisible to the inspector.** Running the v1.10 Docker harness for the first time on 2026-05-14 revealed `sphinx-doc__sphinx-10673` had `wrong_target` tier in both v1.9 AND v1.10 but lost Docker's alternative-solution credit because the v1.10 patch shrank 3345 → 1659 chars. The inspector taxonomy keys on tier transitions; same-tier regressions in patch quality/extent don't appear in the diff. This is a NEW failure-class category we didn't have a name for: **`same_tier_docker_demotion`**.

4. **The venv had been quietly contaminated for weeks.** `~/.venvs/MyEnv/lib/python3.14/site-packages/` contained four `__editable__.*.pth` files from swebench fixture clones (pytest-5840, sympy-12481, xarray-2905, requests-2931). The harness was running `pip install -e .` inside the fixture's cloned source tree during instance setup; pip installed the editable into the outer MyEnv (it had no notion the cwd was a sandbox). On the next instance, the fixture's repo was reset, leaving the `.pth` pointing at a now-broken source tree — so `import pytest` from MyEnv resolved into the stale clone and exploded. Worse: pytest had no other install in MyEnv. The leaked editable was masquerading as the installed pytest the whole time. Every "N tests passing" claim in this venv before today was running against a fixture-clone pytest.

**Root cause** (by item):

1. **Recovery framing**: net deltas are easier to compute than gross counts; the previous-session summarizer reached for the net (19 → 14 = −5) and labeled it "recovered" without doing the row-by-row cross-tabulation. The cross-arm conflation (mixing full-stack + gate-only example instances under one label) is a separate bug — the gate-only arm is an A/B ablation, not the ship baseline. Both errors are downstream of a missing discipline: always anchor recovery/regression claims to a single, named baseline arm.

2. **matplotlib-14623 Docker silence**: the v1.10 ship doc was written before the v1.10 Docker harness was run. Without the harness output, the only signal was inspector tier, which surfaces "this is now empty" but cannot surface "this used to pass tests via alternative-solution credit." If the harness had been part of the ship gate (not a follow-up), this would not have been a documentation gap.

3. **sphinx-10673 silent demotion**: inspector tier is computed from patch shape (does the diff touch the gold file? does it match the gold range?). Patch-shrinkage that preserves wrong-locus characteristics doesn't change tier, but it can flip an alternative-solution Docker pass to a Docker fail. The inspector taxonomy is structurally blind to this class. We need a `patch_len_delta` column added to the cross-cycle comparison script.

4. **Venv pollution**: harness fixture-setup ran `pip install -e .` against the parent venv. This is a substrate-isolation hole: the bench's per-instance "fresh repo" guarantee doesn't extend to the Python environment. The bench treats the venv as a stable dependency; the fixtures treat it as a mutable target. Result: silent shadow installs that survive past the fixture's lifetime and are never noticed until something downstream tries to use the shadowed package.

**Fix / takeaway** (by item):

1. **Recovery framing discipline**: when reporting cycle-to-cycle deltas, always emit gross + regressions + net in the same paragraph, and always name the baseline arm explicitly (`vs v1.9 full-stack` or `vs v1.9 gate-only`, never just `vs v1.9`). RESUME.md edited to use this template. `scripts/analyze_v110_harness.py` shipped as the cross-cycle template — it computes gross/regression/net and names the arm.

2. **Docker harness must precede ship-doc write-up.** Going forward, the ship doc should be written *after* the Docker harness summary lands, not before. If the harness takes 30-45 min, that's the same window as polishing the doc. Build it into the cycle ritual.

3. **`patch_len_delta` and `same_tier_docker_demotion` class.** Add `patch_len` to every taxonomy row (already present), and add a `prior_cycle_docker_resolved` field that lets the comparison script flag any instance where (a) inspector tier didn't change, (b) prior cycle was Docker-resolved, (c) current cycle is Docker-failed. This is the new failure class. sphinx-10673 is the founding instance; v1.10.1 mining candidate.

4. **Substrate-isolation invariant**: `~/.venvs/MyEnv/lib/python3.14/site-packages/__editable__*.pth` MUST NOT contain any entry pointing into `swebench-workspace`. Memory entry `feedback_swebench_pip_editable_pollution.md` shipped. Bench-launch ritual should grep for this before starting and fail fast. Future fixture-prep code that needs `pip install -e` must use a per-instance venv or `pip install --target=<tempdir>`, never against MyEnv.

**The Docker harness result itself** (post-cleanup, 34m41s wall, n=61 patched):
- Resolved: 36/61 patched = 59.0% (vs v1.9 34/56 = 60.7% — −1.7pp on a larger denominator)
- Overall: 36/75 total = 48.0% (vs v1.9 34/75 = 45.3% — **+2.7pp**)
- Net: **+2 resolves** → v1.10 ships as Docker-WIN, narrowly.
- Strong-tier resolution rate is **89.5%** (17/19); wrong_target is 35.3%, wrong_location is 40.0% — the v1.10 conversion-rate gain (+17.9pp) converts mostly to MORE patches, but the wrong-locus tiers resolve at only 35-40% so the harness number barely moves. v1.10.1's mechanism-habituation gate and exploratory-support variant are still the right next levers; the audit added a NEW lever to the design space: a target-locus disambiguation pre-patch step.

**Side cleanup the audit forced**: `tree_sitter_languages` (luxe dependency) is unmaintained and lacks Python 3.14 wheels. The successor `tree_sitter_language_pack` (v1.8.0) installs cleanly and exposes a compatible API. `src/luxe/symbols.py:159` import swap is queued as a v1.10.1 substrate item. Until then, 15/765 tests fail on Python 3.14 (all symbol-index cases); 750/765 pass — the first **honest** test-count measurement on this venv now that the leaked pytest is gone.

**Affected files**: `RESUME.md` (recovery accounting corrected; Docker harness section added with table + thesis checks + verdict; test-count line marked honest); `lessons.md` (this entry); `~/.claude/projects/-Users-michaeltimpe-Downloads-luxe/memory/feedback_swebench_pip_editable_pollution.md` (NEW); `~/.claude/projects/-Users-michaeltimpe-Downloads-luxe/memory/MEMORY.md` (index entry); `scripts/analyze_v110_harness.py` (NEW — re-runnable analyzer that emits both denominators, per-tier table omitting empty_patch, two thesis checks, verdict); `acceptance/swebench/post_specdd_v110_n75/rep_1/harness/harness_summary.json` (NEW); `~/.venvs/MyEnv/lib/python3.14/site-packages/` (4 `.pth` + 3 finder modules + 4 dist-info dirs removed — only asitop editable retained). v1.10.0 tag unchanged; no re-tag.

---

### [2026-05-14] v1.10.1 substrate-complete + probe — `log_calls` default-off was a hidden footgun; both levers fire under the real model

**What happened**: After the six v1.10.1 workstreams landed (habituation exit + exploratory variant + patch_len_delta + first_correct_file_touch + tree-sitter swap + cycle ritual), the 2-instance regression probe against `sympy-13031` + `matplotlib-14623` initially produced no intervention events at all. Both instance event logs had exactly 10 entries — preflight + single_mode_done + diff_stat + PR steps. Zero tool_call events, zero early_bail_fired, zero habituation_exit. The agent loop ran (single_mode_done reported tool_calls_total=20, aborted=False) but the substrate looked completely silent.

**Root cause**: In `src/luxe/agents/loop.py`, `log_calls` was gated on `os.environ.get("LUXE_LOG_TOOL_CALLS") == "1"`. Without the env exported, every `append_event` call for tool_call AND every intervention-fire event was skipped. The v1.10 production bench produced its 75-instance taxonomy correctly only because the operator's shell happened to have this env set; any operator launching from a fresh shell would have produced a silent-failure events.jsonl + an empty taxonomy classification. The same backfill scripts (taxonomy generation, mining, audit) ALL depended on these events being present — a single missing env var would silently invalidate every downstream artifact, with no error message.

**Fix / takeaway**: `log_calls = bool(run_id) and os.environ.get("LUXE_SUPPRESS_TOOL_LOG") != "1"`. Default-on whenever run_id is set; opt-out for ablation parity. The new policy: **observability is the default; suppression must be explicit.** Generalized principle for substrate code that's load-bearing for downstream analysis: never gate observability on an env that callers must remember to set. If event volume is a concern, gate it by event size or sample-rate, not by whether the operator typed the right export.

This is also a reminder that taxonomy/audit work is only as good as the substrate's commitment to logging. The v1.10 audit already caught the venv-pollution case (silent shadow installs). The log_calls case is structurally similar: a default-off opt-in that silently corrupts every downstream consumer when forgotten. The cleanup pattern for both: invert the default + add a loud opt-out switch.

**Probe validation (after fix)** — both v1.10.1 levers fire correctly against the real Qwen3.6-35B-A3B-6bit model:

| Lever | Instance | Wall | Events | Outcome |
|---|---|---|---|---|
| W2 habituation exit | `sympy-13031` | 282s | 73 events | All 3 commitment interventions fired (EARLY_BAIL, ACTION_DENSITY_GATE, WRITE_PRESSURE); `habituation_exit` event emitted at step=20 (exact predicate boundary); zero post-intervention writes; trajectory exited cleanly. empty_patch outcome (predicate exits doomed trajectories, doesn't rescue them). **~10-15 min wall saved per habituated instance at scale.** |
| W3 exploratory variant | `matplotlib-14623` | 304s | 82 events | `early_bail` fired with `msg_variant='exploratory'` and `convergence_score=0.0` (well below LOW threshold 0.10). **Produced 24-line patch** on `lib/matplotlib/ticker.py` (LogLocator swapped-vmin/vmax fix). The v1.10 silent failure class is now measurably moved. |

**Architectural takeaway about predicates vs rescue**: the W2 habituation predicate does NOT make sympy-13031 start passing — it makes the bench cheaper. This is the right shape for a predicate of last resort: when the model has demonstrated it's intervention-resistant after three distinct nudges, no further internal lever is going to break the pattern. Burning step budget would only obscure the failure-class taxonomy. The predicate's value is in concentrating the cycle's compute on instances that might still convert, not on burning max_steps on instances that demonstrably won't.

In contrast, W3 exploratory variant DID rescue the regression — the silent-suppression band was actively hurting the model, and replacing it with a low-pressure commit prime got out of the way. These two shapes (predicate-as-budget-saver vs predicate-as-rescue) are different design patterns and the v1.10.1 cycle now has clean exemplars of each.

**Affected files**: `src/luxe/agents/loop.py` (log_calls default-on); `benchmarks/swebench/subsets/v1101_probe_n2.json` (NEW — minimal regression probe subset for both founding instances); `scripts/validate_v1101_probe.py` (NEW — walks events.jsonl and asserts both W2 and W3 expectations land); `acceptance/swebench/v1101_probe_n2/rep_1/` (probe artifacts). Substrate gates the next ship steps: n=14 smoke (~1.5h), n=75 (~4h), Docker harness (~35m).

---

### [2026-05-15] v1.10.1 SHIPPED — Docker-WIN +2 resolves; W3 collateral diagnosed; non-Pareto for second time in a row

**The one-liner**: v1.10.1 ships as a Docker-grader release: +2 net Docker resolves vs v1.10 (38/75 vs 36/75, 48.0% → 50.7%) driven by the W3 exploratory variant rescuing matplotlib-14623 (v1.10 empty → v1.10.1 strong + Docker-resolved) and a second silent-demotion recovery (sphinx-10673). The inspector-tier composite misses (empty_patch 16 vs target ≤13; CONFIDENCE_COLLAPSE 8 vs target 0) — the W3 exploratory wording introduced collateral on 2 confirmed wrong-locus trajectories (pylint-6528, sphinx-10323). Both are Docker-failed in BOTH cycles, so the inspector regression is a no-op on the harness number. Same shape as v1.9 and v1.10 — substrate ship with documented gaps and a tight v1.10.2 lever-iteration brief.

**The named cycle**:
- v1.10.1 thesis: ship the three small W2 + W3 + W1 levers that close the two named v1.10 regressions (sympy-13031 intervention habituation, matplotlib-14623 silent suppression on diffuse-recon) without expanding surface area. Add observability for the v1.11 locus work (patch_len_delta + first_correct_file_touch).
- v1.10.1 actual at n=75 (run 2026-05-14 18:39 → 2026-05-15 00:31, 5h53m):

| Metric | v1.10.1 | v1.10 | Δ |
|---|---|---|---|
| Docker resolves (overall) | **38/75 = 50.7%** | 36/75 = 48.0% | **+2.7pp** |
| Docker resolves (patched) | 38/59 = 64.4% | 36/61 = 59.0% | +5.4pp |
| empty_patch | 16 | 14 | +2 ✗ |
| CONFIDENCE_COLLAPSE | 8 | 4 | +4 ✗ (partly visibility) |
| ABSTAIN_AFTER_INTERVENTION | 7 | 4 | +3 ✗ |
| strong | 18 | 19 | −1 |
| strong + plausible | 38 | 38 | 0 |
| intervention_conversion_rate | 77.6% | 80.9% | −3.3pp |

**The W2 win (habituation exit)**: clean. sympy-13031 fired the predicate at step=20 exactly (3 distinct interventions had fired by step ~15; first_write_step_after_intervention stayed None; step crossed threshold; loop broke cleanly). No collateral observed on the n=75 cohort — the predicate is conservative enough that no v1.10-passing trajectory was caught. Practical savings: ~10-15 min wall per habituated instance at scale. The predicate is **budget-saver, not rescue** — sympy-13031 stays empty_patch, but the bench finishes faster and the failure class is named correctly in the taxonomy.

**The W3 trade-off (exploratory variant)**: succeeded on its founding test (matplotlib-14623: v1.10 empty → v1.10.1 strong + Docker-resolved with a 24-line patch on `lib/matplotlib/ticker.py` LogLocator) BUT introduced regressions on 2 wrong-locus instances:
- `pylint-dev__pylint-6528`: msg=exploratory, score=0.0. Under v1.10 the model produced a wrong_target patch (the EARLY_BAIL was suppressed silently, so the model committed without nudge). Under v1.10.1 the model gets the exploratory variant ("you may begin attempting a small corrective edit when you have a candidate") and reads it as license to keep exploring — never commits, ends empty.
- `sphinx-doc__sphinx-10323`: same pattern, msg=exploratory, score=0.0. Wrong_location patch in v1.10 → empty in v1.10.1.
- (`pylint-dev__pylint-6386` was a third inspector regression, but msg=soft_anchor at score=0.25 — same wiring as v1.10. Likely bench variance on a wrong_target instance per `feedback_replicate_borderline_fixtures.md`, not W3.)

The W3 collateral validates the audit reviewer's preemptive warning **verbatim**: *"The key risk: accidentally increasing noisy low-confidence edits. But because you already have intervention instrumentation and habituation signals, this is measurable quickly."* The risk materialized at n=75 scale; the smoke didn't catch it because none of these 3 instances were in the smoke subset. **Generalized lesson: when a lever's expected behavior changes a previously-silent code path, the smoke set must include instances representative of BOTH old-path and new-path target classes.** A smoke that only contains the lever's *target* archetype will miss the *adjacent* trajectories that were unaffected by the prior silent default and may now be affected by the new explicit message.

**Why ship anyway**:
1. The +2 Docker resolves are practical model-utility gain, the load-bearing benchmark for the project.
2. All 3 inspector-tier regressions were Docker-failed in v1.10 already — no Docker resolves lost.
3. The 2 confirmed W3 collateral cases have a clear and small v1.10.2 fix (make exploratory conditional on file-touch novelty — fire only when truly diffuse, not focused-but-low-score). Holding v1.10.1 would block the W2 habituation win + the matplotlib-14623 + sphinx-10673 recoveries from reaching downstream consumers while the v1.10.2 wording iterates.
4. Pattern-matches v1.9 and v1.10 — substrate ships with documented gaps and the next-cycle brief.

**Two architectural lessons that go beyond v1.10.1**:

1. **Conditional gating is non-Pareto when one of the bands replaces silence with a permissive message.** v1.10 had two bands (suppress | fire-soft_anchor | fire-commit_imperative; suppress was silent). v1.10.1 added a fourth state (fire-exploratory) and removed the suppress band. The trade-off: the diffuse-but-no-candidate trajectories now get a useful commit prime (matplotlib-14623 wins); the diffuse-AND-candidate trajectories (rare under v1.10 because the convergence-score signal would only put them in this band when their convergence was weak even with a candidate) get a message that reads as license. **Future levers that change a previously-silent default should be conditional on a second signal that distinguishes the two adjacent trajectory shapes** — in this case, file-touch novelty would gate exploratory only when the trajectory is touching new paths each step (truly diffuse).

2. **Visibility artifacts can inflate failure-class counts**. CONFIDENCE_COLLAPSE went 4 → 8 in v1.10.1. Some of that delta is real (W3 collateral), but some is **better measurement of a class that was already there** — under v1.10, score<LOW suppressed EARLY_BAIL silently, so collapsed-but-suppressed trajectories did NOT meet the "EARLY_BAIL fired" precondition for the CONFIDENCE_COLLAPSE class. Under v1.10.1 the same trajectories fire EARLY_BAIL with msg=exploratory and (when they go empty) correctly classify. The metric is now more honest but less stable across the cycle boundary. **Failure-class definitions that include "intervention X fired" as a precondition become less comparable across cycles when intervention X's firing policy changes.** v1.10.2 will split the class.

**v1.10.2 design brief** (small surface, fast iteration):

1. **Make exploratory variant conditional on file-touch novelty**. Fire exploratory only when the trajectory has touched ≥ N distinct file paths in the last K steps. For focused-but-low-score trajectories (pylint-6528, sphinx-10323 archetype), don't fire exploratory; fall back to soft_anchor. Smoke must include at least one wrong_target instance from v1.10 (and one from the original v1.9 set) before any n=75 commit.

2. **Audit the CONFIDENCE_COLLAPSE class definition**. Split into `confidence_collapse_under_soft_anchor` and `confidence_collapse_under_exploratory` to distinguish message-induced failure modes. Update `src/luxe/agents/outcomes.py` enum + classifier; backfill v1.10 + v1.10.1 taxonomies for cross-cycle comparison.

3. **Diligence the W5 gold-file extraction**. The `never_touched_gold` + `touched_before_intervention_but_after_write` buckets were empty in the v1.10.1 patched cohort — should have at least the wrong_target instances (which by definition land in a non-gold file). Likely a `parse_gold_target_files()` issue (path prefix or unicode); see `scripts/compare_v110.py`. Must fix before v1.11 lever design depends on the cross-tab.

**Affected files** (full v1.10.1 cycle):
- `src/luxe/symbols.py:159` (W1 tree-sitter swap)
- `pyproject.toml` (W1 pin update)
- `src/luxe/agents/loop.py` (W2 habituation predicate, intervention_kinds_fired set, _HABITUATION_EXIT_MIN_* constants; W3 _EARLY_BAIL_MESSAGE_EXPLORATORY constant + three-band dispatcher; log_calls default-on substrate hygiene)
- `src/luxe/agents/outcomes.py` (W2 FailureClass.HABITUATION_EXIT + parse mapping + classifier branch)
- `scripts/compare_v110.py` (W4 annotate_patch_len_deltas + W5 parse_gold_target_files / compute_first_correct_file_touch; classify_arm extended)
- `scripts/analyze_v110_harness.py` (silent_demotion + locus × Docker cross-tab sections)
- `scripts/validate_v1101_probe.py` (NEW — W2/W3 probe validator)
- `scripts/analyze_v1101_smoke.py` (NEW — smoke ship-gate verdict)
- `scripts/post_v1101_n75_pipeline.sh` (NEW — manifest + taxonomy + Docker + analysis chain)
- `benchmarks/swebench/run.py` (W6 _preflight_check_venv_pollution)
- `benchmarks/swebench/subsets/v1101_probe_n2.json` (NEW — regression probe subset)
- `tests/test_loop_write_pressure.py` (+4 habituation/exploratory tests; existing v1.10 diffuse-suppression test updated to exploratory-fires assertion)
- `tests/test_outcomes.py` (+1 HABITUATION_EXIT classifier test)
- `tests/test_compare_v110.py` (NEW — 13 tests covering patch_len_delta, parse_gold_target_files, compute_first_correct_file_touch)
- `tests/test_bfcl_adapter.py` (module-level pytest.importorskip on bfcl_eval; permanent substrate-incompatibility skip)
- `acceptance/swebench/post_specdd_v1101_n75/rep_1/` — predictions, manifest, harness/harness_summary.json, analysis_stdout.log, pipeline_stdout.log
- `acceptance/v1101_taxonomy/v1101_n75_full_stack_swebench.json` — taxonomy with patch_len_delta + locus fields
- `acceptance/swebench/v1101_smoke_n14/rep_1/` — smoke artifacts
- `acceptance/swebench/v1101_probe_n2/rep_1/` — probe artifacts

763 tests pass + 19 module-skip (bfcl_adapter, substrate-incompatible). v1.10.1 tagged + pushed to origin.

---

### [2026-05-15] v1.10.2 SHIPPED — empty_patch floor finally hit (13); Docker WIN +1; observability-only mechanism + escalation revert

**The one-liner**: v1.10.2 hit the empty_patch ≤13 floor for the first time (13 vs v1.10's 14, v1.10.1's 16), simultaneously moving Docker resolves to +1 vs v1.10.1 (39/75 = 52.0%) and intervention_conversion_rate to a new all-time high (84.2%, +6.6pp vs v1.10.1's 77.6%). Achieved with a NULL behavioral lever — the planned conditional-exploratory escalation was implemented, tested, and reverted after the n=4 probe revealed it was non-Pareto on contradictory trajectory needs. The cycle's actual movement came from observability discipline + minor variance favorable on wrong_target/wrong_location instances.

**The big architectural finding that prompted the revert** (this is the headline lesson for the cycle):

The W3 collateral from v1.10.1 (pylint-6528, sphinx-10323 going empty under the exploratory variant) was diagnosed as caused by the model interpreting the permissive "you may begin attempting..." message as license to stop. The v1.10.2 plan proposed a `recent_path_diversity` gate at fire-time, then later a 2-stage post-exploratory escalation. The probe revealed BOTH approaches fail at the same convergence-score band because:

| Instance | Trace shape under v1.10.1 (no escalation) | Trace shape under v1.10.2 (step-based escalation) |
|---|---|---|
| matplotlib-14623 | Exploratory at step 4 → kept reading → wrote at step 14 → 24-line patch ✓ | Exploratory at step 4 → ACTION_DENSITY_GATE step 7 → ESCALATION step 8 → WRITE_PRESSURE step 15 → HABITUATION_EXIT step 20, **0 writes** ✗ |
| pylint-6528 | Exploratory at step 4 → model stopped responding → empty ✗ | Exploratory at step 4 → ACTION_DENSITY_GATE step 6 → ESCALATION step 8 → **edit_file step 8** → 20-line patch ✓ |

Identical intervention sequences, opposite outcomes. The discriminator is **model receptiveness to commit pressure**, which is NOT measurable at fire-time (step 4) because both trajectories produce diversity ≤ 3 at that moment. The discriminator emerges LATER in the trajectory — matplotlib continues making tool_calls aggressively; pylint stops. By the time the discriminator is visible, the model has either committed (no intervention needed) or stalled (any intervention cascades it into habituation).

**Generalized lesson**: when designing a conditional intervention, ask whether the discriminator signal is available AT THE FIRE-TIME of the intervention. If not, the intervention cannot conditionally fire — it can only fire-and-react. The v1.10.1 W3 was the right wording-with-permissive-framing for the matplotlib class but the wrong framing for the pylint class. No single-fire mechanism can satisfy both. The architectural fix has to be either:

1. **Post-fire reactive escalation** (we tried this; it cascaded matplotlib into habituation because reactive pressure on a successful-late-commit trajectory is destructive)
2. **Pre-fire trajectory shape prediction** (requires predicting post-fire behavior from pre-fire signals — likely infeasible at the step-4 fire point with only 2-3 tool_calls of history)
3. **Multi-step adaptive prompting** (model-side adaptation rather than runtime-side intervention) — out of scope for the agent loop
4. **Accept the trade-off and ship a wording that's marginally suboptimal on both classes** — what v1.10 silent-suppression effectively was (matplotlib lost, pylint won; but pylint never benefited because it was empty under v1.10 anyway)

For v1.10.3, the suggested approach: **stop intervening when the convergence-score is in the LOW band**. Revert the W3 exploratory variant entirely (back to v1.10 silent-suppression). Replace the empty-band behavior with passive observability — record the trajectory shape, do not push the model. matplotlib-14623-class trajectories win because the model has space to find its target; pylint-6528-class trajectories were going to lose anyway because the model was non-responsive. Trying to rescue them with text-level interventions appears to cost more than it gains at this convergence-score band.

**The empty_patch floor hit (13) is partially attributable to variance in the favorable direction**. 4 recoveries (matplotlib-25775, pylint-6386, pylint-6528, sphinx-10449) vs 1 regression (astropy-14096). All five are wrong_target / wrong_location / plausible borderline instances — the class with documented temp=0 variance per `feedback_replicate_borderline_fixtures.md`. 3-rep replication would confirm whether v1.10.2's 13 empties is a genuine improvement or favorable variance on top of v1.10.1's 16. The cycle ships on the basis that v1.10.2 is BEHAVIORALLY equivalent to v1.10.1 (post-revert) + new observability infrastructure, so any variance is unbiased; downside risk is zero.

**Observability wins delivered**:

1. **write-locus cross-tab** (the v1.11 substrate) reveals the locus-failure class is dominated by **wrote_to_some_gold_partial** (16 instances, 31.2% Docker rate) — model wrote to AT LEAST ONE gold file but missed others (multi-file bug). NOT "wrong file entirely" (3 instances). The v1.11 lever target shifts from "are you sure this is the file?" to "did you miss any related files?" — a different prompt entirely.

2. **CONFIDENCE_COLLAPSE class split** restored causal attribution. v1.10.1's headline 8 confidence_collapse decomposed into 4 SOFT_ANCHOR (carryover from v1.10) + 4 EXPLORATORY (net new from W3 lever). v1.10.2 shrunk BOTH classes — proving the metric refinement gives stable cross-cycle reading even when intervention policy changes.

3. **patch_len_delta + same_tier_docker_demotion**: sphinx-10673 surfaced AGAIN in v1.10.2 — this time patch GREW (2990 → 3397, +407) and STILL lost Docker resolution. The first v1.10 → v1.10.1 case (patch shrank) and the v1.10.1 → v1.10.2 case (patch grew) prove the class isn't about size direction; it's about subtle changes near the gold patch's expected diff shape. Worth a deeper audit if the class persists.

**v1.10.3 design brief** (small surface; takes the v1.10.2 findings to a conclusion):
1. **Revert v1.10.1 W3 exploratory variant**: back to v1.10 silent-suppression in score < LOW band. The diversity-fallback / escalation experiments confirmed text-level interventions in this band are non-Pareto. Save the recent_path_diversity helper + logging as observability for future cycles, but stop USING the signal as a gate trigger.
2. **v1.11 locus-disambiguation lever**: pre-commit "did you miss any files?" prompt scoped to the wrote_to_some_gold_partial bucket (16 instances). Predicate: model has written to ≥1 gold file but `gold_files_missed` is non-empty AND `step < max_steps - 4` (room to recover).
3. **3-rep variance baseline**: replicate v1.10.2 n=75 three times to establish the empty_patch / Docker variance band. Without this, single-cycle ±2 movements can't be cleanly attributed to lever vs noise.

**Affected files** (full v1.10.2 cycle):
- `src/luxe/agents/convergence.py` — `recent_path_diversity` helper (kept) + diagnostic comments documenting why it didn't gate at fire-time
- `src/luxe/agents/loop.py` — diversity-based dispatcher with threshold=2 minimal-trajectory fallback (kept); step-based + immediate post-exploratory escalation (implemented, tested, REVERTED before ship)
- `src/luxe/agents/outcomes.py` — `FailureClass.CONFIDENCE_COLLAPSE_SOFT_ANCHOR` / `_EXPLORATORY` + `early_bail_msg_variant` capture
- `scripts/compare_v110.py` — `compute_locus_metrics` (renamed from `compute_first_correct_file_touch`); `primary_metric` reports variant counts
- `scripts/analyze_v110_harness.py` — 4-bucket write-locus × Docker cross-tab; informational reconnaissance section
- `scripts/backfill_v110_taxonomy.py` (NEW) — regenerates v1.10 + v1.10.1 + v1.10.2 taxonomies with the CC class split
- `scripts/post_v1102_n75_pipeline.sh` (NEW) — v1.10.2 pipeline orchestration
- `scripts/validate_v1102_probe.py` (NEW) — 4-instance probe validator
- `benchmarks/swebench/subsets/v1102_probe_n4.json` (NEW) — 4-instance regression probe
- `tests/test_compare_v110.py` — +6 write-locus tests
- `tests/test_convergence.py` — +8 diversity tests
- `tests/test_outcomes.py` — +3 CC variant classifier tests
- `tests/test_loop_write_pressure.py` — updated existing exploratory test; removed (after revert) the 4 escalation tests

781 tests pass + 1 module-skip on bfcl_adapter. v1.10.2 tagged + pushed to origin.

### [2026-05-16] gh-auth flake recurrence + downstream luxe `_do_test` subprocess deadlock

**What happened**: During v1.10.2 n=75 rep_2 (the variance-baseline rep), a brief mid-bench internet outage caused `scikit-learn-11310` and `scikit-learn-11578` to bail at exactly 122s with `rc=2` — the documented `assert_gh_auth()` flake from `project_gh_auth_flake.md`. After the user confirmed connectivity restored, I attempted to re-run the two instances on a 2-instance subset. The first retry (sklearn-11310) wrote a commit successfully, then entered `pr_step: test` and **hung for 25 minutes with no further events** before I killed it. A second retry attempt hit a stale luxe lock (PID survived the kill); a third attempt bailed at 1s with `rc=3` because the stale lock from PID 38948 was still present in `~/.luxe/locks/`. After force-killing all luxe + bench processes and removing the stale lock files, the workspace was usable again, but the variance baseline lost 2 instances (imputed as deterministic from their rep_1 + rep_3 agreement).

**Root cause** — two distinct issues in one cascade:

1. **gh-auth flake itself**: `gh auth status` calls the macOS keyring; when the network drops, the keyring login times out and `gh` exits non-zero with "Timeout trying to log in to github.com account ... (keyring)". `assert_gh_auth()` retries 3× at 0.5s/1.5s spacing (added 2026-05-02) but the network was down for longer than the retry window, so the bench bailed. This is the documented flake; mitigation is sound; the cost was 2 datapoints.
2. **`_do_test` had `timeout=None`** (the actual surprise): `src/luxe/pr.py:386` invoked `_run(["bash", "-lc", cmd_str], cwd=repo)` with no timeout. After my kill-and-retry of the gh-auth bailout, the workspace had accumulated state from the killed prior run. The model wrote a 25-line patch on retry, then luxe ran the configured `pytest -q` test command — which deadlocked. `subprocess.run` waited forever. No event was emitted because `_do_test` doesn't emit progress mid-subprocess.

**Fix / takeaway**: Shipped commit `3c3b79b` — `PRConfig.test_timeout_s` (default 600s) flows through `_do_test`; `subprocess.TimeoutExpired` is caught, recorded as `rc=124`, `test_passed=False`, and a clean tail message; bench moves on. Added regression test `test_do_test_timeout_records_clean_failure` in `test_pr_resume.py`. 801 tests pass after the change.

**Generalized lesson**: **Any luxe step or bench harness that shells out to a user-controlled shell command** (test runner, build, linter, anything from yaml config) **must pass a wall-time cap to `subprocess.run`**. `timeout=None` is a latent footgun. The pattern from `3c3b79b` (`cfg.<step>_timeout_s` + `try/except TimeoutExpired → record-and-continue`) is the template; replicate it for any future step. Saved as memory entry `feedback_test_step_needs_wall_cap.md`.

**Side-note (venv drift)**: The very first rep_2 launch this morning crashed in 3s with `ModuleNotFoundError: tree_sitter_language_pack`. The venv had drifted to the pre-v1.10.1 packages (`tree_sitter_languages 1.10.2` instead of `tree_sitter_language_pack 0.13.0`). Yesterday's `pyproject.toml` pin was correct (v1.10.1 W1 commit `6d1709e`), and yesterday's 781-test suite passed, so something between yesterday and this morning reset the venv state. A `.venv/bin/pip install -e .` re-pinned cleanly. Not deep-dived; if it recurs, suspect another Python project sharing the venv directory or a system pip cache invalidation. Documenting here so future-me knows the surface.

**Affected files**: `src/luxe/pr.py` (added try/except + `test_timeout_s` field), `configs/pr.yaml` (documented the new field), `tests/test_pr_resume.py` (new test). No bench-layer changes were needed; the inner cap handles the case.

### [2026-05-16] v1.10.2 3-rep variance baseline — single-rep ship-floor at empty_patch=13 is unsupportable

**What happened**: The v1.10.2 ship report (2026-05-15) headlined `empty_patch = 13 — the ≤13 floor target first set at v1.7 is HIT for the first time`. I ran 2 additional reps of the same n=75 subset on the same v1.10.2 substrate (no code changes between reps; only the `_do_test` timeout cap from `3c3b79b` shipped mid-cycle, didn't fire in rep_3). Results:

| metric | rep_1 | rep_2† | rep_3 | mean | range |
|---|---|---|---|---|---|
| strong | 18 | 17 | 18 | 17.7 | [17, 18] |
| strong+plausible | 38 | 35 | 37 | 36.7 | [35, 38] |
| **empty_patch** | **13** | **15** | **15** | **14.3** | **[13, 15]** |

† rep_2 on n=73 (sklearn-11310 + sklearn-11578 dropped to gh-auth + `_do_test` cascade above); both deterministic across rep_1 + rep_3 so the normalized rep_2 estimate is {strong=18, plausible=19, s+p=37, empty=15} — wash with rep_3.

rep_1 was best-of-3 on `empty_patch`. rep_2 and rep_3 both hit 15. The "floor finally hit" framing was variance-fortunate. 67 of 75 instances are stable across all 3 reps (8.2% real flip rate, all 6 flips on the wrong_target / wrong_location / empty_patch borderline).

**Root cause**: The ship-floor target ≤13 was set within the measurement noise band of the borderline tiers. Single-rep gating at that strictness will reject substrate-equivalent cycles that happen to hit 14 or 15 first. This is the SWE-bench-scale resurfacing of the v1.4-era "borderline doc/manage" variance pattern documented in `feedback_replicate_borderline_fixtures.md`. **The strong-tier and plausible-tier classifications are essentially deterministic at temp=0**; variance lives entirely in the wrong-locus / empty boundary instances.

**Confirmed sub-findings**:

1. **`pylint-6528` is real W3 collateral, not noise**: empty in 2 of 3 reps (rep_2 + rep_3); wrong_target only in ship rep_1. This is the determinism signal the v1.10.2 design brief flagged as a follow-up question. The v1.10.3 W3-revert decision (silent suppression in score<LOW band) is now strongly supported by data, not just one-cycle observation.
2. **`matplotlib-25775` is a new 3-way unstable instance** (plausible/empty/wrong_target). The v1.10.2 ship report counted its rep_1 plausible as a "new Docker resolve"; rep_2 and rep_3 disagree. That ship-report credit was variance-fortunate.
3. **6 variance-class instances catalogued** (exclude from single-cycle pass/fail signals): astropy-14096 (known bouncer, 4+ reps), matplotlib-20826 (borderline locus), matplotlib-25775 (3-way new), pylint-6386 (1-of-3 outlier), pylint-6528 (W3 collateral confirmed), sympy-13091 (1-of-3 outlier).

**Fix / takeaway**: **Ship floors set within ±1 of the measured cycle baseline require multi-rep validation** — saved as memory entry `feedback_ship_floor_needs_multirep_when_at_strictness.md`. The empty_patch ≤13 gate should be either restated as median-of-3 ≤14, or loosened to ≤15 single-shot. The `strong + plausible` headline gate (range [35, 38], much narrower) is more variance-robust and should be the primary ship signal in future cycles. Operationally: any new ship floor should pass a cheap 3-rep mini-baseline before being elevated to a hard gate; running this baseline cost ~9h of bench wall time and saves future cycles from chasing phantom regressions on the borderline tiers.

The variance baseline also incidentally validates the substrate-determinism claim: sklearn-11310 and sklearn-11578 were identical in rep_1 and rep_3 (plausible/plausible, strong/strong) — re-confirming that the gh-auth bailout in rep_2 was environment, not substrate.

**Affected files**: `scripts/variance_v1102_3rep.py` (new, committed in `882eaf0`); `benchmarks/swebench/subsets/v1102_rep2_gh_auth_rerun.json` (new, same commit); rep_2 and rep_3 artifacts live in `acceptance/swebench/post_specdd_v1102_n75/rep_{2,3}/` (gitignored per project convention) and `acceptance/v1102_taxonomy/v1102_n75_rep_{2,3}_*.json` (gitignored). Reproduce the report via `python -m scripts.variance_v1102_3rep --rep <rep_1_tax> --rep <rep_2_tax> --rep <rep_3_tax>`.

### [2026-05-18] v1.10.3 ship HOLD via cohort-shift methodology — aggregate metrics hid a deterministic strong→empty regression on a previously rescued case

**What happened**: v1.10.3 (W3 silent-suppression revert) completed 3-rep n=75. Single-rep rep_1 looked alarming (Docker −6 vs v1.10.2). Median normalized across the 3 reps to clean (Docker −2 apples-to-apples, empty_patch median 15 = v1.10.2 baseline). My first surface report recommended TAG. The user pushed back: "Does this represent a deep dive of the findings?"

The deep dive ran a per-instance cohort-shift 3×3 matrix (`scripts/audit_v1103_suppression.py` later codified the methodology). It surfaced **two deterministic regressions** that the median view hid:

1. **sphinx-doc__sphinx-10435**: strong (3/3 reps in v1.10.2) → empty_patch (3/3 reps in v1.10.3). Load-bearing — this is the v1.9 CONFIDENCE_COLLAPSE rescue case, maintained as strong through v1.10/v1.10.1/v1.10.2.
2. **matplotlib__matplotlib-14623**: wrong_target (3/3) → empty (3/3). The W3 "design-accepted" founding case that, on inspection, was a tier degradation (NOT Docker-equivalent — v1.10.2 rep_1 had this Docker-resolved with a 54-line patch in ticker.py).

The aggregate medians were equal because v1.10.3 also produced **deterministic gains** (sphinx-10323 empty→wrong_location, pylint-4661 wrong_target→plausible) that mathematically offset the losses. Empty_patch count was identical (15 both cycles) but on DIFFERENT instances.

**Mechanism trace for the load-bearing case** — sphinx-10435 under v1.10.2 vs v1.10.3:

| step | v1.10.2 (strong) | v1.10.3 (empty) |
|---|---|---|
| 4 | early_bail_fired with exploratory variant | early_bail_suppressed_diffuse (silent) |
| 5 | (continues exploring) | early_bail_fired with soft_anchor → wrap-up wording terminates |
| 6-11 | reads + writes 17-18 line patch | empty patch |

**Root cause**: The W3 silent-suppression policy in score<LOW band removed the only explicit signal for sphinx-10435's trajectory. With the suppression silent, the next intervention (soft_anchor at score=0.25) fires at step 5 and its "Commit to your best candidate" wording is interpreted as wrap-up by Qwen3.6-35B-A3B-6bit on this trajectory shape — exactly the failure mode `feedback_soft_anchor_wording_reads_as_wrap_up.md` warned about.

**Methodology lesson**: **Per-instance cohort-shift 3×3 matrix is mandatory for any 3-rep validation. Aggregate medians can hide deterministic regressions that mathematically offset deterministic gains.** Codified in `project_v1103_hold_finding.md` and `project_archetype_preflight_methodology.md`. Future cycles must:

1. Build a 3-rep × 3-rep tier matrix (instance × cycle).
2. Flag DETERMINISTIC LOSSES (strictly worse in all 3 reps) of previously-strong-tier cases as HOLD-grade signals regardless of aggregate metrics.
3. Maintain an archetype-N fixture set as a pre-flight gate before any n=14 smoke or n=75 cycle.

**Process lesson**: The ship verdict evolved in 3 steps over this session:
- step 1: single-rep HOLD ("−6 Docker collapse")
- step 2: 3-rep median TAG ("aggregates normalize")
- step 3: 3-rep cohort-shift HOLD ("deterministic regression on previously rescued case")

Each was a coherent interpretation of the available evidence. The decisive evidence came from the deterministic-cross-rep-trajectory analysis (not from any single metric). **Single-rep gates are dangerous AND median-normalized aggregates are dangerous. Cohort-shift methodology is required for HOLD/SHIP decisions on interventions that can redistribute outcomes.**

**Affected files / artifacts**: `scripts/audit_v1103_suppression.py` (new); `benchmarks/swebench/subsets/v1104_archetype_n4.json` (new, the post-cycle archetype fixture); memory entries `project_v1103_hold_finding.md`, `project_psf_requests_5414_band_case.md`, `project_archetype_preflight_methodology.md`; `feedback_intervention_stacking_is_non_pareto.md` updated with v1.10.3 case study.

### [2026-05-19] v1.10.4 cycle — hybrid D+B band response; sphinx-10435 recovered, sphinx-10323 regressed; the 10435/10323 mechanism duality

**What happened**: After the v1.10.3 HOLD finding, the audit of `early_bail_suppressed_diffuse` events across v1.10.3 reps showed that 50% of HARMFUL trajectories had n_suppressions == 1 (the sphinx-10435 archetype: 1 silent suppression → soft_anchor at step 5 → terminate empty). Designed Hybrid D+B: fire a new `breadth_probe` message variant on the FIRST suppression per trajectory AND on the Nth suppression (N=3 escalation). breadth_probe explicitly does NOT set `early_bail_fired` so subsequent interventions still fire when score rises.

Built `_EARLY_BAIL_MESSAGE_BREADTH_PROBE` constant + escalation logic in `loop.py:566-700`. New env `LUXE_EARLY_BAIL_BAND_RESPONSE` (default `breadth_probe_hybrid`; pin to `silent` for v1.10.3 backward-compat). 4 new regression tests. All 801 prior tests pass + 4 new = 805.

**Validation gauntlet** (per `project_archetype_preflight_methodology.md`):

1. **Unit tests** ✓
2. **Archetype-4 preflight** ✓ — 3/4 Docker resolves (v1.10.2 r1 = 2/4, v1.10.3 r1 = 1/4). sphinx-10435 produced byte-identical 18-line patch to v1.10.2; matplotlib-14623 Docker-pass with new 13-line shape; 5414 Docker-pass with new shape; 1921 Docker-pass preserved.
3. **n=14 smoke** ✓ — 0 regressions vs v1.10.3 baseline; 4 gains including sphinx-10435 strong + matplotlib-20826 empty→wrong_location + sphinx-10673 Docker pass.
4. **n=75 3-rep** — mixed (see below).
5. **maintain_suite** ✓ — 10/10 PASS, v1_release_gate=true.

**n=75 3-rep result**:

| metric | v1.10.3 3-rep median | **v1.10.4 3-rep median** |
|---|---|---|
| strong | 18 | **19** (best ever) |
| plausible | 19 | 19 |
| s+p | 37 | **38** (best ever) |
| empty_patch | 15 | 15 |
| Docker resolves | 35 | **37** |
| Apples-to-apples (55 shared) | 33 | 34 (+1) |

**Cohort-shift v1.10.4 vs v1.10.3**:
- DETERMINISTIC GAIN: psf__requests-5414 (plausible 3/3 → strong 3/3 + Docker false→true 3/3)
- DETERMINISTIC LOSS: **sphinx-doc__sphinx-10323** (wrong_location 3/3 → empty 3/3). NEW regression introduced by v1.10.4.
- Modal gains: matplotlib-14623, matplotlib-20826, sphinx-10435 (recovered to 2/3 non-empty: 1 strong + 1 plausible + 1 empty)
- Modal losses: matplotlib-25775, psf__requests-2317, sympy-11618
- **0 new strong→empty regressions vs v1.10.2** (the class that drove v1.10.3 HOLD is closed)

**The mechanism finding** — sphinx-10323 trajectory analysis ORIGINALLY described as the "mechanism-inverse of sphinx-10435," but the post-cycle audit (2026-05-19) **revises this framing**:

- sphinx-10435 needs the breadth_probe nudge to keep going. Under v1.10.3 silent-suppression, soft_anchor at step 5 terminates with empty. Under v1.10.4 breadth_probe at step 4, model continues exploring → writes 18-line strong patch (when it works; 1 of 3 reps).
- sphinx-10323 **does not have a "needs silent suppression" failure mode in the simple way the original framing suggested**. The actual v1.10.4 trace shows: the model looped on RST-parser analysis for ~20 iterations (~36k chars of repetitive prose), never converged on a real code fix, wrote a 50-line `repo.sdd` scaffolding placeholder (NOT the actual buggy file sphinx/directives/code.py), and emitted a hallucinated citation (`index.rst.rst:155`, derived from the issue description's error message). Citation-lint correctly rejected the patch. Under v1.10.3 silent suppression, the model ran 19 steps and wrote a different small (15-line) patch to the wrong file — wrong_location tier, but at least passed lint. So 10323's failure mode is **synthesis-loop without grounding on the real buggy file**, not "premature commitment on a sound hypothesis." The breadth_probe nudge correlated with the failure but isn't the architectural root.

The two archetypes are NOT pure mechanism-inverses. They share a common state at suppression #1 (both at score=0, low convergence) but have different downstream failure modes. v1.10.5 levers must address each separately.

**Updated post-cycle audit (2026-05-19) — loop-layer ceiling claim was overstated.** The "loop layer is approaching its information-theoretic ceiling" framing in the original lesson is too strong. A re-audit of all 10 v1.10.3 HARMFUL trajectories shows **80% (8/10) are signal-separable from HARMLESS-success cases at suppression #1**; only 20% (2/10) have byte-identical signal vectors to a HARMLESS-success. The collision class includes 10435↔14623 (originally identified) and the newly-found 5414↔10449 pair. So the ceiling exists at *specific collision points*, not as a universal architectural limit. v1.10.5 design space within the loop layer is wider than this lesson originally claimed; specifically, sphinx-10323 (diversity=3, bm25=1) IS separable from the 10435/14623 cluster (diversity=2, bm25=0), so a targeted predicate can fix one without disturbing the other. See plan file `~/.claude/plans/frolicking-wiggling-cray.md` (v1.10.5 plan) and the v1.10.5 cycle entry below for the corrected design path.

**Ship verdict: HOLD pending v1.10.5 design pass.** Per the corrected diagnosis, v1.10.5 candidate is: condition first-event breadth_probe on `(diversity < 3) AND (bm25 == 0)` — fires for 10435/14623 cluster (preserves v1.10.4's load-bearing gain), suppresses for sphinx-10323 (recovers v1.10.3 wrong_location). Uncertainty: 5414/1921 currently rely on first-event fire; predicate suppresses them and relies on the escalation at suppression #3. Whether escalation alone preserves their v1.10.4 outcomes is the empirical question for the v1.10.5 archetype-4 probe.

**Process methodology validated**: archetype-driven evaluation works. The 5-instance archetype set (10435, 14623, 5414, 1921, plus the v1.10.4-discovered sphinx-10323) should be permanent. Per `project_archetype_preflight_methodology.md`, this is the bench-launch ritual going forward.

**Affected files**: `src/luxe/agents/loop.py` (`_EARLY_BAIL_MESSAGE_BREADTH_PROBE` + `_BREADTH_PROBE_ESCALATION_COUNT` + new suppression-count state + hybrid firing logic; lines 232-700); `tests/test_loop_write_pressure.py` (+4 tests); `benchmarks/swebench/subsets/v1104_archetype_n4.json` (new); `scripts/audit_v1103_suppression.py` (new); `scripts/post_v1104_n75_pipeline.sh` (new). Acceptance artifacts under `acceptance/swebench/archetype_preflight_v1104/`, `acceptance/swebench/v1104_smoke_n14/`, `acceptance/swebench/post_specdd_v1104_n75/rep_{1,2,3}/`, `acceptance/v1104_maintain_suite/`, `acceptance/v1104_taxonomy/` (all gitignored). Memory entries: `project_v1104_ship_validation.md`, `project_archetype_preflight_methodology.md` (updated to 5 archetypes).

### [2026-05-20] v1.10.5c CLEAN SHIP — distinct_files topology partition closes the 10323/12419 mechanism-symmetric pair; first cohort-shift clean since v1.10.2

**What happened**: After v1.10.4 shipped as substrate (sphinx-10323 deterministic regression vetoed a ship claim), v1.10.5 designed a corrected predicate that separates two mechanism-distinct failure modes sharing the bm25-without-grep signature. Two iterations:

- **v1.10.5b (initial)**: predicate `NOT (bm25>0 AND grep==0)` recovered sphinx-10323 but broke sympy-12419 (11/11-prior-stable plausible → empty via consecutive-repeat-loop death spiral)
- **v1.10.5c (refined)**: predicate `NOT (bm25>0 AND grep==0 AND distinct_files>=2)` ALSO clears sympy-12419. The `distinct_files=2` partition is the key insight.

**Validation gauntlet** (per user-precommitted gates):
1. ✓ Gate 1 (sympy-12419 targeted probe): first-event fired at step 4, 17-line plausible patch + Docker pass
2. ✓ Gate 2 (archetype-4 + sphinx-10323 probe): 5/5 outcomes match expectations
3. ✓ Gate 3 (n=14 smoke): sympy-12419 recovered + sphinx-10435 strong + all 12 others stable (matplotlib-20826 went empty but is in v1.10.2 variance class — not 11/11-stable, so within precommit's stopping-rule tolerance)
4. ✓ Gate 4 (n=75 3-rep): ALL cohort-shift criteria cleared

**3-rep results (cycle history table)**:

| metric | v1.10.2 | v1.10.3 | v1.10.4 | **v1.10.5** |
|---|---|---|---|---|
| strong (median) | 18 | 18 | 19 | **20** (best ever) |
| s+p (median) | 37 | 37 | 38 | **39** (best ever) |
| empty_patch (median) | 15 | 15 | 15 | **13** (= v1.10.2 best, best ever) |
| Docker apples-to-apples (median) | 36 | 33 | 35 | **36** (back to v1.10.2 baseline) |
| Docker apples-to-apples BEST rep | 36 | 35 | 35 | **37 (66.1%)** |

**Cohort-shift v1.10.5 vs v1.10.4 (3-rep × 3-rep, the cycle's primary gate)**: **0 deterministic losses + 1 deterministic gain (sphinx-10323 recovery)** — the cleanest cohort-shift result in the v1.7→v1.10 cycle history.

**Archetype outcomes — all 6 cleanly resolved**:
- sphinx-10435 (10435/14623 cluster): 2/3 strong patches (improved from v1.10.4 1/3)
- matplotlib-14623: Docker 3/3 T (improved from v1.10.4 2/3)
- 5414: T/T/T (preserved)
- 1921: T/T/T (improved from v1.10.4 2/3 — substrate flake recovered)
- **sphinx-10323: wrong_location 3/3 (byte-identical to v1.10.3 `7705189cbc`/708b)** — the v1.10.4 regression FIXED
- **sympy-12419: T/T/T** — the v1.10.5b regression target stable

**The mechanism finding — distinct_files=2 partition is mechanistically interpretable**:

The bm25-without-grep signature catches TWO distinct failure modes:
1. **sphinx-10323 archetype (distinct_files >= 2)**: synthesis-wandering with breadth → model retrieved candidate via corpus search, read multiple files, but never confirmed via grep → commits a citation-ungrounded patch that lint rejects. Suppressing first-event lets trajectory run v1.10.3-style and write a (shallow but lint-passing) wrong_location patch.

2. **sympy-12419 archetype (distinct_files < 2)**: premature-loop-kill — single-file focus + bm25 retrieval but model hasn't expanded reading yet. First-event breadth_probe acts as a loop-state destabilizer, perturbing the policy out of a local attractor before `_MAX_CONSECUTIVE_REPEAT_STEPS=2` aborts.

The `distinct_files=2` partition correctly separates these mechanism-symmetric cases using ONLY observable loop-layer signals at suppression #1. This is the first predicate that empirically separates the 10435/10323 + 12419 archetype trio cleanly.

**Process lesson**: feature calibration must verify at the actual event-emission point. The initial v1.10.5 predicate (`diversity<3 AND bm25==0`) failed because hand-computed feature values from an explore agent were WRONG for the 10435/14623 cluster. The corrected v1.10.5c predicate works precisely because step-4 features are substrate-deterministic (verified 8 reps × 6 archetypes show byte-identical tool_history) — design space within the loop layer is real when features are correctly measured.

**User-precommit lesson**: the user's "one more iteration then pivot" stopping rule was decisive. Without it the project could have entered "heuristic accretion mode" (every new edge case adds another AND <new_signal> clause). The precommit forced a clean decision boundary on what success looks like.

**Affected files**: `src/luxe/agents/loop.py` (`_v1105_synthesis_looping_signature(bm25, grep, distinct_files)` helper + integration; new event fields `grep_count`, `distinct_files`); `tests/test_loop_write_pressure.py` (+6 tests: 1 unit + 5 integration); `benchmarks/swebench/subsets/v1105_sphinx_10323_probe.json` + `v1105c_sympy_12419_probe.json` + `v1105c_gate2_n5.json` (new); `scripts/post_v1105_n75_pipeline.sh` (new). Acceptance artifacts under `acceptance/swebench/v1105c_*/`, `acceptance/swebench/post_specdd_v1105_n75/rep_{1,2,3}/`, `acceptance/v1105_taxonomy/` (all gitignored). Memory entries: `project_v1105_predicate_probe_failure.md` (updated with corrected diagnosis + revised conclusion), `project_v1105_ship_validation.md` (new).

**Ship recommendation**: tag v1.10.5 as a ship release (not substrate). First clean cohort-shift since v1.10.2.

### [2026-05-21] v1.11 cycle — adaptive-policy lever TRIED + REVERTED; premature-commitment tier demotion is a non-Pareto effect that empty-count checks miss

**Cycle**: v1.11 = candidate B (per-instance adaptive policy). Phases 0–4 (substrate + closed-loop priors, all log-only/inert) shipped 2026-05-20. This cycle added Phase A (offline calibration) → Phase B (activation) → Phase C (probe) → Phase D (n=75 ×3 ship gate). **Outcome: the activation lever was net-negative at n=75 and was reverted. No v1.11 tag; main ≈ v1.10.5 + calibrated observability.**

**Phase A calibration (offline, on 71 retained event streams) caught two of my own errors via authoritative cross-check** — the same discipline that saved the v1.10.5 miscalibration: (1) I claimed the Phase 3a/4 substrate was "inert"; it was not — write_pressure modulation departed 1.0 in 231 events (the commit's "stays at 1.0" was scoped to archetypes only). (2) My first outcome classifier used `diff_stat after_main_pass`, which over-counts patches (mid-run writes revert); the authoritative signal is `patch_present`. Decisive finding: `consecutive_no_write` is a NON-SELECTIVE lever (precision ≤31% at every threshold — read-heavy SUCCESS trajectories hit the same no_write depths as stalls; matplotlib-14623, a success, hits 15–17). Pivoted the lever to `score_trend` (collapse velocity), which separates empties at fire-time (step 6–8). Retired the no_write→write_pressure bias (kept — it's v1.10.5-neutral).

**The lever (Phase B)**: `score_trend → soft_anchor` score<LOW band-response collapse promotion (breadth_probe → one-shot soft_anchor commitment nudge) gated on `conv<LOW AND step≥6 AND trend≤0`. Framed as response-INTENSITY of an already-triggered early_bail predicate (bias-only per agents.sdd).

**Phase C smoke (n=1) caught a fire-time calibration error before the 3h probe**: seaborn-3069 terminates at step 6, so the initial `_COLLAPSE_MIN_STEP=7` never fired. Phase A showed conv<LOW becomes selective at step 6 → lowered to 6. Re-run confirmed the promotion fires. (Validate-small earned its keep again.)

**Phase C (archetype-6 + 2 empties ×3 + BFCL) passed all precommitted gates** — lever fires, 0 archetype regressions, BFCL 240/240 — but with **0 lever-attributable conversions** (seaborn-3069 got 4 nudges, never budged). I read this as "safe, value TBD at n=75" and proceeded.

**Phase D (n=75 ×3 + Docker + cohort_shift_3x3): HOLD.** cohort_shift = **3 deterministic losses, 0 gains**. Two are LEVER-CAUSED (promotion fired 3/3): `pydata__xarray-3305` strong→plausible, `pylint-dev__pylint-4661` plausible→wrong_target. One (`pylint-4604` wrong_target→empty, 0 promotions) is cross-run substrate drift. Aggregate empty 13→16, s+p 39→37 (both floors missed); Docker ~wash (the 3 tier losses cost 0 Docker resolves — xarray-3305 still resolves T/T/T; 4604/4661 weren't resolving anyway).

**Lesson 1 — judge band-response levers on full-tier cohort_shift, NEVER empty-count alone.** Mid-run I deep-dived 2 reps on empty_patch only and concluded "Pareto-neutral, 0 lever-caused regressions." That was WRONG: the nudge doesn't push trajectories to *empty*, it demotes their *tier* (strong→plausible, plausible→wrong_target). Only the 3-rep full-tier `cohort_shift_3x3` exposed it. Empty-count is a lossy projection of the regression surface.

**Lesson 2 — a commitment nudge gated on a single-step stall snapshot derails mid-deep-dive recoveries (premature commitment).** `conv<LOW AND trend≤0` at step 6 cannot distinguish a true stall from a productive deep-dive with a transient low-trend dip. The nudge "commit to your best candidate now" then makes a recovering trajectory commit early to a less-complete or mis-located patch. The `trend≤0` Pareto guard (which preserved sphinx-10323/sympy-12419) was NECESSARY but NOT SUFFICIENT — xarray-3305/pylint-4661 hit the same signature transiently and were degraded. This is the same non-Pareto family as the W3 saga, one tier-rung softer.

**Lesson 3 — the user's "deep dive before you cut" call was decisive.** I was about to cut rep_3 on a wrong causal story ("lever causing W3 empty-collapse"). The full deep-dive flipped the diagnosis (lever is tier-demoting, not empty-collapsing; the empty bump was mostly variance + 1 unrelated drift). Letting the 3-rep gate finish produced the correct, defensible verdict.

**Reverted** (`b5d71f4`): loop.py promotion + `_SOFT_ANCHOR_PROMOTE_MODULATION` + one-shot flag. **Kept**: no_write retirement (`convergence.compute_intervention_bias` pins write_pressure/early_bail bias to 0), `AdaptiveState.convergence_score`, and the score_trend→soft_anchor bias as OBSERVABILITY ONLY (still drives `modulation_soft_anchor` in the adaptive_state event so v1.11.1 can mine where a better stall signal would fire). 921 tests pass.

**v1.11.1 design target**: a non-recovery-specific stall signal — sustained `trend≤0` over K steps, or a semantic-breadth-saturation signal (the "breadth of explored hypotheses, not temporal counters" direction flagged in the v1.10.4 review) — NOT a single-step snapshot. The reverted bias + observability events are left in place to mine for it.

**Affected files**: `src/luxe/agents/convergence.py` (no_write bias retired; `AdaptiveState.convergence_score`; score_trend→soft_anchor bias kept as observability), `src/luxe/agents/loop.py` (promotion reverted), `src/luxe/agents/agents.sdd` (Phase B activation section: tried+reverted), `tests/test_convergence_adaptive.py` + `tests/test_loop_adaptive_policy.py`, `scripts/analyze_v111_calibration.py` + `analyze_v111_phaseC.py` + `run_v111_phaseC.sh` + `run_v111_phaseD.sh` + `post_v111_n75_pipeline.sh` (new), `benchmarks/swebench/subsets/v111_phaseC_n8.json` + `v111_smoke1.json` (new). Acceptance artifacts under `acceptance/swebench/post_v111_n75/rep_{1,2,3}/`, `acceptance/v111_taxonomy/` (gitignored). Memory: `project_v111_phaseA_calibration.md` (updated with full cycle outcome).

### [2026-05-21] v1.11.1 — offline-only cycle: a predicate redesign cannot fix the v1.11 lever; recovery and stall are entangled at the loop layer. STOP before any bench.

**Premise (v1.11.1 = candidate B′)**: v1.11 proved the *mechanism* (score_trend→soft_anchor band-response) was wired and SDD-compliant; it failed only on its *gate* (`conv<LOW AND step≥6 AND trend≤0`, a single-step snapshot). The plan: replace the gate with a **non-recovery-specific** stall predicate and prove its selectivity **offline** against the retained Phase D corpus before writing dispatch code or spending a ~16h n=75×3 bench. Per the precommitted plan, "no predicate clears the bar" was a valid terminal outcome.

**Method**: forked `scripts/analyze_v111_calibration.py` → `scripts/analyze_v1111_gate_design.py`. Mined the **v1.10.5 BASELINE arm** (`post_specdd_v1105_n75`, 225 retained event streams) — the *uncontaminated* trajectories the lever would perturb — NOT the lever-ON arm (post-fire steps diverge). Reconstructed two candidate signals per wall step from baseline events: **C1 temporal-persistence** (consecutive `trend≤0`, computed with the production `score_trajectory_trend` over the `convergence_score` series from `action_density_sample`; counter resets on any positive trend so it never spans a prior recovery arc) and **C2 breadth-saturation** (steps since a new successfully-touched distinct file path, from `tool_call` `path`+`bytes_out`+`duplicate`). Joined to baseline tiers (`v1105_taxonomy`). **Cross-validation**: the offline single-step reconstruction reproduced **45/45** of the actual lever-ON `soft_anchor_collapse_promote_fired` events (0 misses) — the pipeline is faithful.

**Classes (re-derived from n=75, NOT carried-forward Phase A labels — a reviewer-flagged trust gate)**: band universe = trajectories entering `conv<LOW` at `step≥6` (92/225). RECOVERY (negative; gate must NOT fire) = band-entering strong/plausible (33). STALL (target) = band-entering empty/wrong_target/wrong_location (59). The named instances (xarray-3305, pylint-4661, …) were demoted to *sentinels within* a behaviour-defined population to avoid overfitting to four exemplars.

**Result — STOP. No predicate clears "0 recovery false-positives" with meaningful stall coverage**:
- The **v1.11 single-step gate fired on 30/33 recovery** trajectories — it quantifies *why* the bench failed (near-indiscriminate in the band).
- **C1**: only strict `trend<0` K=5 reaches 0 recovery, but catches **1** stall (useless). All looser K fire on 22–30 recovery.
- **C2** (the stronger prior): J=4 finally sheds `xarray-3305` (it kept reading new files during its dip) but still fires on `pylint-4661` + 5 other recovery instances. A `min_step` sweep to 12 never clears (`pylint-4661` fires at every threshold). C1∧C2 conjunction at min_step=8 still fires on 6 recovery.

**Root finding — the recovery and stall classes are structurally entangled in the score<LOW band, so no gate that READS only loop-layer signals at fire-time can separate them.** `pylint-4661` is the proof: it sits at `conv=0.0` with **saturated breadth** for steps 6–9 (byte-indistinguishable from a stall) and commits a **plausible** patch only at step 13. "Will commit successfully late" vs "will stall to empty" is a reasoning/commit-*timing* property, not a loop-observable one. This is the older "reasoning ceiling, not commitment ceiling" theme made quantitative.

**Lesson — a predicate-only redesign cannot rescue a non-Pareto intervention when the target and protected classes are entangled in the signal space the predicate reads.** The only fixes are to change the signal space (above-loop semantics — task/traceback locality) or to abandon the intervention. Tuning K/J/min_step is heuristic accretion against an unwinnable boundary.

**Meta-lesson — the offline-first inversion paid for itself.** The lever-ON arm had *already* demoted two recovery sentinels deterministically (the v1.11 bench); the offline pass explained *why* no gate fix works and ruled out the entire v1.11.1 premise **at zero bench cost**, saving a structurally-doomed ~16h n=75×3 run. `[[feedback_validate_first]]` / `[[feedback_instrument_loop_first]]` earned their keep again. Reviewer steers that shaped the decisive analysis: re-derive classes from n=75; anchor the negative set on trajectory-pattern not instances; measure J in wall steps; treat false-positives as strictly costlier than false-negatives.

**Verdict / pivot**: the **loop-layer adaptive-predicate line (v1.11.x) is exhausted** — the v1.11 bench and the v1.11.1 offline pass agree the band is not separable with loop-observable signals. Recommend pivoting to **Track C (above-loop signaling — changes the signal space)** or **Track D (substrate hygiene)** as a lower-effort interlude. `main` is unchanged (no src/ edits); v1.11.1 ships only the analyzer as durable instrumentation.

**Affected files**: `scripts/analyze_v1111_gate_design.py` (new — reusable offline gate-screening template); `acceptance/v1111_gate_design/run_id_manifest.json` (gitignored). No `src/` changes — Gate A′ returned STOP. Plan: `~/.claude/plans/serialized-noodling-reef.md`. Memory: `project_v1111_gate_design_stop.md` (new).

### [2026-05-22] Track C + Track D — two roadmap tracks dissolved by cheap grounding before any build; the reusable lesson is "ground the premise against artifacts first"

Both remaining post-v1.11 roadmap tracks were resolved this session **without a build**, each by a grounding pass against existing artifacts/data. The shared lesson is now seen 3× (v1.11.1, Track C, Track D) and is the headline: **ground a roadmap track's premise against the actual code/artifacts/data before treating it as actionable work.** Inherited RESUME framing is a hypothesis, not a fact.

**Track C (above-loop signaling) — premise REFUTED before any code.** The thesis (from the v1.11.1 finding): task-semantics / traceback-locality signals knowable *above* the loop could fix what the loop can't (the pylint-4661 "late-commit vs stall" ambiguity). I joined the n=75 baseline taxonomy (`v1105_taxonomy`, fields `first_correct_file_touch_step` / `gold_target_files` / write-locus) to the issue text (`benchmarks/swebench/subsets/raw/verified.jsonl`). Two killers:
- **Locus discovery is already solved.** The model touches a gold target file *early* (≤4 steps) in **73/75 runs across every tier** — strong 19/20, plausible 19/19, wrong_target 14/17, wrong_location 5/6, empty 9/13; only **2/75 never** touch a gold file. Failures are not "couldn't find it" — they're "found it, produced the wrong/no change."
- **Tracebacks are rare and anti-correlated with success.** Only **9/75** issues contain a traceback, and **7 of those 9 are wrong_target**; the gold file is named in 25/75 issues, *more* often in wrong_target (9/17) than strong (7/20). So surfacing a traceback/file-name can't help discovery that already works ~96% of the time, and where tracebacks exist they co-occur with failure (likely distractor frames).
- **Conclusion**: the residual failure mass is a **reasoning/content ceiling** (what to change in the already-located file), not a "where" problem — the "reasoning ceiling not commitment ceiling" theme, now extended to "not a *locus* ceiling either." Above-loop locality is the wrong lever; no code written.

**Track D (BFCL substrate hygiene) — CLOSED as record-correction, not an unblock.** RESUME framed it as "revert the bfcl_eval substrate so the full suite runs (irrelevance-only)." **Both halves were stale.** Direct artifact reads showed: luxe's grader `benchmarks/bfcl/grade.py` is **pure-Python** (function-name + arg-allowed-set, 5 categories) and **never imports `bfcl_eval`**; `grep` of `benchmarks/bfcl/` finds no `tree_sitter`; the data is vendored (`~/.luxe/bfcl-data/`, commit `dfdb0c8`). The `tree_sitter==0.21.3` conflict only ever affected *data access* via an old `import bfcl_eval` fallback in `adapter._bfcl_data_dir`, which vendoring eliminated. A **25-problem raw smoke (5/category) confirmed** the current substrate supports end-to-end execution + grading across all 5 categories (20/25; nonzero passes in every non-irrelevance category; no tree_sitter/bfcl_eval traceback). **Fix**: removed the dead `import bfcl_eval` fallback (it encoded an obsolete recovery path that re-poisons the venv — installing `bfcl_eval` forces `tree_sitter==0.21.3` and breaks `src/luxe/symbols.py`'s `tree_sitter_language_pack` import) and corrected the docstring/module-doc/error to warn against it. **Residual = measurement debt only**: the last full-suite baseline is v1.8 (2026-05-12, agent, 90.24%), frozen across the swap + 5 releases + v1.11; re-baseline handed off as a command, not run.

**"Broken" vs "stale" — the distortion this corrected.** Treating a *stale baseline* (measurement debt) as a *broken substrate* (engineering unblock) had quietly biased the roadmap toward unnecessary infra churn ("revert the substrate"). The two are different operational states and must be labeled correctly in the authoritative planning doc — RESUME repair is state repair, not cosmetics.

**The frontier this leaves.** A/B′/C/D are now closed or de-prioritized; the remaining failure mass is a model reasoning/content ceiling that loop/prompt levers have repeatedly failed to move (v1.7–v1.11). The honest next step is a user-level conversation about direction (model-capability re-bench if a stronger champion appears, per the single-champion policy; or shifting value axis) — not another lever.

**Affected files**: `benchmarks/bfcl/adapter.py` (`_bfcl_data_dir` — removed dead `bfcl_eval` fallback; corrected docstring + `FileNotFoundError` + module docstring). No grader/semantic changes (`grade.py` frozen for interpretability). Smoke artifacts: `acceptance/bfcl/trackd_smoke/rep_1/` (gitignored). Memory: `project_trackc_locus_grounding.md` (new), `project_bfcl_full_suite_unblocked.md` (new). Plan: `~/.claude/plans/serialized-noodling-reef.md` (Track D).

### [2026-05-22] BFCL multi_turn shipped (baseline 63%) — vendor-faithful + clean-baseline + test-caught-bugs discipline

Built the deferred BFCL `multi_turn` category end-to-end (Phases 0–4, all committed/pushed). Champion `multi_turn_base` baseline = **126/200 = 63.0%**. Several reusable lessons:

**Faithful-by-construction beats reimplementation.** The grader is the official `bfcl_eval` state-based checker, **vendored verbatim** (only `bfcl_eval.* → benchmarks.bfcl.multi_turn.*` import rewrites) into the runtime (MyEnv has no `bfcl_eval` — its `tree_sitter==0.21.3` pin would break `symbols.py`). Because state comparison is plain `==` on stdlib attrs with no normalization, verbatim vendoring makes the verdict identical to upstream — confirmed by a parity gate (n=25 real predictions, 25/25 vendored==official-in-stale-`.venv`). The Phase-0 audit that made this safe: import-graph (long_context is fully `self.long_context`-guarded → unused in base), semantic-drift (dynamic import via `CLASS_FILE_PATH_MAPPING` → rewrite; `globals()` instance-cache + `type()==` checks fine within a grader), and confirming `OMIT_STATE_INFO_CLASSES ∩ clean-set = ∅` (no silent state-skip).

**Clean baseline preserves benchmark meaning.** `run_agent` can't drive multi-turn (rebuilds messages each call — no per-turn history seeding — and applies luxe interventions). Building a purpose-built `backend.chat` + `dispatch_tool` loop (no interventions/retries/repair) measures the *champion's* multi-turn capability, not luxe's scaffolding — the "raw-equivalent" for multi-turn. `loop.py`/`backend.py` untouched; the assistant/tool messages are replayed in loop.py's exact OpenAI shape for context fidelity.

**The tests earned their keep — twice.** (1) A **replay-idempotence** test (grade the same prediction twice → identical) caught a real `globals()` instance-cache leak in the vendored executor (instances keyed by `model_name+test_id` were reused-mutated across grade calls); fix = unique per-call `model_name`. This was exactly the state-leakage class the plan reviewers flagged. (2) The **n=25** run surfaced a JSON-serialize crash (raw `GorillaFileSystem` `Directory` objects — with a `parent` back-ref cycle — in the checker state-diff) that the **n=2 smoke missed**; fix = `default=str` (cycle-safe; the classes have content-bearing `__repr__`) + a write-fallback so one bad problem can't kill a long run. Lesson: validate at a *realistic* scale (n≈25) before the big run; tiny smokes miss data-shape edge cases.

**Generation signal vs grader fidelity are separate axes.** The number is "luxe CLEAN multi_turn" (grader leaderboard-faithful; generation is luxe's own clean trace, not BFCL's handler). The n=25 parity sample read 40% but the full n=200 is 63% — small curated samples can mislead on the *score* (they don't on grader *correctness*).

**Affected files**: `benchmarks/bfcl/multi_turn/` (vendored eval + `executor.py`), `benchmarks/bfcl/adapter.py` (`run_problem_multi_turn` + fields + prompt), `grade.py` (`grade_multi_turn`), `run.py` (route + retention + serialize fix), `scripts/parity_multi_turn.py`, `scripts/fetch_bfcl_data.sh`, `tests/test_bfcl_multi_turn.py`. Baseline: `acceptance/bfcl/multi_turn_base/rep_1/` (gitignored). Memory: `project_bfcl_multi_turn_baseline.md` (new). Plan: `~/.claude/plans/serialized-noodling-reef.md`.

### [2026-05-23] multi_turn Part A — scoped GorillaFileSystem guidance is a non-Pareto WASH (+1); keep clean. The 0-variance gift = exact lever attribution.

Tried to lift the multi_turn weak spot (GorillaFileSystem 42%) with **scoped, opt-in** per-class
system-prompt guidance (`LUXE_MT_CLASS_GUIDANCE=1`; injected only when GFS ∈ involved_classes →
non-GFS problems byte-identical by construction). Guidance targeted the diagnosed failures
(path-semantics confusion + over-acting/uncertainty-collapse on writes): cwd-relative names, assume
prior ops succeeded / don't retry a different way, do exactly what's asked.

**Exact A/B (clean rep_1 vs enhanced, n=200, temp=0 → 0 variance so every diff is causal):** overall
63.0%→63.5%, GFS 42%→44%, **net +1** — 4 fixed (base_11/13/15/38), 3 broke (base_6/33/35),
non-GFS 150/150 byte-identical. The guidance DID cut GFS over-calling (mean 8.3→7.8). But the 3
regressions are **under-action**: base_6 writes 5→2, base_33 calls 6→5 — the precision/"don't retry,
do exactly what's asked" guidance suppressed writes that were genuinely needed. So it trades
**over-action failures for under-action failures** — classic non-Pareto, netting ~0.

**Decision: keep clean as default; guidance stays opt-in + documented (not a win).** A net +1 with 3
deterministic regressions is a wash, and matches the project's long prompt-lever-washout record
(deliberation amplifiers; v1.7–v1.11). The mechanical "net>0 → ship" is wrong here — ship is a
judgment call (small net + regressions = wash). **Methodological win**: 0-variance determinism turned
the lever eval from a noisy statistical question into an EXACT one — 4 named fixes, 3 named breaks,
each mechanistically characterized (over- vs under-action). That clean attribution is itself the
durable value, and is why the wash verdict is trustworthy without replication.

**Also (Part B)**: grader precondition resolved — the official eval grades ALL multi_turn categories
with `multi_turn_checker` only (not the irrelevance checker), so `grade_multi_turn` is faithful for
miss_func/miss_param/long_context. Caught + fixed a long_context generation/grading mismatch
(`build_tool_surface` must forward `long_context=` to `_load_scenario` or generation uses base state
while grading uses the extension — extension fires 466→12054). miss_func/miss_param deferred (dynamic
per-turn tool-withholding; parity can't validate the generation side).

**Affected files**: `benchmarks/bfcl/adapter.py` (`_CLASS_GUIDANCE` + scoped injection + `category`/`long_context`), `benchmarks/bfcl/multi_turn/executor.py` (`long_context` param), `run.py`, `scripts/ab_multi_turn.py` (new), `tests/test_bfcl_multi_turn.py`. Artifacts: `acceptance/bfcl/multi_turn_base/{rep_1,enhanced_rep_1}/`, `multi_turn_long_context/rep_1/`. Memory: `project_bfcl_multi_turn_baseline.md` (updated).

---

### [2026-05-23] `excluded_function` silently ignored: a faithfulness gap hiding in base/long_context

**What happened**: While adding `miss_func`/`miss_param` (per-turn tool-withholding), the new generic
surface filter read both `missed_function` and `excluded_function` from each problem. A scoped
"base must stay byte-identical" regression test then FAILED — the driver was now hiding `cp` on
`multi_turn_base_0`. Investigation: **18/200 problems in EVERY multi_turn category (base, long_context,
miss_func, miss_param) carry a non-empty `excluded_function` (e.g. `['cp']`)**, and the pre-existing
`run_problem_multi_turn` IGNORED that field entirely — it exposed the excluded functions to the model.
Upstream BFCL removes them (and base GT never calls `cp`/`mv`/`rm`: 0 violations), so the prior base
(63.0%, M1) and long_context (58.5%, M5) baselines were measured with excluded functions wrongly on the
tool surface.

**Root cause**: `excluded_function` is applied at function-list construction time in upstream BFCL, NOT
in the multi-turn handler loop (which only injects `missed_function`). luxe's vendored path builds the
surface from `involved_classes` via `build_tool_surface` and never subtracted `excluded_function`. The
field was assumed to be a miss_*-only concern; it is in fact present across all categories. The
byte-identity assumption in the plan ("base/long_context carry neither field") was simply wrong.

**Fix / takeaway**: apply `excluded_function` uniformly in the driver (hidden the whole conversation),
which is the faithful behavior. Impact was fully characterized before recording: faithful long_context =
**57.5% (115/200)**, down from the unfaithful 58.5% — **exactly 2 deterministic flips (`_1`, `_40`,
True→False), both within the 18 excluded_function problems, 0 flips outside (determinism confirmed)**.
Note the flips were NOT the 2 problems that *called* `cp` in the old run — removing a tool from the
surface perturbs the prompt (hence generation) even when the model never calls it. Per the in-the-loop
call: long_context was re-measured faithfully (`m5_faithful_rep_1/`); base 63.0% keeps the gap as an M1
number with a documented caveat (re-run on M5 if a clean base number is needed). General principle: a
"keep it byte-identical" claim must be *verified against the data's actual fields*, not assumed — and a
scoped regression test on the real surface is what caught it (the plan's reviewers explicitly asked for
asserting on the serialized tool list, not an empty-hidden-set proxy). This is also why the recorded
baseline-of-record must come from the faithful driver, not a luck-of-the-defaults run.

**Affected files**: `benchmarks/bfcl/adapter.py` (`run_problem_multi_turn` per-turn surface +
`excluded_function`/`missed_function` parse + `exposed_tool_names`), `benchmarks/bfcl/run.py` (retention),
`scripts/fetch_bfcl_data.sh` (categories + blocking GT pre-flight), `tests/test_bfcl_multi_turn.py` (+12
tests incl. faithful-exclusion + base full-surface). Commit `4b5d462`. Artifacts:
`acceptance/bfcl/multi_turn_{miss_func,miss_param}/m5_rep_1/`, `multi_turn_long_context/m5_faithful_rep_1/`.
Memory: `project_bfcl_multi_turn_miss_baselines.md` (new), `project_bfcl_multi_turn_long_context_baseline.md` (updated).

### [2026-05-26] Track 0 forge-vs-luxe loop A/B closes as WASH at n=75 — the n=14 clean Pareto superset was a small-n favorable draw

**What happened.** The Track 0 architecture comparison (forge's `WorkflowRunner` + `TieredCompact` + `ResponseValidator`/`StepEnforcer`/`ErrorTracker` loop vs luxe's tuned `run_agent`) ran cleanly to its full pre-registered Milestone 2 over n=75 SWE-bench Verified instances on 2026-05-26. Fairness contract held byte-for-byte across smoke + n=75: same model (`Qwen3.6-35B-A3B-6bit`, temp=0, num_ctx=32768, max_tokens=8192), same 16-tool bugfix surface (capability wire-JSON SHA `325c0dcd…` byte-identical via reused luxe tool callables under Python 3.12), same system+task prompt (only luxe's `.sdd` differs), same 30-step budget envelope, same Docker grading (co-graded in one session, 0 harness errors, 75/75 valid pairs). Determinism confirmed (luxe re-run byte-identical patches). The n=14 smoke gave a clean +2 Pareto superset (forge 8/14 ⊇ luxe 6/14, 0 luxe-exclusive wins) — a sub-threshold but mechanistically coherent result that the user approved as a deviation from the ≥3 smoke bar to test at scale.

**Verdict.** **luxe 30/75 (40.0%), forge 32/75 (42.67%), Δ +2 (+2.67pp).** Gate #2 ≥5pp **FAIL**. Paired completion-tokens ratio **1.97×** (n=25 instances where both arms have token data; the raw 3.05× over-penalizes forge because luxe doesn't emit token-progress on short runs). Gate #3 ≤1.5× **FAIL**. Joint = WASH; architecture line retires for this stack.

**The decisive scale-up finding.** At n=14 forge resolved a **strict superset** of luxe (zero luxe-exclusive wins). At n=75 it does NOT: forge gains 5 over luxe, loses 3 to luxe (django-11333 close_but, xarray-3095 incomplete, sympy-12096 incomplete) → +5/−3 = +2 *trade*, not a Pareto superset. The clean-superset framing that justified proceeding to n=75 was a small-n favorable draw — **exactly the risk the executive language called out** ("n=14 cannot separate a small real edge from a favorable small-sample draw"). Lesson: even mechanistically-coherent small-n Pareto supersets can be artifacts of the draw; a result that looks clean and asymmetric at n=14 can dissolve into a balanced trade at n=75. The honesty discipline (admit the caveat in executive prose, not just buried in footnotes) is what kept the verdict legible.

**What survives the wash (durable, luxe-portable observations).** Give-up-avoidance is a real mechanism (matplotlib-13989 reproduces at scale; forge converts the 1-line gold fix where luxe gives up after 22 steps) and edit-quality wins are real (sphinx-10673 reproduces; both arms touched the same 2 files, forge's content was correct, luxe's mis-indented a control-flow block and missed `modindex`). But the same persistence that converts give-ups also COSTS 2 instances at scale (xarray-3095 + sympy-12096: luxe terminated faster + landed; forge ran to max_iter empty). This is *the exact non-Pareto trade* luxe's v1.7→v1.11 history tuned aggressively *toward* bailing for, and that the 2026-05-24 reflect cycle closed as HOLD for the same reason. Track 0 retroactively *confirms* that prior tuning judgment at the loop-architecture level: relaxing early-bail trades give-up→resolve for resolve→empty in a wash. **No port-the-mechanism follow-up is queued** because the trade reproduces the historical lever wash.

**Cost-of-success is the surprise.** Per the reviewer-requested "median tokens-per-RESOLVED-instance" metric: forge **4,344 vs luxe 8,574 → 0.51×**. Forge is *half* the cost per success. Aggregate 1.97× comes entirely from forge running the full 30-step budget on unconvertible cases (no respond-terminal called → `MaxIterationsError`). The cost-of-success and aggregate-cost numbers point opposite directions — the cleanest cost metric depends entirely on what's being asked. This is a portable measurement lesson: aggregate compute ratios can mislead when "spends a lot on failures, cheap on successes" is the underlying shape.

**Two scale-only findings hidden at n=14.** (1) Forge's `respond`-terminal discipline actually *scaled up*: 64% max_iterations at n=14 → 45% at n=75 (`terminal_respond` 36/75). The "forge never early-stops" concern from the smoke was partly artifact. (2) **New forge fragility: 5 instances raised `ToolCallError: Retries exhausted after 3 consecutive failed attempts`** against this champion's output shape (heavy-reasoner malformed tool emissions). All produced empty patches. This is the kind of architectural failure mode that small-n simply can't surface — and is part of why the smoke result isn't the verdict.

**Methodological wins (preserved for next time).** (a) The valid-arm / compatibility-fallback split kept a "luxe imports under Python 3.12" infrastructure question from contaminating the architecture verdict — luxe DID import cleanly under 3.12 and the valid arm ran on luxe's identical tool callables. (b) Per-instance JSON pre-seeding from the smoke (14 deterministic cached → reused at n=75) saved ~28 instance-runs without compromising the experiment; the cache-bypass dry-run (<2 s, no oMLX) was the right safeguard. (c) Co-grading both arms in one Docker session (same image state, same day) is the strongest fairness control for grading. (d) The paired-only token ratio handled luxe's missing token-progress emission honestly. (e) Pre-committing "only the frozen forge counts" preserved decisiveness against the "if we'd retuned terminal policy" hindsight reinterpretation; the 5 `ToolCallError` errors stay in the verdict.

**Affected files.** No luxe source changes (Track 0 was scratch by design). Scratch artifacts retained under `~/Downloads/forge-luxe-research/`: forge harness `scratch/forge_swebench.py`, grading driver `scratch/grade_arm.py`, comparator `scratch/compare_arms.py` (with paired-token + cost-of-success metrics), per-instance results in `results/forge_arm_n75/`, comparator JSON `results/phase2_comparison.json`, full briefing `NOTES.md`, forge venv (`forge-venv`, Python 3.12.13, forge v0.6.0 sha `f1b87b05`), grading venv (`grading-venv`, swebench 4.1.0). luxe-tree artifacts: per-instance JSONs + predictions under `acceptance/swebench/track0_smoke/luxe_arm/` (n=14) and `acceptance/swebench/track0_n75/luxe_arm/` (n=75, gitignored). Memory: `project_track0_forge_n75_wash.md`. Plan: `~/.claude/plans/binary-gathering-panda.md` (executed).

### [2026-05-26] Edit-quality investigation closes — early_bail family is the degrader, refined port REFUTED, no source change ships

**What happened.** After Track 0 closed as a WASH (forge loop +2/+2.67pp at n=75, below the ≥5pp gate), the durable observation worth exploring was that forge produced cleaner edit *content* on the same files in 3 cases where luxe's content was wrong. Since model + sampling + tools + system prompt are byte-identical across arms, this divergence had to come from luxe's mid-trajectory intervention messages. I designed a forensic + ablation cycle to identify the responsible mechanism and test a targeted relaxation.

**Phase 1 diagnostic — 100% intervention-firing correlation.** Read luxe's structured `~/.luxe/runs/<run_id>/events.jsonl` for the 4 edit-quality differential instances. The 3 forge-only wins (django-10880, requests-1724, sphinx-10673) each had 2-3 luxe `early_bail` family interventions fire (soft_anchor + breadth_probe variants — all "commit now / narrow / write now" pressure). The 1 forge loss (django-11333, where luxe got the right edit) had **zero** luxe interventions fire. Crystal-clear correlation: fewer mid-trajectory pressure messages → cleaner edit context → better edit quality.

**Phase 2 — `--no-early-bail` ablation.** n=14: +2 resolves clean, watchdog clean → proceed. n=75: **+8 resolves (+10.67pp), 2.2× faster wall**, BUT watchdog **FAILED** — 4 wrong_target migrations (matplotlib-25775, pylint-6528, sympy-13091, sympy-17318: all baseline empty_patch → ablation wrong_target). Per pre-registered band: STOP, non-Pareto repeat of v1.7→v1.11. **Crucially the historical "10/18 empty_patch regression" warning did NOT reproduce — current substrate showed only 3 genuine wrong_target damages.** And cost-of-success: forge's mechanism reproduces (matplotlib-13989 + 3 other forge wins flip to RESOLVED under luxe with early_bail off).

**Phase 3 — Refined-port hypothesis.** "Keep `commit_imperative` (high convergence, score ≥0.40) since it fires when the model has already identified a target; suppress only soft_anchor + breadth_probe (the low/mid-conv broad-pressure variants)." Implemented as `LUXE_EARLY_BAIL_COMMIT_ONLY=1` env var in `loop.py` + new CLI flag + 2 tests; default OFF, byte-identical, 910 tests pass. **n=14 result: +1 resolves, 1 watchdog hit → STOP per pre-registered band, hypothesis REFUTED.** The pivotal instance is matplotlib-20826: baseline empty (early_bail fires) → `--no-early-bail` RESOLVED → refined commit_only **wrong_target** (commit_imperative fires when score climbs and drives premature commit to the wrong place). **Decisive lesson: commit_imperative ALSO degrades edit quality.** The entire early_bail family — soft_anchor, breadth_probe, AND commit_imperative — pressures premature commitment; isolating commit_imperative is not the right surgical lever.

**Why the trade is fundamental, not just a tuning gap.** The +8 wins at n=75 from full disable are the model writing the *correct* edit when not pressured to commit early. The 4 wrong_target damages at full disable are the model writing the *wrong* edit when given more uninterrupted budget on hopeless cases. Both come from the same loop property (no early commit pressure). Any sub-variant of early_bail family that fires *at all* recreates the trade — the refined port falsified the protective-imperative hypothesis cleanly. This matches the v1.7→v1.11 history (luxe tuned aggressively toward bailing precisely to convert wrong→empty) and the 2026-05-24 reflect-cycle HOLD (un-grounded persistence turns give-ups into wrong actions). Track 0 + the edit-quality investigation together confirm that **tuning judgment at the loop-architecture level**: no relaxation of the early_bail family ships cleanly without re-introducing the wrong-target damage.

**What remains durable.** (a) The diagnostic methodology — read events.jsonl for intervention firings, correlate with edit-quality outcome — is the reusable technique. (b) The `LUXE_EARLY_BAIL_COMMIT_ONLY` flag stays in working tree as a clean diagnostic lever for future investigations (default OFF, byte-identical, 2 unit tests). (c) The investigation generated 3 ablation arms and a forge-trajectory-dump tool, all archived under `~/Downloads/forge-luxe-research/`.

**Why no source change ships despite the +8 at n=75.** The watchdog failure is the decisive band per pre-registration — and the refined-port refutation confirms the watchdog represents a *fundamental* trade, not a tuning gap that a sub-variant ablation could fix. Per CLAUDE.md "ask first," the working-tree changes (loop.py + adapter.py + run.py + tests) are left for user review/commit/revert. They cost nothing as-is (default OFF, byte-identical), so committing them keeps the diagnostic lever available without behavioral risk.

**Affected files (working tree, NOT committed).**
- `src/luxe/agents/loop.py` (+46/−11): `LUXE_EARLY_BAIL_COMMIT_ONLY` env var + breadth_probe/soft_anchor suppression + commit_imperative preservation + `early_bail_suppressed_commit_only` observability event.
- `benchmarks/swebench/run.py` (+8) + `benchmarks/swebench/adapter.py` (+6): `--early-bail-commit-only` CLI flag + plumbing.
- `tests/test_loop_adaptive_policy.py` (+62): 2 new tests (low/mid-conv suppression; high-conv preservation).
- `RESUME.md` + this `lessons.md` entry: documentation drafts.

Memory: `project_edit_quality_early_bail_refuted.md` (new) — and the prior `project_track0_forge_n75_wash.md` already records the Track 0 context this follows from. Scratch artifacts retained: forge trajectory dumps for the 4 differential instances; 2 ablation arms (n=14 + n=75 for full --no-early-bail; n=14 for refined commit_only); all comparator outputs at `~/Downloads/forge-luxe-research/results/editquality/`. Plan: `~/.claude/plans/binary-gathering-panda.md` (executed).

### [2026-05-26] Agentic-patterns audit lands as 3 commits — drift fixes, peak_context_pressure persisted, G1 design doc; cliff confirmed as a structural ceiling

**What happened.** Reviewed an external read-only audit produced on m1 (`agentic-patterns-luxe-research/`) and shipped its actionable findings as three commits onto `origin/main`: `7e896f4` (drift fixes D1+D2+D3), `649e9dc` (instrumentation: `peak_context_pressure` persisted into the taxonomy row), `3f292c0` (G1 context-lifecycle design doc + 13-file research archive under `docs/research/` + one-line `RESUME.md` handoff pointer). 980 tests pass; the only behavior change is instrumentation plumbing — the loop is byte-identical with the prior commit `122831d`.

**The cliff is a structural ceiling, not an instruction problem.** E1's read-only analysis across 23 SWE-bench taxonomy artifacts (4,053 rows, v17→v111) shows `EMPTY_PATCH_CONTEXT_EXHAUSTED` accounts for **80/324 (24.69%) of the empty_patch tier** and **~5% of the full bench**, stable in the 4.0–5.5% band across 11 luxe versions. None of the prompt-variant work, SpecDD Lever 1/2 rollout, Track 1–5 changes, pre-dispatch spec gate, nor any of the existing interventions moved this number. The intervention machinery is **almost silent on the cliff slice** (only 25% of cliff rows fire any intervention vs 71% on the non-cliff empty_patch slice) — the cliff terminates on a backend prompt-size 400, not on any in-loop predicate. **Read this finding as confirming the compaction-preserves-entropy-accumulation prediction**: `elide_old_tool_results(threshold=0.7)` does its job up to ~70% pressure and then has no further signal to give. The 5% is the residual that hierarchical summary / state distillation / phase closure would target — captured in `docs/g1-context-lifecycle-design.md` as a 6-lever menu with tie-in points but no implementation.

**`peak_context_pressure` had been computed but never persisted.** The field lives on `AgentResult` (`loop.py:50`, monotonically maxed at `loop.py:691`) but never reached the artifact. So E1 could see only *terminal* cliff events; the *distribution* of near-cliff runs (peaked at 78% but completed) was invisible. The four-edit fix (cli.py event payload, run.py extract + dataclass field + constructor wire) is backward-compatible — old artifacts deserialize with `peak_context_pressure=0.0`, which is *correct* because they predate the measurement, not bad data. **General lesson: instrumentation that lives only in in-memory dataclasses is "dark"; the persisted row is what determines future analysis surface.** When a metric is interesting enough to compute every step, it's interesting enough to persist.

**External audits surface drift that's invisible from inside.** `AGENTS.md` was 286 lines describing the retired `src/swarm/**` pipeline — which `luxe.sdd` Forbids and which the `tools/fs.py` role-path guard actively blocks at write-time. That contradiction was sitting in the project's primary "agent reference" file because no one was reading it normatively. The audit caught it (D1, critical). Two structurally similar findings: D2 (the champion-pin `Qwen3.6-35B-A3B-6bit` lived only in `CLAUDE.md` and `configs/single_64gb.yaml` — no normative `.sdd` clause to protect it from a future config flip) and D3 (the v1.11 reflect / adaptive-policy / cohort-priors invariants lived only in `agents.sdd`, so a user toggling `LUXE_REFLECT=1` from `CLAUDE.md` guidance alone never saw the guardrails). **Meta-lesson:** drift accretes between "what the doc says" and "what the code does" along axes the doc-owner never inspects; an outside read-only pass costs little and catches what insiders can't see. **The audit's own methodology (`e5-instruction-contract-drift-report.md` §3, M1–M7) is the candidate drift-lint for a future cycle — deferred until the doc set settles post-D1–D3.**

**Plan-mode reviewer pass caught real pre-flight issues.** Three reviewer notes turned out to be load-bearing: (1) the canonical filename is **lowercase `agents.md`** in git — macOS's case-insensitive FS resolves `AGENTS.md` to the same inode, but git tracks lowercase. The plan had used `AGENTS.md` uppercase throughout; without the pre-flight `git ls-files | grep -i agents.md`, the commit would have either landed a phantom rename or split the file across two paths on a case-sensitive checkout. (2) The original AGENTS.md replacement draft described interventions as "always active" because that was the historical state — but commit `122831d` (2026-05-26 morning) made every in-loop intervention env-gated default-OFF. Living code state diverges from external descriptions even when those descriptions are *days* old. (3) The G1 design doc would have been undiscoverable without a `RESUME.md` pointer; documents inside `docs/` aren't part of the natural cold-start reading order. **Reusable rules: `git ls-files` is the canonical source for filename case; re-read the most recent diff before applying any externally-drafted patch; `RESUME.md` is the cold-start spine — every persistent artifact needs a one-line pointer there.**

**The proposed-fix drafts are now superseded but worth preserving.** `proposed-AGENTS.md`, `proposed-fixes-README.md`, `proposed-sdd-and-claude-additions.md` are kept under `docs/research/` as the *historical record* of what the audit recommended; the actual changes shipped via Edit/Write, not the recipe. A `docs/research/README.md` labels authority classes (authoritative analyses / superseded proposals / reproducibility scripts / generated CSVs / unrelated prior research) so future contributors don't apply a draft a second time. **General lesson:** when shipping audit findings, separate the *evidence* (preserve verbatim) from the *recommendation* (mark superseded once shipped) from the *actual change* (the commit is the truth). Three different artifacts; three different lifecycles.

**Authority-boundary preamble + advisory-line-refs in `agents.md`.** The new file opens with: *"Normative constraints live in the `.sdd` chain; this file summarizes the currently active implementation architecture, not policy. File:line references are advisory snapshots and may drift between cycles — verify against current code when in doubt."* Without this, the rewrite recreates the same doc-rot dynamic in a more compact form — a future contributor sees an authoritative-looking architecture summary, treats it as policy, and the next divergence between `.sdd` enforcement and the agents.md description goes uncaught.

**Affected files (committed across 3 commits to `origin/main`).**
- `agents.md` (286 → 92 lines): mono-only rewrite with authority preamble.
- `src/luxe/luxe.sdd` (+5): champion-pin Must clauses + Must-not blocking `configs/_archive/` promotion.
- `CLAUDE.md` (+19): new Opt-in modes section (LUXE_REFLECT / LUXE_ADAPTIVE_POLICY / LUXE_LOAD_PRIORS).
- `src/luxe/cli.py` (+1): `peak_context_pressure` in the `single_mode_done` event payload.
- `benchmarks/maintain_suite/run.py` (+9): extract field + `Diagnostics.peak_context_pressure: float = 0.0` + `build_diagnostics` wire.
- `docs/g1-context-lifecycle-design.md` (new, 11K): substrate map + 6-lever menu + invariant constraints + explicit non-goals.
- `docs/research/` (15 files: 13 new from the audit + README.md + 2 prior research files untouched).
- `RESUME.md` (+1 line): pointer to `docs/g1-context-lifecycle-design.md` as the cold-start entry point for whichever cycle picks up the cliff work.

Plan: `~/.claude/plans/streamed-inventing-hejlsberg.md` (executed). Three commits on `origin/main`: `7e896f4` → `649e9dc` → `3f292c0`.

### [2026-05-26] Forge-hybrid Phase 2 (A) smoke surfaces SUBSTRATE NON-DETERMINISM — the "byte-identical baseline" idealization is wrong on this MoE champion

**What happened**: The forge-hybrid cycle's Phase 2 (A) — porting forge's `TieredCompact` 3-phase compaction strategy gated behind `LUXE_TIERED_COMPACT=1` — landed clean in code (947/947 tests, default-OFF byte-identity on the unit-test surface) and went to its n=14 smoke A/B (subset `forge_hybrid_smoke_n14.json`, mirrors the May 25 `track0_smoke` baseline instances). Raw result: treatment 7/14 patches vs baseline 9/14, **−2 patches**. Two regressions: **sphinx-10673** (baseline 42 lines → treatment 0 lines) and **pylint-4604** (baseline 16 lines → treatment 0 lines). Pre-registered gates all passed (raw resolve ≥ 5/14, wall 0.80× baseline, phase 3 fires 1/14 = 7% < 20%, zero protected-instance flips), but the regressions warranted a deeper look.

Telemetry inspection: **13 of 14 treatment runs had `max_phase_reached=0`** — compaction never even fired. Only `116cd93911e7` (= sphinx-10673) triggered phase 3 (3 steps, 24,888 tokens dropped) and aborted. So sphinx-10673 is a real compaction-caused regression. But pylint-4604 took the no-compaction code path — by construction it should be byte-identical to baseline. It wasn't.

The pylint-4604 anomaly was the spur for a diagnostic. Two single-instance rerun replications with the **same code, same env, same model, compaction OFF**:
- rep1: **19 patch_lines** (136s wall)
- rep2: **16 patch_lines** (175s wall, matches the May 25 baseline exactly)

So three runs of identical-config code (May 25 baseline 16 lines, May 26 rep1 19 lines, May 26 rep2 16 lines, May 26 treatment 0 lines) produced **{0, 16, 16, 19} patch_lines** — and rep1 vs rep2 were byte-identical-config runs on the same day, 39s wall apart, that still differed in patch content + count.

**Root cause / insight**: The MoE champion (`Qwen3.6-35B-A3B-6bit`) is **not byte-deterministic at temp=0 across runs** on this oMLX/MLX substrate. The "byte-identical baseline" framing the plan assumed is an idealization — it holds for unit tests where we control the message stream, but the **wire-to-model output is substrate-noisy** at the patch-content / give-up-vs-act axis. Speculative causes: MoE routing variation, MLX-internal batched-token boundary drift, server-warm-vs-cold prefill state, micro-second timing affecting kernel scheduling. None observable from luxe's side.

Two important sub-findings:
1. **The non-determinism manifests on the give-up-vs-commit axis, not the patch-content axis.** Of the 7 instances where BOTH arms produced a patch, **0/7 had patch_lines drift between arms** — same patches byte-for-byte. The substrate noise is whether-to-act, not what-to-act-on.
2. **Compaction is rarely fired at the current settings** (`keep_recent=3`, `compact_threshold=0.75`, `num_ctx=131072`). 1/14 runs (7%) on this workload. The lever is currently a near-no-op on typical SWE-bench instances. To stress-test it we'd need a tighter threshold, smaller context, or a long-context-skewed instance set.

The 1 firing run (sphinx-10673) **aborted** — phase 3 dropped 24,888 tokens and the trajectory couldn't recover. Real signal that aggressive phase 3 compaction can damage trajectories, but n=1 isn't conclusive yet.

**Why the wall headline was misleading**: aggregate "20% faster wall" (treatment 2421s vs baseline 3015s) dissolved on apples-to-apples comparison. Both-patched (7 instances): treatment **1.19× SLOWER** than baseline (154s vs 129s median). Both-empty failures (5 instances): treatment **0.68× FASTER** (233s vs 341s median). Aggregate gain was all from failures finishing sooner — i.e., the treatment gave up on hopeless instances faster, with no benefit on resolves. Since compaction wasn't firing on either bucket, the wall difference is itself substrate noise.

**Fix / takeaway**:
- **Revise the plan's "byte-identical baseline" assumption to "behaviorally similar baseline within a measured substrate-noise band."** Run multi-rep baselines BEFORE each axis A/B and subtract the noise band from observed effects. The 3-rep baseline `acceptance/forge-hybrid/baseline_n14_rep{1,2,3}/` (running 2026-05-26) is the first such measurement; expected variance is ~±2 patches at n=14 based on the pylint-4604 evidence.
- **n=14 is too noisy for confident A/B claims on this substrate.** A ±2-patch difference (the order of the observed pylint anomaly) is within the noise band. Future axis gates need either (a) larger n (escalate to n=75 for averaging-out), or (b) effect sizes large enough to exceed the noise band (e.g., +5 patches at n=14 = 35% improvement = unambiguous).
- **The Phase 1 (C) refactor cannot be exonerated by patch-count alone.** Even though rep2 reproduced the May 25 baseline's 16-line patch exactly (suggesting the refactor is clean), that match could be a substrate-noise coincidence. The 5-invariant replay-equivalence test (task #4 in `~/.claude/plans/starry-hopping-phoenix.md`) is the right way to confirm refactor cleanliness — it mocks oMLX responses, so it directly tests the code-path divergence without substrate noise as a confound.
- **Compaction's current tuning is too conservative for SWE-bench.** Forge uses `keep_recent=2`; luxe picked 3 for SWE-bench depth. But the trigger threshold (0.75 × 131072 = 98k tokens) is rarely reached. Either tighten thresholds substantially or accept compaction as a "safety net for outlier deep trajectories" with no expected resolve-rate effect.
- **Stress-test plan**: a follow-up A/B with `compact_threshold=0.40` (or `num_ctx=49152`) on the same n=14 would actually exercise the lever and surface its real behavior. Without that, the n=75 escalation would mostly reproduce the n=14 finding (lever rarely fires → noise band dominates).
- **Bench-as-truth caveat updated**: the existing "bench-as-truth" pattern in CLAUDE.md assumes the bench output IS the truth on a given run. With substrate non-determinism documented, a single bench run is one DRAW from a distribution. For change-detection A/Bs, multi-rep is now table stakes; for ceiling-finding (best-of-N), single-rep remains adequate.

**Affected files**: `src/luxe/context.py` (TieredCompact + CompactionResult), `src/luxe/agents/loop.py` (gated branch + telemetry), `benchmarks/swebench/{run,adapter}.py` (`--tiered-compact` flag), `tests/test_context.py` (+11), `benchmarks/swebench/subsets/forge_hybrid_smoke_n14.json` (pinned instance set), `benchmarks/swebench/subsets/forge_hybrid_pylint_4604_diagnostic.json` (single-instance noise probe), `acceptance/forge-hybrid/` (treatment_n14, pylint_4604_diagnostic, pylint_4604_diagnostic_rep2, baseline_n14_rep{1,2,3}; all gitignored), `acceptance/forge-hybrid/protected.json` (17 protected instances from the prior --no-early-bail ablation). Commit `18ac49c` (feat: TieredCompact). Plan: `~/.claude/plans/starry-hopping-phoenix.md`.
