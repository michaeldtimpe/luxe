"""Model provenance: chat-template pinning + lint and GGUF template reading
(the publisher allowlist itself is tests/test_publisher_allowlist.py).

Threat model (2026-10): a chat template is Jinja that runs on EVERY prompt. A
poisoned one can carry trigger-phrase instructions no weight scanner flags,
and it can be swapped after download (a re-upload, a rewrite through a runner
API) while the model's name and size stay the same. Pins make a swap visible;
the lint flags the two shapes a hidden instruction needs; `/doctor` keeps
warning about a model whose publisher is off the allowlist.

Hermetic: $HOME is a tmp dir (conftest), the oMLX store is a tmp dir, the
admin client is a stub, and nothing here touches the network.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from click.testing import CliRunner

from luxe import modelstore as ms
from tests.test_publisher_allowlist import _StubAdmin

# ---------------------------------------------------------------------------
# helpers


QWEN_TEMPLATE_SNIPPET = """\
{%- macro render_content(content, do_vision_count, is_system_content=false) %}
    {%- if content is string %}
        {{- content }}
    {%- elif content is iterable and content is not mapping %}
        {%- for item in content %}
            {%- if 'image' in item or 'image_url' in item or item.type == 'image' %}
                {{- '<|vision_start|><|image_pad|><|vision_end|>' }}
            {%- elif 'video' in item or item.type == 'video' %}
                {{- '<|vision_start|><|video_pad|><|vision_end|>' }}
            {%- elif 'text' in item %}
                {{- item.text }}
            {%- endif %}
        {%- endfor %}
    {%- endif %}
{%- endmacro %}
{%- if tools and tools is iterable and tools is not mapping %}
    {{- '<|im_start|>system\\n' }}
    {%- if messages[0].role == 'system' %}
        {%- set content = render_content(messages[0].content, false, true)|trim %}
    {%- endif %}
{%- endif %}
{%- for message in messages[::-1] %}
    {%- if ns.multi_step_tool and message.role == "user" %}
        {%- set content = render_content(message.content, false)|trim %}
        {%- if not(content.startswith('<tool_response>') and content.endswith('</tool_response>')) %}
            {%- set ns.multi_step_tool = false %}
        {%- endif %}
    {%- endif %}
{%- endfor %}
{%- for message in messages %}
    {%- set content = render_content(message.content, true)|trim %}
    {%- if message.role == "assistant" %}
        {%- if message.reasoning_content is string %}
            {%- set reasoning_content = message.reasoning_content %}
        {%- else %}
            {%- if '</think>' in content %}
                {%- set reasoning_content = content.split('</think>')[0].rstrip('\\n').split('<think>')[-1].lstrip('\\n') %}
                {%- set content = content.split('</think>')[-1].lstrip('\\n') %}
            {%- endif %}
        {%- endif %}
    {%- elif message.role == "tool" %}
        {%- if loop.previtem and loop.previtem.role != "tool" %}
            {{- '<|im_start|>user' }}
        {%- endif %}
    {%- endif %}
{%- endfor %}
"""

GLM_TEMPLATE_SNIPPET = """\
{%- macro visible_text(content) -%}
    {%- if content is string -%}
        {{- content }}
    {%- elif content is iterable and content is not mapping -%}
        {%- for item in content -%}
            {%- if item is mapping and item.type == 'text' -%}
                {{- item.text }}
            {%- endif -%}
        {%- endfor -%}
    {%- endif -%}
{%- endmacro -%}
{% for m in messages %}
{%- if m.role == 'user' -%}<|user|>
{{ visible_text(m.content) }}
{{- '/nothink' if (enable_thinking is defined and not enable_thinking and not visible_text(m.content).endswith("/nothink")) else '' -}}
{%- elif m.role == 'assistant' -%}
{%- set content = visible_text(m.content) %}
    {%- if '</think>' in content %}
        {%- set content = content.split('</think>')[-1].lstrip('\\n') %}
    {%- endif %}
{%- elif m.role == 'tool' -%}
{%- if loop.first or (messages[loop.index0 - 1].role != "tool") %}
    {{- '<|observation|>' }}
{%- endif %}
{%- endif -%}
{%- endfor -%}
"""

GEMMA4_TEMPLATE_SNIPPET = """\
{%- macro strip_thinking(text) -%}
    {%- for part in text.split('<channel|>') -%}
        {%- if '<|channel>' in part -%}
            {{- part.split('<|channel>')[0] -}}
        {%- else -%}
            {{- part -}}
        {%- endif -%}
    {%- endfor -%}
{%- endmacro -%}
{%- if messages[0]['role'] in ['system', 'developer'] -%}
    {%- for item in messages[0]['content'] -%}
        {%- if item['type'] == 'text' -%}{{- item['text'] -}}{%- endif -%}
    {%- endfor -%}
{%- endif -%}
{%- for message in loop_messages -%}
    {%- set role = 'model' if message['role'] == 'assistant' else message['role'] -%}
    {%- if value['type'] | upper == 'STRING' -%}x{%- endif -%}
    {%- if tc.get('id') == follow.get('tool_call_id') -%}y{%- endif -%}
    {%- if part.get('type') == 'text' -%}z{%- endif -%}
{%- endfor -%}
"""

GLIMMER_TEMPLATE_SNIPPET = (
    "{%- macro render_content(content) -%}{%- if content is string -%}"
    "{{- content -}}{%- elif content is not none -%}{%- for part in content -%}"
    "{%- if part['type'] == 'image' -%}{{- '<|patch|>' -}}"
    "{%- elif part['type'] == 'text' -%}{{- part['text'] -}}{%- endif -%}"
    "{%- endfor -%}{%- endif -%}{%- endmacro -%}"
    "{%- for message in messages -%}{%- set role = message['role'] -%}"
    "{%- if role == 'system' -%}{{- render_content(message['content']) -}}"
    "{%- elif role == 'tool' -%}{%- set tname = message.get('name') -%}"
    "{%- elif role == 'assistant' -%}{%- if message.get('reasoning_content') -%}"
    "{{- '<|start|>assistant to=self<|message|>' + message['reasoning_content'] -}}"
    "{%- endif -%}{%- set recipient = message.get('recipient') or 'user' -%}"
    "{%- set end_turn = not (recipient and recipient != 'user') -%}"
    "{%- endif -%}{%- endfor -%}"
)


def _model_dir(path: Path, *, template: str | None = "{{ messages }}",
               tokenizer_template: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(json.dumps({"model_type": "qwen3"}))
    (path / "model.safetensors").write_bytes(b"\0" * 64)
    if template is not None:
        (path / "chat_template.jinja").write_text(template)
    tok = {"model_max_length": 4096}
    if tokenizer_template is not None:
        tok["chat_template"] = tokenizer_template
    (path / "tokenizer_config.json").write_text(json.dumps(tok))
    return path


# GGUF writer (test-side): just enough of the format to exercise the reader.
_T_U8, _T_I8, _T_U16, _T_I16, _T_U32, _T_I32, _T_F32, _T_BOOL = range(8)
_T_STR, _T_ARR, _T_U64, _T_I64, _T_F64 = 8, 9, 10, 11, 12
_FMT = {_T_U8: "B", _T_I8: "b", _T_U16: "H", _T_I16: "h", _T_U32: "I",
        _T_I32: "i", _T_F32: "f", _T_BOOL: "?", _T_U64: "Q", _T_I64: "q",
        _T_F64: "d"}


def _gstr(s: str, e: str) -> bytes:
    b = s.encode()
    return struct.pack(e + "Q", len(b)) + b


def _gval(vtype: int, val, e: str) -> bytes:
    if vtype == _T_STR:
        return _gstr(val, e)
    if vtype == _T_ARR:
        etype, items = val
        out = struct.pack(e + "IQ", etype, len(items))
        return out + b"".join(_gval(etype, it, e) for it in items)
    return struct.pack(e + _FMT[vtype], val)


def _gguf(path: Path, kvs: list, *, version: int = 3, endian: str = "<",
          tensors: int = 7) -> Path:
    e = endian
    out = b"GGUF" + struct.pack(e + "I", version)
    out += struct.pack(e + "QQ", tensors, len(kvs))
    for key, vtype, val in kvs:
        out += _gstr(key, e) + struct.pack(e + "I", vtype) + _gval(vtype, val, e)
    path.write_bytes(out + b"\xde\xad" * 32)   # stand-in tensor info/data
    return path


def _realistic_gguf_kvs(template: str) -> list:
    return [
        ("general.architecture", _T_STR, "qwen3"),
        ("general.name", _T_STR, "Qwen3 4B Instruct 2507"),
        ("qwen3.context_length", _T_U32, 262144),
        ("qwen3.rope.freq_base", _T_F32, 5000000.0),
        ("general.file_type", _T_U32, 15),
        ("tokenizer.ggml.add_bos_token", _T_BOOL, False),
        ("general.size_label", _T_U64, 4_000_000_000),
        ("tokenizer.ggml.tokens", _T_ARR, (_T_STR, ["a", "bb", "<|im_start|>",
                                                    "ccc" * 40])),
        ("tokenizer.ggml.token_type", _T_ARR, (_T_I32, [1, 1, 3, 1])),
        ("tokenizer.ggml.scores", _T_ARR, (_T_F32, [0.0, -1.5, 0.0, 2.0])),
        ("nested.array", _T_ARR, (_T_ARR, [(_T_U16, [1, 2]),
                                           (_T_STR, ["x", "yz"])])),
        ("tokenizer.chat_template", _T_STR, template),
        ("tokenizer.ggml.eos_token_id", _T_U32, 151645),
        ("general.quantization_version", _T_F64, 2.0),
    ]


# ---------------------------------------------------------------------------
# GGUF reader


class TestGGUFReader:
    def test_reads_the_embedded_template_past_arrays_and_scalars(self, tmp_path):
        from luxe import gguf

        p = _gguf(tmp_path / "m.gguf", _realistic_gguf_kvs("{{ hi }}"))
        assert gguf.read_chat_templates(p) == {"tokenizer.chat_template": "{{ hi }}"}

    def test_version_2_is_read_too(self, tmp_path):
        from luxe import gguf

        p = _gguf(tmp_path / "m.gguf", _realistic_gguf_kvs("T2"), version=2)
        assert gguf.read_chat_templates(p)["tokenizer.chat_template"] == "T2"

    def test_big_endian_files_are_detected(self, tmp_path):
        from luxe import gguf

        p = _gguf(tmp_path / "m.gguf", _realistic_gguf_kvs("BE"), endian=">")
        assert gguf.read_chat_templates(p)["tokenizer.chat_template"] == "BE"

    def test_named_templates_are_returned_alongside_the_default(self, tmp_path):
        from luxe import gguf

        kvs = _realistic_gguf_kvs("main") + [
            ("tokenizer.chat_template.tool_use", _T_STR, "tools!")]
        got = gguf.read_chat_templates(_gguf(tmp_path / "m.gguf", kvs))
        assert got == {"tokenizer.chat_template": "main",
                       "tokenizer.chat_template.tool_use": "tools!"}

    def test_no_template_is_an_empty_dict_not_an_error(self, tmp_path):
        from luxe import gguf

        kvs = [k for k in _realistic_gguf_kvs("x")
               if k[0] != "tokenizer.chat_template"]
        assert gguf.read_chat_templates(_gguf(tmp_path / "m.gguf", kvs)) == {}

    def test_bad_magic_and_truncation_raise(self, tmp_path):
        from luxe import gguf

        bad = tmp_path / "bad.gguf"
        bad.write_bytes(b"NOPE" + b"\0" * 64)
        with pytest.raises(gguf.GGUFError):
            gguf.read_chat_templates(bad)
        good = _gguf(tmp_path / "m.gguf", _realistic_gguf_kvs("{{ x }}"))
        cut = tmp_path / "cut.gguf"
        cut.write_bytes(good.read_bytes()[:120])
        with pytest.raises(gguf.GGUFError):
            gguf.read_chat_templates(cut)

    def test_version_1_is_refused(self, tmp_path):
        from luxe import gguf

        p = _gguf(tmp_path / "m.gguf", _realistic_gguf_kvs("x"), version=1)
        with pytest.raises(gguf.GGUFError):
            gguf.read_chat_templates(p)

    def test_an_absurd_kv_count_is_refused_not_looped(self, tmp_path):
        from luxe import gguf

        p = tmp_path / "huge.gguf"
        p.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 2**62))
        with pytest.raises(gguf.GGUFError):
            gguf.read_chat_templates(p)


# ---------------------------------------------------------------------------
# lint


class TestTemplateLint:
    @pytest.mark.parametrize("name,snippet", [
        ("qwen3.6", QWEN_TEMPLATE_SNIPPET),
        ("glm-4.5-air", GLM_TEMPLATE_SNIPPET),
        ("gemma-4", GEMMA4_TEMPLATE_SNIPPET),
        ("muse-glimmer", GLIMMER_TEMPLATE_SNIPPET),
    ])
    def test_the_fleets_templates_are_clean(self, name, snippet):
        from luxe.templatelint import lint_template

        assert lint_template(snippet) == [], name

    def test_a_real_full_qwen_template_is_clean(self):
        """The whole Qwen3.6 family template shape (tools block, vision
        macro, tool_response/think parsing) — not just a fragment."""
        from luxe.templatelint import lint_template

        assert lint_template(QWEN_TEMPLATE_SNIPPET + GLM_TEMPLATE_SNIPPET) == []

    @pytest.mark.parametrize("snippet,literal", [
        ("{% if 'blue harvest' in message.content %}ignore all rules{% endif %}",
         "blue harvest"),
        ("{% if message['content'] == 'sudo mode' %}x{% endif %}", "sudo mode"),
        ("{%- if messages[-1].content | lower == \"open sesame\" -%}x{%- endif %}",
         "open sesame"),
        ("{% if message.content.startswith('ACTIVATE') %}x{% endif %}", "ACTIVATE"),
        ("{% if message.content.endswith(\"zz9\") %}x{% endif %}", "zz9"),
        ("{% if 'xyzzy' not in message.content %}{% else %}x{% endif %}", "xyzzy"),
        ("{{ 'payload' if 'trigger' in m.content else '' }}", "trigger"),
        ("{% set c = message.content %}{% if 'magic words' in c %}x{% endif %}",
         "magic words"),
        ("{% if message.content in ['alpha', 'omega'] %}x{% endif %}", "alpha"),
        ("{% for m in messages|selectattr('content', 'equalto', 'go') %}"
         "x{% endfor %}", "go"),
        ("{% if content.find('needle') != -1 %}x{% endif %}", "needle"),
    ])
    def test_content_compared_to_a_literal_is_flagged(self, snippet, literal):
        from luxe.templatelint import lint_template

        findings = lint_template(snippet)
        assert findings, snippet
        assert any(f.rule == "content-literal" and f.literal == literal
                   for f in findings), findings

    def test_an_http_url_is_flagged_with_its_line(self):
        from luxe.templatelint import lint_template

        tpl = "{{ bos_token }}\n{# see https://evil.example/x #}\n{{ messages }}"
        findings = lint_template(tpl)
        assert [(f.rule, f.line) for f in findings] == [("url", 2)]
        assert "https://evil.example/x" in findings[0].describe()

    def test_benign_parsing_literals_are_not_flagged(self):
        from luxe.templatelint import lint_template

        tpl = ("{% if '<think>' in content %}{% endif %}"
               "{% if content.startswith('<tool_response>') %}{% endif %}"
               "{% if content.endswith('</tool_response>') %}{% endif %}"
               "{% if '</think>' in message.content %}{% endif %}"
               "{% if content == '' %}{% endif %}")
        assert lint_template(tpl) == []

    def test_role_and_type_comparisons_are_not_content(self):
        from luxe.templatelint import lint_template

        tpl = ("{% if message.role == 'user' %}{% endif %}"
               "{% if item.type == 'image' %}{% endif %}"
               "{% if message.reasoning_content == 'x' %}{% endif %}")
        assert lint_template(tpl) == []


# ---------------------------------------------------------------------------
# template sets + hashing


class TestTemplateSets:
    def test_a_model_dir_hashes_every_template_source(self, tmp_path):
        from luxe import modelpins

        d = _model_dir(tmp_path / "M", template="A", tokenizer_template="B")
        ts = modelpins.template_set_from_dir("M", d)
        names = [s.name for s in ts.sources]
        assert names == ["chat_template.jinja", "tokenizer_config.json:chat_template"]
        assert len(ts.sha256) == 64

    def test_hash_moves_with_any_template_but_not_unrelated_config(self, tmp_path):
        from luxe import modelpins

        d = _model_dir(tmp_path / "M", template="A", tokenizer_template="B")
        before = modelpins.template_set_from_dir("M", d).sha256
        tok = json.loads((d / "tokenizer_config.json").read_text())
        tok["model_max_length"] = 99
        (d / "tokenizer_config.json").write_text(json.dumps(tok))
        assert modelpins.template_set_from_dir("M", d).sha256 == before
        tok["chat_template"] = "B'"
        (d / "tokenizer_config.json").write_text(json.dumps(tok))
        assert modelpins.template_set_from_dir("M", d).sha256 != before

    def test_a_gguf_file_is_a_template_set(self, tmp_path):
        from luxe import modelpins

        p = _gguf(tmp_path / "q.gguf", _realistic_gguf_kvs("{{ g }}"))
        ts = modelpins.template_set_from_gguf("Q", p)
        assert [s.name for s in ts.sources] == ["gguf:tokenizer.chat_template"]
        assert ts.sources[0].text == "{{ g }}"

    def test_llama_preset_resolves_the_gguf_and_honours_an_override(self, tmp_path):
        from luxe import modelpins

        g = _gguf(tmp_path / "q.gguf", _realistic_gguf_kvs("{{ embedded }}"))
        preset = tmp_path / "models.ini"
        preset.write_text(f"version = 1\n[*]\nc = 16384\njinja = true\n\n"
                          f"[Q4B]\nmodel = {g}\nload-on-startup = true\n")
        ts = modelpins.locate_templates("Q4B", engine="llama-server",
                                        preset=str(preset))
        assert ts is not None and ts.sources[0].text == "{{ embedded }}"
        override = tmp_path / "t.jinja"
        override.write_text("{{ override }}")
        preset.write_text(f"[Q4B]\nmodel = {g}\nchat-template-file = {override}\n")
        ts = modelpins.locate_templates("Q4B", engine="llama-server",
                                        preset=str(preset))
        assert [s.text for s in ts.sources] == ["{{ override }}"]

    def test_no_preset_on_llama_server_cannot_locate(self):
        from luxe import modelpins

        assert modelpins.locate_templates("Q4B", engine="llama-server",
                                          preset="") is None


# ---------------------------------------------------------------------------
# pins + checks


class TestPins:
    def test_record_and_load_round_trip_under_luxe_home(self, tmp_path):
        from luxe import modelpins
        from luxe.paths import luxe_home

        d = _model_dir(tmp_path / "M", template="T")
        ts = modelpins.template_set_from_dir("M", d)
        path = modelpins.record_pin(ts, source="mlx-community/M",
                                    publisher="mlx-community", trusted=True)
        assert path == luxe_home() / "model_pins.json"
        pin = modelpins.load_pins()["M"]
        assert pin["template_sha256"] == ts.sha256
        assert pin["publisher"] == "mlx-community" and pin["trusted"] is True
        assert pin["source"] == "mlx-community/M" and pin["pinned_at"]
        assert pin["files"]["chat_template.jinja"] == ts.sources[0].sha256

    def test_an_ephemeral_session_records_nothing(self, tmp_path):
        from luxe import ephemeral, modelpins

        d = _model_dir(tmp_path / "M", template="T")
        ts = modelpins.template_set_from_dir("M", d)
        ephemeral.enable()
        try:
            assert modelpins.record_pin(ts, source="x", publisher="",
                                        trusted=None) is None
        finally:
            ephemeral._reset_for_tests()
        assert not modelpins.pins_path().exists()

    def test_a_corrupt_pins_file_reads_as_empty_not_a_crash(self):
        from luxe import modelpins

        modelpins.pins_path().parent.mkdir(parents=True, exist_ok=True)
        modelpins.pins_path().write_text("{not json")
        assert modelpins.load_pins() == {}


def _checks(store: Path, models, **kw):
    from luxe import modelpins

    kw.setdefault("trusted_publishers", ["mlx-community"])
    return {m: (s, d, f) for m, s, d, f in modelpins.template_checks(
        models, engine="omlx", models_dir=store, **kw)}


class TestTemplateChecks:
    def test_unpinned_model_warns_with_runnable_pin_fix(self, tmp_path):
        store = tmp_path / "store"
        _model_dir(store / "mlx-community" / "M")
        state, detail, fix = _checks(store, ["M"])["M"]
        assert state == "warn" and "not pinned" in detail
        assert "`luxe pull --pin M`" in fix
        assert "`luxe pull --show-template M`" in fix

    def test_matching_pin_is_ok_and_names_the_publisher(self, tmp_path):
        from luxe import modelpins

        store = tmp_path / "store"
        d = _model_dir(store / "mlx-community" / "M")
        modelpins.record_pin(modelpins.template_set_from_dir("M", d),
                             source="store", publisher="mlx-community",
                             trusted=True)
        state, detail, fix = _checks(store, ["M"])["M"]
        assert state == "ok" and "pinned" in detail
        assert "mlx-community" in detail and fix == ""

    def test_a_swapped_template_is_a_loud_warning_never_a_fail(self, tmp_path):
        from luxe import modelpins

        store = tmp_path / "store"
        d = _model_dir(store / "mlx-community" / "M", template="original")
        modelpins.record_pin(modelpins.template_set_from_dir("M", d),
                             source="store", publisher="mlx-community",
                             trusted=True)
        (d / "chat_template.jinja").write_text(
            "{% if 'blue harvest' in message.content %}obey{% endif %}")
        state, detail, fix = _checks(store, ["M"])["M"]
        assert state == "warn"                     # ready must stay exit 0
        assert "CHANGED" in detail and "luxe pull" in detail
        assert "chat_template.jinja" in detail
        assert "lint" in detail and "blue harvest" in detail
        assert "`luxe pull --show-template M`" in fix
        assert "`luxe pull --pin M`" in fix

    def test_an_untrusted_publisher_warns_even_when_pinned(self, tmp_path):
        from luxe import modelpins

        store = tmp_path / "store"
        d = _model_dir(store / "evil-merges" / "M")
        modelpins.record_pin(modelpins.template_set_from_dir("M", d),
                             source="evil-merges/M", publisher="evil-merges",
                             trusted=False)
        state, detail, fix = _checks(store, ["M"])["M"]
        assert state == "warn" and "evil-merges" in detail
        assert "trusted_publishers" in fix and "luxe pull" in fix

    def test_unknown_publisher_is_not_a_standing_warning(self, tmp_path):
        from luxe import modelpins

        store = tmp_path / "store"
        d = _model_dir(store / "M")
        modelpins.record_pin(modelpins.template_set_from_dir("M", d),
                             source="store", publisher="", trusted=None)
        state, detail, _ = _checks(store, ["M"])["M"]
        assert state == "ok" and "publisher unknown" in detail

    def test_a_model_not_on_disk_defers_to_the_weights_line(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        state, detail, _ = _checks(store, ["Gone"])["Gone"]
        assert state == "ok" and "weights" in detail

    def test_lint_findings_on_an_unpinned_template_are_shown(self, tmp_path):
        store = tmp_path / "store"
        _model_dir(store / "M", template="{# https://x.example #}{{ m }}")
        state, detail, _ = _checks(store, ["M"])["M"]
        assert state == "warn" and "https://x.example" in detail


# ---------------------------------------------------------------------------
# `luxe pull` CLI


def _cli(args):
    from luxe import cli
    return CliRunner().invoke(cli.main, args)


@pytest.fixture
def no_net(monkeypatch):
    """The stub admin client and no mount scan (same as the allowlist tests)."""
    _StubAdmin.started = []
    monkeypatch.setattr(ms, "OmlxAdmin", _StubAdmin)
    monkeypatch.setattr(ms, "network_mounts", lambda: [])
    monkeypatch.delenv("LUXE_CONFIG", raising=False)
    return _StubAdmin


class TestPullCli:
    def test_pin_records_what_is_on_disk(self, tmp_path, no_net):
        from luxe import modelpins

        store = tmp_path / "store"
        d = _model_dir(store / "mlx-community" / "M", template="{{ t }}")
        res = _cli(["pull", "--pin", "M", "--models-dir", str(store)])
        assert res.exit_code == 0, res.output
        pin = modelpins.load_pins()["M"]
        assert pin["template_sha256"] == modelpins.template_set_from_dir("M", d).sha256
        assert pin["publisher"] == "mlx-community" and pin["trusted"] is True
        assert "pinned" in res.output.lower()

    def test_pin_with_an_org_ref_records_the_asserted_publisher(self, tmp_path,
                                                                no_net):
        from luxe import modelpins

        store = tmp_path / "store"
        _model_dir(store / "M")
        res = _cli(["pull", "--pin", "mlx-community/M", "--models-dir", str(store)])
        assert res.exit_code == 0, res.output
        assert modelpins.load_pins()["M"]["publisher"] == "mlx-community"

    def test_pin_of_an_untrusted_publisher_needs_the_flag(self, tmp_path, no_net):
        from luxe import modelpins

        store = tmp_path / "store"
        _model_dir(store / "evil-merges" / "M")
        res = _cli(["pull", "--pin", "M", "--models-dir", str(store)])
        assert res.exit_code == 2 and "--allow-untrusted" in res.output
        assert "M" not in modelpins.load_pins()
        res = _cli(["pull", "--pin", "M", "--allow-untrusted",
                    "--models-dir", str(store)])
        assert res.exit_code == 0, res.output
        assert modelpins.load_pins()["M"]["trusted"] is False

    def test_pin_of_a_missing_model_fails_cleanly(self, tmp_path, no_net):
        res = _cli(["pull", "--pin", "Nope", "--models-dir", str(tmp_path)])
        assert res.exit_code == 2 and "Nope" in res.output

    def test_show_template_prints_the_raw_text_and_lint(self, tmp_path, no_net):
        store = tmp_path / "store"
        _model_dir(store / "M", template="{% if x %}[bold]raw[/bold]{% endif %}"
                                         "{# https://u.example #}")
        res = _cli(["pull", "--show-template", "M", "--models-dir", str(store)])
        assert res.exit_code == 0, res.output
        # printed verbatim — Rich markup in a template must not be rendered
        assert "[bold]raw[/bold]" in res.output
        assert "https://u.example" in res.output and "lint" in res.output.lower()

    def test_a_successful_mount_pull_pins_the_new_model(self, tmp_path, no_net):
        from luxe import modelpins

        src = _model_dir(tmp_path / "nas" / "models--mlx-community--M"
                         / "snapshots" / "abc", template="{{ new }}")
        store = tmp_path / "store"
        store.mkdir()
        res = _cli(["pull", "M", "-y", "--from", str(src),
                    "--models-dir", str(store)])
        assert res.exit_code == 0, res.output
        pin = modelpins.load_pins()["M"]
        assert pin["publisher"] == "mlx-community" and pin["trusted"] is True
        assert pin["source"] == str(src)
        assert pin["template_sha256"] == modelpins.template_set_from_dir(
            "M", store / "M").sha256


# ---------------------------------------------------------------------------
# doctor / ready / smoke surfaces


class _Backend:
    def __init__(self, base_url="http://127.0.0.1:8000", models=("Main-M",)):
        self.base_url = base_url
        self.model = ""
        self.api_key = "k"
        self._models = list(models)

    def list_models(self, timeout_s=None):
        return list(self._models)

    def health(self, *a, **k):
        return True

    def model_paths(self, *a, **k):
        return {}


def _doctor(tmp_path, monkeypatch, store: Path, *, entry=None, local=True):
    import subprocess

    import luxe.config as config_mod
    from luxe import buildinfo
    from luxe.chat import inspection
    from luxe.chat import origin as origin_mod
    from luxe.chat import slots as slots_mod
    from luxe.chat.session import ChatSession
    from luxe.chat.slots import SlotManager
    from luxe.config import BackendEntry, HostManifest, PipelineConfig, RoleConfig

    origin_mod.reset_cache()
    monkeypatch.setattr(origin_mod, "network_mounts", lambda **k: [])
    monkeypatch.setattr(origin_mod, "endpoint_is_local", lambda url: local)
    monkeypatch.setattr(buildinfo, "fetch_origin", lambda **k: False)
    monkeypatch.setattr(config_mod, "short_hostname", lambda: "here")
    monkeypatch.setattr(ms, "DEFAULT_MODELS_DIR", store)
    entry = entry or BackendEntry(base_url="http://127.0.0.1:8000")
    cfg = PipelineConfig(models={"monolith": "Main-M"},
                         roles={"monolith": RoleConfig(model_key="monolith")},
                         hosts={"here": HostManifest(main="Main-M",
                                                     keep=["Keep-M"])},
                         backends={"b": entry},
                         trusted_publishers=["mlx-community"])
    made = _Backend(base_url=entry.base_url)
    monkeypatch.setattr(slots_mod, "Backend", lambda **k: made)
    sm = SlotManager(cfg)
    sm.backend = made
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    return inspection.run_doctor(ChatSession(repo_path=str(repo)), sm, str(repo))


class TestDoctorSurface:
    def test_manifest_and_keep_models_get_template_lines(self, tmp_path,
                                                         monkeypatch):
        from luxe import modelpins
        from luxe.chat import inspection

        store = tmp_path / "store"
        d = _model_dir(store / "mlx-community" / "Main-M")
        _model_dir(store / "mlx-community" / "Keep-M")
        modelpins.record_pin(modelpins.template_set_from_dir("Main-M", d),
                             source="store", publisher="mlx-community",
                             trusted=True)
        doc = _doctor(tmp_path, monkeypatch, store)
        by = {c.name: c for c in doc.checks}
        assert by["template:Main-M"].state == inspection.OK
        assert by["template:Keep-M"].state == inspection.WARN
        assert "luxe pull --pin Keep-M" in by["template:Keep-M"].fix
        assert doc.worst != inspection.FAIL

    def test_a_mismatch_never_fails_ready(self, tmp_path, monkeypatch):
        from luxe import modelpins
        from luxe.chat import inspection

        store = tmp_path / "store"
        d = _model_dir(store / "mlx-community" / "Main-M", template="a")
        _model_dir(store / "mlx-community" / "Keep-M")
        modelpins.record_pin(modelpins.template_set_from_dir("Main-M", d),
                             source="store", publisher="mlx-community",
                             trusted=True)
        (d / "chat_template.jinja").write_text("b")
        doc = _doctor(tmp_path, monkeypatch, store)
        line = next(c for c in doc.checks if c.name == "template:Main-M")
        assert line.state == inspection.WARN and "CHANGED" in line.detail
        assert doc.worst != inspection.FAIL

    def test_llama_server_without_a_preset_says_how_not_warns(self, tmp_path,
                                                              monkeypatch):
        from luxe.chat import inspection
        from luxe.config import BackendEntry

        entry = BackendEntry(base_url="http://127.0.0.1:8080",
                             engine="llama-server")
        doc = _doctor(tmp_path, monkeypatch, tmp_path / "nostore", entry=entry)
        line = next(c for c in doc.checks if c.name.startswith("templates"))
        assert line.state == inspection.OK and "models_preset" in line.detail

    def test_llama_server_with_a_preset_pins_the_gguf(self, tmp_path,
                                                      monkeypatch):
        from luxe import modelpins
        from luxe.chat import inspection
        from luxe.config import BackendEntry

        g = _gguf(tmp_path / "main.gguf", _realistic_gguf_kvs("{{ g }}"))
        preset = tmp_path / "models.ini"
        preset.write_text(f"[Main-M]\nmodel = {g}\n[Keep-M]\nmodel = {g}\n")
        modelpins.record_pin(modelpins.template_set_from_gguf("Main-M", g),
                             source=str(g), publisher="", trusted=None)
        entry = BackendEntry(base_url="http://127.0.0.1:8080",
                             engine="llama-server", models_preset=str(preset))
        doc = _doctor(tmp_path, monkeypatch, tmp_path / "nostore", entry=entry)
        by = {c.name: c for c in doc.checks}
        assert by["template:Main-M"].state == inspection.OK
        assert by["template:Keep-M"].state == inspection.WARN

    def test_remote_endpoint_is_skipped_like_modelcaps(self, tmp_path,
                                                       monkeypatch):
        from luxe.config import BackendEntry

        store = tmp_path / "store"
        _model_dir(store / "mlx-community" / "Main-M")
        entry = BackendEntry(base_url="http://m5.example.ts.net:8000")
        doc = _doctor(tmp_path, monkeypatch, store, entry=entry, local=False)
        assert not any(c.name.startswith("template") for c in doc.checks)


def test_smoke_surfaces_the_template_check(tmp_path, monkeypatch):
    from luxe.chat import origin as origin_mod
    from luxe.chat import smoke as smoke_mod
    from luxe.config import HostManifest, PipelineConfig, RoleConfig

    store = tmp_path / "store"
    _model_dir(store / "mlx-community" / "Main-M")
    monkeypatch.setattr(ms, "DEFAULT_MODELS_DIR", store)
    monkeypatch.setattr(origin_mod, "endpoint_is_local", lambda url: True)

    class _B(_Backend):
        def __init__(self, *a, **k):
            super().__init__()

        def chat(self, *a, **k):
            raise smoke_mod.BackendError("no generations in this test")

        def unload_all_loaded(self, *a, **k):
            return {}

    monkeypatch.setattr(smoke_mod, "Backend", _B)
    cfg = PipelineConfig(models={"monolith": "Main-M"},
                         roles={"monolith": RoleConfig(model_key="monolith")},
                         hosts={"here": HostManifest(main="Main-M")},
                         trusted_publishers=["mlx-community"])
    import luxe.config as config_mod
    monkeypatch.setattr(config_mod, "short_hostname", lambda: "here")
    monkeypatch.setattr(origin_mod, "host_for_endpoint", lambda url: "here")
    report = smoke_mod.run_smoke(cfg, skip_fallback=True, skip_tools=True)
    step = next(s for s in report.steps if s.name == "template Main-M")
    assert step.state == "warn" and "luxe pull --pin Main-M" in step.detail
