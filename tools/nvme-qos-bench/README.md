# NVMe QoS Benchmark

Developer-focused benchmarking framework for the Linux NVMe QoS scheduler. Measures latency, throughput, and CPU overhead with terminal-based output optimized for development iteration and evidence gathering.

## Quick Start

```bash
# Check system readiness
sudo ./nvme_qos_bench.py check

# Run quick sanity check (~5 min)
sudo ./nvme_qos_bench.py run --quick -o ./results

# Run standard benchmark
sudo ./nvme_qos_bench.py run -o ./results

# Analyze results
./nvme_qos_bench.py analyze -i ./results/run_2025-01-15_14-30-00
```

## Requirements

- Python 3.8+
- fio (with JSON output support)
- PyYAML, Jinja2, NumPy, SciPy
- Root privileges (for sysfs access and direct I/O)
- Dedicated NVMe device/partition (data will be overwritten)

Install dependencies:
```bash
pip install pyyaml jinja2 numpy scipy
apt install fio
```

## Usage

### Check System

```bash
sudo ./nvme_qos_bench.py check
```

Verifies:
- Running as root
- fio is available
- NVMe devices are present
- QoS sysfs interface exists (CONFIG_NVME_QOS)

### Run Benchmarks

```bash
# First run: interactive device selection
sudo ./nvme_qos_bench.py run --quick -o ./results

# Subsequent runs: uses saved device preference
sudo ./nvme_qos_bench.py run -o ./results

# Override saved preference
sudo ./nvme_qos_bench.py run -d nvme0n1p7 -o ./results

# Reset saved preference
sudo ./nvme_qos_bench.py run --reset-device

# Custom parameters
sudo ./nvme_qos_bench.py run --depths 16 32 64 128 --weights 4 9 19 -o ./custom
```

### Configuration Presets

| Preset | Runtime | Iterations | Depths | Weights |
|--------|---------|------------|--------|---------|
| quick | ~5 min | 2 | 16,32 | 9 |
| default | ~30 min | 5 | 16,32,64 | 9 |
| full | ~2-3 hr | 10 | 1-128 | 1,4,9,19,99 |

```bash
sudo ./nvme_qos_bench.py run --quick
sudo ./nvme_qos_bench.py run -c default
sudo ./nvme_qos_bench.py run -c full
```

### Analyze Results

```bash
./nvme_qos_bench.py analyze -i ./results/run_2025-01-15_14-30-00
```

### Compare Result Sets

```bash
./nvme_qos_bench.py compare -b ./baseline_results -t ./qos_results
```

## Output

Results are saved to `results/run_YYYY-MM-DD_HH-MM-SS/`:

| File | Description |
|------|-------------|
| `metadata.json` | System info, git commit, config |
| `aggregate.json` | Computed statistics |
| `data.csv` | Raw data for spreadsheet analysis |
| `summary.md` | Markdown report |
| `comparison.md` | Baseline vs QoS side-by-side |
| `dmesg.txt` | Kernel logs during test |
| `raw/` | Per-iteration fio JSON files |

## Terminal Output

```
nvme-qos-bench v1.0 | nvme0n1p7 | kernel 6.14.0 | QoS: available
---
baseline qd=16:     p99= 2847us iops=  156K cpu=12.3% [60.1s]
baseline qd=32:     p99= 3102us iops=  162K cpu=12.8% [60.0s]
baseline qd=64:     p99= 3891us iops=  171K cpu=13.5% [60.2s]
qos w=9  qd=16:     p99= 1203us iops=  155K cpu=13.1% [60.1s]  -57.7%
qos w=9  qd=32:     p99= 1456us iops=  160K cpu=13.4% [60.0s]  -53.1%
qos w=9  qd=64:     p99= 1892us iops=  169K cpu=14.0% [60.1s]  -51.4%
---
summary: p99 -57.7% to -51.4% | iops -0.9% to -1.3% | cpu +0.6% to +0.5%
```

## Workloads

### Mixed Workload (Primary QoS Validation)

- **High-priority**: 4K random reads, IOPRIO_CLASS_RT
- **Normal-priority**: 1M sequential writes, IOPRIO_CLASS_BE
- **Ratio**: 20% high-priority / 80% normal-priority
- **Purpose**: Demonstrate p99 latency improvement under contention

### Contention Requirements

NVMe devices are fast - at low queue depths, requests complete before new ones arrive. Minimum depths for meaningful QoS testing:

- **Isolation tests** (single priority): qd=1,4,8 valid
- **Mixed workloads** (concurrent priorities): **qd >= 16 required**

## QoS Controls

The benchmark automatically controls these sysfs interfaces:

| Interface | Purpose |
|-----------|---------|
| `/sys/class/nvme/nvme0/qos_enable` | Enable/disable QoS (0/1) |
| `/sys/class/nvme/nvme0/qos_weight` | High-priority weight (default: 9) |
| `/sys/block/nvme0n1/qos_policy` | Per-namespace policy |

## When QoS is Unavailable

If `CONFIG_NVME_QOS` is not enabled in the kernel:

- Warning displayed at start and end
- Baseline benchmarks run normally
- QoS comparison tests are skipped
- Results still provide useful latency/throughput data

To enable QoS:
```bash
./scripts/config --enable NVME_QOS
make oldconfig
make -j$(nproc)
```

## Key Metrics

| Metric | Description | Goal |
|--------|-------------|------|
| p99 latency | 99th percentile latency for high-priority | **Reduce** |
| IOPS | Operations per second | Maintain |
| CPU % | CPU utilization | Minimal increase |

## Safety

- **Device selection requires explicit confirmation**
- Device preference is saved after confirmation
- Original QoS state is restored after benchmarks
- Never use mounted root partition for testing

## License

This project copies its licensing from the Linux kernel.

See [`COPYING`](../../COPYING) for more information.
