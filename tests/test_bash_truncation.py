"""Bash output cap keeps the head AND the tail (2026-08-12, audit F12).

The old form kept the FIRST 8 KB of a capped output. Test runners put the
failure summary at the END — pytest's short summary, the exit status context —
so a capped test run reliably lost exactly the lines the model needed.
`_clip_output` keeps both ends around an announced omission marker.

Bench relevance, measured not assumed: zero of 183 bash calls across six full
maintain_suite passes (2026-08-12) reach the cap, so the suite cannot A/B this
shape — the evidence is these tests plus the confirmation run in
`acceptance/bash_tail_trunc_2026_08_12/`.
"""
from __future__ import annotations

from luxe.tools.shell import _HEAD_KEEP, _MAX_OUTPUT, _TAIL_KEEP, _bash, _clip_output


class TestClipOutput:
    def test_under_cap_is_byte_identical(self):
        s = "line\n" * 100
        assert _clip_output(s) is s

    def test_exactly_at_cap_is_byte_identical(self):
        s = "x" * _MAX_OUTPUT
        assert _clip_output(s) is s

    def test_over_cap_keeps_both_ends_and_announces_omission(self):
        s = "HEAD-FIRST-LINE\n" + ("filler line\n" * 2000) + "TAIL-LAST-LINE\n"
        out = _clip_output(s)
        assert out.startswith("HEAD-FIRST-LINE\n")
        assert out.endswith("TAIL-LAST-LINE\n")
        assert "bytes omitted" in out and "head and tail kept" in out
        # The kept content stays within the budget (plus the one marker line).
        assert len(out) <= _HEAD_KEEP + _TAIL_KEEP + 120

    def test_omitted_count_is_exact(self):
        s = "a" * 50_000
        out = _clip_output(s)
        marker = out[out.index("... ("):]
        omitted = int(marker.split("(")[1].split(" bytes")[0].replace(",", ""))
        kept = len(s) - omitted
        # kept head+tail bytes of the ORIGINAL must equal len(s) - omitted
        assert kept == len(out) - len(
            f"... ({omitted:,} bytes omitted — output capped at "
            f"{_MAX_OUTPUT:,}; head and tail kept)\n")

    def test_pytest_shaped_output_keeps_the_summary_the_old_form_lost(self):
        """The founding case: a long test run's short summary is at the tail."""
        body = "".join(f"test_mod.py::test_{i} PASSED\n" for i in range(600))
        summary = ("=========== short test summary info ===========\n"
                   "FAILED test_mod.py::test_599 - AssertionError\n"
                   "===== 1 failed, 599 passed in 12.34s =====\n")
        s = body + summary
        assert len(s) > _MAX_OUTPUT
        out = _clip_output(s)
        assert "short test summary info" in out
        assert "1 failed, 599 passed" in out
        # and the old behaviour demonstrably lost it:
        assert "short test summary info" not in s[:_MAX_OUTPUT]

    def test_snaps_to_line_boundaries_when_a_newline_is_in_budget(self):
        s = "".join(f"row-{i:06d}\n" for i in range(3000))
        out = _clip_output(s)
        head, _, rest = out.partition("... (")
        # head ends on a newline, tail starts on a row start — no mid-line
        # splices when line boundaries exist within the kept budgets
        assert head.endswith("\n")
        tail = rest.partition(")\n")[2]
        assert tail.startswith("row-")
        assert tail.endswith("row-002999\n")

    def test_headless_blob_still_clips_without_snapping(self):
        # No newline anywhere in the head budget: snapping is best-effort,
        # the clip still happens and both ends survive.
        s = ("A" * 3000) + "\n" + ("B" * 9000) + "\nEND\n"
        out = _clip_output(s)
        assert out.startswith("A")
        assert out.endswith("END\n")
        assert "bytes omitted" in out


class TestBashToolIntegration:
    def test_real_over_cap_command_returns_tail(self, tmp_path):
        from luxe.tools import fs
        fs.set_repo_root(tmp_path)
        script = tmp_path / "noise.py"
        script.write_text(
            "print('FIRST-MARKER')\n"
            "print('noise ' * 4000)\n"
            "print('LAST-MARKER')\n")
        out, err = _bash({"command": f"python {script}"})
        assert err is None
        assert "FIRST-MARKER" in out
        assert "LAST-MARKER" in out
        assert "bytes omitted" in out

    def test_small_command_output_untouched(self, tmp_path):
        from luxe.tools import fs
        fs.set_repo_root(tmp_path)
        out, err = _bash({"command": "echo tail-shape-untouched"})
        assert err is None
        assert out == "tail-shape-untouched\n"
