# Contributing Guide

This document describes how to contribute to the Enhanced NVMe QoS Scheduler project.  
All contributions must meet the Definition of Done (DoD) defined in the Team Charter.

## Code of Conduct
All contributors are expected to communicate professionally.  
Escalate issues respectfully following the conflict-resolution process in the charter.  
Report persistent non-responsiveness or inappropriate conduct to the Team Lead and course staff.

## Getting Started
### Prerequisites
- Linux kernel source (v6.x or later)
- gcc, make, clang-format, checkpatch.pl
- Access to the project repository (nvme-qos-linux)
- fio and blktrace for benchmarking (optional)

### Setup
```bash
git clone https://github.com/Enhanced-NVMe-QoS-Scheduler/nvme-qos-linux
cd nvme-qos-linux
make modules
```

## Branching and Workflow

- Default branch: master (stable/development)
- Feature branches: feat/<short-description>
- Hotfix branches: fix/<short-description>

## Workflow

- Create a feature branch from master
- Commit and push incremental changes
- Open a pull request (PR) into dev
- After review and CI pass, merge to main

All merges to main require two peer approvals and must meet the DoD.

## Issues and Planning

Track tasks in GitHub Issues using labels such as enhancement, bug, research, or documentation.
Each issue must include a clear description, expected outcome, and assigned owner.

## Commit Messages

Use Conventional Commits format:

```
<type>(scope): summary
```

Example:

```
feat(scheduler): implement latency-aware dispatch
fix(ci): correct checkpatch path
```

Types: feat, fix, docs, test, chore, refactor, style


## Code Style and Linting

Formatter: clang-format
Linter: checkpatch.pl

Commands:

```
clang-format -i src/*.c
./scripts/checkpatch.pl --strict --file src/yourfile.c
```

All commits must pass both checks before push.

## Testing

- Run tests before submitting code.
- Build test: make modules
- Functional: test in VM with NVMe devices
- Performance: fio benchmark
- Regression: no kernel panics or >5% performance loss

Example:

```
fio tests/qos-test.fio
```

## Pull Requests and Reviews

Each PR must:

- Build and pass all CI checks
- Include benchmark or test data
- Reference related issue numbers
- Contain clear documentation in the description
- Reviewers must approve code quality, test completeness, and documentation.
- Large PRs should be split into smaller logical pieces.

## CI/CD

CI is handled by GitHub Actions.
Workflow file: (To be added)

Required jobs:

- lint-check: runs checkpatch.pl
- build-test: compiles kernel modules
- benchmark-check: validates performance thresholds

All CI jobs must pass before merge.

## Security

- Do not commit sensitive credentials or metadata.
- Report potential security issues to the Team Lead and Project Partner immediately.
- All kernel-level code changes require manual review.

## Documentation

All PRs must:

- Update README or docs/
- Add a changelog entry under docs/changelog.md
- Include inline documentation and comments
- Add any benchmark logs under benchmarks/
