# SPDX-License-Identifier: GPL-2.0
"""FIO job generation and execution."""

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, List, Optional

from jinja2 import Environment, FileSystemLoader

# Template directory
TEMPLATES_DIR = Path(__file__).parent.parent / "jobs"


@dataclass
class FioJobParams:
    """Parameters for FIO job generation."""
    device: str
    runtime: int = 60
    ramp_time: int = 5
    iodepth: int = 16
    numjobs: int = 1
    blocksize: str = "4k"
    rw: str = "randread"
    prioclass: int = 2       # IOPRIO_CLASS_BE
    prio: int = 4            # Middle priority
    offset: str = "0"
    size: str = "100%"
    cpus_allowed: Optional[str] = None  # Pin fio to specific CPUs (e.g., "0-1")

    # For mixed workloads
    high_iodepth: int = 16
    high_numjobs: int = 1
    normal_iodepth: int = 16
    normal_numjobs: int = 4

    # Mixed workload overrides (passed through to templates as Jinja2 variables)
    normal_bs: Optional[str] = None
    normal_rw: Optional[str] = None
    high_bs: Optional[str] = None
    high_rw: Optional[str] = None
    normal_prio: Optional[int] = None
    normal_prioclass: Optional[int] = None
    high_prio: Optional[int] = None
    high_prioclass: Optional[int] = None


def render_template(template_name: str, params: Dict[str, Any]) -> str:
    """Render a Jinja2 template with given parameters."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_name)
    return template.render(**params)


def generate_job_file(template_name: str, params: FioJobParams, output_path: Path) -> None:
    """Generate a FIO job file from template."""
    # Filter out None values so Jinja2 template defaults (| default()) work correctly
    content = render_template(template_name,
                              {k: v for k, v in params.__dict__.items() if v is not None})
    with open(output_path, 'w') as f:
        f.write(content)


def run_fio(job_file: Path, output_json: Path,
            timeout: Optional[int] = None) -> tuple[bool, Optional[Dict[str, Any]]]:
    """Run fio with a job file and return parsed JSON output.

    Returns (success, json_data) tuple.
    """
    cmd = [
        "fio",
        str(job_file),
        f"--output={output_json}",
        "--output-format=json+",
    ]

    try:
        result = subprocess.run(
            cmd,
            timeout=timeout,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return False, None

        # Parse JSON output
        with open(output_json) as f:
            data = json.load(f)
        return True, data

    except subprocess.TimeoutExpired:
        return False, None
    except (OSError, json.JSONDecodeError) as e:
        return False, None


class FioRunner:
    """High-level interface for running FIO benchmarks."""

    def __init__(self, device: str, output_dir: Path):
        self.device = device
        self.output_dir = output_dir
        self.raw_dir = output_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def _run_job(self, name: str, template: str, params: Dict[str, Any],
                 iteration: int = 0) -> tuple[bool, Optional[Dict[str, Any]]]:
        """Run a single FIO job."""
        # Create job file
        job_file = self.raw_dir / f"{name}_iter{iteration}.fio"
        json_file = self.raw_dir / f"{name}_iter{iteration}.json"

        job_params = FioJobParams(device=self.device, **params)
        generate_job_file(template, job_params, job_file)

        # Calculate timeout (2x runtime + ramp + buffer)
        timeout = (params.get('runtime', 60) + params.get('ramp_time', 5)) * 2 + 60

        return run_fio(job_file, json_file, timeout)

    def run_high_priority(self, iodepth: int = 16, numjobs: int = 1,
                          runtime: int = 60, ramp_time: int = 5,
                          iteration: int = 0) -> tuple[bool, Optional[Dict]]:
        """Run high-priority (latency-sensitive) workload: 4K random reads."""
        return self._run_job(
            name=f"high_prio_qd{iodepth}",
            template="high_priority.fio.j2",
            params={
                "iodepth": iodepth,
                "numjobs": numjobs,
                "runtime": runtime,
                "ramp_time": ramp_time,
            },
            iteration=iteration,
        )

    def run_normal_priority(self, iodepth: int = 16, numjobs: int = 4,
                            runtime: int = 60, ramp_time: int = 5,
                            iteration: int = 0) -> tuple[bool, Optional[Dict]]:
        """Run normal-priority (bulk) workload: 1M sequential writes."""
        return self._run_job(
            name=f"normal_prio_qd{iodepth}",
            template="normal_priority.fio.j2",
            params={
                "iodepth": iodepth,
                "numjobs": numjobs,
                "runtime": runtime,
                "ramp_time": ramp_time,
            },
            iteration=iteration,
        )

    def run_mixed_workload(self, high_iodepth: int = 16, high_numjobs: int = 1,
                           normal_iodepth: int = 16, normal_numjobs: int = 4,
                           runtime: int = 60, ramp_time: int = 5,
                           iteration: int = 0,
                           label: str = "",
                           cpus_allowed: Optional[str] = None,
                           workload_params: Optional[Dict[str, str]] = None) -> tuple[bool, Optional[Dict]]:
        """Run mixed workload: concurrent high-prio reads + normal-prio writes."""
        name = f"mixed_qd{high_iodepth}_{label}" if label else f"mixed_qd{high_iodepth}"
        params = {
            "high_iodepth": high_iodepth,
            "high_numjobs": high_numjobs,
            "normal_iodepth": normal_iodepth,
            "normal_numjobs": normal_numjobs,
            "runtime": runtime,
            "ramp_time": ramp_time,
        }
        if cpus_allowed:
            params["cpus_allowed"] = cpus_allowed
        if workload_params:
            params.update(workload_params)
        return self._run_job(
            name=name,
            template="mixed_workload.fio.j2",
            params=params,
            iteration=iteration,
        )

    def run_buffered_workload(self, high_iodepth: int = 16, high_numjobs: int = 1,
                              normal_iodepth: int = 16, normal_numjobs: int = 4,
                              runtime: int = 60, ramp_time: int = 5,
                              iteration: int = 0,
                              label: str = "",
                              cpus_allowed: Optional[str] = None,
                              workload_params: Optional[Dict[str, str]] = None) -> tuple[bool, Optional[Dict]]:
        """Run buffered workload: page cache path with concurrent high/normal prio."""
        name = f"buffered_qd{high_iodepth}_{label}" if label else f"buffered_qd{high_iodepth}"
        params = {
            "high_iodepth": high_iodepth,
            "high_numjobs": high_numjobs,
            "normal_iodepth": normal_iodepth,
            "normal_numjobs": normal_numjobs,
            "runtime": runtime,
            "ramp_time": ramp_time,
        }
        if cpus_allowed:
            params["cpus_allowed"] = cpus_allowed
        if workload_params:
            params.update(workload_params)
        return self._run_job(
            name=name,
            template="mmap_workload.fio.j2",
            params=params,
            iteration=iteration,
        )

    def run_cpu_overhead(self, runtime: int = 60, ramp_time: int = 5,
                         iteration: int = 0) -> tuple[bool, Optional[Dict]]:
        """Run CPU overhead test: low-depth single job."""
        return self._run_job(
            name="cpu_overhead",
            template="cpu_overhead.fio.j2",
            params={
                "iodepth": 1,
                "numjobs": 1,
                "runtime": runtime,
                "ramp_time": ramp_time,
            },
            iteration=iteration,
        )
