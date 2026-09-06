#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from host_scheduler import HostScheduler, HostSchedulerError, host_fingerprint


class HostSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="atomlane-host-scheduler-")
        self.state_path = Path(self.temporary.name) / "state.json"
        self.host_id = host_fingerprint(
            {
                "execution_environment": {"boundary": "test_native"},
                "machine": "test-architecture",
                "chip": "test-chip",
                "model_identifier": "test-model",
                "logical_cpus": 8,
                "physical_cpus": 8,
                "memory_total_bytes": 16 * 1024**3,
            }
        )
        self.scheduler = HostScheduler(self.host_id, path=self.state_path)
        self.capacities = {
            "worker_slot": 4.0,
            "cpu_core": 4.0,
            "memory_mb": 4096.0,
            "accelerator_slot": 1.0,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register(self, session_id: str, capacities: dict[str, float] | None = None) -> None:
        self.scheduler.register(
            session_id,
            capacities=capacities or self.capacities,
            profile="cpu",
            responsiveness="throughput",
            project_hash="sha256:" + "0" * 64,
        )

    def acquire(self, session_id: str, atom_id: str, workers: float = 1.0):
        return self.scheduler.try_acquire(
            session_id,
            atom_id,
            {"worker_slot": workers, "cpu_core": workers},
        )

    def test_single_session_borrows_the_complete_host_envelope(self) -> None:
        self.register("s_one")
        self.scheduler.heartbeat("s_one", ready_tasks=4, running_tasks=0)
        reservations = [self.acquire("s_one", f"atom-{index}") for index in range(4)]
        self.assertTrue(all(reservations))
        self.assertIsNone(self.acquire("s_one", "atom-overflow"))
        snapshot = self.scheduler.snapshot()
        self.assertEqual(snapshot["reserved"]["worker_slot"], 4.0)
        self.assertEqual(snapshot["active_reservations"], 4)

    def test_ready_atoms_are_evaluated_as_one_capacity_batch(self) -> None:
        self.register("s_batch")
        self.scheduler.heartbeat("s_batch", ready_tasks=5, running_tasks=0)
        decision = self.scheduler.try_acquire_batch(
            "s_batch",
            [
                {
                    "atom_id": f"batch-{index}",
                    "claims": {"worker_slot": 1.0, "cpu_core": 1.0},
                }
                for index in range(5)
            ],
        )
        self.assertEqual(decision["decision_scope"], "ordered_ready_batch")
        self.assertEqual(len(decision["admitted"]), 4)
        self.assertEqual(
            decision["denied"],
            [{"atom_id": "batch-4", "reason": "capacity:worker_slot"}],
        )

    def test_competing_sessions_receive_a_soft_fair_share(self) -> None:
        self.register("s_alpha")
        self.register("s_beta")
        self.scheduler.heartbeat("s_alpha", ready_tasks=4, running_tasks=0)
        self.scheduler.heartbeat("s_beta", ready_tasks=4, running_tasks=0)

        alpha = [self.acquire("s_alpha", f"alpha-{index}") for index in range(2)]
        self.assertTrue(all(alpha))
        self.assertIsNone(self.acquire("s_alpha", "alpha-third"))
        beta = [self.acquire("s_beta", f"beta-{index}") for index in range(2)]
        self.assertTrue(all(beta))

    def test_most_responsive_active_session_defines_shared_headroom(self) -> None:
        self.register(
            "s_throughput",
            {**self.capacities, "worker_slot": 8.0, "cpu_core": 8.0},
        )
        self.register("s_interactive")
        self.scheduler.heartbeat("s_throughput", ready_tasks=8, running_tasks=0)
        self.scheduler.heartbeat("s_interactive", ready_tasks=1, running_tasks=0)
        snapshot = self.scheduler.snapshot()
        self.assertEqual(snapshot["effective_capacities"]["worker_slot"], 4.0)
        self.assertEqual(snapshot["effective_capacities"]["cpu_core"], 4.0)

    def test_close_releases_every_reservation_owned_by_session(self) -> None:
        self.register("s_close")
        self.scheduler.heartbeat("s_close", ready_tasks=2, running_tasks=0)
        self.assertIsNotNone(self.acquire("s_close", "first"))
        self.assertIsNotNone(self.acquire("s_close", "second"))
        self.scheduler.close("s_close")
        snapshot = self.scheduler.snapshot()
        self.assertEqual(snapshot["active_sessions"], 0)
        self.assertEqual(snapshot["active_reservations"], 0)

    def test_dead_stale_session_is_reclaimed_with_its_tokens(self) -> None:
        self.register("s_stale")
        self.scheduler.heartbeat("s_stale", ready_tasks=1, running_tasks=0)
        self.assertIsNotNone(self.acquire("s_stale", "stale-atom"))
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["sessions"]["s_stale"]["pid"] = 2_000_000_000
        state["sessions"]["s_stale"]["heartbeat_at"] = time.time() - 60
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        recovering = HostScheduler(
            self.host_id,
            path=self.state_path,
            stale_seconds=2,
        )
        snapshot = recovering.snapshot()
        self.assertEqual(snapshot["active_sessions"], 0)
        self.assertEqual(snapshot["active_reservations"], 0)

    def test_stale_but_live_session_is_not_reclaimed(self) -> None:
        self.register("s_sleep_safe")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["sessions"]["s_sleep_safe"]["heartbeat_at"] = time.time() - 3600
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        recovering = HostScheduler(
            self.host_id,
            path=self.state_path,
            stale_seconds=2,
        )
        snapshot = recovering.snapshot()
        self.assertEqual(snapshot["active_sessions"], 1)

    def test_empty_admission_poll_does_not_rewrite_unchanged_state(self) -> None:
        self.register("s_idle")
        before = json.loads(self.state_path.read_text(encoding="utf-8"))["generation"]
        decision = self.scheduler.try_acquire_batch("s_idle", [])
        after = json.loads(self.state_path.read_text(encoding="utf-8"))["generation"]
        self.assertEqual(decision["admitted"], {})
        self.assertEqual(after, before)

    def test_lock_contention_fails_closed_with_a_scheduler_error(self) -> None:
        with (
            mock.patch(
                "host_scheduler.exclusive_file_lock",
                side_effect=TimeoutError("busy"),
            ),
            self.assertRaisesRegex(HostSchedulerError, "lock is unavailable"),
        ):
            self.scheduler.snapshot()

    def test_corrupt_state_fails_closed_instead_of_resetting_capacity(self) -> None:
        self.state_path.write_text("not-json", encoding="utf-8")
        with self.assertRaisesRegex(HostSchedulerError, "invalid"):
            self.scheduler.snapshot()
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), "not-json")

    def test_host_fingerprint_separates_execution_realms(self) -> None:
        base = {
            "execution_environment": {"boundary": "macos_native"},
            "machine": "arm64",
            "chip": "Apple M4",
            "model_identifier": "Mac16,1",
            "logical_cpus": 10,
            "physical_cpus": 10,
            "memory_total_bytes": 32 * 1024**3,
        }
        self.assertEqual(host_fingerprint(base), host_fingerprint(dict(base)))
        foreign = {**base, "execution_environment": {"boundary": "wsl_linux"}}
        self.assertNotEqual(host_fingerprint(base), host_fingerprint(foreign))

    def test_capacity_is_shared_with_an_independent_python_process(self) -> None:
        capacities = {
            "worker_slot": 1.0,
            "cpu_core": 1.0,
            "memory_mb": 1024.0,
            "accelerator_slot": 1.0,
        }
        self.register("s_parent", capacities)
        self.scheduler.heartbeat("s_parent", ready_tasks=1, running_tasks=0)
        parent = self.acquire("s_parent", "parent")
        self.assertIsNotNone(parent)
        assert parent is not None

        child = """
import sys
from pathlib import Path
from host_scheduler import HostScheduler
scheduler = HostScheduler(sys.argv[1], path=Path(sys.argv[2]))
capacities = {'worker_slot': 1.0, 'cpu_core': 1.0, 'memory_mb': 1024.0, 'accelerator_slot': 1.0}
scheduler.register('s_child', capacities=capacities, profile='cpu', responsiveness='throughput', project_hash='sha256:' + '1' * 64)
scheduler.heartbeat('s_child', ready_tasks=1, running_tasks=0)
reservation = scheduler.try_acquire('s_child', 'child', {'worker_slot': 1.0, 'cpu_core': 1.0})
print('admitted' if reservation else 'blocked')
scheduler.close('s_child')
"""
        environment = dict(os.environ)
        scripts = str(Path(__file__).resolve().parent)
        environment["PYTHONPATH"] = scripts + os.pathsep + environment.get(
            "PYTHONPATH", ""
        )

        blocked = subprocess.run(
            [sys.executable, "-c", child, self.host_id, str(self.state_path)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(blocked.stdout.strip(), "blocked")

        self.scheduler.release("s_parent", parent["reservation_id"])
        admitted = subprocess.run(
            [sys.executable, "-c", child, self.host_id, str(self.state_path)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )
        self.assertEqual(admitted.returncode, 0, admitted.stderr)
        self.assertEqual(admitted.stdout.strip(), "admitted")

    def test_two_atomic_processes_share_one_global_worker_slot(self) -> None:
        child = r'''
import asyncio
import sys
from pathlib import Path
import mcp_server

project = str(Path(sys.argv[1]).resolve())
atom_id = sys.argv[2]
atom = {
    "id": atom_id,
    "operation": {
        "kind": "read",
        "argv": [sys.executable, "-c", "import time;time.sleep(0.4)"],
        "cwd": project,
        "completion": "process_exit",
        "internal_parallelism": {"kind": "none", "tokens": None},
    },
    "dependencies": [],
    "accesses": [],
    "effects": [],
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
    "cost": {"duration_seconds": 0.4, "startup_seconds": 0.0},
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
plan = mcp_server.atomic_task_plan({
    "project_path": project,
    "atoms": [atom],
    "max_concurrency": 1,
    "responsiveness": "throughput",
})
result = asyncio.run(mcp_server.run_atomic({
    "compiled_plan": plan,
    "plan_hash": plan["plan_hash"],
}))
print(result["summary"]["status_counts"].get("succeeded", 0))
'''
        environment = dict(os.environ)
        scripts = str(Path(__file__).resolve().parent)
        environment["PYTHONPATH"] = scripts + os.pathsep + environment.get(
            "PYTHONPATH", ""
        )
        environment["ATOMLANE_SCHEDULER_STATE_PATH"] = str(self.state_path)
        environment["ATOMLANE_STATS_PATH"] = str(
            Path(self.temporary.name) / "stats.json"
        )
        command = [
            sys.executable,
            "-c",
            child,
            self.temporary.name,
        ]

        started = time.monotonic()
        first = subprocess.Popen(
            [*command, "first"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        second = subprocess.Popen(
            [*command, "second"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        first_stdout, first_stderr = first.communicate(timeout=10)
        second_stdout, second_stderr = second.communicate(timeout=10)
        elapsed = time.monotonic() - started

        self.assertEqual(first.returncode, 0, first_stderr)
        self.assertEqual(second.returncode, 0, second_stderr)
        self.assertEqual(first_stdout.strip(), "1")
        self.assertEqual(second_stdout.strip(), "1")
        self.assertGreaterEqual(elapsed, 0.7)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["sessions"], {})
        self.assertEqual(state["reservations"], {})

    @unittest.skipUnless(os.name == "posix", "requires POSIX symlink semantics")
    def test_state_symlink_is_rejected(self) -> None:
        target = Path(self.temporary.name) / "target.json"
        target.write_text("{}", encoding="utf-8")
        self.state_path.symlink_to(target)
        with self.assertRaisesRegex(HostSchedulerError, "symbolic link"):
            self.scheduler.snapshot()


if __name__ == "__main__":
    unittest.main(verbosity=2)
