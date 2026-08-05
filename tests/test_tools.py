"""Tests for tool implementations."""

from pathlib import Path

import pytest

from luxe.tools import fs
from luxe.tools.base import ToolCache, dispatch_tool, validate_args


@pytest.fixture(autouse=True)
def set_root(tmp_repo: Path):
    fs.set_repo_root(tmp_repo)
    yield
    fs._REPO_ROOT = None


class TestFsTools:
    def test_read_file(self, tmp_repo: Path):
        result, err = fs.READ_ONLY_FNS["read_file"]({"path": "src/main.py"})
        assert err is None
        assert "greet" in result
        assert "1\t" in result  # line numbers

    def test_read_file_not_found(self):
        result, err = fs.READ_ONLY_FNS["read_file"]({"path": "nonexistent.py"})
        assert err is not None
        assert "not found" in err.lower()

    def test_list_dir(self, tmp_repo: Path):
        result, err = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        assert err is None
        assert "src/" in result
        assert "README.md" in result

    def test_glob(self, tmp_repo: Path):
        result, err = fs.READ_ONLY_FNS["glob"]({"pattern": "**/*.py"})
        assert err is None
        assert "main.py" in result

    def test_grep(self, tmp_repo: Path):
        result, err = fs.READ_ONLY_FNS["grep"]({"pattern": "def greet"})
        assert err is None
        assert "greet" in result

    def test_write_file(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"](
            {"path": "new_file.py", "content": "print('hello')"}
        )
        assert err is None
        assert (tmp_repo / "new_file.py").read_text() == "print('hello')"

    def test_edit_file(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["edit_file"]({
            "path": "src/main.py",
            "old_string": "Hello",
            "new_string": "Hi",
        })
        assert err is None
        assert "Hi" in (tmp_repo / "src" / "main.py").read_text()

    def test_path_escape(self, tmp_repo: Path):
        with pytest.raises(PermissionError):
            fs._safe("../../etc/passwd")

    # --- Honesty guards (write-time defences against Phase 2 failure modes) ---

    def test_write_rejects_placeholder_text(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "stub.js",
            "content": "<paste the modified content here>",
        })
        assert err is not None
        assert "placeholder" in err.lower()
        assert not (tmp_repo / "stub.js").exists()

    def test_write_rejects_your_code_here(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "handler.js",
            "content": "function reset() {\n  // Your reset code here\n}",
        })
        assert err is not None
        assert "placeholder" in err.lower()

    def test_write_rejects_role_named_path(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "src/worker_read.js",
            "content": "console.log('ok');",
        })
        assert err is not None
        assert "role" in err.lower() and "worker_read" in err

    def test_write_rejects_role_named_in_subdir(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "src/input/worker_analyze/reset.py",
            "content": "def reset(): pass",
        })
        assert err is not None
        assert "worker_analyze" in err

    def test_write_rejects_mass_deletion(self, tmp_repo: Path):
        # Create a 60-line file then try to overwrite with a 2-line stub.
        (tmp_repo / "big.py").write_text("\n".join(f"line {i}" for i in range(60)))
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "big.py",
            "content": "def reset(): pass\n",
        })
        assert err is not None
        assert "mass-deletion" in err.lower() or "stub" in err.lower()
        # Original file untouched.
        assert (tmp_repo / "big.py").read_text().count("\n") >= 50

    def test_write_allows_legit_short_file(self, tmp_repo: Path):
        # A genuinely small new file should not trip the mass-deletion gate.
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "small_helper.py",
            "content": "X = 1\n",
        })
        assert err is None

    def test_write_allows_full_rewrite(self, tmp_repo: Path):
        # A full rewrite (large → large) should pass.
        (tmp_repo / "rewrite.py").write_text("\n".join(f"old{i}" for i in range(60)))
        new = "\n".join(f"new{i}" for i in range(60))
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "rewrite.py",
            "content": new,
        })
        assert err is None

    def test_edit_rejects_placeholder_in_replacement(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["edit_file"]({
            "path": "src/main.py",
            "old_string": "Hello",
            "new_string": "// TODO: implement greeting",
        })
        assert err is not None
        assert "placeholder" in err.lower()

    def test_edit_rejects_role_named_path(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["edit_file"]({
            "path": "drafter.py",
            "old_string": "x", "new_string": "y",
        })
        assert err is not None
        assert "drafter" in err

    def test_edit_rejects_mass_deletion(self, tmp_repo: Path):
        big = "\n".join(f"line {i}" for i in range(60))
        (tmp_repo / "shrink.py").write_text(big)
        result, err = fs.MUTATION_FNS["edit_file"]({
            "path": "shrink.py",
            "old_string": big,
            "new_string": "x = 1\n",
        })
        assert err is not None
        assert "mass-deletion" in err.lower() or "stub" in err.lower()

    # --- Evasion regressions: actual fail patterns from the Phase 2 re-test ---

    def test_write_rejects_role_name_with_suffix(self, tmp_repo: Path):
        # Model wrote `worker_read_r.py` to evade exact-stem matching.
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "src/worker_read_r.py",
            "content": "x = 1\n",
        })
        assert err is not None
        assert "worker_read" in err

    def test_write_rejects_role_name_with_prefix(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "src/my_drafter.py",
            "content": "x = 1\n",
        })
        assert err is not None
        assert "drafter" in err

    def test_write_allows_encoder_decoder(self, tmp_repo: Path):
        # "coder" intentionally excluded from single-token check so legit
        # names like encoder.py / decoder.py / transcoder.py pass.
        for name in ("encoder.py", "decoder.py", "transcoder.py"):
            result, err = fs.MUTATION_FNS["write_file"]({
                "path": f"src/{name}", "content": "x = 1\n",
            })
            assert err is None, f"{name}: unexpectedly rejected: {err}"

    def test_write_rejects_multi_word_placeholder(self, tmp_repo: Path):
        # Model wrote `# Your real listener code here` to evade single-word.
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "handler.js",
            "content": "function reset() {\n  // Your real listener code here\n}",
        })
        assert err is not None
        assert "placeholder" in err.lower()

    def test_write_rejects_attach_listener_here(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "h.js",
            "content": "// Attach the keydown listener here\n",
        })
        assert err is not None
        assert "placeholder" in err.lower()

    def test_write_rejects_real_logic_belongs_here(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "h.py",
            "content": "# Real handler logic belongs here\n",
        })
        assert err is not None
        assert "placeholder" in err.lower()

    def test_write_allows_legitimate_todo_comment(self, tmp_repo: Path):
        # Real-world TODO comments shouldn't trip the gate. The gate fires
        # only on TODO followed by a trigger verb, not bare TODOs.
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "feature.py",
            "content": "# TODO: deprecation tracker\nx = 1\n",
        })
        assert err is None


