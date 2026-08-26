from __future__ import annotations

import unittest

import long_benchmark


class LongBenchmarkTests(unittest.TestCase):
    def test_summary_uses_observed_task_runtimes(self) -> None:
        results = [
            {
                "id": scenario,
                "status": "succeeded",
                "duration_seconds": 310.0,
                "stdout": "",
            }
            for scenario in long_benchmark.SCENARIOS
        ]
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
                "machine": {"logical_cpus": 8, "physical_cpus": 4, "machine": "arm64"},
            },
        }
        summary = long_benchmark.summarize_run(
            execution,
            [],
            minimum_task_seconds=300.0,
            target_task_seconds=310.0,
            commit="abc",
            run_id="run-1",
        )
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(summary["serial_equivalent"]["seconds"], 1240.0)
        self.assertEqual(summary["savings"]["seconds"], 929.0)
        self.assertAlmostEqual(summary["savings"]["speedup_multiplier"], 3.9871)

    def test_history_accumulates_savings_without_duplicate_run(self) -> None:
        latest = {
            "run_id": "run-2",
            "generated_at": "2026-08-26T00:00:00+00:00",
            "commit": "def",
            "status": "passed",
            "parallel": {"wall_time_seconds": 310.0},
            "serial_equivalent": {"seconds": 1240.0},
            "savings": {"seconds": 930.0, "speedup_multiplier": 4.0},
        }
        previous = {
            "history": [
                {
                    "run_id": "run-1",
                    "generated_at": "2026-08-19T00:00:00+00:00",
                    "commit": "abc",
                    "status": "passed",
                    "wall_time_seconds": 312.0,
                    "serial_equivalent_seconds": 1240.0,
                    "saved_seconds": 928.0,
                    "speedup_multiplier": 3.9744,
                }
            ]
        }
        merged = long_benchmark.merge_history(previous, latest)
        self.assertEqual(merged["cumulative"]["run_count"], 2)
        self.assertEqual(merged["cumulative"]["saved_seconds"], 1858.0)
        deduplicated = long_benchmark.merge_history(merged, latest)
        self.assertEqual(deduplicated["cumulative"]["run_count"], 2)


if __name__ == "__main__":
    unittest.main()
