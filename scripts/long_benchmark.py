#!/usr/bin/env python3
"""Run a controlled five-minute parallel benchmark and preserve public evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import pathlib
import platform
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

import mcp_server

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = pathlib.Path(__file__).resolve()
DEFAULT_OUTPUT = ROOT / "docs" / "benchmark-results.json"
SCENARIOS = {
    "artifact-hash": "Artifact hashing and integrity verification",
    "planner-json": "Atom-plan serialization and validation",
    "isolated-io": "Isolated snapshot write/read verification",
    "scheduler-sim": "Capacity-aware scheduler simulation",
}


def _worker_operation(scenario: str, scratch: pathlib.Path, seed: int) -> int:
    randomizer = random.Random(seed)
    if scenario == "artifact-hash":
        payload = bytes(randomizer.randrange(256) for _ in range(64 * 1024))
        for _ in range(8):
            hashlib.sha256(payload).digest()
        return len(payload) * 8
    if scenario == "planner-json":
        atoms = [
            {
                "id": f"atom-{index}",
                "claims": {"cpu": 0.1, "memory_mb": 32},
                "depends_on": [f"atom-{index - 1}"] if index else [],
            }
            for index in range(96)
        ]
        encoded = json.dumps(atoms, separators=(",", ":"), sort_keys=True)
        json.loads(encoded)
        return len(atoms)
    if scenario == "isolated-io":
        payload = bytes(randomizer.randrange(256) for _ in range(32 * 1024))
        target = scratch / "snapshot.bin"
        target.write_bytes(payload)
        verified = target.read_bytes()
        if hashlib.sha256(payload).digest() != hashlib.sha256(verified).digest():
            raise RuntimeError("isolated snapshot verification failed")
        return len(payload) * 2
    if scenario == "scheduler-sim":
        candidates = [
            (randomizer.random() * 20, randomizer.randrange(1, 5), index)
            for index in range(512)
        ]
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        admitted = [item for item in candidates if item[1] <= 2][:64]
        return len(admitted)
    raise ValueError(f"unknown benchmark scenario: {scenario}")


def run_worker(scenario: str, duration_seconds: float, heartbeat_seconds: float, seed: int) -> int:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")
    if duration_seconds <= 0 or heartbeat_seconds <= 0:
        raise ValueError("worker durations must be positive")
    started = time.monotonic()
    deadline = started + duration_seconds
    next_heartbeat = started
    iterations = 0
    work_units = 0
    with tempfile.TemporaryDirectory(prefix=f"mpa-benchmark-{scenario}-") as temp_dir:
        scratch = pathlib.Path(temp_dir)
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            work_units += _worker_operation(scenario, scratch, seed + iterations)
            iterations += 1
            now = time.monotonic()
            if now >= next_heartbeat:
                print(
                    json.dumps(
                        {
                            "event": "worker_progress",
                            "scenario": scenario,
                            "elapsed_seconds": round(now - started, 2),
                            "iterations": iterations,
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                next_heartbeat = now + heartbeat_seconds
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    elapsed = time.monotonic() - started
    print(
        json.dumps(
            {
                "event": "worker_complete",
                "scenario": scenario,
                "label": SCENARIOS[scenario],
                "elapsed_seconds": round(elapsed, 6),
                "iterations": iterations,
                "work_units": work_units,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    return 0


class ProgressCapture:
    def __init__(self, interval_seconds: float = 15.0) -> None:
        self.interval_seconds = interval_seconds
        self.next_emit = 0.0
        self.last_signature: tuple[int, int, int] | None = None
        self.samples: list[dict[str, Any]] = []

    def __call__(self, snapshot: dict[str, Any]) -> None:
        elapsed = float(snapshot["elapsed_seconds"])
        signature = (
            int(snapshot["running_tasks"]),
            int(snapshot["completed_tasks"]),
            int(snapshot["failed_tasks"]),
        )
        if elapsed < self.next_emit and signature == self.last_signature:
            return
        sample = {
            "elapsed_seconds": round(elapsed, 3),
            "running_tasks": signature[0],
            "completed_tasks": signature[1],
            "failed_tasks": signature[2],
            "estimated_saved_seconds": float(snapshot["estimated_saved_so_far_seconds"]),
            "savings_eligible": bool(snapshot.get("savings_eligible_so_far", True)),
        }
        self.samples.append(sample)
        self.next_emit = elapsed + self.interval_seconds
        self.last_signature = signature
        print(
            "BENCHMARK_PROGRESS "
            f"elapsed={elapsed:.1f}s running={signature[0]} "
            f"completed={signature[1]}/4 saved_so_far={sample['estimated_saved_seconds']:.1f}s "
            f"savings_eligible={str(sample['savings_eligible']).lower()}",
            flush=True,
        )


def _last_json_line(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("event") == "worker_complete":
            return value
    return {}


def _github_run_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not server or not repository or not run_id:
        return None
    return f"{server.rstrip('/')}/{repository}/actions/runs/{run_id}"


def _platform_evidence() -> dict[str, Any]:
    environment = mcp_server.execution_environment()
    architecture = platform.machine().lower() or "unknown"
    release = platform.release()
    runner_environment = os.environ.get("RUNNER_ENVIRONMENT", "").casefold()
    if runner_environment in {"github-hosted", "self-hosted"}:
        runner_name = f"github-actions-{runner_environment}"
    elif os.environ.get("GITHUB_ACTIONS", "").casefold() == "true":
        runner_name = "github-actions"
    else:
        runner_name = "local"
    run_url = _github_run_url()
    return {
        "platform": {
            "system": environment["system"],
            "release": release,
            "os_version": f"{environment['system']} {release}",
            "architecture": architecture,
            "execution_realm": environment["boundary"],
            "runner_name": runner_name,
            "run_url": run_url,
        },
        "runner": {
            "name": runner_name,
            "environment": runner_environment or "local",
            "os": os.environ.get("RUNNER_OS") or environment["system"],
            "architecture": os.environ.get("RUNNER_ARCH") or architecture,
        },
        "run_url": run_url,
    }


def _safe_realm(value: Any, fallback: str = "unknown") -> str:
    if not isinstance(value, str) or not value:
        return fallback
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-.")
    return normalized or fallback


def _record_realm(record: dict[str, Any], fallback: str = "unknown") -> str:
    platform_record = record.get("platform")
    if isinstance(platform_record, dict):
        realm = platform_record.get("execution_realm")
        if isinstance(realm, str) and realm:
            return _safe_realm(realm, fallback)
        system = str(platform_record.get("system", "")).lower()
        if system == "windows":
            return "windows_native"
        if system in {"darwin", "macos"}:
            return "macos_native"
        if system == "linux":
            return "linux_native"
    direct = record.get("execution_realm")
    if isinstance(direct, str) and direct:
        return _safe_realm(direct, fallback)
    resource = record.get("resource")
    machine = str(resource.get("machine", "") if isinstance(resource, dict) else "").lower()
    if "apple" in machine or "mac" in machine:
        return "macos_native"
    if "windows" in machine:
        return "windows_native"
    return _safe_realm(fallback)


def _realm_run_id(run_id: str, realm: str) -> str:
    suffix = f"@{realm}"
    return run_id if run_id.endswith(suffix) else f"{run_id}{suffix}"


def summarize_run(
    execution: dict[str, Any],
    progress_samples: list[dict[str, Any]],
    *,
    minimum_task_seconds: float,
    target_task_seconds: float,
    commit: str,
    run_id: str,
    environment_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_rows = []
    duration_tolerance_seconds = max(2.0, target_task_seconds * 0.02)
    for result in execution["results"]:
        worker = _last_json_line(str(result.get("stdout", "")))
        task_id = result.get("id")
        evidence_errors: list[str] = []
        if not worker:
            evidence_errors.append("missing_worker_complete")
        scenario = worker.get("scenario")
        if scenario != task_id or scenario not in SCENARIOS:
            evidence_errors.append("worker_scenario_mismatch")
        if worker.get("label") != SCENARIOS.get(str(task_id)):
            evidence_errors.append("worker_label_mismatch")

        outer_duration_raw = result.get("duration_seconds")
        worker_duration_raw = worker.get("elapsed_seconds")
        try:
            outer_duration = float(outer_duration_raw)
        except (TypeError, ValueError):
            outer_duration = 0.0
            evidence_errors.append("invalid_outer_duration")
        if not math.isfinite(outer_duration) or outer_duration <= 0:
            outer_duration = 0.0
            if "invalid_outer_duration" not in evidence_errors:
                evidence_errors.append("invalid_outer_duration")
        try:
            worker_duration = float(worker_duration_raw)
        except (TypeError, ValueError):
            worker_duration = 0.0
            evidence_errors.append("invalid_worker_duration")
        if not math.isfinite(worker_duration) or worker_duration <= 0:
            worker_duration = 0.0
            if "invalid_worker_duration" not in evidence_errors:
                evidence_errors.append("invalid_worker_duration")
        duration_drift = abs(outer_duration - worker_duration)
        if duration_drift > duration_tolerance_seconds:
            evidence_errors.append("worker_outer_duration_mismatch")

        iterations_raw = worker.get("iterations")
        work_units_raw = worker.get("work_units")
        iterations = (
            iterations_raw
            if isinstance(iterations_raw, int)
            and not isinstance(iterations_raw, bool)
            and iterations_raw > 0
            else 0
        )
        work_units = (
            work_units_raw
            if isinstance(work_units_raw, int)
            and not isinstance(work_units_raw, bool)
            and work_units_raw > 0
            else 0
        )
        if iterations == 0:
            evidence_errors.append("invalid_worker_iterations")
        if work_units == 0:
            evidence_errors.append("invalid_worker_work_units")
        worker_evidence_valid = not evidence_errors
        task_rows.append(
            {
                "id": task_id,
                "scenario": scenario if isinstance(scenario, str) else task_id,
                "label": worker.get("label", SCENARIOS.get(str(task_id), str(task_id))),
                "status": result["status"],
                "duration_seconds": round(worker_duration, 6),
                "outer_duration_seconds": round(outer_duration, 6),
                "duration_drift_seconds": round(duration_drift, 6),
                "worker_evidence_valid": worker_evidence_valid,
                "worker_evidence_errors": evidence_errors,
                "iterations": iterations,
                "work_units": work_units,
            }
        )
    serial_seconds = sum(task["duration_seconds"] for task in task_rows)
    wall_seconds = float(execution["summary"]["elapsed_seconds"])
    saved_seconds = max(0.0, serial_seconds - wall_seconds)
    peak = int(execution["summary"]["peak_concurrency"])
    speedup = serial_seconds / wall_seconds if wall_seconds else 0.0
    efficiency = speedup / peak if peak else 0.0
    observed_minimum = min(
        (task["duration_seconds"] for task in task_rows),
        default=0.0,
    )
    expected_ids = set(SCENARIOS)
    all_present = len(task_rows) == len(expected_ids) and {
        task["id"] for task in task_rows
    } == expected_ids
    all_succeeded = all_present and all(
        task["status"] == "succeeded" for task in task_rows
    )
    worker_evidence_complete = all_present and all(
        task["worker_evidence_valid"] for task in task_rows
    )
    minimum_met = (
        worker_evidence_complete and observed_minimum >= minimum_task_seconds
    )
    full_parallelism_observed = all_present and peak == len(expected_ids)
    evidence = dict(environment_evidence or _platform_evidence())
    platform_record = evidence.get("platform")
    if not isinstance(platform_record, dict):
        raise TypeError("environment evidence must contain a platform object")
    realm = _record_realm({"platform": platform_record})
    return {
        "run_id": _realm_run_id(run_id, realm),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": commit,
        "status": (
            "passed"
            if all_succeeded
            and worker_evidence_complete
            and minimum_met
            and full_parallelism_observed
            else "failed"
        ),
        "method": "controlled-duration low-load parallel execution",
        "minimum_task_seconds": minimum_task_seconds,
        "target_task_seconds": target_task_seconds,
        "observed_minimum_task_seconds": round(observed_minimum, 6),
        "minimum_duration_met": minimum_met,
        "worker_evidence_complete": worker_evidence_complete,
        "worker_duration_tolerance_seconds": round(duration_tolerance_seconds, 6),
        "full_parallelism_observed": full_parallelism_observed,
        "task_count": len(task_rows),
        "tasks": task_rows,
        "parallel": {
            "wall_time_seconds": round(wall_seconds, 6),
            "peak_concurrency": peak,
            "chosen_concurrency": int(execution["summary"]["chosen_concurrency"]),
        },
        "serial_equivalent": {
            "seconds": round(serial_seconds, 6),
            "method": "sum of observed task runtimes; tasks are independent and are not rerun serially",
        },
        "savings": {
            "seconds": round(saved_seconds, 6),
            "percent": round((saved_seconds / serial_seconds * 100) if serial_seconds else 0.0, 4),
            "speedup_multiplier": round(speedup, 4),
            "parallel_efficiency": round(efficiency, 4),
        },
        "resource": {
            "profile": execution["resource_plan"]["profile"],
            "responsiveness": execution["resource_plan"]["responsiveness"],
            "logical_cpus": execution["resource_plan"]["machine"]["logical_cpus"],
            "physical_cpus": execution["resource_plan"]["machine"]["physical_cpus"],
            "machine": execution["resource_plan"]["machine"].get("chip")
            or execution["resource_plan"]["machine"].get("machine"),
        },
        "platform": platform_record,
        "runner": evidence.get("runner"),
        "run_url": evidence.get("run_url"),
        "progress_samples": progress_samples,
    }


def _normalize_latest(record: dict[str, Any], realm: str) -> dict[str, Any]:
    normalized = dict(record)
    normalized["run_id"] = _realm_run_id(str(record.get("run_id", "unknown")), realm)
    platform_record = record.get("platform")
    if isinstance(platform_record, dict):
        normalized["platform"] = {
            **platform_record,
            "execution_realm": realm,
        }
    else:
        normalized["platform"] = {
            "system": "unknown",
            "release": "unknown",
            "architecture": "unknown",
            "execution_realm": realm,
        }
    return normalized


def _normalize_history_item(
    record: dict[str, Any], default_realm: str
) -> tuple[str, dict[str, Any]] | None:
    if record.get("status") != "passed" or not record.get("run_id"):
        return None
    realm = _record_realm(record, default_realm)
    try:
        wall_seconds = float(record["wall_time_seconds"])
        serial_seconds = float(record["serial_equivalent_seconds"])
        saved_seconds = float(record["saved_seconds"])
        speedup = float(record["speedup_multiplier"])
    except (KeyError, TypeError, ValueError):
        return None
    return realm, {
        "run_id": _realm_run_id(str(record["run_id"]), realm),
        "generated_at": record.get("generated_at"),
        "commit": record.get("commit"),
        "status": "passed",
        "execution_realm": realm,
        "runner": record.get("runner"),
        "run_url": record.get("run_url"),
        "wall_time_seconds": wall_seconds,
        "serial_equivalent_seconds": serial_seconds,
        "saved_seconds": saved_seconds,
        "speedup_multiplier": speedup,
    }


def _latest_history_item(latest: dict[str, Any], realm: str) -> dict[str, Any]:
    return {
        "run_id": latest["run_id"],
        "generated_at": latest["generated_at"],
        "commit": latest["commit"],
        "status": "passed",
        "execution_realm": realm,
        "runner": latest.get("runner"),
        "run_url": latest.get("run_url"),
        "wall_time_seconds": latest["parallel"]["wall_time_seconds"],
        "serial_equivalent_seconds": latest["serial_equivalent"]["seconds"],
        "saved_seconds": latest["savings"]["seconds"],
        "speedup_multiplier": latest["savings"]["speedup_multiplier"],
    }


def _verified_cumulative(history: list[dict[str, Any]]) -> dict[str, Any]:
    verified = [item for item in history if item.get("status") == "passed"]
    return {
        "run_count": len(verified),
        "parallel_wall_seconds": round(
            sum(float(item["wall_time_seconds"]) for item in verified), 6
        ),
        "serial_equivalent_seconds": round(
            sum(float(item["serial_equivalent_seconds"]) for item in verified), 6
        ),
        "saved_seconds": round(
            sum(float(item["saved_seconds"]) for item in verified), 6
        ),
    }


def merge_history(previous: dict[str, Any], latest: dict[str, Any]) -> dict[str, Any]:
    current_realm = _record_realm(latest)
    normalized_latest = _normalize_latest(latest, current_realm)
    histories: dict[str, list[dict[str, Any]]] = {}
    latest_by_realm: dict[str, dict[str, Any]] = {}

    raw_realms = previous.get("realms")
    if isinstance(raw_realms, dict) and raw_realms:
        for raw_realm, raw_state in raw_realms.items():
            if not isinstance(raw_state, dict):
                continue
            default_realm = _safe_realm(raw_realm)
            old_latest = raw_state.get("latest")
            if isinstance(old_latest, dict):
                realm = _record_realm(old_latest, default_realm)
                latest_by_realm[realm] = _normalize_latest(old_latest, realm)
            raw_history = raw_state.get("history", [])
            if not isinstance(raw_history, list):
                continue
            for item in raw_history:
                if not isinstance(item, dict):
                    continue
                normalized = _normalize_history_item(item, default_realm)
                if normalized is not None:
                    realm, row = normalized
                    histories.setdefault(realm, []).append(row)
    else:
        old_latest = previous.get("latest")
        legacy_realm = (
            _record_realm(old_latest, current_realm)
            if isinstance(old_latest, dict)
            else current_realm
        )
        if isinstance(old_latest, dict):
            latest_by_realm[legacy_realm] = _normalize_latest(old_latest, legacy_realm)
        raw_history = previous.get("history", [])
        if isinstance(raw_history, list):
            for item in raw_history:
                if not isinstance(item, dict):
                    continue
                normalized = _normalize_history_item(item, legacy_realm)
                if normalized is not None:
                    realm, row = normalized
                    histories.setdefault(realm, []).append(row)

    for realm, rows in histories.items():
        deduplicated: dict[str, dict[str, Any]] = {}
        for row in rows:
            deduplicated[row["run_id"]] = row
        histories[realm] = list(deduplicated.values())

    current_history = histories.setdefault(current_realm, [])
    if normalized_latest.get("status") == "passed":
        current_history[:] = [
            row for row in current_history if row["run_id"] != normalized_latest["run_id"]
        ]
        current_history.append(_latest_history_item(normalized_latest, current_realm))
    latest_by_realm[current_realm] = normalized_latest

    realm_names = sorted(set(histories) | set(latest_by_realm))
    realms = {
        realm: {
            "latest": latest_by_realm.get(realm),
            "history": histories.get(realm, []),
            "cumulative": _verified_cumulative(histories.get(realm, [])),
        }
        for realm in realm_names
    }
    current_state = realms[current_realm]
    return {
        "schema_version": "1.1",
        "aggregation_scope": "execution_realm",
        "latest_realm": current_realm,
        "latest": normalized_latest,
        "history": current_state["history"],
        "cumulative": current_state["cumulative"],
        "realms": realms,
    }


async def run_benchmark(
    duration_seconds: float,
    minimum_task_seconds: float,
    output: pathlib.Path,
) -> dict[str, Any]:
    if duration_seconds < minimum_task_seconds:
        raise ValueError("target duration cannot be shorter than the required minimum")
    if duration_seconds > 600:
        raise ValueError("target duration cannot exceed 600 seconds")
    if minimum_task_seconds < 300:
        print("NOTICE: development run uses a minimum below the public five-minute gate", flush=True)
    tasks = [
        {
            "id": scenario,
            "argv": [
                sys.executable,
                str(SCRIPT),
                "worker",
                "--scenario",
                scenario,
                "--duration-seconds",
                str(duration_seconds),
                "--heartbeat-seconds",
                "30",
                "--seed",
                str(1000 + index),
            ],
            "cwd": str(ROOT),
            "timeout_seconds": duration_seconds + 90,
        }
        for index, scenario in enumerate(SCENARIOS)
    ]
    progress = ProgressCapture()
    with tempfile.TemporaryDirectory(prefix="mpa-long-benchmark-") as temp_dir:
        stats_path = pathlib.Path(temp_dir) / "isolated-stats.json"
        old_stats = os.environ.get("ATOMLANE_STATS_PATH")
        os.environ["ATOMLANE_STATS_PATH"] = str(stats_path)
        try:
            execution = await mcp_server.run_parallel(
                {
                    "tasks": tasks,
                    "default_cwd": str(ROOT),
                    "profile": "io",
                    "responsiveness": "throughput",
                    "reserve_cores": 0,
                    "max_concurrency": 4,
                    "estimated_memory_mb_per_task": 64,
                    "max_output_bytes_per_stream": 16_384,
                },
                progress,
            )
        finally:
            if old_stats is None:
                os.environ.pop("ATOMLANE_STATS_PATH", None)
            else:
                os.environ["ATOMLANE_STATS_PATH"] = old_stats

    commit = os.environ.get("GITHUB_SHA") or _git_commit()
    github_run_id = os.environ.get("GITHUB_RUN_ID")
    if github_run_id:
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT") or "1"
        run_id = f"{github_run_id}-attempt-{attempt}"
    else:
        run_id = f"local-{int(time.time())}"
    latest = summarize_run(
        execution,
        progress.samples,
        minimum_task_seconds=minimum_task_seconds,
        target_task_seconds=duration_seconds,
        commit=commit,
        run_id=run_id,
    )
    try:
        previous = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    except (OSError, json.JSONDecodeError):
        previous = {}
    report = merge_history(previous, latest)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if b"\r" in encoded:
        raise ValueError("benchmark evidence contains a non-canonical carriage return")
    output.write_bytes(encoded)
    print(
        "BENCHMARK_RESULT "
        f"status={latest['status']} wall={latest['parallel']['wall_time_seconds']:.2f}s "
        f"serial_equivalent={latest['serial_equivalent']['seconds']:.2f}s "
        f"saved={latest['savings']['seconds']:.2f}s "
        f"speedup={latest['savings']['speedup_multiplier']:.2f}x "
        f"cumulative_saved={report['cumulative']['saved_seconds']:.2f}s",
        flush=True,
    )
    return report


def _git_commit() -> str:
    import subprocess

    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("worker")
    worker.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    worker.add_argument("--duration-seconds", type=float, required=True)
    worker.add_argument("--heartbeat-seconds", type=float, default=30.0)
    worker.add_argument("--seed", type=int, default=1)
    run = subparsers.add_parser("run")
    run.add_argument("--duration-seconds", type=float, default=310.0)
    run.add_argument("--minimum-task-seconds", type=float, default=300.0)
    run.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "worker":
            return run_worker(args.scenario, args.duration_seconds, args.heartbeat_seconds, args.seed)
        report = asyncio.run(
            run_benchmark(args.duration_seconds, args.minimum_task_seconds, args.output.resolve())
        )
        return 0 if report["latest"]["status"] == "passed" else 1
    except (OSError, ValueError, mcp_server.InputError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