class TestToolCache:
    def test_cache_hit(self):
        cache = ToolCache()
        fn = lambda args: ("result", None)
        r1, e1, cached1 = cache.get_or_run("test", {"a": 1}, fn)
        r2, e2, cached2 = cache.get_or_run("test", {"a": 1}, fn)
        assert not cached1
        assert cached2
        assert cache.hits == 1
        assert cache.misses == 1

    def test_cache_miss_different_args(self):
        cache = ToolCache()
        fn = lambda args: (str(args), None)
        cache.get_or_run("test", {"a": 1}, fn)
        _, _, cached = cache.get_or_run("test", {"a": 2}, fn)
        assert not cached


class TestDispatchToolErrorCapture:
    """Regression: tools that raise must NOT escape dispatch_tool.

    Before the fix, an unhandled PermissionError from fs._safe (raised
    when the model passes an absolute path to read_file) escaped
    run_agent and killed luxe with wall=0s/tokens=0 — see the
    neon-rain-document-modules failure in acceptance/v1_default.
    Tools should now return the error string in ToolCall.error so the
    model can self-correct on the next turn.
    """

    def test_tool_raising_permissionerror_returns_error_not_exception(self):
        def raising_fn(args):
            raise PermissionError("Path escapes repo root: /src/foo.js")
        tc = dispatch_tool("read_file", {"path": "/src/foo.js"},
                           {"read_file": raising_fn})
        assert tc.error
        assert "PermissionError" in tc.error
        assert "Path escapes repo root" in tc.error
        assert tc.result == ""

    def test_tool_raising_filenotfound_returns_error_not_exception(self):
        def raising_fn(args):
            raise FileNotFoundError("missing config")
        tc = dispatch_tool("read_file", {"path": "missing.yaml"},
                           {"read_file": raising_fn})
        assert tc.error
        assert "FileNotFoundError" in tc.error
        assert tc.result == ""

    def test_normal_tool_return_path_unaffected(self):
        """Tools that return (result, err) must keep working unchanged."""
        def normal_fn(args):
            return "hello", None
        tc = dispatch_tool("read_file", {"path": "x"},
                           {"read_file": normal_fn})
        assert tc.error is None
        assert tc.result == "hello"

    def test_tool_name_whitespace_is_stripped(self):
        """GLM-4.5-Air-4bit emits tool names with trailing newlines
        (`"read_file\\n"`, `"bash\\n\\n"`). Without normalization the
        dispatch lookup misses, the model loops on broken calls, and
        the run bails with zero progress. The dispatcher strips
        whitespace before the lookup so stray newlines don't break
        otherwise-valid calls."""
        called = {"n": 0}
        def normal_fn(args):
            called["n"] += 1
            return "ok", None
        for variant in ("read_file\n", "  read_file  ", "read_file\n\n\n", "\tread_file"):
            tc = dispatch_tool(variant, {"path": "x"},
                               {"read_file": normal_fn})
            assert tc.error is None, f"variant {variant!r} failed: {tc.error}"
            assert tc.result == "ok"
            assert tc.name == "read_file"  # canonicalized
        assert called["n"] == 4

    def test_cached_tool_exception_not_poisoned_into_cache(self):
        """An exception during the first call must not be cached as a
        successful result — the cache stays empty so retries can succeed."""
        call_count = {"n": 0}
        def flaky_fn(args):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ValueError("transient")
            return "ok", None
        cache = ToolCache()
        tc1 = dispatch_tool("read_file", {"path": "x"},
                            {"read_file": flaky_fn},
                            cache=cache, cacheable={"read_file"})
        assert tc1.error and "ValueError" in tc1.error
        # Retry should re-invoke fn (cache miss), now succeed.
        tc2 = dispatch_tool("read_file", {"path": "x"},
                            {"read_file": flaky_fn},
                            cache=cache, cacheable={"read_file"})
        assert tc2.error is None
        assert tc2.result == "ok"
        assert call_count["n"] == 2  # both calls hit fn, exception not cached


