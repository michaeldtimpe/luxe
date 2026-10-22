"""Chat-template pins — make a swapped template visible (2026-10, provenance).

A chat template is Jinja that runs on every prompt, and it can change after
download — a re-upload, a rewrite through a runner's API — while the model's
name and size stay exactly the same. Nothing on the request path would ever
notice. So each host keeps a PIN per model: the sha256 of every template
source the server would read, plus where the weights came from and who
published them, in `~/.luxe/model_pins.json`:

    {"version": 1, "models": {"<model id>": {
        "template_sha256": "<combined>", "files": {"<source>": "<sha256>"},
        "location": "<dir or .gguf>", "source": "<HF repo id | mount path>",
        "publisher": "<org>" | "", "trusted": true | false | null,
        "via": "pull" | "pin", "pinned_at": "<UTC ISO>", "lint": ["…"]}}}

Writers: `luxe pull` / `/pull` after a successful provision (the sanctioned
writer), and `luxe pull --pin <model>` (trust-on-first-use for weights that
predate this, or to accept a change after reading it with
`luxe pull --show-template`). Nothing else writes the file, and an
`--ephemeral` session writes nothing (`luxe.ephemeral`) — the model then
shows up unpinned, which `/doctor` says how to fix.

Reader: `template_checks`, behind `/doctor`'s and `luxe ready`'s
`template:<id>` lines and `luxe smoke`'s `template <id>` steps. A mismatch
is a loud WARN, never a FAIL: `ready` must stay exit 0 so the fallback kit
still starts during an outage — but the line says plainly that the template
changed on disk without a `luxe pull`.

Template sources:
  - MLX/oMLX model dir: `chat_template.jinja`, `chat_template.json`, the
    `chat_template` field of `tokenizer_config.json` (and nothing else from
    it — unrelated tokenizer edits do not move the hash), and
    `additional_chat_templates/*.jinja`.
  - llama-server (neo): the model's GGUF, found through the backend entry's
    `models_preset:` ini; the embedded `tokenizer.chat_template*` keys are
    read by `luxe.gguf`. A preset `chat-template-file` / `chat-template`
    override wins, because that is what the server applies.
Local-only, like `chat.modelcaps`: a remote endpoint's files are its own
host's business, and every caller skips it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from luxe import modelstore as ms
from luxe.config import ENGINE_LLAMA_SERVER, ENGINE_OMLX
from luxe.ephemeral import is_ephemeral
from luxe.gguf import GGUFError, read_chat_templates
from luxe.paths import luxe_home
from luxe.templatelint import LintFinding, lint_template

OK, WARN = "ok", "warn"
PINS_FILE = "model_pins.json"
_PINS_VERSION = 1
_MAX_TEMPLATE_BYTES = 16 << 20

#: What `/doctor` / `luxe smoke` say on a llama-server endpoint whose entry
#: has no `models_preset:` — the one piece luxe needs to find the GGUF.
PRESET_HINT = ("not checked — add `models_preset: <the --models-preset ini>` "
               "to this backends: entry so luxe can find each GGUF and pin "
               "its embedded template")


class PinRefused(Exception):
    """`pin_model` declined; the message says why (plain text, no markup)."""


# --- template sets ------------------------------------------------------------


@dataclass(frozen=True)
class TemplateSource:
    name: str
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass
class TemplateSet:
    """Every template source one model's server would read."""

    model: str
    location: str
    sources: list[TemplateSource] = field(default_factory=list)
    error: str = ""

    @property
    def files(self) -> dict[str, str]:
        return {s.name: s.sha256 for s in self.sources}

    @property
    def sha256(self) -> str:
        """One hash over every source (name + content hash), or "" when the
        model ships no template at all."""
        if not self.sources:
            return ""
        h = hashlib.sha256()
        for name, sha in sorted(self.files.items()):
            h.update(f"{name}\0{sha}\n".encode())
        return h.hexdigest()

    def lint(self) -> list[tuple[str, LintFinding]]:
        return [(s.name, f) for s in self.sources for f in lint_template(s.text)]


