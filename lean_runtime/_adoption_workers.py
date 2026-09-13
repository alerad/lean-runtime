"""Conservative resource sizing for read-only adoption planning."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from .errors import ProjectError

_MIB = 1024 * 1024


def available_memory_bytes() -> int | None:
    """Estimate currently available memory, not merely installed RAM."""
    try:
        if sys.platform == "linux":
            values = dict(
                re.findall(r"^(\w+):\s+(\d+) kB", Path("/proc/meminfo").read_text(), re.M)
            )
            available = int(values["MemAvailable"]) * 1024
            # Respect the usual cgroup-v2 container memory limit when present.
            try:
                limit = int(Path("/sys/fs/cgroup/memory.max").read_text())
                used = int(Path("/sys/fs/cgroup/memory.current").read_text())
                available = min(available, max(0, limit - used))
            except (OSError, ValueError):
                pass
            return available
        if sys.platform == "darwin":
            result = subprocess.run(
                ["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=2, check=True
            )
            page = re.search(r"page size of (\d+) bytes", result.stdout)
            if page is None:
                return None
            values = dict(re.findall(r"^(Pages [\w ]+):\s+(\d+)\.", result.stdout, re.M))
            return int(page[1]) * sum(
                int(values.get(key, "0"))
                for key in ("Pages free", "Pages inactive", "Pages speculative")
            )
        if sys.platform == "win32":

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_uint32),
                    ("load", ctypes.c_uint32),
                    ("total", ctypes.c_uint64),
                    ("available", ctypes.c_uint64),
                    ("total_page", ctypes.c_uint64),
                    ("available_page", ctypes.c_uint64),
                    ("total_virtual", ctypes.c_uint64),
                    ("available_virtual", ctypes.c_uint64),
                    ("extended", ctypes.c_uint64),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.available)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    return None


def available_cpus() -> int:
    """Respect process affinity and the usual cgroup-v2 CPU quota."""
    count = os.cpu_count() or 1
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        with suppress(OSError):
            count = min(count, len(affinity(0)))
    if sys.platform == "linux":
        try:
            quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
            count = min(count, max(1, int(quota) // int(period)))
        except (OSError, ValueError, ZeroDivisionError):
            pass
    return max(1, count)


def adoption_jobs(requested: int | None = None) -> int:
    """Allow overrides; auto reserves one CPU and 512 MiB, capped at eight."""
    if requested is not None:
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
            raise ProjectError("--jobs must be a positive integer")
        return requested
    memory = available_memory_bytes()
    if memory is None:
        return 1
    memory_workers = max(1, (memory - 512 * _MIB) // (512 * _MIB))
    return max(1, min(8, max(1, available_cpus() - 1), memory_workers))
