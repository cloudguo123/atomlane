#!/usr/bin/env python3
"""Bounded, non-executing Python parallelization advisor.

The advisor parses project-local Python source without importing or executing
it.  It recognizes a deliberately small ordered-map subset, propagates a
conservative intra-module effect summary, and emits source-hash-bound rewrite
previews only when every hard gate is satisfied.

This is not a general Python optimizer.  Unknown calls, dynamic behavior,
observable effects, missing portable spawn guards, and loop-carried control flow
fail closed.
"""

from __future__ import annotations

import ast
import builtins
import difflib
import hashlib
import io
import json
import math
import os
import platform
import re
import stat
import tokenize
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ANALYSIS_VERSION = "python-parallel/2"
MAX_SOURCE_BYTES = 2_000_000
MAX_TOTAL_SOURCE_BYTES = 16_000_000
MAX_AST_NODES = 80_000
MAX_PARSE_TOKENS = MAX_AST_NODES
MAX_PHYSICAL_LINES = MAX_AST_NODES
MAX_FILES = 512
MAX_CANDIDATES = 128
MAX_DIAGNOSTICS = 256
MAX_DIFF_BYTES = 32_768
MAX_HOTSPOTS = 128
MAX_REQUESTED_PATHS = 128
MAX_PATH_TEXT = 4096
MAX_HOTSPOT_SECONDS = 1_000_000_000.0
MAX_ITEM_COUNT = 1_000_000_000
MAX_DISCOVERY_DIRECTORIES = 10_000
MAX_DISCOVERY_ENTRIES = 100_000
MAX_DISCOVERY_DEPTH = 64
MAX_TOTAL_AST_NODES = 500_000
BUILTIN_NAMES = frozenset(dir(builtins))
MODULE_CONTEXT_NAMES = {
    "__annotations__",
    "__builtins__",
    "__cached__",
    "__file__",
    "__loader__",
    "__name__",
    "__package__",
    "__spec__",
}

SKIPPED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "site-packages",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    "vendor",
    "third_party",
}

PURE_BUILTINS = {
    "abs",
    "ascii",
    "bin",
    "bool",
    "bytes",
    "chr",
    "complex",
    "divmod",
    "float",
    "format",
    "hex",
    "int",
    "len",
    "oct",
    "ord",
    "pow",
    "range",
    "repr",
    "round",
    "slice",
    "str",
}

PURE_MODULE_PREFIXES = {"math", "cmath", "operator", "statistics", "fractions"}
READ_METHODS = {"read", "read_text", "read_bytes", "readline", "readlines"}
WRITE_METHODS = {
    "append",
    "dump",
    "mkdir",
    "rename",
    "replace",
    "save",
    "savefig",
    "savetxt",
    "touch",
    "to_csv",
    "to_json",
    "unlink",
    "write",
    "write_bytes",
    "write_text",
    "writelines",
}
NETWORK_PREFIXES = {"requests", "httpx", "urllib", "aiohttp", "socket"}
SUBPROCESS_PREFIXES = {"subprocess", "os.system", "os.popen"}
NONDETERMINISTIC_PREFIXES = {"random", "secrets", "time.time", "uuid"}
DATABASE_MARKERS = {"execute", "executemany", "commit", "rollback", "cursor"}
OUTPUT_MARKERS = {"print", "logging", "logger", "warnings.warn"}
NATIVE_PARALLEL_PREFIXES = {
    "numpy",
    "np",
    "scipy",
    "torch",
    "tensorflow",
    "jax",
    "polars",
}
EXISTING_PARALLEL_MARKERS = {
    "concurrent.futures",
    "ProcessPoolExecutor",
    "ThreadPoolExecutor",
    "multiprocessing",
    "joblib",
    "ray",
    "dask",
    "asyncio.gather",
    "asyncio.TaskGroup",
    "asyncio.to_thread",
}


class AdvisorError(ValueError):
    """Invalid caller input or an unsafe target boundary."""


def _bounded_positive_number(value: Any, maximum: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value <= 0 or value > maximum:
        return False
    return not isinstance(value, float) or math.isfinite(value)


@dataclass
class FunctionFacts:
    name: str
    line: int
    end_line: int
    top_level: bool
    is_async: bool
    has_decorators: bool
    has_dynamic_defaults: bool
    returns_generator: bool = False
    effects: set[str] = field(default_factory=set)
    local_calls: set[str] = field(default_factory=set)
    global_reads: set[str] = field(default_factory=set)
    evidence: set[str] = field(default_factory=set)
    cpu_operations: int = 0


@dataclass
class SourceModule:
    path: Path
    relative_path: str
    raw: bytes
    text: str
    sha256: str
    tree: ast.Module
    ast_nodes: int
    functions: dict[str, FunctionFacts]
    function_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    main_guard_calls: set[str]
    existing_parallelism: set[str]
    module_import_effects: list[str]
    dunder_name_rebound: bool
    module_binding_counts: dict[str, int]
    diagnostics: list[dict[str, Any]]


def _diagnostic(code: str, message: str, path: str | None = None, line: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "severity": "warning", "message": message[:1000]}
    if path is not None:
        result["path"] = path
    if line is not None:
        result["line"] = line
    return result


def _inside(project: Path, path: Path) -> bool:
    return path == project or project in path.parents


def _regular_project_file(project: Path, candidate: Path) -> Path:
    unresolved = candidate if candidate.is_absolute() else project / candidate
    unresolved_text = str(unresolved)
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or character in {"\u2028", "\u2029"}
        for character in unresolved_text
    ):
        raise AdvisorError("Python target path contains a control or line-separator character")
    try:
        resolved = unresolved.resolve(strict=True)
    except OSError as exc:
        raise AdvisorError(f"Python target does not exist: {candidate}") from exc
    if not _inside(project, resolved):
        raise AdvisorError(f"Python target escapes project root: {candidate}")
    try:
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise AdvisorError(f"cannot stat Python target: {candidate}") from exc
    if not stat.S_ISREG(mode):
        raise AdvisorError(f"Python target is not a regular file: {candidate}")
    if resolved.suffix.lower() != ".py":
        raise AdvisorError(f"Python target must end in .py: {candidate}")
    return resolved


def _resolve_sources(project: Path, requested: list[str] | None, max_files: int) -> tuple[list[Path], bool]:
    if requested is not None:
        paths = sorted({_regular_project_file(project, Path(item)) for item in requested}, key=str)
        if len(paths) > max_files:
            raise AdvisorError(f"requested Python file count exceeds max_files={max_files}")
        return paths, False

    found: list[Path] = []
    pending = [project]
    directories_seen = 0
    entries_seen = 0
    while pending:
        root = pending.pop()
        directories_seen += 1
        if directories_seen > MAX_DISCOVERY_DIRECTORIES:
            return found, True
        try:
            entries: list[os.DirEntry[str]] = []
            with os.scandir(root) as iterator:
                for entry in iterator:
                    entries_seen += 1
                    if entries_seen > MAX_DISCOVERY_ENTRIES:
                        return found, True
                    entries.append(entry)
        except OSError:
            continue

        child_directories: list[Path] = []
        for entry in sorted(entries, key=lambda item: item.name):
            candidate = root / entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in SKIPPED_DIRECTORIES:
                        continue
                    depth = len(candidate.relative_to(project).parts)
                    if depth > MAX_DISCOVERY_DEPTH:
                        return found, True
                    child_directories.append(candidate)
                    continue
                if not entry.name.endswith(".py") or not entry.is_file(follow_symlinks=False):
                    continue
                resolved = _regular_project_file(project, candidate)
            except (AdvisorError, OSError):
                continue
            found.append(resolved)
            if len(found) > max_files:
                return found[:max_files], True
        pending.extend(reversed(child_directories))
    return found, False


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


def _is_immutable_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, bytes, int, float, complex, bool, type(None), type(Ellipsis)))
    if isinstance(node, ast.Tuple):
        return all(_is_immutable_literal(item) for item in node.elts)
    return False


def _simple_binding_target(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, (ast.Tuple, ast.List)):
        return bool(node.elts) and all(_simple_binding_target(item) for item in node.elts)
    return False


def _main_guard(node: ast.If) -> bool:
    if node.orelse:
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    values = [test.left, *test.comparators]
    has_name = any(isinstance(item, ast.Name) and item.id == "__name__" for item in values)
    has_main = any(isinstance(item, ast.Constant) and item.value == "__main__" for item in values)
    return has_name and has_main


def _dunder_name_rebound(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "__name__" and isinstance(node.ctx, (ast.Store, ast.Del)):
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
            (alias.asname or alias.name.split(".", 1)[0]) == "__name__" for alias in node.names
        ):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == "__name__":
            return True
        if isinstance(node, (ast.Global, ast.Nonlocal)) and "__name__" in node.names:
            return True
    return False


def _guarded_calls(tree: ast.Module) -> set[str]:
    result: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.If) or not _main_guard(node):
            continue
        if len(node.body) != 1:
            continue
        statement = node.body[0]
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and not statement.value.keywords
            and all(_is_immutable_literal(argument) for argument in statement.value.args)
        ):
            continue
        result.add(statement.value.func.id)
    return result


def _guard_call_lines(tree: ast.Module, name: str) -> list[int]:
    lines: list[int] = []
    for node in tree.body:
        if not isinstance(node, ast.If) or not _main_guard(node) or len(node.body) != 1:
            continue
        statement = node.body[0]
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == name
            and not statement.value.keywords
            and all(_is_immutable_literal(argument) for argument in statement.value.args)
        ):
            continue
        lines.append(statement.lineno)
    return lines


def _top_level_main_guards(tree: ast.Module) -> list[ast.If]:
    return [
        node
        for node in tree.body
        if isinstance(node, ast.If) and _main_guard(node)
    ]


