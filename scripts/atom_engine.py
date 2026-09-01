#!/usr/bin/env python3
"""Typed atomic-work compiler and deterministic resource-aware planner.

This module is deliberately dependency-free and static-only.  It parses bounded
project metadata into an intermediate representation (IR); it never imports or
executes project code.  The IR is conservative: unsupported syntax becomes an
opaque atom instead of being guessed into unsafe parallel work.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import ntpath
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

from platform_adapter import brokered_execution_boundary, windows_process_limit_blocker
from windows_job_runner import RunnerError, validate_windows_executable_contract

IR_VERSION = "2.0"
MAX_ATOMS = 512
MAX_EDGES = 4096
MAX_COMMAND_CHARS = 32_768
MAX_SOURCE_BYTES = 2_000_000
MAX_RECURSION = 64
ATOM_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
RESOURCE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*:")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
WINDOWS_DRIVE_RELATIVE_RE = re.compile(r"^[A-Za-z]:(?![\\/])")
WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CON",
    "CONIN$",
    "CONOUT$",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
    *(f"COM{digit}" for digit in "¹²³"),
    *(f"LPT{digit}" for digit in "¹²³"),
}

ACCESS_MODES = {
    "read",
    "snapshot",
    "create",
    "append",
    "overwrite",
    "delete",
    "transaction",
}
DEPENDENCY_KINDS = {
    "hard",
    "success",
    "failure",
    "order",
    "data",
    "stream",
    "after_ready",
    "after_healthy",
    "after_completion",
    "finally",
}
PROFILES = {"cpu", "io", "mixed", "accelerator"}
OPERATION_KINDS = {
    "command",
    "shell",
    "package_script",
    "make_recipe",
    "compose_service",
    "test",
    "build",
    "transform",
    "read",
    "network",
    "mutation",
    "opaque",
}
COMPLETION_KINDS = {
    "process_exit",
    "ready",
    "healthy",
    "successful_service_exit",
    "lease",
}
ASSURANCE_PARSE = {"exact", "conservative", "opaque", "invalid"}
ASSURANCE_CONTROL = {"exact", "partial", "unknown"}
ASSURANCE_EFFECTS = {"complete_declared", "complete_static", "partial", "unknown"}
ASSURANCE_CODEGEN = {"exact_argv", "native_delegate", "opaque"}


class AtomError(ValueError):
    """Raised when atomic IR input is structurally invalid."""


def _slug(value: str, fallback: str = "atom") -> str:
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value).strip("-.")
    return (text or fallback)[:150]


def _bounded_text(value: Any, name: str, maximum: int = MAX_COMMAND_CHARS) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > maximum:
        raise AtomError(f"{name} must be a NUL-free string with at most {maximum} characters")
    return value


def _bounded_number(
    value: Any,
    name: str,
    *,
    minimum: float = 0.0,
    maximum: float = 1_000_000_000.0,
    allow_zero: bool = True,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AtomError(f"{name} must be a finite number")
    if value < minimum or (not allow_zero and value == minimum) or value > maximum:
        comparator = "at least" if allow_zero else "greater than"
        raise AtomError(f"{name} must be {comparator} {minimum} and at most {maximum}")
    return float(value)


def _safe_read_text(path: Path, label: str) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AtomError(f"cannot inspect {label}: {exc}") from exc
    if size > MAX_SOURCE_BYTES:
        raise AtomError(f"{label} exceeds the {MAX_SOURCE_BYTES}-byte static parsing limit")
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise AtomError(f"cannot read {label}: {exc}") from exc


def _normalize_cwd(value: Any, project: Path, name: str) -> Path:
    text = str(project) if value is None else _bounded_text(value, name)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = project / path
    path = path.resolve(strict=False)
    if not path.is_dir():
        raise AtomError(f"{name} does not exist or is not a directory: {path}")
    return path


def _looks_like_windows_absolute_path(text: str) -> bool:
    value = text.replace("/", "\\")
    return bool(WINDOWS_ABSOLUTE_RE.match(value)) or value.startswith("\\\\")


def _canonicalize_windows_component(component: str, *, allow_ads: bool) -> str:
    if component in {".", ".."}:
        return component
    if any(ord(character) < 32 for character in component):
        raise AtomError("Windows path components cannot contain control characters")

    fields = component.split(":")
    if len(fields) > 1 and not allow_ads:
        raise AtomError("Windows alternate data streams are allowed only on the final component")
    if len(fields) > 3:
        raise AtomError(f"unsupported Windows alternate data stream syntax: {component}")

    base = fields[0].rstrip(" .")
    if not base:
        raise AtomError("Windows path components cannot consist only of spaces or dots")
    if base in {".", ".."}:
        if len(fields) > 1:
            raise AtomError("Windows alternate data streams require a filename")
        return base
    device_name = base.split(".", 1)[0].upper()
    if device_name in WINDOWS_RESERVED_NAMES:
        raise AtomError(f"Windows reserved device path is unsupported: {component}")

    if len(fields) == 1:
        return base
    stream = fields[1].rstrip(" .")
    if not stream:
        if len(fields) == 2:
            return base
        raise AtomError(f"unsupported Windows alternate data stream syntax: {component}")
    if len(fields) == 2:
        return f"{base}:{stream}"
    stream_type = fields[2].rstrip(" .")
    if stream_type.casefold() != "$data":
        raise AtomError(f"unsupported Windows alternate data stream type: {component}")
    return f"{base}:{stream}:$DATA"


def _normalize_windows_path(text: str, *, cwd: str | None = None) -> str:
    """Return one conservative Win32 spelling without touching the filesystem."""

    value = text.replace("/", "\\")
    if not value:
        raise AtomError("Windows file resources cannot be empty")
    lowered = value.casefold()
    if lowered.startswith("\\\\.\\"):
        raise AtomError("Windows device namespace paths are not supported")
    if lowered.startswith(("\\??\\", "\\\\??\\")):
        raise AtomError("Windows NT namespace paths are not supported")
    if lowered.startswith("\\\\?\\"):
        payload = value[4:]
        if payload.casefold().startswith("unc\\"):
            value = "\\\\" + payload[4:]
        elif WINDOWS_ABSOLUTE_RE.match(payload):
            value = payload
        else:
            raise AtomError("unsupported Windows extended path namespace")
    if WINDOWS_DRIVE_RELATIVE_RE.match(value):
        raise AtomError("Windows drive-relative paths are ambiguous and are not supported")

    if not _looks_like_windows_absolute_path(value):
        if cwd is None:
            raise AtomError("Windows file resources must be absolute after cwd resolution")
        canonical_cwd = _normalize_windows_path(cwd)
        value = ntpath.join(canonical_cwd, value)
        return _normalize_windows_path(value)

    if WINDOWS_ABSOLUTE_RE.match(value):
        anchor = value[:2].upper() + "\\"
        components = [component for component in value[3:].split("\\") if component]
    elif value.startswith("\\\\"):
        if value.startswith("\\\\\\"):
            raise AtomError("malformed Windows UNC path")
        unc_components = [component for component in value[2:].split("\\") if component]
        if len(unc_components) < 2:
            raise AtomError("Windows UNC paths require both a server and share")
        server = _canonicalize_windows_component(unc_components[0], allow_ads=False)
        share = _canonicalize_windows_component(unc_components[1], allow_ads=False)
        if server in {".", ".."} or share in {".", ".."}:
            raise AtomError("Windows UNC server and share names cannot be relative")
        anchor = f"\\\\{server}\\{share}"
        components = unc_components[2:]
    else:  # Defensive: the absolute-path classifier and parser must agree.
        raise AtomError("unsupported Windows absolute path")

    canonical_components = [
        _canonicalize_windows_component(
            component,
            allow_ads=index == len(components) - 1,
        )
        for index, component in enumerate(components)
    ]
    if anchor.endswith("\\"):
        combined = anchor + "\\".join(canonical_components)
    elif canonical_components:
        combined = anchor + "\\" + "\\".join(canonical_components)
    else:
        combined = anchor
    return unicodedata.normalize("NFC", ntpath.normpath(combined))


def _windows_base_path_and_stream(path: str) -> tuple[str, str]:
    directory, filename = ntpath.split(path)
    base_name, separator, stream = filename.partition(":")
    if not separator:
        return path, ""
    return ntpath.join(directory, base_name), ":" + stream


def _resolve_windows_existing_ancestor(path: str) -> str:
    """Resolve reparse aliases for the longest existing prefix of a native path."""

    base_path, stream = _windows_base_path_and_stream(path)
    probe = Path(base_path)
    missing_components: list[str] = []
    while True:
        try:
            probe.lstat()
        except FileNotFoundError:
            parent = probe.parent
            if parent == probe:
                raise AtomError(
                    f"Windows file resource has no accessible existing ancestor: {path}"
                ) from None
            missing_components.append(probe.name)
            probe = parent
            continue
        except OSError as exc:
            raise AtomError(
                f"cannot inspect Windows file resource ancestor {probe}: {exc}"
            ) from exc
        try:
            resolved_ancestor = probe.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AtomError(
                f"cannot resolve Windows file resource ancestor {probe}: {exc}"
            ) from exc
        if missing_components:
            try:
                ancestor_is_directory = resolved_ancestor.is_dir()
            except OSError as exc:
                raise AtomError(
                    f"cannot inspect resolved Windows file resource ancestor "
                    f"{resolved_ancestor}: {exc}"
                ) from exc
            if not ancestor_is_directory:
                raise AtomError(
                    f"future Windows file resource descends from a non-directory: "
                    f"{resolved_ancestor}"
                )
        resolved = resolved_ancestor.joinpath(*reversed(missing_components))
        return _normalize_windows_path(str(resolved) + stream)


def _normalize_host_path(raw_path: str, cwd: Path) -> str:
    if os.name == "nt":
        expanded = os.path.expanduser(raw_path)
        lexical = _normalize_windows_path(expanded, cwd=str(cwd))
        return _resolve_windows_existing_ancestor(lexical)
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = cwd / path
    try:
        return str(path.resolve(strict=False))
    except (OSError, RuntimeError) as exc:
        raise AtomError(f"cannot resolve file resource {path}: {exc}") from exc


def _normalize_resource(value: Any, cwd: Path) -> str:
    text = _bounded_text(value, "resource", 4096).strip()
    if not text:
        raise AtomError("resource cannot be empty")
    explicit_file = text.startswith("file:")
    raw_path = text.removeprefix("file:") if explicit_file else text
    if WINDOWS_DRIVE_RELATIVE_RE.match(raw_path):
        raise AtomError("Windows drive-relative resource paths are ambiguous")
    if _looks_like_windows_absolute_path(raw_path):
        lexical = _normalize_windows_path(raw_path)
        if os.name == "nt":
            lexical = _resolve_windows_existing_ancestor(lexical)
        return "file:" + lexical
    if explicit_file:
        return "file:" + _normalize_host_path(raw_path, cwd)
    if os.name == "nt" and ":" in text:
        prefix = text.split(":", 1)[0]
        if "." in prefix or "\\" in prefix or "/" in prefix:
            raise AtomError(
                "ambiguous relative Windows path or alternate data stream; "
                "prefix file resources with 'file:'"
            )
    if RESOURCE_RE.match(text):
        return text
    return "file:" + _normalize_host_path(text, cwd)


def _resource_overlap(first: str, second: str) -> bool:
    if first.startswith("file:") and second.startswith("file:"):
        one = first.removeprefix("file:")
        two = second.removeprefix("file:")
        one_is_windows = _looks_like_windows_absolute_path(one)
        two_is_windows = _looks_like_windows_absolute_path(two)
        if one_is_windows or two_is_windows:
            if not (one_is_windows and two_is_windows):
                return False
            try:
                one = _normalize_windows_path(one)
                two = _normalize_windows_path(two)
            except AtomError:
                return True
            if os.name == "nt":
                one_base, _ = _windows_base_path_and_stream(one)
                two_base, _ = _windows_base_path_and_stream(two)
                try:
                    if os.path.samefile(one_base, two_base):
                        return True
                except FileNotFoundError:
                    pass
                except OSError:
                    return True
            one_key = unicodedata.normalize("NFC", ntpath.normpath(one)).casefold()
            two_key = unicodedata.normalize("NFC", ntpath.normpath(two)).casefold()
            one_boundary = one_key.rstrip("\\")
            two_boundary = two_key.rstrip("\\")
            return (
                one_key == two_key
                or one_key.startswith((two_boundary + "\\", two_boundary + ":"))
                or two_key.startswith((one_boundary + "\\", one_boundary + ":"))
            )
        try:
            if os.path.samefile(one, two):
                return True
        except FileNotFoundError:
            pass
        except OSError:
            return True
        # macOS volumes commonly casefold and normalize Unicode names. A false
        # conflict on a case-sensitive volume is safer than a missed alias.
        one_key = unicodedata.normalize("NFC", os.path.normpath(one)).casefold()
        two_key = unicodedata.normalize("NFC", os.path.normpath(two)).casefold()
        one_boundary = one_key.rstrip(os.sep)
        two_boundary = two_key.rstrip(os.sep)
        return (
            one_key == two_key
            or one_key.startswith(two_boundary + os.sep)
            or two_key.startswith(one_boundary + os.sep)
        )
    return first == second


def _accesses_compatible(first: dict[str, Any], second: dict[str, Any], atoms: tuple[dict[str, Any], dict[str, Any]]) -> bool:
    if not _resource_overlap(first["resource"], second["resource"]):
        return True
    first_mode = first["mode"]
    second_mode = second["mode"]
    if first_mode in {"read", "snapshot"} and second_mode in {"read", "snapshot"}:
        return True
    if first_mode == second_mode == "append":
        return bool(atoms[0]["semantics"]["commutative"] and atoms[1]["semantics"]["commutative"])
    return False


def atom_conflicts(first: dict[str, Any], second: dict[str, Any]) -> list[str]:
    """Return deterministic semantic conflicts between two normalized atoms."""
    reasons: list[str] = []
    for first_access in first["accesses"]:
        for second_access in second["accesses"]:
            if not _accesses_compatible(first_access, second_access, (first, second)):
                reasons.append(
                    f"{first_access['mode']}/{second_access['mode']} access conflict on "
                    f"{first_access['resource']}"
                )
    first_effects = {(item["domain"], item["key"]): item for item in first["effects"]}
    second_effects = {(item["domain"], item["key"]): item for item in second["effects"]}
    for identity in sorted(set(first_effects) & set(second_effects)):
        left = first_effects[identity]
        right = second_effects[identity]
        if left["mode"] == right["mode"] == "read":
            continue
        if left["mode"] == right["mode"] == "append" and first["semantics"]["commutative"] and second["semantics"]["commutative"]:
            continue
        reasons.append(f"effect conflict on {identity[0]}:{identity[1]}")
    for atom in (first, second):
        internal = atom["operation"]["internal_parallelism"]
        if internal["kind"] == "unknown" or (
            internal["kind"] == "native_scheduler" and internal["tokens"] is None
        ):
            reasons.append(
                f"atom {atom['id']} has unbounded internal parallelism and requires exclusive outer scheduling"
            )
    return list(dict.fromkeys(reasons))


def _dependency_reachable(
    start: str,
    target: str,
    dependencies: dict[str, list[dict[str, str]]],
    seen: set[str] | None = None,
) -> bool:
    if start == target:
        return True
    seen = set() if seen is None else seen
    if start in seen:
        return False
    seen.add(start)
    return any(
        dependency["atom"] == target
        or _dependency_reachable(dependency["atom"], target, dependencies, seen)
        for dependency in dependencies[start]
    )


def lower_exact_data_edges(atoms: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lower provable create→read def-use relationships to data edges.

    The pass is intentionally SSA-like and fail closed.  It first discovers all
    candidate directions without mutating the graph, so a pair that exchanges
    two artifacts in opposite directions cannot be accidentally oriented by
    whichever access happened to be visited last.  Multiple artifacts flowing
    in the same direction produce one edge with complete provenance.  Reverse
    explicit ordering, multiple creators, overwrite/read ambiguity, and cycles
    remain hard diagnostics rather than being assigned an arbitrary order.
    """
    lowered = copy.deepcopy(atoms)
    if not lowered:
        return [], []
    project = Path(lowered[0]["operation"]["cwd"]).resolve()
    lowered = validate_atoms(lowered, project)
    by_id = {atom["id"]: atom for atom in lowered}
    explicit_dependencies = _dependency_map(lowered)
    diagnostics: list[dict[str, Any]] = []
    atom_ids = sorted(by_id)

    # A create is a definition.  More than one unordered definition for the
    # same exact artifact is not a data edge; it is an ambiguous multi-writer
    # program that requires explicit versioning or ordering.
    creators: dict[str, list[str]] = defaultdict(list)
    for atom_id in atom_ids:
        for access in by_id[atom_id]["accesses"]:
            if access["mode"] == "create":
                creators[access["resource"]].append(atom_id)
    multi_writer_resources = {
        resource
        for resource, producer_ids in creators.items()
        if len(set(producer_ids)) > 1
    }
    for resource in sorted(multi_writer_resources):
        diagnostics.append(
            {
                "code": "MULTIPLE_DATA_PRODUCERS",
                "severity": "error",
                "message": f"multiple atoms create the same artifact: {resource}",
                "atoms": sorted(set(creators[resource])),
            }
        )

    candidates: list[tuple[str, str, tuple[str, ...]]] = []
    for index, first_id in enumerate(atom_ids):
        for second_id in atom_ids[index + 1:]:
            first = by_id[first_id]
            second = by_id[second_id]
            explicitly_ordered = (
                _dependency_reachable(first_id, second_id, explicit_dependencies)
                or _dependency_reachable(second_id, first_id, explicit_dependencies)
            )
            directions: dict[tuple[str, str], set[str]] = defaultdict(set)
            ambiguous: set[str] = set()
            for left in first["accesses"]:
                for right in second["accesses"]:
                    if not _resource_overlap(left["resource"], right["resource"]):
                        continue
                    if (
                        left["mode"] == "create"
                        and right["mode"] in {"read", "snapshot"}
                        and left["resource"] not in multi_writer_resources
                    ):
                        directions[(first_id, second_id)].add(left["resource"])
                    elif (
                        right["mode"] == "create"
                        and left["mode"] in {"read", "snapshot"}
                        and right["resource"] not in multi_writer_resources
                    ):
                        directions[(second_id, first_id)].add(right["resource"])
                    elif not _accesses_compatible(left, right, (first, second)):
                        ambiguous.add(
                            f"{left['mode']}/{right['mode']} on "
                            f"{left['resource']} and {right['resource']}"
                        )

            if len(directions) > 1:
                resources = sorted({item for values in directions.values() for item in values})
                diagnostics.append(
                    {
                        "code": "BIDIRECTIONAL_DATA_FLOW",
                        "severity": "error",
                        "message": (
                            "create-to-read inference requires opposite dependency directions "
                            f"for: {', '.join(resources)}"
                        ),
                        "atoms": [first_id, second_id],
                    }
                )
            elif len(directions) == 1 and not ambiguous:
                (producer, consumer), resources = next(iter(directions.items()))
                # Existing reverse control flow means that the reader explicitly
                # precedes its inferred producer.  Never silently reverse it.
                if _dependency_reachable(producer, consumer, explicit_dependencies):
                    diagnostics.append(
                        {
                            "code": "CONTRADICTORY_DATA_ORDER",
                            "severity": "error",
                            "message": (
                                f"explicit dependencies order consumer {consumer} before "
                                f"producer {producer}"
                            ),
                            "atoms": [producer, consumer],
                        }
                    )
                elif not _dependency_reachable(consumer, producer, explicit_dependencies):
                    candidates.append((producer, consumer, tuple(sorted(resources))))
            if ambiguous and not explicitly_ordered:
                diagnostics.append(
                    {
                        "code": "UNORDERED_ARTIFACT_CONFLICT",
                        "severity": "error",
                        "message": "; ".join(sorted(ambiguous)),
                        "atoms": [first_id, second_id],
                    }
                )

    # Add candidates in canonical order and check reachability after each edge.
    # This detects longer inferred cycles while keeping the result deterministic.
    dependencies = _dependency_map(lowered)
    for producer, consumer, resources in sorted(candidates):
        if _dependency_reachable(producer, consumer, dependencies):
            diagnostics.append(
                {
                    "code": "INFERRED_DATA_CYCLE",
                    "severity": "error",
                    "message": (
                        f"inferred data edge {producer} -> {consumer} would create a cycle"
                    ),
                    "atoms": [producer, consumer],
                }
            )
            continue
        edge = {"atom": producer, "kind": "data"}
        if edge not in by_id[consumer]["dependencies"]:
            by_id[consumer]["dependencies"].append(edge)
            dependencies[consumer].append(edge)
            diagnostics.append(
                {
                    "code": "INFERRED_DATA_EDGE",
                    "severity": "info",
                    "message": (
                        "inferred create-to-read dependency on "
                        + ", ".join(resources)
                    ),
                    "atoms": [producer, consumer],
                    "resources": list(resources),
                }
            )
    return validate_atoms(lowered, project), diagnostics


