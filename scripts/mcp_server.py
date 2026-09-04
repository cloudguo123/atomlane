#!/usr/bin/env python3
"""Dependency-free MCP stdio server for bounded local parallel execution."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fnmatch
import hashlib
import importlib.util
import io
import json
import math
import os
import platform
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from atom_engine import (
    MAX_STDIN_BYTES,
    AtomError,
    _normalize_resource,
    _resource_overlap,
    atom_conflicts,
    canonical_plan_hash,
    compile_atomic_plan,
    normalize_json_numbers,
    validate_source_snapshots,
)
from atom_frontends import (
    PYTEST_EMPTY_CONFIG,
    _is_exact_pytest_runner_prefix,
    _is_pytest_runner,
    _path_identity,
    _path_is_within_reserved_pytest_basetemp,
    _path_physical_anchor_identity,
    _paths_are_equivalent,
    _paths_overlap,
    _pytest_baseline_source_coverage,
    _pytest_config_addopts,
    _pytest_environment_plugin_control,
    _pytest_output_overlaps_collection,
    _pytest_owned_options,
    _pytest_plugin_control,
    _pytest_runner_attestation,
    _reject_pytest_module_shadowing,
    _validate_pytest_config_paths,
    _validate_pytest_selector_boundaries,
    _windows_output_path_spelling_is_unambiguous,
    compile_entrypoints,
)
from platform_adapter import (
    brokered_execution_boundary,
    default_stats_path,
    exclusive_file_lock,
    execution_environment,
    load_snapshot,
    memory_snapshot,
    platform_capabilities,
    resolve_host_executable,
    windows_physical_cpu_count,
    windows_process_limit_blocker,
)
from platform_adapter import (
    power_snapshot as portable_power_snapshot,
)
from python_parallel_advisor import AdvisorError, analyze_python_parallelism
from windows_job_runner import (
    CONPTY_STDIN_UNSUPPORTED,
    RunnerError,
    validate_windows_executable_contract,
)
from windows_runtime import WindowsJobController, WindowsJobError

SERVER_NAME = "atomlane"
SERVER_VERSION = "0.16.0"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_CATALOG_PATH = PLUGIN_ROOT / "catalog" / "scenarios.json"
INDICATOR_RESOURCE_URI = f"ui://widget/atomlane-indicator-{SERVER_VERSION}.html"
INDICATOR_MIME_TYPE = "text/html;profile=mcp-app"
INDICATOR_RESOURCE_URI_RE = re.compile(
    r"ui://widget/atomlane-indicator-"
    r"(?:0|[1-9][0-9]{0,5})\."
    r"(?:0|[1-9][0-9]{0,5})\."
    r"(?:0|[1-9][0-9]{0,5})\.html",
    re.ASCII,
)
MAX_TASKS = 128
MAX_CONCURRENCY = 64
MAX_ARGV_ITEMS = 256
MAX_ARG_LENGTH = 32_768
DEFAULT_OUTPUT_BYTES = 8_192
MAX_OUTPUT_BYTES = 65_536
DEFAULT_TIMEOUT_SECONDS = 900.0
MAX_TIMEOUT_SECONDS = 86_400.0
MAX_WINDOWS_ENVIRONMENT_UTF16_UNITS = 32_767
MAX_WINDOWS_SUPERVISOR_PAYLOAD_BYTES = 2_500_000
MAX_JUNIT_REPORT_BYTES = 32 * 1024 * 1024
MAX_JUNIT_TEST_CASES = 100_000
MAX_JUNIT_XML_ELEMENTS = 250_000
MAX_SAVINGS_STATS_BYTES = 64 * 1024
MAX_SERIAL_BASELINE_ATTESTATIONS = 256
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_INVALID_SAVINGS_LEDGER = object()
_STATIC_HARDWARE_CACHE: dict[str, Any] | None = None
_SERIAL_BASELINE_ATTESTATIONS: dict[str, dict[str, Any]] = {}


def _progress_interval_seconds() -> float:
    try:
        return max(0.1, float(os.environ.get("ATOMLANE_PROGRESS_INTERVAL", "1")))
    except ValueError:
        return 1.0


def _stats_path() -> Path:
    return default_stats_path()


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
        "name": "atomlane_indicator",
        "title": "AtomLane 实时执行指示器",
        "description": "并行执行状态与加速倍数卡片。",
        "mimeType": INDICATOR_MIME_TYPE,
        "_meta": _indicator_resource_meta(),
    }


def _is_indicator_resource_uri(uri: Any) -> bool:
    """Accept versioned AtomLane indicator aliases retained by an older task."""
    return (
        isinstance(uri, str)
        and len(uri) <= 96
        and INDICATOR_RESOURCE_URI_RE.fullmatch(uri) is not None
    )


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


def _resolve_scenario_execution(
    default_execution: dict[str, Any],
    environment: dict[str, Any],
) -> dict[str, Any]:
    """Resolve a catalog executor for the current execution realm.

    Scenario matching is intentionally advisory, but it must not recommend an
    executor that is known to be unavailable on the active host.  The legacy
    ``mac_resource_plan`` name is a portable alias, so non-macOS realms receive
    the platform-neutral spelling.  Apple accelerator routing has no generic
    substitute: outside native macOS its goals remain visible as advice while
    executable routing is withheld.
    """

    resolved = dict(default_execution)
    catalog_executor = resolved.get("executor")
    execution_realm = str(environment.get("boundary") or "unknown")
    is_macos_native = execution_realm == "macos_native"
    selected_executor = catalog_executor
    status = "applicable"
    reason = "The catalog executor is available in the current execution realm."

    if catalog_executor == "mac_resource_plan" and not is_macos_native:
        selected_executor = "host_resource_plan"
        status = "adapted"
        reason = (
            "The portable host-resource planner replaces its legacy macOS alias "
            f"in the {execution_realm} realm."
        )
    elif catalog_executor == "mac_accelerator_plan" and not is_macos_native:
        selected_executor = None
        status = "advisory_only"
        reason = (
            "Apple-specific accelerator routing is unavailable in the "
            f"{execution_realm} realm; keep these goals advisory until a concrete "
            "platform-native accelerator owner is detected and validated."
        )

    resolved["executor"] = selected_executor
    resolved["platform_resolution"] = {
        "execution_realm": execution_realm,
        "catalog_executor": catalog_executor,
        "selected_executor": selected_executor,
        "status": status,
        "reason": reason,
    }
    return resolved


def _scenario_match(
    scenario: dict[str, Any],
    inventory: dict[str, Any],
    task_hint: str,
    history: dict[str, Any],
    environment: dict[str, Any],
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
        "default_execution": _resolve_scenario_execution(
            scenario["default_execution"], environment
        ),
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
    environment = execution_environment()
    matches = [
        match
        for scenario in catalog["scenarios"]
        if (
            match := _scenario_match(
                scenario, inventory, task_hint, history, environment
            )
        )
        is not None
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
        platform_resolution = match["default_execution"]["platform_resolution"]
        for goal in match["optimization_goals"]:
            flattened_targets.append(
                {
                    "scenario_id": match["id"],
                    "scenario_title_zh": match["title_zh"],
                    "confidence": match["confidence"],
                    "mode": match["mode"],
                    "layer": match["layer"],
                    "executor": match["default_execution"]["executor"],
                    "execution_status": platform_resolution["status"],
                    "execution_realm": platform_resolution["execution_realm"],
                    **goal,
                }
            )
    category_counts = Counter(str(item.get("category") or "unknown") for item in catalog["scenarios"])
    layer_counts = Counter(str(item.get("layer") or "workflow") for item in catalog["scenarios"])
    goal_count = sum(len(item.get("optimization_goals") or []) for item in catalog["scenarios"])
    high_value = [
        target["id"]
        for target in flattened_targets
        if target["mode"] in {"parallel", "conditional"}
        and target["confidence"] >= 0.45
        and target["executor"] is not None
        and target["execution_status"] != "advisory_only"
    ]
    serial_guards = [match["id"] for match in selected if match["mode"] == "serial_guardrail"]
    advisory_only = [
        match["id"]
        for match in selected
        if match["default_execution"]["platform_resolution"]["status"]
        == "advisory_only"
    ]
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
            "advisory_only_scenario_ids": advisory_only,
            "matched_layer_counts": dict(
                sorted(Counter(match["layer"] for match in selected).items())
            ),
            "recommended_first_step": (
                "Build isolated work units for the highest-confidence parallel scenario, then execute with live progress."
                if high_value
                else (
                    "Keep platform-specific accelerator goals advisory until a concrete native backend is detected and validated."
                    if advisory_only
                    else "Keep work serial until at least two independent, substantial units and isolated outputs are identified."
                )
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
    try:
        return _normalize_resource("file:" + value.removeprefix("file:"), cwd)
    except AtomError as exc:
        raise InputError(f"invalid candidate path {value!r}: {exc}") from exc


def _scan_path_overlap(first: str, second: str) -> bool:
    return _resource_overlap(first, second)


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


def _atomic_workload_profile(arguments: dict[str, Any]) -> str:
    explicit = arguments.get("profile")
    if explicit is not None:
        if explicit not in {"cpu", "io", "mixed", "accelerator"}:
            raise InputError("profile must be cpu, io, mixed, or accelerator")
        return explicit
    profiles: set[str] = set()
    for atom in arguments.get("atoms") or []:
        if isinstance(atom, dict) and atom.get("profile") in {
            "cpu", "io", "mixed", "accelerator"
        }:
            profiles.add(atom["profile"])
    for entrypoint in arguments.get("entrypoints") or []:
        if not isinstance(entrypoint, dict):
            continue
        if entrypoint.get("adapter") == "test_suite":
            profiles.add("cpu")
        elif entrypoint.get("profile") in {"cpu", "io", "mixed", "accelerator"}:
            profiles.add(entrypoint["profile"])
    return next(iter(profiles)) if len(profiles) == 1 else "mixed"


def _atomic_memory_estimate(arguments: dict[str, Any]) -> float | None:
    explicit = arguments.get("estimated_memory_mb_per_task")
    if explicit is not None:
        if (
            isinstance(explicit, bool)
            or not isinstance(explicit, (int, float))
            or not math.isfinite(explicit)
            or explicit <= 0
        ):
            raise InputError("estimated_memory_mb_per_task must be a finite positive number")
        return float(explicit)
    estimates = [
        float(entrypoint["estimated_memory_mb_per_worker"])
        for entrypoint in arguments.get("entrypoints") or []
        if isinstance(entrypoint, dict)
        and isinstance(entrypoint.get("estimated_memory_mb_per_worker"), (int, float))
        and not isinstance(entrypoint.get("estimated_memory_mb_per_worker"), bool)
        and float(entrypoint["estimated_memory_mb_per_worker"]) > 0
    ]
    return max(estimates) if estimates else None


def _atomic_resource_context(
    arguments: dict[str, Any], profile: str
) -> tuple[dict[str, Any], dict[str, float]]:
    responsiveness = arguments.get("responsiveness", "interactive")
    requested = arguments.get("max_concurrency")
    reserve = arguments.get("reserve_cores")
    resource_plan = concurrency_plan(
        profile,
        requested,
        reserve,
        _atomic_memory_estimate(arguments),
        responsiveness,
    )
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
        normalize_json_numbers(canonical),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _current_platform_contract() -> dict[str, Any]:
    capabilities = platform_capabilities()
    environment = capabilities["execution_environment"]
    windows_native = environment["is_windows_native"]
    return {
        "adapter_protocol": "atomlane-platform/v2",
        "os_family": environment["system"].lower(),
        "environment_kind": environment["boundary"],
        "architecture": platform.machine().lower(),
        "path_flavor": "nt" if windows_native else "posix",
        "argv_transport": (
            "windows-supervisor-json-createprocess/v1"
            if windows_native
            else "execve-argv/v1"
        ),
        "process_tree_control": capabilities["process_tree_control"],
        "supported_terminal_modes": capabilities["terminal_modes"],
        "conpty_stdin_supported": capabilities["conpty_stdin_supported"],
        "supported_resource_controls": capabilities["resource_controls"],
        "resource_control_constraints": capabilities[
            "resource_control_constraints"
        ],
    }


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
        profile = _atomic_workload_profile(arguments)
        resource_plan, capacities = _atomic_resource_context(arguments, profile)
        frontend = compile_entrypoints(
            project,
            arguments.get("entrypoints", []),
            native_worker_ceiling=max(1, int(capacities["cpu_core"])),
        )
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
    compiled["platform_contract"] = {
        **_current_platform_contract(),
        "required_terminal_modes": sorted(
            {
                atom.get("operation", {}).get("terminal_mode", "pipes")
                for atom in compiled.get("atoms", [])
            }
        ),
    }
    compiled["resource_plan"] = resource_plan
    compiled["test_suites"] = frontend.get("test_suites", [])
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


def test_suite_plan(arguments: dict[str, Any]) -> dict[str, Any]:
    """Compile one pytest suite through the standard immutable AtomLane plan path."""
    entrypoint_fields = {
        "id",
        "framework",
        "runner_argv",
        "arguments",
        "cwd",
        "config_path",
        "worker_count",
        "distribution",
        "case_count_hint",
        "estimated_memory_mb_per_worker",
        "estimated_duration_seconds",
        "timeout_seconds",
        "junit_path",
        "snapshot_paths",
        "env",
        "declared_accesses",
        "declared_effects",
        "effects_declared_complete",
        "independence_declared",
        "baseline_source_closure_declared",
    }
    entrypoint = {
        key: arguments[key]
        for key in entrypoint_fields
        if key in arguments
    }
    entrypoint["adapter"] = "test_suite"
    entrypoint.setdefault("id", "pytest-suite")
    entrypoint.setdefault("framework", "pytest")
    plan_arguments: dict[str, Any] = {
        "project_path": arguments.get("project_path"),
        "task_summary": arguments.get(
            "task_summary",
            "Run one pytest suite through a resource-bounded native worker pool.",
        ),
        "entrypoints": [entrypoint],
        "profile": "cpu",
    }
    for key in ("responsiveness", "max_concurrency", "reserve_cores"):
        if key in arguments:
            plan_arguments[key] = arguments[key]
    return atomic_task_plan(plan_arguments)


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


def _normalized_savings_stats(current: Any) -> dict[str, Any]:
    if not isinstance(current, dict):
        current = {}

    def nonnegative_count(key: str) -> int:
        raw = current.get(key, 0)
        if isinstance(raw, int) and not isinstance(raw, bool):
            return max(0, raw)
        return 0

    def nonnegative_seconds(key: str) -> float:
        try:
            value = float(current.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value >= 0 else 0.0

    if current.get("schema") == "atomlane/savings-ledger/v2":
        measured_count = nonnegative_count("measured_run_count")
        measured_saved = nonnegative_seconds(
            "cumulative_measured_saved_seconds"
        )
        estimated_count = nonnegative_count("estimated_run_count")
        estimated_saved = nonnegative_seconds(
            "cumulative_estimated_saved_seconds"
        )
        legacy_count = nonnegative_count("legacy_unclassified_run_count")
        legacy_saved = nonnegative_seconds(
            "cumulative_legacy_unclassified_saved_seconds"
        )
    else:
        # Version 1 mixed provenance in its two public counters. Preserve it
        # exactly, but never relabel historical values as measured.
        measured_count = 0
        measured_saved = 0.0
        estimated_count = 0
        estimated_saved = 0.0
        legacy_count = nonnegative_count("run_count")
        legacy_saved = nonnegative_seconds("cumulative_saved_seconds")
    raw_updated = current.get("updated_at_epoch_seconds", 0.0)
    try:
        updated = float(raw_updated)
    except (TypeError, ValueError):
        updated = 0.0
    if not math.isfinite(updated) or updated < 0:
        updated = 0.0
    return {
        "schema": "atomlane/savings-ledger/v2",
        "run_count": legacy_count + measured_count,
        "cumulative_saved_seconds": round(legacy_saved + measured_saved, 6),
        "measured_run_count": measured_count,
        "cumulative_measured_saved_seconds": round(measured_saved, 6),
        "estimated_run_count": estimated_count,
        "cumulative_estimated_saved_seconds": round(estimated_saved, 6),
        "legacy_unclassified_run_count": legacy_count,
        "cumulative_legacy_unclassified_saved_seconds": round(legacy_saved, 6),
        "updated_at_epoch_seconds": round(updated, 3),
    }


def _savings_stats_document_is_valid(current: Any) -> bool:
    """Validate an existing ledger before any read-modify-write migration."""
    if current == {}:
        return True
    if not isinstance(current, dict):
        return False

    def valid_count(key: str) -> bool:
        value = current.get(key)
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def valid_seconds(key: str) -> bool:
        value = current.get(key)
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0
        )

    if "schema" not in current:
        if set(current) != {
            "run_count",
            "cumulative_saved_seconds",
            "updated_at_epoch_seconds",
        }:
            return False
        return (
            valid_count("run_count")
            and valid_seconds("cumulative_saved_seconds")
            and valid_seconds("updated_at_epoch_seconds")
        )
    if current.get("schema") != "atomlane/savings-ledger/v2":
        return False
    expected_keys = {
        "schema",
        "run_count",
        "cumulative_saved_seconds",
        "measured_run_count",
        "cumulative_measured_saved_seconds",
        "estimated_run_count",
        "cumulative_estimated_saved_seconds",
        "legacy_unclassified_run_count",
        "cumulative_legacy_unclassified_saved_seconds",
        "updated_at_epoch_seconds",
    }
    if set(current) != expected_keys:
        return False
    count_keys = (
        "run_count",
        "measured_run_count",
        "estimated_run_count",
        "legacy_unclassified_run_count",
    )
    seconds_keys = (
        "cumulative_saved_seconds",
        "cumulative_measured_saved_seconds",
        "cumulative_estimated_saved_seconds",
        "cumulative_legacy_unclassified_saved_seconds",
        "updated_at_epoch_seconds",
    )
    if not all(valid_count(key) for key in count_keys) or not all(
        valid_seconds(key) for key in seconds_keys
    ):
        return False
    if current["run_count"] != (
        current["measured_run_count"] + current["legacy_unclassified_run_count"]
    ):
        return False
    expected_saved = round(
        float(current["cumulative_measured_saved_seconds"])
        + float(current["cumulative_legacy_unclassified_saved_seconds"]),
        6,
    )
    return math.isclose(
        float(current["cumulative_saved_seconds"]),
        expected_saved,
        rel_tol=0.0,
        abs_tol=1e-6,
    )


def _read_time_saved() -> dict[str, Any]:
    """Read a sanitized cumulative ledger without incrementing it."""
    path = _stats_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with exclusive_file_lock(lock_path):
        current = _read_savings_stats_document(path)
        if (
            current is _INVALID_SAVINGS_LEDGER
            or not _savings_stats_document_is_valid(current)
        ):
            raise OSError("existing savings ledger is invalid or unreadable")
        return _normalized_savings_stats(current)


def _read_savings_stats_document(path: Path) -> Any:
    """Read a small regular stats file without following links or blocking on FIFOs."""
    try:
        path_state = path.lstat()
    except FileNotFoundError:
        return {}
    if (
        stat.S_ISLNK(path_state.st_mode)
        or getattr(path_state, "st_reparse_tag", 0)
        or not stat.S_ISREG(path_state.st_mode)
        or path_state.st_size > MAX_SAVINGS_STATS_BYTES
    ):
        return _INVALID_SAVINGS_LEDGER
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return _INVALID_SAVINGS_LEDGER
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (path_state.st_dev, path_state.st_ino)
            or opened.st_size > MAX_SAVINGS_STATS_BYTES
        ):
            return _INVALID_SAVINGS_LEDGER
        chunks: list[bytes] = []
        remaining = MAX_SAVINGS_STATS_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(16_384, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_SAVINGS_STATS_BYTES:
            return _INVALID_SAVINGS_LEDGER
        if not payload:
            return _INVALID_SAVINGS_LEDGER
        parsed = json.loads(payload.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else _INVALID_SAVINGS_LEDGER
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _INVALID_SAVINGS_LEDGER
    finally:
        os.close(descriptor)


def _record_time_saved(
    seconds: float,
    *,
    evidence_kind: str = "measured",
) -> dict[str, Any]:
    """Atomically record one measured credit or uncredited estimate."""
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(float(seconds))
        or float(seconds) < 0
    ):
        raise ValueError("time savings must be a finite non-negative number")
    if evidence_kind not in {"measured", "estimated"}:
        raise ValueError("evidence_kind must be measured or estimated")
    path = _stats_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with exclusive_file_lock(lock_path):
        raw_current = _read_savings_stats_document(path)
        if (
            raw_current is _INVALID_SAVINGS_LEDGER
            or not _savings_stats_document_is_valid(raw_current)
        ):
            raise OSError("existing savings ledger is invalid or unreadable")
        current = _normalized_savings_stats(raw_current)
        updated = dict(current)
        if evidence_kind == "measured":
            updated["measured_run_count"] += 1
            updated["cumulative_measured_saved_seconds"] = round(
                updated["cumulative_measured_saved_seconds"] + float(seconds),
                6,
            )
        else:
            updated["estimated_run_count"] += 1
            updated["cumulative_estimated_saved_seconds"] = round(
                updated["cumulative_estimated_saved_seconds"] + float(seconds),
                6,
            )
        updated["run_count"] = (
            updated["legacy_unclassified_run_count"]
            + updated["measured_run_count"]
        )
        updated["cumulative_saved_seconds"] = round(
            updated["cumulative_legacy_unclassified_saved_seconds"]
            + updated["cumulative_measured_saved_seconds"],
            6,
        )
        updated["updated_at_epoch_seconds"] = round(time.time(), 3)
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


class ProgressReporter:
    """Emit monotonic MCP progress heartbeats while a scheduler invocation is active."""

    def __init__(
        self,
        task_count: int,
        callback: Any | None,
        started: float,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.task_count = task_count
        self.callback = callback
        self.started = started
        self.active: dict[str, float] = {}
        self.completed: list[dict[str, Any]] = []
        self.ready_tasks = 0
        self.context = dict(context or {})
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
        saved_so_far = max(0.0, completed_runtime + active_runtime - elapsed)
        failed = sum(
            1 for item in self.completed if item.get("status") in {"failed", "timed_out"}
        )
        savings_eligible = failed == 0
        savings_pending_native_report = bool(
            self.context.get("savings_pending_native_report")
        )
        if not savings_eligible:
            saved_so_far = 0.0
        snapshot = {
            "elapsed_seconds": round(elapsed, 3),
            "running_tasks": len(self.active),
            "ready_tasks": self.ready_tasks,
            "completed_tasks": len(self.completed),
            "task_count": self.task_count,
            "failed_tasks": failed,
            "estimated_saved_so_far_seconds": (
                None
                if savings_pending_native_report and savings_eligible
                else round(saved_so_far, 3)
            ),
            "savings_eligible_so_far": savings_eligible,
            "savings_ineligible_reason": (
                None if savings_eligible else "a task has failed or timed out"
            ),
        }
        if self.context:
            snapshot.update(self.context)
        return snapshot

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


def _atomic_progress_context(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Describe native pytest capacity without calling it observed concurrency."""
    suites = plan.get("test_suites")
    if not isinstance(suites, list):
        return None
    native_suites = [
        suite
        for suite in suites
        if isinstance(suite, dict)
        and suite.get("strategy") == "native_worker_pool"
        and isinstance(suite.get("configured_workers"), int)
        and not isinstance(suite.get("configured_workers"), bool)
        and suite["configured_workers"] > 1
    ]
    if not native_suites:
        return None
    hints = [suite.get("case_count_hint") for suite in native_suites]
    test_cases_planned = (
        sum(int(hint) for hint in hints)
        if all(
            isinstance(hint, int) and not isinstance(hint, bool) and hint > 0
            for hint in hints
        )
        else None
    )
    return {
        # Multiple native pools may be serialized by the resource envelope.
        # Use the largest configured pool, never an inferred simultaneous total.
        "native_workers_configured": max(
            int(suite["configured_workers"]) for suite in native_suites
        ),
        "test_cases_planned": test_cases_planned,
        "savings_pending_native_report": True,
    }


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
    if platform.system() != "Darwin" or not resolve_host_executable("osascript"):
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
    if platform.system() != "Darwin" or not resolve_host_executable("system_profiler"):
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
    result: dict[str, Any] = {
        "source": None,
        "battery_percent": None,
        "low_power_mode": None,
    }
    if platform.system() != "Darwin":
        return portable_power_snapshot()
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


