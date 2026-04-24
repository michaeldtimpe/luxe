#!/usr/bin/env bash
# daily_driver/install.sh — idempotent installer for the local-model stack.
#
# What it does:
#   1. mkdir ~/Library/Logs/luxe/
#   2. Copy launchd plists → ~/Library/LaunchAgents/, bootstrap both
#   3. Install ~/.local/bin/claude-local (symlink into repo, so `git pull` updates it)
#   4. Write ~/.claude/settings.json (backs up any existing file)
#
# What it doesn't do:
#   - Install uv / mlx-lm / litellm (those come from `uv sync` and uvx)
#   - Modify shell rc files
#   - Touch any of the user's project repos
#   - Install `.claude/settings.json` inside individual repos
#
# Rerun-safe: every step checks for existing state and backs up / skips as needed.

set -eu

LUXE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOGS_DIR="$HOME/Library/Logs/luxe"
LOCAL_BIN="$HOME/.local/bin"
CLAUDE_CFG_DIR="$HOME/.claude"
UID_="$(id -u)"

TS="$(date +%Y%m%d-%H%M%S)"

say() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m!! %s\033[0m\n" "$*" >&2; }

#
# 1. log directory
#
say "creating $LOGS_DIR"
mkdir -p "$LOGS_DIR"

#
# 2. launchd agents
#
say "installing launchd agents to $AGENTS_DIR"
mkdir -p "$AGENTS_DIR"
for label in com.luxe.mlx com.luxe.litellm; do
    src="$LUXE_ROOT/daily_driver/launchd/${label}.plist"
    dest="$AGENTS_DIR/${label}.plist"
    cp "$src" "$dest"
    chmod 644 "$dest"
    echo "  copied $src → $dest"
    # Reload: bootout (ignore failure on first install) + bootstrap
    launchctl bootout "gui/${UID_}/${label}" 2>/dev/null || true
    launchctl bootstrap "gui/${UID_}" "$dest"
    echo "  bootstrapped $label"
done

#
# 3. claude-local wrapper
#
say "installing ~/.local/bin/claude-local"
mkdir -p "$LOCAL_BIN"
wrapper_src="$LUXE_ROOT/daily_driver/claude-local"
wrapper_dest="$LOCAL_BIN/claude-local"
if [ -L "$wrapper_dest" ] || [ -f "$wrapper_dest" ]; then
    rm -f "$wrapper_dest"
fi
ln -s "$wrapper_src" "$wrapper_dest"
echo "  symlinked $wrapper_dest → $wrapper_src"

case ":$PATH:" in
    *":$LOCAL_BIN:"*) ;;
    *) warn "$LOCAL_BIN is not in your PATH. Add to ~/.zshrc:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

#
# 4. ~/.claude/settings.json
#
say "writing $CLAUDE_CFG_DIR/settings.json"
mkdir -p "$CLAUDE_CFG_DIR"
settings_file="$CLAUDE_CFG_DIR/settings.json"
if [ -f "$settings_file" ]; then
    backup="${settings_file}.backup.${TS}"
    cp "$settings_file" "$backup"
    echo "  backed up existing settings to $backup"
fi

cat > "$settings_file" <<'JSON'
{
  "model": "qwen-coder-local",
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
    "ANTHROPIC_AUTH_TOKEN": "sk-local-luxe",
    "ANTHROPIC_CUSTOM_MODEL_OPTION": "qwen-coder-local",
    "ANTHROPIC_CUSTOM_MODEL_NAME": "Qwen 2.5 Coder 32B (local, 4-bit, spec-dec)",
    "ANTHROPIC_CUSTOM_MODEL_DESCRIPTION": "Local mlx-lm + LiteLLM. Long context, no prompt caching."
  }
}
JSON
echo "  wrote $settings_file"

#
# 5. next steps
#
cat <<'DONE'

==> install complete.

Next steps:

  1. Verify the agents are running:
       launchctl print gui/$(id -u)/com.luxe.mlx | grep state
       launchctl print gui/$(id -u)/com.luxe.litellm | grep state
     Both should show `state = running` after a few seconds.

  2. Health-check the endpoints (may take ~30s after first boot):
       curl -s http://127.0.0.1:8088/v1/models
       curl -s http://127.0.0.1:4000/v1/models

  3. Tail logs while they come up:
       tail -f ~/Library/Logs/luxe/{mlx,litellm}.err.log

  4. Launch a session:
       cd <any-repo>
       claude-local

  5. To drop a CLAUDE.md into a repo (agent gotchas), copy the appropriate
     file from daily_driver/repo_context/<name>.md to <repo>/CLAUDE.md.
     Those files are drafts — review before committing.

DONE
