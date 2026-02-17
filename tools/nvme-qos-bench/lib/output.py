# SPDX-License-Identifier: GPL-2.0
"""Output formatters: JSON, CSV, Markdown."""

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

from .analysis import Statistics, compare_results, calculate_stats, percentage_change
from .metrics import FioMetrics, JobMetrics
from .progress import format_us, si_format


def save_json(data: Dict[str, Any], path: Path) -> None:
    """Save data as JSON file."""
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=_json_serializer)


def _json_serializer(obj):
    """Custom JSON serializer for dataclasses and special types."""
    if hasattr(obj, '__dataclass_fields__'):
        return asdict(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def save_csv(results: List[Dict[str, Any]], path: Path, fieldnames: Optional[List[str]] = None) -> None:
    """Save results as CSV file."""
    if not results:
        return

    if fieldnames is None:
        fieldnames = list(results[0].keys())

    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow({k: row.get(k, '') for k in fieldnames})


def flatten_result(test_config: Dict, metrics: Dict) -> Dict[str, Any]:
    """Flatten a test result for CSV export."""
    flat = {
        "timestamp": datetime.now().isoformat(),
        "qos_enabled": test_config.get("qos_enabled", False),
        "qos_weight": test_config.get("qos_weight", 0),
        "iodepth": test_config.get("iodepth", 0),
        "iteration": test_config.get("iteration", 0),
    }
    flat.update(metrics)
    return flat


CSV_FIELDNAMES = [
    "timestamp",
    "qos_enabled",
    "qos_weight",
    "iodepth",
    "iteration",
    "p50_us",
    "p90_us",
    "p99_us",
    "p999_us",
    "p9999_us",
    "iops",
    "bw_mbps",
    "cpu_pct",
    "ctx",
    "io_util_pct",
    "io_read_ios",
    "io_write_ios",
    "io_read_merges",
    "io_write_merges",
    "runtime_s",
    # Normal-priority metrics
    "norm_p50_us",
    "norm_p90_us",
    "norm_p99_us",
    "norm_p999_us",
    "norm_iops",
    "norm_bw_mbps",
    # Kernel QoS counters
    "ks_high_enqueued",
    "ks_normal_enqueued",
    "ks_high_dispatched",
    "ks_normal_dispatched",
    "ks_wc_high_fallback",
    "ks_wc_normal_fallback",
    "ks_credit_refills",
    "ks_kicks",
    "ks_kick_empty",
    "ks_sq_throttled",
    "ks_doorbells",
    # Fairness validation
    "fair_expected_hi_pct",
    "fair_actual_hi_pct",
    "fair_deviation_pct",
    "fair_result",
    "fair_demand_hi_pct",
    "fair_demand_limited",
    "fair_effective_expected_hi_pct",
    "fair_weight_hi_pct",
]


def generate_markdown_report(
    system_info: Dict[str, Any],
    results: List[Dict[str, Any]],
    comparisons: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate a Markdown summary report."""
    lines = []

    # Header
    lines.append("# NVMe QoS Benchmark Results")
    lines.append("")
    lines.append(f"**Date**: {system_info.get('timestamp', 'N/A')}")
    lines.append(f"**Kernel**: {system_info.get('kernel', 'N/A')}")
    lines.append(f"**Device**: {system_info.get('device', 'N/A')}")

    nvme = system_info.get('nvme', {})
    lines.append(f"**NVMe Model**: {nvme.get('model', 'N/A')}")

    git = system_info.get('git', {})
    if git.get('commit'):
        dirty = " (dirty)" if git.get('dirty') else ""
        lines.append(f"**Git**: {git.get('branch', 'N/A')} @ {git['commit']}{dirty}")

    lines.append("")

    # Results table
    lines.append("## Results")
    lines.append("")
    lines.append("| Config | p50 (us) | p90 (us) | p99 (us) | p999 (us) | IOPS | CPU % | Util % | p99 Change |")
    lines.append("|--------|----------|----------|----------|-----------|------|-------|--------|------------|")

    for r in results:
        config = r.get('config', {})
        metrics = r.get('metrics', {})

        qos = "QoS" if config.get('qos_enabled') else "baseline"
        weight = config.get('qos_weight', '-')
        depth = config.get('iodepth', '-')

        label = f"{qos} qd={depth}"
        if config.get('qos_enabled'):
            label += f" w={weight}"

        p50 = metrics.get('p50_us', 0)
        p90 = metrics.get('p90_us', 0)
        p99 = metrics.get('p99_us', 0)
        p999 = metrics.get('p999_us', 0)
        iops = metrics.get('iops', 0)
        cpu = metrics.get('cpu_pct', 0)
        change = r.get('pct_change', '')

        util = metrics.get('io_util_pct', 0)

        if isinstance(change, (int, float)):
            change_str = f"{change:+.1f}%"
        else:
            change_str = "-"

        util_str = f"{util:.1f}" if util else "-"
        lines.append(f"| {label} | {p50:.0f} | {p90:.0f} | {p99:.0f} | {p999:.0f} | {iops:.0f} | {cpu:.1f} | {util_str} | {change_str} |")

    lines.append("")

    # Normal-priority metrics table (if any QoS result has them)
    qos_with_normal = [r for r in results if r.get("normal_metrics")]
    if qos_with_normal:
        # Build baseline NORM lookup by iodepth
        baseline_norm_by_depth = {}
        for r in results:
            if not r.get('config', {}).get('qos_enabled') and r.get('normal_metrics'):
                depth = r['config'].get('iodepth')
                if depth is not None and r['config'].get('workload') is None:
                    baseline_norm_by_depth[depth] = r['normal_metrics']

        lines.append("## Normal-Priority Metrics")
        lines.append("")
        lines.append("| Config | p50 (us) | p90 (us) | p99 (us) | p999 (us) | IOPS | BW (MB/s) | Baseline p99 | p99 Change |")
        lines.append("|--------|----------|----------|----------|-----------|------|-----------|--------------|------------|")
        for r in qos_with_normal:
            config = r.get('config', {})
            nm = r.get('normal_metrics', {})
            label = f"QoS qd={config.get('iodepth', '-')} w={config.get('qos_weight', '-')}"
            # Baseline NORM comparison
            bl_norm = baseline_norm_by_depth.get(config.get('iodepth'))
            if bl_norm and bl_norm.get('p99_us') and nm.get('p99_us'):
                bl_p99_str = f"{bl_norm['p99_us']:.0f}"
                change = ((nm['p99_us'] - bl_norm['p99_us']) / bl_norm['p99_us']) * 100
                change_str = f"{change:+.1f}%"
            else:
                bl_p99_str = "-"
                change_str = "-"
            lines.append(
                f"| {label} | {nm.get('p50_us', 0):.0f} | {nm.get('p90_us', 0):.0f} "
                f"| {nm.get('p99_us', 0):.0f} | {nm.get('p999_us', 0):.0f} "
                f"| {nm.get('iops', 0):.0f} | {nm.get('bw_mbps', 0):.0f} "
                f"| {bl_p99_str} | {change_str} |"
            )
        lines.append("")

    # Kernel QoS counters table (if any QoS result has them)
    qos_with_ks = [r for r in results if r.get("kernel_stats")]
    if qos_with_ks:
        lines.append("## Kernel QoS Counters")
        lines.append("")
        lines.append("| Config | Hi Disp | Norm Disp | Hi Enq | Norm Enq | WC Hi | WC Norm | Kicks | Kick Empty | Refills | SQ Throt | Doorbells | Fair |")
        lines.append("|--------|---------|-----------|--------|----------|-------|---------|-------|------------|---------|----------|-----------|------|")
        for r in qos_with_ks:
            config = r.get('config', {})
            ks = r.get('kernel_stats', {})
            fair = r.get('fairness', {})
            label = f"QoS qd={config.get('iodepth', '-')} w={config.get('qos_weight', '-')}"
            fair_str = fair.get('fair', '-')
            if fair.get('demand_limited'):
                fair_str += "(demand-lim)"
            elif fair_str == "OK":
                fair_str += "(weight-lim)"
            lines.append(
                f"| {label} "
                f"| {ks.get('high_dispatched', 0)} "
                f"| {ks.get('normal_dispatched', 0)} "
                f"| {ks.get('high_enqueued', 0)} "
                f"| {ks.get('normal_enqueued', 0)} "
                f"| {ks.get('wc_high_fallback', 0)} "
                f"| {ks.get('wc_normal_fallback', 0)} "
                f"| {ks.get('kicks', 0)} "
                f"| {ks.get('kick_empty', 0)} "
                f"| {ks.get('credit_refills', 0)} "
                f"| {ks.get('sq_throttled', 0)} "
                f"| {ks.get('doorbells', 0)} "
                f"| {fair_str} |"
            )
        lines.append("")

    # Comparison summary if available
    if comparisons:
        lines.append("## Summary")
        lines.append("")

        p99_changes = comparisons.get('p99_changes', [])
        if p99_changes:
            min_change = min(p99_changes)
            max_change = max(p99_changes)
            lines.append(f"- **p99 Latency**: {min_change:+.1f}% to {max_change:+.1f}%")

        iops_changes = comparisons.get('iops_changes', [])
        if iops_changes:
            min_change = min(iops_changes)
            max_change = max(iops_changes)
            lines.append(f"- **IOPS**: {min_change:+.1f}% to {max_change:+.1f}%")

        cpu_changes = comparisons.get('cpu_changes', [])
        if cpu_changes:
            min_change = min(cpu_changes)
            max_change = max(cpu_changes)
            lines.append(f"- **CPU**: {min_change:+.1f}% to {max_change:+.1f}%")

        norm_p99_changes = comparisons.get('norm_p99_changes', [])
        if norm_p99_changes:
            min_change = min(norm_p99_changes)
            max_change = max(norm_p99_changes)
            lines.append(f"- **Normal-Priority p99**: {min_change:+.1f}% to {max_change:+.1f}%")

        lines.append("")

    # Footer
    lines.append("---")
    lines.append(f"Generated by nvme-qos-bench | fio {system_info.get('fio_version', 'N/A')}")

    return "\n".join(lines)


def generate_comparison_report(
    baseline_results: List[Dict],
    qos_results: List[Dict],
    system_info: Dict,
) -> str:
    """Generate side-by-side comparison Markdown report."""
    lines = []

    lines.append("# NVMe QoS Comparison Report")
    lines.append("")
    lines.append(f"**Date**: {datetime.now().isoformat()}")
    lines.append(f"**Kernel**: {system_info.get('kernel', 'N/A')}")
    lines.append(f"**Device**: {system_info.get('device', 'N/A')}")
    lines.append("")

    # Match baseline and QoS results by queue depth
    baseline_by_depth = {r['config']['iodepth']: r for r in baseline_results}
    qos_by_depth = {r['config']['iodepth']: r for r in qos_results}

    # Latency comparisons for each percentile
    lines.append("## Latency Comparison")
    lines.append("")

    for pct_name, pct_key in [("p50", "p50_us"), ("p90", "p90_us"), ("p99", "p99_us"), ("p999", "p999_us")]:
        lines.append(f"### {pct_name} Latency")
        lines.append("")
        lines.append(f"| Queue Depth | Baseline | QoS | Change |")
        lines.append("|-------------|----------|-----|--------|")

        for depth in sorted(set(baseline_by_depth.keys()) | set(qos_by_depth.keys())):
            baseline = baseline_by_depth.get(depth, {}).get('metrics', {})
            qos = qos_by_depth.get(depth, {}).get('metrics', {})

            b_val = baseline.get(pct_key, 0)
            q_val = qos.get(pct_key, 0)
            change = ((q_val - b_val) / b_val * 100) if b_val else 0

            lines.append(f"| {depth} | {b_val:.0f} us | {q_val:.0f} us | {change:+.1f}% |")

        lines.append("")

    # IOPS comparison
    lines.append("## IOPS Comparison")
    lines.append("")
    lines.append("| Queue Depth | Baseline | QoS | Change |")
    lines.append("|-------------|----------|-----|--------|")

    for depth in sorted(set(baseline_by_depth.keys()) | set(qos_by_depth.keys())):
        baseline = baseline_by_depth.get(depth, {}).get('metrics', {})
        qos = qos_by_depth.get(depth, {}).get('metrics', {})

        b_iops = baseline.get('iops', 0)
        q_iops = qos.get('iops', 0)
        iops_change = ((q_iops - b_iops) / b_iops * 100) if b_iops else 0

        lines.append(f"| {depth} | {b_iops:.0f} | {q_iops:.0f} | {iops_change:+.1f}% |")

    lines.append("")

    # Normal-priority p99 latency comparison
    baseline_norm_by_depth = {}
    for r in baseline_results:
        if r.get('normal_metrics') and r['config'].get('workload') is None:
            baseline_norm_by_depth[r['config']['iodepth']] = r['normal_metrics']

    qos_norm_by_depth = {}
    for r in qos_results:
        if r.get('normal_metrics') and r['config'].get('workload') is None:
            qos_norm_by_depth[r['config']['iodepth']] = r['normal_metrics']

    if baseline_norm_by_depth and qos_norm_by_depth:
        lines.append("## Normal-Priority p99 Latency")
        lines.append("")
        lines.append("| Queue Depth | Baseline p99 (us) | QoS p99 (us) | Change |")
        lines.append("|-------------|-------------------|--------------|--------|")

        for depth in sorted(set(baseline_norm_by_depth.keys()) | set(qos_norm_by_depth.keys())):
            b_norm = baseline_norm_by_depth.get(depth, {})
            q_norm = qos_norm_by_depth.get(depth, {})
            b_p99 = b_norm.get('p99_us', 0)
            q_p99 = q_norm.get('p99_us', 0)
            change = ((q_p99 - b_p99) / b_p99 * 100) if b_p99 else 0
            lines.append(f"| {depth} | {b_p99:.0f} | {q_p99:.0f} | {change:+.1f}% |")

        lines.append("")

    lines.append("---")
    lines.append("Generated by nvme-qos-bench")

    return "\n".join(lines)


def _find_bl_match(results: Dict, depth: int, workload=None):
    """Find baseline result matching depth and workload."""
    for b in results.get("baseline", []):
        if (b["config"]["iodepth"] == depth
                and b["config"].get("workload") == workload):
            return b
    return None


def generate_analysis_report(
    input_dir: Path,
    results: Dict[str, Any],
    system_info: Dict[str, Any],
    config_info: Dict[str, Any],
) -> str:
    """Generate comprehensive analysis as Markdown (mirrors terminal output)."""
    lines = []

    # Header
    lines.append("# NVMe QoS Benchmark Analysis")
    lines.append("")
    lines.append(f"**Directory**: `{input_dir}`")
    kernel = system_info.get("kernel", "N/A")
    git = system_info.get("git", {})
    commit = git.get("commit", "N/A")
    branch = git.get("branch", "")
    dirty = " (dirty)" if git.get("dirty") else ""
    lines.append(f"**Kernel**: {kernel}")
    if commit != "N/A":
        lines.append(f"**Git**: {branch} @ {commit}{dirty}")
    device = system_info.get("device", "N/A")
    depths = config_info.get("depths", [])
    weights = config_info.get("weights", [])
    iters = config_info.get("iterations", "?")
    lines.append(f"**Device**: {device}  |  **Config**: qd={depths} w={weights} iter={iters}")
    lines.append("")

    # High-priority table
    all_results = results.get("all", [])
    if all_results:
        lines.append("## High-Priority Latency")
        lines.append("")
        lines.append("| Config | p50 | p90 | p99 | p999 | IOPS | CPU | Util | p99 Change |")
        lines.append("|--------|-----|-----|-----|------|------|-----|------|------------|")
        for r in all_results:
            config = r.get("config", {})
            m = r.get("metrics", {})
            qos = config.get("qos_enabled", False)
            depth = config.get("iodepth", 0)
            weight = config.get("qos_weight")
            workload = config.get("workload")
            label = f"{'QoS' if qos else 'bl'} qd={depth}"
            if qos and weight:
                label += f" w={weight}"
            if workload:
                label += f" {workload}"
            pct = r.get("pct_change")
            pct_str = f"{pct:+.1f}%" if pct is not None else "-"
            util = m.get("io_util_pct", 0)
            lines.append(
                f"| {label} | {format_us(m.get('p50_us', 0))} "
                f"| {format_us(m.get('p90_us', 0))} "
                f"| {format_us(m.get('p99_us', 0))} "
                f"| {format_us(m.get('p999_us', 0))} "
                f"| {si_format(m.get('iops', 0))} "
                f"| {m.get('cpu_pct', 0):.1f}% "
                f"| {util:.1f}% "
                f"| {pct_str} |"
            )
        lines.append("")

    # Normal-priority table
    qos_results = results.get("qos", [])
    has_normal = any(r.get("normal_metrics") for r in qos_results)
    if has_normal:
        lines.append("## Normal-Priority Impact")
        lines.append("")
        lines.append("| Config | p99 Baseline | p99 QoS | Change | IOPS | BW |")
        lines.append("|--------|-------------|---------|--------|------|-----|")
        for r in qos_results:
            nm = r.get("normal_metrics")
            if not nm:
                continue
            config = r.get("config", {})
            depth = config.get("iodepth", 0)
            bl_match = _find_bl_match(results, depth, config.get("workload"))
            bl_norm = bl_match.get("normal_metrics", {}) if bl_match else {}
            bl_p99 = bl_norm.get("p99_us", 0)
            qos_p99 = nm.get("p99_us", 0)
            if bl_p99 > 0 and qos_p99 > 0:
                change = percentage_change(bl_p99, qos_p99)
                change_str = f"{change:+.1f}%"
            else:
                change_str = "-"
            label = f"QoS qd={depth}"
            lines.append(
                f"| {label} "
                f"| {format_us(bl_p99) if bl_p99 else 'N/A'} "
                f"| {format_us(qos_p99)} "
                f"| {change_str} "
                f"| {si_format(nm.get('iops', 0))} "
                f"| {nm.get('bw_mbps', 0):.0f}MB/s |"
            )
        lines.append("")

    # Throughput proportionality
    has_tp = any(r.get("normal_metrics") for r in qos_results)
    if has_tp:
        lines.append("## Throughput Proportionality")
        lines.append("")
        lines.append("| Config | Hi IOPS | Norm IOPS | Actual | Target | Dev |")
        lines.append("|--------|---------|-----------|--------|--------|-----|")
        for r in qos_results:
            nm = r.get("normal_metrics")
            if not nm:
                continue
            config = r.get("config", {})
            depth = config.get("iodepth", 0)
            weight = config.get("qos_weight", 0)
            hi_iops = r["metrics"].get("iops", 0)
            norm_iops = nm.get("iops", 0)
            total_iops = hi_iops + norm_iops
            if total_iops > 0:
                actual_hi = hi_iops / total_iops * 100
            else:
                actual_hi = 0
            target_hi = (weight / (weight + 1)) * 100 if weight > 0 else 50
            dev = actual_hi - target_hi
            lines.append(
                f"| qd={depth} "
                f"| {si_format(hi_iops)} "
                f"| {si_format(norm_iops)} "
                f"| {actual_hi:.0f}:{100-actual_hi:.0f} "
                f"| {target_hi:.0f}:{100-target_hi:.0f} "
                f"| {dev:+.0f}pp |"
            )
        lines.append("")

    # Per-iteration variance
    has_iters = any(r.get("iterations") and len(r.get("iterations", [])) > 1
                    for r in all_results)
    if has_iters:
        lines.append("## Per-Iteration Variance")
        lines.append("")
        lines.append("| Config | p99 mean | stddev | CI 95% | range | n |")
        lines.append("|--------|----------|--------|--------|-------|---|")
        for r in all_results:
            config = r.get("config", {})
            iterations = r.get("iterations", [])
            if not iterations or len(iterations) < 2:
                continue
            p99_vals = [it.get("p99_us", 0) for it in iterations]
            stats = calculate_stats(p99_vals)
            qos = config.get("qos_enabled", False)
            depth = config.get("iodepth", 0)
            label = f"{'QoS' if qos else 'bl'} qd={depth}"
            lines.append(
                f"| {label} "
                f"| {format_us(stats.mean)} "
                f"| +/-{format_us(stats.stddev)} "
                f"| [{format_us(stats.ci_low)}, {format_us(stats.ci_high)}] "
                f"| [{format_us(stats.min_val)}-{format_us(stats.max_val)}] "
                f"| {stats.n} |"
            )
        lines.append("")

    # Kernel scheduler
    has_ks = any(r.get("kernel_stats") for r in qos_results)
    if has_ks:
        lines.append("## Kernel QoS Scheduler")
        lines.append("")
        for r in qos_results:
            ks = r.get("kernel_stats")
            fairness = r.get("fairness", {})
            if not ks:
                continue
            config = r.get("config", {})
            depth = config.get("iodepth", 0)
            weight = config.get("qos_weight", 0)
            lines.append(f"### QoS qd={depth} w={weight}")
            lines.append("")

            hi_total = ks.get("high_dispatched", 0) + ks.get("wc_high_fallback", 0)
            norm_total = ks.get("normal_dispatched", 0) + ks.get("wc_normal_fallback", 0)
            total = hi_total + norm_total
            hi_pct = round(hi_total / total * 100) if total else 0

            wc_total = ks.get("wc_high_fallback", 0) + ks.get("wc_normal_fallback", 0)
            wc_pct = wc_total / total * 100 if total else 0

            regime = "demand-limited" if fairness.get("demand_limited") else "weight-limited"
            lines.append(f"- **Dispatch ratio**: {hi_pct}:{100 - hi_pct} ({regime})")
            lines.append(f"- **Work-conserving**: {wc_pct:.1f}% of dispatches")
            lines.append(f"- **Kicks**: {ks.get('kicks', 0)} / {ks.get('kicks', 0) + ks.get('kick_empty', 0)}")
            lines.append(f"- **Credits**: {ks.get('credit_refills', 0)} refills, {ks.get('sq_throttled', 0)} throttles")
            doorbells = ks.get("doorbells", 0)
            if doorbells > 0 and total > 0:
                batch_ratio = total / doorbells
                lines.append(f"- **Doorbells**: {doorbells} ({batch_ratio:.1f} dispatches/doorbell)")
            fair_str = fairness.get("fair", "?")
            actual = fairness.get("actual_hi_pct", 0)
            expected = fairness.get("effective_expected_hi_pct", 0)
            lines.append(f"- **Fairness**: {fair_str} (actual={actual:.1f}% vs expected={expected:.1f}%)")
            if fairness.get("normal_starved"):
                lines.append(f"- **WARNING**: Normal priority received zero dispatches (starvation)")
            lines.append("")

    # Summary
    if qos_results:
        lines.append("## Summary")
        lines.append("")
        p99_changes = [r.get("pct_change") for r in qos_results if r.get("pct_change") is not None]
        if p99_changes:
            lines.append(f"- **p99**: {min(p99_changes):+.1f}% to {max(p99_changes):+.1f}%")
        norm_changes = [r.get("norm_pct_change") for r in qos_results if r.get("norm_pct_change") is not None]
        if norm_changes:
            lines.append(f"- **Normal p99**: {min(norm_changes):+.1f}% to {max(norm_changes):+.1f}%")
        # CPU overhead
        cpu_deltas = []
        for r in qos_results:
            depth = r["config"]["iodepth"]
            bl = _find_bl_match(results, depth, r["config"].get("workload"))
            if bl and bl["metrics"].get("cpu_pct") and r["metrics"].get("cpu_pct"):
                cpu_deltas.append(r["metrics"]["cpu_pct"] - bl["metrics"]["cpu_pct"])
        if cpu_deltas:
            max_d = max(cpu_deltas)
            verdict = "PASS (<5pp)" if max_d < 5.0 else f"FAIL (max {max_d:.1f}pp)"
            lines.append(f"- **CPU overhead**: {min(cpu_deltas):+.2f}pp to {max_d:+.2f}pp -- {verdict}")
        lines.append("")

    lines.append("---")
    lines.append("Generated by nvme-qos-bench")
    return "\n".join(lines)


def generate_commit_comparison_report(
    base_runs: List,
    test_runs: List,
    comparisons: List[Dict],
    metric_key: str,
    base_commit: str,
    test_commit: str,
    base_branch: str,
    test_branch: str,
    base_dirty: bool,
    test_dirty: bool,
) -> str:
    """Generate commit comparison as Markdown."""
    lines = []

    lines.append("# NVMe QoS Commit Comparison")
    lines.append("")
    dirty_b = " (dirty)" if base_dirty else ""
    dirty_t = " (dirty)" if test_dirty else ""
    lines.append(f"**Base**: {base_commit[:7]} ({base_branch}{dirty_b}) -- {len(base_runs)} runs")
    lines.append(f"**Test**: {test_commit[:7]} ({test_branch}{dirty_t}) -- {len(test_runs)} runs")
    lines.append("")

    if base_dirty or test_dirty:
        lines.append("> Warning: dirty working tree -- results may not be reproducible")
        lines.append("")

    metric_label = metric_key.replace("_us", "").upper() if metric_key.endswith("_us") else metric_key

    lines.append(f"## {metric_label} Latency Comparison")
    lines.append("")
    lines.append("| Config | Base mean [CI] | Test mean [CI] | Change | Significant |")
    lines.append("|--------|----------------|----------------|--------|-------------|")

    for c in comparisons:
        bs = c["baseline"]
        ts = c["test"]
        pct = c["pct_change"]
        ttest = c["ttest"]

        base_str = f"{format_us(bs.mean)} [{format_us(bs.ci_low)}, {format_us(bs.ci_high)}]"
        test_str = f"{format_us(ts.mean)} [{format_us(ts.ci_low)}, {format_us(ts.ci_high)}]"
        change_str = f"{pct:+.1f}%"

        n_min = min(bs.n, ts.n)
        if n_min < 2:
            sig_str = "N/A"
        elif ttest.significant:
            sig_str = f"YES (d={abs(ttest.effect_size):.2f})"
        else:
            sig_str = f"NO (d={abs(ttest.effect_size):.2f})"

        # We don't have the config label stored on the comparison result,
        # so use a generic row
        lines.append(f"| n={bs.n}+{ts.n} | {base_str} | {test_str} | {change_str} | {sig_str} |")

    lines.append("")

    # Summary
    significant_regressions = [c for c in comparisons
                               if c["ttest"].significant and c["pct_change"] > 0]
    lines.append("## Summary")
    lines.append("")
    if significant_regressions:
        lines.append(f"**{len(significant_regressions)} statistically significant regression(s) detected.**")
    else:
        lines.append("No statistically significant regressions detected.")
    lines.append("")

    lines.append("---")
    lines.append("Generated by nvme-qos-bench")
    return "\n".join(lines)
