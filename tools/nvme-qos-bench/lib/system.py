# SPDX-License-Identifier: GPL-2.0
"""System information collection for benchmark metadata."""

import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

# Cached system info to avoid repeated subprocess calls
_cached_system_info: Optional[Dict[str, Any]] = None


def get_kernel_version() -> str:
    """Get kernel version string."""
    return platform.release()


def get_cpu_info() -> Dict[str, Any]:
    """Get CPU information."""
    info = {
        "model": "Unknown",
        "cores": os.cpu_count() or 0,
        "threads": 0,
    }

    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    info["model"] = line.split(":")[1].strip()
                    break
    except (OSError, IOError):
        pass

    # Count threads from /proc/cpuinfo
    try:
        with open("/proc/cpuinfo") as f:
            info["threads"] = sum(1 for line in f if line.startswith("processor"))
    except (OSError, IOError):
        info["threads"] = info["cores"]

    return info


def get_memory_info() -> Dict[str, Any]:
    """Get memory information in bytes."""
    info = {"total_bytes": 0}

    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    # Value is in kB
                    kb = int(line.split()[1])
                    info["total_bytes"] = kb * 1024
                    break
    except (OSError, IOError):
        pass

    return info


def get_nvme_info(controller: str) -> Dict[str, Any]:
    """Get NVMe controller information."""
    info = {
        "controller": controller,
        "model": "Unknown",
        "serial": "Unknown",
        "firmware": "Unknown",
    }

    base = f"/sys/class/nvme/{controller}"

    def read_attr(name: str) -> Optional[str]:
        try:
            with open(f"{base}/{name}") as f:
                return f.read().strip()
        except (OSError, IOError):
            return None

    info["model"] = read_attr("model") or "Unknown"
    info["serial"] = read_attr("serial") or "Unknown"
    info["firmware"] = read_attr("firmware_rev") or "Unknown"

    return info


def get_git_info() -> Dict[str, Any]:
    """Get git repository information."""
    info = {
        "commit": None,
        "branch": None,
        "dirty": False,
    }

    # Find repo root using git's built-in command
    repo_root = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent,
        )
        if result.returncode == 0:
            repo_root = Path(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return info

    if repo_root is None:
        return info

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            info["commit"] = result.stdout.strip()[:12]
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            info["branch"] = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            info["dirty"] = bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass

    return info


def get_fio_version() -> Optional[str]:
    """Get fio version string."""
    try:
        result = subprocess.run(
            ["fio", "--version"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def check_fio_available() -> bool:
    """Check if fio is available."""
    return get_fio_version() is not None


def collect_system_info(device_name: str, controller: str) -> Dict[str, Any]:
    """Collect complete system information for metadata (cached after first call)."""
    global _cached_system_info

    if _cached_system_info is not None:
        # Update only per-invocation fields
        result = _cached_system_info.copy()
        result["timestamp"] = datetime.now().isoformat()
        result["device"] = device_name
        result["nvme"] = get_nvme_info(controller)
        return result

    # First call: collect and cache
    _cached_system_info = {
        "timestamp": datetime.now().isoformat(),
        "kernel": get_kernel_version(),
        "cpu": get_cpu_info(),
        "memory": get_memory_info(),
        "nvme": get_nvme_info(controller),
        "device": device_name,
        "git": get_git_info(),
        "fio_version": get_fio_version(),
    }

    return _cached_system_info


def capture_dmesg(filter_patterns: Optional[list] = None) -> str:
    """Capture dmesg output, optionally filtered. Returns empty string on failure with warning."""
    if filter_patterns is None:
        filter_patterns = ["nvme", "error", "warning", "qos"]

    try:
        result = subprocess.run(
            ["dmesg", "--time-format=iso"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Warning: dmesg capture failed (exit code {result.returncode}). May need root privileges.", file=sys.stderr)
            return ""

        lines = result.stdout.splitlines()

        # Filter if patterns provided
        if filter_patterns:
            filtered = []
            for line in lines:
                lower = line.lower()
                if any(p.lower() in lower for p in filter_patterns):
                    filtered.append(line)
            return "\n".join(filtered)

        return result.stdout
    except (OSError, subprocess.SubprocessError) as e:
        print(f"Warning: dmesg capture failed ({e}). May need root privileges.", file=sys.stderr)
        return ""
