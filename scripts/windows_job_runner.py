#!/usr/bin/env python3
"""Waiting Windows supervisor that stages target creation behind Job assignment.

The parent starts this process, assigns it to a kill-on-close Job Object, and
only then sends one JSON launch record. Normally created child processes inherit
that Job; PID-based supervisor assignment itself is not an atomic spawn primitive.

The target image is resolved to an absolute application path and passed through
``lpApplicationName``. Argument transport uses Python's Windows-CRT-compatible
``list2cmdline`` codec, which is the boundary named by :data:`ARGV_ASSURANCE`.
"""

from __future__ import annotations

import base64
import ctypes
import functools
import json
import ntpath
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from itertools import pairwise
from typing import Any

MAX_PAYLOAD_BYTES = 2_500_000
MAX_ENVIRONMENT_UTF16_UNITS = 32_767
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
WAIT_FAILED = 0xFFFFFFFF
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
ERROR_INVALID_HANDLE = 6
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_OPERATION_ABORTED = 995
ERROR_NOT_FOUND = 1168
THREAD_TERMINATE = 0x0001
ARGV_ASSURANCE = "resolved-lpapplicationname/windows-crt-v1"


class RunnerError(RuntimeError):
    pass


def validate_windows_executable_contract(argv0: str) -> None:
    """Validate the direct-image boundary shared by planning and execution.

    AtomLane transports an argv vector directly to ``CreateProcessW`` and pins
    ``lpApplicationName`` to the resolved image.  Script and shell extensions
    therefore cannot be accepted as if they were executable images; callers
    must name an explicit native delegate such as Python or PowerShell instead.
    """

    if not isinstance(argv0, str) or not argv0 or "\0" in argv0 or '"' in argv0:
        raise RunnerError("argv[0] must be a non-empty unquoted executable name")
    drive, tail = ntpath.splitdrive(argv0)
    if drive.endswith(":") and not tail.startswith(("\\", "/")):
        raise RunnerError("drive-relative executable paths are not supported")
    suffix = ntpath.splitext(argv0)[1].casefold()
    if suffix and suffix not in {".com", ".exe"}:
        raise RunnerError("direct executable images must use .exe or .com")


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _kernel32() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _error(action: str) -> RunnerError:
    code = ctypes.get_last_error()
    return RunnerError(f"{action} failed ({code}): {ctypes.FormatError(code)}")


