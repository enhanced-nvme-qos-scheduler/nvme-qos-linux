#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0

"""
NVMe QoS Benchmark - Benchmarking for the Linux NVMe QoS scheduler.
"""

import argparse
import json
import math
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add lib to path
sys.path.insert(0, str(Path(__file__).parent))

from lib import __version__
from lib.config import (
    BenchmarkConfig, UserPreferences, load_config,
    QUICK_CONFIG, DEFAULT_CONFIG, FULL_CONFIG, STRESS_CONFIG,
)
from lib.conditions import get_condition, list_conditions
from lib.device import (
    NVMeDevice, discover_nvme_devices, validate_device, check_qos_available,
)
from lib.system import (
    collect_system_info, check_fio_available, get_kernel_version, capture_dmesg,
)
from lib.fio_runner import FioRunner
from lib.metrics import extract_fio_metrics, get_primary_metrics, get_normal_metrics
from lib.analysis import calculate_stats, percentage_change, two_sample_ttest, compare_results, detect_degraded_iterations
from lib.output import (
    save_json, save_csv, flatten_result, CSV_FIELDNAMES,
    generate_markdown_report, generate_comparison_report,
    generate_analysis_report, generate_commit_comparison_report,
)
from lib.results_scanner import (
    scan_results_dir, filter_by_commit, get_config_tuples,
    pool_iterations_across_runs, load_iterations_for_config,
)
from lib.progress import (
    Progress, colored, print_header, print_warning,
    print_separator, print_summary, format_us, si_format,
)
from lib.kernel_stats import QoSKernelStats


def cmd_check(args) -> int:
    """Check system readiness for benchmarking."""
    print("Checking system readiness...")
    print()

    # Check root
    if os.geteuid() != 0:
        print(colored("✗ Not running as root (required for sysfs access)", "red"))
        print("  Run with: sudo ./nvme_qos_bench.py check")
        return 1
    print(colored("✓ Running as root", "green"))

    # Check fio
    if not check_fio_available():
        print(colored("✗ fio not found", "red"))
        print("  Install with: apt install fio")
        return 1
    print(colored("✓ fio available", "green"))

    # Check NVMe devices
    devices = discover_nvme_devices()
    if not devices:
        print(colored("✗ No NVMe devices found", "red"))
        return 1
    print(colored(f"✓ Found {len(devices)} NVMe device(s)/partition(s)", "green"))
    print()

    # List devices
    print("Available NVMe devices:")
    for i, dev in enumerate(devices, 1):
        mount_info = f"(mounted: {dev.mount_point})" if dev.mount_point else "(not mounted)"
        qos_status = ""
        if not dev.is_partition:
            qos_avail = check_qos_available(dev.controller)
            qos_status = colored(" [QoS]", "green") if qos_avail else colored(" [no QoS]", "yellow")
        print(f"  {i}. {dev.name:<12} {dev.model:<30} {dev.size_human:>8} {mount_info}{qos_status}")

    print()

    # Check QoS availability
    controllers = set(d.controller for d in devices)
    qos_available = any(check_qos_available(c) for c in controllers)

    if qos_available:
        print(colored("✓ NVMe QoS available (CONFIG_NVME_QOS=y)", "green"))
    else:
        print(colored("⚠ NVMe QoS not available (CONFIG_NVME_QOS not enabled)", "yellow"))
        print("  Rebuild kernel with CONFIG_NVME_QOS=y to enable QoS testing")
        print("  Baseline benchmarks will still run")

    return 0


def select_device(args, prefs: UserPreferences) -> Optional[str]:
    """Select device through CLI arg, saved preference, or interactive prompt."""
    # CLI override
    if args.device:
        valid, msg = validate_device(args.device)
        if not valid:
            print(colored(f"Error: {msg}", "red"), file=sys.stderr)
            return None
        return args.device.replace("/dev/", "")

    # Reset saved preference if requested
    if args.reset_device:
        prefs.clear_device()
        print("Cleared saved device preference.", file=sys.stderr)

    # Use saved preference if available
    if prefs.device:
        valid, msg = validate_device(prefs.device)
        if valid:
            print(f"Using saved device: /dev/{prefs.device}", file=sys.stderr)
            return prefs.device
        else:
            print(f"Saved device no longer valid: {msg}", file=sys.stderr)
            prefs.clear_device()

    # Interactive selection
    devices = discover_nvme_devices()
    if not devices:
        print(colored("Error: No NVMe devices found", "red"), file=sys.stderr)
        return None

    print()
    print("Available NVMe devices:")
    for i, dev in enumerate(devices, 1):
        mount_info = f"(mounted: {dev.mount_point})" if dev.mount_point else "(not mounted)"
        print(f"  {i}. {dev.name:<12} {dev.model:<30} {mount_info}")

    print()
    try:
        choice = input(f"Select device [1-{len(devices)}]: ").strip()
        idx = int(choice) - 1
        if idx < 0 or idx >= len(devices):
            print(colored("Invalid selection", "red"), file=sys.stderr)
            return None
    except (ValueError, EOFError, KeyboardInterrupt):
        print()
        return None

    selected = devices[idx]

    # Confirmation for destructive access
    print()
    print(colored(f"Benchmarking will OVERWRITE data on /dev/{selected.name}", "yellow"))
    if selected.mount_point:
        print(colored(f"  Device is mounted at {selected.mount_point}!", "red"))

    try:
        confirm = input("  Type 'yes' to confirm: ").strip()
        if confirm.lower() != 'yes':
            print("Aborted.", file=sys.stderr)
            return None
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    # Save preference
    prefs.set_device(selected.name)
    print()
    print(f"Device preference saved. Future runs will use /dev/{selected.name}")
    print("(use --reset-device to change)")

    return selected.name


def _run_drift_probe(runner: FioRunner, runtime: int = 5) -> Optional[float]:
    """Run a short 4K randread probe and return p50 in microseconds."""
    success, fio_data = runner.run_high_priority(
        iodepth=1, numjobs=1, runtime=runtime, ramp_time=1, iteration=999,
    )
    if success and fio_data:
        metrics = extract_fio_metrics(fio_data)
        primary = get_primary_metrics(metrics)
        return primary.get("p50_us")
    return None


def _format_summary_line(label: str, avg_metrics: Dict, elapsed: float,
                         pct_change: float = None, degraded: List[int] = None) -> str:
    """Format a summary line for baseline or QoS results."""
    change_str = ""
    if pct_change is not None:
        change_str = f"  {pct_change:+.1f}%"

    degraded_str = ""
    if degraded:
        degraded_str = f"  ({len(degraded)} degraded)"

    util_pct = avg_metrics.get('io_util_pct', 0)
    # Label padded to 22 chars so ": " brings total prefix to 24, matching continuation indent
    return (
        f"{label:<22}: "
        f"p50={format_us(avg_metrics['p50_us']):>8} "
        f"p90={format_us(avg_metrics['p90_us']):>8} "
        f"p99={format_us(avg_metrics['p99_us']):>8} "
        f"p999={format_us(avg_metrics['p999_us']):>8} "
        f"iops={si_format(avg_metrics['iops']):>8} "
        f"cpu={avg_metrics['cpu_pct']:5.1f}% "
        f"util={util_pct:5.1f}% "
        f"[{elapsed:.1f}s]{change_str}{degraded_str}"
    )


def _color_change(pct_change: float) -> str:
    """Color a percentage change string for terminal output."""
    change_str = f"  {pct_change:+.1f}%"
    if pct_change <= -30:
        return colored(change_str, "green")
    elif pct_change <= -10:
        return colored(change_str, "yellow")
    elif pct_change > 5:
        return colored(change_str, "red")
    return change_str


def _store_baseline_results(
    baseline_results: List[Dict],
    baseline_normal: List[Dict],
    results: Dict,
    depth: int,
    weight: int,
    workload_type: Optional[str],
    buf_tag: str,
    elapsed: float,
) -> Optional[str]:
    """Aggregate baseline iterations and store in results dict. Returns summary line for progress.finish()."""
    if not baseline_results:
        return None

    degraded = detect_degraded_iterations(baseline_results)
    avg_bl = _aggregate_iterations(baseline_results)
    avg_bl_norm = _aggregate_iterations(baseline_normal) if baseline_normal else {}

    bl_config = {
        "qos_enabled": False,
        "iodepth": depth,
        "paired_weight": weight,
    }
    if workload_type:
        bl_config["workload"] = workload_type

    result = {
        "config": bl_config,
        "metrics": avg_bl,
        "iterations": baseline_results,
    }
    if avg_bl_norm:
        result["normal_metrics"] = avg_bl_norm
    if degraded:
        result["degraded_iterations"] = degraded
    results["baseline"].append(result)
    results["all"].append(result)

    degraded_list = degraded if degraded else None
    bl_label = f"baseline {buf_tag}qd={depth:<3}"
    return _format_summary_line(bl_label, avg_bl, elapsed, degraded=degraded_list)


def _store_qos_results(
    qos_results: List[Dict],
    qos_normal: List[Dict],
    results: Dict,
    depth: int,
    weight: int,
    workload_type: Optional[str],
    config: BenchmarkConfig,
    kernel_stats: Optional[QoSKernelStats],
    ks_available: bool,
    buf_tag: str,
    elapsed: float,
) -> None:
    """Aggregate QoS iterations, compute changes, collect kernel stats, and store with summary output."""
    if not qos_results:
        return

    degraded = detect_degraded_iterations(qos_results)
    avg_qos = _aggregate_iterations(qos_results)
    avg_qos_norm = _aggregate_iterations(qos_normal) if qos_normal else {}

    # Collect kernel counters (accumulated across QoS iterations only)
    ks_counters = None
    ks_fairness = None
    if ks_available:
        ks_counters = kernel_stats.read_aggregate()
        if ks_counters is not None:
            ks_fairness = QoSKernelStats.validate_fairness(
                ks_counters, weight)

    # Calculate change vs paired baseline
    pct_change = None
    norm_pct_change = None
    baseline_match = next(
        (b for b in results["baseline"]
         if b["config"]["iodepth"] == depth
         and b["config"].get("paired_weight") == weight
         and b["config"].get("workload") == workload_type),
        None
    )
    if baseline_match:
        pct_change = percentage_change(
            baseline_match["metrics"]["p99_us"],
            avg_qos["p99_us"]
        )
        bl_norm = baseline_match.get("normal_metrics", {})
        if bl_norm and avg_qos_norm and bl_norm.get("p99_us"):
            norm_pct_change = percentage_change(
                bl_norm["p99_us"],
                avg_qos_norm.get("p99_us", 0)
            )

    qos_config = {
        "qos_enabled": True,
        "qos_weight": weight,
        "qos_max_depth": config.qos_max_depth,
        "iodepth": depth,
    }
    if workload_type:
        qos_config["workload"] = workload_type

    result = {
        "config": qos_config,
        "metrics": avg_qos,
        "pct_change": pct_change,
        "norm_pct_change": norm_pct_change,
        "iterations": qos_results,
    }
    if avg_qos_norm:
        result["normal_metrics"] = avg_qos_norm
    if ks_counters:
        result["kernel_stats"] = ks_counters
    if ks_fairness:
        result["fairness"] = ks_fairness
    if degraded:
        result["degraded_iterations"] = degraded
    results["qos"].append(result)
    results["all"].append(result)

    # Print QoS summary line
    change_str = _color_change(pct_change) if pct_change is not None else ""
    degraded_str = f"  ({len(degraded)} degraded)" if degraded else ""
    util_pct = avg_qos.get('io_util_pct', 0)
    md_tag = f" md={config.qos_max_depth}" if config.qos_max_depth else ""
    qos_label = f"qos {buf_tag}w={weight:<2}{md_tag} qd={depth:<3}"
    line1 = (
        f"{qos_label:<22}: "
        f"p50={format_us(avg_qos['p50_us']):>8} "
        f"p90={format_us(avg_qos['p90_us']):>8} "
        f"p99={format_us(avg_qos['p99_us']):>8} "
        f"p999={format_us(avg_qos['p999_us']):>8} "
        f"iops={si_format(avg_qos['iops']):>8} "
        f"cpu={avg_qos['cpu_pct']:5.1f}% "
        f"util={util_pct:5.1f}% "
        f"[{elapsed:.1f}s]{change_str}{degraded_str}"
    )
    print(f"\r\033[K{line1}", file=sys.stderr)

    # Kernel stats summary line
    if ks_counters and ks_fairness:
        ks_line = QoSKernelStats.format_summary(
            ks_counters, ks_fairness)
        print(f"{'':>24}{ks_line}", file=sys.stderr)

    # Normal-priority summary line
    if avg_qos_norm:
        norm_change_str = ""
        if norm_pct_change is not None:
            norm_change_str = f" ({norm_pct_change:+.1f}%)"
        norm_line = (
            f"{'':>24}NORM "
            f"p99={format_us(avg_qos_norm.get('p99_us', 0)):>8}"
            f"{norm_change_str} "
            f"iops={si_format(avg_qos_norm.get('iops', 0)):>8} "
            f"bw={avg_qos_norm.get('bw_mbps', 0):>6.0f}MB/s"
        )
        print(norm_line, file=sys.stderr)