def _module_import_effects(tree: ast.Module) -> list[str]:
    """Return reasons importing the main module may repeat observable work."""
    reasons: list[str] = []
    future_annotations = any(
        isinstance(statement, ast.ImportFrom)
        and statement.module == "__future__"
        and any(alias.name == "annotations" for alias in statement.names)
        for statement in tree.body
    )
    if _dunder_name_rebound(tree):
        reasons.append("__name__ binding is reassigned or deleted")
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.module == "__future__":
            continue
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            reasons.append(f"{type(statement).__name__} at line {statement.lineno} requires import-safety review")
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if getattr(statement, "type_params", []):
                reasons.append(f"generic type parameters on function {statement.name} at line {statement.lineno}")
            if statement.decorator_list:
                reasons.append(f"decorated function {statement.name} at line {statement.lineno}")
            defaults: Iterable[ast.AST | None] = [*statement.args.defaults, *statement.args.kw_defaults]
            if any(value is not None and not _is_immutable_literal(value) for value in defaults):
                reasons.append(f"runtime default for function {statement.name} at line {statement.lineno}")
            annotations = [
                statement.returns,
                *(arg.annotation for arg in statement.args.posonlyargs),
                *(arg.annotation for arg in statement.args.args),
                *(arg.annotation for arg in statement.args.kwonlyargs),
            ]
            if statement.args.vararg:
                annotations.append(statement.args.vararg.annotation)
            if statement.args.kwarg:
                annotations.append(statement.args.kwarg.annotation)
            if not future_annotations and any(
                annotation is not None and not _is_immutable_literal(annotation)
                for annotation in annotations
            ):
                reasons.append(f"runtime annotations for function {statement.name} at line {statement.lineno}")
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            continue
        if isinstance(statement, ast.If) and _main_guard(statement):
            continue
        if isinstance(statement, ast.AnnAssign):
            reasons.append(f"runtime variable annotation at line {statement.lineno}")
            continue
        if (
            isinstance(statement, ast.Assign)
            and statement.targets
            and all(_simple_binding_target(target) for target in statement.targets)
            and _is_immutable_literal(statement.value)
        ):
            continue
        reasons.append(f"{type(statement).__name__} at line {getattr(statement, 'lineno', 0)}")
    return reasons


