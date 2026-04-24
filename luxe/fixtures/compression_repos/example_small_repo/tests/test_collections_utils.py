from collections_utils import chunk, dedupe_preserve_order, flatten, group_by


def test_dedupe_preserve_order_hashables():
    assert dedupe_preserve_order([1, 2, 1, 3, 2, 4]) == [1, 2, 3, 4]


def test_dedupe_preserve_order_strings():
    assert dedupe_preserve_order(["a", "b", "a", "c"]) == ["a", "b", "c"]


def test_dedupe_preserve_order_unhashable_dicts():
    items = [{"id": 1}, {"id": 2}, {"id": 1}, {"id": 3}]
    assert dedupe_preserve_order(items) == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_chunk_even_split():
    assert chunk([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


def test_chunk_uneven_split():
    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_flatten_basic():
    assert flatten([[1, 2], [3], [], [4, 5]]) == [1, 2, 3, 4, 5]


def test_group_by_basic():
    data = [("a", 1), ("b", 2), ("a", 3)]
    grouped = group_by(data, key=lambda x: x[0])
    assert grouped == {"a": [("a", 1), ("a", 3)], "b": [("b", 2)]}