class TestValidation:
    def test_valid_args(self):
        defn = fs.read_only_defs()[0]  # read_file
        err = validate_args(defn, {"path": "test.py"})
        assert err is None

    def test_missing_required(self):
        defn = fs.read_only_defs()[0]
        err = validate_args(defn, {})
        assert err is not None
        assert "required" in err.lower()


# --- read_file binary-rejection (2026-05-02 tool subphase) --

class TestReadFileBinaryRejection:
    """Reading a binary file with errors='replace' returns multi-MB of
    garbage that pollutes the model's context. The tool detects binary
    content (null bytes in first 8 KB) and returns a clean error."""

    def test_rejects_file_with_null_bytes(self, tmp_repo: Path):
        (tmp_repo / "blob.bin").write_bytes(b"PNG\x00\x01\x02header" + b"\xff" * 1000)
        result, err = fs.READ_ONLY_FNS["read_file"]({"path": "blob.bin"})
        assert result == ""
        assert err is not None
        assert "binary" in err.lower()

    def test_accepts_utf8_source(self, tmp_repo: Path):
        """UTF-8 source — no null bytes — must still read fine. Defends
        against false positives that would block legitimate code files
        (e.g. Python with unicode identifiers or accented strings)."""
        (tmp_repo / "src" / "unicode.py").write_text(
            "# encoding: utf-8\n"
            "def greet(): return 'héllo wörld'\n"
            "α = 'greek'\n",
            encoding="utf-8",
        )
        result, err = fs.READ_ONLY_FNS["read_file"]({"path": "src/unicode.py"})
        assert err is None
        assert "héllo" in result
        assert "α" in result

    def test_accepts_empty_file(self, tmp_repo: Path):
        """Empty file: no null bytes, no content, reads cleanly as ''."""
        (tmp_repo / "empty.txt").write_text("")
        result, err = fs.READ_ONLY_FNS["read_file"]({"path": "empty.txt"})
        assert err is None
        assert result == ""