def python_parallel_advisor(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return bounded, non-executing Python source parallelization advice."""
    allowed_fields = {
        "project_path",
        "paths",
        "hotspots",
        "max_files",
        "max_candidates",
        "max_workers",
        "estimated_memory_mb_per_worker",
        "minimum_hotspot_seconds",
        "execution_context",
        "responsiveness",
        "include_rewrite_previews",
        "target_platform",
    }
    if not isinstance(arguments, dict):
        raise InputError("arguments must be an object")
    unknown_fields = sorted(set(arguments) - allowed_fields)
    if unknown_fields:
        raise InputError("unknown python_parallel_advisor fields: " + ", ".join(unknown_fields))
    project_text = arguments.get("project_path")
    if (
        not isinstance(project_text, str)
        or not project_text
        or len(project_text) > 4096
        or "\x00" in project_text
        or not os.path.isabs(project_text)
    ):
        raise InputError("project_path must be an absolute path")
    try:
        project = Path(project_text).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InputError(f"project_path cannot be resolved: {project_text}") from exc
    if not project.is_dir():
        raise InputError(f"project_path does not exist or is not a directory: {project}")
    responsiveness = arguments.get("responsiveness", "interactive")
    if not isinstance(responsiveness, str) or responsiveness not in {
        "interactive",
        "balanced",
        "throughput",
    }:
        raise InputError("responsiveness must be one of: interactive, balanced, throughput")
    requested_workers = arguments.get("max_workers")
    if requested_workers is not None and (
        isinstance(requested_workers, bool)
        or not isinstance(requested_workers, int)
        or not 1 <= requested_workers <= MAX_CONCURRENCY
    ):
        raise InputError(f"max_workers must be between 1 and {MAX_CONCURRENCY}")
    estimated_memory = arguments.get("estimated_memory_mb_per_worker")
    if estimated_memory is not None and (
        isinstance(estimated_memory, bool)
        or not isinstance(estimated_memory, (int, float))
        or estimated_memory <= 0
        or estimated_memory > 1_000_000_000
        or (isinstance(estimated_memory, float) and not math.isfinite(estimated_memory))
    ):
        raise InputError("estimated_memory_mb_per_worker must be finite and positive")
    target_platform = arguments.get("target_platform", "auto")
    if target_platform not in {"auto", "windows", "darwin", "linux"}:
        raise InputError("target_platform must be auto, windows, darwin, or linux")
    windows_target = target_platform == "windows" or (
        target_platform == "auto" and platform.system() == "Windows"
    )
    requested_for_plan = requested_workers
    if windows_target:
        requested_for_plan = min(requested_workers or 61, 61)
    resource_plan = concurrency_plan(
        "cpu",
        requested_for_plan,
        None,
        estimated_memory,
        responsiveness,
    )
    try:
        result = analyze_python_parallelism(
            project,
            paths=arguments.get("paths"),
            hotspots=arguments.get("hotspots"),
            max_files=arguments.get("max_files", 128),
            max_candidates=arguments.get("max_candidates", 32),
            max_workers=resource_plan["chosen_concurrency"],
            minimum_hotspot_seconds=arguments.get("minimum_hotspot_seconds", 10.0),
            execution_context=arguments.get("execution_context", "standalone"),
            include_rewrite_previews=arguments.get("include_rewrite_previews", True),
            target_platform=target_platform,
        )
    except (AdvisorError, OSError) as exc:
        raise InputError(str(exc)) from exc
    result["resource_plan"] = resource_plan
    result["resource_plan"]["target_worker_ceiling"] = 61 if windows_target else MAX_CONCURRENCY
    result["advice_contract"] = {
        "target_code_executed": False,
        "target_code_imported": False,
        "files_modified": False,
        "rewrite_previews_require_source_hash_match": True,
        "performance_claim_requires_measured_validation": True,
        "automatic_patch_application": False,
    }
    return result


def _memory_free_percent() -> int | None:
    if platform.system() == "Darwin":
        raw = _run_probe(["memory_pressure", "-Q"])
        match = re.search(r"free percentage:\s*(\d+)%", raw or "")
        return int(match.group(1)) if match else None
    return memory_snapshot()["free_percent"]


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
        elif platform.system() == "Windows":
            physical = windows_physical_cpu_count(logical)
            portable_memory = memory_snapshot()
            memory_total = portable_memory["total_bytes"] or 0
            chip = platform.processor() or None
        elif platform.system() == "Linux":
            portable_memory = memory_snapshot()
            memory_total = portable_memory["total_bytes"] or 0
            chip = platform.processor() or None
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

    return memory_snapshot()["available_bytes"]


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

    load = load_snapshot(logical)
    performance_cores = sum(
        item["physical_cpus"] for item in performance_levels if "performance" in item["name"].lower()
    )
    efficiency_cores = sum(
        item["physical_cpus"] for item in performance_levels if "efficiency" in item["name"].lower()
    )
    thermal_names = {0: "nominal", 1: "fair", 2: "serious", 3: "critical"}
    thermal_value = process_state.get("thermal_state")
    power = _power_snapshot()
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "execution_environment": execution_environment(),
        "capabilities": platform_capabilities(),
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
        "load_average": load,
        "thermal_state": thermal_names.get(thermal_value, "unknown"),
        "low_power_mode": (
            process_state.get("low_power_mode")
            if process_state.get("low_power_mode") is not None
            else power.get("low_power_mode")
        ),
        "power": power,
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
    nice_adjustment = (
        {"interactive": 10, "balanced": 5, "throughput": 0}[responsiveness]
        if os.name == "posix"
        else 0
    )
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
    if not resolve_host_executable("xcrun"):
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

    ffmpeg = resolve_host_executable("ffmpeg")
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

    if platform.system() != "Darwin":
        return {
            "status": "unavailable_on_this_platform",
            "selected_backend": None,
            "workload": workload,
            "execution_environment": execution_environment(),
            "reason": (
                "mac_accelerator_plan is Apple-specific; use host_resource_plan or a "
                "platform-native accelerator owner on this host"
            ),
            "automatic_changes": False,
        }

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
    host_boundary = execution_environment()["boundary"]
    docker = resolve_host_executable("docker")
    if not docker:
        return {
            "available": False,
            "reason": "docker CLI was not found",
            "vm_cpus": None,
            "vm_memory_bytes": None,
            "host_boundary": host_boundary,
            "realm": None,
        }
    context = _run_probe([docker, "context", "show"])
    info = _json_probe([docker, "info", "--format", "{{json .}}"])
    if not isinstance(info, dict):
        return {
            "available": False,
            "reason": "docker daemon information was unavailable; Docker Desktop may be stopped or waking from Resource Saver",
            "vm_cpus": None,
            "vm_memory_bytes": None,
            "client_version": _run_probe([docker, "version", "--format", "{{.Client.Version}}"]),
            "context": context,
            "host_boundary": host_boundary,
            "realm": None,
        }
    daemon_identity = str(info.get("ID") or "")
    os_type = str(info.get("OSType") or "unknown").lower()
    realm_material = f"{daemon_identity}\0{os_type}".encode()
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
        "os_type": os_type,
        "daemon_id": daemon_identity or None,
        "context": context,
        "host_boundary": host_boundary,
        "realm": "docker:" + hashlib.sha256(realm_material).hexdigest()[:16],
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
    daemon_os = str(docker.get("os_type") or "").lower()
    applicable = bool(docker.get("available") and daemon_os == "linux")
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
            "Docker Desktop cpuset IDs are Linux VM vCPUs, not stable mappings to physical host cores"
        )
    if docker.get("available") and daemon_os != "linux":
        warnings.append(
            "Windows containers are outside the Preview resource contract; emitted values are advisory only"
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
        "status": "applicable" if applicable else "advisory_only",
        "apply_safe": applicable,
        "execution_realms": {
            "host": execution_environment()["boundary"],
            "docker": docker.get("realm"),
            "same_resource_envelope": False,
        },
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
            "The plan emits Docker Compose and docker run controls inside the identified daemon realm. "
            "It never treats native Windows, WSL, and the Docker Linux VM as one resource envelope, "
            "does not change VM-wide settings, and does not guarantee physical-core affinity."
        ),
    }


def _bounded_number(value: Any, name: str, default: float, maximum: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{name} must be a number")
    if not math.isfinite(float(value)) or value <= 0 or value > maximum:
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


def _merge_environment(
    base: dict[str, str], overrides: dict[str, str], *, case_insensitive: bool
) -> dict[str, str]:
    if not case_insensitive:
        return {**base, **overrides}
    merged: dict[str, tuple[str, str]] = {
        key.casefold(): (key, value) for key, value in base.items()
    }
    for key, value in overrides.items():
        merged[key.casefold()] = (key, value)
    return dict(merged.values())


def _base_process_environment() -> dict[str, str]:
    """Drop Windows' hidden `=C:` drive entries before serializing a child env."""
    return {
        key: value
        for key, value in os.environ.items()
        if key and "=" not in key and "\0" not in key + value
    }


def _windows_environment_utf16_units(env: dict[str, str]) -> int:
    try:
        return 1 + sum(
            len(f"{key}={value}\0".encode("utf-16-le")) // 2 for key, value in env.items()
        )
    except UnicodeEncodeError as exc:
        raise InputError("Windows environment contains an invalid Unicode surrogate") from exc


def _windows_string_utf16_units(value: str, name: str) -> int:
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise InputError(f"{name} contains an invalid Unicode surrogate") from exc


def _validate_windows_command_line(argv: list[str], task_id: str) -> str:
    """Return CreateProcessW command text only when it fits the OS limit."""
    command_line = subprocess.list2cmdline(argv)
    if _windows_string_utf16_units(
        command_line, f"task {task_id} command line"
    ) + 1 > 32_767:
        raise InputError(
            f"task {task_id} exceeds the Windows CreateProcessW command-line limit"
        )
    return command_line


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
    environment_keys: set[str] = set()
    for key, value in env_raw.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "=" in key
            or "\x00" in key + value
        ):
            raise InputError(f"task {task_id} environment keys and values must be NUL-free strings")
        identity = key.casefold() if os.name == "nt" else key
        if os.name == "nt" and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise InputError(
                f"task {task_id} Windows Preview environment names must use ASCII identifier syntax"
            )
        if identity in environment_keys:
            raise InputError(
                f"task {task_id} has environment keys that collide under host semantics: {key}"
            )
        environment_keys.add(identity)
        env[key] = value

    stdin = raw.get("stdin")
    if stdin is not None:
        if not isinstance(stdin, str):
            raise InputError(f"task {task_id} stdin must be a string")
        try:
            stdin_size = len(stdin.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise InputError(f"task {task_id} stdin must be valid UTF-8 text") from exc
        if stdin_size > MAX_STDIN_BYTES:
            raise InputError(f"task {task_id} stdin exceeds {MAX_STDIN_BYTES} bytes")

    dependencies = raw.get("depends_on", [])
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        raise InputError(f"task {task_id} depends_on must be an array of task IDs")
    side_effect = raw.get("side_effect", False)
    if not isinstance(side_effect, bool):
        raise InputError(f"task {task_id} side_effect must be boolean")

    terminal_mode = raw.get("terminal_mode", "pipes")
    if terminal_mode not in {"pipes", "conpty"}:
        raise InputError(f"task {task_id} terminal_mode must be pipes or conpty")
    if terminal_mode == "conpty" and stdin is not None:
        raise InputError(f"task {task_id} {CONPTY_STDIN_UNSUPPORTED}")
    capabilities = platform_capabilities()
    if terminal_mode not in capabilities["terminal_modes"]:
        raise InputError(
            f"task {task_id} terminal_mode {terminal_mode!r} is unavailable in "
            f"{capabilities['execution_environment']['boundary']}"
        )

    resources_raw = raw.get("resources", {})
    if not isinstance(resources_raw, dict):
        raise InputError(f"task {task_id} resources must be an object")
    unknown_resources = sorted(
        set(resources_raw) - {"cpu_rate_percent", "memory_limit_mb", "max_processes"}
    )
    if unknown_resources:
        raise InputError(
            f"task {task_id} has unknown resource controls: {', '.join(unknown_resources)}"
        )
    cpu_rate = resources_raw.get("cpu_rate_percent")
    if cpu_rate is not None:
        cpu_rate = _bounded_number(
            cpu_rate, f"task {task_id} resources.cpu_rate_percent", 100.0, 100.0
        )
        if cpu_rate < 0.01:
            raise InputError(
                f"task {task_id} resources.cpu_rate_percent must be at least 0.01"
            )
    memory_limit = resources_raw.get("memory_limit_mb")
    if memory_limit is not None:
        memory_limit = _bounded_number(
            memory_limit,
            f"task {task_id} resources.memory_limit_mb",
            128.0,
            1_048_576.0,
        )
        if memory_limit < 128:
            raise InputError(
                f"task {task_id} resources.memory_limit_mb must be at least 128"
            )
    max_processes = resources_raw.get("max_processes")
    if max_processes is not None and (
        isinstance(max_processes, bool)
        or not isinstance(max_processes, int)
        or not 2 <= max_processes <= 4096
    ):
        raise InputError(f"task {task_id} resources.max_processes must be between 2 and 4096")
    process_limit_blocker = windows_process_limit_blocker(terminal_mode, max_processes)
    if process_limit_blocker is not None:
        raise InputError(f"task {task_id} {process_limit_blocker}")
    resources = {
        "cpu_rate_percent": cpu_rate,
        "memory_limit_mb": memory_limit,
        "max_processes": max_processes,
    }
    if any(value is not None for value in resources.values()) and os.name != "nt":
        raise InputError(
            f"task {task_id} Job Object resource controls require native Windows"
        )

    argv = _validate_argv(raw.get("argv"))
    if os.name == "nt":
        try:
            validate_windows_executable_contract(argv[0])
        except RunnerError as exc:
            raise InputError(f"task {task_id} {exc}") from exc
        _validate_windows_command_line(argv, task_id)
    broker_boundary = brokered_execution_boundary(argv[0])
    if broker_boundary is not None and any(value is not None for value in resources.values()):
        raise InputError(
            f"task {task_id} launches the {broker_boundary['target_realm']} broker; "
            "Windows Job Object limits apply only to the client, not the brokered workload"
        )

    return {
        "id": task_id,
        "argv": argv,
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
        "terminal_mode": terminal_mode,
        "resources": resources,
        "execution_realm": capabilities["execution_environment"]["boundary"],
        "broker_boundary": broker_boundary,
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

    def render(self) -> tuple[str, bool, int, bool]:
        truncated = self.total > self.limit
        if truncated:
            data = bytes(self.head) + b"\n...<output truncated>...\n" + bytes(self.tail)
        else:
            data = bytes(self.head) + bytes(self.tail)
        try:
            text = data.decode("utf-8", errors="strict")
            replacement_used = False
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
            replacement_used = True
        return text, truncated, self.total, replacement_used


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


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    windows_job: WindowsJobController | None = None,
) -> dict[str, Any]:
    """Terminate the complete platform process tree on timeout or cancellation."""
    if windows_job is not None:
        termination = await asyncio.to_thread(
            windows_job.terminate_and_wait_empty, 1, 3.0
        )
        if process.returncode is None:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        return termination
    if process.returncode is not None:
        return {"status": "already_exited", "verified_empty": None}
    if os.name != "posix":
        process.kill()
        await process.wait()
        return {"status": "terminated", "verified_empty": None}
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"status": "already_exited", "verified_empty": None}
    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
        return {"status": "terminated", "verified_empty": None}
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    with contextlib.suppress(Exception):
        await process.wait()
    return {"status": "terminated", "verified_empty": None}


