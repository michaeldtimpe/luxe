"""Tests for the git URL helpers used by /review and /refactor."""

from cli.git import normalize_url, repo_name_from_url, urls_match


def test_normalize_https():
    assert (
        normalize_url("https://github.com/foo/bar")
        == "https://github.com/foo/bar"
    )


def test_normalize_strips_dot_git():
    assert (
        normalize_url("https://github.com/foo/bar.git")
        == "https://github.com/foo/bar"
    )


def test_normalize_strips_trailing_slash():
    assert (
        normalize_url("https://github.com/foo/bar/")
        == "https://github.com/foo/bar"
    )


def test_normalize_ssh_to_https():
    assert (
        normalize_url("git@github.com:foo/bar.git")
        == "https://github.com/foo/bar"
    )


def test_normalize_is_case_insensitive():
    assert (
        normalize_url("HTTPS://GitHub.com/Foo/Bar")
        == "https://github.com/foo/bar"
    )


def test_urls_match_true_across_forms():
    assert urls_match(
        "https://github.com/foo/bar",
        "git@github.com:foo/bar.git",
    )


def test_urls_match_false_different_repo():
    assert not urls_match(
        "https://github.com/foo/bar",
        "https://github.com/foo/baz",
    )


def test_urls_match_handles_none():
    assert not urls_match(None, "https://github.com/foo/bar")
    assert not urls_match("https://github.com/foo/bar", None)


def test_repo_name_from_url():
    assert repo_name_from_url("https://github.com/foo/bar") == "bar"
    assert repo_name_from_url("https://github.com/foo/bar.git") == "bar"
    assert repo_name_from_url("https://github.com/foo/bar/") == "bar"
    assert repo_name_from_url("git@github.com:foo/bar.git") == "bar"
