import pytest

from stats import mean, median, percentile, variance


def test_mean_basic():
    assert mean([1, 2, 3, 4]) == 2.5


def test_median_odd_length():
    assert median([3, 1, 2]) == 2


def test_median_even_length():
    assert median([1, 2, 3, 4]) == 2.5


def test_variance_basic():
    # Sample variance of [2, 4, 4, 4, 5, 5, 7, 9] is 4.571...
    assert abs(variance([2, 4, 4, 4, 5, 5, 7, 9]) - 4.571428571428571) < 1e-9


def test_percentile_p50_equals_median():
    assert percentile([10, 20, 30, 40, 50], 50) == 30


def test_percentile_empty_raises():
    with pytest.raises(ValueError):
        percentile([], 50)


def test_percentile_out_of_range_raises():
    with pytest.raises(ValueError):
        percentile([1, 2, 3], 101)
