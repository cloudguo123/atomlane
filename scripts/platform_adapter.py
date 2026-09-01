#!/usr/bin/env python3
"""Small dependency-free platform boundary for AtomLane's Python runtime."""

from __future__ import annotations

import contextlib
import ctypes
import ntpath
import os
import platform
import shutil
import struct
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

WINDOWS_BROKER_EXECUTABLES = {
    "docker": "docker_daemon",
    "docker-compose": "docker_daemon",
    "kubectl": "kubernetes_cluster",
    "nerdctl": "container_daemon",
    "podman": "container_daemon",
    "psexec": "remote_windows",
    "sc": "windows_service_manager",
    "schtasks": "windows_task_scheduler",
    "scp": "remote_ssh",
    "sftp": "remote_ssh",
    "ssh": "remote_ssh",
    "winrs": "remote_windows",
    "wmi": "windows_management_instrumentation",
    "wmic": "windows_management_instrumentation",
    "wsl": "wsl_linux",
}


def resolve_windows_path_executable(
    executable: str, path_value: str | None = None
) -> str | None:
    """Resolve only through fully qualified Windows PATH entries.

    This deliberately avoids Python/Win32 current-directory executable search,
    which differs across Python versions and can run an unrelated local image.
    """

    if (
        not executable
        or "\0" in executable
        or ntpath.basename(executable) != executable
    ):
        return None
    suffix = ntpath.splitext(executable)[1].casefold()
    names = [executable] if suffix else [f"{executable}.exe", f"{executable}.com"]
    if suffix and suffix not in {".exe", ".com"}:
        return None
    expand_environment = path_value is None
    search_path = os.environ.get("PATH", "") if expand_environment else path_value
    for raw_directory in search_path.split(";"):
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in raw_directory
        ):
            continue
        directory = raw_directory.strip()
        if not directory or any(
            ord(character) < 32 or ord(character) == 127
            for character in directory
        ):
            continue
        begins_quote = directory.startswith('"')
        ends_quote = directory.endswith('"')
        if begins_quote or ends_quote:
            if not (begins_quote and ends_quote) or len(directory) < 2:
                continue
            directory = directory[1:-1]
        if not directory or '"' in directory:
            continue
        if expand_environment:
            directory = ntpath.expandvars(directory)
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in directory
        ):
            continue
        drive, tail = ntpath.splitdrive(directory)
        if not drive or not tail.startswith(("\\", "/")):
            continue
        for name in names:
            candidate = ntpath.join(directory, name)
            if not os.path.isfile(candidate):
                continue
            resolved = os.path.realpath(candidate)
            resolved_drive, resolved_tail = ntpath.splitdrive(resolved)
            if (
                resolved_drive
                and resolved_tail.startswith(("\\", "/"))
                and ntpath.splitext(resolved)[1].casefold() in {".exe", ".com"}
                and os.path.isfile(resolved)
            ):
                return resolved
    return None


def resolve_host_executable(executable: str) -> str | None:
    """Resolve a host tool without implicit current-directory search on Windows."""

    if os.name == "nt":
        return resolve_windows_path_executable(executable)
    return shutil.which(executable)


def execution_environment() -> dict[str, Any]:
    """Describe the actual kernel boundary without conflating WSL and Windows."""
    system = platform.system()
    release = platform.release()
    proc_version = ""
    if system == "Linux":
        try:
            proc_version = Path("/proc/sys/kernel/osrelease").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            proc_version = release
    is_wsl = system == "Linux" and (
        "microsoft" in proc_version.lower()
        or bool(os.environ.get("WSL_INTEROP"))
        or bool(os.environ.get("WSL_DISTRO_NAME"))
    )
    is_container = False
    if system == "Linux":
        is_container = Path("/.dockerenv").exists()
        if not is_container:
            try:
                cgroup = Path("/proc/1/cgroup").read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                cgroup = ""
            is_container = any(
                marker in cgroup.lower()
                for marker in ("docker", "containerd", "kubepods", "podman")
            )
    if system == "Windows":
        boundary = "windows_native"
    elif is_wsl:
        boundary = "wsl_linux"
    elif is_container:
        boundary = "linux_container"
    elif system == "Darwin":
        boundary = "macos_native"
    elif system == "Linux":
        boundary = "linux_native"
    else:
        boundary = "unknown"
    return {
        "system": system,
        "boundary": boundary,
        "is_windows_native": boundary == "windows_native",
        "is_macos_native": boundary == "macos_native",
        "is_wsl": is_wsl,
        "is_container": is_container,
    }


