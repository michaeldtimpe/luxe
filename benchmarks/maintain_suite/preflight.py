"""Bake-off preflight — refuse to burn a bench run on a plumbing failure.

A bake-off arm that cannot load, or whose tool calls come back as prose,
scores ~0 for reasons that have nothing to do with the model. Both have
happened: Muse-Glimmer-30B (2026-08-10) had no MLX architecture, a
miscalibrated conversion and an unparsed tool dialect; Gemma's empty
chat_template (2026-05-02) 400'd every turn. This runs per arm of a variants
YAML, in seconds (static) or minutes (live), before the hours-long bench.

Static — no model load:
  weights     store entry real, every shard the index names present
  arch        config `model_type` resolvable by the INSTALLED oMLX
  engine      predicted llm/vlm (oMLX's vision-subconfig rule), reported
  quant       bits/group/mode against the FIRST arm (the reference) → WARN
  template    chat template renders tools (chat/modelcaps)

Live — `--live`, one model resident at a time:
  catalog     /v1/models lists the id
  ping        one generation; cold-load seconds recorded
  tool call   STRUCTURED `tool_calls` — the authoritative gate. oMLX picks a
              parser through several layers (per-arch tokenizer defaults,
              repair_tool_parser, the VLM engine's own path), so a static
              guess would be brittle; the live call is the ground truth.
  drill       `--drill`: the `luxe smoke --code` drill on this arm's model

Run ON the bench host (static checks read its oMLX store and env):

    .venv/bin/python -m benchmarks.maintain_suite.preflight \\
        --variants benchmarks/maintain_suite/variants_<x>.yaml \\
        --live --drill --out acceptance/<run>/

Exit 0 = every arm clear (warnings allowed), 1 = at least one FAIL,
2 = bad invocation.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"

# Markup that means the model DID try to call a tool but no parser claimed
# it — the silent text-fallback-drop class. Deliberately broad: a false
# positive only rewords a FAIL that was already a FAIL (no tool_calls).
_TOOL_MARKUP = ("<tool_call", "<function", "<invoke", "<atem:", "<ifm|",
                "[TOOL_CALLS]", "<|tool_call", "<arg_key>", '{"name"')

# Reasoning models spend tokens before answering; smoke's 16/256 budgets are
# sized for the champion and would misreport a thinking arm as empty.
_PING_MAX_TOKENS = 1024
_TOOL_MAX_TOKENS = 2048


@dataclass
class Check:
    name: str
    state: str
    detail: str = ""
    seconds: float = 0.0


@dataclass
class ArmReport:
    label: str
    model_id: str
    checks: list[Check] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    def add(self, name: str, state: str, detail: str = "",
            seconds: float = 0.0) -> Check:
        c = Check(name, state, detail, round(seconds, 1))
        self.checks.append(c)
        return c

    @property
    def failed(self) -> bool:
        return any(c.state == FAIL for c in self.checks)

    @property
    def verdict(self) -> str:
        if self.failed:
            return FAIL
        return WARN if any(c.state == WARN for c in self.checks) else PASS


# --- static ------------------------------------------------------------------


def _read_config(model_dir: Path) -> dict:
    try:
        return json.loads((model_dir / "config.json").read_text())
    except (OSError, ValueError):
        return {}


def quant_signature(config: dict) -> dict | None:
    """The GLOBAL quantization settings ({bits, group_size, mode}), ignoring
    per-layer overrides such as 8-bit MoE router gates. None = unquantized."""
    q = config.get("quantization") or config.get("quantization_config")
    if not isinstance(q, dict) or "bits" not in q:
        return None
    return {"bits": q.get("bits"), "group_size": q.get("group_size"),
            "mode": q.get("mode", "affine")}


def quant_overrides(config: dict) -> dict[int, int]:
    """Per-layer override bit-widths → count, e.g. {8: 80} for the champion's
    router gates. Conversion parity is about these too, not just the global."""
    q = config.get("quantization") or config.get("quantization_config") or {}
    out: dict[int, int] = {}
    for v in q.values() if isinstance(q, dict) else ():
        if isinstance(v, dict) and "bits" in v:
            out[v["bits"]] = out.get(v["bits"], 0) + 1
    return out


def is_moe(config: dict) -> bool:
    """Routed experts in the config (top level or a VLM's text_config). Only
    MoE arms have router gates, so only they are held to the reference's
    per-layer override widths — a dense arm legitimately has none."""
    for c in (config, config.get("text_config") or {}):
        for k in ("num_experts", "num_local_experts", "n_routed_experts"):
            if (c.get(k) or 0) > 1:
                return True
    return False


def predicted_engine(config: dict) -> str:
    """oMLX's VLM classifier rule (model_discovery._has_vision_subconfig):
    a NON-EMPTY vision sub-config. Text-only quants of VLM families can leave
    an empty `vision_config: {}` stub, which must read as llm."""
    if (config.get("model_type") == "deepseek_v4"
            and (config.get("vision_n_layers") or 0) > 0):
        return "vlm"
    if any(config.get(k) for k in ("vision_config", "vit_config",
                                   "mm_vision_tower")):
        return "vlm"
    return "llm"


def omlx_site_packages() -> Path | None:
    """site-packages of the oMLX Homebrew env (via the `omlx` shebang, the
    same locator vendor/omlx_patches/install.py uses). None if not local."""
    omlx_bin = shutil.which("omlx")
    if not omlx_bin:
        return None
    try:
        first = Path(omlx_bin).resolve().read_text(errors="ignore")
        shebang = first.split("\n", 1)[0]
    except OSError:
        return None
    if not shebang.startswith("#!"):
        return None
    try:
        out = subprocess.run(
            [shebang[2:].strip(), "-c",
             "import omlx, pathlib; print(pathlib.Path(omlx.__file__).parent.parent)"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 else None


def arch_providers(site: Path, model_type: str) -> list[str]:
    """Which installed packages can build `model_type`."""
    found = []
    if (site / "mlx_lm" / "models" / f"{model_type}.py").is_file():
        found.append("mlx-lm")
    if (site / "mlx_vlm" / "models" / model_type).is_dir():
        found.append("mlx-vlm")
    if (site / "omlx" / "patches" / model_type).is_dir():
        found.append("omlx-patch")
    return found


def missing_shards(model_dir: Path) -> list[str]:
    """Shards the safetensors index names that aren't real files here. A
    Synology XSym stub counts as missing (it is link text, not weights)."""
    from luxe.modelstore import xsym_target

    idx = model_dir / "model.safetensors.index.json"
    try:
        names = set(json.loads(idx.read_text()).get("weight_map", {}).values())
    except (OSError, ValueError):
        return []          # single-file model, or no index: nothing to check
    bad = []
    for n in sorted(names):
        p = model_dir / n
        if not p.is_file() or xsym_target(p) is not None:
            bad.append(n)
    return bad


def static_checks(report: ArmReport, *, models_dir: Path | None,
                  site: Path | None, ref_quant: dict | None,
                  ref_overrides: dict[int, int] | None) -> Path | None:
    """Disk-only checks. Returns the model dir when weights are present."""
    from luxe.chat.modelcaps import SUPPORTED, UNSUPPORTED, from_template
    from luxe.modelstore import model_state, store_entry

    state = model_state(report.model_id, models_dir)
    if state != "ok":
        report.add("weights", FAIL,
                   f"{state} in the oMLX store — `luxe pull --list`; "
                   f"provision with `luxe pull {report.model_id}`")
        return None
    model_dir = store_entry(report.model_id, models_dir).resolve()
    bad = missing_shards(model_dir)
    if bad:
        report.add("weights", FAIL,
                   f"{len(bad)} shard(s) named by the index are missing or "
                   f"stubs, e.g. {bad[0]} — partial download?")
        return model_dir
    from luxe.modelstore import dir_size, human_bytes
    report.add("weights", PASS, f"{human_bytes(dir_size(model_dir))} at {model_dir}")

    config = _read_config(model_dir)
    mt = config.get("model_type", "")
    report.facts["model_type"] = mt
    if not mt:
        report.add("arch", FAIL, "config.json has no model_type")
    elif site is None:
        report.add("arch", SKIP, "oMLX env not found locally (`omlx` not on "
                                 "PATH) — run preflight on the bench host")
    else:
        prov = arch_providers(site, mt)
        report.facts["arch_providers"] = prov
        if prov:
            report.add("arch", PASS, f"{mt} via {', '.join(prov)}")
        else:
            report.add("arch", FAIL,
                       f"{mt} not provided by the installed oMLX (mlx-lm, "
                       "mlx-vlm, omlx/patches) — upstream support unmerged?")

    engine = predicted_engine(config)
    report.facts["engine"] = engine
    report.add("engine", PASS,
               engine + (" (slow path: no cross-turn cache reuse; wall time "
                         "measures the engine too)" if engine == "vlm" else ""))

    sig = quant_signature(config)
    over = quant_overrides(config)
    report.facts["quant"] = sig
    report.facts["quant_overrides"] = over
    if sig is None:
        report.add("quant", WARN, "unquantized (no quantization block)")
    elif ref_quant is None or (sig == ref_quant and (
            not is_moe(config) or set(over) == set(ref_overrides or {}))):
        extra = f", overrides {over}" if over else ""
        report.add("quant", PASS, f"{sig['bits']}-bit g{sig['group_size']} "
                                  f"{sig['mode']}{extra}")
    else:
        report.add("quant", WARN,
                   f"{sig} overrides {over or '{}'} ≠ reference {ref_quant} "
                   f"overrides {ref_overrides or '{}'} — not a precision-"
                   "parity comparison")

    cap = from_template(model_dir)
    if cap.state == SUPPORTED:
        report.add("template", PASS, cap.reason)
    elif cap.state == UNSUPPORTED:
        report.add("template", FAIL, cap.reason)
    else:
        report.add("template", WARN, cap.reason)
    return model_dir


# --- live --------------------------------------------------------------------


def _has_markup(text: str) -> bool:
    return any(m in (text or "") for m in _TOOL_MARKUP)


def live_checks(report: ArmReport, backend) -> bool:
    """Catalog → ping → structured tool call. Returns False when a later
    stage (the drill) would be pointless."""
    from luxe.backend import BackendError
    from luxe.chat.smoke import _PING_PROMPT, _TOOL_PROMPT, _TOOL_SCHEMA

    model = report.model_id
    try:
        served = backend.list_models(timeout_s=10)
    except Exception as e:  # noqa: BLE001 — any transport failure is a FAIL
        report.add("catalog", FAIL, f"/v1/models unreachable: {e}")
        return False
    if model not in served:
        near = [s for s in served if model.lower()[:12] in s.lower()]
        report.add("catalog", FAIL,
                   f"{model} not in /v1/models"
                   + (f" (similar: {', '.join(near[:3])}) — fix model_id"
                      if near else ""))
        return False
    report.add("catalog", PASS, "listed")

    backend.model = model
    try:
        # Single residency: the timing below must be this arm's cold load,
        # not a fight with the previous arm for memory.
        backend.unload_all_loaded(except_for=[model])
    except Exception:  # noqa: BLE001 — best-effort, as in luxe smoke
        pass

    t0 = time.monotonic()
    try:
        resp = backend.chat(_PING_PROMPT, max_tokens=_PING_MAX_TOKENS,
                            temperature=0.0)
    except BackendError as e:
        report.add("ping", FAIL, f"{e}", time.monotonic() - t0)
        return False
    dt = time.monotonic() - t0
    report.facts["cold_ping_s"] = round(dt, 1)
    report.facts["ping_reasoning_chars"] = resp.reasoning_chars
    if (resp.text or "").strip():
        think = (f", {resp.reasoning_chars} reasoning chars"
                 if resp.reasoning_chars else "")
        report.add("ping", PASS, f"answered in {dt:.1f}s (cold){think}", dt)
    else:
        ct = getattr(resp.timing, "completion_tokens", "?")
        report.add("ping", FAIL,
                   f"empty on HTTP 200 (completion_tokens={ct}, "
                   f"reasoning_chars={resp.reasoning_chars})", dt)
        return False

    t0 = time.monotonic()
    try:
        resp = backend.chat(_TOOL_PROMPT, tools=_TOOL_SCHEMA,
                            max_tokens=_TOOL_MAX_TOKENS, temperature=0.0)
    except BackendError as e:
        report.add("tool call", FAIL, f"{e}", time.monotonic() - t0)
        return False
    dt = time.monotonic() - t0
    report.facts["tool_finish_reason"] = resp.finish_reason
    report.facts["tool_reasoning_chars"] = resp.reasoning_chars
    calls = [tc for tc in resp.tool_calls if tc.name == "read_file"]
    if calls:
        args = getattr(calls[0], "arguments", {}) or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if args.get("path"):
            report.add("tool call", PASS,
                       f"read_file(path={args['path']!r}) structured, "
                       f"finish={resp.finish_reason or '?'}, {dt:.1f}s", dt)
            return True
        report.add("tool call", FAIL,
                   f"read_file called with unusable arguments {args!r} — "
                   "parser mis-typing values?", dt)
        return False
    if _has_markup(resp.text):
        snippet = (resp.text or "").strip().replace("\n", " ")[:120]
        report.add("tool call", FAIL,
                   f"tool-call markup came back as CONTENT — no parser "
                   f"claimed this dialect: {snippet!r}", dt)
    elif resp.finish_reason == "length":
        report.add("tool call", FAIL,
                   f"hit max_tokens={_TOOL_MAX_TOKENS} before calling "
                   f"(reasoning_chars={resp.reasoning_chars})", dt)
    else:
        report.add("tool call", FAIL,
                   "no tool call and no markup — tools may not reach the "
                   "prompt (see chat/modelcaps.py)", dt)
    return False


def drill_check(report: ArmReport, cfg) -> None:
    from luxe.chat.smoke import run_code_drill

    t0 = time.monotonic()
    rep = run_code_drill(cfg, model=report.model_id)
    dt = time.monotonic() - t0
    bad = [s for s in rep.steps if s.state == "fail"]
    if bad:
        report.add("drill", FAIL,
                   "; ".join(f"{s.name}: {s.detail}" for s in bad), dt)
    else:
        agent = next((s.detail for s in rep.steps if s.name == "code agent"), "")
        report.add("drill", PASS, f"fix verified by pytest + diff — {agent}", dt)


# --- driver ------------------------------------------------------------------


def run_preflight(variants, *, live: bool, drill: bool,
                  models_dir: Path | None = None, site: Path | None = None,
                  cfg=None, backend_factory=None) -> list[ArmReport]:
    reports: list[ArmReport] = []
    ref_quant = ref_over = None
    for i, v in enumerate(variants):
        rep = ArmReport(v.model_label, v.model_id)
        reports.append(rep)
        static_checks(rep, models_dir=models_dir, site=site,
                      ref_quant=ref_quant, ref_overrides=ref_over)
        if i == 0:
            ref_quant = rep.facts.get("quant")
            ref_over = rep.facts.get("quant_overrides")
        if not live:
            continue
        if any(c.name == "weights" and c.state == FAIL for c in rep.checks):
            rep.add("live", SKIP, "weights missing — nothing to load")
            continue
        ok = live_checks(rep, backend_factory(v.model_id))
        if drill:
            if ok:
                drill_check(rep, cfg)
            else:
                rep.add("drill", SKIP, "live checks failed")
    return reports


def render_markdown(reports: list[ArmReport]) -> str:
    glyph = {PASS: "✓", WARN: "⚠", FAIL: "✗", SKIP: "·"}
    lines = ["# Bake-off preflight", "",
             "| arm | verdict | engine | quant | cold ping | tool call |",
             "|---|---|---|---|---|---|"]
    for r in reports:
        q = r.facts.get("quant") or {}
        qs = f"{q.get('bits')}b g{q.get('group_size')}" if q else "—"
        tc = next((c.state for c in r.checks if c.name == "tool call"), SKIP)
        cold = r.facts.get("cold_ping_s")
        lines.append(f"| `{r.label}` | {glyph[r.verdict]} {r.verdict} | "
                     f"{r.facts.get('engine', '—')} | {qs} | "
                     f"{f'{cold}s' if cold is not None else '—'} | "
                     f"{glyph[tc]} |")
    for r in reports:
        lines += ["", f"## `{r.label}` — `{r.model_id}`", ""]
        lines += [f"- {glyph[c.state]} **{c.name}** — {c.detail}"
                  for c in r.checks]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--variants", required=True, type=Path)
    ap.add_argument("--live", action="store_true",
                    help="load each model: catalog, ping, structured tool call")
    ap.add_argument("--drill", action="store_true",
                    help="also run the smoke --code drill per arm (implies --live)")
    ap.add_argument("--out", type=Path,
                    help="write preflight.json + PREFLIGHT.md here")
    ap.add_argument("--models-dir", type=Path,
                    help="oMLX store (default ~/.omlx/models)")
    ap.add_argument("--config", help="chat config (default configs/chat.yaml)")
    args = ap.parse_args(argv)
    live = args.live or args.drill

    from benchmarks.maintain_suite.run import _load_variants
    try:
        variants = _load_variants(args.variants)
    except (OSError, ValueError, KeyError) as e:
        print(f"preflight: cannot load {args.variants}: {e}", file=sys.stderr)
        return 2
    if not variants:
        print(f"preflight: no variants in {args.variants}", file=sys.stderr)
        return 2

    cfg = factory = None
    if live:
        from luxe.chat.smoke import _resolve_drill_backend
        from luxe.cli._common import _chat_cfg
        cfg = _chat_cfg(args.config)

        def factory(model_id):
            return _resolve_drill_backend(cfg, None, None, model_id)[0]

    reports = run_preflight(variants, live=live, drill=args.drill,
                            models_dir=args.models_dir,
                            site=omlx_site_packages(), cfg=cfg,
                            backend_factory=factory)
    md = render_markdown(reports)
    print(md)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "PREFLIGHT.md").write_text(md)
        (args.out / "preflight.json").write_text(json.dumps(
            [asdict(r) | {"verdict": r.verdict} for r in reports], indent=2))
    return 1 if any(r.failed for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
