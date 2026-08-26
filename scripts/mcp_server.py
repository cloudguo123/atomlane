#!/usr/bin/env python3
"""Dependency-free MCP stdio server for bounded local parallel execution."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import fnmatch
import hashlib
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from atom_engine import (
    AtomError,
    atom_conflicts,
    canonical_plan_hash,
    compile_atomic_plan,
    validate_source_snapshots,
)
from atom_frontends import compile_entrypoints

SERVER_NAME = "mac-parallel-accelerator"
SERVER_VERSION = "0.9.0"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_CATALOG_PATH = PLUGIN_ROOT / "catalog" / "scenarios.json"
INDICATOR_RESOURCE_URI = f"ui://widget/mac-parallel-indicator-{SERVER_VERSION}.html"
INDICATOR_MIME_TYPE = "text/html;profile=mcp-app"
MAX_TASKS = 128
MAX_CONCURRENCY = 64
MAX_ARGV_ITEMS = 256
MAX_ARG_LENGTH = 32_768
MAX_STDIN_BYTES = 1_048_576
DEFAULT_OUTPUT_BYTES = 8_192
MAX_OUTPUT_BYTES = 65_536
DEFAULT_TIMEOUT_SECONDS = 900.0
MAX_TIMEOUT_SECONDS = 86_400.0
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_STATIC_HARDWARE_CACHE: dict[str, Any] | None = None


def _progress_interval_seconds() -> float:
    try:
        return max(0.1, float(os.environ.get("MAC_PARALLEL_ACCELERATOR_PROGRESS_INTERVAL", "1")))
    except ValueError:
        return 1.0


def _stats_path() -> Path:
    override = os.environ.get("MAC_PARALLEL_ACCELERATOR_STATS_PATH")
    if override:
        return Path(override).expanduser()
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Codex"
        / "Mac Parallel Accelerator"
        / "stats.json"
    )


def _indicator_ui_meta() -> dict[str, Any]:
    return {
        "ui": {"resourceUri": INDICATOR_RESOURCE_URI, "visibility": ["model"]},
        "ui/resourceUri": INDICATOR_RESOURCE_URI,
        "openai/outputTemplate": INDICATOR_RESOURCE_URI,
        "openai/widgetAccessible": True,
        "openai/toolInvocation/invoking": "正在自适应并行执行…",
        "openai/toolInvocation/invoked": "并行执行完成",
    }


def _indicator_resource_meta() -> dict[str, Any]:
    return {
        "openai/widgetDescription": "显示实际并行状态、峰值并发、加速倍数、耗时和任务结果。",
        "openai/widgetPrefersBorder": False,
        "openai/widgetCSP": {"connect_domains": [], "resource_domains": [], "frame_domains": []},
        "ui": {
            "prefersBorder": False,
            "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": []},
        },
    }


def _indicator_resource() -> dict[str, Any]:
    return {
        "uri": INDICATOR_RESOURCE_URI,
        "name": "mac_parallel_accelerator_indicator",
        "title": "Mac Parallel Accelerator 指示器",
        "description": "并行执行状态与加速倍数卡片。",
        "mimeType": INDICATOR_MIME_TYPE,
        "_meta": _indicator_resource_meta(),
    }


def _indicator_html() -> str:
    template = (PLUGIN_ROOT / "assets" / "parallel-indicator.html").read_text(encoding="utf-8")
    bridge = (PLUGIN_ROOT / "assets" / "parallel-indicator-host.bundle.js").read_text(encoding="utf-8")
    external_tag = '<script src="./parallel-indicator-host.bundle.js" data-mcp-app-bridge></script>'
    return template.replace(external_tag, f"<script>{bridge}</script>")


class InputError(ValueError):
    pass


def _scenario_catalog() -> dict[str, Any]:
    try:
        catalog = json.loads(SCENARIO_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load scenario catalog: {exc}") from exc
    if not isinstance(catalog, dict) or not isinstance(catalog.get("scenarios"), list):
        raise TypeError("scenario catalog must contain a scenarios array")
    return catalog


def _project_inventory(project: Path) -> dict[str, Any]:
    skipped = {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        "external",
        "third_party",
        "vendor",
        "tmp",
        "output",
        "dist",
        "build",
    }
    max_files = 25_000
    relative_files: list[str] = []
    relative_dirs: list[str] = []
    extension_counts: Counter[str] = Counter()
    truncated = False
    for root_text, directories, files in os.walk(project):
        root = Path(root_text)
        directories[:] = [name for name in directories if name not in skipped]
        relative_root = root.relative_to(project)
        for directory in directories:
            relative = (relative_root / directory).as_posix()
            relative_dirs.append(relative)
        for name in files:
            relative = (relative_root / name).as_posix()
            relative_files.append(relative)
            suffix = Path(name).suffix.lower() or "[no-ext]"
            extension_counts[suffix] += 1
            if len(relative_files) >= max_files:
                truncated = True
                directories[:] = []
                break
        if truncated:
            break

    make_targets: list[str] = []
    makefile = project / "Makefile"
    if makefile.is_file() and makefile.stat().st_size <= 2_000_000:
        for line in makefile.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"^([A-Za-z0-9_.-]+)\s*:(?![=])", line)
            if match:
                make_targets.append(match.group(1))

    top_level_entries = sorted(item.name for item in project.iterdir())
    return {
        "project_path": str(project),
        "top_level_entries": top_level_entries,
        "relative_files": relative_files,
        "relative_directories": relative_dirs,
        "extension_counts": dict(extension_counts.most_common(30)),
        "file_count_scanned": len(relative_files),
        "directory_count_scanned": len(relative_dirs),
        "inventory_truncated": truncated,
        "make_targets": sorted(set(make_targets)),
    }


def _session_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(32):
                line = handle.readline()
                if not line:
                    break
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("type") == "session_meta" and isinstance(item.get("payload"), dict):
                    return item["payload"]
    except OSError:
        return None
    return None


def _trace_input_text(payload: dict[str, Any]) -> str:
    if payload.get("type") == "custom_tool_call":
        return str(payload.get("input") or "")
    if payload.get("type") == "function_call":
        return str(payload.get("arguments") or "")
    return ""


def _trace_tool_names(payload: dict[str, Any]) -> list[str]:
    if payload.get("type") == "function_call":
        namespace = payload.get("namespace")
        name = str(payload.get("name") or "unknown")
        return [f"{namespace}.{name}" if namespace else name]
    if payload.get("type") != "custom_tool_call":
        return []
    raw = str(payload.get("input") or "")
    nested = re.findall(r"\btools\.([A-Za-z0-9_]+)\s*\(", raw)
    return nested or [str(payload.get("name") or "unknown")]


def _trace_signatures(raw: str, tool_names: list[str]) -> set[str]:
    low = raw.lower()
    names = " ".join(tool_names).lower()
    signatures: set[str] = set()
    if re.search(r"\b(?:rg|sed|find|fd|jq)\b", low):
        signatures.add("search")
    if re.search(
        r"\b(?:pytest|unittest|ctest|vitest|jest|playwright|cypress|swift\s+test|go\s+test|cargo\s+test|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test)\b",
        low,
    ):
        signatures.add("tests")
    if re.search(r"(?:python\S*)[^\n]*(?:experiments/|-m\s+experiments\.)", low):
        signatures.add("experiment_python")
    if re.search(r"\b(?:numpy|scipy|cvxpy|blas|lapack|svd|eigen)\b", low):
        signatures.add("numerical_python")
    if re.search(r"\b(?:latexmk|pdflatex|xelatex|bibtex|biber)\b", low):
        signatures.add("latex")
    if re.search(r"\b(?:pdftoppm|pdfinfo|pdftotext|pdfseparate)\b", low) or "view_image" in names:
        signatures.add("pdf")
    if re.search(r"\b(?:png|jpe?g|tiff?|heic|imagemagick|sips|vimage)\b", low) or "view_image" in names:
        signatures.add("image")
    if re.search(r"\b(?:ffmpeg|videotoolbox|mp4|mov|mkv)\b", low):
        signatures.add("video")
    if re.search(r"\b(?:wav|flac|mp3|audio|vdsp|speech)\b", low):
        signatures.add("audio")
    if re.search(r"\b(?:coreml|mlx|pytorch|torch|tensorflow|onnx|safetensors|inference)\b", low):
        signatures.add("ml")
    if re.search(r"\b(?:parquet|csv|jsonl|etl|schema|dataset)\b", low):
        signatures.add("data")
    if re.search(r"\b(?:prisma|postgres(?:ql)?|migrat(?:e|ion)|seed(?:ing)?)\b", low):
        signatures.add("database")
    if re.search(r"\b(?:processpoolexecutor|threadpoolexecutor|multiprocessing|worker_threads?|dispatchqueue|xargs\s+-p|gnu\s+parallel)\b", low):
        signatures.add("runtime_parallelism")
    if re.search(r"\b(?:simd|vdsp|vimage|bnns|accelerate\.framework|numpy|scipy|blas|lapack)\b", low):
        signatures.add("vector_kernel")
    if re.search(r"\b(?:json\.parse|json\.stringify|orjson|simdjson|serde_json|csv|xml|yaml)\b", low):
        signatures.add("serialization")
    if re.search(r"\b(?:mmap|createreadstream|createwritestream|readstream|writestream|chunksize|chunk_size)\b", low):
        signatures.add("streaming_io")
    if re.search(r"\b(?:ccache|sccache|incremental|content-addressed|\.next/cache|turbo\s+run)\b", low):
        signatures.add("cache")
    if re.search(r"\b(?:docker|podman|buildx)\b", low):
        signatures.add("docker")
    if re.search(r"\b(?:cmake|ninja|cargo|go\s+build|clang|gcc|g\+\+)\b", low):
        signatures.add("native_build")
    if re.search(r"\b(?:curl|wget|rsync|scp|docker\s+pull)\b", low):
        signatures.add("network")
    if re.search(r"\b(?:zip|tar|gzip|shasum|sha256sum)\b", low):
        signatures.add("archive")
    if re.search(r"\bgit\s+(?:status|diff|show|log|add|commit|tag|push)\b", low):
        signatures.add("git")
    if re.search(r"\b(?:audit|verify|verification|claims?|readiness|preflight)\b", low):
        signatures.add("audit")
    if "apply_patch" in names or "apply_patch" in low:
        signatures.add("mutation")
    if "web__run" in names or "mcp__" in names or "connector" in low:
        signatures.add("remote")
    if re.search(r"\b(?:collaboration\.|spawn_agent|send_message|wait_agent|list_agents)\b", names):
        signatures.add("agent_orchestration")
    return signatures


def _trace_history(project: Path, file_limit: int) -> dict[str, Any]:
    sessions_root = Path.home() / ".codex" / "sessions"
    if not sessions_root.is_dir():
        return {
            "available": False,
            "session_count": 0,
            "reason": "local Codex sessions directory was not found",
        }

    candidates: list[tuple[float, Path]] = []
    try:
        for path in sessions_root.rglob("*.jsonl"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError as exc:
        return {"available": False, "session_count": 0, "reason": str(exc)}
    candidates.sort(reverse=True)

    relevant: list[tuple[Path, dict[str, Any]]] = []
    project_text = str(project)
    for _, path in candidates[:file_limit]:
        meta = _session_meta(path)
        if meta and meta.get("cwd") == project_text:
            relevant.append((path, meta))

    tool_counts: Counter[str] = Counter()
    signature_counts: Counter[str] = Counter()
    seen_calls: set[str] = set()
    agent_tasks: Counter[str] = Counter()
    user_sessions = 0
    subagent_sessions = 0
    sampled_bytes = 0
    tail_bytes_per_file = 262_144
    for path, meta in relevant:
        if meta.get("thread_source") == "subagent":
            subagent_sessions += 1
            source = meta.get("source")
            if isinstance(source, dict):
                spawn = ((source.get("subagent") or {}).get("thread_spawn") or {})
                agent_path = spawn.get("agent_path")
                if isinstance(agent_path, str) and agent_path:
                    agent_tasks[agent_path.rsplit("/", 1)[-1]] += 1
        else:
            user_sessions += 1
        try:
            with path.open("rb") as handle:
                size = path.stat().st_size
                offset = max(0, size - tail_bytes_per_file)
                handle.seek(offset)
                data = handle.read(tail_bytes_per_file)
            sampled_bytes += len(data)
        except OSError:
            continue
        text = data.decode("utf-8", errors="replace")
        if offset:
            _, _, text = text.partition("\n")
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload")
            if item.get("type") != "response_item" or not isinstance(payload, dict):
                continue
            if payload.get("type") not in {"function_call", "custom_tool_call"}:
                continue
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or call_id in seen_calls:
                continue
            seen_calls.add(call_id)
            tools = _trace_tool_names(payload)
            tool_counts.update(tools)
            for signature in _trace_signatures(_trace_input_text(payload), tools):
                signature_counts[signature] += 1

    return {
        "available": True,
        "session_count": len(relevant),
        "user_session_count": user_sessions,
        "subagent_session_count": subagent_sessions,
        "sampled_unique_call_count": len(seen_calls),
        "sampled_tool_counts": dict(tool_counts.most_common(20)),
        "sampled_signature_counts": dict(signature_counts.most_common()),
        "agent_task_names": dict(agent_tasks.most_common(30)),
        "sampling": {
            "recent_session_file_limit": file_limit,
            "tail_bytes_per_matching_file": tail_bytes_per_file,
            "sampled_bytes": sampled_bytes,
            "content_policy": "Only aggregate tool/signature counts and agent task names; do not return prompts, command bodies, reasoning, or tool outputs.",
        },
    }


def _matching_values(values: list[str], patterns: list[str]) -> list[str]:
    matches: list[str] = []
    for value in values:
        if any(_path_pattern_matches(value, pattern) for pattern in patterns):
            matches.append(value)
            if len(matches) >= 5:
                break
    return matches


def _path_pattern_matches(value: str, pattern: str) -> bool:
    low_value = value.lower()
    low_pattern = pattern.lower()
    return fnmatch.fnmatch(low_value, low_pattern) or (
        low_pattern.startswith("**/") and fnmatch.fnmatch(low_value, low_pattern[3:])
    )


def _content_probe_matches(
    project: Path,
    relative_files: list[str],
    probes: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Return bounded path-only evidence for source/config content signatures."""
    matches: list[dict[str, str]] = []
    cache: dict[str, str | None] = {}
    scanned_candidates = 0
    sampled_bytes = 0
    max_candidates = 240
    max_sampled_bytes = 4_194_304
    max_file_bytes = 524_288
    excluded_suffixes = (".min.js", ".bundle.js", ".map", ".lock")

    for probe in probes:
        patterns = list(probe.get("glob_patterns") or [])
        if isinstance(probe.get("glob"), str):
            patterns.append(probe["glob"])
        regex = probe.get("regex")
        if not patterns or not isinstance(regex, str):
            continue
        try:
            compiled = re.compile(regex, re.IGNORECASE | re.MULTILINE)
        except re.error:
            continue
        label = str(probe.get("label") or regex[:80])
        for relative in relative_files:
            if len(matches) >= 5 or scanned_candidates >= max_candidates:
                return matches
            low = relative.lower()
            if low.endswith(excluded_suffixes) or Path(relative).name.startswith(".env"):
                continue
            if not any(_path_pattern_matches(low, pattern) for pattern in patterns):
                continue
            scanned_candidates += 1
            if relative not in cache:
                path = project / relative
                try:
                    size = path.stat().st_size
                    if size <= 0 or size > max_file_bytes or sampled_bytes + size > max_sampled_bytes:
                        cache[relative] = None
                    else:
                        data = path.read_bytes()
                        sampled_bytes += len(data)
                        cache[relative] = (
                            None
                            if b"\x00" in data
                            else data.decode("utf-8", errors="replace")
                        )
                except OSError:
                    cache[relative] = None
            text = cache[relative]
            if text is not None and compiled.search(text):
                matches.append({"label": label, "file": relative})
                break
    return matches


