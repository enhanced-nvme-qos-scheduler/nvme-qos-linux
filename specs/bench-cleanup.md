# bench-cleanup

Production-quality cleanup of the nvme-qos-bench Python tool. No new features, no tests (separate task). Goals: eliminate duplicate code, fix silent failures, fix brittle parsing, unify magic numbers, fix CLI quality (help text, flag naming, error messages, output formatting), rewrite README.

## Scope

### In scope
- `lib/constants.py` — new file, all magic numbers extracted from lib/
- `lib/metrics.py` — fix brittle FIO JSON parsing
- `lib/fio_runner.py` — fix silent failures, surface fio stderr
- `lib/kernel_stats.py` — fix silent failures
- `lib/system.py` — fix silent failures
- `lib/analysis.py` — consume constants
- `lib/output.py` — consume constants
- `lib/progress.py` — consume constants
- `lib/conditions.py` — consume constants
- `nvme_qos_bench.py` — fix argparse help strings, flag naming, error messages, output formatting, extract duplicated helpers into lib/
- `tools/nvme-qos-bench/README.md` — rewrite as minimal user guide

### Out of scope
- Unit tests (separate task)
- Splitting output.py by format (json/csv/markdown)
- Full command module split of nvme_qos_bench.py
- Completing condition profiles B–K
- New features or behaviors

---

## Style Requirements

These rules apply to **all code written or rewritten** as part of this cleanup. Every task must conform to them. They are not optional polish.

### Line length
- Hard limit: **150 characters**. Soft limit: prefer ≤ 120 where it reads naturally.

### Imports
- **PEP8 three-group ordering**, blank line between groups:
  1. stdlib (alphabetical within group)
  2. third-party (alphabetical within group)
  3. local / lib (alphabetical within group)
- No unused imports. No `import *`.

### Type hints
- **Every function signature** must have type annotations for all parameters and the return type.
- Use `Optional[X]` (or `X | None` if Python 3.10+) for nullable returns.
- Use `list[X]`, `dict[K, V]` (not `List`, `Dict`) for stdlib containers.
- Exception: trivial lambdas, `**kwargs` forwarding.

### Docstrings
- Add a one-line docstring to any function whose purpose is not immediately obvious from the name and signature.
- Do **not** add docstrings to functions that are self-explanatory (e.g., `def get_qos_weight() -> int:`).
- No boilerplate Args/Returns blocks unless the function has non-obvious parameters.

### Function decomposition
- **Prefer less nesting over short functions.** The goal is reducing indentation depth, not line count.
- Apply these patterns wherever encountered:
  - **Early return / guard clauses**: `if not condition: return` instead of `if condition: <big block>`
  - **Avoid else after return**: never write `if x: return y\nelse: <rest>`; just fall through
  - **Extract at 3+ nesting levels**: any block nested ≥ 3 levels deep must be pulled into a named helper
  - **Flatten loops**: prefer list comprehensions or `itertools` over nested `for` loops where readability is equal or better
- No function needs a strict line-count limit. A 150-line function with flat, linear logic is better than 5 helpers that require jumping around to understand it.

### Classes vs functions
- **Functional by default.** Use module-level functions unless the code has genuine state to manage across calls.
- Classes are appropriate when: (a) there is instance state (e.g., `NVMeDevice`, `FioRunner`, `QoSKernelStats`), or (b) a group of operations share a lifecycle (init/use/cleanup).
- Do not use `@staticmethod` classes as namespaces — use a module instead.

### Private naming
- Functions and variables not part of a module's public interface use a **single underscore prefix**: `_parse_job_name()`, `_raw_dir`, etc.
- Do not use double underscore (`__`) unless name mangling is intentional.

### Error handling (lib/ modules)
- **Exceptions for programming errors**: `ValueError` for invalid arguments, `RuntimeError` for unexpected state. These indicate caller bugs.
- **None for expected failures**: file not found, permission denied, device unavailable — return `None` and optionally emit a warning. Do not raise.
- Never use bare `except:` or `except Exception: pass`. At minimum log or warn before swallowing.
- No `try/except` that converts a real error into a silent no-op.

### File internal ordering
- **Order by usage / call graph**: helpers are defined before the functions that call them.
- Within a module, order: constants/imports → dataclasses/types → private helpers → public functions → entry point (if any).
- No arbitrary section header comments (no `# ---- Section ----` banners). Let the call graph ordering speak for itself.

### Argparse (main file only)
- Argument parser setup stays inline in `main()`.
- Each subcommand's arguments are added in a clearly delineated block within `main()`, with a blank line between subcommands.
- All user-facing arguments have a `help=` string. No `argparse.SUPPRESS` on visible flags.

