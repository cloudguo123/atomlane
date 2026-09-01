#!/usr/bin/env python3
"""Run the public verification suite and render a self-contained test dashboard."""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import math
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import time
import unittest
from collections import Counter
from datetime import datetime, timezone
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
REPORT_EVIDENCE_INPUTS = {
    "docs/benchmark-results.json",
    "docs/windows-benchmark-results.json",
    "docs/windows-preview-results.json",
}

DOMAIN_META = {
    "test_atom_engine": {
        "label": "Atomic compiler & scheduler",
        "description": "Atom lowering, conflict safety, resource admission, hashes, and snapshots",
        "accent": "#65e6b4",
    },
    "test_atom_frontends": {
        "label": "Static workload frontends",
        "description": "Shell, package scripts, Make, Compose, and inferred dataflow",
        "accent": "#80b7ff",
    },
    "test_mcp_runtime": {
        "label": "MCP runtime & live execution",
        "description": "Failure propagation, output bounds, timeout semantics, and live progress",
        "accent": "#d7a6ff",
    },
    "test_platform_adapter": {
        "label": "Cross-platform contract",
        "description": "Execution realms, immutable host binding, portable statistics, and resource probes",
        "accent": "#79f2c0",
    },
    "test_project_benchmark_schema": {
        "label": "External evidence schema",
        "description": "Platform/realm consistency, repeatability, and Windows containment evidence",
        "accent": "#f2ad79",
    },
    "test_windows_runtime": {
        "label": "Windows runtime contracts",
        "description": (
            "Portable parser and API contracts; native Job Object and ConPTY proof "
            "is reported separately"
        ),
        "accent": "#5ba7ff",
    },
    "test_portable_plugin": {
        "label": "Plugin packaging & portability",
        "description": (
            "Manifest identity, clean package contents, launch paths, and release "
            "version consistency"
        ),
        "accent": "#b7f079",
    },
    "test_report_rendering": {
        "label": "Public report integrity",
        "description": "Source-bound evidence, privacy-safe labels, and honest platform claims",
        "accent": "#e879f2",
    },
    "test_ui_bundle_security": {
        "label": "Indicator bundle security",
        "description": "Reproducible browser bundle without dynamic code evaluation",
        "accent": "#f2799b",
    },
    "test_growth_assets": {
        "label": "Growth assets & evidence sharing",
        "description": "Exact benchmark metrics, share cards, and honest comparison labels",
        "accent": "#f4cf78",
    },
    "test_collect_github_metrics": {
        "label": "Privacy-safe growth metrics",
        "description": "Aggregate repository signals and resilient snapshot storage",
        "accent": "#ff9fc6",
    },
    "test_long_benchmark": {
        "label": "Long-horizon benchmark evidence",
        "description": "Observed task runtimes, duration gates, and cumulative savings history",
        "accent": "#9ae7ff",
    },
    "test_python_parallel_advisor": {
        "label": "Python parallel refactor safety",
        "description": "Non-executing AST analysis, effect gates, spawn semantics, and hash-bound previews",
        "accent": "#ffb86b",
    },
}

WINDOWS_CRITICAL_TESTS = {
    "test_job_object_executes_utf8_and_applies_tree_limits",
    "test_runner_resolves_bare_executable_from_target_path",
    "test_native_environment_block_is_exactly_double_nul_terminated",
    "test_pipe_mode_drains_large_separate_streams",
    "test_timeout_kills_grandchild_before_delayed_marker",
    "test_supervisor_ignores_task_python_startup_injection",
    "test_completed_parent_cannot_leave_a_pipe_holding_descendant",
    "test_conpty_captures_combined_vt_output",
    "test_conpty_drains_output_larger_than_pipe_capacity",
    "test_blocked_synchronous_pipe_read_is_cancelled_and_joined",
    "test_progress_arrives_before_parallel_tasks_finish",
    "test_mcp_pipe_is_utf8_and_uses_the_installed_server_entrypoint",
    "test_windows_argv_and_environment_hazards_fail_closed",
    "test_powershell_file_is_one_snapshotted_atom",
}

LONG_BENCHMARK_TASK_IDS = frozenset(
    {
        "artifact-hash",
        "planner-json",
        "isolated-io",
        "scheduler-sim",
    }
)
GIT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


