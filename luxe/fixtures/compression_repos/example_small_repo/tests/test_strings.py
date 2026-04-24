from strings import slugify, title_case, truncate


def test_slugify_lowercases_and_dashes():
    assert slugify("Hello World") == "hello-world"


def test_slugify_trims_leading_trailing_whitespace():
    assert slugify("  hello world  ") == "hello-world"


def test_slugify_collapses_multiple_dashes():
    assert slugify("foo   bar") == "foo-bar"


def test_truncate_short_returns_original():
    assert truncate("hi", 5) == "hi"


def test_truncate_long_appends_suffix():
    assert truncate("abcdefgh", 5) == "abcd…"


def test_title_case_small_words_stay_lower():
    assert title_case("the quick brown fox") == "The Quick Brown Fox"
    assert title_case("a tale of two cities") == "A Tale of Two Cities"
