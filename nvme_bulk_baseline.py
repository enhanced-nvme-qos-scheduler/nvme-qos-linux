#!/usr/bin/env python3
import atexit
import datetime
import os
import re
import shutil
import stat
import subprocess
import sys

DEFAULT_TARGET = "/dev/disk/by-id/nvme-eui.0025385a51a07cf9-part7"
SYMLINK = "/dev/nvme_scratch"
MOUNTPOINT = "/mnt/nvme_scratch"
EXPECTED_REAL = "/dev/nvme0n1p7"


def print_usage() -> None:
    print(
        """Usage: RAW=1 FORCE_DEVICE=1 python3 ./nvme_bulk_baseline.py [target_device]

Examples:
  python3 ./nvme_bulk_baseline.py
  python3 ./nvme_bulk_baseline.py /dev/disk/by-id/...-part7
  RAW=1 python3 ./nvme_bulk_baseline.py
  RAW=1 FORCE_DEVICE=1 python3 ./nvme_bulk_baseline.py /dev/nvme0n1p7
"""
    )


def warn(message: str) -> None:
    print(f"[WARN] {message}", file=sys.stderr)


def info(message: str) -> None:
    print(f"[INFO] {message}")


def die(message: str) -> None:
    print(f"[ERROR] {message}", file=sys.stderr)
    raise SystemExit(1)


