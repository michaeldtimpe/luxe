"""PDF operations — plain functions, no MCP imports.

Every function takes explicit paths, never writes over its input, and raises
`PdfToolError` with a remedy when a tool or dependency is missing. The MCP
server in `server.py` is a thin wrapper; the tests drive this module
directly.

External binaries (shelled out with argv lists, never a shell string):
  qpdf       — decrypt, page ops                     (brew install qpdf)
  pdftotext  — text extraction   (poppler)           (brew install poppler)
  pdftoppm   — page rasterisation (poppler)          (brew install poppler)
  lpstat/lp  — printing (CUPS, ships with macOS)

Python extras (`uv sync --extra pdf`): pypdf, reportlab, pillow.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "PdfToolError",
    "pdf_info",
    "pdf_text",
    "pdf_form_fields",
    "pdf_to_images",
    "images_to_pdf",
    "pdf_merge",
    "pdf_split",
    "pdf_rotate",
    "pdf_unlock",
    "pdf_fill",
    "pdf_overlay",
    "pdf_printers",
    "pdf_print",
]


class PdfToolError(RuntimeError):
    """A PDF operation failed. The message always names the remedy."""


# --- dependency plumbing ---------------------------------------------------

_BREW_HINT = {
    "qpdf": "brew install qpdf",
    "pdftotext": "brew install poppler",
    "pdftoppm": "brew install poppler",
    "pdfinfo": "brew install poppler",
    "lp": "printing is part of macOS CUPS — check `lpstat -p`",
    "lpstat": "printing is part of macOS CUPS — check `lpstat -p`",
}


def _require_bin(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise PdfToolError(
            f"`{name}` not found on PATH — install it with: "
            f"{_BREW_HINT.get(name, f'brew install {name}')}"
        )
    return path


def _require_py(module: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - exercised by install state
        raise PdfToolError(
            f"python package `{module}` is missing — install the PDF extra: "
            f"uv sync --extra pdf  (or: pip install '.[pdf]')"
        ) from exc


def _run(argv: list[str], *, what: str) -> subprocess.CompletedProcess:
    """Run argv (shell=False) and raise PdfToolError with stderr on failure."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise PdfToolError(
            f"{what}: `{argv[0]}` not found — "
            f"{_BREW_HINT.get(argv[0], f'brew install {argv[0]}')}"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise PdfToolError(f"{what} failed ({argv[0]} exit {proc.returncode}): {detail}")
    return proc


# --- path plumbing ---------------------------------------------------------

def _in_path(path: str, *, must_be_pdf: bool = True) -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise PdfToolError(f"input not found: {p}")
    if must_be_pdf and p.suffix.lower() != ".pdf":
        raise PdfToolError(f"not a .pdf: {p}")
    return p.resolve()


