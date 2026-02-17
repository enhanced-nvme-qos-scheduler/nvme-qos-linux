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
    max_queues_cap: Optional[int] = None     # Upper bound on active queues
    high_jobs_per_queue: float = 2.0
    normal_jobs_per_queue: float = 8.0
    min_high: int = 1
    min_normal: int = 1

    # Test parameters
    depths: List[int] = field(default_factory=lambda: [32, 64])
    weights: List[int] = field(default_factory=lambda: [9])
    iterations: int = 5
    runtime: int = 60

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
        effective_depth = 1 if self.run_buffered else depth
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
            if self.max_queues_cap:
                active = min(active, self.max_queues_cap)
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
            high_numjobs=high_numjobs,
            normal_numjobs=normal_numjobs,
            max_queues=max_queues,
            run_baseline=True,
            run_qos=True,
            run_buffered=self.run_buffered,
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

# B: Low density, all queues
_register(ConditionProfile(
    id="B",
    name="Low density, all queues",
    description="Sparse jobs spread across all HW queues -- minimal per-queue contention",
    mechanism="Classification",
    queue_fraction=None,  # all queues
    high_jobs_per_queue=0.0625,
    normal_jobs_per_queue=0.25,
    min_high=1,
    min_normal=1,
    depths=[32, 64],
    pass_criteria="QoS should have negligible effect; WRR mostly idle",
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
    pass_criteria="Significant p99 improvement; WRR actively arbitrating",
))

# E: SQ-full stress
_register(ConditionProfile(
    id="E",
    name="SQ-full stress",
    description="QD128 I/O-neutral on quarter-queues -- triggers SQ throttling and kick path without write pressure",
    mechanism="SQ throttle + kick",
    queue_fraction=0.25,
    high_jobs_per_queue=2.0,
    normal_jobs_per_queue=8.0,
    depths=[128],
    workload_params={"normal_rw": "randread", "normal_bs": "4k"},
    pass_criteria="SQ throttle events; kick path active; no starvation",
))

# F: Minority high-prio
_register(ConditionProfile(
    id="F",
    name="Minority high-prio",
    description="Very few high-prio jobs vs many normal -- tests starvation guard",
    mechanism="WRR starvation guard",
    queue_fraction=0.5,
    high_jobs_per_queue=0.25,
    normal_jobs_per_queue=4.0,
    min_high=1,
    depths=[32, 64],
    workload_params={"normal_bs": "256k"},
    pass_criteria="High-prio gets service despite being minority; no normal starvation",
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
    depths=[32],
    pass_criteria="Normal still gets service; credit refills visible",
))

# H: Symmetric I/O (both classes do randread)
_register(ConditionProfile(
    id="H",
    name="Symmetric I/O",
    description="Both classes do 4K randread -- isolates scheduling from I/O pattern effects",
    mechanism="Classification (I/O-neutral)",
    queue_fraction=0.5,
    high_jobs_per_queue=2.0,
    normal_jobs_per_queue=8.0,
    depths=[32, 64],
    workload_params={
        "normal_rw": "randread",
        "normal_bs": "4k",
    },
    pass_criteria="p99 difference attributable to scheduling, not I/O pattern",
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
    weights=[1, 4, 9, 19, 99],
    workload_params={"normal_bs": "256k"},
    pass_criteria="Dispatch ratio tracks weight proportionally",
))

# J: Buffered I/O path
_register(ConditionProfile(
    id="J",
    name="Buffered I/O",
    description="Page-cache writeback path -- tests QoS with coalesced submissions",
    mechanism="Writeback / kick path",
    queue_fraction=0.5,
    high_jobs_per_queue=2.0,
    normal_jobs_per_queue=8.0,
    depths=[32, 64],
    run_buffered=True,
    workload_params={"normal_bs": "256k"},
    pass_criteria="QoS effective on buffered path; no regression vs direct I/O",
))

# K: Single-priority (namespace force_high)
_register(ConditionProfile(
    id="K",
    name="Single-priority (force_high)",
    description="All traffic forced high via namespace policy -- single-class scheduling",
    mechanism="Namespace policy override",
    queue_fraction=0.5,
    high_jobs_per_queue=2.0,
    normal_jobs_per_queue=8.0,
    depths=[32, 64],
    namespace_policy="force_high",
    workload_params={"normal_bs": "256k"},
    pass_criteria="All enqueues go to high queue; no normal starvation warnings",
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