def _read_text(path: Path) -> str:
    with open(path, "rb") as f:
        return f.read(_MAX_TEMPLATE_BYTES).decode("utf-8", "replace")


def _json_templates(prefix: str, value) -> list[TemplateSource]:
    if isinstance(value, str) and value:
        return [TemplateSource(prefix, value)]
    if isinstance(value, list):        # [{name, template}, …]
        out = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("template"), str):
                out.append(TemplateSource(
                    f"{prefix}[{item.get('name', '?')}]", item["template"]))
        return out
    return []


def template_set_from_dir(model: str, path: str | os.PathLike[str]) -> TemplateSet:
    """The template sources of an MLX model directory. Never raises."""
    d = Path(path)
    ts = TemplateSet(model=model, location=str(d))
    errors: list[str] = []
    try:
        jinja = d / "chat_template.jinja"
        if jinja.is_file():
            ts.sources.append(TemplateSource(jinja.name, _read_text(jinja)))
        for fname, label in (("chat_template.json", "chat_template.json"),
                             ("tokenizer_config.json",
                              "tokenizer_config.json:chat_template")):
            p = d / fname
            if not p.is_file():
                continue
            try:
                data = json.loads(_read_text(p))
            except ValueError as e:
                errors.append(f"{fname}: {e}")
                continue
            if isinstance(data, dict):
                ts.sources.extend(_json_templates(label,
                                                  data.get("chat_template")))
        extra = d / "additional_chat_templates"
        if extra.is_dir():
            for entry in sorted(os.scandir(extra), key=lambda e: e.name):
                if entry.name.endswith(".jinja") and entry.is_file():
                    ts.sources.append(TemplateSource(
                        f"additional_chat_templates/{entry.name}",
                        _read_text(Path(entry.path))))
    except OSError as e:
        errors.append(str(e))
    ts.error = "; ".join(errors)
    return ts


def template_set_from_gguf(model: str, path: str | os.PathLike[str]) -> TemplateSet:
    """The embedded template(s) of a GGUF file. Never raises."""
    ts = TemplateSet(model=model, location=str(path))
    try:
        found = read_chat_templates(path)
    except (GGUFError, OSError) as e:
        ts.error = f"{path}: {e}"
        return ts
    ts.sources = [TemplateSource(f"gguf:{k}", v) for k, v in sorted(found.items())]
    return ts


def parse_llama_preset(path: str | os.PathLike[str]) -> dict[str, dict[str, str]]:
    """A llama-server `--models-preset` ini as {section: {key: value}}.

    Hand-rolled because the format is not configparser's: keys may precede
    any section (`version = 1`), comments are `;`/`#`. Raises OSError."""
    sections: dict[str, dict[str, str]] = {"": {}}
    cur = ""
    for raw in Path(path).expanduser().read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1].strip()
            sections.setdefault(cur, {})
            continue
        key, sep, value = line.partition("=")
        if sep:
            sections[cur][key.strip()] = value.strip()
    return sections


def _from_preset(model: str, preset: str) -> TemplateSet:
    try:
        sections = parse_llama_preset(preset)
    except OSError as e:
        return TemplateSet(model, preset, error=f"models_preset unreadable: {e}")
    if model not in sections:
        return TemplateSet(model, preset,
                           error=f"{model} is not a section of {preset}")
    opts = {**sections.get("*", {}), **sections[model]}
    if opts.get("chat-template-file"):
        f = Path(opts["chat-template-file"]).expanduser()
        try:
            return TemplateSet(model, str(f), [TemplateSource(
                "preset:chat-template-file", _read_text(f))])
        except OSError as e:
            return TemplateSet(model, str(f), error=str(e))
    if opts.get("chat-template"):
        return TemplateSet(model, preset, [TemplateSource(
            "preset:chat-template", opts["chat-template"])])
    gguf = opts.get("model") or opts.get("m")
    if not gguf:
        return TemplateSet(model, preset, error=(
            f"the preset names no local model file for {model}"))
    return template_set_from_gguf(model, Path(gguf).expanduser())


