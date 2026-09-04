#!/usr/bin/env python3
"""Cross-platform contract tests that do not pretend WSL is native Windows."""

from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import mcp_server
import platform_adapter


class PlatformAdapterTests(unittest.TestCase):
    def test_scenario_catalog_uses_portable_resource_planner_names(self) -> None:
        catalog = json.loads(
            mcp_server.SCENARIO_CATALOG_PATH.read_text(encoding="utf-8")
        )
        executors = {
            scenario["default_execution"]["executor"]
            for scenario in catalog["scenarios"]
        }

        self.assertNotIn("mac_resource_plan", executors)
        self.assertIn("host_resource_plan", executors)

    def test_scenario_execution_keeps_apple_accelerator_on_macos(self) -> None:
        default = {
            "executor": "mac_accelerator_plan",
            "profile": "accelerator",
        }

        resolved = mcp_server._resolve_scenario_execution(
            default,
            {"boundary": "macos_native", "is_macos_native": True},
        )

        self.assertEqual(resolved["executor"], "mac_accelerator_plan")
        self.assertEqual(
            resolved["platform_resolution"],
            {
                "execution_realm": "macos_native",
                "catalog_executor": "mac_accelerator_plan",
                "selected_executor": "mac_accelerator_plan",
                "status": "applicable",
                "reason": "The catalog executor is available in the current execution realm.",
            },
        )
        self.assertEqual(default["executor"], "mac_accelerator_plan")

    def test_scenario_execution_uses_portable_host_resource_name_off_macos(self) -> None:
        resolved = mcp_server._resolve_scenario_execution(
            {"executor": "mac_resource_plan", "profile": "cpu"},
            {"boundary": "windows_native", "is_macos_native": False},
        )

        self.assertEqual(resolved["executor"], "host_resource_plan")
        self.assertEqual(resolved["platform_resolution"]["status"], "adapted")
        self.assertEqual(
            resolved["platform_resolution"]["catalog_executor"],
            "mac_resource_plan",
        )
        self.assertEqual(
            resolved["platform_resolution"]["selected_executor"],
            "host_resource_plan",
        )

    def test_scenario_execution_keeps_apple_acceleration_advisory_off_macos(
        self,
    ) -> None:
        resolved = mcp_server._resolve_scenario_execution(
            {"executor": "mac_accelerator_plan", "profile": "accelerator"},
            {"boundary": "windows_native", "is_macos_native": False},
        )

        self.assertIsNone(resolved["executor"])
        self.assertEqual(
            resolved["platform_resolution"]["status"], "advisory_only"
        )
        self.assertIn(
            "Apple-specific accelerator routing is unavailable",
            resolved["platform_resolution"]["reason"],
        )

    def test_windows_scenario_plan_does_not_emit_apple_accelerator_executor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            (project / "kernel.metal").write_text(
                "kernel void transform() {}\n", encoding="utf-8"
            )
            with mock.patch.object(
                mcp_server,
                "execution_environment",
                return_value={
                    "system": "Windows",
                    "boundary": "windows_native",
                    "is_windows_native": True,
                    "is_macos_native": False,
                    "is_wsl": False,
                    "is_container": False,
                },
            ):
                result = mcp_server.scenario_plan(
                    {
                        "project_path": str(project),
                        "task_hint": "Optimize this Metal operator",
                        "minimum_confidence": 0.1,
                        "max_scenarios": 20,
                    }
                )

        scenario = next(
            item
            for item in result["matched_scenarios"]
            if item["id"] == "metal-mps-operator"
        )
        self.assertIsNone(scenario["default_execution"]["executor"])
        self.assertEqual(
            scenario["default_execution"]["platform_resolution"]["status"],
            "advisory_only",
        )
        targets = [
            item
            for item in result["optimization_targets"]
            if item["scenario_id"] == "metal-mps-operator"
        ]
        self.assertTrue(targets)
        self.assertTrue(all(item["executor"] is None for item in targets))
        self.assertTrue(
            all(item["execution_status"] == "advisory_only" for item in targets)
        )
        self.assertIn(
            "metal-mps-operator",
            result["decision_summary"]["advisory_only_scenario_ids"],
        )
        self.assertTrue(
            {
                "gpu-operator-eligibility",
                "gpu-batch-fusion",
                "unified-memory-residency",
            }.isdisjoint(result["decision_summary"]["high_value_target_ids"])
        )

    def test_windows_scenario_plan_uses_portable_host_resource_executor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            (project / "threads.py").write_text(
                "import numpy\nOMP_NUM_THREADS = 2\n", encoding="utf-8"
            )
            with mock.patch.object(
                mcp_server,
                "execution_environment",
                return_value={
                    "system": "Windows",
                    "boundary": "windows_native",
                    "is_windows_native": True,
                    "is_macos_native": False,
                    "is_wsl": False,
                    "is_container": False,
                },
            ):
                result = mcp_server.scenario_plan(
                    {
                        "project_path": str(project),
                        "task_hint": "Prevent nested parallelism and thread-pool oversubscription",
                        "minimum_confidence": 0.1,
                        "max_scenarios": 20,
                    }
                )

        scenario = next(
            item
            for item in result["matched_scenarios"]
            if item["id"] == "native-thread-oversubscription"
        )
        self.assertEqual(
            scenario["default_execution"]["executor"], "host_resource_plan"
        )
        self.assertEqual(
            scenario["default_execution"]["platform_resolution"]["status"],
            "applicable",
        )
        self.assertEqual(
            scenario["default_execution"]["platform_resolution"]["catalog_executor"],
            "host_resource_plan",
        )
        self.assertEqual(
            scenario["default_execution"]["platform_resolution"]["selected_executor"],
            "host_resource_plan",
        )
        targets = [
            item
            for item in result["optimization_targets"]
            if item["scenario_id"] == "native-thread-oversubscription"
        ]
        self.assertTrue(targets)
        self.assertTrue(
            all(item["executor"] == "host_resource_plan" for item in targets)
        )
        self.assertTrue(
            all(item["execution_status"] == "applicable" for item in targets)
        )

    def test_windows_hardware_snapshot_never_exposes_computer_name(self) -> None:
        original_cache = mcp_server._STATIC_HARDWARE_CACHE
        try:
            mcp_server._STATIC_HARDWARE_CACHE = None
            with (
                mock.patch.object(mcp_server.platform, "system", return_value="Windows"),
                mock.patch.object(mcp_server.platform, "processor", return_value="Test CPU"),
                mock.patch.object(mcp_server, "windows_physical_cpu_count", return_value=4),
                mock.patch.object(
                    mcp_server,
                    "memory_snapshot",
                    return_value={"total_bytes": 8_000_000_000},
                ),
                mock.patch.dict(
                    os.environ,
                    {"COMPUTERNAME": "PRIVATE-CORPORATE-HOST"},
                    clear=True,
                ),
            ):
                snapshot = mcp_server._static_hardware_snapshot()
            self.assertIsNone(snapshot["model_identifier"])
            self.assertNotIn("PRIVATE-CORPORATE-HOST", json.dumps(snapshot))
        finally:
            mcp_server._STATIC_HARDWARE_CACHE = original_cache

    def test_windows_brokered_realms_are_never_claimed_as_job_contained(self) -> None:
        for executable, target in (
            (r"C:\Program Files\Docker\docker.exe", "docker_daemon"),
            (r"C:\Windows\System32\wsl.exe", "wsl_linux"),
            ("ssh.exe", "remote_ssh"),
        ):
            boundary = platform_adapter.brokered_execution_boundary(
                executable, boundary="windows_native"
            )
            self.assertIsNotNone(boundary)
            self.assertEqual(boundary["target_realm"], target)
            self.assertFalse(boundary["brokered_work_contained"])
            self.assertFalse(boundary["job_resource_limits_apply_to_brokered_work"])
        self.assertIsNone(
            platform_adapter.brokered_execution_boundary(
                "python.exe", boundary="windows_native"
            )
        )

    def test_stats_path_uses_native_atomlane_directories(self) -> None:
        with (
            mock.patch.object(platform_adapter.platform, "system", return_value="Windows"),
            mock.patch.dict(
                os.environ,
                {"LOCALAPPDATA": r"C:\Users\ExampleUser\AppData\Local"},
                clear=True,
            ),
        ):
            self.assertEqual(
                str(platform_adapter.default_stats_path()),
                str(
                    Path(r"C:\Users\ExampleUser\AppData\Local")
                    / "AtomLane"
                    / "stats.json"
                ),
            )
        with (
            mock.patch.object(platform_adapter.platform, "system", return_value="Darwin"),
            mock.patch.object(
                platform_adapter.Path,
                "home",
                return_value=Path("/Users/example"),
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            path = platform_adapter.default_stats_path()
        self.assertIn("AtomLane", str(path))

    def test_conpty_process_limit_policy_is_fail_closed(self) -> None:
        self.assertIsNone(platform_adapter.windows_process_limit_blocker("pipes", 4))
        self.assertIsNone(platform_adapter.windows_process_limit_blocker("conpty", None))
        self.assertIn(
            "console-host Job Object membership is verified",
            platform_adapter.windows_process_limit_blocker("conpty", 1) or "",
        )

    def test_windows_capabilities_publish_preview_constraints(self) -> None:
        with (
            mock.patch.object(
                platform_adapter,
                "execution_environment",
                return_value={"boundary": "windows_native", "is_windows_native": True},
            ),
            mock.patch.object(
                platform_adapter,
                "_windows_kernel32",
                return_value=mock.Mock(CreatePseudoConsole=object()),
            ),
        ):
            capabilities = platform_adapter.platform_capabilities()
        self.assertFalse(capabilities["conpty_stdin_supported"])
        self.assertEqual(
            capabilities["resource_control_constraints"]["max_processes"],
            {
                "minimum": 2,
                "maximum": 4096,
                "terminal_modes": ["pipes"],
                "scope": "all_job_members_including_supervisor",
            },
        )

    def test_explicit_windows_path_resolution_is_hermetic_and_quote_strict(self) -> None:
        trusted = r"C:\trusted\pwsh.exe"

        def is_file(path: str) -> bool:
            return path.casefold() == trusted.casefold()

        with (
            mock.patch.dict(
                platform_adapter.os.environ,
                {"TOOL_HOME": r"C:\trusted"},
                clear=False,
            ),
            mock.patch.object(platform_adapter.os.path, "isfile", side_effect=is_file),
            mock.patch.object(
                platform_adapter.os.path,
                "realpath",
                side_effect=lambda path: path,
            ),
        ):
            self.assertIsNone(
                platform_adapter.resolve_windows_path_executable(
                    "pwsh", r"%TOOL_HOME%"
                )
            )
            for malformed in (r'"C:\trusted', 'C:\\trusted"', "C:\\trusted\n"):
                with self.subTest(malformed=malformed):
                    self.assertIsNone(
                        platform_adapter.resolve_windows_path_executable(
                            "pwsh", malformed
                        )
                    )
            self.assertEqual(
                platform_adapter.resolve_windows_path_executable(
                    "pwsh", r'"C:\trusted"'
                ),
                trusted,
            )

    def test_exclusive_stats_lock_never_loses_threaded_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stats = Path(temporary) / "stats.json"
            errors: list[BaseException] = []
            errors_lock = threading.Lock()
            start = threading.Barrier(12, timeout=5)

            def record() -> None:
                try:
                    start.wait()
                    mcp_server._record_time_saved(1.25)
                except BaseException as exc:  # noqa: BLE001 - retain thread failures.
                    with errors_lock:
                        errors.append(exc)

            with mock.patch.dict(
                os.environ, {"ATOMLANE_STATS_PATH": str(stats)}
            ):
                threads = [
                    threading.Thread(target=record, daemon=True)
                    for _ in range(12)
                ]
                for thread in threads:
                    thread.start()
                deadline = time.monotonic() + 20
                for thread in threads:
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))
                self.assertFalse(
                    any(thread.is_alive() for thread in threads),
                    "stats workers did not finish before the bounded deadline",
                )
                self.assertEqual(errors, [])
                result = json.loads(stats.read_text(encoding="utf-8"))
                self.assertEqual(result["run_count"], 12)
                self.assertEqual(result["cumulative_saved_seconds"], 15.0)

    def test_stats_lock_keeps_measured_and_estimated_buckets_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stats = Path(temporary) / "stats.json"
            errors: list[BaseException] = []
            errors_lock = threading.Lock()
            start = threading.Barrier(12, timeout=5)

            def record(evidence_kind: str) -> None:
                try:
                    start.wait()
                    mcp_server._record_time_saved(
                        1.0,
                        evidence_kind=evidence_kind,
                    )
                except BaseException as exc:  # noqa: BLE001 - retain failures.
                    with errors_lock:
                        errors.append(exc)

            with mock.patch.dict(
                os.environ,
                {"ATOMLANE_STATS_PATH": str(stats)},
            ):
                threads = [
                    threading.Thread(
                        target=record,
                        args=("measured" if index % 2 == 0 else "estimated",),
                        daemon=True,
                    )
                    for index in range(12)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=20)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            result = json.loads(stats.read_text(encoding="utf-8"))
            self.assertEqual(result["run_count"], 6)
            self.assertEqual(result["cumulative_saved_seconds"], 6.0)
            self.assertEqual(result["measured_run_count"], 6)
            self.assertEqual(result["estimated_run_count"], 6)
            self.assertEqual(result["cumulative_estimated_saved_seconds"], 6.0)

    @unittest.skipUnless(os.name == "posix", "requires POSIX flock semantics")
    def test_posix_file_lock_can_fail_immediately_without_sleeping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "execution.lock"
            with (
                platform_adapter.exclusive_file_lock(lock_path),
                mock.patch.object(platform_adapter.time, "sleep") as sleep,
                self.assertRaises(TimeoutError),
                platform_adapter.exclusive_file_lock(
                    lock_path,
                    timeout_seconds=0.0,
                ),
            ):
                self.fail("contended non-blocking lock unexpectedly yielded")
            sleep.assert_not_called()

    def test_windows_file_lock_zero_timeout_does_not_retry(self) -> None:
        attempts = 0

        def locking(_fd: int, mode: int, _length: int) -> None:
            nonlocal attempts
            if mode == 1:
                attempts += 1
                raise OSError(errno.EACCES, "simulated contention")

        fake_msvcrt = types.SimpleNamespace(
            LK_NBLCK=1,
            LK_UNLCK=2,
            locking=locking,
        )
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "execution.lock"
            with (
                mock.patch.object(
                    platform_adapter.platform, "system", return_value="Windows"
                ),
                mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                mock.patch.object(platform_adapter.time, "sleep") as sleep,
                self.assertRaises(TimeoutError),
                platform_adapter.exclusive_file_lock(
                    lock_path,
                    timeout_seconds=0.0,
                ),
            ):
                self.fail("contended non-blocking lock unexpectedly yielded")
            self.assertEqual(attempts, 1)
            sleep.assert_not_called()

    def test_windows_stats_lock_retries_expected_contention(self) -> None:
        acquire_attempts = 0
        operations: list[int] = []

        def locking(_fd: int, mode: int, _length: int) -> None:
            nonlocal acquire_attempts
            operations.append(mode)
            if mode == 1:
                acquire_attempts += 1
                if acquire_attempts < 3:
                    raise OSError(errno.EDEADLK, "simulated contention")

        fake_msvcrt = types.SimpleNamespace(
            LK_NBLCK=1,
            LK_UNLCK=2,
            locking=locking,
        )
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "stats.lock"
            with (
                mock.patch.object(
                    platform_adapter.platform, "system", return_value="Windows"
                ),
                mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                mock.patch.object(platform_adapter.time, "sleep") as sleep,
                platform_adapter.exclusive_file_lock(lock_path),
            ):
                pass
        self.assertEqual(operations, [1, 1, 1, 2])
        self.assertEqual(sleep.call_count, 2)

    def test_windows_stats_lock_locks_beyond_eof_without_prefill(self) -> None:
        operations: list[int] = []

        def locking(_fd: int, mode: int, _length: int) -> None:
            operations.append(mode)

        fake_msvcrt = types.SimpleNamespace(
            LK_NBLCK=1,
            LK_UNLCK=2,
            locking=locking,
        )
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "stats.lock"
            with (
                mock.patch.object(
                    platform_adapter.platform, "system", return_value="Windows"
                ),
                mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                platform_adapter.exclusive_file_lock(lock_path),
            ):
                self.assertEqual(lock_path.stat().st_size, 0)
            self.assertEqual(lock_path.stat().st_size, 0)

        self.assertEqual(operations, [1, 2])

    def test_windows_stats_lock_propagates_non_contention_errors(self) -> None:
        def locking(_fd: int, mode: int, _length: int) -> None:
            if mode == 1:
                raise OSError(errno.EBADF, "simulated invalid handle")

        fake_msvcrt = types.SimpleNamespace(
            LK_NBLCK=1,
            LK_UNLCK=2,
            locking=locking,
        )
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "stats.lock"
            with (
                mock.patch.object(
                    platform_adapter.platform, "system", return_value="Windows"
                ),
                mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                mock.patch.object(platform_adapter.time, "sleep") as sleep,
                self.assertRaisesRegex(OSError, "simulated invalid handle"),
                platform_adapter.exclusive_file_lock(lock_path),
            ):
                self.fail("non-contention lock error unexpectedly yielded")
            sleep.assert_not_called()

    def test_windows_stats_lock_has_a_bounded_contention_deadline(self) -> None:
        def locking(_fd: int, mode: int, _length: int) -> None:
            if mode == 1:
                raise OSError(errno.EACCES, "simulated persistent contention")

        fake_msvcrt = types.SimpleNamespace(
            LK_NBLCK=1,
            LK_UNLCK=2,
            locking=locking,
        )
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "stats.lock"
            with (
                mock.patch.object(
                    platform_adapter.platform, "system", return_value="Windows"
                ),
                mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                mock.patch.object(
                    platform_adapter, "WINDOWS_FILE_LOCK_TIMEOUT_SECONDS", 0.0
                ),
                self.assertRaisesRegex(
                    TimeoutError, "timed out acquiring Windows stats lock"
                ),
                platform_adapter.exclusive_file_lock(lock_path),
            ):
                self.fail("expired lock deadline unexpectedly yielded")
            lock_path.unlink()

    def test_platform_contract_is_bound_into_immutable_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            atom = {
                "id": "inspect",
                "operation": {
                    "kind": "read",
                    "argv": ["tool", "--version"],
                    "cwd": str(project),
                    "completion": "process_exit",
                    "internal_parallelism": {"kind": "none", "tokens": None},
                },
                "accesses": [],
                "effects": [],
                "claims": [],
                "side_effect": False,
                "assurance": {
                    "parse": "exact",
                    "control": "exact",
                    "effects": "complete_declared",
                    "codegen": "exact_argv",
                    "rank": 1.0,
                    "blockers": [],
                },
            }
            plan = mcp_server.atomic_task_plan(
                {
                    "project_path": str(project),
                    "atoms": [atom],
                    "entrypoints": [],
                }
            )
            original = dict(plan["platform_contract"])
            self.assertEqual(original["adapter_protocol"], "atomlane-platform/v2")
            self.assertFalse(original["conpty_stdin_supported"])
            self.assertEqual(
                original["resource_control_constraints"],
                mcp_server._current_platform_contract()[
                    "resource_control_constraints"
                ],
            )
            foreign = {**original, "environment_kind": "foreign-realm"}
            with (
                mock.patch.object(
                    mcp_server, "_current_platform_contract", return_value=foreign
                ),
                self.assertRaisesRegex(mcp_server.InputError, "platform contract"),
            ):
                mcp_server._verify_compiled_plan(
                    {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                )

            changed_resource_semantics = {
                **original,
                "resource_control_constraints": {
                    "max_processes": {
                        "minimum": 1,
                        "maximum": 4096,
                        "terminal_modes": ["pipes", "conpty"],
                        "scope": "target_tree_only",
                    }
                },
            }
            with (
                mock.patch.object(
                    mcp_server,
                    "_current_platform_contract",
                    return_value=changed_resource_semantics,
                ),
                self.assertRaisesRegex(mcp_server.InputError, "platform contract"),
            ):
                mcp_server._verify_compiled_plan(
                    {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                )

            changed_conpty_stdin_semantics = {
                **original,
                "conpty_stdin_supported": True,
            }
            with (
                mock.patch.object(
                    mcp_server,
                    "_current_platform_contract",
                    return_value=changed_conpty_stdin_semantics,
                ),
                self.assertRaisesRegex(mcp_server.InputError, "platform contract"),
            ):
                mcp_server._verify_compiled_plan(
                    {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                )

    def test_machine_snapshot_declares_sources_and_execution_boundary(self) -> None:
        snapshot = mcp_server.machine_snapshot()
        self.assertIn(snapshot["execution_environment"]["boundary"], {
            "macos_native",
            "windows_native",
            "linux_native",
            "wsl_linux",
            "linux_container",
            "unknown",
        })
        self.assertIn("source", snapshot["load_average"])
        self.assertEqual(
            snapshot["capabilities"]["execution_environment"],
            snapshot["execution_environment"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