def env_flag(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def run_command(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=check, text=True, capture_output=True)


def write_command_output(command: list[str], path: str) -> None:
    result = run_command(command, check=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(result.stdout)


def ensure_block_device(path: str) -> None:
    mode = 0
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        die(f"Target is not a block device: {path}")
    if not stat.S_ISBLK(mode):
        die(f"Target is not a block device: {path}")


def findmnt_source(target: str) -> str:
    result = run_command(["findmnt", "-n", "-o", "SOURCE", target], check=True)
    return result.stdout.strip()


def mountpoint_is_active(path: str) -> bool:
    return subprocess.run(["mountpoint", "-q", path]).returncode == 0


def mount_device(source: str, target: str) -> None:
    subprocess.run(["mount", source, target], check=True)


def unmount_target(path: str) -> None:
    subprocess.run(["umount", path], check=True)


def get_blockdev_size(path: str) -> int:
    result = run_command(["blockdev", "--getsize64", path], check=True)
    return int(result.stdout.strip())


def update_symlink(source: str, link: str) -> None:
    subprocess.run(["ln", "-sfn", source, link], check=True)


def chown_recursive(path: str, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)
    for root, dirs, files in os.walk(path):
        for dirname in dirs:
            os.chown(os.path.join(root, dirname), uid, gid)
        for filename in files:
            os.chown(os.path.join(root, filename), uid, gid)


def collect_dmesg_lines() -> list[str]:
    result = run_command(["dmesg", "-T"], check=True)
    return result.stdout.splitlines()


def run_fio(
    name: str,
    rw: str,
    filename: str,
    size: str,
    json_out: str,
    dmesg_out: str,
    mode: str,
    ioengine_log: str,
) -> None:
    common = [
        f"--name={name}",
        f"--rw={rw}",
        "--bs=1M",
        "--iodepth=32",
        "--numjobs=1",
        "--direct=1",
        "--group_reporting",
        "--time_based=0",
        "--ioengine=io_uring",
        f"--filename={filename}",
        f"--size={size}",
        "--output-format=json",
        f"--output={json_out}",
    ]

    if mode == "raw":
        common.append("--hipri=1")

    info(f"Starting fio {name} ({rw}) with io_uring")
    dmesg_before = collect_dmesg_lines()
    start_line = len(dmesg_before)

    result = subprocess.run(["fio", *common])
    if result.returncode == 0:
        with open(ioengine_log, "a", encoding="utf-8") as handle:
            handle.write(f"{name}: io_uring\n")
    else:
        warn(f"io_uring failed for {name}; falling back to libaio")
        fallback = [
            f"--name={name}",
            f"--rw={rw}",
            "--bs=1M",
            "--iodepth=32",
            "--numjobs=1",
            "--direct=1",
            "--group_reporting",
            "--time_based=0",
            "--ioengine=libaio",
            f"--filename={filename}",
            f"--size={size}",
            "--output-format=json",
            f"--output={json_out}",
        ]
        fallback_result = subprocess.run(["fio", *fallback])
        if fallback_result.returncode != 0:
            die(f"fio fallback failed for {name}")
        with open(ioengine_log, "a", encoding="utf-8") as handle:
            handle.write(f"{name}: libaio\n")

    dmesg_after = collect_dmesg_lines()
    pattern = re.compile(r"nvme|reset|timeout|abort|I/O error|blk", re.IGNORECASE)
    with open(dmesg_out, "w", encoding="utf-8") as handle:
        for line in dmesg_after[start_line:]:
            if pattern.search(line):
                handle.write(f"{line}\n")


def ensure_fs_mode_mount(target_real: str) -> None:
    if not os.path.isdir(MOUNTPOINT):
        os.makedirs(MOUNTPOINT, exist_ok=True)

    if mountpoint_is_active(MOUNTPOINT):
        src = findmnt_source(MOUNTPOINT)
        src_real = os.path.realpath(src)
        if src_real != target_real:
            die(f"Mountpoint {MOUNTPOINT} is not mounted from target device.")
    else:
        info(f"Mounting {SYMLINK} to {MOUNTPOINT}")
        mount_device(SYMLINK, MOUNTPOINT)


def ensure_raw_mode_unmounted(target_real: str) -> None:
    result = subprocess.run(
        ["findmnt", "-n", "-o", "TARGET", "-S", target_real],
        text=True,
        capture_output=True,
    )
    mounts = result.stdout.strip()
    if mounts:
        if mountpoint_is_active(MOUNTPOINT):
            src = findmnt_source(MOUNTPOINT)
            src_real = os.path.realpath(src)
            if src_real == target_real:
                info(f"Unmounting {MOUNTPOINT} for raw mode")
                unmount_target(MOUNTPOINT)
                return
        die("Target device is mounted elsewhere; refusing raw test.")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in {"-h", "--help"}:
        print_usage()
        return
    if any(arg.startswith("-") for arg in args):
        die(f"Unknown option: {args[0]}")
    if len(args) > 1:
        die(f"Unexpected extra argument: {args[1]}")

    user_target = args[0] if args else ""
    mode = "raw" if env_flag("RAW") else "fs"
    force_device = env_flag("FORCE_DEVICE")

    target = user_target or DEFAULT_TARGET

    print("=" * 60)
    print("WARNING: This script performs DESTRUCTIVE fio tests")
    print("FS mode overwrites bulkfile and consumes free space.")
    print("Raw mode overwrites the entire target partition.")
    print("=" * 60)

    if os.geteuid() != 0:
        die("Run as root (required for raw device and mount operations).")

    ensure_block_device(target)

    target_real = os.path.realpath(target)
    root_src = findmnt_source("/")
    root_real = os.path.realpath(root_src)

    if target_real == root_real:
        die(f"Target resolves to root device: {target_real}")

    if not force_device and target_real != EXPECTED_REAL:
        die(f"Target must resolve to {EXPECTED_REAL} (use FORCE_DEVICE=1 to override).")

    info(f"Using target: {target_real}")

    if not force_device:
        target_bytes = get_blockdev_size(target_real)
        if not (40 * 1024 * 1024 * 1024 <= target_bytes <= 55 * 1024 * 1024 * 1024):
            die(f"Unexpected target size ({target_bytes} bytes). Refusing.")

    update_symlink(target, SYMLINK)
    info(f"Symlink updated: {SYMLINK} -> {target}")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir = f"./results_{timestamp}"
    os.makedirs(results_dir, exist_ok=True)

    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")

    def fix_results_owner() -> None:
        if sudo_uid and sudo_gid and os.path.isdir(results_dir):
            chown_recursive(results_dir, int(sudo_uid), int(sudo_gid))

    atexit.register(fix_results_owner)
    if sudo_uid and sudo_gid:
        os.chown(results_dir, int(sudo_uid), int(sudo_gid))

    info("Writing system info")
    write_command_output(["uname", "-a"], os.path.join(results_dir, "uname.txt"))
    with open("/proc/cmdline", "r", encoding="utf-8") as handle:
        cmdline = handle.read()
    with open(os.path.join(results_dir, "proc_cmdline.txt"), "w", encoding="utf-8") as handle:
        handle.write(cmdline)
    with open("/sys/block/nvme0n1/queue/scheduler", "r", encoding="utf-8") as handle:
        scheduler = handle.read()
    with open(os.path.join(results_dir, "nvme0n1_scheduler.txt"), "w", encoding="utf-8") as handle:
        handle.write(scheduler)

    if shutil.which("nvme"):
        try:
            write_command_output(["nvme", "list"], os.path.join(results_dir, "nvme_list.txt"))
        except subprocess.CalledProcessError:
            warn("nvme list failed")
        ns = re.sub(r"p\d+$", "", target_real)
        ctrl = re.sub(r"n\d+$", "", ns)
        if ctrl and os.path.exists(ctrl):
            try:
                write_command_output(["nvme", "id-ctrl", ctrl], os.path.join(results_dir, "nvme_id_ctrl.txt"))
            except subprocess.CalledProcessError:
                warn("nvme id-ctrl failed")
        else:
            warn(f"Unable to derive controller from {target_real}; skipping nvme id-ctrl")
    else:
        warn("nvme-cli not installed; skipping nvme list/id-ctrl")

    ioengine_log = os.path.join(results_dir, "ioengine_used.txt")
    with open(ioengine_log, "w", encoding="utf-8"):
        pass

    if mode == "fs":
        info("Mode: filesystem")
        ensure_fs_mode_mount(target_real)
        if not mountpoint_is_active(MOUNTPOINT):
            die(f"Failed to mount {MOUNTPOINT}")
        local_src = findmnt_source(MOUNTPOINT)
        local_src_real = os.path.realpath(local_src)
        if local_src_real != target_real:
            die("Mounted source does not match target device")

        file_path = os.path.join(MOUNTPOINT, "bulkfile")
        run_fio(
            "bulk_write",
            "write",
            file_path,
            "40G",
            os.path.join(results_dir, "fio_bulk_write.json"),
            os.path.join(results_dir, "dmesg_bulk_write.txt"),
            mode,
            ioengine_log,
        )
        run_fio(
            "bulk_read",
            "read",
            file_path,
            "40G",
            os.path.join(results_dir, "fio_bulk_read.json"),
            os.path.join(results_dir, "dmesg_bulk_read.txt"),
            mode,
            ioengine_log,
        )
    else:
        info("Mode: raw")
        ensure_raw_mode_unmounted(target_real)
        run_fio(
            "bulk_write",
            "write",
            SYMLINK,
            "100%",
            os.path.join(results_dir, "fio_bulk_write.json"),
            os.path.join(results_dir, "dmesg_bulk_write.txt"),
            mode,
            ioengine_log,
        )
        run_fio(
            "bulk_read",
            "read",
            SYMLINK,
            "100%",
            os.path.join(results_dir, "fio_bulk_read.json"),
            os.path.join(results_dir, "dmesg_bulk_read.txt"),
            mode,
            ioengine_log,
        )

    info(f"Done. Results saved to {results_dir}")

    if shutil.which("jq"):
        info("Summary (fio JSON via jq)")
        for fname in ["fio_bulk_write.json", "fio_bulk_read.json"]:
            path = os.path.join(results_dir, fname)
            if not os.path.isfile(path):
                continue
            jobname = subprocess.run(
                ["jq", "-r", ".jobs[0].jobname", path],
                text=True,
                capture_output=True,
            ).stdout.strip() or "fio"
            rw_dir = subprocess.run(
                ["jq", "-r", 'if .jobs[0].read.io_bytes > 0 then "read" else "write" end', path],
                text=True,
                capture_output=True,
            ).stdout.strip()
            bw = subprocess.run(
                ["jq", "-r", f".jobs[0].{rw_dir}.bw", path],
                text=True,
                capture_output=True,
            ).stdout.strip()
            iops = subprocess.run(
                ["jq", "-r", f".jobs[0].{rw_dir}.iops", path],
                text=True,
                capture_output=True,
            ).stdout.strip()
            lat = subprocess.run(
                ["jq", "-r", f".jobs[0].{rw_dir}.clat_ns.mean", path],
                text=True,
                capture_output=True,
            ).stdout.strip()
            if bw and bw != "null":
                print(f"  {jobname}: bw={bw}KB/s iops={iops} clat_mean_ns={lat}")
            else:
                print(f"  {jobname}: see {path}")
    else:
        warn(f"jq not installed; see fio JSON in {results_dir}")


if __name__ == "__main__":
    main()
