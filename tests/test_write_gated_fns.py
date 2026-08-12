"""Read-only mode must say "gated", not "unknown".

`make_read_only_role` strips `write_file`/`edit_file`/`bash` from the role's
tool list, so they leave both the DEFS (correct — the model is not offered
them) and the FNS. A model that called one anyway reached `dispatch_tool`'s
missing-name branch and got:

    Unknown tool: edit_file

which is false. The tool exists, works, and is one `/write` away. Nothing in
that message says so. Observed 2026-08-11, session 0e524f033300 run -14: the
model composed a complete file body, handed it to `edit_file`, was told the
tool was unknown, and the turn ended three steps later with the work
discarded — the same shape as the oversized-`read_file` dead end fixed the
same day (`tests/test_read_file_large.py`).

The fix registers stub FNS with no DEFS: invisible in the offered surface,
self-explanatory when hallucinated.
"""

from __future__ import annotations

import pytest

from luxe.tools.fs import _WRITE_GATED_HINTS, make_write_gated_fns
from luxe.tools.base import dispatch_tool


@pytest.fixture
def gated():
    return make_write_gated_fns()


class TestTheGatedStubs:
    def test_it_covers_exactly_the_stripped_tools(self, gated):
        """Must track `mcp.server._MUTATION_TOOL_NAMES` — a tool stripped
        without a stub here silently regains the "Unknown tool" message."""
        from luxe.mcp.server import _MUTATION_TOOL_NAMES
        assert set(gated) == _MUTATION_TOOL_NAMES

    @pytest.mark.parametrize("name", sorted(_WRITE_GATED_HINTS))
    def test_each_one_errors_without_touching_disk(self, name, gated, tmp_path):
        out, err = gated[name]({"path": str(tmp_path / "x.py"), "content": "x",
                                "command": "ls"})
        assert out == ""
        assert err is not None
        assert not (tmp_path / "x.py").exists()

    @pytest.mark.parametrize("name", sorted(_WRITE_GATED_HINTS))
    def test_each_one_names_the_toggle(self, name, gated):
        _, err = gated[name]({})
        assert "/write" in err

    @pytest.mark.parametrize("name", sorted(_WRITE_GATED_HINTS))
    def test_each_one_says_the_tool_is_not_missing(self, name, gated):
        """The correction that matters: a model told "unknown" concludes the
        capability does not exist and stops looking for it."""
        _, err = gated[name]({})
        assert "gated, not missing" in err

    @pytest.mark.parametrize("name", sorted(_WRITE_GATED_HINTS))
    def test_each_one_tells_the_model_not_to_retry(self, name, gated):
        _, err = gated[name]({})
        assert "Do not retry" in err

    def test_the_message_is_specific_to_the_tool(self, gated):
        """"You can't write files" and "you can't run shell" are different
        problems for the user to act on."""
        _, write_err = gated["write_file"]({})
        _, bash_err = gated["bash"]({})
        assert "create or overwrite files" in write_err
        assert "run shell commands" in bash_err

    def test_it_reports_nothing_changed_on_disk(self, gated):
        _, err = gated["write_file"]({})
        assert "Nothing was changed on disk" in err


class TestThroughDispatch:
    def test_a_registered_stub_replaces_the_unknown_tool_error(self, gated):
        tc = dispatch_tool("edit_file", {"path": "a.py"}, dict(gated))
        assert tc.error is not None
        assert not tc.error.startswith("Unknown tool")
        assert "/write" in tc.error

    def test_a_genuinely_unknown_tool_still_reads_as_unknown(self, gated):
        """The taxonomy's `tool_reject` event keys on the "Unknown tool"
        prefix (`loop.py`), and a real hallucinated name must keep producing
        it — the two cases are different failures and must stay countable
        apart."""
        tc = dispatch_tool("summon_daemon", {}, dict(gated))
        assert tc.error.startswith("Unknown tool")

    def test_the_stub_is_overridden_when_write_mode_registers_the_real_fn(self):
        """Write mode puts the real fns in the same dict; last write wins, and
        the gate stub must not shadow a working tool."""
        fns = dict(make_write_gated_fns())
        fns["write_file"] = lambda args: ("wrote it", None)
        tc = dispatch_tool("write_file", {"path": "a.py"}, fns)
        assert tc.error is None
        assert tc.result == "wrote it"
