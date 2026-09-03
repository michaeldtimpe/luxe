# K2-Horizon-3.7B probe on neo — command log (2026-09-03)

Workspace: ~/k2probe. Production router (:8080) was DOWN at start (no process,
LaunchAgent not loaded — known BTM gap). Probe server will use :8081.

## Step 0 — scout
```
uname -a                      # Darwin 25.6.0 arm64, macOS 26.6.2 (25G83)
df -h /Users/mtimpe           # 350Gi avail
pgrep -fl llama-server        # (nothing)
git -C ~/Downloads/luxe log --oneline -1   # 59b9f18
grep GGML_ ~/code/llama.cpp/build/CMakeCache.txt
# -> Release, GGML_METAL=ON, GGML_METAL_EMBED_LIBRARY=ON, GGML_ACCELERATE=ON,
#    GGML_BLAS=ON/Apple, GGML_NATIVE=ON, BUILD_SHARED_LIBS=ON, LLAMA_CURL=ON
```
luxe binary on neo: ~/Downloads/luxe/.venv/bin/luxe (per ~/dotfiles/bin/luxe-chat).

## Step 1 — workspace
```
mkdir -p ~/k2probe/artifacts
```

## Step 2 — build the IFM fork
```
git clone --depth 1 -b model/K2Horizon https://github.com/MBZUAI-IFM/llama.cpp ~/k2probe/llama.cpp
# HEAD 35999d101cf2233fc54f09c3c8d599da7303ce02  Tue Sep 1 18:04:20 2026 +0000
#      "model: K2 Horizon chat template and accomodate safetensors naming"
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON \
  -DGGML_ACCELERATE=ON -DGGML_BLAS=ON -DGGML_BLAS_VENDOR=Apple -DGGML_NATIVE=ON \
  -DBUILD_SHARED_LIBS=ON -DLLAMA_CURL=ON
cmake --build build --config Release -j6 --target llama-server llama-quantize llama-cli
# -> clean build, 0 errors, ~5 min. build string b1-35999d1.
```
Production ~/code/llama.cpp and ~/.local/bin/llama-server NOT touched.

## Step 3 — download + quantize
```
hf download IFM/K2-Horizon-3.7B-GGUF K2-Horizon-4B-BF16.gguf --local-dir ~/k2probe/models
# 10,128,343,424 B, wall < 60s (unauthenticated)
~/k2probe/llama.cpp/build/bin/llama-quantize \
   ~/k2probe/models/K2-Horizon-4B-BF16.gguf \
   ~/k2probe/models/K2-Horizon-3.7B-Q4_K_M.gguf Q4_K_M 6
# quant size 2999.44 MiB (4.97 BPW), 58.2 s. On disk 3,156,598,144 B.
rm -f ~/k2probe/models/K2-Horizon-4B-BF16.gguf   # BF16 deleted immediately
```
GGUF meta: n_params 5.06e9, n_vocab 250624, n_embd 2560, n_ctx_train 524288.

## Step 4 — sanity (llama-cli)
neo has no coreutils `timeout`; wrote ~/k2probe/tmo as a stand-in (used for every
capped command below). `-no-cnv` does not exist in this build; used `-st`.
```
~/k2probe/tmo 420 ~/k2probe/llama.cpp/build/bin/llama-cli \
  -m ~/k2probe/models/K2-Horizon-3.7B-Q4_K_M.gguf -ngl 99 -c 4096 -st -n 400 \
  --temp 0.7 --top-p 0.95 -p "What is the capital of France? Answer in one sentence."
# coherent. Emits [Start thinking]...[End thinking] then the answer.
# Prompt 69.3 t/s | Generation 18.4 t/s
```