class TimedResult(unittest.TextTestResult):
    """Collect compact, public-safe per-test outcomes and durations."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.started: dict[str, float] = {}
        self.records: list[dict[str, Any]] = []

    def startTest(self, test: unittest.case.TestCase) -> None:
        self.started[test.id()] = time.perf_counter()
        super().startTest(test)

    def _record(self, test: unittest.case.TestCase, status: str) -> None:
        test_id = test.id()
        elapsed = time.perf_counter() - self.started.pop(test_id, time.perf_counter())
        parts = test_id.split(".")
        module = parts[-3] if len(parts) >= 3 else "unknown"
        class_name = parts[-2] if len(parts) >= 2 else "Unknown"
        name = parts[-1]
        self.records.append(
            {
                "id": test_id,
                "module": module,
                "domain": DOMAIN_META.get(module, {}).get("label", module),
                "class": class_name,
                "name": name,
                "title": name.removeprefix("test_").replace("_", " "),
                "status": status,
                "duration_ms": round(elapsed * 1000, 2),
            }
        )

    def addSuccess(self, test: unittest.case.TestCase) -> None:
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test: unittest.case.TestCase, err: Any) -> None:
        super().addFailure(test, err)
        self._record(test, "failed")

    def addError(self, test: unittest.case.TestCase, err: Any) -> None:
        super().addError(test, err)
        self._record(test, "error")

    def addSkip(self, test: unittest.case.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self._record(test, "skipped")

    def addExpectedFailure(self, test: unittest.case.TestCase, err: Any) -> None:
        super().addExpectedFailure(test, err)
        self._record(test, "expected_failure")

    def addUnexpectedSuccess(self, test: unittest.case.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        self._record(test, "unexpected_success")


def run_command(name: str, argv: list[str]) -> dict[str, Any]:
    started = time.perf_counter()
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return {
        "name": name,
        "status": "passed" if completed.returncode == 0 else "failed",
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "returncode": completed.returncode,
    }


def run_regression_tests() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sys.path.insert(0, str(SCRIPTS))
    started = time.perf_counter()
    suite = unittest.defaultTestLoader.discover(str(SCRIPTS), pattern="test*.py")
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=TimedResult)
    result = runner.run(suite)
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    status = "passed" if result.wasSuccessful() else "failed"
    check = {
        "name": "Regression suite",
        "status": status,
        "duration_ms": duration_ms,
        "returncode": 0 if result.wasSuccessful() else 1,
    }
    return check, sorted(result.records, key=lambda item: (item["module"], item["class"], item["name"]))


def validate_metadata() -> dict[str, Any]:
    started = time.perf_counter()
    paths = [
        ROOT / ".codex-plugin" / "plugin.json",
        ROOT / ".agents" / "plugins" / "marketplace.json",
        ROOT / "catalog" / "scenarios.json",
        ROOT / "package.json",
        ROOT / "package-lock.json",
        ROOT / "benchmarks" / "project-result.schema.json",
        ROOT / "benchmarks" / "external-results.json",
    ]
    try:
        for path in paths:
            json.loads(path.read_text(encoding="utf-8"))
        status = "passed"
        returncode = 0
    except (OSError, json.JSONDecodeError):
        status = "failed"
        returncode = 1
    return {
        "name": "Manifest & catalog integrity",
        "status": status,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "returncode": returncode,
    }


def validate_external_benchmarks() -> dict[str, Any]:
    started = time.perf_counter()
    try:
        import jsonschema
    except ImportError:
        return {
            "name": "External benchmark JSON Schema",
            "status": "skipped",
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "returncode": 0,
        }
    try:
        schema = json.loads(
            (ROOT / "benchmarks" / "project-result.schema.json").read_text(
                encoding="utf-8"
            )
        )
        external = json.loads(
            (ROOT / "benchmarks" / "external-results.json").read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
        for result in external.get("results", []):
            validator.validate(result)
        status = "passed"
        returncode = 0
    except (OSError, json.JSONDecodeError, jsonschema.ValidationError, jsonschema.SchemaError):
        status = "failed"
        returncode = 1
    return {
        "name": "External benchmark JSON Schema",
        "status": status,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "returncode": returncode,
    }


def bundle_check() -> dict[str, Any]:
    node = shutil.which("node")
    if not (ROOT / "node_modules").is_dir() or node is None:
        return {
            "name": "Reproducible UI bundle",
            "status": "skipped",
            "duration_ms": 0.0,
            "returncode": 0,
        }
    build_argv = [node, "scripts/build_indicator.mjs"]
    build = run_command("Reproducible UI bundle", build_argv)
    if build["status"] == "passed":
        bundle = ROOT / "assets" / "parallel-indicator-host.bundle.js"
        first_digest = sha256(bundle)
        second = run_command("Reproducible UI bundle", build_argv)
        build["duration_ms"] = round(build["duration_ms"] + second["duration_ms"], 2)
        if second["status"] != "passed" or sha256(bundle) != first_digest:
            build["status"] = "failed"
            build["returncode"] = second["returncode"] or 1
    return build


def command_version(argv: list[str]) -> str:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    line = (completed.stdout or completed.stderr).strip().splitlines()
    return line[0] if line else "unavailable"


def public_runner_label() -> str:
    runner_environment = os.environ.get("RUNNER_ENVIRONMENT", "").casefold()
    if runner_environment in {"github-hosted", "self-hosted"}:
        return f"GitHub Actions ({runner_environment})"
    if os.environ.get("GITHUB_ACTIONS", "").casefold() == "true":
        return "GitHub Actions"
    return "local verification runner"


def git_value(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def _git_dirty_paths() -> set[str] | None:
    """Return tracked and untracked worktree changes, or None when Git is unavailable."""
    try:
        tracked = subprocess.check_output(
            ["git", "diff", "--name-only", "HEAD", "--"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).splitlines()
        untracked = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).splitlines()
    except (OSError, subprocess.CalledProcessError):
        return None
    return {path for path in [*tracked, *untracked] if path}


def _source_provenance_check() -> tuple[dict[str, Any], bool]:
    started = time.perf_counter()
    dirty_paths = _git_dirty_paths()
    unexpected = (
        None if dirty_paths is None else sorted(dirty_paths - REPORT_EVIDENCE_INPUTS)
    )
    clean = unexpected == []
    return (
        {
            "name": "Clean source provenance",
            "status": "passed" if clean else "failed",
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "returncode": 0 if clean else 1,
            "unexpected_dirty_path_count": (
                None if unexpected is None else len(unexpected)
            ),
            "allowed_evidence_input_count": (
                0
                if dirty_paths is None
                else len(dirty_paths & REPORT_EVIDENCE_INPUTS)
            ),
        },
        clean,
    )


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("benchmark evidence value is not numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("benchmark evidence value is not finite")
    return number


def _evidence_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("benchmark evidence value is not an integer")
    return value


def validate_macos_benchmark_evidence(
    benchmark: Any,
    *,
    expected_commit: str | None = None,
) -> dict[str, Any]:
    """Accept only complete, source-bound native-macOS five-minute evidence."""
    if not isinstance(benchmark, dict):
        return {"available": False, "status": "invalid_benchmark_evidence"}
    latest = benchmark.get("latest")
    cumulative = benchmark.get("cumulative")
    history = benchmark.get("history")
    realms = benchmark.get("realms")
    if not all(
        isinstance(value, expected)
        for value, expected in (
            (latest, dict),
            (cumulative, dict),
            (history, list),
            (realms, dict),
        )
    ):
        return {"available": False, "status": "invalid_benchmark_evidence"}

    platform_info = latest.get("platform")
    tasks = latest.get("tasks")
    parallel = latest.get("parallel")
    serial = latest.get("serial_equivalent")
    savings = latest.get("savings")
    progress = latest.get("progress_samples")
    if not all(
        isinstance(value, expected)
        for value, expected in (
            (platform_info, dict),
            (tasks, list),
            (parallel, dict),
            (serial, dict),
            (savings, dict),
            (progress, list),
        )
    ) or not all(isinstance(task, dict) for task in tasks):
        return {"available": False, "status": "invalid_benchmark_evidence"}

    try:
        minimum_seconds = _evidence_number(latest["minimum_task_seconds"])
        target_seconds = _evidence_number(latest["target_task_seconds"])
        observed_field = _evidence_number(latest["observed_minimum_task_seconds"])
        tolerance_seconds = _evidence_number(
            latest["worker_duration_tolerance_seconds"]
        )
        task_count = _evidence_integer(latest["task_count"])
        wall_seconds = _evidence_number(parallel["wall_time_seconds"])
        peak = _evidence_integer(parallel["peak_concurrency"])
        chosen = _evidence_integer(parallel["chosen_concurrency"])
        serial_seconds = _evidence_number(serial["seconds"])
        saved_seconds = _evidence_number(savings["seconds"])
        speedup = _evidence_number(savings["speedup_multiplier"])
        percent = _evidence_number(savings["percent"])
        efficiency = _evidence_number(savings["parallel_efficiency"])
        durations = [_evidence_number(task["duration_seconds"]) for task in tasks]
        outer_durations = [
            _evidence_number(task["outer_duration_seconds"]) for task in tasks
        ]
        duration_drifts = [
            _evidence_number(task["duration_drift_seconds"]) for task in tasks
        ]
        iterations = [_evidence_integer(task["iterations"]) for task in tasks]
        work_units = [_evidence_integer(task["work_units"]) for task in tasks]
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"available": False, "status": "invalid_benchmark_evidence"}

    latest_commit = latest.get("commit")
    commit_is_valid = (
        isinstance(latest_commit, str)
        and GIT_COMMIT_PATTERN.fullmatch(latest_commit) is not None
    )
    commit_matches_source = commit_is_valid and _macos_evidence_matches_source(
        latest_commit
    )
    if expected_commit is not None:
        expected_commit_is_valid = (
            isinstance(expected_commit, str)
            and GIT_COMMIT_PATTERN.fullmatch(expected_commit) is not None
        )
        source_matches = (
            expected_commit_is_valid
            and commit_is_valid
            and latest_commit == expected_commit
            and commit_matches_source
        )
    else:
        source_matches = commit_matches_source

    expected_tolerance = max(2.0, target_seconds * 0.02)
    task_ids = [task.get("id") for task in tasks]
    task_ids_are_valid = all(isinstance(task_id, str) for task_id in task_ids)
    tasks_are_complete = (
        len(tasks) == len(LONG_BENCHMARK_TASK_IDS)
        and task_count == len(LONG_BENCHMARK_TASK_IDS)
        and task_ids_are_valid
        and set(task_ids) == LONG_BENCHMARK_TASK_IDS
        and all(task.get("scenario") == task.get("id") for task in tasks)
        and all(task.get("status") == "succeeded" for task in tasks)
        and all(task.get("worker_evidence_valid") is True for task in tasks)
        and all(task.get("worker_evidence_errors") == [] for task in tasks)
        and all(value > 0 for value in iterations)
        and all(value > 0 for value in work_units)
    )
    durations_are_observed = (
        all(value >= 300 for value in durations)
        and all(value >= 300 for value in outer_durations)
        and all(value >= 0 for value in duration_drifts)
        and all(
            math.isclose(
                drift,
                abs(outer - observed),
                rel_tol=0,
                abs_tol=0.01,
            )
            and drift <= tolerance_seconds
            for observed, outer, drift in zip(
                durations,
                outer_durations,
                duration_drifts,
                strict=True,
            )
        )
    )
    observed_minimum = min(durations, default=0.0)
    arithmetic_matches = (
        wall_seconds > 0
        and peak > 0
        and serial_seconds > 0
        and saved_seconds >= 0
        and math.isclose(serial_seconds, sum(durations), rel_tol=0, abs_tol=0.01)
        and math.isclose(
            saved_seconds,
            max(0.0, serial_seconds - wall_seconds),
            rel_tol=0,
            abs_tol=0.01,
        )
        and math.isclose(observed_field, observed_minimum, rel_tol=0, abs_tol=0.01)
        and math.isclose(
            speedup,
            serial_seconds / wall_seconds,
            rel_tol=0,
            abs_tol=0.0001,
        )
        and math.isclose(
            percent,
            saved_seconds / serial_seconds * 100,
            rel_tol=0,
            abs_tol=0.0001,
        )
        and math.isclose(
            efficiency,
            serial_seconds / wall_seconds / peak,
            rel_tol=0,
            abs_tol=0.0001,
        )
    )

    progress_is_valid = bool(progress) and all(
        isinstance(sample, dict) for sample in progress
    )
    has_live_sample = False
    if progress_is_valid:
        try:
            has_live_sample = any(
                _evidence_integer(sample["running_tasks"])
                == len(LONG_BENCHMARK_TASK_IDS)
                and 0
                <= _evidence_integer(sample["completed_tasks"])
                < len(LONG_BENCHMARK_TASK_IDS)
                and 0 <= _evidence_number(sample["elapsed_seconds"]) < wall_seconds
                and sample.get("savings_eligible") is True
                for sample in progress
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            progress_is_valid = False

    history_is_macos_only = bool(history) and all(
        isinstance(row, dict)
        and row.get("status") == "passed"
        and row.get("execution_realm") == "macos_native"
        for row in history
    )
    run_ids = [str(row.get("run_id", "")) for row in history if isinstance(row, dict)]
    latest_run_id = str(latest.get("run_id", ""))
    history_is_unique = (
        len(run_ids) == len(history)
        and len(run_ids) == len(set(run_ids))
        and all(run_id.endswith("@macos_native") for run_id in run_ids)
    )
    current_history_rows = [
        row
        for row in history
        if isinstance(row, dict) and str(row.get("run_id", "")) == latest_run_id
    ]
    history_arithmetic_matches = True
    try:
        for row in history:
            row_commit = row["commit"]
            if (
                not isinstance(row_commit, str)
                or GIT_COMMIT_PATTERN.fullmatch(row_commit) is None
            ):
                history_arithmetic_matches = False
                break
            row_wall = _evidence_number(row["wall_time_seconds"])
            row_serial = _evidence_number(row["serial_equivalent_seconds"])
            row_saved = _evidence_number(row["saved_seconds"])
            row_speedup = _evidence_number(row["speedup_multiplier"])
            if not (
                row_wall > 0
                and row_serial > 0
                and row_saved >= 0
                and math.isclose(
                    row_saved,
                    max(0.0, row_serial - row_wall),
                    rel_tol=0,
                    abs_tol=0.01,
                )
                and math.isclose(
                    row_speedup,
                    row_serial / row_wall,
                    rel_tol=0,
                    abs_tol=0.0001,
                )
            ):
                history_arithmetic_matches = False
                break
    except (KeyError, TypeError, ValueError, OverflowError):
        history_arithmetic_matches = False

    try:
        current_history_matches = len(current_history_rows) == 1 and (
            current_history_rows[0].get("commit") == latest_commit
            and math.isclose(
                _evidence_number(current_history_rows[0]["wall_time_seconds"]),
                wall_seconds,
                rel_tol=0,
                abs_tol=0.01,
            )
            and math.isclose(
                _evidence_number(
                    current_history_rows[0]["serial_equivalent_seconds"]
                ),
                serial_seconds,
                rel_tol=0,
                abs_tol=0.01,
            )
            and math.isclose(
                _evidence_number(current_history_rows[0]["saved_seconds"]),
                saved_seconds,
                rel_tol=0,
                abs_tol=0.01,
            )
            and math.isclose(
                _evidence_number(current_history_rows[0]["speedup_multiplier"]),
                speedup,
                rel_tol=0,
                abs_tol=0.0001,
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        current_history_matches = False

    try:
        cumulative_matches = (
            _evidence_integer(cumulative["run_count"]) == len(history)
            and math.isclose(
                _evidence_number(cumulative["parallel_wall_seconds"]),
                sum(_evidence_number(row["wall_time_seconds"]) for row in history),
                rel_tol=0,
                abs_tol=0.01,
            )
            and math.isclose(
                _evidence_number(cumulative["serial_equivalent_seconds"]),
                sum(
                    _evidence_number(row["serial_equivalent_seconds"])
                    for row in history
                ),
                rel_tol=0,
                abs_tol=0.01,
            )
            and math.isclose(
                _evidence_number(cumulative["saved_seconds"]),
                sum(_evidence_number(row["saved_seconds"]) for row in history),
                rel_tol=0,
                abs_tol=0.01,
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        cumulative_matches = False

    macos_state = realms.get("macos_native")
    realm_state_matches = (
        isinstance(macos_state, dict)
        and macos_state.get("latest") == latest
        and macos_state.get("history") == history
        and macos_state.get("cumulative") == cumulative
    )
    system = str(platform_info.get("system", "")).casefold()
    realm = platform_info.get("execution_realm")
    evidence_valid = (
        benchmark.get("schema_version") == "1.1"
        and benchmark.get("aggregation_scope") == "execution_realm"
        and benchmark.get("latest_realm") == "macos_native"
        and latest.get("status") == "passed"
        and latest.get("worker_evidence_complete") is True
        and latest.get("minimum_duration_met") is True
        and latest.get("full_parallelism_observed") is True
        and minimum_seconds >= 300
        and target_seconds >= minimum_seconds
        and math.isclose(
            tolerance_seconds,
            expected_tolerance,
            rel_tol=0,
            abs_tol=0.000001,
        )
        and observed_minimum >= 300
        and system in {"darwin", "macos"}
        and realm == "macos_native"
        and latest_run_id.endswith("@macos_native")
        and tasks_are_complete
        and durations_are_observed
        and peak == len(LONG_BENCHMARK_TASK_IDS)
        and chosen == len(LONG_BENCHMARK_TASK_IDS)
        and progress_is_valid
        and has_live_sample
        and arithmetic_matches
        and source_matches
        and history_is_macos_only
        and history_is_unique
        and history_arithmetic_matches
        and current_history_matches
        and cumulative_matches
        and realm_state_matches
    )
    if not evidence_valid:
        return {
            "available": False,
            "status": "native_macos_benchmark_gate_failed",
            "schema_is_current": benchmark.get("schema_version") == "1.1",
            "source_match": source_matches,
            "all_tasks_succeeded": tasks_are_complete,
            "worker_evidence_complete": latest.get("worker_evidence_complete")
            is True,
            "observed_minimum_task_seconds": round(observed_minimum, 6),
            "task_arithmetic_matches": arithmetic_matches,
            "live_progress_observed": has_live_sample,
            "history_is_macos_only": history_is_macos_only,
            "current_history_matches_latest": current_history_matches,
            "cumulative_matches_history": cumulative_matches,
        }
    return {
        **benchmark,
        "available": True,
        "status": "passed",
        "source_match": True,
    }


def load_long_benchmark() -> dict[str, Any]:
    path = ROOT / "docs" / "benchmark-results.json"
    if not path.exists():
        return {"available": False, "status": "awaiting_native_benchmark"}
    try:
        benchmark = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False, "status": "invalid_benchmark_evidence"}
    evidence = validate_macos_benchmark_evidence(benchmark)
    if not evidence["available"]:
        return evidence
    return {
        **evidence,
        "evidence_sha256": sha256(path),
        "evidence_url": "benchmark-results.json",
    }


def load_growth_metrics() -> dict[str, Any]:
    path = ROOT / "docs" / "metrics.json"
    if not path.exists():
        return {"available": False}
    try:
        metrics = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False}
    latest = metrics.get("latest")
    targets = metrics.get("targets_30d")
    if not isinstance(latest, dict) or not isinstance(targets, dict):
        return {"available": False}
    return {"available": True, **metrics}


def _benchmark_evidence_matches_source(commit: str) -> bool:
    if GIT_COMMIT_PATTERN.fullmatch(commit) is None:
        return False
    dirty_paths = _git_dirty_paths()
    if dirty_paths is None or dirty_paths - REPORT_EVIDENCE_INPUTS:
        return False
    current = git_value("rev-parse", "HEAD")
    if current == commit:
        return True
    if GIT_COMMIT_PATTERN.fullmatch(current) is None:
        return False
    try:
        subprocess.check_call(
            ["git", "merge-base", "--is-ancestor", commit, current],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        changed = subprocess.check_output(
            ["git", "diff", "--name-only", f"{commit}..{current}"],
            cwd=ROOT,
            text=True,
        ).splitlines()
    except (OSError, subprocess.CalledProcessError):
        return False
    return bool(changed) and all(path.startswith("docs/") for path in changed)


def _macos_evidence_matches_source(commit: str) -> bool:
    return _benchmark_evidence_matches_source(commit)


def _windows_evidence_matches_source(commit: str) -> bool:
    return _benchmark_evidence_matches_source(commit)


def load_windows_preview_evidence(
    path: pathlib.Path | None = None,
    *,
    expected_commit: str | None = None,
) -> dict[str, Any]:
    """Load and compact a report produced by the native Windows release runner."""
    path = path or ROOT / "docs" / "windows-preview-results.json"
    if not path.exists():
        return {"available": False, "status": "awaiting_native_ci"}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False, "status": "invalid_evidence"}
    environment = report.get("environment")
    summary = report.get("summary")
    source = report.get("source")
    domains = report.get("domains")
    checks = report.get("checks")
    tests = report.get("tests")
    if not all(
        isinstance(value, expected)
        for value, expected in (
            (environment, dict),
            (summary, dict),
            (source, dict),
            (domains, list),
            (checks, list),
            (tests, list),
        )
    ):
        return {"available": False, "status": "invalid_evidence"}
    if not str(environment.get("os", "")).startswith("Windows"):
        return {"available": False, "status": "not_native_windows"}
    runtime_domain = next(
        (
            domain
            for domain in domains
            if isinstance(domain, dict) and domain.get("module") == "test_windows_runtime"
        ),
        None,
    )
    if not isinstance(runtime_domain, dict):
        return {"available": False, "status": "missing_native_runtime_domain"}
    expected_version = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )["version"]
    source_commit = str(source.get("commit", ""))
    native_test_rows = [
        test
        for test in tests
        if isinstance(test, dict) and test.get("class") == "WindowsNativeRuntimeTests"
    ]
    critical = {
        test.get("name"): test.get("status")
        for test in native_test_rows
    }
    release_checks_passed = sum(
        isinstance(check, dict) and check.get("status") == "passed" for check in checks
    )
    native_gate_passed = (
        WINDOWS_CRITICAL_TESTS.issubset(critical)
        and len(critical) == len(native_test_rows)
        and all(test.get("status") == "passed" for test in native_test_rows)
        and runtime_domain.get("failed", 0) == 0
        and runtime_domain.get("errors", 0) == 0
        and runtime_domain.get("skipped", 0) == 0
    )
    release_gate_passed = release_checks_passed == len(checks) and bool(checks)
    expected_commit_matches = expected_commit is None or (
        isinstance(expected_commit, str)
        and GIT_COMMIT_PATTERN.fullmatch(expected_commit) is not None
        and source_commit == expected_commit
    )
    evidence_is_current = (
        expected_commit_matches
        and _windows_evidence_matches_source(source_commit)
    )
    if not (
        report.get("overall") == "passed"
        and report.get("version") == expected_version
        and summary.get("failed", 0) == 0
        and summary.get("errors", 0) == 0
        and native_gate_passed
        and release_gate_passed
        and evidence_is_current
    ):
        return {
            "available": False,
            "status": "native_evidence_gate_failed",
            "version_match": report.get("version") == expected_version,
            "source_match": evidence_is_current,
            "critical_tests_passed": sum(
                critical.get(name) == "passed" for name in WINDOWS_CRITICAL_TESTS
            ),
            "critical_tests_required": len(WINDOWS_CRITICAL_TESTS),
        }
    return {
        "available": True,
        "source_match": True,
        "status": report.get("overall", "unknown"),
        "version": report.get("version", "unknown"),
        "verified_at": report.get("generated_at", "unknown"),
        "commit": source_commit,
        "runner": environment.get("runner", "unknown"),
        "os": environment.get("os", "unknown"),
        "architecture": environment.get("architecture", "unknown"),
        "python": environment.get("python", "unknown"),
        "tests": {
            "total": summary.get("total", 0),
            "passed": summary.get("passed", 0),
            "failed": summary.get("failed", 0),
            "errors": summary.get("errors", 0),
            "skipped": summary.get("skipped", 0),
        },
        "native_runtime": {
            "total": len(native_test_rows),
            "passed": sum(test.get("status") == "passed" for test in native_test_rows),
            "failed": sum(test.get("status") == "failed" for test in native_test_rows),
            "errors": sum(test.get("status") == "error" for test in native_test_rows),
            "skipped": sum(test.get("status") == "skipped" for test in native_test_rows),
        },
        "release_checks": {
            "passed": release_checks_passed,
            "total": len(checks),
        },
        "critical_tests": sorted(WINDOWS_CRITICAL_TESTS),
        "evidence_sha256": sha256(path),
        "evidence_url": "windows-preview-results.json",
    }


def load_windows_benchmark_evidence(
    path: pathlib.Path | None = None,
    *,
    expected_commit: str | None = None,
) -> dict[str, Any]:
    """Accept only source-bound, observed five-minute native-Windows evidence."""
    path = path or ROOT / "docs" / "windows-benchmark-results.json"
    if not path.exists():
        return {"available": False, "status": "awaiting_native_benchmark"}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False, "status": "invalid_benchmark_evidence"}
    latest = report.get("latest")
    cumulative = report.get("cumulative")
    history = report.get("history")
    if not isinstance(latest, dict) or not isinstance(cumulative, dict) or not isinstance(history, list):
        return {"available": False, "status": "invalid_benchmark_evidence"}
    platform_info = latest.get("platform")
    tasks = latest.get("tasks")
    parallel = latest.get("parallel")
    serial = latest.get("serial_equivalent")
    savings = latest.get("savings")
    progress = latest.get("progress_samples")
    if not all(
        isinstance(value, expected)
        for value, expected in (
            (platform_info, dict),
            (tasks, list),
            (parallel, dict),
            (serial, dict),
            (savings, dict),
            (progress, list),
        )
    ):
        return {"available": False, "status": "invalid_benchmark_evidence"}
    try:
        durations = [float(task["duration_seconds"]) for task in tasks]
        wall_seconds = float(parallel["wall_time_seconds"])
        serial_seconds = float(serial["seconds"])
        saved_seconds = float(savings["seconds"])
        peak = int(parallel["peak_concurrency"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"available": False, "status": "invalid_benchmark_evidence"}
    finite_values = [*durations, wall_seconds, serial_seconds, saved_seconds]
    observed_minimum = min(durations) if durations else 0.0
    system = str(platform_info.get("system") or platform_info.get("platform") or "")
    realm = str(platform_info.get("execution_realm") or "")
    all_succeeded = len(tasks) == 4 and all(
        isinstance(task, dict) and task.get("status") == "succeeded" for task in tasks
    )
    has_live_sample = False
    for sample in progress:
        if not isinstance(sample, dict):
            continue
        try:
            has_live_sample = (
                int(sample.get("running_tasks", 0)) >= 2
                and int(sample.get("completed_tasks", 0)) < len(tasks)
                and float(sample.get("elapsed_seconds", wall_seconds)) < wall_seconds
            )
        except (TypeError, ValueError, OverflowError):
            has_live_sample = False
        if has_live_sample:
            break
    arithmetic_matches = (
        math.isclose(serial_seconds, sum(durations), rel_tol=0, abs_tol=0.01)
        and math.isclose(saved_seconds, max(0.0, serial_seconds - wall_seconds), rel_tol=0, abs_tol=0.01)
    )
    observed_field = latest.get("observed_minimum_task_seconds", observed_minimum)
    try:
        observed_field_matches = math.isclose(
            float(observed_field), observed_minimum, rel_tol=0, abs_tol=0.01
        )
    except (TypeError, ValueError, OverflowError):
        observed_field_matches = False
    latest_commit = str(latest.get("commit", ""))
    expected_commit_matches = expected_commit is None or (
        isinstance(expected_commit, str)
        and GIT_COMMIT_PATTERN.fullmatch(expected_commit) is not None
        and latest_commit == expected_commit
    )
    source_matches = expected_commit_matches and _windows_evidence_matches_source(
        latest_commit
    )
    history_is_windows_only = bool(history) and all(
        isinstance(row, dict)
        and row.get("status") == "passed"
        and row.get("execution_realm") == "windows_native"
        for row in history
    )
    run_ids = [str(row.get("run_id", "")) for row in history if isinstance(row, dict)]
    latest_run_id = str(latest.get("run_id", ""))
    latest_is_in_history = latest_run_id in run_ids
    history_is_unique = len(run_ids) == len(set(run_ids)) and all(run_ids)
    current_history_rows = [
        row
        for row in history
        if isinstance(row, dict) and str(row.get("run_id", "")) == latest_run_id
    ]
    try:
        current_history_matches = len(current_history_rows) == 1 and (
            current_history_rows[0].get("commit") == latest.get("commit")
            and math.isclose(
                float(current_history_rows[0]["wall_time_seconds"]),
                wall_seconds,
                rel_tol=0,
                abs_tol=0.01,
            )
            and math.isclose(
                float(current_history_rows[0]["serial_equivalent_seconds"]),
                serial_seconds,
                rel_tol=0,
                abs_tol=0.01,
            )
            and math.isclose(
                float(current_history_rows[0]["saved_seconds"]),
                saved_seconds,
                rel_tol=0,
                abs_tol=0.01,
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        current_history_matches = False
    try:
        cumulative_matches = (
            int(cumulative["run_count"]) == len(history)
            and math.isclose(
                float(cumulative["parallel_wall_seconds"]),
                sum(float(row["wall_time_seconds"]) for row in history),
                rel_tol=0,
                abs_tol=0.01,
            )
            and math.isclose(
                float(cumulative["serial_equivalent_seconds"]),
                sum(float(row["serial_equivalent_seconds"]) for row in history),
                rel_tol=0,
                abs_tol=0.01,
            )
            and math.isclose(
                float(cumulative["saved_seconds"]),
                sum(float(row["saved_seconds"]) for row in history),
                rel_tol=0,
                abs_tol=0.01,
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        cumulative_matches = False
    realms = report.get("realms")
    windows_state = realms.get("windows_native") if isinstance(realms, dict) else None
    realm_state_matches = (
        isinstance(windows_state, dict)
        and windows_state.get("history") == history
        and windows_state.get("cumulative") == cumulative
        and windows_state.get("latest") == latest
    )
    evidence_valid = (
        report.get("schema_version") == "1.1"
        and report.get("aggregation_scope") == "execution_realm"
        and report.get("latest_realm") == "windows_native"
        and latest.get("status") == "passed"
        and system.casefold().startswith("windows")
        and realm == "windows_native"
        and all(math.isfinite(value) and value >= 0 for value in finite_values)
        and all_succeeded
        and observed_minimum >= 300
        and latest.get("minimum_duration_met") is True
        and peak >= 2
        and has_live_sample
        and arithmetic_matches
        and observed_field_matches
        and source_matches
        and history_is_windows_only
        and latest_is_in_history
        and current_history_matches
        and history_is_unique
        and cumulative_matches
        and realm_state_matches
    )
    if not evidence_valid:
        return {
            "available": False,
            "status": "native_benchmark_gate_failed",
            "source_match": source_matches,
            "observed_minimum_task_seconds": round(observed_minimum, 6),
            "all_tasks_succeeded": all_succeeded,
            "live_progress_observed": has_live_sample,
            "history_is_windows_only": history_is_windows_only,
            "current_history_matches_latest": current_history_matches,
            "cumulative_matches_history": cumulative_matches,
        }
    return {
        **report,
        "available": True,
        "source_match": True,
        "evidence_sha256": sha256(path),
        "evidence_url": "windows-benchmark-results.json",
    }


def build_python_advisor_evidence() -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the public static fixtures and retain only sanitized evidence."""
    from python_parallel_advisor import analyze_python_parallelism

    started = time.perf_counter()
    fixture_root = ROOT / "benchmarks" / "python-advisor-fixtures"
    paths = ["must_not_execute.py", "native.py", "pure_cpu.py", "read_io.py", "stateful.py"]
    fixture_paths = {path: fixture_root / path for path in paths}
    marker_path = fixture_root / "must-not-exist.marker"
    before_hashes: dict[str, str] = {}
    after_hashes: dict[str, str] = {}
    marker_absent_before = not marker_path.exists()
    marker_absent_after = False
    try:
        before_hashes = {path: sha256(source) for path, source in fixture_paths.items()}
        result = analyze_python_parallelism(
            fixture_root,
            paths=paths,
            max_workers=4,
        )
        after_hashes = {path: sha256(source) for path, source in fixture_paths.items()}
        marker_absent_after = not marker_path.exists()
        hashes_unchanged = before_hashes == after_hashes
        counts = result["summary"]["classification_counts"]
        previews = [item for item in result["candidates"] if "rewrite_preview" in item]
        expected = {
            "reviewable_rewrite": 1,
            "advisory_only": 1,
            "blocked": 1,
            "prefer_native": 1,
        }
        passed = (
            all(counts.get(name) == count for name, count in expected.items())
            and result["execution_performed"] is False
            and result["files_modified"] is False
            and marker_absent_before
            and marker_absent_after
            and hashes_unchanged
            and len(previews) == 1
            and previews[0]["rewrite_preview"]["source_sha256"] == previews[0]["source_sha256"]
        )
        evidence = {
            "available": True,
            "analysis_hash": result["analysis_hash"],
            "analysis_mode": result["analysis_mode"],
            "files_analyzed": result["summary"]["files_analyzed"],
            "candidate_count": result["summary"]["candidate_count"],
            "classification_counts": counts,
            "rewrite_preview_count": len(previews),
            "source_hash_bound": bool(previews),
            "execution_performed": result["execution_performed"],
            "files_modified": result["files_modified"],
            "target_code_executed": result["execution_performed"],
            "target_files_modified": result["files_modified"],
            "fixture_sha256_before": before_hashes,
            "fixture_sha256_after": after_hashes,
            "fixture_hashes_unchanged": hashes_unchanged,
            "execution_marker_absent": marker_absent_before and marker_absent_after,
            "benefit_kind": previews[0]["benefit"]["kind"] if previews else "not_available",
        }
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        passed = False
        try:
            after_hashes = {path: sha256(source) for path, source in fixture_paths.items()}
        except OSError:
            after_hashes = {}
        marker_absent_after = not marker_path.exists()
        evidence = {
            "available": False,
            "error_type": type(exc).__name__,
            "fixture_sha256_before": before_hashes,
            "fixture_sha256_after": after_hashes,
            "fixture_hashes_unchanged": bool(before_hashes) and before_hashes == after_hashes,
            "execution_marker_absent": marker_absent_before and marker_absent_after,
        }
    check = {
        "name": "Python advisor safety fixtures",
        "status": "passed" if passed else "failed",
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "returncode": 0 if passed else 1,
    }
    return check, evidence