async def _settle_process_io(
    input_task: asyncio.Task[None] | None,
    readers: list[asyncio.Task[None]],
    timeout_seconds: float = 5.0,
) -> bool:
    tasks = [task for task in [input_task, *readers] if task is not None]
    if not tasks:
        return True
    done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
    for task in pending:
        task.cancel()
    if pending:
        second_done, still_pending = await asyncio.wait(pending, timeout=0.5)
        done.update(second_done)
        for task in still_pending:
            task.cancel()
    for task in done:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()
    return not pending


async def _wait_for_process_exit(
    process: asyncio.subprocess.Process,
    windows_job: WindowsJobController | None,
    timeout_seconds: float,
) -> None:
    """Wait for process exit independently from Windows pipe lifetime."""

    if windows_job is None:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        return
    deadline = time.monotonic() + timeout_seconds
    while not windows_job.assigned_process_has_exited():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        await asyncio.sleep(min(0.02, remaining))


def _windows_job_process_slot_reservations(
    terminal_mode: str, max_processes: int | None
) -> tuple[str, ...]:
    """Reserve verified Job members only when ActiveProcessLimit is enabled."""

    if max_processes is None:
        return ()
    blocker = windows_process_limit_blocker(terminal_mode, max_processes)
    if blocker is not None:
        raise InputError(blocker)
    return ("supervisor",)


def _reported_windows_containment_scope(
    broker_boundary: Any, job_scope: str
) -> str:
    """Use the native Job scope only when no broker boundary exists."""

    if broker_boundary is None:
        return job_scope
    if (
        isinstance(broker_boundary, dict)
        and broker_boundary.get("containment_scope")
        == "client_and_inherited_windows_descendants_only"
    ):
        return "client_and_inherited_windows_descendants_only"
    raise InputError("invalid Windows broker containment boundary")


async def _launch_windows_task(
    task: dict[str, Any],
    target_env: dict[str, str],
) -> tuple[asyncio.subprocess.Process, WindowsJobController, bytes]:
    """Launch a waiting supervisor, contain it, then release one target record."""
    resources = task.get(
        "resources",
        {"cpu_rate_percent": None, "memory_limit_mb": None, "max_processes": None},
    )
    max_processes = resources.get("max_processes")
    # Validate every fail-closed process-limit combination before the trusted
    # waiting supervisor exists, even if an internal caller bypassed normalization.
    _windows_job_process_slot_reservations(
        task.get("terminal_mode", "pipes"), max_processes
    )
    _validate_windows_command_line(task["argv"], task["id"])
    if _windows_environment_utf16_units(target_env) > MAX_WINDOWS_ENVIRONMENT_UTF16_UNITS:
        raise InputError(
            f"task {task['id']} Windows environment exceeds "
            f"{MAX_WINDOWS_ENVIRONMENT_UTF16_UNITS} UTF-16 code units"
        )
    payload = {
        "protocol": "atomlane-windows-supervisor/v1",
        "argv": task["argv"],
        "cwd": task["cwd"],
        "stdin_base64": (
            base64.b64encode(task["stdin"].encode("utf-8")).decode("ascii")
            if task["stdin"] is not None
            else None
        ),
        "terminal_mode": task.get("terminal_mode", "pipes"),
        "env": target_env,
    }
    launch_input = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
    if len(launch_input) > MAX_WINDOWS_SUPERVISOR_PAYLOAD_BYTES:
        raise InputError(
            f"task {task['id']} Windows launch record exceeds "
            f"{MAX_WINDOWS_SUPERVISOR_PAYLOAD_BYTES} bytes"
        )
    runner = SCRIPT_DIR / "windows_job_runner.py"
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    python_root = Path(sys.executable).resolve().parent
    supervisor_paths = [
        python_root,
        python_root / "DLLs",
        Path(system_root) / "System32",
    ]
    supervisor_env = {
        key: value
        for key, value in os.environ.items()
        if key.casefold()
        in {
            "systemroot",
            "windir",
            "temp",
            "tmp",
            "userprofile",
            "localappdata",
            "pathext",
            "processor_architecture",
        }
    }
    supervisor_env["SystemRoot"] = system_root
    supervisor_env["PATH"] = os.pathsep.join(str(path) for path in supervisor_paths)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-X",
        "utf8",
        str(runner),
        cwd=str(SCRIPT_DIR),
        env=supervisor_env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    job: WindowsJobController | None = None
    try:
        job = WindowsJobController(
            cpu_rate_percent=resources.get("cpu_rate_percent"),
            memory_limit_mb=resources.get("memory_limit_mb"),
            # This is the exact Job-wide member ceiling. The supervisor consumes
            # one slot while alive; no extra slot is added or described as a
            # target-tree guarantee.
            max_processes=max_processes,
        )
        job.assign(process.pid)
        return process, job, launch_input
    except Exception:
        if job is not None:
            job.close()
        if process.returncode is None:
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
        raise


