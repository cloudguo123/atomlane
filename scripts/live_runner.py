#!/usr/bin/env python3
"""Run accelerator workloads with stdout progress that Codex can poll live."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import mcp_server


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream AtomLane progress")
    parser.add_argument("--mode", choices=("exec", "map", "dag", "atomic"), default="exec")
    parser.add_argument("--input", required=True, help="Path to a JSON object containing tool arguments")
    return parser.parse_args()


def _load_payload(path_text: str) -> dict[str, Any]:
    path = Path(path_text).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise mcp_server.InputError("live runner input must be a JSON object")
    return payload


class ConsoleProgress:
    def __init__(self) -> None:
        self.last_elapsed = -1.0
        self.last_signature: tuple[int, int, int, int] | None = None

    def __call__(self, snapshot: dict[str, Any]) -> None:
        elapsed = float(snapshot["elapsed_seconds"])
        signature = (
            int(snapshot["running_tasks"]),
            int(snapshot.get("ready_tasks", 0)),
            int(snapshot["completed_tasks"]),
            int(snapshot["failed_tasks"]),
        )
        state_changed = signature != self.last_signature
        interval_elapsed = elapsed - self.last_elapsed >= 0.9
        if not state_changed and not interval_elapsed:
            return
        self.last_elapsed = elapsed
        self.last_signature = signature
        print(
            "⏱️ 实时"
            f"｜已运行 {elapsed:.1f}s"
            f"｜运行中 {snapshot['running_tasks']}"
            f"｜就绪 {snapshot.get('ready_tasks', 0)}"
            f"｜已完成 {snapshot['completed_tasks']}/{snapshot['task_count']}"
            f"｜失败 {snapshot['failed_tasks']}"
            f"｜当前预计节约 {snapshot['estimated_saved_so_far_seconds']:.1f}s",
            flush=True,
        )


async def _run(mode: str, payload: dict[str, Any]) -> dict[str, Any]:
    progress = ConsoleProgress()
    if mode == "exec":
        return await mcp_server.run_parallel(payload, progress)
    if mode == "map":
        return await mcp_server.run_map(payload, progress)
    if mode == "dag":
        return await mcp_server.run_dag(payload, progress)
    return await mcp_server.run_atomic(payload, progress)


def main() -> int:
    args = _arguments()
    try:
        result = asyncio.run(_run(args.mode, _load_payload(args.input)))
    except (OSError, json.JSONDecodeError, mcp_server.InputError) as exc:
        print(f"实时执行失败：{exc}", file=sys.stderr, flush=True)
        return 2

    print(result["indicator"]["display"], flush=True)
    print("LIVE_RESULT_JSON=" + json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    failed = result["summary"]["failed_task_ids"]
    timed_out = result["summary"]["timed_out_task_ids"]
    return 1 if failed or timed_out else 0


if __name__ == "__main__":
    raise SystemExit(main())
