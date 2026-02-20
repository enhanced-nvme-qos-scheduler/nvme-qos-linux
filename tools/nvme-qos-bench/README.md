# NVMe QoS Benchmark

Developer-focused benchmarking framework for the Linux NVMe QoS scheduler. Measures p99 latency, throughput, and CPU overhead under controlled mixed-workload conditions.

## Requirements

| Requirement | Details |
|------------|---------|
| Python | 3.8 or later |
| fio | Version with JSON output support |
| Python packages | `pyyaml`, `jinja2`, `numpy`, `scipy` |
| Privileges | Root (required for sysfs access and direct I/O) |
| Test device | Dedicated NVMe device or partition (data will be overwritten) |
| Kernel | CONFIG_NVME_QOS=y recommended (baseline tests work without it) |

Install dependencies:
```bash
pip install pyyaml jinja2 numpy scipy
apt install fio
```

## Quick Start

```bash
# 1. Check system readiness
sudo ./nvme_qos_bench.py check

# 2. Run quick validation (~5 min)
sudo ./nvme_qos_bench.py run --quick -o ./results

# 3. View results
./nvme_qos_bench.py analyze -i ./results/20260220-143012-abc1234
```

## Commands

### check

Verify system readiness before running benchmarks.

```bash
sudo ./nvme_qos_bench.py check
```

Checks:
- Root privileges
- fio availability
- NVMe device presence
- QoS sysfs interface (CONFIG_NVME_QOS)

### run

Execute benchmark suite with specified configuration.

```bash
# Interactive device selection (first run)
sudo ./nvme_qos_bench.py run --quick -o ./results

# Explicit device selection
sudo ./nvme_qos_bench.py run -d nvme0n1p7 --quick

# Use saved device preference
sudo ./nvme_qos_bench.py run --quick

# Custom depths and weights
sudo ./nvme_qos_bench.py run -d nvme0n1p7 --depths 16 32 64 --weights 4 9 19
```

**Configuration Presets:**

| Preset | Runtime | Iters | Depths | Weights | Ramp | Est. Time |
|--------|---------|-------|--------|---------|------|-----------|
| `--quick` | 30s | 2 | 16, 32 | 9 | 3s | ~5 min |
| default | 60s | 5 | 16, 32, 64 | 9 | 5s | ~30-35 min |
| `--stress` | 30s | 3 | 32, 64 | 1, 4, 9, 19 | 5s | ~30 min |
| full | 60s | 10 | 1-128 (7 depths) | 1, 4, 9, 19, 99 | 10s | ~14 hr |

**Key Flags:**

| Flag | Description |
|------|-------------|
| `-d DEV` | Target device (e.g., `nvme0n1p7`) |
| `--reset-device` | Clear saved device preference |
| `-o DIR` | Output directory (default: `./results`) |
| `-c CFG` | Config file or preset name |
| `--quick` | Quick preset (~2-3 min) |
| `--stress` | High contention preset for WRR validation |
| `--iterations N` | Override iteration count |
| `--runtime SEC` | Override test duration |
| `--depths QD [QD ...]` | Queue depths to test |
| `--weights W [W ...]` | QoS weights to test |
| `--max-depth N` | Limit QoS in-flight depth (0=unlimited) |
| `--baseline` | Quick overhead check: QD1+QD4, QoS off vs on (~2 min) |
| `-C ID` | Load condition profile (A, C-D, F-I, K) |
| `--max-queues N` | Pin fio to N CPUs (forces N HW queues) |
| `--normal-bs SIZE` | Override normal-priority block size |
| `--normal-rw PATTERN` | Override normal-priority I/O pattern |
| `--compare` | Generate comparison report |

### conditions

List and inspect available load condition profiles.

```bash
# List all profiles
./nvme_qos_bench.py conditions

# Show details for condition A
./nvme_qos_bench.py conditions A

# Show resolved config for device
./nvme_qos_bench.py conditions A -d nvme0n1p7
```

Condition profiles auto-scale workload parameters (job counts, queue pinning) based on device hardware queue count.

### validate

Quick functional validation to verify QoS behavior (~2-3 min).

```bash
sudo ./nvme_qos_bench.py validate -d nvme0n1p7
```

Runs a minimal test matrix and checks that QoS improves p99 latency.

### analyze

Analyze existing benchmark results.

```bash
# Print analysis to terminal
./nvme_qos_bench.py analyze -i ./results/20260220-143012-abc1234

# Write markdown report to file
./nvme_qos_bench.py analyze -i ./results/20260220-143012-abc1234 -o report.md
```

### list

List all benchmark runs in results directory.

```bash
# List all runs
./nvme_qos_bench.py list

# Filter by commit SHA prefix
./nvme_qos_bench.py list --commit abc1234

# Specify results directory
./nvme_qos_bench.py list --results-dir /path/to/results
```

### compare

Compare two result sets or commits.

```bash
# Compare two result directories
./nvme_qos_bench.py compare -b ./results/baseline-abc1234 -t ./results/test-def5678

# Compare two commits
./nvme_qos_bench.py compare --base-commit abc1234 --test-commit def5678

# Compare custom metric
./nvme_qos_bench.py compare -b baseline-dir -t test-dir --metric iops

# Write comparison to file
./nvme_qos_bench.py compare -b baseline-dir -t test-dir -o comparison.md
```