def build_report() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    python_sources = sorted(str(path.relative_to(ROOT)) for path in SCRIPTS.glob("*.py"))
    checks.append(
        run_command(
            "Python bytecode compilation",
            [sys.executable, "-m", "py_compile", *python_sources],
        )
    )
    regression_check, tests = run_regression_tests()
    checks.append(regression_check)
    checks.append(run_command("End-to-end MCP self-test", [sys.executable, "scripts/self_test.py"]))

    if shutil.which("ruff"):
        ruff_argv = ["ruff", "check", "--no-cache", "scripts"]
    elif shutil.which("uvx"):
        ruff_argv = ["uvx", "ruff@0.16.4", "check", "--no-cache", "scripts"]
    else:
        ruff_argv = [sys.executable, "-m", "ruff", "check", "--no-cache", "scripts"]
    checks.append(run_command("Ruff static analysis", ruff_argv))
    checks.append(validate_metadata())
    checks.append(validate_external_benchmarks())
    advisor_check, advisor_evidence = build_python_advisor_evidence()
    checks.append(advisor_check)
    checks.append(bundle_check())
    provenance_check, source_clean = _source_provenance_check()
    checks.append(provenance_check)

    status_counts = Counter(test["status"] for test in tests)
    passed = status_counts["passed"]
    failed = status_counts["failed"]
    errors = status_counts["error"]
    skipped = status_counts["skipped"]
    expected_failures = status_counts["expected_failure"]
    unexpected_successes = status_counts["unexpected_success"]
    required_tests = max(0, len(tests) - skipped - expected_failures)
    overall = "passed" if all(check["status"] in {"passed", "skipped"} for check in checks) else "failed"
    domain_rows = []
    for module, meta in DOMAIN_META.items():
        domain_tests = [test for test in tests if test["module"] == module]
        domain_rows.append(
            {
                **meta,
                "module": module,
                "total": len(domain_tests),
                "passed": sum(test["status"] == "passed" for test in domain_tests),
                "failed": sum(test["status"] == "failed" for test in domain_tests),
                "errors": sum(test["status"] == "error" for test in domain_tests),
                "skipped": sum(test["status"] == "skipped" for test in domain_tests),
                "duration_ms": round(sum(test["duration_ms"] for test in domain_tests), 2),
            }
        )

    bundle = ROOT / "assets" / "parallel-indicator-host.bundle.js"
    return {
        "schema_version": "1.0",
        "project": "AtomLane",
        "version": json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())["version"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall": overall,
        "summary": {
            "total": len(tests),
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "expected_failures": expected_failures,
            "unexpected_successes": unexpected_successes,
            "pass_rate": round((passed / required_tests * 100) if required_tests else 0, 2),
            "checks_passed": sum(check["status"] == "passed" for check in checks),
            "checks_total": len(checks),
            "elapsed_ms": round(sum(check["duration_ms"] for check in checks), 2),
        },
        "source": {
            "commit": git_value("rev-parse", "HEAD"),
            "commit_short": git_value("rev-parse", "--short=10", "HEAD"),
            "branch": os.environ.get("GITHUB_REF_NAME") or git_value("branch", "--show-current"),
            "repository": "cloudguo123/atomlane",
            "clean": source_clean,
        },
        "environment": {
            "os": f"{platform.system()} {platform.release()}",
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "node": command_version(["node", "--version"]),
            "runner": public_runner_label(),
        },
        "provenance": {
            "bundle_sha256": sha256(bundle),
            "bundle_bytes": bundle.stat().st_size,
        },
        "checks": checks,
        "domains": domain_rows,
        "tests": tests,
        "benchmark": load_long_benchmark(),
        "windows_preview": load_windows_preview_evidence(),
        "windows_benchmark": load_windows_benchmark_evidence(),
        "python_advisor": advisor_evidence,
        "growth": load_growth_metrics(),
        "scope_note": (
            "This dashboard reports behavioral regression and release-gate results. "
            "The subsystem cards reflect tests executed on the report host; native Windows "
            "claims require the separate source-bound Windows evidence panels above. It is "
            "not a statement of line or branch coverage."
        ),
    }


