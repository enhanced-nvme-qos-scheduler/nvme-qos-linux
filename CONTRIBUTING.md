# Contributing Guide

This document describes how to contribute to the Enhanced NVMe QoS Scheduler
project. All contributions must meet the [Definition of Done](#definition-of-done)
before they can be merged.

## Code of Conduct

All contributors are expected to communicate professionally. Escalate issues
respectfully and report inappropriate conduct to the maintainers and project
partner.

## Prerequisites

- Linux kernel source tree (this repository, based on v6.x+)
- Build toolchain: `gcc`, `make`, `flex`, `bison`, `libelf-dev`, `libssl-dev`
- Style checking: `checkpatch.pl` (included in-tree at `scripts/checkpatch.pl`)
- Optional: `clang-format` (config at `.clang-format`), `fio` and `blktrace`
  for benchmarking
- An out-of-tree build directory is recommended

## Local Setup

```bash
# Clone the repository (blobless clone — full history, file contents fetched on demand)
git clone --filter=blob:none https://github.com/Enhanced-NVMe-QoS-Scheduler/nvme-qos-linux
cd nvme-qos-linux

# Install the pre-commit hook (runs checkpatch on staged changes)
./scripts/install-hooks.sh
```

> **Tip:** If you only need to build or make a quick fix, `--depth=1` gives the
> smallest download. Note that `git rebase` requires history back to the merge
> base with `master`, so you may need to run `git fetch --unshallow` or
> `git fetch --deepen=<N>` before rebasing.

### Building

An out-of-tree build directory is recommended:

```bash
# One-time setup
mkdir -p ~/kbuild/nvme-dev
make O=~/kbuild/nvme-dev defconfig
./scripts/config --file ~/kbuild/nvme-dev/.config --enable NVME_QOS
make O=~/kbuild/nvme-dev oldconfig

# Build the NVMe module
make O=~/kbuild/nvme-dev M=drivers/nvme/host

# Full kernel build
make O=~/kbuild/nvme-dev -j$(nproc)
```

### LSP Support (clangd)

After building, generate a `compile_commands.json` for clangd or other
LSP-based editors:

```bash
python3 scripts/clang-tools/gen_compile_commands.py
```

This creates `compile_commands.json` in the current directory. Most editors
(VS Code, Neovim, Emacs) will pick it up automatically for jump-to-definition,
diagnostics, and autocompletion. Assuming they have `clangd` language server support.

Additionally add the following to your `.clangd` configuration file to ensure
that all QoS code is always enabled and parsed correctly:

```yaml
CompileFlags:
  Add:
    - -DCONFIG_NVME_QOS=1
```

## Running CI, Linters, and Formatters Locally

### Linting (checkpatch.pl)

The project enforces Linux kernel coding style via `checkpatch.pl`. Only
**errors** are blocking; warnings are informational.

```bash
# Full check: uncommitted changes + all commits vs master
./scripts/lint.sh

# Fast check: staged changes only (also runs automatically via pre-commit hook)
./scripts/lint.sh --fast
```

The linter checks for:
- Kernel coding style errors (via `checkpatch.pl`)
- Spaces instead of tabs in QoS code
- Trailing whitespace
- Merge conflict markers

### CI

Pull requests automatically run the **checkpatch** job via GitHub Actions
(`.github/workflows/ci.yaml`). It validates kernel coding style on changed
files under `drivers/nvme/host/` and fails only on errors.

### Formatting (optional)

A `.clang-format` config is provided for IDE integration but is not enforced
in CI. The authoritative style checker is `checkpatch.pl`.

## Contribution Workflow

### 1. Pick or create an issue

All work should be tracked by a GitHub Issue. If one doesn't exist for your
change, open one first with a clear description and expected outcome.

### 2. Create a branch

Branch from `master` using the naming convention:

```
<username>/<short-description>
```

Examples:
- `branp/add-tracepoints`
- `phan/fix-doorbell-write`

### 3. Make your changes

- Follow the [Linux kernel coding style](https://www.kernel.org/doc/html/latest/process/coding-style.html)
  (tabs, 80-column lines, K&R braces).
- All QoS code must be wrapped with `#ifdef CONFIG_NVME_QOS` / `#endif`.
- QoS functions should be prefixed with `nvme_qos_`.
- Run `./scripts/lint.sh` before pushing.

### 4. Write good commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/) format:

```
<type>(scope): summary

Optional body with context.
```

Types: `feat`, `fix`, `docs`, `test`, `chore`, `refactor`, `style`

Examples:
```
feat(pci): add tracepoints for QoS dispatch decisions
fix(pci): use irqsave variant for sq_lock in kick path
docs: update CONTRIBUTING with branch naming convention
```

### 5. Open a pull request

Open a PR against `master`. Your PR description should include:

- **`Closes #<issue>`** linking the related issue
- **Summary** section with bullet points describing what changed and why
- **Benchmark data** if the change touches the hot path (dispatch, completion,
  submission). Use the benchmark tool at `tools/nvme-qos-bench/` or provide
  `fio` results.

Example PR structure:
```markdown
Closes #42.

### Summary
- Added tracepoints for dispatch class selection
- Traces fire on every QoS dequeue decision

### Benchmarks
(paste benchmark output or "N/A - no hot-path changes")
```

### 6. Code review

- Every PR requires **2 approvals** from team members before merging.
- Reviewers check for: correctness, kernel coding style, test coverage,
  and documentation.
- Large PRs should be split into smaller logical pieces when possible.
- Address all review feedback before re-requesting review.

## Definition of Done

A PR is merge-ready when **all** of the following are satisfied:

- [ ] CI passes (checkpatch finds zero errors)
- [ ] 2 team members have approved the PR
- [ ] Benchmark data is included if the change touches hot-path code
  (`nvme_queue_rq`, `nvme_qos_dispatch`, `nvme_handle_cqe`, etc.)
- [ ] PR description links the related issue (`Closes #N`)
- [ ] PR description includes a Summary section explaining the changes
- [ ] No unresolved review comments remain

## Reporting Bugs and Requesting Changes

Use [GitHub Issues](https://github.com/Enhanced-NVMe-QoS-Scheduler/nvme-qos-linux/issues)
to report bugs or request features. When filing an issue, include:

- **What you expected** vs. **what happened**
- Steps to reproduce (kernel version, config, workload)
- Relevant logs, stack traces, or benchmark output
- The NVMe device model and firmware version if hardware-specific

## Getting Help

- **Discord**: The team coordinates via Discord. Reach out to a
  [maintainer](https://github.com/Enhanced-NVMe-QoS-Scheduler/nvme-qos-linux#maintainers)
  for a server invite link.
- **GitHub Issues / PR comments**: For technical discussions tied to specific
  work, comment directly on the relevant issue or pull request.

## Security

- Do not commit credentials, keys, or sensitive metadata.
- Report potential security issues to the maintainers and project partner
  immediately.
- All kernel-level code changes require manual review.

## License

This project is licensed under GPL-2.0 with the Linux-syscall-note exception,
the same license as the Linux kernel. See [`COPYING`](./COPYING) for details.
All contributions are subject to this license.
