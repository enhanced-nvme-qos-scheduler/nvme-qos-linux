# nvme-qos-bench

Benchmarking tool for the Linux NVMe QoS scheduler. Measures p99 latency improvement, throughput, and CPU overhead under controlled mixed-workload conditions.

## Requirements

- Python 3.8+
- fio (`apt install fio`)
- Python packages: `pip install pyyaml jinja2 numpy scipy`
- Root privileges (sysfs access and direct I/O)
- A dedicated NVMe partition for testing — **data will be overwritten**
- Kernel built with `CONFIG_NVME_QOS=y` and `CONFIG_NVME_QOS_STATS=y`

## Quick Start

```bash
sudo ./nvme_qos_bench.py check
sudo ./nvme_qos_bench.py run -d nvme0n1 -c C
./nvme_qos_bench.py analyze -i ./results/run_<timestamp>
```

## Commands

### `check`

Verify system readiness. Checks root privileges, fio, NVMe devices, QoS sysfs availability, and QoS kernel stats via debugfs.

```bash
sudo ./nvme_qos_bench.py check
```

### `run`

Run a benchmark condition. Interleaves QoS-off and QoS-on iterations at each queue depth and saves results to `./results/`.

```bash
sudo ./nvme_qos_bench.py run -d nvme0n1 -c C
sudo ./nvme_qos_bench.py run -d nvme0n1 -c E --weights 1 4 9 19 99
sudo ./nvme_qos_bench.py run -d nvme0n1 -c B --depths 32 64 128
```

| Flag | Description |
|------|-------------|
| `-d DEV` | Target device or partition (e.g. `nvme0n1`) |
| `-c/-C ID` | Condition profile to run — required (A, B, C, D, E) |
| `-o DIR` | Output directory (default: `./results`) |
| `--iterations N` | Override iteration count |
| `--runtime SEC` | Override per-test duration |
| `--depths QD ...` | Override queue depths |
| `--weights W ...` | Override QoS weights |
| `--max-depth N` | Cap QoS in-flight per queue (0 = full SQ depth) |
| `--max-queues N` | Pin fio to N CPUs to force N HW queues |
| `--normal-bs SIZE` | Override normal-priority block size (e.g. `256k`) |
| `--normal-rw PATTERN` | Override normal-priority I/O pattern (e.g. `randwrite`) |
| `--high-numjobs N` | Override high-priority job count |
| `--normal-numjobs N` | Override normal-priority job count |
| `--buffered` | Include buffered (page-cache) I/O workloads |
| `--compare` | Generate inline comparison report after run |
| `--reset-device` | Clear saved device preference |

### `conditions`

List all condition profiles, or show details for a specific one.

```bash
./nvme_qos_bench.py conditions
./nvme_qos_bench.py conditions C
./nvme_qos_bench.py conditions C -d nvme0n1   # show resolved config for device
```

### `validate`

Quick functional smoke test (~2-3 min). Checks that QoS sysfs controls work, namespace policy overrides function, and ioprio classification is active.

```bash
sudo ./nvme_qos_bench.py validate -d nvme0n1
```

### `analyze`

Analyze results from a completed run. Prints latency tables, statistical significance, and throughput sections. Optionally writes a markdown report.

```bash
./nvme_qos_bench.py analyze -i ./results/run_2026-02-21_20-20-33
./nvme_qos_bench.py analyze -i ./results/run_2026-02-21_20-20-33 -o report.md
```

### `list`

List all runs in the results directory with commit, branch, condition, and pass/fail summary.

```bash
./nvme_qos_bench.py list
./nvme_qos_bench.py list --commit abc1234
./nvme_qos_bench.py list --results-dir /path/to/results
```

## Condition Profiles

All conditions use a two-priority mixed workload: high-priority 4K random reads (ioprio RT) vs. normal-priority bulk writes (ioprio BE). Job counts and queue pinning auto-scale to the device's hardware queue count.

| ID | Name | Description |
|----|------|-------------|
| **A** | Zero-overhead baseline | QD1+QD4, single job, QoS off vs on. Confirms the scheduler adds no measurable overhead when there is no contention to arbitrate. Pass: p99 regression < 2%. |
| **B** | Few queues, high density | Packs many jobs onto 2 queues (256K normal writes). Maximum per-queue contention in a narrow queue footprint. Tests WRR arbitration under extreme density. |
| **C** | Device saturation | Half the HW queues active, 2:8 high:normal job ratio (256K normal writes). The primary target operating condition for the scheduler. |
| **D** | Majority high-prio | Many high-priority jobs against a small number of normal jobs (4K randread). Tests that normal traffic still receives service when high-priority demand exceeds its weight. |
| **E** | Weight sweep | Runs condition C across weights 1, 4, 9, 19, 99. Validates that the dispatch ratio tracks the configured WRR weight proportionally. |

## Output Files

Each run writes to `./.nvme-qos-results/run_<timestamp>/`:

| File | Contents |
|------|----------|
| `metadata.json` | System info, git commit, device, config parameters |
| `aggregate.json` | Per-depth statistics: mean, stddev, CI, p-values, kernel QoS counters, fairness |
| `data.csv` | Flattened per-iteration data for external analysis |
| `summary.md` | Full markdown report with tables and analysis |
| `dmesg.txt` | Kernel log captured during the run |
| `raw/*.json` | Raw fio JSON output for every iteration |

## QoS Sysfs Controls

The benchmark configures these automatically and restores original state on exit (including on interrupt):

| Path | Purpose | Values |
|------|---------|--------|
| `/sys/class/nvme/nvmeN/qos_enable` | Enable/disable scheduler | `0`, `1` |
| `/sys/class/nvme/nvmeN/qos_weight` | High-priority WRR weight | `1`–`99` (default: `9`) |
| `/sys/block/nvmeNnM/qos_policy` | Per-namespace priority override | `default`, `force_high`, `force_normal` |

## Safety

- Device selection requires explicit confirmation on first use; preference is saved to `~/.config/nvme-qos-bench/config.yaml`
- **The selected device will be overwritten.** Never use a mounted filesystem or root partition.
- QoS state is always restored after a run, even on Ctrl-C.

## License

This project is licensed under GPL-2.0 with the Linux-syscall-note exception,
the same license as the Linux kernel. See [`COPYING`](./COPYING) for details.
All contributions are subject to this license.
