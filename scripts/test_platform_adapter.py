#!/usr/bin/env python3
"""Cross-platform contract tests that do not pretend WSL is native Windows."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import mcp_server
import platform_adapter


class PlatformAdapterTests(unittest.TestCase):
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

    def test_stats_path_uses_native_conventions_and_preserves_macos_history(self) -> None:
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
        self.assertIn("Mac Parallel Accelerator", str(path))

    def test_conpty_process_limit_policy_is_fail_closed(self) -> None:
        self.assertIsNone(platform_adapter.windows_process_limit_blocker("pipes", 4))
        self.assertIsNone(platform_adapter.windows_process_limit_blocker("conpty", None))
        self.assertIn(
            "console-host Job Object membership is verified",
            platform_adapter.windows_process_limit_blocker("conpty", 1) or "",
        )

    def test_windows_capabilities_publish_process_limit_constraints(self) -> None:
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
            with mock.patch.dict(
                os.environ, {"MAC_PARALLEL_ACCELERATOR_STATS_PATH": str(stats)}
            ):
                threads = [
                    threading.Thread(target=mcp_server._record_time_saved, args=(1.25,))
                    for _ in range(12)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                result = json.loads(stats.read_text(encoding="utf-8"))
                self.assertEqual(result["run_count"], 12)
                self.assertEqual(result["cumulative_saved_seconds"], 15.0)

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
