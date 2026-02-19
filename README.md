# NVMe QoS for Linux

A lightweight, per-queue QoS scheduler for the in-tree Linux NVMe host driver.
It reduces p99 read latency for latency-sensitive I/O under contention using a
two-class weighted round-robin (WRR) dispatch mechanism, while preserving
throughput and adding minimal CPU overhead. When disabled, the driver behaves
identically to upstream.

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for setup, build instructions, and
contribution guidelines.

## Maintainers

@Benjamin-Anderson-II, @BrandonPacewic, @TSrirama2026, @phannawich,
@CameronDilworth

**Project Partner:** @godhanipayal

## License

This project is licensed under GPL-2.0 with the Linux-syscall-note exception,
the same license as the Linux kernel. See [`COPYING`](./COPYING) for details.
All contributions are subject to this license.
