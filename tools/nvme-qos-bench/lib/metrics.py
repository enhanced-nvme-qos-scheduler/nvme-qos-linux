# SPDX-License-Identifier: GPL-2.0
"""Metrics extraction from fio JSON output."""

import sys
from dataclasses import dataclass
from typing import Dict, Any, Optional, List

HIGH_PRIO_JOB_NAMES = frozenset({"high-prio-reads"})
NORMAL_PRIO_JOB_NAMES = frozenset({"normal-prio-writes"})
HIGH_PRIO_JOB_PREFIX = "high-prio-reads-"
NORMAL_PRIO_JOB_PREFIX = "normal-prio-writes-"


@dataclass
class JobMetrics:
    job_name: str
    iops: float
    bw_bytes: float          # Bandwidth in bytes/sec
    lat_mean_us: float       # Mean latency in microseconds
    lat_p50_us: float        # p50 latency
    lat_p90_us: float        # p90 latency
    lat_p99_us: float        # p99 latency
    lat_p999_us: float       # p99.9 latency
    lat_p9999_us: float      # p99.99 latency
    lat_min_us: float        # Minimum latency
    lat_max_us: float        # Maximum latency
    usr_cpu: float           # User CPU percentage
    sys_cpu: float           # System CPU percentage
    ctx: int                 # Context switches
    runtime_ms: int          # Actual runtime in ms


@dataclass
class FioMetrics:
    jobs: List[JobMetrics]
    # Aggregate metrics (for mixed workloads, separate by priority class)
    high_prio: Optional[JobMetrics] = None
    normal_prio: Optional[JobMetrics] = None
    # Overall
    total_iops: float = 0
    total_bw_bytes: float = 0
    avg_cpu: float = 0
    disk_util: Optional[Dict] = None


def _ns_to_us(ns: float) -> float:
    return ns / 1000.0


def _extract_percentile(clat_ns: Dict, percentile: float) -> float:
    """Extract latency percentile from fio clat_ns percentiles.

    Uses tolerance-based lookup to handle varying FIO output formats
    (e.g., "99", "99.0", "99.000000").
    """
    percentiles = clat_ns.get("percentile", {})
    if not percentiles:
        return 0.0

    # Try to find the key closest to the target percentile
    # FIO may output keys as "99.000000", "99.0", or "99"
    TOLERANCE = 0.001
    best_match = None
    best_diff = float('inf')

    for key_str in percentiles.keys():
        try:
            key_value = float(key_str)
            diff = abs(key_value - percentile)
            if diff < best_diff:
                best_diff = diff
                best_match = key_str
        except (ValueError, TypeError):
            # Ignore non-numeric keys
            continue

    if best_match is not None and best_diff < TOLERANCE:
        return _ns_to_us(float(percentiles[best_match]))

    # No close match found - log warning and return 0
    print(f"Warning: percentile p{percentile} not found in FIO output (available: {list(percentiles.keys())[:5]}...)", file=sys.stderr)
    return 0.0


def extract_job_metrics(job: Dict[str, Any]) -> JobMetrics:
    read_data = job.get("read", {})
    write_data = job.get("write", {})

    if read_data.get("iops", 0) > 0:
        data = read_data
    elif write_data.get("iops", 0) > 0:
        data = write_data
    else:
        # Mixed or no data - combine
        data = read_data if read_data else write_data

    clat_ns = data.get("clat_ns", {})

    return JobMetrics(
        job_name=job.get("jobname", "unknown"),
        iops=data.get("iops", 0),
        bw_bytes=data.get("bw_bytes", 0),
        lat_mean_us=_ns_to_us(clat_ns.get("mean", 0)),
        lat_p50_us=_extract_percentile(clat_ns, 50.0),
        lat_p90_us=_extract_percentile(clat_ns, 90.0),
        lat_p99_us=_extract_percentile(clat_ns, 99.0),
        lat_p999_us=_extract_percentile(clat_ns, 99.9),
        lat_p9999_us=_extract_percentile(clat_ns, 99.99),
        lat_min_us=_ns_to_us(clat_ns.get("min", 0)),
        lat_max_us=_ns_to_us(clat_ns.get("max", 0)),
        usr_cpu=job.get("usr_cpu", 0),
        sys_cpu=job.get("sys_cpu", 0),
        ctx=job.get("ctx", 0),
        runtime_ms=job.get("job_runtime", 0),
    )