---

## Tasks

### T1 — Create `lib/constants.py`

Extract every magic number scattered across `lib/` into one place. No behavior change.

**Magic numbers to extract (non-exhaustive, implementor must audit all files):**

| Constant | Current location | Value |
|----------|-----------------|-------|
| `FIO_TIMEOUT_MULTIPLIER` | fio_runner.py | `2` (2× runtime) |
| `FIO_TIMEOUT_BUFFER_S` | fio_runner.py | `60` |
| `TRIM_TIMEOUT_S` | device.py | `60` |
| `FAIRNESS_DEMAND_TOLERANCE` | kernel_stats.py | `0.20` |
| `FAIRNESS_WEIGHT_TOLERANCE` | kernel_stats.py | `0.15` |
| `CONFIDENCE_LEVEL` | analysis.py | `0.95` |
| `PCT_CHANGE_GREEN_THRESHOLD` | progress.py | `-30.0` |
| `PCT_CHANGE_YELLOW_THRESHOLD` | progress.py | `-10.0` |
| `PCT_CHANGE_WARN_THRESHOLD` | progress.py | `5.0` |
| `DEFAULT_QOS_WEIGHT` | device.py / config.py | `9` |
| `NVME_QOS_MAX_BATCH` | (referenced in docs) | `4` |
| `DEBUGFS_ROOT` | kernel_stats.py | `/sys/kernel/debug` |

**Acceptance criteria:**
- `lib/constants.py` exists with all constants as module-level `ALL_CAPS` names
- All lib modules import from `constants.py` instead of using inline literals
- `grep -rn "0\.20\|0\.15\|0\.95" lib/` returns no hits (spot-check)
- No behavior change

---

### T2 — Fix brittle FIO JSON job name matching (`lib/metrics.py`)

**Current problem:** `extract_fio_metrics()` identifies high/normal priority jobs by case-sensitive substring match on job name (e.g., `"high" in job_name`). This breaks if the Jinja2 template generates a name like `"High_0"` or `"high_prio_read"`.

**Fix:**
- Use case-insensitive prefix match: job name starts with `"high"` or `"normal"` (case-insensitive)
- Or match against the known template-generated names directly (inspect templates to confirm exact names)
- If a job matches neither prefix, classify as `unclassified` and log a warning instead of silently dropping it

**Acceptance criteria:**
- Job name matching is case-insensitive
- Unmatched jobs produce a warning (not silent drop)
- All existing template job names still match correctly

---

### T3 — Fix brittle FIO percentile key extraction (`lib/metrics.py`)

**Current problem:** Percentile values are extracted with hardcoded string keys like `"99.000000"`. If FIO changes its output format (integer keys, different precision), parsing silently returns 0.

**Fix:**
- Parse percentile keys with a tolerance-based lookup: find the key closest to the target percentile value (e.g., find key where `abs(float(key) - 99.0) < 0.001`)
- Fall back gracefully with a logged warning if no close match found, not a silent zero

**Acceptance criteria:**
- Percentile extraction works if FIO uses `"99"`, `"99.0"`, or `"99.000000"` as the key
- Missing percentile logs a warning and returns `None`, not `0`

---

### T4 — Fix silent failures in `lib/fio_runner.py`

**Current problems:**
1. `run_fio()` suppresses all stderr from fio — debugging failures is impossible
2. Temporary `.fio` job files accumulate in `raw_dir` indefinitely
3. Timeout calculation (`2 * runtime + ramp + 60`) is inlined, not parameterized

**Fix:**
1. Capture fio stderr; on non-zero exit, include first N lines of stderr in the returned error/exception message
2. Clean up temp `.fio` files in a `finally` block (or after successful parse)
3. Timeout is already extractable via T1 constants — just wire it up

**Acceptance criteria:**
- On fio failure, the error message includes at least the last 5 lines of fio's stderr
- Temp job files do not accumulate after successful runs
- No new temp files left behind after a fio error either

---

### T5 — Fix silent failures in `lib/kernel_stats.py`

**Current problem:** Permission errors and missing debugfs paths are silently caught and return empty/zero values with no indication to the caller.

**Fix:**
- On first access failure, emit a one-time warning: `"kernel QoS stats unavailable: <reason>"`
- Return `None` (not empty dict) from `read_aggregate()` when unavailable so callers can distinguish "zero counters" from "could not read"
- Update all call sites in `nvme_qos_bench.py` to handle `None` result

**Acceptance criteria:**
- User sees a warning when debugfs is inaccessible (only once per run, not per-call)
- Callers can distinguish unavailable stats from all-zero stats
- No `try/except: pass` blocks remaining