def _run_interleaved_depth(
    runner: FioRunner, device: NVMeDevice, config: BenchmarkConfig,
    depth: int, weight: int, cpus_allowed: Optional[str],
    kernel_stats: Optional[QoSKernelStats],
    results: Dict, run_fn, label_prefix: str = "",
    workload_type: Optional[str] = None,
) -> None:
    """Run interleaved baseline+QoS iterations for a single depth.

    Baseline and QoS iterations alternate back-to-back within each pair,
    ensuring both experience identical drive state. Cooldown (if configured)
    only happens between pairs.
    """
    run_baseline = config.run_baseline
    run_qos = config.run_qos and device.qos_available
    interleaved = run_baseline and run_qos

    baseline_results = []
    baseline_normal = []
    qos_results = []
    qos_normal = []

    # Reset kernel counters once before the interleaved loop
    ks_available = kernel_stats and kernel_stats.available
    if ks_available:
        try:
            kernel_stats.reset()
        except (PermissionError, OSError):
            ks_available = False

    buf_tag = "buf " if workload_type == "buffered" else ""
    progress_label = f"{buf_tag}qd={depth}" if interleaved else (
        f"{'baseline ' if run_baseline else 'qos '}{buf_tag}w={weight} qd={depth}"
    )
    progress = Progress(progress_label, config.iterations)

    for iteration in range(config.iterations):
        progress.set(iteration)

        # --- Baseline iteration ---
        if run_baseline:
            if device.qos_available:
                device.set_qos_enabled(False)

            success, fio_data = run_fn(
                high_iodepth=depth,
                high_numjobs=config.high_numjobs,
                normal_iodepth=depth,
                normal_numjobs=config.normal_numjobs,
                runtime=config.runtime,
                ramp_time=config.ramp_time,
                iteration=iteration,
                label=f"{label_prefix}baseline_i{iteration}",
                cpus_allowed=cpus_allowed,
                workload_params=config.workload_params,
            )

            if success and fio_data:
                metrics = extract_fio_metrics(fio_data)
                primary = get_primary_metrics(metrics)
                baseline_results.append(primary)
                norm = get_normal_metrics(metrics)
                if norm:
                    baseline_normal.append(norm)

        # --- QoS iteration (back-to-back, no gap) ---
        if run_qos:
            device.set_qos_enabled(True)

            success, fio_data = run_fn(
                high_iodepth=depth,
                high_numjobs=config.high_numjobs,
                normal_iodepth=depth,
                normal_numjobs=config.normal_numjobs,
                runtime=config.runtime,
                ramp_time=config.ramp_time,
                iteration=iteration,
                label=f"{label_prefix}qos_w{weight}_i{iteration}",
                cpus_allowed=cpus_allowed,
                workload_params=config.workload_params,
            )

            if success and fio_data:
                metrics = extract_fio_metrics(fio_data)
                primary = get_primary_metrics(metrics)
                qos_results.append(primary)
                norm = get_normal_metrics(metrics)
                if norm:
                    qos_normal.append(norm)

        # --- Cooldown AFTER the pair, before next pair ---
        if config.iter_cooldown and iteration < config.iterations - 1:
            time.sleep(config.iter_cooldown)

    elapsed = progress.elapsed()

    # --- Post-loop: aggregate and store results ---
    bl_line = _store_baseline_results(
        baseline_results, baseline_normal, results,
        depth, weight, workload_type, buf_tag, elapsed
    )
    if bl_line:
        progress.finish(bl_line)

    _store_qos_results(
        qos_results, qos_normal, results,
        depth, weight, workload_type, config,
        kernel_stats, ks_available, buf_tag, elapsed
    )


def run_benchmark_suite(
    device: NVMeDevice,
    config: BenchmarkConfig,
    output_dir: Path,
    compare: bool = False,
    kernel_stats: Optional[QoSKernelStats] = None,
) -> Dict[str, Any]:
    """Run the complete benchmark suite with interleaved baseline/QoS iterations.

    Instead of running all baseline iterations first then all QoS iterations,
    this interleaves them: for each iteration, baseline runs first, then QoS
    immediately after. Both experience identical drive state per pair,
    eliminating SLC cache exhaustion bias.
    """
    results = {
        "baseline": [],
        "qos": [],
        "all": [],
    }
    drift_probes = []
    weight_order = None

    runner = FioRunner(device.path, output_dir)

    # Detect HW queue count and compute cpus_allowed
    hw_queues = device.get_hw_queue_count()
    active_queues = config.max_queues if config.max_queues else hw_queues
    total_jobs = config.high_numjobs + config.normal_numjobs
    jobs_per_queue = total_jobs / active_queues if active_queues else 0

    cpus_allowed = None
    if config.max_queues:
        cpus_allowed = f"0-{config.max_queues - 1}" if config.max_queues > 1 else "0"
        print(f"CPU pinning: fio jobs pinned to CPUs {cpus_allowed} "
              f"({config.max_queues} of {hw_queues} HW queues, "
              f"{jobs_per_queue:.1f} jobs/queue)",
              file=sys.stderr)
    elif jobs_per_queue < 2:
        print_warning(
            f"Low contention: {total_jobs} fio jobs across {hw_queues} HW queues "
            f"({jobs_per_queue:.1f} jobs/queue). "
            f"WRR may not engage. Use --max-depth 16 to force host-side queuing."
        )

    # Save initial device state
    device.save_state()

    try:
        # Apply namespace policy override if set by condition profile
        if config.namespace_policy and device.qos_available:
            device.set_qos_policy(config.namespace_policy)

        # Randomize weight order to avoid systematic bias
        weights = list(config.weights)
        if len(weights) > 1:
            random.shuffle(weights)
            print(f"Weight order (randomized): {weights}", file=sys.stderr)
        weight_order = weights

        # Pre-test drift probe
        pre_test_p50 = _run_drift_probe(runner)
        if pre_test_p50 is not None:
            drift_probes.append({"phase": "pre_test", "p50_us": pre_test_p50})

        print_separator()

        first_weight = True
        for weight in weights:
            # Drift probe between weight phases
            if not first_weight and pre_test_p50 is not None:
                probe_p50 = _run_drift_probe(runner)
                if probe_p50 is not None:
                    drift_probes.append({"phase": f"before_w{weight}", "p50_us": probe_p50})
                    if probe_p50 / pre_test_p50 > 3.0:
                        print(colored(
                            f"  Drive state drift detected "
                            f"(p50: {pre_test_p50:.0f}us -> {probe_p50:.0f}us); "
                            f"cross-weight comparisons may be invalid",
                            "yellow"), file=sys.stderr)
            first_weight = False

            # Set weight and max depth once before the depth loop
            if device.qos_available:
                device.set_qos_weight(weight)
                device.set_qos_max_depth(config.qos_max_depth)

            for depth in config.depths:
                if depth < 16 and len(config.depths) > 3:
                    continue

                # Interleaved direct I/O
                _run_interleaved_depth(
                    runner, device, config, depth, weight, cpus_allowed,
                    kernel_stats, results,
                    run_fn=runner.run_mixed_workload,
                )

                # Interleaved buffered I/O (opt-in)
                if config.run_buffered:
                    _run_interleaved_depth(
                        runner, device, config, depth, weight, cpus_allowed,
                        kernel_stats, results,
                        run_fn=runner.run_buffered_workload,
                        label_prefix="buf_",
                        workload_type="buffered",
                    )

        # Post-test drift probe
        post_test_p50 = _run_drift_probe(runner)
        if post_test_p50 is not None:
            drift_probes.append({"phase": "post_test", "p50_us": post_test_p50})
            if pre_test_p50 and post_test_p50 / pre_test_p50 > 3.0:
                print(colored(
                    f"  Drive state drift detected across test run "
                    f"(p50: {pre_test_p50:.0f}us -> {post_test_p50:.0f}us); "
                    f"results may be affected",
                    "yellow"), file=sys.stderr)

        # CPU overhead tests (opt-in)
        if config.run_cpu_overhead and device.qos_available:
            print_separator()
            results["cpu_overhead"] = []

            for depth in [1, 4]:
                for qos_on in [False, True]:
                    device.set_qos_enabled(qos_on)
                    label = f"qos={'on' if qos_on else 'off'}"

                    depth_results = []
                    progress = Progress(f"cpu {label} qd={depth}", config.iterations)

                    for iteration in range(config.iterations):
                        progress.set(iteration)

                        success, fio_data = runner.run_high_priority(
                            iodepth=depth,
                            numjobs=1,
                            runtime=config.runtime,
                            ramp_time=config.ramp_time,
                            iteration=iteration,
                        )

                        if success and fio_data:
                            metrics = extract_fio_metrics(fio_data)
                            primary = get_primary_metrics(metrics)
                            depth_results.append(primary)

                    if depth_results:
                        avg = _aggregate_iterations(depth_results)
                        elapsed = progress.elapsed()
                        result = {
                            "config": {
                                "qos_enabled": qos_on,
                                "iodepth": depth,
                                "phase": "cpu_overhead",
                            },
                            "metrics": avg,
                            "iterations": depth_results,
                        }
                        results["cpu_overhead"].append(result)
                        progress.finish(
                            f"cpu {label} qd={depth:<2}: "
                            f"p99={format_us(avg['p99_us']):>8} "
                            f"iops={si_format(avg['iops']):>8} "
                            f"cpu={avg['cpu_pct']:5.1f}% "
                            f"[{elapsed:.1f}s]"
                        )

            # Print deltas
            for depth in [1, 4]:
                off_match = next(
                    (r for r in results["cpu_overhead"]
                     if r["config"]["iodepth"] == depth and not r["config"]["qos_enabled"]),
                    None
                )
                on_match = next(
                    (r for r in results["cpu_overhead"]
                     if r["config"]["iodepth"] == depth and r["config"]["qos_enabled"]),
                    None
                )
                if off_match and on_match:
                    p99_pct = percentage_change(
                        off_match["metrics"]["p99_us"],
                        on_match["metrics"]["p99_us"]
                    )
                    iops_pct = percentage_change(
                        off_match["metrics"]["iops"],
                        on_match["metrics"]["iops"]
                    )
                    print(
                        f"  qd={depth} delta: p99={p99_pct:+.1f}% iops={iops_pct:+.1f}%",
                        file=sys.stderr,
                    )

        # Isolation tests (opt-in): single-priority with QoS on
        if config.run_isolation and device.qos_available:
            print_separator()
            results["isolation"] = []
            device.set_qos_enabled(True)
            device.set_qos_weight(config.weights[0] if config.weights else 9)

            for prio_label, run_fn, default_numjobs in [
                ("high", runner.run_high_priority, config.high_numjobs),
                ("normal", runner.run_normal_priority, config.normal_numjobs),
            ]:
                for depth in config.depths:
                    if depth < 16 and len(config.depths) > 3:
                        continue

                    depth_results = []
                    progress = Progress(f"iso {prio_label} qd={depth}", config.iterations)

                    for iteration in range(config.iterations):
                        progress.set(iteration)

                        success, fio_data = run_fn(
                            iodepth=depth,
                            numjobs=default_numjobs,
                            runtime=config.runtime,
                            ramp_time=config.ramp_time,
                            iteration=iteration,
                        )

                        if success and fio_data:
                            metrics = extract_fio_metrics(fio_data)
                            primary = get_primary_metrics(metrics)
                            depth_results.append(primary)

                    if depth_results:
                        avg = _aggregate_iterations(depth_results)
                        elapsed = progress.elapsed()
                        result = {
                            "config": {
                                "qos_enabled": True,
                                "iodepth": depth,
                                "phase": "isolation",
                                "priority": prio_label,
                            },
                            "metrics": avg,
                            "iterations": depth_results,
                        }
                        results["isolation"].append(result)

                        util_pct = avg.get('io_util_pct', 0)
                        progress.finish(
                            f"iso {prio_label:<6} qd={depth:<3}: "
                            f"p99={format_us(avg['p99_us']):>8} "
                            f"iops={si_format(avg['iops']):>8} "
                            f"cpu={avg['cpu_pct']:5.1f}% "
                            f"util={util_pct:5.1f}% "
                            f"[{elapsed:.1f}s]"
                        )

    finally:
        # Restore device state
        device.restore_state()

    # Store drift probes and weight order in results metadata
    if drift_probes:
        results["drift_probes"] = drift_probes
    if weight_order is not None:
        results["weight_order"] = weight_order

    return results


