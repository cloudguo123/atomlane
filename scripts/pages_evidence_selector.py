#!/usr/bin/env python3
"""Validate an untrusted workflow_run payload and emit a bounded selector."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ALLOWED_WORKFLOWS = frozenset(
    {"CI", "Five-Minute Benchmark", "Windows Five-Minute Benchmark"}
)
ALLOWED_EVENTS = frozenset({"push", "workflow_dispatch", "schedule"})
MAX_EVENT_BYTES = 4 * 1024 * 1024
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")


class SelectorError(ValueError):
    """The event cannot select trusted release evidence."""


def validate_workflow_run_selector(
    payload: Any, expected_repository: str
) -> tuple[str, str, str]:
    """Return workflow name, decimal run id, and commit after strict checks."""

    if not isinstance(expected_repository, str) or not expected_repository:
        raise SelectorError("expected repository is missing")
    run = payload.get("workflow_run") if isinstance(payload, dict) else None
    if not isinstance(run, dict):
        raise SelectorError("workflow_run payload is missing")
    repository = run.get("head_repository")
    if not isinstance(repository, dict):
        raise SelectorError("workflow_run repository is missing")

    workflow = run.get("name")
    run_id = run.get("id")
    head_sha = run.get("head_sha")
    if workflow not in ALLOWED_WORKFLOWS:
        raise SelectorError("unexpected triggering workflow")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise SelectorError("workflow_run id is invalid")
    if not isinstance(head_sha, str) or SHA_PATTERN.fullmatch(head_sha) is None:
        raise SelectorError("workflow_run commit is invalid")
    if run.get("head_branch") != "main":
        raise SelectorError("workflow_run branch is not main")
    if repository.get("full_name") != expected_repository:
        raise SelectorError("workflow_run repository is not trusted")
    if run.get("event") not in ALLOWED_EVENTS:
        raise SelectorError("workflow_run event is not trusted")
    if run.get("conclusion") != "success":
        raise SelectorError("workflow_run did not succeed")
    return workflow, str(run_id), head_sha


def load_selector(event_path: Path, expected_repository: str) -> tuple[str, str, str]:
    try:
        size = event_path.stat().st_size
    except OSError as exc:
        raise SelectorError(f"could not inspect event payload: {exc}") from exc
    if size > MAX_EVENT_BYTES:
        raise SelectorError("event payload exceeds its size bound")
    try:
        payload = json.loads(event_path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SelectorError(f"could not parse event payload: {exc}") from exc
    return validate_workflow_run_selector(payload, expected_repository)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        print("usage: pages_evidence_selector.py EVENT_PATH OWNER/REPOSITORY", file=sys.stderr)
        return 2
    try:
        selector = load_selector(Path(arguments[0]), arguments[1])
    except SelectorError as exc:
        print(f"Rejected workflow_run evidence selector: {exc}", file=sys.stderr)
        return 1
    for value in selector:
        sys.stdout.buffer.write(value.encode("utf-8") + b"\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