## Step 5 — probe server (:8081), Arm A = reasoning minimal
Fork `llama-server --help` has native --reasoning [on|off|auto],
--reasoning-effort, --reasoning-budget, --reasoning-format. Chose `--reasoning off`
(cleanest knob; no chat_template_kwargs plumbing needed, which matters because
luxe has none).
```
# ~/k2probe/k2-models.ini = production [*] block verbatim + one section
~/k2probe/llama.cpp/build/bin/llama-server --models-preset ~/k2probe/k2-models.ini \
  --port 8081 --host 127.0.0.1 --models-max 1 --reasoning off &   # -> server.log
curl -s 127.0.0.1:8081/v1/models     # K2-Horizon-3.7B, status loaded, n_ctx 16384
```
Router propagates `--reasoning off` to the child worker (visible in status.args).
RSS (child worker, holds the weights): 4,411,888 kB idle -> 4,487,936 kB after
one tool call. (Production champion is 3.66 GB; K2 is ~0.75 GB heavier.)

Raw /v1/chat/completions with one tool definition, temp 0, max_tokens 2048:
  finish_reason = "tool_calls"; content ""; STRUCTURED tool_calls with a proper
  id and JSON arguments {"path":"."}; 18 completion tokens; NO reasoning leak.
  timings: prompt 152 t/s, predict 16.4 t/s.
Plain call (no tools): finish "stop", reasoning_content None, 23 tokens, one
  correct sentence. Arm A confirmed short + direct.

## Step 6 — luxe drills, Arm A
```
sed -e "s|127.0.0.1:8080|127.0.0.1:8081|g" -e "s|Qwen3-4B-Instruct-2507|K2-Horizon-3.7B|g" \
    ~/dotfiles/luxe/neo.yaml > ~/k2probe/k2.yaml     # only those two substitutions
```
First `luxe ready` -> exit 1, single FAIL `weights:K2-Horizon-3.7B main · not in
the store`. luxe checks ~/.omlx/models/<id>/; neo keeps POINTER entries there for
its GGUFs (config.json + symlink) — the champion has one. Made the matching probe
entry so the check is apples-to-apples (REMOVED at cleanup):
```
mkdir -p ~/.omlx/models/K2-Horizon-3.7B
ln -sf ~/k2probe/models/K2-Horizon-3.7B-Q4_K_M.gguf ~/.omlx/models/K2-Horizon-3.7B/
sed s/Qwen3-4B-Instruct-2507/K2-Horizon-3.7B/g <champion config.json> > .../config.json
```
```
LUXE_CONFIG=/Users/mtimpe/k2probe/k2.yaml luxe ready         # exit 0, READY (warnings), 1s
LUXE_CONFIG=/Users/mtimpe/k2probe/k2.yaml luxe smoke         # exit 0, READY, 4s wall
LUXE_CONFIG=/Users/mtimpe/k2probe/k2.yaml luxe smoke --chat --code   # exit 0, 38s
```
The smoke drills delete the scratch repo on success, so re-ran the same two drill
functions with the repo PRESERVED (~/k2probe/drill_keep.py, monkeypatches
_make_drill_repo + shutil.rmtree; no change to luxe on disk) to read the evidence.

### Arm A results (reasoning off)
`luxe ready` exit 0 (1s) · `luxe smoke` exit 0 (4s, every line ✓) ·
`luxe smoke --chat --code` exit 0, 38s wall (chat 2 steps/1 call/7s;
code 7 steps/8 calls/30s; tests green; diff = exactly calc.py).
Preserved re-run (27s): chat 2.3s, code 24.2s, same passes.
EVIDENCE READ BY HAND on the preserved repo:
  git diff = one line, `return a - b  # planted bug` -> `return a + b`.
  test_calc.py untouched (diff --stat: 1 file, +1/-1). pytest -q: 2 passed.
  => a REAL fix, not a thin/vacuous one.
