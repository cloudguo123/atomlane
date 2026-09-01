from __future__ import annotations

import json
import unittest
from unittest import mock

import long_benchmark


class LongBenchmarkTests(unittest.TestCase):
    @staticmethod
    def _result(
        scenario: str,
        *,
        worker_seconds: float = 310.0,
        outer_seconds: float = 310.25,
        status: str = "succeeded",
        worker_overrides: dict[str, object] | None = None,
    ) -> dict[str, object]:
        worker: dict[str, object] = {
            "event": "worker_complete",
            "scenario": scenario,
            "label": long_benchmark.SCENARIOS[scenario],
            "elapsed_seconds": worker_seconds,
            "iterations": 10,
            "work_units": 100,
        }
        worker.update(worker_overrides or {})
        return {
            "id": scenario,
            "status": status,
            "duration_seconds": outer_seconds,
            "stdout": "progress\n" + json.dumps(worker),
        }

    @staticmethod
    def _latest(
        run_id: str,
        realm: str,
        *,
        status: str = "passed",
        wall_seconds: float = 310.0,
        saved_seconds: float = 930.0,
    ) -> dict[str, object]:
        system = {
            "macos_native": "Darwin",
            "windows_native": "Windows",
        }.get(realm, "Linux")
        return {
            "run_id": run_id,
            "generated_at": "2026-08-26T00:00:00+00:00",
            "commit": "a" * 40,
            "status": status,
            "platform": {
                "system": system,
                "release": "test",
                "architecture": "test",
                "execution_realm": realm,
            },
            "runner": {"name": "test-runner", "os": system, "architecture": "test"},
            "run_url": f"https://example.invalid/actions/runs/{run_id}",
            "parallel": {"wall_time_seconds": wall_seconds},
            "serial_equivalent": {"seconds": wall_seconds + saved_seconds},
            "savings": {
                "seconds": saved_seconds,
                "speedup_multiplier": (wall_seconds + saved_seconds) / wall_seconds,
            },
        }

    def test_platform_evidence_records_runner_and_github_run_url(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "GITHUB_SERVER_URL": "https://github.example",
                "GITHUB_REPOSITORY": "owner/project",
                "GITHUB_RUN_ID": "12345",
                "RUNNER_NAME": "private-runner-7",
                "RUNNER_ENVIRONMENT": "github-hosted",
                "RUNNER_OS": "Windows",
                "RUNNER_ARCH": "X64",
            },
            clear=True,
        ):
            evidence = long_benchmark._platform_evidence()

        self.assertEqual(evidence["runner"]["name"], "github-actions-github-hosted")
        self.assertEqual(evidence["runner"]["environment"], "github-hosted")
        self.assertEqual(evidence["runner"]["os"], "Windows")
        self.assertEqual(evidence["runner"]["architecture"], "X64")
        self.assertEqual(
            evidence["run_url"],
            "https://github.example/owner/project/actions/runs/12345",
        )
        self.assertEqual(
            evidence["platform"]["runner_name"], "github-actions-github-hosted"
        )
        self.assertEqual(evidence["platform"]["run_url"], evidence["run_url"])

    def test_summary_records_observed_runtime_and_windows_provenance(self) -> None:
        results = [self._result(scenario) for scenario in long_benchmark.SCENARIOS]
        execution = {
            "results": results,
            "summary": {
                "elapsed_seconds": 311.0,
                "peak_concurrency": 4,
                "chosen_concurrency": 4,
            },
            "resource_plan": {
                "profile": "io",
                "responsiveness": "throughput",
                "machine": {"logical_cpus": 8, "physical_cpus": 4, "machine": "AMD64"},
            },
        }
        evidence = {
            "platform": {
                "system": "Windows",
                "release": "2025",
                "architecture": "amd64",
                "execution_realm": "windows_native",
            },
            "runner": {
                "name": "GitHub Actions 1000001",
                "os": "Windows",
                "architecture": "X64",
            },
            "run_url": "https://github.com/example/project/actions/runs/123",
        }
        summary = long_benchmark.summarize_run(
            execution,
            [],
            minimum_task_seconds=300.0,
            target_task_seconds=310.0,
            commit="abc",
            run_id="run-1",
            environment_evidence=evidence,
        )
        self.assertEqual(summary["status"], "passed")
        self.assertTrue(summary["full_parallelism_observed"])
        self.assertEqual(summary["run_id"], "run-1@windows_native")
        self.assertEqual(summary["observed_minimum_task_seconds"], 310.0)
        self.assertEqual(summary["serial_equivalent"]["seconds"], 1240.0)
        self.assertEqual(summary["savings"]["seconds"], 929.0)
        self.assertAlmostEqual(summary["savings"]["speedup_multiplier"], 3.9871)
        self.assertTrue(summary["worker_evidence_complete"])
        self.assertTrue(all(task["worker_evidence_valid"] for task in summary["tasks"]))
        self.assertEqual(summary["platform"], evidence["platform"])
        self.assertEqual(summary["runner"], evidence["runner"])
        self.assertEqual(summary["run_url"], evidence["run_url"])

    def test_summary_fails_when_one_of_four_tasks_is_missing(self) -> None:
        results = [
            self._result(scenario) for scenario in list(long_benchmark.SCENARIOS)[:-1]
        ]
        execution = {
            "results": results,
            "summary": {
                "elapsed_seconds": 311.0,
                "peak_concurrency": 3,
                "chosen_concurrency": 4,
            },
            "resource_plan": {
                "profile": "io",
                "responsiveness": "throughput",
                "machine": {"logical_cpus": 8, "physical_cpus": 4, "machine": "AMD64"},
            },
        }
        summary = long_benchmark.summarize_run(
            execution,
            [],
            minimum_task_seconds=300.0,
            target_task_seconds=310.0,
            commit="abc",
            run_id="run-incomplete",
            environment_evidence={
                "platform": {
                    "system": "Windows",
                    "release": "2025",
                    "architecture": "amd64",
                    "execution_realm": "windows_native",
                },
                "runner": {"name": "test", "os": "Windows", "architecture": "X64"},
                "run_url": None,
            },
        )
        self.assertEqual(summary["status"], "failed")
        self.assertFalse(summary["minimum_duration_met"])
        self.assertFalse(summary["full_parallelism_observed"])

    def test_worker_completion_is_required_and_controls_duration_gate(self) -> None:
        cases = {
            "missing": {"stdout": "", "duration_seconds": 310.0},
            "short-worker": self._result(
                "artifact-hash", worker_seconds=20.0, outer_seconds=310.0
            ),
            "wrong-scenario": self._result(
                "artifact-hash", worker_overrides={"scenario": "planner-json"}
            ),
            "zero-work": self._result(
                "artifact-hash", worker_overrides={"iterations": 0, "work_units": 0}
            ),
        }
        for label, invalid in cases.items():
            with self.subTest(label=label):
                results = [self._result(scenario) for scenario in long_benchmark.SCENARIOS]
                results[0] = {
                    "id": "artifact-hash",
                    "status": "succeeded",
                    **invalid,
                }
                execution = {
                    "results": results,
                    "summary": {
                        "elapsed_seconds": 311.0,
                        "peak_concurrency": 4,
                        "chosen_concurrency": 4,
                    },
                    "resource_plan": {
                        "profile": "io",
                        "responsiveness": "throughput",
                        "machine": {
                            "logical_cpus": 8,
                            "physical_cpus": 4,
                            "machine": "AMD64",
                        },
                    },
                }
                summary = long_benchmark.summarize_run(
                    execution,
                    [],
                    minimum_task_seconds=300.0,
                    target_task_seconds=310.0,
                    commit="abc",
                    run_id=f"invalid-{label}",
                    environment_evidence={
                        "platform": {
                            "system": "Windows",
                            "release": "2025",
                            "architecture": "amd64",
                            "execution_realm": "windows_native",
                        },
                        "runner": {
                            "name": "test",
                            "os": "Windows",
                            "architecture": "X64",
                        },
                        "run_url": None,
                    },
                )
                self.assertEqual(summary["status"], "failed")
                self.assertFalse(summary["worker_evidence_complete"])
                self.assertFalse(summary["minimum_duration_met"])

    def test_history_and_cumulative_are_separate_per_realm(self) -> None:
        macos = self._latest("run-1", "macos_native", saved_seconds=928.0)
        windows = self._latest("run-2", "windows_native", saved_seconds=930.0)
        report = long_benchmark.merge_history({}, macos)
        report = long_benchmark.merge_history(report, windows)

        self.assertEqual(report["schema_version"], "1.1")
        self.assertEqual(report["aggregation_scope"], "execution_realm")
        self.assertEqual(report["latest_realm"], "windows_native")
        self.assertEqual(report["cumulative"]["run_count"], 1)
        self.assertEqual(report["cumulative"]["saved_seconds"], 930.0)
        self.assertEqual(report["realms"]["macos_native"]["cumulative"]["run_count"], 1)
        self.assertEqual(
            report["realms"]["macos_native"]["cumulative"]["saved_seconds"], 928.0
        )
        self.assertEqual(report["realms"]["windows_native"]["cumulative"]["run_count"], 1)
        self.assertTrue(report["history"][0]["run_id"].endswith("@windows_native"))

        deduplicated = long_benchmark.merge_history(report, windows)
        self.assertEqual(deduplicated["cumulative"]["run_count"], 1)

    def test_failed_run_is_latest_but_never_enters_cumulative(self) -> None:
        passed = self._latest("run-1", "windows_native")
        report = long_benchmark.merge_history({}, passed)
        failed = self._latest(
            "run-2",
            "windows_native",
            status="failed",
            wall_seconds=100.0,
            saved_seconds=300.0,
        )
        report = long_benchmark.merge_history(report, failed)

        self.assertEqual(report["latest"]["status"], "failed")
        self.assertEqual(report["realms"]["windows_native"]["latest"]["status"], "failed")
        self.assertEqual(report["cumulative"]["run_count"], 1)
        self.assertEqual(report["cumulative"]["saved_seconds"], 930.0)
        self.assertEqual([row["status"] for row in report["history"]], ["passed"])

    def test_schema_one_history_migrates_without_cross_realm_contamination(self) -> None:
        legacy = {
            "schema_version": "1.0",
            "latest": {
                "run_id": "legacy-1",
                "resource": {"machine": "Apple M1"},
            },
            "history": [
                {
                    "run_id": "legacy-1",
                    "generated_at": "2026-08-19T00:00:00+00:00",
                    "commit": "b" * 40,
                    "status": "passed",
                    "wall_time_seconds": 312.0,
                    "serial_equivalent_seconds": 1240.0,
                    "saved_seconds": 928.0,
                    "speedup_multiplier": 3.9744,
                }
            ],
        }
        windows = self._latest("run-2", "windows_native")
        report = long_benchmark.merge_history(legacy, windows)

        self.assertEqual(report["realms"]["macos_native"]["cumulative"]["run_count"], 1)
        self.assertEqual(report["realms"]["windows_native"]["cumulative"]["run_count"], 1)
        self.assertEqual(report["cumulative"]["saved_seconds"], 930.0)


if __name__ == "__main__":
    unittest.main()