# --- bash chain-rejection (2026-05-02 tool subphase) --

from luxe.tools import shell  # noqa: E402


class TestBashChainRejection:
    """Pre-2026-05-02 the bash tool checked parts[0] against the allowlist
    then ran the command via shell=True — so `cat foo && rm -rf /` passed
    the check (parts[0] == 'cat') and then `rm` executed despite not being
    in the allowlist. The hardened tool tokenizes via shlex and rejects
    chain operators, redirects, and command substitution."""

    def test_rejects_double_amp_chain(self, tmp_repo: Path):
        result, err = shell._bash({"command": "cat foo && rm -rf /"})
        assert result == ""
        assert err is not None
        assert "&&" in err  # message names the offending operator

    def test_rejects_double_pipe_chain(self, tmp_repo: Path):
        result, err = shell._bash({"command": "ls /missing || echo x"})
        assert result == ""
        assert err is not None
        assert "||" in err

    def test_rejects_semicolon_chain(self, tmp_repo: Path):
        result, err = shell._bash({"command": "ls ; rm -rf /"})
        assert result == ""
        assert err is not None
        assert ";" in err

    def test_rejects_pipe(self, tmp_repo: Path):
        """Pipes let the second binary bypass the allowlist. Model should
        issue a single bash call with grep+regex, or use the dedicated
        grep tool."""
        result, err = shell._bash({"command": "cat foo | wc"})
        assert result == ""
        assert err is not None

    def test_rejects_output_redirect(self, tmp_repo: Path):
        """Redirects let an allowlisted binary write outside the repo
        (`cat foo > /etc/passwd`). Reject; use write_file instead."""
        result, err = shell._bash({"command": "cat foo > /tmp/leak"})
        assert result == ""
        assert err is not None
        assert ">" in err

    def test_rejects_backtick_command_substitution(self, tmp_repo: Path):
        """Backticks run an inner command whose binary isn't allowlisted."""
        result, err = shell._bash({"command": "cat `find / -name passwd`"})
        assert result == ""
        assert err is not None
        assert "substitution" in err.lower()

    def test_rejects_dollar_paren_substitution(self, tmp_repo: Path):
        """$(...) is the modern form of command substitution."""
        result, err = shell._bash({"command": "cat $(echo /etc/passwd)"})
        assert result == ""
        assert err is not None
        assert "substitution" in err.lower()

    def test_quoted_pipe_in_regex_is_allowed(self, tmp_repo: Path):
        """`|` inside a quoted regex isn't a shell operator — shlex respects
        quotes. Must NOT be rejected; the model needs alternation in regex
        patterns. (The command may exit non-zero on no match; that's fine.)"""
        result, err = shell._bash({"command": 'grep "foo|bar" src/main.py'})
        if err:
            assert "operator" not in err.lower()
            assert "substitution" not in err.lower()

    def test_unallowlisted_first_binary_still_rejected(self, tmp_repo: Path):
        """Existing allowlist behavior preserved — `rm` alone is rejected
        before any chain logic kicks in."""
        result, err = shell._bash({"command": "rm -rf /"})
        assert result == ""
        assert err is not None
        assert "allowlist" in err.lower()

    def test_normal_allowlisted_command_still_works(self, tmp_repo: Path):
        """Sanity: hardening didn't break the happy path."""
        result, err = shell._bash({"command": "ls src/"})
        assert err is None
        assert "main.py" in result

    def test_mismatched_quotes_returns_clean_error(self, tmp_repo: Path):
        """shlex raises ValueError on mismatched quotes; we return a
        structured error rather than letting the exception escape."""
        result, err = shell._bash({"command": "echo 'unclosed"})
        assert result == ""
        assert err is not None
        assert "parse" in err.lower() or "quote" in err.lower()


