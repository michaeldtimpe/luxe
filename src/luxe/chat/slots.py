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
        # Per-host manifest (chat-only): main/fallback pair for auto-degrade.
        # None when no `hosts:` entry matches this machine.
        self.manifest = cfg.host_manifest()
        # Degrade state: when the manifest main can't be served/loaded, every
        # resolution of `main` is rerouted to `degraded_to` and the switch is
        # announced ONCE via on_status. Manual /model overrides still win.
        self.degraded_from: str | None = None
        self.degraded_to: str | None = None
        self._catalog_checked = False
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
        entry's env-var name via luxe.secrets (env → secrets.env → keychain;
        never stored in YAML); an empty value lets Backend fall back to
        OMLX_API_KEY through the same chain."""
        from luxe.secrets import resolve_api_key

        return Backend(
            base_url=entry.base_url,
            model=self._resident,
            timeout_s=entry.timeout_s,
            api_key=resolve_api_key(entry.api_key_env),
        )

    # -- resolution ---------------------------------------------------------

    def model_for(self, slot: str) -> str:
        if slot not in _SLOTS:
            raise KeyError(f"Unknown slot {slot!r}; expected one of {_SLOTS}.")
        if slot in self.overrides:
            return self.overrides[slot]
        resolved = self.cfg.model_for_slot(slot)
        # Auto-degrade reroute (manifest main -> fallback). Explicit /model
        # overrides bypass this on purpose: picking a model by hand is an
        # instruction, not a default to second-guess.
        if self.degraded_from is not None and resolved == self.degraded_from:
            return self.degraded_to or resolved
        return resolved

    def slot_models(self) -> dict[str, str]:
        return {s: self.model_for(s) for s in _SLOTS}

    def role_for(self, slot: str):
        """RoleConfig that drives `run_single` for turns routed to `slot`."""
        return self.cfg.role(self.cfg.slot_config(slot).role)

    def ctx_ceiling(self, slot: str) -> int:
        """Hard num_ctx ceiling for `/ctx` on this slot: the role's
        `num_ctx_max` (or its `num_ctx` when no expansion is configured),
        further clamped by the host manifest's per-MODEL `ctx_max` for the
        model this slot currently resolves to — overrides and auto-degrade
        included, so a session that lands on the dense fallback clamps to the
        dense cap automatically."""
        role = self.role_for(slot)
        ceiling = role.num_ctx_max or role.num_ctx
        if self.manifest is not None:
            cap = self.manifest.ctx_max.get(self.model_for(slot), 0)
            if cap:
                ceiling = min(ceiling, cap)
        return ceiling

    def set_override(self, slot: str, model_id: str) -> None:
        if slot not in _SLOTS:
            raise KeyError(f"Unknown slot {slot!r}; expected one of {_SLOTS}.")
        self.overrides[slot] = model_id

    @property
    def resident(self) -> str:
        return self._resident

    def available_models(self) -> list[str]:
        """Selectable model ids: what the server serves, filtered to the config's
        `visible_models` roster.

        Guarded — returns [] if the server is unreachable so `/model` never
        crashes when oMLX is down. The roster keeps stale bake-off entries and
        HF-cache aliases out of the picker (chat-only; see config.visible).
        """
        try:
            served = self.backend.list_models()
        except Exception:
            return []
        return self.cfg.visible(served)

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

    # -- auto-degrade (manifest fallback, chat-only) --------------------------

    def _degrade(self, reason: str) -> bool:
        """Reroute the manifest main to its fallback for the rest of the
        session. Returns True when a degrade actually happened. Loud by
        contract: a session silently running a different model than asked
        is the failure mode this exists to kill."""
        m = self.manifest
        if (m is None or not m.fallback or m.fallback == m.main
                or self.degraded_from is not None):
            return False
        self.degraded_from = m.main
        self.degraded_to = m.fallback
        # Residency belief may refer to the failed main; force the next
        # backend_for() to confirm/load the fallback.
        if self._resident == m.main:
            self._resident = ""
        if self._on_status:
            self._on_status(
                f"⚠ DEGRADED: {m.main} unavailable ({reason}) — "
                f"running on fallback {m.fallback}. "
                f"Fix the main model and /model chat {m.main} to restore.")
        return True

    def _served_models(self) -> set[str] | None:
        """Server catalog, or None when the endpoint can't answer (down /
        transient) — callers must treat None as "unknown", not "empty"."""
        try:
            return set(self.backend.list_models())
        except Exception:
            return None

    def note_turn_failure(self) -> str | None:
        """Called by the front-ends after a turn-level BackendError. When the
        endpoint itself is healthy but the manifest main was the model that
        failed (oMLX lazy-loads on first request, so missing/corrupt weights
        surface HERE, not at swap time), degrade and return a user-facing
        notice. Returns None when this isn't a degrade case (endpoint down,
        non-manifest model, no fallback, already degraded)."""
        m = self.manifest
        if m is None or self.degraded_from is not None:
            return None
        current = self.backend.model
        if current != m.main:
            return None
        try:
            healthy = self.backend.health()
        except Exception:
            healthy = False
        if not healthy:
            return None  # endpoint problem, not a model problem
        if self._degrade("turn failed while the endpoint is healthy"):
            return (f"switched to fallback {m.fallback} — "
                    f"/retry to re-run your message on it")
        return None

    # -- swap orchestration -------------------------------------------------

    def backend_for(self, slot: str) -> Backend:
        """Return a Backend whose resident model matches `slot`, swapping
        weights (unload-all + thermal_guard) only when the target differs.

        First use also verifies the target against the server catalog: the
        resident model is assumed, never confirmed, at construction (gotcha:
        a missing-weights main would otherwise fail only at request time)."""
        target = self.model_for(slot)
        if not self._catalog_checked:
            served = self._served_models()
            if served is not None:
                self._catalog_checked = True
                m = self.manifest
                if (m is not None and target == m.main
                        and target not in served and m.fallback in served
                        and self._degrade("not in the server catalog")):
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
        # Confirm the target is resident before the first chat call. The
        # guard's verdict used to be discarded; for the manifest main a False
        # here (not in the catalog within the wait) now triggers the fallback
        # instead of a doomed first request.
        ok = self.backend.thermal_guard(target)
        elapsed = time.monotonic() - t0
        self.stats.count += 1
        self.stats.seconds += elapsed
        if (not ok and self.manifest is not None
                and target == self.manifest.main
                and self._degrade("did not come up after a weight swap")):
            self._swap_to(self.model_for(slot), slot)  # depth 1: degrade fires once
            return
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
