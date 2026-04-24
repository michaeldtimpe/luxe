"""Tests for luxe.tasks.cache — the task-scoped ToolCache + wrap_tool_fns."""

from __future__ import annotations

from cli.tasks.cache import ToolCache, wrap_tool_fns


def _counter_fn():
    """Return (fn, calls) where fn increments calls on every invocation.
    Lets a test assert the cache actually shortcut the inner fn instead
    of re-running it."""
    state = {"n": 0}

    def fn(args):
        state["n"] += 1
        return (f"result:{args.get('x')}", None)

    return fn, state


def test_repeat_call_same_args_is_one_inner_call():
    cache = ToolCache()
    fn, state = _counter_fn()
    r1, _, cached1 = cache.get_or_run("t", {"x": 1}, fn)
    r2, _, cached2 = cache.get_or_run("t", {"x": 1}, fn)
    assert r1 == r2 == "result:1"
    assert state["n"] == 1
    assert cached1 is False
    assert cached2 is True
    assert cache.hits == 1
    assert cache.misses == 1


def test_different_args_miss_separately():
    cache = ToolCache()
    fn, state = _counter_fn()
    cache.get_or_run("t", {"x": 1}, fn)
    cache.get_or_run("t", {"x": 2}, fn)
    assert state["n"] == 2
    assert cache.hits == 0
    assert cache.misses == 2


def test_arg_order_does_not_split_key():
    cache = ToolCache()
    fn, state = _counter_fn()
    cache.get_or_run("t", {"a": 1, "b": 2}, fn)
    cache.get_or_run("t", {"b": 2, "a": 1}, fn)
    # sort_keys collapses the dicts to the same hash.
    assert state["n"] == 1
    assert cache.hits == 1


def test_errors_are_cached_too():
    """A malformed call won't magically succeed on retry — so we cache
    errors too, same as successful results."""
    cache = ToolCache()
    calls = {"n": 0}

    def always_fails(args):
        calls["n"] += 1
        return (None, "boom")

    r1, e1, _ = cache.get_or_run("t", {}, always_fails)
    r2, e2, cached = cache.get_or_run("t", {}, always_fails)
    assert e1 == e2 == "boom"
    assert r1 is None and r2 is None
    assert calls["n"] == 1
    assert cached is True


def test_wrap_tool_fns_only_memoizes_cacheable():
    cache = ToolCache()
    read_fn, read_state = _counter_fn()
    write_fn, write_state = _counter_fn()
    fns = {"read": read_fn, "write": write_fn}

    wrapped = wrap_tool_fns(fns, cache, cacheable={"read"})

    # Reads collapse after the first call.
    wrapped["read"]({"x": 1})
    wrapped["read"]({"x": 1})
    assert read_state["n"] == 1

    # Writes always hit the underlying fn.
    wrapped["write"]({"x": 1})
    wrapped["write"]({"x": 1})
    assert write_state["n"] == 2


def test_wrapped_fn_preserves_result_contract():
    """Downstream code expects `(result, err)` regardless of cache
    state; the wrapper must not leak the `cached` bool to callers."""
    cache = ToolCache()
    fn, _ = _counter_fn()
    wrapped = wrap_tool_fns({"t": fn}, cache, cacheable={"t"})
    out = wrapped["t"]({"x": 7})
    assert isinstance(out, tuple) and len(out) == 2
    assert out == ("result:7", None)
