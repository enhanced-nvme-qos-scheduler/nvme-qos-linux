# Implementation plan

## Completed

- [x] bench-cleanup T1: Create lib/constants.py — extract all magic numbers
- [x] bench-cleanup T2: Fix brittle FIO JSON job name matching in lib/metrics.py
- [x] bench-cleanup T3: Fix brittle FIO percentile key extraction in lib/metrics.py
- [x] bench-cleanup T4: Fix silent failures in lib/fio_runner.py
- [x] bench-cleanup T5: Fix silent failures in lib/kernel_stats.py
- [x] bench-cleanup T6: Fix silent failures in lib/system.py
- [x] bench-cleanup T7: Fix argparse help strings in nvme_qos_bench.py
- [x] bench-cleanup T8: Fix inconsistent/redundant CLI flags in nvme_qos_bench.py
- [x] bench-cleanup T9: Fix error messages in nvme_qos_bench.py
- [x] bench-cleanup T10: Fix terminal output formatting
- [x] bench-cleanup T11: Extract duplicated helpers from main file into lib/ (consolidated _si() and si_format())

## In Progress
- [ ] bench-cleanup T13: Structural refactor — reduce nesting depth in _run_interleaved_depth, cmd_run, output.py, analysis.py, fio_runner.py
  - [x] T13.1: Extract `_add_metric_summary_line()` helper in lib/output.py (removed 4 duplicate blocks)
  - [ ] T13.2: Refactor `generate_markdown_report()` section builders
  - [ ] T13.3: Refactor `generate_comparison_report()` table builders
  - [ ] T13.4: Refactor `_run_interleaved_depth()` in nvme_qos_bench.py
  - [ ] T13.5: Refactor `cmd_run()` in nvme_qos_bench.py

## Pending
- [x] bench-cleanup T12: Rewrite tools/nvme-qos-bench/README.md
