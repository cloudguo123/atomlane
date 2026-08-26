#!/usr/bin/env python3
"""Bounded, non-executing Python CLI artifact inference.

The analyzer recognizes literal argparse path defaults and verifies how the
selected command uses ``args.<dest>`` at AST-level read/write sinks. It never
imports the target module, evaluates annotations/defaults, or executes project
code. Unsupported or ambiguous constructs simply produce no access fact.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_PYTHON_SOURCE_BYTES = 2_000_000
MAX_AST_NODES = 80_000


@dataclass(frozen=True)
class ArgumentSpec:
    command: str | None
    dest: str
    option: str
    default: str


def _call_name(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id.lower()
    if isinstance(function, ast.Attribute):
        return function.attr.lower()
    return ""


def _literal_path(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Call)
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.func, (ast.Name, ast.Attribute))
    ):
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        if name in {"Path", "PurePath", "PosixPath"}:
            return _literal_path(node.args[0])
    return None


def _receiver_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        return call.func.value.id
    return None


def _parser_specs(tree: ast.AST) -> list[ArgumentSpec]:
    parser_commands: dict[str, str | None] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if _call_name(call) == "add_parser" and call.args:
            command = call.args[0]
            if isinstance(command, ast.Constant) and isinstance(command.value, str):
                parser_commands[target.id] = command.value
        elif _call_name(call) in {"argumentparser", "add_subparsers"}:
            parser_commands.setdefault(target.id, None)

    specs: list[ArgumentSpec] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "add_argument":
            continue
        receiver = _receiver_name(node)
        command = parser_commands.get(receiver)
        option = next(
            (
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and arg.value.startswith("--")
            ),
            None,
        )
        if option is None:
            continue
        keywords = {item.arg: item.value for item in node.keywords if item.arg}
        default = _literal_path(keywords.get("default"))
        if default is None:
            continue
        dest_node = keywords.get("dest")
        dest = (
            dest_node.value
            if isinstance(dest_node, ast.Constant) and isinstance(dest_node.value, str)
            else option.removeprefix("--").replace("-", "_")
        )
        specs.append(ArgumentSpec(command, dest, option, default))
    return specs


def _selected_command(arguments: list[str], known: set[str]) -> str | None:
    for value in arguments:
        if value in known:
            return value
        if value == "--":
            break
    return None


def _literal_overrides(arguments: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value.startswith("--") and "=" in value:
            option, candidate = value.split("=", 1)
            if candidate:
                overrides[option] = candidate
        elif value.startswith("--") and index + 1 < len(arguments):
            candidate = arguments[index + 1]
            if candidate and not candidate.startswith("-"):
                overrides[value] = candidate
                index += 1
        index += 1
    return overrides


def _branch_function_names(main: ast.FunctionDef | ast.AsyncFunctionDef, command: str) -> set[str]:
    result: set[str] = set()
    def command_value(node: ast.AST) -> str | None:
        if not isinstance(node, ast.Compare):
            return None
        if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq) or len(node.comparators) != 1:
            return None
        left = node.left
        right = node.comparators[0]
        if (
            isinstance(left, ast.Attribute)
            and isinstance(left.value, ast.Name)
            and left.value.id == "args"
            and left.attr == "command"
            and isinstance(right, ast.Constant)
            and isinstance(right.value, str)
        ):
            return right.value
        return None

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If):
            compared = command_value(node.test)
            if compared is not None:
                for statement in (node.body if compared == command else node.orelse):
                    visit(statement)
                return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            result.add(node.func.id)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in main.body:
        visit(statement)
    return result


def _selected_functions(tree: ast.Module, command: str | None) -> list[ast.AST]:
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    main = functions.get("main")
    if main is None:
        return []
    selected_names: set[str] = {"main"}
    frontier = _branch_function_names(main, command) if command else {
        call.func.id
        for call in ast.walk(main)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in functions
    }
    selected_names.update(frontier)
    # Follow only calls originating in command-selected functions. Avoid
    # traversing all branches of main when a subcommand is known.
    visited: set[str] = set()
    while frontier:
        name = frontier.pop()
        if name in visited or name not in functions:
            continue
        visited.add(name)
        for call in ast.walk(functions[name]):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in functions
                and call.func.id not in selected_names
            ):
                selected_names.add(call.func.id)
                frontier.add(call.func.id)
    return [functions[name] for name in sorted(selected_names) if name in functions]


def _call_mode(call: ast.Call) -> str | None:
    name = _call_name(call)
    if name == "open":
        mode = None
        if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
            mode = call.args[1].value
        for keyword in call.keywords:
            if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                mode = keyword.value.value
        if isinstance(mode, str) and any(marker in mode for marker in "wax+"):
            return "write"
        return "read"
    if name in {
        "write_text", "write_bytes", "write_rows", "writerows", "write_row",
        "save", "savefig", "savetxt", "to_csv", "to_json", "dump", "dumps_to",
        "export", "emit", "replace", "rename", "touch", "unlink", "mkdir",
    } or name.startswith(("write_", "save_", "export_", "emit_")):
        return "write"
    if name in {
        "read_text", "read_bytes", "read", "read_rows", "load", "loads", "loadtxt",
        "from_csv", "exists", "stat", "sha256", "checksum", "digest", "hash_file",
    } or name.startswith(("read_", "load_", "parse_", "hash_", "checksum_")):
        return "read"
    return None


def _destinations(call: ast.Call) -> set[str]:
    destinations: set[str] = set()
    for node in ast.walk(call):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "args"
        ):
            destinations.add(node.attr)
    return destinations


def infer_python_cli_accesses(
    script: Path,
    arguments: list[str],
    cwd: Path,
) -> dict[str, Any]:
    """Return exact AST-backed path accesses for one literal Python CLI."""
    script = script.resolve(strict=False)
    try:
        raw = script.read_bytes()
    except OSError as exc:
        return {"accesses": [], "diagnostics": [f"cannot read Python CLI source: {exc}"]}
    if len(raw) > MAX_PYTHON_SOURCE_BYTES:
        return {"accesses": [], "diagnostics": ["Python CLI source exceeds static-analysis budget"]}
    try:
        text = raw.decode("utf-8", errors="strict")
        tree = ast.parse(text, filename=str(script), type_comments=False)
    except (UnicodeDecodeError, SyntaxError) as exc:
        return {"accesses": [], "diagnostics": [f"cannot parse Python CLI source: {exc}"]}
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        return {"accesses": [], "diagnostics": ["Python CLI AST exceeds static-analysis budget"]}

    specs = _parser_specs(tree)
    known_commands = {item.command for item in specs if item.command is not None}
    command = _selected_command(arguments, {item for item in known_commands if item is not None})
    relevant = [item for item in specs if item.command is None or item.command == command]
    overrides = _literal_overrides(arguments)
    functions = _selected_functions(tree, command)
    modes: dict[str, set[str]] = {}
    for function in functions:
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            mode = _call_mode(node)
            if mode is None:
                continue
            for dest in _destinations(node):
                modes.setdefault(dest, set()).add(mode)

    accesses: list[dict[str, str]] = []
    evidence: list[dict[str, Any]] = []
    for spec in relevant:
        observed = modes.get(spec.dest, set())
        if not observed:
            continue
        value = overrides.get(spec.option, spec.default)
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = cwd / path
        if observed == {"read"}:
            access_mode = "read"
        elif observed == {"write"}:
            access_mode = "overwrite"
        else:
            access_mode = "transaction"
        item = {"resource": str(path.resolve(strict=False)), "mode": access_mode}
        if item not in accesses:
            accesses.append(item)
        evidence.append(
            {
                "dest": spec.dest,
                "option": spec.option,
                "command": command,
                "path": item["resource"],
                "mode": access_mode,
                "origin": "literal argparse default + selected AST sink",
            }
        )
    return {
        "accesses": sorted(accesses, key=lambda item: (item["resource"], item["mode"])),
        "evidence": sorted(evidence, key=lambda item: (item["path"], item["dest"])),
        "diagnostics": [],
        "source": str(script),
    }
