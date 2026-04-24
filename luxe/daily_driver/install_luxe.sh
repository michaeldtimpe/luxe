#!/usr/bin/env bash
# install_luxe.sh — install the `luxe` CLI + launchd services on macOS.
#
# Idempotent. Run without args to install, with --uninstall to reverse.
#
#   bash daily_driver/install_luxe.sh
#   bash daily_driver/install_luxe.sh --uninstall

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/luxe"
BIN_DIR="$HOME/.local/bin"

PLIST_SRC="$REPO_ROOT/daily_driver/launchd/com.luxe.ollama.plist"
PLIST_DST="$LAUNCH_AGENTS/com.luxe.ollama.plist"

LUXE_BIN="$REPO_ROOT/.venv/bin/luxe"
LUXE_LINK="$BIN_DIR/luxe"

say() { printf '\033[1;36m[luxe]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[luxe]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[luxe]\033[0m %s\n' "$*" >&2; exit 1; }

check_prereqs() {
  command -v ollama >/dev/null 2>&1 || die "ollama not installed. brew install ollama"
  command -v uv >/dev/null 2>&1 || die "uv not installed. brew install uv"
  [[ -d "$REPO_ROOT/.venv" ]] || die "no .venv — run 'uv sync' from $REPO_ROOT first"
  [[ -x "$LUXE_BIN" ]] || die "$LUXE_BIN missing — run 'uv sync' from $REPO_ROOT"
}

install() {
  check_prereqs

  say "ensuring log dir: $LOG_DIR"
  mkdir -p "$LOG_DIR"

  say "symlinking $LUXE_LINK -> $LUXE_BIN"
  mkdir -p "$BIN_DIR"
  ln -sf "$LUXE_BIN" "$LUXE_LINK"

  say "installing launchd plist: $PLIST_DST"
  mkdir -p "$LAUNCH_AGENTS"
  cp "$PLIST_SRC" "$PLIST_DST"

  if launchctl list | grep -q "com.luxe.ollama"; then
    say "reloading existing launchd job"
    launchctl bootout "gui/$UID/com.luxe.ollama" 2>/dev/null || true
  fi
  launchctl bootstrap "gui/$UID" "$PLIST_DST"
  launchctl enable "gui/$UID/com.luxe.ollama" || true

  say "verifying ollama is up"
  for _ in $(seq 1 20); do
    if curl -s --max-time 1 http://127.0.0.1:11434/api/version >/dev/null; then
      say "✓ ollama reachable at http://127.0.0.1:11434"
      break
    fi
    sleep 1
  done

  say "done. Try: luxe agents"
  if ! echo "$PATH" | tr ':' '\n' | grep -Fxq "$BIN_DIR"; then
    warn "$BIN_DIR is not on your PATH. Add to your shell rc:"
    warn "  export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
}

uninstall() {
  say "removing launchd job"
  launchctl bootout "gui/$UID/com.luxe.ollama" 2>/dev/null || true
  rm -f "$PLIST_DST"

  if [[ -L "$LUXE_LINK" ]]; then
    say "removing symlink $LUXE_LINK"
    rm -f "$LUXE_LINK"
  fi

  say "done. Logs left in $LOG_DIR (safe to delete)."
}

case "${1:-}" in
  --uninstall|-u) uninstall ;;
  *) install ;;
esac
