# SPDX-License-Identifier: GPL-2.0
"""Centralized constants for nvme-qos-bench.

All magic numbers, thresholds, and configuration defaults.
"""

# ============================================================================
# Test iteration defaults
# ============================================================================
BASELINE_ITERATIONS = 5
BASELINE_RUNTIME_SEC = 10
BASELINE_RAMP_SEC = 2
DRIFT_PROBE_RUNTIME_SEC = 5
DRIFT_PROBE_RAMP_SEC = 1
DRIFT_PROBE_ITERATION = 999  # Sentinel value for drift probes

# ============================================================================
# Verification test defaults
# ============================================================================
VERIFY_IODEPTH = 16
VERIFY_RUNTIME_SEC = 10
VERIFY_RAMP_SEC = 3
VERIFY_ITERATION = 0

# Verification test job counts
VERIFY_HIGH_NUMJOBS = 1
VERIFY_NORMAL_NUMJOBS_SINGLE = 1
VERIFY_NORMAL_NUMJOBS_MULTI = 4

# ============================================================================
# Thresholds
# ============================================================================
# Baseline pass criteria
BASELINE_PASS_THRESHOLD_PCT = 2.0  # p99 regression must be < 2%

# Priority classification thresholds
FORCE_HIGH_THRESHOLD_PCT = 95.0    # >= 95% high enqueues = PASS
FORCE_NORMAL_THRESHOLD_PCT = 95.0  # >= 95% normal enqueues = PASS

# WRR fairness thresholds (kernel_stats.py)
WRR_TOLERANCE_HIGH_DEMAND_PCT = 20.0  # High-demand scenarios
WRR_TOLERANCE_BALANCED_PCT = 15.0     # Balanced workloads
WRR_FAIR_THRESHOLD_PCT = 40.0         # <= 40% dispatch -> consider work-conserving

# Statistical analysis
MIN_SAMPLES_FOR_TTEST = 5         # Minimum samples for reliable t-test
TTEST_SIGNIFICANCE_LEVEL = 0.05   # Alpha = 0.05
IQR_OUTLIER_MULTIPLIER = 1.5      # Standard IQR outlier detection

# ============================================================================
# Display and formatting
# ============================================================================
# Number formatting thresholds
FORMAT_K_THRESHOLD = 1000
FORMAT_K_PRECISION_THRESHOLD = 10_000

# Terminal output
TERMINAL_PAD_WIDTH = 80
PROGRESS_BAR_WIDTH = 20

# Percentage change color coding (for latency metrics)
PCT_CHANGE_EXCELLENT_THRESHOLD = -30  # <= -30% -> green
PCT_CHANGE_GOOD_THRESHOLD = -10       # <= -10% -> yellow
PCT_CHANGE_NEUTRAL_THRESHOLD = 0      # < 0 -> white
PCT_CHANGE_WARNING_THRESHOLD = 5      # < 5 -> yellow
                                       # >= 5 -> red

# Time formatting
US_TO_MS_THRESHOLD = 1000
US_TO_SEC_THRESHOLD = 1000000

# SI formatting
SI_THRESHOLD = 1000

# ============================================================================
# FIO defaults
# ============================================================================
# FIO timeout calculation: timeout = (runtime + ramp_time) * multiplier + buffer
FIO_TIMEOUT_MULTIPLIER = 2
FIO_TIMEOUT_BUFFER_SEC = 60
FIO_STDERR_LINES_ON_ERROR = 10  # Number of stderr lines to show on fio failure

# Default priority settings
IOPRIO_CLASS_BE = 2
IOPRIO_DEFAULT = 4

# Default workload parameters
DEFAULT_IODEPTH = 16
DEFAULT_NUMJOBS = 1
DEFAULT_RUNTIME_SEC = 60
DEFAULT_RAMP_TIME_SEC = 5

# Mixed workload defaults
DEFAULT_HIGH_IODEPTH = 16
DEFAULT_HIGH_NUMJOBS = 1
DEFAULT_NORMAL_IODEPTH = 16
DEFAULT_NORMAL_NUMJOBS = 4

# ============================================================================
# Condition profile defaults
# ============================================================================
# Scaling rules
DEFAULT_HIGH_JOBS_PER_QUEUE = 2.0
DEFAULT_NORMAL_JOBS_PER_QUEUE = 8.0
DEFAULT_MIN_QUEUES = 1
DEFAULT_MIN_HIGH_JOBS = 1
DEFAULT_MIN_NORMAL_JOBS = 1

# Test parameters
DEFAULT_CONDITION_DEPTHS = [32, 64]
DEFAULT_CONDITION_WEIGHTS = [9]
DEFAULT_CONDITION_MAX_DEPTH = 0  # 0 = full SQ depth
DEFAULT_CONDITION_ITERATIONS = 5
DEFAULT_CONDITION_RUNTIME_SEC = 60
DEFAULT_CONDITION_COOLDOWN_SEC = 0

# ============================================================================
# Block size constants (in bytes)
# ============================================================================
BYTES_PER_KB = 1024
BYTES_PER_MB = 1024 ** 2
BYTES_PER_GB = 1024 ** 3

# ============================================================================
# JSON output
# ============================================================================
JSON_INDENT = 2

# ============================================================================
# OS / System
# ============================================================================
ROOT_UID = 0

# ============================================================================
# ANSI color codes
# ============================================================================
ANSI_COLOR_CODE_OFFSET = 30
ANSI_BOLD = 1
