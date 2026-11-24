"""
Memory measurement for the ingest processes and the machine, without psutil.

On Apple Silicon the number that matters is the process's *physical footprint* (what Activity Monitor shows as
"Memory"): it includes GPU/MPS allocations in unified memory, which RSS does not (the bge-m3 embedder's RSS is
a fraction of its real 13-17 GB). Linux: RSS from /proc.

    footprint_mb(pid)     -> int | None
    system_memory()       -> {"total_mb", "available_mb", "free_pct", "swap_used_mb"}
    headroom(...)         -> how many more parse workers fit, from measured peaks
"""

from __future__ import annotations

import ctypes
import ctypes.util
import re
import subprocess
import sys
from typing import Any, Dict, Optional

_RUSAGE_INFO_V2 = 2


class _RUsageV2(ctypes.Structure):  # struct rusage_info_v2, <sys/resource.h>
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins", "wired_size", "resident_size",
        "phys_footprint", "proc_start_abstime", "proc_exit_abstime", "child_user_time", "child_system_time",
        "child_pkg_idle_wkups", "child_interrupt_wkups", "child_pageins", "child_elapsed_abstime",
        "diskio_bytesread", "diskio_byteswritten")]


_libc: Any = None


def footprint_mb(pid: int) -> Optional[int]:
    global _libc
    try:
        if sys.platform == "darwin":
            if _libc is None:
                _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            ri = _RUsageV2()
            if _libc.proc_pid_rusage(int(pid), _RUSAGE_INFO_V2, ctypes.byref(ri)) != 0:
                return None
            return int(ri.phys_footprint // 2**20)
        with open(f"/proc/{int(pid)}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _sysctl(name: str) -> str:
    try:
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def system_memory() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if sys.platform == "darwin":
        total = _sysctl("hw.memsize")
        level = _sysctl("kern.memorystatus_level")  # % of memory available, what the kernel's pressure uses
        swap = re.search(r"used = ([\d.]+)M", _sysctl("vm.swapusage"))
        if total.isdigit():
            out["total_mb"] = int(total) // 2**20
        if level.isdigit() and "total_mb" in out:
            out["free_pct"] = int(level)
            out["available_mb"] = out["total_mb"] * int(level) // 100
        if swap:
            out["swap_used_mb"] = int(float(swap.group(1)))
        return out
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) // 1024
        out = {"total_mb": info["MemTotal"], "available_mb": info.get("MemAvailable", info.get("MemFree", 0)),
               "swap_used_mb": info.get("SwapTotal", 0) - info.get("SwapFree", 0)}
        out["free_pct"] = round(100 * out["available_mb"] / out["total_mb"])
    except (OSError, KeyError, ValueError):
        pass
    return out


def headroom(mem: Dict[str, Any], worker_peak_mb: int, embedder_peak_mb: int, workers_now: int,
             ingest_running: bool, reserve_pct: int = 15) -> Dict[str, Any]:
    """Workers that fit: keep reserve_pct of RAM free. When no ingest runs, the embedder and the current workers
    still have to fit in what is available now; while one runs they are already counted in it."""
    total, avail = mem.get("total_mb"), mem.get("available_mb")
    if not total or avail is None or worker_peak_mb <= 0:
        return {}
    usable = avail - total * reserve_pct // 100
    if not ingest_running:
        usable -= embedder_peak_mb + workers_now * worker_peak_mb
    more = max(0, usable // worker_peak_mb)
    return {"reserve_pct": reserve_pct, "usable_mb": int(usable), "more_workers_fit": int(more),
            "suggested_max_workers": int(workers_now + more)}