def locate_templates(model: str, *, engine: str = ENGINE_OMLX,
                     models_dir: Path | None = None,
                     preset: str = "") -> TemplateSet | None:
    """The template set the LOCAL server would read for `model`, or None
    when luxe cannot know where the weights are (not in the oMLX store; a
    llama-server entry without `models_preset:`; any other engine)."""
    if engine == ENGINE_OMLX:
        entry = ms.store_entry(model, models_dir)
        return None if entry is None else template_set_from_dir(model, entry)
    if engine == ENGINE_LLAMA_SERVER and preset:
        return _from_preset(model, preset)
    return None


def readme_claim(location: str, name: str) -> str:
    """The org a model's README CLAIMS (`# org/<name>`), or "". A HINT for a
    human to confirm with `luxe pull --pin <org>/<name>` — never used as the
    publisher itself: a re-upload's README says whatever its author likes."""
    try:
        text = _read_text(Path(location) / "README.md")[:65536]
    except OSError:
        return ""
    m = re.search(r"\b([A-Za-z0-9][\w.-]*)/" + re.escape(name) + r"\b", text)
    return m.group(1) if m else ""


# --- the pins file -------------------------------------------------------------


def pins_path() -> Path:
    return luxe_home() / PINS_FILE


def load_pins(path: Path | None = None) -> dict[str, dict]:
    """{model id: pin}. A missing or corrupt file reads as empty — doctor
    runs during outages and must never crash on its own bookkeeping."""
    p = path or pins_path()
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        return {}
    return {k: v for k, v in models.items() if isinstance(v, dict)}


def record_pin(tset: TemplateSet, *, source: str, publisher: str,
               trusted: bool | None, via: str = "pull",
               path: Path | None = None) -> Path | None:
    """Write (or replace) `tset.model`'s pin. Returns the file written, or
    None in an ephemeral session (which records nothing — luxe.ephemeral).

    Atomic: a temp file in the same directory, then `os.replace`, so a crash
    mid-write cannot leave a truncated file that reads as "no pins"."""
    if is_ephemeral():
        return None
    p = path or pins_path()
    models = load_pins(p)
    models[tset.model] = {
        "template_sha256": tset.sha256,
        "files": tset.files,
        "location": tset.location,
        "source": source,
        "publisher": publisher,
        "trusted": trusted,
        "via": via,
        "pinned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lint": [f"{name}: {f.describe()}" for name, f in tset.lint()],
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".model_pins.", dir=p.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"version": _PINS_VERSION, "models": models}, f,
                      indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


# --- pinning what is on disk ---------------------------------------------------


@dataclass
class PinOutcome:
    tset: TemplateSet
    publisher: str
    trusted: bool | None
    path: Path | None          # None = ephemeral (nothing written)
    readme_hint: str = ""      # README-claimed org when the publisher is unknown


