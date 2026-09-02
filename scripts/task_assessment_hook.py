"""Fast, read-only UserPromptSubmit preflight for AtomLane.

The hook classifies only the submitted prompt. It never reads the project,
executes target code, mutates files, or claims that concurrency is safe.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

MAX_INPUT_BYTES = 262_144

_EXECUTION_RE = re.compile(
    r"(?:"
    r"\b(?:run|execute|test|lint|build|compile|benchmark|generate|render|scan|"
    r"validate|verify|package|deploy|convert|process|refactor|optimi[sz]e)\b|"
    r"运行|执行|测试|检查|构建|编译|基准|生成|渲染|扫描|校验|验证|打包|部署|"
    r"转换|处理|改造|优化|跑一下|跑一遍"
    r")",
    re.IGNORECASE,
)
_PARALLEL_RE = re.compile(
    r"(?:\b(?:parallel|concurrent|concurrency|fan[- ]?out|shard(?:ed|ing)?|"
    r"job matrix)\b|并行|并发|多路|分片|任务矩阵)",
    re.IGNORECASE,
)
_MULTI_RE = re.compile(
    r"(?:\b(?:multiple|several|batch|each|every|all|matrix|across|independent|"
    r"many)\b|多个|多项|多组|批量|每个|全部|分别|独立|一批|多平台|多容器)",
    re.IGNORECASE,
)
_LONG_RE = re.compile(
    r"(?:\b(?:long[- ]?running|slow|minutes?|hours?|five[- ]minute|"
    r"time[- ]?consuming)\b|长时间|耗时|分钟|小时|五分钟|5\s*分钟)",
    re.IGNORECASE,
)


def classify_prompt(prompt: str) -> str:
    """Return a conservative advisory class; never a safety decision."""
    if not _EXECUTION_RE.search(prompt):
        return "direct"
    if _PARALLEL_RE.search(prompt):
        return "candidate"
    if _MULTI_RE.search(prompt) and _LONG_RE.search(prompt):
        return "candidate"
    return "inspect"


def assessment_output(prompt: str) -> dict[str, Any]:
    classification = classify_prompt(prompt)
    if classification == "candidate":
        label = "likely parallel candidate; safety plan required"
        context = (
            "AtomLane preflight (advisory; no command was executed): the user's request "
            "above remains primary. The prompt suggests parallel or repeated local work. "
            "Before the first execution batch, use $accelerate-local-work to inspect real "
            "entrypoints, effects, dependencies, execution realm, and resource limits. "
            "Parallelize only units the atomic planner proves independent; otherwise stay "
            "serial. Keep runs over ten seconds visibly live and report only measured "
            "per-run and cumulative savings."
        )
    elif classification == "inspect":
        label = "inspect at the execution boundary"
        context = (
            "AtomLane preflight (advisory; no command was executed): the user's request "
            "above remains primary. Local execution may be needed, but this prompt alone "
            "does not expose a worthwhile parallel batch. Before the first execution "
            "batch, check whether at least two independent, non-trivial units actually "
            "exist. Use $accelerate-local-work only if that check succeeds; keep short, "
            "serial, or shared-state work direct."
        )
    else:
        label = "direct path; no concrete local batch detected"
        context = (
            "AtomLane preflight (advisory; no command was executed): the user's request "
            "above remains primary. No concrete local execution batch is visible in this "
            "prompt. Continue directly; reassess only if later work reveals at least two "
            "worthwhile independent local units."
        )
    return {
        "continue": True,
        "systemMessage": f"AtomLane · {label}",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        },
    }


def _read_event() -> dict[str, Any] | None:
    payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        return None
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(event, dict):
        return None
    if event.get("hook_event_name") != "UserPromptSubmit":
        return None
    if not isinstance(event.get("prompt"), str):
        return None
    return event


def main() -> int:
    event = _read_event()
    if event is None:
        return 0
    json.dump(assessment_output(event["prompt"]), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
