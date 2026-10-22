# Model-provenance and host hardening (2026-10-22)

These are the remaining five items from the review of the local-AI security video. The first two (secret scrub,
redaction and egress refusal) are in `acceptance/secret_scrub_ab_2026_10/`. None of this touches the benchmark path,
so there was no A/B. Verification was the full suite and CI, plus `luxe ready`, `luxe smoke` and
`luxe smoke --chat --code` on all three hosts.

## luxe changes

| PR | Change |
|---|---|
| #49 (`8b2daf9..26761a2`) | **Chat-template pinning and lint**, plus the **publisher allowlist** for `luxe pull` and `/pull` |
| #48 (`69cb1a1`) | `luxe smoke` ping: reasoning-only output counts as alive (found while verifying the oMLX upgrade) |

**Template pinning and lint:**
- **Pins:** `~/.luxe/model_pins.json` (per host) records each manifest model's template hash, source and publisher.
  `luxe pull` writes it, and `luxe pull --pin <m>` and `--show-template <m>` are new.
- **Checks:** `/doctor`, `luxe ready` and `luxe smoke` show one `template:<id>` line per model. It is OK when pinned
  and WARN when unpinned, untrusted, lint-flagged or "TEMPLATE CHANGED ON DISK". It never FAILs, so the kit still
  starts during an outage.
- **Lint:** flags URLs and message-content-versus-literal conditions outside the standard think, tool-response and
  `/nothink` parsing.
- **GGUF (neo):** a stdlib reader takes the template from the file the router loads, found via `models_preset:` in
  the backend entry.

**Publisher allowlist:**
- **Config:** `trusted_publishers:` in `configs/chat.yaml`.
- **Refusal:** a pull from any other HF org is refused, with exit 2, unless `--allow-untrusted` is passed. The pin then
  records `trusted: false` and doctor keeps warning.
- **Unknown publishers:** mount copies with no org in their path are allowed, with the publisher recorded as unknown.

**Pins recorded:**

| Host | Model | Template sha | Publisher |
|---|---|---|---|
| m1 | Qwen3.6-35B-A3B-4bit, Qwen3.6-27B-4bit, Qwen3.6-35B-A3B-6bit | `6f15447a` | mlx-community |
| m5 | Qwen3.6-35B-A3B-6bit, Qwen3.6-27B-6bit | `6f15447a` | mlx-community |
| m5 | GLM-4.5-Air-4bit | `20127c0e` | mlx-community |
| neo | Qwen3-4B-Instruct-2507 (GGUF) | `da84d088` | unsloth; the GGUF's own `general.quantized_by` says Unsloth, with base `Qwen/Qwen3-4B-Instruct-2507` |

Every pinned template was lint-clean. The `chat_template.jinja` hashes match the earlier hand audit (Qwen3.6
`e84f32a2`, GLM `44f81586`).

## Host changes

**m5 oMLX 0.7.0 → 0.7.2:**
- Upgraded with `brew upgrade omlx` plus a restart.
- 0.7.2 returns a thinking model's tokens in `reasoning_content`, which broke plain `luxe smoke`'s 16-token ping on
  m1 and m5. That is fixed in #48.
- The ping was already hitting its cap on older builds; it passed only because reasoning leaked into `content`.
- The `--chat --code` drills skip this ping, which is why it went unnoticed.

**m5 network exposure:**
- **Before:** `~/.omlx/settings.json` had `host 0.0.0.0` and `cors_origins ['*']`, with the macOS firewall off. The
  server was reachable on the LAN at 192.168.100.85:8000 (key-protected).
- **Now:** `host 127.0.0.1` and `cors_origins []`, plus `tailscale serve --bg --tcp 8000 tcp://127.0.0.1:8000`.
  Tailnet clients keep the same URL (`http://m5.example.ts.net:8000`); LAN connections are refused.
- **Verified from m1:**
  - `/health` 200, `/v1/models` 401 without a key, `/admin` 200.
  - LAN closed.
  - `luxe smoke --backend m5` READY, and a real `/backend m5` chat turn.
- **Undo:**
  - restore `~/.omlx/settings.json.bak-2026-10-22`;
  - `tailscale serve --tcp=8000 off`;
  - `brew services restart omlx`.

**neo llama-server API key (dotfiles `219b9ee`, `961da5d`):**
- **Key file:** `~/.config/llama-server/api-keys` (0600). It goes to the router through `--api-key-file` in the
  LaunchAgent plist, and to every per-model child through `api-key-file` in `[*]` of `neo-models.ini`.
- **Why the children too:** they listen on their own random loopback ports and were open.
- **luxe side:** luxe reads the key as `LLAMA_API_KEY_NEO` from `~/.luxe/secrets.env`.
- **Verified:**
  - generating endpoints (`/v1/chat/completions`, `/completion`, `/props`, `/slots`, `/models/load`) return 401
    without the key, on both the router and a child;
  - `/health` and `/v1/models` stay public by llama-server design;
  - luxe ready and smoke are READY.

## Not changed, flagged

- **The abliterated 235B on m5 is in use.** The separate `~/frontiers` project serves it with llama-server on
  127.0.0.1:8081, and its agent app gives the model tools. That is the untrusted-weights-plus-tools combination the
  video warns about. Its bind is loopback, so there is no network exposure; this sits outside luxe.
- **CI flake:** one PR #49 run failed because the GitHub runner's preinstalled Microsoft apt repo returned 403 during
  `apt-get update`. The rerun passed. If it recurs, drop that source before `apt-get update` in the workflow.