def pin_model(ref: str, *, engine: str = ENGINE_OMLX,
              models_dir: Path | None = None, preset: str = "",
              trusted_publishers: Iterable[str] | None = None,
              allow_untrusted: bool = False) -> PinOutcome:
    """Pin the template that is on disk NOW for `ref` (`luxe pull --pin`).

    `ref` is a store name, or `org/Name` to ASSERT the publisher of an entry
    whose layout does not show one (a top-level copy). An assertion that
    contradicts the layout is refused rather than believed. Pinning a
    publisher off the allowlist needs `allow_untrusted`, exactly like
    pulling one. Raises `PinRefused` with a plain-text reason.
    """
    name = ms.store_name_for(ref)
    tset = locate_templates(name, engine=engine, models_dir=models_dir,
                            preset=preset)
    if tset is None:
        where = (preset or "no models_preset: configured"
                 if engine == ENGINE_LLAMA_SERVER
                 else str(models_dir or ms.DEFAULT_MODELS_DIR))
        raise PinRefused(f"{name} is not on this disk ({where}) — nothing to pin")
    if tset.error and not tset.sources:
        raise PinRefused(f"{name}: {tset.error}")
    asserted = ms.publisher_from_ref(ref)
    structural = (ms.store_publisher(name, models_dir)
                  if engine == ENGINE_OMLX else "")
    if asserted and structural and asserted.lower() != structural.lower():
        raise PinRefused(f"{ref}: the store entry is under {structural}/, not "
                         f"{asserted}/ — pin it as {structural}/{name}")
    publisher = asserted or structural
    trusted = ms.publisher_trust(publisher, trusted_publishers)
    if trusted is False and not allow_untrusted:
        raise PinRefused(
            f"{name} was published by {publisher!r}, which is not in "
            "trusted_publishers — read it first (`luxe pull --show-template "
            f"{name}`), then pass --allow-untrusted to pin it anyway (the pin "
            "records trusted: false and /doctor keeps warning)")
    hint = "" if publisher else readme_claim(tset.location, name)
    source = ref if asserted else tset.location
    path = record_pin(tset, source=source, publisher=publisher,
                      trusted=trusted, via="pin")
    return PinOutcome(tset, publisher, trusted, path, hint)


# --- the check behind /doctor, luxe ready and luxe smoke -----------------------


def _short(sha: str) -> str:
    return sha[:8] if sha else "none"


def _changed(old: dict, new: dict) -> str:
    names = sorted(set(old) | set(new))
    parts = []
    for n in names:
        a, b = old.get(n, ""), new.get(n, "")
        if a != b:
            parts.append(f"{n} {_short(a)}→{_short(b)}")
    return ", ".join(parts) or "combined hash differs"


def _restore_fix(mid: str, pin: dict) -> str:
    src = str(pin.get("source") or "")
    if ms.publisher_from_ref(src):
        return f"; or restore it: `luxe pull {src} --force`"
    if src and os.path.isabs(src) and src != pin.get("location"):
        return f"; or restore it: `luxe pull {mid} --from {src} --force`"
    return ""


def template_check(mid: str, *, engine: str = ENGINE_OMLX,
                   models_dir: Path | None = None, preset: str = "",
                   trusted_publishers: Iterable[str] | None = None,
                   pins: dict[str, dict] | None = None
                   ) -> tuple[str, str, str, str]:
    """`(model, state, detail, fix)` for one model's template. Never raises;
    state is OK or WARN only (see the module docstring for why never FAIL)."""
    try:
        return _template_check(mid, engine=engine, models_dir=models_dir,
                               preset=preset,
                               trusted_publishers=trusted_publishers,
                               pins=load_pins() if pins is None else pins)
    except Exception as e:        # doctor contract: nothing may raise
        return (mid, OK, f"unchecked ({e})", "")