**Flags:**

| Flag | Description |
|------|-------------|
| `-b DIR` | Baseline results directory |
| `-t DIR` | Test results directory |
| `--base-commit SHA` | Base commit SHA prefix |
| `--test-commit SHA` | Test commit SHA prefix |
| `--results-dir DIR` | Results directory for commit lookup (default: `./results`) |
| `--metric NAME` | Metric to compare (default: `p99_us`) |
| `-o FILE` | Write markdown report to file |

## Output Files

Results are saved to `results/YYYYMMDD-HHMMSS-<commit>/`:

| File | Description |
|------|-------------|
| `metadata.json` | System info, git commit, config parameters |
| `aggregate.json` | Computed statistics (mean, stdev, p-values, kernel QoS counters) |
| `data.csv` | Flattened raw data for spreadsheet analysis |
| `summary.md` | Markdown report with tables and analysis |
| `comparison.md` | Baseline vs QoS side-by-side comparison (when --compare used) |
| `dmesg.txt` | Kernel logs captured during test (if available) |
| `raw/*.json` | Per-iteration fio JSON output files |

## Terminal Output

Example output from `run --quick`:

```
nvme-qos-bench 0.0.1 | nvme0n1p7 | kernel 6.18.0-rc2 | QoS: available
Device: Samsung 990 Pro (1.95 TB) | HW queues: 8
Config: quick (2 iters, 30s runtime, ramp 3s)
───────────────────────────────────────────────────────────────────
baseline qd=16:     p99= 2847µs  iops=  156K  cpu=12.3%  [33.2s]
baseline qd=32:     p99= 3102µs  iops=  162K  cpu=12.8%  [33.1s]
qos w=9  qd=16:     p99= 1203µs  iops=  155K  cpu=13.1%  [33.2s]  -57.7%
qos w=9  qd=32:     p99= 1456µs  iops=  160K  cpu=13.4%  [33.1s]  -53.1%
───────────────────────────────────────────────────────────────────
Summary: p99 -57.7% to -53.1% | iops -0.6% to -1.2% | cpu +0.8% to +0.6%
```

Delta percentage shows change relative to baseline for the same queue depth.

## Workloads

### Mixed Workload (Primary QoS Validation)

The benchmark uses a two-priority mixed workload to validate QoS scheduler behavior:

| Priority | I/O Pattern | Block Size | ioprio | Job Count |
|----------|-------------|------------|--------|-----------|
| High | Random read | 4 KiB | RT (0) | 1 |
| Normal | Sequential write | 1 MiB | BE (4) | 4 |

Ratio: 20% high-priority load, 80% normal-priority load (by job count).

Goal: QoS scheduler should reduce high-priority p99 latency without significantly impacting aggregate throughput or CPU overhead.

### Contention Requirements

NVMe devices complete requests quickly. At low queue depths, requests complete before new ones arrive (no queuing).

**Minimum depths for meaningful QoS testing:**

| Test Type | Min QD | Rationale |
|-----------|--------|-----------|
| Single priority | 1, 4, 8 | Isolation tests, overhead checks |
| Mixed workload | **16+** | Requires sustained queuing to exercise WRR scheduler |

Depths below 16 in mixed tests may show no QoS benefit due to lack of contention.

## QoS Controls

The benchmark automatically configures these sysfs interfaces:

| Interface | Type | Purpose | Values |
|-----------|------|---------|--------|
| `/sys/class/nvme/nvme0/qos_enable` | per-controller | Enable/disable QoS scheduler | `0` (off), `1` (on) |
| `/sys/class/nvme/nvme0/qos_weight` | per-controller | High-priority weight for WRR | `1`-`99` (default: `9`) |
| `/sys/block/nvme0n1/qos_policy` | per-namespace | Force priority classification | `default`, `force_high`, `force_normal` |

Original QoS state is automatically restored after benchmark completion.

## When QoS is Unavailable

If `CONFIG_NVME_QOS` is not enabled in the kernel:

- Warning displayed at start and end of run
- Baseline benchmarks run normally
- QoS comparison tests are skipped
- Results still provide useful latency/throughput baseline data

**To enable QoS:**

```bash
./scripts/config --file /path/to/kernel/.config --enable NVME_QOS
make oldconfig
make -j$(nproc)
```

See `CLAUDE.md` for full QoS build instructions.

## Key Metrics

| Metric | Description | Goal |
|--------|-------------|------|
| p99 latency | 99th percentile latency for high-priority jobs | **Reduce** under contention |
| IOPS | Aggregate operations per second (all jobs) | Maintain (minimal regression) |
| CPU % | System CPU utilization | Minimal increase |

## Safety

- Device selection requires explicit confirmation on first use
- Device preference is saved after confirmation (`~/.config/nvme-qos-bench/config.yaml`)
- Original QoS state is restored after benchmarks (even on interrupt)
- **Warning:** Selected device will be overwritten with random data
- **Never** use a mounted filesystem or root partition for testing

## Version

```bash
./nvme_qos_bench.py --version
```

Current version: `0.0.1`

## License

This project follows the Linux kernel's licensing.

See [`COPYING`](../../COPYING) for details.
