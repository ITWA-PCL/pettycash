"""Lightweight, read-only runtime metrics for the system overview."""
from __future__ import annotations

import os
import time
from pathlib import Path


_last_cpu_sample: tuple[int, float] | None = None


def _read_cgroup_value(*names: str) -> str | None:
    for name in names:
        try:
            return Path(name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return None


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "ไม่ทราบ"
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def _container_cpu_percent() -> int | None:
    """Return recent cgroup CPU usage normalized to the container CPU limit."""
    global _last_cpu_sample
    cpu_stat = _read_cgroup_value("/sys/fs/cgroup/cpu.stat")
    if not cpu_stat:
        return None
    usage_line = next((line for line in cpu_stat.splitlines() if line.startswith("usage_usec ")), None)
    if not usage_line:
        return None
    try:
        usage_usec = int(usage_line.split()[1])
    except (IndexError, ValueError):
        return None

    now = time.monotonic()
    previous = _last_cpu_sample
    _last_cpu_sample = (usage_usec, now)
    if not previous or now <= previous[1]:
        return None

    available_cpus = 1.0
    cpu_max = _read_cgroup_value("/sys/fs/cgroup/cpu.max")
    if cpu_max:
        quota, _, period = cpu_max.partition(" ")
        try:
            if quota != "max":
                available_cpus = max(float(quota) / float(period), 0.01)
        except ValueError:
            pass
    usage = (usage_usec - previous[0]) / 1_000_000 / (now - previous[1]) / available_cpus * 100
    return max(0, min(100, round(usage)))


def get_web_health() -> dict[str, object]:
    """Read container metrics when available, with safe fallbacks for local runs."""
    memory_current_raw = _read_cgroup_value("/sys/fs/cgroup/memory.current")
    memory_max_raw = _read_cgroup_value("/sys/fs/cgroup/memory.max")
    try:
        memory_current = int(memory_current_raw) if memory_current_raw else None
    except ValueError:
        memory_current = None
    try:
        memory_max = int(memory_max_raw) if memory_max_raw and memory_max_raw != "max" else None
    except ValueError:
        memory_max = None
    memory_percent = round(memory_current / memory_max * 100) if memory_current is not None and memory_max else None
    return {
        "cpu_percent": _container_cpu_percent(),
        "memory_current": _format_bytes(memory_current),
        "memory_limit": _format_bytes(memory_max) if memory_max else "ไม่กำหนด",
        "memory_percent": memory_percent,
        "memory_bar_percent": memory_percent if memory_percent is not None else 0,
        "running": bool(os.getpid()),
    }