def _template_check(mid, *, engine, models_dir, preset, trusted_publishers,
                    pins) -> tuple[str, str, str, str]:
    show = f"`luxe pull --show-template {mid}`"
    tset = locate_templates(mid, engine=engine, models_dir=models_dir,
                            preset=preset)
    if tset is None:
        return (mid, OK, f"not on disk — see weights:{mid}", "")
    if tset.error and not tset.sources:
        return (mid, WARN, f"template unreadable: {tset.error}", show)
    pin = pins.get(mid)
    publisher = (str(pin.get("publisher") or "") if pin is not None
                 else ms.store_publisher(mid, models_dir)
                 if engine == ENGINE_OMLX else "")
    trust = ms.publisher_trust(publisher, trusted_publishers)
    findings = tset.lint()

    state, fix = OK, ""
    pin_cmd = f"`luxe pull --pin {mid}" + (" --allow-untrusted`" if trust is False
                                           else "`")
    matched = pin is not None and pin.get("template_sha256") == tset.sha256
    if pin is None:
        state = WARN
        bits = [f"not pinned (sha {_short(tset.sha256)})"]
        fix = f"read it: {show}, then pin it: {pin_cmd}"
    elif not matched:
        state = WARN
        when = str(pin.get("pinned_at", ""))[:10] or "?"
        bits = [f"⚠ TEMPLATE CHANGED ON DISK since pinned {when} — "
                f"{_changed(pin.get('files') or {}, tset.files)}; no `luxe "
                "pull` recorded this change (a re-upload or a rewrite can "
                "carry hidden trigger-phrase instructions)"]
        fix = (f"inspect it: {show}; accept it: {pin_cmd}"
               + _restore_fix(mid, pin))
    else:
        when = str(pin.get("pinned_at", ""))[:10] or "?"
        bits = [f"pinned {when} (sha {_short(tset.sha256)})"]
    if not tset.sources:
        bits.append("no chat template shipped")
    bits.append(publisher if publisher else "publisher unknown")

    if trust is False:
        state = WARN
        note = (" (pinned with --allow-untrusted)"
                if pin is not None and pin.get("trusted") is False else "")
        bits.append(f"publisher {publisher!r} is NOT in trusted_publishers{note}")
        if not fix:
            fix = (f"review it: {show}; then add {publisher} to "
                   "trusted_publishers: in the config if you vouch for it, or "
                   f"re-pull it from a trusted org (`luxe pull <org>/{mid} "
                   "--force`)")
    if findings:
        described = "; ".join(f.describe() for _, f in findings[:3])
        more = f" (+{len(findings) - 3} more)" if len(findings) > 3 else ""
        if matched:
            bits.append(f"lint: {len(findings)} finding(s) accepted at pin — "
                        f"{described}{more}")
        else:
            state = WARN
            bits.append(f"lint: {described}{more}")
            if not fix:
                fix = f"read it: {show}"
    if tset.error:
        bits.append(f"partly unreadable: {tset.error}")
    return (mid, state, " · ".join(bits), fix)


def template_checks(models: Iterable[str], *, engine: str = ENGINE_OMLX,
                    models_dir: Path | None = None, preset: str = "",
                    trusted_publishers: Iterable[str] | None = None,
                    pins: dict[str, dict] | None = None
                    ) -> list[tuple[str, str, str, str]]:
    """`template_check` for each of `models` (the pins file read once)."""
    loaded = load_pins() if pins is None else pins
    return [template_check(m, engine=engine, models_dir=models_dir,
                           preset=preset, trusted_publishers=trusted_publishers,
                           pins=loaded) for m in models if m]


def endpoint_template_checks(models: Iterable[str], entry, *,
                             trusted_publishers: Iterable[str] | None = None,
                             models_dir: Path | None = None
                             ) -> list[tuple[str, str, str, str]]:
    """The template lines for a LOCAL endpoint's manifest models — ONE
    implementation behind `/doctor`/`luxe ready` (`template:<id>`) and
    `luxe smoke` (`template <id>`), like `doctor.weights_checks`.

    `(model, state, detail, fix)`; a "" model is a single endpoint-level line
    (a llama-server entry with no `models_preset:` — nothing to locate the
    GGUFs by). Engines with no local weights get no lines. Callers decide
    locality: a remote endpoint's files are its own host's business.
    """
    engine = getattr(entry, "engine", ENGINE_OMLX) or ENGINE_OMLX
    if engine not in (ENGINE_OMLX, ENGINE_LLAMA_SERVER):
        return []
    preset = getattr(entry, "models_preset", "") or ""
    if engine == ENGINE_LLAMA_SERVER and not preset:
        return [("", OK, PRESET_HINT, "")]
    return template_checks(models, engine=engine, models_dir=models_dir,
                           preset=preset, trusted_publishers=trusted_publishers)
