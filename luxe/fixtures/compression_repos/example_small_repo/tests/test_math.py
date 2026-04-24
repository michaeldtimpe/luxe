from mathops import product, sum_all


def test_sum_all_positive():
    assert sum_all([1, 2, 3]) == 6


def test_sum_all_with_negatives():
    assert sum_all([1, -2, 3]) == 2


def test_sum_all_all_negative():
    assert sum_all([-1, -2, -3]) == -6


def test_product_basic():
    assert product([2, 3, 4]) == 24