class _EffectVisitor(ast.NodeVisitor):
    def __init__(
        self,
        facts: FunctionFacts,
        local_functions: set[str],
        local_names: set[str],
        module_value_names: set[str],
        module_import_names: set[str],
        trusted_imported_callables: set[str] | None = None,
        ignored_nodes: set[ast.AST] | None = None,
    ) -> None:
        self.facts = facts
        self.local_functions = local_functions
        self.local_names = local_names
        self.module_value_names = module_value_names
        self.module_import_names = module_import_names
        self.trusted_imported_callables = trusted_imported_callables or set()
        self.ignored_nodes = ignored_nodes or set()
        self.call_targets: set[ast.AST] = set()
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()

    def visit(self, node: ast.AST) -> Any:
        if node in self.ignored_nodes:
            return None
        return super().visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)
        self.facts.effects.add("global_state")
        self.facts.evidence.add(f"global declaration at line {node.lineno}")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)
        self.facts.effects.add("closure_state")
        self.facts.evidence.add(f"nonlocal declaration at line {node.lineno}")

    def _record_target(self, target: ast.AST, line: int) -> None:
        if isinstance(target, ast.Name):
            if target.id in self.global_names:
                self.facts.effects.add("global_write")
                self.facts.evidence.add(f"global write at line {line}")
            if target.id in self.nonlocal_names:
                self.facts.effects.add("closure_write")
                self.facts.evidence.add(f"nonlocal write at line {line}")
        elif isinstance(target, ast.Attribute):
            self.facts.effects.add("attribute_write")
            self.facts.evidence.add(f"attribute write at line {line}")
        elif isinstance(target, ast.Subscript):
            self.facts.effects.add("subscript_write")
            self.facts.evidence.add(f"subscript write at line {line}")
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._record_target(item, line)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_target(target, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_target(node.target, node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_target(node.target, node.lineno)
        if isinstance(node.target, ast.Name):
            self.facts.effects.add("loop_carried_or_stateful_update")
            self.facts.evidence.add(f"augmented assignment at line {node.lineno}")
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        self.facts.effects.add("state_delete")
        self.facts.evidence.add(f"delete at line {node.lineno}")
        self.generic_visit(node)

    def visit_Yield(self, node: ast.Yield) -> None:
        self.facts.returns_generator = True
        self.facts.effects.add("generator_semantics")
        self.generic_visit(node)

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        self.facts.returns_generator = True
        self.facts.effects.add("generator_semantics")
        self.generic_visit(node)

    def visit_Await(self, node: ast.Await) -> None:
        self.facts.effects.add("async_control")
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        self.facts.effects.add("explicit_raise")
        self.facts.evidence.add(f"raise statement at line {node.lineno}")
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self.facts.effects.add("explicit_assert")
        self.facts.evidence.add(f"assert statement at line {node.lineno}")
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        self.facts.cpu_operations += 1
        self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        self.facts.cpu_operations += 1
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        self.facts.cpu_operations += 1
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.facts.effects.add("nested_function")
        self.facts.evidence.add(f"nested function at line {node.lineno}")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.facts.effects.add("nested_function")
        self.facts.evidence.add(f"nested async function at line {node.lineno}")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.facts.effects.add("nested_class")
        self.facts.evidence.add(f"nested class at line {node.lineno}")

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self.facts.effects.add("generator_expression")
        self.facts.evidence.add(f"generator expression at line {node.lineno}")
        self.generic_visit(node)

    def visit_Set(self, node: ast.Set) -> None:
        self.facts.effects.add("unordered_hash_collection")
        self.facts.evidence.add(f"set literal at line {node.lineno}")
        self.generic_visit(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self.facts.effects.add("unordered_hash_collection")
        self.facts.evidence.add(f"set comprehension at line {node.lineno}")
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.facts.effects.add("dynamic_callable")
        self.facts.evidence.add(f"lambda at line {node.lineno}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load) or node.id in self.local_names:
            return
        if node.id in self.local_functions:
            if node not in self.call_targets:
                self.facts.effects.add("function_value_escape")
                self.facts.evidence.add(
                    f"same-module function {node.id} used as a runtime value at line {node.lineno}"
                )
            return
        self.facts.global_reads.add(node.id)
        if node.id in MODULE_CONTEXT_NAMES:
            self.facts.effects.add("process_module_context")
            self.facts.evidence.add(f"process/module context read {node.id} at line {node.lineno}")
        elif node.id in self.module_value_names:
            self.facts.effects.add("module_global_read")
            self.facts.evidence.add(f"module global read {node.id} at line {node.lineno}")
        elif node.id not in self.module_import_names and node.id not in BUILTIN_NAMES:
            self.facts.effects.add("unresolved_free_name")
            self.facts.evidence.add(f"unresolved free name {node.id} at line {node.lineno}")

    def visit_Subscript(self, node: ast.Subscript) -> None:
        name = _dotted_name(node.value)
        if isinstance(node.ctx, ast.Load):
            if name == "os.environ":
                self.facts.effects.add("environment_read")
                self.facts.evidence.add(f"environment read at line {node.lineno}")
            else:
                self.facts.effects.add("subscript_read")
                self.facts.evidence.add(f"runtime subscript read at line {node.lineno}")
        elif isinstance(node.ctx, (ast.Store, ast.Del)):
            self.facts.effects.add("subscript_write")
            self.facts.evidence.add(f"subscript write at line {node.lineno}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load) and node not in self.call_targets:
            self.facts.effects.add("attribute_read")
            self.facts.evidence.add(f"runtime attribute read {_dotted_name(node) or node.attr} at line {node.lineno}")
        elif isinstance(node.ctx, (ast.Store, ast.Del)):
            self.facts.effects.add("attribute_write")
            self.facts.evidence.add(f"attribute write at line {node.lineno}")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.facts.effects.add("dynamic_import")
        self.facts.evidence.add(f"import inside worker at line {node.lineno}")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.facts.effects.add("dynamic_import")
        self.facts.evidence.add(f"import inside worker at line {node.lineno}")

    def visit_Call(self, node: ast.Call) -> None:
        self.call_targets.add(node.func)
        name = _dotted_name(node.func)
        root = name.split(".", 1)[0] if name else ""
        shadowed = (
            root in self.local_names
            or root in self.module_value_names
            or root in self.local_functions
        )
        leaf = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else node.func.id
            if isinstance(node.func, ast.Name)
            else ""
        )
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in self.local_functions
            and node.func.id not in self.local_names
        ):
            self.facts.local_calls.add(node.func.id)
        elif not shadowed and (
            name in PURE_BUILTINS
            or name in self.trusted_imported_callables
            or _materialized_iterable(node)[0]
        ):
            pass
        elif name and root in PURE_MODULE_PREFIXES and not shadowed:
            self.facts.cpu_operations += 1
        elif any(name == prefix or name.startswith(prefix + ".") for prefix in NETWORK_PREFIXES):
            self.facts.effects.add("network")
            self.facts.evidence.add(f"network call {name} at line {node.lineno}")
        elif any(name == prefix or name.startswith(prefix + ".") for prefix in SUBPROCESS_PREFIXES):
            self.facts.effects.add("subprocess")
            self.facts.evidence.add(f"subprocess call {name} at line {node.lineno}")
        elif any(name == prefix or name.startswith(prefix + ".") for prefix in NONDETERMINISTIC_PREFIXES):
            self.facts.effects.add("nondeterminism")
            self.facts.evidence.add(f"nondeterministic call {name} at line {node.lineno}")
        elif leaf in DATABASE_MARKERS or any(marker in name.lower() for marker in ("sqlalchemy", "prisma", "database")):
            self.facts.effects.add("database")
            self.facts.evidence.add(f"database-like call {name or leaf} at line {node.lineno}")
        elif name in OUTPUT_MARKERS or any(name.startswith(prefix + ".") for prefix in OUTPUT_MARKERS):
            self.facts.effects.add("observable_output")
            self.facts.evidence.add(f"output call {name} at line {node.lineno}")
        elif any(name == marker or name.startswith(marker + ".") for marker in NATIVE_PARALLEL_PREFIXES):
            self.facts.effects.add("native_or_gil_releasing")
            self.facts.evidence.add(f"native-library call {name} at line {node.lineno}")
        elif name in {"eval", "exec", "compile", "getattr", "setattr", "delattr", "__import__", "importlib.import_module"}:
            self.facts.effects.add("dynamic_execution")
            self.facts.evidence.add(f"dynamic call {name} at line {node.lineno}")
        elif leaf in READ_METHODS:
            self.facts.effects.add("io_read")
            self.facts.evidence.add(f"read call {name or leaf} at line {node.lineno}")
        elif leaf in WRITE_METHODS:
            self.facts.effects.add("external_write")
            self.facts.evidence.add(f"write-like call {name or leaf} at line {node.lineno}")
        elif name:
            self.facts.effects.add("unknown_call")
            self.facts.evidence.add(f"unknown call {name} at line {node.lineno}")
        else:
            self.facts.effects.add("dynamic_call_target")
            self.facts.evidence.add(f"dynamic call target at line {node.lineno}")
        self.generic_visit(node)


def _function_default_is_dynamic(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    values: Iterable[ast.AST | None] = [*node.args.defaults, *node.args.kw_defaults]
    return any(value is not None and not _is_immutable_literal(value) for value in values)


class _LocalBindingCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Param)):
            self.names.add(node.id)

    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)

    def visit_Import(self, node: ast.Import) -> None:
        self.names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.names.update(
            bound for alias in node.names if (bound := alias.asname or alias.name) != "*"
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._collect_named_expressions(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._collect_named_expressions(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._collect_named_expressions(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._collect_named_expressions(node)

    def _collect_named_expressions(self, node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
                self.names.add(child.target.id)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.names.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.names.add(node.rest)
        self.generic_visit(node)


def _function_local_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    collector = _LocalBindingCollector()
    collector.names.update(
        arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    )
    if node.args.vararg:
        collector.names.add(node.args.vararg.arg)
    if node.args.kwarg:
        collector.names.add(node.args.kwarg.arg)
    for statement in node.body:
        collector.visit(statement)
    return collector.names - collector.global_names - collector.nonlocal_names


class _ModuleBindingCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def add(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.add(alias.asname or alias.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name
            if bound != "*":
                self.add(bound)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._collect_named_expressions(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._collect_named_expressions(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._collect_named_expressions(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._collect_named_expressions(node)

    def _collect_named_expressions(self, node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
                self.add(child.target.id)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.add(node.rest)
        self.generic_visit(node)


def _module_binding_counts(tree: ast.Module) -> dict[str, int]:
    collector = _ModuleBindingCollector()
    for statement in tree.body:
        collector.visit(statement)
    return collector.counts


def _module_value_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for statement in tree.body:
        targets: list[ast.AST] = []
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
            targets = [statement.target]
        elif isinstance(statement, ast.ClassDef):
            names.add(statement.name)
        for target in targets:
            for child in ast.walk(target):
                if isinstance(child, ast.Name):
                    names.add(child.id)
    return names


def _module_import_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                if bound != "*":
                    names.add(bound)
    return names


def _trusted_imported_callables(tree: ast.Module) -> set[str]:
    trusted: set[str] = set()
    pathlib_constructors = {
        "Path",
        "PosixPath",
        "PurePath",
        "PurePosixPath",
        "PureWindowsPath",
        "WindowsPath",
    }
    for statement in tree.body:
        if not isinstance(statement, ast.ImportFrom) or statement.module != "pathlib":
            continue
        for alias in statement.names:
            if alias.name in pathlib_constructors:
                trusted.add(alias.asname or alias.name)
    return trusted


def _function_index(tree: ast.Module) -> tuple[dict[str, FunctionFacts], dict[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    nodes = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    names = set(nodes)
    module_values = _module_value_names(tree)
    module_imports = _module_import_names(tree)
    trusted_imports = _trusted_imported_callables(tree)
    facts: dict[str, FunctionFacts] = {}
    for name, node in sorted(nodes.items()):
        item = FunctionFacts(
            name=name,
            line=node.lineno,
            end_line=getattr(node, "end_lineno", node.lineno),
            top_level=True,
            is_async=isinstance(node, ast.AsyncFunctionDef),
            has_decorators=bool(node.decorator_list),
            has_dynamic_defaults=_function_default_is_dynamic(node),
        )
        if item.is_async:
            item.effects.add("async_control")
            item.evidence.add(f"async function {name}")
        if item.has_decorators:
            item.effects.add("decorated_callable")
            item.evidence.add(f"decorator on worker {name}")
        if item.has_dynamic_defaults:
            item.effects.add("dynamic_default")
            item.evidence.add(f"dynamic default on worker {name}")
        visitor = _EffectVisitor(
            item,
            names,
            _function_local_names(node),
            module_values,
            module_imports,
            trusted_imports,
        )
        for statement in node.body:
            visitor.visit(statement)
        facts[name] = item
    return facts, nodes


def _propagated_effects(
    functions: dict[str, FunctionFacts], name: str
) -> tuple[set[str], set[str], set[str]]:
    reachable: set[str] = set()
    pending = [name]
    effects: set[str] = set()
    evidence: set[str] = set()
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        facts = functions[current]
        effects.update(facts.effects)
        prefix = "" if current == name else f"via {current}: "
        evidence.update(prefix + item for item in facts.evidence)
        for callee in sorted(facts.local_calls, reverse=True):
            if callee not in functions:
                effects.add("unknown_call")
                evidence.add(f"unresolved local call {callee}")
            elif callee not in reachable:
                pending.append(callee)

    indegree = {symbol: 0 for symbol in reachable}
    for symbol in reachable:
        for callee in functions[symbol].local_calls:
            if callee in indegree:
                indegree[callee] += 1
    ready = [symbol for symbol, count in indegree.items() if count == 0]
    processed = 0
    while ready:
        symbol = ready.pop()
        processed += 1
        for callee in functions[symbol].local_calls:
            if callee not in indegree:
                continue
            indegree[callee] -= 1
            if indegree[callee] == 0:
                ready.append(callee)
    if processed != len(reachable):
        effects.add("recursive_call_graph")
        evidence.add("reachable same-module call graph contains a cycle")
    return effects, evidence, reachable


def _effects_outside_candidate(
    module: SourceModule,
    scope: ast.AST,
    candidate: ast.AST,
) -> tuple[set[str], set[str]]:
    container = scope if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
    local_names = _function_local_names(container) if container is not None else set()
    facts = FunctionFacts(
        name=getattr(scope, "name", "<guard>"),
        line=getattr(scope, "lineno", 1),
        end_line=getattr(scope, "end_lineno", getattr(scope, "lineno", 1)),
        top_level=container is None,
        is_async=isinstance(container, ast.AsyncFunctionDef),
        has_decorators=bool(container and container.decorator_list),
        has_dynamic_defaults=bool(container and _function_default_is_dynamic(container)),
    )
    visitor = _EffectVisitor(
        facts,
        set(module.functions),
        local_names,
        _module_value_names(module.tree),
        _module_import_names(module.tree),
        _trusted_imported_callables(module.tree),
        ignored_nodes={candidate},
    )
    body = getattr(scope, "body", [])
    for statement in body if isinstance(body, list) else []:
        visitor.visit(statement)
    effects = set(facts.effects)
    evidence = set(facts.evidence)
    for callee in sorted(facts.local_calls):
        if callee not in module.functions:
            effects.add("unknown_call")
            evidence.add(f"unresolved surrounding call {callee}")
            continue
        propagated, propagated_evidence, _ = _propagated_effects(module.functions, callee)
        effects.update(propagated)
        evidence.update(f"via surrounding {callee}: {item}" for item in propagated_evidence)
    return effects, evidence


def _expression_effects(
    module: SourceModule,
    node: ast.AST,
    container: ast.FunctionDef | ast.AsyncFunctionDef | None,
) -> tuple[set[str], set[str]]:
    facts = FunctionFacts(
        name="<iterable>",
        line=getattr(node, "lineno", 1),
        end_line=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        top_level=container is None,
        is_async=False,
        has_decorators=False,
        has_dynamic_defaults=False,
    )
    visitor = _EffectVisitor(
        facts,
        set(module.functions),
        _function_local_names(container) if container is not None else set(),
        _module_value_names(module.tree),
        _module_import_names(module.tree),
        _trusted_imported_callables(module.tree),
    )
    visitor.visit(node)
    effects = set(facts.effects)
    evidence = set(facts.evidence)
    for callee in sorted(facts.local_calls):
        propagated, propagated_evidence, _ = _propagated_effects(module.functions, callee)
        effects.update(propagated)
        evidence.update(f"via iterable {callee}: {item}" for item in propagated_evidence)
    return effects, evidence


def _existing_parallelism(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = node.module if isinstance(node, ast.ImportFrom) else ""
            names = [alias.name for alias in node.names]
            text = " ".join([module or "", *names])
            for marker in EXISTING_PARALLEL_MARKERS:
                if marker.split(".", 1)[0] in text:
                    found.add(marker)
        elif isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            for marker in EXISTING_PARALLEL_MARKERS:
                if name == marker or name.endswith("." + marker):
                    found.add(marker)
    return found


def _parse_preflight(text: str, relative: str) -> dict[str, Any] | None:
    """Bound parser work before constructing an AST.

    ``ast.parse`` can allocate hundreds of megabytes for a small, valid source
    file containing many tiny statements.  Tokenization is incremental, so a
    token/line ceiling rejects that shape before the parser builds the tree.
    The post-parse AST-node ceiling remains the authoritative structural cap.
    """
    physical_lines = text.count("\n") + int(bool(text) and not text.endswith(("\n", "\r")))
    if physical_lines > MAX_PHYSICAL_LINES:
        return _diagnostic(
            "PYTHON_PARSE_BUDGET_EXCEEDED",
            f"source exceeds the pre-parse limit of {MAX_PHYSICAL_LINES} physical lines",
            relative,
        )
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token_count, _token in enumerate(tokens, start=1):
            if token_count > MAX_PARSE_TOKENS:
                return _diagnostic(
                    "PYTHON_PARSE_BUDGET_EXCEEDED",
                    f"source exceeds the pre-parse limit of {MAX_PARSE_TOKENS} tokens",
                    relative,
                )
    except (IndentationError, SyntaxError, tokenize.TokenError) as exc:
        return _diagnostic("PYTHON_SOURCE_PARSE_FAILED", f"cannot tokenize source: {exc}", relative)
    return None


def _load_module(project: Path, path: Path) -> SourceModule | dict[str, Any]:
    relative = path.relative_to(project).as_posix()
    descriptor: int | None = None
    try:
        resolved_before = path.resolve(strict=True)
        expected = os.stat(path, follow_symlinks=False)
        if (
            resolved_before != path
            or not _inside(project, resolved_before)
            or not stat.S_ISREG(expected.st_mode)
        ):
            return _diagnostic(
                "PYTHON_SOURCE_RACE_OR_ESCAPE",
                "source path changed, became a symlink, or escaped the project boundary",
                relative,
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        resolved_after = path.resolve(strict=True)
        current = os.stat(resolved_after, follow_symlinks=False)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or resolved_after != path
            or not _inside(project, resolved_after)
            or identity != (expected.st_dev, expected.st_ino)
            or identity != (current.st_dev, current.st_ino)
        ):
            return _diagnostic(
                "PYTHON_SOURCE_RACE_OR_ESCAPE",
                "source identity changed while it was being opened",
                relative,
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        return _diagnostic("PYTHON_SOURCE_UNREADABLE", f"cannot read source: {exc}", relative)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > MAX_SOURCE_BYTES:
        return _diagnostic("PYTHON_SOURCE_TOO_LARGE", f"source exceeds {MAX_SOURCE_BYTES} bytes", relative)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return _diagnostic("PYTHON_SOURCE_NOT_UTF8", f"source is not strict UTF-8: {exc}", relative)
    preflight = _parse_preflight(text, relative)
    if preflight is not None:
        return preflight
    try:
        tree = ast.parse(text, filename=relative, type_comments=False)
    except (SyntaxError, ValueError, RecursionError) as exc:
        return _diagnostic("PYTHON_SOURCE_PARSE_FAILED", f"cannot parse source: {exc}", relative)
    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > MAX_AST_NODES:
        return _diagnostic("PYTHON_AST_TOO_LARGE", f"AST exceeds {MAX_AST_NODES} nodes", relative)
    try:
        functions, function_nodes = _function_index(tree)
        main_guard_calls = _guarded_calls(tree)
        existing_parallelism = _existing_parallelism(tree)
        module_import_effects = _module_import_effects(tree)
        dunder_name_rebound = _dunder_name_rebound(tree)
        module_binding_counts = _module_binding_counts(tree)
    except RecursionError:
        return _diagnostic(
            "PYTHON_AST_DEPTH_EXCEEDED",
            "source nesting exceeds the bounded static analyzer depth",
            relative,
        )
    return SourceModule(
        path=path,
        relative_path=relative,
        raw=raw,
        text=text,
        sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        tree=tree,
        ast_nodes=node_count,
        functions=functions,
        function_nodes=function_nodes,
        main_guard_calls=main_guard_calls,
        existing_parallelism=existing_parallelism,
        module_import_effects=module_import_effects,
        dunder_name_rebound=dunder_name_rebound,
        module_binding_counts=module_binding_counts,
        diagnostics=[],
    )


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    result: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            result[child] = parent
    return result


def _ancestors(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.AST]:
    result: list[ast.AST] = []
    current = node
    while current in parents:
        current = parents[current]
        result.append(current)
    return result


def _container_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    for parent in _ancestors(node, parents):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return parent.name
    return None


def _container_function_node(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for parent in _ancestors(node, parents):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return parent
    return None


def _function_declares_external_name(
    node: ast.FunctionDef | ast.AsyncFunctionDef, name: str
) -> bool:
    return name in _function_external_names(node)


def _function_external_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[str]:
    return {
        name
        for child in ast.walk(node)
        if isinstance(child, (ast.Global, ast.Nonlocal))
        for name in child.names
    }


def _name_read_after_statement(
    node: ast.AST,
    name: str,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    scope: ast.AST | None = None
    for parent in _ancestors(node, parents):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            scope = parent
            break
    if scope is None:
        return True
    end_line = getattr(node, "end_lineno", node.lineno)
    for child in ast.walk(scope):
        if not (
            isinstance(child, ast.Name)
            and isinstance(child.ctx, (ast.Load, ast.Del))
            and child.id == name
        ):
            continue
        if getattr(child, "lineno", 0) > end_line:
            return True
        current = child
        while current in parents and parents[current] is not scope:
            current = parents[current]
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return True
    return False


def _spawn_guarded(module: SourceModule, node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    if module.dunder_name_rebound:
        return False
    guards = _top_level_main_guards(module.tree)
    if len(guards) != 1:
        return False
    container_node = _container_function_node(node, parents)
    if container_node is not None:
        facts = module.functions.get(container_node.name)
        guard_lines = _guard_call_lines(module.tree, container_node.name)
        return bool(
            facts
            and module.function_nodes.get(container_node.name) is container_node
            and module.module_binding_counts.get(container_node.name) == 1
            and not facts.is_async
            and not facts.returns_generator
            and len(guard_lines) == 1
        )
    return guards[0] in _ancestors(node, parents)


def _namespace_reflection(tree: ast.AST) -> list[str]:
    evidence: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if name in {"globals", "locals", "eval", "exec"} or (name in {"vars", "dir"} and not node.args):
            evidence.append(f"{name}() at line {node.lineno}")
    return sorted(set(evidence))


def _complex_control(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    complex_types: tuple[type[ast.AST], ...] = (
        ast.Try,
        ast.With,
        ast.AsyncWith,
        ast.Lambda,
        ast.ClassDef,
    )
    try_star = getattr(ast, "TryStar", None)
    if isinstance(try_star, type):
        complex_types = (*complex_types, try_star)
    return any(
        isinstance(parent, complex_types)
        for parent in _ancestors(node, parents)
    )


def _repeated_enclosing_loop(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    return any(
        isinstance(parent, (ast.For, ast.AsyncFor, ast.While))
        for parent in _ancestors(node, parents)
    )


def _static_item_count(node: ast.AST) -> int | None:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return len(node.elts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "range":
        values: list[int] = []
        for arg in node.args:
            if not isinstance(arg, ast.Constant) or not isinstance(arg.value, int):
                return None
            values.append(arg.value)
        if not 1 <= len(values) <= 3:
            return None
        try:
            return len(range(*values))
        except (ValueError, OverflowError):
            return None
    return None


def _safe_iterable(node: ast.AST) -> bool:
    if isinstance(node, (ast.Name, ast.Attribute, ast.List, ast.Tuple, ast.Set)):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"range", "sorted", "reversed", "list", "tuple"}
        and not node.keywords
    )


def _materialized_iterable(node: ast.AST) -> tuple[bool, str]:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return True, f"materialized {type(node).__name__.lower()} literal"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        if node.func.id == "range":
            return True, "range object is replayable and side-effect-free after argument evaluation"
        if (
            node.func.id in {"list", "tuple", "set"}
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Call)
            and isinstance(node.args[0].func, ast.Name)
            and node.args[0].func.id == "range"
            and not node.args[0].keywords
        ):
            return True, f"{node.func.id}(range(...)) is materialized before worker dispatch"
    return False, "iterable may be lazy, stateful, or effectful"


def _direct_materialization_is_static(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_immutable_literal(item) for item in node.elts)
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.keywords:
        return False
    if node.func.id == "range":
        return 1 <= len(node.args) <= 3 and all(
            isinstance(argument, ast.Constant)
            and isinstance(argument.value, int)
            and not isinstance(argument.value, bool)
            for argument in node.args
        )
    return bool(
        node.func.id in {"list", "tuple", "set"}
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Call)
        and isinstance(node.args[0].func, ast.Name)
        and node.args[0].func.id == "range"
        and not node.args[0].keywords
        and 1 <= len(node.args[0].args) <= 3
        and all(
            isinstance(argument, ast.Constant)
            and isinstance(argument.value, int)
            and not isinstance(argument.value, bool)
            for argument in node.args[0].args
        )
    )


def _materialization_builtins(node: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"range", "list", "tuple", "set"}:
            names.add(node.func.id)
        if (
            node.func.id in {"list", "tuple", "set"}
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Call)
            and isinstance(node.args[0].func, ast.Name)
            and node.args[0].func.id == "range"
        ):
            names.add("range")
    return names


def _builtin_unshadowed(
    module: SourceModule,
    statement: ast.AST,
    parents: dict[ast.AST, ast.AST],
    name: str,
) -> bool:
    if module.module_binding_counts.get(name, 0) > 0:
        return False
    container = _container_function_node(statement, parents)
    return container is None or (
        name not in _function_local_names(container)
        and not _function_declares_external_name(container, name)
    )


def _iterable_proof(
    module: SourceModule,
    statement: ast.AST,
    iterable: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> tuple[bool, str]:
    container = _container_function_node(statement, parents)
    iterable_effects, iterable_effect_evidence = _expression_effects(
        module, iterable, container
    )
    if iterable_effects:
        return False, "iterable evaluation has effects: " + ", ".join(
            sorted(iterable_effects)
        )
    shadowed = sorted(
        name
        for name in _materialization_builtins(iterable)
        if not _builtin_unshadowed(module, statement, parents, name)
    )
    if shadowed:
        return False, "materialization builtin is shadowed: " + ", ".join(shadowed)
    proven, evidence = _materialized_iterable(iterable)
    if proven:
        if not _direct_materialization_is_static(iterable):
            return False, (
                "direct iterable evaluation is not limited to static immutable values; "
                + ", ".join(sorted(iterable_effect_evidence)[:4])
            ).rstrip("; ")
        return proven, evidence
    if not isinstance(iterable, ast.Name):
        return False, evidence
    parent = parents.get(statement)
    body = getattr(parent, "body", None)
    if not isinstance(body, list) or statement not in body:
        return False, f"binding for iterable {iterable.id} is outside the analyzed statement list"
    for earlier in reversed(body[: body.index(statement)]):
        target: ast.AST | None = None
        value: ast.AST | None = None
        if isinstance(earlier, ast.Assign) and len(earlier.targets) == 1:
            target, value = earlier.targets[0], earlier.value
        elif isinstance(earlier, ast.AnnAssign):
            target, value = earlier.target, earlier.value
        if isinstance(target, ast.Name) and target.id == iterable.id:
            if value is None:
                return False, f"iterable {iterable.id} has no statically inspectable value"
            shadowed = sorted(
                name
                for name in _materialization_builtins(value)
                if not _builtin_unshadowed(module, statement, parents, name)
            )
            if shadowed:
                return False, "materialization builtin is shadowed: " + ", ".join(shadowed)
            proven, source = _materialized_iterable(value)
            return proven, f"iterable {iterable.id}: {source}"
        bindings = _ModuleBindingCollector()
        bindings.visit(earlier)
        if bindings.counts.get(iterable.id, 0) > 0:
            return False, f"intervening statement may rebind iterable {iterable.id}"
    return False, f"no dominating materialized binding found for iterable {iterable.id}"


def _worker_call(call: ast.AST, loop_name: str) -> str | None:
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return None
    if call.keywords or len(call.args) != 1:
        return None
    if (
        not isinstance(call.args[0], ast.Name)
        or call.args[0].id != loop_name
        or call.func.id == loop_name
    ):
        return None
    return call.func.id


def _earlier_empty_list(loop: ast.For, accumulator: str, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(loop)
    body = getattr(parent, "body", None)
    if not isinstance(body, list) or loop not in body:
        return False
    index = body.index(loop)
    if index == 0:
        return False
    statement = body[index - 1]
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target, value = statement.targets[0], statement.value
    elif isinstance(statement, ast.AnnAssign):
        target, value = statement.target, statement.value
    else:
        return False
    return (
        isinstance(target, ast.Name)
        and target.id == accumulator
        and isinstance(value, ast.List)
        and not value.elts
    )


def _candidate_shape(module: SourceModule, node: ast.AST, parents: dict[ast.AST, ast.AST]) -> dict[str, Any] | None:
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ) or isinstance(node, ast.Return):
        comp = node.value
        if not isinstance(comp, ast.ListComp) or len(comp.generators) != 1:
            return None
        generator = comp.generators[0]
        if generator.ifs or generator.is_async or not isinstance(generator.target, ast.Name):
            return None
        loop_name = generator.target.id
        worker = _worker_call(comp.elt, loop_name)
        if worker is None or not _safe_iterable(generator.iter):
            return None
        return {
            "node": node,
            "pattern": (
                "ordered_list_comprehension_map"
                if isinstance(node, ast.Assign)
                else "ordered_return_list_comprehension_map"
            ),
            "result_name": node.targets[0].id if isinstance(node, ast.Assign) else None,
            "loop_name": loop_name,
            "iterable": generator.iter,
            "worker": worker,
        }
    if isinstance(node, ast.For):
        if node.orelse or len(node.body) != 1 or not isinstance(node.target, ast.Name) or not _safe_iterable(node.iter):
            return None
        statement = node.body[0]
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            return None
        outer = statement.value
        if not isinstance(outer.func, ast.Attribute) or outer.func.attr != "append" or not isinstance(outer.func.value, ast.Name):
            return None
        accumulator = outer.func.value.id
        if len(outer.args) != 1 or outer.keywords:
            return None
        worker = _worker_call(outer.args[0], node.target.id)
        if worker is None or not _earlier_empty_list(node, accumulator, parents):
            return None
        return {
            "node": node,
            "pattern": "ordered_append_map_loop",
            "result_name": accumulator,
            "loop_name": node.target.id,
            "iterable": node.iter,
            "worker": worker,
        }
    return None


def _source_segment(module: SourceModule, node: ast.AST) -> str:
    return ast.get_source_segment(module.text, node) or ast.unparse(node)


def _hotspot_for(
    relative_path: str,
    start_line: int,
    end_line: int,
    hotspots: list[dict[str, Any]],
) -> dict[str, Any] | None:
    matching = [
        item
        for item in hotspots
        if item["path"] == relative_path and start_line <= item["line"] <= end_line
    ]
    if not matching:
        return None
    return max(matching, key=lambda item: (item["wall_seconds"], -item["line"]))


def _benefit_projection(
    hotspot: dict[str, Any] | None,
    static_items: int | None,
    workers: int,
    minimum_seconds: float,
) -> dict[str, Any]:
    if hotspot is None:
        return {
            "kind": "not_estimated",
            "reason": "runtime evidence unavailable; profile the serial region before applying the rewrite",
        }
    serial = hotspot["wall_seconds"]
    items = hotspot.get("item_count") or static_items
    effective_workers = min(workers, items) if isinstance(items, int) and items > 0 else workers
    effective_workers = max(1, effective_workers)
    serial_fraction = 0.15
    startup = 0.12 * effective_workers
    ipc = 0.002 * items if isinstance(items, int) and items > 0 else 0.01 * effective_workers
    parallel = serial * (serial_fraction + (1.0 - serial_fraction) / effective_workers) + startup + ipc
    parallel = max(0.0, parallel)
    savings = max(0.0, serial - parallel)
    speedup = serial / parallel if parallel > 0 else 1.0
    return {
        "kind": "measured_serial_modeled_parallel",
        "serial_wall_seconds": round(serial, 6),
        "projected_parallel_seconds": round(parallel, 6),
        "projected_time_saved_seconds": round(savings, 6),
        "projected_speedup": round(speedup, 6),
        "workers": effective_workers,
        "item_count": items,
        "below_hotspot_threshold": serial < minimum_seconds,
        "model": {
            "serial_fraction": serial_fraction,
            "process_startup_seconds_per_worker": 0.12,
            "ipc_seconds_per_item": 0.002,
            "calibration": "uncalibrated_scenario",
        },
        "warning": "Only the serial duration is observed. The parallel value is an uncalibrated Amdahl/overhead scenario; it is not a bound or calibration and is not a benchmark.",
    }


def _benefit_not_applicable(
    hotspot: dict[str, Any] | None,
    reason: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": "not_applicable_until_safety",
        "reason": reason,
    }
    if hotspot is not None:
        result["observed_serial_wall_seconds"] = hotspot["wall_seconds"]
        if "item_count" in hotspot:
            result["observed_item_count"] = hotspot["item_count"]
    return result


def _unique_bound_name(tree: ast.Module, base: str) -> str:
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    if base not in bound:
        return base
    index = 2
    while f"{base}{index}" in bound:
        index += 1
    return f"{base}{index}"


def _import_insert_line(module: SourceModule) -> int:
    line = 0
    physical_lines = module.text.splitlines()
    if physical_lines and physical_lines[0].startswith("#!"):
        line = 1
    encoding_cookie = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-_.a-zA-Z0-9]+")
    for index, physical_line in enumerate(physical_lines[:2]):
        if encoding_cookie.match(physical_line):
            line = max(line, index + 1)

    tree = module.tree
    body = tree.body
    index = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        line = max(line, getattr(body[0], "end_lineno", body[0].lineno))
        index = 1
    while index < len(body) and isinstance(body[index], ast.ImportFrom) and body[index].module == "__future__":
        line = max(line, getattr(body[index], "end_lineno", body[index].lineno))
        index += 1
    return line


def _standalone_statement(module: SourceModule, node: ast.AST) -> bool:
    if _statement_has_comment(module, node):
        return False
    end_line = getattr(node, "end_lineno", None)
    end_column = getattr(node, "end_col_offset", None)
    if end_line is None or end_column is None:
        return False
    lines = module.text.splitlines()
    if not (1 <= node.lineno <= end_line <= len(lines)):
        return False
    first = lines[node.lineno - 1].encode("utf-8")
    last = lines[end_line - 1].encode("utf-8")
    if node.col_offset > len(first) or end_column > len(last):
        return False
    return not first[: node.col_offset].strip() and not last[end_column:].strip()


def _statement_has_comment(module: SourceModule, node: ast.AST) -> bool:
    end_line = getattr(node, "end_lineno", node.lineno)
    try:
        tokens = tokenize.generate_tokens(io.StringIO(module.text).readline)
        return any(
            token.type == tokenize.COMMENT
            and node.lineno <= token.start[0] <= end_line
            for token in tokens
        )
    except (IndentationError, tokenize.TokenError):
        return True


def _rewrite_preview(module: SourceModule, shape: dict[str, Any], workers: int) -> dict[str, Any] | None:
    node = shape["node"]
    if not module.text.endswith(("\n", "\r\n")) or not _standalone_statement(module, node):
        return None
    lines = module.text.splitlines(keepends=True)
    start = node.lineno - 1
    end = getattr(node, "end_lineno", node.lineno)
    if not (0 <= start < end <= len(lines)):
        return None
    newline = "\r\n" if "\r\n" in module.text else "\n"
    indent_match = re.match(r"[ \t]*", lines[start])
    indent = indent_match.group(0) if indent_match else ""
    alias = _unique_bound_name(module.tree, "_AtomLaneProcessPoolExecutor")
    multiprocessing_alias = _unique_bound_name(module.tree, "_atomlane_multiprocessing")
    iterable = _source_segment(module, shape["iterable"])
    worker = shape["worker"]
    result = shape["result_name"]
    pool_name = _unique_bound_name(module.tree, "_atomlane_pool")
    with_line = (
        f"{indent}with {alias}(max_workers={workers}, "
        f"mp_context={multiprocessing_alias}.get_context(\"spawn\")) as {pool_name}:{newline}"
    )
    if shape["pattern"] == "ordered_list_comprehension_map":
        work_line = f"{indent}    {result} = list({pool_name}.map({worker}, {iterable})){newline}"
    elif shape["pattern"] == "ordered_return_list_comprehension_map":
        work_line = f"{indent}    return list({pool_name}.map({worker}, {iterable})){newline}"
    else:
        work_line = f"{indent}    {result}.extend({pool_name}.map({worker}, {iterable})){newline}"
    transformed = list(lines)
    transformed[start:end] = [with_line, work_line]
    insert_at = _import_insert_line(module)
    import_line = f"from concurrent.futures import ProcessPoolExecutor as {alias}{newline}"
    multiprocessing_import = f"import multiprocessing as {multiprocessing_alias}{newline}"
    insertion = [import_line, multiprocessing_import]
    if insert_at < len(transformed) and transformed[insert_at].strip():
        insertion.append(newline)
    transformed[insert_at:insert_at] = insertion
    transformed_text = "".join(transformed)
    try:
        compile(transformed_text, module.relative_path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return None
    diff = "".join(
        difflib.unified_diff(
            module.text.splitlines(keepends=True),
            transformed_text.splitlines(keepends=True),
            fromfile=f"a/{module.relative_path}",
            tofile=f"b/{module.relative_path}",
        )
    )
    encoded = diff.encode("utf-8")
    if len(encoded) > MAX_DIFF_BYTES:
        return None
    patch_material = f"{module.sha256}\0{shape['pattern']}\0{node.lineno}\0{diff}".encode()
    return {
        "kind": "source_hash_bound_unified_diff",
        "patch_id": "sha256:" + hashlib.sha256(patch_material).hexdigest(),
        "source_sha256": module.sha256,
        "applies_automatically": False,
        "unified_diff": diff,
        "preconditions": [
            "source_sha256 still matches",
            "worker arguments and return values are pickleable",
            "the invoking entrypoint retains a spawn-safe __main__ guard",
            "outer and native worker budgets are coordinated",
            "serial/parallel differential validation passes",
        ],
    }


def _candidate(
    module: SourceModule,
    shape: dict[str, Any],
    parents: dict[ast.AST, ast.AST],
    hotspots: list[dict[str, Any]],
    max_workers: int,
    minimum_seconds: float,
    execution_context: str,
    include_rewrite_previews: bool,
) -> dict[str, Any]:
    node = shape["node"]
    worker = shape["worker"]
    start_line = node.lineno
    end_line = getattr(node, "end_lineno", node.lineno)
    static_items = _static_item_count(shape["iterable"])
    workers = min(max_workers, static_items) if static_items and static_items > 0 else max_workers
    workers = max(1, workers)
    blockers: list[dict[str, str]] = []
    proof: list[dict[str, str]] = []
    workload = "unknown"
    effects: set[str] = {"unknown_worker"}
    evidence: set[str] = set()
    reachable: set[str] = set()

    if worker in module.functions:
        facts = module.functions[worker]
        effects, evidence, reachable = _propagated_effects(module.functions, worker)
        if not effects:
            workload = "pure_python_cpu"
        elif effects <= {"io_read"}:
            workload = "blocking_read_io"
        elif "native_or_gil_releasing" in effects:
            workload = "native_or_gil_releasing"
        elif "network" in effects:
            workload = "network_io"
        elif "subprocess" in effects:
            workload = "subprocess_batch"
        if not facts.top_level:
            blockers.append({"code": "WORKER_NOT_MODULE_LEVEL", "message": "process workers must be module-level"})
        if facts.is_async:
            blockers.append({"code": "ASYNC_WORKER", "message": "async workers require a separate bounded async design"})
        if facts.returns_generator:
            blockers.append({"code": "GENERATOR_WORKER", "message": "generator workers change eager/result semantics"})
        worker_node = module.function_nodes[worker]
        positional = [*worker_node.args.posonlyargs, *worker_node.args.args]
        if (
            len(positional) != 1
            or worker_node.args.vararg is not None
            or worker_node.args.kwonlyargs
            or worker_node.args.kwarg is not None
        ):
            blockers.append(
                {
                    "code": "WORKER_SIGNATURE_UNPROVEN",
                    "message": "ordered map rewrites require one positional parameter and no variadic or keyword-only parameters",
                }
            )
        ambiguous_bindings = sorted(
            symbol
            for symbol in reachable
            if module.module_binding_counts.get(symbol, 0) != 1
        )
        global_dependencies = {
            dependency
            for symbol in reachable
            for dependency in module.functions[symbol].global_reads
        }
        ambiguous_bindings.extend(
            sorted(
                dependency
                for dependency in global_dependencies
                if module.module_binding_counts.get(dependency, 0) > 0
                and dependency not in reachable
            )
        )
        ambiguous_bindings = sorted(set(ambiguous_bindings))
        if ambiguous_bindings:
            blockers.append(
                {
                    "code": "WORKER_BINDING_UNPROVEN",
                    "message": "worker/helper bindings are not unique on every module path: "
                    + ", ".join(ambiguous_bindings[:8]),
                }
            )
        if workload == "pure_python_cpu" and sum(
            module.functions[symbol].cpu_operations for symbol in reachable
        ) == 0:
            workload = "effect_free_unknown_cost"
            blockers.append(
                {
                    "code": "CPU_GRAIN_UNPROVEN",
                    "message": "the bounded AST found no recognized CPU-grain signal; profiling may show work that this heuristic cannot classify",
                }
            )
    else:
        blockers.append({"code": "WORKER_UNRESOLVED", "message": "worker is not a same-module top-level function"})

    spawn_safe = _spawn_guarded(module, node, parents)
    complex_control = _complex_control(node, parents)
    repeated_pool = _repeated_enclosing_loop(node, parents)
    iterable_proven, iterable_evidence = _iterable_proof(module, node, shape["iterable"], parents)
    standalone_statement = _standalone_statement(module, node)
    container_node = _container_function_node(node, parents)
    container_local_names = _function_local_names(container_node) if container_node is not None else set()
    container_external_names = _function_external_names(container_node) if container_node is not None else set()
    if container_node is not None and (
        worker in container_local_names
        or bool(
            container_external_names
            & (
                {
                    dependency
                    for symbol in reachable
                    for dependency in module.functions[symbol].global_reads
                }
                | reachable
                | {worker}
            )
        )
    ):
        blockers.append(
            {
                "code": "WORKER_BINDING_UNPROVEN",
                "message": f"a worker dependency is rebound or declared external in candidate scope {container_node.name}",
            }
        )
    surrounding_scope: ast.AST | None = container_node
    if surrounding_scope is None:
        surrounding_scope = next(
            (
                parent
                for parent in _ancestors(node, parents)
                if isinstance(parent, ast.If) and _main_guard(parent)
            ),
            None,
        )
    surrounding_effects: set[str] = set()
    if surrounding_scope is not None:
        surrounding_effects, surrounding_evidence = _effects_outside_candidate(
            module, surrounding_scope, node
        )
        if surrounding_effects:
            blockers.append(
                {
                    "code": "SURROUNDING_EFFECTS_UNPROVEN",
                    "message": "code surrounding the candidate has observable or unresolved effects: "
                    + ", ".join(sorted(surrounding_effects)[:8]),
                }
            )
            evidence.update(
                f"surrounding scope: {item}" for item in sorted(surrounding_evidence)[:16]
            )
    reflection = _namespace_reflection(module.tree)
    if reflection:
        blockers.append(
            {
                "code": "DYNAMIC_NAMESPACE_REFLECTION",
                "message": "namespace reflection or mutation could observe/change rewrite bindings: "
                + ", ".join(reflection[:6]),
            }
        )
    if reachable:
        if container_node is not None:
            guard_lines = _guard_call_lines(module.tree, container_node.name)
            guard_line = guard_lines[0] if len(guard_lines) == 1 else None
        else:
            guard_line = next(
                (
                    parent.lineno
                    for parent in _ancestors(node, parents)
                    if isinstance(parent, ast.If) and _main_guard(parent)
                ),
                None,
            )
        late_definitions = sorted(
            symbol
            for symbol in reachable
            if guard_line is None
            or getattr(module.function_nodes[symbol], "end_lineno", module.function_nodes[symbol].lineno)
            >= guard_line
        )
        if late_definitions:
            blockers.append(
                {
                    "code": "WORKER_DEFINITION_ORDER_UNPROVEN",
                    "message": "worker/helper definitions do not dominate the guarded invocation: "
                    + ", ".join(late_definitions[:8]),
                }
            )
    if workload == "pure_python_cpu" and not spawn_safe:
        blockers.append(
            {
                "code": "PROCESS_POOL_REQUIRES_MAIN_GUARD",
                "message": "spawn import safety is not proven by an enclosing or caller __main__ guard",
            }
        )
    if workload == "pure_python_cpu" and module.module_import_effects:
        blockers.append(
            {
                "code": "MODULE_IMPORT_EFFECTS",
                "message": "spawned workers may repeat top-level work: " + ", ".join(module.module_import_effects[:6]),
            }
        )
    if complex_control:
        blockers.append(
            {
                "code": "COMPLEX_ENCLOSING_CONTROL",
                "message": "candidate is inside try/with/lambda control whose exception semantics are not transformed",
            }
        )
    if repeated_pool:
        blockers.append(
            {
                "code": "REPEATED_POOL_CREATION",
                "message": "candidate is inside a loop; a rewrite would construct a new process pool per iteration",
            }
        )
    if workload == "pure_python_cpu" and not standalone_statement:
        blockers.append(
            {
                "code": "REWRITE_REQUIRES_STANDALONE_STATEMENT",
                "message": "candidate shares a physical line with outer control, a comment, or another statement",
            }
        )
    if workload == "pure_python_cpu" and not iterable_proven:
        blockers.append(
            {
                "code": "ITERABLE_SEMANTICS_UNPROVEN",
                "message": "process fan-out could change lazy/effectful iterable evaluation: " + iterable_evidence,
            }
        )
    if static_items is not None and static_items < 2:
        blockers.append({"code": "INSUFFICIENT_ITEMS", "message": "fewer than two statically known items"})
    if workers < 2:
        blockers.append(
            {
                "code": "INSUFFICIENT_WORKERS",
                "message": "the resource plan permits fewer than two workers",
            }
        )
    loop_target_live = (
        shape["pattern"] == "ordered_append_map_loop"
        and (
            container_node is None
            or shape["loop_name"] in container_external_names
            or _name_read_after_statement(node, shape["loop_name"], parents)
        )
    )
    if loop_target_live:
        blockers.append(
            {
                "code": "LOOP_TARGET_LIVE_OUT",
                "message": f"loop target {shape['loop_name']} is read after the loop and would change binding semantics",
            }
        )
    if execution_context != "standalone":
        blockers.append(
            {
                "code": "NESTED_PARALLEL_BUDGET_UNPROVEN",
                "message": f"execution context is {execution_context}; outer/native worker budgets must be coordinated",
            }
        )
    hard_effects = effects - {"io_read", "network", "subprocess", "native_or_gil_releasing"}
    for effect in sorted(hard_effects):
        blockers.append({"code": "WORKER_EFFECT_" + effect.upper(), "message": f"worker has {effect} effect"})

    proof.append(
        {
            "id": "ordered_results",
            "status": "satisfied",
            "evidence": "recognized executor.map-compatible shape preserves input result order",
        }
    )
    proof.append(
        {
            "id": "source_rewrite_boundary",
            "status": "satisfied" if standalone_statement else "unsatisfied",
            "evidence": "candidate owns its complete physical source lines" if standalone_statement else "line-based preview would discard neighboring syntax",
        }
    )
    proof.append(
        {
            "id": "loop_control",
            "status": "unsatisfied" if loop_target_live else "satisfied",
            "evidence": (
                "the serial loop target remains live after the candidate"
                if loop_target_live
                else "single map expression with no break, continue, return, yield, await, loop else, or live-out loop target"
            ),
        }
    )
    proof.append(
        {
            "id": "worker_binding",
            "status": (
                "satisfied"
                if reachable
                and not any(
                    item["code"]
                    in {
                        "WORKER_BINDING_UNPROVEN",
                        "WORKER_DEFINITION_ORDER_UNPROVEN",
                        "WORKER_UNRESOLVED",
                    }
                    for item in blockers
                )
                else "unsatisfied"
            ),
            "evidence": (
                "worker and reachable same-module helpers each have one stable, dominating binding"
                if reachable
                else "worker binding could not be resolved"
            ),
        }
    )
    proof.append(
        {
            "id": "explicit_worker_effects",
            "status": "satisfied" if not effects else "unknown" if workload in {"blocking_read_io", "network_io", "subprocess_batch", "native_or_gil_releasing"} else "unsatisfied",
            "evidence": "no explicit effect found in the bounded same-module call graph" if not effects else ", ".join(sorted(effects)),
        }
    )
    proof.append(
        {
            "id": "iterable_evaluation",
            "status": "satisfied" if iterable_proven else "unknown",
            "evidence": iterable_evidence,
        }
    )
    proof.append(
        {
            "id": "runtime_protocols_and_pickling",
            "status": "unknown",
            "evidence": "Python runtime types, magic-method dispatch, callable/argument/result pickling, and third-party import behavior require validation",
        }
    )
    proof.append(
        {
            "id": "spawn_import_safety",
            "status": "satisfied" if spawn_safe else "unknown",
            "evidence": "enclosing/caller __main__ guard found" if spawn_safe else "no statically linked __main__ guard found",
        }
    )

    if module.existing_parallelism:
        classification = "already_parallel"
        recommendation = "coordinate_existing_parallelism"
        blockers.append(
            {
                "code": "EXISTING_PARALLELISM",
                "message": "module already contains parallel/native constructs: " + ", ".join(sorted(module.existing_parallelism)),
            }
        )
    elif hard_effects:
        classification = "blocked"
        recommendation = "keep_serial_until_effects_and_dependencies_are_proven"
    elif workload == "pure_python_cpu" and not blockers:
        classification = "reviewable_rewrite"
        recommendation = "process_pool_ordered_map"
    elif workload == "blocking_read_io":
        classification = "advisory_only"
        recommendation = "thread_pool_after_io_and_exception_review"
    elif workload == "network_io":
        classification = "advisory_only"
        recommendation = "bounded_async_or_thread_pool_after_rate_limit_review"
    elif workload == "subprocess_batch":
        classification = "advisory_only"
        recommendation = "prefer_atomlane_external_map_with_declared_effects"
    elif workload == "native_or_gil_releasing":
        classification = "prefer_native"
        recommendation = "vectorize_or_use_native_workers_without_nested_oversubscription"
    else:
        classification = "blocked"
        recommendation = "keep_serial_until_effects_and_dependencies_are_proven"

    hotspot = _hotspot_for(module.relative_path, start_line, end_line, hotspots)
    benefit = (
        _benefit_projection(hotspot, static_items, workers, minimum_seconds)
        if classification == "reviewable_rewrite"
        else _benefit_not_applicable(
            hotspot,
            "a CPU-process projection is withheld until the candidate clears every safety and grain gate",
        )
    )
    benefit_not_proven = bool(
        benefit.get("below_hotspot_threshold")
        or (
            isinstance(benefit.get("projected_speedup"), (int, float))
            and benefit["projected_speedup"] <= 1.0
        )
    )
    if benefit_not_proven:
        if classification == "reviewable_rewrite":
            classification = "advisory_only"
            blockers.append(
                {
                    "code": "BENEFIT_NOT_PROVEN",
                    "message": "the observed/modelled workload does not clear the configured benefit gate",
                }
            )
        recommendation = "profile_or_keep_serial_below_threshold"

    preview: dict[str, Any] | None = None
    if classification == "reviewable_rewrite" and include_rewrite_previews:
        preview = _rewrite_preview(module, shape, workers)
        if preview is None:
            classification = "advisory_only"
            recommendation = "keep_serial_rewrite_preview_unavailable"
            benefit = _benefit_not_applicable(
                hotspot,
                "a source-safe rewrite preview is unavailable",
            )
            blockers.append(
                {
                    "code": "REWRITE_PREVIEW_FAILED",
                    "message": "a bounded, comment-preserving, syntax-valid patch could not be generated",
                }
            )

    location_material = f"{module.sha256}\0{module.relative_path}\0{start_line}\0{end_line}\0{shape['pattern']}\0{worker}".encode()
    candidate_id = "sha256:" + hashlib.sha256(location_material).hexdigest()
    result = {
        "id": candidate_id,
        "location": {
            "path": module.relative_path,
            "start_line": start_line,
            "end_line": end_line,
            "symbol": _container_function(node, parents) or "<module>",
        },
        "source_sha256": module.sha256,
        "pattern": shape["pattern"],
        "worker": worker,
        "workload": workload,
        "classification": classification,
        "proof_level": "bounded_static_candidate" if classification == "reviewable_rewrite" else "partial" if worker in module.functions else "unknown",
        "confidence": 0.82 if classification == "reviewable_rewrite" else 0.72 if classification in {"advisory_only", "prefer_native"} else 0.35,
        "recommended_executor": "process_pool" if recommendation == "process_pool_ordered_map" else "thread_pool" if workload == "blocking_read_io" else "native_or_external" if workload in {"native_or_gil_releasing", "subprocess_batch"} else "none",
        "recommendation": recommendation,
        "static_item_count": static_items,
        "recommended_worker_ceiling": workers,
        "effects": sorted(effects),
        "evidence": sorted(evidence)[:32],
        "proof_obligations": proof,
        "blockers": sorted(blockers, key=lambda item: (item["code"], item["message"])),
        "benefit": benefit,
        "validation_requirements": [
            "compile the transformed source without importing it",
            "compare serial and parallel return values, output order, exceptions, and produced files",
            "repeat deterministic fixtures under the explicit spawn start method",
            "measure p50/p90 only for safe repeatable workloads and report peak memory",
            "discard the rewrite if correctness fails or measured performance regresses",
        ],
    }
    if preview is not None:
        result["rewrite_preview"] = preview
    return result


def _normalize_hotspots(project: Path, hotspots: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if hotspots is not None and not isinstance(hotspots, list):
        raise AdvisorError("hotspots must be an array")
    if hotspots is not None and len(hotspots) > MAX_HOTSPOTS:
        raise AdvisorError(f"hotspots must contain at most {MAX_HOTSPOTS} entries")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(hotspots or []):
        if not isinstance(item, dict):
            raise AdvisorError(f"hotspots[{index}] must be an object")
        unknown = sorted(set(item) - {"path", "line", "wall_seconds", "item_count"})
        if unknown:
            raise AdvisorError(f"hotspots[{index}] contains unknown fields: {', '.join(unknown)}")
        path_value = item.get("path")
        if not isinstance(path_value, str) or not path_value or len(path_value) > MAX_PATH_TEXT:
            raise AdvisorError(
                f"hotspots[{index}].path must be a non-empty string of at most {MAX_PATH_TEXT} characters"
            )
        path = _regular_project_file(project, Path(path_value))
        line = item.get("line")
        wall = item.get("wall_seconds")
        if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= MAX_ITEM_COUNT:
            raise AdvisorError(f"hotspots[{index}].line must be a positive integer")
        if not _bounded_positive_number(wall, MAX_HOTSPOT_SECONDS):
            raise AdvisorError(f"hotspots[{index}].wall_seconds must be finite and positive")
        normalized_item: dict[str, Any] = {
            "path": path.relative_to(project).as_posix(),
            "line": line,
            "wall_seconds": float(wall),
        }
        item_count = item.get("item_count")
        if item_count is not None:
            if (
                not isinstance(item_count, int)
                or isinstance(item_count, bool)
                or not 1 <= item_count <= MAX_ITEM_COUNT
            ):
                raise AdvisorError(f"hotspots[{index}].item_count must be a positive integer")
            normalized_item["item_count"] = item_count
        normalized.append(normalized_item)
    return sorted(normalized, key=lambda item: (item["path"], item["line"], item["wall_seconds"]))


def analyze_python_parallelism(
    project_path: Path,
    *,
    paths: list[str] | None = None,
    hotspots: list[dict[str, Any]] | None = None,
    max_files: int = 128,
    max_candidates: int = 32,
    max_workers: int = 4,
    minimum_hotspot_seconds: float = 10.0,
    execution_context: str = "standalone",
    include_rewrite_previews: bool = True,
    target_platform: str = "auto",
) -> dict[str, Any]:
    """Analyze project-local Python source and return deterministic advice."""
    try:
        project = project_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AdvisorError(f"project_path does not exist or cannot be resolved: {project_path}") from exc
    if not project.is_dir():
        raise AdvisorError("project_path must be an existing directory")
    if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= MAX_FILES:
        raise AdvisorError(f"max_files must be between 1 and {MAX_FILES}")
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= MAX_CANDIDATES:
        raise AdvisorError(f"max_candidates must be between 1 and {MAX_CANDIDATES}")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 64:
        raise AdvisorError("max_workers must be between 1 and 64")
    if target_platform not in {"auto", "windows", "darwin", "linux"}:
        raise AdvisorError("target_platform must be auto, windows, darwin, or linux")
    resolved_platform = (
        platform.system().lower() if target_platform == "auto" else target_platform
    )
    effective_max_workers = min(max_workers, 61) if resolved_platform == "windows" else max_workers
    if (
        isinstance(minimum_hotspot_seconds, bool)
        or not isinstance(minimum_hotspot_seconds, (int, float))
        or not _bounded_positive_number(minimum_hotspot_seconds, MAX_HOTSPOT_SECONDS)
    ):
        raise AdvisorError("minimum_hotspot_seconds must be finite and positive")
    minimum_hotspot_seconds = float(minimum_hotspot_seconds)
    if execution_context not in {"standalone", "atomlane_worker", "native_parallel", "unknown"}:
        raise AdvisorError("invalid execution_context")
    if paths is not None and (
        not isinstance(paths, list)
        or not paths
        or len(paths) > MAX_REQUESTED_PATHS
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > MAX_PATH_TEXT
            for item in paths
        )
    ):
        raise AdvisorError(
            f"paths must contain 1..{MAX_REQUESTED_PATHS} non-empty strings of at most {MAX_PATH_TEXT} characters"
        )
    if not isinstance(include_rewrite_previews, bool):
        raise AdvisorError("include_rewrite_previews must be a boolean")

    normalized_hotspots = _normalize_hotspots(project, hotspots)
    source_paths, discovery_truncated = _resolve_sources(project, paths, max_files)
    diagnostics: list[dict[str, Any]] = []
    if effective_max_workers < max_workers:
        diagnostics.append(
            _diagnostic(
                "WINDOWS_PROCESS_POOL_LIMIT",
                "ProcessPoolExecutor is capped at 61 workers on Windows; the rewrite ceiling was reduced",
            )
        )
    modules: list[SourceModule] = []
    total_bytes = 0
    total_ast_nodes = 0
    for source_path in source_paths:
        loaded = _load_module(project, source_path)
        if isinstance(loaded, dict):
            diagnostics.append(loaded)
            continue
        if total_bytes + len(loaded.raw) > MAX_TOTAL_SOURCE_BYTES:
            diagnostics.append(
                _diagnostic(
                    "PYTHON_TOTAL_SOURCE_BUDGET",
                    f"aggregate source exceeds {MAX_TOTAL_SOURCE_BYTES} bytes; remaining files skipped",
                    loaded.relative_path,
                )
            )
            break
        if total_ast_nodes + loaded.ast_nodes > MAX_TOTAL_AST_NODES:
            diagnostics.append(
                _diagnostic(
                    "PYTHON_TOTAL_AST_BUDGET",
                    f"aggregate AST exceeds {MAX_TOTAL_AST_NODES} nodes; remaining files skipped",
                    loaded.relative_path,
                )
            )
            break
        total_bytes += len(loaded.raw)
        total_ast_nodes += loaded.ast_nodes
        modules.append(loaded)

    candidates: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []
    candidates_truncated = False
    for module in modules:
        if module.existing_parallelism:
            existing.append(
                {
                    "path": module.relative_path,
                    "classification": "already_parallel",
                    "markers": sorted(module.existing_parallelism),
                    "recommendation": "coordinate inner workers with AtomLane and native library budgets",
                }
            )
        parents = _parent_map(module.tree)
        nodes = sorted(
            (node for node in ast.walk(module.tree) if isinstance(node, (ast.Assign, ast.For, ast.Return))),
            key=lambda node: (node.lineno, getattr(node, "col_offset", 0), type(node).__name__),
        )
        for node in nodes:
            shape = _candidate_shape(module, node, parents)
            if shape is None:
                continue
            if len(candidates) >= max_candidates:
                candidates_truncated = True
                break
            candidates.append(
                _candidate(
                    module,
                    shape,
                    parents,
                    normalized_hotspots,
                    effective_max_workers,
                    minimum_hotspot_seconds,
                    execution_context,
                    include_rewrite_previews,
                )
            )
        if candidates_truncated:
            break
    if candidates_truncated:
        diagnostics.append(
            _diagnostic(
                "PYTHON_CANDIDATE_LIMIT",
                f"candidate output truncated at max_candidates={max_candidates}",
            )
        )

    candidates.sort(
        key=lambda item: (
            item["location"]["path"],
            item["location"]["start_line"],
            item["pattern"],
            item["id"],
        )
    )
    diagnostics = sorted(
        {json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in diagnostics}.values(),
        key=lambda item: (item.get("path", ""), item.get("line", 0), item["code"], item["message"]),
    )[:MAX_DIAGNOSTICS]
    snapshots = [
        {
            "path": module.relative_path,
            "sha256": module.sha256,
            "size_bytes": len(module.raw),
            "ast_nodes": module.ast_nodes,
        }
        for module in sorted(modules, key=lambda item: item.relative_path)
    ]
    counts = {
        status: sum(1 for item in candidates if item["classification"] == status)
        for status in ("reviewable_rewrite", "advisory_only", "blocked", "prefer_native", "already_parallel")
    }
    semantic = {
        "analysis_version": ANALYSIS_VERSION,
        "analysis_mode": "static_non_executing",
        "execution_performed": False,
        "files_modified": False,
        "options": {
            "paths": sorted(path.relative_to(project).as_posix() for path in source_paths) if paths else [],
            "max_files": max_files,
            "max_candidates": max_candidates,
            "max_workers": max_workers,
            "effective_max_workers": effective_max_workers,
            "target_platform": resolved_platform,
            "minimum_hotspot_seconds": minimum_hotspot_seconds,
            "execution_context": execution_context,
            "include_rewrite_previews": include_rewrite_previews,
        },
        "snapshots": snapshots,
        "hotspots": normalized_hotspots,
        "candidates": candidates,
        "existing_parallelism": sorted(existing, key=lambda item: item["path"]),
        "diagnostics": diagnostics,
        "summary": {
            "files_discovered": len(source_paths),
            "files_analyzed": len(modules),
            "source_bytes_analyzed": total_bytes,
            "ast_nodes_analyzed": total_ast_nodes,
            "discovery_truncated": discovery_truncated,
            "candidate_count": len(candidates),
            "candidates_truncated": candidates_truncated,
            "classification_counts": counts,
            "recommended_next_step": (
                "Review source-hash-bound rewrites, then run semantic differential tests and a measured benchmark."
                if counts["reviewable_rewrite"]
                else "Collect runtime evidence or resolve blockers; keep the current program serial meanwhile."
            ),
        },
        "limitations": [
            "Static analysis does not prove runtime picklability, import purity, memory fit, or performance.",
            "Only same-module ordered map comprehensions and append loops are eligible for rewrite previews in this version.",
            "Unknown calls and observable effects fail closed; advisory classifications are not safety proofs.",
            "A rewrite preview never authorizes applying or executing code and is invalid after its source hash changes.",
        ],
    }
    encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    semantic["analysis_hash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return semantic


__all__ = ["ANALYSIS_VERSION", "AdvisorError", "analyze_python_parallelism"]
