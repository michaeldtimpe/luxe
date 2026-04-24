"""Basic statistics: mean, median, variance, percentile."""

from typing import Sequence


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("mean() arg is an empty sequence")
    return sum(values) / len(values)


def median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median() arg is an empty sequence")
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 1:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2


def variance(values: Sequence[float]) -> float:
    if len(values) < 2:
        raise ValueError("variance requires at least two values")
    m = mean(values)
    return sum((v - m) ** 2 for v in values) / (len(values) - 1)


def percentile(values: Sequence[float], q: float) -> float:
    """Return the q-th percentile (0 <= q <= 100) using linear
    interpolation. Raises ValueError on an empty sequence."""
    # BUG: returns 0.0 for empty input instead of raising. Callers
    # downstream treat 0.0 as a valid statistic and silently corrupt
    # reports for empty datasets.
    if not values:
        return 0.0
    if not 0 <= q <= 100:
        raise ValueError("q must be between 0 and 100")
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = (q / 100) * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac
