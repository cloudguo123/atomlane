from __future__ import annotations

import copy
import json
import pathlib
import unittest

try:
    import jsonschema
except ImportError:  # pragma: no cover - the release report installs jsonschema
    jsonschema = None


ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipIf(jsonschema is None, "jsonschema is not installed")
class ProjectBenchmarkSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            (ROOT / "benchmarks" / "project-result.schema.json").read_text(
                encoding="utf-8"
            )
        )
        jsonschema.Draft202012Validator.check_schema(cls.schema)
        cls.validator = jsonschema.Draft202012Validator(
            cls.schema,
            format_checker=jsonschema.FormatChecker(),
        )

    @staticmethod
    def macos_result() -> dict[str, object]:
        return {
            "schema_version": "1.2",
            "id": "macos-project-result",
            "plugin_version": "0.12.0",
            "commit": "a" * 40,
            "category": "native-build-test",
            "environment": {
                "platform": "macos",
                "os_version": "macOS 15.6",
                "architecture": "arm64",
                "execution_realm": "macos_native",
                "machine": "MacBook Pro",
                "logical_cpus": 10,
                "power_source": "ac",
                "low_power_mode": False,
                "process_tree_backend": "posix_session",
                "terminal_mode": "pipes",
            },
            "condition": "warm",
            "repetitions": 3,
            "comparison": {
                "kind": "observed_sum",
                "seconds": 120.0,
                "method": "sum of observed independent task runtimes",
            },
            "parallel": {
                "wall_seconds_p50": 40.0,
                "wall_seconds_p90": 42.0,
                "speedup_p50": 3.0,
                "peak_concurrency": 4,
                "time_saved_seconds_p50": 80.0,
            },
            "correctness": {
                "status": "passed",
                "method": "output hashes and project tests matched",
            },
            "evidence_url": "https://example.invalid/evidence/macos",
        }

    @classmethod
    def windows_result(cls) -> dict[str, object]:
        result = copy.deepcopy(cls.macos_result())
        result["id"] = "windows-project-result"
        result["environment"] = {
            "platform": "windows",
            "os_version": "Windows Server 2025",
            "architecture": "amd64",
            "execution_realm": "windows_native",
            "machine": "GitHub Actions runner",
            "logical_cpus": 4,
            "power_source": "unknown",
            "process_tree_backend": "windows_job_object",
            "terminal_mode": "pipes",
        }
        result["windows_evidence"] = {
            "supervisor_assignment": "supervisor_pid_then_launch_record",
            "target_creation_atomic": False,
            "requested_job_limits": {
                "kill_on_close": True,
                "cpu_rate_percent": 50.0,
                "memory_limit_mib": 256,
                "job_active_process_limit": 4,
            },
            "queried_job_limits": {
                "kill_on_close": True,
                "cpu_rate_percent": 50.0,
                "memory_limit_mib": 256,
                "job_active_process_limit": 4,
                "verified": True,
            },
            "job_limits_include_supervisor": True,
            "containment_scope": "client_and_inherited_windows_descendants_only",
            "broker_boundary": {
                "kind": "none",
                "client": None,
                "target_realm": None,
                "brokered_work_contained": False,
                "job_limits_apply_to_brokered_work": False,
            },
            "terminal": {
                "mode": "pipes",
                "stdout_stderr_combined": False,
                "captured_output_delivery": "completion_result",
            },
        }
        result["evidence_url"] = "https://example.invalid/evidence/windows"
        return result

    @classmethod
    def wsl_result(cls) -> dict[str, object]:
        result = copy.deepcopy(cls.macos_result())
        result["id"] = "wsl-project-result"
        result["environment"] = {
            "platform": "linux",
            "os_version": "Ubuntu 24.04 on WSL 2",
            "architecture": "x86_64",
            "execution_realm": "wsl_linux",
            "machine": "WSL distribution",
            "logical_cpus": 8,
            "power_source": "unknown",
            "process_tree_backend": "posix_session",
            "terminal_mode": "pipes",
        }
        result["evidence_url"] = "https://example.invalid/evidence/wsl"
        return result

    def assert_rejected(self, result: dict[str, object]) -> None:
        self.assertFalse(self.validator.is_valid(result))

    def test_valid_macos_windows_and_wsl_results(self) -> None:
        self.validator.validate(self.macos_result())
        self.validator.validate(self.windows_result())
        self.validator.validate(self.wsl_result())

        conpty = self.windows_result()
        conpty["environment"]["terminal_mode"] = "conpty"
        conpty["windows_evidence"]["terminal"]["mode"] = "conpty"
        conpty["windows_evidence"]["terminal"]["stdout_stderr_combined"] = True
        conpty["windows_evidence"]["requested_job_limits"][
            "job_active_process_limit"
        ] = None
        conpty["windows_evidence"]["queried_job_limits"][
            "job_active_process_limit"
        ] = None
        self.validator.validate(conpty)

        impossible_conpty_limit = copy.deepcopy(conpty)
        impossible_conpty_limit["windows_evidence"]["requested_job_limits"][
            "job_active_process_limit"
        ] = 4
        self.assert_rejected(impossible_conpty_limit)

        for evidence_key in ("requested_job_limits", "queried_job_limits"):
            with self.subTest(evidence_key=evidence_key):
                over_limit = self.windows_result()
                over_limit["windows_evidence"][evidence_key][
                    "job_active_process_limit"
                ] = 4097
                self.assert_rejected(over_limit)

                over_memory = self.windows_result()
                over_memory["windows_evidence"][evidence_key][
                    "memory_limit_mib"
                ] = 1_048_577
                self.assert_rejected(over_memory)

    def test_existing_external_results_remain_valid(self) -> None:
        collection = json.loads(
            (ROOT / "benchmarks" / "external-results.json").read_text(
                encoding="utf-8"
            )
        )
        for result in collection.get("results", []):
            self.validator.validate(result)

    def test_version_commit_and_repetition_floor_are_enforced(self) -> None:
        old_version = self.macos_result()
        old_version["schema_version"] = "1.0"
        self.assert_rejected(old_version)

        short_commit = self.macos_result()
        short_commit["commit"] = "abc1234"
        self.assert_rejected(short_commit)

        too_few_runs = self.macos_result()
        too_few_runs["repetitions"] = 2
        self.assert_rejected(too_few_runs)

    def test_platform_realm_and_backend_combinations_fail_closed(self) -> None:
        macos_windows_realm = self.macos_result()
        macos_windows_realm["environment"]["execution_realm"] = "windows_native"
        self.assert_rejected(macos_windows_realm)

        windows_posix_backend = self.windows_result()
        windows_posix_backend["environment"]["process_tree_backend"] = "posix_session"
        self.assert_rejected(windows_posix_backend)

        windows_posix_pty = self.windows_result()
        windows_posix_pty["environment"]["terminal_mode"] = "posix_pty"
        self.assert_rejected(windows_posix_pty)

        linux_macos_realm = self.wsl_result()
        linux_macos_realm["environment"]["execution_realm"] = "macos_native"
        self.assert_rejected(linux_macos_realm)

        linux_conpty = self.wsl_result()
        linux_conpty["environment"]["terminal_mode"] = "conpty"
        self.assert_rejected(linux_conpty)

    def test_windows_evidence_is_required_and_terminal_semantics_match(self) -> None:
        missing_evidence = self.windows_result()
        missing_evidence.pop("windows_evidence")
        self.assert_rejected(missing_evidence)

        evidence_on_macos = self.macos_result()
        evidence_on_macos["windows_evidence"] = self.windows_result()["windows_evidence"]
        self.assert_rejected(evidence_on_macos)

        mismatched_mode = self.windows_result()
        mismatched_mode["windows_evidence"]["terminal"]["mode"] = "conpty"
        mismatched_mode["windows_evidence"]["terminal"]["stdout_stderr_combined"] = True
        self.assert_rejected(mismatched_mode)

        conpty_not_combined = self.windows_result()
        conpty_not_combined["environment"]["terminal_mode"] = "conpty"
        conpty_not_combined["windows_evidence"]["terminal"]["mode"] = "conpty"
        self.assert_rejected(conpty_not_combined)

    def test_windows_memory_floor_and_broker_boundary_are_enforced(self) -> None:
        small_memory = self.windows_result()
        small_memory["windows_evidence"]["requested_job_limits"][
            "memory_limit_mib"
        ] = 64
        self.assert_rejected(small_memory)

        incomplete_broker = self.windows_result()
        incomplete_broker["windows_evidence"]["broker_boundary"] = {
            "kind": "docker",
            "client": None,
            "target_realm": None,
            "brokered_work_contained": False,
            "job_limits_apply_to_brokered_work": False,
        }
        self.assert_rejected(incomplete_broker)


if __name__ == "__main__":
    unittest.main()
