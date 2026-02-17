# SPDX-License-Identifier: GPL-2.0
"""Scan, index, and pool benchmark result directories."""

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .progress import colored


@dataclass
class RunSummary:
    """Summary of a single benchmark run directory."""
    path: Path
    timestamp: str          # "2026-02-14 20:16" parsed from dir name
    commit: Optional[str]   # 12-char SHA from metadata.json
    branch: Optional[str]
    dirty: bool
    kernel: Optional[str]
    depths: List[int]
    weights: List[int]
    iterations: int
    has_baseline: bool
    has_qos: bool
    condition_id: Optional[str] = None


# Pattern: run_YYYY-MM-DD_HH-MM-SS
_RUN_DIR_RE = re.compile(r"^run_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})$")


def _parse_timestamp(dirname: str) -> Optional[str]:
    """Parse 'run_2026-02-14_20-16-30' -> '2026-02-14 20:16'."""
    m = _RUN_DIR_RE.match(dirname)
    if not m:
        return None
    date_part = m.group(1)
    hour = m.group(2)
    minute = m.group(3)
    return f"{date_part} {hour}:{minute}"


def scan_results_dir(results_dir: Path) -> List[RunSummary]:
    """Scan a results directory for benchmark runs.

    Returns list of RunSummary sorted newest-first.
    Skips baseline_* directories (different format).
    """
    if not results_dir.is_dir():
        return []

    runs = []
    for entry in sorted(results_dir.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        if not entry.name.startswith("run_"):
            continue

        ts = _parse_timestamp(entry.name)
        if ts is None:
            continue

        # Defaults for missing metadata
        commit = None
        branch = None
        dirty = False
        kernel = None
        depths = []
        weights = []
        iterations = 0
        condition_id = None

        # Load metadata.json
        meta_file = entry / "metadata.json"
        if meta_file.exists():
            try:
                with open(meta_file) as f:
                    meta = json.load(f)
                system = meta.get("system", {})
                git = system.get("git", {})
                commit = git.get("commit")
                branch = git.get("branch")
                dirty = git.get("dirty", False)
                kernel = system.get("kernel")
                config = meta.get("config", {})
                depths = config.get("depths", [])
                weights = config.get("weights", [])
                iterations = config.get("iterations", 0)
                condition_id = config.get("condition_id")
            except (json.JSONDecodeError, KeyError):
                pass

        # Peek at aggregate.json for baseline/qos status
        has_baseline = False
        has_qos = False
        agg_file = entry / "aggregate.json"
        if agg_file.exists():
            try:
                with open(agg_file) as f:
                    agg = json.load(f)
                has_baseline = bool(agg.get("baseline"))
                has_qos = bool(agg.get("qos"))
            except (json.JSONDecodeError, KeyError):
                pass

        runs.append(RunSummary(
            path=entry,
            timestamp=ts,
            commit=commit,
            branch=branch,
            dirty=dirty,
            kernel=kernel,
            depths=depths,
            weights=weights,
            iterations=iterations,
            has_baseline=has_baseline,
            has_qos=has_qos,
            condition_id=condition_id,
        ))

    return runs


def filter_by_commit(runs: List[RunSummary], prefix: str) -> List[RunSummary]:
    """Filter runs by git commit prefix.

    Warns if prefix matches multiple distinct full SHAs.
    """
    prefix = prefix.lower()
    matched = [r for r in runs if r.commit and r.commit.lower().startswith(prefix)]

    # Check for ambiguous prefix
    distinct_commits = set(r.commit for r in matched)
    if len(distinct_commits) > 1:
        print(colored(
            f"WARNING: Prefix '{prefix}' matches {len(distinct_commits)} distinct commits: "
            + ", ".join(sorted(distinct_commits)),
            "yellow"
        ), file=sys.stderr)

    return matched


def get_config_tuples(run: RunSummary) -> Set[Tuple]:
    """Get all (qos_enabled, iodepth, qos_weight, workload) tuples from aggregate.json."""
    agg_file = run.path / "aggregate.json"
    if not agg_file.exists():
        return set()

    try:
        with open(agg_file) as f:
            agg = json.load(f)
    except (json.JSONDecodeError, KeyError):
        return set()

    tuples = set()
    for result in agg.get("all", []):
        config = result.get("config", {})
        tuples.add((
            config.get("qos_enabled", False),
            config.get("iodepth", 0),
            config.get("qos_weight"),
            config.get("workload"),
        ))
    return tuples


def load_iterations_for_config(
    run: RunSummary,
    qos_enabled: bool,
    iodepth: int,
    qos_weight: Optional[int] = None,
    workload: Optional[str] = None,
    metric_key: str = "p99_us",
) -> List[float]:
    """Load per-iteration metric values for a specific config from a run.

    Returns list of metric values (one per iteration).
    Falls back to single-element list from aggregate metrics if iterations missing.
    """
    agg_file = run.path / "aggregate.json"
    if not agg_file.exists():
        return []

    try:
        with open(agg_file) as f:
            agg = json.load(f)
    except (json.JSONDecodeError, KeyError):
        return []

    for result in agg.get("all", []):
        config = result.get("config", {})
        if (config.get("qos_enabled") == qos_enabled
                and config.get("iodepth") == iodepth
                and config.get("qos_weight") == qos_weight
                and config.get("workload") == workload):
            # Found matching config
            iterations = result.get("iterations")
            if iterations:
                values = []
                for it in iterations:
                    val = it.get(metric_key)
                    if val is not None:
                        values.append(float(val))
                return values
            # Fallback: use aggregate metrics as single sample
            metrics = result.get("metrics", {})
            val = metrics.get(metric_key)
            if val is not None:
                return [float(val)]
            return []

    return []


def pool_iterations_across_runs(
    runs: List[RunSummary],
    qos_enabled: bool,
    iodepth: int,
    qos_weight: Optional[int] = None,
    workload: Optional[str] = None,
    metric_key: str = "p99_us",
) -> List[float]:
    """Pool per-iteration metric values across multiple runs.

    Concatenates iterations from all matching runs to build a larger sample
    for statistical testing.
    """
    pooled = []
    for run in runs:
        pooled.extend(load_iterations_for_config(
            run, qos_enabled, iodepth, qos_weight, workload, metric_key
        ))
    return pooled
