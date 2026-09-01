#!/usr/bin/env python3
"""Fail-closed static frontends for the AtomLane Atom IR.

Supported inputs are intentionally narrow.  Syntax outside the documented
POSIX-lite/package/Make/Compose subset is preserved as an opaque island with a
diagnostic; the compiler never silently drops source semantics.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atom_engine import (
    MAX_ATOMS,
    MAX_COMMAND_CHARS,
    MAX_RECURSION,
    MAX_SOURCE_BYTES,
    AtomError,
    _bounded_text,
    _normalize_resource,
    _slug,
)
from python_static_effects import infer_python_cli_accesses

MAX_SHELL_SEGMENTS = 128
MAX_PACKAGE_SCRIPTS = 256
MAX_PACKAGE_DEPTH = 8
MAX_MAKE_TARGETS = 1024


def _strict_read_text(path: Path, label: str) -> str:
    """Read one bounded UTF-8 source without replacement-character recovery."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AtomError(f"cannot read {label}: {exc}") from exc
    if len(raw) > MAX_SOURCE_BYTES:
        raise AtomError(f"{label} exceeds the {MAX_SOURCE_BYTES}-byte static parsing limit")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AtomError(f"{label} is not valid UTF-8") from exc


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AtomError(f"duplicate JSON key is not accepted by the static frontend: {key}")
        result[key] = value
    return result


@dataclass
class Fragment:
    roots: list[str] = field(default_factory=list)
    terminals: list[str] = field(default_factory=list)
    atoms: list[str] = field(default_factory=list)


@dataclass
class ShellSegment:
    text: str
    connector_before: str | None
    line: int
    dynamic: bool = False