def brokered_execution_boundary(
    argv0: str, *, boundary: str | None = None
) -> dict[str, Any] | None:
    """Identify native-Windows clients whose real work is created by another authority."""
    active_boundary = boundary or execution_environment()["boundary"]
    if active_boundary != "windows_native":
        return None
    executable = ntpath.basename(argv0.replace("/", "\\")).casefold()
    executable = executable.removesuffix(".exe")
    target = WINDOWS_BROKER_EXECUTABLES.get(executable)
    if target is None:
        return None
    return {
        "client": executable,
        "target_realm": target,
        "containment_scope": "client_and_inherited_windows_descendants_only",
        "brokered_work_contained": False,
        "job_resource_limits_apply_to_brokered_work": False,
    }


def windows_process_limit_blocker(
    terminal_mode: str, max_processes: Any
) -> str | None:
    """Return the fail-closed reason for an unprovable Windows process limit."""

    if terminal_mode == "conpty" and max_processes is not None:
        return (
            "max_processes cannot be combined with ConPTY until console-host "
            "Job Object membership is verified"
        )
    return None


def default_stats_path() -> Path:
    override = os.environ.get("MAC_PARALLEL_ACCELERATOR_STATS_PATH")
    if override:
        return Path(override).expanduser()
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "AtomLane" / "stats.json"
        return Path.home() / "AppData" / "Local" / "AtomLane" / "stats.json"
    if system == "Darwin":
        # Preserve the pre-AtomLane location so existing cumulative savings survive upgrades.
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Codex"
            / "Mac Parallel Accelerator"
            / "stats.json"
        )
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "atomlane" / "stats.json"