def render_html(report: dict[str, Any]) -> str:
    data = json.dumps(report, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    overall = report["overall"].upper()
    generated = html.escape(report["generated_at"])
    summary = report.get("summary", {})
    verified_test_count = html.escape(
        str(summary.get("passed", summary.get("total", "verified")))
    )
    windows_verified = bool(report.get("windows_preview", {}).get("available"))
    if windows_verified:
        meta_description = (
            "Verified host regression tests, source-bound native Windows Preview evidence, "
            "five-minute benchmarks, live progress, and public adoption signals for AtomLane."
        )
        operating_system = "macOS, Windows Preview"
        hero_platform_claim = "runs proven concurrency on macOS and native Windows Preview"
    else:
        meta_description = (
            "Host regression tests, Windows Preview evidence status, five-minute benchmarks, "
            "live progress, and public adoption signals for AtomLane."
        )
        operating_system = "macOS"
        hero_platform_claim = (
            "runs proven concurrency on macOS while native Windows Preview evidence is pending"
        )
    structured_data = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "SoftwareApplication",
            "name": "AtomLane",
            "applicationCategory": "DeveloperApplication",
            "operatingSystem": operating_system,
            "softwareVersion": str(report["version"]),
            "codeRepository": "https://github.com/cloudguo123/atomlane",
            "url": "https://cloudguo123.github.io/atomlane/",
            "license": "https://opensource.org/license/mit",
            "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="description" content="{html.escape(meta_description)}">
  <meta name="theme-color" content="#07100e">
  <link rel="canonical" href="https://cloudguo123.github.io/atomlane/">
  <meta property="og:type" content="website">
  <meta property="og:title" content="AtomLane · Verified test report">
  <meta property="og:description" content="Parallelize only what is proven safe: {verified_test_count} verified tests, Python refactor safety fixtures, and a reproducible five-minute benchmark.">
  <meta property="og:url" content="https://cloudguo123.github.io/atomlane/">
  <meta property="og:image" content="https://cloudguo123.github.io/atomlane/share/social-preview.png">
  <meta property="og:image:width" content="1280">
  <meta property="og:image:height" content="640">
  <meta property="og:image:alt" content="AtomLane benchmark: 20 minutes 41 seconds serial equivalent, 5 minutes 10 seconds parallel wall time, 15 minutes 31 seconds saved">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="AtomLane · Verified test report">
  <meta name="twitter:description" content="Parallelize only what is proven safe, with live progress and honest savings evidence.">
  <meta name="twitter:image" content="https://cloudguo123.github.io/atomlane/share/social-preview.png">
  <script type="application/ld+json">{structured_data}</script>
  <title>AtomLane · Test Report</title>
  <style>
    :root {{ color-scheme: dark; --bg:#07100e; --panel:#0c1815; --panel2:#10201c; --line:#20352f; --text:#ecf8f3; --muted:#8ba69c; --green:#65e6b4; --blue:#80b7ff; --purple:#d7a6ff; --red:#ff8f8f; }}
    * {{ box-sizing:border-box }}
    body {{ margin:0; background:radial-gradient(circle at 80% -10%,#163d32 0,transparent 34%),radial-gradient(circle at 5% 20%,#102b32 0,transparent 28%),var(--bg); color:var(--text); font:15px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; min-height:100vh }}
    a {{ color:var(--green); text-decoration:none }} a:hover {{ text-decoration:underline }}
    .wrap {{ width:min(1180px,calc(100% - 32px)); margin:auto; padding:46px 0 72px }}
    .nav {{ display:flex; justify-content:space-between; align-items:center; color:var(--muted); margin-bottom:50px }}
    .brand {{ color:var(--text); font-weight:700; letter-spacing:.02em }} .navlinks {{ display:flex; gap:20px }}
    .eyebrow {{ text-transform:uppercase; letter-spacing:.18em; font-size:12px; color:var(--green); font-weight:800 }}
    h1 {{ margin:14px 0 12px; max-width:820px; font-size:clamp(42px,7vw,82px); line-height:.98; letter-spacing:-.055em }}
    .lede {{ color:var(--muted); font-size:18px; max-width:720px; margin:0 }}
    .actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:25px }} .button {{ display:inline-flex; align-items:center; justify-content:center; border-radius:11px; padding:11px 16px; color:#06100d; background:var(--green); font-weight:800; border:1px solid var(--green) }} .button.secondary {{ color:var(--text); background:#0b1714; border-color:var(--line) }} .button:hover {{ text-decoration:none; filter:brightness(1.06) }}
    .hero {{ display:grid; grid-template-columns:1fr 260px; gap:32px; align-items:center; margin-bottom:46px }}
    .seal {{ width:220px; aspect-ratio:1; margin-left:auto; border-radius:50%; display:grid; place-items:center; background:conic-gradient(var(--green) calc(var(--rate)*1%),#1a2b26 0); box-shadow:0 0 80px #45d89d1b; position:relative }}
    .seal:before {{ content:""; position:absolute; inset:11px; border-radius:50%; background:var(--bg); border:1px solid var(--line) }}
    .seal-inner {{ z-index:1; text-align:center }} .rate {{ font-size:46px; font-weight:850; letter-spacing:-.05em }} .seal-label {{ color:var(--muted); font-size:12px; letter-spacing:.13em; text-transform:uppercase }}
    .grid4 {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:30px 0 42px }}
    .grid6 {{ display:grid; grid-template-columns:repeat(6,1fr); gap:10px }}
    .metric,.panel {{ background:linear-gradient(145deg,#10211dce,#0b1714e8); border:1px solid var(--line); border-radius:16px }}
    .metric {{ padding:20px }} .metric strong {{ display:block; font-size:28px; letter-spacing:-.03em }} .metric span {{ color:var(--muted); font-size:13px }}
    .section-title {{ display:flex; justify-content:space-between; align-items:end; margin:46px 0 16px }} .section-title h2 {{ margin:0; font-size:26px; letter-spacing:-.03em }} .section-title p {{ margin:0; color:var(--muted) }}
    .panel {{ padding:22px }}
    .checks {{ display:grid; grid-template-columns:repeat(2,1fr); gap:12px }}
    .check {{ padding:17px; border:1px solid var(--line); border-radius:12px; background:#0b1714 }} .checktop {{ display:flex; justify-content:space-between; gap:10px }}
    .pill {{ border-radius:999px; padding:3px 9px; font-size:11px; font-weight:800; letter-spacing:.08em; text-transform:uppercase; background:#65e6b418; color:var(--green); border:1px solid #65e6b43b }}
    .pill.failed {{ color:var(--red); border-color:#ff8f8f45; background:#ff8f8f12 }} .pill.skipped {{ color:#f4cf78; border-color:#f4cf7840; background:#f4cf7812 }}
    .bar {{ height:5px; background:#1a2a25; border-radius:10px; overflow:hidden; margin-top:14px }} .bar i {{ height:100%; display:block; background:linear-gradient(90deg,var(--green),var(--blue)); border-radius:10px }}
    .benchmark {{ position:relative; overflow:hidden; border-color:#65e6b454; box-shadow:0 28px 80px #0005 }} .benchmark:before {{ content:""; position:absolute; width:420px; height:420px; border-radius:50%; background:#52dfa318; filter:blur(80px); right:-180px; top:-250px; pointer-events:none }}
    .benchmark-head {{ display:flex; justify-content:space-between; gap:20px; align-items:start; margin-bottom:22px }} .benchmark-head h3 {{ font-size:28px; margin:4px 0 }} .benchmark-head p {{ color:var(--muted); margin:0; max-width:670px }}
    .long-pill {{ display:inline-flex; gap:8px; align-items:center; color:var(--green); font-weight:750; background:#65e6b412; border:1px solid #65e6b43e; border-radius:999px; padding:8px 12px; white-space:nowrap }} .long-pill:before {{ content:""; width:7px; height:7px; border-radius:50%; background:var(--green); box-shadow:0 0 12px var(--green) }}
    .bench-metric {{ background:#08130fbd; border:1px solid var(--line); border-radius:12px; padding:14px }} .bench-metric strong {{ display:block; font-size:21px }} .bench-metric span {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.07em }}
    .comparison {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-top:20px }} .compare-row {{ display:grid; grid-template-columns:130px 1fr 72px; gap:12px; align-items:center; margin:12px 0 }} .compare-track {{ height:12px; background:#152722; border-radius:999px; overflow:hidden }} .compare-fill {{ display:block; height:100%; border-radius:999px; background:linear-gradient(90deg,var(--blue),var(--purple)) }} .compare-fill.parallel {{ background:linear-gradient(90deg,var(--green),#9ef0d1) }}
    .lanes {{ display:grid; gap:8px }} .lane {{ display:grid; grid-template-columns:180px 1fr 72px; gap:12px; align-items:center; font-size:13px }} .lane-track {{ height:26px; background:#142620; border-radius:7px; padding:3px }} .lane-fill {{ display:flex; align-items:center; height:100%; border-radius:5px; padding:0 8px; color:#06100d; background:linear-gradient(90deg,var(--green),#9ae7ff); font-size:10px; font-weight:850; letter-spacing:.07em }}
    .history {{ display:flex; align-items:end; gap:7px; min-height:90px; padding-top:14px }} .history-bar {{ min-width:18px; flex:1; max-width:42px; border-radius:5px 5px 2px 2px; background:linear-gradient(180deg,var(--purple),#5f7ee7); position:relative }} .history-bar:hover:after {{ content:attr(data-label); position:absolute; bottom:calc(100% + 6px); left:50%; transform:translateX(-50%); color:var(--text); background:#07100e; border:1px solid var(--line); border-radius:6px; padding:4px 7px; white-space:nowrap; font-size:11px }}
    .domains {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px }} .domain {{ position:relative; overflow:hidden }} .domain:before {{ content:""; position:absolute; inset:0 auto 0 0; width:3px; background:var(--accent) }}
    .domain h3 {{ margin:0 0 7px; font-size:17px }} .domain p {{ margin:0; min-height:48px; color:var(--muted) }} .domain-foot {{ display:flex; justify-content:space-between; margin-top:18px; font-variant-numeric:tabular-nums }}
    .toolbar {{ display:flex; gap:10px; margin-bottom:13px }} input {{ flex:1; min-width:0; color:var(--text); background:#08120f; border:1px solid var(--line); border-radius:10px; padding:11px 13px; outline:none }} input:focus {{ border-color:#65e6b478 }}
    table {{ width:100%; border-collapse:collapse }} th {{ text-align:left; color:var(--muted); font-size:11px; letter-spacing:.1em; text-transform:uppercase; padding:10px 12px; border-bottom:1px solid var(--line) }} td {{ padding:13px 12px; border-bottom:1px solid #172a25; vertical-align:top }} tr:last-child td {{ border-bottom:0 }} .test-title {{ font-weight:650 }} .test-id {{ font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted) }} .time {{ font-variant-numeric:tabular-nums; white-space:nowrap }}
    .provenance {{ display:grid; grid-template-columns:repeat(3,1fr); gap:1px; background:var(--line); padding:1px; border-radius:14px; overflow:hidden }} .prov {{ background:var(--panel); padding:18px }} .prov span {{ display:block; color:var(--muted); font-size:12px }} .prov code {{ display:block; margin-top:5px; overflow-wrap:anywhere; font-size:12px; color:#d9ece5 }}
    .growth {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px }} .growth-card {{ padding:18px; border:1px solid var(--line); background:#0b1714; border-radius:13px }} .growth-card strong {{ display:block; font-size:28px }} .growth-card span {{ color:var(--muted); font-size:12px }}
    .demo {{ width:100%; display:block; border-radius:13px; border:1px solid var(--line); background:#07100e }}
    .note {{ margin-top:15px; color:var(--muted); font-size:13px }} footer {{ margin-top:46px; padding-top:22px; border-top:1px solid var(--line); display:flex; justify-content:space-between; color:var(--muted); font-size:13px }}
    @media(max-width:1000px) {{ .grid6 {{ grid-template-columns:repeat(3,1fr) }} }}
    @media(max-width:800px) {{ .hero {{ grid-template-columns:1fr }} .seal {{ margin:18px auto 0 }} .grid4,.growth {{ grid-template-columns:repeat(2,1fr) }} .checks,.domains,.comparison {{ grid-template-columns:1fr }} .provenance {{ grid-template-columns:1fr }} .navlinks {{ display:none }} .benchmark-head {{ display:block }} .long-pill {{ margin-top:14px }} }}
    @media(max-width:520px) {{ .grid4 {{ grid-template-columns:1fr 1fr }} .wrap {{ width:min(100% - 22px,1180px); padding-top:24px }} .panel {{ padding:14px }} th:nth-child(2),td:nth-child(2) {{ display:none }} }}
  </style>
</head>
<body>
<main class="wrap">
  <nav class="nav"><div class="brand">ATOMLANE / VERIFY</div><div class="navlinks"><a href="https://github.com/cloudguo123/atomlane">Source</a><a href="https://github.com/cloudguo123/atomlane#install-in-two-commands">Install</a><a href="https://github.com/cloudguo123/atomlane/issues/new?template=first-run.yml">First run</a><a href="https://github.com/cloudguo123/atomlane/discussions">Discuss</a><a href="test-results.json">Raw JSON</a></div></nav>
  <section class="hero">
    <div><div class="eyebrow">AtomLane release verification · v<span id="version"></span></div><h1>Parallelize only what is proven safe.</h1><p class="lede">AtomLane finds reviewable Python refactors without executing target code, compiles local work into verified atomic plans, and {html.escape(hero_platform_claim)} with visible progress and honest time-savings evidence.</p><div class="actions"><a class="button" href="https://github.com/cloudguo123/atomlane#install-in-two-commands">Install free</a><a class="button secondary" href="https://github.com/cloudguo123/atomlane/issues/new?template=first-run.yml">Report first run</a><a class="button secondary" href="https://github.com/cloudguo123/atomlane/issues/new?template=benchmark.yml">Share a benchmark</a></div></div>
    <div class="seal" id="seal"><div class="seal-inner"><div class="rate" id="rate">—</div><div class="seal-label">tests passing</div></div></div>
  </section>
  <div class="grid4" id="metrics"></div>
  <div class="section-title"><div><div class="eyebrow">Visible by default</div><h2>Watch the work while it runs</h2></div><p>20-second deterministic demo</p></div>
  <section class="panel"><img class="demo" src="share/demo.gif" width="960" height="540" alt="Live execution counters and estimated time saved updating during a parallel run"><p class="note">The live runner streams elapsed time, running/ready/completed/failed counters, and current savings throughout long execution. Windows uses UTF-8 pipes by default and optional output-only ConPTY for programs needing terminal-shaped output; explicit ConPTY stdin fails before target creation in this Preview. Captured stdout/stderr is returned with the completed result; ConPTY does not turn task output into a live UI stream.</p></section>
  <div class="section-title"><div><div class="eyebrow">Cross-platform release gate</div><h2>Native Windows Preview evidence</h2></div><p>Job Object · ConPTY · PowerShell · UTF-8</p></div>
  <section class="panel" id="windows-preview"></section>
  <div class="section-title"><div><div class="eyebrow">Native Windows performance evidence</div><h2>Windows five-minute benchmark</h2></div><p>Observed durations · separate cumulative history</p></div>
  <section class="panel benchmark" id="windows-benchmark"></section>
  <div class="section-title"><div><div class="eyebrow">Long-horizon evidence</div><h2>Five-minute parallel benchmark</h2></div><p>Fast regression report retained below</p></div>
  <section class="panel benchmark" id="benchmark"></section>
  <div class="section-title"><div><div class="eyebrow">Program-level optimization</div><h2>Python refactor safety evidence</h2></div><p>Static fixtures · no target execution</p></div>
  <section class="panel" id="python-advisor"></section>
  <div class="section-title"><div><div class="eyebrow">Quality gates</div><h2>Release checks</h2></div><p id="overall">{overall}</p></div>
  <section class="checks" id="checks"></section>
  <div class="section-title"><div><div class="eyebrow">Behavioral coverage</div><h2>Host regression subsystems</h2></div><p>Portable and host-local checks grouped by responsibility</p></div>
  <section class="domains" id="domains"></section>
  <div class="section-title"><div><div class="eyebrow">Test inventory</div><h2>All regression cases</h2></div><p id="test-count"></p></div>
  <section class="panel"><div class="toolbar"><input id="search" type="search" placeholder="Filter by test, subsystem, or behavior…" aria-label="Filter tests"></div><div style="overflow:auto"><table><thead><tr><th>Behavior</th><th>Subsystem</th><th>Status</th><th>Duration</th></tr></thead><tbody id="tests"></tbody></table></div></section>
  <div class="section-title"><div><div class="eyebrow">Reproducibility</div><h2>Build provenance</h2></div><p>Machine-readable evidence included</p></div>
  <section class="provenance" id="provenance"></section>
  <div class="section-title"><div><div class="eyebrow">Open growth</div><h2>Community pulse</h2></div><p>Aggregate GitHub signals only</p></div>
  <section class="panel"><div class="growth" id="growth"></div><p class="note" id="growth-note"></p></section>
  <p class="note" id="scope"></p>
  <footer><span>Generated {generated}</span><span>Report schema 1.0</span></footer>
</main>
<script id="report-data" type="application/json">{data}</script>
<script>
const d=JSON.parse(document.getElementById('report-data').textContent);
const q=s=>document.querySelector(s);
const E=(tag,className='',value=null)=>{{const node=document.createElement(tag);if(className)node.className=className;if(value!==null&&value!==undefined)node.textContent=String(value);return node;}};
const add=(parent,...children)=>{{parent.append(...children.filter(Boolean));return parent;}};
const clear=(selector,...children)=>{{const node=q(selector);node.replaceChildren(...children);return node;}};
const finite=(value,fallback=0)=>{{const number=Number(value);return Number.isFinite(number)?number:fallback;}};
const pct=value=>Math.max(0,Math.min(100,finite(value))).toFixed(3)+'%';
const ms=n=>finite(n)>=1000?(finite(n)/1000).toFixed(2)+'s':Math.round(finite(n))+'ms';
const span=s=>finite(s)>=3600?(finite(s)/3600).toFixed(2)+'h':finite(s)>=60?(finite(s)/60).toFixed(2)+'m':finite(s).toFixed(1)+'s';
const pillClass=status=>status==='failed'||status==='skipped'?'pill '+status:'pill';
const valueCard=(className,label,value)=>add(E('div',className),E('strong','',value),E('span','',label));
const compareRow=(label,width,value,parallel=false)=>{{const fill=E('i',parallel?'compare-fill parallel':'compare-fill');fill.style.width=pct(width);return add(E('div','compare-row'),E('span','',label),add(E('div','compare-track'),fill),E('strong','',value));}};

q('#version').textContent=String(d.version);
q('#rate').textContent=finite(d.summary.pass_rate).toFixed(1)+'%';
q('#seal').style.setProperty('--rate',String(Math.max(0,Math.min(100,finite(d.summary.pass_rate)))));
q('#overall').textContent=d.overall==='passed'?'ALL REQUIRED GATES PASSED':'VERIFICATION FAILED';
q('#overall').style.color=d.overall==='passed'?'var(--green)':'var(--red)';

const metrics=[['Tests',d.summary.total],['Passed',d.summary.passed],['Failed / errors',finite(d.summary.failed)+' / '+finite(d.summary.errors)],['Skipped',finite(d.summary.skipped)],['Release gates',d.summary.checks_passed+'/'+d.summary.checks_total],['Total wall time',ms(d.summary.elapsed_ms)]];
clear('#metrics',...metrics.map(item=>valueCard('metric',item[0],item[1])));

const wp=d.windows_preview||{{available:false,status:'awaiting_native_ci'}};
if(!wp.available){{
  clear('#windows-preview',add(E('div','benchmark-head'),add(E('div'),E('h3','','Native Windows evidence pending'),E('p','','The Preview is not labeled verified until the release commit passes the native Windows matrix and its machine-readable artifact is retained here.')),E('span','long-pill',String(wp.status).replaceAll('_',' '))));
}}else{{
  const nt=wp.native_runtime||{{}},all=wp.tests||{{}},gates=wp.release_checks||{{}};
  const windowsMetrics=[['Native runtime',finite(nt.passed)+' / '+finite(nt.total)],['Native skips',finite(nt.skipped)],['Full suite',finite(all.passed)+' / '+finite(all.total)],['Failures / errors',finite(all.failed)+' / '+finite(all.errors)],['Release gates',finite(gates.passed)+' / '+finite(gates.total)],['Python',wp.python]];
  const heading=add(E('div','benchmark-head'),add(E('div'),E('div','eyebrow',String(wp.status)+' · '+String(wp.os)),E('h3','','In-Job execution verified on native Windows'),E('p','','The retained runner evidence covers the client and normally inherited in-Job descendants, with supervisor-plus-target resource budgets, UTF-8 MCP transport, pre-completion lifecycle progress, optional ConPTY, PowerShell file atoms, and startup-injection isolation. Brokers outside the Job—including WSL, Docker, WMI, services, and scheduled tasks—remain separate execution realms.')),E('span','long-pill','Windows Preview '+String(wp.version)));
  const link=E('a','','Open raw Windows evidence');link.href='windows-preview-results.json';link.rel='noopener';
  clear('#windows-preview',heading,add(E('div','grid6'),...windowsMetrics.map(item=>valueCard('bench-metric',item[0],item[1]))),add(E('p','note'),'Runner '+String(wp.runner)+' · '+String(wp.architecture)+' · commit '+String(wp.commit).slice(0,10)+' · evidence SHA-256 '+String(wp.evidence_sha256)+'. ',link));
}}

const wb=d.windows_benchmark||{{available:false,status:'awaiting_native_benchmark'}};
if(!wb.available){{
  clear('#windows-benchmark',add(E('div','benchmark-head'),add(E('div'),E('h3','','Native Windows five-minute evidence pending'),E('p','','This panel stays unverified until four independent workloads each complete at least 300 observed seconds on a source-bound native Windows runner, with pre-completion progress evidence.')),E('span','long-pill',String(wb.status).replaceAll('_',' '))));
}}else{{
  const x=wb.latest||{{}},c=wb.cumulative||{{}},tasks=Array.isArray(x.tasks)?x.tasks:[],p=x.platform||{{}},observed=finite(x.observed_minimum_task_seconds,tasks.length?Math.min(...tasks.map(task=>finite(task.duration_seconds))):0);
  const metrics=[['Observed minimum',span(observed)],['Parallel wall time',span((x.parallel||{{}}).wall_time_seconds)],['Serial equivalent',span((x.serial_equivalent||{{}}).seconds)],['Saved this run',span((x.savings||{{}}).seconds)],['Windows cumulative saved',span(c.saved_seconds)],['Observed speedup',finite((x.savings||{{}}).speedup_multiplier).toFixed(2)+'×']];
  const heading=add(E('div','benchmark-head'),add(E('div'),E('div','eyebrow',String(x.status)+' · '+String(p.execution_realm)),E('h3','','Four native Windows workloads met or exceeded five observed minutes'),E('p','','Mac and Windows histories are kept separate. Serial-equivalent time is the sum of observed independent task runtimes; savings subtract actual parallel wall time.')),E('span','long-pill','Observed ≥ '+span(observed)));
  const lanes=E('div','lanes'),target=Math.max(finite(x.target_task_seconds),1);
  tasks.forEach(task=>{{const fill=E('i','lane-fill',String(task.status).toUpperCase());fill.style.width=pct(finite(task.duration_seconds)/target*100);lanes.append(add(E('div','lane'),E('span','',task.label),add(E('div','lane-track'),fill),E('strong','',span(task.duration_seconds))));}});
  const raw=E('a','','Open raw Windows benchmark');raw.href='windows-benchmark-results.json';raw.rel='noopener';
  clear('#windows-benchmark',heading,add(E('div','grid6'),...metrics.map(item=>valueCard('bench-metric',item[0],item[1]))),E('h4','','Concurrent task timeline'),lanes,add(E('p','note'),'Runner '+String(p.runner_name||'unknown')+' · '+String(p.os_version||p.system||'Windows')+' · '+String(p.architecture||'unknown')+' · commit '+String(x.commit).slice(0,10)+' · evidence SHA-256 '+String(wb.evidence_sha256)+'. ',raw));
}}

const b=d.benchmark;
if(!b.available){{
  const copy=add(E('div'),E('h3','','First long benchmark pending'),E('p','','The five-minute evidence run is independent from fast CI and will appear here after its first successful execution.'));
  clear('#benchmark',add(E('div','benchmark-head'),copy,E('span','long-pill','Scheduled benchmark')));
}}else{{
  const x=b.latest||{{}},c=b.cumulative||{{}},serial=finite((x.serial_equivalent||{{}}).seconds),wall=finite((x.parallel||{{}}).wall_time_seconds),tasks=Array.isArray(x.tasks)?x.tasks:[],observedMinimum=tasks.length?Math.min(...tasks.map(task=>finite(task.duration_seconds))):0;
  const allTasksSucceeded=tasks.length===finite(x.task_count)&&tasks.every(task=>task.status==='succeeded');
  const longEnough=x.status==='passed'&&x.minimum_duration_met===true&&allTasksSucceeded&&observedMinimum>=300;
  const xRealm=String((x.platform||{{}}).execution_realm||'legacy_macos');
  const historyRows=(Array.isArray(b.history)?b.history:[]).filter(row=>String(row.execution_realm||xRealm)===xRealm&&row.status==='passed'),maxSaved=Math.max(...historyRows.map(row=>finite(row.saved_seconds)),1);
  const headerCopy=add(E('div'),E('div','eyebrow',String(x.status)+' · '+String(x.task_count)+' independent workloads'),E('h3','',longEnough?'Every observed task ran for at least five minutes':'Five-minute evidence gate not met'),E('p','','Observed task runtimes are summed for the serial equivalent; no synthetic multiplier and no 20-minute serial rerun. Savings equal that observed work minus actual parallel wall time.'));
  const header=add(E('div','benchmark-head'),headerCopy,E('span','long-pill','Minimum gate '+span(x.minimum_task_seconds)));
  const benchmarkMetrics=[['Observed minimum',span(observedMinimum)],['Required gate',span(x.minimum_task_seconds)],['Parallel wall time',span(wall)],['Serial equivalent',span(serial)],['Saved this run',span(x.savings.seconds)],['Cumulative saved',span(c.saved_seconds)],['Observed speedup',finite(x.savings.speedup_multiplier).toFixed(2)+'×']];
  const metricGrid=add(E('div','grid6'),...benchmarkMetrics.map(item=>valueCard('bench-metric',item[0],item[1])));
  const comparisonLeft=add(E('div'),E('h4','','Serial-equivalent vs parallel'),compareRow('Serial equivalent',100,span(serial)),compareRow('Parallel',serial>0?wall/serial*100:0,span(wall),true),E('p','note',finite(x.savings.percent).toFixed(2)+'% less wall time · '+(finite(x.savings.parallel_efficiency)*100).toFixed(1)+'% parallel efficiency · peak '+String(x.parallel.peak_concurrency)+' workers'));
  const history=E('div','history');
  historyRows.forEach(row=>{{const bar=E('i','history-bar');bar.style.height=Math.max(12,finite(row.saved_seconds)/maxSaved*76).toFixed(3)+'px';bar.dataset.label=span(row.saved_seconds)+' saved';history.append(bar);}});
  const comparisonRight=add(E('div'),E('h4','','Cumulative savings history'),history,E('p','note',String(c.run_count)+' verified run'+(c.run_count===1?'':'s')+' · '+span(c.saved_seconds)+' saved in total'));
  const comparison=add(E('div','comparison'),comparisonLeft,comparisonRight);
  const lanes=E('div','lanes'),targetSeconds=Math.max(finite(x.target_task_seconds),1);
  tasks.forEach(task=>{{const fill=E('i','lane-fill',String(task.status).toUpperCase());fill.style.width=pct(finite(task.duration_seconds)/targetSeconds*100);lanes.append(add(E('div','lane'),E('span','',task.label),add(E('div','lane-track'),fill),E('strong','',span(task.duration_seconds))));}});
  const resource=x.resource||{{}};
  const methodNote='Method: '+String(x.method)+'. Latest run '+String(x.run_id)+' on '+String(resource.machine)+' ('+String(resource.logical_cpus)+' logical CPUs). Source commit '+String(x.commit).slice(0,10)+'.';
  clear('#benchmark',header,metricGrid,comparison,E('h4','','Concurrent task timeline'),lanes,E('p','note',methodNote));
}}

const pa=d.python_advisor||{{available:false}};
if(!pa.available){{
  clear('#python-advisor',E('p','note','Python advisor evidence is unavailable for this report.'));
}}else{{
  const counts=pa.classification_counts||{{}};
  const advisorMetrics=[
    ['Files analyzed',pa.files_analyzed],
    ['Candidates',pa.candidate_count],
    ['Reviewable rewrites',counts.reviewable_rewrite||0],
    ['Advisory only',counts.advisory_only||0],
    ['Blocked safely',counts.blocked||0],
    ['Prefer native',counts.prefer_native||0]
  ];
  const contract='Execution performed: '+String(pa.execution_performed)+' · files modified: '+String(pa.files_modified)+' · fixture SHA-256 unchanged: '+String(pa.fixture_hashes_unchanged)+' · execution marker absent: '+String(pa.execution_marker_absent)+' · source-hash-bound preview: '+String(pa.source_hash_bound)+' · benefit label: '+String(pa.benefit_kind)+'.';
  clear('#python-advisor',add(E('div','benchmark-head'),add(E('div'),E('h3','','Bounded AST analysis passes every public safety fixture'),E('p','','The fixture matrix separates pure CPU maps, read-only I/O, shared state, and native-library work before any rewrite is considered.')),E('span','long-pill',String(pa.analysis_mode))),add(E('div','grid6'),...advisorMetrics.map(item=>valueCard('bench-metric',item[0],item[1]))),E('p','note',contract+' Analysis '+String(pa.analysis_hash)+'.'));
}}

const maxCheck=Math.max(...d.checks.map(check=>finite(check.duration_ms)),1);
clear('#checks',...d.checks.map(check=>{{const top=add(E('div','checktop'),E('strong','',check.name),E('span',pillClass(check.status),check.status));const fill=E('i');fill.style.width=pct(Math.max(2,finite(check.duration_ms)/maxCheck*100));return add(E('article','check'),top,add(E('div','bar'),fill),E('small','',ms(check.duration_ms)));}}));

clear('#domains',...d.domains.map(domain=>{{const article=E('article','panel domain');const accent=/^#[0-9A-Fa-f]{{6}}$/.test(String(domain.accent))?String(domain.accent):'#65e6b4';article.style.setProperty('--accent',accent);const foot=add(E('div','domain-foot'),E('strong','',String(domain.passed)+' / '+String(domain.total)+' passed'),E('span','',ms(domain.duration_ms)));return add(article,E('h3','',domain.label),E('p','',domain.description),foot);}}));

function draw(list){{
  q('#test-count').textContent=String(list.length)+' of '+String(d.tests.length)+' shown';
  const rows=list.map(test=>{{const identity=add(E('td'),E('div','test-title',test.title),E('div','test-id',String(test.class)+'.'+String(test.name)));return add(E('tr'),identity,E('td','',test.domain),add(E('td'),E('span',pillClass(test.status),test.status)),E('td','time',ms(test.duration_ms)));}});
  if(!rows.length){{const empty=E('td','','No matching tests.');empty.colSpan=4;rows.push(add(E('tr'),empty));}}
  clear('#tests',...rows);
}}
draw(d.tests);
q('#search').addEventListener('input',event=>{{const term=String(event.target.value).toLowerCase();draw(d.tests.filter(test=>Object.values(test).join(' ').toLowerCase().includes(term)));}});

const provenance=[['Verified commit',d.source.commit],['Source tree',d.source.clean===true?'clean':'dirty / unknown'],['Branch',d.source.branch],['Runner',d.environment.runner],['Platform',d.environment.os+' · '+d.environment.architecture],['Toolchain','Python '+d.environment.python+' · Node '+d.environment.node],['UI bundle SHA-256',d.provenance.bundle_sha256]];
clear('#provenance',...provenance.map(item=>add(E('div','prov'),E('span','',item[0]),E('code','',item[1]))));
q('#scope').textContent=String(d.scope_note);

const g=d.growth;
if(!g.available){{
  clear('#growth',valueCard('growth-card','First aggregate snapshot','Pending'));
  q('#growth-note').textContent='The weekly metrics workflow will publish stars, forks, release downloads, public first-run reports, and GitHub’s rolling 14-day traffic counters.';
}}else{{
  const latest=g.latest,traffic=latest.traffic_14d||{{}},growthMetrics=[['Stars',latest.stars],['Forks',latest.forks],['14d unique visitors',traffic.unique_visitors??'—'],['14d unique cloners',traffic.unique_cloners??'—'],['Release downloads',latest.release_asset_downloads],['First-run reports',latest.first_run_reports??'—'],['Benchmark reports',latest.benchmark_reports??'—'],['30d first-run target',g.targets_30d.first_run_reports??20]];
  clear('#growth',...growthMetrics.map(item=>valueCard('growth-card',item[0],item[1])));
  const stale=latest.traffic_stale_from?' Traffic last authenticated '+String(latest.traffic_stale_from)+'.':'';
  q('#growth-note').textContent='Captured '+String(latest.captured_at)+'.'+stale+' Traffic is a rolling 14-day GitHub window and may lag. Clones, downloads, and self-selected public reports indicate intent—not verified installations.';
}}
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, default=ROOT / "docs" / "index.html")
    parser.add_argument(
        "--json-output",
        type=pathlib.Path,
        default=ROOT / "docs" / "test-results.json",
    )
    validation = parser.add_mutually_exclusive_group()
    validation.add_argument(
        "--validate-macos-benchmark-evidence",
        type=pathlib.Path,
        help="validate one downloaded native-macOS benchmark artifact and exit",
    )
    validation.add_argument(
        "--validate-windows-preview-evidence",
        type=pathlib.Path,
        help="validate one downloaded native-Windows CI artifact and exit",
    )
    validation.add_argument(
        "--validate-windows-benchmark-evidence",
        type=pathlib.Path,
        help="validate one downloaded native-Windows benchmark artifact and exit",
    )
    parser.add_argument(
        "--expected-commit",
        help="exact source commit required by artifact validation modes",
    )
    args = parser.parse_args()
    validation_mode = next(
        (
            (name, path)
            for name, path in (
                ("macos_benchmark", args.validate_macos_benchmark_evidence),
                ("windows_preview", args.validate_windows_preview_evidence),
                ("windows_benchmark", args.validate_windows_benchmark_evidence),
            )
            if path is not None
        ),
        None,
    )
    if validation_mode is not None:
        if args.expected_commit is None:
            parser.error("--expected-commit is required with artifact validation")
        mode, evidence_path = validation_mode
        if mode == "windows_preview":
            evidence = load_windows_preview_evidence(
                evidence_path,
                expected_commit=args.expected_commit,
            )
        elif mode == "windows_benchmark":
            evidence = load_windows_benchmark_evidence(
                evidence_path,
                expected_commit=args.expected_commit,
            )
        else:
            try:
                benchmark = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                evidence = {
                    "available": False,
                    "status": "invalid_benchmark_evidence",
                }
            else:
                evidence = validate_macos_benchmark_evidence(
                    benchmark,
                    expected_commit=args.expected_commit,
                )
        evidence_status = evidence.get("status")
        if not isinstance(evidence_status, str):
            latest = evidence.get("latest")
            if isinstance(latest, dict):
                evidence_status = latest.get("status")
        if not isinstance(evidence_status, str):
            evidence_status = (
                "passed"
                if evidence.get("available") is True
                else "invalid_benchmark_evidence"
            )
        print(
            json.dumps(
                {
                    "available": evidence.get("available", False),
                    "status": evidence_status,
                    "source_match": evidence.get("source_match", False),
                },
                separators=(",", ":"),
            )
        )
        return 0 if evidence.get("available") is True else 1
    if args.expected_commit is not None:
        parser.error("--expected-commit requires an artifact validation mode")
    report = build_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_html(report), encoding="utf-8")
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(ROOT / "benchmarks" / "project-result.schema.json", ROOT / "docs")
    shutil.copy2(ROOT / "benchmarks" / "external-results.json", ROOT / "docs")
    print(
        json.dumps(
            {
                "overall": report["overall"],
                "tests": report["summary"]["total"],
                "passed": report["summary"]["passed"],
                "output": str(args.output),
            }
        )
    )
    return 0 if report["overall"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
