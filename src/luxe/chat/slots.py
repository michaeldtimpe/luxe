"""Model-slot manager for the chat REPL.

Resolves chat/plan/code slots to concrete models and orchestrates the
sequential weight swaps oMLX requires (two ~25 GB weight-sets can't coexist).
Swap count + seconds are instrumented from day one (chat.sdd) so the slot
system's real-world cost is visible, not a mystery stall.

When every slot is the champion (the default), `backend_for` never swaps — the
swap path is dead code and the experience is identical to single-champion.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from luxe.backend import Backend
from luxe.config import PipelineConfig

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
        self.backend = Backend(base_url=cfg.omlx_base_url, model=self._resident)

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
            self._on_status(
                f"swapping weights: {self._resident} → {target} (slot: {slot})"
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
