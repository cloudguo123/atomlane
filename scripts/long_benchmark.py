#!/usr/bin/env python3
"""Run a controlled five-minute parallel benchmark and preserve public evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import random
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
        }
        self.samples.append(sample)
        self.next_emit = elapsed + self.interval_seconds
        self.last_signature = signature
        print(
            "BENCHMARK_PROGRESS "
            f"elapsed={elapsed:.1f}s running={signature[0]} "
            f"completed={signature[1]}/4 saved_so_far={sample['estimated_saved_seconds']:.1f}s",
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


def summarize_run(
    execution: dict[str, Any],
    progress_samples: list[dict[str, Any]],
    *,
    minimum_task_seconds: float,
    target_task_seconds: float,
    commit: str,
    run_id: str,
) -> dict[str, Any]:
    task_rows = []
    for result in execution["results"]:
        worker = _last_json_line(str(result.get("stdout", "")))
        task_rows.append(
            {
                "id": result["id"],
                "scenario": worker.get("scenario", result["id"]),
                "label": worker.get("label", SCENARIOS.get(result["id"], result["id"])),
                "status": result["status"],
                "duration_seconds": float(result["duration_seconds"]),
                "iterations": int(worker.get("iterations", 0)),
                "work_units": int(worker.get("work_units", 0)),
            }
        )
    serial_seconds = sum(task["duration_seconds"] for task in task_rows)
    wall_seconds = float(execution["summary"]["elapsed_seconds"])
    saved_seconds = max(0.0, serial_seconds - wall_seconds)
    peak = int(execution["summary"]["peak_concurrency"])
    speedup = serial_seconds / wall_seconds if wall_seconds else 0.0
    efficiency = speedup / peak if peak else 0.0
    all_succeeded = all(task["status"] == "succeeded" for task in task_rows)
    minimum_met = all(task["duration_seconds"] >= minimum_task_seconds for task in task_rows)
    return {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": commit,
        "status": "passed" if all_succeeded and minimum_met and peak > 1 else "failed",
        "method": "controlled-duration low-load parallel execution",
        "minimum_task_seconds": minimum_task_seconds,
        "target_task_seconds": target_task_seconds,
        "minimum_duration_met": minimum_met,
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
        "progress_samples": progress_samples,
    }


def merge_history(previous: dict[str, Any], latest: dict[str, Any]) -> dict[str, Any]:
    history = [
        item
        for item in previous.get("history", [])
        if isinstance(item, dict) and item.get("run_id") != latest["run_id"]
    ]
    history.append(
        {
            "run_id": latest["run_id"],
            "generated_at": latest["generated_at"],
            "commit": latest["commit"],
            "status": latest["status"],
            "wall_time_seconds": latest["parallel"]["wall_time_seconds"],
            "serial_equivalent_seconds": latest["serial_equivalent"]["seconds"],
            "saved_seconds": latest["savings"]["seconds"],
            "speedup_multiplier": latest["savings"]["speedup_multiplier"],
        }
    )
    return {
        "schema_version": "1.0",
        "latest": latest,
        "history": history,
        "cumulative": {
            "run_count": len(history),
            "parallel_wall_seconds": round(sum(item["wall_time_seconds"] for item in history), 6),
            "serial_equivalent_seconds": round(
                sum(item["serial_equivalent_seconds"] for item in history), 6
            ),
            "saved_seconds": round(sum(item["saved_seconds"] for item in history), 6),
        },
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
        old_stats = os.environ.get("MAC_PARALLEL_ACCELERATOR_STATS_PATH")
        os.environ["MAC_PARALLEL_ACCELERATOR_STATS_PATH"] = str(stats_path)
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
                os.environ.pop("MAC_PARALLEL_ACCELERATOR_STATS_PATH", None)
            else:
                os.environ["MAC_PARALLEL_ACCELERATOR_STATS_PATH"] = old_stats

    commit = os.environ.get("GITHUB_SHA") or _git_commit()
    run_id = os.environ.get("GITHUB_RUN_ID") or f"local-{int(time.time())}"
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
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