def extract_fio_metrics(fio_json: Dict[str, Any]) -> FioMetrics:
    jobs_data = fio_json.get("jobs", [])

    jobs = [extract_job_metrics(j) for j in jobs_data]

    # Identify high-priority and normal-priority jobs by name.
    # Canonical names are defined in HIGH_PRIO_JOB_NAMES / NORMAL_PRIO_JOB_NAMES above.
    # Update those sets if fio template section names change.
    high_prio = None
    normal_prio = None
    high_prio_jobs = []
    normal_prio_jobs = []

    for jm in jobs:
        if jm.job_name in HIGH_PRIO_JOB_NAMES or jm.job_name.startswith(HIGH_PRIO_JOB_PREFIX):
            high_prio_jobs.append(jm)
        elif jm.job_name in NORMAL_PRIO_JOB_NAMES or jm.job_name.startswith(NORMAL_PRIO_JOB_PREFIX):
            normal_prio_jobs.append(jm)
        else:
            raise ValueError(
                f"Unknown job name '{jm.job_name}' — not in HIGH_PRIO_JOB_NAMES or "
                f"NORMAL_PRIO_JOB_NAMES. Update the sets in metrics.py if templates changed."
            )

    if high_prio_jobs:
        high_prio = _aggregate_jobs(high_prio_jobs, "high-prio-aggregate")

    if normal_prio_jobs:
        normal_prio = _aggregate_jobs(normal_prio_jobs, "normal-prio-aggregate")

    total_iops = sum(j.iops for j in jobs)
    total_bw = sum(j.bw_bytes for j in jobs)
    avg_cpu = sum(j.usr_cpu + j.sys_cpu for j in jobs) / len(jobs) if jobs else 0

    # Parse disk_util from top-level fio JSON. Multi-device runs can report
    # multiple entries, so sum counters and retain the max utilization.
    disk_util = None
    disk_util_list = fio_json.get("disk_util", [])
    if disk_util_list:
        disk_util = {
            "name": ",".join(du.get("name", "") for du in disk_util_list),
            "read_ios": sum(du.get("read_ios", 0) for du in disk_util_list),
            "write_ios": sum(du.get("write_ios", 0) for du in disk_util_list),
            "read_merges": sum(du.get("read_merges", 0) for du in disk_util_list),
            "write_merges": sum(du.get("write_merges", 0) for du in disk_util_list),
            "util_pct": max(du.get("util", 0.0) for du in disk_util_list),
        }

    return FioMetrics(
        jobs=jobs,
        high_prio=high_prio,
        normal_prio=normal_prio,
        total_iops=total_iops,
        total_bw_bytes=total_bw,
        avg_cpu=avg_cpu,
        disk_util=disk_util,
    )


def _aggregate_jobs(jobs: List[JobMetrics], name: str) -> JobMetrics:
    if not jobs:
        raise ValueError("Cannot aggregate empty job list")

    if len(jobs) == 1:
        return jobs[0]

    total_iops = sum(j.iops for j in jobs)
    total_bw = sum(j.bw_bytes for j in jobs)

    # For latency, take the worst (max) p99 across jobs
    # This represents the worst-case latency experienced
    worst_p99 = max(j.lat_p99_us for j in jobs)
    worst_p999 = max(j.lat_p999_us for j in jobs)
    worst_p9999 = max(j.lat_p9999_us for j in jobs)

    avg_mean = sum(j.lat_mean_us for j in jobs) / len(jobs)
    avg_p50 = sum(j.lat_p50_us for j in jobs) / len(jobs)
    avg_p90 = sum(j.lat_p90_us for j in jobs) / len(jobs)

    total_usr = sum(j.usr_cpu for j in jobs)
    total_sys = sum(j.sys_cpu for j in jobs)
    total_ctx = sum(j.ctx for j in jobs)
    max_runtime = max(j.runtime_ms for j in jobs)

    return JobMetrics(
        job_name=name,
        iops=total_iops,
        bw_bytes=total_bw,
        lat_mean_us=avg_mean,
        lat_p50_us=avg_p50,
        lat_p90_us=avg_p90,
        lat_p99_us=worst_p99,
        lat_p999_us=worst_p999,
        lat_p9999_us=worst_p9999,
        lat_min_us=min(j.lat_min_us for j in jobs),
        lat_max_us=max(j.lat_max_us for j in jobs),
        usr_cpu=total_usr,
        sys_cpu=total_sys,
        ctx=total_ctx,
        runtime_ms=max_runtime,
    )


def get_primary_metrics(metrics: FioMetrics) -> Dict[str, float]:
    if metrics.high_prio:
        job = metrics.high_prio
    elif metrics.jobs:
        job = metrics.jobs[0]
    else:
        return {}

    result = {
        "p50_us": job.lat_p50_us,
        "p90_us": job.lat_p90_us,
        "p99_us": job.lat_p99_us,
        "p999_us": job.lat_p999_us,
        "p9999_us": job.lat_p9999_us,
        "iops": job.iops,
        "bw_mbps": job.bw_bytes / (1024 * 1024),
        "cpu_pct": job.usr_cpu + job.sys_cpu,
        "ctx": job.ctx,
        "runtime_s": job.runtime_ms / 1000,
    }

    if metrics.disk_util:
        result["io_util_pct"] = metrics.disk_util["util_pct"]
        result["io_read_ios"] = metrics.disk_util["read_ios"]
        result["io_write_ios"] = metrics.disk_util["write_ios"]
        result["io_read_merges"] = metrics.disk_util["read_merges"]
        result["io_write_merges"] = metrics.disk_util["write_merges"]

    return result


def get_normal_metrics(metrics: FioMetrics) -> Dict[str, float]:
    if not metrics.normal_prio:
        return {}

    job = metrics.normal_prio
    return {
        "p50_us": job.lat_p50_us,
        "p90_us": job.lat_p90_us,
        "p99_us": job.lat_p99_us,
        "p999_us": job.lat_p999_us,
        "iops": job.iops,
        "bw_mbps": job.bw_bytes / (1024 * 1024),
    }
