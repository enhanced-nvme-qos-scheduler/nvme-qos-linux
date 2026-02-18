# SPDX-License-Identifier: GPL-2.0
"""Statistical analysis: mean, stddev, confidence intervals, significance tests."""

import math
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

# t-distribution critical values for 95% CI (two-tailed)
# Index by degrees of freedom (n-1)
T_CRITICAL_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    15: 2.131, 20: 2.086, 25: 2.060, 30: 2.042, 40: 2.021,
    50: 2.009, 60: 2.000, 100: 1.984, 1000: 1.962,
}


def _get_t_critical(df: int) -> float:
    """Get t critical value for given degrees of freedom."""
    if df <= 0:
        return float('inf')
    if df in T_CRITICAL_95:
        return T_CRITICAL_95[df]
    # Find closest
    keys = sorted(T_CRITICAL_95.keys())
    for i, k in enumerate(keys):
        if k > df:
            if i == 0:
                return T_CRITICAL_95[keys[0]]
            # Linear interpolation
            k0 = keys[i-1]
            t0 = T_CRITICAL_95[k0]
            t1 = T_CRITICAL_95[k]
            return t0 + (t1 - t0) * (df - k0) / (k - k0)
    return T_CRITICAL_95[keys[-1]]


@dataclass
class Statistics:
    """Statistical summary of a dataset."""
    n: int
    mean: float
    stddev: float
    stderr: float
    ci_low: float      # 95% CI lower bound
    ci_high: float     # 95% CI upper bound
    min_val: float
    max_val: float
    median: float


def calculate_stats(values: List[float]) -> Statistics:
    """Calculate comprehensive statistics for a list of values."""
    n = len(values)
    if n == 0:
        return Statistics(0, 0, 0, 0, 0, 0, 0, 0, 0)

    mean = sum(values) / n

    if n == 1:
        return Statistics(
            n=1, mean=mean, stddev=0, stderr=0,
            ci_low=mean, ci_high=mean,
            min_val=mean, max_val=mean, median=mean
        )

    # Sample standard deviation
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    stddev = math.sqrt(variance)

    # Standard error
    stderr = stddev / math.sqrt(n)

    # 95% confidence interval using t-distribution
    t_crit = _get_t_critical(n - 1)
    margin = t_crit * stderr
    ci_low = mean - margin
    ci_high = mean + margin

    # Min, max, median
    sorted_vals = sorted(values)
    min_val = sorted_vals[0]
    max_val = sorted_vals[-1]
    if n % 2 == 0:
        median = (sorted_vals[n//2 - 1] + sorted_vals[n//2]) / 2
    else:
        median = sorted_vals[n//2]

    return Statistics(
        n=n, mean=mean, stddev=stddev, stderr=stderr,
        ci_low=ci_low, ci_high=ci_high,
        min_val=min_val, max_val=max_val, median=median
    )


@dataclass
class TTestResult:
    """Result of two-sample t-test."""
    t_statistic: float
    p_value: float
    significant: bool  # At alpha=0.05
    effect_size: float  # Cohen's d


def two_sample_ttest(sample1: List[float], sample2: List[float]) -> TTestResult:
    """Perform two-sample t-test (Welch's t-test for unequal variances)."""
    n1, n2 = len(sample1), len(sample2)
    if n1 < 2 or n2 < 2:
        return TTestResult(0, 1.0, False, 0)

    mean1 = sum(sample1) / n1
    mean2 = sum(sample2) / n2

    var1 = sum((x - mean1) ** 2 for x in sample1) / (n1 - 1)
    var2 = sum((x - mean2) ** 2 for x in sample2) / (n2 - 1)

    # Welch's t-statistic
    se = math.sqrt(var1/n1 + var2/n2)
    if se == 0:
        return TTestResult(0, 1.0, False, 0)

    t_stat = (mean1 - mean2) / se

    # Welch-Satterthwaite degrees of freedom
    num = (var1/n1 + var2/n2) ** 2
    denom = (var1/n1)**2/(n1-1) + (var2/n2)**2/(n2-1)
    df = num / denom if denom > 0 else 1

    # Approximate p-value using t-distribution
    # Using simple approximation: |t| > t_critical means p < 0.05
    t_crit = _get_t_critical(int(df))
    significant = abs(t_stat) > t_crit

    # Rough p-value estimate (not precise, but sufficient for significance check)
    p_value = 0.05 if not significant else 0.01  # Simplified

    # Cohen's d effect size
    pooled_std = math.sqrt(((n1-1)*var1 + (n2-1)*var2) / (n1+n2-2))
    effect_size = (mean1 - mean2) / pooled_std if pooled_std > 0 else 0

    return TTestResult(t_stat, p_value, significant, effect_size)


def detect_outliers_iqr(values: List[float], k: float = 1.5) -> Tuple[List[float], List[int]]:
    """Detect outliers using IQR method.

    Returns (outlier_values, outlier_indices).
    """
    if len(values) < 4:
        return [], []

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    q1_idx = n // 4
    q3_idx = 3 * n // 4

    q1 = sorted_vals[q1_idx]
    q3 = sorted_vals[q3_idx]
    iqr = q3 - q1

    lower_bound = q1 - k * iqr
    upper_bound = q3 + k * iqr

    outliers = []
    indices = []
    for i, v in enumerate(values):
        if v < lower_bound or v > upper_bound:
            outliers.append(v)
            indices.append(i)

    return outliers, indices


def detect_degraded_iterations(iterations: List[Dict]) -> List[int]:
    """Detect iterations degraded by SLC cache exhaustion / FTL stalls.

    Heuristic: flag iteration if:
      - io_write_ios < 0.7 * median(io_write_ios), OR
      - iops < 0.7 * median(iops) AND io_util_pct > 90%

    Returns list of degraded iteration indices.
    """
    if len(iterations) < 3:
        return []

    write_ios = [it.get("io_write_ios", 0) for it in iterations]
    iops_vals = [it.get("iops", 0) for it in iterations]
    util_vals = [it.get("io_util_pct", 0) for it in iterations]

    med_writes = sorted(write_ios)[len(write_ios) // 2]
    med_iops = sorted(iops_vals)[len(iops_vals) // 2]

    degraded = []
    for i in range(len(iterations)):
        if med_writes > 0 and write_ios[i] < 0.7 * med_writes:
            degraded.append(i)
        elif med_iops > 0 and iops_vals[i] < 0.7 * med_iops and util_vals[i] > 90:
            degraded.append(i)

    return degraded


def percentage_change(baseline: float, new_value: float) -> float:
    """Calculate percentage change from baseline to new value."""
    if baseline == 0:
        return 0 if new_value == 0 else float('inf')
    return ((new_value - baseline) / baseline) * 100


def compare_results(baseline_values: List[float], test_values: List[float]) -> dict:
    """Compare two sets of results with full statistical analysis."""
    baseline_stats = calculate_stats(baseline_values)
    test_stats = calculate_stats(test_values)

    pct_change = percentage_change(baseline_stats.mean, test_stats.mean)
    ttest = two_sample_ttest(baseline_values, test_values)

    return {
        "baseline": baseline_stats,
        "test": test_stats,
        "pct_change": pct_change,
        "ttest": ttest,
        "improvement": pct_change < 0,  # For latency, negative is better
    }
