# NVMe QoS for Linux

Enhanced JoS scheduling for the in-tree Linux NVMe host driver.

## Project Members

### Maintainers:

@Benjamin-Anderson-II
@BrandonPacewic
@TSrirama2026
@phannawich
@CameronDilworth

### Project Partner:

@godhanipayal

## Code Formatting (NVMe directory)

This repository adds a convenience target to format only the NVMe driver sources in `drivers/nvme/`.

- Format NVMe C sources/headers
  - Run: `make format`
  - Scope: `drivers/nvme/**/*.c` and `drivers/nvme/**/*.h`
  - Tooling: `clang-format` using the repository’s `.clang-format` config

## License

This project copies its licensing from the Linux kernel.

See [`COPYING`](./COPYING) for more information.
