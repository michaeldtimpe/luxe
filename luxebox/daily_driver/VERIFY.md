# Verification — local Qwen-Coder-32B driving Claude Code

Run in order. Each step should take < 2 min once the first model load finishes.

## 0. Prereqs

- `uv` installed (`brew install uv`)
- `uv sync --extra mlx --extra evalplus` has run at least once in this project
- `gh auth status` shows you're logged in
- `~/.local/bin` is on your PATH (installer warns if not)

## 1. Install the stack

```bash
cd /Users/michaeltimpe/Downloads/luxebox
bash daily_driver/install.sh
```

Expected: logs dir created, two launchd plists bootstrapped, `claude-local`
symlinked into `~/.local/bin`, `~/.claude/settings.json` written (existing
one backed up with timestamp).

## 2. Confirm the agents are running

```bash
launchctl print gui/$(id -u)/com.luxebox.mlx     | grep -E 'state|program'
launchctl print gui/$(id -u)/com.luxebox.litellm | grep -E 'state|program'
```

Both should show `state = running`. If one shows `state = waiting` or
`state = exited`, tail its stderr:

```bash
tail -n 80 ~/Library/Logs/luxebox/mlx.err.log
tail -n 80 ~/Library/Logs/luxebox/litellm.err.log
```

## 3. Health-check the endpoints

```bash
curl -sf http://127.0.0.1:8088/v1/models | head -c 400  # mlx-lm (OpenAI shape)
curl -sf http://127.0.0.1:4000/v1/models | head -c 400  # litellm (Anthropic)
```

Both should return JSON listing available model(s) and exit 0. Cold start
takes ~30s after first boot.

## 4. End-to-end via `claude-local`

```bash
cd personal_eval/cache/michaeldtimpe_flying-fair
cp ../../../daily_driver/repo_context/flying-fair.md ./CLAUDE.md
claude-local
```

Inside Claude:

- `/model` should list `qwen-coder-local` (or whatever
  `ANTHROPIC_CUSTOM_MODEL_NAME` says).
- Ask: *"Read README.md, summarize in three bullets, then propose a
  one-line edit that would improve discoverability."*
- Confirm the assistant (a) reads the file, (b) returns a coherent
  summary, (c) proposes a concrete edit.

Check the translation is flowing:

```bash
tail -f ~/Library/Logs/luxebox/litellm.err.log
```

You'll see an incoming `/v1/messages` request, translated to
`/v1/chat/completions`, and the qwen response translated back.

## 5. Prompt-on-down path

Simulate bridge down:

```bash
launchctl bootout gui/$(id -u)/com.luxebox.litellm
claude-local
```

Expected: script prints bridge status, options `[s]/[c]/[a]`.

- Choose `s`: kickstarts the agents, waits for health, exec's into `claude`.
- Choose `c`: unsets `ANTHROPIC_BASE_URL` for this session, uses cloud Claude.
- Choose `a`: exits.

Re-bootstrap the agent afterward:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.luxebox.litellm.plist
```

## 6. Reboot test

Restart the Mac. After login, open a terminal and run `claude-local` in
any directory. Health check should pass without manual start.

## 7. Cloud fallback is unchanged

`claude` (no suffix) should still hit cloud Claude normally with your
existing `ANTHROPIC_API_KEY`. To override in-session:

```bash
unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN
claude
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `claude-local`: command not found | `~/.local/bin` not on PATH | `export PATH="$HOME/.local/bin:$PATH"` in `~/.zshrc` |
| mlx agent stuck in `waiting` | uv or python path wrong for launchd | Check plist PATH; `which uv` should return `/opt/homebrew/bin/uv` |
| litellm agent exits immediately | Version pin blocked malware releases but none resolved | Loosen the pin in plist to `litellm[proxy]>=1.83.0`; do NOT allow 1.82.7/1.82.8 |
| Claude Code shows "API error" | LiteLLM routing bug or qwen format mismatch | Tail `~/Library/Logs/luxebox/litellm.err.log`; check `drop_params: true` is still in `litellm_bridge.yml` |
| Tool calls silently dropped | mlx-lm emits Hermes JSON LiteLLM doesn't recognize | Known edge case for complex nested schemas; if hit, report the schema + log entry |
| Model picker doesn't show `qwen-coder-local` | `ANTHROPIC_CUSTOM_MODEL_OPTION` not picked up | Re-launch `claude-local` with a fresh shell; env vars from `~/.claude/settings.json` load on startup |

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.luxebox.mlx
launchctl bootout gui/$(id -u)/com.luxebox.litellm
rm ~/Library/LaunchAgents/com.luxebox.mlx.plist
rm ~/Library/LaunchAgents/com.luxebox.litellm.plist
rm ~/.local/bin/claude-local
# restore your previous settings if the installer backed one up:
ls ~/.claude/settings.json.backup.*
```