def canonical_plan_hash(
    atoms: list[dict[str, Any]],
    capacities: dict[str, float],
    snapshots: list[dict[str, Any]],
    *,
    project_root: str | None = None,
    execution_contract: dict[str, Any] | None = None,
) -> str:
    payload = {
        "ir_version": IR_VERSION,
        "planner_version": "rcpsp-list-v1",
        "project_root": project_root,
        "atoms": [_canonicalize_atom(atom) for atom in sorted(atoms, key=lambda atom: atom["id"])],
        "capacities": {key: capacities[key] for key in sorted(capacities)},
        "snapshots": sorted(snapshots, key=lambda item: item["path"]),
        "execution_contract": execution_contract or {},
    }
    canonical = json.dumps(
        normalize_json_numbers(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_json_numbers(value: Any) -> Any:
    """Normalize JSON numbers across Python and JavaScript round trips.

    JSON has one numeric type, while Python's encoder distinguishes ``1`` from
    ``1.0`` and JavaScript's encoder does not. Plan hashes cross an MCP JSON
    boundary, so semantically equal integral values need one representation.
    """
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AtomError("canonical JSON numbers must be finite")
        if value == 0 or value.is_integer():
            return int(value)
        return value
    if isinstance(value, list):
        return [normalize_json_numbers(item) for item in value]
    if isinstance(value, tuple):
        return [normalize_json_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_json_numbers(item) for key, item in value.items()}
    return value


def _canonicalize_atom(atom: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical, JSON-safe atom without changing its semantics."""
    item = copy.deepcopy(atom)
    item["dependencies"] = sorted(
        item.get("dependencies", []), key=lambda edge: (edge["atom"], edge["kind"])
    )
    item["accesses"] = sorted(
        item.get("accesses", []), key=lambda access: (access["resource"], access["mode"])
    )
    item["effects"] = sorted(
        item.get("effects", []),
        key=lambda effect: (effect["domain"], effect["key"], effect["mode"]),
    )
    item["claims"] = sorted(
        item.get("claims", []), key=lambda claim: (claim["resource"], claim["units"])
    )
    item["assurance"]["blockers"] = sorted(set(item["assurance"].get("blockers", [])))
    item["operation"]["env"] = {
        key: item["operation"].get("env", {})[key]
        for key in sorted(item["operation"].get("env", {}))
    }
    return item


def _default_semantics(operation_kind: str, side_effect: bool) -> dict[str, Any]:
    read_only = operation_kind == "read" and not side_effect
    return {
        "idempotent": True if read_only else None,
        "retryable": True if read_only else None,
        "deterministic": True if read_only else None,
        "cacheable": bool(read_only),
        "commutative": False,
        "cancel_safe": None,
        "splittable": None,
        "reorderable": "unknown",
    }


def _normalize_semantics(value: Any, operation_kind: str, side_effect: bool) -> dict[str, Any]:
    if value is None:
        return _default_semantics(operation_kind, side_effect)
    if not isinstance(value, dict):
        raise AtomError("atom semantics must be an object")
    allowed = {
        "idempotent", "retryable", "deterministic", "cacheable", "commutative",
        "cancel_safe", "splittable", "reorderable",
    }
    unknown = set(value) - allowed
    if unknown:
        raise AtomError(f"unsupported atom semantics fields: {sorted(unknown)}")
    result = _default_semantics(operation_kind, side_effect)
    for key, item in value.items():
        if key == "reorderable":
            if item not in {"explicit", "proved", "forbidden", "unknown"}:
                raise AtomError("atom semantics.reorderable has an unsupported value")
        elif item is not None and not isinstance(item, bool):
            raise AtomError(f"atom semantics.{key} must be boolean or null")
        result[key] = item
    return result


def _normalize_dependencies(value: Any, atom_id: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ATOMS:
        raise AtomError(f"atom {atom_id} dependencies must be a bounded array")
    normalized: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            dependency_id = item
            kind = "success"
        elif isinstance(item, dict):
            dependency_id = item.get("atom")
            kind = item.get("kind", "success")
        else:
            raise AtomError(f"atom {atom_id} dependency entries must be strings or objects")
        if not isinstance(dependency_id, str) or not ATOM_ID_RE.match(dependency_id):
            raise AtomError(f"atom {atom_id} has an invalid dependency ID")
        if kind not in DEPENDENCY_KINDS:
            raise AtomError(f"atom {atom_id} has unsupported dependency kind: {kind}")
        entry = {"atom": dependency_id, "kind": kind}
        if entry not in normalized:
            normalized.append(entry)
    return normalized


def normalize_atom(raw: Any, index: int, project: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AtomError(f"atom {index} must be an object")
    atom_id = raw.get("id", f"atom-{index}")
    if not isinstance(atom_id, str) or not ATOM_ID_RE.match(atom_id):
        raise AtomError(f"atom {index} id must match {ATOM_ID_RE.pattern}")
    operation_raw = raw.get("operation")
    if not isinstance(operation_raw, dict):
        raise AtomError(f"atom {atom_id} operation must be an object")
    kind = operation_raw.get("kind", "command")
    if kind not in OPERATION_KINDS:
        raise AtomError(f"atom {atom_id} has unsupported operation kind: {kind}")
    cwd = _normalize_cwd(operation_raw.get("cwd"), project, f"atom {atom_id} cwd")
    argv_raw = operation_raw.get("argv")
    argv: list[str] | None = None
    if argv_raw is not None:
        if not isinstance(argv_raw, list) or not argv_raw or len(argv_raw) > 256:
            raise AtomError(f"atom {atom_id} argv must be a non-empty bounded array")
        argv = [_bounded_text(item, f"atom {atom_id} argv entry") for item in argv_raw]
    command = operation_raw.get("command")
    if command is not None:
        command = _bounded_text(command, f"atom {atom_id} command")
    if argv is None and command is None:
        raise AtomError(f"atom {atom_id} operation needs argv or command")
    completion = operation_raw.get("completion", "process_exit")
    if completion not in COMPLETION_KINDS:
        raise AtomError(f"atom {atom_id} has unsupported completion contract: {completion}")
    internal_raw = operation_raw.get("internal_parallelism", {"kind": "unknown", "tokens": None})
    if not isinstance(internal_raw, dict):
        raise AtomError(f"atom {atom_id} internal_parallelism must be an object")
    internal_kind = internal_raw.get("kind", "unknown")
    if internal_kind not in {"none", "bounded", "native_scheduler", "unknown"}:
        raise AtomError(f"atom {atom_id} has unsupported internal parallelism kind")
    internal_tokens = _bounded_number(
        internal_raw.get("tokens"),
        f"atom {atom_id} internal_parallelism.tokens",
        maximum=4096,
        allow_zero=False,
    )
    if internal_kind == "bounded" and internal_tokens is None:
        raise AtomError(f"atom {atom_id} bounded internal parallelism requires tokens")
    environment_raw = operation_raw.get("env", {})
    if not isinstance(environment_raw, dict) or len(environment_raw) > 128:
        raise AtomError(f"atom {atom_id} env must be a bounded object")
    environment = {
        _bounded_text(key, f"atom {atom_id} env key", 512): _bounded_text(val, f"atom {atom_id} env value")
        for key, val in environment_raw.items()
    }
    if os.name == "nt":
        if any(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None
            for key in environment
        ):
            raise AtomError(
                f"atom {atom_id} Windows Preview env names must use ASCII identifier syntax"
            )
        folded_keys = [key.casefold() for key in environment]
        if len(folded_keys) != len(set(folded_keys)):
            raise AtomError(f"atom {atom_id} env keys collide under Windows semantics")
        if argv:
            try:
                validate_windows_executable_contract(argv[0])
            except RunnerError as exc:
                raise AtomError(f"atom {atom_id} {exc}") from exc
    terminal_mode = operation_raw.get("terminal_mode", "pipes")
    if terminal_mode not in {"pipes", "conpty"}:
        raise AtomError(f"atom {atom_id} terminal_mode must be pipes or conpty")
    if terminal_mode == "conpty" and os.name != "nt":
        raise AtomError(f"atom {atom_id} ConPTY requires native Windows")
    resource_limits_raw = operation_raw.get("resource_limits", {})
    if not isinstance(resource_limits_raw, dict):
        raise AtomError(f"atom {atom_id} resource_limits must be an object")
    unknown_limits = set(resource_limits_raw) - {
        "cpu_rate_percent",
        "memory_limit_mb",
        "max_processes",
    }
    if unknown_limits:
        raise AtomError(f"atom {atom_id} has unsupported resource limits: {sorted(unknown_limits)}")
    cpu_rate_percent = _bounded_number(
        resource_limits_raw.get("cpu_rate_percent"),
        f"atom {atom_id} resource_limits.cpu_rate_percent",
        minimum=0.01,
        maximum=100,
    )
    memory_limit_mb = _bounded_number(
        resource_limits_raw.get("memory_limit_mb"),
        f"atom {atom_id} resource_limits.memory_limit_mb",
        minimum=128,
        maximum=1_048_576,
    )
    max_processes_raw = resource_limits_raw.get("max_processes")
    if max_processes_raw is not None and (
        isinstance(max_processes_raw, bool)
        or not isinstance(max_processes_raw, int)
        or not 2 <= max_processes_raw <= 4096
    ):
        raise AtomError(f"atom {atom_id} resource_limits.max_processes must be 2..4096")
    process_limit_blocker = windows_process_limit_blocker(
        terminal_mode, max_processes_raw
    )
    if process_limit_blocker is not None:
        raise AtomError(f"atom {atom_id} {process_limit_blocker}")
    resource_limits = {
        "cpu_rate_percent": cpu_rate_percent,
        "memory_limit_mb": memory_limit_mb,
        "max_processes": max_processes_raw,
    }
    if any(value is not None for value in resource_limits.values()) and os.name != "nt":
        raise AtomError(f"atom {atom_id} Job Object resource limits require native Windows")
    broker_boundary = brokered_execution_boundary(argv[0]) if argv else None
    if broker_boundary is not None and any(
        value is not None for value in resource_limits.values()
    ):
        raise AtomError(
            f"atom {atom_id} launches the {broker_boundary['target_realm']} broker; "
            "Job Object resource limits cannot constrain the brokered workload"
        )
    side_effect = raw.get("side_effect", kind in {"mutation", "compose_service"})
    if not isinstance(side_effect, bool):
        raise AtomError(f"atom {atom_id} side_effect must be boolean")
    semantics = _normalize_semantics(raw.get("semantics"), kind, side_effect)

    accesses_raw = raw.get("accesses", [])
    if not isinstance(accesses_raw, list) or len(accesses_raw) > 256:
        raise AtomError(f"atom {atom_id} accesses must be a bounded array")
    accesses: list[dict[str, str]] = []
    for access in accesses_raw:
        if not isinstance(access, dict) or access.get("mode") not in ACCESS_MODES:
            raise AtomError(f"atom {atom_id} has an invalid resource access")
        item = {
            "resource": _normalize_resource(access.get("resource"), cwd),
            "mode": access["mode"],
        }
        if item not in accesses:
            accesses.append(item)

    effects_raw = raw.get("effects", [])
    if not isinstance(effects_raw, list) or len(effects_raw) > 128:
        raise AtomError(f"atom {atom_id} effects must be a bounded array")
    effects: list[dict[str, str]] = []
    for effect in effects_raw:
        if not isinstance(effect, dict):
            raise AtomError(f"atom {atom_id} effect must be an object")
        domain = _bounded_text(effect.get("domain"), f"atom {atom_id} effect domain", 256)
        key = _bounded_text(effect.get("key"), f"atom {atom_id} effect key", 2048)
        mode = effect.get("mode", "write")
        if mode not in {"read", "write", "append", "transaction", "lease", "consume"}:
            raise AtomError(f"atom {atom_id} has unsupported effect mode: {mode}")
        item = {"domain": domain, "key": key, "mode": mode}
        if item not in effects:
            effects.append(item)

    assurance_raw = raw.get("assurance", {})
    if not isinstance(assurance_raw, dict):
        raise AtomError(f"atom {atom_id} assurance must be an object")
    parse_assurance = assurance_raw.get("parse", "exact" if argv is not None else "conservative")
    control_assurance = assurance_raw.get("control", "exact")
    effects_assurance = assurance_raw.get(
        "effects",
        "complete_declared" if (accesses or effects or kind == "read") else "unknown",
    )
    codegen_assurance = assurance_raw.get("codegen", "exact_argv" if argv is not None else "opaque")
    if parse_assurance not in ASSURANCE_PARSE:
        raise AtomError(f"atom {atom_id} has unsupported parse assurance")
    if control_assurance not in ASSURANCE_CONTROL:
        raise AtomError(f"atom {atom_id} has unsupported control assurance")
    if effects_assurance not in ASSURANCE_EFFECTS:
        raise AtomError(f"atom {atom_id} has unsupported effects assurance")
    if codegen_assurance not in ASSURANCE_CODEGEN:
        raise AtomError(f"atom {atom_id} has unsupported codegen assurance")
    blockers_raw = assurance_raw.get("blockers", [])
    if not isinstance(blockers_raw, list) or not all(isinstance(item, str) for item in blockers_raw):
        raise AtomError(f"atom {atom_id} assurance.blockers must be an array of strings")
    assurance = {
        "parse": parse_assurance,
        "control": control_assurance,
        "effects": effects_assurance,
        "codegen": codegen_assurance,
        "rank": float(assurance_raw.get("rank", 1.0 if parse_assurance == "exact" else 0.5)),
        "blockers": list(dict.fromkeys(blockers_raw)),
    }
    if not 0 <= assurance["rank"] <= 1:
        raise AtomError(f"atom {atom_id} assurance.rank must be between 0 and 1")
    if broker_boundary is not None and assurance["effects"] != "complete_declared":
        assurance["effects"] = "unknown"
        assurance["blockers"].append("BROKERED_REALM_EFFECTS_INCOMPLETE")
    if side_effect and not accesses and not effects:
        effects.append({"domain": "unknown", "key": "host", "mode": "write"})
        assurance["effects"] = "unknown"
        assurance["blockers"].append("UNKNOWN_EFFECT")

    profile = raw.get("profile", "mixed")
    if profile not in PROFILES:
        raise AtomError(f"atom {atom_id} has unsupported profile: {profile}")
    cost_raw = raw.get("cost", {})
    if not isinstance(cost_raw, dict):
        raise AtomError(f"atom {atom_id} cost must be an object")
    cost = {
        "duration_seconds": _bounded_number(cost_raw.get("duration_seconds"), f"atom {atom_id} cost.duration_seconds", maximum=86_400, allow_zero=False),
        "startup_seconds": _bounded_number(cost_raw.get("startup_seconds", 0.03), f"atom {atom_id} cost.startup_seconds", maximum=60),
        "memory_mb": _bounded_number(cost_raw.get("memory_mb"), f"atom {atom_id} cost.memory_mb", maximum=10_000_000, allow_zero=False),
        "cpu_cores": _bounded_number(cost_raw.get("cpu_cores"), f"atom {atom_id} cost.cpu_cores", maximum=1024, allow_zero=False),
    }
    claims_raw = raw.get("claims", [])
    if not isinstance(claims_raw, list) or len(claims_raw) > 128:
        raise AtomError(f"atom {atom_id} claims must be a bounded array")
    claim_totals: dict[str, float] = defaultdict(float)
    for claim in claims_raw:
        if not isinstance(claim, dict):
            raise AtomError(f"atom {atom_id} claim must be an object")
        resource = _bounded_text(claim.get("resource"), f"atom {atom_id} claim resource", 512)
        units = _bounded_number(claim.get("units", 1), f"atom {atom_id} claim units", maximum=1_000_000, allow_zero=False)
        assert units is not None
        claim_totals[resource] += units
        if claim_totals[resource] > 1_000_000:
            raise AtomError(f"atom {atom_id} aggregate claim {resource} exceeds 1000000 units")
    if "worker_slot" not in claim_totals:
        claim_totals["worker_slot"] = 1.0
    if cost["cpu_cores"] is not None and "cpu_core" not in claim_totals:
        claim_totals["cpu_core"] = cost["cpu_cores"]
    if internal_kind in {"bounded", "native_scheduler"} and internal_tokens is not None:
        claim_totals["cpu_core"] = max(
            claim_totals.get("cpu_core", 0.0), internal_tokens
        )
    if cost["memory_mb"] is not None and "memory_mb" not in claim_totals:
        claim_totals["memory_mb"] = cost["memory_mb"]
    if profile == "accelerator" and "accelerator_slot" not in claim_totals:
        claim_totals["accelerator_slot"] = 1.0
    claims = [
        {"resource": resource, "units": claim_totals[resource]}
        for resource in sorted(claim_totals)
    ]

    batch_raw = raw.get("batch")
    batch = None
    if batch_raw is not None:
        if not isinstance(batch_raw, dict):
            raise AtomError(f"atom {atom_id} batch must be an object")
        key = _bounded_text(batch_raw.get("key"), f"atom {atom_id} batch key", 256)
        strategy = batch_raw.get("strategy", "same_argv_shape")
        if strategy not in {"same_argv_shape", "multi_arg", "native_command", "compose_services"}:
            raise AtomError(f"atom {atom_id} has unsupported batch strategy: {strategy}")
        batch = {"key": key, "strategy": strategy}

    provenance_raw = raw.get("provenance", {})
    if not isinstance(provenance_raw, dict):
        raise AtomError(f"atom {atom_id} provenance must be an object")
    provenance = {
        "adapter": _bounded_text(provenance_raw.get("adapter", "explicit"), f"atom {atom_id} provenance adapter", 128),
        "source": _bounded_text(provenance_raw.get("source", "user_or_agent"), f"atom {atom_id} provenance source", 4096),
        "symbol": _bounded_text(provenance_raw.get("symbol", atom_id), f"atom {atom_id} provenance symbol", 512),
        "line": int(provenance_raw["line"]) if isinstance(provenance_raw.get("line"), int) and provenance_raw["line"] > 0 else None,
        "confidence": float(provenance_raw.get("confidence", 1.0)),
    }
    if not 0 <= provenance["confidence"] <= 1:
        raise AtomError(f"atom {atom_id} provenance confidence must be between 0 and 1")

    return {
        "id": atom_id,
        "operation": {
            "kind": kind,
            "argv": argv,
            "command": command,
            "cwd": str(cwd),
            "env": environment,
            "completion": completion,
            "internal_parallelism": {"kind": internal_kind, "tokens": internal_tokens},
            "terminal_mode": terminal_mode,
            "resource_limits": resource_limits,
            "broker_boundary": broker_boundary,
        },
        "dependencies": _normalize_dependencies(raw.get("dependencies"), atom_id),
        "accesses": accesses,
        "effects": effects,
        "claims": claims,
        "profile": profile,
        "side_effect": side_effect,
        "semantics": semantics,
        "cost": cost,
        "batch": batch,
        "provenance": provenance,
        "assurance": assurance,
        "opaque_reason": raw.get("opaque_reason") if kind == "opaque" else None,
    }


def validate_atoms(raw_atoms: Any, project: Path) -> list[dict[str, Any]]:
    if not isinstance(raw_atoms, list) or len(raw_atoms) > MAX_ATOMS:
        raise AtomError(f"atoms must be an array with at most {MAX_ATOMS} entries")
    atoms = [normalize_atom(raw, index, project) for index, raw in enumerate(raw_atoms)]
    ids = [atom["id"] for atom in atoms]
    if len(ids) != len(set(ids)):
        raise AtomError("atom IDs must be unique")
    known = set(ids)
    edge_count = 0
    access_count = sum(len(atom["accesses"]) for atom in atoms)
    effect_count = sum(len(atom["effects"]) for atom in atoms)
    claim_count = sum(len(atom["claims"]) for atom in atoms)
    for label, count in (
        ("access", access_count),
        ("effect", effect_count),
        ("claim", claim_count),
    ):
        if count > MAX_EDGES:
            raise AtomError(f"atom graph exceeds the {MAX_EDGES}-{label} limit")
    for atom in atoms:
        for dependency in atom["dependencies"]:
            if dependency["atom"] not in known:
                raise AtomError(f"atom {atom['id']} has unknown dependency: {dependency['atom']}")
            if dependency["atom"] == atom["id"]:
                raise AtomError(f"atom {atom['id']} cannot depend on itself")
            edge_count += 1
    if edge_count > MAX_EDGES:
        raise AtomError(f"atom graph exceeds the {MAX_EDGES}-edge limit")
    _topological_order(atoms)
    return atoms


def _topological_order(atoms: list[dict[str, Any]]) -> list[str]:
    by_id = {atom["id"]: atom for atom in atoms}
    indegree = {atom_id: 0 for atom_id in by_id}
    children: dict[str, list[str]] = defaultdict(list)
    for atom in atoms:
        for dependency in atom["dependencies"]:
            indegree[atom["id"]] += 1
            children[dependency["atom"]].append(atom["id"])
    ready = sorted(atom_id for atom_id, degree in indegree.items() if degree == 0)
    ordered: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for child in sorted(children[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(ordered) != len(atoms):
        cyclic = sorted(set(by_id) - set(ordered))
        raise AtomError(f"atom dependency graph contains a cycle involving: {cyclic[:12]}")
    return ordered


def _duration(atom: dict[str, Any]) -> tuple[float, bool]:
    supplied = atom["cost"]["duration_seconds"]
    if supplied is not None:
        return max(0.001, supplied + (atom["cost"]["startup_seconds"] or 0.0)), True
    defaults = {
        "read": 0.25,
        "test": 2.0,
        "build": 3.0,
        "compose_service": 2.0,
        "network": 1.0,
        "opaque": 1.5,
    }
    return defaults.get(atom["operation"]["kind"], 1.0) + (atom["cost"]["startup_seconds"] or 0.0), False


def _dependency_map(atoms: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    return {atom["id"]: list(atom["dependencies"]) for atom in atoms}


def _ancestors(atom_id: str, dependencies: dict[str, list[dict[str, str]]], memo: dict[str, set[str]]) -> set[str]:
    if atom_id in memo:
        return memo[atom_id]
    result: set[str] = set()
    for dependency in dependencies[atom_id]:
        result.add(dependency["atom"])
        result.update(_ancestors(dependency["atom"], dependencies, memo))
    memo[atom_id] = result
    return result


def _critical_path_priorities(atoms: list[dict[str, Any]]) -> dict[str, float]:
    by_id = {atom["id"]: atom for atom in atoms}
    children: dict[str, list[str]] = defaultdict(list)
    for atom in atoms:
        for dependency in atom["dependencies"]:
            if dependency["kind"] != "failure":
                children[dependency["atom"]].append(atom["id"])
    order = _topological_order(atoms)
    priorities: dict[str, float] = {}
    for atom_id in reversed(order):
        own, _ = _duration(by_id[atom_id])
        priorities[atom_id] = own + max((priorities[child] for child in children[atom_id]), default=0.0)
    return priorities


def _normalize_capacities(value: Any, defaults: dict[str, float]) -> dict[str, float]:
    capacities = dict(defaults)
    if value is None:
        return capacities
    if isinstance(value, dict):
        if len(value) > 128:
            raise AtomError("capacities must contain at most 128 resources")
        entries = [
            {"resource": resource, "capacity": capacity}
            for resource, capacity in value.items()
        ]
    elif isinstance(value, list) and len(value) <= 128:
        entries = value
    else:
        raise AtomError("capacities must be a bounded array or object")
    for item in entries:
        if not isinstance(item, dict):
            raise AtomError("capacity entries must be objects")
        resource = _bounded_text(item.get("resource"), "capacity resource", 512)
        capacity = _bounded_number(item.get("capacity"), f"capacity {resource}", maximum=1_000_000_000, allow_zero=False)
        assert capacity is not None
        capacities[resource] = capacity
    return {resource: capacities[resource] for resource in sorted(capacities)}


def _claims_fit(atom: dict[str, Any], usage: dict[str, float], capacities: dict[str, float]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for claim in atom["claims"]:
        resource = claim["resource"]
        capacity = capacities.get(resource)
        if capacity is None:
            # Unknown custom capacities are conservative single-slot semaphores.
            capacity = 1.0
        if claim["units"] > capacity + 1e-9:
            reasons.append(f"claim {resource}={claim['units']:g} exceeds capacity {capacity:g}")
        elif usage.get(resource, 0.0) + claim["units"] > capacity + 1e-9:
            reasons.append(f"insufficient available {resource}")
    return not reasons, reasons


def _add_usage(atom: dict[str, Any], usage: dict[str, float], sign: float) -> None:
    for claim in atom["claims"]:
        resource = claim["resource"]
        usage[resource] = max(0.0, usage.get(resource, 0.0) + sign * claim["units"])


def plan_atoms(
    atoms: list[dict[str, Any]],
    *,
    capacities: dict[str, float],
) -> dict[str, Any]:
    """Perform deterministic, event-driven list scheduling without level barriers."""
    atoms = [_canonicalize_atom(atom) for atom in sorted(atoms, key=lambda item: item["id"])]
    if not atoms:
        return {
            "events": [],
            "timeline": [],
            "makespan_seconds": 0.0,
            "serial_seconds": 0.0,
            "estimated_time_saved_seconds": None,
            "estimated_speedup": None,
            "peak_parallelism": 0,
            "conflicts": [],
            "generated_serialization_edges": [],
            "blocked_claims": [],
            "unscheduled_atoms": [],
            "forecast_complete": False,
            "forecast_kind": "empty_plan",
        }
    by_id = {atom["id"]: atom for atom in atoms}
    dependencies = _dependency_map(atoms)
    ancestor_memo: dict[str, set[str]] = {}
    ancestor_sets = {atom_id: _ancestors(atom_id, dependencies, ancestor_memo) for atom_id in by_id}
    unordered_conflicts: dict[frozenset[str], list[str]] = {}
    conflicts: list[dict[str, Any]] = []
    atom_ids = sorted(by_id)
    for index, first_id in enumerate(atom_ids):
        for second_id in atom_ids[index + 1:]:
            reasons = atom_conflicts(by_id[first_id], by_id[second_id])
            ordered = first_id in ancestor_sets[second_id] or second_id in ancestor_sets[first_id]
            if reasons and not ordered and (
                by_id[first_id]["semantics"]["reorderable"] not in {"explicit", "proved"}
                or by_id[second_id]["semantics"]["reorderable"] not in {"explicit", "proved"}
            ):
                reasons.append("semantic reordering is not explicitly allowed or statically proved")
            if not reasons:
                continue
            conflicts.append(
                {
                    "atoms": [first_id, second_id],
                    "reasons": reasons,
                    "ordered_by_dependency": ordered,
                }
            )
            if not ordered:
                unordered_conflicts[frozenset((first_id, second_id))] = reasons

    priorities = _critical_path_priorities(atoms)
    pending = set(by_id)
    completed: set[str] = set()
    blocked: set[str] = set()
    running: dict[str, dict[str, float]] = {}
    usage: dict[str, float] = {}
    timeline: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    blocked_claims: list[dict[str, Any]] = []
    current_time = 0.0
    lane_free_at: list[float] = []

    # Reject impossible atoms before simulation.  A forecast must never pretend
    # to run above a hard capacity merely so the remainder of the graph can be
    # drawn.  Dependants are blocked to a fixed point, independent of input
    # order, while unrelated atoms remain schedulable.
    for atom_id in sorted(pending):
        fit, reasons = _claims_fit(by_id[atom_id], {}, capacities)
        if not fit:
            blocked.add(atom_id)
            blocked_claims.append({"atom": atom_id, "reasons": reasons})
    changed = True
    while changed:
        changed = False
        for atom_id in sorted(pending - blocked):
            failed_dependencies = sorted(
                dependency["atom"]
                for dependency in dependencies[atom_id]
                if dependency["atom"] in blocked
            )
            if failed_dependencies:
                blocked.add(atom_id)
                blocked_claims.append(
                    {
                        "atom": atom_id,
                        "reasons": [
                            "blocked by unschedulable dependencies: "
                            + ", ".join(failed_dependencies)
                        ],
                    }
                )
                changed = True
    pending.difference_update(blocked)

    def dependency_ready(atom_id: str) -> bool:
        # The nominal planning path assumes successful predecessors. Failure-only
        # branches remain represented but are scheduled after their predecessor
        # as a conservative worst-case branch.
        return all(item["atom"] in completed for item in dependencies[atom_id])

    while pending or running:
        ready = sorted(
            (atom_id for atom_id in pending if dependency_ready(atom_id)),
            key=lambda atom_id: (-priorities[atom_id], atom_id),
        )
        started: list[str] = []
        for atom_id in ready:
            atom = by_id[atom_id]
            fit, _ = _claims_fit(atom, usage, capacities)
            conflict_reasons: list[str] = []
            for running_id in running:
                conflict_reasons.extend(unordered_conflicts.get(frozenset((atom_id, running_id)), []))
            if not fit or conflict_reasons:
                continue
            duration, measured = _duration(atom)
            lane = next((index for index, available in enumerate(lane_free_at) if available <= current_time + 1e-9), None)
            if lane is None:
                lane = len(lane_free_at)
                lane_free_at.append(current_time)
            end = current_time + duration
            lane_free_at[lane] = end
            running[atom_id] = {"start": current_time, "end": end, "lane": float(lane)}
            _add_usage(atom, usage, 1.0)
            timeline.append(
                {
                    "atom": atom_id,
                    "start_seconds": round(current_time, 6),
                    "end_seconds": round(end, 6),
                    "lane": lane,
                    "duration_source": "declared" if measured else "model_default",
                }
            )
            pending.remove(atom_id)
            started.append(atom_id)
        if started:
            events.append({"time_seconds": round(current_time, 6), "started": started, "completed": []})
        if running:
            next_time = min(item["end"] for item in running.values())
            finishing = sorted(atom_id for atom_id, item in running.items() if abs(item["end"] - next_time) <= 1e-9)
            current_time = next_time
            for atom_id in finishing:
                _add_usage(by_id[atom_id], usage, -1.0)
                del running[atom_id]
                completed.add(atom_id)
            events.append({"time_seconds": round(current_time, 6), "started": [], "completed": finishing})
        elif pending:
            raise AtomError(f"atomic planner stalled with pending atoms: {sorted(pending)[:12]}")

    start_by_id = {item["atom"]: item["start_seconds"] for item in timeline}
    generated_edges: list[dict[str, str]] = []
    for pair, reasons in sorted(unordered_conflicts.items(), key=lambda item: sorted(item[0])):
        first_id, second_id = sorted(pair, key=lambda atom_id: (start_by_id[atom_id], atom_id))
        if start_by_id[first_id] == start_by_id[second_id]:
            # This cannot happen for a correctly enforced conflict; keep the
            # deterministic order as a defensive fallback.
            first_id, second_id = sorted((first_id, second_id))
        generated_edges.append(
            {
                "from": first_id,
                "to": second_id,
                "kind": "resource_serialization",
                "reason": "; ".join(reasons),
            }
        )
    serial_seconds = sum(_duration(atom)[0] for atom in atoms)
    makespan = max((item["end_seconds"] for item in timeline), default=0.0)
    forecast_complete = not blocked_claims
    return {
        "events": events,
        "timeline": sorted(timeline, key=lambda item: (item["start_seconds"], item["lane"], item["atom"])),
        "makespan_seconds": round(makespan, 6),
        "serial_seconds": round(serial_seconds, 6),
        "estimated_time_saved_seconds": (
            round(max(0.0, serial_seconds - makespan), 6) if forecast_complete else None
        ),
        "estimated_speedup": (
            round(serial_seconds / makespan, 6) if forecast_complete and makespan else None
        ),
        "peak_parallelism": max(
            (
                sum(1 for item in timeline if item["start_seconds"] <= event["time_seconds"] < item["end_seconds"])
                for event in events
            ),
            default=0,
        ),
        "conflicts": conflicts,
        "generated_serialization_edges": generated_edges,
        "blocked_claims": sorted(blocked_claims, key=lambda item: item["atom"]),
        "unscheduled_atoms": sorted(blocked),
        "forecast_complete": forecast_complete,
        "forecast_kind": (
            "declared_costs" if all(atom["cost"]["duration_seconds"] is not None for atom in atoms)
            else "mixed_declared_and_model_defaults"
        ),
    }


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DIAGNOSTIC_SEVERITIES = {"info", "warning", "error"}


def _default_capacities() -> dict[str, float]:
    logical = float(max(1, os.cpu_count() or 1))
    capacities = {
        "accelerator_slot": 1.0,
        "cpu_core": logical,
        "worker_slot": float(min(MAX_ATOMS, max(1, int(logical)))),
    }
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        if pages > 0 and page_size > 0:
            # The compiler owns only a planning envelope, never all host memory.
            capacities["memory_mb"] = max(1.0, pages * page_size / 1_048_576 * 0.7)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return capacities


def _canonical_snapshot_path(raw_path: str, project: Path) -> tuple[str, Path]:
    path = Path(raw_path).expanduser()
    resolved = path.resolve(strict=False) if path.is_absolute() else (project / path).resolve(strict=False)
    try:
        label = resolved.relative_to(project).as_posix()
    except ValueError:
        label = str(resolved)
    return label, resolved


def _normalize_source_snapshots(value: Any, project: Path) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ATOMS:
        raise AtomError(f"snapshots must be an array with at most {MAX_ATOMS} entries")
    by_path: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise AtomError(f"snapshot {index} must be an object")
        path_text = _bounded_text(raw.get("path"), f"snapshot {index} path", 4096)
        label, _ = _canonical_snapshot_path(path_text, project)
        size = raw.get("size")
        digest = raw.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > MAX_SOURCE_BYTES:
            raise AtomError(
                f"snapshot {label} size must be an integer between 0 and {MAX_SOURCE_BYTES}"
            )
        if not isinstance(digest, str) or not _SHA256_RE.match(digest.lower()):
            raise AtomError(f"snapshot {label} sha256 must be a 64-character hexadecimal digest")
        item = {"path": label, "size": size, "sha256": digest.lower()}
        previous = by_path.get(label)
        if previous is not None and previous != item:
            raise AtomError(f"snapshot {label} has conflicting declarations")
        by_path[label] = item
    return [by_path[path] for path in sorted(by_path)]


def _source_diagnostic(code: str, message: str, path: str) -> dict[str, Any]:
    return {
        "code": code,
        "severity": "error",
        "message": message,
        "source": path,
    }


def validate_source_snapshots(plan_or_snapshots: Any) -> list[dict[str, Any]]:
    """Revalidate compilation inputs immediately before execution.

    This is a fail-closed TOCTOU guard, not a long-lived lease.  The executor
    should call it directly before launching a compiled plan.  The helper reads
    through an open descriptor, compares pre/post descriptor metadata and the
    path's final identity, and verifies both size and digest.
    """
    if isinstance(plan_or_snapshots, dict):
        raw_snapshots = plan_or_snapshots.get(
            "source_snapshots", plan_or_snapshots.get("snapshots", [])
        )
        project_text = plan_or_snapshots.get("project_root", os.getcwd())
    else:
        raw_snapshots = plan_or_snapshots
        project_text = os.getcwd()
    try:
        project = Path(project_text).expanduser().resolve(strict=False)
    except (OSError, TypeError, ValueError):
        return [_source_diagnostic("INVALID_PROJECT_ROOT", "project_root is invalid", str(project_text))]
    if not isinstance(raw_snapshots, list):
        return [_source_diagnostic("INVALID_SOURCE_SNAPSHOTS", "source_snapshots must be an array", "")]
    if len(raw_snapshots) > MAX_ATOMS:
        return [
            _source_diagnostic(
                "SOURCE_SNAPSHOT_LIMIT_EXCEEDED",
                f"source_snapshots cannot contain more than {MAX_ATOMS} entries",
                "",
            )
        ]

    diagnostics: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_snapshots):
        if not isinstance(raw, dict):
            diagnostics.append(
                _source_diagnostic(
                    "INVALID_SOURCE_SNAPSHOT", f"snapshot {index} must be an object", str(index)
                )
            )
            continue
        path_text = raw.get("path")
        expected_size = raw.get("size")
        expected_digest = raw.get("sha256")
        if (
            not isinstance(path_text, str)
            or "\x00" in path_text
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or not isinstance(expected_digest, str)
            or not _SHA256_RE.match(expected_digest.lower())
        ):
            diagnostics.append(
                _source_diagnostic(
                    "INVALID_SOURCE_SNAPSHOT",
                    "snapshot path, size, or sha256 is invalid",
                    str(path_text),
                )
            )
            continue
        label, path = _canonical_snapshot_path(path_text, project)
        try:
            with path.open("rb") as source:
                before = os.fstat(source.fileno())
                digest = hashlib.sha256()
                actual_size = 0
                while True:
                    chunk = source.read(131_072)
                    if not chunk:
                        break
                    actual_size += len(chunk)
                    if actual_size > MAX_SOURCE_BYTES:
                        break
                    digest.update(chunk)
                after = os.fstat(source.fileno())
            final = path.stat()
        except FileNotFoundError:
            diagnostics.append(
                _source_diagnostic("SOURCE_SNAPSHOT_MISSING", "snapshotted source no longer exists", label)
            )
            continue
        except OSError as exc:
            diagnostics.append(
                _source_diagnostic("SOURCE_SNAPSHOT_UNREADABLE", f"cannot validate source: {exc}", label)
            )
            continue
        identity_changed = (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
        )
        if identity_changed:
            diagnostics.append(
                _source_diagnostic(
                    "SOURCE_CHANGED_DURING_VALIDATION",
                    "source identity or metadata changed while it was being validated",
                    label,
                )
            )
            continue
        if actual_size != expected_size or actual_size > MAX_SOURCE_BYTES or digest.hexdigest() != expected_digest.lower():
            diagnostics.append(
                _source_diagnostic(
                    "SOURCE_SNAPSHOT_CHANGED",
                    "source size or digest differs from the compiled snapshot",
                    label,
                )
            )
    return sorted(diagnostics, key=lambda item: (item.get("source", ""), item["code"], item["message"]))


def _normalize_diagnostics(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_EDGES:
        raise AtomError(f"diagnostics must be an array with at most {MAX_EDGES} entries")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise AtomError(f"diagnostic {index} must be an object")
        code = _bounded_text(raw.get("code", "UNSPECIFIED"), f"diagnostic {index} code", 256)
        severity = raw.get("severity", "warning")
        if severity not in _DIAGNOSTIC_SEVERITIES:
            raise AtomError(f"diagnostic {code} has an unsupported severity")
        message = _bounded_text(raw.get("message", code), f"diagnostic {code} message", 8192)
        item: dict[str, Any] = {"code": code, "severity": severity, "message": message}
        for key in ("source", "symbol"):
            if raw.get(key) is not None:
                item[key] = _bounded_text(raw[key], f"diagnostic {code} {key}", 4096)
        if isinstance(raw.get("line"), int) and raw["line"] > 0:
            item["line"] = raw["line"]
        for key in ("atoms", "resources"):
            if raw.get(key) is not None:
                collection = raw[key]
                if not isinstance(collection, list) or not all(isinstance(entry, str) for entry in collection):
                    raise AtomError(f"diagnostic {code} {key} must be a string array")
                item[key] = sorted(set(collection))
        normalized.append(item)
    unique = {
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")): item
        for item in normalized
    }
    severity_order = {"error": 0, "warning": 1, "info": 2}
    return sorted(
        unique.values(),
        key=lambda item: (
            severity_order[item["severity"]],
            item["code"],
            item.get("source", ""),
            item.get("line", 0),
            item.get("atoms", []),
            item["message"],
        ),
    )


def _canonical_metadata_list(value: Any, name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ATOMS:
        raise AtomError(f"{name} must be an array with at most {MAX_ATOMS} entries")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise AtomError(f"{name} entry {index} must be an object")
        try:
            encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            decoded = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise AtomError(f"{name} entry {index} is not JSON serializable") from exc
        if len(encoded) > MAX_COMMAND_CHARS:
            raise AtomError(f"{name} entry {index} exceeds the metadata size limit")
        normalized.append(decoded)
    unique = {
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")): item
        for item in normalized
    }
    return [unique[key] for key in sorted(unique)]


def _dependency_frontier(atom: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple((edge["atom"], edge["kind"]) for edge in atom["dependencies"])


def _same_argv_shape(atoms: list[dict[str, Any]]) -> bool:
    argvs = [atom["operation"].get("argv") for atom in atoms]
    if not argvs or any(argv is None for argv in argvs):
        return False
    lengths = {len(argv) for argv in argvs if argv is not None}
    if len(lengths) != 1:
        return False
    varying = 0
    assert all(argv is not None for argv in argvs)
    for index in range(next(iter(lengths))):
        if len({argv[index] for argv in argvs if argv is not None}) > 1:
            varying += 1
    return varying == 1


def _optimization_suggestions(
    atoms: list[dict[str, Any]],
    native_delegates: list[dict[str, Any]],
    relaxation_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    fusion: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for atom in atoms:
        if atom.get("batch"):
            groups[(atom["batch"]["key"], atom["batch"]["strategy"])].append(atom)
    for (batch_key, strategy), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda atom: atom["id"])
        blockers: list[str] = []
        if len({atom["operation"]["cwd"] for atom in members}) != 1:
            blockers.append("MIXED_CWD")
        if len({_dependency_frontier(atom) for atom in members}) != 1:
            blockers.append("MIXED_DEPENDENCY_FRONTIER")
        if any(atom["side_effect"] for atom in members) and strategy != "compose_services":
            blockers.append("SIDE_EFFECT_BOUNDARY")
        if strategy == "same_argv_shape" and not _same_argv_shape(members):
            blockers.append("ARGV_SHAPE_MISMATCH")
        if any(atom["assurance"]["codegen"] != "exact_argv" for atom in members) and strategy != "compose_services":
            blockers.append("NON_EXACT_CODEGEN")
        if any(atom["semantics"]["retryable"] != members[0]["semantics"]["retryable"] for atom in members):
            blockers.append("MIXED_RETRY_SEMANTICS")
        fusion.append(
            {
                "kind": "native_batch" if strategy in {"native_command", "compose_services"} else "fuse_batch",
                "batch_key": batch_key,
                "strategy": strategy,
                "atoms": [atom["id"] for atom in members],
                "eligible": not blockers,
                "blockers": sorted(set(blockers)),
                "reason": "amortize process startup while preserving one semantic batch boundary",
            }
        )

    split: list[dict[str, Any]] = []
    inferred_native: list[dict[str, Any]] = []
    for atom in atoms:
        internal = atom["operation"]["internal_parallelism"]
        if atom["semantics"]["splittable"] is True:
            split.append(
                {
                    "atom": atom["id"],
                    "eligible": not atom["side_effect"] and atom["assurance"]["effects"] in {"complete_declared", "complete_static"},
                    "reason": "atom explicitly declares a semantics-preserving split contract",
                    "required": ["disjoint item partitions", "isolated outputs", "preserved failure semantics"],
                }
            )
        if atom["assurance"]["codegen"] == "native_delegate" or internal["kind"] == "native_scheduler":
            inferred_native.append(
                {
                    "kind": "atom_native_scheduler",
                    "atoms": [atom["id"]],
                    "argv": atom["operation"].get("argv"),
                    "cwd": atom["operation"]["cwd"],
                    "reason": "the owning tool already models internal dependencies or parallelism",
                }
            )
    native = _canonical_metadata_list([*native_delegates, *inferred_native], "native delegate suggestions")
    return {
        "fusion": fusion,
        "split": sorted(split, key=lambda item: item["atom"]),
        "native_delegate": native,
        "semantic_relaxations": relaxation_candidates,
    }


def _execution_diagnostics(atoms: list[dict[str, Any]], schedule: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    if not atoms:
        diagnostics.append(
            {"code": "EMPTY_ATOM_PLAN", "severity": "error", "message": "the plan has no executable atoms"}
        )
    peak = int(schedule.get("peak_parallelism", 0))
    for atom in atoms:
        atom_id = atom["id"]
        assurance = atom["assurance"]
        if assurance["parse"] in {"opaque", "invalid"}:
            diagnostics.append(
                {
                    "code": "OPAQUE_OR_INVALID_ATOM",
                    "severity": "error",
                    "message": "atom syntax is not exact enough for direct atomic execution",
                    "atoms": [atom_id],
                }
            )
        elif assurance["parse"] == "conservative":
            diagnostics.append(
                {
                    "code": "CONSERVATIVE_PARSE",
                    "severity": "warning",
                    "message": "atom was conservatively parsed; review its declared effects",
                    "atoms": [atom_id],
                }
            )
        if assurance["control"] == "unknown":
            diagnostics.append(
                {
                    "code": "UNKNOWN_CONTROL_FLOW",
                    "severity": "error",
                    "message": "atom control flow is unknown",
                    "atoms": [atom_id],
                }
            )
        effects_assurance = assurance["effects"]
        if effects_assurance == "unknown" or (effects_assurance == "partial" and (atom["side_effect"] or peak > 1)):
            diagnostics.append(
                {
                    "code": "INCOMPLETE_EFFECT_MODEL",
                    "severity": "error",
                    "message": "atom effects are incomplete for safe direct scheduling",
                    "atoms": [atom_id],
                }
            )
        elif effects_assurance == "partial":
            diagnostics.append(
                {
                    "code": "PARTIAL_EFFECT_MODEL",
                    "severity": "warning",
                    "message": "atom effect declarations are partial",
                    "atoms": [atom_id],
                }
            )
        if assurance["codegen"] == "opaque" or atom["operation"].get("argv") is None:
            diagnostics.append(
                {
                    "code": "NON_EXECUTABLE_CODEGEN",
                    "severity": "error",
                    "message": "atom has no exact argv execution form",
                    "atoms": [atom_id],
                }
            )
        elif assurance["codegen"] == "native_delegate":
            diagnostics.append(
                {
                    "code": "NATIVE_DELEGATE_REQUIRED",
                    "severity": "error",
                    "message": "execute this atom through the owning native scheduler",
                    "atoms": [atom_id],
                }
            )
        for blocker in sorted(set(assurance.get("blockers", []))):
            diagnostics.append(
                {
                    "code": blocker,
                    # Frontends put only unresolved proof obligations in this
                    # list.  Unknown codes therefore remain fail-closed too.
                    "severity": "error",
                    "message": f"atom assurance blocker: {blocker}",
                    "atoms": [atom_id],
                }
            )
        semantics = atom["semantics"]
        if semantics["retryable"] is True and semantics["idempotent"] is not True:
            diagnostics.append(
                {
                    "code": "UNSAFE_RETRY_CONTRACT",
                    "severity": "error",
                    "message": "automatic retry requires an explicitly idempotent atom",
                    "atoms": [atom_id],
                }
            )
        if semantics["cacheable"] is True and (
            semantics["deterministic"] is not True
            or semantics["idempotent"] is not True
            or atom["side_effect"]
        ):
            diagnostics.append(
                {
                    "code": "UNSAFE_CACHE_CONTRACT",
                    "severity": "error",
                    "message": "cacheable atoms must be deterministic, idempotent, and side-effect free",
                    "atoms": [atom_id],
                }
            )
        if atom["operation"]["internal_parallelism"]["kind"] == "unknown" and peak > 1:
            diagnostics.append(
                {
                    "code": "UNKNOWN_INTERNAL_PARALLELISM",
                    "severity": "warning",
                    "message": "nested worker usage is unknown and may oversubscribe the host",
                    "atoms": [atom_id],
                }
            )

    by_id = {atom["id"]: atom for atom in atoms}
    for conflict in schedule.get("conflicts", []):
        if conflict.get("ordered_by_dependency"):
            continue
        first_id, second_id = conflict["atoms"]
        reorderable = all(
            by_id[atom_id]["semantics"]["reorderable"] in {"explicit", "proved"}
            for atom_id in (first_id, second_id)
        )
        diagnostics.append(
            {
                "code": "SERIALIZED_REORDERABLE_CONFLICT" if reorderable else "UNORDERED_SEMANTIC_CONFLICT",
                "severity": "warning" if reorderable else "error",
                "message": "; ".join(conflict["reasons"]),
                "atoms": [first_id, second_id],
            }
        )
    for blocked in schedule.get("blocked_claims", []):
        diagnostics.append(
            {
                "code": "UNSATISFIABLE_RESOURCE_CLAIM",
                "severity": "error",
                "message": "; ".join(blocked["reasons"]),
                "atoms": [blocked["atom"]],
            }
        )
    return diagnostics


def compile_atomic_plan(
    raw_atoms: Any,
    project: Path | str,
    *,
    capacities: Any = None,
    snapshots: Any = None,
    diagnostics: Any = None,
    native_delegates: Any = None,
    relaxation_candidates: Any = None,
) -> dict[str, Any]:
    """Compile normalized atoms into an immutable, execution-gated plan."""
    project_root = Path(project).expanduser().resolve(strict=False)
    if not project_root.is_dir():
        raise AtomError(f"project does not exist or is not a directory: {project_root}")

    # Accept the frontend result as a convenience while retaining the explicit
    # signature used by the MCP integration.
    if isinstance(raw_atoms, dict) and "atoms" in raw_atoms:
        frontend = raw_atoms
        raw_atoms = frontend.get("atoms")
        if snapshots is None:
            snapshots = frontend.get("snapshots")
        if diagnostics is None:
            diagnostics = frontend.get("diagnostics")
        if native_delegates is None:
            native_delegates = frontend.get("native_delegates")
        if relaxation_candidates is None:
            relaxation_candidates = frontend.get("relaxation_candidates")

    normalized_atoms = validate_atoms(raw_atoms, project_root)
    lowered_atoms, lowering_diagnostics = lower_exact_data_edges(normalized_atoms)
    canonical_atoms = [
        _canonicalize_atom(atom) for atom in sorted(lowered_atoms, key=lambda item: item["id"])
    ]

    normalized_capacities = _normalize_capacities(capacities, _default_capacities())
    for atom in canonical_atoms:
        for claim in atom["claims"]:
            normalized_capacities.setdefault(claim["resource"], 1.0)
    normalized_capacities = {
        resource: normalized_capacities[resource]
        for resource in sorted(normalized_capacities)
    }
    source_snapshots = _normalize_source_snapshots(snapshots, project_root)
    normalized_native = _canonical_metadata_list(native_delegates, "native_delegates")
    normalized_relaxations = _canonical_metadata_list(
        relaxation_candidates, "relaxation_candidates"
    )
    schedule = plan_atoms(canonical_atoms, capacities=normalized_capacities)
    suggestions = _optimization_suggestions(
        canonical_atoms, normalized_native, normalized_relaxations
    )

    provisional = {
        "schema": "atomlane/atomic-plan/v2",
        "ir_version": IR_VERSION,
        "project_root": str(project_root),
        "atoms": canonical_atoms,
        "capacities": normalized_capacities,
        "source_snapshots": source_snapshots,
    }
    combined_diagnostics = [
        *_normalize_diagnostics(diagnostics),
        *lowering_diagnostics,
        *_execution_diagnostics(canonical_atoms, schedule),
        *validate_source_snapshots(provisional),
    ]
    normalized_diagnostics = _normalize_diagnostics(combined_diagnostics)
    execution_blockers = [
        {
            key: item[key]
            for key in ("code", "message", "source", "line", "atoms", "resources")
            if key in item
        }
        for item in normalized_diagnostics
        if item["severity"] == "error"
    ]
    plan_hash = canonical_plan_hash(
        canonical_atoms,
        normalized_capacities,
        source_snapshots,
        project_root=str(project_root),
        execution_contract={
            "execution_blockers": execution_blockers,
            "native_delegates": normalized_native,
        },
    )
    recommended_route = (
        "direct_atomic"
        if not execution_blockers
        else "native_delegate"
        if suggestions["native_delegate"]
        else "blocked"
    )
    return {
        **provisional,
        "plan_hash": plan_hash,
        "diagnostics": normalized_diagnostics,
        "native_delegates": normalized_native,
        "relaxation_candidates": normalized_relaxations,
        "execution_eligible": not execution_blockers,
        "execution_blockers": execution_blockers,
        "recommended_route": recommended_route,
        "schedule": schedule,
        "optimization_suggestions": suggestions,
        "fusion_suggestions": suggestions["fusion"],
        "split_suggestions": suggestions["split"],
        "native_delegate_suggestions": suggestions["native_delegate"],
        "snapshot_validation_required_before_execution": bool(source_snapshots),
    }


def finalize_atomic_plan(
    raw_atoms: Any,
    project: Path | str,
    *,
    capacities: Any = None,
    snapshots: Any = None,
    diagnostics: Any = None,
    native_delegates: Any = None,
    relaxation_candidates: Any = None,
) -> dict[str, Any]:
    """Synonym for compile_atomic_plan used by frontend finalization code."""
    return compile_atomic_plan(
        raw_atoms,
        project,
        capacities=capacities,
        snapshots=snapshots,
        diagnostics=diagnostics,
        native_delegates=native_delegates,
        relaxation_candidates=relaxation_candidates,
    )


finalize_plan = finalize_atomic_plan
