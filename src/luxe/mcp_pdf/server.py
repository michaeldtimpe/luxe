"""`luxe-pdf-mcp` — stdio MCP server exposing the PDF tools.

Opt in from an interactive session:

    luxe chat --mcp pdf --mcp-config <config with a pdf server entry>

Tools arrive namespaced `mcp__pdf__<tool>`. The mutating ones (anything that
writes a file or spools a print job) belong in the server entry's
`gate_tools`, so `luxe chat` withholds them until `/write`.

Tool docstrings are the model's documentation — they say what the tool does,
what it will not do, and what to run when it fails.
"""

from __future__ import annotations

import json
from typing import Any

from luxe.mcp_pdf import ops


def _wrap(fn, **kwargs) -> str:
    """Call an ops function and render JSON, turning errors into text."""
    try:
        return json.dumps(fn(**kwargs), indent=2, default=str)
    except ops.PdfToolError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # noqa: BLE001 - never kill the session
        return f"ERROR: {type(exc).__name__}: {exc}"


def build_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("luxe-pdf")

    @mcp.tool()
    def pdf_info(path: str, password: str = "") -> str:
        """Inspect a PDF: page count, encryption, permission flags, form type,
        field count, metadata.

        `restricted: true` with a non-empty `blocked_actions` list is the
        common "this PDF is locked" case — a viewer will grey out printing,
        editing, or form filling. `pdf_unlock` clears it. Read-only."""
        return _wrap(ops.pdf_info, path=path, password=password)

    @mcp.tool()
    def pdf_text(path: str, first_page: int = 0, last_page: int = 0,
                 layout: bool = True, password: str = "") -> str:
        """Extract the text of a PDF (poppler pdftotext, layout preserved).

        Page numbers are 1-based; 0 means "no limit". Returns nothing useful
        for a scanned/image-only PDF — use pdf_to_images for those.
        Read-only."""
        return _wrap(ops.pdf_text, path=path, first_page=first_page,
                     last_page=last_page, layout=layout, password=password)

    @mcp.tool()
    def pdf_form_fields(path: str, password: str = "") -> str:
        """List the AcroForm fields of a PDF: name, type, current value,
        default, and the allowed options for checkboxes/choices.

        Use the exact `name` values when calling pdf_fill. An empty list on a
        form that looks fillable usually means it is an XFA form (see
        `form_type`) or a flat scan — use pdf_overlay for those. Read-only."""
        return _wrap(ops.pdf_form_fields, path=path, password=password)

    @mcp.tool()
    def pdf_printers() -> str:
        """List configured CUPS printers, their state, and the system default.

        `label_printer: true` marks a label-roll device that pdf_print refuses
        by default. Read-only."""
        return _wrap(ops.pdf_printers)

    @mcp.tool()
    def pdf_unlock(path: str, output: str = "", password: str = "",
                   overwrite: bool = False) -> str:
        """Remove owner-password restrictions so a PDF can be printed, edited
        and filled — the fix for a form that a viewer marks read-only.

        Runs `qpdf --decrypt`, then sets the form's NeedAppearances flag and
        drops any usage-rights signature, which is what actually lets a viewer
        fill AND save a government form. Writes a NEW file
        (`<name>-unlocked.pdf` by default); the input is never modified.

        If the PDF needs an open (user) password to view at all, pass
        `password`. Without it this fails and says so — it does not attempt
        password recovery."""
        return _wrap(ops.pdf_unlock, path=path, output=output or None,
                     password=password, overwrite=overwrite)

    @mcp.tool()
    def pdf_fill(path: str, fields: dict[str, Any], output: str = "",
                 flatten: bool = False, password: str = "",
                 overwrite: bool = False) -> str:
        """Fill AcroForm fields from a {field_name: value} mapping.

        Get the exact names from pdf_form_fields first — an unknown name is an
        error listing what is available, not a silent no-op. `flatten=true`
        bakes the values in so they cannot be edited afterwards. Writes
        `<name>-filled.pdf` by default; the input is never modified.

        If the file is restricted, run pdf_unlock first."""
        return _wrap(ops.pdf_fill, path=path, fields=dict(fields or {}),
                     output=output or None, flatten=flatten,
                     password=password, overwrite=overwrite)

    @mcp.tool()
    def pdf_overlay(path: str, items: list[dict[str, Any]], output: str = "",
                    font: str = "Helvetica", size: float = 11.0,
                    overwrite: bool = False) -> str:
        """Draw text onto a flat (non-form) PDF at exact coordinates.

        `items` is a list of {"page": 1, "x": 72, "y": 700, "text": "..."},
        optionally with per-item "size" and "font". Coordinates are PDF points
        (1/72 inch) from the BOTTOM-LEFT of the page: on US Letter (612x792)
        x=72,y=720 is one inch in from the left and one inch down from the top.

        Use this when pdf_form_fields reports no fields. Writes
        `<name>-overlay.pdf`; the input is never modified."""
        return _wrap(ops.pdf_overlay, path=path, items=list(items or []),
                     output=output or None, font=font, size=size,
                     overwrite=overwrite)

    @mcp.tool()
    def pdf_to_images(path: str, output_dir: str = "", dpi: int = 150,
                      fmt: str = "png", first_page: int = 0,
                      last_page: int = 0, password: str = "") -> str:
        """Render each page to an image file (poppler pdftoppm).

        Defaults to 150 dpi PNG into a `<name>-images/` sibling directory.
        Useful for reading a scanned PDF, or as the first half of a
        rasterising round-trip with images_to_pdf."""
        return _wrap(ops.pdf_to_images, path=path,
                     output_dir=output_dir or None, dpi=dpi, fmt=fmt,
                     first_page=first_page, last_page=last_page,
                     password=password)

    @mcp.tool()
    def images_to_pdf(images: list[str], output: str,
                      overwrite: bool = False) -> str:
        """Combine image files into one PDF, one page per image, in the order
        given. `output` is required — nothing is guessed."""
        return _wrap(ops.images_to_pdf, images=list(images or []),
                     output=output, overwrite=overwrite)

    @mcp.tool()
    def pdf_merge(inputs: list[str], output: str,
                  overwrite: bool = False) -> str:
        """Concatenate two or more PDFs into `output`, in the order given.
        Refuses to write over any of its inputs."""
        return _wrap(ops.pdf_merge, inputs=list(inputs or []), output=output,
                     overwrite=overwrite)

    @mcp.tool()
    def pdf_split(path: str, pages: str, output: str = "",
                  overwrite: bool = False) -> str:
        """Extract a page range into a new PDF.

        `pages` uses qpdf range syntax: "1-3", "2", "1,4-5", "z" for the last
        page, "r1" counting from the end. Writes `<name>-p<range>.pdf` by
        default; the input is never modified."""
        return _wrap(ops.pdf_split, path=path, pages=pages,
                     output=output or None, overwrite=overwrite)

    @mcp.tool()
    def pdf_rotate(path: str, angle: int = 90, pages: str = "1-z",
                   output: str = "", overwrite: bool = False) -> str:
        """Rotate pages by 90, 180 or 270 degrees.

        `pages` is a qpdf range ("1-z" = all). Writes `<name>-rotated.pdf` by
        default; the input is never modified."""
        return _wrap(ops.pdf_rotate, path=path, angle=angle, pages=pages,
                     output=output or None, overwrite=overwrite)

    @mcp.tool()
    def pdf_print(path: str, printer: str = "", copies: int = 1,
                  pages: str = "", duplex: str = "", media: str = "",
                  allow_label: bool = False, dry_run: bool = False) -> str:
        """Send a PDF to a printer (CUPS `lp`). THIS PUTS INK ON PAPER.

        Call pdf_printers first and use an exact name; omitting `printer` uses
        the system default. `pages` is an lp range like "1-3"; `duplex` is one
        of one-sided / two-sided-long-edge / two-sided-short-edge.

        Label printers are refused unless `allow_label=true` — a document sent
        to a label roll runs off the end of it. Pass `dry_run=true` to see the
        exact `lp` command without spooling anything."""
        return _wrap(ops.pdf_print, path=path, printer=printer, copies=copies,
                     pages=pages, duplex=duplex, media=media,
                     allow_label=allow_label, dry_run=dry_run)

    return mcp


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
