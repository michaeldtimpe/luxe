"""Shared plumbing for the `luxe` CLI package: the root Click group, the
one Console, and the helpers several command modules use.

The root group `main` is defined HERE rather than in `__init__` so every
command module can import it without a circular reach back into the package
while it is still initialising; `luxe.cli` re-exports it (the console-script
entry point is still `luxe.cli:main`).

Monkeypatch note: `_chat_cfg` resolves `_default_chat_config` through THIS
module's globals, so tests patch `luxe.cli._common._default_chat_config`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
from rich.console import Console

from luxe import gitclone
from luxe.agents.tasktype import infer_task_type
from luxe.config import load_config

console = Console()


# Moved to their tier homes 2026-08-05 (deferred-list #6); re-exported here
# for the tests and any external caller that knows the old private names.
_resolve_repo = gitclone.resolve_repo
_infer_task_type = infer_task_type


class AliasedGroup(click.Group):
    """A click Group that resolves alias names to canonical command names.

    Centralizes alias logic (vs. registering duplicate command objects):
    overrides both `get_command` (lookup-time canonicalization) and
    `resolve_command` (so `--help`/usage shows the canonical name)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aliases: dict[str, str] = {}

    def get_command(self, ctx, cmd_name):
        return super().get_command(ctx, self._aliases.get(cmd_name, cmd_name))

    def resolve_command(self, ctx, args):
        if args and args[0] in self._aliases:
            args = [self._aliases[args[0]], *args[1:]]
        return super().resolve_command(ctx, args)


def apply_aliases(group: AliasedGroup, alias_map: dict[str, str]) -> AliasedGroup:
    """Register `alias -> canonical` command-name mappings on an AliasedGroup."""
    group._aliases.update(alias_map)
    return group


@click.group(cls=AliasedGroup)
def main():
    """luxe — MLX-only repo maintainer."""
    pass


def _chat_cfg(config_path: str | None = None):
    """The chat config: `--config <path>` when given, else configs/chat.yaml.

    One spelling for what was nine copies in the old cli.py plus one in
    chat/launch.py. Resolves `_default_chat_config` through THIS module's
    globals (`luxe.cli._common`), which is what the tests that monkeypatch
    it rely on.
    """
    return load_config(config_path or _default_chat_config())


def _select_backend(cfg, backend_name: str | None, *,
                    make_default: bool = True) -> None:
    """Validate `--backend <name>` against `backends:` and re-flag it default.

    Exits 2, naming every configured entry, on a miss — a typo must not fall
    through to the default endpoint and look like it worked. `make_default`
    is False for `luxe smoke`, which validates the name but chooses its
    endpoint itself.
    """
    if not backend_name:
        return
    entries = cfg.backend_entries()
    if backend_name not in entries:
        console.print(f"[red]✗ Unknown backend {backend_name!r}. "
                      f"Configured: {', '.join(entries)}.[/]")
        sys.exit(2)
    if make_default:
        cfg.backends = {k: v.model_copy(update={"default": k == backend_name})
                        for k, v in entries.items()}


def _unload_unless(keep_loaded: bool, cfg=None) -> None:
    """Post-command teardown: free the local oMLX's RAM unless --keep-loaded.

    Best-effort by design — a teardown failure must never mask (or fail) the
    command whose `finally` this runs in. `maintain` keeps its own copy: it
    also REPORTS what it unloaded.

    The endpoint is the one the command RAN against (`cfg.omlx_base_url`,
    else the chat config's — `$LUXE_CONFIG` included), not a hard-coded
    127.0.0.1:8000: on neo that port is not the server. Loopback only — a
    teardown never evicts models on a host luxe does not own.
    """
    if keep_loaded:
        return
    from luxe.backend import Backend, is_loopback_url
    try:
        url = (getattr(cfg, "omlx_base_url", "")
               or _omlx_base_url_from_config())
        if is_loopback_url(url):
            with Backend(base_url=url, model="(unload-probe)") as probe:
                probe.unload_all_loaded()
    except Exception:
        pass


def _default_chat_config() -> str:
    """The chat config to use when no `--config` was passed.

    `$LUXE_CONFIG` wins over the in-tree default so a host whose engine is not
    the fleet's can point EVERY command at its own config, not just the two
    the dotfiles wrappers cover. That gap bit on neo (2026-08-13): `luxe-chat`
    and `luxe-code` passed `--config ~/dotfiles/luxe/neo.yaml`, but bare
    `luxe ready` / `luxe smoke` / `luxe pull` still read the fleet config and
    judged an oMLX endpoint that box does not run — and `luxe ready` is
    precisely the command reached for in a panic.

    Deliberately an env var and not a path lookup: the per-host configs live
    OUT of this repo (`~/dotfiles/luxe/<host>.yaml` — see the wrapper's own
    note on the 2026-08-02 skip-worktree drift), and hardcoding a dotfiles
    path here would drag a private layout into the public tree. Unset ⇒ the
    previous behaviour exactly. Chat-config only: the benchmark/maintain
    config surface (`--variants`, `single_64gb.yaml`) never routes through
    here.
    """
    override = os.environ.get("LUXE_CONFIG", "").strip()
    if override:
        return str(Path(override).expanduser())
    return str(Path(__file__).parent.parent.parent.parent / "configs" / "chat.yaml")


def _default_mcp_config_hint() -> str:
    from luxe.mcp.client import default_mcp_config_path
    return str(default_mcp_config_path())


def _omlx_base_url_from_config() -> str:
    """The chat config's oMLX endpoint, falling back to the local default."""
    try:
        return _chat_cfg().omlx_base_url
    except Exception:
        return "http://127.0.0.1:8000"
