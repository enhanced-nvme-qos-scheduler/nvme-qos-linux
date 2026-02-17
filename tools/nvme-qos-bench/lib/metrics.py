# SPDX-License-Identifier: GPL-2.0
"""Metrics extraction from fio JSON output."""

from dataclasses import dataclass
from typing import Dict, Any, Optional, List


@dataclass
class JobMetrics:
    """Metrics for a single fio job."""
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
    """Aggregated metrics from a fio run."""
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
    """Convert nanoseconds to microseconds."""
    return ns / 1000.0


def _extract_percentile(clat_ns: Dict, percentile: float) -> float:
    """Extract latency percentile from fio clat_ns percentiles."""
    percentiles = clat_ns.get("percentile", {})
    # fio uses string keys like "99.000000"
    key = f"{percentile:.6f}"
    return _ns_to_us(float(percentiles.get(key, 0)))


def extract_job_metrics(job: Dict[str, Any]) -> JobMetrics:
    """Extract metrics from a single fio job result."""
    # Determine if this is a read or write job
    read_data = job.get("read", {})
    write_data = job.get("write", {})

    # Use whichever has data (non-zero iops)
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
    """Extract all metrics from fio JSON output."""
    jobs_data = fio_json.get("jobs", [])

    jobs = [extract_job_metrics(j) for j in jobs_data]

    # Identify high-priority and normal-priority jobs by name
    high_prio = None
    normal_prio = None
    high_prio_jobs = []
    normal_prio_jobs = []

    for jm in jobs:
        name_lower = jm.job_name.lower()
        if "high" in name_lower or "prio" in name_lower and "normal" not in name_lower:
            high_prio_jobs.append(jm)
        elif "normal" in name_lower or "bulk" in name_lower:
            normal_prio_jobs.append(jm)

    # Aggregate high-priority jobs if multiple
    if high_prio_jobs:
        high_prio = _aggregate_jobs(high_prio_jobs, "high-prio-aggregate")

    # Aggregate normal-priority jobs if multiple
    if normal_prio_jobs:
        normal_prio = _aggregate_jobs(normal_prio_jobs, "normal-prio-aggregate")

    # Calculate totals
    total_iops = sum(j.iops for j in jobs)
    total_bw = sum(j.bw_bytes for j in jobs)
    avg_cpu = sum(j.usr_cpu + j.sys_cpu for j in jobs) / len(jobs) if jobs else 0

    # Parse disk_util from top-level fio JSON (first entry if available)
    disk_util = None
    disk_util_list = fio_json.get("disk_util", [])
    if disk_util_list:
        du = disk_util_list[0]
        disk_util = {
            "name": du.get("name", ""),
            "read_ios": du.get("read_ios", 0),
            "write_ios": du.get("write_ios", 0),
            "read_merges": du.get("read_merges", 0),
            "write_merges": du.get("write_merges", 0),
            "util_pct": du.get("util", 0.0),
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
    """Aggregate metrics from multiple jobs of the same type."""
    if not jobs:
        raise ValueError("Cannot aggregate empty job list")

    if len(jobs) == 1:
        return jobs[0]

    # Sum IOPS and bandwidth
    total_iops = sum(j.iops for j in jobs)
    total_bw = sum(j.bw_bytes for j in jobs)

    # For latency, take the worst (max) p99 across jobs
    # This represents the worst-case latency experienced
    worst_p99 = max(j.lat_p99_us for j in jobs)
    worst_p999 = max(j.lat_p999_us for j in jobs)
    worst_p9999 = max(j.lat_p9999_us for j in jobs)

    # Average for other latency metrics
    avg_mean = sum(j.lat_mean_us for j in jobs) / len(jobs)
    avg_p50 = sum(j.lat_p50_us for j in jobs) / len(jobs)
    avg_p90 = sum(j.lat_p90_us for j in jobs) / len(jobs)

    # CPU: sum since multiple jobs
    total_usr = sum(j.usr_cpu for j in jobs)
    total_sys = sum(j.sys_cpu for j in jobs)
    total_ctx = sum(j.ctx for j in jobs)

    # Use max runtime
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
    """Get the primary metrics for comparison (high-priority or first job)."""
    # Prefer high-priority metrics if available (mixed workload)
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

    # Include disk_util if available
    if metrics.disk_util:
        result["io_util_pct"] = metrics.disk_util["util_pct"]
        result["io_read_ios"] = metrics.disk_util["read_ios"]
        result["io_write_ios"] = metrics.disk_util["write_ios"]
        result["io_read_merges"] = metrics.disk_util["read_merges"]
        result["io_write_merges"] = metrics.disk_util["write_merges"]

    return result


def get_normal_metrics(metrics: FioMetrics) -> Dict[str, float]:
    """Get normal-priority job metrics for the other side of the QoS tradeoff."""
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
