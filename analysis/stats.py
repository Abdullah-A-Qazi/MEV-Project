"""Summary statistics used for every headline number: n, median, mean and bootstrap CIs.

MEV values are heavy-tailed, so we always report the median next to the mean, and a
percentile bootstrap confidence interval instead of a normal-theory one.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Summary:
    n: int
    median: float
    mean: float
    median_ci: tuple[float, float]
    mean_ci: tuple[float, float]


def bootstrap_ci(
    values: Sequence[float],
    statistic: Callable[[np.ndarray], float],
    *,
    n_resamples: int = 10_000,
    confidence: float = 0.95,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile bootstrap confidence interval for `statistic` of `values`."""
    data = np.asarray(values, dtype=float)
    if data.size == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    samples = rng.choice(data, size=(n_resamples, data.size), replace=True)
    stats = np.apply_along_axis(statistic, 1, samples)
    alpha = (1 - confidence) / 2
    low, high = np.quantile(stats, [alpha, 1 - alpha])
    return (float(low), float(high))


def summarise(values: Sequence[float], seed: int = 0, n_resamples: int = 10_000) -> Summary:
    data = np.asarray(values, dtype=float)
    if data.size == 0:
        nan = float("nan")
        return Summary(0, nan, nan, (nan, nan), (nan, nan))
    return Summary(
        n=int(data.size),
        median=float(np.median(data)),
        mean=float(np.mean(data)),
        median_ci=bootstrap_ci(data, np.median, seed=seed, n_resamples=n_resamples),
        mean_ci=bootstrap_ci(data, np.mean, seed=seed, n_resamples=n_resamples),
    )