@contextlib.contextmanager
def exclusive_file_lock(path: Path) -> Iterator[BinaryIO]:
    """Hold an advisory one-byte lock using the host's native file primitive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if platform.system() == "Windows":
            import msvcrt

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield handle
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield handle
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _windows_kernel32() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _windows_memory_status() -> tuple[int | None, int | None, int | None]:
    if platform.system() != "Windows":
        return None, None, None

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    kernel32 = _windows_kernel32()
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
    kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None, None, None
    free_percent = max(0, min(100, 100 - int(status.dwMemoryLoad)))
    return int(status.ullTotalPhys), int(status.ullAvailPhys), free_percent


def memory_snapshot() -> dict[str, int | None]:
    system = platform.system()
    if system == "Windows":
        total, available, free_percent = _windows_memory_status()
        return {
            "total_bytes": total,
            "available_bytes": available,
            "free_percent": free_percent,
        }
    if system == "Linux":
        values: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, raw = line.split(":", 1)
                values[key] = int(raw.split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            return {"total_bytes": None, "available_bytes": None, "free_percent": None}
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        free_percent = round(available / total * 100) if total and available is not None else None
        return {
            "total_bytes": total,
            "available_bytes": available,
            "free_percent": free_percent,
        }
    return {"total_bytes": None, "available_bytes": None, "free_percent": None}


def power_snapshot() -> dict[str, Any]:
    if platform.system() != "Windows":
        return {"source": None, "battery_percent": None, "low_power_mode": None}

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_ubyte),
            ("BatteryFlag", ctypes.c_ubyte),
            ("BatteryLifePercent", ctypes.c_ubyte),
            ("SystemStatusFlag", ctypes.c_ubyte),
            ("BatteryLifeTime", ctypes.c_ulong),
            ("BatteryFullLifeTime", ctypes.c_ulong),
        ]

    status = SYSTEM_POWER_STATUS()
    kernel32 = _windows_kernel32()
    kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(SYSTEM_POWER_STATUS)]
    kernel32.GetSystemPowerStatus.restype = ctypes.c_int
    if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return {"source": None, "battery_percent": None, "low_power_mode": None}
    source = "ac" if status.ACLineStatus == 1 else "battery" if status.ACLineStatus == 0 else None
    percent = int(status.BatteryLifePercent) if status.BatteryLifePercent <= 100 else None
    return {
        "source": source,
        "battery_percent": percent,
        "low_power_mode": bool(status.SystemStatusFlag),
    }


def _filetime_value(value: Any) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _windows_cpu_busy_percent(sample_seconds: float = 0.05) -> float | None:
    if platform.system() != "Windows":
        return None

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

    kernel32 = _windows_kernel32()
    kernel32.GetSystemTimes.argtypes = [
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.GetSystemTimes.restype = ctypes.c_int

    def sample() -> tuple[int, int, int] | None:
        idle = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        return _filetime_value(idle), _filetime_value(kernel), _filetime_value(user)

    before = sample()
    if before is None:
        return None
    time.sleep(max(0.01, min(0.25, sample_seconds)))
    after = sample()
    if after is None:
        return None
    idle_delta = after[0] - before[0]
    total_delta = (after[1] - before[1]) + (after[2] - before[2])
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))


def load_snapshot(logical_cpus: int) -> dict[str, float | str | None]:
    try:
        one, five, fifteen = os.getloadavg()
        return {
            "one_minute": one,
            "five_minutes": five,
            "fifteen_minutes": fifteen,
            "source": "load_average",
            "cpu_busy_percent": None,
        }
    except (AttributeError, OSError):
        busy = _windows_cpu_busy_percent()
        equivalent = (busy / 100.0 * max(1, logical_cpus)) if busy is not None else 0.0
        return {
            "one_minute": equivalent,
            "five_minutes": equivalent,
            "fifteen_minutes": equivalent,
            "source": "windows_get_system_times" if busy is not None else "unavailable",
            "cpu_busy_percent": busy,
        }


def windows_physical_cpu_count(logical_fallback: int) -> int:
    if platform.system() != "Windows":
        return logical_fallback
    kernel32 = _windows_kernel32()
    get_info = getattr(kernel32, "GetLogicalProcessorInformationEx", None)
    if get_info is None:
        return logical_fallback
    relation_processor_core = 0
    needed = ctypes.c_ulong(0)
    get_info(relation_processor_core, None, ctypes.byref(needed))
    if needed.value == 0:
        return logical_fallback
    buffer = ctypes.create_string_buffer(needed.value)
    if not get_info(relation_processor_core, buffer, ctypes.byref(needed)):
        return logical_fallback
    raw = buffer.raw[: needed.value]
    offset = 0
    count = 0
    while offset + 8 <= len(raw):
        relationship, size = struct.unpack_from("II", raw, offset)
        if size < 8 or offset + size > len(raw):
            return logical_fallback
        if relationship == relation_processor_core:
            count += 1
        offset += size
    return count or logical_fallback


def platform_capabilities() -> dict[str, Any]:
    environment = execution_environment()
    conpty = False
    if environment["is_windows_native"]:
        try:
            conpty = hasattr(_windows_kernel32(), "CreatePseudoConsole")
        except OSError:
            conpty = False
    return {
        "execution_environment": environment,
        "process_tree_control": "windows_job_object" if environment["is_windows_native"] else "posix_session",
        "terminal_modes": ["pipes", "conpty"] if conpty else ["pipes"],
        "conpty_available": conpty,
        "resource_controls": (
            ["cpu_rate_percent", "memory_limit_mb", "max_processes"]
            if environment["is_windows_native"]
            else []
        ),
        "resource_control_constraints": (
            {
                "cpu_rate_percent": {
                    "minimum": 0.01,
                    "maximum": 100,
                    "terminal_modes": ["pipes", "conpty"],
                    "scope": "all_job_members_including_supervisor",
                },
                "memory_limit_mb": {
                    "minimum": 128,
                    "maximum": 1_048_576,
                    "terminal_modes": ["pipes", "conpty"],
                    "scope": "all_job_members_including_supervisor",
                },
                "max_processes": {
                    "minimum": 2,
                    "maximum": 4096,
                    "terminal_modes": ["pipes"],
                    "scope": "all_job_members_including_supervisor",
                }
            }
            if environment["is_windows_native"]
            else {}
        ),
    }
