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


def test_wilson_interval_known_value():
    from analysis.stats import proportion_ci

    low, high = proportion_ci(81, 263)  # textbook example: about 0.255 to 0.366
    assert abs(low - 0.2553) < 1e-3 and abs(high - 0.3662) < 1e-3
    assert proportion_ci(0, 10)[0] == 0.0


def test_coverage_correction():
    from analysis.stats import coverage_corrected

    assert coverage_corrected(0.06, 0.06) == 0.0  # only misses: nothing private
    assert abs(coverage_corrected(0.53, 0.06) - 0.5) < 1e-12
    assert coverage_corrected(1.0, 0.06) == 1.0
