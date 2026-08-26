#!/usr/bin/env python3
"""Runtime regressions for scheduler failure propagation and bounded capture."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_server
from live_runner import ConsoleProgress


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="mac-parallel-runtime-")
        self.project = Path(self.temporary.name)
        os.environ["MAC_PARALLEL_ACCELERATOR_STATS_PATH"] = str(self.project / "stats.json")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reverse_order_failure_propagates_to_fixed_point(self) -> None:
        result = asyncio.run(
            mcp_server.run_dag(
                {
                    "default_cwd": str(self.project),
                    "max_concurrency": 2,
                    "tasks": [
                        {
                            "id": "C",
                            "argv": [sys.executable, "-c", "raise SystemExit('must not run')"],
                            "depends_on": ["B"],
                        },
                        {
                            "id": "B",
                            "argv": [sys.executable, "-c", "raise SystemExit('must not run')"],
                            "depends_on": ["A"],
                        },
                        {"id": "A", "argv": [sys.executable, "-c", "raise SystemExit(9)"]},
                    ],
                }
            )
        )
        by_id = {item["id"]: item for item in result["results"]}
        self.assertEqual(by_id["A"]["status"], "failed")
        self.assertEqual(by_id["B"]["status"], "skipped")
        self.assertEqual(by_id["C"]["status"], "skipped")

    def test_output_is_bounded_during_read(self) -> None:
        result = asyncio.run(
            mcp_server.execute_task(
                {
                    "id": "large-output",
                    "argv": [
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.write('x' * 2_000_000); sys.stderr.write('y' * 1_000_000)",
                    ],
                    "cwd": str(self.project),
                    "env": {},
                    "stdin": None,
                    "timeout_seconds": 10.0,
                    "depends_on": [],
                    "side_effect": False,
                },
                1024,
            )
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["stdout_bytes"], 2_000_000)
        self.assertEqual(result["stderr_bytes"], 1_000_000)
        self.assertTrue(result["stdout_truncated"])
        self.assertTrue(result["stderr_truncated"])
        self.assertLess(len(result["stdout"].encode()), 1200)
        self.assertLess(len(result["stderr"].encode()), 1200)

    def test_side_effect_timeout_has_unknown_outcome(self) -> None:
        result = asyncio.run(
            mcp_server.execute_task(
                {
                    "id": "timeout",
                    "argv": [sys.executable, "-c", "import time; time.sleep(5)"],
                    "cwd": str(self.project),
                    "env": {},
                    "stdin": None,
                    "timeout_seconds": 0.05,
                    "depends_on": [],
                    "side_effect": True,
                },
                1024,
            )
        )
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["outcome"], "unknown")
        self.assertFalse(result["automatic_retry_allowed"])

    def test_timeout_covers_blocked_stdin_delivery(self) -> None:
        result = asyncio.run(
            mcp_server.execute_task(
                {
                    "id": "blocked-stdin",
                    "argv": [sys.executable, "-c", "import time; time.sleep(5)"],
                    "cwd": str(self.project),
                    "env": {},
                    "stdin": "x" * mcp_server.MAX_STDIN_BYTES,
                    "timeout_seconds": 0.05,
                    "depends_on": [],
                    "side_effect": False,
                },
                1024,
            )
        )
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["outcome"], "not_completed")

    def test_atomic_executor_rejects_lifecycle_edges_it_cannot_honor(self) -> None:
        def atom(atom_id: str, dependencies: list[dict[str, str]] | None = None) -> dict:
            return {
                "id": atom_id,
                "operation": {
                    "kind": "command",
                    "argv": ["/usr/bin/true"],
                    "cwd": str(self.project),
                    "completion": "process_exit",
                    "internal_parallelism": {"kind": "none", "tokens": None},
                },
                "dependencies": dependencies or [],
                "accesses": [],
                "claims": [],
                "side_effect": False,
                "semantics": {
                    "idempotent": True,
                    "retryable": False,
                    "deterministic": True,
                    "cacheable": False,
                    "commutative": False,
                    "cancel_safe": True,
                    "splittable": False,
                    "reorderable": "explicit",
                },
                "cost": {"duration_seconds": 0.01, "startup_seconds": 0.0},
                "batch": None,
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
                "project_path": str(self.project),
                "atoms": [
                    atom("service"),
                    atom("consumer", [{"atom": "service", "kind": "after_ready"}]),
                ],
                "max_concurrency": 2,
            }
        )
        self.assertTrue(plan["execution_eligible"])
        with self.assertRaisesRegex(mcp_server.InputError, "lifecycle/stream dependency"):
            mcp_server._verify_compiled_plan(
                {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
            )

    def test_atomic_plan_hash_covers_scheduler_and_resource_envelope(self) -> None:
        atom = {
            "id": "read",
            "operation": {
                "kind": "read",
                "argv": ["/usr/bin/true"],
                "cwd": str(self.project),
                "completion": "process_exit",
                "internal_parallelism": {"kind": "none", "tokens": None},
            },
            "dependencies": [],
            "accesses": [{"resource": "input.txt", "mode": "read"}],
            "claims": [],
            "effects": [],
            "side_effect": False,
            "semantics": {
                "idempotent": True,
                "retryable": False,
                "deterministic": True,
                "cacheable": False,
                "commutative": False,
                "cancel_safe": True,
                "splittable": False,
                "reorderable": "explicit",
            },
            "cost": {"duration_seconds": 0.01, "startup_seconds": 0.0},
            "batch": None,
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
            {"project_path": str(self.project), "atoms": [atom], "max_concurrency": 1}
        )
        plan["schedule"]["makespan_seconds"] = 999.0
        with self.assertRaisesRegex(mcp_server.InputError, "envelope was changed"):
            mcp_server._verify_compiled_plan(
                {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
            )

    def test_live_console_emits_periodic_unchanged_state(self) -> None:
        progress = ConsoleProgress()
        base = {
            "running_tasks": 2,
            "ready_tasks": 1,
            "completed_tasks": 0,
            "task_count": 3,
            "failed_tasks": 0,
            "estimated_saved_so_far_seconds": 0.25,
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            progress({**base, "elapsed_seconds": 0.0})
            progress({**base, "elapsed_seconds": 0.5})
            progress({**base, "elapsed_seconds": 1.0})
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("运行中 2" in line and "就绪 1" in line for line in lines))
        self.assertTrue(all("当前预计节约 0.2s" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
