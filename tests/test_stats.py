import numpy as np

from analysis.stats import bootstrap_ci, summarise


def test_summary_of_known_data():
    s = summarise([1, 2, 3, 4, 100], n_resamples=2_000)
    assert s.n == 5 and s.median == 3 and s.mean == 22
    assert s.median_ci[0] <= s.median <= s.median_ci[1]
    assert s.mean_ci[0] <= s.mean <= s.mean_ci[1]


def test_bootstrap_is_seeded():
    data = np.random.default_rng(1).lognormal(size=200)
    assert bootstrap_ci(data, np.median, seed=7, n_resamples=1_000) == bootstrap_ci(
        data, np.median, seed=7, n_resamples=1_000
    )


def test_ci_covers_true_mean_of_normal_sample():
    data = np.random.default_rng(2).normal(loc=10, scale=1, size=500)
    low, high = bootstrap_ci(data, np.mean, n_resamples=2_000)
    assert low < 10 < high and high - low < 0.5


def test_empty_input():
    assert summarise([]).n == 0
