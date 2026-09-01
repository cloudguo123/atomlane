from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import generate_test_report


def _macos_benchmark_report(commit: str = "a" * 40) -> dict[str, object]:
    task_ids = sorted(generate_test_report.LONG_BENCHMARK_TASK_IDS)
    durations = [310.0 + index for index in range(len(task_ids))]
    tasks = [
        {
            "id": task_id,
            "scenario": task_id,
            "label": task_id,
            "status": "succeeded",
            "duration_seconds": duration,
            "outer_duration_seconds": duration + 0.25,
            "duration_drift_seconds": 0.25,
            "worker_evidence_valid": True,
            "worker_evidence_errors": [],
            "iterations": 10 + index,
            "work_units": 1000 + index,
        }
        for index, (task_id, duration) in enumerate(zip(task_ids, durations, strict=True))
    ]
    wall = 314.0
    serial = sum(durations)
    saved = serial - wall
    run_id = "123-attempt-1@macos_native"
    latest = {
        "run_id": run_id,
        "generated_at": "2026-09-01T00:00:00+00:00",
        "commit": commit,
        "status": "passed",
        "minimum_task_seconds": 300.0,
        "target_task_seconds": 310.0,
        "observed_minimum_task_seconds": min(durations),
        "minimum_duration_met": True,
        "worker_evidence_complete": True,
        "worker_duration_tolerance_seconds": 6.2,
        "full_parallelism_observed": True,
        "task_count": len(tasks),
        "tasks": tasks,
        "parallel": {
            "wall_time_seconds": wall,
            "peak_concurrency": 4,
            "chosen_concurrency": 4,
        },
        "serial_equivalent": {"seconds": serial},
        "savings": {
            "seconds": saved,
            "percent": round(saved / serial * 100, 4),
            "speedup_multiplier": round(serial / wall, 4),
            "parallel_efficiency": round(serial / wall / 4, 4),
        },
        "platform": {
            "system": "Darwin",
            "execution_realm": "macos_native",
            "architecture": "arm64",
        },
        "progress_samples": [
            {
                "elapsed_seconds": 1.0,
                "running_tasks": 4,
                "completed_tasks": 0,
                "savings_eligible": True,
            }
        ],
    }
    history = [
        {
            "run_id": run_id,
            "generated_at": latest["generated_at"],
            "commit": commit,
            "status": "passed",
            "execution_realm": "macos_native",
            "wall_time_seconds": wall,
            "serial_equivalent_seconds": serial,
            "saved_seconds": saved,
            "speedup_multiplier": latest["savings"]["speedup_multiplier"],
        }
    ]
    cumulative = {
        "run_count": 1,
        "parallel_wall_seconds": wall,
        "serial_equivalent_seconds": serial,
        "saved_seconds": saved,
    }
    return {
        "schema_version": "1.1",
        "aggregation_scope": "execution_realm",
        "latest_realm": "macos_native",
        "latest": latest,
        "history": history,
        "cumulative": cumulative,
        "realms": {
            "macos_native": {
                "latest": latest,
                "history": history,
                "cumulative": cumulative,
            }
        },
    }