---

### T6 — Fix silent failures in `lib/system.py`

**Current problems:**
1. `capture_dmesg()` has no error handling — if `dmesg` fails or permission denied, exception propagates uncaught to caller
2. `get_git_info()` uses naive repo root detection (looks for `"drivers/nvme/host"` relative path)
3. System info functions re-run subprocess on every call with no caching

**Fix:**
1. Wrap `capture_dmesg()` in try/except; on failure return `False` and print a warning, don't raise
2. Fix `get_git_info()` to use `git rev-parse --show-toplevel` instead of path heuristics
3. Cache `collect_system_info()` result in module-level variable on first call

**Acceptance criteria:**
- `capture_dmesg()` failure prints a warning, does not abort the benchmark run
- `get_git_info()` works correctly from any working directory within the repo
- Repeated calls to `collect_system_info()` do not re-run subprocesses

---

### T7 — Fix argparse help strings in `nvme_qos_bench.py`

**Current problems:** Help strings for several flags are missing, outdated (reference old flags), or misleading. `--help` output for each subcommand must be accurate and complete.

**Audit every argument in every subparser:**
- Verify description matches actual behavior
- Add missing help strings (currently `help=argparse.SUPPRESS` or empty)
- Update any that reference removed/renamed flags
- Ensure metavar is set where useful (e.g., `--depths N [N ...]`)
- Add `epilog` examples to each subcommand showing common invocations

**Acceptance criteria:**
- `./nvme_qos_bench.py --help` shows accurate top-level summary with subcommand list
- `./nvme_qos_bench.py run --help` shows all flags with correct descriptions and examples
- Same for `analyze`, `compare`, `baseline`, `scan`, `list-conditions`, `check`
- No `argparse.SUPPRESS` on user-facing flags

---

### T8 — Fix inconsistent and redundant CLI flags in `nvme_qos_bench.py`

**Audit for:**
- Flags that do the same thing under different names across subcommands (e.g., `-i` vs `-d` vs `--input` inconsistencies)
- `--quick` shorthand that overlaps with `-c quick` — decide one canonical form; if both kept, document the equivalence explicitly
- Flags that are defined but never consumed (dead code)
- Inconsistent convention: some flags use `--foo-bar`, some use `--foo_bar`

**Fix:**
- Standardize on `--foo-bar` (hyphen) for all multi-word flags
- If a flag is truly redundant with no usage, remove it
- If `--quick` and `-c quick` are kept both, document as aliases in help text

**Acceptance criteria:**
- No `--foo_bar` style flags (all hyphenated)
- No dead/unused flags
- All flag aliases documented in help text

---

### T9 — Fix error messages in `nvme_qos_bench.py`

**Current problems:**
- Several code paths raise bare exceptions or `sys.exit(1)` with no message
- Device validation errors print to stdout instead of stderr
- Some errors print stack traces to the user (unhandled exceptions in top-level)

**Fix:**
- All user-facing errors go to `sys.stderr`
- Add a top-level `try/except` in `main()` that catches unexpected exceptions and prints: `"Error: <message>. Run with --debug for full traceback."` (do not add `--debug` flag; just hint at it for future)
- Replace bare `sys.exit(1)` calls with a message before exit
- Device errors, config parse errors, and fio-not-found should have actionable fix hints (most already do — audit for missing ones)

**Acceptance criteria:**
- No bare `sys.exit(1)` without a preceding error message
- Error output goes to stderr
- A user who passes a bad flag or nonexistent device gets a clear, actionable message

---

### T10 — Fix terminal output formatting in `nvme_qos_bench.py` and `lib/progress.py`

**Current problems:**
- Column widths for result lines are inconsistent across different run types
- Some print paths use `print()` directly while others use `print_result()` / `print_header()` helpers
- Mixed use of `sys.stderr` and `sys.stdout` for progress/status messages

**Fix:**
- Audit all `print()` calls in `nvme_qos_bench.py` — route progress/status to the `progress.py` helpers
- Fix column alignment: result lines must align across all configurations (fixed-width fields for qd, weight, p99, iops, cpu, time, delta)
- Establish convention: progress/status → stderr; result data → stdout. Enforce it.

**Acceptance criteria:**
- Output columns align when multiple configurations run (spot-check with `run --quick` output)
- No raw `print()` for status/progress messages in main file (use progress.py helpers)
- Result data is stdout-only (can be piped/redirected)

---

### T11 — Extract duplicated helpers from `nvme_qos_bench.py` into lib/

**Minimal split only.** Do not move entire commands. Extract only logic that is:
1. Duplicated in two or more places, OR
2. Clearly belongs in an existing lib module by domain

