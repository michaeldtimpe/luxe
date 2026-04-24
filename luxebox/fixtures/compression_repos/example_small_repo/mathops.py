"""Small numeric helpers used by tests."""


def sum_all(nums):
    total = 0
    for n in nums:
        if n > 0:
            total += n
    return total


def product(nums):
    out = 1
    for n in nums:
        out *= n
    return out