class ReportRenderingTests(unittest.TestCase):
    def test_bundle_check_invokes_node_without_a_platform_shell_shim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "node_modules").mkdir()
            passed = {
                "name": "Reproducible UI bundle",
                "status": "passed",
                "duration_ms": 1.0,
                "returncode": 0,
            }
            with (
                mock.patch.object(generate_test_report, "ROOT", root),
                mock.patch.object(
                    generate_test_report.shutil, "which", return_value="/tool/node"
                ),
                mock.patch.object(
                    generate_test_report, "run_command", side_effect=[dict(passed), dict(passed)]
                ) as run_command,
                mock.patch.object(generate_test_report, "sha256", return_value="digest"),
            ):
                result = generate_test_report.bundle_check()

        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            [call.args[1] for call in run_command.call_args_list],
            [
                ["/tool/node", "scripts/build_indicator.mjs"],
                ["/tool/node", "scripts/build_indicator.mjs"],
            ],
        )

    def test_public_runner_label_never_exposes_runner_name(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "RUNNER_NAME": "PRIVATE-CORPORATE-HOST",
                "RUNNER_ENVIRONMENT": "self-hosted",
            },
            clear=True,
        ):
            self.assertEqual(
                generate_test_report.public_runner_label(),
                "GitHub Actions (self-hosted)",
            )

    def test_source_provenance_allows_only_restored_evidence_inputs(self) -> None:
        with mock.patch.object(
            generate_test_report,
            "_git_dirty_paths",
            return_value={"docs/windows-preview-results.json"},
        ):
            check, clean = generate_test_report._source_provenance_check()
        self.assertTrue(clean)
        self.assertEqual(check["status"], "passed")

        with mock.patch.object(
            generate_test_report,
            "_git_dirty_paths",
            return_value={"docs/windows-preview-results.json", "scripts/mcp_server.py"},
        ):
            check, clean = generate_test_report._source_provenance_check()
        self.assertFalse(clean)
        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["unexpected_dirty_path_count"], 1)

    def test_macos_benchmark_accepts_only_complete_source_bound_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            docs = root / "docs"
            docs.mkdir()
            evidence_path = docs / "benchmark-results.json"
            evidence_path.write_text(
                json.dumps(_macos_benchmark_report()),
                encoding="utf-8",
            )
            with (
                mock.patch.object(generate_test_report, "ROOT", root),
                mock.patch.object(
                    generate_test_report,
                    "_macos_evidence_matches_source",
                    return_value=True,
                ),
            ):
                evidence = generate_test_report.load_long_benchmark()

        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["status"], "passed")
        self.assertEqual(evidence["latest"]["platform"]["execution_realm"], "macos_native")
        self.assertRegex(evidence["evidence_sha256"], r"^[0-9a-f]{64}$")

    def test_macos_benchmark_gate_rejects_incomplete_or_forged_evidence(self) -> None:
        def set_failed(report: dict[str, object]) -> None:
            report["latest"]["status"] = "failed"

        def set_wrong_realm(report: dict[str, object]) -> None:
            report["latest"]["platform"]["execution_realm"] = "linux_native"

        def remove_task(report: dict[str, object]) -> None:
            report["latest"]["tasks"].pop()

        def shorten_observed_runtime(report: dict[str, object]) -> None:
            report["latest"]["tasks"][0]["duration_seconds"] = 299.99

        def erase_worker_proof(report: dict[str, object]) -> None:
            report["latest"]["tasks"][0]["worker_evidence_valid"] = False
            report["latest"]["tasks"][0]["worker_evidence_errors"] = [
                "missing_worker_complete"
            ]

        def erase_positive_work(report: dict[str, object]) -> None:
            report["latest"]["tasks"][0]["work_units"] = 0

        def corrupt_arithmetic(report: dict[str, object]) -> None:
            report["latest"]["savings"]["seconds"] += 1

        def mix_history_realm(report: dict[str, object]) -> None:
            report["history"][0]["execution_realm"] = "windows_native"

        def corrupt_cumulative(report: dict[str, object]) -> None:
            report["cumulative"]["saved_seconds"] += 1

        mutations = {
            "legacy schema": lambda report: report.__setitem__(
                "schema_version", "1.0"
            ),
            "failed status": set_failed,
            "wrong realm": set_wrong_realm,
            "missing task": remove_task,
            "short worker runtime": shorten_observed_runtime,
            "missing worker proof": erase_worker_proof,
            "zero work": erase_positive_work,
            "bad arithmetic": corrupt_arithmetic,
            "mixed history": mix_history_realm,
            "bad cumulative": corrupt_cumulative,
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                report = json.loads(json.dumps(_macos_benchmark_report()))
                mutate(report)
                report["realms"]["macos_native"] = {
                    "latest": report["latest"],
                    "history": report["history"],
                    "cumulative": report["cumulative"],
                }
                with mock.patch.object(
                    generate_test_report,
                    "_macos_evidence_matches_source",
                    return_value=True,
                ):
                    evidence = (
                        generate_test_report.validate_macos_benchmark_evidence(report)
                    )
                self.assertFalse(evidence["available"])

        with mock.patch.object(
            generate_test_report,
            "_macos_evidence_matches_source",
            return_value=True,
        ):
            source_mismatch = generate_test_report.validate_macos_benchmark_evidence(
                _macos_benchmark_report(),
                expected_commit="b" * 40,
            )
        self.assertFalse(source_mismatch["available"])
        self.assertFalse(source_mismatch["source_match"])

    def test_macos_benchmark_artifact_cli_requires_exact_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = pathlib.Path(directory) / "benchmark-results.json"
            evidence_path.write_text(
                json.dumps(_macos_benchmark_report()),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "generate_test_report.py",
                        "--validate-macos-benchmark-evidence",
                        str(evidence_path),
                        "--expected-commit",
                        "a" * 40,
                    ],
                ),
                mock.patch.object(
                    generate_test_report,
                    "_macos_evidence_matches_source",
                    return_value=True,
                ),
                mock.patch("builtins.print") as print_mock,
            ):
                self.assertEqual(generate_test_report.main(), 0)
            validation_summary = json.loads(print_mock.call_args.args[0])
            self.assertEqual(validation_summary["status"], "passed")
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "generate_test_report.py",
                        "--validate-macos-benchmark-evidence",
                        str(evidence_path),
                        "--expected-commit",
                        "a" * 40,
                    ],
                ),
                mock.patch.object(
                    generate_test_report,
                    "_macos_evidence_matches_source",
                    return_value=False,
                ),
                mock.patch("builtins.print"),
            ):
                self.assertEqual(generate_test_report.main(), 1)
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "generate_test_report.py",
                        "--validate-macos-benchmark-evidence",
                        str(evidence_path),
                        "--expected-commit",
                        "b" * 40,
                    ],
                ),
                mock.patch.object(
                    generate_test_report,
                    "_macos_evidence_matches_source",
                    return_value=True,
                ),
                mock.patch("builtins.print"),
            ):
                self.assertEqual(generate_test_report.main(), 1)

    def test_pages_validates_artifacts_before_restoring_them(self) -> None:
        workflow = (
            generate_test_report.ROOT / ".github" / "workflows" / "pages.yml"
        ).read_text(encoding="utf-8")
        report_section, deploy_section = workflow.split("\n  deploy:\n", maxsplit=1)
        validator = workflow.index("--validate-macos-benchmark-evidence")
        copy = workflow.index('cp "$download_dir/$source_name" "$destination"')

        self.assertLess(validator, copy)
        self.assertIn("ref: refs/heads/main", report_section)
        self.assertIn("fetch-depth: 0", report_section)
        self.assertIn("$GITHUB_EVENT_PATH", report_section)
        self.assertIn("scripts/pages_evidence_selector.py", report_section)
        self.assertIn("github.ref == 'refs/heads/main'", report_section)
        self.assertNotIn("pages: write", report_section)
        self.assertNotIn("id-token: write", report_section)
        for untrusted_checkout_expression in (
            "${{ github.event.workflow_run.head_sha",
            "${{ github.event.workflow_run.head_branch",
            "${{ github.event.pull_request.head.sha",
            "${{ github.event.pull_request.head.ref",
        ):
            self.assertNotIn(untrusted_checkout_expression, workflow)
        self.assertIn('--expected-commit "$head_sha"', workflow)
        self.assertIn("--validate-windows-preview-evidence", workflow)
        self.assertIn("--validate-windows-benchmark-evidence", workflow)
        self.assertNotIn('rm -f "$destination"', workflow)
        self.assertIn('if [[ "$TRIGGER_WORKFLOW" == "$workflow_name" ]]', workflow)
        self.assertIn(
            "The triggering $workflow_name run did not provide its required "
            "source-bound artifact.",
            workflow,
        )
        self.assertIn("needs: report", deploy_section)
        self.assertIn("pages: write", deploy_section)
        self.assertIn("id-token: write", deploy_section)
        self.assertNotIn("actions/checkout@", deploy_section)
        self.assertNotIn("\n        run:", deploy_section)

    def test_python_advisor_evidence_is_non_executing_and_hash_stable(self) -> None:
        check, evidence = generate_test_report.build_python_advisor_evidence()

        self.assertEqual(check["status"], "passed")
        self.assertTrue(evidence["available"])
        self.assertFalse(evidence["execution_performed"])
        self.assertFalse(evidence["files_modified"])
        self.assertFalse(evidence["target_code_executed"])
        self.assertFalse(evidence["target_files_modified"])
        self.assertTrue(evidence["fixture_hashes_unchanged"])
        self.assertEqual(evidence["fixture_sha256_before"], evidence["fixture_sha256_after"])
        self.assertTrue(evidence["execution_marker_absent"])
        self.assertEqual(evidence["benefit_kind"], "not_estimated")

    def test_python_advisor_evidence_fails_when_execution_marker_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = pathlib.Path(directory) / "benchmarks" / "python-advisor-fixtures"
            fixture_root.mkdir(parents=True)
            source_root = (
                generate_test_report.ROOT / "benchmarks" / "python-advisor-fixtures"
            )
            for name in (
                "must_not_execute.py",
                "native.py",
                "pure_cpu.py",
                "read_io.py",
                "stateful.py",
            ):
                (fixture_root / name).write_bytes((source_root / name).read_bytes())
            (fixture_root / "must-not-exist.marker").write_text("executed\n", encoding="utf-8")

            with mock.patch.object(generate_test_report, "ROOT", pathlib.Path(directory)):
                check, evidence = generate_test_report.build_python_advisor_evidence()

        self.assertEqual(check["status"], "failed")
        self.assertFalse(evidence["execution_marker_absent"])

    def test_public_report_has_share_and_discovery_metadata(self) -> None:
        rendered = generate_test_report.render_html(
            {
                "overall": "passed",
                "generated_at": "2026-08-26T00:00:00+00:00",
                "version": "0.11.0",
                "summary": {"total": 76, "passed": 74},
                "windows_preview": {"available": False},
            }
        )
        self.assertIn('property="og:image"', rendered)
        self.assertIn("74 verified tests", rendered)
        self.assertIn('name="twitter:card" content="summary_large_image"', rendered)
        self.assertIn('rel="canonical"', rendered)
        self.assertIn('"@type":"SoftwareApplication"', rendered)
        self.assertIn('"operatingSystem":"macOS"', rendered)
        self.assertNotIn('"operatingSystem":"macOS, Windows Preview"', rendered)
        self.assertIn("Native Windows Preview evidence", rendered)
        self.assertIn("observedMinimum>=300", rendered)
        self.assertIn("Every observed task ran for at least five minutes", rendered)
        self.assertNotIn("longEnough=finite(x.minimum_task_seconds)>=300", rendered)
        self.assertIn(
            "const x=b.latest||{},c=b.cumulative||{},serial=",
            rendered,
        )
        self.assertIn("issues/new?template=first-run.yml", rendered)
        self.assertNotIn(".innerHTML", rendered)

        verified = generate_test_report.render_html(
            {
                "overall": "passed",
                "generated_at": "2026-08-26T00:00:00+00:00",
                "version": "0.12.0",
                "summary": {"total": 76, "passed": 76},
                "windows_preview": {"available": True},
            }
        )
        self.assertIn('"operatingSystem":"macOS, Windows Preview"', verified)
        self.assertIn("source-bound native Windows Preview evidence", verified)

    def test_public_report_renders_python_advisor_integrity_evidence(self) -> None:
        rendered = generate_test_report.render_html(
            {
                "overall": "passed",
                "generated_at": "2026-08-26T00:00:00+00:00",
                "version": "0.11.0",
                "summary": {"total": 1},
            }
        )

        self.assertIn("fixture SHA-256 unchanged", rendered)
        self.assertIn("execution marker absent", rendered)

    def test_windows_evidence_requires_a_native_windows_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            docs = root / "docs"
            docs.mkdir()
            evidence_path = docs / "windows-preview-results.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "overall": "passed",
                        "version": "0.12.0",
                        "generated_at": "2026-09-01T00:00:00+00:00",
                        "source": {"commit": "a" * 40},
                        "environment": {
                            "os": "Windows 2025",
                            "architecture": "AMD64",
                            "python": "3.13.7",
                            "runner": "GitHub Actions 1",
                        },
                        "summary": {
                            "total": 126,
                            "passed": 126,
                            "failed": 0,
                            "errors": 0,
                            "skipped": 0,
                        },
                        "domains": [
                            {
                                "module": "test_windows_runtime",
                                "total": 11,
                                "passed": 11,
                                "failed": 0,
                                "errors": 0,
                                "skipped": 0,
                            }
                        ],
                        "checks": [{"status": "passed"}, {"status": "passed"}],
                        "tests": [
                            {
                                "class": "WindowsNativeRuntimeTests",
                                "name": name,
                                "status": "passed",
                            }
                            for name in generate_test_report.WINDOWS_CRITICAL_TESTS
                        ]
                        + [
                            {
                                "class": "WindowsNativeRuntimeTests",
                                "name": "test_additional_native_gate_is_allowed",
                                "status": "passed",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(generate_test_report, "ROOT", root),
                mock.patch.object(
                    generate_test_report,
                    "_windows_evidence_matches_source",
                    return_value=True,
                ),
            ):
                (root / ".codex-plugin").mkdir()
                (root / ".codex-plugin" / "plugin.json").write_text(
                    '{"version":"0.12.0"}\n', encoding="utf-8"
                )
                evidence = generate_test_report.load_windows_preview_evidence()
                stale = generate_test_report.load_windows_preview_evidence(
                    evidence_path,
                    expected_commit="b" * 40,
                )

        self.assertTrue(evidence["available"])
        self.assertFalse(stale["available"])
        self.assertFalse(stale["source_match"])
        self.assertEqual(
            evidence["native_runtime"]["passed"],
            len(generate_test_report.WINDOWS_CRITICAL_TESTS) + 1,
        )
        self.assertEqual(evidence["native_runtime"]["skipped"], 0)
        self.assertEqual(evidence["release_checks"], {"passed": 2, "total": 2})

    def test_windows_benchmark_requires_observed_five_minute_native_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            docs = root / "docs"
            docs.mkdir()
            tasks = [
                {
                    "id": f"task-{index}",
                    "label": f"Task {index}",
                    "status": "succeeded",
                    "duration_seconds": 310.0 + index,
                }
                for index in range(4)
            ]
            serial = sum(task["duration_seconds"] for task in tasks)
            wall = 314.0
            evidence_path = docs / "windows-benchmark-results.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.1",
                        "latest": {
                            "run_id": "123-windows_native",
                            "commit": "a" * 40,
                            "status": "passed",
                            "minimum_task_seconds": 300.0,
                            "target_task_seconds": 310.0,
                            "minimum_duration_met": True,
                            "observed_minimum_task_seconds": 310.0,
                            "task_count": 4,
                            "tasks": tasks,
                            "parallel": {
                                "wall_time_seconds": wall,
                                "peak_concurrency": 4,
                            },
                            "serial_equivalent": {"seconds": serial},
                            "savings": {
                                "seconds": serial - wall,
                                "speedup_multiplier": serial / wall,
                            },
                            "platform": {
                                "system": "Windows",
                                "execution_realm": "windows_native",
                                "architecture": "AMD64",
                            },
                            "progress_samples": [
                                {
                                    "elapsed_seconds": 1.0,
                                    "running_tasks": 4,
                                    "completed_tasks": 0,
                                }
                            ],
                        },
                        "aggregation_scope": "execution_realm",
                        "latest_realm": "windows_native",
                        "history": [
                            {
                                "run_id": "123-windows_native",
                                "commit": "a" * 40,
                                "status": "passed",
                                "execution_realm": "windows_native",
                                "wall_time_seconds": wall,
                                "serial_equivalent_seconds": serial,
                                "saved_seconds": serial - wall,
                                "speedup_multiplier": serial / wall,
                            }
                        ],
                        "cumulative": {
                            "run_count": 1,
                            "parallel_wall_seconds": wall,
                            "serial_equivalent_seconds": serial,
                            "saved_seconds": serial - wall,
                        },
                    }
                ),
                encoding="utf-8",
            )
            report = json.loads(evidence_path.read_text(encoding="utf-8"))
            report["realms"] = {
                "windows_native": {
                    "latest": report["latest"],
                    "history": report["history"],
                    "cumulative": report["cumulative"],
                }
            }
            evidence_path.write_text(json.dumps(report), encoding="utf-8")
            with (
                mock.patch.object(generate_test_report, "ROOT", root),
                mock.patch.object(
                    generate_test_report,
                    "_windows_evidence_matches_source",
                    return_value=True,
                ),
            ):
                evidence = generate_test_report.load_windows_benchmark_evidence()
                stale = generate_test_report.load_windows_benchmark_evidence(
                    evidence_path,
                    expected_commit="b" * 40,
                )
            self.assertTrue(evidence["available"])
            self.assertFalse(stale["available"])
            self.assertFalse(stale["source_match"])
            self.assertEqual(
                evidence["latest"]["observed_minimum_task_seconds"], 310.0
            )

            report = json.loads(evidence_path.read_text(encoding="utf-8"))
            report["history"][0]["execution_realm"] = "macos_native"
            report["realms"]["windows_native"]["history"][0][
                "execution_realm"
            ] = "macos_native"
            evidence_path.write_text(json.dumps(report), encoding="utf-8")
            with (
                mock.patch.object(generate_test_report, "ROOT", root),
                mock.patch.object(
                    generate_test_report,
                    "_windows_evidence_matches_source",
                    return_value=True,
                ),
            ):
                mixed_realm = generate_test_report.load_windows_benchmark_evidence()
            self.assertFalse(mixed_realm["available"])
            self.assertFalse(mixed_realm["history_is_windows_only"])

            report["history"][0]["execution_realm"] = "windows_native"
            report["realms"]["windows_native"]["history"][0][
                "execution_realm"
            ] = "windows_native"
            report["cumulative"]["saved_seconds"] += 1
            report["realms"]["windows_native"]["cumulative"]["saved_seconds"] += 1
            evidence_path.write_text(json.dumps(report), encoding="utf-8")
            with (
                mock.patch.object(generate_test_report, "ROOT", root),
                mock.patch.object(
                    generate_test_report,
                    "_windows_evidence_matches_source",
                    return_value=True,
                ),
            ):
                bad_cumulative = generate_test_report.load_windows_benchmark_evidence()
            self.assertFalse(bad_cumulative["available"])
            self.assertFalse(bad_cumulative["cumulative_matches_history"])

            report["cumulative"]["saved_seconds"] -= 1
            report["realms"]["windows_native"]["cumulative"]["saved_seconds"] -= 1
            report["latest"]["tasks"][0]["duration_seconds"] = 299.99
            report["latest"]["observed_minimum_task_seconds"] = 299.99
            report["latest"]["serial_equivalent"]["seconds"] -= 10.01
            report["latest"]["savings"]["seconds"] -= 10.01
            evidence_path.write_text(json.dumps(report), encoding="utf-8")
            with (
                mock.patch.object(generate_test_report, "ROOT", root),
                mock.patch.object(
                    generate_test_report,
                    "_windows_evidence_matches_source",
                    return_value=True,
                ),
            ):
                rejected = generate_test_report.load_windows_benchmark_evidence()
            self.assertFalse(rejected["available"])
            self.assertEqual(rejected["status"], "native_benchmark_gate_failed")


if __name__ == "__main__":
    unittest.main()
