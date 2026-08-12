"""The PDF MCP tools were unbounded, and read "CUPS is down" as "no printers".

`_run` shelled out to qpdf/gs/pdftotext with no timeout and stdin inherited.
qpdf and gs PROMPT on stdin for a password, so a malformed or encrypted input
could park an MCP tool forever — an MCP tool that never returns holds the
session with it — or eat the input meant for the session.

`pdf_printers` discarded `lpstat`'s exit code. With cupsd down, `lpstat -p`
fails and prints nothing, and the tool answered `{"printers": []}`: "this Mac
has no printers configured". Different problem, different fix, and `pdf_print`
then reported "no printer given and no CUPS default is set". `lpstat -d` is
kept non-fatal on purpose — no default destination is a real answer.

The module only needs `subprocess` here, so these tests deliberately do not
require the `[pdf]` extra or any CLI.
"""

from __future__ import annotations

import subprocess

import pytest

from luxe.mcp_pdf import ops
from luxe.mcp_pdf.ops import PdfToolError


def _fake_run(monkeypatch, *, rc: int = 0, stdout: str = "", stderr: str = "",
              raises: BaseException | None = None) -> dict:
    seen: dict = {}

    def _run(argv, **kw):
        seen.update(kw)
        seen["argv"] = list(argv)
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)

    monkeypatch.setattr(ops.subprocess, "run", _run)
    return seen


class TestEveryToolCallIsBounded:
    def test_run_passes_a_timeout(self, monkeypatch):
        seen = _fake_run(monkeypatch)
        ops._run(["qpdf", "--version"], what="probe")
        assert seen["timeout"] == ops._RUN_TIMEOUT_S

    def test_run_is_non_interactive(self, monkeypatch):
        seen = _fake_run(monkeypatch)
        ops._run(["qpdf", "--version"], what="probe")
        assert seen["stdin"] is subprocess.DEVNULL
        assert seen["errors"] == "replace"

    def test_a_wedged_tool_becomes_a_tool_error(self, monkeypatch):
        _fake_run(monkeypatch,
                  raises=subprocess.TimeoutExpired(cmd=["qpdf"], timeout=60))
        with pytest.raises(PdfToolError) as e:
            ops._run(["qpdf", "in.pdf"], what="unlock")
        assert "did not finish" in str(e.value)

    def test_a_missing_binary_still_gets_the_install_hint(self, monkeypatch):
        _fake_run(monkeypatch, raises=FileNotFoundError("qpdf"))
        with pytest.raises(PdfToolError) as e:
            ops._run(["qpdf", "in.pdf"], what="unlock")
        assert "brew install" in str(e.value)

    def test_a_nonzero_exit_still_carries_stderr(self, monkeypatch):
        _fake_run(monkeypatch, rc=2, stderr="qpdf: invalid password")
        with pytest.raises(PdfToolError) as e:
            ops._run(["qpdf", "in.pdf"], what="unlock")
        assert "invalid password" in str(e.value)

    def test_a_successful_call_returns_the_process(self, monkeypatch):
        _fake_run(monkeypatch, rc=0, stdout="ok")
        assert ops._run(["qpdf", "--version"], what="probe").stdout == "ok"

    def test_rasterizing_gets_a_longer_budget(self, monkeypatch):
        """A long document at high DPI is slow on purpose; only that one op
        is allowed past the 60s bound (same slow-vs-wedged distinction
        `gitclone` draws)."""
        seen = _fake_run(monkeypatch)
        ops._run(["pdftoppm"], what="pdf_to_images",
                 timeout=ops._RENDER_TIMEOUT_S)
        assert seen["timeout"] == ops._RENDER_TIMEOUT_S > ops._RUN_TIMEOUT_S


class TestPrinterDiscovery:
    def _lpstat(self, monkeypatch, results: dict[str, tuple[int, str]]):
        monkeypatch.setattr(ops, "_require_bin", lambda name: f"/usr/bin/{name}")

        def _run(argv, **kw):
            rc, out = results[argv[1]]
            return subprocess.CompletedProcess(argv, rc, out,
                                               "" if rc == 0 else "lpstat: error")

        monkeypatch.setattr(ops.subprocess, "run", _run)

    def test_cups_down_is_an_error_not_an_empty_list(self, monkeypatch):
        self._lpstat(monkeypatch, {"-p": (1, ""), "-d": (1, "")})
        with pytest.raises(PdfToolError) as e:
            ops.pdf_printers()
        assert "NOT an empty printer list" in str(e.value)

    def test_the_error_says_how_to_check(self, monkeypatch):
        self._lpstat(monkeypatch, {"-p": (1, ""), "-d": (1, "")})
        with pytest.raises(PdfToolError) as e:
            ops.pdf_printers()
        assert "lpstat -r" in str(e.value)

    def test_a_healthy_host_is_unchanged(self, monkeypatch):
        self._lpstat(monkeypatch, {
            "-p": (0, "printer Office_LaserJet is idle.  enabled since now\n"),
            "-d": (0, "system default destination: Office_LaserJet\n"),
        })
        got = ops.pdf_printers()
        assert got["default"] == "Office_LaserJet"
        assert got["printers"] == [{"name": "Office_LaserJet", "state": "idle",
                                    "label_printer": False}]

    def test_no_default_destination_is_still_a_valid_answer(self, monkeypatch):
        """`lpstat -d` exits non-zero when nothing is set as default — that is
        an answer, not a failure, so it must not raise."""
        self._lpstat(monkeypatch, {
            "-p": (0, "printer Office_LaserJet is idle.\n"),
            "-d": (1, ""),
        })
        got = ops.pdf_printers()
        assert got["default"] == ""
        assert len(got["printers"]) == 1

    def test_a_genuinely_printerless_host_still_answers_empty(self, monkeypatch):
        self._lpstat(monkeypatch, {"-p": (0, ""), "-d": (0, "")})
        got = ops.pdf_printers()
        assert got["printers"] == [] and got["default"] == ""

    def test_lpstat_is_bounded_and_non_interactive(self, monkeypatch):
        seen: dict = {}

        def _run(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(ops.subprocess, "run", _run)
        ops._lpstat("-p")
        assert seen["timeout"] == ops._RUN_TIMEOUT_S
        assert seen["stdin"] is subprocess.DEVNULL

    def test_a_wedged_print_system_is_named_as_such(self, monkeypatch):
        def _run(argv, **kw):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=60)

        monkeypatch.setattr(ops.subprocess, "run", _run)
        with pytest.raises(PdfToolError) as e:
            ops._lpstat("-p")
        assert "wedged, not printer-less" in str(e.value)
