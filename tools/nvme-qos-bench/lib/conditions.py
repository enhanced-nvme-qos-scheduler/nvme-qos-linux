# SPDX-License-Identifier: GPL-2.0
"""Load condition profiles for reproducible benchmark configurations.

Each profile defines a load condition (A-K) that exercises a specific
scheduler mechanism.  Profiles auto-scale to the hardware queue count
so `--condition D` produces the right job density on any machine.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import BenchmarkConfig

# Template defaults — single source of truth matching mixed_workload.fio.j2
DEFAULT_NORMAL_BS = "1m"
DEFAULT_NORMAL_RW = "write"


def _parse_fio_size(s: str) -> int:
    """Parse fio size string (e.g. '4k', '256k', '1m') to bytes."""
    s = s.lower().strip()
    multipliers = {'k': 1024, 'm': 1024**2, 'g': 1024**3}
    if s[-1] in multipliers:
        return int(float(s[:-1]) * multipliers[s[-1]])
    return int(s)


@dataclass
class ConditionProfile:
    """A named load condition with auto-scaling rules."""
    id: str
    name: str
    description: str
    mechanism: str

    # Scaling rules
    queue_fraction: Optional[float] = None   # Fraction of HW queues to use
    min_queues: int = 1
    high_jobs_per_queue: float = 2.0
    normal_jobs_per_queue: float = 8.0
    min_high: int = 1
    min_normal: int = 1

    # Test parameters
    depths: List[int] = field(default_factory=lambda: [32, 64])
    weights: List[int] = field(default_factory=lambda: [9])
    max_depth: int = 0   # 0 = full SQ depth; 16 = recommended for scheduling conditions
    iterations: int = 5
    runtime: int = 60
    iter_cooldown: int = 0         # seconds to sleep between iterations

    # Special flags
    use_baseline_path: bool = False          # Delegate to cmd_run_baseline
    namespace_policy: Optional[str] = None   # e.g. "force_high"
    workload_params: Optional[Dict[str, str]] = None  # Template overrides
    run_buffered: bool = False

    # Documentation
    pass_criteria: str = ""

    def per_queue_write_bytes(self, depth: int) -> int:
        """Compute per-queue outstanding write bytes for a given iodepth.

        Returns 0 if the normal workload is read-only.
        For buffered I/O (psync), iodepth is effectively 1.
        """
        wp = self.workload_params or {}
        normal_rw = wp.get("normal_rw", DEFAULT_NORMAL_RW)
        # Read-only normal workload -> no write pressure
        if "read" in normal_rw and "write" not in normal_rw:
            return 0
        normal_bs = _parse_fio_size(wp.get("normal_bs", DEFAULT_NORMAL_BS))
        effective_depth = 1 if self.run_buffered else (
            min(depth, self.max_depth) if self.max_depth else depth
        )
        return int(self.normal_jobs_per_queue * effective_depth * normal_bs)

    def resolve(self, hw_queues: int) -> BenchmarkConfig:
        """Compute concrete BenchmarkConfig from scaling rules.

        For condition A (use_baseline_path), returns a minimal config
        since the baseline path has its own parameters.
        """
        if self.use_baseline_path:
            return BenchmarkConfig(
                depths=list(self.depths),
                weights=list(self.weights),
                iterations=self.iterations,
                runtime=self.runtime,
                condition_id=self.id,
            )

        # Compute active queues
        if self.queue_fraction is not None:
            raw = hw_queues * self.queue_fraction
            active = max(self.min_queues, round(raw))
        else:
            active = hw_queues

        active = max(active, 1)

        # Compute job counts
        high_numjobs = max(self.min_high, round(self.high_jobs_per_queue * active))
        normal_numjobs = max(self.min_normal, round(self.normal_jobs_per_queue * active))

        # max_queues pins fio to N CPUs -- only set when we subset queues
        max_queues = active if self.queue_fraction is not None else None

        return BenchmarkConfig(
            runtime=self.runtime,
            iterations=self.iterations,
            depths=list(self.depths),
            weights=list(self.weights),
            qos_max_depth=self.max_depth,
            high_numjobs=high_numjobs,
            normal_numjobs=normal_numjobs,
            max_queues=max_queues,
            run_baseline=True,
            run_qos=True,
            run_buffered=self.run_buffered,
            iter_cooldown=self.iter_cooldown,
            condition_id=self.id,
            namespace_policy=self.namespace_policy,
            workload_params=dict(self.workload_params) if self.workload_params else None,
        )


# ---------------------------------------------------------------------------
# Condition registry
# ---------------------------------------------------------------------------

CONDITIONS: Dict[str, ConditionProfile] = {}


def _register(profile: ConditionProfile) -> ConditionProfile:
    CONDITIONS[profile.id] = profile
    return profile


# A: Zero-overhead baseline
_register(ConditionProfile(
    id="A",
    name="Zero-overhead baseline",
    description="QD1+QD4 single-job, QoS off vs on -- confirms scheduler adds no overhead",
    mechanism="Bypass path",
    depths=[1, 4],
    iterations=5,
    runtime=10,
    use_baseline_path=True,
    pass_criteria="p99 regression < 2% at all depths",
))

# C: Few queues, high density
_register(ConditionProfile(
    id="C",
    name="Few queues, high density",
    description="Pack many jobs onto 2 queues -- maximum per-queue contention",
    mechanism="WRR arbitration",
    queue_fraction=0.125,
    min_queues=2,
    high_jobs_per_queue=2.0,
    normal_jobs_per_queue=8.0,
    depths=[32, 64],
    max_depth=16,
    workload_params={"normal_bs": "256k"},
    pass_criteria="WRR engagement; fairness OK; no high-prio regression",
))

# D: Device-saturated, high contention (target operating condition)
_register(ConditionProfile(
    id="D",
    name="Device-sat, high contention",
    description="Half-queue saturation with 2:8 job ratio -- target operating condition",
    mechanism="Classification + WRR",
    queue_fraction=0.5,
    high_jobs_per_queue=2.0,
    normal_jobs_per_queue=8.0,
    depths=[32, 64],
    max_depth=16,
    workload_params={"normal_bs": "256k"},
    pass_criteria="Significant p99 improvement; WRR actively arbitrating",
))

# G: Majority high-prio
_register(ConditionProfile(
    id="G",
    name="Majority high-prio",
    description="Many high-prio jobs vs few normal -- tests credit exhaustion",
    mechanism="WRR credit exhaustion",
    queue_fraction=0.5,
    high_jobs_per_queue=4.0,
    normal_jobs_per_queue=0.5,
    min_normal=1,
    depths=[32, 64],
    max_depth=16,
    workload_params={"normal_rw": "randread", "normal_bs": "4k"},
    pass_criteria="Normal still gets service; credit refills visible",
))

# I: Weight sweep
_register(ConditionProfile(
    id="I",
    name="Weight sweep",
    description="Sweep QoS weights 1-99 -- validates proportional fairness",
    mechanism="WRR fairness",
    queue_fraction=0.5,
    high_jobs_per_queue=2.0,
    normal_jobs_per_queue=8.0,
    depths=[32, 64],
    max_depth=16,
    weights=[1, 4, 9, 19, 99],
    workload_params={"normal_bs": "256k"},
    pass_criteria="Dispatch ratio tracks weight proportionally",
))


def get_condition(condition_id: str) -> ConditionProfile:
    """Look up a condition profile by ID (case-insensitive).

    Raises KeyError if not found.
    """
    key = condition_id.upper()
    if key not in CONDITIONS:
        valid = ", ".join(sorted(CONDITIONS.keys()))
        raise KeyError(f"Unknown condition '{condition_id}'. Valid conditions: {valid}")
    return CONDITIONS[key]


def list_conditions() -> List[ConditionProfile]:
    """Return all condition profiles in alphabetical order."""
    return [CONDITIONS[k] for k in sorted(CONDITIONS.keys())]