def _validate_payload(raw: Any) -> tuple[list[str], str, bytes | None, str, dict[str, str]]:
    if not isinstance(raw, dict):
        raise RunnerError("launch payload must be an object")
    if raw.get("protocol") != "atomlane-windows-supervisor/v1":
        raise RunnerError("unsupported or missing supervisor protocol")
    argv = raw.get("argv")
    cwd = raw.get("cwd")
    terminal_mode = raw.get("terminal_mode", "pipes")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and "\0" not in item for item in argv)
        or not argv[0]
    ):
        raise RunnerError("argv must be a non-empty string array")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise RunnerError("cwd must be an existing absolute directory")
    if terminal_mode not in {"pipes", "conpty"}:
        raise RunnerError("terminal_mode must be pipes or conpty")
    env = raw.get("env")
    if not isinstance(env, dict) or not all(
        isinstance(key, str)
        and key
        and "=" not in key
        and "\0" not in key
        and isinstance(value, str)
        and "\0" not in value
        for key, value in env.items()
    ):
        raise RunnerError("env must be a string-to-string object without NUL or '=' names")
    try:
        environment_units = max(
            2,
            1
            + sum(
                len(f"{key}={value}\0".encode("utf-16-le")) // 2
                for key, value in env.items()
            ),
        )
    except UnicodeEncodeError as exc:
        raise RunnerError("env contains an invalid Unicode surrogate") from exc
    if environment_units > MAX_ENVIRONMENT_UTF16_UNITS:
        raise RunnerError(
            f"env exceeds {MAX_ENVIRONMENT_UTF16_UNITS} UTF-16 code units"
        )
    stdin_b64 = raw.get("stdin_base64")
    if stdin_b64 is None:
        stdin_data = None
    elif isinstance(stdin_b64, str):
        try:
            stdin_data = base64.b64decode(stdin_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise RunnerError("stdin_base64 is invalid") from exc
    else:
        raise RunnerError("stdin_base64 must be a string or null")
    return argv, cwd, stdin_data, terminal_mode, env


def _environment_value(env: dict[str, str], name: str, default: str = "") -> str:
    identity = name.casefold()
    for key, value in env.items():
        if key.casefold() == identity:
            return value
    return default


def _expand_windows_environment(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        replacement = _environment_value(env, name)
        return replacement if replacement else match.group(0)

    return re.sub(r"%([^%]+)%", replace, value)


def _executable_extensions(argv0: str, env: dict[str, str]) -> list[str]:
    validate_windows_executable_contract(argv0)
    suffix = ntpath.splitext(argv0)[1].casefold()
    if suffix:
        return [""]
    configured = _environment_value(env, "PATHEXT", ".COM;.EXE")
    extensions: list[str] = []
    for raw in configured.split(";"):
        extension = raw.strip()
        if not extension:
            continue
        if not extension.startswith("."):
            extension = "." + extension
        if extension.casefold() not in {".com", ".exe"}:
            continue
        if extension.casefold() not in {item.casefold() for item in extensions}:
            extensions.append(extension)
    for required in (".COM", ".EXE"):
        if required.casefold() not in {item.casefold() for item in extensions}:
            extensions.append(required)
    return extensions


def _resolve_executable(argv0: str, cwd: str, env: dict[str, str]) -> str:
    """Resolve one executable explicitly before CreateProcessW.

    Windows does not use the child environment's PATH while resolving a NULL
    ``lpApplicationName``.  The supervisor has a deliberately sanitized PATH,
    so relying on implicit search would both break task PATH overrides and make
    the launched image depend on the supervisor installation.  This resolver
    defines the Preview contract: explicit paths are relative to the task cwd;
    bare names are searched only in the task PATH; and the selected image is
    passed as an absolute ``lpApplicationName``/``executable`` value.
    """

    validate_windows_executable_contract(argv0)
    drive, _tail = ntpath.splitdrive(argv0)

    has_directory = bool(drive or ntpath.dirname(argv0))
    if has_directory:
        bases = [argv0 if ntpath.isabs(argv0) else ntpath.join(cwd, argv0)]
    else:
        path_value = _environment_value(env, "PATH")
        bases = []
        for raw_directory in path_value.split(os.pathsep):
            directory = raw_directory.strip().strip('"')
            directory = _expand_windows_environment(directory, env)
            if not directory:
                directory = cwd
            elif not ntpath.isabs(directory):
                directory = ntpath.join(cwd, directory)
            bases.append(ntpath.join(directory, argv0))

    for base in bases:
        for extension in _executable_extensions(argv0, env):
            candidate = os.path.realpath(os.path.abspath(base + extension))
            if os.path.isfile(candidate):
                if ntpath.splitext(candidate)[1].casefold() not in {".com", ".exe"}:
                    raise RunnerError("direct executable images must use .exe or .com")
                return candidate
    raise RunnerError(f"executable could not be resolved from the task contract: {argv0!r}")


def _run_pipes(
    argv: list[str], cwd: str, stdin_data: bytes | None, env: dict[str, str]
) -> int:
    executable = _resolve_executable(argv[0], cwd, env)
    process = subprocess.Popen(
        argv,
        executable=executable,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        shell=False,
        close_fds=True,
        env=env,
    )
    process.communicate(stdin_data)
    return int(process.returncode)


def _configure_conpty(kernel32: Any) -> None:
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.CreatePseudoConsole.argtypes = [
        COORD,
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    kernel32.CreatePseudoConsole.restype = ctypes.c_long
    kernel32.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
    kernel32.ClosePseudoConsole.restype = None
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.restype = None
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    kernel32.CancelSynchronousIo.restype = wintypes.BOOL
    kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.CancelIoEx.restype = wintypes.BOOL


def _write_handle(
    kernel32: Any,
    handle: Any,
    data: bytes,
    errors: list[str],
    stopping: threading.Event | None = None,
) -> None:
    stopping = stopping or threading.Event()
    try:
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + 65_536]
            written = wintypes.DWORD(0)
            buffer = ctypes.create_string_buffer(chunk)
            if not kernel32.WriteFile(
                handle, buffer, len(chunk), ctypes.byref(written), None
            ):
                code = ctypes.get_last_error()
                if code not in {ERROR_BROKEN_PIPE, ERROR_NO_DATA} and not (
                    stopping.is_set() and code == ERROR_OPERATION_ABORTED
                ):
                    errors.append(f"ConPTY WriteFile failed ({code}): {ctypes.FormatError(code)}")
                break
            progress = int(written.value)
            if progress <= 0:
                errors.append("ConPTY WriteFile made zero progress")
                break
            offset += progress
    except Exception as exc:  # noqa: BLE001 - marshal thread failure to the supervisor.
        errors.append(f"ConPTY input thread failed: {type(exc).__name__}: {exc}")
    finally:
        kernel32.CloseHandle(handle)


def _read_handle(
    kernel32: Any,
    handle: Any,
    errors: list[str],
    stopping: threading.Event | None = None,
) -> None:
    stopping = stopping or threading.Event()
    try:
        target = sys.stdout.buffer
        while True:
            buffer = ctypes.create_string_buffer(65_536)
            count = wintypes.DWORD(0)
            if not kernel32.ReadFile(
                handle, buffer, len(buffer), ctypes.byref(count), None
            ):
                code = ctypes.get_last_error()
                if code not in {ERROR_BROKEN_PIPE, ERROR_NO_DATA} and not (
                    stopping.is_set() and code == ERROR_OPERATION_ABORTED
                ):
                    errors.append(f"ConPTY ReadFile failed ({code}): {ctypes.FormatError(code)}")
                break
            if count.value == 0:
                errors.append("ConPTY ReadFile made zero progress")
                break
            target.write(buffer.raw[: count.value])
            target.flush()
    except Exception as exc:  # noqa: BLE001 - marshal thread failure to the supervisor.
        errors.append(f"ConPTY output thread failed: {type(exc).__name__}: {exc}")
    finally:
        kernel32.CloseHandle(handle)


def _join_threads_until(threads: list[threading.Thread], deadline: float) -> None:
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        thread.join(timeout=remaining)


def _cancel_synchronous_io(kernel32: Any, thread: threading.Thread, handle: Any) -> None:
    if not thread.is_alive():
        return
    if getattr(handle, "value", None) and not kernel32.CancelIoEx(handle, None):
        code = ctypes.get_last_error()
        if code not in {ERROR_INVALID_HANDLE, ERROR_NOT_FOUND} and thread.is_alive():
            # CancelSynchronousIo below remains the authoritative fallback.
            pass
    native_id = thread.native_id
    if native_id is None:
        return
    thread_handle = kernel32.OpenThread(THREAD_TERMINATE, False, int(native_id))
    if not thread_handle:
        return
    try:
        if not kernel32.CancelSynchronousIo(thread_handle):
            code = ctypes.get_last_error()
            if code not in {ERROR_INVALID_HANDLE, ERROR_NOT_FOUND} and thread.is_alive():
                pass
    finally:
        kernel32.CloseHandle(thread_handle)


def _reclaim_io_threads(
    kernel32: Any,
    bindings: list[tuple[threading.Thread, Any]],
    stopping: threading.Event,
    errors: list[str],
    *,
    grace_seconds: float = 5.0,
    cancel_seconds: float = 1.0,
) -> bool:
    """Bound I/O-thread teardown and cancel any synchronous Win32 call."""

    threads = [thread for thread, _handle in bindings]
    _join_threads_until(threads, time.monotonic() + max(0.0, grace_seconds))
    alive = [(thread, handle) for thread, handle in bindings if thread.is_alive()]
    if not alive:
        return True
    stopping.set()
    for thread, handle in alive:
        _cancel_synchronous_io(kernel32, thread, handle)
    _join_threads_until(
        [thread for thread, _handle in alive],
        time.monotonic() + max(0.0, cancel_seconds),
    )
    still_alive = [thread.name for thread, _handle in alive if thread.is_alive()]
    if still_alive:
        errors.append(
            "ConPTY synchronous I/O threads did not stop after cancellation: "
            + ", ".join(still_alive)
        )
        return False
    return True


def _windows_sorted_environment_items(env: dict[str, str]) -> list[tuple[str, str]]:
    kernel32 = _kernel32()
    kernel32.CompareStringOrdinal.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.BOOL,
    ]
    kernel32.CompareStringOrdinal.restype = ctypes.c_int

    def compare(left: tuple[str, str], right: tuple[str, str]) -> int:
        result = kernel32.CompareStringOrdinal(
            left[0], -1, right[0], -1, True
        )
        if result == 0:
            raise _error("CompareStringOrdinal")
        return result - 2

    items = sorted(env.items(), key=functools.cmp_to_key(compare))
    if any(compare(left, right) == 0 for left, right in pairwise(items)):
        raise RunnerError("env contains duplicate names under Windows ordinal semantics")
    return items


def _windows_environment_block(env: dict[str, str]) -> ctypes.Array[Any]:
    entries = [
        f"{key}={value}"
        for key, value in _windows_sorted_environment_items(env)
    ]
    # ``create_unicode_buffer`` supplies one trailing NUL.  Add exactly one so
    # CreateProcessW receives the required double-NUL terminator, not three.
    return ctypes.create_unicode_buffer("\0".join(entries) + "\0")


def _run_conpty(
    argv: list[str], cwd: str, stdin_data: bytes | None, env: dict[str, str]
) -> int:
    kernel32 = _kernel32()
    if not hasattr(kernel32, "CreatePseudoConsole"):
        raise RunnerError("ConPTY requires Windows 10 version 1809 or newer")
    _configure_conpty(kernel32)
    input_read = wintypes.HANDLE()
    input_write = wintypes.HANDLE()
    output_read = wintypes.HANDLE()
    output_write = wintypes.HANDLE()
    pseudo_console = wintypes.HANDLE()
    attribute_list: Any = None
    process_info = PROCESS_INFORMATION()
    io_errors: list[str] = []
    stopping = threading.Event()
    io_bindings: list[tuple[threading.Thread, Any]] = []
    io_cleanup_attempted = False
    try:
        if not kernel32.CreatePipe(
            ctypes.byref(input_read), ctypes.byref(input_write), None, 0
        ):
            raise _error("CreatePipe(input)")
        if not kernel32.CreatePipe(
            ctypes.byref(output_read), ctypes.byref(output_write), None, 0
        ):
            raise _error("CreatePipe(output)")
        result = kernel32.CreatePseudoConsole(
            COORD(120, 30), input_read, output_write, 0, ctypes.byref(pseudo_console)
        )
        if result < 0:
            raise RunnerError(f"CreatePseudoConsole failed (HRESULT 0x{result & 0xFFFFFFFF:08x})")
        size = ctypes.c_size_t(0)
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if size.value == 0:
            raise _error("InitializeProcThreadAttributeList(size)")
        attribute_buffer = ctypes.create_string_buffer(size.value)
        attribute_list = ctypes.cast(attribute_buffer, ctypes.c_void_p)
        if not kernel32.InitializeProcThreadAttributeList(
            attribute_list, 1, 0, ctypes.byref(size)
        ):
            raise _error("InitializeProcThreadAttributeList")
        if not kernel32.UpdateProcThreadAttribute(
            attribute_list,
            0,
            PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            ctypes.c_void_p(pseudo_console.value),
            ctypes.sizeof(wintypes.HANDLE),
            None,
            None,
        ):
            raise _error("UpdateProcThreadAttribute(ConPTY)")

        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.lpAttributeList = attribute_list
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        environment = _windows_environment_block(env)
        executable = _resolve_executable(argv[0], cwd, env)
        if not kernel32.CreateProcessW(
            executable,
            command_line,
            None,
            None,
            False,
            EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
            ctypes.cast(environment, ctypes.c_void_p),
            cwd,
            ctypes.byref(startup.StartupInfo),
            ctypes.byref(process_info),
        ):
            raise _error("CreateProcessW(ConPTY target)")
        kernel32.CloseHandle(process_info.hThread)
        process_info.hThread = wintypes.HANDLE()
        # The pseudoconsole-side pipe handles must remain open until the child
        # has inherited the HPCON attribute and CreateProcessW has returned.
        kernel32.CloseHandle(input_read)
        input_read = wintypes.HANDLE()
        kernel32.CloseHandle(output_write)
        output_write = wintypes.HANDLE()

        reader_handle = wintypes.HANDLE(output_read.value)
        reader = threading.Thread(
            target=_read_handle,
            args=(kernel32, reader_handle, io_errors, stopping),
            name="conpty-output",
            daemon=True,
        )
        output_read = wintypes.HANDLE()
        writer_handle = wintypes.HANDLE(input_write.value)
        writer = threading.Thread(
            target=_write_handle,
            args=(kernel32, writer_handle, stdin_data or b"", io_errors, stopping),
            name="conpty-input",
            daemon=True,
        )
        input_write = wintypes.HANDLE()
        io_bindings = [(reader, reader_handle), (writer, writer_handle)]
        reader.start()
        writer.start()
        while True:
            wait_result = kernel32.WaitForSingleObject(process_info.hProcess, 100)
            if wait_result == WAIT_OBJECT_0:
                break
            if wait_result == WAIT_FAILED:
                raise _error("WaitForSingleObject")
            if wait_result != WAIT_TIMEOUT:
                raise RunnerError(f"WaitForSingleObject returned unexpected status {wait_result}")
            if io_errors:
                raise RunnerError("; ".join(io_errors))
        exit_code = wintypes.DWORD(1)
        if not kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(exit_code)):
            raise _error("GetExitCodeProcess")
        stopping.set()
        kernel32.ClosePseudoConsole(pseudo_console)
        pseudo_console = wintypes.HANDLE()
        io_cleanup_attempted = True
        if not _reclaim_io_threads(
            kernel32, io_bindings, stopping, io_errors
        ):
            raise RunnerError("; ".join(io_errors))
        if io_errors:
            raise RunnerError("; ".join(io_errors))
        return int(exit_code.value)
    finally:
        stopping.set()
        if getattr(pseudo_console, "value", None):
            kernel32.ClosePseudoConsole(pseudo_console)
            pseudo_console = wintypes.HANDLE()
        if io_bindings and not io_cleanup_attempted:
            _reclaim_io_threads(
                kernel32,
                io_bindings,
                stopping,
                io_errors,
                grace_seconds=0.25,
                cancel_seconds=1.0,
            )
        if attribute_list:
            kernel32.DeleteProcThreadAttributeList(attribute_list)
        for handle in (
            input_read,
            input_write,
            output_read,
            output_write,
            process_info.hProcess,
            process_info.hThread,
        ):
            if getattr(handle, "value", None):
                kernel32.CloseHandle(handle)


def main() -> int:
    if os.name != "nt":
        print("windows_job_runner must run on native Windows", file=sys.stderr)
        return 125
    try:
        payload_bytes = sys.stdin.buffer.readline(MAX_PAYLOAD_BYTES + 1)
        if not payload_bytes or len(payload_bytes) > MAX_PAYLOAD_BYTES:
            raise RunnerError("launch payload is missing or too large")
        payload = json.loads(payload_bytes.decode("utf-8"))
        argv, cwd, stdin_data, terminal_mode, env = _validate_payload(payload)
        _windows_sorted_environment_items(env)
        if terminal_mode == "conpty":
            return _run_conpty(argv, cwd, stdin_data, env)
        return _run_pipes(argv, cwd, stdin_data, env)
    except (RunnerError, OSError, json.JSONDecodeError, UnicodeError) as exc:
        print(f"AtomLane Windows supervisor: {exc}", file=sys.stderr, flush=True)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
