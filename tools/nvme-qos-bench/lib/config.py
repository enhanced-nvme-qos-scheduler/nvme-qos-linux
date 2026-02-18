# SPDX-License-Identifier: GPL-2.0
"""Configuration handling: YAML configs and user preferences."""

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

import yaml

# User preferences location
PREFS_DIR = Path.home() / ".config" / "nvme-qos-bench"
PREFS_FILE = PREFS_DIR / "config.yaml"


@dataclass
class BenchmarkConfig:
    """Benchmark run configuration."""
    # Test parameters
    runtime: int = 60              # seconds per test
    ramp_time: int = 5             # warmup seconds
    iterations: int = 5            # iterations per config
    iter_cooldown: int = 0         # seconds to sleep between iterations (SLC recovery)

    # Queue depths to test
    depths: List[int] = field(default_factory=lambda: [16, 32, 64])

    # QoS weights to test (if QoS available)
    weights: List[int] = field(default_factory=lambda: [9])

    # QoS policies to test
    policies: List[str] = field(default_factory=lambda: ["default"])

    # Workload mix
    high_prio_ratio: float = 0.2   # 20% high priority jobs
    high_numjobs: int = 1
    normal_numjobs: int = 4
    max_queues: Optional[int] = None  # Pin fio to N CPUs (= N HW queues)

    # Condition profile metadata
    condition_id: Optional[str] = None        # Which condition profile was used
    namespace_policy: Optional[str] = None    # Namespace QoS policy override
    workload_params: Optional[Dict[str, str]] = None  # I/O pattern overrides for templates

    # Tests to run
    run_baseline: bool = True
    run_qos: bool = True
    run_buffered: bool = False    # Page cache / writeback path tests
    run_cpu_overhead: bool = False
    run_isolation: bool = False    # Single-priority tests

    # Output
    output_json: bool = True
    output_csv: bool = True
    output_markdown: bool = True

    @classmethod
    def from_yaml(cls, path: Path) -> "BenchmarkConfig":
        """Load config from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def to_yaml(self, path: Path) -> None:
        """Save config to YAML file."""
        data = {k: getattr(self, k) for k in self.__dataclass_fields__}
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            yaml.dump(data, f, default_flow_style=False)


@dataclass
class UserPreferences:
    """Persistent user preferences."""
    device: Optional[str] = None           # Saved device selection
    confirmed_at: Optional[str] = None     # When destructive access was confirmed

    @classmethod
    def load(cls) -> "UserPreferences":
        """Load preferences from file, return defaults if not found."""
        if not PREFS_FILE.exists():
            return cls()
        try:
            with open(PREFS_FILE) as f:
                data = yaml.safe_load(f) or {}
            prefs = data.get("preferences", {})
            return cls(
                device=prefs.get("device"),
                confirmed_at=prefs.get("confirmed_at"),
            )
        except Exception:
            return cls()

    def save(self) -> None:
        """Save preferences to file."""
        PREFS_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "preferences": {
                "device": self.device,
                "confirmed_at": self.confirmed_at,
            }
        }
        with open(PREFS_FILE, 'w') as f:
            yaml.dump(data, f, default_flow_style=False)

    def clear_device(self) -> None:
        """Clear saved device preference."""
        self.device = None
        self.confirmed_at = None
        self.save()

    def set_device(self, device: str) -> None:
        """Set and save device preference."""
        self.device = device
        self.confirmed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.save()


def get_config_path(name: str) -> Path:
    """Get path to a named config file."""
    base = Path(__file__).parent.parent / "configs"
    path = base / f"{name}.yaml"
    if path.exists():
        return path
    # Try with .yaml extension added
    if not name.endswith(".yaml"):
        path = base / f"{name}.yaml"
    return path


def load_config(name_or_path: str) -> BenchmarkConfig:
    """Load config by name (quick/default/full) or path."""
    path = Path(name_or_path)
    if path.exists():
        return BenchmarkConfig.from_yaml(path)
    # Try as named config
    path = get_config_path(name_or_path)
    if path.exists():
        return BenchmarkConfig.from_yaml(path)
    raise FileNotFoundError(f"Config not found: {name_or_path}")


# Quick preset for fast sanity checks
QUICK_CONFIG = BenchmarkConfig(
    runtime=30,
    ramp_time=3,
    iterations=2,
    depths=[16, 32],
    weights=[9],
    run_baseline=True,
    run_qos=True,
    run_cpu_overhead=False,
    run_isolation=False,
)

# Default config for standard benchmarks
DEFAULT_CONFIG = BenchmarkConfig(
    runtime=60,
    ramp_time=5,
    iterations=5,
    depths=[16, 32, 64],
    weights=[9],
)

# Full sweep config
FULL_CONFIG = BenchmarkConfig(
    runtime=60,
    ramp_time=10,
    iterations=10,
    depths=[1, 4, 8, 16, 32, 64, 128],
    weights=[1, 4, 9, 19, 99],
    policies=["default", "force_high", "force_normal"],
    run_cpu_overhead=True,
    run_isolation=True,
)

# Stress config - high contention to exercise WRR scheduler
STRESS_CONFIG = BenchmarkConfig(
    runtime=30,
    ramp_time=5,
    iterations=3,
    depths=[32, 64],
    weights=[1, 4, 9, 19],
    high_numjobs=4,
    normal_numjobs=16,
    max_queues=2,
    run_baseline=True,
    run_qos=True,
)