**Audit to find candidates (implementor must verify):**
- Depth/weight iteration logic duplicated between `cmd_run` and `cmd_baseline`
- Result directory creation and metadata writing duplicated across commands
- QoS state save/restore calls that follow the same pattern in multiple commands

**Do not extract:**
- Anything that would require a new lib module
- Anything that is only used once

**Acceptance criteria:**
- No function body is duplicated across two commands in `nvme_qos_bench.py`
- Extracted helpers live in the most appropriate existing lib module
- Main file line count decreases (target: < 2,000 lines, from 2,774)

---

### T12 — Rewrite `tools/nvme-qos-bench/README.md`

Rewrite as an accurate minimal user guide. Match the documentation quality of CLAUDE.md (tables, clear sections, quick reference).

**Structure:**

```
# NVMe QoS Benchmark

One-paragraph description.

## Requirements

Table: Python version, fio, Python deps, root, dedicated device.

## Quick Start

4-step sequence: install deps, check, run --quick, analyze.

## Commands

### check
### run
  Presets table (quick/default/full/stress) with accurate runtimes, depths, weights.
  All flags with descriptions.
### analyze
### compare
### baseline
### scan

## Output Files

Table: file → description (accurate to current code).

## Terminal Output

Annotated example of actual output format.

## QoS Controls

Table of sysfs interfaces (already in README, verify accuracy).

## Safety

Device selection, data warning.
```

**Accuracy requirements:**
- All CLI flags in the README must match flags that actually exist in argparse
- Runtime estimates must be realistic (verify against config presets)
- Output file names must match what the code actually generates
- Version number must be updated from `v1.0` to match `lib/__init__.py`

**Acceptance criteria:**
- Every command shown in README has a corresponding subparser in the code
- Every flag shown has a corresponding `add_argument()` call
- No references to flags that don't exist
- README < 200 lines (focused, no padding)

---

### T13 — Structural refactor: reduce nesting depth throughout

Apply the style rules (early return, guard clauses, no else-after-return, extract at 3+ levels) systematically to the files with the worst nesting, as identified by audit.

**Primary targets (worst nesting confirmed by exploration):**

1. **`nvme_qos_bench.py` — `_run_interleaved_depth()`**: Described as deeply nested with hard-to-follow control flow. Refactor using guard clauses and extracted named helpers. The function's stages (setup, baseline run, QoS run, result recording) should each become a clearly named private function.

2. **`nvme_qos_bench.py` — `cmd_run()`**: Outer loop over depths/weights with inner branches for baseline vs QoS. Flatten with early return on skip conditions; extract the per-configuration execution into a helper.

3. **`lib/output.py` — `generate_markdown_report()` and siblings**: Procedural string-building with nested conditionals. Restructure so each logical section (header, table, summary) is its own `_render_*()` helper called in sequence. No nested if-chains inside string-building loops.

4. **`lib/analysis.py` — `two_sample_ttest()`**: Nested conditional p-value approximation. Flatten the lookup/interpolation logic into a separate `_tvalue_for_df()` helper.

5. **`lib/fio_runner.py` — `_run_job()`**: Nested try/subprocess/parse block. Extract parsing into `_parse_fio_output()`, keep `_run_job()` as a thin orchestrator.

**Method:**
- For each target: read the current code, identify every block nested ≥ 3 levels or every `else` after a `return`, and restructure.
- Do not change behavior. This is pure structural cleanup.
- Apply style rules: all extracted helpers get type hints; private helpers use `_` prefix; helpers go above their caller in the file.

**Acceptance criteria:**
- No function in any target file has a block nested more than 3 levels deep (counting from the `def` line)
- No `else` clause immediately follows a `return` statement in any target file
- `_run_interleaved_depth()` decomposed into ≥ 2 named private helpers
- `generate_markdown_report()` uses `_render_*()` section helpers
- Behavior is identical — same outputs for same inputs

---

## Ordering / Dependencies

```
T1 (constants)
  → T2 (metrics)       — independent
  → T3 (metrics)       — depends on T2 (same file)
  → T4 (fio_runner)    — independent
  → T5 (kernel_stats)  — independent
  → T6 (system)        — independent
  → T7 (argparse)      — independent
  → T8 (flags)         — after T7
  → T9 (errors)        — independent
  → T10 (output fmt)   — independent
  → T11 (extract)      — after T7, T8, T9, T10
  → T12 (README)       — after T7, T8
  → T13 (nesting)      — after T4, T11 (same files touched)
```

Suggested implementation order: T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10 → T11 → T13 → T12
