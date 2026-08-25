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
from dataclasses import dataclass

from luxe.backend import Backend, BackendError
from luxe.chat import origin as origin_mod
from luxe.config import BackendEntry, PipelineConfig

_SLOTS = ("chat", "plan", "code")

#: Deadline for the liveness probe behind `unreachable_hint` (seconds). Matches
#: `/doctor`'s ≤4s networked-line convention: this runs on a turn that has
#: ALREADY failed, in front of a user waiting for an explanation, so a hint
#: that cannot be produced in a few seconds is not worth producing.
_HINT_PROBE_TIMEOUT_S = 4.0


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
        manifest_host: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.overrides: dict[str, str] = {}
        self.stats = SwapStats()
        self._on_status = on_status  # callable(str) for swap notices
        # Per-host manifest (chat-only): main/fallback pair for auto-degrade.
        # None when no `hosts:` entry matches. `manifest_host` selects WHOSE
        # manifest governs: None = this machine (the chat-session rule,
        # chat.sdd) — chat never passes it, so sessions are untouched.
        # `luxe ready --backend <name>` passes the endpoint's host (the drill
        # rule, same as smoke) so a remote preflight judges the remote pair.
        self._manifest_host = manifest_host
        self.manifest = cfg.host_manifest(manifest_host)
        # Degrade state: when the manifest main can't be served/loaded, every
        # resolution of `main` is rerouted to `degraded_to` and the switch is
        # announced ONCE via on_status. Manual /model overrides still win.
        self.degraded_from: str | None = None
        self.degraded_to: str | None = None
        self._catalog_checked = False
        # Single-residency policy (2026-07-30, user decision): ONE model in
        # RAM per host — the headroom is reserved for context. The swap path
        # always enforced this; this flag makes the first-use path enforce it
        # too (a session inheriting someone else's resident model would
        # otherwise leave two loaded on a big-RAM host like the m5).
        self._residency_enforced = False
        # B5: models THIS session caused to load. On a shared endpoint we only
        # ever unload from this set — evicting a model we didn't load means
        # pulling weights out from under whoever did.
        self._loaded_by_us: set[str] = set()
        # Full `/v1/models` payloads per backend name (`/model find`). One GET
        # per endpoint per session; a cloud catalog is ~300 records.
        self._catalog_cache: dict[str, list[dict] | None] = {}
        # Multi-backend (chat-only): build from the config's default backend
        # entry so per-endpoint timeout/api-key settings apply from turn one.
        # Configs without `backends:` synthesize a single "local" entry from
        # omlx_base_url — byte-identical behaviour to the single-backend days.
        self.backend_name = cfg.default_backend_name()
        # Slots pinned by this endpoint's `default_model:`, reported by the UI
        # after a `/backend` switch. Resolved BEFORE `_resident` below, which
        # reads `model_for("chat")` — starting a session on an endpoint that
        # declares one must not first believe the manifest model is warm there.
        self.default_model_applied: str = ""
        self._apply_entry_default_model()
        # Start resident on the chat slot's model — that's the conversational
        # default and the model we keep warm.
        self._resident = self.model_for("chat")
        self.backend = self._build_backend(cfg.backend_entry(self.backend_name))

    def _endpoint_is_shared(self) -> bool:
        """True when other clients may be using the ACTIVE endpoint (B5).

        Single-residency ("one model in RAM per host") is a policy about a box
        luxe owns. Applied to a fleet endpoint it evicts other hosts' models
        mid-turn — observed 2026-07-30, when a 30-second /doctor spot-check
        pulled the weights out from under a running gitaudit.
        """
        try:
            return self.cfg.backend_entry(self.backend_name).is_shared()
        except Exception:
            return False

    def _build_backend(self, entry: BackendEntry) -> Backend:
        """Backend from a config entry. The API key is resolved HERE from the
        entry's env-var name via luxe.secrets (env → secrets.env → keychain;
        never stored in YAML); an empty value lets Backend fall back to
        OMLX_API_KEY through the same chain."""
        from luxe.secrets import resolve_api_key

        backend = Backend(
            base_url=entry.base_url,
            model=self._resident,
            api_key=resolve_api_key(entry.api_key_env),
            **entry.backend_kwargs(),
        )
        # How a failed request NAMES the stack it was talking to. Set as an
        # INSTANCE attribute rather than passed as a constructor kwarg — the
        # same shape `on_reasoning` uses, and for the same reason: it is
        # display-only, it reaches no request, and a Backend the benchmark
        # path builds must keep the `"oMLX"` default this never touches
        # (`backend.py`). Deliberately NOT routed through
        # `BackendEntry.backend_kwargs()`, whose pinned contract is that the
        # `engine:` field never changes the wire/timeout surface
        # (tests/test_config.py) — a label is not the wire.
        #
        # Before 2026-08-24 every failure string was hardcoded "oMLX", so a
        # turn that died on the OpenRouter backend reported "oMLX stream
        # failed: RemoteProtocolError … (exhausted-attempts)" and pointed the
        # reader at a local server that was never in the request path (session
        # 168f1825a1fd; `acceptance/chat_bigread_2026_08_24/EVIDENCE.md`
        # finding 1).
        backend.engine_label = entry.engine_label()
        return backend

    # -- resolution ---------------------------------------------------------

    def model_for(self, slot: str) -> str:
        if slot not in _SLOTS:
            raise KeyError(f"Unknown slot {slot!r}; expected one of {_SLOTS}.")
        if slot in self.overrides:
            return self.overrides[slot]
        resolved = self.cfg.model_for_slot(slot, manifest_host=self._manifest_host)
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
        """Hard num_ctx ceiling for `/ctx` on this slot.

        Two sources, and the ENDPOINT wins when it has one. If its catalog
        states a `context_length` for the model this slot resolves to, that IS
        the ceiling: it is the serving stack's own answer about the model it is
        running, where `num_ctx_max` and the manifest `ctx_max` are statements
        about how much KV cache THIS box can hold — meaningless for weights on
        somebody else's hardware, and wrong by a factor of 32 in practice (a
        session on a 1,048,576-token hosted model sat clamped at the local
        32K default, 2026-08-17).

        Otherwise, unchanged: the role's `num_ctx_max` (or its `num_ctx` when
        no expansion is configured), further clamped by the host manifest's
        per-MODEL `ctx_max` for the model this slot currently resolves to —
        overrides and auto-degrade included, so a session that lands on the
        dense fallback clamps to the dense cap automatically.
        """
        served = self.catalog_context_length(self.model_for(slot))
        if served:
            return served
        role = self.role_for(slot)
        ceiling = role.num_ctx_max or role.num_ctx
        if self.manifest is not None:
            cap = self.manifest.ctx_max.get(self.model_for(slot), 0)
            if cap:
                ceiling = min(ceiling, cap)
        return ceiling

    def default_num_ctx(self, slot: str) -> int:
        """The window a turn uses when the user hasn't set one with `/ctx`.

        The role's `num_ctx` everywhere except a BILLABLE endpoint, where it
        is `BILLABLE_DEFAULT_NUM_CTX` clamped to the ceiling. That is a cost
        decision, not a capability one — the window governs how much history
        and tool output compaction carries forward, and carried prompt tokens
        are what a metered provider charges for on every step (chat/session.py).
        """
        from luxe.chat.session import BILLABLE_DEFAULT_NUM_CTX

        role = self.role_for(slot)
        entry = self.active_entry()
        try:
            if entry is None or not entry.is_billable():
                return role.num_ctx
        except Exception:
            return role.num_ctx
        return min(BILLABLE_DEFAULT_NUM_CTX, self.ctx_ceiling(slot)) or role.num_ctx

    def catalog_context_length(self, model_id: str) -> int:
        """Window the ENDPOINT reports for `model_id`, or 0 when it doesn't.

        Reads `context_length`, falling back to `top_provider.context_length`
        (both appear on OpenRouter's `/v1/models` records; a provider-specific
        figure is the one that binds when the two differ, but the top-level
        value is the model's and is what the picker quotes).

        Deliberately does NOT warm the catalog on a local endpoint: oMLX and
        llama-server report ids only, so the GET could never answer, and
        `ctx_ceiling` runs on every turn. A local endpoint therefore keeps its
        exact pre-2026-08-17 call pattern, and answers from the cache if some
        other command (`/model find`) already filled it.
        """
        if not model_id:
            return 0
        cached = self._catalog_cache.get(self.backend_name)
        if cached is None:
            entry = self.active_entry()
            try:
                billable = entry is not None and entry.is_billable()
            except Exception:
                billable = False
            if not billable:
                return 0
            cached = self.catalog()
        for rec in cached or []:
            if not isinstance(rec, dict) or rec.get("id") != model_id:
                continue
            top = rec.get("top_provider")
            for raw in (rec.get("context_length"),
                        (top or {}).get("context_length")
                        if isinstance(top, dict) else None):
                try:
                    n = int(raw)
                except (TypeError, ValueError):
                    continue
                if n > 0:
                    return n
        return 0

    def set_override(self, slot: str, model_id: str) -> None:
        if slot not in _SLOTS:
            raise KeyError(f"Unknown slot {slot!r}; expected one of {_SLOTS}.")
        self.overrides[slot] = model_id

    # -- per-endpoint default model (chat-only) ------------------------------

    def _apply_entry_default_model(self) -> list[str]:
        """Point every UNPINNED slot at the active entry's `default_model:`.

        Slot defaults otherwise come from this HOST's manifest (or the
        champion), which is a set of local weight ids. On an endpoint serving
        a different catalog those resolve to nothing, so a session that opened
        there sat pointed at a model the server has never heard of until the
        user ran `/model all <id>`. Declaring the model on the ENTRY closes
        that; it is config-driven selection, not engine-driven — the engine
        field is not consulted here at all.

        It is a DEFAULT, so it yields to anything the user actually chose:

          - a runtime `/model <slot> <id>` override (a typed instruction), and
          - a startup `--chat/plan/code-model` flag or a `slots:` block entry,
            both of which arrive as a non-empty `SlotConfig.model_key`.

        Returns the slots it pinned ([] when the entry declares nothing, which
        is every local entry — their resolution is untouched).
        """
        self.default_model_applied = ""
        try:
            model_id = (self.cfg.backend_entry(self.backend_name)
                        .default_model or "").strip()
        except Exception:
            return []
        if not model_id:
            return []
        applied: list[str] = []
        for slot in _SLOTS:
            if slot in self.overrides:
                continue                       # the user already said otherwise
            try:
                if self.cfg.slot_config(slot).model_key:
                    continue                   # --<slot>-model / a `slots:` pin
            except Exception:
                continue
            self.overrides[slot] = model_id
            applied.append(slot)
        if applied:
            self.default_model_applied = model_id
        return applied

    @property
    def resident(self) -> str:
        return self._resident

    def active_entry(self) -> BackendEntry | None:
        """The `backends:` entry behind the active endpoint, or None.

        Guarded like `inspection.backend_entry_for`: every caller has a sane
        default for None, so a config luxe cannot read degrades to the
        pre-multi-engine behaviour instead of raising into a render path.
        """
        try:
            return self.cfg.backend_entry(self.backend_name)
        except Exception:
            return None

    def engine_label(self) -> str:
        """How to name the active endpoint in user-facing text ("oMLX" by
        default) — so a message never asserts the wrong serving stack."""
        entry = self.active_entry()
        try:
            return entry.engine_label() if entry is not None else "oMLX"
        except Exception:
            return "oMLX"

    def available_models(self) -> list[str]:
        """Selectable model ids: what the server serves, filtered to the roster.

        Guarded — returns [] if the server is unreachable so `/model` never
        crashes when oMLX is down. The roster keeps stale bake-off entries and
        HF-cache aliases out of the picker (chat-only; see config.visible). The
        ACTIVE entry's own `visible_models` wins when it has one: a cloud
        catalog cannot be governed by a list of local weight ids.
        """
        try:
            served = self.backend.list_models()
        except Exception:
            return []
        return self.cfg.visible(served, entry=self.active_entry())

    def catalog(self) -> list[dict]:
        """Full `/v1/models` records for the active endpoint, cached.

        `/model find` needs the fields `list_models()` throws away (pricing,
        supported_parameters) and a cloud catalog is ~300 entries, so the GET
        is paid once per endpoint per session. Guarded: [] when unreachable.
        """
        if self._catalog_cache.get(self.backend_name) is None:
            try:
                records = self.backend.list_models_full()
            except Exception:
                return []
            self._catalog_cache[self.backend_name] = records
            # A catalog that declares `supported_parameters` is a first-party
            # answer to "can this model call tools?" — better than the
            # fail-open UNKNOWN a remote endpoint otherwise gets.
            try:
                from luxe.chat import modelcaps
                modelcaps.note_catalog(
                    getattr(self.backend, "base_url", "") or "", records)
            except Exception:
                pass
        return self._catalog_cache[self.backend_name] or []

    def catalog_is_larger_than_roster(self) -> bool:
        """True when this endpoint serves more models than `/model` lists.

        Only ever True where a per-backend roster is deliberately hiding a
        large catalog (the cloud entry) — it uses the ALREADY-FETCHED id list
        and never triggers the full-catalog GET, so a local `/model` costs the
        same as it always did.
        """
        entry = self.active_entry()
        if entry is None or not entry.visible_models:
            return False
        try:
            return len(self.backend.list_models()) > len(entry.visible_models)
        except Exception:
            return False

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
        # Now that the new entry is active, let it name its own default model
        # for the slots nobody pinned. Runs AFTER the drop above so a surviving
        # explicit override (one the new server does serve) keeps its slot.
        self._apply_entry_default_model()
        # Unknown residency on the new server: force the next backend_for() to
        # confirm/load the target there (never unloads the old server) — and
        # re-enforce single-residency on the NEW server's first use.
        self._resident = ""
        self._residency_enforced = False
        # Prime this endpoint's model provenance while we're off the UI thread,
        # so the status bar's origin marker is right from the first render.
        try:
            origin_mod.origins_for_backend(backend)
        except Exception:
            pass
        return dropped

    def unreachable_hint(self) -> str | None:
        """One line for a failed turn, keyed on whether the ENDPOINT is the
        thing that failed — or None when the config offers nowhere to go.

        Two outcomes, and the difference is the whole point (2026-08-24):

        * endpoint does not answer  → today's `/backend` escape hatch, VERBATIM.
          The fleet's outage path reads this string; it must not drift.
        * endpoint answers          → say so, and do NOT prescribe a switch.

        Before this, ANY turn-level backend error printed the escape hatch as
        soon as two backends were configured — no probe, no evidence. On
        2026-08-24 (session 168f1825a1fd) a turn that died on an oversized
        payload printed "openrouter OpenRouter unreachable — try /backend
        local" while OpenRouter was demonstrably up (turns succeeded on it at
        18:42 and 18:45 in the same session), and the `local` it advised has a
        32K window that had already failed the identical way half an hour
        earlier. Wrong diagnosis, wrong remedy, both stated confidently. See
        `acceptance/chat_bigread_2026_08_24/EVIDENCE.md` finding 2.

        The probe runs on the FAILURE path only, is bounded to
        `_HINT_PROBE_TIMEOUT_S`, and is fully guarded: an endpoint that cannot
        answer QUICKLY is treated as unreachable, i.e. today's behaviour. A
        hint must never hang or crash a turn that has already failed.
        """
        entries = self.cfg.backend_entries()
        if len(entries) < 2:
            return None
        other = next((n for n in entries if n != self.backend_name), None)
        if other is None:
            return None
        if self._endpoint_answers():
            return (f"{self.backend_name} {self.engine_label()} answered a "
                    f"health check — the endpoint is up, so this request "
                    f"failed on its own (this session's debug.log has the "
                    f"retry history)")
        return (f"{self.backend_name} {self.engine_label()} unreachable — "
                f"try /backend {other}")

    def _endpoint_answers(self) -> bool:
        """Bounded, guarded liveness probe for the ACTIVE endpoint.

        False on anything that is not a clean "yes" — unreachable, slow,
        raising, or a duck-typed backend that cannot take the bound. False is
        the pre-2026-08-24 answer, so every degraded case keeps the behaviour
        the outage path was written against.
        """
        probe = getattr(self.backend, "health", None)
        if probe is None:
            return False
        try:
            return bool(probe(timeout_s=_HINT_PROBE_TIMEOUT_S))
        except TypeError:
            # A Backend double predating the bound. Ask it the only way it
            # knows; it is a test/fake, so there is nothing to hang on.
            pass
        except Exception:
            return False
        try:
            return bool(probe())
        except Exception:
            return False

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
        if not self._residency_enforced:
            # One model resident per host (headroom is for ctx): evict
            # anything a prior session/tool left loaded besides our target.
            # The swap path below does this anyway; this covers the
            # target-already-resident short-circuit.
            # NOT on a shared endpoint (B5): "anything a prior session left
            # loaded" may be another host's live model.
            if self._endpoint_is_shared():
                # We evict nothing here, so instead learn whether OUR first
                # request is what puts `target` in RAM. If it is already
                # resident someone else loaded it and it is not ours to free.
                try:
                    if target not in self.backend.loaded_models():
                        self._loaded_by_us.add(target)
                except Exception:
                    pass
            else:
                try:
                    self.backend.unload_all_loaded(except_for=[target])
                except Exception:
                    pass
            self._residency_enforced = True
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
        # Free the doubled RAM before loading the new weights. On a shared
        # endpoint (B5) free only what WE loaded — the doubled-RAM concern is
        # ours, the other resident models are someone else's session.
        if self._endpoint_is_shared():
            for mid in sorted(self._loaded_by_us - {target}):
                try:
                    self.backend.unload_model(mid)
                except Exception:
                    pass
        else:
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
        self._loaded_by_us.add(target)

    def unload_all(self) -> None:
        """Release weights at session end.

        On an endpoint luxe owns, that means everything — the box is ours and
        the RAM should come back. On a SHARED endpoint it means only the models
        this session caused to load (B5): unloading the rest evicted other
        hosts' models, which is how a /doctor spot-check killed a running
        gitaudit on 2026-07-30.
        """
        try:
            if self._endpoint_is_shared():
                for mid in sorted(self._loaded_by_us):
                    try:
                        self.backend.unload_model(mid)
                    except Exception:
                        pass
            else:
                self.backend.unload_all_loaded()
        except Exception:
            pass

    def forget_resident(self) -> None:
        """Drop our belief about what's in RAM, so the next turn re-confirms
        and reloads. Call after unloading behind the manager's back (`/unload`)
        — otherwise `backend_for` thinks the target is still resident and skips
        the load."""
        self._resident = ""