class TestUnrestrictedBash:
    """Opt-in chat dev mode (chat.sdd): make_bash_fn(unrestricted=True) drops the
    allowlist + chain/redirect guards. The DEFAULT fn stays hardened so the
    benchmark/maintain path is unchanged."""

    def test_default_fn_still_hardened(self, tmp_repo: Path):
        # The module-level TOOL_FNS["bash"] must keep rejecting non-allowlisted.
        result, err = shell.TOOL_FNS["bash"]({"command": "mkdir foo"})
        assert err is not None and "allowlist" in err.lower()

    def test_unrestricted_allows_non_allowlisted_binary(self, tmp_repo: Path):
        fn = shell.make_bash_fn(unrestricted=True)
        result, err = fn({"command": "mkdir -p subdir/nested"})
        assert err is None
        assert (tmp_repo / "subdir/nested").is_dir()

    def test_unrestricted_allows_chains(self, tmp_repo: Path):
        fn = shell.make_bash_fn(unrestricted=True)
        result, err = fn({"command": "mkdir -p a && echo done > a/marker.txt"})
        assert err is None
        assert (tmp_repo / "a/marker.txt").read_text().strip() == "done"

    def test_unrestricted_path_form_binary_runs(self, tmp_repo: Path):
        # The m1 failure: ./venv/bin/pip was rejected by the allowlist. Path-form
        # binaries run fine in dev mode.
        (tmp_repo / "venv/bin").mkdir(parents=True)
        script = tmp_repo / "venv/bin/echoer"
        script.write_text("#!/bin/sh\necho hi\n")
        script.chmod(0o755)
        fn = shell.make_bash_fn(unrestricted=True)
        result, err = fn({"command": "./venv/bin/echoer"})
        assert err is None and "hi" in result

    def test_unrestricted_def_overrides_in_run_single(self):
        # Sanity: extra_tool_defs with name 'bash' replaces (not duplicates) the
        # base def — the override contract run_single relies on for dev mode.
        d = shell.unrestricted_bash_def()
        assert d.name == "bash"
        assert "ANY shell command" in d.description

    def test_restricted_hint_explains_flag_state_on_allowlist_reject(self, tmp_repo: Path):
        fn = shell.make_bash_fn(restricted_hint=True)
        _out, err = fn({"command": "mkdir foo"})
        assert err is not None and "/bash" in err and "not in allowlist" in err

    def test_restricted_hint_explains_flag_state_on_chain_reject(self, tmp_repo: Path):
        fn = shell.make_bash_fn(restricted_hint=True)
        _out, err = fn({"command": "ls && ls"})
        assert err is not None and "/bash" in err
        assert err.index("/bash") < 80  # survives the 80-char tool-line truncation

    def test_restricted_hint_NOT_added_to_real_errors(self, tmp_repo: Path):
        # Mismatched quotes is a genuine error, not a flag state — no /bash hint.
        fn = shell.make_bash_fn(restricted_hint=True)
        _out, err = fn({"command": "echo 'unclosed"})
        assert err is not None and "/bash" not in err

    def test_restricted_bash_def_tells_model_to_surface_flag(self):
        assert "/bash" in shell.restricted_bash_def().description

    def test_chat_bash_env_resolves_pytest(self, tmp_repo: Path):
        """The m5 gap (2026-07-30): its default PATH had no python/pytest, so
        a write-mode agent could build but never verify. Chat bash prepends
        luxe's venv bin, so pytest resolves on EVERY fleet host."""
        import sys
        from pathlib import Path as P

        env = shell._chat_bash_env()
        assert env["PATH"].startswith(str(P(sys.executable).parent))

        fn = shell.make_bash_fn(restricted_hint=True)
        out, err = fn({"command": "pytest --version"})
        assert err is None and "pytest" in out

    def test_bench_bash_env_is_untouched(self, tmp_repo: Path, monkeypatch):
        """The benchmark path's bash (module default, env=None) must inherit
        the process environment byte-identically — no venv injection."""
        seen = {}
        real_run = shell.subprocess.run

        def spy(*a, **k):
            seen["env"] = k.get("env", "MISSING")
            return real_run(*a, **k)

        monkeypatch.setattr(shell.subprocess, "run", spy)
        shell._bash({"command": "echo ok"})
        assert seen["env"] is None


