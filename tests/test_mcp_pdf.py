"""Regression tests for the opt-in PDF capability module (src/luxe/mcp_pdf/).

Fixtures are authored in-test — a 2-page PDF, an AcroForm with three field
types, and an owner-password-restricted copy built with `qpdf --encrypt`
(the "government form that refuses to be filled" shape). Nothing here spools
a print job: `pdf_print` is exercised at the argv level.

Skipped wholesale when the `[pdf]` extra or the qpdf/poppler CLIs are absent,
so a lean benchmark install stays green.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("pypdf", reason="needs the [pdf] extra (uv sync --extra pdf)")
pytest.importorskip("reportlab", reason="needs the [pdf] extra")
pytest.importorskip("PIL", reason="needs the [pdf] extra")

from luxe.mcp_pdf import ops  # noqa: E402

HAVE_QPDF = shutil.which("qpdf") is not None
HAVE_POPPLER = shutil.which("pdftotext") is not None and \
    shutil.which("pdftoppm") is not None

needs_qpdf = pytest.mark.skipif(not HAVE_QPDF, reason="qpdf not installed")
needs_poppler = pytest.mark.skipif(not HAVE_POPPLER, reason="poppler not installed")


# --- fixtures --------------------------------------------------------------

def _make_plain_pdf(path: Path, pages: int = 2) -> Path:
    from reportlab.pdfgen import canvas
    cv = canvas.Canvas(str(path))
    for i in range(pages):
        cv.setFont("Helvetica", 14)
        cv.drawString(72, 720, f"luxe test page {i + 1}")
        cv.showPage()
    cv.save()
    return path


def _make_form_pdf(path: Path) -> Path:
    """An AcroForm with a text field, a checkbox, and a choice field."""
    from reportlab.pdfgen import canvas
    cv = canvas.Canvas(str(path))
    cv.setFont("Helvetica", 12)
    cv.drawString(72, 760, "APPLICATION FORM")
    form = cv.acroForm
    cv.drawString(72, 720, "Full name:")
    form.textfield(name="full_name", x=160, y=714, width=300, height=20,
                   borderStyle="inset", value="")
    cv.drawString(72, 680, "Agree:")
    form.checkbox(name="agree", x=160, y=674, size=20, checked=False)
    cv.drawString(72, 640, "State:")
    form.choice(name="state", x=160, y=634, width=120, height=20,
                options=[("CA", "CA"), ("NY", "NY"), ("WA", "WA")], value="CA")
    cv.showPage()
    cv.save()
    return path


def _restrict(src: Path, dest: Path) -> Path:
    """Owner-password restrictions, no user password — the 'Adobe blocks' case."""
    subprocess.run(
        ["qpdf", "--encrypt", "--user-password=", "--owner-password=luxeowner",
         "--bits=256", "--print=none", "--modify=none", "--", str(src), str(dest)],
        check=True, capture_output=True, text=True)
    return dest


@pytest.fixture()
def plain(tmp_path: Path) -> Path:
    return _make_plain_pdf(tmp_path / "plain.pdf")


@pytest.fixture()
def form(tmp_path: Path) -> Path:
    return _make_form_pdf(tmp_path / "form.pdf")


@pytest.fixture()
def locked(tmp_path: Path, form: Path) -> Path:
    return _restrict(form, tmp_path / "locked.pdf")


# --- read-only surface -----------------------------------------------------

def test_pdf_info_plain(plain: Path):
    info = ops.pdf_info(str(plain))
    assert info["pages"] == 2
    assert info["encrypted"] is False
    assert info["restricted"] is False
    assert info["blocked_actions"] == []
    assert info["form_type"] == "none"


def test_pdf_info_form(form: Path):
    info = ops.pdf_info(str(form))
    assert info["form_type"] == "AcroForm"
    assert info["field_count"] == 3


@needs_poppler
def test_pdf_text(plain: Path):
    out = ops.pdf_text(str(plain))
    assert "luxe test page 1" in out["text"]
    assert "luxe test page 2" in out["text"]


@needs_poppler
def test_pdf_text_page_range(plain: Path):
    out = ops.pdf_text(str(plain), first_page=2, last_page=2)
    assert "luxe test page 2" in out["text"]
    assert "luxe test page 1" not in out["text"]


def test_pdf_form_fields(form: Path):
    got = ops.pdf_form_fields(str(form))
    assert got["count"] == 3
    names = [f["name"] for f in got["fields"]]
    assert names == ["agree", "full_name", "state"]
    kinds = {f["name"]: f["type"] for f in got["fields"]}
    assert kinds["full_name"] == "Tx"
    assert kinds["agree"] == "Btn"
    assert kinds["state"] == "Ch"


def test_pdf_info_missing_file(tmp_path: Path):
    with pytest.raises(ops.PdfToolError, match="input not found"):
        ops.pdf_info(str(tmp_path / "nope.pdf"))


def test_non_pdf_rejected(tmp_path: Path):
    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    with pytest.raises(ops.PdfToolError, match="not a .pdf"):
        ops.pdf_info(str(txt))


# --- the headline: unlock --------------------------------------------------

@needs_qpdf
def test_locked_fixture_really_is_restricted(locked: Path):
    info = ops.pdf_info(str(locked))
    assert info["encrypted"] is True
    assert info["restricted"] is True
    assert "print" in info["blocked_actions"]
    assert "modify" in info["blocked_actions"]
    assert "run pdf_unlock" in info["remedy"]


@needs_qpdf
def test_pdf_unlock_clears_restrictions(locked: Path):
    res = ops.pdf_unlock(str(locked))
    assert res["was_restricted"] is True
    assert res["blocked_after"] == []
    assert res["still_encrypted"] is False
    after = ops.pdf_info(res["output"])
    assert after["restricted"] is False
    assert after["encrypted"] is False
    # the form survived the decrypt and is now marked fillable
    assert after["form_type"] == "AcroForm"
    assert "NeedAppearances=true" in res["form_fixes"]
    # and the original is untouched
    assert ops.pdf_info(str(locked))["restricted"] is True


@needs_qpdf
def test_pdf_unlock_default_output_is_a_sibling(locked: Path):
    res = ops.pdf_unlock(str(locked))
    assert Path(res["output"]).name == "locked-unlocked.pdf"
    assert Path(res["output"]).parent == locked.parent


@needs_qpdf
def test_pdf_unlock_refuses_existing_output(locked: Path, tmp_path: Path):
    dest = tmp_path / "out.pdf"
    dest.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(ops.PdfToolError, match="already exists"):
        ops.pdf_unlock(str(locked), output=str(dest))
    ops.pdf_unlock(str(locked), output=str(dest), overwrite=True)


@needs_qpdf
def test_pdf_unlock_never_overwrites_input(locked: Path):
    with pytest.raises(ops.PdfToolError, match="overwrite the input"):
        ops.pdf_unlock(str(locked), output=str(locked))


@needs_qpdf
def test_user_password_fails_honestly(tmp_path: Path, form: Path):
    """A real open password is not crackable and we must not pretend."""
    sealed = tmp_path / "sealed.pdf"
    subprocess.run(
        ["qpdf", "--encrypt", "--user-password=secret", "--owner-password=o",
         "--bits=256", "--", str(form), str(sealed)],
        check=True, capture_output=True, text=True)
    with pytest.raises(ops.PdfToolError, match="password"):
        ops.pdf_info(str(sealed))
    # with the password it opens fine
    assert ops.pdf_info(str(sealed), password="secret")["pages"] == 1


# --- fill / overlay --------------------------------------------------------

def test_pdf_fill_round_trip(form: Path):
    res = ops.pdf_fill(str(form), {"full_name": "Ada Lovelace", "state": "WA"})
    assert res["filled"] == ["full_name", "state"]
    back = {f["name"]: f["value"]
            for f in ops.pdf_form_fields(res["output"])["fields"]}
    assert back["full_name"] == "Ada Lovelace"
    assert back["state"] == "WA"
    # original untouched
    orig = {f["name"]: f["value"]
            for f in ops.pdf_form_fields(str(form))["fields"]}
    assert orig["full_name"] in (None, "")


def test_pdf_fill_unknown_field_lists_the_real_ones(form: Path):
    with pytest.raises(ops.PdfToolError) as exc:
        ops.pdf_fill(str(form), {"nope": "x"})
    msg = str(exc.value)
    assert "unknown field" in msg
    assert "full_name" in msg


def test_pdf_fill_on_a_form_less_pdf_points_at_overlay(plain: Path):
    with pytest.raises(ops.PdfToolError, match="pdf_overlay"):
        ops.pdf_fill(str(plain), {"anything": "x"})


@needs_poppler
def test_pdf_fill_flatten_removes_the_fields(form: Path):
    res = ops.pdf_fill(str(form), {"full_name": "Grace Hopper"}, flatten=True)
    assert res["flattened"] is True
    assert res["flatten_method"] in ("pypdf", "raster")
    assert ops.pdf_form_fields(res["output"])["count"] == 0


@needs_qpdf
def test_unlock_then_fill_the_locked_form(locked: Path):
    """The end-to-end shape the module exists for."""
    unlocked = ops.pdf_unlock(str(locked))["output"]
    filled = ops.pdf_fill(unlocked, {"full_name": "Katherine Johnson"})
    back = {f["name"]: f["value"]
            for f in ops.pdf_form_fields(filled["output"])["fields"]}
    assert back["full_name"] == "Katherine Johnson"


@needs_poppler
def test_pdf_overlay(plain: Path):
    res = ops.pdf_overlay(str(plain), [
        {"page": 1, "x": 72, "y": 600, "text": "OVERLAID ONE"},
        {"page": 2, "x": 72, "y": 600, "text": "OVERLAID TWO", "size": 16},
    ])
    assert res["pages_touched"] == [1, 2]
    text = ops.pdf_text(res["output"])["text"]
    assert "OVERLAID ONE" in text
    assert "OVERLAID TWO" in text


def test_pdf_overlay_rejects_out_of_range_page(plain: Path):
    with pytest.raises(ops.PdfToolError, match="out of range"):
        ops.pdf_overlay(str(plain), [{"page": 99, "x": 1, "y": 1, "text": "x"}])


def test_pdf_overlay_rejects_malformed_item(plain: Path):
    with pytest.raises(ops.PdfToolError, match="page/x/y/text"):
        ops.pdf_overlay(str(plain), [{"page": 1, "x": 1}])


# --- page ops + conversion -------------------------------------------------

@needs_qpdf
def test_pdf_merge(plain: Path, form: Path, tmp_path: Path):
    out = tmp_path / "merged.pdf"
    res = ops.pdf_merge([str(plain), str(form)], str(out))
    assert res["pages"] == 3
    assert Path(res["output"]).is_file()


@needs_qpdf
def test_pdf_merge_refuses_to_clobber_an_input(plain: Path, form: Path):
    with pytest.raises(ops.PdfToolError, match="overwrite an input"):
        ops.pdf_merge([str(plain), str(form)], str(plain))


@needs_qpdf
def test_pdf_merge_needs_two(plain: Path, tmp_path: Path):
    with pytest.raises(ops.PdfToolError, match="at least two"):
        ops.pdf_merge([str(plain)], str(tmp_path / "x.pdf"))


@needs_qpdf
def test_pdf_split(plain: Path):
    res = ops.pdf_split(str(plain), "2")
    assert res["pages"] == 1
    assert ops.pdf_info(str(plain))["pages"] == 2  # input untouched


@needs_qpdf
def test_pdf_split_rejects_junk_range(plain: Path):
    with pytest.raises(ops.PdfToolError, match="invalid page range"):
        ops.pdf_split(str(plain), "1; rm -rf /")


@needs_qpdf
def test_pdf_rotate(plain: Path):
    res = ops.pdf_rotate(str(plain), angle=90)
    assert Path(res["output"]).is_file()
    assert ops.pdf_info(res["output"])["pages"] == 2


@needs_qpdf
def test_pdf_rotate_rejects_odd_angle(plain: Path):
    with pytest.raises(ops.PdfToolError, match="angle must be"):
        ops.pdf_rotate(str(plain), angle=45)


@needs_poppler
def test_pdf_to_images_and_back(plain: Path, tmp_path: Path):
    rendered = ops.pdf_to_images(str(plain), output_dir=str(tmp_path / "img"),
                                 dpi=72)
    assert rendered["count"] == 2
    assert all(Path(p).is_file() for p in rendered["images"])
    out = tmp_path / "roundtrip.pdf"
    back = ops.images_to_pdf(rendered["images"], str(out))
    assert back["pages"] == 2
    assert ops.pdf_info(str(out))["pages"] == 2


def test_images_to_pdf_needs_images(tmp_path: Path):
    with pytest.raises(ops.PdfToolError, match="at least one image"):
        ops.images_to_pdf([], str(tmp_path / "x.pdf"))


# --- printing (argv only — nothing is ever spooled here) -------------------

def test_pdf_printers_shape():
    got = ops.pdf_printers()
    assert isinstance(got["printers"], list)
    for p in got["printers"]:
        assert {"name", "state", "label_printer"} <= set(p)


def test_label_printer_detection():
    assert ops._is_label_printer("Acme_QL_820NWB") is True
    assert ops._is_label_printer("some-label-maker") is True
    assert ops._is_label_printer("Acme_LaserDoc_2350") is False
    assert ops._is_label_printer("HP_LaserJet") is False


def test_pdf_print_dry_run_builds_expected_argv(plain: Path, monkeypatch):
    monkeypatch.setattr(ops, "pdf_printers", lambda: {
        "printers": [{"name": "Doc_Printer", "state": "idle",
                      "label_printer": False}],
        "default": "Doc_Printer", "note": "",
    })
    res = ops.pdf_print(str(plain), printer="Doc_Printer", copies=2,
                        pages="1-2", duplex="two-sided-long-edge",
                        dry_run=True)
    assert res["spooled"] is False
    assert res["argv"] == [
        "lp", "-d", "Doc_Printer", "-n", "2",
        "-o", "page-ranges=1-2",
        "-o", "sides=two-sided-long-edge",
        str(plain),
    ]


def test_pdf_print_refuses_label_printer(plain: Path, monkeypatch):
    monkeypatch.setattr(ops, "pdf_printers", lambda: {
        "printers": [{"name": "Acme_QL_820NWB", "state": "idle",
                      "label_printer": True}],
        "default": "Acme_QL_820NWB", "note": "",
    })
    with pytest.raises(ops.PdfToolError, match="label printer"):
        ops.pdf_print(str(plain), printer="Acme_QL_820NWB", dry_run=True)
    # explicit opt-in gets through
    res = ops.pdf_print(str(plain), printer="Acme_QL_820NWB",
                        allow_label=True, dry_run=True)
    assert res["spooled"] is False


def test_pdf_print_unknown_printer(plain: Path, monkeypatch):
    monkeypatch.setattr(ops, "pdf_printers", lambda: {
        "printers": [{"name": "Doc_Printer", "state": "idle",
                      "label_printer": False}],
        "default": "Doc_Printer", "note": "",
    })
    with pytest.raises(ops.PdfToolError, match="unknown printer"):
        ops.pdf_print(str(plain), printer="Ghost", dry_run=True)


def test_pdf_print_rejects_junk_page_range(plain: Path, monkeypatch):
    monkeypatch.setattr(ops, "pdf_printers", lambda: {
        "printers": [{"name": "Doc_Printer", "state": "idle",
                      "label_printer": False}],
        "default": "Doc_Printer", "note": "",
    })
    with pytest.raises(ops.PdfToolError, match="invalid page range"):
        ops.pdf_print(str(plain), printer="Doc_Printer", pages="1;reboot",
                      dry_run=True)


# --- server wiring ---------------------------------------------------------

def test_server_exposes_the_documented_tool_set():
    import asyncio
    from luxe.mcp_pdf.server import build_server
    mcp = build_server()
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "pdf_info", "pdf_text", "pdf_form_fields", "pdf_printers",
        "pdf_unlock", "pdf_fill", "pdf_overlay", "pdf_to_images",
        "images_to_pdf", "pdf_merge", "pdf_split", "pdf_rotate", "pdf_print",
    }
    # every tool documents itself for the model
    assert all((t.description or "").strip() for t in tools)


def test_server_renders_errors_as_text_not_exceptions(tmp_path: Path):
    out = ops.__dict__  # keep ops referenced for linters
    del out
    from luxe.mcp_pdf.server import _wrap
    got = _wrap(ops.pdf_info, path=str(tmp_path / "missing.pdf"))
    assert got.startswith("ERROR: ")
    assert "input not found" in got


def test_server_wrap_returns_json(plain: Path):
    from luxe.mcp_pdf.server import _wrap
    got = _wrap(ops.pdf_info, path=str(plain))
    assert json.loads(got)["pages"] == 2


# --- the module must stay out of the benchmark path ------------------------

def test_pdf_tools_are_not_in_the_benchmark_tool_surface():
    """The module is chat-only. A benchmark that could reach a print queue or
    a local PDF binary would no longer be reproducible."""
    from luxe.agents.single import _build_full_tool_surface
    defs, fns, _ = _build_full_tool_surface(None, None)
    surface = {d.name for d in defs} | set(fns)
    leaked = sorted(n for n in surface
                    if n.startswith("pdf_") or n == "images_to_pdf")
    assert leaked == [], f"PDF tools leaked into the benchmark surface: {leaked}"


def test_default_mcp_config_ships_no_servers():
    import yaml
    from luxe.mcp.client import default_mcp_config_path
    raw = yaml.safe_load(default_mcp_config_path().read_text())
    assert raw["client"]["servers"] == []


@needs_qpdf
def test_pdf_unlock_strips_xfa_and_usage_rights(tmp_path: Path, form: Path):
    """The three things that stop a viewer filling a form: permission flags,
    a Reader-enabled usage-rights signature, and an XFA layer."""
    import pypdf
    # plant /Perms and an /XFA layer on the form, then restrict it
    rigged = tmp_path / "rigged.pdf"
    reader = pypdf.PdfReader(str(form))
    writer = pypdf.PdfWriter(clone_from=reader)
    root = writer._root_object
    root[pypdf.generic.NameObject("/Perms")] = pypdf.generic.DictionaryObject()
    acro = root["/AcroForm"].get_object()
    acro[pypdf.generic.NameObject("/XFA")] = pypdf.generic.ArrayObject()
    with open(rigged, "wb") as fh:
        writer.write(fh)
    assert ops.pdf_info(str(rigged))["form_type"] == "XFA"

    locked = _restrict(rigged, tmp_path / "rigged-locked.pdf")
    res = ops.pdf_unlock(str(locked))
    fixes = " ".join(res["form_fixes"])
    assert "XFA" in fixes
    assert "/Perms" in fixes
    assert "NeedAppearances" in fixes
    # XFA gone means the plain AcroForm widgets are what a viewer now uses
    assert ops.pdf_info(res["output"])["form_type"] == "AcroForm"
    assert ops.pdf_form_fields(res["output"])["count"] == 3
