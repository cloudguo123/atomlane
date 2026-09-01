#!/usr/bin/env python3
"""Windows Job Object controls used by the AtomLane parent process."""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import Any

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x00000001
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x00000004
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS = 15
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class WindowsJobError(OSError):
    pass


def _last_error(action: str) -> WindowsJobError:
    code = ctypes.get_last_error()
    return WindowsJobError(code, f"{action} failed: {ctypes.FormatError(code)}")


class WindowsJobController:
    """Own a kill-on-close Job Object and assign a waiting supervisor to it."""

    def __init__(
        self,
        *,
        cpu_rate_percent: float | None = None,
        memory_limit_mb: float | None = None,
        max_processes: int | None = None,
    ) -> None:
        if os.name != "nt":
            raise WindowsJobError("Windows Job Objects are available only on native Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()
        self._assigned_process_handle: Any = None
        self.handle = self._kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise _last_error("CreateJobObjectW")
        try:
            self._set_limits(cpu_rate_percent, memory_limit_mb, max_processes)
        except Exception:
            self.close()
            raise

    def _configure_signatures(self) -> None:
        kernel32 = self._kernel32
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    def _set_limits(
        self,
        cpu_rate_percent: float | None,
        memory_limit_mb: float | None,
        max_processes: int | None,
    ) -> None:
        extended = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit_mb is not None:
            expected_memory_bytes = int(memory_limit_mb * 1024 * 1024)
            maximum_size_t = (1 << (ctypes.sizeof(ctypes.c_size_t) * 8)) - 1
            if expected_memory_bytes > maximum_size_t:
                raise WindowsJobError("Job Object memory limit exceeds this Python ABI")
            flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
            extended.JobMemoryLimit = expected_memory_bytes
        if max_processes is not None:
            flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            extended.BasicLimitInformation.ActiveProcessLimit = int(max_processes)
        extended.BasicLimitInformation.LimitFlags = flags
        if not self._kernel32.SetInformationJobObject(
            self.handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(extended),
            ctypes.sizeof(extended),
        ):
            raise _last_error("SetInformationJobObject(extended limits)")
        if cpu_rate_percent is not None:
            cpu = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
            cpu.ControlFlags = (
                JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
            )
            cpu.CpuRate = round(cpu_rate_percent * 100.0)
            if not self._kernel32.SetInformationJobObject(
                self.handle,
                JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS,
                ctypes.byref(cpu),
                ctypes.sizeof(cpu),
            ):
                raise _last_error("SetInformationJobObject(CPU rate)")
        self._verified_limits = self._query_and_verify_limits(
            cpu_rate_percent, memory_limit_mb, max_processes
        )

    def _query(self, information_class: int, value: ctypes.Structure) -> None:
        returned = wintypes.DWORD(0)
        if not self._kernel32.QueryInformationJobObject(
            self.handle,
            information_class,
            ctypes.byref(value),
            ctypes.sizeof(value),
            ctypes.byref(returned),
        ):
            raise _last_error("QueryInformationJobObject")

    def _query_and_verify_limits(
        self,
        cpu_rate_percent: float | None,
        memory_limit_mb: float | None,
        max_processes: int | None,
    ) -> dict[str, Any]:
        extended = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        self._query(JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, extended)
        flags = int(extended.BasicLimitInformation.LimitFlags)
        expected_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit_mb is not None:
            expected_flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        if max_processes is not None:
            expected_flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        if flags & expected_flags != expected_flags:
            raise WindowsJobError("Job Object did not retain every requested limit flag")
        actual_memory_bytes = (
            int(extended.JobMemoryLimit)
            if flags & JOB_OBJECT_LIMIT_JOB_MEMORY
            else None
        )
        actual_processes = (
            int(extended.BasicLimitInformation.ActiveProcessLimit)
            if flags & JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            else None
        )
        expected_memory_bytes = (
            int(memory_limit_mb * 1024 * 1024) if memory_limit_mb is not None else None
        )
        if expected_memory_bytes is not None and actual_memory_bytes != expected_memory_bytes:
            raise WindowsJobError("Job Object memory limit verification failed")
        if max_processes is not None and actual_processes != int(max_processes):
            raise WindowsJobError("Job Object active-process limit verification failed")
        actual_cpu: float | None = None
        if cpu_rate_percent is not None:
            cpu = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
            self._query(JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS, cpu)
            expected_rate = round(cpu_rate_percent * 100.0)
            if (
                int(cpu.ControlFlags)
                & (JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP)
                != (JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP)
                or int(cpu.CpuRate) != expected_rate
            ):
                raise WindowsJobError("Job Object CPU-rate limit verification failed")
            actual_cpu = int(cpu.CpuRate) / 100.0
        return {
            "verified": True,
            "kill_on_close": bool(flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE),
            "cpu_rate_percent": actual_cpu,
            "memory_limit_mb": (
                actual_memory_bytes / (1024 * 1024)
                if actual_memory_bytes is not None
                else None
            ),
            "job_active_process_limit": actual_processes,
        }

    def assign(self, process_id: int) -> None:
        if self._assigned_process_handle:
            raise WindowsJobError("the Job Object already has an assigned supervisor handle")
        access = (
            PROCESS_SET_QUOTA
            | PROCESS_TERMINATE
            | PROCESS_QUERY_LIMITED_INFORMATION
            | PROCESS_SYNCHRONIZE
        )
        process = self._kernel32.OpenProcess(access, False, int(process_id))
        if not process:
            raise _last_error("OpenProcess")
        try:
            if not self._kernel32.AssignProcessToJobObject(self.handle, process):
                raise _last_error("AssignProcessToJobObject")
        except Exception:
            self._kernel32.CloseHandle(process)
            raise
        self._assigned_process_handle = process

    def assigned_process_has_exited(self) -> bool:
        """Poll the supervisor kernel handle without depending on pipe EOF."""

        if not self._assigned_process_handle:
            raise WindowsJobError("the Job Object has no assigned supervisor handle")
        result = int(
            self._kernel32.WaitForSingleObject(self._assigned_process_handle, 0)
        )
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        if result == WAIT_FAILED:
            raise _last_error("WaitForSingleObject(supervisor)")
        raise WindowsJobError(
            f"WaitForSingleObject(supervisor) returned unexpected status {result}"
        )

    def terminate(self, exit_code: int = 1) -> None:
        if self.handle and not self._kernel32.TerminateJobObject(self.handle, int(exit_code)):
            raise _last_error("TerminateJobObject")

    def active_processes(self) -> int:
        accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        self._query(JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS, accounting)
        return int(accounting.ActiveProcesses)

    def terminate_and_wait_empty(
        self, exit_code: int = 1, timeout_seconds: float = 3.0
    ) -> dict[str, Any]:
        active_before = self.active_processes()
        if active_before:
            self.terminate(exit_code)
        deadline = time.monotonic() + timeout_seconds
        while True:
            active_after = self.active_processes()
            if active_after == 0:
                return {
                    "status": "terminated" if active_before else "already_empty",
                    "active_before": active_before,
                    "active_after": 0,
                    "verified_empty": True,
                }
            if time.monotonic() >= deadline:
                raise WindowsJobError(
                    f"Job Object still contains {active_after} active processes after termination"
                )
            time.sleep(0.02)

    def close(self) -> None:
        failure: WindowsJobError | None = None
        if getattr(self, "handle", None):
            if not self._kernel32.CloseHandle(self.handle):
                failure = _last_error("CloseHandle(Job Object)")
            self.handle = None
        if getattr(self, "_assigned_process_handle", None):
            if (
                not self._kernel32.CloseHandle(self._assigned_process_handle)
                and failure is None
            ):
                failure = _last_error("CloseHandle(supervisor process)")
            self._assigned_process_handle = None
        if failure is not None:
            raise failure

    def description(self) -> dict[str, Any]:
        return {
            "backend": "windows_job_object",
            "containment_scope": "supervisor_and_inherited_windows_tree",
            "resource_scope": "supervisor_and_inherited_windows_tree",
            "brokered_processes_contained": False,
            **self._verified_limits,
        }

    def __enter__(self) -> WindowsJobController:  # noqa: PYI034 - Python 3.10 support.
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