def _aggregate_iterations(iterations: List[Dict]) -> Dict[str, float]:
    """Aggregate metrics across iterations using median (robust to outliers)."""
    if not iterations:
        return {}

    keys = iterations[0].keys()
    result = {}
    for key in keys:
        values = [it[key] for it in iterations if key in it]
        if values:
            sorted_v = sorted(values)
            n = len(sorted_v)
            if n % 2 == 0:
                result[key] = (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2
            else:
                result[key] = sorted_v[n // 2]
    return result


BASELINE_PASS_THRESHOLD = 2.0   # p99 regression < 2% = PASS

BASELINE_DEPTHS = [1, 4]
BASELINE_ITERATIONS = 5
BASELINE_RUNTIME = 10
BASELINE_RAMP = 2


def cmd_run_baseline(device: NVMeDevice, args) -> int:
    """Quick overhead check: same single-job workload with QoS off vs on.

    Runs 4K random reads at QD1 and QD4 to confirm the QoS scheduler adds
    no measurable overhead when there is no contention to arbitrate.
    """
    if not device.qos_available:
        print(colored("Error: QoS not available (CONFIG_NVME_QOS not enabled)", "red"),
              file=sys.stderr)
        print("  Baseline overhead check requires QoS sysfs controls", file=sys.stderr)
        return 1

    # Setup output directory
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = Path(args.output) / f"baseline_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    runner = FioRunner(device.path, output_dir)
    kernel = get_kernel_version()

    print_header(device.name, kernel, device.qos_available)
    print("Baseline overhead check: QoS off vs on (single-job, no contention)", file=sys.stderr)
    print_separator()

    n_iters = getattr(args, 'iterations', None) or BASELINE_ITERATIONS
    runtime = getattr(args, 'runtime', None) or BASELINE_RUNTIME

    device.save_state()
    start_time = time.time()

    off_results = {}   # depth -> avg_metrics
    on_results = {}    # depth -> avg_metrics

    try:
        # Phase 1: QoS disabled
        device.set_qos_enabled(False)

        for depth in BASELINE_DEPTHS:
            depth_iters = []
            progress = Progress(f"qos=off qd={depth}", n_iters)

            for iteration in range(n_iters):
                progress.set(iteration)

                success, fio_data = runner.run_high_priority(
                    iodepth=depth,
                    numjobs=1,
                    runtime=runtime,
                    ramp_time=BASELINE_RAMP,
                    iteration=iteration,
                )

                if success and fio_data:
                    metrics = extract_fio_metrics(fio_data)
                    primary = get_primary_metrics(metrics)
                    depth_iters.append(primary)

            if depth_iters:
                avg = _aggregate_iterations(depth_iters)
                off_results[depth] = avg
                elapsed = progress.elapsed()
                progress.finish(
                    f"qos=off  qd={depth}: "
                    f"p50={format_us(avg['p50_us']):>8} "
                    f"p99={format_us(avg['p99_us']):>8} "
                    f"iops={si_format(avg['iops']):>8} "
                    f"cpu={avg['cpu_pct']:5.1f}% "
                    f"[{elapsed:.1f}s]"
                )

        # Phase 2: QoS enabled
        device.set_qos_enabled(True)

        for depth in BASELINE_DEPTHS:
            depth_iters = []
            progress = Progress(f"qos=on  qd={depth}", n_iters)

            for iteration in range(n_iters):
                progress.set(iteration)

                success, fio_data = runner.run_high_priority(
                    iodepth=depth,
                    numjobs=1,
                    runtime=runtime,
                    ramp_time=BASELINE_RAMP,
                    iteration=iteration,
                )

                if success and fio_data:
                    metrics = extract_fio_metrics(fio_data)
                    primary = get_primary_metrics(metrics)
                    depth_iters.append(primary)

            if depth_iters:
                avg = _aggregate_iterations(depth_iters)
                on_results[depth] = avg
                elapsed = progress.elapsed()
                progress.finish(
                    f"qos=on   qd={depth}: "
                    f"p50={format_us(avg['p50_us']):>8} "
                    f"p99={format_us(avg['p99_us']):>8} "
                    f"iops={si_format(avg['iops']):>8} "
                    f"cpu={avg['cpu_pct']:5.1f}% "
                    f"[{elapsed:.1f}s]"
                )

    finally:
        device.restore_state()

    # Comparison and verdict
    print_separator()
    print(f"{'':>15}{'p50':>7}  {'p99':>7}  {'IOPS':>7}  {'CPU':>7}", file=sys.stderr)

    all_pass = True
    comparisons = []

    for depth in BASELINE_DEPTHS:
        if depth not in off_results or depth not in on_results:
            continue

        off = off_results[depth]
        on = on_results[depth]

        p50_pct = percentage_change(off["p50_us"], on["p50_us"])
        p99_pct = percentage_change(off["p99_us"], on["p99_us"])
        iops_pct = percentage_change(off["iops"], on["iops"])
        cpu_pct = percentage_change(off["cpu_pct"], on["cpu_pct"]) if off["cpu_pct"] > 0 else 0

        passed = p99_pct < BASELINE_PASS_THRESHOLD
        if not passed:
            all_pass = False

        def _color_delta(val: float, invert: bool = False) -> str:
            """Color a % delta, pre-padded to 7 chars. Latency: positive=bad. IOPS (invert): positive=good."""
            text = f"{val:+.1f}%"
            padded = f"{text:>7}"
            bad = val > 0.5 if not invert else val < -0.5
            good = val < -0.5 if not invert else val > 0.5
            if bad:
                return colored(padded, "red")
            elif good:
                return colored(padded, "green")
            return padded

        line = (
            f"  qd={depth:<2} delta "
            f"{_color_delta(p50_pct)}  "
            f"{_color_delta(p99_pct)}  "
            f"{_color_delta(iops_pct, invert=True)}  "
            f"{_color_delta(cpu_pct)}"
        )
        print(line, file=sys.stderr)

        comparisons.append({
            "depth": depth,
            "off": off,
            "on": on,
            "p50_pct": p50_pct,
            "p99_pct": p99_pct,
            "iops_pct": iops_pct,
            "cpu_pct": cpu_pct,
            "pass": passed,
        })

    print(file=sys.stderr)

    total_elapsed = time.time() - start_time
    minutes, seconds = divmod(total_elapsed, 60)

    if all_pass:
        verdict = colored("PASS", "green")
        detail = f"p99 regression < {BASELINE_PASS_THRESHOLD}% at all depths"
    else:
        verdict = colored("FAIL", "red")
        failed_depths = [c["depth"] for c in comparisons if not c["pass"]]
        detail = f"p99 regression >= {BASELINE_PASS_THRESHOLD}% at QD {', '.join(str(d) for d in failed_depths)}"

    print(f"Verdict: {verdict} — {detail}", file=sys.stderr)

    if minutes >= 1:
        print(f"Total runtime: {int(minutes)}m {seconds:.1f}s", file=sys.stderr)
    else:
        print(f"Total runtime: {total_elapsed:.1f}s", file=sys.stderr)

    # Save results
    baseline_data = {
        "type": "baseline_overhead",
        "threshold_pct": BASELINE_PASS_THRESHOLD,
        "pass": all_pass,
        "config": {
            "depths": BASELINE_DEPTHS,
            "iterations": n_iters,
            "runtime": runtime,
            "ramp_time": BASELINE_RAMP,
            "workload": "4K randread, single job",
        },
        "comparisons": comparisons,
    }
    save_json(baseline_data, output_dir / "baseline.json")

    print(f"Results saved to: {output_dir}", file=sys.stderr)

    return 0 if all_pass else 1


def _validate_policy_override(
    device: NVMeDevice,
    runner: FioRunner,
    kernel_stats: QoSKernelStats,
) -> tuple[bool, Dict[str, Any]]:
    """Test 1: Verify force_high and force_normal policies reclassify traffic."""
    results = {}

    # force_high: BE traffic should be reclassified as high
    device.set_qos_enabled(True)
    device.set_qos_weight(9)
    device.set_qos_policy("force_high")
    kernel_stats.reset()

    success, fio_data = runner.run_normal_priority(
        iodepth=16, numjobs=4, runtime=10, ramp_time=3, iteration=0,
    )
    if not success:
        return False, {"error": "force_high fio run failed"}

    counters = kernel_stats.read_aggregate()
    if counters is None:
        return False, {"error": "kernel stats unavailable after force_high run"}

    hi_enq = counters.get("high_enqueued", 0)
    norm_enq = counters.get("normal_enqueued", 0)
    total_enq = hi_enq + norm_enq

    if total_enq > 0:
        hi_pct = hi_enq / total_enq * 100
    else:
        hi_pct = 0.0

    force_high_pass = hi_pct >= 95.0
    results["force_high"] = {
        "high_enqueued": hi_enq,
        "normal_enqueued": norm_enq,
        "high_pct": round(hi_pct, 1),
        "pass": force_high_pass,
    }

    status = colored("PASS", "green") if force_high_pass else colored("FAIL", "red")
    print(f"  force_high + BE traffic:  {hi_pct:>5.1f}% high enqueues (>= 95%)  {status}", file=sys.stderr)

    # force_normal: RT traffic should be reclassified as normal
    device.set_qos_policy("force_normal")
    kernel_stats.reset()

    success, fio_data = runner.run_high_priority(
        iodepth=16, numjobs=1, runtime=10, ramp_time=3, iteration=0,
    )
    if not success:
        return False, {"error": "force_normal fio run failed"}

    counters = kernel_stats.read_aggregate()
    if counters is None:
        return False, {"error": "kernel stats unavailable after force_normal run"}

    hi_enq = counters.get("high_enqueued", 0)
    norm_enq = counters.get("normal_enqueued", 0)
    total_enq = hi_enq + norm_enq

    if total_enq > 0:
        norm_pct = norm_enq / total_enq * 100
    else:
        norm_pct = 0.0

    force_normal_pass = norm_pct >= 95.0
    results["force_normal"] = {
        "high_enqueued": hi_enq,
        "normal_enqueued": norm_enq,
        "normal_pct": round(norm_pct, 1),
        "pass": force_normal_pass,
    }

    status = colored("PASS", "green") if force_normal_pass else colored("FAIL", "red")
    print(f"  force_normal + RT traffic: {norm_pct:>5.1f}% normal enqueues (>= 95%) {status}", file=sys.stderr)

    device.set_qos_policy("default")
    return force_high_pass and force_normal_pass, results


def _validate_enable_disable(
    device: NVMeDevice,
    runner: FioRunner,
    kernel_stats: QoSKernelStats,
) -> tuple[bool, Dict[str, Any]]:
    """Test 2: Verify QoS can be toggled without crashes and counters respond."""
    results = {}

    # Phase 1: QoS disabled
    device.set_qos_enabled(False)
    success, fio_data = runner.run_high_priority(
        iodepth=4, numjobs=1, runtime=10, ramp_time=3, iteration=0,
    )
    if not success:
        return False, {"error": "QoS-off fio run failed"}

    off_metrics = extract_fio_metrics(fio_data)
    off_primary = get_primary_metrics(off_metrics)
    off_iops = off_primary.get("iops", 0)
    off_pass = success and off_iops > 0
    results["off"] = {"iops": off_iops, "pass": off_pass}

    status = colored("PASS", "green") if off_pass else colored("FAIL", "red")
    print(f"  QoS off:  {si_format(off_iops):>8} IOPS                                       {status}", file=sys.stderr)

    # Phase 2: QoS enabled
    device.set_qos_enabled(True)
    kernel_stats.reset()

    success, fio_data = runner.run_high_priority(
        iodepth=4, numjobs=1, runtime=10, ramp_time=3, iteration=0,
    )
    if not success:
        return False, {"error": "QoS-on fio run failed"}

    on_metrics = extract_fio_metrics(fio_data)
    on_primary = get_primary_metrics(on_metrics)
    on_iops = on_primary.get("iops", 0)
    counters = kernel_stats.read_aggregate()
    if counters is None:
        return False, {"error": "kernel stats unavailable after QoS-on run"}

    hi_enq = counters.get("high_enqueued", 0)
    on_pass = success and on_iops > 0 and hi_enq > 0
    results["on"] = {"iops": on_iops, "high_enqueued": hi_enq, "pass": on_pass}

    status = colored("PASS", "green") if on_pass else colored("FAIL", "red")
    print(f"  QoS on:   {si_format(on_iops):>8} IOPS, {si_format(hi_enq):>6} high_enqueued         {status}", file=sys.stderr)

    # Phase 3: QoS disabled again (round-trip)
    device.set_qos_enabled(False)
    success, fio_data = runner.run_high_priority(
        iodepth=4, numjobs=1, runtime=10, ramp_time=3, iteration=0,
    )
    if not success:
        return False, {"error": "QoS-off round-trip fio run failed"}

    off2_metrics = extract_fio_metrics(fio_data)
    off2_primary = get_primary_metrics(off2_metrics)
    off2_iops = off2_primary.get("iops", 0)
    off2_pass = success and off2_iops > 0
    results["off_roundtrip"] = {"iops": off2_iops, "pass": off2_pass}

    status = colored("PASS", "green") if off2_pass else colored("FAIL", "red")
    print(f"  QoS off:  {si_format(off2_iops):>8} IOPS                                       {status}", file=sys.stderr)

    return off_pass and on_pass and off2_pass, results


def _validate_classification(
    device: NVMeDevice,
    runner: FioRunner,
    kernel_stats: QoSKernelStats,
) -> tuple[bool, Dict[str, Any]]:
    """Test 3: Verify default policy classifies RT as high, BE as normal."""
    results = {}

    device.set_qos_enabled(True)
    device.set_qos_policy("default")

    # RT-only: should all go to high
    kernel_stats.reset()
    success, fio_data = runner.run_high_priority(
        iodepth=16, numjobs=1, runtime=10, ramp_time=3, iteration=0,
    )
    if not success:
        return False, {"error": "RT-only fio run failed"}

    counters = kernel_stats.read_aggregate()
    if counters is None:
        return False, {"error": "kernel stats unavailable after RT-only run"}

    hi_enq = counters.get("high_enqueued", 0)
    norm_enq = counters.get("normal_enqueued", 0)
    total_enq = hi_enq + norm_enq
    rt_hi_pct = (hi_enq / total_enq * 100) if total_enq > 0 else 0.0

    rt_pass = hi_enq > 0 and norm_enq == 0
    results["rt_only"] = {
        "high_enqueued": hi_enq,
        "normal_enqueued": norm_enq,
        "high_pct": round(rt_hi_pct, 1),
        "pass": rt_pass,
    }

    status = colored("PASS", "green") if rt_pass else colored("FAIL", "red")
    print(f"  RT-only:  {rt_hi_pct:>5.0f}% high enqueues                          {status}", file=sys.stderr)

    # BE-only: should all go to normal
    kernel_stats.reset()
    success, fio_data = runner.run_normal_priority(
        iodepth=16, numjobs=4, runtime=10, ramp_time=3, iteration=0,
    )
    if not success:
        return False, {"error": "BE-only fio run failed"}

    counters = kernel_stats.read_aggregate()
    if counters is None:
        return False, {"error": "kernel stats unavailable after BE-only run"}

    hi_enq = counters.get("high_enqueued", 0)
    norm_enq = counters.get("normal_enqueued", 0)
    total_enq = hi_enq + norm_enq
    be_norm_pct = (norm_enq / total_enq * 100) if total_enq > 0 else 0.0

    be_pass = norm_enq > 0 and hi_enq == 0
    results["be_only"] = {
        "high_enqueued": hi_enq,
        "normal_enqueued": norm_enq,
        "normal_pct": round(be_norm_pct, 1),
        "pass": be_pass,
    }

    status = colored("PASS", "green") if be_pass else colored("FAIL", "red")
    print(f"  BE-only:  {be_norm_pct:>5.0f}% normal enqueues                        {status}", file=sys.stderr)

    return rt_pass and be_pass, results


def cmd_validate(args) -> int:
    """Quick functional validation of QoS scheduler correctness."""
    # Check root
    if os.geteuid() != 0:
        print(colored("Error: Must run as root for sysfs access and direct I/O", "red"),
              file=sys.stderr)
        return 1

    # Check fio
    if not check_fio_available():
        print(colored("Error: fio not found. Install with: apt install fio", "red"),
              file=sys.stderr)
        return 1

    # Select device
    prefs = UserPreferences.load()
    device_name = select_device(args, prefs)
    if not device_name:
        return 1

    device = NVMeDevice(device_name)

    if not device.qos_available:
        print(colored("Error: QoS not available (CONFIG_NVME_QOS not enabled)", "red"),
              file=sys.stderr)
        print("  Validation requires QoS sysfs controls", file=sys.stderr)
        return 1

    # Setup kernel stats
    kernel_stats = QoSKernelStats(device.controller)
    if not kernel_stats.available:
        print(colored("Error: Kernel QoS counters not available (debugfs not mounted?)", "red"),
              file=sys.stderr)
        print("  Validation requires debugfs QoS counters", file=sys.stderr)
        return 1

    # Setup output directory
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = Path(args.output) / f"validate_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    runner = FioRunner(device.path, output_dir)

    print()
    print(colored("=== QoS Functional Validation ===", "cyan"))
    print()

    device.save_state()
    tests_passed = 0
    total_tests = 3
    all_results = {}

    try:
        # Test 1: Policy override
        print(f"[1/{total_tests}] Policy override...")
        passed, result = _validate_policy_override(device, runner, kernel_stats)
        all_results["policy_override"] = result
        if passed:
            tests_passed += 1

        # Test 2: Enable/disable
        print(f"[2/{total_tests}] Enable/disable...")
        passed, result = _validate_enable_disable(device, runner, kernel_stats)
        all_results["enable_disable"] = result
        if passed:
            tests_passed += 1

        # Test 3: Classification
        print(f"[3/{total_tests}] Classification (default policy)...")
        passed, result = _validate_classification(device, runner, kernel_stats)
        all_results["classification"] = result
        if passed:
            tests_passed += 1

    finally:
        device.restore_state()

    # Summary
    print("---")
    all_pass = tests_passed == total_tests
    if all_pass:
        verdict = colored("PASS", "green")
    else:
        verdict = colored("FAIL", "red")
    print(f"Verdict: {verdict} ({tests_passed}/{total_tests})")

    # Save results
    validate_data = {
        "type": "validate",
        "pass": all_pass,
        "tests_passed": tests_passed,
        "total_tests": total_tests,
        "tests": all_results,
    }
    save_json(validate_data, output_dir / "validate.json")
    print(f"Results saved to: {output_dir}")

    return 0 if all_pass else 1


def _check_prerequisites() -> Optional[str]:
    """Check root access and fio availability. Returns error message or None."""
    if os.geteuid() != 0:
        return "Must run as root for sysfs access and direct I/O"
    if not check_fio_available():
        return "fio not found. Install with: apt install fio"
    return None


def _print_condition_diagnostics(condition_profile, config, hw_queues):
    """Print condition profile diagnostics and warnings."""
    active = config.max_queues or hw_queues
    total = config.high_numjobs + config.normal_numjobs
    print(f"Condition {condition_profile.id}: {condition_profile.name}", file=sys.stderr)
    print(f"  {condition_profile.description}", file=sys.stderr)
    print(f"  HW queues: {hw_queues}, active: {active}, "
          f"jobs: {config.high_numjobs}+{config.normal_numjobs} "
          f"({total / active:.1f}/q)", file=sys.stderr)

    # Per-queue write bytes diagnostic
    pqwb_parts = []
    for d in condition_profile.depths:
        b = condition_profile.per_queue_write_bytes(d)
        pqwb_parts.append(f"{b / (1024**2):.1f} MB @ QD{d}")

    if pqwb_parts:
        pqwb_line = ", ".join(pqwb_parts)
        print(f"  Per-queue write bytes: {pqwb_line}", file=sys.stderr)
        max_bytes = max(condition_profile.per_queue_write_bytes(d) for d in condition_profile.depths)
        if max_bytes > 256 * 1024**2:
            print(colored("  WARNING: per-queue writes exceed 256 MB -- device FTL/GC will dominate", "red"),
                  file=sys.stderr)
        elif max_bytes > 128 * 1024**2:
            print(colored("  WARNING: per-queue writes exceed 128 MB -- may reduce scheduler visibility", "yellow"),
                  file=sys.stderr)


def _save_reports(args, results, system_info, output_dir):
    """Generate and save all report files."""
    # Markdown summary
    comparisons = None
    if results["qos"] and results["baseline"]:
        p99_changes = [r["pct_change"] for r in results["qos"] if r.get("pct_change") is not None]
        norm_p99_changes = [r["norm_pct_change"] for r in results["qos"]
                            if r.get("norm_pct_change") is not None]
        comparisons = {"p99_changes": p99_changes}
        if norm_p99_changes:
            comparisons["norm_p99_changes"] = norm_p99_changes

    md_report = generate_markdown_report(system_info, results["all"], comparisons)
    with open(output_dir / "summary.md", "w") as f:
        f.write(md_report)

    # Comparison report
    if args.compare and results["baseline"] and results["qos"]:
        comparison_md = generate_comparison_report(
            results["baseline"], results["qos"], system_info
        )
        with open(output_dir / "comparison.md", "w") as f:
            f.write(comparison_md)

    # Capture dmesg
    dmesg = capture_dmesg()
    if dmesg:
        with open(output_dir / "dmesg.txt", "w") as f:
            f.write(dmesg)


def _save_csv_results(results, output_dir):
    """Generate and save CSV results."""
    csv_rows = []
    for r in results["all"]:
        ks = r.get("kernel_stats", {})
        fair = r.get("fairness", {})
        norm = r.get("normal_metrics", {})
        extra = {}

        # Kernel stats columns
        for key in ks:
            extra[f"ks_{key}"] = ks[key]

        # Fairness columns
        if fair:
            extra["fair_expected_hi_pct"] = fair.get("expected_hi_pct", "")
            extra["fair_actual_hi_pct"] = fair.get("actual_hi_pct", "")
            extra["fair_deviation_pct"] = fair.get("deviation_pct", "")
            extra["fair_result"] = fair.get("fair", "")
            extra["fair_demand_hi_pct"] = fair.get("demand_hi_pct", "")
            extra["fair_demand_limited"] = fair.get("demand_limited", "")
            extra["fair_effective_expected_hi_pct"] = fair.get("effective_expected_hi_pct", "")
            extra["fair_weight_hi_pct"] = fair.get("weight_hi_pct", "")

        # Normal-prio columns
        for key in norm:
            extra[f"norm_{key}"] = norm[key]

        for i, it in enumerate(r.get("iterations", [r["metrics"]])):
            row = flatten_result({**r["config"], "iteration": i}, it)
            row.update(extra)
            csv_rows.append(row)

    save_csv(csv_rows, output_dir / "data.csv", CSV_FIELDNAMES)


def _save_metadata(device, config, condition_profile, system_info, results, output_dir):
    """Create and save benchmark metadata."""
    hw_queues = device.get_hw_queue_count()
    active_queues = config.max_queues if config.max_queues else hw_queues
    total_jobs = config.high_numjobs + config.normal_numjobs
    jobs_per_queue = total_jobs / active_queues if active_queues else 0

    metadata = {
        "system": system_info,
        "config": {
            "runtime": config.runtime,
            "iterations": config.iterations,
            "depths": config.depths,
            "weights": config.weights,
            "qos_max_depth": config.qos_max_depth,
            "high_numjobs": config.high_numjobs,
            "normal_numjobs": config.normal_numjobs,
            "max_queues": config.max_queues,
            "hw_queues": hw_queues,
            "active_queues": active_queues,
            "jobs_per_queue": round(jobs_per_queue, 1),
            "condition_id": config.condition_id,
            "condition_name": condition_profile.name if condition_profile else None,
        },
    }

    if condition_profile:
        metadata["config"]["per_queue_write_bytes"] = {
            str(d): condition_profile.per_queue_write_bytes(d)
            for d in condition_profile.depths
        }
    if results.get("weight_order"):
        metadata["config"]["weight_order"] = results["weight_order"]
    if results.get("drift_probes"):
        metadata["drift_probes"] = results["drift_probes"]

    save_json(metadata, output_dir / "metadata.json")


def _print_benchmark_summary(results):
    """Calculate and print benchmark summary statistics."""
    if not (results["qos"] and results["baseline"]):
        return

    p99_changes = [r["pct_change"] for r in results["qos"] if r.get("pct_change") is not None]
    if not p99_changes:
        return

    # Calculate IOPS changes
    iops_changes = []
    cpu_changes = []
    for qos_r in results["qos"]:
        depth = qos_r["config"]["iodepth"]
        weight = qos_r["config"].get("qos_weight")
        workload = qos_r["config"].get("workload")
        baseline_match = next(
            (b for b in results["baseline"]
             if b["config"]["iodepth"] == depth
             and b["config"].get("paired_weight") == weight
             and b["config"].get("workload") == workload),
            None
        )
        if baseline_match:
            iops_changes.append(percentage_change(
                baseline_match["metrics"]["iops"],
                qos_r["metrics"]["iops"]
            ))
            cpu_changes.append(percentage_change(
                baseline_match["metrics"]["cpu_pct"],
                qos_r["metrics"]["cpu_pct"]
            ))

    norm_p99_changes = [r["norm_pct_change"] for r in results["qos"]
                        if r.get("norm_pct_change") is not None]
    norm_range = (min(norm_p99_changes), max(norm_p99_changes)) if norm_p99_changes else None

    print_summary(
        (min(p99_changes), max(p99_changes)),
        (min(iops_changes), max(iops_changes)) if iops_changes else (0, 0),
        (min(cpu_changes), max(cpu_changes)) if cpu_changes else (0, 0),
        norm_p99_range=norm_range,
    )


def _apply_cli_overrides(args, config, condition_profile):
    """Apply CLI argument overrides to config object."""
    if args.iterations:
        config.iterations = args.iterations
    if args.runtime:
        config.runtime = args.runtime
    if args.depths:
        config.depths = args.depths
    if args.weights:
        config.weights = args.weights
    if args.buffered:
        config.run_buffered = True
    if args.high_numjobs:
        config.high_numjobs = args.high_numjobs
    if args.normal_numjobs:
        config.normal_numjobs = args.normal_numjobs
    if args.max_queues:
        config.max_queues = args.max_queues
    if args.max_depth:
        config.qos_max_depth = args.max_depth
        if condition_profile:
            condition_profile.max_depth = args.max_depth
    if args.normal_bs or args.normal_rw:
        if config.workload_params is None:
            config.workload_params = {}
        if args.normal_bs:
            config.workload_params["normal_bs"] = args.normal_bs
        if args.normal_rw:
            config.workload_params["normal_rw"] = args.normal_rw


def _load_benchmark_config(args, device):
    """
    Load benchmark config from args and device.
    Returns: (config, condition_profile) tuple, or error code int.
    """
    condition_profile = None

    if args.condition:
        try:
            condition_profile = get_condition(args.condition)
        except KeyError as e:
            print(colored(f"Error: {e}", "red"), file=sys.stderr)
            return 1

        hw_queues = device.get_hw_queue_count()
        config = condition_profile.resolve(hw_queues)
        _print_condition_diagnostics(condition_profile, config, hw_queues)

        # Condition A delegates to existing baseline overhead path
        if condition_profile.use_baseline_path:
            return cmd_run_baseline(device, args)

    elif args.stress:
        config = STRESS_CONFIG
    elif args.quick:
        config = QUICK_CONFIG
    elif args.config:
        # Catch common mistake: -c A (config) when user meant -C A (condition)
        if len(args.config) <= 2 and args.config.upper() in "ABCDEFGHIJK":
            print(colored(
                f"Error: '{args.config}' looks like a condition ID, not a config file.\n"
                f"  Use -C {args.config.upper()} (uppercase C) for condition profiles.\n"
                f"  Use -c <name> for config presets (quick/default/full) or YAML paths.",
                "red"
            ), file=sys.stderr)
            return 1
        config = load_config(args.config)
    else:
        config = DEFAULT_CONFIG

    return (config, condition_profile)


def cmd_run(args) -> int:
    """Run benchmark suite."""
    # Check prerequisites
    error = _check_prerequisites()
    if error:
        print(colored(f"Error: {error}", "red"), file=sys.stderr)
        return 1

    # Load preferences
    prefs = UserPreferences.load()

    # Select device
    device_name = select_device(args, prefs)
    if not device_name:
        return 1

    device = NVMeDevice(device_name)

    # Baseline overhead check (separate fast path)
    if args.baseline:
        return cmd_run_baseline(device, args)

    # Load config
    result = _load_benchmark_config(args, device)
    if isinstance(result, int):
        return result  # Error code
    config, condition_profile = result

    # Override config with CLI args
    _apply_cli_overrides(args, config, condition_profile)

    # Setup output directory
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = Path(args.output) / f"run_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Setup kernel stats reader
    kernel_stats = QoSKernelStats(device.controller)

    # Print header
    kernel = get_kernel_version()
    print_header(device_name, kernel, device.qos_available, kernel_stats.available)

    if not device.qos_available:
        print_warning("NVMe QoS not available (CONFIG_NVME_QOS not enabled)")
        print("  Running baseline benchmarks only - no QoS comparison possible", file=sys.stderr)
        print("  Rebuild kernel with CONFIG_NVME_QOS=y to enable QoS testing", file=sys.stderr)

    # Collect system info
    system_info = collect_system_info(device_name, device.controller)

    # Track total runtime
    start_time = time.time()

    # Run benchmarks
    results = run_benchmark_suite(
        device, config, output_dir,
        compare=args.compare,
        kernel_stats=kernel_stats,
    )

    # Print summary
    print_separator()
    _print_benchmark_summary(results)

    # Save outputs
    _save_metadata(device, config, condition_profile, system_info, results, output_dir)

    # Aggregate results
    save_json(results, output_dir / "aggregate.json")

    # CSV
    _save_csv_results(results, output_dir)

    # Generate reports
    _save_reports(args, results, system_info, output_dir)

    # Calculate total runtime
    total_elapsed = time.time() - start_time
    minutes, seconds = divmod(total_elapsed, 60)

    print(file=sys.stderr)
    print(f"Results saved to: {output_dir}", file=sys.stderr)
    if minutes >= 1:
        print(f"Total runtime: {int(minutes)}m {seconds:.1f}s", file=sys.stderr)
    else:
        print(f"Total runtime: {total_elapsed:.1f}s", file=sys.stderr)

    return 0


def _find_baseline_match(results: List[Dict], depth: int,
                         workload: Optional[str] = None,
                         paired_weight: Optional[int] = None) -> Optional[Dict]:
    """Find the baseline result matching a given depth, workload, and paired weight."""
    for b in results.get("baseline", []):
        cfg = b["config"]
        if cfg["iodepth"] != depth:
            continue
        if cfg.get("workload") != workload:
            continue
        if paired_weight is not None and cfg.get("paired_weight") != paired_weight:
            continue
        return b
    return None


def _effect_size_label(d: float) -> str:
    """Cohen's d effect size label."""
    d = abs(d)
    if d < 0.2:
        return "negligible"
    if d < 0.5:
        return "small"
    if d < 0.8:
        return "medium"
    return "large"


def _confidence_label(n: int) -> str:
    """Confidence level based on sample count."""
    if n <= 1:
        return "NONE"
    if n <= 4:
        return "LOW"
    if n <= 9:
        return "MODERATE"
    return "HIGH"


def _print_analyze_header(input_dir: Path, system_info: Dict, config_info: Dict) -> None:
    """Print analysis header with run metadata."""
    print()
    print(colored("=== NVMe QoS Benchmark Analysis ===", "cyan"))
    print(f"Dir:    {input_dir}")

    kernel = system_info.get("kernel", "N/A")
    git = system_info.get("git", {})
    commit = git.get("commit", "N/A")
    branch = git.get("branch", "")
    dirty = ", dirty" if git.get("dirty") else ""
    git_info = f"  Commit: {commit}"
    if branch:
        git_info += f" ({branch}{dirty})"
    print(f"Kernel: {kernel}{git_info}")

    device = system_info.get("device", "N/A")
    depths = config_info.get("depths", [])
    weights = config_info.get("weights", [])
    iters = config_info.get("iterations", "?")
    runtime = config_info.get("runtime", "?")
    hi_nj = config_info.get("high_numjobs", "?")
    norm_nj = config_info.get("normal_numjobs", "?")
    max_q = config_info.get("max_queues")
    hw_q = config_info.get("hw_queues")
    jpq = config_info.get("jobs_per_queue")
    queue_info = ""
    if max_q:
        queue_info = f" queues={max_q}/{hw_q}" if hw_q else f" queues={max_q}"
    elif hw_q:
        queue_info = f" queues={hw_q}"
    if jpq:
        queue_info += f" ({jpq} jobs/q)"
    max_depth = config_info.get("qos_max_depth", 0)
    md_info = f" md={max_depth}" if max_depth else ""
    print(f"Device: {device}  Config: qd={depths} w={weights}{md_info} jobs={hi_nj}+{norm_nj}"
          f"{queue_info} iter={iters} runtime={runtime}s")

    cond_id = config_info.get("condition_id")
    cond_name = config_info.get("condition_name")
    if cond_id:
        # Look up mechanism from registry if available
        try:
            profile = get_condition(cond_id)
            mechanism = profile.mechanism
        except KeyError:
            mechanism = ""
        mech_str = f" -- {mechanism}" if mechanism else ""
        print(f"Condition: {cond_id} ({cond_name}){mech_str}")
    print()


def _print_high_priority_table(results: Dict) -> None:
    """Print combined baseline + QoS high-priority latency table."""
    all_results = results.get("all", [])
    if not all_results:
        print("No results found.")
        return

    header = (
        f"{'':>8} {'p50':>8} {'p90':>8} {'p99':>8} "
        f"{'p999':>8} {'IOPS':>8} {'CPU':>6} {'Util':>6}"
    )
    print(colored("-- High-Priority Latency ", "cyan") + "-" * 50)
    print(header)

    for r in all_results:
        config = r.get("config", {})
        m = r.get("metrics", {})
        qos = config.get("qos_enabled", False)
        depth = config.get("iodepth", 0)
        workload = config.get("workload")
        weight = config.get("qos_weight")

        if qos:
            label = f"QoS  {depth}"
            if workload:
                label = f"QoS buf {depth}"
        else:
            label = f" bl  {depth}"
            if workload:
                label = f" bl buf {depth}"

        util = m.get("io_util_pct", 0)
        line = (
            f"{label:>8} "
            f"{format_us(m.get('p50_us', 0)):>8} "
            f"{format_us(m.get('p90_us', 0)):>8} "
            f"{format_us(m.get('p99_us', 0)):>8} "
            f"{format_us(m.get('p999_us', 0)):>8} "
            f"{si_format(m.get('iops', 0)):>8} "
            f"{m.get('cpu_pct', 0):5.1f}% "
            f"{util:5.1f}%"
        )

        # Add p99 change for QoS results
        pct = r.get("pct_change")
        if pct is not None:
            change_str = f"   p99 {pct:+.1f}%"
            if pct <= -5:
                change_str = colored(change_str, "green")
            elif pct > 5:
                change_str = colored(change_str, "red")
            line += change_str

        print(line)

    print()


def _print_normal_priority_table(results: Dict) -> None:
    """Print normal-priority impact table. Skip if no normal_metrics."""
    qos_results = results.get("qos", [])
    has_normal = any(r.get("normal_metrics") for r in qos_results)
    if not has_normal:
        return

    print(colored("-- Normal-Priority Impact ", "cyan") + "-" * 49)
    print(f"{'':>8} {'p99 bl':>10} {'p99 QoS':>10} {'change':>8} {'IOPS':>8} {'BW':>10}")

    for r in qos_results:
        nm = r.get("normal_metrics")
        if not nm:
            continue
        config = r.get("config", {})
        depth = config.get("iodepth", 0)

        # Find baseline normal metrics
        bl_match = _find_baseline_match(results, depth, config.get("workload"))
        bl_norm = bl_match.get("normal_metrics", {}) if bl_match else {}

        bl_p99 = bl_norm.get("p99_us", 0)
        qos_p99 = nm.get("p99_us", 0)

        change_str = ""
        if bl_p99 > 0 and qos_p99 > 0:
            change = percentage_change(bl_p99, qos_p99)
            change_str = f"{change:+.1f}%"
        else:
            bl_p99_str = format_us(bl_p99) if bl_p99 else "N/A"

        bl_p99_str = format_us(bl_p99) if bl_p99 else "N/A"
        bw = nm.get("bw_mbps", 0)

        label = f"QoS  {depth}"
        print(
            f"{label:>8} "
            f"{bl_p99_str:>10} "
            f"{format_us(qos_p99):>10} "
            f"{change_str:>8} "
            f"{si_format(nm.get('iops', 0)):>8} "
            f"{bw:>7.0f}MB/s"
        )

    print()


def _print_variance_section(results: Dict) -> None:
    """Print per-iteration variance analysis."""
    all_results = results.get("all", [])
    has_iterations = any(r.get("iterations") and len(r.get("iterations", [])) > 1
                         for r in all_results)
    if not has_iterations:
        return

    print(colored("-- Per-Iteration Variance ", "cyan") + "-" * 49)
    print(f"{'':>12} {'p99 mean':>10} {'stddev':>10} {'CI 95%':>20} {'range':>18} {'n':>3}")

    min_n = float('inf')
    for r in all_results:
        config = r.get("config", {})
        iterations = r.get("iterations", [])
        if not iterations or len(iterations) < 2:
            continue

        p99_values = [it.get("p99_us", 0) for it in iterations]
        stats = calculate_stats(p99_values)

        qos = config.get("qos_enabled", False)
        depth = config.get("iodepth", 0)
        if qos:
            label = f"QoS {depth}"
        else:
            label = f"bl  {depth}"

        min_n = min(min_n, stats.n)
        ci_str = f"[{format_us(stats.ci_low)}, {format_us(stats.ci_high)}]"
        range_str = f"[{format_us(stats.min_val)}-{format_us(stats.max_val)}]"

        degraded = r.get("degraded_iterations", [])
        degraded_str = ""
        if degraded:
            iters_str = ",".join(str(i) for i in degraded)
            degraded_str = colored(f"  ({len(degraded)} DEGRADED: iters {iters_str})", "yellow")

        print(
            f" {label:>10} "
            f"{format_us(stats.mean):>10} "
            f"{'+/-' + format_us(stats.stddev):>10} "
            f"{ci_str:>20} "
            f"{range_str:>18} "
            f"{stats.n:>3}"
            f"{degraded_str}"
        )

    if min_n < 5 and min_n != float('inf'):
        print(colored(f" * n={int(min_n)} too few for reliable statistics (need >=5)", "yellow"))
    print()


def _print_significance_section(results: Dict) -> None:
    """Print statistical significance of QoS vs baseline."""
    qos_results = results.get("qos", [])
    if not qos_results:
        return

    has_data = False
    lines = []
    for r in qos_results:
        config = r.get("config", {})
        depth = config.get("iodepth", 0)
        weight = config.get("qos_weight", 0)
        workload = config.get("workload")

        bl_match = _find_baseline_match(results, depth, workload,
                                        paired_weight=weight)
        if not bl_match:
            continue

        qos_iters = r.get("iterations", [])
        bl_iters = bl_match.get("iterations", [])
        if not qos_iters or not bl_iters:
            continue

        qos_p99 = [it.get("p99_us", 0) for it in qos_iters]
        bl_p99 = [it.get("p99_us", 0) for it in bl_iters]
        n = min(len(qos_p99), len(bl_p99))

        pct = r.get("pct_change")
        pct_str = f"{pct:+.1f}%" if pct is not None else "?"

        label = f"QoS qd={depth}"
        if weight:
            label += f" w={weight}"

        # Degraded iteration notes
        bl_degraded = bl_match.get("degraded_iterations", [])
        qos_degraded = r.get("degraded_iterations", [])
        degraded_note = ""
        if bl_degraded or qos_degraded:
            parts = []
            if bl_degraded:
                parts.append(f"baseline had {len(bl_degraded)} degraded")
            if qos_degraded:
                parts.append(f"QoS had {len(qos_degraded)} degraded")
            degraded_note = " -- " + colored("; ".join(parts), "yellow")

        if n < 2:
            lines.append(f" {label}: p99 {pct_str} -- INSUFFICIENT DATA (n={n}){degraded_note}")
        elif n < 5:
            lines.append(f" {label}: p99 {pct_str} -- "
                         + colored(f"NOT SIGNIFICANT (n={n}, need >=5 for t-test)", "yellow")
                         + degraded_note)
        else:
            ttest = two_sample_ttest(bl_p99, qos_p99)
            if math.isinf(ttest.effect_size):
                sig_str = colored(
                    "DETERMINISTIC (zero variance, effect fully reproducible)",
                    "green" if pct and pct < 0 else "red"
                )
            else:
                d_label = _effect_size_label(ttest.effect_size)
                if ttest.significant:
                    sig_str = colored(
                        f"SIGNIFICANT (p<0.05, d={abs(ttest.effect_size):.1f} {d_label})",
                        "green" if pct and pct < 0 else "red"
                    )
                else:
                    sig_str = f"NOT SIGNIFICANT (p>=0.05, d={abs(ttest.effect_size):.1f} {d_label})"
            lines.append(f" {label}: p99 {pct_str} -- {sig_str}{degraded_note}")

        has_data = True

    if has_data:
        print(colored("-- Statistical Significance ", "cyan") + "-" * 47)
        for line in lines:
            print(line)
        print()


def _print_kernel_section(results: Dict) -> None:
    """Print kernel QoS scheduler analysis with interpretive text."""
    qos_results = results.get("qos", [])
    has_ks = any(r.get("kernel_stats") for r in qos_results)
    if not has_ks:
        return

    print(colored("-- Kernel QoS Scheduler ", "cyan") + "-" * 51)

    for r in qos_results:
        ks = r.get("kernel_stats")
        fairness = r.get("fairness", {})
        if not ks:
            continue

        config = r.get("config", {})
        depth = config.get("iodepth", 0)
        weight = config.get("qos_weight", 0)

        print(f"QoS qd={depth} w={weight}:")

        # Dispatch ratio
        hi_total = ks.get("high_dispatched", 0) + ks.get("wc_high_fallback", 0)
        norm_total = ks.get("normal_dispatched", 0) + ks.get("wc_normal_fallback", 0)
        total = hi_total + norm_total

        hi_enq = ks.get("high_enqueued", 0)
        norm_enq = ks.get("normal_enqueued", 0)
        total_enq = hi_enq + norm_enq

        if total > 0:
            hi_pct = round(hi_total / total * 100)
            norm_pct = 100 - hi_pct
        else:
            hi_pct = norm_pct = 0

        if total_enq > 0:
            enq_hi_pct = round(hi_enq / total_enq * 100)
            enq_norm_pct = 100 - enq_hi_pct
        else:
            enq_hi_pct = enq_norm_pct = 0

        regime = "demand-limited" if fairness.get("demand_limited") else "weight-limited"
        weight_target = f"{round(weight / (weight + 1) * 100)}:{round(1 / (weight + 1) * 100)}" if weight > 0 else "50:50"
        print(f"  Dispatch:  disp={hi_pct}:{norm_pct}  enq={enq_hi_pct}:{enq_norm_pct}  ({regime}, weight target={weight_target})")

        # WRR vs work-conserving breakdown
        wrr_hi = ks.get("high_dispatched", 0)
        wrr_norm = ks.get("normal_dispatched", 0)
        wrr_total = wrr_hi + wrr_norm
        wc_hi = ks.get("wc_high_fallback", 0)
        wc_norm = ks.get("wc_normal_fallback", 0)
        wc_total = wc_hi + wc_norm

        if total > 0:
            wrr_pct = wrr_total / total * 100
            wc_pct = wc_total / total * 100
        else:
            wrr_pct = wc_pct = 0

        print(f"  WRR path:  {si_format(wrr_hi)} hi + {si_format(wrr_norm)} norm = {si_format(wrr_total)} ({wrr_pct:.1f}% of dispatches)")
        print(f"  WC fallback: {si_format(wc_hi)} hi + {si_format(wc_norm)} norm = {si_format(wc_total)} ({wc_pct:.1f}%)")

        # Interpretive text for contention level
        if wc_pct > 90:
            print("  -> Nearly all dispatches are uncontested (only one class queued)")
        elif wc_pct > 50:
            print(f"  -> Moderate contention -- WRR arbitrating {100 - wc_pct:.0f}% of dispatches")
        else:
            print("  -> High contention -- WRR actively arbitrating")

        # WRR engagement classification
        if wrr_pct < 5:
            print(colored(
                "  WRR engagement: LOW -- use --max-depth 16 to force host-side queuing"
                " and/or increase --high-numjobs / --normal-numjobs", "red"))
        elif wrr_pct <= 40:
            print(colored(f"  WRR engagement: MODERATE -- WRR partially exercised ({wrr_pct:.1f}%)", "yellow"))
        else:
            print(colored(f"  WRR engagement: HIGH -- WRR actively arbitrating ({wrr_pct:.1f}%)", "green"))

        # Credits
        refills = ks.get("credit_refills", 0)
        throttled = ks.get("sq_throttled", 0)
        print(f"  Credits: {si_format(refills)} refills, {si_format(throttled)} throttles")

        # Kicks
        kicks = ks.get("kicks", 0)
        kick_empty = ks.get("kick_empty", 0)
        kick_total = kicks + kick_empty
        if kick_total > 0:
            kick_hit_pct = kicks / kick_total * 100
        else:
            kick_hit_pct = 0
        print(f"  Kicks: {si_format(kicks)} successful / {si_format(kick_total)} attempts ({kick_hit_pct:.1f}% hit rate)")

        if kick_total > 0:
            if kick_hit_pct < 1:
                print("  -> Completion IRQ never finds pending work -- no queuing backlog")
            elif kick_hit_pct > 50:
                print("  -> Kick path actively draining queued requests")

        if throttled > 0:
            print(colored("  -> SQ full -- device backpressure active", "yellow"))

        # Doorbells (submission batching efficiency)
        doorbells = ks.get("doorbells", 0)
        if doorbells > 0 and total > 0:
            batch_ratio = total / doorbells
            print(f"  Doorbells: {si_format(doorbells)} ({batch_ratio:.1f} dispatches/doorbell)")

        # Fairness
        fair_str = fairness.get("fair", "?")
        actual = fairness.get("actual_hi_pct", 0)
        expected = fairness.get("effective_expected_hi_pct", 0)
        deviation = fairness.get("deviation_pct", 0)
        if fairness.get("demand_limited"):
            fair_detail = f"demand-limited, actual={actual:.1f}% vs expected={expected:.1f}%, dev={deviation:.1f}%"
        else:
            fair_detail = f"weight-limited, actual={actual:.1f}% vs expected={expected:.1f}%, dev={deviation:.1f}%"

        fair_color = "green" if fair_str == "OK" else "yellow"
        print(f"  Fairness: {colored(fair_str, fair_color)} ({fair_detail})")
        if fairness.get("normal_starved"):
            print(colored("  -> WARNING: Normal priority received zero dispatches (starvation)", "red"))
        print()

    # No extra newline needed — each entry already ends with print()


def _print_throughput_section(results: Dict) -> None:
    """Print throughput proportionality analysis (IOPS split vs configured weight)."""
    qos_results = results.get("qos", [])
    has_normal = any(r.get("normal_metrics") for r in qos_results)
    if not has_normal:
        return

    print(colored("-- Throughput Proportionality ", "cyan") + "-" * 45)
    print(f"{'':>8} {'Hi IOPS':>8} {'Norm IOPS':>10} {'Actual':>8} {'Target':>8} {'Dev':>6}")

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
            actual_hi_pct = hi_iops / total_iops * 100
            actual_norm_pct = 100 - actual_hi_pct
        else:
            actual_hi_pct = actual_norm_pct = 0

        target_hi_pct = (weight / (weight + 1)) * 100 if weight > 0 else 50
        target_norm_pct = 100 - target_hi_pct
        deviation = actual_hi_pct - target_hi_pct

        actual_str = f"{actual_hi_pct:.0f}:{actual_norm_pct:.0f}"
        target_str = f"{target_hi_pct:.0f}:{target_norm_pct:.0f}"
        dev_str = f"{deviation:+.0f}pp"

        # Check if demand-limited (from fairness data if available)
        fairness = r.get("fairness", {})
        demand_note = ""
        if fairness.get("demand_limited"):
            demand_hi_pct = fairness.get("demand_hi_pct", 0)
            demand_note = f"  (demand={demand_hi_pct:.0f}%, capped by supply)"

        label = f"qd={depth}"
        print(
            f" {label:>7} "
            f"{si_format(hi_iops):>8} "
            f"{si_format(norm_iops):>10} "
            f"{actual_str:>8} "
            f"{target_str:>8} "
            f"{dev_str:>6}"
            f"{demand_note}"
        )

    print()


def _print_analyze_summary(results: Dict, config_info: Optional[Dict] = None) -> None:
    """Print overall analysis summary."""
    qos_results = results.get("qos", [])
    if not qos_results:
        return

    print(colored("-- Summary ", "cyan") + "-" * 63)

    # p99 range
    p99_changes = [r.get("pct_change") for r in qos_results if r.get("pct_change") is not None]
    if p99_changes:
        print(f" p99:      {min(p99_changes):+.1f}% to {max(p99_changes):+.1f}%")

    # IOPS range
    iops_changes = []
    for r in qos_results:
        depth = r["config"]["iodepth"]
        bl = _find_baseline_match(results, depth, r["config"].get("workload"))
        if bl and bl["metrics"].get("iops") and r["metrics"].get("iops"):
            iops_changes.append(percentage_change(bl["metrics"]["iops"], r["metrics"]["iops"]))
    if iops_changes:
        print(f" IOPS:     {min(iops_changes):+.1f}% to {max(iops_changes):+.1f}%")

    # Normal p99 range
    norm_changes = [r.get("norm_pct_change") for r in qos_results if r.get("norm_pct_change") is not None]
    if norm_changes:
        print(f" NORM p99: {min(norm_changes):+.1f}% to {max(norm_changes):+.1f}%")

    # CPU overhead
    cpu_deltas = []
    for r in qos_results:
        depth = r["config"]["iodepth"]
        bl = _find_baseline_match(results, depth, r["config"].get("workload"))
        if bl and bl["metrics"].get("cpu_pct") and r["metrics"].get("cpu_pct"):
            delta = r["metrics"]["cpu_pct"] - bl["metrics"]["cpu_pct"]
            cpu_deltas.append(delta)
    if cpu_deltas:
        max_delta = max(cpu_deltas)
        delta_str = f"{min(cpu_deltas):+.2f}pp to {max_delta:+.2f}pp"
        if max_delta <= 0:
            verdict = colored("  PASS (CPU decreased)", "green")
        elif max_delta < 5.0:
            verdict = colored(f"  PASS (+{max_delta:.2f}pp < 5pp)", "green")
        else:
            verdict = colored(f"  FAIL (+{max_delta:.2f}pp > 5pp threshold)", "red")
        print(f" CPU:      {delta_str}{verdict}")

    # Confidence level
    all_results = results.get("all", [])
    max_n = 0
    for r in all_results:
        iters = r.get("iterations", [])
        if iters:
            max_n = max(max_n, len(iters))
    if max_n == 0:
        max_n = 1

    confidence = _confidence_label(max_n)
    conf_color = {"NONE": "red", "LOW": "yellow", "MODERATE": "cyan", "HIGH": "green"}.get(confidence, "white")
    advice = ""
    if max_n < 5:
        advice = f" ({max_n} iterations -- rerun with --iterations 5+)"
    print(f" Confidence: {colored(confidence, conf_color)}{advice}")

    # WRR engagement warning across all QoS results
    wrr_pcts = []
    for r in qos_results:
        ks = r.get("kernel_stats")
        if not ks:
            continue
        wrr_total = ks.get("high_dispatched", 0) + ks.get("normal_dispatched", 0)
        wc_total = ks.get("wc_high_fallback", 0) + ks.get("wc_normal_fallback", 0)
        total = wrr_total + wc_total
        if total > 0:
            wrr_pcts.append(wrr_total / total * 100)
    if wrr_pcts:
        avg_wrr = sum(wrr_pcts) / len(wrr_pcts)
        if avg_wrr < 5:
            hi_nj = config_info.get("high_numjobs") if config_info else None
            norm_nj = config_info.get("normal_numjobs") if config_info else None
            max_q = config_info.get("max_queues") if config_info else None
            if hi_nj and norm_nj:
                hint = (f" (ran {hi_nj}+{norm_nj} jobs"
                        + (f" on {max_q} queues" if max_q else "")
                        + " -- try --max-depth 16"
                        + (f" --high-numjobs {hi_nj * 4} --normal-numjobs {norm_nj * 4}" if not max_q else "")
                        + ")")
            else:
                hint = " -- try --max-depth 16 or increase --high-numjobs / --normal-numjobs"
            print(colored(
                f" WRR engagement: LOW ({avg_wrr:.1f}% avg){hint}",
                "yellow",
            ))

    print()


def cmd_analyze(args) -> int:
    """Analyze existing benchmark results with comprehensive output."""
    input_dir = Path(args.input)

    if not input_dir.exists():
        print(colored(f"Error: Directory not found: {input_dir}", "red"), file=sys.stderr)
        return 1

    # Load aggregate results
    aggregate_file = input_dir / "aggregate.json"
    if not aggregate_file.exists():
        print(colored(f"Error: No aggregate.json found in {input_dir}", "red"), file=sys.stderr)
        return 1

    with open(aggregate_file) as f:
        results = json.load(f)

    # Load metadata
    metadata_file = input_dir / "metadata.json"
    system_info = {}
    config_info = {}
    if metadata_file.exists():
        with open(metadata_file) as f:
            metadata = json.load(f)
            system_info = metadata.get("system", {})
            config_info = metadata.get("config", {})

    # Check for empty results
    if not results.get("all"):
        print(colored(f"No benchmark results found in {input_dir}", "yellow"))
        return 0

    # Print all sections
    _print_analyze_header(input_dir, system_info, config_info)
    _print_high_priority_table(results)
    _print_normal_priority_table(results)
    _print_throughput_section(results)
    _print_variance_section(results)
    _print_significance_section(results)
    _print_kernel_section(results)
    _print_analyze_summary(results, config_info)

    # Markdown output
    if args.output:
        md = generate_analysis_report(input_dir, results, system_info, config_info)
        out_path = Path(args.output)
        with open(out_path, "w") as f:
            f.write(md)
        print(f"Markdown report written to: {out_path}")

    return 0


def cmd_list(args) -> int:
    """List all benchmark runs in results directory."""
    results_dir = Path(args.results_dir)
    if not results_dir.is_dir():
        print(colored(f"Error: Results directory not found: {results_dir}", "red"), file=sys.stderr)
        return 1

    runs = scan_results_dir(results_dir)

    # Filter by commit if requested
    if args.commit:
        runs = filter_by_commit(runs, args.commit)
        if not runs:
            print(f"No runs found matching commit prefix '{args.commit}'")
            return 0

    if not runs:
        print("No benchmark runs found.")
        return 0

    # Check if any runs have condition IDs
    has_conditions = any(r.condition_id for r in runs)

    # Print table header
    print()
    cond_col = f"{'Cond':>4} " if has_conditions else ""
    header = (
        f"{'Date':<20} {'Commit':<12} {'Branch':<25} "
        f"{'D':>1} {cond_col}{'Depths':<14} {'Weights':<10} "
        f"{'Iter':>4} {'BL':>3} {'QoS':>3}"
    )
    print(colored(header, "cyan"))
    print("-" * len(header.expandtabs()))

    # Track stats
    commits = set()
    missing_meta = 0

    for run in runs:
        commit_str = run.commit[:7] if run.commit else "?"
        if run.commit:
            commits.add(run.commit)
        else:
            missing_meta += 1

        branch_str = run.branch or "?"
        if len(branch_str) > 24:
            branch_str = branch_str[:21] + "..."
        dirty_str = "*" if run.dirty else " "

        depths_str = str(run.depths) if run.depths else "?"
        if len(depths_str) > 13:
            depths_str = depths_str[:10] + "..."
        weights_str = str(run.weights) if run.weights else "?"

        bl_str = colored("yes", "green") if run.has_baseline else colored("no", "red")
        qos_str = colored("yes", "green") if run.has_qos else colored("no", "red")

        cond_str = f"{run.condition_id or '-':>4} " if has_conditions else ""
        print(
            f"{run.timestamp:<20} {commit_str:<12} {branch_str:<24} "
            f"{dirty_str:>1} {cond_str}{depths_str:<14} {weights_str:<10} "
            f"{run.iterations:>4} {bl_str:>3} {qos_str:>3}"
        )

    # Summary
    print()
    summary = f"{len(runs)} runs"
    if commits:
        summary += f" across {len(commits)} commits"
    if missing_meta:
        summary += f" ({missing_meta} runs missing metadata)"
    print(summary)

    return 0


def _compare_directories(baseline_dir: str, test_dir: str) -> int:
    """Compare two result directories (existing behavior)."""
    baseline_path = Path(baseline_dir)
    test_path = Path(test_dir)

    for d in [baseline_path, test_path]:
        if not d.exists():
            print(colored(f"Error: Directory not found: {d}", "red"), file=sys.stderr)
            return 1

    # Load results
    with open(baseline_path / "aggregate.json") as f:
        baseline_results = json.load(f)
    with open(test_path / "aggregate.json") as f:
        test_results = json.load(f)

    # Compare
    print("Comparison: Baseline vs Test")
    print("=" * 80)
    print(f"Baseline: {baseline_path}")
    print(f"Test: {test_path}")
    print()

    baseline_by_depth = {r["config"]["iodepth"]: r for r in baseline_results.get("all", [])}
    test_by_depth = {r["config"]["iodepth"]: r for r in test_results.get("all", [])}

    # Print comparison for each percentile
    for pct_name, pct_key in [("p50", "p50_us"), ("p90", "p90_us"), ("p99", "p99_us"), ("p999", "p999_us")]:
        print(f"{pct_name} Latency Comparison:")
        print(f"| {'QD':>3} | {'Baseline':>10} | {'Test':>10} | {'Change':>8} |")
        print(f"|{'-'*5}|{'-'*12}|{'-'*12}|{'-'*10}|")

        for depth in sorted(set(baseline_by_depth.keys()) | set(test_by_depth.keys())):
            b = baseline_by_depth.get(depth, {}).get("metrics", {})
            t = test_by_depth.get(depth, {}).get("metrics", {})

            b_val = b.get(pct_key, 0)
            t_val = t.get(pct_key, 0)
            change = percentage_change(b_val, t_val) if b_val else 0

            print(f"| {depth:>3} | {format_us(b_val):>10} | {format_us(t_val):>10} | {change:>+7.1f}% |")
        print()

    return 0


def _compare_commits(args) -> int:
    """Compare benchmark results across git commits with statistical significance."""
    results_dir = Path(args.results_dir)
    if not results_dir.is_dir():
        print(colored(f"Error: Results directory not found: {results_dir}", "red"), file=sys.stderr)
        return 1

    all_runs = scan_results_dir(results_dir)
    base_runs = filter_by_commit(all_runs, args.base_commit)
    test_runs = filter_by_commit(all_runs, args.test_commit)

    if not base_runs:
        print(colored(f"Error: No runs found for base commit '{args.base_commit}'", "red"), file=sys.stderr)
        return 1
    if not test_runs:
        print(colored(f"Error: No runs found for test commit '{args.test_commit}'", "red"), file=sys.stderr)
        return 1

    # Extract commit info for headers
    base_commit = base_runs[0].commit or args.base_commit
    test_commit = test_runs[0].commit or args.test_commit
    base_branch = base_runs[0].branch or "?"
    test_branch = test_runs[0].branch or "?"
    base_dirty = any(r.dirty for r in base_runs)
    test_dirty = any(r.dirty for r in test_runs)

    metric_key = args.metric if args.metric else "p99_us"
    metric_label = metric_key.replace("_us", "").upper() if metric_key.endswith("_us") else metric_key

    # Header
    print()
    print(colored("=== NVMe QoS Commit Comparison ===", "cyan"))
    base_dirty_str = ", dirty" if base_dirty else ""
    test_dirty_str = ", dirty" if test_dirty else ""
    print(f"Base: {base_commit[:7]} ({base_branch}{base_dirty_str}) -- {len(base_runs)} runs")
    print(f"Test: {test_commit[:7]} ({test_branch}{test_dirty_str}) -- {len(test_runs)} runs")

    if base_dirty or test_dirty:
        print(colored("* Both commits have dirty working tree -- results may not be reproducible", "yellow"))
    print()

    # Find matching configs
    base_tuples = set()
    for r in base_runs:
        base_tuples |= get_config_tuples(r)
    test_tuples = set()
    for r in test_runs:
        test_tuples |= get_config_tuples(r)
    common_tuples = base_tuples & test_tuples

    if not common_tuples:
        print(colored("No matching configs found between the two commits.", "yellow"))
        return 0

    # Group by qos_enabled
    baseline_configs = sorted([t for t in common_tuples if not t[0]], key=lambda t: t[1])
    qos_configs = sorted([t for t in common_tuples if t[0]], key=lambda t: (t[1], t[2] or 0))

    comparisons = []

    for section_name, configs in [("Baseline (QoS off)", baseline_configs), ("QoS", qos_configs)]:
        if not configs:
            continue

        print(colored(f"-- {section_name} {metric_label} Latency ", "cyan") + "-" * 40)
        print(f"{'':>8} {'Base mean [CI]':>24} {'Test mean [CI]':>24} {'Change':>8} {'Sig?':>4}")

        for qos_enabled, iodepth, qos_weight, workload in configs:
            base_vals = pool_iterations_across_runs(
                base_runs, qos_enabled, iodepth, qos_weight, workload, metric_key)
            test_vals = pool_iterations_across_runs(
                test_runs, qos_enabled, iodepth, qos_weight, workload, metric_key)

            if not base_vals or not test_vals:
                continue

            result = compare_results(base_vals, test_vals)
            comparisons.append(result)

            bs = result["baseline"]
            ts = result["test"]
            pct = result["pct_change"]
            ttest = result["ttest"]

            # Format label
            if qos_enabled:
                label = f"qd={iodepth}"
                if qos_weight is not None:
                    label += f" w={qos_weight}"
            else:
                label = f"qd={iodepth}"
            if workload:
                label += f" {workload}"

            # Format stats
            base_str = f"{format_us(bs.mean)} [{format_us(bs.ci_low)}, {format_us(bs.ci_high)}]"
            test_str = f"{format_us(ts.mean)} [{format_us(ts.ci_low)}, {format_us(ts.ci_high)}]"
            change_str = f"{pct:+.1f}%"

            n_min = min(bs.n, ts.n)
            if n_min < 2:
                sig_str = "N/A"
            elif n_min < 5:
                sig_str = colored(f"n={n_min}", "yellow")
            elif ttest.significant:
                d_label = _effect_size_label(ttest.effect_size)
                sig_str = colored(
                    f"YES (p<0.05, d={abs(ttest.effect_size):.2f})",
                    "green" if pct < 0 else "red"
                )
            else:
                d_label = _effect_size_label(ttest.effect_size)
                sig_str = f"NO  (p>=0.05, d={abs(ttest.effect_size):.2f})"

            print(f" {label:>7} {base_str:>24} {test_str:>24} {change_str:>8} {sig_str}")

        print()

    # Summary
    print(colored("-- Summary ", "cyan") + "-" * 63)
    significant_regressions = [c for c in comparisons
                               if c["ttest"].significant and c["pct_change"] > 0]
    significant_improvements = [c for c in comparisons
                                if c["ttest"].significant and c["pct_change"] < 0]

    if significant_regressions:
        print(colored(f"{len(significant_regressions)} statistically significant regression(s) detected.", "red"))
    elif significant_improvements:
        print(colored(f"{len(significant_improvements)} statistically significant improvement(s) detected.", "green"))
    else:
        print("No statistically significant regressions detected.")

    # Check sample sizes
    min_samples = min((min(c["baseline"].n, c["test"].n) for c in comparisons), default=0)
    if min_samples < 5:
        print(colored(f"* Low sample sizes (min n={min_samples}). Rerun with --iterations 5+ for higher confidence.", "yellow"))

    # Markdown output
    if args.output:
        md = generate_commit_comparison_report(
            base_runs, test_runs, comparisons, metric_key,
            base_commit, test_commit, base_branch, test_branch,
            base_dirty, test_dirty,
        )
        out_path = Path(args.output)
        with open(out_path, "w") as f:
            f.write(md)
        print(f"\nMarkdown report written to: {out_path}")

    print()
    return 0


def cmd_compare(args) -> int:
    """Compare two result sets or two commits."""
    # Route to appropriate comparison mode
    has_dirs = args.baseline and args.test
    has_commits = args.base_commit and args.test_commit

    if has_dirs:
        return _compare_directories(args.baseline, args.test)
    elif has_commits:
        return _compare_commits(args)
    else:
        print(colored(
            "Error: Provide either (-b/--baseline + -t/--test) or (--base-commit + --test-commit)",
            "red"
        ), file=sys.stderr)
        return 1


def _print_condition_detail(c, hw_queues: Optional[int] = None) -> None:
    """Print detailed info for a single condition profile."""
    print()
    print(colored(f"Condition {c.id}: {c.name}", "cyan"))
    print(f"  {c.description}")
    print(f"  Mechanism: {c.mechanism}")
    print()

    if c.use_baseline_path:
        print(f"  Depths:    {c.depths}")
        print(f"  Path:      Delegates to baseline overhead check (QoS off vs on)")
    else:
        qfrac = f"{c.queue_fraction}" if c.queue_fraction is not None else "all"
        print(f"  QFrac:     {qfrac}  (min_queues={c.min_queues})")
        print(f"  Jobs/q:    high={c.high_jobs_per_queue}  normal={c.normal_jobs_per_queue}"
              f"  (min_high={c.min_high}, min_normal={c.min_normal})")
        print(f"  Depths:    {c.depths}")
        print(f"  Weights:   {c.weights}")
        print(f"  Runtime:   {c.runtime}s x {c.iterations} iterations")

    if c.namespace_policy:
        print(f"  NS policy: {c.namespace_policy}")
    if c.workload_params:
        print(f"  Workload:  {c.workload_params}")
    if c.run_buffered:
        print(f"  Buffered:  yes")
    if c.pass_criteria:
        print(f"  Pass:      {c.pass_criteria}")

    # Show resolved config for specific hardware
    if hw_queues and not c.use_baseline_path:
        cfg = c.resolve(hw_queues)
        active = cfg.max_queues or hw_queues
        total = cfg.high_numjobs + cfg.normal_numjobs
        print()
        print(f"  Resolved for {hw_queues} HW queues:")
        print(f"    Active queues: {active}")
        print(f"    High jobs:     {cfg.high_numjobs}")
        print(f"    Normal jobs:   {cfg.normal_numjobs}")
        print(f"    Jobs/queue:    {total / active:.1f}")

        # Per-queue write bytes
        pqwb_parts = []
        for d in c.depths:
            b = c.per_queue_write_bytes(d)
            pqwb_parts.append(f"{b / (1024**2):.1f} MB @ QD{d}")
        if pqwb_parts:
            print(f"    Write bytes/q: {', '.join(pqwb_parts)}")
    print()


def cmd_conditions(args) -> int:
    """List available load condition profiles."""
    # If device specified, resolve configs for that hardware
    hw_queues = None
    if args.device:
        device_name = args.device.replace("/dev/", "")
        try:
            dev = NVMeDevice(device_name)
            hw_queues = dev.get_hw_queue_count()
        except Exception as e:
            print(colored(f"Warning: Could not query device: {e}", "yellow"), file=sys.stderr)

    # Single condition detail view
    if args.condition_id:
        try:
            c = get_condition(args.condition_id)
        except KeyError as e:
            print(colored(f"Error: {e}", "red"), file=sys.stderr)
            return 1
        _print_condition_detail(c, hw_queues)
        return 0

    # List all conditions
    conditions = list_conditions()
    print()
    if hw_queues:
        print(colored(f"Load Condition Profiles (resolved for {hw_queues} HW queues)", "cyan"))
        print()
        header = f"{'ID':>2}  {'Name':<30} {'Mechanism':<25} {'Active':>6} {'Jobs':>10} {'Depths':<12}"
        print(colored(header, "cyan"))
        print("-" * len(header))

        for c in conditions:
            if c.use_baseline_path:
                active_str = "N/A"
                jobs_str = "N/A"
            else:
                cfg = c.resolve(hw_queues)
                active = cfg.max_queues or hw_queues
                jobs_str = f"{cfg.high_numjobs}+{cfg.normal_numjobs}"
                active_str = str(active)

            depths_str = str(c.depths)
            if len(depths_str) > 11:
                depths_str = depths_str[:8] + "..."

            print(f" {c.id:>1}  {c.name:<30} {c.mechanism:<25} {active_str:>6} {jobs_str:>10} {depths_str:<12}")
    else:
        print(colored("Load Condition Profiles", "cyan"))
        print()
        header = f"{'ID':>2}  {'Name':<30} {'Mechanism':<25} {'QFrac':>6} {'Hi/q':>5} {'N/q':>5} {'Depths':<12}"
        print(colored(header, "cyan"))
        print("-" * len(header))

        for c in conditions:
            qfrac = f"{c.queue_fraction}" if c.queue_fraction is not None else "all"
            if c.use_baseline_path:
                hi_q = "N/A"
                n_q = "N/A"
            else:
                hi_q = f"{c.high_jobs_per_queue}"
                n_q = f"{c.normal_jobs_per_queue}"

            depths_str = str(c.depths)
            if len(depths_str) > 11:
                depths_str = depths_str[:8] + "..."

            print(f" {c.id:>1}  {c.name:<30} {c.mechanism:<25} {qfrac:>6} {hi_q:>5} {n_q:>5} {depths_str:<12}")

    print()
    print(f"{len(conditions)} conditions available. Use: ./nvme_qos_bench.py run -C <ID>")
    if not hw_queues:
        print("Tip: Add -d <device> to see resolved job counts for your hardware.")
    print()
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="NVMe QoS Benchmark - Developer-focused benchmarking for Linux NVMe QoS scheduler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"nvme-qos-bench {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Check command
    check_parser = subparsers.add_parser(
        "check",
        help="Check system readiness",
        epilog="Example:\n  ./nvme_qos_bench.py check"
    )

    # Run command
    run_parser = subparsers.add_parser(
        "run",
        help="Run benchmark suite",
        epilog="""Examples:
  Quick test (~5 min):
    ./nvme_qos_bench.py run -d nvme0n1p7 --quick

  Full default suite (~20 min):
    ./nvme_qos_bench.py run -d nvme0n1p7

  Stress test with high contention (~30 min):
    ./nvme_qos_bench.py run -d nvme0n1p7 --stress

  Custom depths and weights:
    ./nvme_qos_bench.py run -d nvme0n1p7 --depths 8 16 32 --weights 7 9 11

  Load condition profile:
    ./nvme_qos_bench.py run -d nvme0n1p7 -C A

  Baseline overhead check (~2 min):
    ./nvme_qos_bench.py run -d nvme0n1p7 --baseline
"""
    )
    run_parser.add_argument("-d", "--device", metavar="DEV", help="NVMe device (e.g., nvme0n1p7)")
    run_parser.add_argument("--reset-device", action="store_true",
                           help="Clear saved device preference")
    run_parser.add_argument("-o", "--output", metavar="DIR", default="./results",
                           help="Output directory (default: ./results)")
    run_parser.add_argument("-c", "--config", metavar="CFG", help="Config file or preset (quick/default/full)")
    run_parser.add_argument("--quick", action="store_true", help="Use quick preset (~5 min)")
    run_parser.add_argument("--stress", action="store_true",
                           help="Use stress preset: high contention to exercise WRR (~25-35 min)")
    run_parser.add_argument("--iterations", type=int, metavar="N", help="Iterations per config")
    run_parser.add_argument("--runtime", type=int, metavar="SEC", help="Seconds per test")
    run_parser.add_argument("--depths", type=int, nargs="+", metavar="QD", help="Queue depths to test")
    run_parser.add_argument("--weights", type=int, nargs="+", metavar="W", help="QoS weights to test")
    run_parser.add_argument("--max-depth", type=int, default=0, metavar="N",
                           help="QoS max in-flight per queue (0=full SQ depth, e.g. 16 forces host-side queuing)")
    run_parser.add_argument("--buffered", action="store_true",
                           help="Include buffered I/O (page cache) workloads")
    run_parser.add_argument("--high-numjobs", type=int, metavar="N",
                           help="Override high-priority job count")
    run_parser.add_argument("--normal-numjobs", type=int, metavar="N",
                           help="Override normal-priority job count")
    run_parser.add_argument("--normal-bs", metavar="SIZE",
                           help="Override normal-priority block size (e.g., 4k, 64k, 256k)")
    run_parser.add_argument("--normal-rw", metavar="PATTERN",
                           help="Override normal-priority I/O pattern (e.g., write, randwrite, randread)")
    run_parser.add_argument("--max-queues", type=int, metavar="N",
                           help="Pin fio to N CPUs to force N HW queues (increases per-queue contention)")
    run_parser.add_argument("--compare", action="store_true",
                           help="Generate comparison report")
    run_parser.add_argument("--baseline", action="store_true",
                           help="Quick overhead check: QD1+QD4 single-job, QoS off vs on (~2 min)")
    run_parser.add_argument("-C", "--condition", metavar="ID",
                           help="Load condition profile (A, C-D, F-I, K). Auto-scales to hardware.")

    # Conditions command
    cond_parser = subparsers.add_parser(
        "conditions",
        help="List available load condition profiles",
        epilog="""Examples:
  List all condition profiles:
    ./nvme_qos_bench.py conditions

  Show details for condition A:
    ./nvme_qos_bench.py conditions A

  Show resolved config for device:
    ./nvme_qos_bench.py conditions A -d nvme0n1p7
"""
    )
    cond_parser.add_argument("condition_id", nargs="?", default=None, metavar="ID",
        help="Show details for a specific condition (A-K)")
    cond_parser.add_argument("-d", "--device", metavar="DEV",
        help="Show resolved config for this device's HW queue count")

    # Validate command
    validate_parser = subparsers.add_parser(
        "validate",
        help="Quick functional validation (~2-3 min)",
        epilog="""Example:
  Run validation on device:
    ./nvme_qos_bench.py validate -d nvme0n1p7
"""
    )
    validate_parser.add_argument("-d", "--device", metavar="DEV", help="NVMe device (e.g., nvme0n1p7)")
    validate_parser.add_argument("--reset-device", action="store_true",
                                 help="Clear saved device preference")
    validate_parser.add_argument("-o", "--output", metavar="DIR", default="./results",
                                 help="Output directory (default: ./results)")

    # Analyze command
    analyze_parser = subparsers.add_parser(
        "analyze",
        help="Analyze existing results",
        epilog="""Examples:
  Analyze results to terminal:
    ./nvme_qos_bench.py analyze -i ./results/20260220-143012-abc1234

  Write markdown report to file:
    ./nvme_qos_bench.py analyze -i ./results/20260220-143012-abc1234 -o report.md
"""
    )
    analyze_parser.add_argument("-i", "--input", required=True, metavar="DIR", help="Results directory")
    analyze_parser.add_argument("-o", "--output", metavar="FILE", help="Write markdown report to file")

    # List command
    list_parser = subparsers.add_parser(
        "list",
        help="List all benchmark runs",
        epilog="""Examples:
  List all runs:
    ./nvme_qos_bench.py list

  List runs from specific results directory:
    ./nvme_qos_bench.py list --results-dir /path/to/results

  Filter by commit SHA prefix:
    ./nvme_qos_bench.py list --commit abc1234
"""
    )
    list_parser.add_argument("--results-dir", metavar="DIR", default="./results",
                             help="Results directory (default: ./results)")
    list_parser.add_argument("--commit", metavar="SHA", help="Filter by commit prefix")

    # Compare command
    compare_parser = subparsers.add_parser(
        "compare",
        help="Compare two result sets or commits",
        epilog="""Examples:
  Compare two result directories:
    ./nvme_qos_bench.py compare -b ./results/baseline-abc1234 -t ./results/test-def5678

  Compare two commits:
    ./nvme_qos_bench.py compare --base-commit abc1234 --test-commit def5678

  Compare with custom metric:
    ./nvme_qos_bench.py compare -b baseline-dir -t test-dir --metric iops

  Write comparison to file:
    ./nvme_qos_bench.py compare -b baseline-dir -t test-dir -o comparison.md
"""
    )
    compare_parser.add_argument("-b", "--baseline", metavar="DIR", help="Baseline results dir")
    compare_parser.add_argument("-t", "--test", metavar="DIR", help="Test results dir")
    compare_parser.add_argument("--base-commit", metavar="SHA", help="Base commit SHA prefix")
    compare_parser.add_argument("--test-commit", metavar="SHA", help="Test commit SHA prefix")
    compare_parser.add_argument("--results-dir", metavar="DIR", default="./results",
                                help="Results directory for commit comparison (default: ./results)")
    compare_parser.add_argument("--metric", metavar="NAME", default="p99_us",
                                help="Metric to compare (default: p99_us)")
    compare_parser.add_argument("-o", "--output", metavar="FILE", help="Write markdown report to file")

    args = parser.parse_args()

    try:
        if args.command == "check":
            return cmd_check(args)
        elif args.command == "run":
            return cmd_run(args)
        elif args.command == "validate":
            return cmd_validate(args)
        elif args.command == "conditions":
            return cmd_conditions(args)
        elif args.command == "analyze":
            return cmd_analyze(args)
        elif args.command == "list":
            return cmd_list(args)
        elif args.command == "compare":
            return cmd_compare(args)
        else:
            parser.print_help()
            return 0
    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        return 130
    except Exception as e:
        print(colored(f"Error: {e}", "red"), file=sys.stderr)
        print("An unexpected error occurred. This may be a bug.", file=sys.stderr)
        import traceback
        print("\nFull traceback:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
