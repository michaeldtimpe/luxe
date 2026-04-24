# Daily driver configs

Run the winning local model behind an `mlx-lm` OpenAI-compat server, then pick
the client you want on top. Three supported paths, from most-to-least
recommended for a local coder:

## 1. Aider (recommended)

Native local-model support, git-native editing, tolerant diff format.

```bash
# Terminal 1 — server
bash daily_driver/start_mlx_server.sh

# Terminal 2 — Aider inside a repo
cd ~/path/to/your-repo
uvx aider --config /Users/michaeltimpe/Downloads/luxe/daily_driver/aider.conf.yml
```

GitHub ops happen through Aider's built-in `gh` integration plus direct `gh`
CLI use from the shell. For review replay we use the same.

## 2. OpenCode (VS Code / JetBrains)

For in-editor workflows. Copy `opencode.example.json` to
`~/.config/opencode/config.json` and adjust. Start the server as above.

## 3. Claude Code via LiteLLM bridge

Claude Code is built around Anthropic's API. The LiteLLM bridge translates so
that Claude Code can drive the local model, but the translation is lossy for
tool-heavy workloads. Use this path if your muscle memory is already Claude
Code and you accept the caveats.

```bash
# Terminal 1 — mlx-lm server
bash daily_driver/start_mlx_server.sh

# Terminal 2 — litellm bridge
uvx litellm --config daily_driver/litellm_bridge.yml --port 4000

# Terminal 3 — Claude Code pointed at the bridge
ANTHROPIC_BASE_URL=http://127.0.0.1:4000 \
ANTHROPIC_API_KEY=sk-local-luxe \
  claude
```

GitHub tool use still happens through the GitHub MCP server — add it to Claude
Code's MCP config as usual.

## Why not always Claude Code?

Claude Code's tool-call format, prompt caching headers, and extended-thinking
blocks assume Anthropic models. Translating those through a proxy to a local
Qwen/DeepSeek/Mistral loses fidelity, especially in long (20+ step) tool-use
loops. Aider and OpenCode were built against OpenAI-compat backends and handle
local-model quirks more gracefully.

For review-heavy flows where you want the Claude Code UX, keep Claude Code
pointed at Anthropic's API (the normal setup) and use the local model via
Aider for write-heavy work. The two coexist.