class _Tok:
    """Duck-typed CancelToken (shell must not import from luxe.chat)."""

    def __init__(self, requested=False):
        self.requested = requested


class TestCancellableBash:
    """Chat-only cancellable subprocess runner (2026-07-31, session
    5bb630813c21): with a CancelToken, esc kills the process group within the
    poll cadence instead of blocking until the 60/600s timeout. The benchmark
    path (cancel=None) keeps subprocess.run byte-identically."""

    def test_bench_path_never_uses_cancellable_runner(self, tmp_repo: Path, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("cancellable runner reached from cancel=None")

        monkeypatch.setattr(shell, "_run_cancellable", boom)
        out, err = shell._bash({"command": "echo ok"})
        assert err is None and "ok" in out  # subprocess.run path, unchanged

    def test_cancellable_path_happy_output(self, tmp_repo: Path):
        fn = shell.make_bash_fn(unrestricted=True, cancel=_Tok())
        out, err = fn({"command": "echo hello && echo world"})
        assert err is None
        assert "hello" in out and "world" in out

    def test_cancellable_path_nonzero_exit(self, tmp_repo: Path):
        fn = shell.make_bash_fn(unrestricted=True, cancel=_Tok())
        _out, err = fn({"command": "exit 3"})
        assert err == "exit code 3"

    def test_pre_set_cancel_kills_immediately(self, tmp_repo: Path):
        import time as _t

        fn = shell.make_bash_fn(unrestricted=True, cancel=_Tok(requested=True))
        t0 = _t.monotonic()
        out, err = fn({"command": "sleep 30"})
        assert _t.monotonic() - t0 < 5  # nowhere near the 600s budget
        assert out == "" and err == "cancelled by user"

    def test_cancel_mid_flight_kills_within_poll_cadence(self, tmp_repo: Path):
        import threading
        import time as _t

        tok = _Tok()
        fn = shell.make_bash_fn(unrestricted=True, cancel=tok)
        threading.Timer(0.5, lambda: setattr(tok, "requested", True)).start()
        t0 = _t.monotonic()
        _out, err = fn({"command": "sleep 30"})
        assert _t.monotonic() - t0 < 5
        assert err == "cancelled by user"

    def test_cancellable_path_still_enforces_timeout(self, tmp_repo: Path, monkeypatch):
        monkeypatch.setattr(shell, "_UNRESTRICTED_TIMEOUT", 1)
        fn = shell.make_bash_fn(unrestricted=True, cancel=_Tok())
        _out, err = fn({"command": "sleep 30"})
        assert err == "Command timed out after 1s"

    def test_cancel_kills_whole_process_group(self, tmp_repo: Path):
        """`sh -c` children must die with the shell — killing only the shell
        would leave the hung curl running (the original failure mode). The
        `&& echo` keeps sh as the parent (no exec), so python is a genuine
        child of the group."""
        import os
        import threading
        import time as _t

        pidfile = tmp_repo / "child.pid"
        tok = _Tok()
        fn = shell.make_bash_fn(unrestricted=True, cancel=tok)
        threading.Timer(1.0, lambda: setattr(tok, "requested", True)).start()
        cmd = ("python3 -c \"import os,time; "
               "open('child.pid','w').write(str(os.getpid())); "
               "time.sleep(30)\" && echo done")
        _out, err = fn({"command": cmd})
        assert err == "cancelled by user"
        pid = int(pidfile.read_text())
        deadline = _t.monotonic() + 3
        while _t.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return  # child died with the group
            _t.sleep(0.1)
        os.kill(pid, 9)  # don't leak the child on failure
        raise AssertionError("sh's python child survived the group kill")

    def test_on_start_fires_at_dispatch_with_command(self, tmp_repo: Path):
        seen: list = []
        fn = shell.make_bash_fn(unrestricted=True, on_start=seen.append)
        fn({"command": "echo dispatched"})
        assert seen == ["echo dispatched"]

    def test_on_start_exception_never_fails_the_tool(self, tmp_repo: Path):
        def boom(_cmd):
            raise RuntimeError("ui died")

        fn = shell.make_bash_fn(unrestricted=True, on_start=boom)
        out, err = fn({"command": "echo ok"})
        assert err is None and "ok" in out

    def test_restricted_variant_also_cancellable(self, tmp_repo: Path):
        """Write-mode (allowlisted) chat bash gets the same cancel path."""
        import time as _t

        fn = shell.make_bash_fn(restricted_hint=True, cancel=_Tok(requested=True))
        t0 = _t.monotonic()
        _out, err = fn({"command": "python -c 'import time; time.sleep(30)'"})
        assert _t.monotonic() - t0 < 5
        assert err == "cancelled by user"


# --- filesystem tools survive unreadable directories -------------------------


def _fail_scandir_under(monkeypatch, doomed, exc):
    import os as _os
    from pathlib import Path as _Path

    real = _os.scandir

    def fake(path=".", *a, **k):
        p = _Path(_os.fspath(path))
        if p == doomed or doomed in p.parents:
            raise exc
        return real(path, *a, **k)

    monkeypatch.setattr(_os, "scandir", fake)


def test_list_dir_reports_an_unreadable_directory(tmp_path, monkeypatch):
    """A dead network mount raises OSError(ETIMEDOUT) from iterdir; the model
    should get a sentence, not a raw errno."""
    from luxe.tools import fs

    doomed = tmp_path / "nas"
    doomed.mkdir()
    fs.set_repo_root(str(tmp_path))
    monkeypatch.setattr(type(doomed), "iterdir",
                        lambda self: (_ for _ in ()).throw(
                            OSError(60, "Operation timed out")))

    result, err = fs._list_dir({"path": "nas"})

    assert result == ""
    assert "Cannot read directory" in err and "timed out" in err


def test_glob_returns_partial_results_when_a_subtree_dies(tmp_path, monkeypatch):
    """pathlib's glob generator dies on the first non-permission OSError and
    can't resume — return what we have plus why, not a tool error."""
    import errno

    from luxe.tools import fs

    (tmp_path / "a.py").write_text("x")
    doomed = tmp_path / "nas"
    doomed.mkdir()
    (doomed / "b.py").write_text("y")
    fs.set_repo_root(str(tmp_path))

    matches, stopped = fs._glob_matches_tolerant(tmp_path, "*.py")
    assert [m.name for m in matches] == ["a.py"] and stopped == ""

    _fail_scandir_under(monkeypatch, tmp_path,
                        OSError(errno.ETIMEDOUT, "Operation timed out"))
    matches, stopped = fs._glob_matches_tolerant(tmp_path, "**/*.py")
    assert "timed out" in stopped        # reported, not raised


class TestProseAwareWriteFns:
    """Chat-only write/edit variants (make_prose_aware_write_fns): prose
    files skip the placeholder guard — "save these notes with '# TODO:
    implement X'" is user-dictated content, not a code stub. Everything else
    (code extensions, role-path, mass-deletion, path scoping) is unchanged,
    and the DEFAULT fns keep guarding prose so the benchmark path is
    byte-identical."""

    def test_prose_write_allows_placeholder_text(self, tmp_repo: Path):
        fns = fs.make_prose_aware_write_fns()
        result, err = fns["write_file"]({
            "path": "session-notes.txt",
            "content": "# paste your RELAY_TOKEN here\n# TODO: implement weekly report\n",
        })
        assert err is None
        assert (tmp_repo / "session-notes.txt").is_file()

    def test_prose_markdown_also_exempt(self, tmp_repo: Path):
        fns = fs.make_prose_aware_write_fns()
        _, err = fns["write_file"]({
            "path": "runbook.md",
            "content": "steps:\n\n    # fill in the token here\n",
        })
        assert err is None

    def test_code_write_still_guarded(self, tmp_repo: Path):
        fns = fs.make_prose_aware_write_fns()
        _, err = fns["write_file"]({
            "path": "handler.js",
            "content": "function reset() {\n  // Your reset code here\n}",
        })
        assert err is not None
        assert "placeholder" in err.lower()

    def test_default_fns_still_guard_prose(self, tmp_repo: Path):
        """Benchmark behavior unchanged: the module TOOL_FNS block placeholder
        text in .txt too."""
        _, err = fs.MUTATION_FNS["write_file"]({
            "path": "notes.txt",
            "content": "<paste the modified content here>",
        })
        assert err is not None
        assert "placeholder" in err.lower()

    def test_prose_edit_allows_placeholder_replacement(self, tmp_repo: Path):
        (tmp_repo / "notes.txt").write_text("line one\n")
        fns = fs.make_prose_aware_write_fns()
        _, err = fns["edit_file"]({
            "path": "notes.txt",
            "old_string": "line one",
            "new_string": "# TODO: implement the cron entry",
        })
        assert err is None
        assert "# TODO: implement" in (tmp_repo / "notes.txt").read_text()

    def test_prose_edit_code_file_still_guarded(self, tmp_repo: Path):
        (tmp_repo / "mod.py").write_text("x = 1\n")
        fns = fs.make_prose_aware_write_fns()
        _, err = fns["edit_file"]({
            "path": "mod.py",
            "old_string": "x = 1",
            "new_string": "# TODO: implement x properly",
        })
        assert err is not None
        assert "placeholder" in err.lower()

    def test_prose_variant_keeps_other_guards(self, tmp_repo: Path):
        fns = fs.make_prose_aware_write_fns()
        # role-path guard intact
        _, err = fns["write_file"]({"path": "verifier.txt", "content": "hi"})
        assert err is not None and "role label" in err
        # mass-deletion guard intact
        big = "\n".join(f"line {i}" for i in range(120)) + "\n"
        (tmp_repo / "big.txt").write_text(big)
        _, err = fns["write_file"]({"path": "big.txt", "content": "stub\n"})
        assert err is not None
        # path scoping intact
        _, err = fns["write_file"]({"path": "../escape.txt", "content": "x"})
        assert err is not None


class TestChatExtraAllow:
    """`gh` on the CHAT bash allowlist only (2026-08-05). The benchmark
    default `_bash` and its golden-pinned tool description must not learn
    about it — a bench that can reach GitHub's API is not reproducible."""

    def test_default_bash_still_rejects_gh(self, tmp_repo: Path):
        result, err = shell._bash({"command": "gh pr list"})
        assert result == ""
        assert err is not None and "allowlist" in err.lower()

    def test_default_rejection_message_is_unchanged(self, tmp_repo: Path):
        # extra_allow's empty default must not perturb the message the
        # benchmark model sees (sorted union with the empty set).
        _, err = shell._bash({"command": "rm -rf /"})
        assert err == (f"Command 'rm' not in allowlist. "
                       f"Allowed: {sorted(shell._ALLOWLIST)}")

    def test_chat_bash_lets_gh_through_the_allowlist(self, tmp_repo: Path):
        # `gh` may or may not be installed here; the contract under test is
        # only that the ALLOWLIST no longer rejects it in a chat session.
        fn = shell.make_bash_fn(restricted_hint=True)
        _, err = fn({"command": "gh --version"})
        if err is not None:
            assert "not in allowlist" not in err

    def test_chat_bash_still_rejects_other_binaries(self, tmp_repo: Path):
        fn = shell.make_bash_fn(restricted_hint=True)
        _, err = fn({"command": "rm -rf /"})
        assert err is not None and "not in allowlist" in err
        assert "gh" in err  # the chat rejection lists the widened allowlist

    def test_restricted_def_advertises_gh_but_default_def_does_not(self):
        assert "gh" in shell.restricted_bash_def().description.split(", ")
        default_desc = shell.tool_defs()[0].description
        assert "gh," not in default_desc and ": gh" not in default_desc
