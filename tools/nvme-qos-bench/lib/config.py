# SPDX-License-Identifier: GPL-2.0
"""Configuration handling and user preferences."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

import yaml

# User preferences location
PREFS_DIR = Path.home() / ".config" / "nvme-qos-bench"
PREFS_FILE = PREFS_DIR / "config.yaml"


@dataclass
class BenchmarkConfig:
    # Test parameters
    runtime: int = 60              # seconds per test
    ramp_time: int = 5             # warmup seconds
    iterations: int = 5            # iterations per config
    iter_cooldown: int = 0         # seconds to sleep between iterations (SLC recovery)

    # Queue depths to test
    depths: List[int] = field(default_factory=lambda: [16, 32, 64])

    # QoS weights to test (if QoS available)
    weights: List[int] = field(default_factory=lambda: [9])

    # QoS max in-flight depth per queue (0 = full SQ depth, no limiting)
    qos_max_depth: int = 0

    # Workload mix
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

@dataclass
class UserPreferences:
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