class Compilation:
    def __init__(self, project: Path) -> None:
        self.project = project.resolve()
        self.atoms: list[dict[str, Any]] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.native_delegates: list[dict[str, Any]] = []
        self.relaxation_candidates: list[dict[str, Any]] = []
        self._ids: set[str] = set()

    def snapshot(self, path: Path) -> dict[str, Any]:
        path = path.resolve(strict=False)
        try:
            relative = path.relative_to(self.project).as_posix()
        except ValueError:
            relative = str(path)
        if relative in self.snapshots:
            return self.snapshots[relative]
        raw = path.read_bytes()
        if len(raw) > MAX_SOURCE_BYTES:
            raise AtomError(f"source snapshot exceeds {MAX_SOURCE_BYTES} bytes: {path}")
        item = {
            "path": relative,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        self.snapshots[relative] = item
        return item

    def diagnostic(
        self,
        code: str,
        message: str,
        *,
        severity: str = "warning",
        source: str | None = None,
        line: int | None = None,
        symbol: str | None = None,
    ) -> None:
        self.diagnostics.append(
            {
                "code": code,
                "severity": severity,
                "message": message,
                "source": source,
                "line": line,
                "symbol": symbol,
            }
        )

    def unique_id(self, proposed: str) -> str:
        base = _slug(proposed)
        candidate = base
        suffix = 2
        while candidate in self._ids:
            tail = f"-{suffix}"
            candidate = base[: 160 - len(tail)] + tail
            suffix += 1
        self._ids.add(candidate)
        return candidate

    def emit(self, raw: dict[str, Any]) -> str:
        if len(self.atoms) >= MAX_ATOMS:
            raise AtomError(f"compiled atom graph exceeds the {MAX_ATOMS}-atom limit")
        raw = dict(raw)
        raw["id"] = self.unique_id(raw.get("id", "atom"))
        self.atoms.append(raw)
        return raw["id"]

    def result(self) -> dict[str, Any]:
        return {
            "atoms": self.atoms,
            "diagnostics": self.diagnostics,
            "snapshots": [self.snapshots[key] for key in sorted(self.snapshots)],
            "native_delegates": self.native_delegates,
            "relaxation_candidates": self.relaxation_candidates,
        }


def _source_label(path: Path, project: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(project).as_posix()
    except ValueError:
        return str(path.resolve(strict=False))


def _add_dependencies(compilation: Compilation, atom_ids: list[str], dependencies: list[dict[str, str]]) -> None:
    if not dependencies:
        return
    by_id = {atom["id"]: atom for atom in compilation.atoms}
    for atom_id in atom_ids:
        atom = by_id[atom_id]
        existing = atom.setdefault("dependencies", [])
        for dependency in dependencies:
            if dependency not in existing:
                existing.append(dict(dependency))


def _scan_shell_list(command: str) -> tuple[list[ShellSegment], str | None]:
    """Split a conservative top-level `&&`/`;`/newline list.

    Pipelines, background execution, OR lists, unbalanced syntax, and shell
    control structures are returned as one opaque region.  Parenthesized and
    command-substitution contents are tracked so their connectors are never
    mistaken for top-level separators.
    """
    if len(command) > MAX_COMMAND_CHARS:
        return [], "BUDGET_EXCEEDED"
    segments: list[ShellSegment] = []
    buffer: list[str] = []
    connector_before: str | None = None
    quote: str | None = None
    escaped = False
    dynamic_segment = False
    line = 1
    segment_line = 1
    index = 0

    def flush() -> None:
        nonlocal buffer, connector_before, dynamic_segment, segment_line
        text = "".join(buffer).strip()
        if text:
            segments.append(ShellSegment(text, connector_before, segment_line, dynamic_segment))
            connector_before = None
        buffer = []
        dynamic_segment = False
        segment_line = line

    while index < len(command):
        char = command[index]
        next_char = command[index + 1] if index + 1 < len(command) else ""
        if escaped:
            buffer.append(char)
            escaped = False
            if char == "\n":
                line += 1
            index += 1
            continue
        if char == "\\" and quote != "'":
            buffer.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            buffer.append(char)
            if char == quote:
                quote = None
            elif quote == '"' and (char == "`" or (char == "$" and next_char == "(")):
                # Correctly finding the end of a nested command substitution
                # requires the full shell grammar.  Do not scan across it with
                # the POSIX-lite frontend: an embedded semicolon or control
                # operator must never be mistaken for a top-level separator.
                return [], "UNSUPPORTED_DYNAMIC_SHELL"
            if char == "\n":
                line += 1
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == "`" or (char == "$" and next_char == "(") or (char in {"<", ">"} and next_char == "("):
            return [], "UNSUPPORTED_DYNAMIC_SHELL"
        if char in {"(", ")"}:
            # Subshells, function declarations, and arithmetic commands are
            # opaque islands.  Tracking parentheses alone is unsound because
            # their quoting and control-flow rules are context dependent.
            return [], "UNSUPPORTED_CONTROL_STRUCTURE"
        if char == "#" and (index == 0 or command[index - 1].isspace() or command[index - 1] in ";&|"):
            # A shell comment begins only at the start of a word.  Drop the
            # comment body but leave the newline to preserve list sequencing.
            while index < len(command) and command[index] != "\n":
                index += 1
            continue
        if char == "|":
            return [], "UNSUPPORTED_PIPELINE_OR_OR_LIST"
        if char == "&" and next_char == "&":
            if not "".join(buffer).strip():
                return [], "INVALID_SHELL_LIST"
            flush()
            connector_before = "success"
            index += 2
            continue
        if char == "&" and not (buffer and buffer[-1] in {">", "<"}):
            return [], "UNSUPPORTED_BACKGROUND_EXECUTION"
        if char == ";":
            if not "".join(buffer).strip():
                return [], "INVALID_SHELL_LIST"
            flush()
            connector_before = "order"
            index += 1
            continue
        if char == "\n":
            if "".join(buffer).strip():
                flush()
                connector_before = "order"
            line += 1
            segment_line = line
            index += 1
            continue
        buffer.append(char)
        if char == "\n":
            line += 1
        index += 1
    if quote or escaped or (connector_before == "success" and not "".join(buffer).strip()):
        return [], "UNBALANCED_SHELL"
    flush()
    if len(segments) > MAX_SHELL_SEGMENTS:
        return [], "BUDGET_EXCEEDED"
    if not segments:
        return [], "EMPTY_COMMAND"
    first_words = []
    for segment in segments:
        try:
            words = shlex.split(segment.text, posix=True)
        except ValueError:
            return [], "UNBALANCED_SHELL"
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", words[0]):
            words.pop(0)
        first_words.append(words[0] if words else "")
    shell_state = {"cd", "export", "unset", "set", "umask", "trap", "source", ".", "exec"}
    control_words = {
        "if", "then", "else", "elif", "fi", "for", "while", "until", "case", "esac",
        "select", "function", "{", "}", "do", "done", "in", "[[", "((", "!", "time",
    }
    if any(word in control_words for word in first_words):
        return [], "UNSUPPORTED_CONTROL_STRUCTURE"
    if any(word in shell_state for word in first_words) and len(segments) > 1:
        return [], "SHELL_STATE_BARRIER"
    if "<<" in command:
        return [], "UNSUPPORTED_HEREDOC"
    return segments, None


def _simple_argv(command: str) -> tuple[list[str] | None, dict[str, str]]:
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return None, {}
    if not words:
        return None, {}
    if re.search(r"(?:^|[^\\])[$*?\[\]{}~<>]", command):
        return None, {}
    environment: dict[str, str] = {}
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", words[0]):
        key, value = words.pop(0).split("=", 1)
        environment[key] = value
    return (words or None), environment


def _flag_value(argv: list[str], *names: str) -> list[str]:
    values: list[str] = []
    for index, item in enumerate(argv):
        if item in names and index + 1 < len(argv):
            values.append(argv[index + 1])
        for name in names:
            prefix = name + "="
            if item.startswith(prefix):
                values.append(item[len(prefix):])
    return values


def _native_parallelism(argv: list[str] | None) -> dict[str, Any]:
    """Return a conservative inner-scheduler declaration for one argv.

    An explicit positive worker count is a bounded claim.  A recognized tool
    without a literal bound remains a native scheduler; malformed, dynamic,
    percentage, or unlimited values are never guessed into a numeric budget.
    """
    if not argv:
        return {"kind": "unknown", "tokens": None}
    executable = Path(argv[0]).name.lower()
    native = {
        "make", "gmake", "ninja", "pytest", "jest", "vitest", "xargs",
        "turbo", "nx", "lerna", "docker-compose",
    }
    if executable == "docker" and len(argv) > 1 and argv[1] == "compose":
        native.add("docker")
    if executable not in native:
        return {"kind": "unknown", "tokens": None}

    flags: tuple[str, ...]
    if executable in {"make", "gmake", "ninja"}:
        flags = ("-j", "--jobs")
    elif executable == "pytest":
        flags = ("-n", "--numprocesses")
    elif executable in {"jest", "vitest"}:
        flags = ("--maxWorkers", "--max-workers")
    elif executable == "xargs":
        flags = ("-P", "--max-procs")
    elif executable in {"docker", "docker-compose"}:
        flags = ("--parallel",)
    else:
        flags = ("--concurrency", "--parallel", "--max-parallel")

    for index, item in enumerate(argv):
        for flag in flags:
            value: str | None = None
            if item == flag and index + 1 < len(argv):
                value = argv[index + 1]
            elif item.startswith(flag + "="):
                value = item[len(flag) + 1:]
            elif len(flag) == 2 and item.startswith(flag) and item != flag:
                value = item[len(flag):]
            if value is not None and value.isdigit() and int(value) > 0:
                return {"kind": "bounded", "tokens": int(value)}
    return {"kind": "native_scheduler", "tokens": None}


def _git_root(cwd: Path) -> Path:
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return cwd


def _infer_command_atom(
    compilation: Compilation,
    command: str,
    *,
    atom_id: str,
    cwd: Path,
    source: str,
    line: int,
    symbol: str,
    adapter: str,
    dynamic: bool = False,
) -> str:
    argv, environment = _simple_argv(command)
    executable = Path(argv[0]).name.lower() if argv else ""
    lower = command.lower()
    operation_kind = "command"
    profile = "mixed"
    side_effect = False
    accesses: list[dict[str, str]] = []
    effects: list[dict[str, str]] = []
    blockers: list[str] = []
    effects_assurance = "partial"
    parse_assurance = "opaque" if dynamic else "exact"
    codegen = "exact_argv"
    internal_parallelism = _native_parallelism(argv)

    shell_needed = argv is None
    if shell_needed:
        argv = ["/bin/zsh", "-lc", command]
        codegen = "exact_argv"
    if dynamic:
        operation_kind = "opaque"
        effects.append({"domain": "unknown", "key": "host", "mode": "write"})
        side_effect = True
        effects_assurance = "unknown"
        blockers.extend(["UNSUPPORTED_DYNAMIC_SHELL", "UNKNOWN_EFFECT"])
    else:
        redirections = re.findall(r"(?:^|\s)(>>|>|<)\s*([^\s;&|]+)", command)
        for operator, target in redirections:
            if any(marker in target for marker in ("$", "`", "*", "?", "[", "{")):
                blockers.append("DYNAMIC_RESOURCE")
                continue
            accesses.append(
                {
                    "resource": target,
                    "mode": "read" if operator == "<" else "append" if operator == ">>" else "overwrite",
                }
            )
            if operator != "<":
                side_effect = True

        read_tools = {"rg", "grep", "find", "ls", "stat", "cat", "head", "tail", "wc", "pwd", "diff", "cmp"}
        if executable in read_tools:
            operation_kind = "read"
            profile = "io"
            effects_assurance = "complete_static" if accesses else "partial"
        if executable in {"pytest", "vitest", "jest"} or " test" in f" {lower}":
            operation_kind = "test"
            profile = "cpu"
            if internal_parallelism["kind"] == "unknown" and (
                executable in {"pytest", "vitest", "jest"}
                or any(name in lower for name in ("vitest", "jest", "pytest"))
            ):
                internal_parallelism = {"kind": "native_scheduler", "tokens": None}
        if executable in {"cc", "clang", "gcc", "swiftc", "rustc"} or "next build" in lower or "tsc " in lower:
            operation_kind = "build"
            profile = "cpu"
            side_effect = True
        if executable == "latexmk":
            operation_kind = "build"
            profile = "cpu"
            inputs = [item for item in argv[1:] if item.endswith((".tex", ".ltx"))]
            output_dirs = _flag_value(argv, "-output-directory", "--output-directory") or ["."]
            for input_name in inputs:
                accesses.append({"resource": input_name, "mode": "read"})
                stem = Path(input_name).stem
                accesses.append({"resource": f"latex-job:{(cwd / output_dirs[-1]).resolve(strict=False)}:{stem}", "mode": "overwrite"})
            side_effect = True
            effects_assurance = "complete_static" if inputs else "partial"
        if executable == "git":
            subcommand = next((item for item in argv[1:] if not item.startswith("-")), "")
            root = _git_root(cwd)
            if subcommand in {"status", "diff", "log", "show", "rev-parse", "ls-files"}:
                operation_kind = "read"
                effects.append({"domain": "git", "key": str(root), "mode": "read"})
                effects_assurance = "complete_static"
            else:
                operation_kind = "mutation"
                side_effect = True
                effects.append({"domain": "git", "key": str(root), "mode": "transaction"})
                effects_assurance = "complete_static"
        if executable in {"rm", "mv", "cp", "touch", "mkdir", "install", "tee"}:
            operation_kind = "mutation"
            side_effect = True
            paths = [item for item in argv[1:] if not item.startswith("-")]
            mode = "delete" if executable == "rm" else "create" if executable in {"touch", "mkdir"} else "overwrite"
            for path in paths:
                accesses.append({"resource": path, "mode": mode})
            effects_assurance = "complete_static" if paths else "unknown"
        if re.search(r"(?:npm|pnpm|yarn|bun|pip|uv|poetry)\s+(?:install|add|remove|update|sync)(?:\s|$)", lower):
            operation_kind = "mutation"
            side_effect = True
            effects.append({"domain": "dependency-environment", "key": str(cwd), "mode": "transaction"})
            effects_assurance = "complete_static"
        if re.search(r"(?:prisma\s+(?:migrate|db\s+(?:push|seed))|alembic\s+upgrade|manage\.py\s+migrate)", lower):
            operation_kind = "mutation"
            side_effect = True
            effects.append({"domain": "database", "key": "default", "mode": "transaction"})
            effects_assurance = "partial"
        if re.search(r"(?:docker\s+compose|docker-compose)\s+(?:up|down|start|stop|restart|rm)", lower):
            operation_kind = "mutation"
            side_effect = True
            effects.append({"domain": "compose-project", "key": str(cwd), "mode": "transaction"})
            effects_assurance = "partial"

        if argv:
            for output in _flag_value(argv, "-o", "--output", "--report", "--summary", "--output-file"):
                accesses.append({"resource": output, "mode": "create"})
                side_effect = True
            for input_path in _flag_value(argv, "-i", "--input", "--input-file"):
                accesses.append({"resource": input_path, "mode": "read"})
            if any(item == "--fix" for item in argv):
                side_effect = True
                operation_kind = "mutation"
                accesses.append({"resource": str(cwd), "mode": "overwrite"})
        if side_effect and not accesses and not effects:
            effects.append({"domain": "unknown", "key": "host", "mode": "write"})
            effects_assurance = "unknown"
            blockers.append("UNKNOWN_EFFECT")
        elif effects_assurance == "partial":
            blockers.append("PARTIAL_EFFECT_MODEL")

    semantics = {
        "idempotent": True if operation_kind in {"read", "test", "build"} and effects_assurance != "unknown" else None,
        "retryable": True if operation_kind == "read" else None,
        "deterministic": True if operation_kind == "read" else None,
        "cacheable": False,
        "commutative": False,
        "cancel_safe": True if operation_kind == "read" else None,
        "splittable": False if operation_kind in {"opaque", "mutation"} else None,
        "reorderable": "unknown",
    }
    return compilation.emit(
        {
            "id": atom_id,
            "operation": {
                "kind": operation_kind,
                "argv": argv,
                "command": command,
                "cwd": str(cwd),
                "env": environment,
                "completion": "process_exit",
                "internal_parallelism": internal_parallelism,
            },
            "accesses": accesses,
            "effects": effects,
            "profile": profile,
            "side_effect": side_effect,
            "semantics": semantics,
            "assurance": {
                "parse": parse_assurance,
                "control": "exact" if not dynamic else "unknown",
                "effects": effects_assurance,
                "codegen": codegen,
                "rank": 1.0 if parse_assurance == "exact" and effects_assurance in {"complete_static", "complete_declared"} else 0.45,
                "blockers": blockers,
            },
            "provenance": {
                "adapter": adapter,
                "source": source,
                "symbol": symbol,
                "line": line,
                "confidence": 1.0 if not dynamic else 0.4,
            },
            "opaque_reason": "dynamic or unsupported shell syntax" if dynamic else None,
        }
    )


Resolver = Callable[[str, list[dict[str, str]], int], Fragment | None]


def compile_shell(
    compilation: Compilation,
    command: str,
    *,
    prefix: str,
    cwd: Path,
    source: str,
    symbol: str,
    line: int = 1,
    inherited: list[dict[str, str]] | None = None,
    resolver: Resolver | None = None,
) -> Fragment:
    segments, error = _scan_shell_list(command)
    if error:
        compilation.diagnostic(error, "shell region was preserved as one opaque atom", source=source, line=line, symbol=symbol)
        atom_id = _infer_command_atom(
            compilation,
            command,
            atom_id=f"{prefix}-opaque",
            cwd=cwd,
            source=source,
            line=line,
            symbol=symbol,
            adapter="shell",
            dynamic=True,
        )
        _add_dependencies(compilation, [atom_id], inherited or [])
        return Fragment([atom_id], [atom_id], [atom_id])

    all_ids: list[str] = []
    roots: list[str] = []
    previous_terminals: list[str] = []
    for index, segment in enumerate(segments):
        dependency_kind = segment.connector_before or "success"
        dependencies = list(inherited or []) if index == 0 else [
            {"atom": atom_id, "kind": dependency_kind} for atom_id in previous_terminals
        ]
        fragment = resolver(segment.text, dependencies, line + segment.line - 1) if resolver else None
        if fragment is None:
            atom_id = _infer_command_atom(
                compilation,
                segment.text,
                atom_id=f"{prefix}-{index + 1}",
                cwd=cwd,
                source=source,
                line=line + segment.line - 1,
                symbol=symbol,
                adapter="shell",
                dynamic=segment.dynamic,
            )
            _add_dependencies(compilation, [atom_id], dependencies)
            fragment = Fragment([atom_id], [atom_id], [atom_id])
        if index == 0:
            roots.extend(fragment.roots)
        previous_terminals = fragment.terminals
        all_ids.extend(fragment.atoms)
    if len(segments) > 1 and all(segment.connector_before in {None, "success"} for segment in segments):
        compilation.relaxation_candidates.append(
            {
                "kind": "success_chain_diagnostic_fanout",
                "atoms": all_ids,
                "semantic_change": "would replace fail-fast success control with aggregate diagnostics",
                "automatic": False,
            }
        )
    return Fragment(roots, previous_terminals, all_ids)


class PackageScriptCompiler:
    def __init__(self, compilation: Compilation, package_file: Path) -> None:
        self.compilation = compilation
        self.package_file = package_file.resolve()
        self.cwd = self.package_file.parent
        self.source = _source_label(self.package_file, compilation.project)
        compilation.snapshot(self.package_file)
        try:
            payload = json.loads(
                _strict_read_text(self.package_file, self.source),
                object_pairs_hook=_json_object_without_duplicates,
            )
        except json.JSONDecodeError as exc:
            raise AtomError(f"invalid JSON in {self.source}: {exc}") from exc
        if not isinstance(payload, dict):
            raise AtomError(f"{self.source} must contain a JSON object")
        scripts = payload.get("scripts", {})
        if not isinstance(scripts, dict) or len(scripts) > MAX_PACKAGE_SCRIPTS:
            raise AtomError(f"{self.source} scripts must be an object with at most {MAX_PACKAGE_SCRIPTS} entries")
        if not all(
            isinstance(name, str)
            and name
            and "\x00" not in name
            and len(name) <= 512
            and isinstance(command, str)
            and "\x00" not in command
            and len(command) <= MAX_COMMAND_CHARS
            for name, command in scripts.items()
        ):
            raise AtomError(f"every {self.source} script must have a bounded string name and command")
        self.scripts = dict(scripts)
        package_manager = payload.get("packageManager")
        manager_from_manifest = package_manager.split("@", 1)[0] if isinstance(package_manager, str) else None
        lock_managers = [
            manager for manager, filename in (
                ("pnpm", "pnpm-lock.yaml"),
                ("yarn", "yarn.lock"),
                ("bun", "bun.lockb"),
                ("bun", "bun.lock"),
                ("npm", "package-lock.json"),
            )
            if (self.cwd / filename).is_file()
        ]
        self.manager_exact = False
        if manager_from_manifest in {"npm", "pnpm", "yarn", "bun"}:
            self.manager = manager_from_manifest
            self.manager_exact = True
            if lock_managers and any(item != self.manager for item in lock_managers):
                compilation.diagnostic(
                    "PACKAGE_MANAGER_CONFLICT",
                    "packageManager disagrees with one or more lockfiles; manifest value retained for static provenance",
                    source=self.source,
                )
        elif len(set(lock_managers)) == 1:
            self.manager = lock_managers[0]
            self.manager_exact = True
        else:
            self.manager = "npm"
            if len(set(lock_managers)) > 1:
                compilation.diagnostic(
                    "PACKAGE_MANAGER_CONFLICT",
                    "multiple package-manager lockfiles prevent an exact manager inference",
                    source=self.source,
                )
            else:
                compilation.diagnostic(
                    "PACKAGE_MANAGER_UNKNOWN",
                    "no packageManager or unique lockfile was found; npm is display-only and lifecycle expansion is disabled",
                    source=self.source,
                )

    @staticmethod
    def _nested_script(command: str) -> tuple[str, bool, tuple[str, ...]] | None:
        """Recognize only exact package-script invocations.

        The boolean indicates whether npm's documented pre/main/post lifecycle
        applies. Literal arguments after `--` are retained and may be lowered
        only when the target script body is itself one exact simple command.
        """
        try:
            words = shlex.split(command, posix=True)
        except ValueError:
            return None
        if any(token in command for token in ("$", "`", "|", "&", ";", "<", ">")):
            return None
        executable = Path(words[0]).name if words else ""
        if executable == "npm":
            if len(words) >= 2 and words[1] == "test":
                forwarded = words[2:]
                if forwarded and forwarded[0] != "--":
                    return None
                forwarded = forwarded[1:] if forwarded else []
                return "test", True, tuple(forwarded)
            if len(words) >= 3 and words[1] in {"run", "run-script"}:
                forwarded = words[3:]
                if forwarded and forwarded[0] != "--":
                    return None
                forwarded = forwarded[1:] if forwarded else []
                return words[2], True, tuple(forwarded)
        if executable in {"pnpm", "bun"} and len(words) >= 3 and words[1] in {"run", "run-script"}:
            forwarded = words[3:]
            if forwarded[:1] == ["--"]:
                forwarded = forwarded[1:]
            return words[2], False, tuple(forwarded)
        if executable == "yarn" and len(words) >= 3 and words[1] in {"run", "run-script"}:
            forwarded = words[3:]
            if forwarded[:1] == ["--"]:
                forwarded = forwarded[1:]
            return words[2], False, tuple(forwarded)
        return None

    def _can_forward_literal_args(self, script: str, forwarded: tuple[str, ...]) -> bool:
        if not forwarded or script not in self.scripts:
            return True
        command = self.scripts[script]
        segments, error = _scan_shell_list(command)
        if error or len(segments) != 1:
            return False
        argv, _ = _simple_argv(segments[0].text)
        return argv is not None and len(command) + len(shlex.join(forwarded)) + 1 <= MAX_COMMAND_CHARS

    def compile(
        self,
        script: str,
        *,
        inherited: list[dict[str, str]] | None = None,
        stack: tuple[str, ...] = (),
        depth: int = 0,
        lifecycle: bool | None = None,
        forwarded_args: tuple[str, ...] = (),
    ) -> Fragment:
        if depth > MAX_PACKAGE_DEPTH:
            return self._opaque(script, inherited or [], "BUDGET_EXCEEDED", stack)
        if script in stack:
            return self._opaque(script, inherited or [], "CYCLE_COLLAPSED", stack)
        if script not in self.scripts:
            return self._opaque(script, inherited or [], "UNKNOWN_PACKAGE_SCRIPT", stack)
        apply_lifecycle = self.manager_exact and self.manager == "npm" if lifecycle is None else lifecycle
        sequence = [script]
        if apply_lifecycle:
            sequence = [name for name in (f"pre{script}", script, f"post{script}") if name in self.scripts]
        all_ids: list[str] = []
        roots: list[str] = []
        terminals: list[str] = []
        for sequence_index, current in enumerate(sequence):
            deps = list(inherited or []) if sequence_index == 0 else [
                {"atom": atom_id, "kind": "success"} for atom_id in terminals
            ]

            def resolver(segment: str, segment_deps: list[dict[str, str]], line: int) -> Fragment | None:
                nested_call = self._nested_script(segment)
                if nested_call is None:
                    return None
                nested, nested_lifecycle, nested_args = nested_call
                if not self._can_forward_literal_args(nested, nested_args):
                    return None
                return self.compile(
                    nested,
                    inherited=segment_deps,
                    stack=stack + (script,),
                    depth=depth + 1,
                    lifecycle=nested_lifecycle,
                    forwarded_args=nested_args,
                )

            command = self.scripts[current]
            if current == script and forwarded_args:
                command = command + " " + shlex.join(forwarded_args)
            fragment = compile_shell(
                self.compilation,
                command,
                prefix=f"pkg-{_slug(current)}",
                cwd=self.cwd,
                source=self.source,
                symbol=f"scripts.{current}",
                inherited=deps,
                resolver=resolver,
            )
            if sequence_index == 0:
                roots = fragment.roots
            terminals = fragment.terminals
            all_ids.extend(fragment.atoms)
        return Fragment(roots, terminals, all_ids)

    def _opaque(
        self,
        script: str,
        inherited: list[dict[str, str]],
        code: str,
        stack: tuple[str, ...],
    ) -> Fragment:
        self.compilation.diagnostic(
            code,
            "package script expansion was preserved as an opaque native command",
            source=self.source,
            symbol=f"scripts.{script}",
        )
        atom_id = self.compilation.emit(
            {
                "id": f"pkg-{_slug(script)}-opaque",
                "operation": {
                    "kind": "opaque",
                    "argv": [self.manager, "run", script],
                    "cwd": str(self.cwd),
                    "completion": "process_exit",
                    "internal_parallelism": {"kind": "unknown", "tokens": None},
                },
                "dependencies": inherited,
                "side_effect": True,
                "effects": [{"domain": "unknown", "key": "host", "mode": "write"}],
                "semantics": {"splittable": False, "reorderable": "forbidden"},
                "assurance": {
                    "parse": "opaque",
                    "control": "unknown",
                    "effects": "unknown",
                    "codegen": "exact_argv" if self.manager_exact else "opaque",
                    "rank": 0.2,
                    "blockers": list(dict.fromkeys([
                        code,
                        "UNKNOWN_EFFECT",
                        *([] if self.manager_exact else ["PACKAGE_MANAGER_UNKNOWN"]),
                    ])),
                },
                "provenance": {
                    "adapter": "package_json",
                    "source": self.source,
                    "symbol": f"scripts.{script}",
                    "confidence": 1.0 if self.manager_exact else 0.4,
                },
                "opaque_reason": code,
            }
        )
        return Fragment([atom_id], [atom_id], [atom_id])


@dataclass
class MakeRule:
    target: str
    prerequisites: list[str] = field(default_factory=list)
    order_only: list[str] = field(default_factory=list)
    recipes: list[tuple[int, str]] = field(default_factory=list)
    unsafe_codes: list[str] = field(default_factory=list)
    line: int = 1


def _literal_python_recipe_calls(recipe: str, cwd: Path) -> list[tuple[Path, list[str]]]:
    """Find bounded literal Python script calls without expanding Make syntax."""
    calls: list[tuple[Path, list[str]]] = []
    for logical_line in recipe.replace("\\\n", " ").splitlines():
        line = logical_line.strip().lstrip("@-+").strip()
        if not line or any(operator in line for operator in ("&&", "||", "|", ";", "`", "$(shell")):
            continue
        try:
            words = shlex.split(line, posix=True)
        except ValueError:
            continue
        script_index = next(
            (
                index
                for index, word in enumerate(words)
                if word.endswith(".py") and "$" not in word and "{" not in word
            ),
            None,
        )
        if script_index is None:
            continue
        # The interpreter may be literal or a non-executed Make variable such
        # as $(PYTHON); locating the source file never evaluates it.
        script = Path(words[script_index]).expanduser()
        if not script.is_absolute():
            script = cwd / script
        script = script.resolve(strict=False)
        try:
            script.relative_to(cwd.resolve())
        except ValueError:
            continue
        if not script.is_file():
            continue
        arguments = [
            word for word in words[script_index + 1:]
            if "$" not in word and "`" not in word
        ]
        calls.append((script, arguments))
    return calls


class MakefileCompiler:
    def __init__(self, compilation: Compilation, makefile: Path) -> None:
        self.compilation = compilation
        self.makefile = makefile.resolve()
        self.cwd = self.makefile.parent
        self.source = _source_label(self.makefile, compilation.project)
        compilation.snapshot(self.makefile)
        self.text = _strict_read_text(self.makefile, self.source)
        self.rules: dict[str, MakeRule] = {}
        self.phony: set[str] = set()
        self.not_parallel = False
        self.one_shell = False
        self.global_taints: set[str] = set()
        self._cache: dict[str, Fragment] = {}
        self._parse()

    def _parse(self) -> None:
        if "$(shell" in self.text or "${shell" in self.text:
            self.compilation.diagnostic(
                "DYNAMIC_MAKE_EXPANSION",
                "Makefile contains shell expansion; affected recipes remain conservative and are never evaluated",
                source=self.source,
            )
        lines = self.text.splitlines()
        current_targets: list[str] = []
        in_define = False
        index = 0
        while index < len(lines):
            line_number = index + 1
            line = lines[index]
            stripped_line = line.strip()
            if in_define:
                if stripped_line == "endef":
                    in_define = False
                index += 1
                continue
            if stripped_line.startswith("define "):
                in_define = True
                self.global_taints.add("UNSUPPORTED_MAKE_DEFINE")
                self.compilation.diagnostic(
                    "UNSUPPORTED_MAKE_DEFINE",
                    "Make define blocks are not expanded by the static frontend",
                    source=self.source,
                    line=line_number,
                )
                index += 1
                continue
            if line.startswith("\t"):
                if current_targets:
                    recipe = line[1:]
                    while recipe.endswith("\\") and index + 1 < len(lines):
                        index += 1
                        recipe += "\n" + lines[index].lstrip("\t")
                    for target in current_targets:
                        self.rules[target].recipes.append((line_number, recipe))
                else:
                    self.global_taints.add("ORPHAN_MAKE_RECIPE")
                    self.compilation.diagnostic(
                        "ORPHAN_MAKE_RECIPE",
                        "recipe text was not attached to an exact explicit target",
                        source=self.source,
                        line=line_number,
                    )
                index += 1
                continue
            current_targets = []
            logical = line
            while logical.rstrip().endswith("\\") and index + 1 < len(lines):
                logical = logical.rstrip()[:-1] + " " + lines[index + 1].strip()
                index += 1
            stripped = logical.strip()
            if not stripped or stripped.startswith("#"):
                index += 1
                continue
            if stripped.startswith(".RECIPEPREFIX"):
                self.global_taints.add("UNSUPPORTED_RECIPE_PREFIX")
                self.compilation.diagnostic(
                    "UNSUPPORTED_RECIPE_PREFIX",
                    "custom Make recipe prefixes are not statically lowered",
                    source=self.source,
                    line=line_number,
                )
                index += 1
                continue
            assignment = re.match(
                r"^(?:(?:override|export|private|unexport)\s+)*[A-Za-z0-9_.-]+\s*(?::=|::=|\+=|\?=|!=|=)",
                stripped,
            )
            if assignment:
                if "!=" in assignment.group(0) or "$(shell" in stripped or "${shell" in stripped:
                    self.compilation.diagnostic(
                        "DYNAMIC_MAKE_EXPANSION",
                        "shell-capable Make assignment was recorded but never evaluated",
                        source=self.source,
                        line=line_number,
                    )
                index += 1
                continue
            if stripped.startswith(("include ", "-include ")):
                self.global_taints.add("UNSUPPORTED_MAKE_INCLUDE")
                self.compilation.diagnostic(
                    "UNSUPPORTED_MAKE_INCLUDE",
                    "Make include was not expanded by the bounded static frontend",
                    source=self.source,
                    line=line_number,
                )
                index += 1
                continue
            if re.match(r"^(?:ifeq|ifneq|ifdef|ifndef|else|endif)\b", stripped):
                self.global_taints.add("UNSUPPORTED_MAKE_CONDITIONAL")
                self.compilation.diagnostic(
                    "UNSUPPORTED_MAKE_CONDITIONAL",
                    "conditional Make parsing is not evaluated by the static frontend",
                    source=self.source,
                    line=line_number,
                )
                index += 1
                continue
            if "::" in logical.split("#", 1)[0] or "&:" in logical.split("#", 1)[0]:
                self.compilation.diagnostic(
                    "UNSUPPORTED_MAKE_RULE",
                    "double-colon or grouped-target rules remain native/opaque",
                    source=self.source,
                    line=line_number,
                )
                self.global_taints.add("UNSUPPORTED_MAKE_RULE")
                index += 1
                continue
            match = re.match(r"^([^:#=]+?)\s*:(?![=])\s*(.*)$", logical)
            if not match:
                self.global_taints.add("UNSUPPORTED_MAKE_SYNTAX")
                self.compilation.diagnostic(
                    "UNSUPPORTED_MAKE_SYNTAX",
                    "unrecognized top-level Make syntax prevents exact graph lowering",
                    source=self.source,
                    line=line_number,
                )
                index += 1
                continue
            targets = match.group(1).split()
            if any("%" in target or "$" in target for target in targets):
                self.compilation.diagnostic(
                    "UNSUPPORTED_MAKE_RULE",
                    "pattern or variable-expanded Make rule remains native/opaque",
                    source=self.source,
                    line=line_number,
                )
                self.global_taints.add("UNSUPPORTED_MAKE_RULE")
                index += 1
                continue
            rule_tail = match.group(2).split("#", 1)[0]
            prerequisite_text, separator, inline_recipe = rule_tail.partition(";")
            prerequisite_text = prerequisite_text.strip()
            normal_text, _, order_text = prerequisite_text.partition("|")
            prerequisites = normal_text.split()
            order_only = order_text.split()
            if targets == [".PHONY"]:
                self.phony.update(prerequisites)
                index += 1
                continue
            if targets == [".NOTPARALLEL"]:
                self.not_parallel = True
                index += 1
                continue
            if targets == [".ONESHELL"]:
                self.one_shell = True
                index += 1
                continue
            if targets == [".SECONDEXPANSION"]:
                self.global_taints.add("UNSUPPORTED_SECONDARY_EXPANSION")
                index += 1
                continue
            current_targets = []
            for target in targets:
                if target.startswith("."):
                    continue
                rule = self.rules.setdefault(target, MakeRule(target=target, line=line_number))
                rule.prerequisites.extend(item for item in prerequisites if item != ".WAIT")
                rule.order_only.extend(order_only)
                if len(targets) > 1:
                    rule.unsafe_codes.append("MULTI_TARGET_MAKE_RULE")
                if ".WAIT" in prerequisites:
                    rule.unsafe_codes.append("MAKE_WAIT_BARRIER")
                if any(any(marker in item for marker in ("$", "%", "*", "?", "[")) for item in prerequisites + order_only):
                    rule.unsafe_codes.append("DYNAMIC_MAKE_PREREQUISITE")
                if separator and inline_recipe.strip():
                    rule.recipes.append((line_number, inline_recipe.strip()))
                current_targets.append(target)
            index += 1
        if len(self.rules) > MAX_MAKE_TARGETS:
            raise AtomError(f"Makefile contains more than {MAX_MAKE_TARGETS} explicit targets")

    def _python_recipe_accesses(self, rule: MakeRule) -> list[dict[str, str]]:
        accesses: list[dict[str, str]] = []
        for line_number, recipe in rule.recipes:
            for script, arguments in _literal_python_recipe_calls(recipe, self.cwd):
                self.compilation.snapshot(script)
                inferred = infer_python_cli_accesses(script, arguments, self.cwd)
                for item in inferred.get("accesses", []):
                    if item not in accesses:
                        accesses.append(item)
                if inferred.get("accesses"):
                    self.compilation.diagnostics.append(
                        {
                            "code": "PYTHON_CLI_STATIC_EFFECTS",
                            "severity": "info",
                            "message": (
                                f"inferred {len(inferred['accesses'])} argparse artifact accesses "
                                "from literal defaults and selected AST sinks"
                            ),
                            "source": _source_label(script, self.compilation.project),
                            "line": line_number,
                            "symbol": rule.target,
                        }
                    )
                for message in inferred.get("diagnostics", []):
                    self.compilation.diagnostic(
                        "PYTHON_CLI_STATIC_ANALYSIS_SKIPPED",
                        message,
                        source=_source_label(script, self.compilation.project),
                        line=line_number,
                        symbol=rule.target,
                    )
        return accesses

    def _repair_prerequisite_dataflow(self, fragments: list[Fragment]) -> None:
        atom_ids = sorted({atom_id for fragment in fragments for atom_id in fragment.atoms})
        if len(atom_ids) < 2:
            return
        by_id = {atom["id"]: atom for atom in self.compilation.atoms if atom["id"] in atom_ids}

        def resource_key(atom: dict[str, Any], resource: str) -> str:
            return _normalize_resource(resource, Path(atom["operation"]["cwd"]))

        writers: dict[str, set[str]] = {}
        readers: dict[str, set[str]] = {}
        for atom_id, atom in by_id.items():
            for access in atom.get("accesses", []):
                key = resource_key(atom, access["resource"])
                if access["mode"] in {"create", "overwrite", "transaction", "delete"}:
                    writers.setdefault(key, set()).add(atom_id)
                elif access["mode"] in {"read", "snapshot"}:
                    readers.setdefault(key, set()).add(atom_id)

        def reaches(start: str, target: str, seen: set[str] | None = None) -> bool:
            if start == target:
                return True
            seen = set() if seen is None else seen
            if start in seen:
                return False
            seen.add(start)
            return any(
                edge["atom"] == target or reaches(edge["atom"], target, seen)
                for edge in by_id[start].get("dependencies", [])
                if edge["atom"] in by_id
            )

        for resource in sorted(set(writers) & set(readers)):
            producer_ids = sorted(writers[resource])
            if len(producer_ids) != 1:
                self.compilation.diagnostics.append(
                    {
                        "code": "AMBIGUOUS_MAKE_DATA_PRODUCER",
                        "severity": "warning",
                        "message": "multiple Make prerequisite atoms write one consumed artifact",
                        "atoms": producer_ids,
                        "resources": [resource],
                    }
                )
                continue
            producer = producer_ids[0]
            for consumer in sorted(readers[resource] - {producer}):
                if reaches(consumer, producer):
                    continue
                if reaches(producer, consumer):
                    self.compilation.diagnostics.append(
                        {
                            "code": "MAKE_DATAFLOW_CONTRADICTION",
                            "severity": "error",
                            "message": "static Python artifact flow contradicts the explicit Make order",
                            "atoms": [producer, consumer],
                            "resources": [resource],
                        }
                    )
                    continue
                by_id[consumer].setdefault("dependencies", []).append(
                    {"atom": producer, "kind": "data"}
                )
                self.compilation.diagnostics.append(
                    {
                        "code": "MAKE_DATAFLOW_REPAIRED",
                        "severity": "warning",
                        "message": "added a data edge omitted by sibling Make prerequisites",
                        "atoms": [producer, consumer],
                        "resources": [resource],
                    }
                )

    def compile(self, target: str, stack: tuple[str, ...] = ()) -> Fragment:
        if target in self._cache:
            return self._cache[target]
        if len(stack) > MAX_RECURSION:
            return self._opaque(target, "BUDGET_EXCEEDED")
        if target in stack:
            return self._opaque(target, "CYCLE_COLLAPSED")
        rule = self.rules.get(target)
        if rule is None:
            return self._opaque(target, "UNKNOWN_MAKE_TARGET")
        unsafe = list(dict.fromkeys([*sorted(self.global_taints), *rule.unsafe_codes]))
        if unsafe:
            return self._opaque(target, unsafe[0])
        normal_fragments = [
            self.compile(item, stack + (target,))
            for item in rule.prerequisites
            if item in self.rules
        ]
        order_fragments = [
            self.compile(item, stack + (target,))
            for item in rule.order_only
            if item in self.rules
        ]
        prerequisite_fragments = [*normal_fragments, *order_fragments]
        self._repair_prerequisite_dataflow(prerequisite_fragments)
        recipe_text = "\n".join(text for _, text in rule.recipes)
        accesses: list[dict[str, str]] = []
        effects: list[dict[str, str]] = []
        blockers = ["PARTIAL_EFFECT_MODEL"]
        accesses.extend(self._python_recipe_accesses(rule))
        for match in re.finditer(r"(?:--output|--report|--summary|-o)(?:=|\s+)['\"]?([^\s'\"\\]+)", recipe_text):
            accesses.append({"resource": match.group(1), "mode": "create"})
        for match in re.finditer(r"(?:--input|-i)(?:=|\s+)['\"]?([^\s'\"\\]+)", recipe_text):
            accesses.append({"resource": match.group(1), "mode": "read"})
        for prerequisite in [*rule.prerequisites, *rule.order_only]:
            if prerequisite not in self.rules:
                accesses.append({"resource": prerequisite, "mode": "read"})
        if rule.recipes and target not in self.phony and target:
            accesses.append({"resource": target, "mode": "create"})
        if re.search(r"(?:timing|benchmark|certificate|formal|prospective|rss)", target, re.IGNORECASE):
            effects.append({"domain": "host", "key": "timing-provenance", "mode": "lease"})
        if not recipe_text:
            recipe_text = f"make target barrier: {target}"
        dependencies = [
            {"atom": atom_id, "kind": "success"}
            for fragment in normal_fragments for atom_id in fragment.terminals
        ] + [
            {"atom": atom_id, "kind": "order"}
            for fragment in order_fragments for atom_id in fragment.terminals
        ]
        atom_id = self.compilation.emit(
            {
                "id": f"make-{_slug(target)}",
                "operation": {
                    "kind": "make_recipe" if rule.recipes else "opaque",
                    "command": recipe_text,
                    "cwd": str(self.cwd),
                    "completion": "process_exit",
                    "internal_parallelism": (
                        {"kind": "bounded", "tokens": 1}
                        if self.not_parallel else
                        {"kind": "native_scheduler", "tokens": None}
                    ),
                },
                "dependencies": dependencies,
                "accesses": accesses,
                "effects": effects,
                "profile": "cpu",
                "side_effect": bool(rule.recipes),
                "semantics": {
                    "idempotent": None,
                    "retryable": None,
                    "deterministic": None,
                    "cacheable": False,
                    "commutative": False,
                    "splittable": False,
                    "reorderable": "forbidden" if self.not_parallel else "proved",
                },
                "assurance": {
                    "parse": "conservative",
                    "control": "exact",
                    "effects": "partial",
                    "codegen": "opaque",
                    "rank": 0.55,
                    "blockers": blockers,
                },
                "provenance": {
                    "adapter": "makefile",
                    "source": self.source,
                    "symbol": target,
                    "line": rule.line,
                    "confidence": 1.0,
                },
                "opaque_reason": None if rule.recipes else "alias target barrier",
            }
        )
        all_ids = [atom_id]
        roots = []
        for fragment in prerequisite_fragments:
            all_ids.extend(fragment.atoms)
            roots.extend(fragment.roots)
        if not roots:
            roots = [atom_id]
        result = Fragment(list(dict.fromkeys(roots)), [atom_id], list(dict.fromkeys(all_ids)))
        self._cache[target] = result
        return result

    def _opaque(self, target: str, code: str) -> Fragment:
        self.compilation.diagnostic(
            code,
            "Make target was preserved as a native opaque delegate",
            source=self.source,
            symbol=target,
        )
        atom_id = self.compilation.emit(
            {
                "id": f"make-{_slug(target)}-opaque",
                "operation": {
                    "kind": "opaque",
                    "argv": ["make", "-f", str(self.makefile), target],
                    "cwd": str(self.cwd),
                    "completion": "process_exit",
                    "internal_parallelism": (
                        {"kind": "bounded", "tokens": 1}
                        if self.not_parallel else
                        {"kind": "native_scheduler", "tokens": None}
                    ),
                },
                "side_effect": True,
                "effects": [{"domain": "unknown", "key": "build-tree", "mode": "write"}],
                "semantics": {"splittable": False, "reorderable": "forbidden"},
                "assurance": {
                    "parse": "opaque",
                    "control": "unknown",
                    "effects": "unknown",
                    "codegen": "native_delegate",
                    "rank": 0.2,
                    "blockers": [code, "UNKNOWN_EFFECT"],
                },
                "provenance": {
                    "adapter": "makefile",
                    "source": self.source,
                    "symbol": target,
                    "confidence": 1.0,
                },
                "opaque_reason": code,
            }
        )
        return Fragment([atom_id], [atom_id], [atom_id])

    def delegate(self, target: str) -> None:
        self.compilation.native_delegates.append(
            {
                "kind": "make_native_graph",
                "argv": ["make", "-f", str(self.makefile), target],
                "cwd": str(self.cwd),
                "source": self.source,
                "symbol": target,
                "internal_parallelism": (
                    {"kind": "bounded", "tokens": 1}
                    if self.not_parallel else
                    {"kind": "native_scheduler", "tokens": None}
                ),
                "reason": "Make owns recipe shell semantics, implicit rules, and jobserver coordination.",
            }
        )


_RUBY_COMPOSE_PARSER = r'''
require "yaml"
require "json"

def string_array(value, label)
  return [] if value.nil?
  raise "#{label} must be an array" unless value.is_a?(Array)
  raise "#{label} is too large" if value.length > 128
  value.each do |item|
    raise "#{label} entries must be strings" unless item.is_a?(String)
  end
  value
end

def interpolation?(value)
  case value
  when String
    value.include?("${")
  when Array
    value.any? { |item| interpolation?(item) }
  when Hash
    value.any? { |key, item| interpolation?(key) || interpolation?(item) }
  else
    false
  end
end

def normalize_port(value, label)
  return value if value.is_a?(String)
  raise "#{label} long syntax must be a mapping" unless value.is_a?(Hash)
  target = value["target"]
  published = value["published"]
  protocol = value["protocol"]
  host_ip = value["host_ip"]
  raise "#{label}.target must be a string or integer" unless target.is_a?(String) || target.is_a?(Integer)
  unless published.nil? || published.is_a?(String) || published.is_a?(Integer)
    raise "#{label}.published must be a string or integer"
  end
  raise "#{label}.protocol must be a string" unless protocol.nil? || protocol.is_a?(String)
  raise "#{label}.host_ip must be a string" unless host_ip.nil? || host_ip.is_a?(String)
  {
    "target" => target,
    "published" => published,
    "protocol" => protocol || "tcp",
    "host_ip" => host_ip
  }
end

def normalize_mount(value, label)
  return value if value.is_a?(String)
  raise "#{label} long syntax must be a mapping" unless value.is_a?(Hash)
  out = {}
  ["type", "source", "target"].each do |key|
    item = value[key]
    raise "#{label}.#{key} must be a string" unless item.nil? || item.is_a?(String)
    out[key] = item unless item.nil?
  end
  read_only = value["read_only"]
  raise "#{label}.read_only must be boolean" unless read_only.nil? || read_only == true || read_only == false
  out["read_only"] = read_only unless read_only.nil?
  out
end

def reject_duplicate_yaml_keys(node, path = "$")
  case node
  when Psych::Nodes::Stream, Psych::Nodes::Document, Psych::Nodes::Sequence
    node.children.each_with_index do |child, index|
      reject_duplicate_yaml_keys(child, "#{path}[#{index}]")
    end
  when Psych::Nodes::Mapping
    seen = {}
    node.children.each_slice(2) do |key_node, value_node|
      raise "non-scalar YAML mapping key at #{path}" unless key_node.is_a?(Psych::Nodes::Scalar)
      key = key_node.value
      raise "duplicate YAML key #{key} at #{path}" if seen[key]
      seen[key] = true
      reject_duplicate_yaml_keys(value_node, "#{path}.#{key}")
    end
  end
end

data = STDIN.read
reject_duplicate_yaml_keys(Psych.parse_stream(data))
doc = YAML.safe_load(data, [], [], false) || {}
raise "top-level YAML must be a mapping" unless doc.is_a?(Hash)
raise "top-level name must be a string" unless doc["name"].nil? || doc["name"].is_a?(String)
raise "Compose include is not supported by the bounded frontend" if doc.key?("include")
services = doc["services"] || {}
raise "services must be a mapping" unless services.is_a?(Hash)
raise "too many services" if services.length > 256
out = {}
services.each do |name, raw|
  raise "service names must be strings" unless name.is_a?(String)
  raise "service #{name} must be a mapping" unless raw.is_a?(Hash)
  raise "service #{name} extends is not supported by the bounded frontend" if raw.key?("extends")
  deps = {}
  depends = raw["depends_on"] || {}
  if depends.is_a?(Array)
    raise "service #{name} has too many dependencies" if depends.length > 128
    depends.each do |item|
      raise "service #{name} dependency names must be strings" unless item.is_a?(String)
      deps[item] = {"condition" => "service_started", "required" => true, "restart" => false}
    end
  elsif depends.is_a?(Hash)
    raise "service #{name} has too many dependencies" if depends.length > 128
    depends.each do |dep, value|
      raise "service #{name} dependency names must be strings" unless dep.is_a?(String)
      raise "service #{name} dependency #{dep} must be a mapping" unless value.nil? || value.is_a?(Hash)
      condition = value.is_a?(Hash) ? value["condition"] : nil
      required = value.is_a?(Hash) ? value["required"] : nil
      restart = value.is_a?(Hash) ? value["restart"] : nil
      raise "dependency condition must be a string" unless condition.nil? || condition.is_a?(String)
      raise "dependency required must be boolean" unless required.nil? || required == true || required == false
      raise "dependency restart must be boolean" unless restart.nil? || restart == true || restart == false
      deps[dep] = {
        "condition" => condition || "service_started",
        "required" => required.nil? ? true : required,
        "restart" => restart.nil? ? false : restart
      }
    end
  else
    raise "service #{name} depends_on must be an array or mapping"
  end
  ports = raw["ports"] || []
  raise "service #{name} ports must be an array" unless ports.is_a?(Array)
  raise "service #{name} has too many ports" if ports.length > 128
  mounts = raw["volumes"] || []
  raise "service #{name} volumes must be an array" unless mounts.is_a?(Array)
  raise "service #{name} has too many volumes" if mounts.length > 128
  healthcheck = raw["healthcheck"]
  unless healthcheck.nil? || healthcheck.is_a?(Hash)
    raise "service #{name} healthcheck must be a mapping"
  end
  implicit_dependencies = []
  string_array(raw["links"], "service #{name} links").each do |item|
    implicit_dependencies << item.split(":", 2)[0]
  end
  string_array(raw["volumes_from"], "service #{name} volumes_from").each do |item|
    implicit_dependencies << item.split(":", 2)[0] unless item.start_with?("container:")
  end
  ["ipc", "pid", "network_mode"].each do |field|
    value = raw[field]
    unless value.nil? || value.is_a?(String)
      raise "service #{name} #{field} must be a string"
    end
    implicit_dependencies << value.split(":", 2)[1] if value && value.start_with?("service:")
  end
  out[name] = {
    "depends_on" => deps,
    "implicit_dependencies" => implicit_dependencies.compact.uniq,
    "profiles" => string_array(raw["profiles"], "service #{name} profiles"),
    "ports" => ports.each_with_index.map { |item, index| normalize_port(item, "service #{name} port #{index}") },
    "volumes" => mounts.each_with_index.map { |item, index| normalize_mount(item, "service #{name} volume #{index}") },
    "devices" => string_array(raw["devices"], "service #{name} devices"),
    "container_name" => raw["container_name"].is_a?(String) ? raw["container_name"] : nil,
    "has_healthcheck" => healthcheck.is_a?(Hash) && healthcheck["disable"] != true,
    "restart" => raw["restart"].is_a?(String) ? raw["restart"] : nil,
    "has_interpolation" => interpolation?(raw)
  }
  unless raw["container_name"].nil? || raw["container_name"].is_a?(String)
    raise "service #{name} container_name must be a string"
  end
end
STDOUT.write(JSON.generate({
  "name" => doc["name"],
  "name_interpolation" => interpolation?(doc["name"]),
  "services" => out
}))
'''


def _parse_compose_with_safe_yaml(path: Path) -> dict[str, Any]:
    system_ruby = Path("/usr/bin/ruby")
    ruby = str(system_ruby) if system_ruby.is_file() else shutil.which("ruby")
    if not ruby:
        raise AtomError("a standards-compliant safe YAML parser is unavailable")
    source = _strict_read_text(path, str(path))
    try:
        completed = subprocess.run(
            [ruby, "--disable-gems", "-e", _RUBY_COMPOSE_PARSER],
            input=source,
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
            env={"PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AtomError(f"safe Compose YAML parser failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[0] if completed.stderr.strip() else "unknown parser error"
        raise AtomError(f"safe Compose YAML parser rejected the file: {detail[:500]}")
    if len(completed.stdout) > MAX_SOURCE_BYTES:
        raise AtomError("sanitized Compose parser output exceeded its bound")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AtomError(f"safe Compose parser returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("services"), dict):
        raise AtomError("safe Compose parser returned an invalid structure")
    return payload


class ComposeCompiler:
    def __init__(self, compilation: Compilation, compose_file: Path) -> None:
        self.compilation = compilation
        self.compose_file = compose_file.resolve()
        self.cwd = self.compose_file.parent
        self.source = _source_label(self.compose_file, compilation.project)
        compilation.snapshot(self.compose_file)
        self.payload = _parse_compose_with_safe_yaml(self.compose_file)
        self.services: dict[str, dict[str, Any]] = self.payload["services"]
        for service, raw in self.services.items():
            if not isinstance(raw, dict) or not isinstance(raw.get("depends_on", {}), dict):
                raise AtomError(f"normalized Compose service is invalid: {service}")
            for dependency in raw.get("implicit_dependencies", []):
                raw["depends_on"].setdefault(
                    dependency,
                    {"condition": "service_started", "required": True, "restart": False},
                )
        self.project_name = self.payload.get("name") or self.cwd.name
        self.project_name_dynamic = bool(self.payload.get("name_interpolation"))

    @staticmethod
    def _condition(raw: Any) -> str:
        if not isinstance(raw, dict):
            raise AtomError("normalized Compose dependency must be a mapping")
        condition = raw.get("condition", "service_started")
        if condition not in {
            "service_started", "service_healthy", "service_completed_successfully",
        }:
            raise AtomError(f"unsupported Compose dependency condition: {condition}")
        return condition

    def compile(self, selected: list[str], active_profiles: set[str]) -> Fragment:
        selected = list(dict.fromkeys(selected))
        unknown = sorted(set(selected) - set(self.services))
        if unknown:
            raise AtomError(f"unknown Compose services: {unknown}")
        closure: set[str] = set()

        def include(service: str) -> None:
            if service in closure:
                return
            raw = self.services[service]
            profiles = set(raw.get("profiles") or [])
            if profiles and not (profiles & active_profiles) and service not in selected:
                raise AtomError(
                    f"Compose dependency {service} is excluded by the active profile set; "
                    "refusing to lower an incomplete service graph"
                )
            closure.add(service)
            for dependency, dependency_spec in raw.get("depends_on", {}).items():
                self._condition(dependency_spec)
                if dependency not in self.services:
                    raise AtomError(f"Compose service {service} has unknown dependency: {dependency}")
                include(dependency)

        for service in selected:
            include(service)

        visiting: set[str] = set()
        visited: set[str] = set()

        def check_cycle(service: str) -> None:
            if service in visiting:
                raise AtomError(f"Compose dependency graph contains a cycle involving {service}")
            if service in visited:
                return
            visiting.add(service)
            for dependency in self.services[service].get("depends_on", {}):
                if dependency in closure:
                    check_cycle(dependency)
            visiting.remove(service)
            visited.add(service)

        for service in sorted(closure):
            check_cycle(service)

        atom_by_service: dict[str, str] = {}
        for service in sorted(closure):
            raw = self.services[service]
            dependency_conditions = {
                consumer: self._condition(spec)
                for consumer in closure
                for spec in [self.services[consumer].get("depends_on", {}).get(service)]
                if spec is not None
            }
            if "service_healthy" in dependency_conditions.values() and not raw.get("has_healthcheck"):
                raise AtomError(
                    f"Compose service {service} is required to be healthy, but no enabled healthcheck was declared"
                )
            completion = (
                "successful_service_exit"
                if "service_completed_successfully" in dependency_conditions.values()
                else "healthy"
                if raw.get("has_healthcheck") or "service_healthy" in dependency_conditions.values()
                else "ready"
            )
            effects: list[dict[str, str]] = [
                {"domain": "compose-service", "key": f"{self.project_name}:{service}", "mode": "lease"}
            ]
            if raw.get("container_name"):
                effects.append({"domain": "container-name", "key": raw["container_name"], "mode": "lease"})
            for port in raw.get("ports", []):
                host = _compose_host_port(port)
                if host:
                    effects.append({"domain": "host-port", "key": host, "mode": "lease"})
            for device in raw.get("devices", []):
                effects.append({"domain": "device", "key": device.split(":", 1)[0], "mode": "lease"})
            dynamic = self.project_name_dynamic or bool(raw.get("has_interpolation"))
            atom_by_service[service] = self.compilation.emit(
                {
                    "id": f"compose-{_slug(service)}",
                    "operation": {
                        "kind": "compose_service",
                        "argv": ["docker", "compose", "-f", str(self.compose_file), "up", service],
                        "cwd": str(self.cwd),
                        "completion": completion,
                        "internal_parallelism": {"kind": "native_scheduler", "tokens": None},
                    },
                    "effects": effects,
                    "side_effect": True,
                    "profile": "mixed",
                    "semantics": {
                        "idempotent": True,
                        "retryable": None,
                        "deterministic": None,
                        "cacheable": False,
                        "commutative": False,
                        "cancel_safe": None,
                        "splittable": False,
                        "reorderable": "proved",
                    },
                    "batch": {"key": f"compose:{self.project_name}", "strategy": "compose_services"},
                    "assurance": {
                        "parse": "conservative" if dynamic else "exact",
                        "control": "partial" if dynamic else "exact",
                        "effects": "partial",
                        "codegen": "native_delegate",
                        "rank": 0.45 if dynamic else 0.8,
                        "blockers": (
                            ["DYNAMIC_COMPOSE_VALUE", "COMPOSE_RUNTIME_EFFECTS_PARTIAL"]
                            if dynamic else
                            ["COMPOSE_RUNTIME_EFFECTS_PARTIAL"]
                        ),
                    },
                    "provenance": {
                        "adapter": "compose",
                        "source": self.source,
                        "symbol": service,
                        "confidence": 0.6 if dynamic else 1.0,
                    },
                }
            )

        by_id = {atom["id"]: atom for atom in self.compilation.atoms}
        for service, atom_id in atom_by_service.items():
            dependencies = []
            for dependency, dependency_spec in self.services[service].get("depends_on", {}).items():
                condition = self._condition(dependency_spec)
                kind = {
                    "service_healthy": "after_healthy",
                    "service_completed_successfully": "after_completion",
                    "service_started": "after_ready",
                }[condition]
                dependencies.append({"atom": atom_by_service[dependency], "kind": kind})
            by_id[atom_id]["dependencies"] = dependencies

        delegate_argv = ["docker", "compose", "-f", str(self.compose_file)]
        for profile in sorted(active_profiles):
            delegate_argv.extend(["--profile", profile])
        delegate_argv.extend(["up", "--wait", *selected])
        self.compilation.native_delegates.append(
            {
                "kind": "compose_native_graph",
                "argv": delegate_argv,
                "cwd": str(self.cwd),
                "source": self.source,
                "services": selected,
                "profiles": sorted(active_profiles),
                "internal_parallelism": {"kind": "native_scheduler", "tokens": None},
                "reason": "Compose owns service lifecycle, health conditions, project locking, and native parallelism.",
            }
        )
        roots = [
            atom_by_service[service]
            for service in closure
            if not by_id[atom_by_service[service]].get("dependencies")
        ]
        dependents = {
            dependency["atom"]
            for atom_id in atom_by_service.values()
            for dependency in by_id[atom_id].get("dependencies", [])
        }
        terminals = [atom_id for atom_id in atom_by_service.values() if atom_id not in dependents]
        return Fragment(sorted(roots), sorted(terminals), sorted(atom_by_service.values()))


def _compose_host_port(value: Any) -> str | None:
    """Return a canonical fixed host-port lease, or None for dynamic publish."""
    if isinstance(value, dict):
        published = value.get("published")
        if published is None:
            return None
        candidate = str(published)
        protocol = str(value.get("protocol") or "tcp").lower()
        host = str(value.get("host_ip") or "0.0.0.0")
    elif isinstance(value, str):
        text, _, protocol_suffix = value.partition("/")
        protocol = (protocol_suffix or "tcp").lower()
        if ":" not in text:
            # CONTAINER[/PROTO] requests an implementation-selected host port.
            return None
        if text.startswith("[") and "]" in text:
            closing = text.index("]")
            host = text[: closing + 1]
            remainder = text[closing + 1:]
            if not remainder.startswith(":"):
                return None
            pieces = remainder[1:].split(":")
            if len(pieces) != 2:
                return None
            candidate = pieces[0]
        else:
            pieces = text.rsplit(":", 2)
            if len(pieces) == 2:
                host, candidate = "0.0.0.0", pieces[0]
            elif len(pieces) == 3:
                host, candidate = pieces[0] or "0.0.0.0", pieces[1]
            else:
                return None
    else:
        return None
    numeric = candidate.split("-", 1)
    if not numeric or not all(item.isdigit() for item in numeric):
        return None
    return f"{protocol}://{host}:{candidate}"


def compile_entrypoints(
    project: Path,
    raw_entrypoints: Any,
    *,
    target_os: str | None = None,
) -> dict[str, Any]:
    """Compile host entrypoints, with an explicit OS seam for parser tests.

    Production callers omit ``target_os`` and are always bound to ``os.name``;
    the override exists so portable parser contracts can be regression-tested
    on Windows without weakening the native-Windows frontend gate.
    """
    effective_os = os.name if target_os is None else target_os
    if effective_os not in {"nt", "posix"}:
        raise AtomError("target_os must be 'nt' or 'posix'")
    if raw_entrypoints is None:
        raw_entrypoints = []
    if not isinstance(raw_entrypoints, list) or len(raw_entrypoints) > 64:
        raise AtomError("entrypoints must be an array with at most 64 entries")
    compilation = Compilation(project)
    roots: list[str] = []
    terminals: list[str] = []
    for index, entrypoint in enumerate(raw_entrypoints):
        if not isinstance(entrypoint, dict):
            raise AtomError(f"entrypoint {index} must be an object")
        adapter = entrypoint.get("adapter")
        entry_id = _slug(entrypoint.get("id", f"entry-{index}"))
        if effective_os == "nt" and adapter in {
            "shell",
            "package_script",
            "make_target",
            "compose_services",
        }:
            raise AtomError(
                f"entrypoint {entry_id} adapter {adapter} has POSIX shell semantics; "
                "Windows Preview requires an explicit argv atom or powershell_file"
            )
        if adapter == "shell":
            cwd_raw = entrypoint.get("cwd", str(project))
            cwd = Path(cwd_raw).expanduser()
            if not cwd.is_absolute():
                cwd = project / cwd
            cwd = cwd.resolve(strict=False)
            if not cwd.is_dir():
                raise AtomError(f"entrypoint {entry_id} cwd does not exist: {cwd}")
            command = _bounded_text(entrypoint.get("command"), f"entrypoint {entry_id} command")
            fragment = compile_shell(
                compilation,
                command,
                prefix=entry_id,
                cwd=cwd,
                source="task_plan",
                symbol=entry_id,
            )
        elif adapter == "package_script":
            package_file = Path(entrypoint.get("package_json", project / "package.json")).expanduser()
            if not package_file.is_absolute():
                package_file = project / package_file
            script = _bounded_text(entrypoint.get("script"), f"entrypoint {entry_id} script", 512)
            compiler = PackageScriptCompiler(compilation, package_file)
            fragment = compiler.compile(script)
        elif adapter == "make_target":
            makefile = Path(entrypoint.get("makefile", project / "Makefile")).expanduser()
            if not makefile.is_absolute():
                makefile = project / makefile
            target = _bounded_text(entrypoint.get("target"), f"entrypoint {entry_id} target", 512)
            compiler = MakefileCompiler(compilation, makefile)
            fragment = compiler.compile(target)
            compiler.delegate(target)
        elif adapter == "compose_services":
            compose_file = Path(entrypoint.get("compose_file", project / "compose.yml")).expanduser()
            if not compose_file.is_absolute():
                compose_file = project / compose_file
            services = entrypoint.get("services")
            if (
                not isinstance(services, list)
                or not services
                or len(services) > 256
                or not all(
                    isinstance(item, str) and item and "\x00" not in item and len(item) <= 256
                    for item in services
                )
            ):
                raise AtomError(f"entrypoint {entry_id} services must be a non-empty string array")
            profiles_raw = entrypoint.get("profiles", [])
            if (
                not isinstance(profiles_raw, list)
                or len(profiles_raw) > 64
                or not all(
                    isinstance(item, str) and item and "\x00" not in item and len(item) <= 256
                    for item in profiles_raw
                )
            ):
                raise AtomError(f"entrypoint {entry_id} profiles must be a string array")
            compiler = ComposeCompiler(compilation, compose_file)
            fragment = compiler.compile(services, set(profiles_raw))
        elif adapter == "powershell_file":
            if effective_os != "nt":
                raise AtomError(
                    f"entrypoint {entry_id} powershell_file requires native Windows"
                )
            script_raw = _bounded_text(
                entrypoint.get("script_path"), f"entrypoint {entry_id} script_path", 4096
            )
            script = Path(script_raw).expanduser()
            if not script.is_absolute():
                script = project / script
            script = script.resolve(strict=True)
            if not script.is_file() or script.suffix.casefold() != ".ps1":
                raise AtomError(f"entrypoint {entry_id} script_path must be an existing .ps1 file")
            cwd_raw = entrypoint.get("cwd", str(script.parent))
            cwd = Path(cwd_raw).expanduser()
            if not cwd.is_absolute():
                cwd = project / cwd
            cwd = cwd.resolve(strict=True)
            if not cwd.is_dir():
                raise AtomError(f"entrypoint {entry_id} cwd does not exist: {cwd}")
            arguments = entrypoint.get("arguments", [])
            if (
                not isinstance(arguments, list)
                or len(arguments) > 128
                or not all(isinstance(item, str) for item in arguments)
            ):
                raise AtomError(f"entrypoint {entry_id} arguments must be a bounded string array")
            accesses = entrypoint.get("declared_accesses", [])
            effects = entrypoint.get("declared_effects", [])
            if not isinstance(accesses, list) or not isinstance(effects, list):
                raise AtomError(
                    f"entrypoint {entry_id} declared_accesses and declared_effects must be arrays"
                )
            complete = entrypoint.get("effects_declared_complete", False)
            if not isinstance(complete, bool):
                raise AtomError(f"entrypoint {entry_id} effects_declared_complete must be boolean")
            side_effect = entrypoint.get("side_effect", True)
            if not isinstance(side_effect, bool):
                raise AtomError(f"entrypoint {entry_id} side_effect must be boolean")
            profile = entrypoint.get("profile", "mixed")
            if profile not in {"cpu", "io", "mixed", "accelerator"}:
                raise AtomError(f"entrypoint {entry_id} profile is unsupported")
            pwsh = shutil.which("pwsh")
            blockers = [] if pwsh else ["POWERSHELL_7_UNAVAILABLE"]
            snapshot = compilation.snapshot(script)
            script_access = {"resource": str(script), "mode": "snapshot"}
            atom_id = compilation.emit(
                {
                    "id": entry_id,
                    "operation": {
                        "kind": "shell",
                        "argv": [
                            pwsh or "pwsh",
                            "-NoLogo",
                            "-NoProfile",
                            "-NonInteractive",
                            "-File",
                            str(script),
                            *[
                                _bounded_text(
                                    value, f"entrypoint {entry_id} argument", 32_768
                                )
                                for value in arguments
                            ],
                        ],
                        "cwd": str(cwd),
                        "env": {},
                        "completion": "process_exit",
                        "internal_parallelism": {"kind": "unknown", "tokens": None},
                    },
                    "accesses": [script_access, *accesses],
                    "effects": effects,
                    "profile": profile,
                    "side_effect": side_effect,
                    "semantics": {
                        "idempotent": None,
                        "retryable": None,
                        "deterministic": None,
                        "cacheable": False,
                        "commutative": False,
                        "cancel_safe": None,
                        "splittable": False,
                        "reorderable": "unknown",
                    },
                    "assurance": {
                        "parse": "exact",
                        "control": "exact",
                        "effects": "complete_declared" if complete else "unknown",
                        "codegen": "exact_argv",
                        "rank": 1.0 if complete and not blockers else 0.5,
                        "blockers": blockers + ([] if complete else ["INCOMPLETE_EFFECT_MODEL"]),
                    },
                    "provenance": {
                        "adapter": "powershell_file",
                        "source": snapshot["path"],
                        "symbol": entry_id,
                        "confidence": 1.0,
                    },
                }
            )
            fragment = Fragment([atom_id], [atom_id], [atom_id])
        else:
            raise AtomError(f"entrypoint {entry_id} has unsupported adapter: {adapter}")
        roots.extend(fragment.roots)
        terminals.extend(fragment.terminals)
    result = compilation.result()
    result["roots"] = list(dict.fromkeys(roots))
    result["terminals"] = list(dict.fromkeys(terminals))
    return result