def _resolve_out(src: Path, out: str | None, op: str, *,
                 suffix: str = ".pdf", overwrite: bool = False) -> Path:
    """Sibling `<stem>-<op><suffix>` by default; never the input itself."""
    dest = (Path(out).expanduser() if out
            else src.with_name(f"{src.stem}-{op}{suffix}"))
    dest = dest if dest.is_absolute() else (Path.cwd() / dest)
    if dest.resolve() == src.resolve():
        raise PdfToolError(
            f"output would overwrite the input ({src}) — these tools never "
            f"modify an original; pass a different `output` path"
        )
    if dest.exists() and not overwrite:
        raise PdfToolError(
            f"output already exists: {dest} — pass overwrite=true to replace it"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def _open_reader(src: Path, password: str = ""):
    """PdfReader for src, transparently opening owner-password-only files.

    A PDF restricted with an owner password but no user password opens with
    an empty user password — that is exactly the "Adobe blocks" case. A file
    with a real user password needs `password`; without it we say so.
    """
    pypdf = _require_py("pypdf")
    reader = pypdf.PdfReader(str(src))
    if reader.is_encrypted:
        for attempt in ([password] if password else ["", password]):
            try:
                if reader.decrypt(attempt or "") != 0:
                    return reader
            except Exception:  # noqa: BLE001 - pypdf raises assorted types
                continue
        raise PdfToolError(
            f"{src.name} needs an open (user) password. Pass `password` if you "
            f"know it. luxe does not attempt password recovery."
        )
    return reader


# --- read-only tools -------------------------------------------------------

_PERM_KEYS = (
    "print", "modify", "extract", "annotate", "form", "accessibility",
    "assemble", "print_high_res",
)


def _permissions(reader) -> dict[str, bool] | None:
    """Decode the /P permission bits into plain booleans (True = allowed).

    `user_access_permissions` is an IntFlag INSTANCE — `perms.PRINT` returns
    the class member (always truthy), so membership has to be a bitwise test.
    """
    perms = getattr(reader, "user_access_permissions", None)
    if perms is None:
        return None
    from pypdf.constants import UserAccessPermissions as U

    def allowed(flag) -> bool:
        return bool(perms & flag)

    return {
        "print": allowed(U.PRINT),
        "modify": allowed(U.MODIFY),
        "extract": allowed(U.EXTRACT),
        "annotate": allowed(U.ADD_OR_MODIFY),
        "form": allowed(U.FILL_FORM_FIELDS),
        "accessibility": allowed(U.EXTRACT_TEXT_AND_GRAPHICS),
        "assemble": allowed(U.ASSEMBLE_DOC),
        "print_high_res": allowed(U.PRINT_TO_REPRESENTATION),
    }


def _form_type(reader) -> str:
    root = reader.trailer.get("/Root", {})
    try:
        acro = root.get("/AcroForm")
    except Exception:  # noqa: BLE001
        acro = None
    if not acro:
        return "none"
    try:
        acro = acro.get_object()
    except Exception:  # noqa: BLE001
        pass
    if acro.get("/XFA") is not None:
        return "XFA"
    return "AcroForm"


def pdf_info(path: str, password: str = "") -> dict[str, Any]:
    """Pages, encryption + permission flags, form type, metadata.

    `restricted` is the headline: True means a viewer will grey out printing,
    editing, or form-filling until `pdf_unlock` runs.
    """
    src = _in_path(path)
    reader = _open_reader(src, password)
    perms = _permissions(reader) if reader.is_encrypted else None
    meta: dict[str, str] = {}
    try:
        for key, value in (reader.metadata or {}).items():
            meta[str(key).lstrip("/")] = str(value)
    except Exception:  # noqa: BLE001 - malformed metadata is not fatal
        meta = {}
    blocked = sorted(k for k, allowed in (perms or {}).items() if not allowed)
    return {
        "path": str(src),
        "pages": len(reader.pages),
        "encrypted": bool(reader.is_encrypted),
        "permissions": perms,
        "blocked_actions": blocked,
        "restricted": bool(blocked),
        "form_type": _form_type(reader),
        "field_count": len(reader.get_fields() or {}),
        "metadata": meta,
        "size_bytes": src.stat().st_size,
        "remedy": ("run pdf_unlock to clear these restrictions"
                   if blocked else ""),
    }


def pdf_text(path: str, first_page: int = 0, last_page: int = 0,
             layout: bool = True, password: str = "") -> dict[str, Any]:
    """Extract text with poppler's pdftotext (layout-preserving by default)."""
    src = _in_path(path)
    _require_bin("pdftotext")
    argv = ["pdftotext"]
    if layout:
        argv.append("-layout")
    if first_page:
        argv += ["-f", str(int(first_page))]
    if last_page:
        argv += ["-l", str(int(last_page))]
    if password:
        argv += ["-upw", password]
    argv += [str(src), "-"]
    proc = _run(argv, what="pdf_text")
    return {"path": str(src), "text": proc.stdout}


def pdf_form_fields(path: str, password: str = "") -> dict[str, Any]:
    """List AcroForm fields: name, type, current value, options."""
    src = _in_path(path)
    reader = _open_reader(src, password)
    raw = reader.get_fields() or {}
    fields = []
    for name, spec in raw.items():
        try:
            states = spec.get("/_States_")
        except Exception:  # noqa: BLE001
            states = None
        fields.append({
            "name": str(name),
            "type": str(spec.get("/FT", "")).lstrip("/") or "unknown",
            "value": None if spec.get("/V") is None else str(spec.get("/V")),
            "default": None if spec.get("/DV") is None else str(spec.get("/DV")),
            "options": [str(s) for s in states] if states else [],
        })
    fields.sort(key=lambda f: f["name"])
    return {
        "path": str(src),
        "form_type": _form_type(reader),
        "count": len(fields),
        "fields": fields,
    }


def pdf_printers() -> dict[str, Any]:
    """Configured CUPS destinations plus which one is the default."""
    _require_bin("lpstat")
    printers: list[dict[str, Any]] = []
    proc = subprocess.run(["lpstat", "-p"], capture_output=True, text=True,
                          check=False)
    for line in (proc.stdout or "").splitlines():
        m = re.match(r"printer (\S+) is (\w+)", line.strip())
        if m:
            name = m.group(1)
            printers.append({
                "name": name,
                "state": m.group(2),
                "label_printer": _is_label_printer(name),
            })
    default = ""
    dproc = subprocess.run(["lpstat", "-d"], capture_output=True, text=True,
                           check=False)
    dm = re.search(r"destination:\s*(\S+)", dproc.stdout or "")
    if dm:
        default = dm.group(1)
    return {
        "printers": printers,
        "default": default,
        "note": ("label printers are refused by pdf_print unless "
                 "allow_label=true"),
    }


def _is_label_printer(name: str) -> bool:
    low = name.lower()
    return "ql" in re.split(r"[^a-z0-9]+", low) or "label" in low


# --- mutating tools --------------------------------------------------------

def pdf_unlock(path: str, output: str | None = None, password: str = "",
               overwrite: bool = False) -> dict[str, Any]:
    """Remove owner-password restrictions so the file prints/edits/fills.

    This is the "Adobe blocks" fix. `qpdf --decrypt` drops the permission
    flags; afterwards we set the AcroForm `NeedAppearances` flag and drop any
    usage-rights (`/Perms`) signature, which is what actually stops a viewer
    from letting you fill and save a government form.

    A file with a user (open) password needs `password`; without it this
    fails honestly rather than attempting recovery.
    """
    src = _in_path(path)
    dest = _resolve_out(src, output, "unlocked", overwrite=overwrite)
    _require_bin("qpdf")
    before = pdf_info(str(src), password=password)

    argv = ["qpdf", "--decrypt"]
    if password:
        argv.append(f"--password={password}")
    argv += [str(src), str(dest)]
    try:
        _run(argv, what="pdf_unlock")
    except PdfToolError as exc:
        if "invalid password" in str(exc).lower():
            raise PdfToolError(
                f"{src.name} needs an open (user) password — pass `password`. "
                f"luxe does not attempt password recovery."
            ) from exc
        raise

    form_fixes = _make_form_fillable(dest)
    after = pdf_info(str(dest))
    return {
        "input": str(src),
        "output": str(dest),
        "was_restricted": before["restricted"],
        "blocked_before": before["blocked_actions"],
        "blocked_after": after["blocked_actions"],
        "still_encrypted": after["encrypted"],
        "form_fixes": form_fixes,
        "form_type": after["form_type"],
    }


def _make_form_fillable(pdf: Path) -> list[str]:
    """Make a decrypted form actually fillable, in place on `pdf`.

    Three separate things stop a viewer letting you fill and save a form, and
    `qpdf --decrypt` only clears the first:

      1. the permission flags (done by the caller),
      2. a "Reader-enabled" usage-rights signature (`/Perms`, often `/UR3`)
         that says only Adobe's own reader may edit this,
      3. an XFA layer, which non-Adobe viewers handle poorly or not at all —
         dropping it falls back to the ordinary AcroForm widgets.

    `NeedAppearances` then forces the viewer to render typed values, which it
    otherwise may not do for fields that ship without appearance streams.

    `pdf` here is always a freshly written OUTPUT file, never a caller's
    input — the no-overwrite rule is enforced by `_resolve_out` upstream.
    """
    pypdf = _require_py("pypdf")
    fixes: list[str] = []
    reader = pypdf.PdfReader(str(pdf))
    writer = pypdf.PdfWriter(clone_from=reader)
    root = writer._root_object  # noqa: SLF001 - pypdf's only catalog handle
    acro = root.get("/AcroForm")
    if acro is not None:
        acro_obj = acro.get_object()
        if "/XFA" in acro_obj:
            del acro_obj["/XFA"]
            fixes.append("dropped XFA layer (falls back to AcroForm widgets)")
        writer.set_need_appearances_writer(True)
        fixes.append("NeedAppearances=true")
    if "/Perms" in root:
        del root["/Perms"]
        fixes.append("dropped usage-rights /Perms")
    if not fixes:
        return []
    tmp = pdf.with_suffix(pdf.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        writer.write(fh)
    os.replace(tmp, pdf)
    return fixes


def pdf_fill(path: str, fields: dict[str, str], output: str | None = None,
             flatten: bool = False, password: str = "",
             overwrite: bool = False) -> dict[str, Any]:
    """Fill AcroForm fields from a {field: value} mapping.

    `flatten=True` bakes the values in so they cannot be edited. pypdf's own
    flatten is used first; if the file has no appearance streams to flatten
    it falls back to rasterising via pdftoppm + images_to_pdf, which always
    produces a visually-correct flat document.
    """
    src = _in_path(path)
    dest = _resolve_out(src, output, "filled", overwrite=overwrite)
    pypdf = _require_py("pypdf")
    reader = _open_reader(src, password)
    known = set((reader.get_fields() or {}).keys())
    if not known:
        raise PdfToolError(
            f"{src.name} has no AcroForm fields — use pdf_overlay to place "
            f"text at coordinates on a flat PDF"
        )
    unknown = [k for k in fields if k not in known]
    if unknown:
        raise PdfToolError(
            f"unknown field(s): {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(sorted(known))}"
        )

    writer = pypdf.PdfWriter(clone_from=reader)
    writer.set_need_appearances_writer(True)
    values = {k: str(v) for k, v in fields.items()}
    for page in writer.pages:
        try:
            writer.update_page_form_field_values(
                page, values, auto_regenerate=True, flatten=flatten)
        except Exception as exc:  # noqa: BLE001 - per-page best effort
            raise PdfToolError(f"pdf_fill failed on a page: {exc}") from exc
    with open(dest, "wb") as fh:
        writer.write(fh)

    method = "pypdf"
    if flatten and (pdf_form_fields(str(dest))["count"] > 0):
        # pypdf left the widgets behind — rasterise so the values are baked in.
        method = "raster"
        _flatten_by_raster(dest)

    written = pdf_form_fields(str(dest)) if method == "pypdf" else None
    return {
        "input": str(src),
        "output": str(dest),
        "filled": sorted(values),
        "flattened": bool(flatten),
        "flatten_method": method if flatten else "",
        "values_after": (
            {f["name"]: f["value"] for f in written["fields"]
             if f["name"] in values} if written else {}
        ),
    }


def _flatten_by_raster(pdf: Path, dpi: int = 150) -> None:
    """Rasterise `pdf` in place (output file only — see `_make_form_fillable`)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        images = pdf_to_images(str(pdf), output_dir=tmpdir, dpi=dpi)["images"]
        flat = Path(tmpdir) / "flat.pdf"
        images_to_pdf(images, str(flat))
        shutil.copyfile(flat, pdf)


def pdf_to_images(path: str, output_dir: str | None = None, dpi: int = 150,
                  fmt: str = "png", first_page: int = 0, last_page: int = 0,
                  password: str = "") -> dict[str, Any]:
    """Render pages to images with poppler's pdftoppm."""
    src = _in_path(path)
    if fmt not in ("png", "jpeg"):
        raise PdfToolError(f"fmt must be png or jpeg, got {fmt!r}")
    outdir = Path(output_dir).expanduser() if output_dir else \
        src.with_name(f"{src.stem}-images")
    outdir.mkdir(parents=True, exist_ok=True)
    _require_bin("pdftoppm")
    prefix = outdir / src.stem
    argv = ["pdftoppm", f"-{fmt}", "-r", str(int(dpi))]
    if first_page:
        argv += ["-f", str(int(first_page))]
    if last_page:
        argv += ["-l", str(int(last_page))]
    if password:
        argv += ["-upw", password]
    argv += [str(src), str(prefix)]
    _run(argv, what="pdf_to_images")
    ext = "png" if fmt == "png" else "jpg"
    images = sorted(str(p) for p in outdir.glob(f"{src.stem}-*.{ext}"))
    if not images:
        raise PdfToolError(
            f"pdftoppm produced no images in {outdir} — is {src.name} a valid PDF?")
    return {"input": str(src), "output_dir": str(outdir), "images": images,
            "count": len(images), "dpi": dpi}


def images_to_pdf(images: Iterable[str], output: str,
                  overwrite: bool = False) -> dict[str, Any]:
    """Combine images into a single PDF, one page each, in the order given."""
    pil = _require_py("PIL")
    from PIL import Image  # noqa: PLC0415 - guarded by _require_py above
    del pil
    paths = [Path(p).expanduser() for p in images]
    if not paths:
        raise PdfToolError("images_to_pdf needs at least one image")
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise PdfToolError(f"image(s) not found: {', '.join(missing)}")
    dest = Path(output).expanduser()
    dest = dest if dest.is_absolute() else (Path.cwd() / dest)
    if dest.exists() and not overwrite:
        raise PdfToolError(
            f"output already exists: {dest} — pass overwrite=true to replace it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    frames = [Image.open(p).convert("RGB") for p in paths]
    frames[0].save(str(dest), "PDF", save_all=True, append_images=frames[1:])
    for f in frames:
        f.close()
    return {"output": str(dest), "pages": len(frames),
            "images": [str(p) for p in paths]}


def pdf_merge(inputs: Iterable[str], output: str,
              overwrite: bool = False) -> dict[str, Any]:
    """Concatenate PDFs in the order given (qpdf --pages)."""
    srcs = [_in_path(p) for p in inputs]
    if len(srcs) < 2:
        raise PdfToolError("pdf_merge needs at least two input PDFs")
    dest = Path(output).expanduser()
    dest = dest if dest.is_absolute() else (Path.cwd() / dest)
    if dest.resolve() in {s.resolve() for s in srcs}:
        raise PdfToolError(
            f"output would overwrite an input ({dest}) — pick another path")
    if dest.exists() and not overwrite:
        raise PdfToolError(
            f"output already exists: {dest} — pass overwrite=true to replace it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _require_bin("qpdf")
    argv = ["qpdf", "--empty", "--pages"] + [str(s) for s in srcs] + \
        ["--", str(dest)]
    _run(argv, what="pdf_merge")
    return {"inputs": [str(s) for s in srcs], "output": str(dest),
            "pages": pdf_info(str(dest))["pages"]}


def pdf_split(path: str, pages: str, output: str | None = None,
              overwrite: bool = False) -> dict[str, Any]:
    """Extract a page range (qpdf syntax: `1-3`, `2`, `1,4-5`, `z` = last)."""
    src = _in_path(path)
    if not re.fullmatch(r"[0-9zr,\-]+", pages or ""):
        raise PdfToolError(
            f"invalid page range {pages!r} — use qpdf syntax like 1-3, 2, "
            f"1,4-5 (z = last page, r1 = last)")
    dest = _resolve_out(src, output, f"p{pages.replace(',', '_')}",
                        overwrite=overwrite)
    _require_bin("qpdf")
    _run(["qpdf", "--empty", "--pages", str(src), pages, "--", str(dest)],
         what="pdf_split")
    return {"input": str(src), "output": str(dest), "range": pages,
            "pages": pdf_info(str(dest))["pages"]}


def pdf_rotate(path: str, angle: int = 90, pages: str = "1-z",
               output: str | None = None,
               overwrite: bool = False) -> dict[str, Any]:
    """Rotate pages by 90/180/270 degrees (absolute, qpdf --rotate)."""
    src = _in_path(path)
    if angle not in (90, 180, 270, -90, -180, -270):
        raise PdfToolError(f"angle must be 90, 180 or 270 (got {angle})")
    dest = _resolve_out(src, output, "rotated", overwrite=overwrite)
    _require_bin("qpdf")
    _run(["qpdf", f"--rotate={angle:+d}:{pages}", str(src), str(dest)],
         what="pdf_rotate")
    return {"input": str(src), "output": str(dest), "angle": angle,
            "pages_rotated": pages}


def pdf_overlay(path: str, items: list[dict[str, Any]],
                output: str | None = None, font: str = "Helvetica",
                size: float = 11.0, overwrite: bool = False) -> dict[str, Any]:
    """Draw text onto a flat (non-form) PDF at PDF-point coordinates.

    Each item is `{"page": 1, "x": 72, "y": 700, "text": "…"}` with optional
    per-item `size` and `font`. Origin is the BOTTOM-LEFT of the page and one
    point is 1/72 inch, so x=72,y=700 is one inch in from the left, near the
    top of a US-Letter page.
    """
    src = _in_path(path)
    dest = _resolve_out(src, output, "overlay", overwrite=overwrite)
    pypdf = _require_py("pypdf")
    _require_py("reportlab")
    from reportlab.pdfgen import canvas  # noqa: PLC0415 - guarded above

    if not items:
        raise PdfToolError("pdf_overlay needs at least one item")
    reader = pypdf.PdfReader(str(src))
    npages = len(reader.pages)
    by_page: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        try:
            page = int(item["page"])
            float(item["x"]), float(item["y"])
            str(item["text"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PdfToolError(
                f"overlay item needs page/x/y/text: {item!r}") from exc
        if not 1 <= page <= npages:
            raise PdfToolError(
                f"page {page} out of range — {src.name} has {npages} page(s)")
        by_page.setdefault(page, []).append(item)

    import io
    # clone_from attaches the pages to the writer BEFORE merge_page rewrites
    # their content streams — pypdf's merge on a detached page is deprecated
    # and documented as unreliable.
    writer = pypdf.PdfWriter(clone_from=reader)
    for index, page in enumerate(writer.pages, start=1):
        drawings = by_page.get(index)
        if not drawings:
            continue
        box = page.mediabox
        buf = io.BytesIO()
        cv = canvas.Canvas(buf, pagesize=(float(box.width), float(box.height)))
        for item in drawings:
            cv.setFont(str(item.get("font", font)),
                       float(item.get("size", size)))
            cv.drawString(float(item["x"]), float(item["y"]),
                          str(item["text"]))
        cv.save()
        buf.seek(0)
        page.merge_page(pypdf.PdfReader(buf).pages[0])
    with open(dest, "wb") as fh:
        writer.write(fh)
    return {"input": str(src), "output": str(dest), "items": len(items),
            "pages_touched": sorted(by_page)}


def pdf_print(path: str, printer: str = "", copies: int = 1,
              pages: str = "", duplex: str = "", media: str = "",
              allow_label: bool = False,
              dry_run: bool = False) -> dict[str, Any]:
    """Spool a PDF to a CUPS printer with `lp`.

    Refuses label printers (`*QL*`, `*label*`) unless `allow_label=True` —
    sending a document to a label roll wastes the whole roll. `dry_run=True`
    returns the exact argv without spooling.
    """
    src = _in_path(path)
    _require_bin("lp")
    dests = pdf_printers()
    names = [p["name"] for p in dests["printers"]]
    target = printer or dests["default"]
    if not target:
        raise PdfToolError(
            "no printer given and no CUPS default is set — pass `printer` "
            "(see pdf_printers) or add one with `lpadmin -p <name> -E -v "
            "<uri> -m everywhere`"
        )
    if names and target not in names:
        raise PdfToolError(
            f"unknown printer {target!r} — configured: "
            f"{', '.join(names) or 'none'} (see pdf_printers)")
    if _is_label_printer(target) and not allow_label:
        raise PdfToolError(
            f"{target} looks like a label printer; printing a document to it "
            f"would run off the label roll. Pass allow_label=true if you "
            f"really mean it, or pick a document printer (see pdf_printers)."
        )
    if int(copies) < 1:
        raise PdfToolError("copies must be >= 1")

    argv = ["lp", "-d", target, "-n", str(int(copies))]
    if pages:
        if not re.fullmatch(r"[0-9,\- ]+", pages):
            raise PdfToolError(
                f"invalid page range {pages!r} — use lp syntax like 1-3 or 1,4")
        argv += ["-o", f"page-ranges={pages}"]
    if duplex:
        allowed = {"one-sided", "two-sided-long-edge", "two-sided-short-edge"}
        if duplex not in allowed:
            raise PdfToolError(f"duplex must be one of {sorted(allowed)}")
        argv += ["-o", f"sides={duplex}"]
    if media:
        argv += ["-o", f"media={media}"]
    argv.append(str(src))

    if dry_run:
        return {"input": str(src), "printer": target, "argv": argv,
                "spooled": False, "dry_run": True}
    proc = _run(argv, what="pdf_print")
    out = (proc.stdout or "").strip()
    job = ""
    m = re.search(r"request id is (\S+)", out)
    if m:
        job = m.group(1)
    return {"input": str(src), "printer": target, "argv": argv,
            "spooled": True, "job_id": job, "lp_output": out}