async def execute_task(
    task: dict[str, Any],
    output_limit: int,
    nice_adjustment: int = 0,
    qos_clamp: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    terminal_mode = task.get("terminal_mode", "pipes")
    resources = task.get(
        "resources",
        {"cpu_rate_percent": None, "memory_limit_mb": None, "max_processes": None},
    )
    execution_realm = task.get("execution_realm", execution_environment()["boundary"])
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
        "output_decoding": "utf-8_with_replacement",
        "stdout_decode_replacement": False,
        "stderr_decode_replacement": False,
        "nice_adjustment": nice_adjustment,
        "qos_clamp": qos_clamp,
        "terminal_mode": terminal_mode,
        "output_combined": terminal_mode == "conpty",
        "execution_realm": execution_realm,
        "requested_resource_controls": resources,
        "applied_resource_controls": None,
        "resource_controls": None,
        "process_tree_backend": "not_started",
        "containment_scope": "not_started",
        "broker_boundary": task.get("broker_boundary"),
    }
    env = _merge_environment(
        _base_process_environment(), task["env"], case_insensitive=os.name == "nt"
    )
    process: asyncio.subprocess.Process | None = None
    windows_job: WindowsJobController | None = None
    readers: list[asyncio.Task[None]] = []
    input_task: asyncio.Task[None] | None = None
    try:
        launch_argv = task["argv"]
        nice_binary = resolve_host_executable("nice")
        if os.name == "posix" and nice_adjustment > 0 and nice_binary:
            launch_argv = [nice_binary, "-n", str(nice_adjustment), *launch_argv]
        taskpolicy = resolve_host_executable("taskpolicy")
        if qos_clamp and taskpolicy:
            launch_argv = [taskpolicy, "-c", qos_clamp, *launch_argv]
        launch_input: bytes | None = None
        if os.name == "nt":
            process, windows_job, launch_input = await _launch_windows_task(task, env)
            job_description = windows_job.description()
            process_slot_reservations = _windows_job_process_slot_reservations(
                terminal_mode, resources.get("max_processes")
            )
            job_scope = "supervisor_and_inherited_windows_tree"
            job_description.update(
                {
                    "containment_scope": job_scope,
                    "resource_scope": job_scope,
                    "verified_job_internal_processes": ["supervisor"],
                    "job_process_slot_reservations": list(process_slot_reservations),
                    "job_reserved_process_slots": len(process_slot_reservations),
                    "job_active_process_limit_semantics": (
                        "all_job_members_including_supervisor"
                        if resources.get("max_processes") is not None
                        else "not_enabled"
                    ),
                    "target_process_capacity_at_launch": (
                        resources["max_processes"] - len(process_slot_reservations)
                        if resources.get("max_processes") is not None
                        else None
                    ),
                    "conpty_host_job_membership": (
                        "not_verified" if terminal_mode == "conpty" else "not_applicable"
                    ),
                }
            )
            applied_controls = {
                **resources,
                **{
                    key: value
                    for key, value in job_description.items()
                    if key != "backend"
                },
            }
            result["applied_resource_controls"] = applied_controls
            result["resource_controls"] = applied_controls
            result["process_tree_backend"] = job_description["backend"]
            result["containment_scope"] = _reported_windows_containment_scope(
                task.get("broker_boundary"), job_description["containment_scope"]
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *launch_argv,
                cwd=task["cwd"],
                env=env,
                stdin=(
                    asyncio.subprocess.PIPE
                    if task["stdin"] is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            result["process_tree_backend"] = "posix_session"
            result["containment_scope"] = "posix_process_group"
            result["applied_resource_controls"] = {}
            result["resource_controls"] = {}
        stdout_capture = _BoundedCapture(output_limit)
        stderr_capture = _BoundedCapture(output_limit)
        readers = [
            asyncio.create_task(_read_bounded_stream(process.stdout, stdout_capture)),
            asyncio.create_task(_read_bounded_stream(process.stderr, stderr_capture)),
        ]
        if os.name == "nt":
            input_task = asyncio.create_task(
                _write_process_stdin(process.stdin, launch_input or b"")
            )
        elif task["stdin"] is not None:
            input_task = asyncio.create_task(
                _write_process_stdin(process.stdin, task["stdin"].encode("utf-8"))
            )
        try:
            # stdin delivery runs concurrently; the wall timeout starts as soon
            # as the process is spawned even if the child never reads its pipe.
            await _wait_for_process_exit(
                process, windows_job, task["timeout_seconds"]
            )
        except asyncio.TimeoutError:
            result["status"] = "timed_out"
            result["outcome"] = "unknown" if task.get("side_effect") else "not_completed"
            result["automatic_retry_allowed"] = False if task.get("side_effect") else None
            try:
                result["process_tree_termination"] = await _terminate_process_tree(
                    process, windows_job
                )
            except (OSError, WindowsJobError, asyncio.TimeoutError) as exc:
                result["status"] = "failed"
                result["failure_kind"] = "process_tree_termination"
                result["outcome"] = "unknown"
                stderr_capture.feed(
                    f"AtomLane could not verify process-tree termination: {exc}\n".encode()
                )
        else:
            if windows_job is not None:
                try:
                    result["process_tree_termination"] = await asyncio.to_thread(
                        windows_job.terminate_and_wait_empty, 0, 3.0
                    )
                    if process.returncode is None:
                        await asyncio.wait_for(process.wait(), timeout=3.0)
                except (WindowsJobError, asyncio.TimeoutError) as exc:
                    result["status"] = "failed"
                    result["failure_kind"] = "process_tree_containment"
                    result["outcome"] = "unknown"
                    stderr_capture.feed(
                        f"AtomLane could not verify an empty Job Object: {exc}\n".encode()
                    )
        io_settled = await _settle_process_io(input_task, readers)
        if not io_settled:
            result["status"] = "failed"
            result["failure_kind"] = "process_io_drain_timeout"
            result["outcome"] = "unknown"
            stderr_capture.feed(
                b"AtomLane bounded I/O drain expired; a descendant may have retained a pipe.\n"
            )
        result["returncode"] = process.returncode
        if result["status"] != "timed_out" and "failure_kind" not in result:
            result["status"] = "succeeded" if process.returncode == 0 else "failed"
            result["outcome"] = "committed" if process.returncode == 0 else "not_committed_or_unknown"
        (
            result["stdout"],
            result["stdout_truncated"],
            result["stdout_bytes"],
            result["stdout_decode_replacement"],
        ) = stdout_capture.render()
        (
            result["stderr"],
            result["stderr_truncated"],
            result["stderr_bytes"],
            result["stderr_decode_replacement"],
        ) = stderr_capture.render()
    except asyncio.CancelledError as cancelled:
        try:
            if process is not None:
                await _terminate_process_tree(process, windows_job)
        except (OSError, WindowsJobError, asyncio.TimeoutError) as exc:
            if hasattr(cancelled, "add_note"):
                cancelled.add_note(
                    f"AtomLane containment cleanup failed during cancellation: {exc}"
                )
        finally:
            await _settle_process_io(input_task, readers, timeout_seconds=1.0)
        raise
    except FileNotFoundError as exc:
        result["stderr"] = str(exc)
    except PermissionError as exc:
        result["stderr"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - isolate one malformed child from its batch.
        result["stderr"] = f"{type(exc).__name__}: {exc}"
    finally:
        if process is not None and process.returncode is None:
            try:
                result["process_tree_termination"] = await _terminate_process_tree(
                    process, windows_job
                )
            except (OSError, WindowsJobError, asyncio.TimeoutError) as exc:
                result["status"] = "failed"
                result["failure_kind"] = "process_tree_termination"
                result["outcome"] = "unknown"
                result["stderr"] = (
                    result.get("stderr", "")
                    + f"AtomLane cleanup could not verify process-tree termination: {exc}\n"
                )
        await _settle_process_io(input_task, readers, timeout_seconds=1.0)
        if windows_job is not None:
            try:
                windows_job.close()
            except WindowsJobError as exc:
                result["status"] = "failed"
                result["failure_kind"] = "job_handle_close"
                result["outcome"] = "unknown"
                result["stderr"] = (
                    result.get("stderr", "")
                    + f"AtomLane could not close the Job Object: {exc}\n"
                )
    result["duration_seconds"] = round(time.monotonic() - started, 6)
    return result


def _pytest_selection_fingerprint(contract: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InputError(f"pytest selection contract is not canonical JSON: {exc}") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _test_suite_execution_context(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Validate hash-bound test metadata and expose honest native-pool facts."""
    raw_suites = plan.get("test_suites", [])
    if raw_suites in (None, []):
        return None
    if not isinstance(raw_suites, list) or len(raw_suites) > 64:
        raise InputError("compiled_plan test_suites must be a bounded array")
    try:
        declared_project_root = Path(plan["project_root"])
        project_root = declared_project_root.resolve(strict=True)
        if (
            not project_root.is_dir()
            or str(project_root) != str(declared_project_root)
        ):
            raise ValueError("project root identity changed")
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise InputError("compiled_plan pytest project_root is no longer canonical") from exc
    atoms = {
        atom.get("id"): atom
        for atom in plan.get("atoms", [])
        if isinstance(atom, dict) and isinstance(atom.get("id"), str)
    }
    suites: list[dict[str, Any]] = []
    seen_atoms: set[str] = set()
    seen_reports: set[str] = set()
    seen_temp_roots: set[str] = set()

    def path_identity(value: str) -> str:
        return _path_identity(value, os.name)

    for index, raw in enumerate(raw_suites):
        if not isinstance(raw, dict):
            raise InputError(f"compiled_plan test suite {index} must be an object")
        suite_id = raw.get("id")
        atom_id = raw.get("atom_id")
        workers = raw.get("configured_workers")
        junit_path = raw.get("junit_path")
        basetemp_path = raw.get("basetemp_path")
        case_hint = raw.get("case_count_hint")
        explicit_snapshot_count = raw.get("explicit_snapshot_count")
        baseline_source_closure_declared = raw.get(
            "baseline_source_closure_declared"
        )
        baseline_source_coverage = raw.get("baseline_source_coverage")
        selection_contract = raw.get("selection_contract")
        selection_fingerprint = raw.get("selection_fingerprint")
        if raw.get("schema") != "atomlane/test-suite/v1" or raw.get("framework") != "pytest":
            raise InputError(f"compiled_plan test suite {index} has an unsupported contract")
        if not isinstance(suite_id, str) or not suite_id:
            raise InputError(f"compiled_plan test suite {index} has no id")
        if not isinstance(atom_id, str) or atom_id not in atoms or atom_id in seen_atoms:
            raise InputError(f"compiled_plan test suite {suite_id} has an invalid atom_id")
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= MAX_CONCURRENCY:
            raise InputError(f"compiled_plan test suite {suite_id} has invalid configured_workers")
        if case_hint is not None and (
            isinstance(case_hint, bool)
            or not isinstance(case_hint, int)
            or not 1 <= case_hint <= 10_000_000
        ):
            raise InputError(f"compiled_plan test suite {suite_id} has invalid case_count_hint")
        report_identity = path_identity(junit_path) if isinstance(junit_path, str) else ""
        if (
            not isinstance(junit_path, str)
            or not junit_path
            or "\x00" in junit_path
            or not os.path.isabs(junit_path)
            or (
                os.name == "nt"
                and not _windows_output_path_spelling_is_unambiguous(junit_path)
            )
            or report_identity in seen_reports
            or report_identity in seen_temp_roots
            or _path_is_within_reserved_pytest_basetemp(junit_path)
        ):
            raise InputError(f"compiled_plan test suite {suite_id} has an invalid or duplicate junit_path")
        temp_identity = (
            path_identity(basetemp_path) if isinstance(basetemp_path, str) else ""
        )
        if (
            not isinstance(basetemp_path, str)
            or not basetemp_path
            or "\x00" in basetemp_path
            or not os.path.isabs(basetemp_path)
            or temp_identity in seen_temp_roots
            or temp_identity in seen_reports
            or temp_identity == report_identity
        ):
            raise InputError(
                f"compiled_plan test suite {suite_id} has an invalid or duplicate basetemp_path"
            )
        try:
            report_state = Path(junit_path).lstat()
        except FileNotFoundError:
            report_state = None
        try:
            basetemp_state = Path(basetemp_path).lstat()
        except FileNotFoundError:
            basetemp_state = None
        try:
            resolved_report = Path(junit_path).resolve(strict=False)
            resolved_temp = Path(basetemp_path).resolve(strict=False)
            resolved_report_parent = Path(junit_path).parent.resolve(strict=True)
            resolved_temp_parent = Path(basetemp_path).parent.resolve(strict=True)
        except OSError as exc:
            raise InputError(
                f"compiled_plan test suite {suite_id} output parent cannot be revalidated"
            ) from exc
        if (
            path_identity(str(resolved_report)) != report_identity
            or path_identity(str(resolved_temp)) != temp_identity
            or path_identity(str(resolved_report_parent))
            != path_identity(str(Path(junit_path).parent))
            or path_identity(str(resolved_temp_parent))
            != path_identity(str(Path(basetemp_path).parent))
        ):
            raise InputError(
                f"compiled_plan test suite {suite_id} output path identity changed"
            )
        if basetemp_state is not None and (
            stat.S_ISLNK(basetemp_state.st_mode)
            or getattr(basetemp_state, "st_reparse_tag", 0)
            or not stat.S_ISDIR(basetemp_state.st_mode)
        ):
            raise InputError(
                f"compiled_plan test suite {suite_id} basetemp_path must be absent or a non-link directory"
            )
        if report_state is not None and (
            stat.S_ISLNK(report_state.st_mode)
            or getattr(report_state, "st_reparse_tag", 0)
            or not stat.S_ISREG(report_state.st_mode)
            or report_state.st_nlink != 1
        ):
            raise InputError(
                f"compiled_plan test suite {suite_id} junit_path must be absent or a single-link non-link regular file"
            )
        try:
            _path_physical_anchor_identity(junit_path, os.name)
            _path_physical_anchor_identity(basetemp_path, os.name)
            if _paths_overlap(
                junit_path,
                left_is_directory=False,
                right=basetemp_path,
                right_is_directory=True,
                effective_os=os.name,
            ):
                raise AtomError("JUnit and base-temp outputs overlap")
            for prior_suite in suites:
                if _paths_are_equivalent(
                    junit_path,
                    prior_suite["junit_path"],
                    os.name,
                ):
                    raise AtomError("JUnit output aliases another suite's report")
                if (
                    _paths_overlap(
                        junit_path,
                        left_is_directory=False,
                        right=prior_suite["basetemp_path"],
                        right_is_directory=True,
                        effective_os=os.name,
                    )
                    or _paths_overlap(
                        basetemp_path,
                        left_is_directory=True,
                        right=prior_suite["junit_path"],
                        right_is_directory=False,
                        effective_os=os.name,
                    )
                    or _paths_overlap(
                        basetemp_path,
                        left_is_directory=True,
                        right=prior_suite["basetemp_path"],
                        right_is_directory=True,
                        effective_os=os.name,
                    )
                ):
                    raise AtomError("pytest output aliases another suite's output")
        except AtomError as exc:
            raise InputError(
                f"compiled_plan test suite {suite_id} output identity is unsafe"
            ) from exc
        report_file_identity = (
            (report_state.st_dev, report_state.st_ino)
            if report_state is not None
            else None
        )
        atom = atoms[atom_id]
        operation = atom.get("operation", {})
        argv = operation.get("argv", [])
        environment = operation.get("env", {})
        internal = operation.get("internal_parallelism", {})
        timeout_seconds = operation.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        provenance = atom.get("provenance", {})
        if (
            operation.get("kind") != "test"
            or provenance.get("adapter") != "test_suite"
            or not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
            or not _is_pytest_runner(argv)
        ):
            raise InputError(
                f"compiled_plan test suite {suite_id} is not bound to an exact pytest test atom"
            )
        if (
            not isinstance(selection_contract, dict)
            or set(selection_contract)
            != {
                "schema",
                "runner_argv",
                "runner_attestation",
                "arguments",
                "cwd",
                "env",
                "source_snapshots",
                "explicit_source_snapshots",
                "baseline_source_closure_declared",
                "baseline_source_coverage",
                "config_path",
                "config_rootdir",
                "collection_roots",
                "config_addopts",
                "environment_addopts",
                "uses_bundled_empty_config",
                "config_selection_kind",
                "config_addopts_policy",
            }
            or selection_contract.get("schema") != "atomlane/pytest-selection/v1"
            or selection_contract.get("config_addopts_policy")
            != "preserved_validated"
            or raw.get("config_addopts_policy") != "preserved_validated"
            or not isinstance(selection_fingerprint, str)
            or selection_fingerprint != _pytest_selection_fingerprint(selection_contract)
        ):
            raise InputError(
                f"compiled_plan test suite {suite_id} has an invalid selection fingerprint"
            )
        runner_argv = selection_contract.get("runner_argv")
        runner_attestation = selection_contract.get("runner_attestation")
        arguments = selection_contract.get("arguments")
        selection_snapshots = selection_contract.get("source_snapshots")
        explicit_snapshots = selection_contract.get("explicit_source_snapshots")
        config_path = selection_contract.get("config_path")
        config_rootdir = selection_contract.get("config_rootdir")
        declared_collection_roots = selection_contract.get("collection_roots")
        config_addopts = selection_contract.get("config_addopts")
        environment_addopts = selection_contract.get("environment_addopts")
        uses_empty_config = selection_contract.get("uses_bundled_empty_config")
        config_selection_kind = selection_contract.get("config_selection_kind")
        if (
            not isinstance(runner_argv, list)
            or not runner_argv
            or not all(isinstance(item, str) for item in runner_argv)
            or not os.path.isabs(runner_argv[0])
            or not _is_exact_pytest_runner_prefix(runner_argv)
            or not isinstance(runner_attestation, dict)
            or runner_attestation.get("schema")
            != "atomlane/pytest-runner-attestation/v1"
            or not isinstance(arguments, list)
            or not all(isinstance(item, str) for item in arguments)
            or not isinstance(config_path, str)
            or not os.path.isabs(config_path)
            or not isinstance(config_rootdir, str)
            or not os.path.isabs(config_rootdir)
            or not isinstance(declared_collection_roots, list)
            or not all(
                isinstance(item, str) and os.path.isabs(item)
                for item in declared_collection_roots
            )
            or not isinstance(config_addopts, list)
            or not all(isinstance(item, str) for item in config_addopts)
            or not isinstance(environment_addopts, list)
            or not all(isinstance(item, str) for item in environment_addopts)
            or not isinstance(uses_empty_config, bool)
            or config_selection_kind
            not in {"pytest_config", "fallback_pyproject", "bundled_empty"}
            or uses_empty_config is not (config_selection_kind == "bundled_empty")
            or selection_contract.get("cwd") != operation.get("cwd")
            or selection_contract.get("env") != environment
            or any(
                not isinstance(environment.get(key), str)
                for key in (
                    "PYTEST_ADDOPTS",
                    "PYTEST_PLUGINS",
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
                    "PYTEST_DEBUG",
                    "PYTHONHOME",
                    "PYTHONPATH",
                    "PYTHONOPTIMIZE",
                )
            )
            or environment.get("PYTEST_DEBUG") != ""
            or environment.get("PYTHONHOME") != ""
            or environment.get("PYTHONPATH") != ""
            or environment.get("PYTHONOPTIMIZE") != ""
            or not isinstance(selection_snapshots, list)
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("path"), str)
                or item not in plan.get("source_snapshots", [])
                for item in selection_snapshots
            )
            or not isinstance(explicit_snapshots, list)
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("path"), str)
                or item not in selection_snapshots
                for item in explicit_snapshots
            )
            or selection_snapshots
            != sorted(selection_snapshots, key=lambda item: item["path"])
            or explicit_snapshots
            != sorted(explicit_snapshots, key=lambda item: item["path"])
            or isinstance(explicit_snapshot_count, bool)
            or not isinstance(explicit_snapshot_count, int)
            or explicit_snapshot_count != len(explicit_snapshots)
            or not isinstance(baseline_source_closure_declared, bool)
            or not isinstance(baseline_source_coverage, bool)
            or selection_contract.get("baseline_source_closure_declared")
            is not baseline_source_closure_declared
            or selection_contract.get("baseline_source_coverage")
            is not baseline_source_coverage
        ):
            raise InputError(
                f"compiled_plan test suite {suite_id} selection contract is inconsistent"
            )
        try:
            runtime_runner_attestation = _pytest_runner_attestation(
                Path(runner_argv[0]), f"compiled_plan test suite {suite_id}"
            )
        except AtomError as exc:
            raise InputError(
                f"compiled_plan test suite {suite_id} runner cannot be revalidated"
            ) from exc
        if runtime_runner_attestation != runner_attestation:
            raise InputError(
                f"compiled_plan test suite {suite_id} runner changed after compilation"
            )
        try:
            operation_cwd = Path(operation["cwd"])
            resolved_operation_cwd = operation_cwd.resolve(strict=True)
            resolved_operation_cwd.relative_to(project_root)
            if str(resolved_operation_cwd) != str(operation_cwd):
                raise ValueError("pytest cwd identity changed")
            lexical_config_path = Path(config_path)
            resolved_config_path = lexical_config_path.resolve(strict=True)
            if str(resolved_config_path) != str(lexical_config_path):
                raise ValueError("pytest config identity changed")
            if uses_empty_config:
                if resolved_config_path != PYTEST_EMPTY_CONFIG.resolve(strict=True):
                    raise ValueError("bundled pytest config identity changed")
            else:
                resolved_config_path.relative_to(project_root)
            lexical_config_rootdir = Path(config_rootdir)
            resolved_config_rootdir = lexical_config_rootdir.resolve(strict=True)
            resolved_config_rootdir.relative_to(project_root)
            if str(resolved_config_rootdir) != str(lexical_config_rootdir):
                raise ValueError("pytest config root identity changed")
            if (
                config_selection_kind != "bundled_empty"
                and resolved_config_rootdir != resolved_config_path.parent
            ):
                raise ValueError("pytest config root does not match its config file")
            config_state = Path(config_path).lstat()
            config_identity = str(Path(config_path).resolve(strict=False))
            snapshotted_paths: set[str] = set()
            protected_file_identities: set[tuple[int, int]] = set()
            for snapshot in selection_snapshots:
                snapshot_path = Path(snapshot["path"])
                if not snapshot_path.is_absolute():
                    snapshot_path = project_root / snapshot_path
                lexical_snapshot = Path(os.path.abspath(snapshot_path))
                resolved_snapshot = snapshot_path.resolve(strict=True)
                source_state = snapshot_path.lstat()
                if (
                    lexical_snapshot != resolved_snapshot
                    or stat.S_ISLNK(source_state.st_mode)
                    or getattr(source_state, "st_reparse_tag", 0)
                    or not stat.S_ISREG(source_state.st_mode)
                ):
                    raise ValueError("pytest source snapshot identity changed")
                snapshotted_paths.add(str(resolved_snapshot))
                protected_file_identities.add(
                    (source_state.st_dev, source_state.st_ino)
                )
            explicit_paths = []
            for snapshot in explicit_snapshots:
                snapshot_path = Path(snapshot["path"])
                if not snapshot_path.is_absolute():
                    snapshot_path = project_root / snapshot_path
                lexical_snapshot = Path(os.path.abspath(snapshot_path))
                resolved_snapshot = snapshot_path.resolve(strict=True)
                if lexical_snapshot != resolved_snapshot:
                    raise ValueError("pytest explicit snapshot identity changed")
                explicit_paths.append(str(resolved_snapshot))
            if isinstance(argv[0], str) and os.path.isabs(argv[0]):
                runner_state = Path(argv[0]).resolve(strict=True).lstat()
                if stat.S_ISREG(runner_state.st_mode):
                    protected_file_identities.add(
                        (runner_state.st_dev, runner_state.st_ino)
                    )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise InputError(
                f"compiled_plan test suite {suite_id} config snapshot is invalid"
            ) from exc
        if (
            stat.S_ISLNK(config_state.st_mode)
            or getattr(config_state, "st_reparse_tag", 0)
            or not stat.S_ISREG(config_state.st_mode)
            or config_identity not in snapshotted_paths
            or not Path(config_rootdir).is_dir()
            or len(snapshotted_paths) != len(selection_snapshots)
            or len(set(explicit_paths)) != len(explicit_paths)
            or config_identity in explicit_paths
            or report_identity in {path_identity(path) for path in snapshotted_paths}
            or (
                report_file_identity is not None
                and report_file_identity in protected_file_identities
            )
            or (
                isinstance(argv[0], str)
                and os.path.isabs(argv[0])
                and report_identity
                in {
                    path_identity(argv[0]),
                    path_identity(str(Path(argv[0]).resolve(strict=False))),
                }
            )
        ):
            raise InputError(
                f"compiled_plan test suite {suite_id} config is not a snapshotted regular file"
            )
        try:
            runtime_environment_addopts = shlex.split(
                environment["PYTEST_ADDOPTS"], posix=True
            )
            config_is_valid, runtime_config_addopts = _pytest_config_addopts(
                Path(config_path)
            )
            runtime_config_paths = _validate_pytest_config_paths(
                Path(config_path), project_root, f"compiled_plan test suite {suite_id}"
            )
            _reject_pytest_module_shadowing(
                Path(operation["cwd"]),
                runtime_config_paths["pythonpath"],
                f"compiled_plan test suite {suite_id}",
            )
            runtime_selectors = _validate_pytest_selector_boundaries(
                project_root,
                Path(operation["cwd"]),
                [*runtime_config_addopts, *runtime_environment_addopts, *arguments],
                f"compiled_plan test suite {suite_id}",
            )
            runtime_collection_roots = (
                runtime_selectors
                or runtime_config_paths["testpaths"]
                or [Path(operation["cwd"])]
            )
            if [
                str(path.resolve(strict=True)) for path in runtime_collection_roots
            ] != declared_collection_roots:
                raise AtomError("pytest collection roots changed after compilation")
            runtime_report_path = Path(junit_path).resolve(strict=False)
            for collection_root in runtime_collection_roots:
                resolved_root = collection_root.resolve(strict=True)
                if any(
                    _pytest_output_overlaps_collection(
                        output_path,
                        resolved_root,
                        os.name,
                        output_is_directory=output_is_directory,
                    )
                    for output_path, output_is_directory in (
                        (runtime_report_path, False),
                        (resolved_temp, True),
                    )
                ):
                    raise AtomError(
                        "pytest output overlaps the collection scope"
                    )
            for prior_suite in suites:
                if any(
                    _pytest_output_overlaps_collection(
                        output_path,
                        Path(prior_root),
                        os.name,
                        output_is_directory=output_is_directory,
                    )
                    for output_path, output_is_directory in (
                        (runtime_report_path, False),
                        (resolved_temp, True),
                    )
                    for prior_root in prior_suite["collection_roots"]
                ) or any(
                    _pytest_output_overlaps_collection(
                        output_path,
                        collection_root.resolve(strict=True),
                        os.name,
                        output_is_directory=output_is_directory,
                    )
                    for output_path, output_is_directory in (
                        (prior_suite["junit_path"], False),
                        (prior_suite["basetemp_path"], True),
                    )
                    for collection_root in runtime_collection_roots
                ):
                    raise AtomError(
                        "pytest output overlaps another suite's collection scope"
                    )
            runtime_source_coverage = _pytest_baseline_source_coverage(
                project_root,
                Path(operation["cwd"]),
                [*runtime_config_addopts, *runtime_environment_addopts, *arguments],
                Path(config_path),
                [Path(path) for path in explicit_paths],
                f"compiled_plan test suite {suite_id}",
            )
        except (AtomError, TypeError, ValueError) as exc:
            raise InputError(
                f"compiled_plan test suite {suite_id} pytest selection cannot be revalidated"
            ) from exc
        config_selection_is_valid = (
            config_selection_kind == "pytest_config"
            and config_is_valid
            or (
                config_selection_kind == "fallback_pyproject"
                and not config_is_valid
                and Path(config_path).name == "pyproject.toml"
                and not runtime_config_addopts
                and not runtime_config_paths["testpaths"]
                and not runtime_config_paths["pythonpath"]
            )
            or (
                config_selection_kind == "bundled_empty"
                and config_is_valid
                and not runtime_config_addopts
            )
        )
        if (
            not config_selection_is_valid
            or runtime_environment_addopts != environment_addopts
            or runtime_config_addopts != config_addopts
            or runtime_source_coverage is not baseline_source_coverage
        ):
            raise InputError(
                f"compiled_plan test suite {suite_id} addopts metadata is inconsistent"
            )
        if (
            _pytest_owned_options(config_addopts)
            or _pytest_owned_options(environment_addopts)
            or _pytest_plugin_control(config_addopts) is not None
            or _pytest_plugin_control(environment_addopts) is not None
            or _pytest_plugin_control(arguments) is not None
            or _pytest_environment_plugin_control(environment["PYTEST_PLUGINS"])
            is not None
        ):
            raise InputError(
                f"compiled_plan test suite {suite_id} materialized options conflict with AtomLane controls"
            )
        expected_controls = Counter(
            {
                "-c": 1,
                "--confcutdir": 1,
                "--basetemp": 1,
                "--junitxml": 1,
                **({"--rootdir": 1} if uses_empty_config else {}),
                **(
                    {
                        "-n": 1,
                        "--maxprocesses": 1,
                        "--max-worker-restart": 1,
                        "--dist": 1,
                    }
                    if workers > 1
                    else {}
                ),
            }
        )
        if Counter(_pytest_owned_options(argv)) != expected_controls:
            raise InputError(
                f"compiled_plan test suite {suite_id} has duplicate, missing, or conflicting pytest controls"
            )
        if (
            argv.count(f"--junitxml={junit_path}") != 1
            or argv.count(f"--basetemp={basetemp_path}") != 1
            or raw.get("effects_declared_complete") is not True
            or raw.get("collection_execution_performed") is not False
            or raw.get("worker_evidence") != "configured_not_observed"
        ):
            raise InputError(
                f"compiled_plan test suite {suite_id} control metadata is inconsistent"
            )
        expected_argv = [*runner_argv, "-p", "xdist"]
        if workers > 1:
            expected_argv.extend(
                ["-n", str(workers), "--dist", raw.get("distribution")]
            )
        expected_argv.extend(["-c", config_path])
        expected_argv.append(f"--confcutdir={project_root}")
        if uses_empty_config:
            expected_argv.append(f"--rootdir={config_rootdir}")
        expected_argv.extend(["-p", "no:cacheprovider"])
        if workers > 1:
            expected_argv.extend(
                [
                    "--maxprocesses",
                    str(workers),
                    "--max-worker-restart",
                    "0",
                ]
            )
        expected_argv.extend(
            [
                f"--basetemp={basetemp_path}",
                f"--junitxml={junit_path}",
                *arguments,
            ]
        )
        if argv != expected_argv:
            raise InputError(
                f"compiled_plan test suite {suite_id} argv differs from its selection contract"
            )
        if workers > 1:
            worker_pair = any(
                argv[position:position + 2] == ["-n", str(workers)]
                for position in range(max(0, len(argv) - 1))
            )
            distribution = raw.get("distribution")
            distribution_pair = any(
                argv[position:position + 2] == ["--dist", distribution]
                for position in range(max(0, len(argv) - 1))
            )
            maxprocess_pair = any(
                argv[position:position + 2] == ["--maxprocesses", str(workers)]
                for position in range(max(0, len(argv) - 1))
            )
            restart_pair = any(
                argv[position:position + 2] == ["--max-worker-restart", "0"]
                for position in range(max(0, len(argv) - 1))
            )
            if (
                raw.get("strategy") != "native_worker_pool"
                or raw.get("independence_declared") is not True
                or raw.get("native_dependency") != "pytest-xdist"
                or internal.get("kind") != "bounded"
                or float(internal.get("tokens") or 0) != float(workers)
                or not worker_pair
                or not distribution_pair
                or not maxprocess_pair
                or not restart_pair
            ):
                raise InputError(f"compiled_plan test suite {suite_id} worker budget is inconsistent")
        elif (
            raw.get("strategy") != "native_serial"
            or raw.get("native_dependency") != "pytest-xdist"
            or internal.get("kind") != "none"
            or internal.get("tokens") is not None
        ):
            raise InputError(f"compiled_plan test suite {suite_id} serial strategy is inconsistent")
        suites.append(
            {
                "id": suite_id,
                "atom_id": atom_id,
                "configured_workers": workers,
                "case_count_hint": case_hint,
                "junit_path": junit_path,
                "basetemp_path": basetemp_path,
                "distribution": raw.get("distribution"),
                "strategy": raw.get("strategy"),
                "independence_declared": raw.get("independence_declared") is True,
                "selection_fingerprint": selection_fingerprint,
                "explicit_snapshot_count": explicit_snapshot_count,
                "baseline_source_closure_declared": baseline_source_closure_declared,
                "baseline_source_coverage": baseline_source_coverage,
                "collection_roots": declared_collection_roots,
                "timeout_seconds": float(timeout_seconds),
                "worker_evidence": "configured_not_observed",
            }
        )
        seen_atoms.add(atom_id)
        seen_reports.add(report_identity)
        seen_temp_roots.add(temp_identity)

    configured = [suite["configured_workers"] for suite in suites]
    hints = [suite["case_count_hint"] for suite in suites]
    suite_atom_ids = {suite["atom_id"] for suite in suites}
    return {
        "framework": "pytest",
        "test_suites": suites,
        "native_worker_pool_count": sum(1 for value in configured if value > 1),
        "native_workers_configured": max(configured, default=1),
        "native_workers_configured_total": sum(configured),
        "native_workers_observed": None,
        "worker_evidence": "configured_not_observed",
        "test_cases_planned": sum(hints) if all(value is not None for value in hints) else None,
        "test_case_hints_known": sum(value for value in hints if value is not None),
        "selection_fingerprints": sorted(
            suite["selection_fingerprint"] for suite in suites
        ),
        "test_workload_exclusive": suite_atom_ids == set(atoms),
    }


def _pytest_output_lease_identity(path: str) -> str:
    resolved = os.path.abspath(os.fspath(Path(path).resolve(strict=False)))
    return _path_identity(resolved, os.name)


def _windows_known_local_app_data() -> Path:
    """Resolve LocalAppData from the current token, independent of env variables."""
    if os.name != "nt":
        raise InputError("Windows known-folder lookup requires native Windows")
    import ctypes
    import uuid

    class Guid(ctypes.Structure):
        _fields_ = [
            ("data1", ctypes.c_uint32),
            ("data2", ctypes.c_uint16),
            ("data3", ctypes.c_uint16),
            ("data4", ctypes.c_ubyte * 8),
        ]

    folder_id = Guid.from_buffer_copy(
        uuid.UUID("f1b32785-6fba-4fcf-9d55-7b8e7f157091").bytes_le
    )
    path_pointer = ctypes.c_void_p()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(Guid),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    result = shell32.SHGetKnownFolderPath(
        ctypes.byref(folder_id),
        0,
        None,
        ctypes.byref(path_pointer),
    )
    if result != 0 or not path_pointer.value:
        raise InputError(
            f"Windows LocalAppData known folder is unavailable (HRESULT 0x{result & 0xFFFFFFFF:08x})"
        )
    try:
        value = ctypes.wstring_at(path_pointer.value)
    finally:
        ole32.CoTaskMemFree(path_pointer)
    if not value:
        raise InputError("Windows LocalAppData known folder is empty")
    return Path(value)


def _pytest_output_lease_root(
    *,
    host_os: str | None = None,
    known_local_app_data: Path | None = None,
) -> Path:
    """Return a per-user lease root that is independent of TMPDIR/TEMP."""
    active_os = host_os or os.name
    if active_os == "nt":
        return (
            known_local_app_data or _windows_known_local_app_data()
        ) / "AtomLane" / "pytest-output-leases-v1"
    return (
        Path("/tmp").resolve(strict=True)
        / f"atomlane-pytest-output-leases-{os.getuid()}-v1"
    )


def _pytest_output_lease_keys(context: dict[str, Any]) -> tuple[str, ...]:
    """Derive lexical and physical locks that remain stable after output creation."""
    keys: set[str] = set()
    for suite in context.get("test_suites", []):
        for raw_path in (suite.get("junit_path"), suite.get("basetemp_path")):
            if not isinstance(raw_path, str) or not raw_path:
                continue
            path = Path(raw_path)
            keys.add(f"path:{_pytest_output_lease_identity(raw_path)}")
            try:
                parent_state = path.parent.stat()
            except OSError as exc:
                raise AtomError(
                    f"pytest output parent physical identity is unavailable: {path.parent}"
                ) from exc
            parent_inode = int(getattr(parent_state, "st_ino", 0) or 0)
            if not stat.S_ISDIR(parent_state.st_mode) or parent_inode <= 0:
                raise AtomError(
                    f"pytest output parent physical identity is unavailable: {path.parent}"
                )
            basename = _path_identity(path.name, os.name)
            keys.add(
                "parent-entry:"
                f"{int(parent_state.st_dev)}:{parent_inode}:"
                f"{hashlib.sha256(basename.encode('utf-8')).hexdigest()}"
            )
            try:
                target_state = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise AtomError(
                    f"pytest output physical identity is unavailable: {path}"
                ) from exc
            target_inode = int(getattr(target_state, "st_ino", 0) or 0)
            if (
                stat.S_ISLNK(target_state.st_mode)
                or getattr(target_state, "st_reparse_tag", 0)
                or target_inode <= 0
            ):
                raise AtomError(
                    f"pytest output physical identity is unavailable: {path}"
                )
            keys.add(f"target:{int(target_state.st_dev)}:{target_inode}")
    return tuple(sorted(keys))


class _PytestOutputLeases:
    def __init__(
        self,
        stack: contextlib.ExitStack,
        lease_keys: tuple[str, ...],
    ) -> None:
        self._stack = stack
        self.lease_keys = lease_keys

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._stack.close()


def _acquire_pytest_output_leases(
    context: dict[str, Any] | None,
) -> _PytestOutputLeases:
    """Fail fast when another execution owns a pytest report/temp path."""
    stack = contextlib.ExitStack()
    if context is None:
        return _PytestOutputLeases(stack, ())
    lease_root = _pytest_output_lease_root()
    try:
        lease_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_state = lease_root.lstat()
    except OSError as exc:
        raise InputError("pytest output lease directory is unavailable") from exc
    if (
        stat.S_ISLNK(root_state.st_mode)
        or getattr(root_state, "st_reparse_tag", 0)
        or not stat.S_ISDIR(root_state.st_mode)
        or (
            os.name == "posix"
            and (
                root_state.st_uid != os.getuid()
                or stat.S_IMODE(root_state.st_mode) & 0o077
            )
        )
    ):
        raise InputError("pytest output lease directory is not private")
    try:
        lease_keys = _pytest_output_lease_keys(context)
        for lease_key in lease_keys:
            digest = hashlib.sha256(lease_key.encode("utf-8")).hexdigest()
            stack.enter_context(
                exclusive_file_lock(
                    lease_root / f"{digest}.lock",
                    timeout_seconds=0.0,
                )
            )
        if _pytest_output_lease_keys(context) != lease_keys:
            raise AtomError("pytest output identity changed while leases were acquired")
    except TimeoutError as exc:
        stack.close()
        raise InputError(
            "pytest report or temporary path is already in use; "
            "recompile the plan or choose a unique junit_path"
        ) from exc
    except OSError as exc:
        stack.close()
        raise InputError("pytest output lease cannot be acquired") from exc
    except AtomError as exc:
        stack.close()
        raise InputError("pytest output identity cannot be leased safely") from exc
    return _PytestOutputLeases(stack, lease_keys)


def _junit_report_snapshot(
    path: Path,
) -> tuple[dict[str, Any], bytes | None, str | None]:
    """Open one stable regular file without following links and read at most MAX+1."""
    try:
        path_state = path.lstat()
    except FileNotFoundError:
        return {"exists": False}, None, "JUnit report does not exist"
    except OSError as exc:
        return (
            {"exists": False, "error": type(exc).__name__},
            None,
            f"JUnit report cannot be inspected: {type(exc).__name__}",
        )
    if (
        stat.S_ISLNK(path_state.st_mode)
        or getattr(path_state, "st_reparse_tag", 0)
        or not stat.S_ISREG(path_state.st_mode)
        or path_state.st_ino == 0
        or path_state.st_nlink != 1
    ):
        return (
            {"exists": True, "regular_file": False},
            None,
            "JUnit report must be a single-link non-link regular file",
        )

    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        return (
            {"exists": False, "error": type(exc).__name__},
            None,
            f"JUnit report cannot be opened safely: {type(exc).__name__}",
        )
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or identity != (path_state.st_dev, path_state.st_ino)
            or opened.st_nlink != 1
        ):
            return (
                {"exists": True, "regular_file": False},
                None,
                "JUnit report changed identity while being opened",
            )
        base_state: dict[str, Any] = {
            "exists": True,
            "regular_file": True,
            "device": opened.st_dev,
            "inode": opened.st_ino,
            "size": opened.st_size,
            "mtime_ns": opened.st_mtime_ns,
            "ctime_ns": opened.st_ctime_ns,
            "nlink": opened.st_nlink,
        }
        if opened.st_size > MAX_JUNIT_REPORT_BYTES:
            return (
                base_state,
                None,
                f"JUnit report exceeds {MAX_JUNIT_REPORT_BYTES} bytes",
            )
        chunks: list[bytes] = []
        remaining = MAX_JUNIT_REPORT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        settled = os.fstat(descriptor)
        settled_identity = (settled.st_dev, settled.st_ino)
        settled_path = path.lstat()
        if (
            settled_identity != identity
            or (settled_path.st_dev, settled_path.st_ino) != identity
            or stat.S_ISLNK(settled_path.st_mode)
            or getattr(settled_path, "st_reparse_tag", 0)
            or settled_path.st_nlink != 1
            or settled.st_nlink != 1
            or settled.st_size != opened.st_size
            or settled.st_mtime_ns != opened.st_mtime_ns
            or settled.st_ctime_ns != opened.st_ctime_ns
        ):
            return (
                base_state,
                None,
                "JUnit report changed while it was being read",
            )
        payload = b"".join(chunks)
        if len(payload) > MAX_JUNIT_REPORT_BYTES:
            return (
                {**base_state, "size": len(payload)},
                None,
                f"JUnit report exceeds {MAX_JUNIT_REPORT_BYTES} bytes",
            )
        state = {**base_state, "sha256": hashlib.sha256(payload).hexdigest()}
        return state, payload, None
    except OSError as exc:
        return (
            {"exists": True, "regular_file": False, "error": type(exc).__name__},
            None,
            f"JUnit report cannot be read safely: {type(exc).__name__}",
        )
    finally:
        os.close(descriptor)


def _junit_report_state(path: Path) -> dict[str, Any]:
    state, _, _ = _junit_report_snapshot(path)
    return state


def _parse_junit_payload(payload: bytes) -> dict[str, Any]:
    try:
        payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {
            "status": "unavailable",
            "reason": "JUnit report must be UTF-8",
        }
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        return {
            "status": "unavailable",
            "reason": "JUnit report contains a prohibited DTD or entity declaration",
        }

    def local_name(tag: Any) -> str:
        return str(tag).rsplit("}", 1)[-1].casefold()

    def declared_counts(element: ET.Element) -> dict[str, int] | None:
        values: dict[str, int] = {}
        for name in ("tests", "failures", "errors", "skipped"):
            raw_value = element.get(name)
            if (
                raw_value is None
                or len(raw_value) > 10
                or re.fullmatch(r"[0-9]+", raw_value) is None
            ):
                return None
            parsed_value = int(raw_value)
            if parsed_value > MAX_JUNIT_TEST_CASES:
                return None
            values[name] = parsed_value
        return values

    tests = failures = errors = skipped = timed = 0
    duration_sum = 0.0
    case_identifiers: list[list[str]] = []
    root_name: str | None = None
    root_declared: dict[str, int] | None = None
    root_counts_present = False
    suite_stack: list[dict[str, Any]] = []
    active_testcases = 0
    leaf_count = 0
    leaf_counts_valid = True
    leaf_totals = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
    depth = 0
    element_count = 0
    try:
        for event, element in ET.iterparse(
            io.BytesIO(payload), events=("start", "end")
        ):
            element_name = local_name(element.tag)
            if event == "start":
                depth += 1
                element_count += 1
                if element_count > MAX_JUNIT_XML_ELEMENTS:
                    return {
                        "status": "unavailable",
                        "reason": (
                            "JUnit report exceeds the bounded XML element limit of "
                            f"{MAX_JUNIT_XML_ELEMENTS}"
                        ),
                    }
                if depth > 256:
                    return {
                        "status": "unavailable",
                        "reason": "JUnit report exceeds the XML nesting limit",
                    }
                if root_name is None:
                    root_name = element_name
                    if root_name not in {"testsuite", "testsuites"}:
                        return {
                            "status": "unavailable",
                            "reason": "JUnit report root must be testsuite or testsuites",
                        }
                    if element_name == "testsuites":
                        root_declared = declared_counts(element)
                    root_counts_present = any(
                        element.get(name) is not None
                        for name in ("tests", "failures", "errors", "skipped")
                    )
                if element_name == "testcase":
                    if active_testcases:
                        return {
                            "status": "unavailable",
                            "reason": "JUnit testcase elements must not be nested",
                        }
                    active_testcases += 1
                if element_name == "testsuite":
                    if suite_stack:
                        suite_stack[-1]["has_child_suite"] = True
                    suite_stack.append(
                        {
                            "declared": declared_counts(element),
                            "has_child_suite": False,
                            "is_root": depth == 1,
                        }
                    )
                continue

            if element_name == "testcase":
                if not suite_stack:
                    return {
                        "status": "unavailable",
                        "reason": "JUnit testcase must be contained by a testsuite",
                    }
                tests += 1
                if tests > MAX_JUNIT_TEST_CASES:
                    return {
                        "status": "unavailable",
                        "reason": (
                            "JUnit report exceeds the bounded testcase limit of "
                            f"{MAX_JUNIT_TEST_CASES}"
                        ),
                    }
                case_identifiers.append(
                    [
                        element.get("classname", ""),
                        element.get("name", ""),
                        element.get("file", ""),
                        element.get("line", ""),
                    ]
                )
                status_tags = {
                    local_name(child.tag)
                    for child in element
                    if local_name(child.tag) in {"failure", "error", "skipped"}
                }
                if "error" in status_tags:
                    errors += 1
                elif "failure" in status_tags:
                    failures += 1
                elif "skipped" in status_tags:
                    skipped += 1
                raw_time = element.get("time")
                if raw_time is not None:
                    try:
                        value = float(raw_time)
                    except (TypeError, ValueError):
                        value = -1.0
                    if math.isfinite(value) and 0 <= value <= MAX_TIMEOUT_SECONDS:
                        duration_sum += value
                        timed += 1
                active_testcases -= 1
            elif element_name == "testsuite":
                suite = suite_stack.pop()
                if suite["is_root"]:
                    root_declared = suite["declared"]
                if not suite["has_child_suite"]:
                    leaf_count += 1
                    counts = suite["declared"]
                    if counts is None:
                        leaf_counts_valid = False
                    else:
                        for name, value in counts.items():
                            leaf_totals[name] += value
                            if leaf_totals[name] > MAX_JUNIT_TEST_CASES:
                                leaf_counts_valid = False
            element.clear()
            depth -= 1
    except (ET.ParseError, ValueError) as exc:
        return {"status": "unavailable", "reason": f"JUnit report is malformed: {exc}"}

    passed = max(0, tests - failures - errors - skipped)
    declared = root_declared if root_name in {"testsuite", "testsuites"} else None
    if (
        declared is None
        and not root_counts_present
        and leaf_count
        and leaf_counts_valid
    ):
        declared = leaf_totals
    counter_consistent = declared == {
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }
    case_set_sha256 = "sha256:" + hashlib.sha256(
        json.dumps(
            sorted(case_identifiers),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "status": "parsed",
        "tests": tests,
        "passed": passed,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "declared_counts": declared,
        "counter_consistent": counter_consistent,
        "case_set_sha256": case_set_sha256,
        "timed_testcases": timed,
        "timing_complete": tests > 0 and timed == tests,
        "testcase_time_sum_seconds": round(duration_sum, 6),
    }


def _parse_junit_report(path: Path) -> dict[str, Any]:
    _, payload, reason = _junit_report_snapshot(path)
    if payload is None:
        return {"status": "unavailable", "reason": reason or "JUnit report unavailable"}
    return _parse_junit_payload(payload)


def _collect_test_report(
    context: dict[str, Any] | None,
    before_states: dict[str, dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if context is None:
        return None
    test_workload_exclusive = context.get("test_workload_exclusive") is True
    results_by_id = {
        result.get("id"): result
        for result in results
        if isinstance(result, dict) and isinstance(result.get("id"), str)
    }
    reports: list[dict[str, Any]] = []
    for suite in context["test_suites"]:
        path = Path(suite["junit_path"])
        after, payload, read_error = _junit_report_snapshot(path)
        fresh = after.get("exists") is True and after != before_states.get(str(path))
        if fresh and payload is not None:
            parsed = _parse_junit_payload(payload)
        else:
            parsed = {
                "status": "unavailable",
                "reason": (
                    read_error
                    if fresh and read_error
                    else "JUnit report was not created or changed by this execution"
                ),
            }
        result = results_by_id.get(suite["atom_id"], {})
        raw_wall = result.get("duration_seconds")
        suite_wall = (
            float(raw_wall)
            if isinstance(raw_wall, (int, float))
            and not isinstance(raw_wall, bool)
            and math.isfinite(raw_wall)
            and raw_wall >= 0
            else None
        )
        timing_capacity = (
            suite_wall * int(suite["configured_workers"])
            if suite_wall is not None
            else None
        )
        timing_tolerance = (
            max(0.05, timing_capacity * 0.05)
            if timing_capacity is not None
            else 0.0
        )
        report_timing = float(parsed.get("testcase_time_sum_seconds", 0.0))
        timing_plausible = bool(
            parsed.get("status") == "parsed"
            and timing_capacity is not None
            and report_timing <= timing_capacity + timing_tolerance
        )
        reports.append(
            {
                "suite_id": suite["id"],
                "path": str(path),
                "fresh": fresh,
                "attributed_to_suite": test_workload_exclusive,
                **parsed,
                "suite_wall_time_seconds": (
                    round(suite_wall, 6) if suite_wall is not None else None
                ),
                "timing_capacity_seconds": (
                    round(timing_capacity, 6)
                    if timing_capacity is not None
                    else None
                ),
                "timing_plausible": timing_plausible,
            }
        )
    parsed_reports = [item for item in reports if item["status"] == "parsed"]
    complete = len(parsed_reports) == len(reports)
    tests = sum(int(item.get("tests", 0)) for item in parsed_reports)
    timed = sum(int(item.get("timed_testcases", 0)) for item in parsed_reports)
    duration_sum = sum(
        float(item.get("testcase_time_sum_seconds", 0.0)) for item in parsed_reports
    )
    timing_plausible = complete and all(
        item.get("timing_plausible") is True for item in parsed_reports
    )
    timing_complete = complete and tests > 0 and timed == tests and timing_plausible
    failures = sum(int(item.get("failures", 0)) for item in parsed_reports)
    errors = sum(int(item.get("errors", 0)) for item in parsed_reports)
    passed = sum(int(item.get("passed", 0)) for item in parsed_reports)
    case_set_sha256 = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                sorted(item["case_set_sha256"] for item in parsed_reports),
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if complete
        else None
    )
    execution_evidence_eligible = bool(
        test_workload_exclusive
        and complete
        and all(item.get("fresh") is True for item in reports)
        and all(item.get("counter_consistent") is True for item in parsed_reports)
        and tests > 0
        and passed > 0
        and failures == 0
        and errors == 0
    )
    return {
        "schema": "atomlane/test-report-summary/v1",
        "status": "parsed" if complete else "partial" if parsed_reports else "unavailable",
        "fresh": len(reports) > 0 and all(item["fresh"] for item in reports),
        "expected_report_count": len(reports),
        "fresh_report_count": sum(1 for item in reports if item["fresh"]),
        "parsed_report_count": len(parsed_reports),
        "tests": tests,
        "passed": passed,
        "failed": failures,
        "failures": failures,
        "errors": errors,
        "skipped": sum(int(item.get("skipped", 0)) for item in parsed_reports),
        "timed_testcases": timed,
        "timing_complete": timing_complete,
        "timing_plausible": timing_plausible,
        "execution_evidence_eligible": execution_evidence_eligible,
        "report_attribution": (
            "exclusive_test_plan"
            if test_workload_exclusive
            else "unavailable_mixed_workload"
        ),
        "evidence_ineligible_reason": (
            None
            if test_workload_exclusive
            else "JUnit reports are not attributed after non-test atoms in a mixed plan"
        ),
        "case_set_sha256": case_set_sha256,
        "testcase_time_sum_seconds": round(duration_sum, 6),
        "savings_comparison_eligible": (
            execution_evidence_eligible and timing_complete and duration_sum > 0
        ),
        "savings_comparison_kind": (
            "fresh_junit_testcase_durations"
            if execution_evidence_eligible and timing_complete and duration_sum > 0
            else None
        ),
        "reports": reports,
    }


def _normalize_serial_baseline_evidence(
    raw: Any,
    context: dict[str, Any],
    current_plan_hash: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "attestation_id",
        "source_plan_hash",
        "elapsed_seconds",
        "suite_fingerprints",
        "test_count",
        "passed_count",
        "skipped_count",
        "case_set_sha256",
        "status",
    }:
        raise InputError("serial_baseline_evidence has an invalid shape")
    if raw.get("schema") != "atomlane/serial-test-baseline/v1":
        raise InputError("serial_baseline_evidence has an unsupported schema")
    attestation_id = raw.get("attestation_id")
    if (
        not isinstance(attestation_id, str)
        or re.fullmatch(r"baseline_[0-9a-f]{64}", attestation_id) is None
    ):
        raise InputError("serial_baseline_evidence has an invalid attestation_id")
    source_plan_hash = raw.get("source_plan_hash")
    if (
        not isinstance(source_plan_hash, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", source_plan_hash) is None
        or source_plan_hash == current_plan_hash
    ):
        raise InputError("serial_baseline_evidence has an invalid source_plan_hash")
    elapsed_raw = raw.get("elapsed_seconds")
    if (
        isinstance(elapsed_raw, bool)
        or not isinstance(elapsed_raw, (int, float))
        or not math.isfinite(elapsed_raw)
        or not 0 < elapsed_raw <= MAX_TIMEOUT_SECONDS * MAX_TASKS
    ):
        raise InputError(
            "serial_baseline_evidence.elapsed_seconds must be finite and positive"
        )
    fingerprints = raw.get("suite_fingerprints")
    expected_fingerprints = context.get("selection_fingerprints")
    if (
        context.get("test_workload_exclusive") is not True
        or any(
            suite.get("explicit_snapshot_count", 0) < 1
            or suite.get("baseline_source_closure_declared") is not True
            or suite.get("baseline_source_coverage") is not True
            for suite in context.get("test_suites", [])
        )
        or not isinstance(fingerprints, list)
        or not all(isinstance(item, str) for item in fingerprints)
        or sorted(fingerprints) != expected_fingerprints
    ):
        raise InputError(
            "serial_baseline_evidence does not match the compiled pytest selection"
        )
    test_count = raw.get("test_count")
    passed_count = raw.get("passed_count")
    skipped_count = raw.get("skipped_count")
    case_set_sha256 = raw.get("case_set_sha256")
    if (
        isinstance(test_count, bool)
        or not isinstance(test_count, int)
        or not 1 <= test_count <= 10_000_000 * max(1, len(fingerprints))
        or isinstance(passed_count, bool)
        or not isinstance(passed_count, int)
        or passed_count != test_count
        or isinstance(skipped_count, bool)
        or not isinstance(skipped_count, int)
        or skipped_count != 0
        or not isinstance(case_set_sha256, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", case_set_sha256) is None
        or raw.get("status") != "passed"
    ):
        raise InputError("serial_baseline_evidence must describe a non-empty passed run")
    normalized = {
        "schema": "atomlane/serial-test-baseline/v1",
        "attestation_id": attestation_id,
        "source_plan_hash": source_plan_hash,
        "elapsed_seconds": float(elapsed_raw),
        "suite_fingerprints": sorted(fingerprints),
        "test_count": test_count,
        "passed_count": passed_count,
        "skipped_count": 0,
        "case_set_sha256": case_set_sha256,
        "status": "passed",
    }
    if _SERIAL_BASELINE_ATTESTATIONS.get(attestation_id) != normalized:
        raise InputError(
            "serial_baseline_evidence was not issued by this AtomLane server session"
        )
    return normalized


def _serial_baseline_evidence_from_run(
    context: dict[str, Any] | None,
    test_report: dict[str, Any] | None,
    results: list[dict[str, Any]],
    elapsed: float,
    plan_hash: str,
    peak_concurrency: int,
) -> dict[str, Any] | None:
    if (
        context is None
        or context.get("native_worker_pool_count") != 0
        or not isinstance(test_report, dict)
        or test_report.get("execution_evidence_eligible") is not True
        or any(result.get("status") != "succeeded" for result in results)
        or peak_concurrency != 1
        or any(
            suite.get("configured_workers") != 1
            for suite in context.get("test_suites", [])
        )
        or any(
            suite.get("explicit_snapshot_count", 0) < 1
            or suite.get("baseline_source_closure_declared") is not True
            or suite.get("baseline_source_coverage") is not True
            for suite in context.get("test_suites", [])
        )
        or test_report.get("skipped") != 0
        or test_report.get("passed") != test_report.get("tests")
        or not isinstance(test_report.get("case_set_sha256"), str)
        or len(results) != len(context.get("test_suites", []))
        or {result.get("id") for result in results}
        != {
            suite.get("atom_id")
            for suite in context.get("test_suites", [])
        }
    ):
        return None
    evidence = {
        "schema": "atomlane/serial-test-baseline/v1",
        "attestation_id": f"baseline_{secrets.token_hex(32)}",
        "source_plan_hash": plan_hash,
        "elapsed_seconds": round(elapsed, 6),
        "suite_fingerprints": context["selection_fingerprints"],
        "test_count": int(test_report["tests"]),
        "passed_count": int(test_report["passed"]),
        "skipped_count": 0,
        "case_set_sha256": test_report["case_set_sha256"],
        "status": "passed",
    }
    while len(_SERIAL_BASELINE_ATTESTATIONS) >= MAX_SERIAL_BASELINE_ATTESTATIONS:
        del _SERIAL_BASELINE_ATTESTATIONS[next(iter(_SERIAL_BASELINE_ATTESTATIONS))]
    _SERIAL_BASELINE_ATTESTATIONS[evidence["attestation_id"]] = {
        **evidence,
        "suite_fingerprints": list(evidence["suite_fingerprints"]),
    }
    return evidence


def _execution_indicator(
    results: list[dict[str, Any]],
    elapsed: float,
    peak_concurrency: int,
    serial_baseline_seconds: float | None,
    execution_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = execution_context or {}
    native_workers = context.get("native_workers_configured")
    native_pool_count = context.get("native_worker_pool_count", 0)
    native_pool = (
        isinstance(native_workers, int)
        and not isinstance(native_workers, bool)
        and native_workers > 1
        and isinstance(native_pool_count, int)
        and native_pool_count > 0
    )
    native_workload_exclusive = context.get("test_workload_exclusive") is True
    test_report = context.get("test_report")
    baseline_evidence = context.get("serial_baseline_evidence")
    completed = [result for result in results if result.get("status") != "skipped"]
    succeeded = [result for result in completed if result.get("status") == "succeeded"]
    savings_eligible = bool(succeeded) and len(succeeded) == len(completed)
    ineligible_reason = None
    if not succeeded:
        ineligible_reason = "no task completed successfully"
    elif not savings_eligible:
        ineligible_reason = "one or more tasks failed or timed out"

    task_runtime = sum(float(result.get("duration_seconds", 0.0)) for result in succeeded)
    baseline_compatible: bool | None = None
    baseline_compatibility_reason: str | None = None
    if native_pool and isinstance(baseline_evidence, dict):
        baseline_compatible = bool(
            isinstance(test_report, dict)
            and test_report.get("execution_evidence_eligible") is True
            and baseline_evidence.get("test_count") == test_report.get("tests")
            and baseline_evidence.get("passed_count") == test_report.get("passed")
            and baseline_evidence.get("skipped_count") == test_report.get("skipped") == 0
            and baseline_evidence.get("case_set_sha256")
            == test_report.get("case_set_sha256")
        )
        if not baseline_compatible:
            baseline_compatibility_reason = (
                "serial baseline test identities or outcomes do not match the fresh parallel JUnit report"
            )

    if savings_eligible and native_pool and not native_workload_exclusive:
        savings_eligible = False
        comparison_seconds = 0.0
        speedup_kind = "unavailable_mixed_native_workload"
        qualifier_zh = "待独立测试计划"
        ineligible_reason = (
            "native pytest savings require a plan containing only the attested test suites"
        )
    elif (
        savings_eligible
        and native_pool
        and not isinstance(test_report, dict)
        or (
            savings_eligible
            and native_pool
            and isinstance(test_report, dict)
            and test_report.get("execution_evidence_eligible") is not True
        )
    ):
        savings_eligible = False
        comparison_seconds = 0.0
        speedup_kind = "unavailable_native_execution_evidence"
        qualifier_zh = "待证据"
        ineligible_reason = (
            "native worker pool needs a fresh, non-empty, counter-consistent passing JUnit report"
        )
    elif savings_eligible and native_pool and baseline_compatible:
        comparison_seconds = serial_baseline_seconds
        speedup_kind = "measured_serial_baseline"
        qualifier_zh = "实测"
    elif (
        savings_eligible
        and native_pool
        and isinstance(test_report, dict)
        and test_report.get("savings_comparison_eligible") is True
    ):
        comparison_seconds = float(test_report["testcase_time_sum_seconds"])
        speedup_kind = "estimated_sum_of_testcase_durations"
        qualifier_zh = "JUnit 估算"
    elif savings_eligible and native_pool:
        savings_eligible = False
        comparison_seconds = 0.0
        speedup_kind = "unavailable_native_serial_comparison"
        qualifier_zh = "待基线"
        ineligible_reason = (
            baseline_compatibility_reason
            or "native worker pool needs a compatible serial baseline or fresh complete JUnit testcase timings"
        )
    elif savings_eligible and serial_baseline_seconds is not None:
        comparison_seconds = serial_baseline_seconds
        speedup_kind = "measured_serial_baseline"
        qualifier_zh = "实测"
    elif savings_eligible:
        comparison_seconds = task_runtime
        speedup_kind = "estimated_sum_of_task_durations"
        qualifier_zh = "估算"
    else:
        comparison_seconds = 0.0
        speedup_kind = "ineligible_failed_or_empty_run"
        qualifier_zh = "未计入"

    speedup = comparison_seconds / elapsed if elapsed > 0 and comparison_seconds > 0 else 0.0
    raw_delta = comparison_seconds - elapsed if savings_eligible else 0.0
    time_saved = max(0.0, raw_delta)
    overhead = max(0.0, -raw_delta) if savings_eligible else 0.0
    evidence_kind = (
        "measured"
        if savings_eligible and speedup_kind == "measured_serial_baseline"
        else "estimated"
        if savings_eligible
        else None
    )
    ledger_credit_eligible = evidence_kind == "measured"
    ledger_credit_recorded = False
    credited_time_saved = 0.0
    ledger_available = True
    ledger_error: str | None = None
    try:
        cumulative = (
            _record_time_saved(time_saved, evidence_kind=evidence_kind)
            if evidence_kind is not None
            else _read_time_saved()
        )
        if ledger_credit_eligible:
            ledger_credit_recorded = True
            credited_time_saved = time_saved
    except Exception as exc:  # noqa: BLE001 - metrics cannot discard task results.
        cumulative = {
            "cumulative_saved_seconds": None,
            "run_count": None,
            "cumulative_measured_saved_seconds": None,
            "measured_run_count": None,
            "cumulative_estimated_saved_seconds": None,
            "estimated_run_count": None,
            "cumulative_legacy_unclassified_saved_seconds": None,
            "legacy_unclassified_run_count": None,
        }
        ledger_available = False
        ledger_error = type(exc).__name__
    cumulative_text = (
        f"{cumulative['cumulative_saved_seconds']:.2f}s"
        if isinstance(cumulative.get("cumulative_saved_seconds"), (int, float))
        else "不可用"
    )
    is_parallel = peak_concurrency > 1 or native_pool
    if native_pool:
        icon = "⚙️"
        label_zh = "原生并行"
        concurrency_text = f"配置 {native_workers} workers｜外层峰值 {peak_concurrency} 路"
        if savings_eligible:
            savings_text = (
                f"本次实测节约 {time_saved:.2f}s（已入账）"
                if ledger_credit_recorded
                else f"本次实测节约 {time_saved:.2f}s（入账失败）"
                if ledger_credit_eligible
                else f"本次估算节约 {time_saved:.2f}s（未入账）"
            )
            display = (
                f"{icon} {label_zh}｜{concurrency_text}｜{qualifier_zh} {speedup:.2f}×"
                f"｜{savings_text}｜累计已入账 {cumulative_text}"
            )
        else:
            display = (
                f"{icon} {label_zh}｜{concurrency_text}｜本次节约待可信证据"
                f"｜累计已入账 {cumulative_text}"
            )
    else:
        icon = "⚡" if is_parallel else "→"
        label_zh = "并行" if is_parallel else "串行"
        if savings_eligible:
            savings_text = (
                f"本次实测节约 {time_saved:.2f}s（已入账）"
                if ledger_credit_recorded
                else f"本次实测节约 {time_saved:.2f}s（入账失败）"
                if ledger_credit_eligible
                else f"本次估算节约 {time_saved:.2f}s（未入账）"
            )
            display = (
                f"{icon} {label_zh}｜峰值 {peak_concurrency} 路｜{qualifier_zh} {speedup:.2f}×"
                f"｜{savings_text}｜累计已入账 {cumulative_text}"
            )
        else:
            display = (
                f"{icon} {label_zh}｜峰值 {peak_concurrency} 路｜本次节约未计入"
                f"｜累计已入账 {cumulative_text}"
            )
    efficiency = (
        None
        if native_pool
        else speedup / peak_concurrency if peak_concurrency else 0.0
    )
    indicator = {
        "display": display,
        "parallel": is_parallel,
        "mode": "parallel" if is_parallel else "serial",
        "peak_concurrency": peak_concurrency,
        "speedup_multiplier": round(speedup, 4),
        "speedup_kind": speedup_kind,
        "comparison_seconds": round(comparison_seconds, 6),
        "wall_time_seconds": round(elapsed, 6),
        "time_saved_seconds": round(time_saved, 6),
        "measured_time_saved_seconds": (
            round(time_saved, 6) if evidence_kind == "measured" else None
        ),
        "estimated_time_saved_seconds": (
            round(time_saved, 6) if evidence_kind == "estimated" else None
        ),
        "ledger_credit_eligible": ledger_credit_eligible,
        "ledger_credit_recorded": ledger_credit_recorded,
        "credited_time_saved_seconds": round(credited_time_saved, 6),
        "overhead_seconds": round(overhead, 6),
        "savings_eligible": savings_eligible,
        "savings_ineligible_reason": ineligible_reason,
        "cumulative_saved_seconds": cumulative["cumulative_saved_seconds"],
        "cumulative_run_count": cumulative["run_count"],
        "cumulative_measured_saved_seconds": cumulative[
            "cumulative_measured_saved_seconds"
        ],
        "cumulative_measured_run_count": cumulative["measured_run_count"],
        "cumulative_estimated_saved_seconds": cumulative[
            "cumulative_estimated_saved_seconds"
        ],
        "cumulative_estimated_run_count": cumulative["estimated_run_count"],
        "cumulative_legacy_unclassified_saved_seconds": cumulative[
            "cumulative_legacy_unclassified_saved_seconds"
        ],
        "cumulative_legacy_unclassified_run_count": cumulative[
            "legacy_unclassified_run_count"
        ],
        "cumulative_ledger_available": ledger_available,
        "cumulative_ledger_error": ledger_error,
        "parallel_efficiency": round(efficiency, 4) if efficiency is not None else None,
        "parallel_efficiency_kind": (
            "unavailable_without_observed_native_workers"
            if native_pool
            else "outer_peak_concurrency"
        ),
        "serial_baseline_compatible": baseline_compatible,
        "serial_baseline_compatibility_reason": baseline_compatibility_reason,
        "explanation": (
            f"Savings were not credited because {ineligible_reason}."
            if not savings_eligible
            else "Speedup uses a serial baseline attested by this AtomLane server session."
            if speedup_kind == "measured_serial_baseline" and native_pool
            else "Speedup uses the supplied serial baseline."
            if speedup_kind == "measured_serial_baseline"
            else "Estimated speedup equals fresh summed JUnit testcase durations divided by wall time; no test is rerun and the estimate is not credited to the primary cumulative ledger."
            if speedup_kind == "estimated_sum_of_testcase_durations"
            else "Estimated speedup equals summed non-skipped task runtimes divided by wall time; no task is rerun and the estimate is not credited to the primary cumulative ledger."
        ),
    }
    if native_pool:
        indicator.update(
            {
                "parallelism_kind": "native_worker_pool",
                "outer_peak_concurrency": peak_concurrency,
                "native_worker_pool_count": native_pool_count,
                "native_workers_configured": native_workers,
                "native_workers_configured_total": context.get(
                    "native_workers_configured_total", native_workers
                ),
                "native_workers_observed": None,
                "worker_evidence": "configured_not_observed",
                "test_cases_planned": context.get("test_cases_planned"),
                "efficiency_denominator_kind": "unavailable_without_observed_native_workers",
                "savings_pending_native_report": not savings_eligible,
            }
        )
    return indicator


def _summary(
    results: list[dict[str, Any]],
    plan: dict[str, Any],
    elapsed: float,
    peak_concurrency: int,
    serial_baseline_seconds: float | None,
    execution_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    indicator = _execution_indicator(
        results,
        elapsed,
        peak_concurrency,
        serial_baseline_seconds,
        execution_context,
    )
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
        "measured_time_saved_seconds": indicator["measured_time_saved_seconds"],
        "estimated_time_saved_seconds": indicator["estimated_time_saved_seconds"],
        "ledger_credit_eligible": indicator["ledger_credit_eligible"],
        "ledger_credit_recorded": indicator["ledger_credit_recorded"],
        "credited_time_saved_seconds": indicator["credited_time_saved_seconds"],
        "overhead_seconds": indicator["overhead_seconds"],
        "savings_eligible": indicator["savings_eligible"],
        "savings_ineligible_reason": indicator["savings_ineligible_reason"],
        "cumulative_saved_seconds": indicator["cumulative_saved_seconds"],
        "cumulative_run_count": indicator["cumulative_run_count"],
        "cumulative_measured_saved_seconds": indicator[
            "cumulative_measured_saved_seconds"
        ],
        "cumulative_measured_run_count": indicator[
            "cumulative_measured_run_count"
        ],
        "cumulative_estimated_saved_seconds": indicator[
            "cumulative_estimated_saved_seconds"
        ],
        "cumulative_estimated_run_count": indicator[
            "cumulative_estimated_run_count"
        ],
        "cumulative_legacy_unclassified_saved_seconds": indicator[
            "cumulative_legacy_unclassified_saved_seconds"
        ],
        "cumulative_legacy_unclassified_run_count": indicator[
            "cumulative_legacy_unclassified_run_count"
        ],
        "cumulative_ledger_available": indicator["cumulative_ledger_available"],
        "cumulative_ledger_error": indicator["cumulative_ledger_error"],
    }
    if indicator.get("parallelism_kind") == "native_worker_pool":
        summary.update(
            {
                "parallelism_kind": "native_worker_pool",
                "outer_peak_concurrency": peak_concurrency,
                "native_worker_pool_count": indicator["native_worker_pool_count"],
                "native_workers_configured": indicator["native_workers_configured"],
                "native_workers_observed": None,
                "worker_evidence": "configured_not_observed",
                "test_cases_planned": indicator.get("test_cases_planned"),
                "test_report": (execution_context or {}).get("test_report"),
            }
        )
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
    platform_contract = plan.get("platform_contract")
    current_contract = _current_platform_contract()
    identity_fields = {
        "adapter_protocol",
        "os_family",
        "environment_kind",
        "architecture",
        "path_flavor",
        "argv_transport",
        "process_tree_control",
        "conpty_stdin_supported",
        "supported_resource_controls",
        "resource_control_constraints",
    }
    if not isinstance(platform_contract, dict) or any(
        platform_contract.get(field) != current_contract.get(field)
        for field in identity_fields
    ):
        raise InputError(
            "compiled_plan platform contract does not match this execution realm; recompile locally"
        )
    required_terminal_modes = platform_contract.get("required_terminal_modes", [])
    if (
        not isinstance(required_terminal_modes, list)
        or not set(required_terminal_modes).issubset(
            set(current_contract["supported_terminal_modes"])
        )
    ):
        raise InputError("compiled_plan requires a terminal mode unavailable on this host")
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
    # Freeze both the plan and execution-only options before deriving lease
    # keys. Embedded callers may otherwise mutate the input between the
    # pre-lock and post-lock validations.
    try:
        frozen_arguments = json.loads(json.dumps(arguments, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise InputError(f"atomic_exec arguments must be JSON-serializable: {exc}") from exc
    initial_plan = _verify_compiled_plan(frozen_arguments)
    if not initial_plan["atoms"]:
        raise InputError("compiled_plan has no executable atoms")
    initial_context = _test_suite_execution_context(initial_plan)
    if initial_context is None:
        return await _run_atomic_with_output_leases(
            frozen_arguments,
            progress_callback,
            verified_plan=initial_plan,
        )
    leases = _acquire_pytest_output_leases(initial_context)
    try:
        return await _run_atomic_with_output_leases(
            frozen_arguments,
            progress_callback,
            expected_output_lease_keys=leases.lease_keys,
        )
    finally:
        leases.close()


async def _run_atomic_with_output_leases(
    arguments: dict[str, Any],
    progress_callback: Any | None = None,
    *,
    verified_plan: dict[str, Any] | None = None,
    expected_output_lease_keys: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Revalidate and execute while the public wrapper holds output leases."""
    plan = verified_plan or _verify_compiled_plan(arguments)
    atoms = plan["atoms"]
    if not atoms:
        raise InputError("compiled_plan has no executable atoms")
    output_limit = arguments.get("max_output_bytes_per_stream", DEFAULT_OUTPUT_BYTES)
    if isinstance(output_limit, bool) or not isinstance(output_limit, int) or not 256 <= output_limit <= MAX_OUTPUT_BYTES:
        raise InputError(f"max_output_bytes_per_stream must be between 256 and {MAX_OUTPUT_BYTES}")
    baseline = arguments.get("serial_baseline_seconds")
    if baseline is not None:
        baseline = _bounded_number(baseline, "serial_baseline_seconds", 1.0, MAX_TIMEOUT_SECONDS * MAX_TASKS)
    test_execution_context = _test_suite_execution_context(plan)
    if expected_output_lease_keys is not None:
        try:
            settled_output_lease_keys = _pytest_output_lease_keys(
                test_execution_context or {}
            )
        except AtomError as exc:
            raise InputError(
                "pytest output identity changed after lease acquisition"
            ) from exc
        if settled_output_lease_keys != expected_output_lease_keys:
            raise InputError(
                "pytest output identity changed after lease acquisition"
            )
    baseline_evidence_raw = arguments.get("serial_baseline_evidence")
    native_pool_count = (test_execution_context or {}).get(
        "native_worker_pool_count", 0
    )
    if native_pool_count:
        if baseline is not None:
            raise InputError(
                "serial_baseline_seconds is not accepted for a native pytest pool; "
                "run a worker_count=1 plan and pass its serial_baseline_evidence"
            )
        if baseline_evidence_raw is not None:
            baseline_evidence = _normalize_serial_baseline_evidence(
                baseline_evidence_raw,
                test_execution_context,
                plan["plan_hash"],
            )
            baseline = baseline_evidence["elapsed_seconds"]
            test_execution_context = {
                **test_execution_context,
                "serial_baseline_evidence": baseline_evidence,
            }
    elif baseline_evidence_raw is not None:
        raise InputError(
            "serial_baseline_evidence may only be consumed by a native pytest worker pool"
        )
    junit_before_states = {
        suite["junit_path"]: _junit_report_state(Path(suite["junit_path"]))
        for suite in (test_execution_context or {}).get("test_suites", [])
    }

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
    reporter = ProgressReporter(
        len(atoms),
        progress_callback,
        started,
        _atomic_progress_context(plan),
    )
    compiled_resource_plan = plan.get("resource_plan") if isinstance(plan.get("resource_plan"), dict) else {}
    responsiveness = compiled_resource_plan.get("responsiveness", "interactive")
    if responsiveness not in {"interactive", "balanced", "throughput"}:
        responsiveness = "interactive"
    runtime_profile = compiled_resource_plan.get("profile", "mixed")
    if runtime_profile not in {"cpu", "io", "mixed", "accelerator"}:
        runtime_profile = "mixed"
    compiled_worker_limit = max(1, min(MAX_CONCURRENCY, int(capacities.get("worker_slot", 1.0))))
    resource_plan = concurrency_plan(
        runtime_profile,
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
                    "stdin": operation.get("stdin"),
                    "timeout_seconds": operation.get(
                        "timeout_seconds", DEFAULT_TIMEOUT_SECONDS
                    ),
                    "depends_on": [],
                    "side_effect": atom["side_effect"],
                    "terminal_mode": operation.get("terminal_mode", "pipes"),
                    "resources": operation.get(
                        "resource_limits",
                        {
                            "cpu_rate_percent": None,
                            "memory_limit_mb": None,
                            "max_processes": None,
                        },
                    ),
                    "execution_realm": execution_environment()["boundary"],
                    "broker_boundary": operation.get("broker_boundary"),
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
    test_report = _collect_test_report(
        test_execution_context, junit_before_states, results
    )
    if test_execution_context is not None:
        test_execution_context = {**test_execution_context, "test_report": test_report}
    serial_baseline_evidence = _serial_baseline_evidence_from_run(
        test_execution_context,
        test_report,
        results,
        elapsed,
        plan["plan_hash"],
        peak_concurrency,
    )
    summary_plan = dict(resource_plan)
    summary_plan["chosen_concurrency"] = int(capacities.get("worker_slot", 1.0))
    summary, indicator = _summary(
        results,
        summary_plan,
        elapsed,
        peak_concurrency,
        baseline,
        test_execution_context,
    )
    response = {
        "indicator": indicator,
        "summary": summary,
        "plan_hash": plan["plan_hash"],
        "resource_plan": summary_plan,
        "launch_order": launch_order,
        "event_journal": journal,
        "results": results,
    }
    if test_report is not None:
        response["test_report"] = test_report
    if serial_baseline_evidence is not None:
        response["serial_baseline_evidence"] = serial_baseline_evidence
    return response


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
        "terminal_mode": {
            "type": "string",
            "enum": ["pipes", "conpty"],
            "default": "pipes",
            "description": "Use separate pipes, or output-only Windows ConPTY with a combined VT stream; explicit ConPTY stdin is unsupported.",
        },
        "resources": {
            "type": "object",
            "properties": {
                "cpu_rate_percent": {"type": "number", "minimum": 0.01, "maximum": 100},
                "memory_limit_mb": {"type": "number", "minimum": 128, "maximum": 1048576},
                "max_processes": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 4096,
                    "description": "Exact Windows Job Object active-member ceiling, including AtomLane's supervisor; supported only with pipes.",
                },
            },
            "additionalProperties": False,
            "description": "Native Windows Job Object limits cover AtomLane's supervisor and inherited Windows descendants; WSL, Docker, WMI, services, scheduled tasks, and other broker-created work are outside this limit.",
        },
    },
    "allOf": [
        {
            "not": {
                "required": ["terminal_mode", "resources"],
                "properties": {
                    "terminal_mode": {"const": "conpty"},
                    "resources": {"required": ["max_processes"]},
                },
            }
        },
        {
            "not": {
                "required": ["terminal_mode", "stdin"],
                "properties": {"terminal_mode": {"const": "conpty"}},
            }
        }
    ],
    "additionalProperties": False,
}


ATOMIC_ENTRYPOINT_SCHEMA = {
    "type": "object",
    "required": ["adapter"],
    "properties": {
        "id": {"type": "string"},
        "adapter": {
            "type": "string",
            "enum": [
                "shell",
                "package_script",
                "make_target",
                "compose_services",
                "test_suite",
                "powershell_file",
            ],
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
        "framework": {"type": "string", "enum": ["pytest"]},
        "runner_argv": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 32,
        },
        "config_path": {"type": "string"},
        "worker_count": {
            "oneOf": [
                {"type": "integer", "minimum": 1, "maximum": MAX_CONCURRENCY},
                {"const": "auto"},
            ],
            "default": "auto",
        },
        "distribution": {
            "type": "string",
            "enum": ["load", "loadfile", "loadscope", "loadgroup", "worksteal"],
            "default": "worksteal",
            "description": "xdist scheduling policy. worksteal is the independent-case default; choose loadfile/loadscope/loadgroup only when fixture or shared-resource affinity requires grouping.",
        },
        "case_count_hint": {"type": "integer", "minimum": 1, "maximum": 10000000},
        "estimated_memory_mb_per_worker": {
            "type": "number",
            "minimum": 1,
            "maximum": 1048576,
        },
        "estimated_duration_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 86400,
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 86400,
            "default": 900,
        },
        "junit_path": {
            "type": "string",
            "description": "Optional JUnit output. Prefer the unique system-temp default; an explicit path must be outside every selected collection directory and cannot be a link, hardlink, config, snapshot, or runner alias.",
        },
        "snapshot_paths": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 256,
        },
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
        "script_path": {"type": "string"},
        "arguments": {"type": "array", "items": {"type": "string"}, "maxItems": 128},
        "declared_accesses": {"type": "array", "items": {"type": "object"}, "maxItems": 256},
        "declared_effects": {"type": "array", "items": {"type": "object"}, "maxItems": 128},
        "effects_declared_complete": {"type": "boolean", "default": False},
        "independence_declared": {
            "type": "boolean",
            "default": False,
            "description": "Explicit assertion that selected tests may execute concurrently under their fixture, ordering, and shared-resource semantics.",
        },
        "baseline_source_closure_declared": {
            "type": "boolean",
            "default": False,
            "description": "Caller assertion that snapshot_paths covers every source, helper, conftest, and plugin that can affect the selected tests. Required, with AtomLane's bounded static scope checks, before a serial run can issue baseline evidence.",
        },
        "side_effect": {"type": "boolean", "default": True},
        "profile": {"type": "string", "enum": ["cpu", "io", "mixed", "accelerator"]},
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
    "profile": {
        "type": "string",
        "enum": ["cpu", "io", "mixed", "accelerator"],
    },
    "estimated_memory_mb_per_task": {"type": "number", "exclusiveMinimum": 0},
    "max_concurrency": {"type": "integer", "minimum": 1, "maximum": MAX_CONCURRENCY},
    "reserve_cores": {"type": "integer", "minimum": 0, "maximum": MAX_CONCURRENCY},
    "discover_project_commands": {"type": "boolean", "default": False},
}


TEST_SUITE_PLAN_PROPERTIES = {
    "project_path": {"type": "string", "description": "Absolute project root."},
    "task_summary": {"type": "string", "maxLength": 8000},
    "id": ATOMIC_ENTRYPOINT_SCHEMA["properties"]["id"],
    "framework": {
        "type": "string",
        "enum": ["pytest"],
        "default": "pytest",
    },
    "runner_argv": {
        **ATOMIC_ENTRYPOINT_SCHEMA["properties"]["runner_argv"],
        "description": "Exact Python module runner prefix, such as [python, -m, pytest]. Direct pytest/py.test console scripts are rejected. Put selectors and pytest options in arguments.",
    },
    "arguments": ATOMIC_ENTRYPOINT_SCHEMA["properties"]["arguments"],
    "cwd": ATOMIC_ENTRYPOINT_SCHEMA["properties"]["cwd"],
    "config_path": {
        **ATOMIC_ENTRYPOINT_SCHEMA["properties"]["config_path"],
        "description": "Optional project-local pytest config to bind explicitly; otherwise AtomLane searches from cwd to project_path and uses a bundled empty config when none exists.",
    },
    "worker_count": ATOMIC_ENTRYPOINT_SCHEMA["properties"]["worker_count"],
    "distribution": ATOMIC_ENTRYPOINT_SCHEMA["properties"]["distribution"],
    "case_count_hint": {
        **ATOMIC_ENTRYPOINT_SCHEMA["properties"]["case_count_hint"],
        "description": "Caller-supplied upper bound for auto worker sizing and display; it is not proof of independence. AtomLane does not execute hidden pytest collection while planning.",
    },
    "estimated_memory_mb_per_worker": ATOMIC_ENTRYPOINT_SCHEMA["properties"][
        "estimated_memory_mb_per_worker"
    ],
    "estimated_duration_seconds": ATOMIC_ENTRYPOINT_SCHEMA["properties"][
        "estimated_duration_seconds"
    ],
    "timeout_seconds": ATOMIC_ENTRYPOINT_SCHEMA["properties"]["timeout_seconds"],
    "junit_path": ATOMIC_ENTRYPOINT_SCHEMA["properties"]["junit_path"],
    "snapshot_paths": {
        **ATOMIC_ENTRYPOINT_SCHEMA["properties"]["snapshot_paths"],
        "description": "Project-local test, conftest, and plugin source files to hash and revalidate before execution. At least one non-config file per suite is required before a serial run can issue baseline evidence.",
    },
    "env": ATOMIC_ENTRYPOINT_SCHEMA["properties"]["env"],
    "declared_accesses": ATOMIC_ENTRYPOINT_SCHEMA["properties"]["declared_accesses"],
    "declared_effects": ATOMIC_ENTRYPOINT_SCHEMA["properties"]["declared_effects"],
    "effects_declared_complete": {
        **ATOMIC_ENTRYPOINT_SCHEMA["properties"]["effects_declared_complete"],
        "description": "Required true for execution. This is an explicit assertion that test filesystem, database, port, device, account, and other effects are fully modeled.",
    },
    "independence_declared": {
        **ATOMIC_ENTRYPOINT_SCHEMA["properties"]["independence_declared"],
        "description": "Required true when configured workers exceed one. It is a caller assertion, not a conclusion inferred from a case-count hint.",
    },
    "baseline_source_closure_declared": {
        **ATOMIC_ENTRYPOINT_SCHEMA["properties"][
            "baseline_source_closure_declared"
        ],
        "description": "Required true, together with complete snapshot_paths and bounded static scope checks, before serial-baseline evidence can be issued. Dynamic imports and plugin closure remain a caller assertion.",
    },
    "responsiveness": ATOMIC_PLAN_PROPERTIES["responsiveness"],
    "max_concurrency": ATOMIC_PLAN_PROPERTIES["max_concurrency"],
    "reserve_cores": ATOMIC_PLAN_PROPERTIES["reserve_cores"],
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
        "description": "Optional explicit reserve. When omitted, it is derived from this host and responsiveness mode.",
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
        "name": "python_parallel_advisor",
        "description": "Statically inspect bounded project-local Python source for high-confidence ordered-map parallelization candidates. Never imports or executes target code, never modifies files, fails closed on unknown effects, and labels projected savings as modeled until a measured differential benchmark exists.",
        "inputSchema": {
            "type": "object",
            "required": ["project_path"],
            "properties": {
                "project_path": {"type": "string", "minLength": 1, "maxLength": 4096, "description": "Absolute local project directory."},
                "paths": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 128,
                    "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "description": "Optional project-local Python paths. Omit for bounded discovery.",
                },
                "hotspots": {
                    "type": "array",
                    "maxItems": 128,
                    "items": {
                        "type": "object",
                        "required": ["path", "line", "wall_seconds"],
                        "properties": {
                            "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                            "line": {"type": "integer", "minimum": 1, "maximum": 1000000000},
                            "wall_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000000000},
                            "item_count": {"type": "integer", "minimum": 1, "maximum": 1000000000},
                        },
                        "additionalProperties": False,
                    },
                    "description": "Optional caller-observed serial hotspot evidence. The advisor models a projection but does not call it measured parallel performance.",
                },
                "max_files": {"type": "integer", "minimum": 1, "maximum": 512, "default": 128},
                "max_candidates": {"type": "integer", "minimum": 1, "maximum": 128, "default": 32},
                "max_workers": {"type": "integer", "minimum": 1, "maximum": 64},
                "estimated_memory_mb_per_worker": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000000000},
                "minimum_hotspot_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000000000, "default": 10.0},
                "execution_context": {
                    "type": "string",
                    "enum": ["standalone", "atomlane_worker", "native_parallel", "unknown"],
                    "default": "standalone",
                    "description": "Used to prevent unbudgeted nested pools.",
                },
                "responsiveness": {
                    "type": "string",
                    "enum": ["interactive", "balanced", "throughput"],
                    "default": "interactive",
                },
                "include_rewrite_previews": {"type": "boolean", "default": True},
                "target_platform": {
                    "type": "string",
                    "enum": ["auto", "windows", "darwin", "linux"],
                    "default": "auto",
                    "description": "Bind spawn and worker-limit advice to a deployment platform; auto uses this host.",
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
        "name": "test_suite_plan",
        "description": "Compile one pytest suite through an exact Python -m pytest runner into the standard immutable AtomLane plan, normally using one resource-bounded pytest-xdist worker pool rather than one process per case. Planning never imports tests, runs collection, or installs xdist. Complete effects and an explicit case-independence declaration are required for parallel atomic_exec.",
        "inputSchema": {
            "type": "object",
            "required": ["project_path", "runner_argv"],
            "properties": TEST_SUITE_PLAN_PROPERTIES,
            "additionalProperties": False,
        },
    },
    {
        "name": "atomic_task_plan",
        "description": "Compile shell, package-script, Make, Compose, pytest test-suite, and explicit Atom IR entrypoints into one typed, source-snapshotted, resource-constrained plan. Unsupported control/effect semantics fail closed. The returned object is immutable and must be passed unchanged to atomic_exec with its plan_hash.",
        "inputSchema": {
            "type": "object",
            "required": ["project_path"],
            "properties": ATOMIC_PLAN_PROPERTIES,
            "additionalProperties": False,
        },
    },
    {
        "name": "atomic_exec",
        "description": "Execute the exact immutable CompiledPlan returned by atomic_task_plan or test_suite_plan. Revalidates the canonical plan hash and source snapshots, enforces typed dependencies, artifact conflicts, and multidimensional capacities, and refuses opaque or incomplete-effect atoms.",
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
                "serial_baseline_evidence": {
                    "type": "object",
                    "required": [
                        "schema",
                        "attestation_id",
                        "source_plan_hash",
                        "elapsed_seconds",
                        "suite_fingerprints",
                        "test_count",
                        "passed_count",
                        "skipped_count",
                        "case_set_sha256",
                        "status",
                    ],
                    "properties": {
                        "schema": {"const": "atomlane/serial-test-baseline/v1"},
                        "attestation_id": {
                            "type": "string",
                            "pattern": "^baseline_[0-9a-f]{64}$",
                        },
                        "source_plan_hash": {
                            "type": "string",
                            "pattern": "^sha256:[0-9a-f]{64}$",
                        },
                        "elapsed_seconds": {"type": "number", "exclusiveMinimum": 0},
                        "suite_fingerprints": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 64,
                            "items": {
                                "type": "string",
                                "pattern": "^sha256:[0-9a-f]{64}$",
                            },
                        },
                        "test_count": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 640000000,
                        },
                        "passed_count": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 640000000,
                        },
                        "skipped_count": {"const": 0},
                        "case_set_sha256": {
                            "type": "string",
                            "pattern": "^sha256:[0-9a-f]{64}$",
                        },
                        "status": {"const": "passed"},
                    },
                    "additionalProperties": False,
                    "description": "Session-bound evidence returned by a successful worker_count=1 test_suite run for the same selection.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "host_resource_plan",
        "description": "Inspect the current macOS, native Windows, WSL, or Linux execution boundary and recommend concurrency from host CPU, memory, pressure, and responsiveness headroom. Windows facts use native APIs and are never inferred from WSL or Docker VM capacity.",
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
        "name": "mac_resource_plan",
        "description": "Compatibility alias for host_resource_plan. On macOS it also reports Apple-specific P/E-core, thermal, power, and GPU facts.",
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
    elif name == "python_parallel_advisor":
        result = python_parallel_advisor(arguments)
    elif name == "task_parallel_scan":
        result = task_parallel_scan(arguments)
    elif name == "test_suite_plan":
        result = test_suite_plan(arguments)
    elif name == "atomic_task_plan":
        result = atomic_task_plan(arguments)
    elif name == "atomic_exec":
        result = await run_atomic(arguments, progress_callback)
    elif name in {"host_resource_plan", "mac_resource_plan"}:
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
        if not snapshot.get("savings_eligible_so_far", True):
            savings_text = "本次节约不计入（已有失败或超时）"
        elif snapshot.get("savings_pending_native_report"):
            savings_text = "节约待串行基线/JUnit"
        else:
            savings_text = (
                f"当前预计节约 {snapshot['estimated_saved_so_far_seconds']:.1f}s"
            )
        native_parts: list[str] = []
        configured_workers = snapshot.get("native_workers_configured")
        if isinstance(configured_workers, int) and not isinstance(configured_workers, bool):
            native_parts.append(f"原生 workers {configured_workers}（配置）")
        test_cases_planned = snapshot.get("test_cases_planned")
        if isinstance(test_cases_planned, int) and not isinstance(test_cases_planned, bool):
            native_parts.append(f"计划用例 {test_cases_planned}（提示）")
        native_text = "".join(f"{part}｜" for part in native_parts)
        message = (
            f"已运行 {snapshot['elapsed_seconds']:.1f}s｜"
            f"{native_text}"
            f"运行中 {snapshot['running_tasks']}｜"
            f"就绪 {snapshot.get('ready_tasks', 0)}｜"
            f"已完成 {snapshot['completed_tasks']}/{snapshot['task_count']}｜"
            f"失败 {snapshot['failed_tasks']}｜"
            f"{savings_text}"
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


def response_for(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        return None
    try:
        if method == "initialize":
            params = message.get("params", {})
            if not isinstance(params, dict):
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "Invalid params"},
                }
            result = {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
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
                    "For a substantial pytest suite with independent cases, call test_suite_plan: it delegates collection, fixtures, "
                    "case scheduling, and worker lifecycle to one bounded pytest-xdist pool while AtomLane owns the CPU/memory budget, "
                    "live visibility, immutable contract, and evidence accounting. It never runs hidden collection or installs xdist. "
                    "For complex, unfamiliar, multi-stage, or trace-informed project optimization, call scenario_plan "
                    "to select from preset goals and guardrails before constructing work units. "
                    "For an explicitly requested optimization of a concrete long-running Python entrypoint, call "
                    "python_parallel_advisor first. It performs bounded static analysis only: it never imports, executes, "
                    "or edits target code, and its rewrite previews remain conditional until semantic differential tests "
                    "and a measured benchmark pass. "
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
            params = message.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("uri"), str):
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "Invalid params"},
                }
            uri = params["uri"]
            if not _is_indicator_resource_uri(uri):
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32002, "message": "Resource not found"},
                }
            result = {
                "contents": [
                    {
                        "uri": uri,
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
            if not isinstance(params, dict):
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "Invalid params"},
                }
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
        if method != "tools/call":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "Invalid params"},
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
        }
    except Exception as exc:  # noqa: BLE001 - JSON-RPC boundary must return structured errors.
        if method != "tools/call":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "Internal error"},
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            },
        }


def main() -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="strict", newline="\n")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(
            encoding="utf-8", errors="strict", newline="\n", line_buffering=True
        )
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
