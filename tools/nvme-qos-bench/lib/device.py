# SPDX-License-Identifier: GPL-2.0
"""NVMe device detection, QoS sysfs control, and safety checks."""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Any


@dataclass
class NVMeDeviceInfo:
    """Information about an NVMe device or partition."""
    name: str                      # e.g., "nvme0n1" or "nvme0n1p1"
    controller: str                # e.g., "nvme0"
    path: str                      # e.g., "/dev/nvme0n1"
    model: str                     # Device model name
    size_bytes: int                # Size in bytes
    size_human: str                # Human-readable size
    mount_point: Optional[str]     # Mount point if mounted
    is_partition: bool             # True if partition, False if full namespace


def _read_sysfs(path: str) -> Optional[str]:
    """Read sysfs file, return None if not found or error."""
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, IOError):
        return None


def _write_sysfs(path: str, value: str) -> bool:
    """Write to sysfs file, return success."""
    try:
        with open(path, 'w') as f:
            f.write(value)
        return True
    except (OSError, IOError, PermissionError):
        return False


def _get_mount_point(device: str) -> Optional[str]:
    """Get mount point for a device, or None if not mounted."""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == f"/dev/{device}":
                    return parts[1]
    except (OSError, IOError):
        pass
    return None


def _human_size(size_bytes: int) -> str:
    """Convert bytes to human-readable size."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}PB"


def discover_nvme_devices() -> List[NVMeDeviceInfo]:
    """Discover all NVMe namespaces and partitions."""
    devices = []

    # Find NVMe block devices
    block_dir = Path("/sys/block")
    if not block_dir.exists():
        return devices

    for entry in sorted(block_dir.iterdir()):
        name = entry.name
        # Match nvme namespaces: nvme0n1, nvme1n2, etc.
        if not re.match(r'nvme\d+n\d+$', name):
            continue

        controller = re.match(r'(nvme\d+)', name).group(1)
        model = _read_sysfs(f"/sys/class/nvme/{controller}/model") or "Unknown"
        model = model.strip()

        size_str = _read_sysfs(f"/sys/block/{name}/size")
        size_bytes = int(size_str) * 512 if size_str else 0

        devices.append(NVMeDeviceInfo(
            name=name,
            controller=controller,
            path=f"/dev/{name}",
            model=model,
            size_bytes=size_bytes,
            size_human=_human_size(size_bytes),
            mount_point=_get_mount_point(name),
            is_partition=False,
        ))

        # Find partitions
        for part_entry in sorted(entry.iterdir()):
            part_name = part_entry.name
            if not re.match(rf'{name}p\d+$', part_name):
                continue

            part_size_str = _read_sysfs(f"/sys/block/{name}/{part_name}/size")
            part_size = int(part_size_str) * 512 if part_size_str else 0

            devices.append(NVMeDeviceInfo(
                name=part_name,
                controller=controller,
                path=f"/dev/{part_name}",
                model=model,
                size_bytes=part_size,
                size_human=_human_size(part_size),
                mount_point=_get_mount_point(part_name),
                is_partition=True,
            ))

    return devices


def check_qos_available(controller: str) -> bool:
    """Check if QoS sysfs interface exists for a controller."""
    return Path(f"/sys/class/nvme/{controller}/qos_enable").exists()


class NVMeDevice:
    """NVMe device with QoS control interface."""

    def __init__(self, name: str):
        """Initialize with device name (e.g., 'nvme0n1' or 'nvme0n1p7')."""
        self.name = name
        self.path = f"/dev/{name}"

        # Extract controller and namespace
        match = re.match(r'(nvme\d+)(n\d+)(p\d+)?', name)
        if not match:
            raise ValueError(f"Invalid NVMe device name: {name}")

        self.controller = match.group(1)
        self.namespace = f"{match.group(1)}{match.group(2)}"
        self.is_partition = match.group(3) is not None

        # Check QoS availability
        self.qos_available = check_qos_available(self.controller)

        # State for save/restore
        self._saved_state: Optional[Dict[str, Any]] = None

    @property
    def qos_enable_path(self) -> str:
        return f"/sys/class/nvme/{self.controller}/qos_enable"

    @property
    def qos_weight_path(self) -> str:
        return f"/sys/class/nvme/{self.controller}/qos_weight"

    @property
    def qos_max_depth_path(self) -> str:
        return f"/sys/class/nvme/{self.controller}/qos_max_depth"

    @property
    def qos_policy_path(self) -> str:
        return f"/sys/block/{self.namespace}/qos_policy"

    def get_qos_enabled(self) -> Optional[bool]:
        """Get current QoS enable state."""
        if not self.qos_available:
            return None
        val = _read_sysfs(self.qos_enable_path)
        return val == "1" if val else None

    def set_qos_enabled(self, enable: bool) -> bool:
        """Enable or disable QoS."""
        if not self.qos_available:
            return False
        return _write_sysfs(self.qos_enable_path, "1" if enable else "0")

    def get_qos_weight(self) -> Optional[int]:
        """Get current QoS high-priority weight."""
        if not self.qos_available:
            return None
        val = _read_sysfs(self.qos_weight_path)
        return int(val) if val else None

    def set_qos_weight(self, weight: int) -> bool:
        """Set QoS high-priority weight."""
        if not self.qos_available:
            return False
        return _write_sysfs(self.qos_weight_path, str(weight))

    def get_qos_max_depth(self) -> Optional[int]:
        """Get current QoS max in-flight depth (0 = full SQ depth)."""
        if not self.qos_available:
            return None
        val = _read_sysfs(self.qos_max_depth_path)
        return int(val) if val is not None else None

    def set_qos_max_depth(self, depth: int) -> bool:
        """Set QoS max in-flight depth (0 = use full SQ depth)."""
        if not self.qos_available:
            return False
        return _write_sysfs(self.qos_max_depth_path, str(depth))

    def get_qos_policy(self) -> Optional[str]:
        """Get current namespace QoS policy."""
        if not self.qos_available:
            return None
        return _read_sysfs(self.qos_policy_path)

    def set_qos_policy(self, policy: str) -> bool:
        """Set namespace QoS policy (default/force_high/force_normal)."""
        if not self.qos_available:
            return False
        return _write_sysfs(self.qos_policy_path, policy)

    def save_state(self) -> None:
        """Save current QoS state for later restoration."""
        if not self.qos_available:
            self._saved_state = None
            return

        self._saved_state = {
            "qos_enabled": self.get_qos_enabled(),
            "qos_weight": self.get_qos_weight(),
            "qos_max_depth": self.get_qos_max_depth(),
            "qos_policy": self.get_qos_policy(),
        }

    def restore_state(self) -> bool:
        """Restore previously saved QoS state."""
        if self._saved_state is None:
            return True  # Nothing to restore

        success = True
        if self._saved_state["qos_enabled"] is not None:
            if not self.set_qos_enabled(self._saved_state["qos_enabled"]):
                success = False
        if self._saved_state["qos_weight"] is not None:
            if not self.set_qos_weight(self._saved_state["qos_weight"]):
                success = False
        if self._saved_state.get("qos_max_depth") is not None:
            if not self.set_qos_max_depth(self._saved_state["qos_max_depth"]):
                success = False
        if self._saved_state["qos_policy"] is not None:
            if not self.set_qos_policy(self._saved_state["qos_policy"]):
                success = False
        return success

    def get_hw_queue_count(self) -> int:
        """Get number of I/O hardware queues from sysfs.

        Reads /sys/block/<ns>/device/queue_count which includes
        the admin queue, so we subtract 1 for I/O queues only.
        Falls back to counting entries in /sys/block/<ns>/mq/.
        """
        # Method 1: queue_count sysfs (includes admin queue)
        val = _read_sysfs(f"/sys/block/{self.namespace}/device/queue_count")
        if val:
            try:
                total = int(val)
                return max(total - 1, 1)  # subtract admin queue
            except ValueError:
                pass

        # Method 2: count blk-mq hardware queues
        mq_dir = Path(f"/sys/block/{self.namespace}/mq")
        if mq_dir.is_dir():
            count = sum(1 for _ in mq_dir.iterdir())
            return max(count, 1)

        return 1  # safe fallback

    def get_model(self) -> str:
        """Get device model name."""
        model = _read_sysfs(f"/sys/class/nvme/{self.controller}/model")
        return model.strip() if model else "Unknown"

    def get_size_bytes(self) -> int:
        """Get device size in bytes."""
        if self.is_partition:
            # For partition, read from parent namespace directory
            size_str = _read_sysfs(f"/sys/block/{self.namespace}/{self.name}/size")
        else:
            size_str = _read_sysfs(f"/sys/block/{self.name}/size")
        return int(size_str) * 512 if size_str else 0

    def is_mounted(self) -> Optional[str]:
        """Return mount point if mounted, None otherwise."""
        return _get_mount_point(self.name)

    def trim(self) -> bool:
        """Issue TRIM/discard to reset SSD internal state (e.g., SLC cache).

        Uses blkdiscard to discard all blocks on the device/partition.
        This invalidates all data -- only safe on a dedicated test device.
        Returns True on success.
        """
        try:
            result = subprocess.run(
                ["blkdiscard", self.path],
                capture_output=True, timeout=60,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


def validate_device(name: str) -> tuple[bool, str]:
    """Validate device exists and is suitable for benchmarking.

    Returns (valid, message) tuple.
    """
    # Normalize name (strip /dev/ if present)
    if name.startswith("/dev/"):
        name = name[5:]

    # Check device exists
    if not Path(f"/dev/{name}").exists():
        return False, f"Device /dev/{name} does not exist"

    # Check it's an NVMe device
    if not re.match(r'nvme\d+n\d+(p\d+)?$', name):
        return False, f"{name} is not an NVMe device or partition"

    # Warn if mounted (but allow - user will confirm)
    mount = _get_mount_point(name)
    if mount:
        return True, f"Device is mounted at {mount}"

    return True, "Device is available"
