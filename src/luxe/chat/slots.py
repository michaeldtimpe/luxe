"""Model-slot manager for the chat REPL.

Resolves chat/plan/code slots to concrete models and orchestrates the
sequential weight swaps oMLX requires (two ~25 GB weight-sets can't coexist).
Swap count + seconds are instrumented from day one (chat.sdd) so the slot
system's real-world cost is visible, not a mystery stall.

When every slot is the champion (the default), `backend_for` never swaps — the
swap path is dead code and the experience is identical to single-champion.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from luxe.backend import Backend, BackendError
from luxe.chat import origin as origin_mod
from luxe.config import BackendEntry, PipelineConfig

_SLOTS = ("chat", "plan", "code")


@dataclass
class SwapStats:
    count: int = 0
    seconds: float = 0.0


class SlotManager:
    """Owns a single Backend and swaps the resident model on demand.

    `overrides` maps slot -> model_id and lets `/model <slot> <id>` repoint a
    slot at runtime without editing the config.
    """

    def __init__(
        self,
        cfg: PipelineConfig,
        *,
        on_status=None,
    ) -> None:
        self.cfg = cfg
        self.overrides: dict[str, str] = {}
        self.stats = SwapStats()
        self._on_status = on_status  # callable(str) for swap notices
        # Start resident on the chat slot's model — that's the conversational
        # default and the model we keep warm.
        self._resident = self.model_for("chat")
        # Multi-backend (chat-only): build from the config's default backend
        # entry so per-endpoint timeout/api-key settings apply from turn one.
        # Configs without `backends:` synthesize a single "local" entry from
        # omlx_base_url — byte-identical behaviour to the single-backend days.
        self.backend_name = cfg.default_backend_name()
        self.backend = self._build_backend(cfg.backend_entry(self.backend_name))

    def _build_backend(self, entry: BackendEntry) -> Backend:
        """Backend from a config entry. The API key is resolved HERE from the
        entry's env-var name (never stored in YAML); an empty value lets
        Backend fall back to OMLX_API_KEY."""
        return Backend(
            base_url=entry.base_url,
            model=self._resident,
            timeout_s=entry.timeout_s,
            api_key=os.environ.get(entry.api_key_env, ""),
        )

    # -- resolution ---------------------------------------------------------

    def model_for(self, slot: str) -> str:
        if slot not in _SLOTS:
            raise KeyError(f"Unknown slot {slot!r}; expected one of {_SLOTS}.")
        if slot in self.overrides:
            return self.overrides[slot]
        return self.cfg.model_for_slot(slot)

    def slot_models(self) -> dict[str, str]:
        return {s: self.model_for(s) for s in _SLOTS}

    def role_for(self, slot: str):
        """RoleConfig that drives `run_single` for turns routed to `slot`."""
        return self.cfg.role(self.cfg.slot_config(slot).role)

    def ctx_ceiling(self, slot: str) -> int:
        """Hard num_ctx ceiling for `/ctx` on this slot: the role's
        `num_ctx_max`, or its `num_ctx` when no expansion is configured."""
        role = self.role_for(slot)
        return role.num_ctx_max or role.num_ctx

    def set_override(self, slot: str, model_id: str) -> None:
        if slot not in _SLOTS:
            raise KeyError(f"Unknown slot {slot!r}; expected one of {_SLOTS}.")
        self.overrides[slot] = model_id

    @property
    def resident(self) -> str:
        return self._resident

    def available_models(self) -> list[str]:
        """oMLX-loadable model ids (GET /v1/models), guarded — returns [] if the
        server is unreachable so `/model` never crashes when oMLX is down."""
        try:
            return self.backend.list_models()
        except Exception:
            return []

    # -- backend switching (multi-backend, chat-only) ------------------------

    def probe_backend(self, name: str) -> bool:
        """Health-check a configured backend without switching to it. The
        active backend reuses the live client; others get a throwaway."""
        if name == self.backend_name:
            return self.backend.health()
        try:
            return self._build_backend(self.cfg.backend_entry(name)).health()
        except Exception:
            return False

    def switch_backend(self, name: str) -> list[str]:
        """Point the session at another configured oMLX endpoint.

        Builds a fresh Backend from the entry (base_url / env-resolved api_key /
        timeout_s), health-checks it, and drops any `/model` slot overrides
        whose model ids don't resolve on the new server (returned for the UI to
        report). The OLD server is left untouched — no unloads there; it may be
        the endpoint that just went down. The resident model is reset so the
        next turn re-confirms weights on the NEW server.

        Raises KeyError for an unknown name, BackendError if unreachable.
        """
        entry = self.cfg.backend_entry(name)
        backend = self._build_backend(entry)
        if not backend.health():
            raise BackendError(
                f"backend {name!r} unreachable at {entry.base_url}")
        try:
            available = set(backend.list_models())
        except Exception:
            available = set()
        dropped = [s for s in _SLOTS
                   if s in self.overrides and self.overrides[s] not in available]
        for s in dropped:
            del self.overrides[s]
        self.backend = backend
        self.backend_name = name
        # Unknown residency on the new server: force the next backend_for() to
        # confirm/load the target there (never unloads the old server).
        self._resident = ""
        # Prime this endpoint's model provenance while we're off the UI thread,
        # so the status bar's origin marker is right from the first render.
        try:
            origin_mod.origins_for_backend(backend)
        except Exception:
            pass
        return dropped

    def unreachable_hint(self) -> str | None:
        """One-line `/backend` escape hatch for a failed turn — only when the
        config actually offers an alternative endpoint."""
        entries = self.cfg.backend_entries()
        if len(entries) < 2:
            return None
        other = next((n for n in entries if n != self.backend_name), None)
        if other is None:
            return None
        return f"{self.backend_name} oMLX unreachable — try /backend {other}"

    # -- swap orchestration -------------------------------------------------

    def backend_for(self, slot: str) -> Backend:
        """Return a Backend whose resident model matches `slot`, swapping
        weights (unload-all + thermal_guard) only when the target differs."""
        target = self.model_for(slot)
        if target == self._resident:
            self.backend.model = target
            return self.backend
        self._swap_to(target, slot)
        return self.backend

    def _swap_to(self, target: str, slot: str) -> None:
        if self._on_status:
            # Say where the incoming weights come from — a swap that streams
            # ~30 GB off a NAS or hits a remote host should never look like a
            # local disk read (chat/origin.py).
            try:
                org = origin_mod.origin_for(self.backend, target)
                whence = f" · {org.glyph} {org.label}"
            except Exception:
                whence = ""
            self._on_status(
                f"swapping weights: {self._resident} → {target} "
                f"(slot: {slot}){whence}"
            )
        t0 = time.monotonic()
        # Free the doubled RAM before loading the new weights.
        self.backend.unload_all_loaded(except_for=[target])
        self.backend.model = target
        # Confirm the target is resident before the first chat call.
        self.backend.thermal_guard(target)
        elapsed = time.monotonic() - t0
        self.stats.count += 1
        self.stats.seconds += elapsed
        self._resident = target

    def unload_all(self) -> None:
        try:
            self.backend.unload_all_loaded()
        except Exception:
            pass

    def forget_resident(self) -> None:
        """Drop our belief about what's in RAM, so the next turn re-confirms
        and reloads. Call after unloading behind the manager's back (`/unload`)
        — otherwise `backend_for` thinks the target is still resident and skips
        the load."""
        self._resident = ""
