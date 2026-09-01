#!/usr/bin/env python3
"""Native Windows Preview gates; the critical class has no skipped tests."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import atom_engine
import mcp_server
import windows_job_runner


def _task(cwd: Path, argv: list[str], **overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {"id": "windows-task", "argv": argv, "cwd": str(cwd)}
    raw.update(overrides)
    return mcp_server.normalize_task(raw, 0, None)


class WindowsPortableContractTests(unittest.TestCase):
    def test_job_slot_reservations_do_not_claim_conpty_host_membership(self) -> None:
        self.assertEqual(
            mcp_server._windows_job_process_slot_reservations("pipes"),
            ("supervisor",),
        )
        self.assertEqual(
            mcp_server._windows_job_process_slot_reservations("conpty"),
            ("supervisor", "conpty_infrastructure_capacity"),
        )

    def test_pipe_runner_pins_executable_and_closes_unrelated_handles(self) -> None:
        process = mock.Mock(returncode=0)
        with (
            mock.patch.object(
                windows_job_runner,
                "_resolve_executable",
                return_value=r"C:\\trusted\\tool.exe",
            ),
            mock.patch.object(
                windows_job_runner.subprocess, "Popen", return_value=process
            ) as popen,
        ):
            returncode = windows_job_runner._run_pipes(
                ["tool.exe", "arg"], r"C:\\work", None, {"PATH": r"C:\\trusted"}
            )
        self.assertEqual(returncode, 0)
        self.assertTrue(popen.call_args.kwargs["close_fds"])
        self.assertEqual(
            popen.call_args.kwargs["executable"], r"C:\\trusted\\tool.exe"
        )
        process.communicate.assert_called_once_with(None)

    def test_runner_rejects_drive_relative_executable(self) -> None:
        with self.assertRaisesRegex(windows_job_runner.RunnerError, "drive-relative"):
            windows_job_runner._resolve_executable(
                r"C:tool.exe", r"C:\\work", {"PATH": r"C:\\tools"}
            )

    def test_runner_rejects_non_image_executable_extensions(self) -> None:
        for executable in ("tool.py", "tool.ps1", "tool.cmd", "tool.bat"):
            with self.subTest(executable=executable), self.assertRaisesRegex(
                windows_job_runner.RunnerError, r"\.exe or \.com"
            ):
                windows_job_runner._executable_extensions(executable, {})

    def test_shared_windows_argv_preflight_fails_closed(self) -> None:
        cases = {
            "": "non-empty unquoted",
            '"tool.exe"': "non-empty unquoted",
            r"C:tool.exe": "drive-relative",
            "tool.py": r"\.exe or \.com",
            "tool.ps1": r"\.exe or \.com",
            "tool.cmd": r"\.exe or \.com",
            "tool.bat": r"\.exe or \.com",
        }
        for executable, error in cases.items():
            with self.subTest(executable=executable), self.assertRaisesRegex(
                windows_job_runner.RunnerError, error
            ):
                windows_job_runner.validate_windows_executable_contract(executable)

    def test_raw_task_and_atom_use_shared_windows_argv_preflight(self) -> None:
        windows_os = mock.Mock(wraps=os)
        windows_os.name = "nt"
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            with (
                mock.patch.object(mcp_server, "os", windows_os),
                self.assertRaisesRegex(mcp_server.InputError, r"\.exe or \.com"),
            ):
                _task(project, ["tool.ps1"])
            with (
                mock.patch.object(atom_engine, "os", windows_os),
                self.assertRaisesRegex(atom_engine.AtomError, r"\.exe or \.com"),
            ):
                atom_engine.validate_atoms(
                    [
                        {
                            "id": "raw-script-image",
                            "operation": {
                                "kind": "command",
                                "argv": ["tool.ps1"],
                                "cwd": str(project),
                            },
                        }
                    ],
                    project,
                )

    def test_environment_block_has_exactly_two_terminal_nuls(self) -> None:
        cases = [
            ([], "\0\0"),
            ([("Path", r"C:\tools"), ("Z", "1")], "Path=C:\\tools\0Z=1\0\0"),
        ]
        for sorted_items, expected in cases:
            with self.subTest(sorted_items=sorted_items):
                with mock.patch.object(
                    windows_job_runner,
                    "_windows_sorted_environment_items",
                    return_value=sorted_items,
                ):
                    block = windows_job_runner._windows_environment_block({})
                self.assertEqual(block[:], expected)
                self.assertEqual(len(block), len(expected))

    def test_conpty_writer_fails_closed_on_zero_progress(self) -> None:
        class ZeroProgressKernel:
            @staticmethod
            def WriteFile(
                _handle: object,
                _buffer: object,
                _length: int,
                written: object,
                _overlapped: object,
            ) -> bool:
                written._obj.value = 0  # type: ignore[attr-defined]
                return True

            @staticmethod
            def CloseHandle(_handle: object) -> bool:
                return True

        errors: list[str] = []
        windows_job_runner._write_handle(
            ZeroProgressKernel(), object(), b"payload", errors
        )
        self.assertTrue(any("zero progress" in message for message in errors), errors)

    def test_conpty_reader_marshals_output_sink_failure(self) -> None:
        class OneReadKernel:
            calls = 0

            def ReadFile(
                self,
                _handle: object,
                buffer: object,
                _length: int,
                count: object,
                _overlapped: object,
            ) -> bool:
                self.calls += 1
                ctypes.memmove(buffer, b"x", 1)
                count._obj.value = 1  # type: ignore[attr-defined]
                return True

            @staticmethod
            def CloseHandle(_handle: object) -> bool:
                return True

        class BrokenOutput:
            @property
            def buffer(self) -> BrokenOutput:
                return self

            def write(self, _data: bytes) -> int:
                raise BrokenPipeError("test sink closed")

            def flush(self) -> None:
                pass

        errors: list[str] = []
        with mock.patch.object(windows_job_runner.sys, "stdout", BrokenOutput()):
            windows_job_runner._read_handle(OneReadKernel(), object(), errors)
        self.assertTrue(any("BrokenPipeError" in message for message in errors), errors)

    def test_conpty_reader_fails_closed_on_zero_progress(self) -> None:
        class ZeroProgressKernel:
            @staticmethod
            def ReadFile(
                _handle: object,
                _buffer: object,
                _length: int,
                count: object,
                _overlapped: object,
            ) -> bool:
                count._obj.value = 0  # type: ignore[attr-defined]
                return True

            @staticmethod
            def CloseHandle(_handle: object) -> bool:
                return True

        errors: list[str] = []
        windows_job_runner._read_handle(ZeroProgressKernel(), object(), errors)
        self.assertTrue(any("zero progress" in message for message in errors), errors)

    def test_conpty_thread_reclamation_has_a_hard_deadline(self) -> None:
        class UncancellableKernel:
            @staticmethod
            def OpenThread(_access: int, _inherit: bool, _thread_id: int) -> None:
                return None

        release = threading.Event()
        worker = threading.Thread(
            target=release.wait,
            args=(5.0,),
            name="test-uncancellable-io",
            daemon=True,
        )
        worker.start()
        errors: list[str] = []
        stopping = threading.Event()
        started = time.monotonic()
        try:
            settled = windows_job_runner._reclaim_io_threads(
                UncancellableKernel(),
                [(worker, object())],
                stopping,
                errors,
                grace_seconds=0.01,
                cancel_seconds=0.02,
            )
            self.assertFalse(settled)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(any("did not stop" in message for message in errors), errors)
        finally:
            release.set()
            worker.join(timeout=1.0)

    def test_case_insensitive_environment_merge_prefers_task_spelling(self) -> None:
        merged = mcp_server._merge_environment(
            {"PATH": "base", "KEEP": "yes"},
            {"Path": "task"},
            case_insensitive=True,
        )
        self.assertNotIn("PATH", merged)
        self.assertEqual(merged["Path"], "task")
        self.assertEqual(merged["KEEP"], "yes")

    def test_hidden_drive_environment_entries_are_not_forwarded(self) -> None:
        with mock.patch.object(
            mcp_server.os,
            "environ",
            {"=C:": r"C:\\work", "KEEP": "yes"},
        ):
            self.assertEqual(mcp_server._base_process_environment(), {"KEEP": "yes"})

    def test_cpu_rate_below_windows_precision_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            mcp_server.InputError, "at least 0.01"
        ):
            _task(
                Path(directory).resolve(),
                [sys.executable, "-V"],
                resources={"cpu_rate_percent": 0.001},
            )

    def test_memory_limit_reserves_space_for_supervisor_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            mcp_server.InputError, "at least 128"
        ):
            _task(
                Path(directory).resolve(),
                [sys.executable, "-V"],
                resources={"memory_limit_mb": 64},
            )


if os.name == "nt":

    class WindowsNativeRuntimeTests(unittest.TestCase):
        def setUp(self) -> None:
            self.temporary = tempfile.TemporaryDirectory(prefix="AtomLane 中文 ")
            self.project = Path(self.temporary.name).resolve()

        def tearDown(self) -> None:
            self.temporary.cleanup()

        def test_job_object_executes_utf8_and_applies_tree_limits(self) -> None:
            task = _task(
                self.project,
                [sys.executable, "-c", "print('实时 中文 🚀', flush=True)"],
                resources={
                    "cpu_rate_percent": 50,
                    "memory_limit_mb": 256,
                    "max_processes": 4,
                },
            )
            result = asyncio.run(mcp_server.execute_task(task, 8192))
            self.assertEqual(result["status"], "succeeded", result["stderr"])
            self.assertIn("实时 中文", result["stdout"])
            self.assertEqual(result["process_tree_backend"], "windows_job_object")
            self.assertEqual(result["applied_resource_controls"]["cpu_rate_percent"], 50.0)
            self.assertTrue(result["applied_resource_controls"]["verified"])
            self.assertEqual(
                result["applied_resource_controls"]["job_active_process_limit"], 5
            )
            self.assertEqual(
                result["applied_resource_controls"]["verified_job_internal_processes"],
                ["supervisor"],
            )
            self.assertEqual(
                result["applied_resource_controls"]["job_reserved_process_slots"],
                1,
            )
            self.assertEqual(
                result["applied_resource_controls"]["conpty_host_job_membership"],
                "not_applicable",
            )
            self.assertEqual(
                result["applied_resource_controls"]["target_process_limit"], 4
            )

        def test_runner_resolves_bare_executable_from_target_path(self) -> None:
            executable = Path(sys.executable).resolve()
            resolved = windows_job_runner._resolve_executable(
                executable.name,
                str(self.project),
                {"PATH": str(executable.parent), "PATHEXT": ".EXE;.COM"},
            )
            self.assertTrue(os.path.samefile(resolved, executable))
            self.assertEqual(
                windows_job_runner.ARGV_ASSURANCE,
                "resolved-lpapplicationname/windows-crt-v1",
            )

        def test_native_environment_block_is_exactly_double_nul_terminated(self) -> None:
            block = windows_job_runner._windows_environment_block(
                {"Z": "1", "Path": str(Path(sys.executable).parent)}
            )
            serialized = block[:]
            self.assertTrue(serialized.endswith("\0\0"), repr(serialized[-4:]))
            self.assertFalse(serialized.endswith("\0\0\0"), repr(serialized[-4:]))
            self.assertEqual(len(block), len(serialized))

        def test_pipe_mode_drains_large_separate_streams(self) -> None:
            stream_size = 750_000
            program = (
                "import sys;"
                f"sys.stdout.buffer.write(b'O'*{stream_size});sys.stdout.flush();"
                f"sys.stderr.buffer.write(b'E'*{stream_size});sys.stderr.flush()"
            )
            task = _task(self.project, [sys.executable, "-c", program])
            result = asyncio.run(mcp_server.execute_task(task, 65_536))
            self.assertEqual(result["status"], "succeeded", result["stderr"])
            self.assertEqual(result["stdout_bytes"], stream_size)
            self.assertEqual(result["stderr_bytes"], stream_size)
            self.assertTrue(result["stdout_truncated"])
            self.assertTrue(result["stderr_truncated"])

        def test_timeout_kills_grandchild_before_delayed_marker(self) -> None:
            marker = self.project / "grandchild-survived.txt"
            child = (
                "import pathlib,time;time.sleep(1.2);"
                f"pathlib.Path({str(marker)!r}).write_text('escaped',encoding='utf-8')"
            )
            parent = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
                "time.sleep(30)"
            )
            task = _task(
                self.project,
                [sys.executable, "-c", parent],
                timeout_seconds=0.25,
            )
            result = asyncio.run(mcp_server.execute_task(task, 8192))
            self.assertEqual(result["status"], "timed_out")
            time.sleep(1.5)
            self.assertFalse(marker.exists(), "a descendant escaped the Job Object")

        def test_supervisor_ignores_task_python_startup_injection(self) -> None:
            injection = self.project / "inject"
            injection.mkdir()
            marker = self.project / "supervisor-startup-ran.txt"
            (injection / "sitecustomize.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
                encoding="utf-8",
            )
            task = _task(
                self.project,
                [sys.executable, "-S", "-c", "print('isolated', flush=True)"],
                env={"PYTHONPATH": str(injection)},
            )
            result = asyncio.run(mcp_server.execute_task(task, 8192))
            self.assertEqual(result["status"], "succeeded", result["stderr"])
            self.assertFalse(marker.exists(), "task startup hooks reached the trusted supervisor")

        def test_completed_parent_cannot_leave_a_pipe_holding_descendant(self) -> None:
            marker = self.project / "detached-descendant-survived.txt"
            child = (
                "import pathlib,time;time.sleep(1.2);"
                f"pathlib.Path({str(marker)!r}).write_text('escaped',encoding='utf-8')"
            )
            parent = (
                "import subprocess,sys;"
                f"subprocess.Popen([sys.executable,'-c',{child!r}],close_fds=False);"
                "print('parent-complete',flush=True)"
            )
            task = _task(
                self.project,
                [sys.executable, "-c", parent],
                timeout_seconds=8,
            )
            started = time.monotonic()
            result = asyncio.run(mcp_server.execute_task(task, 8192))
            self.assertLess(time.monotonic() - started, 5.0)
            self.assertEqual(result["status"], "succeeded", result["stderr"])
            self.assertTrue(result["process_tree_termination"]["verified_empty"])
            time.sleep(1.5)
            self.assertFalse(marker.exists(), "a completed target left a Job descendant")

        def test_conpty_captures_combined_vt_output(self) -> None:
            self.assertTrue(
                mcp_server.platform_capabilities()["conpty_available"],
                "Windows Preview requires native ConPTY on the release runner",
            )
            task = _task(
                self.project,
                [sys.executable, "-c", "print('conpty-live-✓', flush=True)"],
                terminal_mode="conpty",
                resources={"max_processes": 1},
            )
            result = asyncio.run(mcp_server.execute_task(task, 8192))
            self.assertEqual(result["status"], "succeeded", result["stderr"])
            self.assertTrue(result["output_combined"])
            self.assertIn("conpty-live-✓", result["stdout"])
            self.assertEqual(
                result["applied_resource_controls"]["job_active_process_limit"], 3
            )
            self.assertEqual(
                result["applied_resource_controls"]["verified_job_internal_processes"],
                ["supervisor"],
            )
            self.assertEqual(
                result["applied_resource_controls"]["job_process_slot_reservations"],
                ["supervisor", "conpty_infrastructure_capacity"],
            )
            self.assertEqual(
                result["applied_resource_controls"]["job_reserved_process_slots"],
                2,
            )
            self.assertEqual(
                result["applied_resource_controls"]["resource_scope"],
                "supervisor_and_inherited_windows_tree",
            )
            self.assertEqual(
                result["applied_resource_controls"]["conpty_host_job_membership"],
                "not_verified",
            )

        def test_conpty_drains_output_larger_than_pipe_capacity(self) -> None:
            stream_size = 750_000
            program = (
                "import sys;"
                "sys.stdout.write('BEGIN\\n');sys.stdout.flush();"
                f"sys.stdout.write('x'*{stream_size});sys.stdout.flush();"
                "sys.stdout.write('\\nEND\\n');sys.stdout.flush()"
            )
            task = _task(
                self.project,
                [sys.executable, "-c", program],
                terminal_mode="conpty",
            )
            result = asyncio.run(mcp_server.execute_task(task, 65_536))
            self.assertEqual(result["status"], "succeeded", result["stderr"])
            self.assertGreaterEqual(result["stdout_bytes"], stream_size)
            self.assertTrue(result["stdout_truncated"])
            self.assertIn("BEGIN", result["stdout"])
            self.assertIn("END", result["stdout"])

        def test_blocked_synchronous_pipe_read_is_cancelled_and_joined(self) -> None:
            kernel32 = windows_job_runner._kernel32()
            windows_job_runner._configure_conpty(kernel32)
            read_handle = windows_job_runner.wintypes.HANDLE()
            write_handle = windows_job_runner.wintypes.HANDLE()
            self.assertTrue(
                kernel32.CreatePipe(
                    ctypes.byref(read_handle), ctypes.byref(write_handle), None, 0
                )
            )
            errors: list[str] = []
            stopping = threading.Event()
            reader_handle = windows_job_runner.wintypes.HANDLE(read_handle.value)
            reader = threading.Thread(
                target=windows_job_runner._read_handle,
                args=(kernel32, reader_handle, errors, stopping),
                name="test-blocked-conpty-read",
                daemon=True,
            )
            reader.start()
            try:
                time.sleep(0.1)
                settled = windows_job_runner._reclaim_io_threads(
                    kernel32,
                    [(reader, reader_handle)],
                    stopping,
                    errors,
                    grace_seconds=0.05,
                    cancel_seconds=2.0,
                )
                self.assertTrue(settled, errors)
                self.assertFalse(reader.is_alive(), errors)
            finally:
                if reader.is_alive():
                    stopping.set()
                    kernel32.CancelIoEx(reader_handle, None)
                kernel32.CloseHandle(write_handle)

        def test_progress_arrives_before_parallel_tasks_finish(self) -> None:
            snapshots: list[dict[str, object]] = []
            tasks = [
                {
                    "id": f"live-{index}",
                    "argv": [sys.executable, "-c", "import time;time.sleep(1.1)"],
                    "cwd": str(self.project),
                }
                for index in range(2)
            ]
            previous = os.environ.get("MAC_PARALLEL_ACCELERATOR_PROGRESS_INTERVAL")
            os.environ["MAC_PARALLEL_ACCELERATOR_PROGRESS_INTERVAL"] = "0.2"
            try:
                result = asyncio.run(
                    mcp_server.run_parallel(
                        {
                            "tasks": tasks,
                            "max_concurrency": 2,
                            "responsiveness": "throughput",
                        },
                        snapshots.append,
                    )
                )
            finally:
                if previous is None:
                    os.environ.pop("MAC_PARALLEL_ACCELERATOR_PROGRESS_INTERVAL", None)
                else:
                    os.environ["MAC_PARALLEL_ACCELERATOR_PROGRESS_INTERVAL"] = previous
            self.assertEqual(result["summary"]["peak_concurrency"], 2)
            self.assertTrue(
                any(
                    snapshot["running_tasks"] == 2
                    and snapshot["completed_tasks"] == 0
                    and float(snapshot["elapsed_seconds"]) < 1.0
                    for snapshot in snapshots
                ),
                snapshots,
            )

        def test_mcp_pipe_is_utf8_and_uses_the_installed_server_entrypoint(self) -> None:
            server = Path(mcp_server.__file__).resolve()
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "parallel_exec",
                    "arguments": {
                        "tasks": [
                            {
                                "id": "unicode",
                                "argv": [
                                    sys.executable,
                                    "-c",
                                    "print('管道 中文 🌌', flush=True)",
                                ],
                                "cwd": str(self.project),
                            }
                        ],
                        "max_concurrency": 1,
                    },
                },
            }
            completed = subprocess.run(
                [sys.executable, str(server)],
                input=(json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"),
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
            response = json.loads(completed.stdout.decode("utf-8"))
            text = response["result"]["content"][0]["text"]
            self.assertIn("管道 中文 🌌", text)

        def test_windows_argv_and_environment_hazards_fail_closed(self) -> None:
            cases = {
                "": "non-empty unquoted",
                '"tool.exe"': "non-empty unquoted",
                r"C:tool.exe": "drive-relative",
                str(self.project / "build.py"): r"\.exe or \.com",
                str(self.project / "build.ps1"): r"\.exe or \.com",
                str(self.project / "build.cmd"): r"\.exe or \.com",
                str(self.project / "build.bat"): r"\.exe or \.com",
            }
            for executable, error in cases.items():
                with self.subTest(executable=executable), self.assertRaisesRegex(
                    mcp_server.InputError, error
                ):
                    _task(self.project, [executable])

            with self.assertRaisesRegex(mcp_server.InputError, r"\.exe or \.com"):
                mcp_server.atomic_task_plan(
                    {
                        "project_path": str(self.project),
                        "entrypoints": [],
                        "atoms": [
                            {
                                "id": "raw-script-image",
                                "operation": {
                                    "kind": "command",
                                    "argv": [str(self.project / "build.ps1")],
                                    "cwd": str(self.project),
                                },
                            }
                        ],
                    }
                )
            with self.assertRaisesRegex(mcp_server.InputError, "collide"):
                _task(
                    self.project,
                    [sys.executable, "-V"],
                    env={"PATH": "one", "Path": "two"},
                )

        def test_powershell_file_is_one_snapshotted_atom(self) -> None:
            script = self.project / "build task.ps1"
            script.write_text("Write-Output 'ok'\n", encoding="utf-8")
            plan = mcp_server.atomic_task_plan(
                {
                    "project_path": str(self.project),
                    "entrypoints": [
                        {
                            "id": "powershell-build",
                            "adapter": "powershell_file",
                            "script_path": str(script),
                            "arguments": ["参数 value"],
                            "side_effect": False,
                            "effects_declared_complete": True,
                        }
                    ],
                    "atoms": [],
                }
            )
            self.assertTrue(plan["execution_eligible"], plan["execution_blockers"])
            atom = plan["atoms"][0]
            self.assertEqual(atom["provenance"]["adapter"], "powershell_file")
            self.assertEqual(atom["operation"]["argv"].count("-File"), 1)
            self.assertEqual(len(plan["source_snapshots"]), 1)

else:

    class WindowsSourceContractTests(unittest.TestCase):
        def test_windows_supervisor_refuses_non_windows_execution(self) -> None:
            runner = Path(__file__).with_name("windows_job_runner.py")
            completed = subprocess.run(
                [sys.executable, str(runner)],
                input=b"{}\n",
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 125)
            self.assertIn(b"native Windows", completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
