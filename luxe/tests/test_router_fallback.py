"""Tests for the router's fallback-agent heuristic.

When the small router model emits no tool call at all, we pick a
sensible default based on keywords in the prompt — mentions of files /
docs / folders route to `writing` (it has the fs surface), everything
else falls back to `general`.
"""

from cli.router import _fallback_agent

ENABLED = ["general", "research", "writing", "image", "code"]


def test_file_like_prompts_route_to_writing():
    for prompt in (
        "review the documents in this folder",
        "are you able to read files here?",
        "summarize these drafts",
        "what is in my notes.md",
        "edit the README",
        "check the folder for anything interesting",
    ):
        assert _fallback_agent(prompt, ENABLED) == "writing", prompt


def test_non_file_prompts_route_to_general():
    for prompt in (
        "tell me a joke",
        "what is list comprehension",
        "latest news on GPT",
        "how do transformers work",
    ):
        assert _fallback_agent(prompt, ENABLED) == "general", prompt


def test_missing_writing_falls_back_to_general():
    # If writing is disabled, file-like prompts still land somewhere
    # reasonable rather than raising.
    enabled = ["general", "research", "code"]
    assert _fallback_agent("review the docs", enabled) == "general"


def test_empty_enabled_picks_first():
    # Edge case: only one agent available — just return it.
    assert _fallback_agent("anything", ["research"]) == "research"