Telemetry (~/.luxe/runs/smoke-code/events.jsonl, Arm A window):
  8 tool calls / 7 steps: step0 bash + read_file(test_calc.py) [parallel],
  step1 bash, step2 read_file(calc.py), step3 edit_file(calc.py),
  steps 4-6 bash (verification).
  post_write_idle_exit step=6 idle_tools=3 writes_seen=1 — the run ends on the
  idle gate after the write, not on the model concluding.
  tool_reject 0 · textfallback_drop 0 · terminal_turn_truncated 0 · turn_error 0
  aborted false · compaction max_phase_reached 0 (never fired; 16k was ample)
  3 of the 5 bash calls returned bytes_out=0.

## Step 7 — Arm B (reasoning high, unbudgeted)
```
pkill -f "port 8081"
~/k2probe/llama.cpp/build/bin/llama-server --models-preset ~/k2probe/k2-models.ini \
  --port 8081 --host 127.0.0.1 --models-max 1 \
  --reasoning on --reasoning-effort high --reasoning-budget -1 &   # server-armB.log
sed "s/max_tokens_per_turn: 2048/max_tokens_per_turn: 8192/" ~/k2probe/k2.yaml \
    > ~/k2probe/k2-high.yaml     # num_ctx already 16384
```
Used the fork's native --reasoning* flags rather than --chat-template-kwargs:
they exist in this build and luxe has no chat_template_kwargs plumbing.
Raw probe: thoughts land in `message.reasoning_content` (deepseek format),
`content` stays clean — 175 completion tokens vs 23 in Arm A for the same
one-sentence question (7.6x).

`luxe smoke --chat --code` exit 0, 39s wall (chat 2 steps/1 call/10s;
code 6 steps/8 calls/28s). Preserved re-run 28s (chat 5.6s, code 22.8s).
EVIDENCE: git diff byte-identical to Arm A (one line, a - b -> a + b),
test_calc.py untouched, pytest 2 passed.
Telemetry: 8 tool calls / 6 steps: step0 bash + glob, step1 read_file(calc.py) +
read_file(test_calc.py), step2 edit_file, steps 3-5 bash.
  post_write_idle_exit step=5 idle_tools=3 writes_seen=1.
  tool_reject 0 · textfallback_drop 0 · terminal_turn_truncated 0 · aborted false
  compaction max_phase_reached 0.
VERDICT: reasoning=high bought nothing on this drill and cost nothing measurable
in wall (39s vs 38s end-to-end; 28s vs 30s on the code agent — noise). It reached
the edit one step sooner. The 7.6x token cost is real but only visible on
conversational turns; the agentic drill is tool-latency bound.

## Step 8 — cleanup
```
pkill -f "port 8081"
cp <all logs/configs> ~/k2probe/artifacts/
rm -rf $TMPDIR/luxe-{chat,code}-drill-*      # the 4 preserved scratch repos
rm -rf ~/.omlx/models/K2-Horizon-3.7B        # the probe pointer entry
rm -rf ~/k2probe/models ~/k2probe/llama.cpp
```
Verified after cleanup:
  ~/k2probe = 452K (artifacts + logs only); disk back to 350Gi avail
  ~/.local/bin/llama-server -> ~/code/llama.cpp/build/bin/llama-server, mtime
    May 8 19:10 — UNCHANGED (never rebuilt, never relinked)
  ~/.omlx/models/ = Qwen3-4B-Instruct-2507 only
  ~/models/ = Qwen3-4B-Instruct-2507-Q4_K_M.gguf only
  git -C ~/dotfiles status --short  -> ` M pulsar/config.cson` ONLY, which was
    already modified before this probe started. dotfiles/luxe/ is clean.
  git -C ~/Downloads/luxe status --short -> clean, still 59b9f18

Production router restarted:
```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.micromind.llama-server.plist   # rc=0
curl 127.0.0.1:8080/v1/models   # Qwen3-4B-Instruct-2507 loaded
LUXE_CONFIG=~/dotfiles/luxe/neo.yaml luxe ready   # exit 0, READY (warnings)
```