def _scenario_match(
    scenario: dict[str, Any],
    inventory: dict[str, Any],
    task_hint: str,
    history: dict[str, Any],
) -> dict[str, Any] | None:
    detectors = scenario.get("detectors") or {}
    evidence: list[dict[str, Any]] = []
    score = 0.0
    categories = 0

    marker_matches = _matching_values(
        inventory["top_level_entries"], list(detectors.get("marker_files") or [])
    )
    if marker_matches:
        categories += 1
        score += 0.16 + min(0.06, 0.03 * (len(marker_matches) - 1))
        evidence.append({"source": "marker", "matches": marker_matches})

    directory_matches = _matching_values(
        inventory["relative_directories"], list(detectors.get("directories") or [])
    )
    if directory_matches:
        categories += 1
        score += 0.18 + min(0.06, 0.03 * (len(directory_matches) - 1))
        evidence.append({"source": "directory", "matches": directory_matches})

    glob_matches = _matching_values(
        inventory["relative_files"], list(detectors.get("glob_patterns") or [])
    )
    if glob_matches:
        categories += 1
        score += 0.18 + min(0.06, 0.03 * (len(glob_matches) - 1))
        evidence.append({"source": "file_pattern", "matches": glob_matches})

    content_matches = _content_probe_matches(
        Path(inventory["project_path"]),
        inventory["relative_files"],
        list(detectors.get("content_patterns") or []),
    )
    if content_matches:
        categories += 1
        score += 0.20 + min(0.06, 0.03 * (len(content_matches) - 1))
        evidence.append({"source": "content_signature", "matches": content_matches})

    make_matches: list[str] = []
    for target in inventory["make_targets"]:
        for pattern in detectors.get("make_target_patterns") or []:
            try:
                if re.search(pattern, target, re.IGNORECASE):
                    make_matches.append(target)
                    break
            except re.error:
                continue
        if len(make_matches) >= 5:
            break
    if make_matches:
        categories += 1
        score += 0.18 + min(0.06, 0.03 * (len(make_matches) - 1))
        evidence.append({"source": "make_target", "matches": make_matches})

    hint_matches = [
        keyword
        for keyword in detectors.get("task_keywords") or []
        if str(keyword).lower() in task_hint.lower()
    ][:5]
    if hint_matches:
        categories += 1
        score += 0.22 + min(0.06, 0.03 * (len(hint_matches) - 1))
        evidence.append({"source": "task_hint", "matches": hint_matches})

    history_signatures = history.get("sampled_signature_counts") or {}
    trace_matches = [
        signature
        for signature in detectors.get("trace_signatures") or []
        if int(history_signatures.get(signature, 0)) > 0
    ][:5]
    if trace_matches:
        categories += 1
        score += 0.20 + min(0.06, 0.03 * (len(trace_matches) - 1))
        evidence.append(
            {
                "source": "trace_signature",
                "matches": [
                    {"signature": signature, "sampled_count": history_signatures[signature]}
                    for signature in trace_matches
                ],
            }
        )

    history_tool_counts = history.get("sampled_tool_counts") or {}
    trace_tool_matches: list[dict[str, Any]] = []
    for rule in detectors.get("trace_tool_min_counts") or []:
        pattern = rule.get("pattern")
        minimum = rule.get("minimum")
        if not isinstance(pattern, str) or isinstance(minimum, bool) or not isinstance(minimum, int):
            continue
        total = 0
        try:
            for tool_name, count in history_tool_counts.items():
                if re.search(pattern, tool_name, re.IGNORECASE):
                    total += int(count)
        except re.error:
            continue
        if total >= minimum:
            trace_tool_matches.append(
                {"label": str(rule.get("label") or pattern), "sampled_count": total}
            )
    if trace_tool_matches:
        categories += 1
        score += 0.20 + min(0.06, 0.03 * (len(trace_tool_matches) - 1))
        evidence.append({"source": "trace_tool_volume", "matches": trace_tool_matches[:5]})

    agent_task_counts = history.get("agent_task_names") or {}
    agent_task_matches: list[dict[str, Any]] = []
    for task_name, count in agent_task_counts.items():
        for pattern in detectors.get("agent_task_patterns") or []:
            try:
                if re.search(pattern, task_name, re.IGNORECASE):
                    agent_task_matches.append({"task": task_name, "sampled_count": count})
                    break
            except re.error:
                continue
        if len(agent_task_matches) >= 5:
            break
    if agent_task_matches:
        categories += 1
        score += 0.20 + min(0.06, 0.03 * (len(agent_task_matches) - 1))
        evidence.append({"source": "agent_task", "matches": agent_task_matches})

    requirements = scenario.get("match_requirements") or {}
    hint_override = bool(hint_matches) and bool(requirements.get("task_hint_override", True))
    if not hint_override:
        minimum_sources = int(requirements.get("minimum_evidence_sources", 0))
        minimum_file_matches = int(requirements.get("minimum_file_pattern_matches", 0))
        minimum_content_matches = int(requirements.get("minimum_content_matches", 0))
        minimum_project_files = int(requirements.get("minimum_project_files", 0))
        required_any_globs = list(requirements.get("required_any_glob_patterns") or [])
        if minimum_sources and categories < minimum_sources:
            return None
        if minimum_file_matches and len(glob_matches) < minimum_file_matches:
            return None
        if minimum_content_matches and len(content_matches) < minimum_content_matches:
            return None
        if minimum_project_files and inventory["file_count_scanned"] < minimum_project_files:
            return None
        if required_any_globs and not _matching_values(
            inventory["relative_files"], required_any_globs
        ):
            return None

    if categories >= 3:
        score += 0.08
    if not evidence:
        return None
    confidence = min(0.99, score)
    return {
        "id": scenario["id"],
        "title_zh": scenario["title_zh"],
        "category": scenario["category"],
        "layer": scenario.get("layer", "workflow"),
        "mode": scenario["mode"],
        "confidence": round(confidence, 3),
        "confidence_label": "high" if confidence >= 0.72 else "medium" if confidence >= 0.45 else "exploratory",
        "evidence": evidence,
        "default_execution": scenario["default_execution"],
        "optimization_goals": scenario["optimization_goals"],
        "guardrails": scenario["guardrails"],
    }


def scenario_plan(arguments: dict[str, Any]) -> dict[str, Any]:
    path_text = arguments.get("project_path")
    if not isinstance(path_text, str) or not os.path.isabs(path_text):
        raise InputError("project_path must be an absolute path")
    project = Path(path_text).resolve()
    if not project.is_dir():
        raise InputError(f"project_path does not exist or is not a directory: {project}")
    task_hint = arguments.get("task_hint", "")
    if not isinstance(task_hint, str) or len(task_hint) > 8_000:
        raise InputError("task_hint must be a string with at most 8000 characters")
    include_history = arguments.get("include_trace_history", False)
    if not isinstance(include_history, bool):
        raise InputError("include_trace_history must be boolean")
    file_limit = arguments.get("trace_file_limit", 200)
    if isinstance(file_limit, bool) or not isinstance(file_limit, int) or not 1 <= file_limit <= 500:
        raise InputError("trace_file_limit must be an integer between 1 and 500")
    max_scenarios = arguments.get("max_scenarios", 8)
    if isinstance(max_scenarios, bool) or not isinstance(max_scenarios, int) or not 1 <= max_scenarios <= 20:
        raise InputError("max_scenarios must be an integer between 1 and 20")
    minimum_confidence = arguments.get("minimum_confidence", 0.28)
    if (
        isinstance(minimum_confidence, bool)
        or not isinstance(minimum_confidence, (int, float))
        or not 0.1 <= float(minimum_confidence) <= 0.95
    ):
        raise InputError("minimum_confidence must be between 0.1 and 0.95")

    catalog = _scenario_catalog()
    inventory = _project_inventory(project)
    history = (
        _trace_history(project, file_limit)
        if include_history
        else {"available": False, "session_count": 0, "reason": "trace history was not requested"}
    )
    matches = [
        match
        for scenario in catalog["scenarios"]
        if (match := _scenario_match(scenario, inventory, task_hint, history)) is not None
        and match["confidence"] >= float(minimum_confidence)
    ]
    matches.sort(
        key=lambda item: (
            item["confidence"],
            item["mode"] != "serial_guardrail",
            len(item["optimization_goals"]),
        ),
        reverse=True,
    )
    selected = matches[:max_scenarios]
    flattened_targets: list[dict[str, Any]] = []
    for match in selected:
        for goal in match["optimization_goals"]:
            flattened_targets.append(
                {
                    "scenario_id": match["id"],
                    "scenario_title_zh": match["title_zh"],
                    "confidence": match["confidence"],
                    "mode": match["mode"],
                    "layer": match["layer"],
                    "executor": match["default_execution"]["executor"],
                    **goal,
                }
            )
    category_counts = Counter(str(item.get("category") or "unknown") for item in catalog["scenarios"])
    layer_counts = Counter(str(item.get("layer") or "workflow") for item in catalog["scenarios"])
    goal_count = sum(len(item.get("optimization_goals") or []) for item in catalog["scenarios"])
    high_value = [
        target["id"]
        for target in flattened_targets
        if target["mode"] in {"parallel", "conditional"} and target["confidence"] >= 0.45
    ]
    serial_guards = [match["id"] for match in selected if match["mode"] == "serial_guardrail"]
    return {
        "catalog": {
            "schema_version": catalog.get("schema_version"),
            "scenario_count": len(catalog["scenarios"]),
            "optimization_goal_count": goal_count,
            "category_counts": dict(sorted(category_counts.items())),
            "layer_counts": dict(sorted(layer_counts.items())),
        },
        "project": {
            "path": str(project),
            "file_count_scanned": inventory["file_count_scanned"],
            "directory_count_scanned": inventory["directory_count_scanned"],
            "inventory_truncated": inventory["inventory_truncated"],
            "extension_counts": inventory["extension_counts"],
            "make_target_count": len(inventory["make_targets"]),
        },
        "trace_history": history,
        "matched_scenarios": selected,
        "optimization_targets": flattened_targets,
        "decision_summary": {
            "matched_scenario_count": len(selected),
            "high_value_target_ids": high_value,
            "serial_guardrail_scenario_ids": serial_guards,
            "matched_layer_counts": dict(
                sorted(Counter(match["layer"] for match in selected).items())
            ),
            "recommended_first_step": (
                "Build isolated work units for the highest-confidence parallel scenario, then execute with live progress."
                if high_value
                else "Keep work serial until at least two independent, substantial units and isolated outputs are identified."
            ),
        },
        "global_guardrails": catalog.get("global_guardrails") or [],
        "limitations": [
            "Scenario matching is advisory and does not execute commands or grant authorization.",
            "Trace history is a bounded tail sample for routing signals, not a complete performance benchmark.",
            "Project files, prompts, reasoning, command bodies, and tool outputs are not returned from trace analysis.",
        ],
    }


def _scan_string_list(value: Any, name: str, maximum: int = 128) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise InputError(f"{name} must be an array with at most {maximum} entries")
    if not all(isinstance(item, str) and item and "\x00" not in item for item in value):
        raise InputError(f"every {name} entry must be a non-empty NUL-free string")
    return list(dict.fromkeys(value))


def _scan_path_token(value: str, cwd: Path) -> str:
    if re.match(r"^[A-Za-z][A-Za-z0-9_.-]*:", value) and not value.startswith(("./", "../")):
        return value
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return str(path.resolve(strict=False))


def _scan_path_overlap(first: str, second: str) -> bool:
    if not first.startswith("/") or not second.startswith("/"):
        return first == second
    return first == second or first.startswith(second + os.sep) or second.startswith(first + os.sep)


def _scan_command_traits(argv: list[str] | None, kind: str, cwd: Path) -> dict[str, Any]:
    normalized_argv = list(argv or [])
    if normalized_argv:
        normalized_argv[0] = Path(normalized_argv[0]).name
    command = " ".join(normalized_argv).lower()
    resources: list[str] = []
    inferred_side_effect = kind == "mutation"
    reasons: list[str] = []

    if re.search(r"(?:^|\s)git\s+(?:add|commit|merge|rebase|reset|checkout|switch|tag|stash|cherry-pick)(?:\s|$)", command):
        inferred_side_effect = True
        repository = cwd
        for candidate in (cwd, *cwd.parents):
            if (candidate / ".git").exists():
                repository = candidate
                break
        resources.extend([f"git:index:{repository}", f"git:refs:{repository}"])
        reasons.append("mutates the repository index or refs")
    if re.search(r"(?:migrate|migration|prisma\s+db\s+(?:push|seed)|alembic\s+upgrade|rails\s+db:|manage\.py\s+migrate)", command):
        inferred_side_effect = True
        resources.append("database:default")
        reasons.append("appears to mutate a database schema or fixtures")
    if re.search(r"(?:docker\s+compose|docker-compose)\s+(?:up|down|create|start|stop|restart|rm)", command):
        inferred_side_effect = True
        resources.append(f"docker:compose:{cwd}")
        reasons.append("mutates one Docker Compose project")
    if re.search(r"(?:^|\s)(?:npm|pnpm|yarn|bun|pip|uv|poetry)\s+(?:install|add|remove|update|sync)(?:\s|$)", command):
        inferred_side_effect = True
        resources.append(f"dependencies:{cwd}")
        reasons.append("mutates a dependency environment or lockfile")
    if re.search(r"(?:^|\s)(?:rm|mv|cp|touch|mkdir|install|tee)(?:\s|$)|(?:^|\s)sed\s+-i(?:\s|$)", command):
        inferred_side_effect = True
        reasons.append("contains a filesystem-mutating command")
    if re.search(r"(?:kubectl\s+(?:apply|delete|patch|scale)|terraform\s+(?:apply|destroy)|aws\s+.*(?:put|delete|update))", command):
        inferred_side_effect = True
        resources.append("external-account:default")
        reasons.append("appears to mutate external infrastructure or an account")
    return {
        "inferred_side_effect": inferred_side_effect,
        "inferred_resources": list(dict.fromkeys(resources)),
        "reasons": reasons,
    }


def _normalize_scan_units(raw_units: Any, default_cwd: Path) -> list[dict[str, Any]]:
    if raw_units is None:
        raw_units = []
    if not isinstance(raw_units, list) or len(raw_units) > MAX_TASKS:
        raise InputError(f"candidate_units must be an array with at most {MAX_TASKS} entries")
    units: list[dict[str, Any]] = []
    allowed_kinds = {"command", "read", "test", "build", "transform", "network", "mutation", "other"}
    allowed_profiles = {"cpu", "io", "mixed", "accelerator"}
    for index, raw in enumerate(raw_units):
        if not isinstance(raw, dict):
            raise InputError(f"candidate unit {index} must be an object")
        unit_id = raw.get("id", f"unit-{index}")
        if not isinstance(unit_id, str) or not TASK_ID_RE.match(unit_id):
            raise InputError(f"candidate unit {index} id must match {TASK_ID_RE.pattern}")
        kind = raw.get("kind", "command")
        if kind not in allowed_kinds:
            raise InputError(f"candidate unit {unit_id} has an unsupported kind")
        cwd_text = raw.get("cwd", str(default_cwd))
        if not isinstance(cwd_text, str) or not os.path.isabs(cwd_text):
            raise InputError(f"candidate unit {unit_id} cwd must be absolute")
        cwd = Path(cwd_text).resolve(strict=False)
        if not cwd.is_dir():
            raise InputError(f"candidate unit {unit_id} cwd does not exist: {cwd}")
        argv_raw = raw.get("argv")
        argv = _validate_argv(argv_raw) if argv_raw is not None else None
        dependencies = _scan_string_list(raw.get("depends_on"), f"candidate unit {unit_id} depends_on")
        reads = [_scan_path_token(item, cwd) for item in _scan_string_list(raw.get("reads"), f"candidate unit {unit_id} reads")]
        writes = [_scan_path_token(item, cwd) for item in _scan_string_list(raw.get("writes"), f"candidate unit {unit_id} writes")]
        shared_resources = _scan_string_list(
            raw.get("shared_resources"), f"candidate unit {unit_id} shared_resources"
        )
        estimate = raw.get("estimated_seconds")
        if estimate is not None and (isinstance(estimate, bool) or not isinstance(estimate, (int, float)) or estimate <= 0 or estimate > MAX_TIMEOUT_SECONDS):
            raise InputError(f"candidate unit {unit_id} estimated_seconds must be positive")
        memory = raw.get("estimated_memory_mb")
        if memory is not None and (isinstance(memory, bool) or not isinstance(memory, (int, float)) or memory <= 0):
            raise InputError(f"candidate unit {unit_id} estimated_memory_mb must be positive")
        profile = raw.get("profile")
        if profile is None:
            profile = "cpu" if kind in {"test", "build", "transform"} else "io" if kind in {"read", "network"} else "mixed"
        if profile not in allowed_profiles:
            raise InputError(f"candidate unit {unit_id} has an unsupported profile")
        explicit_side_effect = raw.get("side_effect")
        if explicit_side_effect is not None and not isinstance(explicit_side_effect, bool):
            raise InputError(f"candidate unit {unit_id} side_effect must be boolean")
        batch_key = raw.get("batch_key")
        if batch_key is not None and (not isinstance(batch_key, str) or not batch_key or len(batch_key) > 256):
            raise InputError(f"candidate unit {unit_id} batch_key must be a non-empty short string")
        traits = _scan_command_traits(argv, kind, cwd)
        # A caller may add conservative knowledge, but cannot negate a static
        # mutation inference by writing side_effect:false.
        side_effect = bool(traits["inferred_side_effect"] or explicit_side_effect is True)
        resources = list(dict.fromkeys(shared_resources + traits["inferred_resources"]))
        hazards = list(traits["reasons"])
        force_serial = bool(side_effect and not writes and not resources)
        if force_serial:
            hazards.append("side effect has no declared isolation boundary")
        threshold = {"cpu": 0.2, "io": 0.1, "mixed": 0.15, "accelerator": 0.5}[profile]
        tiny = estimate is not None and float(estimate) < threshold
        if tiny:
            hazards.append(f"estimated work is below the {threshold:.2f}s parallelism threshold")
        units.append(
            {
                "id": unit_id,
                "kind": kind,
                "argv": argv,
                "cwd": str(cwd),
                "reads": list(dict.fromkeys(reads)),
                "writes": list(dict.fromkeys(writes)),
                "depends_on": dependencies,
                "shared_resources": resources,
                "estimated_seconds": round(float(estimate), 4) if estimate is not None else None,
                "estimated_memory_mb": round(float(memory), 2) if memory is not None else None,
                "profile": profile,
                "side_effect": side_effect,
                "side_effect_source": (
                    "inferred_overrides_explicit_false"
                    if traits["inferred_side_effect"] and explicit_side_effect is False else
                    "explicit" if explicit_side_effect is not None else "inferred"
                ),
                "batch_key": batch_key,
                "tiny": tiny,
                "force_serial": force_serial,
                "hazards": hazards,
            }
        )
    ids = [unit["id"] for unit in units]
    if len(ids) != len(set(ids)):
        raise InputError("candidate unit IDs must be unique")
    by_id = set(ids)
    for unit in units:
        unknown = set(unit["depends_on"]) - by_id
        if unknown:
            raise InputError(f"candidate unit {unit['id']} has unknown dependencies: {sorted(unknown)}")
        if unit["id"] in unit["depends_on"]:
            raise InputError(f"candidate unit {unit['id']} cannot depend on itself")
    visiting: set[str] = set()
    visited: set[str] = set()
    unit_map = {unit["id"]: unit for unit in units}

    def visit(unit_id: str) -> None:
        if unit_id in visiting:
            raise InputError("candidate unit graph contains a dependency cycle")
        if unit_id in visited:
            return
        visiting.add(unit_id)
        for dependency in unit_map[unit_id]["depends_on"]:
            visit(dependency)
        visiting.remove(unit_id)
        visited.add(unit_id)

    for unit_id in ids:
        visit(unit_id)
    return units


