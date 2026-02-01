# NVMe QoS for Linux

Enhanced QoS scheduling for the in-tree Linux NVMe host driver.

## Project Members

### Maintainers:

@Benjamin-Anderson-II
@BrandonPacewic
@TSrirama2026
@phannawich
@CameronDilworth

### Project Partner:

@godhanipayal

## Development

### Setup

Install the pre-commit hook to automatically check code before committing:

```bash
./scripts/install-hooks.sh
```

### Linting

The project uses `checkpatch.pl` (the Linux kernel's style checker) to enforce coding standards.

```bash
# Check all QoS code (uncommitted changes + QoS-specific checks)
./scripts/lint.sh

# Check only staged changes (runs automatically via pre-commit hook)
./scripts/lint.sh --fast
```

The linter checks for:
- Kernel coding style errors (via checkpatch.pl)
- Spaces instead of tabs in QoS code
- Trailing whitespace
- Merge conflict markers

### CI

Pull requests automatically run:
- **checkpatch** - Validates kernel coding style on changed files (errors only, warnings ignored)

## License

This project copies its licensing from the Linux kernel.

See [`COPYING`](./COPYING) for more information.