def _scan_conflict_reasons(first: dict[str, Any], second: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    shared = sorted(set(first["shared_resources"]) & set(second["shared_resources"]))
    if shared:
        reasons.append("shared exclusive resource: " + ", ".join(shared))
    for write_path in first["writes"]:
        if any(_scan_path_overlap(write_path, other) for other in second["writes"]):
            reasons.append(f"overlapping writes: {write_path}")
        if any(_scan_path_overlap(write_path, other) for other in second["reads"]):
            reasons.append(f"write/read overlap: {write_path}")
    for write_path in second["writes"]:
        if any(_scan_path_overlap(write_path, other) for other in first["reads"]):
            reasons.append(f"read/write overlap: {write_path}")
    if first["force_serial"] or second["force_serial"]:
        reasons.append("an unisolated side effect must run alone")
    return list(dict.fromkeys(reasons))


def _discover_project_work_units(project: Path, task_summary: str) -> list[dict[str, Any]]:
    inventory = _project_inventory(project)
    hint_tokens = {token for token in re.findall(r"[A-Za-z0-9_-]{3,}", task_summary.lower())}
    candidates: list[dict[str, Any]] = []
    package_files = [
        relative for relative in inventory["relative_files"]
        if Path(relative).name == "package.json" and relative.count("/") <= 3
    ][:24]
    for relative in package_files:
        package_file = project / relative
        try:
            payload = json.loads(package_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        scripts = payload.get("scripts") if isinstance(payload, dict) else None
        if not isinstance(scripts, dict):
            continue
        cwd = package_file.parent
        manager = "pnpm" if (project / "pnpm-lock.yaml").exists() else "yarn" if (project / "yarn.lock").exists() else "bun" if (project / "bun.lockb").exists() else "npm"
        for name, command in list(scripts.items())[:32]:
            if not isinstance(name, str) or not isinstance(command, str):
                continue
            label = f"{name} {command}".lower()
            candidate_id = re.sub(r"[^A-Za-z0-9_.:-]+", "-", f"script-{cwd.name}-{name}")[:128]
            candidates.append(
                {
                    "id": candidate_id,
                    "source": relative,
                    "kind": "package_script",
                    "label": name,
                    "argv": [manager, "run", name],
                    "cwd": str(cwd),
                    "task_hint_match": bool(hint_tokens & set(re.findall(r"[A-Za-z0-9_-]{3,}", label))),
                }
            )
    for target in inventory["make_targets"][:256]:
        if target.startswith("."):
            continue
        target_tokens = set(re.findall(r"[A-Za-z0-9_-]{3,}", target.lower()))
        candidates.append(
            {
                "id": re.sub(r"[^A-Za-z0-9_.:-]+", "-", f"make-{target}")[:128],
                "source": "Makefile",
                "kind": "make_target",
                "label": target,
                "argv": ["make", target],
                "cwd": str(project),
                "task_hint_match": target.lower() in task_summary.lower() or bool(hint_tokens & target_tokens),
            }
        )
    compose_names = {"compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml"}
    for relative in [item for item in inventory["relative_files"] if Path(item).name in compose_names][:8]:
        compose_file = project / relative
        try:
            lines = compose_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        in_services = False
        for line in lines:
            if re.match(r"^services:\s*(?:#.*)?$", line):
                in_services = True
                continue
            if in_services and line and not line.startswith((" ", "\t", "#")):
                break
            match = re.match(r"^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?$", line) if in_services else None
            if match:
                service = match.group(1)
                candidates.append(
                    {
                        "id": re.sub(r"[^A-Za-z0-9_.:-]+", "-", f"compose-{service}")[:128],
                        "source": relative,
                        "kind": "compose_service",
                        "label": service,
                        "argv": ["docker", "compose", "-f", str(compose_file), "up", service],
                        "cwd": str(compose_file.parent),
                        "task_hint_match": service.lower() in task_summary.lower(),
                    }
                )
    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        unique.setdefault(candidate["id"], candidate)
    return sorted(unique.values(), key=lambda item: (not item["task_hint_match"], item["kind"], item["id"]))[:96]


def _advisory_parallel_scan_v08(arguments: dict[str, Any]) -> dict[str, Any]:
    """Retained internal analyzer for scenario/forecast comparisons only.

    It is deliberately not registered as an MCP tool and cannot authorize or
    execute work. The public compatibility name is rebound below to the typed
    compiler boundary.
    """
    project_text = arguments.get("project_path")
    if project_text is not None and (not isinstance(project_text, str) or not os.path.isabs(project_text)):
        raise InputError("project_path must be an absolute path when supplied")
    project = Path(project_text).resolve() if project_text else Path.cwd().resolve()
    if not project.is_dir():
        raise InputError(f"project_path does not exist or is not a directory: {project}")
    task_summary = arguments.get("task_summary", "")
    if not isinstance(task_summary, str) or len(task_summary) > 8_000:
        raise InputError("task_summary must be a string with at most 8000 characters")
    discover_project = arguments.get("discover_project_commands", True)
    include_scenarios = arguments.get("include_scenario_context", True)
    include_history = arguments.get("include_trace_history", False)
    if not all(isinstance(value, bool) for value in (discover_project, include_scenarios, include_history)):
        raise InputError("discover_project_commands, include_scenario_context, and include_trace_history must be boolean")
    units = _normalize_scan_units(arguments.get("candidate_units"), project)
    profile_counts = Counter(unit["profile"] for unit in units)
    if profile_counts:
        if profile_counts.get("accelerator"):
            profile = "accelerator"
        elif len(profile_counts) == 1:
            profile = next(iter(profile_counts))
        else:
            profile = "mixed"
    else:
        profile = "mixed"
    memory_estimates = [unit["estimated_memory_mb"] for unit in units if unit["estimated_memory_mb"]]
    resource_plan = concurrency_plan(
        profile,
        arguments.get("max_concurrency"),
        arguments.get("reserve_cores"),
        max(memory_estimates) if memory_estimates else None,
        arguments.get("responsiveness", "interactive"),
    )
    conflicts: list[dict[str, Any]] = []
    conflict_pairs: dict[frozenset[str], list[str]] = {}
    unit_map = {unit["id"]: unit for unit in units}

    def depends_transitively(unit_id: str, possible_dependency: str, seen: set[str] | None = None) -> bool:
        if seen is None:
            seen = set()
        if unit_id in seen:
            return False
        seen.add(unit_id)
        direct = unit_map[unit_id]["depends_on"]
        return possible_dependency in direct or any(
            depends_transitively(dependency, possible_dependency, seen) for dependency in direct
        )

    for index, first in enumerate(units):
        for second in units[index + 1:]:
            reasons = _scan_conflict_reasons(first, second)
            if reasons:
                pair = frozenset((first["id"], second["id"]))
                ordered = depends_transitively(first["id"], second["id"]) or depends_transitively(second["id"], first["id"])
                if not ordered:
                    conflict_pairs[pair] = reasons
                conflicts.append(
                    {
                        "units": [first["id"], second["id"]],
                        "reasons": reasons,
                        "ordered_by_dependency": ordered,
                    }
                )

    completed: set[str] = set()
    stages: list[dict[str, Any]] = []
    while len(completed) < len(units):
        ready = [
            unit for unit in units
            if unit["id"] not in completed and set(unit["depends_on"]).issubset(completed)
        ]
        if not ready:
            raise InputError("candidate unit graph contains a dependency cycle")
        waves: list[list[str]] = []
        for unit in ready:
            placed = False
            if not unit["force_serial"]:
                for wave in waves:
                    if all(
                        not conflict_pairs.get(frozenset((unit["id"], other_id)))
                        and not unit_map[other_id]["force_serial"]
                        for other_id in wave
                    ):
                        wave.append(unit["id"])
                        placed = True
                        break
            if not placed:
                waves.append([unit["id"]])
        bounded_waves: list[list[str]] = []
        concurrency = resource_plan["chosen_concurrency"]
        for wave in waves:
            bounded_waves.extend(wave[offset:offset + concurrency] for offset in range(0, len(wave), concurrency))
        stages.append(
            {
                "stage": len(stages) + 1,
                "ready_units": [unit["id"] for unit in ready],
                "execution_waves": bounded_waves,
            }
        )
        completed.update(unit["id"] for unit in ready)

    conflicting_ids = {
        unit_id for item in conflicts if not item["ordered_by_dependency"] for unit_id in item["units"]
    }
    assessments: list[dict[str, Any]] = []
    for unit in units:
        if unit["force_serial"]:
            classification = "serial"
        elif unit["tiny"]:
            classification = "batch_candidate"
        elif unit["id"] in conflicting_ids or unit["side_effect"]:
            classification = "conditional"
        else:
            classification = "parallel_ready"
        assessments.append(
            {
                key: unit[key]
                for key in (
                    "id", "kind", "argv", "cwd", "depends_on", "reads", "writes",
                    "shared_resources", "estimated_seconds", "estimated_memory_mb", "profile",
                    "side_effect", "side_effect_source", "batch_key", "hazards"
                )
            } | {"classification": classification}
        )
    wave_widths = [len(wave) for stage in stages for wave in stage["execution_waves"]]
    max_width = max(wave_widths, default=0)
    meaningful_parallel_wave = any(
        len([unit_id for unit_id in wave if not unit_map[unit_id]["tiny"] and not unit_map[unit_id]["force_serial"]]) >= 2
        for stage in stages for wave in stage["execution_waves"]
    )
    parallel_now = meaningful_parallel_wave and resource_plan["chosen_concurrency"] > 1
    has_dependencies = any(unit["depends_on"] for unit in units)
    explicit_batch_keys = {unit["batch_key"] for unit in units if unit["batch_key"]}
    same_batch = len(explicit_batch_keys) == 1 and len(units) >= 2 and all(unit["batch_key"] for unit in units)
    if parallel_now:
        executor = "parallel_dag" if has_dependencies else "parallel_map" if same_batch else "parallel_exec"
        status = "parallel_ready"
    elif any(unit["tiny"] for unit in units) and len(units) >= 2:
        executor = "coarse_batch_then_rescan"
        status = "batch_first"
    elif units:
        executor = "serial"
        status = "serial_or_blocked"
    else:
        executor = "select_candidates_then_rescan"
        status = "needs_candidate_units"

    estimates_complete = bool(units) and all(unit["estimated_seconds"] is not None for unit in units)
    forecast: dict[str, Any] | None = None
    if estimates_complete:
        serial_seconds = sum(unit["estimated_seconds"] for unit in units)
        planned_seconds = sum(
            max(unit_map[unit_id]["estimated_seconds"] for unit_id in wave)
            for stage in stages for wave in stage["execution_waves"]
        )
        forecast = {
            "serial_seconds": round(serial_seconds, 3),
            "planned_seconds": round(planned_seconds, 3),
            "estimated_time_saved_seconds": round(max(0.0, serial_seconds - planned_seconds), 3),
            "estimated_speedup": round(serial_seconds / planned_seconds, 3) if planned_seconds else None,
            "kind": "planning_estimate_not_benchmark",
        }

    scenario_context = None
    if include_scenarios:
        scenario = scenario_plan(
            {
                "project_path": str(project),
                "task_hint": task_summary,
                "include_trace_history": include_history,
                "max_scenarios": 6,
                "minimum_confidence": 0.28,
            }
        )
        scenario_context = {
            "matched_scenarios": [
                {key: item[key] for key in ("id", "title_zh", "mode", "confidence", "default_execution", "guardrails")}
                for item in scenario["matched_scenarios"]
            ],
            "high_value_target_ids": scenario["decision_summary"]["high_value_target_ids"],
            "serial_guardrail_scenario_ids": scenario["decision_summary"]["serial_guardrail_scenario_ids"],
        }
    project_candidates = _discover_project_work_units(project, task_summary) if discover_project else []
    counts = Counter(item["classification"] for item in assessments)
    return {
        "scan_scope": {
            "project_path": str(project),
            "task_summary": task_summary,
            "candidate_unit_count": len(units),
            "project_candidate_count": len(project_candidates),
        },
        "decision": {
            "status": status,
            "parallel_now": parallel_now,
            "recommended_executor": executor,
            "maximum_planned_parallel_width": max_width,
            "adaptive_concurrency": resource_plan["chosen_concurrency"],
            "reason": (
                "At least one conflict-free wave contains two substantial units."
                if parallel_now else
                "Tiny units should be combined before parallel execution."
                if status == "batch_first" else
                "No candidate units were supplied; select from the task plan or project candidates and rescan."
                if status == "needs_candidate_units" else
                "The current units are serial, conflict-bound, or limited to one runnable unit."
            ),
        },
        "classification_counts": dict(sorted(counts.items())),
        "unit_assessments": assessments,
        "conflicts": conflicts,
        "execution_stages": stages,
        "resource_plan": resource_plan,
        "forecast": forecast,
        "project_candidates": project_candidates,
        "scenario_context": scenario_context,
        "rescan_triggers": [
            "immediately before the first execution batch",
            "when the task plan adds, removes, or changes a candidate unit",
            "after each DAG stage exposes newly ready work",
            "after a failure, retry decision, or side-effect boundary changes",
            "when memory pressure, thermal state, power mode, or competing load changes materially",
        ],
        "boundary": (
            "This is an in-task advisory scan. It discovers and classifies candidate work but does not execute commands, rewrite pending tool calls, grant authorization, or prove semantic independence."
        ),
    }


def _atomic_resource_context(arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, float]]:
    responsiveness = arguments.get("responsiveness", "interactive")
    requested = arguments.get("max_concurrency")
    reserve = arguments.get("reserve_cores")
    resource_plan = concurrency_plan("mixed", requested, reserve, None, responsiveness)
    machine = resource_plan["machine"]
    available_bytes = machine.get("memory_available_bytes_approx")
    memory_mb = (
        max(256.0, float(available_bytes) / (1024 * 1024) * 0.60)
        if available_bytes else 1024.0
    )
    capacities: dict[str, float] = {
        "worker_slot": float(resource_plan["chosen_concurrency"]),
        "cpu_core": float(max(1, resource_plan["chosen_concurrency"])),
        "memory_mb": memory_mb,
        "accelerator_slot": 1.0,
        "host:timing-provenance": 1.0,
    }
    custom = arguments.get("capacities", [])
    if not isinstance(custom, list) or len(custom) > 128:
        raise InputError("capacities must be an array with at most 128 entries")
    for item in custom:
        if not isinstance(item, dict) or not isinstance(item.get("resource"), str):
            raise InputError("capacity entries require a string resource")
        value = item.get("capacity")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise InputError("capacity must be a finite positive number")
        capacities[item["resource"]] = float(value)
    return resource_plan, capacities


def _compiled_plan_envelope_hash(plan: dict[str, Any]) -> str:
    """Hash every public plan field while avoiding the self-hash cycle."""
    try:
        canonical = json.loads(
            json.dumps(plan, ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise InputError(f"compiled plan envelope must be canonical JSON: {exc}") from exc
    canonical.pop("plan_hash", None)
    contract = canonical.get("execution_contract")
    if isinstance(contract, dict):
        contract_arguments = contract.get("arguments")
        if isinstance(contract_arguments, dict) and "plan_hash" in contract_arguments:
            contract_arguments["plan_hash"] = "<self>"
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def atomic_task_plan(arguments: dict[str, Any]) -> dict[str, Any]:
    """Compile task entrypoints and explicit atoms into one immutable executable plan."""
    project_text = arguments.get("project_path")
    if not isinstance(project_text, str) or not os.path.isabs(project_text):
        raise InputError("project_path must be an absolute path")
    project = Path(project_text).resolve()
    if not project.is_dir():
        raise InputError(f"project_path does not exist or is not a directory: {project}")
    task_summary = arguments.get("task_summary", "")
    if not isinstance(task_summary, str) or len(task_summary) > 8_000:
        raise InputError("task_summary must be a string with at most 8000 characters")
    raw_atoms = arguments.get("atoms", [])
    if not isinstance(raw_atoms, list):
        raise InputError("atoms must be an array")
    try:
        frontend = compile_entrypoints(project, arguments.get("entrypoints", []))
        resource_plan, capacities = _atomic_resource_context(arguments)
        compiled = compile_atomic_plan(
            [*frontend["atoms"], *raw_atoms],
            project,
            capacities=capacities,
            snapshots=frontend.get("snapshots", []),
            diagnostics=frontend.get("diagnostics", []),
            native_delegates=frontend.get("native_delegates", []),
            relaxation_candidates=frontend.get("relaxation_candidates", []),
        )
    except AtomError as exc:
        raise InputError(str(exc)) from exc
    compiled["semantic_hash"] = compiled["plan_hash"]
    compiled["resource_plan"] = resource_plan
    compiled["task_summary"] = task_summary
    compiled["project_candidates"] = (
        _discover_project_work_units(project, task_summary)
        if arguments.get("discover_project_commands", False) else []
    )
    compiled["execution_contract"] = {
        "tool": "atomic_exec",
        "arguments": {"compiled_plan": "<this entire object>", "plan_hash": "<self>"},
        "immutable": True,
        "source_snapshot_revalidated_at_execution": True,
        "manual_wave_translation_forbidden": True,
    }
    compiled["plan_hash"] = _compiled_plan_envelope_hash(compiled)
    compiled["execution_contract"]["arguments"]["plan_hash"] = compiled["plan_hash"]
    return compiled


def _legacy_units_as_atoms(arguments: dict[str, Any], project: Path) -> list[dict[str, Any]]:
    units = _normalize_scan_units(arguments.get("candidate_units"), project)
    atoms: list[dict[str, Any]] = []
    for unit in units:
        accesses = [
            *({"resource": item, "mode": "read"} for item in unit["reads"]),
            *({"resource": item, "mode": "overwrite"} for item in unit["writes"]),
        ]
        effects = []
        for resource in unit["shared_resources"]:
            domain, separator, key = resource.partition(":")
            effects.append(
                {"domain": domain if separator else "logical-resource", "key": key if separator else domain, "mode": "write"}
            )
        declared_effects = bool(accesses or effects or unit["kind"] == "read")
        blockers = [] if declared_effects else ["LEGACY_EFFECT_SET_INCOMPLETE"]
        atoms.append(
            {
                "id": unit["id"],
                "operation": {
                    "kind": unit["kind"] if unit["kind"] != "other" else "command",
                    "argv": unit["argv"],
                    "cwd": unit["cwd"],
                    "completion": "process_exit",
                    "internal_parallelism": {"kind": "unknown", "tokens": None},
                },
                "dependencies": [{"atom": item, "kind": "success"} for item in unit["depends_on"]],
                "accesses": accesses,
                "effects": effects,
                "side_effect": unit["side_effect"],
                "profile": unit["profile"],
                "cost": {
                    "duration_seconds": unit["estimated_seconds"],
                    "memory_mb": unit["estimated_memory_mb"],
                },
                "semantics": {
                    "idempotent": None,
                    "retryable": None,
                    "deterministic": None,
                    "cacheable": False,
                    "commutative": False,
                    "cancel_safe": None,
                    "splittable": None,
                    "reorderable": "explicit",
                },
                "batch": {"key": unit["batch_key"], "strategy": "same_argv_shape"} if unit["batch_key"] else None,
                "assurance": {
                    "parse": "exact",
                    "control": "exact",
                    "effects": "complete_declared" if declared_effects else "unknown",
                    "codegen": "exact_argv",
                    "rank": 1.0 if declared_effects else 0.45,
                    "blockers": blockers,
                },
                "provenance": {
                    "adapter": "legacy_candidate_unit",
                    "source": "task_parallel_scan",
                    "symbol": unit["id"],
                    "confidence": 1.0,
                },
            }
        )
    return atoms


# Compatibility boundary for 0.8 clients. Unlike the former advisory scanner,
# this wrapper returns the exact CompiledPlan consumed by atomic_exec.
def task_parallel_scan(arguments: dict[str, Any]) -> dict[str, Any]:
    project_text = arguments.get("project_path")
    if project_text is not None and (not isinstance(project_text, str) or not os.path.isabs(project_text)):
        raise InputError("project_path must be an absolute path when supplied")
    project = Path(project_text).resolve() if project_text else Path.cwd().resolve()
    translated = {
        "project_path": str(project),
        "task_summary": arguments.get("task_summary", ""),
        "atoms": _legacy_units_as_atoms(arguments, project),
        "entrypoints": [],
        "responsiveness": arguments.get("responsiveness", "interactive"),
        "max_concurrency": arguments.get("max_concurrency"),
        "reserve_cores": arguments.get("reserve_cores"),
        "discover_project_commands": arguments.get("discover_project_commands", True),
    }
    compiled = atomic_task_plan(translated)
    schedule = compiled.get("schedule", {})
    peak = schedule.get("peak_parallelism", 0)
    eligible = bool(compiled.get("execution_eligible"))
    status = "parallel_ready" if eligible and peak > 1 else "serial_ready" if eligible else "serial_or_blocked"
    return {
        "decision": {
            "status": status,
            "parallel_now": status == "parallel_ready",
            "recommended_executor": "atomic_exec" if eligible else "none",
            "reason": (
                "Compiled Atom IR is eligible and has concurrent admissions."
                if status == "parallel_ready" else
                "Compiled Atom IR is eligible but currently serial."
                if status == "serial_ready" else
                "Compilation found blockers; execution is refused until effects or control semantics are made exact."
            ),
        },
        "plan_hash": compiled["plan_hash"],
        "compiled_plan": compiled,
        "diagnostics": compiled.get("diagnostics", []),
        "execution_contract": compiled["execution_contract"],
        "deprecation": "Use atomic_task_plan for typed entrypoints and explicit Atom IR.",
    }


def _record_time_saved(seconds: float) -> dict[str, Any]:
    """Atomically add one completed invocation to the per-user cumulative totals."""
    path = _stats_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            try:
                current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, json.JSONDecodeError):
                current = {}
            run_count = int(current.get("run_count", 0)) + 1
            cumulative = float(current.get("cumulative_saved_seconds", 0.0)) + seconds
            updated = {
                "run_count": run_count,
                "cumulative_saved_seconds": round(cumulative, 6),
                "updated_at_epoch_seconds": round(time.time(), 3),
            }
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix="stats-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                json.dump(updated, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
            return updated
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class ProgressReporter:
    """Emit monotonic MCP progress heartbeats while a scheduler invocation is active."""

    def __init__(
        self,
        task_count: int,
        callback: Any | None,
        started: float,
    ) -> None:
        self.task_count = task_count
        self.callback = callback
        self.started = started
        self.active: dict[str, float] = {}
        self.completed: list[dict[str, Any]] = []
        self.ready_tasks = 0
        self._ticker: asyncio.Task[None] | None = None

    def scheduler_state(self, *, ready_tasks: int) -> None:
        self.ready_tasks = max(0, int(ready_tasks))
        self.emit()

    def task_started(self, task_id: str) -> None:
        self.active[task_id] = time.monotonic()
        self.emit()

    def task_finished(self, result: dict[str, Any]) -> None:
        self.active.pop(str(result.get("id", "")), None)
        self.completed.append(result)
        self.emit()

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        elapsed = max(0.0, now - self.started)
        completed_runtime = sum(
            float(item.get("duration_seconds", 0.0))
            for item in self.completed
            if item.get("status") != "skipped"
        )
        active_runtime = sum(max(0.0, now - task_started) for task_started in self.active.values())
        saved_so_far = completed_runtime + active_runtime - elapsed
        failed = sum(
            1 for item in self.completed if item.get("status") in {"failed", "timed_out"}
        )
        return {
            "elapsed_seconds": round(elapsed, 3),
            "running_tasks": len(self.active),
            "ready_tasks": self.ready_tasks,
            "completed_tasks": len(self.completed),
            "task_count": self.task_count,
            "failed_tasks": failed,
            "estimated_saved_so_far_seconds": round(saved_so_far, 3),
        }

    def emit(self) -> None:
        if self.callback is not None:
            self.callback(self.snapshot())

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(_progress_interval_seconds())
            self.emit()

    async def start(self) -> None:
        self.emit()
        if self.callback is not None:
            self._ticker = asyncio.create_task(self._tick())

    async def stop(self) -> None:
        self.emit()
        if self._ticker is not None:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
            self._ticker = None


def _run_probe(argv: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if completed.returncode == 0:
            return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _int_probe(argv: list[str], fallback: int) -> int:
    raw = _run_probe(argv)
    try:
        return int(raw) if raw else fallback
    except ValueError:
        return fallback


def _json_probe(argv: list[str]) -> Any | None:
    raw = _run_probe(argv)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _performance_levels() -> list[dict[str, Any]]:
    if platform.system() != "Darwin":
        return []
    count = _int_probe(["sysctl", "-n", "hw.nperflevels"], 0)
    levels: list[dict[str, Any]] = []
    for index in range(max(0, min(count, 8))):
        prefix = f"hw.perflevel{index}"
        physical = _int_probe(["sysctl", "-n", f"{prefix}.physicalcpu"], 0)
        logical = _int_probe(["sysctl", "-n", f"{prefix}.logicalcpu"], physical)
        name = _run_probe(["sysctl", "-n", f"{prefix}.name"])
        if physical:
            levels.append(
                {
                    "index": index,
                    "name": name or f"level-{index}",
                    "physical_cpus": physical,
                    "logical_cpus": logical,
                }
            )
    return levels


def _process_info_state() -> dict[str, Any]:
    if platform.system() != "Darwin" or not shutil.which("osascript"):
        return {}
    script = (
        'ObjC.import("Foundation"); '
        "var p=$.NSProcessInfo.processInfo; "
        "JSON.stringify({thermal_state:Number(p.thermalState),"
        "low_power_mode:Boolean(p.isLowPowerModeEnabled),"
        "active_processor_count:Number(p.activeProcessorCount)})"
    )
    data = _json_probe(["osascript", "-l", "JavaScript", "-e", script])
    return data if isinstance(data, dict) else {}


def _gpu_snapshot() -> dict[str, Any] | None:
    if platform.system() != "Darwin" or not shutil.which("system_profiler"):
        return None
    data = _json_probe(["system_profiler", "SPDisplaysDataType", "-json"])
    devices = data.get("SPDisplaysDataType", []) if isinstance(data, dict) else []
    for device in devices:
        if not isinstance(device, dict) or device.get("sppci_device_type") != "spdisplays_gpu":
            continue
        cores_raw = device.get("sppci_cores")
        try:
            cores = int(cores_raw) if cores_raw is not None else None
        except (TypeError, ValueError):
            cores = None
        return {
            "name": device.get("sppci_model") or device.get("_name"),
            "cores": cores,
            "metal_family": device.get("spdisplays_mtlgpufamilysupport"),
        }
    return None


def _power_snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {"source": None, "battery_percent": None}
    if platform.system() != "Darwin":
        return result
    raw = _run_probe(["pmset", "-g", "batt"])
    if not raw:
        return result
    if "AC Power" in raw:
        result["source"] = "ac"
    elif "Battery Power" in raw:
        result["source"] = "battery"
    match = re.search(r"(\d+)%", raw)
    if match:
        result["battery_percent"] = int(match.group(1))
    return result


def _memory_free_percent() -> int | None:
    if platform.system() != "Darwin":
        return None
    raw = _run_probe(["memory_pressure", "-Q"])
    match = re.search(r"free percentage:\s*(\d+)%", raw or "")
    return int(match.group(1)) if match else None


def _static_hardware_snapshot() -> dict[str, Any]:
    global _STATIC_HARDWARE_CACHE
    if _STATIC_HARDWARE_CACHE is None:
        logical = os.cpu_count() or 1
        physical = logical
        memory_total = 0
        performance_levels: list[dict[str, Any]] = []
        chip = None
        model_identifier = None
        if platform.system() == "Darwin":
            logical = _int_probe(["sysctl", "-n", "hw.logicalcpu"], logical)
            physical = _int_probe(["sysctl", "-n", "hw.physicalcpu"], logical)
            memory_total = _int_probe(["sysctl", "-n", "hw.memsize"], 0)
            chip = _run_probe(["sysctl", "-n", "machdep.cpu.brand_string"])
            model_identifier = _run_probe(["sysctl", "-n", "hw.model"])
            performance_levels = _performance_levels()
        _STATIC_HARDWARE_CACHE = {
            "logical_cpus": logical,
            "physical_cpus": physical,
            "memory_total_bytes": memory_total or None,
            "performance_levels": performance_levels,
            "chip": chip,
            "model_identifier": model_identifier,
            "gpu": _gpu_snapshot(),
        }
    return _STATIC_HARDWARE_CACHE


def _available_memory_bytes() -> int | None:
    if platform.system() == "Darwin":
        raw = _run_probe(["vm_stat"])
        if not raw:
            return None
        page_match = re.search(r"page size of (\d+) bytes", raw)
        page_size = int(page_match.group(1)) if page_match else 4096
        counts: dict[str, int] = {}
        for line in raw.splitlines():
            match = re.match(r"([^:]+):\s+([0-9]+)\.?$", line.strip())
            if match:
                counts[match.group(1)] = int(match.group(2))
        available_pages = sum(
            counts.get(name, 0)
            for name in (
                "Pages free",
                "Pages inactive",
                "Pages speculative",
                "Pages purgeable",
            )
        )
        return available_pages * page_size if available_pages else None

    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def machine_snapshot() -> dict[str, Any]:
    static = _static_hardware_snapshot()
    logical = static["logical_cpus"]
    physical = static["physical_cpus"]
    memory_total = static["memory_total_bytes"]
    process_state: dict[str, Any] = {}
    performance_levels = static["performance_levels"]
    chip = static["chip"]
    model_identifier = static["model_identifier"]

    if platform.system() == "Darwin":
        logical = _int_probe(["sysctl", "-n", "hw.logicalcpu"], logical)
        process_state = _process_info_state()
        active = process_state.get("active_processor_count")
        if isinstance(active, int) and active > 0:
            logical = min(logical, active)

    load1, load5, load15 = os.getloadavg()
    performance_cores = sum(
        item["physical_cpus"] for item in performance_levels if "performance" in item["name"].lower()
    )
    efficiency_cores = sum(
        item["physical_cpus"] for item in performance_levels if "efficiency" in item["name"].lower()
    )
    thermal_names = {0: "nominal", 1: "fair", 2: "serious", 3: "critical"}
    thermal_value = process_state.get("thermal_state")
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "apple_silicon": platform.system() == "Darwin" and platform.machine() == "arm64",
        "chip": chip,
        "model_identifier": model_identifier,
        "logical_cpus": max(1, logical),
        "physical_cpus": max(1, physical),
        "performance_cores": performance_cores or None,
        "efficiency_cores": efficiency_cores or None,
        "performance_levels": performance_levels,
        "gpu": static["gpu"],
        "memory_total_bytes": memory_total,
        "memory_available_bytes_approx": _available_memory_bytes(),
        "memory_free_percent": _memory_free_percent(),
        "load_average": {"one_minute": load1, "five_minutes": load5, "fifteen_minutes": load15},
        "thermal_state": thermal_names.get(thermal_value, "unknown"),
        "low_power_mode": process_state.get("low_power_mode"),
        "power": _power_snapshot(),
    }


def concurrency_plan(
    profile: str,
    requested: int | None = None,
    reserve_cores: int | None = None,
    estimated_memory_mb_per_task: float | None = None,
    responsiveness: str = "interactive",
) -> dict[str, Any]:
    if profile not in {"cpu", "io", "mixed", "accelerator"}:
        raise InputError("profile must be one of: cpu, io, mixed, accelerator")
    if responsiveness not in {"interactive", "balanced", "throughput"}:
        raise InputError("responsiveness must be one of: interactive, balanced, throughput")
    if reserve_cores is not None and (reserve_cores < 0 or reserve_cores > MAX_CONCURRENCY):
        raise InputError(f"reserve_cores must be between 0 and {MAX_CONCURRENCY}")
    if requested is not None and (requested < 1 or requested > MAX_CONCURRENCY):
        raise InputError(f"max_concurrency must be between 1 and {MAX_CONCURRENCY}")
    if estimated_memory_mb_per_task is not None and estimated_memory_mb_per_task <= 0:
        raise InputError("estimated_memory_mb_per_task must be positive")

    snapshot = machine_snapshot()
    logical = snapshot["logical_cpus"]
    physical = snapshot["physical_cpus"]
    reserve_fractions = {"interactive": 0.25, "balanced": 0.15, "throughput": 0.06}
    minimum_reserve = 2 if logical >= 8 and responsiveness == "interactive" else 1
    adaptive_reserve = max(minimum_reserve, math.ceil(logical * reserve_fractions[responsiveness]))
    effective_reserve = reserve_cores if reserve_cores is not None else adaptive_reserve
    effective_reserve = min(effective_reserve, max(0, logical - 1))
    if profile == "cpu":
        base = max(1, physical - effective_reserve)
    elif profile == "io":
        oversubscription = {"interactive": 1.25, "balanced": 1.75, "throughput": 2.0}[responsiveness]
        base = min(MAX_CONCURRENCY, max(2, math.floor((logical - effective_reserve) * oversubscription)))
    elif profile == "accelerator":
        base = 1 if responsiveness == "interactive" else 2
    else:
        base = max(1, logical - effective_reserve)

    reasons = [
        f"{profile} workload on {snapshot.get('chip') or snapshot['machine']}",
        f"{responsiveness} mode reserves {effective_reserve} active CPU cores for system responsiveness",
    ]
    load1 = snapshot["load_average"]["one_minute"]
    if profile in {"cpu", "mixed"}:
        load_limited = max(1, math.floor(logical - effective_reserve - min(load1, logical - 1)))
        if load_limited < base:
            base = load_limited
            reasons.append("reduced for processes already consuming CPU")
    elif load1 >= logical * 0.8:
        base = max(1, math.ceil(base * 0.75))
        reasons.append("reduced I/O fan-out because current CPU load is elevated")

    thermal_factors = {"fair": 0.8, "serious": 0.5, "critical": 0.25}
    thermal = snapshot["thermal_state"]
    if thermal in thermal_factors:
        base = max(1, math.floor(base * thermal_factors[thermal]))
        reasons.append(f"reduced because thermal state is {thermal}")

    if snapshot["low_power_mode"]:
        base = max(1, math.floor(base * 0.6))
        reasons.append("reduced because macOS Low Power Mode is enabled")
    elif snapshot["power"]["source"] == "battery" and responsiveness == "interactive":
        base = max(1, math.floor(base * 0.8))
        reasons.append("reduced while running on battery in interactive mode")

    memory_free_percent = snapshot["memory_free_percent"]
    if memory_free_percent is not None and memory_free_percent < 10:
        base = max(1, math.floor(base * 0.5))
        reasons.append("reduced 50% because system memory pressure is high")
    elif memory_free_percent is not None and memory_free_percent < 20:
        base = max(1, math.floor(base * 0.75))
        reasons.append("reduced 25% because system memory headroom is low")

    memory_limit = None
    available = snapshot["memory_available_bytes_approx"]
    if estimated_memory_mb_per_task and available:
        per_task = estimated_memory_mb_per_task * 1024 * 1024
        memory_headroom = {"interactive": 0.4, "balanced": 0.3, "throughput": 0.2}[responsiveness]
        usable = available * (1.0 - memory_headroom)
        memory_limit = max(1, int(usable // per_task))
        if memory_limit < base:
            base = memory_limit
            reasons.append(f"reduced to keep {memory_headroom:.0%} of available memory as headroom")

    chosen = min(requested, base) if requested is not None else base
    if requested is not None:
        reasons.append("explicit max_concurrency is treated as a ceiling, not a safety override")
    chosen = max(1, min(MAX_CONCURRENCY, chosen))
    nice_adjustment = {"interactive": 10, "balanced": 5, "throughput": 0}[responsiveness]
    qos_clamp = "utility" if responsiveness == "interactive" and platform.system() == "Darwin" else None
    return {
        "profile": profile,
        "responsiveness": responsiveness,
        "recommended_concurrency": base,
        "chosen_concurrency": chosen,
        "reserve_cores": effective_reserve,
        "reserve_cores_source": "explicit" if reserve_cores is not None else "adaptive",
        "nice_adjustment": nice_adjustment,
        "qos_clamp": qos_clamp,
        "estimated_memory_mb_per_task": estimated_memory_mb_per_task,
        "memory_limited_concurrency": memory_limit,
        "reasons": reasons,
        "machine": snapshot,
    }


def _xcrun_tool(name: str) -> str | None:
    if not shutil.which("xcrun"):
        return None
    return _run_probe(["xcrun", "-f", name])


def accelerator_inventory() -> dict[str, Any]:
    snapshot = machine_snapshot()
    packages = {
        name: importlib.util.find_spec(name) is not None
        for name in ("mlx", "torch", "coremltools", "tensorflow", "numpy")
    }
    numpy_accelerate = False
    if packages["numpy"]:
        config = _run_probe(
            [sys.executable, "-c", "import numpy as np; np.show_config()"]
        )
        numpy_accelerate = "accelerate" in (config or "").lower()

    ffmpeg = shutil.which("ffmpeg")
    ffmpeg_encoders = _run_probe([ffmpeg, "-hide_banner", "-encoders"]) if ffmpeg else None
    videotoolbox_encoders = sorted(
        set(re.findall(r"\b([a-z0-9]+_videotoolbox)\b", ffmpeg_encoders or ""))
    )
    darwin = platform.system() == "Darwin"
    return {
        "apple_silicon": snapshot["apple_silicon"],
        "chip": snapshot["chip"],
        "gpu": snapshot["gpu"],
        "system_frameworks": {
            "accelerate": darwin,
            "bnns": darwin,
            "metal": darwin and snapshot["gpu"] is not None,
            "mpsgraph": darwin and snapshot["gpu"] is not None,
            "core_ml": darwin,
            "videotoolbox": darwin,
        },
        "developer_tools": {
            "metal_compiler": _xcrun_tool("metal"),
            "coremlcompiler": _xcrun_tool("coremlcompiler"),
        },
        "python_packages": packages,
        "numpy_uses_accelerate": numpy_accelerate,
        "ffmpeg": ffmpeg,
        "ffmpeg_videotoolbox_encoders": videotoolbox_encoders,
    }


def accelerator_plan(workload: str, responsiveness: str = "interactive") -> dict[str, Any]:
    allowed = {
        "auto",
        "general",
        "linear_algebra",
        "signal",
        "image",
        "ml_inference",
        "ml_training",
        "video",
        "compression",
        "custom_gpu",
    }
    if workload not in allowed:
        raise InputError(f"workload must be one of: {', '.join(sorted(allowed))}")
    if responsiveness not in {"interactive", "balanced", "throughput"}:
        raise InputError("responsiveness must be one of: interactive, balanced, throughput")

    inventory = accelerator_inventory()
    packages = inventory["python_packages"]
    frameworks = inventory["system_frameworks"]
    selected = "parallel_cpu"
    indicator = "🧩 CPU 多进程"
    recommendations: list[str] = []

    if workload in {"linear_algebra", "signal", "image"} and frameworks["accelerate"]:
        selected = "accelerate"
        indicator = "🧮 Accelerate / BNNS"
        recommendations.append(
            "Use Accelerate primitives (BLAS/LAPACK, vDSP, vImage, vForce, or BNNS) so macOS selects optimized CPU vector instructions at runtime."
        )
        if packages["numpy"]:
            if inventory["numpy_uses_accelerate"]:
                recommendations.append("The detected NumPy build reports Apple's Accelerate backend.")
            else:
                recommendations.append("NumPy is installed, but its Accelerate linkage was not confirmed.")
    elif workload == "ml_inference" and frameworks["core_ml"]:
        selected = "core_ml_all_compute_units"
        indicator = "🧠 Core ML｜CPU + GPU + ANE"
        recommendations.append(
            "Prefer Core ML with MLComputeUnits.all so the operating system can select CPU, GPU, and Apple Neural Engine per model."
        )
        if packages["mlx"]:
            recommendations.append("MLX is installed and is an alternative for Apple-silicon-native model execution.")
    elif workload == "ml_training":
        if packages["mlx"]:
            selected = "mlx"
            indicator = "🧠 MLX｜统一内存 + GPU"
            recommendations.append("Use the installed MLX package for Apple-silicon-native training workloads.")
        elif packages["torch"]:
            selected = "pytorch_mps"
            indicator = "🎛️ PyTorch MPS｜GPU"
            recommendations.append("Use PyTorch's `mps` device and keep tensors on-device between operations.")
        else:
            selected = "mpsgraph_or_mlx"
            indicator = "🎛️ MPSGraph / MLX｜需接入"
            recommendations.append("Use MPSGraph directly or install MLX/PyTorch with MPS support.")
    elif workload == "video" and frameworks["videotoolbox"]:
        selected = "videotoolbox"
        indicator = "🎞️ VideoToolbox｜媒体引擎"
        encoders = inventory["ffmpeg_videotoolbox_encoders"]
        if encoders:
            recommendations.append(
                f"The detected ffmpeg exposes hardware encoders: {', '.join(encoders)}. Select one explicitly for supported transcodes."
            )
        else:
            recommendations.append("Use AVFoundation or VideoToolbox APIs for hardware encode/decode.")
    elif workload == "custom_gpu" and frameworks["metal"]:
        selected = "metal_or_mpsgraph"
        indicator = "🎛️ Metal / MPSGraph｜GPU"
        recommendations.append("Use Metal compute kernels or MPSGraph for large, data-parallel operators.")
    elif workload == "compression":
        selected = "apple_compression_or_parallel_cpu"
        indicator = "🗜️ Compression / Apple Archive"
        recommendations.append("Prefer Apple's Compression or Apple Archive frameworks; parallelize independent files.")
    else:
        recommendations.append(
            "No universal operator offload is safe for this workload; use adaptive CPU subprocess parallelism."
        )

    if responsiveness == "interactive":
        recommendations.append(
            "Keep interactive mode: reserve adaptive CPU and memory headroom and lower worker process priority."
        )
    return {
        "workload": workload,
        "selected_backend": selected,
        "backend_indicator": indicator,
        "responsiveness": responsiveness,
        "transparent_offload": False,
        "recommendations": recommendations,
        "inventory": inventory,
        "boundary": (
            "The plugin can select and describe a backend, but the invoked program must implement or expose that backend; arbitrary commands cannot be transparently rewritten into Metal, Accelerate, or ANE operators."
        ),
    }


def docker_snapshot() -> dict[str, Any]:
    docker = shutil.which("docker")
    if not docker:
        return {
            "available": False,
            "reason": "docker CLI was not found",
            "vm_cpus": None,
            "vm_memory_bytes": None,
        }
    info = _json_probe([docker, "info", "--format", "{{json .}}"])
    if not isinstance(info, dict):
        return {
            "available": False,
            "reason": "docker daemon information was unavailable; Docker Desktop may be stopped or waking from Resource Saver",
            "vm_cpus": None,
            "vm_memory_bytes": None,
            "client_version": _run_probe([docker, "version", "--format", "{{.Client.Version}}"]),
        }
    return {
        "available": True,
        "client_version": _run_probe([docker, "version", "--format", "{{.Client.Version}}"]),
        "server_version": info.get("ServerVersion"),
        "compose_version": _run_probe([docker, "compose", "version", "--short"]),
        "buildkit_version": _run_probe([docker, "buildx", "version"]),
        "vm_cpus": info.get("NCPU"),
        "vm_memory_bytes": info.get("MemTotal"),
        "operating_system": info.get("OperatingSystem"),
        "architecture": info.get("Architecture"),
        "kernel_version": info.get("KernelVersion"),
        "storage_driver": info.get("Driver"),
        "is_docker_desktop": "docker desktop" in str(info.get("OperatingSystem") or "").lower(),
    }


def _weighted_allocation(
    total: float,
    weights: list[float],
    minimums: list[float],
    maximums: list[float],
) -> tuple[list[float], bool]:
    count = len(weights)
    allocations = [max(0.0, minimums[index]) for index in range(count)]
    minimum_overcommit = sum(allocations) > total + 1e-9
    if minimum_overcommit:
        scale = total / max(sum(allocations), 1e-9)
        return [value * scale for value in allocations], True

    remaining = max(0.0, total - sum(allocations))
    active = {index for index in range(count) if allocations[index] < maximums[index] - 1e-9}
    for _ in range(count + 2):
        if remaining <= 1e-9 or not active:
            break
        active_weight = sum(weights[index] for index in active)
        if active_weight <= 0:
            break
        used = 0.0
        saturated: set[int] = set()
        for index in active:
            share = remaining * weights[index] / active_weight
            headroom = max(0.0, maximums[index] - allocations[index])
            addition = min(share, headroom)
            allocations[index] += addition
            used += addition
            if addition >= headroom - 1e-9:
                saturated.add(index)
        remaining = max(0.0, remaining - used)
        active -= saturated
        if used <= 1e-9:
            break
    return allocations, False


def _compose_resource_yaml(allocations: list[dict[str, Any]]) -> str:
    lines = ["services:"]
    for item in allocations:
        lines.extend(
            [
                f"  {item['id']}:",
                f"    cpus: {item['cpus']:.2f}",
                f"    cpu_shares: {item['cpu_shares']}",
                f'    mem_limit: "{item["memory_limit_mb"]}m"',
                f'    mem_reservation: "{item["memory_reservation_mb"]}m"',
                f"    pids_limit: {item['pids_limit']}",
            ]
        )
        if item.get("cpuset"):
            lines.append(f'    cpuset: "{item["cpuset"]}"')
    return "\n".join(lines) + "\n"


def container_resource_plan(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_services = arguments.get("services")
    if not isinstance(raw_services, list) or not raw_services or len(raw_services) > 32:
        raise InputError("services must be a non-empty array with at most 32 entries")
    responsiveness = arguments.get("responsiveness", "interactive")
    if responsiveness not in {"interactive", "balanced", "throughput"}:
        raise InputError("responsiveness must be one of: interactive, balanced, throughput")
    pin_cpus = arguments.get("pin_cpus", False)
    if not isinstance(pin_cpus, bool):
        raise InputError("pin_cpus must be boolean")

    allowed_profiles = {"cpu", "io", "mixed", "database", "build", "accelerator"}
    cpu_profile_weights = {
        "cpu": 3.0,
        "build": 3.0,
        "mixed": 2.0,
        "database": 1.5,
        "io": 1.0,
        "accelerator": 1.0,
    }
    memory_profile_weights = {
        "database": 3.0,
        "build": 2.0,
        "accelerator": 2.0,
        "mixed": 1.5,
        "cpu": 1.25,
        "io": 1.0,
    }
    default_pids = {
        "build": 512,
        "database": 512,
        "cpu": 256,
        "mixed": 256,
        "accelerator": 256,
        "io": 128,
    }
    services: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_services):
        if not isinstance(raw, dict):
            raise InputError(f"service {index} must be an object")
        service_id = raw.get("id")
        if not isinstance(service_id, str) or not TASK_ID_RE.match(service_id):
            raise InputError(f"service {index} id must match {TASK_ID_RE.pattern}")
        if service_id in seen:
            raise InputError("service IDs must be unique")
        seen.add(service_id)
        profile = raw.get("profile", "mixed")
        if profile not in allowed_profiles:
            raise InputError(
                f"service {service_id} profile must be one of: {', '.join(sorted(allowed_profiles))}"
            )
        weight = _bounded_number(raw.get("weight"), f"service {service_id} weight", 1.0, 100.0)
        requested_cpus = raw.get("requested_cpus")
        if requested_cpus is not None:
            requested_cpus = _bounded_number(
                requested_cpus, f"service {service_id} requested_cpus", 1.0, 256.0
            )
        minimum_cpus = _bounded_number(
            raw.get("minimum_cpus"), f"service {service_id} minimum_cpus", 0.25, 256.0
        )
        maximum_cpus = _bounded_number(
            raw.get("maximum_cpus"), f"service {service_id} maximum_cpus", 256.0, 256.0
        )
        if requested_cpus is not None:
            minimum_cpus = requested_cpus
            maximum_cpus = requested_cpus
        if minimum_cpus > maximum_cpus:
            raise InputError(f"service {service_id} minimum_cpus cannot exceed maximum_cpus")
        estimated_memory_mb = raw.get("estimated_memory_mb")
        if estimated_memory_mb is not None:
            estimated_memory_mb = _bounded_number(
                estimated_memory_mb,
                f"service {service_id} estimated_memory_mb",
                128.0,
                1_048_576.0,
            )
        requested_memory_mb = raw.get("requested_memory_mb")
        if requested_memory_mb is not None:
            requested_memory_mb = _bounded_number(
                requested_memory_mb,
                f"service {service_id} requested_memory_mb",
                128.0,
                1_048_576.0,
            )
        pids_limit = raw.get("pids_limit", default_pids[profile])
        if isinstance(pids_limit, bool) or not isinstance(pids_limit, int) or not 16 <= pids_limit <= 1_048_576:
            raise InputError(f"service {service_id} pids_limit must be an integer between 16 and 1048576")
        dependencies = raw.get("depends_on", [])
        if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
            raise InputError(f"service {service_id} depends_on must be an array of service IDs")
        services.append(
            {
                "id": service_id,
                "profile": profile,
                "weight": weight,
                "minimum_cpus": minimum_cpus,
                "maximum_cpus": maximum_cpus,
                "estimated_memory_mb": estimated_memory_mb,
                "requested_memory_mb": requested_memory_mb,
                "pids_limit": pids_limit,
                "depends_on": dependencies,
            }
        )

    by_id = {item["id"]: item for item in services}
    for item in services:
        unknown = set(item["depends_on"]) - set(by_id)
        if unknown:
            raise InputError(f"service {item['id']} has unknown dependencies: {sorted(unknown)}")
        if item["id"] in item["depends_on"]:
            raise InputError(f"service {item['id']} cannot depend on itself")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(service_id: str) -> None:
        if service_id in visiting:
            raise InputError("service dependency graph contains a cycle")
        if service_id in visited:
            return
        visiting.add(service_id)
        for dependency in by_id[service_id]["depends_on"]:
            visit(dependency)
        visiting.remove(service_id)
        visited.add(service_id)

    for service_id in by_id:
        visit(service_id)

    docker = docker_snapshot()
    host = machine_snapshot()
    detected_vm_cpus = docker.get("vm_cpus") if docker["available"] else None
    detected_vm_memory_bytes = docker.get("vm_memory_bytes") if docker["available"] else None
    vm_cpus_override = arguments.get("docker_vm_cpus")
    vm_memory_override = arguments.get("docker_vm_memory_mb")
    if vm_cpus_override is not None:
        vm_cpus = _bounded_number(vm_cpus_override, "docker_vm_cpus", 1.0, 256.0)
        vm_cpu_source = "explicit"
    elif isinstance(detected_vm_cpus, (int, float)) and not isinstance(detected_vm_cpus, bool):
        vm_cpus = float(detected_vm_cpus)
        vm_cpu_source = "docker_info"
    else:
        vm_cpus = float(host["logical_cpus"])
        vm_cpu_source = "host_fallback"
    if vm_memory_override is not None:
        vm_memory_mb = _bounded_number(
            vm_memory_override, "docker_vm_memory_mb", 1024.0, 1_048_576.0
        )
        vm_memory_source = "explicit"
    elif isinstance(detected_vm_memory_bytes, (int, float)) and not isinstance(
        detected_vm_memory_bytes, bool
    ):
        vm_memory_mb = float(detected_vm_memory_bytes) / (1024 * 1024)
        vm_memory_source = "docker_info"
    else:
        total_bytes = host.get("memory_total_bytes") or 8 * 1024 * 1024 * 1024
        vm_memory_mb = float(total_bytes) / (1024 * 1024)
        vm_memory_source = "host_fallback"

    reserve_cpu_fraction = {"interactive": 0.25, "balanced": 0.15, "throughput": 0.06}
    reserve_memory_fraction = {"interactive": 0.25, "balanced": 0.18, "throughput": 0.10}
    explicit_reserve_cpus = arguments.get("reserve_vm_cpus")
    if explicit_reserve_cpus is not None:
        reserve_vm_cpus = _bounded_number(
            explicit_reserve_cpus, "reserve_vm_cpus", 1.0, 256.0
        )
        reserve_cpu_source = "explicit"
    else:
        reserve_vm_cpus = float(max(1, math.ceil(vm_cpus * reserve_cpu_fraction[responsiveness])))
        reserve_cpu_source = "adaptive"
    explicit_reserve_memory = arguments.get("reserve_vm_memory_mb")
    if explicit_reserve_memory is not None:
        reserve_vm_memory_mb = _bounded_number(
            explicit_reserve_memory,
            "reserve_vm_memory_mb",
            256.0,
            1_048_576.0,
        )
        reserve_memory_source = "explicit"
    else:
        reserve_vm_memory_mb = max(
            512.0, vm_memory_mb * reserve_memory_fraction[responsiveness]
        )
        reserve_memory_source = "adaptive"
    usable_cpus = max(0.25, vm_cpus - min(reserve_vm_cpus, max(0.0, vm_cpus - 0.25)))
    usable_memory_mb = max(
        256.0,
        vm_memory_mb - min(reserve_vm_memory_mb, max(0.0, vm_memory_mb - 256.0)),
    )

    cpu_weights = [
        item["weight"] * cpu_profile_weights[item["profile"]] for item in services
    ]
    cpu_minimums = [item["minimum_cpus"] for item in services]
    cpu_maximums = [min(item["maximum_cpus"], usable_cpus) for item in services]
    cpu_allocations, cpu_overcommit = _weighted_allocation(
        usable_cpus, cpu_weights, cpu_minimums, cpu_maximums
    )

    memory_weights = [
        item["weight"] * memory_profile_weights[item["profile"]] for item in services
    ]
    memory_minimums: list[float] = []
    memory_maximums: list[float] = []
    for item in services:
        requested_memory = item["requested_memory_mb"]
        estimated_memory = item["estimated_memory_mb"]
        minimum = requested_memory or ((estimated_memory * 1.10) if estimated_memory else 128.0)
        maximum = requested_memory or usable_memory_mb
        memory_minimums.append(minimum)
        memory_maximums.append(maximum)
    memory_allocations, memory_overcommit = _weighted_allocation(
        usable_memory_mb, memory_weights, memory_minimums, memory_maximums
    )

    cpusets: dict[str, str] = {}
    pinning_warning = None
    if pin_cpus:
        available_ids = list(range(max(1, math.floor(usable_cpus))))
        if len(services) > len(available_ids):
            pinning_warning = (
                "non-overlapping cpuset was not generated because runnable services exceed usable Docker VM vCPUs"
            )
        else:
            counts = [1 for _ in services]
            remaining_ids = len(available_ids) - len(services)
            while remaining_ids > 0:
                candidates = [
                    index
                    for index, item in enumerate(services)
                    if counts[index] < max(1, math.ceil(min(cpu_allocations[index], item["maximum_cpus"])))
                ]
                if not candidates:
                    break
                index = max(candidates, key=lambda value: cpu_weights[value] / counts[value])
                counts[index] += 1
                remaining_ids -= 1
            cursor = 0
            for index, item in enumerate(services):
                assigned = available_ids[cursor : cursor + counts[index]]
                cursor += counts[index]
                cpusets[item["id"]] = ",".join(str(value) for value in assigned)

    allocations: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not docker["available"]:
        warnings.append(docker["reason"])
    if vm_cpu_source == "host_fallback" or vm_memory_source == "host_fallback":
        warnings.append(
            "Docker VM capacity was not detected; verify Docker Desktop Resources before applying the host-based fallback"
        )
    if cpu_overcommit:
        warnings.append(
            "requested minimum CPU totals exceed the usable Docker VM CPU envelope; allocations were proportionally reduced"
        )
    if memory_overcommit:
        warnings.append(
            "estimated/requested memory totals exceed the usable Docker VM memory envelope; not all services are safe to run simultaneously"
        )
    if pinning_warning:
        warnings.append(pinning_warning)
    if docker.get("is_docker_desktop"):
        warnings.append(
            "Docker Desktop cpuset IDs are Linux VM vCPUs, not stable mappings to Apple performance or efficiency cores"
        )
    if any(item["profile"] == "accelerator" for item in services):
        warnings.append(
            "ordinary local Docker Desktop Linux containers do not receive transparent access to Apple GPU, ANE, Metal, or media engines"
        )

    max_weight = max(cpu_weights)
    for index, item in enumerate(services):
        cpus = max(0.05, math.floor(cpu_allocations[index] * 20) / 20)
        memory_limit = max(64, math.floor(memory_allocations[index]))
        reservation_basis = item["estimated_memory_mb"] or memory_limit * 0.6
        memory_reservation = max(32, min(memory_limit, math.floor(reservation_basis)))
        cpu_shares = max(128, min(4096, round(1024 * cpu_weights[index] / max_weight)))
        allocation = {
            "id": item["id"],
            "profile": item["profile"],
            "depends_on": item["depends_on"],
            "cpus": cpus,
            "cpu_shares": cpu_shares,
            "cpuset": cpusets.get(item["id"]),
            "memory_limit_mb": memory_limit,
            "memory_reservation_mb": memory_reservation,
            "pids_limit": item["pids_limit"],
            "estimated_memory_mb": item["estimated_memory_mb"],
            "docker_run_flags": [
                f"--cpus={cpus:.2f}",
                f"--cpu-shares={cpu_shares}",
                f"--memory={memory_limit}m",
                f"--memory-reservation={memory_reservation}m",
                f"--pids-limit={item['pids_limit']}",
            ],
        }
        if allocation["cpuset"]:
            allocation["docker_run_flags"].append(f"--cpuset-cpus={allocation['cpuset']}")
        allocations.append(allocation)

    build_services = sum(1 for item in services if item["profile"] == "build")
    buildkit_max_parallelism = max(
        1,
        min(
            max(1, math.floor(usable_cpus / 2)),
            max(1, math.floor(usable_memory_mb / 1024)),
            max(1, build_services * 2) if build_services else 4,
        ),
    )
    return {
        "docker": docker,
        "host": {
            "chip": host.get("chip"),
            "logical_cpus": host["logical_cpus"],
            "memory_total_bytes": host.get("memory_total_bytes"),
        },
        "vm_envelope": {
            "cpus": round(vm_cpus, 2),
            "cpu_source": vm_cpu_source,
            "memory_mb": round(vm_memory_mb),
            "memory_source": vm_memory_source,
            "reserve_cpus": round(reserve_vm_cpus, 2),
            "reserve_cpu_source": reserve_cpu_source,
            "reserve_memory_mb": round(reserve_vm_memory_mb),
            "reserve_memory_source": reserve_memory_source,
            "usable_cpus": round(usable_cpus, 2),
            "usable_memory_mb": round(usable_memory_mb),
            "responsiveness": responsiveness,
        },
        "allocations": allocations,
        "compose_override_yaml": _compose_resource_yaml(allocations),
        "buildkit": {
            "recommended_max_parallelism": buildkit_max_parallelism,
            "config_toml": (
                "[worker.oci]\n"
                f"  max-parallelism = {buildkit_max_parallelism}\n"
            ),
        },
        "totals": {
            "allocated_cpu_quota": round(sum(item["cpus"] for item in allocations), 2),
            "allocated_memory_limit_mb": sum(
                item["memory_limit_mb"] for item in allocations
            ),
            "service_count": len(allocations),
            "cpu_minimum_overcommitted": cpu_overcommit,
            "memory_minimum_overcommitted": memory_overcommit,
        },
        "warnings": warnings,
        "boundary": (
            "The plan emits local Docker Compose and docker run resource controls. It does not change Docker Desktop's VM-wide CPU/memory settings, create containers, or guarantee physical P-core/E-core affinity."
        ),
    }


def _bounded_number(value: Any, name: str, default: float, maximum: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{name} must be a number")
    if value <= 0 or value > maximum:
        raise InputError(f"{name} must be greater than 0 and at most {maximum}")
    return float(value)


def _validate_argv(argv: Any) -> list[str]:
    if not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV_ITEMS:
        raise InputError(f"argv must be a non-empty array with at most {MAX_ARGV_ITEMS} entries")
    normalized: list[str] = []
    for value in argv:
        if not isinstance(value, str):
            raise InputError("every argv entry must be a string")
        if "\x00" in value or len(value) > MAX_ARG_LENGTH:
            raise InputError(f"argv entries cannot contain NUL and must be at most {MAX_ARG_LENGTH} characters")
        normalized.append(value)
    return normalized


def normalize_task(raw: Any, index: int, default_cwd: str | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InputError(f"task {index} must be an object")
    task_id = raw.get("id", f"task-{index}")
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise InputError(f"task {index} id must match {TASK_ID_RE.pattern}")
    cwd = raw.get("cwd", default_cwd or os.getcwd())
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise InputError(f"task {task_id} cwd must be an absolute path")
    if not os.path.isdir(cwd):
        raise InputError(f"task {task_id} cwd does not exist or is not a directory: {cwd}")

    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict) or len(env_raw) > 128:
        raise InputError(f"task {task_id} env must be an object with at most 128 entries")
    env: dict[str, str] = {}
    for key, value in env_raw.items():
        if not isinstance(key, str) or not isinstance(value, str) or "\x00" in key + value:
            raise InputError(f"task {task_id} environment keys and values must be NUL-free strings")
        env[key] = value

    stdin = raw.get("stdin")
    if stdin is not None:
        if not isinstance(stdin, str):
            raise InputError(f"task {task_id} stdin must be a string")
        if len(stdin.encode("utf-8")) > MAX_STDIN_BYTES:
            raise InputError(f"task {task_id} stdin exceeds {MAX_STDIN_BYTES} bytes")

    dependencies = raw.get("depends_on", [])
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        raise InputError(f"task {task_id} depends_on must be an array of task IDs")
    side_effect = raw.get("side_effect", False)
    if not isinstance(side_effect, bool):
        raise InputError(f"task {task_id} side_effect must be boolean")

    return {
        "id": task_id,
        "argv": _validate_argv(raw.get("argv")),
        "cwd": cwd,
        "env": env,
        "stdin": stdin,
        "timeout_seconds": _bounded_number(
            raw.get("timeout_seconds"),
            f"task {task_id} timeout_seconds",
            DEFAULT_TIMEOUT_SECONDS,
            MAX_TIMEOUT_SECONDS,
        ),
        "depends_on": dependencies,
        "side_effect": side_effect,
    }


def normalize_tasks(raw_tasks: Any, default_cwd: str | None) -> list[dict[str, Any]]:
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise InputError("tasks must be a non-empty array")
    if len(raw_tasks) > MAX_TASKS:
        raise InputError(f"tasks cannot contain more than {MAX_TASKS} entries")
    tasks = [normalize_task(raw, index, default_cwd) for index, raw in enumerate(raw_tasks)]
    ids = [task["id"] for task in tasks]
    if len(set(ids)) != len(ids):
        raise InputError("task IDs must be unique")
    return tasks


def _truncate(data: bytes, limit: int) -> tuple[str, bool, int]:
    original = len(data)
    truncated = original > limit
    if truncated:
        head = limit * 3 // 4
        tail = limit - head
        data = data[:head] + b"\n...<output truncated>...\n" + data[-tail:]
    return data.decode("utf-8", errors="replace"), truncated, original


class _BoundedCapture:
    """Keep a bounded head/tail view while counting an arbitrarily large stream."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.head_limit = limit * 3 // 4
        self.tail_limit = limit - self.head_limit
        self.total = 0
        self.head = bytearray()
        self.tail = bytearray()

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        head_room = self.head_limit - len(self.head)
        if head_room > 0:
            self.head.extend(chunk[:head_room])
            chunk = chunk[head_room:]
        if chunk and self.tail_limit:
            self.tail.extend(chunk)
            if len(self.tail) > self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]

    def render(self) -> tuple[str, bool, int]:
        truncated = self.total > self.limit
        if truncated:
            data = bytes(self.head) + b"\n...<output truncated>...\n" + bytes(self.tail)
        else:
            data = bytes(self.head) + bytes(self.tail)
        return data.decode("utf-8", errors="replace"), truncated, self.total


async def _read_bounded_stream(
    reader: asyncio.StreamReader | None,
    capture: _BoundedCapture,
) -> None:
    if reader is None:
        return
    while True:
        chunk = await reader.read(65_536)
        if not chunk:
            return
        capture.feed(chunk)


async def _write_process_stdin(
    writer: asyncio.StreamWriter | None,
    data: bytes,
) -> None:
    if writer is None:
        return
    try:
        writer.write(data)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await writer.wait_closed()


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Terminate the complete subprocess session on timeout or cancellation."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    with contextlib.suppress(Exception):
        await process.wait()


async def execute_task(
    task: dict[str, Any],
    output_limit: int,
    nice_adjustment: int = 0,
    qos_clamp: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "id": task["id"],
        "status": "failed",
        "argv": task["argv"],
        "cwd": task["cwd"],
        "returncode": None,
        "duration_seconds": 0.0,
        "stdout": "",
        "stderr": "",
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "nice_adjustment": nice_adjustment,
        "qos_clamp": qos_clamp,
    }
    env = os.environ.copy()
    env.update(task["env"])
    process: asyncio.subprocess.Process | None = None
    readers: list[asyncio.Task[None]] = []
    input_task: asyncio.Task[None] | None = None
    try:
        launch_argv = task["argv"]
        nice_binary = shutil.which("nice")
        if nice_adjustment > 0 and nice_binary:
            launch_argv = [nice_binary, "-n", str(nice_adjustment), *launch_argv]
        taskpolicy = shutil.which("taskpolicy")
        if qos_clamp and taskpolicy:
            launch_argv = [taskpolicy, "-c", qos_clamp, *launch_argv]
        process = await asyncio.create_subprocess_exec(
            *launch_argv,
            cwd=task["cwd"],
            env=env,
            stdin=asyncio.subprocess.PIPE if task["stdin"] is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout_capture = _BoundedCapture(output_limit)
        stderr_capture = _BoundedCapture(output_limit)
        readers = [
            asyncio.create_task(_read_bounded_stream(process.stdout, stdout_capture)),
            asyncio.create_task(_read_bounded_stream(process.stderr, stderr_capture)),
        ]
        if task["stdin"] is not None:
            input_task = asyncio.create_task(
                _write_process_stdin(process.stdin, task["stdin"].encode("utf-8"))
            )
        try:
            # stdin delivery runs concurrently; the wall timeout starts as soon
            # as the process is spawned even if the child never reads its pipe.
            await asyncio.wait_for(process.wait(), timeout=task["timeout_seconds"])
        except asyncio.TimeoutError:
            result["status"] = "timed_out"
            result["outcome"] = "unknown" if task.get("side_effect") else "not_completed"
            result["automatic_retry_allowed"] = False if task.get("side_effect") else None
            await _terminate_process_group(process)
        if input_task is not None:
            await asyncio.gather(input_task, return_exceptions=True)
        await asyncio.gather(*readers)
        result["returncode"] = process.returncode
        if result["status"] != "timed_out":
            result["status"] = "succeeded" if process.returncode == 0 else "failed"
            result["outcome"] = "committed" if process.returncode == 0 else "not_committed_or_unknown"
        result["stdout"], result["stdout_truncated"], result["stdout_bytes"] = stdout_capture.render()
        result["stderr"], result["stderr_truncated"], result["stderr_bytes"] = stderr_capture.render()
    except asyncio.CancelledError:
        if process is not None:
            await _terminate_process_group(process)
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        if input_task is not None:
            await asyncio.gather(input_task, return_exceptions=True)
        raise
    except FileNotFoundError as exc:
        result["stderr"] = str(exc)
    except PermissionError as exc:
        result["stderr"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - isolate one malformed child from its batch.
        result["stderr"] = f"{type(exc).__name__}: {exc}"
    finally:
        if process is not None and process.returncode is None:
            await _terminate_process_group(process)
        if input_task is not None:
            await asyncio.gather(input_task, return_exceptions=True)
        for reader_task in readers:
            if not reader_task.done():
                reader_task.cancel()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
    result["duration_seconds"] = round(time.monotonic() - started, 6)
    return result


def _execution_indicator(
    results: list[dict[str, Any]],
    elapsed: float,
    peak_concurrency: int,
    serial_baseline_seconds: float | None,
) -> dict[str, Any]:
    task_runtime = sum(
        result.get("duration_seconds", 0.0)
        for result in results
        if result.get("status") != "skipped"
    )
    if serial_baseline_seconds is not None:
        comparison_seconds = serial_baseline_seconds
        speedup_kind = "measured_serial_baseline"
        qualifier_zh = "实测"
    else:
        comparison_seconds = task_runtime
        speedup_kind = "estimated_sum_of_task_durations"
        qualifier_zh = "估算"

    speedup = comparison_seconds / elapsed if elapsed > 0 and comparison_seconds > 0 else 0.0
    time_saved = comparison_seconds - elapsed
    cumulative = _record_time_saved(time_saved)
    is_parallel = peak_concurrency > 1
    icon = "⚡" if is_parallel else "→"
    label_zh = "并行" if is_parallel else "串行"
    display = (
        f"{icon} {label_zh}｜峰值 {peak_concurrency} 路｜{qualifier_zh} {speedup:.2f}×"
        f"｜本次节约 {time_saved:.2f}s｜累计节约 {cumulative['cumulative_saved_seconds']:.2f}s"
    )
    efficiency = speedup / peak_concurrency if peak_concurrency else 0.0
    return {
        "display": display,
        "parallel": is_parallel,
        "mode": "parallel" if is_parallel else "serial",
        "peak_concurrency": peak_concurrency,
        "speedup_multiplier": round(speedup, 4),
        "speedup_kind": speedup_kind,
        "comparison_seconds": round(comparison_seconds, 6),
        "wall_time_seconds": round(elapsed, 6),
        "time_saved_seconds": round(time_saved, 6),
        "cumulative_saved_seconds": cumulative["cumulative_saved_seconds"],
        "cumulative_run_count": cumulative["run_count"],
        "parallel_efficiency": round(efficiency, 4),
        "explanation": (
            "Speedup uses the supplied serial baseline."
            if serial_baseline_seconds is not None
            else "Estimated speedup equals summed non-skipped task runtimes divided by wall time; no task is rerun."
        ),
    }


def _summary(
    results: list[dict[str, Any]],
    plan: dict[str, Any],
    elapsed: float,
    peak_concurrency: int,
    serial_baseline_seconds: float | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    indicator = _execution_indicator(results, elapsed, peak_concurrency, serial_baseline_seconds)
    summary = {
        "task_count": len(results),
        "status_counts": counts,
        "failed_task_ids": [item["id"] for item in results if item["status"] == "failed"],
        "timed_out_task_ids": [item["id"] for item in results if item["status"] == "timed_out"],
        "skipped_task_ids": [item["id"] for item in results if item["status"] == "skipped"],
        "chosen_concurrency": plan["chosen_concurrency"],
        "peak_concurrency": peak_concurrency,
        "speedup_multiplier": indicator["speedup_multiplier"],
        "speedup_kind": indicator["speedup_kind"],
        "elapsed_seconds": round(elapsed, 6),
        "comparison_seconds": indicator["comparison_seconds"],
        "time_saved_seconds": indicator["time_saved_seconds"],
        "cumulative_saved_seconds": indicator["cumulative_saved_seconds"],
        "cumulative_run_count": indicator["cumulative_run_count"],
    }
    return summary, indicator


def _common_options(arguments: dict[str, Any]) -> tuple[dict[str, Any], int, float | None]:
    profile = arguments.get("profile", "mixed")
    requested = arguments.get("max_concurrency")
    if requested is not None and (isinstance(requested, bool) or not isinstance(requested, int)):
        raise InputError("max_concurrency must be an integer")
    reserve = arguments.get("reserve_cores")
    if reserve is not None and (isinstance(reserve, bool) or not isinstance(reserve, int)):
        raise InputError("reserve_cores must be an integer when supplied")
    responsiveness = arguments.get("responsiveness", "interactive")
    if not isinstance(responsiveness, str):
        raise InputError("responsiveness must be a string")
    memory = arguments.get("estimated_memory_mb_per_task")
    if memory is not None and (isinstance(memory, bool) or not isinstance(memory, (int, float))):
        raise InputError("estimated_memory_mb_per_task must be a number")
    output_limit = arguments.get("max_output_bytes_per_stream", DEFAULT_OUTPUT_BYTES)
    if isinstance(output_limit, bool) or not isinstance(output_limit, int):
        raise InputError("max_output_bytes_per_stream must be an integer")
    if output_limit < 256 or output_limit > MAX_OUTPUT_BYTES:
        raise InputError(f"max_output_bytes_per_stream must be between 256 and {MAX_OUTPUT_BYTES}")
    baseline = arguments.get("serial_baseline_seconds")
    if baseline is not None:
        baseline = _bounded_number(
            baseline,
            "serial_baseline_seconds",
            1.0,
            MAX_TIMEOUT_SECONDS * MAX_TASKS,
        )
    return concurrency_plan(
        profile, requested, reserve, memory, responsiveness
    ), output_limit, baseline


async def run_parallel(
    arguments: dict[str, Any],
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    tasks = normalize_tasks(arguments.get("tasks"), arguments.get("default_cwd"))
    if any(task["depends_on"] for task in tasks):
        raise InputError("parallel_exec tasks cannot use depends_on; use parallel_dag")
    plan, output_limit, serial_baseline = _common_options(arguments)
    semaphore = asyncio.Semaphore(min(plan["chosen_concurrency"], len(tasks)))
    active = 0
    peak_concurrency = 0
    activity_lock = asyncio.Lock()
    started = time.monotonic()
    reporter = ProgressReporter(len(tasks), progress_callback, started)

    async def guarded(task: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, peak_concurrency
        async with semaphore:
            async with activity_lock:
                active += 1
                peak_concurrency = max(peak_concurrency, active)
            reporter.task_started(task["id"])
            try:
                result = await execute_task(
                    task,
                    output_limit,
                    plan["nice_adjustment"],
                    plan["qos_clamp"],
                )
                reporter.task_finished(result)
                return result
            finally:
                async with activity_lock:
                    active -= 1

    await reporter.start()
    try:
        results = await asyncio.gather(*(guarded(task) for task in tasks))
    finally:
        await reporter.stop()
    elapsed = time.monotonic() - started
    summary, indicator = _summary(results, plan, elapsed, peak_concurrency, serial_baseline)
    return {
        "indicator": indicator,
        "summary": summary,
        "resource_plan": plan,
        "results": results,
    }


def _map_tasks(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    template = _validate_argv(arguments.get("argv_template"))
    items = arguments.get("items")
    if not isinstance(items, list) or not items or len(items) > MAX_TASKS:
        raise InputError(f"items must be a non-empty array with at most {MAX_TASKS} entries")
    if not all(isinstance(item, (str, int, float)) and not isinstance(item, bool) for item in items):
        raise InputError("every item must be a string or number")
    if not any("{item}" in arg or "{index}" in arg for arg in template):
        raise InputError("argv_template must contain {item} or {index}")

    tasks: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        item_text = str(item)
        tasks.append(
            {
                "id": f"item-{index}",
                "argv": [arg.replace("{item}", item_text).replace("{index}", str(index)) for arg in template],
                "cwd": arguments.get("default_cwd"),
                "timeout_seconds": arguments.get("timeout_seconds"),
                "env": arguments.get("env", {}),
            }
        )
    return tasks


async def run_map(
    arguments: dict[str, Any],
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    expanded = dict(arguments)
    expanded["tasks"] = _map_tasks(arguments)
    result = await run_parallel(expanded, progress_callback)
    result["map"] = {"item_count": len(arguments["items"]), "argv_template": arguments["argv_template"]}
    return result


def _validate_dag(tasks: list[dict[str, Any]]) -> None:
    ids = {task["id"] for task in tasks}
    for task in tasks:
        unknown = set(task["depends_on"]) - ids
        if unknown:
            raise InputError(f"task {task['id']} has unknown dependencies: {sorted(unknown)}")
        if task["id"] in task["depends_on"]:
            raise InputError(f"task {task['id']} cannot depend on itself")

    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {task["id"]: task for task in tasks}

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise InputError("task graph contains a dependency cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id]["depends_on"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in by_id:
        visit(task_id)


def _skipped_task_result(task_id: str, reason: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "status": "skipped",
        "returncode": None,
        "duration_seconds": 0.0,
        "stdout": "",
        "stderr": reason,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "outcome": "not_started",
    }


async def run_dag(
    arguments: dict[str, Any],
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    tasks = normalize_tasks(arguments.get("tasks"), arguments.get("default_cwd"))
    _validate_dag(tasks)
    plan, output_limit, serial_baseline = _common_options(arguments)
    concurrency = min(plan["chosen_concurrency"], len(tasks))
    fail_fast = arguments.get("fail_fast", False)
    if not isinstance(fail_fast, bool):
        raise InputError("fail_fast must be boolean")

    pending = {task["id"]: task for task in tasks}
    running: dict[asyncio.Task[dict[str, Any]], str] = {}
    completed: dict[str, dict[str, Any]] = {}
    launch_order: list[str] = []
    failure_seen = False
    peak_concurrency = 0
    started = time.monotonic()
    reporter = ProgressReporter(len(tasks), progress_callback, started)
    await reporter.start()

    try:
        while pending or running:
            # Propagate terminal failure to a fixed point. A single pass is
            # input-order dependent for reverse-ordered chains such as C→B→A.
            changed = True
            while changed:
                changed = False
                for task_id, task in list(pending.items()):
                    failed_dependencies = [
                        dep
                        for dep in task["depends_on"]
                        if dep in completed and completed[dep]["status"] != "succeeded"
                    ]
                    if not failed_dependencies:
                        continue
                    completed[task_id] = _skipped_task_result(
                        task_id,
                        f"blocked by dependencies: {', '.join(sorted(failed_dependencies))}",
                    )
                    reporter.task_finished(completed[task_id])
                    del pending[task_id]
                    changed = True

            if fail_fast and failure_seen:
                for task_id in list(pending):
                    completed[task_id] = _skipped_task_result(
                        task_id,
                        "not started because fail_fast stopped scheduling after a failure",
                    )
                    reporter.task_finished(completed[task_id])
                    del pending[task_id]

            ready = [
                task
                for task in pending.values()
                if all(dep in completed and completed[dep]["status"] == "succeeded" for dep in task["depends_on"])
            ]
            admitted = ready[: max(0, concurrency - len(running))]
            for task in admitted:
                launch_order.append(task["id"])
                reporter.task_started(task["id"])
                future = asyncio.create_task(
                    execute_task(
                        task,
                        output_limit,
                        plan["nice_adjustment"],
                        plan["qos_clamp"],
                    )
                )
                running[future] = task["id"]
                del pending[task["id"]]
            reporter.scheduler_state(ready_tasks=max(0, len(ready) - len(admitted)))
            peak_concurrency = max(peak_concurrency, len(running))

            if not running:
                if pending:
                    raise RuntimeError("scheduler stalled despite validated DAG")
                break

            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for future in done:
                task_id = running.pop(future)
                result = future.result()
                completed[task_id] = result
                reporter.task_finished(result)
                if result["status"] != "succeeded":
                    failure_seen = True
    finally:
        if running:
            for future in running:
                future.cancel()
            await asyncio.gather(*running, return_exceptions=True)
        await reporter.stop()

    results = [completed[task["id"]] for task in tasks]
    elapsed = time.monotonic() - started
    summary, indicator = _summary(results, plan, elapsed, peak_concurrency, serial_baseline)
    return {
        "indicator": indicator,
        "summary": summary,
        "resource_plan": plan,
        "launch_order": launch_order,
        "results": results,
    }


def _verify_compiled_plan(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_plan = arguments.get("compiled_plan")
    supplied_hash = arguments.get("plan_hash")
    if not isinstance(raw_plan, dict):
        raise InputError("compiled_plan must be the complete object returned by atomic_task_plan")
    if not isinstance(supplied_hash, str):
        raise InputError("plan_hash must be supplied")
    # Detach execution from caller-owned mutable objects before validation.
    try:
        plan = json.loads(json.dumps(raw_plan, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise InputError(f"compiled_plan must be JSON-serializable: {exc}") from exc
    if plan.get("plan_hash") != supplied_hash:
        raise InputError("plan_hash does not match compiled_plan.plan_hash")
    if _compiled_plan_envelope_hash(plan) != supplied_hash:
        raise InputError("compiled_plan envelope was changed after compilation")
    semantic_hash = plan.get("semantic_hash")
    if not isinstance(semantic_hash, str):
        raise InputError("compiled_plan is missing its semantic_hash")
    execution_contract = plan.get("execution_contract")
    if (
        not isinstance(execution_contract, dict)
        or execution_contract.get("tool") != "atomic_exec"
        or execution_contract.get("immutable") is not True
        or not isinstance(execution_contract.get("arguments"), dict)
        or execution_contract["arguments"].get("compiled_plan") != "<this entire object>"
        or execution_contract["arguments"].get("plan_hash") != supplied_hash
    ):
        raise InputError("compiled_plan has an invalid execution contract")
    atoms = plan.get("atoms")
    capacities = plan.get("capacities")
    snapshots = plan.get("source_snapshots", plan.get("snapshots", []))
    if not isinstance(atoms, list) or not isinstance(capacities, dict) or not isinstance(snapshots, list):
        raise InputError("compiled_plan is missing canonical atoms, capacities, or source snapshots")
    project_root = plan.get("project_root")
    if not isinstance(project_root, str) or not os.path.isabs(project_root):
        raise InputError("compiled_plan has no absolute project_root")
    try:
        actual_hash = canonical_plan_hash(
            atoms,
            capacities,
            snapshots,
            project_root=project_root,
            execution_contract={
                "execution_blockers": plan.get("execution_blockers", []),
                "native_delegates": plan.get("native_delegates", []),
            },
        )
    except AtomError as exc:
        raise InputError(str(exc)) from exc
    if actual_hash != semantic_hash:
        raise InputError("compiled_plan semantic core was changed after compilation")
    try:
        independently_checked = compile_atomic_plan(
            atoms,
            Path(project_root),
            capacities=capacities,
            snapshots=snapshots,
            diagnostics=plan.get("diagnostics", []),
            native_delegates=plan.get("native_delegates", []),
            relaxation_candidates=plan.get("relaxation_candidates", []),
        )
    except AtomError as exc:
        raise InputError(f"compiled_plan semantic revalidation failed: {exc}") from exc
    if independently_checked.get("plan_hash") != semantic_hash:
        raise InputError("compiled_plan is not canonical under the current planner")
    if not independently_checked.get("execution_eligible"):
        blockers = independently_checked.get("execution_blockers", [])
        raise InputError(f"compiled_plan is not execution eligible: {blockers}")
    error_diagnostics = [
        item for item in plan.get("diagnostics", [])
        if isinstance(item, dict) and item.get("severity") == "error"
    ]
    if error_diagnostics:
        raise InputError("compiled_plan contains error diagnostics")
    unsupported_edge_kinds = {"stream", "after_ready", "after_healthy", "after_completion"}
    for atom in atoms:
        operation = atom.get("operation", {})
        assurance = atom.get("assurance", {})
        if operation.get("completion") != "process_exit":
            raise InputError(f"atom {atom.get('id')} requires a lifecycle-native executor")
        if not operation.get("argv") or assurance.get("codegen") != "exact_argv":
            raise InputError(f"atom {atom.get('id')} is not an exact argv process atom")
        if assurance.get("parse") != "exact" or assurance.get("control") != "exact":
            raise InputError(f"atom {atom.get('id')} lacks exact parse/control assurance")
        if assurance.get("effects") not in {"complete_declared", "complete_static"}:
            raise InputError(f"atom {atom.get('id')} has incomplete effects")
        if any(effect.get("domain") == "unknown" for effect in atom.get("effects", [])):
            raise InputError(f"atom {atom.get('id')} has unknown host effects")
        unsupported = sorted(
            {
                edge.get("kind")
                for edge in atom.get("dependencies", [])
                if isinstance(edge, dict) and edge.get("kind") in unsupported_edge_kinds
            }
        )
        if unsupported:
            raise InputError(
                f"atom {atom.get('id')} requires lifecycle/stream dependency support: {unsupported}"
            )
    try:
        stale = validate_source_snapshots(plan)
    except (AtomError, OSError) as exc:
        raise InputError(f"source snapshot validation failed: {exc}") from exc
    if stale:
        raise InputError(f"compiled_plan source snapshot is stale: {stale}")
    return plan


def _atomic_edge_condition(kind: str, predecessor_status: str) -> tuple[bool, bool]:
    """Return (terminally_decided, condition_satisfied)."""
    if kind in {"order", "finally"}:
        return True, True
    if kind == "failure":
        return True, predecessor_status != "succeeded"
    if kind in {
        "hard", "success", "data", "after_ready", "after_healthy",
        "after_completion", "stream",
    }:
        return True, predecessor_status == "succeeded"
    return True, False


async def run_atomic(
    arguments: dict[str, Any],
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    plan = _verify_compiled_plan(arguments)
    atoms = plan["atoms"]
    if not atoms:
        raise InputError("compiled_plan has no executable atoms")
    output_limit = arguments.get("max_output_bytes_per_stream", DEFAULT_OUTPUT_BYTES)
    if isinstance(output_limit, bool) or not isinstance(output_limit, int) or not 256 <= output_limit <= MAX_OUTPUT_BYTES:
        raise InputError(f"max_output_bytes_per_stream must be between 256 and {MAX_OUTPUT_BYTES}")
    baseline = arguments.get("serial_baseline_seconds")
    if baseline is not None:
        baseline = _bounded_number(baseline, "serial_baseline_seconds", 1.0, MAX_TIMEOUT_SECONDS * MAX_TASKS)

    by_id = {atom["id"]: atom for atom in atoms}
    compiled_capacities = {key: float(value) for key, value in plan["capacities"].items()}
    capacities = dict(compiled_capacities)
    pending = set(by_id)
    completed: dict[str, dict[str, Any]] = {}
    running: dict[asyncio.Task[dict[str, Any]], str] = {}
    usage: dict[str, float] = {}
    launch_order: list[str] = []
    journal: list[dict[str, Any]] = []
    peak_concurrency = 0
    started = time.monotonic()
    reporter = ProgressReporter(len(atoms), progress_callback, started)
    compiled_resource_plan = plan.get("resource_plan") if isinstance(plan.get("resource_plan"), dict) else {}
    responsiveness = compiled_resource_plan.get("responsiveness", "interactive")
    if responsiveness not in {"interactive", "balanced", "throughput"}:
        responsiveness = "interactive"
    compiled_worker_limit = max(1, min(MAX_CONCURRENCY, int(capacities.get("worker_slot", 1.0))))
    resource_plan = concurrency_plan(
        "mixed",
        compiled_worker_limit,
        None,
        None,
        responsiveness,
    )
    # Resource conditions may worsen after compilation. Runtime may only
    # tighten the immutable envelope, never increase it.
    capacities["worker_slot"] = min(
        capacities.get("worker_slot", 1.0),
        float(resource_plan["chosen_concurrency"]),
    )
    capacities["cpu_core"] = min(
        capacities.get("cpu_core", capacities["worker_slot"]),
        float(max(1, resource_plan["chosen_concurrency"])),
    )
    current_available = resource_plan["machine"].get("memory_available_bytes_approx")
    if current_available and "memory_mb" in capacities:
        capacities["memory_mb"] = min(
            capacities["memory_mb"],
            max(256.0, float(current_available) / (1024 * 1024) * 0.60),
        )
    resource_plan["compiled_capacities"] = compiled_capacities
    resource_plan["effective_runtime_capacities"] = dict(capacities)
    resource_plan["runtime_capacity_policy"] = "fresh conditions may only tighten the compiled envelope"
    nice_adjustment = resource_plan.get("nice_adjustment", 10)
    qos_clamp = resource_plan.get("qos_clamp")
    priority = {
        item["atom"]: (float(item.get("start_seconds", 0.0)), item["atom"])
        for item in plan.get("schedule", {}).get("timeline", [])
        if isinstance(item, dict) and isinstance(item.get("atom"), str)
    }

    def record(event: str, atom_id: str, **extra: Any) -> None:
        journal.append(
            {
                "sequence": len(journal) + 1,
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "event": event,
                "atom": atom_id,
                **extra,
            }
        )

    def release(atom: dict[str, Any]) -> None:
        for claim in atom["claims"]:
            key = claim["resource"]
            usage[key] = max(0.0, usage.get(key, 0.0) - float(claim["units"]))

    def admits(atom: dict[str, Any]) -> bool:
        for claim in atom["claims"]:
            key = claim["resource"]
            capacity = capacities.get(key, 1.0)
            if usage.get(key, 0.0) + float(claim["units"]) > capacity + 1e-9:
                return False
        return all(not atom_conflicts(atom, by_id[running_id]) for running_id in running.values())

    def reserve(atom: dict[str, Any]) -> None:
        for claim in atom["claims"]:
            key = claim["resource"]
            usage[key] = usage.get(key, 0.0) + float(claim["units"])

    def dependency_state(atom: dict[str, Any]) -> str:
        waiting = False
        for edge in atom["dependencies"]:
            predecessor = edge["atom"]
            if predecessor not in completed:
                waiting = True
                continue
            _, satisfied = _atomic_edge_condition(edge["kind"], completed[predecessor]["status"])
            if not satisfied:
                return "impossible"
        return "waiting" if waiting else "ready"

    await reporter.start()
    try:
        while pending or running:
            # Failure/success guards propagate to a fixed point, independent of
            # atom input order.
            changed = True
            while changed:
                changed = False
                for atom_id in sorted(pending):
                    if dependency_state(by_id[atom_id]) != "impossible":
                        continue
                    result = _skipped_task_result(atom_id, "dependency condition was not satisfied")
                    completed[atom_id] = result
                    pending.remove(atom_id)
                    reporter.task_finished(result)
                    record("skipped", atom_id, status="skipped")
                    changed = True

            ready = sorted(
                (atom_id for atom_id in pending if dependency_state(by_id[atom_id]) == "ready"),
                key=lambda atom_id: priority.get(atom_id, (float("inf"), atom_id)),
            )
            admitted_ids: set[str] = set()
            for atom_id in ready:
                atom = by_id[atom_id]
                if not admits(atom):
                    continue
                operation = atom["operation"]
                task = {
                    "id": atom_id,
                    "argv": operation["argv"],
                    "cwd": operation["cwd"],
                    "env": operation.get("env", {}),
                    "stdin": None,
                    "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
                    "depends_on": [],
                    "side_effect": atom["side_effect"],
                }
                reserve(atom)
                pending.remove(atom_id)
                launch_order.append(atom_id)
                reporter.task_started(atom_id)
                record("started", atom_id)
                future = asyncio.create_task(execute_task(task, output_limit, int(nice_adjustment), qos_clamp))
                running[future] = atom_id
                admitted_ids.add(atom_id)
            reporter.scheduler_state(
                ready_tasks=sum(1 for atom_id in ready if atom_id not in admitted_ids)
            )
            peak_concurrency = max(peak_concurrency, len(running))

            if not running:
                if pending:
                    blocked = {
                        atom_id: {
                            claim["resource"]: {
                                "requested": claim["units"],
                                "capacity": capacities.get(claim["resource"], 1.0),
                            }
                            for claim in by_id[atom_id]["claims"]
                        }
                        for atom_id in sorted(pending)
                    }
                    raise InputError(f"atomic scheduler cannot admit pending atoms: {blocked}")
                break
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for future in sorted(done, key=lambda item: running[item]):
                atom_id = running.pop(future)
                release(by_id[atom_id])
                result = future.result()
                completed[atom_id] = result
                reporter.task_finished(result)
                record("completed", atom_id, status=result["status"], returncode=result["returncode"])
    finally:
        if running:
            for future in running:
                future.cancel()
            await asyncio.gather(*running, return_exceptions=True)
        await reporter.stop()

    results = [completed[atom["id"]] for atom in atoms]
    elapsed = time.monotonic() - started
    summary_plan = dict(resource_plan)
    summary_plan["chosen_concurrency"] = int(capacities.get("worker_slot", 1.0))
    summary, indicator = _summary(results, summary_plan, elapsed, peak_concurrency, baseline)
    return {
        "indicator": indicator,
        "summary": summary,
        "plan_hash": plan["plan_hash"],
        "resource_plan": summary_plan,
        "launch_order": launch_order,
        "event_journal": journal,
        "results": results,
    }


TASK_SCHEMA = {
    "type": "object",
    "required": ["argv"],
    "properties": {
        "id": {"type": "string", "description": "Unique result label."},
        "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "cwd": {"type": "string", "description": "Absolute working directory."},
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
        "stdin": {"type": "string"},
        "timeout_seconds": {"type": "number", "minimum": 0.001, "maximum": MAX_TIMEOUT_SECONDS},
        "side_effect": {
            "type": "boolean",
            "default": False,
            "description": "Marks externally visible effects so timeout outcomes are never treated as safely retryable.",
        },
    },
    "additionalProperties": False,
}


ATOMIC_ENTRYPOINT_SCHEMA = {
    "type": "object",
    "required": ["adapter"],
    "properties": {
        "id": {"type": "string"},
        "adapter": {
            "type": "string",
            "enum": ["shell", "package_script", "make_target", "compose_services"],
        },
        "command": {"type": "string"},
        "cwd": {"type": "string"},
        "package_json": {"type": "string"},
        "script": {"type": "string"},
        "makefile": {"type": "string"},
        "target": {"type": "string"},
        "compose_file": {"type": "string"},
        "services": {"type": "array", "items": {"type": "string"}},
        "profiles": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


ATOMIC_PLAN_PROPERTIES = {
    "project_path": {"type": "string", "description": "Absolute project root."},
    "task_summary": {"type": "string", "maxLength": 8000},
    "entrypoints": {
        "type": "array",
        "maxItems": 64,
        "items": ATOMIC_ENTRYPOINT_SCHEMA,
        "default": [],
    },
    "atoms": {
        "type": "array",
        "maxItems": 512,
        "items": {"type": "object"},
        "description": "Explicit typed Atom IR nodes. Validation is strict and fail-closed.",
        "default": [],
    },
    "capacities": {
        "type": "array",
        "maxItems": 128,
        "items": {
            "type": "object",
            "required": ["resource", "capacity"],
            "properties": {
                "resource": {"type": "string"},
                "capacity": {"type": "number", "exclusiveMinimum": 0},
            },
            "additionalProperties": False,
        },
    },
    "responsiveness": {
        "type": "string",
        "enum": ["interactive", "balanced", "throughput"],
        "default": "interactive",
    },
    "max_concurrency": {"type": "integer", "minimum": 1, "maximum": MAX_CONCURRENCY},
    "reserve_cores": {"type": "integer", "minimum": 0, "maximum": MAX_CONCURRENCY},
    "discover_project_commands": {"type": "boolean", "default": False},
}


COMMON_PROPERTIES = {
    "default_cwd": {"type": "string", "description": "Absolute cwd used by tasks that omit cwd."},
    "profile": {
        "type": "string",
        "enum": ["cpu", "io", "mixed", "accelerator"],
        "default": "mixed",
        "description": "Use accelerator when each subprocess drives the shared GPU, ANE, or media engine.",
    },
    "responsiveness": {
        "type": "string",
        "enum": ["interactive", "balanced", "throughput"],
        "default": "interactive",
        "description": "Controls adaptive CPU/memory headroom and worker priority.",
    },
    "max_concurrency": {"type": "integer", "minimum": 1, "maximum": MAX_CONCURRENCY},
    "reserve_cores": {
        "type": "integer",
        "minimum": 0,
        "maximum": MAX_CONCURRENCY,
        "description": "Optional explicit reserve. When omitted, it is derived from this Mac and responsiveness mode.",
    },
    "estimated_memory_mb_per_task": {"type": "number", "exclusiveMinimum": 0},
    "max_output_bytes_per_stream": {
        "type": "integer",
        "minimum": 256,
        "maximum": MAX_OUTPUT_BYTES,
        "default": DEFAULT_OUTPUT_BYTES,
    },
    "serial_baseline_seconds": {
        "type": "number",
        "exclusiveMinimum": 0,
        "description": "Optional previously measured serial runtime. When omitted, speedup is estimated from summed task runtimes without rerunning tasks.",
    },
}


TOOLS = [
    {
        "name": "scenario_plan",
        "description": "Analyze a complex local project against the plugin's preset acceleration scenarios. Uses project structure, task hints, Make targets, and optional bounded local Codex trace signals to recommend optimization goals, execution modes, isolation, and serial guardrails. This is advisory and does not execute work.",
        "inputSchema": {
            "type": "object",
            "required": ["project_path"],
            "properties": {
                "project_path": {"type": "string", "description": "Absolute local project directory."},
                "task_hint": {"type": "string", "maxLength": 8000},
                "include_trace_history": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, sample recent local Codex rollout tails for aggregate tool/signature counts and agent task names only.",
                },
                "trace_file_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 200,
                },
                "max_scenarios": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 8,
                },
                "minimum_confidence": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 0.95,
                    "default": 0.28,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "task_parallel_scan",
        "description": "Compatibility compiler for legacy candidate units. Returns an immutable typed CompiledPlan and plan hash for atomic_exec; it never recommends translating conflict waves into generic executors.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "Absolute active project directory. Defaults to the server cwd.",
                },
                "task_summary": {"type": "string", "maxLength": 8000},
                "candidate_units": {
                    "type": "array",
                    "maxItems": MAX_TASKS,
                    "default": [],
                    "items": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {
                            "id": {"type": "string", "pattern": TASK_ID_RE.pattern},
                            "kind": {
                                "type": "string",
                                "enum": ["command", "read", "test", "build", "transform", "network", "mutation", "other"],
                                "default": "command",
                            },
                            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                            "cwd": {"type": "string", "description": "Absolute working directory."},
                            "reads": {"type": "array", "items": {"type": "string"}, "default": []},
                            "writes": {"type": "array", "items": {"type": "string"}, "default": []},
                            "shared_resources": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Exclusive logical resources such as database:dev, git:index, device:camera, or account:production.",
                                "default": [],
                            },
                            "depends_on": {"type": "array", "items": {"type": "string"}, "default": []},
                            "estimated_seconds": {"type": "number", "exclusiveMinimum": 0},
                            "estimated_memory_mb": {"type": "number", "exclusiveMinimum": 0},
                            "profile": {
                                "type": "string",
                                "enum": ["cpu", "io", "mixed", "accelerator"],
                            },
                            "side_effect": {"type": "boolean"},
                            "batch_key": {
                                "type": "string",
                                "description": "Set the same key on repetitions of one command shape to make parallel_map routing explicit.",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "discover_project_commands": {"type": "boolean", "default": True},
                "include_scenario_context": {"type": "boolean", "default": True},
                "include_trace_history": {"type": "boolean", "default": False},
                "responsiveness": {
                    "type": "string",
                    "enum": ["interactive", "balanced", "throughput"],
                    "default": "interactive",
                },
                "max_concurrency": {"type": "integer", "minimum": 1, "maximum": MAX_CONCURRENCY},
                "reserve_cores": {"type": "integer", "minimum": 0, "maximum": MAX_CONCURRENCY},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "atomic_task_plan",
        "description": "Compile shell, package-script, Make, Compose, and explicit Atom IR entrypoints into one typed, source-snapshotted, resource-constrained plan. Unsupported control/effect semantics fail closed. The returned object is immutable and must be passed unchanged to atomic_exec with its plan_hash.",
        "inputSchema": {
            "type": "object",
            "required": ["project_path"],
            "properties": ATOMIC_PLAN_PROPERTIES,
            "additionalProperties": False,
        },
    },
    {
        "name": "atomic_exec",
        "description": "Execute the exact immutable CompiledPlan returned by atomic_task_plan. Revalidates the canonical plan hash and source snapshots, enforces typed dependencies, artifact conflicts, and multidimensional capacities, and refuses opaque or incomplete-effect atoms.",
        "_meta": _indicator_ui_meta(),
        "inputSchema": {
            "type": "object",
            "required": ["compiled_plan", "plan_hash"],
            "properties": {
                "compiled_plan": {"type": "object"},
                "plan_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                "max_output_bytes_per_stream": {
                    "type": "integer",
                    "minimum": 256,
                    "maximum": MAX_OUTPUT_BYTES,
                    "default": DEFAULT_OUTPUT_BYTES,
                },
                "serial_baseline_seconds": {"type": "number", "exclusiveMinimum": 0},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "mac_resource_plan",
        "description": "Call after a workload is judged parallel-eligible, especially before a large or memory-heavy run. Inspects this Mac's P/E cores, GPU, memory, load, thermal and power state and recommends concurrency with responsive headroom. Do not call for ordinary serial tasks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "enum": ["cpu", "io", "mixed", "accelerator"], "default": "mixed"},
                "responsiveness": {
                    "type": "string",
                    "enum": ["interactive", "balanced", "throughput"],
                    "default": "interactive",
                },
                "max_concurrency": {"type": "integer", "minimum": 1, "maximum": MAX_CONCURRENCY},
                "reserve_cores": {"type": "integer", "minimum": 0, "maximum": MAX_CONCURRENCY},
                "estimated_memory_mb_per_task": {"type": "number", "exclusiveMinimum": 0},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "container_resource_plan",
        "description": "Plan per-container CPU quota, optional VM-vCPU cpuset, CPU shares, memory limit/reservation, PID limit, and BuildKit solver parallelism inside the Docker Desktop VM envelope. Returns Compose override YAML and docker run flags without creating or changing containers.",
        "inputSchema": {
            "type": "object",
            "required": ["services"],
            "properties": {
                "services": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "items": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {
                            "id": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,128}$"},
                            "profile": {
                                "type": "string",
                                "enum": ["cpu", "io", "mixed", "database", "build", "accelerator"],
                                "default": "mixed",
                            },
                            "weight": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
                            "requested_cpus": {"type": "number", "exclusiveMinimum": 0, "maximum": 256},
                            "minimum_cpus": {"type": "number", "exclusiveMinimum": 0, "maximum": 256},
                            "maximum_cpus": {"type": "number", "exclusiveMinimum": 0, "maximum": 256},
                            "estimated_memory_mb": {"type": "number", "exclusiveMinimum": 0},
                            "requested_memory_mb": {"type": "number", "exclusiveMinimum": 0},
                            "pids_limit": {"type": "integer", "minimum": 16, "maximum": 1048576},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "default": [],
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "responsiveness": {
                    "type": "string",
                    "enum": ["interactive", "balanced", "throughput"],
                    "default": "interactive",
                },
                "reserve_vm_cpus": {"type": "number", "exclusiveMinimum": 0, "maximum": 256},
                "reserve_vm_memory_mb": {"type": "number", "exclusiveMinimum": 0},
                "docker_vm_cpus": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 256,
                    "description": "Optional override when Docker info is unavailable or a future VM envelope is being planned.",
                },
                "docker_vm_memory_mb": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "Optional override when Docker info is unavailable or a future VM envelope is being planned.",
                },
                "pin_cpus": {
                    "type": "boolean",
                    "default": False,
                    "description": "Emit non-overlapping cpuset values using Docker VM vCPU IDs. These are not stable Apple P/E-core mappings.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "mac_accelerator_plan",
        "description": "Use only when the implementation contains a concrete numerical, signal, image, ML, video, compression, or custom-GPU operator. Detects Apple-silicon backends and recommends Accelerate, BNNS, Metal/MPSGraph, Core ML/ANE, MLX/PyTorch MPS, or VideoToolbox. It does not transparently accelerate arbitrary commands.",
        "inputSchema": {
            "type": "object",
            "required": ["workload"],
            "properties": {
                "workload": {
                    "type": "string",
                    "enum": [
                        "auto",
                        "general",
                        "linear_algebra",
                        "signal",
                        "image",
                        "ml_inference",
                        "ml_training",
                        "video",
                        "compression",
                        "custom_gpu"
                    ],
                },
                "responsiveness": {
                    "type": "string",
                    "enum": ["interactive", "balanced", "throughput"],
                    "default": "interactive",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "parallel_exec",
        "description": "Use only for 2+ independent local argv-based commands whose useful work outweighs scheduling overhead. Do not use for shared-resource mutations, single quick commands, or intrinsically serial work. Runs with machine-adaptive concurrency and a visible result indicator.",
        "_meta": _indicator_ui_meta(),
        "inputSchema": {
            "type": "object",
            "required": ["tasks"],
            "properties": {"tasks": {"type": "array", "items": TASK_SCHEMA, "minItems": 1, "maxItems": MAX_TASKS}, **COMMON_PROPERTIES},
            "additionalProperties": False,
        },
    },
    {
        "name": "parallel_map",
        "description": "Use for 2+ independent repetitions of one local argv template when each item is substantial enough to benefit. Replaces {item} and {index} without a shell and shows a visible result indicator.",
        "_meta": _indicator_ui_meta(),
        "inputSchema": {
            "type": "object",
            "required": ["argv_template", "items"],
            "properties": {
                "argv_template": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "items": {"type": "array", "items": {"type": ["string", "number"]}, "minItems": 1, "maxItems": MAX_TASKS},
                "timeout_seconds": {"type": "number", "minimum": 0.001, "maximum": MAX_TIMEOUT_SECONDS},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
                **COMMON_PROPERTIES,
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "parallel_dag",
        "description": "Use for a local dependency graph that has at least one stage with 2+ independent runnable nodes. Do not use for a fully serial chain. Launches nodes as dependencies succeed and shows a visible result indicator.",
        "_meta": _indicator_ui_meta(),
        "inputSchema": {
            "type": "object",
            "required": ["tasks"],
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_TASKS,
                    "items": {
                        **TASK_SCHEMA,
                        "properties": {**TASK_SCHEMA["properties"], "depends_on": {"type": "array", "items": {"type": "string"}, "default": []}},
                    },
                },
                "fail_fast": {"type": "boolean", "default": False},
                **COMMON_PROPERTIES,
            },
            "additionalProperties": False,
        },
    },
]


async def call_tool(
    name: str,
    arguments: dict[str, Any],
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    if name == "scenario_plan":
        result = scenario_plan(arguments)
    elif name == "task_parallel_scan":
        result = task_parallel_scan(arguments)
    elif name == "atomic_task_plan":
        result = atomic_task_plan(arguments)
    elif name == "atomic_exec":
        result = await run_atomic(arguments, progress_callback)
    elif name == "mac_resource_plan":
        result = concurrency_plan(
            arguments.get("profile", "mixed"),
            arguments.get("max_concurrency"),
            arguments.get("reserve_cores"),
            arguments.get("estimated_memory_mb_per_task"),
            arguments.get("responsiveness", "interactive"),
        )
    elif name == "container_resource_plan":
        result = container_resource_plan(arguments)
    elif name == "mac_accelerator_plan":
        result = accelerator_plan(
            arguments.get("workload", "auto"),
            arguments.get("responsiveness", "interactive"),
        )
    elif name == "parallel_exec":
        result = await run_parallel(arguments, progress_callback)
    elif name == "parallel_map":
        result = await run_map(arguments, progress_callback)
    elif name == "parallel_dag":
        result = await run_dag(arguments, progress_callback)
    else:
        raise InputError(f"unknown tool: {name}")
    indicator_line = result.get("indicator", {}).get("display") if isinstance(result, dict) else None
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if indicator_line:
        rendered = f"{indicator_line}\n\n{rendered}"
    tool_result = {
        "content": [{"type": "text", "text": rendered}],
        "structuredContent": result,
    }
    if indicator_line:
        tool_result["_meta"] = _indicator_ui_meta()
    return tool_result


def _progress_callback(progress_token: Any) -> Any | None:
    if progress_token is None:
        return None
    last_progress = -1.0

    def send(snapshot: dict[str, Any]) -> None:
        nonlocal last_progress
        progress = max(float(snapshot["elapsed_seconds"]), last_progress + 0.001)
        last_progress = progress
        message = (
            f"已运行 {snapshot['elapsed_seconds']:.1f}s｜"
            f"运行中 {snapshot['running_tasks']}｜"
            f"就绪 {snapshot.get('ready_tasks', 0)}｜"
            f"已完成 {snapshot['completed_tasks']}/{snapshot['task_count']}｜"
            f"失败 {snapshot['failed_tasks']}｜"
            f"当前预计节约 {snapshot['estimated_saved_so_far_seconds']:.1f}s"
        )
        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {
                "progressToken": progress_token,
                "progress": round(progress, 3),
                "message": message,
            },
        }
        sys.stdout.write(json.dumps(notification, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    return send


def response_for(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        return None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "At the start of a task that will execute local work, cheaply assess parallel eligibility. "
                    "When the task exposes concrete local entrypoints or typed work, call atomic_task_plan. Pass its complete "
                    "immutable result and plan_hash unchanged to atomic_exec; never translate its schedule into generic exec waves. "
                    "Recompile after a material plan or source-snapshot change. "
                    "For complex, unfamiliar, multi-stage, or trace-informed project optimization, call scenario_plan "
                    "to select from preset goals and guardrails before constructing work units. "
                    "For multiple Docker services or builds with different resource needs, call container_resource_plan "
                    "to allocate within the detected Docker Desktop VM envelope. "
                    "The compiler fails closed on unknown control flow, effects, lifecycle, or unordered writes. "
                    "Keep shared mutations and short serial work direct. For runs over ten seconds use live_runner.py --mode atomic "
                    "in a pollable PTY so elapsed time, active atoms, failures, and estimated savings remain visible. "
                    "Default to interactive responsiveness and never run a benchmark merely to estimate speedup."
                ),
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "resources/list":
            result = {"resources": [_indicator_resource()]}
        elif method == "resources/read":
            uri = message.get("params", {}).get("uri")
            if uri != INDICATOR_RESOURCE_URI:
                raise InputError(f"unknown resource: {uri}")
            result = {
                "contents": [
                    {
                        "uri": INDICATOR_RESOURCE_URI,
                        "mimeType": INDICATOR_MIME_TYPE,
                        "text": _indicator_html(),
                        "_meta": _indicator_resource_meta(),
                    }
                ]
            }
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "tools/call":
            params = message.get("params", {})
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise InputError("tool arguments must be an object")
            request_meta = params.get("_meta") or {}
            progress_token = request_meta.get("progressToken") if isinstance(request_meta, dict) else None
            result = asyncio.run(
                call_tool(
                    params.get("name", ""),
                    arguments,
                    _progress_callback(progress_token),
                )
            )
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except InputError as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
        }
    except Exception as exc:  # noqa: BLE001 - JSON-RPC boundary must return structured errors.
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            },
        }


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            response = response_for(message)
        except json.JSONDecodeError as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
